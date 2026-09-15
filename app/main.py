"""Точка входа приложения."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.db import get_sessionmaker
from app.payments.registry import get_providers
from app.routes import admin, public, webhooks
from app.security import RateLimiter
from app.services.orders import expire_stale_orders
from app.services.telegram import outbox_worker
from app.templating import templates

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
EXPIRY_INTERVAL_SECONDS = 60


async def _expiry_worker(sessionmaker, stop_event: asyncio.Event) -> None:
    """Периодически закрывает заказы, у которых вышел срок ожидания оплаты."""
    while not stop_event.is_set():
        try:
            async with sessionmaker() as session:
                expired = await expire_stale_orders(session)
                await session.commit()
                if expired:
                    logger.info("просрочено заказов: %s", expired)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("expiry worker: ошибка цикла")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop_event.wait(), timeout=EXPIRY_INTERVAL_SECONDS)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    sessionmaker = get_sessionmaker()

    app.state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(30.0), follow_redirects=False
    )
    app.state.buy_limiter = RateLimiter(settings.buy_rate_limit_per_minute)
    app.state.stop_event = asyncio.Event()

    providers = get_providers()
    logger.info("включённые платёжные провайдеры: %s", ", ".join(providers) or "нет")
    if not providers:
        logger.warning("ни один платёжный провайдер не включён — оплатить заказ будет нельзя")
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        logger.warning("Telegram не настроен — уведомления копятся в outbox_messages")

    tasks = [
        asyncio.create_task(outbox_worker(sessionmaker, app.state.stop_event)),
        asyncio.create_task(_expiry_worker(sessionmaker, app.state.stop_event)),
    ]
    try:
        yield
    finally:
        app.state.stop_event.set()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await app.state.http_client.aclose()


def create_app() -> FastAPI:
    settings = get_settings()
    logging.basicConfig(
        level=logging.DEBUG if settings.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    application = FastAPI(
        title=settings.site_name,
        lifespan=lifespan,
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    application.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    application.include_router(public.router)
    application.include_router(webhooks.router)
    application.include_router(admin.router)

    if settings.dummy_enabled:
        from app.routes import dummy

        application.include_router(dummy.router)

    @application.exception_handler(404)
    async def not_found_handler(request: Request, exc: Exception) -> Response:
        if request.url.path.startswith(("/api/", "/webhooks/")):
            return JSONResponse({"error": "not_found"}, status_code=404)
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)

    return application


app = create_app()

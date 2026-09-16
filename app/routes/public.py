"""Публичные страницы: оплата, статус заказа, возврат с платёжной формы."""

from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import CONTENT_DIR, Settings, get_settings
from app.db import get_session
from app.models import Order, OrderStatus
from app.payments.base import PaymentRequest, PaymentStatus, ProviderError
from app.payments.registry import get_providers
from app.security import CSRF_COOKIE, CSRF_FIELD, issue_csrf_token, validate_csrf_token
from app.services import orders as order_service
from app.services.content import render_markdown_file
from app.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter()


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _payment_options(settings: Settings) -> list[dict[str, str]]:
    return [
        {"slug": provider.slug, "title": provider.title, "hint": provider.hint}
        for provider in get_providers().values()
    ]


def _form_state(customer_ref: str, email: str, provider: str, ref_confirmed: str) -> dict:
    """Что вернуть в форму, если ввод не прошёл проверку."""
    return {
        "customer_ref": customer_ref,
        "email": email,
        "provider": provider,
        "ref_confirmed": bool(ref_confirmed),
    }


def _render_index(
    request: Request,
    settings: Settings,
    *,
    errors: dict[str, str] | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    token = issue_csrf_token(settings.secret_key)
    response = templates.TemplateResponse(
        request,
        "index.html",
        {
            "instructions_html": render_markdown_file(settings.instructions_path),
            "providers": _payment_options(settings),
            "errors": errors or {},
            "form": form or {},
            "csrf_token": token,
            "csrf_field": CSRF_FIELD,
        },
        status_code=status_code,
    )
    response.set_cookie(
        CSRF_COOKIE, token, httponly=True, samesite="lax", secure=request.url.scheme == "https"
    )
    return response


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, settings: Settings = Depends(get_settings)) -> HTMLResponse:
    return _render_index(request, settings)


@router.post("/buy")
async def buy(
    request: Request,
    customer_ref: str = Form(default=""),
    email: str = Form(default=""),
    provider: str = Form(default=""),
    ref_confirmed: str = Form(default=""),
    csrf_token: str = Form(default="", alias=CSRF_FIELD),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    limiter = request.app.state.buy_limiter
    if not limiter.allow(client_ip(request) or "unknown"):
        return _render_index(
            request,
            settings,
            errors={"__all__": "Слишком много попыток. Подождите минуту и повторите."},
            form=_form_state(customer_ref, email, provider, ref_confirmed),
            status_code=429,
        )

    if not validate_csrf_token(settings.secret_key, csrf_token, request.cookies.get(CSRF_COOKIE)):
        return _render_index(
            request,
            settings,
            errors={"__all__": "Форма устарела. Проверьте данные и отправьте ещё раз."},
            form=_form_state(customer_ref, email, provider, ref_confirmed),
            status_code=400,
        )

    errors: dict[str, str] = {}
    customer_ref_value = ""
    email_value: str | None = None

    try:
        customer_ref_value = order_service.validate_customer_ref(customer_ref, settings)
    except order_service.ValidationProblem as problem:
        errors[problem.field] = problem.message

    if settings.collect_email:
        try:
            email_value = order_service.validate_email(email, settings)
        except order_service.ValidationProblem as problem:
            errors[problem.field] = problem.message

    # Атрибут required в разметке — подсказка браузеру, не защита: проверяем на сервере.
    if settings.require_ref_confirmation and not ref_confirmed:
        errors["ref_confirmed"] = (
            f"Подтвердите, что {settings.customer_field_label} указан верно."
        )

    providers = get_providers()
    payment_provider = providers.get(provider)
    if payment_provider is None:
        errors["provider"] = "Выберите способ оплаты."

    if errors:
        return _render_index(
            request,
            settings,
            errors=errors,
            form=_form_state(customer_ref, email, provider, ref_confirmed),
            status_code=400,
        )

    order = await order_service.create_order(
        session,
        settings=settings,
        customer_ref=customer_ref_value,
        customer_email=email_value,
        client_ip=client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    payment = await order_service.create_payment_attempt(
        session, order=order, provider_slug=payment_provider.slug
    )

    payment_request = PaymentRequest(
        payment_id=payment.id,
        order_id=order.id,
        invoice_no=payment.invoice_no,
        public_code=order.public_code,
        amount=order.amount,
        currency=order.currency,
        description=f"{order.product_title} (заказ {order.public_code})",
        return_url=settings.url(f"/return/{payment.id}"),
        fail_url=settings.url(f"/fail/{payment.id}"),
        webhook_url=settings.url(f"/webhooks/{payment_provider.slug}"),
        customer_ref=order.customer_ref,
        customer_email=order.customer_email,
    )

    try:
        target = await payment_provider.create_payment(
            payment_request, request.app.state.http_client
        )
    except (ProviderError, httpx.HTTPError):
        logger.exception("не удалось создать платёж в %s", payment_provider.slug)
        await session.rollback()
        return _render_index(
            request,
            settings,
            errors={
                "__all__": "Платёжный сервис временно недоступен. "
                "Попробуйте другой способ оплаты или повторите позже."
            },
            form=_form_state(customer_ref, email, provider, ref_confirmed),
            status_code=502,
        )

    payment.provider_payment_id = target.provider_payment_id
    payment.confirmation_url = target.url
    payment.payload = {"created": target.raw}
    await order_service.mark_pending(session, order=order, payment=payment)
    await session.commit()

    if target.method.upper() == "POST":
        return templates.TemplateResponse(
            request,
            "redirect_form.html",
            {"action": target.url, "fields": target.fields},
        )
    return RedirectResponse(target.url, status_code=303)


@router.get("/order/{order_id}", response_class=HTMLResponse)
async def order_page(
    request: Request,
    order_id: uuid.UUID,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await order_service.get_order(session, order_id)
    if order is None:
        return templates.TemplateResponse(
            request, "not_found.html", {}, status_code=404
        )
    return templates.TemplateResponse(
        request,
        "order.html",
        {
            "order": order,
            "after_payment_html": render_markdown_file(settings.after_payment_path),
            "poll": order.status in (OrderStatus.created.value, OrderStatus.pending.value),
        },
    )


@router.get("/api/orders/{order_id}/status")
async def order_status(
    order_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> JSONResponse:
    order = await order_service.get_order(session, order_id)
    if order is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return JSONResponse(
        {
            "status": order.status,
            "paid": order.is_paid,
            "public_code": order.public_code,
            "paid_at": order.paid_at.isoformat() if order.paid_at else None,
        }
    )


async def _sync_payment(
    session: AsyncSession, payment_id: uuid.UUID, app_state
) -> Order | None:
    """Сверка статуса при возврате покупателя.

    Редирект возврата сам по себе ничего не доказывает, поэтому здесь мы не
    доверяем его параметрам, а спрашиваем агрегатор по API. Это нужно на случай,
    когда вебхук задержался, — покупатель не должен видеть «не оплачено».
    """
    payment = await order_service.get_payment(session, payment_id)
    if payment is None:
        return None

    provider = get_providers().get(payment.provider)
    if provider is None or not payment.provider_payment_id:
        return await order_service.get_order(session, payment.order_id)

    try:
        status = await provider.fetch_status(payment.provider_payment_id, app_state.http_client)
    except (ProviderError, httpx.HTTPError):
        logger.warning("сверка статуса %s не удалась", payment.id, exc_info=True)
        return await order_service.get_order(session, payment.order_id)

    if status in (PaymentStatus.succeeded, PaymentStatus.failed, PaymentStatus.canceled):
        newly_paid = await order_service.apply_payment_status(
            session, payment=payment, status=status
        )
        if newly_paid:
            from app.services.telegram import enqueue_order_paid

            order = await order_service.get_order(session, payment.order_id)
            if order is not None:
                await enqueue_order_paid(
                    session, order=order, payment=payment, settings=get_settings()
                )
        await session.commit()

    return await order_service.get_order(session, payment.order_id)


@router.get("/return/{payment_id}")
async def payment_return(
    request: Request,
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await _sync_payment(session, payment_id, request.app.state)
    if order is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return RedirectResponse(f"/order/{order.id}", status_code=303)


@router.get("/fail/{payment_id}")
async def payment_fail(
    request: Request,
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await _sync_payment(session, payment_id, request.app.state)
    if order is None:
        return templates.TemplateResponse(request, "not_found.html", {}, status_code=404)
    return RedirectResponse(f"/order/{order.id}", status_code=303)


@router.get("/healthz")
async def healthz() -> JSONResponse:
    return JSONResponse({"status": "ok"})


# ------------------------------------------------------------------ документы
# Страницы описаны явными маршрутами, а не одним /{page}: catch-all перехватывал
# бы /admin и /webhooks, которые подключаются позже.
LEGAL_PAGES = {
    "offer": "Публичная оферта",
    "privacy": "Политика конфиденциальности",
    "refund": "Условия возврата",
    "contacts": "Контакты",
}


def _legal_response(request: Request, page: str) -> Response:
    return templates.TemplateResponse(
        request,
        "legal.html",
        {
            "title": LEGAL_PAGES[page],
            "body_html": render_markdown_file(CONTENT_DIR / "legal" / f"{page}.md"),
        },
    )


@router.get("/offer", response_class=HTMLResponse)
async def offer(request: Request) -> Response:
    return _legal_response(request, "offer")


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request) -> Response:
    return _legal_response(request, "privacy")


@router.get("/refund", response_class=HTMLResponse)
async def refund(request: Request) -> Response:
    return _legal_response(request, "refund")


@router.get("/contacts", response_class=HTMLResponse)
async def contacts(request: Request) -> Response:
    return _legal_response(request, "contacts")

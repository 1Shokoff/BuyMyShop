"""Приём уведомлений от платёжных агрегаторов.

Единственное место, где заказ признаётся оплаченным. Порядок проверок:

  1. агрегатор известен и включён;
  2. IP-адрес отправителя в разрешённом диапазоне (там, где агрегатор его публикует);
  3. подпись уведомления сходится (или, если агрегатор не подписывает —
     статус подтверждается отдельным запросом к его API);
  4. уведомление ещё не обработано (ключ идемпотентности в журнале событий);
  5. сумма платежа совпадает с суммой заказа.

Только после этого заказ переходит в paid и ставится уведомление в Telegram.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import WebhookEvent
from app.payments.base import PaymentStatus, SignatureError
from app.payments.registry import get_providers
from app.payments.yookassa import ip_allowed
from app.services import orders as order_service
from app.services.telegram import enqueue_order_paid

logger = logging.getLogger(__name__)
router = APIRouter()

MAX_STORED_BODY = 20_000
STORED_HEADERS = (
    "content-type",
    "user-agent",
    "x-forwarded-for",
    "x-api-sha256-signature",
    "signature",
)


async def _log_event(
    session: AsyncSession,
    *,
    provider: str,
    event_key: str | None,
    signature_ok: bool,
    remote_ip: str | None,
    headers: dict[str, str],
    body: bytes,
    response_status: int,
    error: str | None = None,
) -> bool:
    """Пишет событие в журнал. False — событие с таким ключом уже обработано."""
    stmt = (
        pg_insert(WebhookEvent)
        .values(
            provider=provider,
            event_key=event_key,
            signature_ok=signature_ok,
            remote_ip=remote_ip,
            headers={k: v for k, v in headers.items() if k in STORED_HEADERS},
            body=body.decode("utf-8", errors="replace")[:MAX_STORED_BODY],
            response_status=response_status,
            error=error,
        )
        .on_conflict_do_nothing(index_elements=["provider", "event_key"])
        .returning(WebhookEvent.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none() is not None


@router.post("/webhooks/{provider_slug}")
@router.get("/webhooks/{provider_slug}")
async def receive_webhook(
    request: Request,
    provider_slug: str,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    provider = get_providers().get(provider_slug)
    if provider is None:
        return PlainTextResponse("unknown provider", status_code=404)

    body = await request.body()
    headers = {key.lower(): value for key, value in request.headers.items()}
    query = dict(request.query_params)
    remote_ip = request.client.host if request.client else None

    if provider.allowed_webhook_networks and not ip_allowed(
        remote_ip, provider.allowed_webhook_networks
    ):
        logger.warning("webhook %s: отклонён IP %s", provider_slug, remote_ip)
        await _log_event(
            session,
            provider=provider_slug,
            event_key=None,
            signature_ok=False,
            remote_ip=remote_ip,
            headers=headers,
            body=body,
            response_status=403,
            error="ip not allowed",
        )
        await session.commit()
        return PlainTextResponse("forbidden", status_code=403)

    try:
        result = await provider.parse_webhook(headers=headers, body=body, query=query)
    except SignatureError as exc:
        logger.warning("webhook %s: %s", provider_slug, exc)
        await _log_event(
            session,
            provider=provider_slug,
            event_key=None,
            signature_ok=False,
            remote_ip=remote_ip,
            headers=headers,
            body=body,
            response_status=400,
            error=str(exc)[:1000],
        )
        await session.commit()
        return PlainTextResponse("bad signature", status_code=400)

    # Платёж ищем до записи ключа идемпотентности. Иначе ненайденное событие
    # осталось бы помеченным как обработанное, и повторная доставка получила бы
    # 200 — агрегатор перестал бы повторять, а мы так ничего и не сделали бы.
    payment = await order_service.find_payment(
        session,
        provider=provider_slug,
        payment_id=result.payment_id,
        invoice_no=result.invoice_no,
        provider_payment_id=result.provider_payment_id,
    )
    if payment is None:
        logger.error(
            "webhook %s: платёж не найден (payment_id=%s invoice_no=%s provider_id=%s)",
            provider_slug,
            result.payment_id,
            result.invoice_no,
            result.provider_payment_id,
        )
        # event_key=None: запись остаётся в журнале для разбора, но не блокирует
        # повторную попытку агрегатора.
        await _log_event(
            session,
            provider=provider_slug,
            event_key=None,
            signature_ok=True,
            remote_ip=remote_ip,
            headers=headers,
            body=body,
            response_status=404,
            error="payment not found",
        )
        await session.commit()
        return PlainTextResponse("payment not found", status_code=404)

    is_new = await _log_event(
        session,
        provider=provider_slug,
        event_key=result.event_key,
        signature_ok=True,
        remote_ip=remote_ip,
        headers=headers,
        body=body,
        response_status=200,
    )
    if not is_new:
        # Повторная доставка того же события: заказ уже в нужном состоянии.
        await session.commit()
        logger.info("webhook %s: повтор события %s пропущен", provider_slug, result.event_key)
        return _provider_response(result)

    status = result.status
    if provider.confirm_via_api and status is PaymentStatus.succeeded:
        # Агрегатор не подписывает уведомления либо подпись — не единственная
        # гарантия: спрашиваем статус напрямую, прежде чем признать оплату.
        provider_payment_id = result.provider_payment_id or payment.provider_payment_id
        try:
            status = await provider.fetch_status(
                provider_payment_id, request.app.state.http_client
            )
        except Exception:
            logger.exception("webhook %s: сверка статуса не удалась", provider_slug)
            await session.rollback()
            # 5xx — агрегатор повторит доставку позже.
            return PlainTextResponse("status check failed", status_code=503)

    newly_paid = await order_service.apply_payment_status(
        session,
        payment=payment,
        status=status,
        amount=result.amount,
        provider_payment_id=result.provider_payment_id,
        raw=result.raw,
    )
    if newly_paid:
        order = await order_service.get_order(session, payment.order_id)
        if order is not None:
            await enqueue_order_paid(session, order=order, payment=payment, settings=settings)

    await session.commit()
    return _provider_response(result)


def _provider_response(result) -> Response:
    return PlainTextResponse(
        result.response_body, status_code=200, media_type=result.response_media_type
    )

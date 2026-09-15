"""Доставка уведомлений в Telegram через таблицу-outbox.

Прямая отправка из обработчика вебхука недопустима: если Telegram в этот момент
недоступен, номер покупателя будет потерян, а агрегатору мы уже ответили «OK».
Поэтому в транзакции с переводом заказа в paid пишется строка в outbox, а
фоновый воркер доставляет её с повторами и экспоненциальной задержкой.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models import Order, OutboxMessage, Payment
from app.payments.registry import get_providers

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://api.telegram.org/bot{token}/sendMessage"
BATCH_SIZE = 10
MAX_BACKOFF_SECONDS = 3600


def _local_time(moment: datetime, settings: Settings) -> str:
    try:
        tz = ZoneInfo(settings.display_timezone)
    except Exception:  # noqa: BLE001 — некорректная зона не должна ронять уведомление
        tz = UTC
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz).strftime("%d.%m.%Y %H:%M:%S")


def render_paid_message(order: Order, payment: Payment, settings: Settings) -> str:
    providers = get_providers()
    provider = providers.get(payment.provider)
    provider_title = provider.title if provider else payment.provider
    paid_at = order.paid_at or datetime.now(UTC)

    esc = html.escape
    lines = [
        f"✅ <b>Оплачен заказ {esc(order.public_code)}</b>",
        "",
        f"<b>{esc(settings.customer_field_label)}:</b> <code>{esc(order.customer_ref)}</code>",
        f"Товар: {esc(order.product_title)}",
        f"Сумма: {order.amount} {esc(order.currency)}",
        f"Агрегатор: {esc(provider_title)}",
    ]
    if payment.provider_payment_id:
        lines.append(f"ID платежа: <code>{esc(str(payment.provider_payment_id))}</code>")
    if order.customer_email:
        lines.append(f"E-mail: {esc(order.customer_email)}")
    lines.append(f"Время: {_local_time(paid_at, settings)} ({esc(settings.display_timezone)})")
    lines.append("")
    lines.append(f"Заказ: {esc(settings.url('/order/' + str(order.id)))}")
    return "\n".join(lines)


async def enqueue_order_paid(
    session: AsyncSession, *, order: Order, payment: Payment, settings: Settings
) -> OutboxMessage:
    message = OutboxMessage(
        kind="telegram",
        order_id=order.id,
        payload={
            "chat_id": settings.telegram_chat_id,
            "text": render_paid_message(order, payment, settings),
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        next_attempt_at=datetime.now(UTC),
    )
    session.add(message)
    await session.flush()
    return message


async def _send(client: httpx.AsyncClient, settings: Settings, payload: dict) -> None:
    if not settings.telegram_bot_token or not payload.get("chat_id"):
        raise RuntimeError("не настроены TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")

    response = await client.post(
        API_TEMPLATE.format(token=settings.telegram_bot_token), json=payload, timeout=20.0
    )
    if response.status_code == 429:
        retry_after = (response.json().get("parameters") or {}).get("retry_after", 30)
        raise _RetryAfter(int(retry_after))
    if response.status_code >= 400:
        raise RuntimeError(f"Telegram {response.status_code}: {response.text[:300]}")


class _RetryAfter(RuntimeError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"429, повтор через {seconds} c")
        self.seconds = seconds


async def process_outbox_once(
    session: AsyncSession, client: httpx.AsyncClient, settings: Settings
) -> int:
    """Обрабатывает одну пачку сообщений. Возвращает число успешно отправленных."""
    now = datetime.now(UTC)
    stmt = (
        select(OutboxMessage)
        .where(
            OutboxMessage.sent_at.is_(None),
            OutboxMessage.failed_at.is_(None),
            OutboxMessage.next_attempt_at <= now,
        )
        .order_by(OutboxMessage.next_attempt_at)
        .limit(BATCH_SIZE)
        .with_for_update(skip_locked=True)
    )
    messages = list((await session.execute(stmt)).scalars())
    sent = 0

    for message in messages:
        message.attempts += 1
        try:
            await _send(client, settings, message.payload)
        except _RetryAfter as exc:
            message.last_error = str(exc)
            message.next_attempt_at = now + timedelta(seconds=exc.seconds)
        except Exception as exc:  # noqa: BLE001 — ошибка доставки не должна ронять воркер
            message.last_error = f"{type(exc).__name__}: {exc}"[:2000]
            if message.attempts >= settings.telegram_max_attempts:
                message.failed_at = now
                logger.error(
                    "outbox %s: доставка провалена после %s попыток: %s",
                    message.id,
                    message.attempts,
                    message.last_error,
                )
            else:
                delay = min(5 * 2 ** (message.attempts - 1), MAX_BACKOFF_SECONDS)
                message.next_attempt_at = now + timedelta(seconds=delay)
                logger.warning("outbox %s: попытка %s не удалась (%s)", message.id, message.attempts, message.last_error)
        else:
            message.sent_at = now
            message.last_error = None
            sent += 1

    await session.commit()
    return sent


async def outbox_worker(sessionmaker, stop_event: asyncio.Event) -> None:
    """Фоновая задача приложения: доставляет накопившиеся уведомления."""
    settings = get_settings()
    async with httpx.AsyncClient() as client:
        while not stop_event.is_set():
            try:
                async with sessionmaker() as session:
                    await process_outbox_once(session, client, settings)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("outbox worker: непредвиденная ошибка цикла")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=settings.outbox_poll_seconds)
            except TimeoutError:
                continue

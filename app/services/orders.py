"""Жизненный цикл заказа.

Ключевые инварианты:
  * заказ переходит в paid ровно один раз — переход делается одним UPDATE
    с условием по текущему статусу, поэтому повторный вебхук ничего не меняет
    и уведомление в Telegram не задваивается;
  * сумма из уведомления сверяется с суммой заказа: оплата «не на ту сумму»
    не переводит заказ в paid;
  * источник истины по оплате — подписанный вебхук и/или ответ API агрегатора,
    но никогда не редирект возврата покупателя.
"""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Sequence, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import Order, OrderStatus, Payment, PaymentState
from app.payments.base import PaymentStatus

logger = logging.getLogger(__name__)

PUBLIC_CODE_ALPHABET = "ACDEFGHJKLMNPQRTUVWXY3469"
PUBLIC_CODE_LENGTH = 8

_PAYMENT_STATE_BY_STATUS = {
    PaymentStatus.pending: PaymentState.pending,
    PaymentStatus.succeeded: PaymentState.succeeded,
    PaymentStatus.failed: PaymentState.failed,
    PaymentStatus.canceled: PaymentState.canceled,
}


class ValidationProblem(ValueError):
    """Ошибка ввода, которую надо показать покупателю."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


def normalize_customer_ref(raw: str) -> str:
    """Убираем пробелы и разделители: покупатели копируют номер как угодно."""
    return re.sub(r"[\s\-_]+", "", raw or "").strip()


def validate_customer_ref(raw: str, settings: Settings) -> str:
    value = normalize_customer_ref(raw)
    if not value:
        raise ValidationProblem("customer_ref", "Поле обязательно для заполнения.")
    if not re.fullmatch(settings.customer_field_pattern, value):
        raise ValidationProblem("customer_ref", settings.customer_field_error)
    return value


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


def validate_email(raw: str, settings: Settings) -> str | None:
    value = (raw or "").strip()
    if not value:
        if settings.require_email:
            raise ValidationProblem("email", "Укажите e-mail — на него придёт чек.")
        return None
    if not EMAIL_RE.match(value) or len(value) > 255:
        raise ValidationProblem("email", "Проверьте адрес электронной почты.")
    return value.lower()


def generate_public_code() -> str:
    return "".join(secrets.choice(PUBLIC_CODE_ALPHABET) for _ in range(PUBLIC_CODE_LENGTH))


async def create_order(
    session: AsyncSession,
    *,
    settings: Settings,
    customer_ref: str,
    customer_email: str | None,
    client_ip: str | None,
    user_agent: str | None,
) -> Order:
    now = datetime.now(UTC)
    for _ in range(5):
        order = Order(
            public_code=generate_public_code(),
            product_code=settings.product_code,
            product_title=settings.product_title,
            amount=Decimal(settings.product_price),
            currency=settings.product_currency,
            customer_ref=customer_ref,
            customer_email=customer_email,
            status=OrderStatus.created.value,
            client_ip=client_ip,
            user_agent=(user_agent or "")[:512] or None,
            expires_at=now + timedelta(minutes=settings.order_ttl_minutes),
        )
        session.add(order)
        try:
            await session.flush()
        except IntegrityError:
            # Коллизия короткого кода — пробуем другой.
            await session.rollback()
            continue
        return order
    raise RuntimeError("не удалось сгенерировать уникальный код заказа")


# Номер счёта берём из сиквенса отдельным запросом, а не через server_default:
# при генерации на стороне БД значение не возвращается в объект, и любое обращение
# к payment.invoice_no в async-сессии уходит в синхронную догрузку (MissingGreenlet).
INVOICE_SEQUENCE = Sequence("payments_invoice_no_seq")


async def create_payment_attempt(
    session: AsyncSession, *, order: Order, provider_slug: str
) -> Payment:
    invoice_no = await session.scalar(select(INVOICE_SEQUENCE.next_value()))
    payment = Payment(
        invoice_no=invoice_no,
        order_id=order.id,
        provider=provider_slug,
        state=PaymentState.created.value,
        amount=order.amount,
        currency=order.currency,
        payload={},
    )
    session.add(payment)
    await session.flush()
    return payment


async def get_order(session: AsyncSession, order_id: uuid.UUID) -> Order | None:
    return await session.get(Order, order_id)


async def get_payment(session: AsyncSession, payment_id: uuid.UUID) -> Payment | None:
    return await session.get(Payment, payment_id)


async def find_payment(
    session: AsyncSession,
    *,
    provider: str,
    payment_id: uuid.UUID | None = None,
    invoice_no: int | None = None,
    provider_payment_id: str | None = None,
) -> Payment | None:
    """Ищем нашу попытку оплаты по любому из идентификаторов, которые вернул агрегатор."""
    if payment_id is not None:
        payment = await session.get(Payment, payment_id)
        if payment is not None and payment.provider == provider:
            return payment
    if invoice_no is not None:
        stmt = select(Payment).where(Payment.invoice_no == invoice_no, Payment.provider == provider)
        payment = (await session.execute(stmt)).scalar_one_or_none()
        if payment is not None:
            return payment
    if provider_payment_id:
        stmt = select(Payment).where(
            Payment.provider == provider, Payment.provider_payment_id == provider_payment_id
        )
        payment = (await session.execute(stmt)).scalar_one_or_none()
        if payment is not None:
            return payment
    return None


def amounts_match(expected: Decimal, actual: Decimal | None) -> bool:
    """Оплата на другую сумму не считается оплатой заказа."""
    if actual is None:
        return True  # агрегатор не прислал сумму — сверять нечем
    return Decimal(expected).quantize(Decimal("0.01")) == Decimal(actual).quantize(Decimal("0.01"))


async def apply_payment_status(
    session: AsyncSession,
    *,
    payment: Payment,
    status: PaymentStatus,
    amount: Decimal | None = None,
    provider_payment_id: str | None = None,
    raw: dict | None = None,
) -> bool:
    """Применить статус к попытке оплаты. Возвращает True, если заказ впервые стал оплаченным."""
    now = datetime.now(UTC)

    if provider_payment_id and not payment.provider_payment_id:
        payment.provider_payment_id = provider_payment_id
    if raw:
        payment.payload = {**(payment.payload or {}), "last_event": raw}

    if status is PaymentStatus.succeeded and not amounts_match(payment.amount, amount):
        logger.error(
            "payment %s: сумма из уведомления (%s) не совпала с суммой заказа (%s)",
            payment.id,
            amount,
            payment.amount,
        )
        payment.state = PaymentState.failed.value
        payment.payload = {**(payment.payload or {}), "amount_mismatch": str(amount)}
        await session.flush()
        return False

    new_state = _PAYMENT_STATE_BY_STATUS.get(status)
    if new_state is not None:
        payment.state = new_state.value
    await session.flush()

    if status is not PaymentStatus.succeeded:
        if status in (PaymentStatus.failed, PaymentStatus.canceled):
            await session.execute(
                update(Order)
                .where(Order.id == payment.order_id, Order.status == OrderStatus.pending.value)
                .values(status=OrderStatus.failed.value, updated_at=now)
                .execution_options(synchronize_session=False)
            )
        return False

    # Единственный переход в paid: условие по текущему статусу делает операцию
    # идемпотентной без блокировок и гонок между параллельными вебхуками.
    result = await session.execute(
        update(Order)
        .where(Order.id == payment.order_id, Order.status != OrderStatus.paid.value)
        .values(status=OrderStatus.paid.value, paid_at=now, updated_at=now)
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    )
    return result.scalar_one_or_none() is not None


async def mark_pending(session: AsyncSession, *, order: Order, payment: Payment) -> None:
    payment.state = PaymentState.pending.value
    await session.execute(
        update(Order)
        .where(Order.id == order.id, Order.status == OrderStatus.created.value)
        .values(status=OrderStatus.pending.value)
        .execution_options(synchronize_session=False)
    )
    await session.flush()


async def expire_stale_orders(session: AsyncSession) -> int:
    """Помечает неоплаченные заказы, у которых вышел срок ожидания."""
    now = datetime.now(UTC)
    result = await session.execute(
        update(Order)
        .where(
            Order.status.in_([OrderStatus.created.value, OrderStatus.pending.value]),
            Order.expires_at < now,
        )
        .values(status=OrderStatus.expired.value, updated_at=now)
        .returning(Order.id)
        .execution_options(synchronize_session=False)
    )
    return len(result.fetchall())

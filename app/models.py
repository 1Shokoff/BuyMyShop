"""Модели данных.

Статусы хранятся строками, а не native enum PostgreSQL: добавление нового
состояния тогда не требует ALTER TYPE, а валидация всё равно делается в коде.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class OrderStatus(str, enum.Enum):
    created = "created"       # заказ создан, покупатель ещё не ушёл на оплату
    pending = "pending"       # покупатель отправлен на платёжную форму
    paid = "paid"             # оплата подтверждена вебхуком и сверкой по API
    failed = "failed"         # агрегатор сообщил об отказе
    expired = "expired"       # истёк срок ожидания оплаты


class PaymentState(str, enum.Enum):
    created = "created"
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"


TERMINAL_PAYMENT_STATES = {PaymentState.succeeded, PaymentState.failed, PaymentState.canceled}


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Короткий человекочитаемый код для поддержки и для сообщений в Telegram.
    public_code: Mapped[str] = mapped_column(String(16), unique=True, index=True)

    product_code: Mapped[str] = mapped_column(String(64))
    product_title: Mapped[str] = mapped_column(String(255))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))

    # То самое 15-значное число, введённое покупателем (хранится нормализованным).
    customer_ref: Mapped[str] = mapped_column(String(64), index=True)
    customer_email: Mapped[str | None] = mapped_column(String(255), nullable=True)

    status: Mapped[str] = mapped_column(String(16), default=OrderStatus.created.value, index=True)

    client_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    payments: Mapped[list[Payment]] = relationship(
        back_populates="order", lazy="selectin", order_by="Payment.created_at"
    )

    __table_args__ = (Index("ix_orders_status_created_at", "status", "created_at"),)

    @property
    def is_paid(self) -> bool:
        return self.status == OrderStatus.paid.value


class Payment(Base):
    """Одна попытка оплаты. У заказа их может быть несколько (сменил агрегатор)."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Целочисленный номер счёта: Robokassa (InvId) и часть агрегаторов не принимают UUID.
    # Значение берётся из сиквенса явным запросом в services.orders — см. INVOICE_SEQUENCE.
    invoice_no: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)

    order_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(32), index=True)
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    state: Mapped[str] = mapped_column(String(16), default=PaymentState.created.value, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3))

    confirmation_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    order: Mapped[Order] = relationship(back_populates="payments")

    __table_args__ = (
        UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment_id"),
    )


class WebhookEvent(Base):
    """Журнал входящих уведомлений: аудит + защита от повторной обработки."""

    __tablename__ = "webhook_events"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(32), index=True)
    # Ключ идемпотентности от агрегатора (id платежа + статус). NULL, если не удалось извлечь.
    event_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    signature_ok: Mapped[bool] = mapped_column(default=False)
    remote_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    headers: Mapped[dict] = mapped_column(JSONB, default=dict)
    body: Mapped[str] = mapped_column(Text, default="")
    response_status: Mapped[int] = mapped_column(Integer, default=200)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Индекс по времени нужен для разбора инцидентов и для чистки старых событий.
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )

    __table_args__ = (
        UniqueConstraint("provider", "event_key", name="uq_webhook_events_provider_event_key"),
    )


class OutboxMessage(Base):
    """Исходящие уведомления (Telegram) с гарантией доставки и ретраями."""

    __tablename__ = "outbox_messages"

    id: Mapped[uuid.UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    kind: Mapped[str] = mapped_column(String(32), default="telegram")
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("orders.id", ondelete="SET NULL"), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

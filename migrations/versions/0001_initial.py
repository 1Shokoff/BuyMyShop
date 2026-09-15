"""Начальная схема: заказы, платежи, журнал вебхуков, outbox уведомлений.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("public_code", sa.String(16), nullable=False),
        sa.Column("product_code", sa.String(64), nullable=False),
        sa.Column("product_title", sa.String(255), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("customer_ref", sa.String(64), nullable=False),
        sa.Column("customer_email", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="created"),
        sa.Column("client_ip", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(512), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_orders_public_code", "orders", ["public_code"], unique=True)
    op.create_index("ix_orders_customer_ref", "orders", ["customer_ref"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_status_created_at", "orders", ["status", "created_at"])

    op.execute("CREATE SEQUENCE IF NOT EXISTS payments_invoice_no_seq START WITH 1000")

    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "invoice_no",
            sa.BigInteger(),
            nullable=False,
            server_default=sa.text("nextval('payments_invoice_no_seq')"),
        ),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("provider_payment_id", sa.String(128), nullable=True),
        sa.Column("state", sa.String(16), nullable=False, server_default="created"),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("confirmation_url", sa.Text(), nullable=True),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "provider_payment_id", name="uq_payments_provider_payment_id"),
    )
    op.create_index("ix_payments_invoice_no", "payments", ["invoice_no"], unique=True)
    op.create_index("ix_payments_order_id", "payments", ["order_id"])
    op.create_index("ix_payments_provider", "payments", ["provider"])
    op.create_index("ix_payments_state", "payments", ["state"])

    op.create_table(
        "webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("event_key", sa.String(180), nullable=True),
        sa.Column("signature_ok", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("remote_ip", sa.String(64), nullable=True),
        sa.Column("headers", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("response_status", sa.Integer(), nullable=False, server_default="200"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("provider", "event_key", name="uq_webhook_events_provider_event_key"),
    )
    op.create_index("ix_webhook_events_provider", "webhook_events", ["provider"])
    op.create_index("ix_webhook_events_received_at", "webhook_events", ["received_at"])

    op.create_table(
        "outbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("kind", sa.String(32), nullable=False, server_default="telegram"),
        sa.Column(
            "order_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("orders.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_outbox_messages_order_id", "outbox_messages", ["order_id"])
    op.create_index("ix_outbox_messages_next_attempt_at", "outbox_messages", ["next_attempt_at"])


def downgrade() -> None:
    op.drop_table("outbox_messages")
    op.drop_table("webhook_events")
    op.drop_table("payments")
    op.execute("DROP SEQUENCE IF EXISTS payments_invoice_no_seq")
    op.drop_table("orders")

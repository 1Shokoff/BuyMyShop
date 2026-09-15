import uuid
from datetime import UTC, datetime
from decimal import Decimal

from app.config import Settings
from app.models import Order, Payment
from app.services.telegram import render_paid_message


def build(customer_ref: str = "123456789012345") -> tuple[Order, Payment, Settings]:
    order = Order(
        id=uuid.uuid4(),
        public_code="M4XFUWUU",
        product_code="main",
        product_title="Тестовый товар",
        amount=Decimal("990.00"),
        currency="RUB",
        customer_ref=customer_ref,
        customer_email="buyer@example.com",
        status="paid",
        paid_at=datetime(2026, 9, 15, 22, 26, 9, tzinfo=UTC),
        expires_at=datetime(2026, 9, 15, 23, 0, tzinfo=UTC),
    )
    payment = Payment(
        id=uuid.uuid4(),
        invoice_no=1003,
        order_id=order.id,
        provider="robokassa",
        provider_payment_id="1003",
        state="succeeded",
        amount=order.amount,
        currency=order.currency,
    )
    settings = Settings(
        display_timezone="Asia/Magadan",
        customer_field_label="Номер аккаунта",
        public_base_url="https://example.com",
    )
    return order, payment, settings


def test_message_contains_order_essentials():
    order, payment, settings = build()
    text = render_paid_message(order, payment, settings)

    assert "M4XFUWUU" in text
    assert "<code>123456789012345</code>" in text
    assert "990.00 RUB" in text
    assert f"https://example.com/order/{order.id}" in text


def test_time_is_rendered_in_configured_timezone():
    order, payment, settings = build()
    # 22:26 UTC 15 сентября = 09:26 16 сентября в Asia/Magadan (UTC+11).
    assert "16.09.2026 09:26:09 (Asia/Magadan)" in render_paid_message(order, payment, settings)


def test_unknown_timezone_does_not_break_notification():
    order, payment, settings = build()
    settings = settings.model_copy(update={"display_timezone": "Нет/Такой"})
    assert "Оплачен заказ" in render_paid_message(order, payment, settings)


def test_user_input_is_html_escaped():
    # Номер приходит от покупателя и уходит в сообщение с parse_mode=HTML.
    order, payment, settings = build(customer_ref="<b>1234</b>")
    text = render_paid_message(order, payment, settings)
    assert "&lt;b&gt;1234&lt;/b&gt;" in text
    assert "<code><b>1234</b></code>" not in text

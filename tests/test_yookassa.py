import json
import uuid
from decimal import Decimal

import pytest

from app.payments.base import PaymentStatus, SignatureError
from app.payments.yookassa import YooKassaProvider, ip_allowed


def provider() -> YooKassaProvider:
    return YooKassaProvider(shop_id="123", secret_key="secret")


def test_confirm_via_api_is_required():
    # ЮKassa не подписывает уведомления, поэтому статус обязан перепроверяться по API.
    assert provider().confirm_via_api is True


async def test_webhook_parses_succeeded_event():
    payment_uuid = uuid.uuid4()
    body = json.dumps(
        {
            "type": "notification",
            "event": "payment.succeeded",
            "object": {
                "id": "2c8c0a1b-000f-5000-9000-1b68e7f15d3a",
                "status": "succeeded",
                "amount": {"value": "990.00", "currency": "RUB"},
                "metadata": {"payment_id": str(payment_uuid)},
            },
        }
    ).encode()

    result = await provider().parse_webhook(headers={}, body=body, query={})

    assert result.status is PaymentStatus.succeeded
    assert result.payment_id == payment_uuid
    assert result.amount == Decimal("990.00")
    assert result.provider_payment_id == "2c8c0a1b-000f-5000-9000-1b68e7f15d3a"


async def test_webhook_parses_canceled_event():
    body = json.dumps(
        {"event": "payment.canceled", "object": {"id": "x", "status": "canceled"}}
    ).encode()
    result = await provider().parse_webhook(headers={}, body=body, query={})
    assert result.status is PaymentStatus.canceled


async def test_webhook_rejects_non_json():
    with pytest.raises(SignatureError):
        await provider().parse_webhook(headers={}, body=b"<html>", query={})


def test_ip_allowed_filters_outsiders():
    networks = ("185.71.76.0/27", "77.75.156.11/32")
    assert ip_allowed("185.71.76.5", networks) is True
    assert ip_allowed("77.75.156.11", networks) is True
    assert ip_allowed("8.8.8.8", networks) is False
    assert ip_allowed(None, networks) is False
    assert ip_allowed("не-адрес", networks) is False
    # Пустой список сетей означает «проверка отключена».
    assert ip_allowed("8.8.8.8", ()) is True

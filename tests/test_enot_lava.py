import hashlib
import hmac
import json
import uuid

import pytest

from app.payments.base import PaymentStatus, SignatureError
from app.payments.enot import EnotProvider, canonical_json, sign_payload
from app.payments.lava import LavaProvider, sign_body

SECRET = "additional-key"


def test_canonical_json_sorts_keys_recursively():
    data = {"b": 1, "a": {"z": 1, "y": [{"q": 1, "p": 2}]}}
    assert canonical_json(data) == '{"a":{"y":[{"p":2,"q":1}],"z":1},"b":1}'


def test_canonical_json_keeps_unicode_unescaped():
    assert canonical_json({"comment": "Заказ"}) == '{"comment":"Заказ"}'


async def test_enot_webhook_accepts_valid_signature():
    payment_uuid = uuid.uuid4()
    payload = {
        "invoice_id": "inv-1",
        "order_id": str(payment_uuid),
        "amount": "990.00",
        "currency": "RUB",
        "status": "success",
    }
    body = json.dumps(payload, ensure_ascii=False).encode()
    provider = EnotProvider(shop_id="s", secret_key="k", additional_key=SECRET)

    result = await provider.parse_webhook(
        headers={"x-api-sha256-signature": sign_payload(payload, SECRET)}, body=body, query={}
    )

    assert result.status is PaymentStatus.succeeded
    assert result.payment_id == payment_uuid


async def test_enot_webhook_rejects_wrong_signature():
    payload = {"invoice_id": "inv-1", "status": "success"}
    provider = EnotProvider(shop_id="s", secret_key="k", additional_key=SECRET)
    with pytest.raises(SignatureError):
        await provider.parse_webhook(
            headers={"x-api-sha256-signature": "deadbeef"},
            body=json.dumps(payload).encode(),
            query={},
        )


def test_lava_sign_body_is_hmac_sha256_over_raw_bytes():
    body = b'{"sum":"990.00"}'
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert sign_body(body, SECRET) == expected


async def test_lava_webhook_rejects_missing_signature():
    provider = LavaProvider(api_key="k", shop_id="s", webhook_secret=SECRET)
    with pytest.raises(SignatureError):
        await provider.parse_webhook(headers={}, body=b"{}", query={})

import hashlib
import uuid
from decimal import Decimal

import pytest

from app.payments.base import PaymentStatus, SignatureError
from app.payments.robokassa import (
    RobokassaProvider,
    build_result_signature,
    build_start_signature,
)

MERCHANT = "myshop"
PASS1 = "pass-one"
PASS2 = "pass-two"


def provider() -> RobokassaProvider:
    return RobokassaProvider(
        merchant_login=MERCHANT, password1=PASS1, password2=PASS2, algorithm="md5"
    )


def test_start_signature_matches_documented_formula():
    payment_uuid = uuid.uuid4()
    shp = {"shp_payment": payment_uuid.hex}
    expected = hashlib.md5(
        f"{MERCHANT}:990.00:1001:{PASS1}:shp_payment={payment_uuid.hex}".encode()
    ).hexdigest()
    assert (
        build_start_signature(
            merchant_login=MERCHANT, out_sum="990.00", inv_id=1001, password1=PASS1, shp=shp
        )
        == expected
    )


def test_shp_params_are_sorted_in_signature():
    shp = {"shp_b": "2", "shp_a": "1"}
    expected = hashlib.md5(f"100.00:5:{PASS2}:shp_a=1:shp_b=2".encode()).hexdigest()
    assert build_result_signature(out_sum="100.00", inv_id=5, password2=PASS2, shp=shp) == expected


async def test_webhook_accepts_valid_signature():
    payment_uuid = uuid.uuid4()
    shp = {"shp_payment": payment_uuid.hex}
    signature = build_result_signature(
        out_sum="990.00", inv_id=1001, password2=PASS2, shp=shp
    )
    body = (
        f"OutSum=990.00&InvId=1001&SignatureValue={signature}&shp_payment={payment_uuid.hex}"
    ).encode()

    result = await provider().parse_webhook(headers={}, body=body, query={})

    assert result.status is PaymentStatus.succeeded
    assert result.payment_id == payment_uuid
    assert result.invoice_no == 1001
    assert result.amount == Decimal("990.00")
    # Robokassa считает уведомление доставленным только при таком ответе.
    assert result.response_body == "OK1001"


async def test_webhook_rejects_tampered_amount():
    signature = build_result_signature(out_sum="990.00", inv_id=1001, password2=PASS2, shp={})
    # Злоумышленник меняет сумму, подпись остаётся от исходной.
    body = f"OutSum=1.00&InvId=1001&SignatureValue={signature}".encode()

    with pytest.raises(SignatureError):
        await provider().parse_webhook(headers={}, body=body, query={})


async def test_webhook_rejects_missing_signature():
    with pytest.raises(SignatureError):
        await provider().parse_webhook(headers={}, body=b"OutSum=1.00&InvId=1", query={})

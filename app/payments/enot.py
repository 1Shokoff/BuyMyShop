"""Enot.io.

ВНИМАНИЕ. Как и адаптер Lava, написан по общим сведениям: docs.enot.io закрыт
egress-политикой среды. Перед включением сверить:

  1. URL создания счёта и имена полей;
  2. имя заголовка с подписью вебхука (сейчас x-api-sha256-signature);
  3. правило канонизации тела перед HMAC (сейчас: JSON, ключи отсортированы
     по алфавиту рекурсивно, без экранирования unicode и слэшей).

По умолчанию выключен (ENOT_ENABLED=false).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from app.payments.base import (
    PaymentProvider,
    PaymentRequest,
    PaymentStatus,
    ProviderError,
    RedirectTarget,
    SignatureError,
    WebhookResult,
    format_amount,
)

INVOICE_CREATE_URL = "https://api.enot.io/invoice/create"
INVOICE_INFO_URL = "https://api.enot.io/invoice/info"
SIGNATURE_HEADER = "x-api-sha256-signature"

STATUS_MAP = {
    "success": PaymentStatus.succeeded,
    "paid": PaymentStatus.succeeded,
    "fail": PaymentStatus.failed,
    "failed": PaymentStatus.failed,
    "expired": PaymentStatus.canceled,
    "canceled": PaymentStatus.canceled,
    "created": PaymentStatus.pending,
    "pending": PaymentStatus.pending,
}


def canonical_json(data: Any) -> str:
    """Тело, приведённое к канону: ключи отсортированы на всех уровнях."""
    return json.dumps(_sort_keys(data), ensure_ascii=False, separators=(",", ":"))


def _sort_keys(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _sort_keys(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_sort_keys(item) for item in value]
    return value


def sign_payload(data: Any, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), canonical_json(data).encode("utf-8"), hashlib.sha256
    ).hexdigest()


class EnotProvider(PaymentProvider):
    slug = "enot"
    title = "Enot.io"
    hint = "Карты, СБП, криптовалюта"
    confirm_via_api = True

    def __init__(self, *, shop_id: str, secret_key: str, additional_key: str) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.additional_key = additional_key

    async def create_payment(self, req: PaymentRequest, client: httpx.AsyncClient) -> RedirectTarget:
        payload = {
            "amount": float(format_amount(req.amount)),
            "order_id": str(req.payment_id),
            "currency": req.currency,
            "shop_id": self.shop_id,
            "hook_url": req.webhook_url,
            "success_url": req.return_url,
            "fail_url": req.fail_url,
            "expire": 30,
            "comment": req.description[:255],
            "custom_fields": {"public_code": req.public_code},
        }
        if req.customer_email:
            payload["email"] = req.customer_email

        response = await client.post(
            INVOICE_CREATE_URL,
            json=payload,
            headers={"x-api-key": self.secret_key, "Content-Type": "application/json"},
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"Enot: {response.status_code} {response.text[:500]}")

        data = response.json()
        invoice = data.get("data") or {}
        url = invoice.get("url")
        if not url:
            raise ProviderError(f"Enot: в ответе нет ссылки на оплату: {data}")

        return RedirectTarget(url=url, provider_payment_id=invoice.get("id"), raw=data)

    async def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str],
    ) -> WebhookResult:
        received = (headers.get(SIGNATURE_HEADER) or "").strip().lower()
        if not received:
            raise SignatureError("Enot: в уведомлении нет заголовка с подписью")

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignatureError("Enot: тело уведомления не является JSON") from exc

        expected = sign_payload(data, self.additional_key)
        if not hmac.compare_digest(expected, received):
            raise SignatureError("Enot: подпись уведомления не совпала")

        status = STATUS_MAP.get(str(data.get("status", "")).lower(), PaymentStatus.unknown)
        invoice_id = data.get("invoice_id") or data.get("id")
        try:
            amount = Decimal(str(data.get("amount")))
        except (InvalidOperation, TypeError):
            amount = None

        return WebhookResult(
            status=status,
            payment_id=_parse_uuid(data.get("order_id")),
            provider_payment_id=str(invoice_id) if invoice_id else None,
            amount=amount,
            currency=data.get("currency"),
            event_key=f"{invoice_id}:{data.get('status')}",
            raw=data,
            response_body="OK",
        )

    async def fetch_status(
        self, provider_payment_id: str, client: httpx.AsyncClient
    ) -> PaymentStatus:
        response = await client.get(
            INVOICE_INFO_URL,
            params={"invoice_id": provider_payment_id, "shop_id": self.shop_id},
            headers={"x-api-key": self.secret_key},
            timeout=20.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"Enot: статус {response.status_code} {response.text[:300]}")
        data = (response.json() or {}).get("data") or {}
        return STATUS_MAP.get(str(data.get("status", "")).lower(), PaymentStatus.unknown)


def _parse_uuid(value: object) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None

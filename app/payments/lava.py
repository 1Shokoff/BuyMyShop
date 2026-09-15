"""Lava Business (lava.ru).

ВНИМАНИЕ. Адаптер написан по общим сведениям об API: документация dev.lava.ru
недоступна из среды, где писался код (egress-политика). Перед включением в бой
надо сверить с личным кабинетом три вещи и, если надо, поправить константы ниже:

  1. URL создания счёта и точные имена полей запроса;
  2. формулу подписи запроса (сейчас: HMAC-SHA256 от тела JSON, заголовок Signature);
  3. формулу и заголовок подписи вебхука.

Провайдер по умолчанию выключен (LAVA_ENABLED=false). Формулы подписей вынесены
в отдельные функции и покрыты тестами, поэтому правка — одно место.

Выбран именно Lava Business, а не lava.top: lava.top выставляет счёт по заранее
созданному в их кабинете offerId с фиксированной ценой, а нам нужна сумма,
которой управляет наш сервер.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from decimal import Decimal, InvalidOperation

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

INVOICE_CREATE_URL = "https://api.lava.ru/business/invoice/create"
INVOICE_STATUS_URL = "https://api.lava.ru/business/invoice/status"

STATUS_MAP = {
    "success": PaymentStatus.succeeded,
    "completed": PaymentStatus.succeeded,
    "created": PaymentStatus.pending,
    "pending": PaymentStatus.pending,
    "in_progress": PaymentStatus.pending,
    "expired": PaymentStatus.canceled,
    "cancel": PaymentStatus.canceled,
    "error": PaymentStatus.failed,
    "failed": PaymentStatus.failed,
}


def sign_body(body: bytes, secret: str) -> str:
    """HMAC-SHA256 от байтов тела запроса. Тело подписывается ровно в том виде,
    в котором уходит в сеть, — пересериализация ломает подпись."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class LavaProvider(PaymentProvider):
    slug = "lava"
    title = "Lava"
    hint = "Карты, СБП, кошельки"
    confirm_via_api = True

    def __init__(self, *, api_key: str, shop_id: str, webhook_secret: str = "") -> None:
        self.api_key = api_key
        self.shop_id = shop_id
        self.webhook_secret = webhook_secret or api_key

    async def create_payment(self, req: PaymentRequest, client: httpx.AsyncClient) -> RedirectTarget:
        payload = {
            "sum": format_amount(req.amount),
            "orderId": str(req.payment_id),
            "shopId": self.shop_id,
            "comment": req.description[:255],
            "hookUrl": req.return_url.replace("/return/", "/webhooks/lava/"),
            "successUrl": req.return_url,
            "failUrl": req.fail_url,
            "expire": 30,
            "customFields": json.dumps({"public_code": req.public_code}, ensure_ascii=False),
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = await client.post(
            INVOICE_CREATE_URL,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Signature": sign_body(body, self.api_key),
            },
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"Lava: {response.status_code} {response.text[:500]}")

        data = response.json()
        invoice = data.get("data") or {}
        url = invoice.get("url")
        if not url:
            raise ProviderError(f"Lava: в ответе нет ссылки на оплату: {data}")

        return RedirectTarget(url=url, provider_payment_id=invoice.get("id"), raw=data)

    async def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str],
    ) -> WebhookResult:
        received = headers.get("authorization") or headers.get("signature") or ""
        expected = sign_body(body, self.webhook_secret)
        if not received or not hmac.compare_digest(expected, received.strip().lower()):
            raise SignatureError("Lava: подпись уведомления не совпала")

        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignatureError("Lava: тело уведомления не является JSON") from exc

        status = STATUS_MAP.get(str(data.get("status", "")).lower(), PaymentStatus.unknown)
        invoice_id = data.get("invoice_id") or data.get("id")
        try:
            amount = Decimal(str(data.get("amount")))
        except (InvalidOperation, TypeError):
            amount = None

        return WebhookResult(
            status=status,
            payment_id=_parse_uuid(data.get("order_id") or data.get("orderId")),
            provider_payment_id=str(invoice_id) if invoice_id else None,
            amount=amount,
            event_key=f"{invoice_id}:{data.get('status')}",
            raw=data,
            response_body="OK",
        )

    async def fetch_status(
        self, provider_payment_id: str, client: httpx.AsyncClient
    ) -> PaymentStatus:
        payload = {"shopId": self.shop_id, "invoiceId": provider_payment_id}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        response = await client.post(
            INVOICE_STATUS_URL,
            content=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Signature": sign_body(body, self.api_key),
            },
            timeout=20.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"Lava: статус {response.status_code} {response.text[:300]}")
        data = (response.json() or {}).get("data") or {}
        return STATUS_MAP.get(str(data.get("status", "")).lower(), PaymentStatus.unknown)


def _parse_uuid(value: object) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None

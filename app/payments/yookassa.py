"""ЮKassa (YooMoney API v3).

Особенность, определяющая архитектуру обработчика: уведомления ЮKassa
**не подписываются**. Единственный корректный способ убедиться в оплате —
принять уведомление только с их диапазонов IP и после этого самостоятельно
запросить платёж через API. Поэтому confirm_via_api = True.
"""

from __future__ import annotations

import base64
import ipaddress
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

API_BASE = "https://api.yookassa.ru/v3"

# Диапазоны, с которых ЮKassa шлёт уведомления.
# СВЕРИТЬ перед боем: список берётся из документации, доступ к которой закрыт
# egress-политикой этой среды. Значения вынесены в константу, чтобы правка была в одном месте.
DEFAULT_WEBHOOK_NETWORKS = (
    "185.71.76.0/27",
    "185.71.77.0/27",
    "77.75.153.0/25",
    "77.75.156.11/32",
    "77.75.156.35/32",
    "77.75.154.128/25",
    "2a02:5180::/32",
)

STATUS_MAP = {
    "pending": PaymentStatus.pending,
    "waiting_for_capture": PaymentStatus.pending,
    "succeeded": PaymentStatus.succeeded,
    "canceled": PaymentStatus.canceled,
}


class YooKassaProvider(PaymentProvider):
    slug = "yookassa"
    title = "ЮKassa"
    hint = "Карты, СБП, ЮMoney, SberPay"
    confirm_via_api = True

    def __init__(
        self,
        *,
        shop_id: str,
        secret_key: str,
        send_receipt: bool = False,
        vat_code: int = 1,
        payment_subject: str = "service",
        payment_mode: str = "full_prepayment",
        webhook_networks: tuple[str, ...] = DEFAULT_WEBHOOK_NETWORKS,
    ) -> None:
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.send_receipt = send_receipt
        self.vat_code = vat_code
        self.payment_subject = payment_subject
        self.payment_mode = payment_mode
        self.allowed_webhook_networks = webhook_networks

    @property
    def _auth_header(self) -> str:
        raw = f"{self.shop_id}:{self.secret_key}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    # ------------------------------------------------------------------ оплата
    async def create_payment(self, req: PaymentRequest, client: httpx.AsyncClient) -> RedirectTarget:
        payload: dict = {
            "amount": {"value": format_amount(req.amount), "currency": req.currency},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": req.return_url},
            "description": req.description[:128],
            "metadata": {
                "payment_id": str(req.payment_id),
                "order_id": str(req.order_id),
                "public_code": req.public_code,
            },
        }
        if self.send_receipt:
            payload["receipt"] = self._build_receipt(req)

        response = await client.post(
            f"{API_BASE}/payments",
            headers={
                "Authorization": self._auth_header,
                # Ключ идемпотентности привязан к нашей попытке оплаты: повтор
                # запроса при сетевой ошибке не создаст второй счёт.
                "Idempotence-Key": str(req.payment_id),
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"ЮKassa: {response.status_code} {response.text[:500]}")

        data = response.json()
        confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
        if not confirmation_url:
            raise ProviderError(f"ЮKassa: в ответе нет confirmation_url: {data}")

        return RedirectTarget(
            url=confirmation_url,
            provider_payment_id=data.get("id"),
            raw=data,
        )

    def _build_receipt(self, req: PaymentRequest) -> dict:
        customer: dict[str, str] = {}
        if req.customer_email:
            customer["email"] = req.customer_email
        if not customer:
            raise ProviderError(
                "ЮKassa: для чека нужен email или телефон покупателя "
                "(включите COLLECT_EMAIL/REQUIRE_EMAIL либо отключите YOOKASSA_SEND_RECEIPT)"
            )
        return {
            "customer": customer,
            "items": [
                {
                    "description": req.description[:128],
                    "quantity": "1.00",
                    "amount": {"value": format_amount(req.amount), "currency": req.currency},
                    "vat_code": self.vat_code,
                    "payment_subject": self.payment_subject,
                    "payment_mode": self.payment_mode,
                }
            ],
        }

    # ---------------------------------------------------------------- вебхуки
    async def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str],
    ) -> WebhookResult:
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SignatureError("ЮKassa: тело уведомления не является JSON") from exc

        event = data.get("event") or ""
        obj = data.get("object") or {}
        provider_payment_id = obj.get("id")
        if not provider_payment_id:
            raise SignatureError("ЮKassa: в уведомлении нет object.id")

        status = STATUS_MAP.get(str(obj.get("status", "")), PaymentStatus.unknown)
        if status is PaymentStatus.unknown and event.startswith("payment."):
            status = {
                "payment.succeeded": PaymentStatus.succeeded,
                "payment.canceled": PaymentStatus.canceled,
                "payment.waiting_for_capture": PaymentStatus.pending,
            }.get(event, PaymentStatus.unknown)

        metadata = obj.get("metadata") or {}
        amount = None
        try:
            amount = Decimal(str((obj.get("amount") or {}).get("value")))
        except (InvalidOperation, TypeError):
            amount = None

        return WebhookResult(
            status=status,
            payment_id=_parse_uuid(metadata.get("payment_id")),
            provider_payment_id=provider_payment_id,
            amount=amount,
            currency=(obj.get("amount") or {}).get("currency"),
            event_key=f"{provider_payment_id}:{event or obj.get('status')}",
            raw=data,
            response_body="",
        )

    # -------------------------------------------------------------- сверка
    async def fetch_status(
        self, provider_payment_id: str, client: httpx.AsyncClient
    ) -> PaymentStatus:
        response = await client.get(
            f"{API_BASE}/payments/{provider_payment_id}",
            headers={"Authorization": self._auth_header},
            timeout=20.0,
        )
        if response.status_code >= 400:
            raise ProviderError(f"ЮKassa: статус {response.status_code} {response.text[:300]}")
        return STATUS_MAP.get(str(response.json().get("status", "")), PaymentStatus.unknown)


def ip_allowed(remote_ip: str | None, networks: tuple[str, ...]) -> bool:
    """Проверка, что уведомление пришло из разрешённой сети."""
    if not networks:
        return True
    if not remote_ip:
        return False
    try:
        address = ipaddress.ip_address(remote_ip)
    except ValueError:
        return False
    for network in networks:
        try:
            if address in ipaddress.ip_network(network, strict=False):
                return True
        except ValueError:
            continue
    return False


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None

"""Тестовый провайдер: прогоняет весь сценарий оплаты без реальных денег.

Нужен, чтобы проверить связку «заказ → редирект → вебхук → Telegram → страница
статуса» до подключения боевых агрегаторов и на стенде. Включается только
через DUMMY_ENABLED=true.
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
    RedirectTarget,
    SignatureError,
    WebhookResult,
)

SIGNATURE_HEADER = "x-dummy-signature"


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class DummyProvider(PaymentProvider):
    slug = "dummy"
    title = "Тестовая оплата (без денег)"
    hint = "Только для стенда: имитирует успешную или неуспешную оплату"

    def __init__(self, *, base_url: str, secret: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.secret = secret

    async def create_payment(self, req: PaymentRequest, client: httpx.AsyncClient) -> RedirectTarget:
        return RedirectTarget(
            url=f"{self.base_url}/dummy/{req.payment_id}",
            provider_payment_id=f"dummy-{req.invoice_no}",
            raw={"amount": str(req.amount)},
        )

    async def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str],
    ) -> WebhookResult:
        received = (headers.get(SIGNATURE_HEADER) or "").strip()
        if not hmac.compare_digest(sign(body, self.secret), received):
            raise SignatureError("dummy: подпись не совпала")

        data = json.loads(body.decode("utf-8"))
        status = {
            "succeeded": PaymentStatus.succeeded,
            "failed": PaymentStatus.failed,
        }.get(str(data.get("status")), PaymentStatus.unknown)

        try:
            amount = Decimal(str(data.get("amount")))
        except (InvalidOperation, TypeError):
            amount = None

        return WebhookResult(
            status=status,
            payment_id=uuid.UUID(str(data["payment_id"])),
            provider_payment_id=data.get("provider_payment_id"),
            amount=amount,
            event_key=f"{data.get('payment_id')}:{data.get('status')}",
            raw=data,
        )

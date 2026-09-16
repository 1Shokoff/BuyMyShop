"""Общий контракт платёжных провайдеров.

Вся специфика агрегатора (формат запроса, формула подписи, названия полей)
живёт внутри одного класса. Бизнес-логика заказов работает только с этими
структурами, поэтому добавление или отключение агрегатора не трогает
остальной код.
"""

from __future__ import annotations

import abc
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

import httpx


class ProviderError(RuntimeError):
    """Агрегатор вернул ошибку или неожиданный ответ."""


class SignatureError(ProviderError):
    """Подпись уведомления не сошлась — данные считаем поддельными."""


class PaymentStatus(str, Enum):
    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"
    canceled = "canceled"
    unknown = "unknown"


@dataclass(frozen=True)
class PaymentRequest:
    """Всё, что провайдеру нужно знать для создания счёта."""

    payment_id: uuid.UUID
    order_id: uuid.UUID
    invoice_no: int
    public_code: str
    amount: Decimal
    currency: str
    description: str
    return_url: str
    fail_url: str
    # Куда агрегатор шлёт уведомление. Передаётся явно: собирать этот адрес
    # из return_url подстановкой нельзя — маршрут /webhooks/{provider} принимает
    # ровно один сегмент, а в return_url за ним стоит идентификатор платежа.
    webhook_url: str
    customer_ref: str
    customer_email: str | None = None


@dataclass(frozen=True)
class RedirectTarget:
    """Куда отправить покупателя, чтобы он оплатил."""

    url: str
    method: str = "GET"
    # Для method="POST": поля автосабмит-формы.
    fields: dict[str, str] = field(default_factory=dict)
    provider_payment_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WebhookResult:
    """Результат разбора уведомления от агрегатора."""

    status: PaymentStatus
    payment_id: uuid.UUID | None = None
    invoice_no: int | None = None
    provider_payment_id: str | None = None
    amount: Decimal | None = None
    currency: str | None = None
    # Ключ идемпотентности: повторная доставка того же события не обработается дважды.
    event_key: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    response_body: str = "OK"
    response_media_type: str = "text/plain"


class PaymentProvider(abc.ABC):
    """Базовый класс агрегатора."""

    slug: str = ""
    title: str = ""
    # Подсказка для страницы оплаты (какие способы оплаты внутри).
    hint: str = ""
    # Часть агрегаторов подписывает уведомления и дополнительно ограничивает IP.
    allowed_webhook_networks: tuple[str, ...] = ()
    # True — после вебхука дополнительно запросить статус по API агрегатора.
    # Обязательно для тех, кто не подписывает уведомления (ЮKassa).
    confirm_via_api: bool = False

    @abc.abstractmethod
    async def create_payment(self, req: PaymentRequest, client: httpx.AsyncClient) -> RedirectTarget:
        """Создать счёт и вернуть адрес платёжной формы."""

    @abc.abstractmethod
    async def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str],
    ) -> WebhookResult:
        """Проверить подпись и разобрать уведомление. Бросает SignatureError."""

    async def fetch_status(
        self, provider_payment_id: str, client: httpx.AsyncClient
    ) -> PaymentStatus:
        """Контрольный запрос статуса по API агрегатора.

        Используется как вторая, независимая от вебхука проверка. Провайдеры без
        такого метода возвращают unknown — тогда источником истины остаётся
        только подписанный вебхук.
        """
        return PaymentStatus.unknown


def format_amount(amount: Decimal) -> str:
    """Сумма в виде '1234.56' — единый формат для подписей и запросов."""
    return f"{Decimal(amount):.2f}"

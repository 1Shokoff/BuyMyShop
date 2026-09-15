"""Сборка включённых провайдеров по конфигурации."""

from __future__ import annotations

import functools

from app.config import Settings, get_settings
from app.payments.base import PaymentProvider
from app.payments.dummy import DummyProvider
from app.payments.enot import EnotProvider
from app.payments.lava import LavaProvider
from app.payments.robokassa import RobokassaProvider
from app.payments.yookassa import YooKassaProvider


def build_providers(settings: Settings) -> dict[str, PaymentProvider]:
    providers: dict[str, PaymentProvider] = {}

    if settings.dummy_enabled:
        providers[DummyProvider.slug] = DummyProvider(
            base_url=settings.public_base_url, secret=settings.secret_key
        )

    if settings.yookassa_enabled:
        providers[YooKassaProvider.slug] = YooKassaProvider(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            send_receipt=settings.yookassa_send_receipt,
            vat_code=settings.yookassa_vat_code,
            payment_subject=settings.yookassa_payment_subject,
            payment_mode=settings.yookassa_payment_mode,
        )

    if settings.robokassa_enabled:
        providers[RobokassaProvider.slug] = RobokassaProvider(
            merchant_login=settings.robokassa_merchant_login,
            password1=settings.robokassa_password1,
            password2=settings.robokassa_password2,
            algorithm=settings.robokassa_hash_algorithm,
            test_mode=settings.robokassa_test_mode,
        )

    if settings.lava_enabled:
        providers[LavaProvider.slug] = LavaProvider(
            api_key=settings.lava_api_key,
            shop_id=settings.lava_offer_id,
            webhook_secret=settings.lava_webhook_secret,
        )

    if settings.enot_enabled:
        providers[EnotProvider.slug] = EnotProvider(
            shop_id=settings.enot_shop_id,
            secret_key=settings.enot_secret_key,
            additional_key=settings.enot_additional_key,
        )

    return providers


@functools.lru_cache(maxsize=1)
def get_providers() -> dict[str, PaymentProvider]:
    return build_providers(get_settings())


def get_provider(slug: str) -> PaymentProvider | None:
    return get_providers().get(slug)

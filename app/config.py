"""Конфигурация приложения.

Все настройки читаются из переменных окружения (или файла .env рядом с проектом).
Длинные тексты (инструкция по оплате, сообщение после оплаты) лежат в каталоге
content/ отдельными файлами, чтобы их можно было править без пересборки образа.
"""

from __future__ import annotations

import functools
from decimal import Decimal
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
CONTENT_DIR = BASE_DIR / "content"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ общее
    site_name: str = "BuyMyShop"
    # Внешний адрес сайта без завершающего слэша. Из него строятся return_url и
    # адреса вебхуков, поэтому он обязан совпадать с реальным доменом.
    public_base_url: str = "http://localhost:8000"
    secret_key: str = "dev-insecure-secret-change-me"
    debug: bool = False
    display_timezone: str = "Asia/Magadan"
    support_contact: str = ""

    # Сколько заказ ждёт оплаты, прежде чем будет помечен как просроченный.
    order_ttl_minutes: int = 30

    # ----------------------------------------------------------------- товар
    product_code: str = "main"
    product_title: str = "Товар"
    product_price: Decimal = Decimal("100.00")
    product_currency: str = "RUB"

    # --------------------------------------------- поле «15-значное число»
    customer_field_label: str = "Номер аккаунта"
    customer_field_hint: str = "15 цифр, без пробелов и дефисов"
    customer_field_pattern: str = r"^\d{15}$"
    customer_field_error: str = "Введите ровно 15 цифр."

    collect_email: bool = True
    require_email: bool = False

    # -------------------------------------------------------------- хранилище
    database_url: str = "postgresql+psycopg://buymyshop:buymyshop@db:5432/buymyshop"

    # --------------------------------------------------------------- telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    telegram_max_attempts: int = 12
    outbox_poll_seconds: float = 5.0

    # ------------------------------------------------------------------ админ
    admin_username: str = "admin"
    admin_password: str = ""

    # ------------------------------------------------------ лимиты и защита
    buy_rate_limit_per_minute: int = 10

    # -------------------------------------------------------------- провайдеры
    # dummy — встроенный тестовый провайдер: имитирует оплату без реальных денег.
    # Нужен для приёмки flow до подключения боевых агрегаторов. В проде выключить.
    dummy_enabled: bool = False

    yookassa_enabled: bool = False
    yookassa_shop_id: str = ""
    yookassa_secret_key: str = ""
    # Состав чека (54-ФЗ / НПД). Если провайдер формирует чеки сам — оставить пустым.
    yookassa_send_receipt: bool = False
    yookassa_vat_code: int = 1
    yookassa_payment_subject: str = "service"
    yookassa_payment_mode: str = "full_prepayment"

    robokassa_enabled: bool = False
    robokassa_merchant_login: str = ""
    robokassa_password1: str = ""
    robokassa_password2: str = ""
    robokassa_hash_algorithm: str = "md5"
    robokassa_test_mode: bool = False

    lava_enabled: bool = False
    lava_api_key: str = ""
    lava_webhook_secret: str = ""
    lava_offer_id: str = ""

    enot_enabled: bool = False
    enot_shop_id: str = ""
    enot_secret_key: str = ""
    enot_additional_key: str = ""

    @field_validator("public_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("product_currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()

    # ------------------------------------------------------------- производные
    @property
    def instructions_path(self) -> Path:
        return CONTENT_DIR / "instructions.md"

    @property
    def after_payment_path(self) -> Path:
        return CONTENT_DIR / "after_payment.md"

    def url(self, path: str) -> str:
        return f"{self.public_base_url}/{path.lstrip('/')}"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()

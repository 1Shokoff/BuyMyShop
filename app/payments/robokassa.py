"""Robokassa.

Схема: покупатель уходит GET-редиректом на платёжную страницу, оплата
подтверждается запросом на ResultURL, который подписан паролем №2.

Для самозанятого чеки формирует сам Robokassa (сервис «Робочеки СМЗ»),
поэтому параметр Receipt мы не передаём — иначе пришлось бы дублировать
фискализацию на своей стороне.
"""

from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal, InvalidOperation
from urllib.parse import urlencode

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

PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
OPSTATE_URL = "https://auth.robokassa.ru/Merchant/WebService/Service.asmx/OpState"

SUPPORTED_ALGORITHMS = {"md5", "sha1", "sha256", "sha384", "sha512"}

# Коды состояния операции из OpState.
# СВЕРИТЬ с личным кабинетом перед боем: расшифровка кодов бралась из документации,
# доступ к которой заблокирован из этой среды.
OPSTATE_SUCCESS = {100}
OPSTATE_FAILED = {10}


def _digest(algorithm: str, payload: str) -> str:
    if algorithm not in SUPPORTED_ALGORITHMS:
        raise ProviderError(f"Robokassa: неподдерживаемый алгоритм подписи {algorithm!r}")
    return hashlib.new(algorithm, payload.encode("utf-8")).hexdigest()


def _shp_suffix(shp: dict[str, str]) -> str:
    """Пользовательские параметры участвуют в подписи, отсортированные по имени."""
    return "".join(f":{key}={shp[key]}" for key in sorted(shp))


def build_start_signature(
    *,
    merchant_login: str,
    out_sum: str,
    inv_id: int,
    password1: str,
    shp: dict[str, str],
    algorithm: str = "md5",
) -> str:
    payload = f"{merchant_login}:{out_sum}:{inv_id}:{password1}{_shp_suffix(shp)}"
    return _digest(algorithm, payload)


def build_result_signature(
    *,
    out_sum: str,
    inv_id: int,
    password2: str,
    shp: dict[str, str],
    algorithm: str = "md5",
) -> str:
    payload = f"{out_sum}:{inv_id}:{password2}{_shp_suffix(shp)}"
    return _digest(algorithm, payload)


class RobokassaProvider(PaymentProvider):
    slug = "robokassa"
    title = "Robokassa"
    hint = "Банковские карты, СБП, электронные кошельки"

    def __init__(
        self,
        *,
        merchant_login: str,
        password1: str,
        password2: str,
        algorithm: str = "md5",
        test_mode: bool = False,
    ) -> None:
        self.merchant_login = merchant_login
        self.password1 = password1
        self.password2 = password2
        self.algorithm = algorithm.lower()
        self.test_mode = test_mode

    # ------------------------------------------------------------------ оплата
    async def create_payment(self, req: PaymentRequest, client: httpx.AsyncClient) -> RedirectTarget:
        out_sum = format_amount(req.amount)
        shp = {"shp_payment": req.payment_id.hex}
        signature = build_start_signature(
            merchant_login=self.merchant_login,
            out_sum=out_sum,
            inv_id=req.invoice_no,
            password1=self.password1,
            shp=shp,
            algorithm=self.algorithm,
        )
        params: dict[str, str] = {
            "MerchantLogin": self.merchant_login,
            "OutSum": out_sum,
            "InvId": str(req.invoice_no),
            "Description": req.description,
            "SignatureValue": signature,
            "Culture": "ru",
            "Encoding": "utf-8",
            "SuccessUrl2": req.return_url,
            "SuccessUrl2Method": "GET",
            "FailUrl2": req.fail_url,
            "FailUrl2Method": "GET",
            **shp,
        }
        if self.algorithm != "md5":
            params["SignatureValueAlgorithm"] = self.algorithm
        if req.customer_email:
            params["Email"] = req.customer_email
        if self.test_mode:
            params["IsTest"] = "1"

        return RedirectTarget(
            url=f"{PAYMENT_URL}?{urlencode(params)}",
            provider_payment_id=str(req.invoice_no),
            raw={"params": {k: v for k, v in params.items() if k != "SignatureValue"}},
        )

    # ---------------------------------------------------------------- вебхуки
    async def parse_webhook(
        self,
        *,
        headers: dict[str, str],
        body: bytes,
        query: dict[str, str],
    ) -> WebhookResult:
        data = dict(query)
        if body:
            from urllib.parse import parse_qsl

            data.update(dict(parse_qsl(body.decode("utf-8", errors="replace"))))

        out_sum = data.get("OutSum") or data.get("out_summ") or ""
        inv_id_raw = data.get("InvId") or data.get("inv_id") or ""
        received = (data.get("SignatureValue") or "").lower()
        if not out_sum or not inv_id_raw or not received:
            raise SignatureError("Robokassa: в уведомлении нет обязательных полей")

        try:
            inv_id = int(inv_id_raw)
        except ValueError as exc:
            raise SignatureError("Robokassa: InvId не является числом") from exc

        shp = {key: value for key, value in data.items() if key.lower().startswith("shp_")}
        expected = build_result_signature(
            out_sum=out_sum,
            inv_id=inv_id,
            password2=self.password2,
            shp=shp,
            algorithm=self.algorithm,
        )
        if not _constant_time_equals(expected, received):
            raise SignatureError("Robokassa: подпись уведомления не совпала")

        payment_id = _parse_uuid(shp.get("shp_payment") or shp.get("Shp_payment"))
        try:
            amount = Decimal(out_sum)
        except InvalidOperation:
            amount = None

        return WebhookResult(
            # ResultURL Robokassa присылает только по факту успешной оплаты.
            status=PaymentStatus.succeeded,
            payment_id=payment_id,
            invoice_no=inv_id,
            provider_payment_id=str(inv_id),
            amount=amount,
            event_key=f"{inv_id}:succeeded",
            raw=data,
            response_body=f"OK{inv_id}",
        )

    # ------------------------------------------------------------- сверка
    async def fetch_status(
        self, provider_payment_id: str, client: httpx.AsyncClient
    ) -> PaymentStatus:
        try:
            inv_id = int(provider_payment_id)
        except ValueError:
            return PaymentStatus.unknown

        signature = _digest(
            self.algorithm, f"{self.merchant_login}:{inv_id}:{self.password2}"
        )
        response = await client.get(
            OPSTATE_URL,
            params={
                "MerchantLogin": self.merchant_login,
                "InvoiceID": str(inv_id),
                "Signature": signature,
            },
            timeout=15.0,
        )
        response.raise_for_status()
        return _parse_opstate(response.text)


def _parse_opstate(xml_text: str) -> PaymentStatus:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return PaymentStatus.unknown

    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] == "StateCode" and element.text:
            try:
                code = int(element.text.strip())
            except ValueError:
                return PaymentStatus.unknown
            if code in OPSTATE_SUCCESS:
                return PaymentStatus.succeeded
            if code in OPSTATE_FAILED:
                return PaymentStatus.canceled
            return PaymentStatus.pending
    return PaymentStatus.unknown


def _parse_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _constant_time_equals(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.lower(), right.lower())

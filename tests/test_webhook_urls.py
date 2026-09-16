"""Адрес уведомления, который мы отдаём агрегатору, должен вести на наш маршрут."""

import json
import uuid
from decimal import Decimal

import httpx
from starlette.routing import Match

from app.payments.base import PaymentRequest
from app.payments.enot import EnotProvider
from app.payments.lava import LavaProvider
from app.routes.webhooks import router

BASE = "https://example.com"


def request() -> PaymentRequest:
    payment_id = uuid.uuid4()
    return PaymentRequest(
        payment_id=payment_id,
        order_id=uuid.uuid4(),
        invoice_no=1001,
        public_code="M4XFUWUU",
        amount=Decimal("990.00"),
        currency="RUB",
        description="Товар",
        return_url=f"{BASE}/return/{payment_id}",
        fail_url=f"{BASE}/fail/{payment_id}",
        webhook_url=f"{BASE}/webhooks/lava",
        customer_ref="123456789012345",
    )


def route_matches(path: str) -> bool:
    scope = {"type": "http", "method": "POST", "path": path, "headers": [], "root_path": ""}
    return any(route.matches(scope)[0] == Match.FULL for route in router.routes)


def test_route_accepts_only_one_segment_after_webhooks():
    assert route_matches("/webhooks/lava") is True
    # Ровно на этом ломался прежний вариант, где адрес собирался из return_url.
    assert route_matches(f"/webhooks/lava/{uuid.uuid4()}") is False


def capture(provider, req, response_json: dict) -> httpx.Request:
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        return httpx.Response(200, json=response_json)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    import asyncio

    asyncio.run(_create(provider, req, client))
    return sent[0]


async def _create(provider, req, client):
    async with client:
        return await provider.create_payment(req, client)


def test_lava_sends_a_reachable_hook_url():
    provider = LavaProvider(api_key="k", shop_id="s", webhook_secret="w")
    sent = capture(provider, request(), {"data": {"id": "inv1", "url": "https://pay"}})
    hook_url = json.loads(sent.content)["hookUrl"]

    assert hook_url == f"{BASE}/webhooks/lava"
    assert route_matches(httpx.URL(hook_url).path) is True


def test_enot_sends_a_reachable_hook_url():
    provider = EnotProvider(shop_id="s", secret_key="k", additional_key="a")
    req = request()
    req = PaymentRequest(**{**req.__dict__, "webhook_url": f"{BASE}/webhooks/enot"})
    sent = capture(provider, req, {"data": {"id": "inv1", "url": "https://pay"}})
    hook_url = json.loads(sent.content)["hook_url"]

    assert hook_url == f"{BASE}/webhooks/enot"
    assert route_matches(httpx.URL(hook_url).path) is True

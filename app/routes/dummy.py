"""Страница тестового провайдера.

Существует только когда DUMMY_ENABLED=true. Нажатие кнопки отправляет
подписанное уведомление на наш же /webhooks/dummy — то есть проверяется
ровно тот путь, по которому пойдут боевые агрегаторы.
"""

from __future__ import annotations

import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.payments.dummy import SIGNATURE_HEADER, sign
from app.services import orders as order_service
from app.templating import templates

router = APIRouter(prefix="/dummy")

INTERNAL_WEBHOOK_URL = "http://127.0.0.1:8000/webhooks/dummy"


@router.get("/{payment_id}", response_class=HTMLResponse)
async def dummy_page(
    request: Request,
    payment_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> Response:
    payment = await order_service.get_payment(session, payment_id)
    if payment is None or payment.provider != "dummy":
        raise HTTPException(status_code=404)
    order = await order_service.get_order(session, payment.order_id)
    return templates.TemplateResponse(
        request, "dummy.html", {"payment": payment, "order": order}
    )


@router.post("/{payment_id}/{outcome}")
async def dummy_callback(
    request: Request,
    payment_id: uuid.UUID,
    outcome: str,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    if outcome not in {"succeeded", "failed"}:
        raise HTTPException(status_code=400)
    payment = await order_service.get_payment(session, payment_id)
    if payment is None or payment.provider != "dummy":
        raise HTTPException(status_code=404)

    body = json.dumps(
        {
            "payment_id": str(payment.id),
            "provider_payment_id": payment.provider_payment_id,
            "status": outcome,
            "amount": str(payment.amount),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    await request.app.state.http_client.post(
        INTERNAL_WEBHOOK_URL,
        content=body,
        headers={
            "Content-Type": "application/json",
            SIGNATURE_HEADER: sign(body, settings.secret_key),
        },
        timeout=20.0,
    )
    return RedirectResponse(f"/return/{payment.id}", status_code=303)

"""Служебный раздел: список заказов и повторная отправка уведомления."""

from __future__ import annotations

import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import get_session
from app.models import Order, OutboxMessage
from app.services import orders as order_service
from app.services.telegram import enqueue_order_paid
from app.templating import templates

router = APIRouter(prefix="/admin")
security = HTTPBasic(auto_error=False)

PAGE_SIZE = 50


def require_admin(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
    settings: Settings = Depends(get_settings),
) -> str:
    if not settings.admin_password:
        raise HTTPException(status_code=404)
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    username_ok = secrets.compare_digest(credentials.username, settings.admin_username)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@router.get("", response_class=HTMLResponse)
async def orders_list(
    request: Request,
    page: int = Query(default=1, ge=1),
    order_status: str = Query(default="", alias="status"),
    search: str = Query(default=""),
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    stmt = select(Order).order_by(Order.created_at.desc())
    count_stmt = select(func.count()).select_from(Order)

    if order_status:
        stmt = stmt.where(Order.status == order_status)
        count_stmt = count_stmt.where(Order.status == order_status)
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(Order.customer_ref.ilike(pattern) | Order.public_code.ilike(pattern))
        count_stmt = count_stmt.where(
            Order.customer_ref.ilike(pattern) | Order.public_code.ilike(pattern)
        )

    total = (await session.execute(count_stmt)).scalar_one()
    rows = list(
        (await session.execute(stmt.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars()
    )

    return templates.TemplateResponse(
        request,
        "admin/orders.html",
        {
            "orders": rows,
            "total": total,
            "page": page,
            "pages": max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
            "status_filter": order_status,
            "search": search,
        },
    )


@router.get("/orders/{order_id}", response_class=HTMLResponse)
async def order_detail(
    request: Request,
    order_id: uuid.UUID,
    _: str = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404)
    messages = list(
        (
            await session.execute(
                select(OutboxMessage)
                .where(OutboxMessage.order_id == order_id)
                .order_by(OutboxMessage.created_at.desc())
            )
        ).scalars()
    )
    return templates.TemplateResponse(
        request, "admin/order_detail.html", {"order": order, "messages": messages}
    )


@router.post("/orders/{order_id}/resend")
async def resend_notification(
    order_id: uuid.UUID,
    _: str = Depends(require_admin),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> Response:
    order = await order_service.get_order(session, order_id)
    if order is None:
        raise HTTPException(status_code=404)
    succeeded = [p for p in order.payments if p.state == "succeeded"]
    payment = succeeded[-1] if succeeded else (order.payments[-1] if order.payments else None)
    if payment is None:
        raise HTTPException(status_code=400, detail="у заказа нет платежей")

    await enqueue_order_paid(session, order=order, payment=payment, settings=settings)
    await session.commit()
    return RedirectResponse(f"/admin/orders/{order_id}", status_code=303)

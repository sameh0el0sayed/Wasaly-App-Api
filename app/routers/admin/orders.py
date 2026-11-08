from datetime import date, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import OrderStatus as S
from ...database import get_db
from ...deps import admin_user, paging, super_admin_user
from ...models import Order, OrderStatusHistory, Payment, User, utcnow
from ...schemas import PriceAdjustIn, ReasonIn
from ...serializers import history_out, order_for_admin
from ...services.audit import audit
from ...services.notifications import notify
from ...services.orders import close_pending_offers, load_order, transition
from ...services.pricing import apply_discount, fits_decimals, split_order
from .reports import _local_start  # noqa: E402

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- orders ----------------
@router.get("/orders")
async def list_orders(status: str | None = None, customer_id: int | None = None, driver_id: int | None = None,
                      order_id: int | None = None, date_from: date | None = None, date_to: date | None = None,
                      page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(Order)
    if status:
        stmt = stmt.where(Order.order_status == status)
    if customer_id:
        stmt = stmt.where(Order.customer_id == customer_id)
    if driver_id:
        stmt = stmt.where(Order.driver_id == driver_id)
    if order_id:
        stmt = stmt.where(Order.id == order_id)
    if date_from:
        stmt = stmt.where(Order.created_at >= _local_start(date_from))
    if date_to:
        stmt = stmt.where(Order.created_at < _local_start(date_to + timedelta(days=1)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Order.id.desc()).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [order_for_admin(o) for o in rows]}


@router.get("/orders/{order_id}")
async def get_order(order_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id)
    if not o:
        raise HTTPException(404, "Order not found")
    hist = (await db.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == o.id).order_by(OrderStatusHistory.id))).scalars().all()
    return {**order_for_admin(o), "history": [history_out(h) for h in hist]}


@router.post("/orders/{order_id}/cancel")
async def admin_cancel(order_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id, for_update=True)
    if not o:
        raise HTTPException(404, "Order not found")
    if o.order_status in S.FINAL:
        raise HTTPException(409, {"code": "ORDER_ALREADY_FINAL", "status": o.order_status})
    await close_pending_offers(db, o.id)
    o.cancelled_at, o.cancelled_by, o.cancelled_by_role, o.cancellation_reason = utcnow(), admin.id, admin.role, body.reason
    await transition(db, o, S.CANCELLED, admin.id, admin.role, body.reason)
    audit(db, admin.id, "ORDER_CANCELLED", "order", o.id, body.reason)
    for uid in {o.customer_id, o.driver_id} - {None}:
        await notify(db, uid, "ORDER_CANCELLED", "تم إلغاء الطلب", f"ألغت الإدارة الطلب: {body.reason}", o.id)
    await db.commit()
    return order_for_admin(await load_order(db, o.id))


@router.post("/orders/{order_id}/adjust-price")
async def adjust_price(order_id: int, body: PriceAdjustIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """Exceptional, super-admin only, reason mandatory, original price preserved + audit trail."""
    o = await load_order(db, order_id, for_update=True)
    if not o or o.order_status != S.COMPLETED:
        raise HTTPException(409, "Only completed orders can be adjusted")
    if o.payment_status == "PAID" or (o.payment_status == "PENDING_VERIFICATION"):
        raise HTTPException(409, {"code": "ADJUST_AFTER_PAYMENT"})
    if not o.price_adjusted:
        o.original_price = o.final_price
    old = o.final_price
    if not fits_decimals(body.new_price, o.currency_decimals):
        raise HTTPException(422, {"code": "TOO_MANY_DECIMALS", "currency": o.currency, "max_decimals": o.currency_decimals})
    o.final_price, o.price_adjusted = body.new_price, True
    o.commission, o.customer_total, o.driver_earning, cut = split_order(o, o.final_price)
    o.commission, o.customer_total = apply_discount(o.commission, o.customer_total, o.discount_amount or 0)  # keeps the order's original discount
    o.outside_driver_cut = cut if o.is_outside_bahrain else None
    pay = (await db.execute(select(Payment).where(Payment.order_id == o.id))).scalars().first()
    if pay:
        pay.amount, pay.driver_amount, pay.company_amount = o.customer_total, o.driver_earning, o.commission
        if pay.wallet_amount and pay.wallet_amount > pay.amount:  # price went down below what the wallet already covered: give it back
            from ...models import User as _U
            from ...models import WalletEntry
            back = pay.wallet_amount - pay.amount
            cu = (await db.execute(select(_U).where(_U.id == pay.customer_id).with_for_update())).scalar_one()
            cu.wallet_balance = Decimal(cu.wallet_balance or 0) + back
            db.add(WalletEntry(user_id=cu.id, amount=back, kind="REFUND", ref_id=pay.id))
            pay.wallet_amount = pay.amount
    db.add(OrderStatusHistory(order_id=o.id, old_status=o.order_status, new_status=o.order_status, changed_by=admin.id,
                              changed_by_role=admin.role, notes=f"Price adjusted {old} -> {o.final_price}: {body.reason}"))
    audit(db, admin.id, "PRICE_ADJUSTED", "order", o.id, body.reason, {"old": str(old), "new": str(o.final_price)})
    await db.commit()
    return order_for_admin(o)

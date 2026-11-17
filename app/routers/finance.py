"""Admin financial reconciliation and operational analytics (phase 8): per-service totals, mismatches, receivables from merchants/corporates, daily series, CSV."""
import csv
import io
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user
from ..models import Corporate, Merchant, MerchantDelivery, Order, Payment, User

admin_router = APIRouter(prefix="/api/admin/finance", tags=["finance"])
Q3 = Decimal("0.001")


def _month(month: str | None) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC) if month else datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    except ValueError as e:
        raise HTTPException(422, "month must be YYYY-MM") from e
    return start, start.replace(year=start.year + (start.month == 12), month=1 if start.month == 12 else start.month + 1)


def _d(v) -> str:
    return f"{Decimal(v or 0).quantize(Q3):.3f}"


async def reconcile(db, month: str | None) -> dict:
    start, end = _month(month)
    cond = (Order.order_status == S.COMPLETED, Order.completed_at >= start, Order.completed_at < end)
    svc = func.coalesce(Order.service_code, "RIDES")
    rows = (await db.execute(select(svc, func.count(), func.coalesce(func.sum(Order.customer_total), 0), func.coalesce(func.sum(Order.commission), 0),
                                    func.coalesce(func.sum(Order.driver_earning), 0), func.coalesce(func.sum(Order.discount_amount), 0)).where(*cond).group_by(svc))).all()
    by_service = [{"service": r[0], "orders": r[1], "gross": _d(r[2]), "commission": _d(r[3]), "driver_earnings": _d(r[4]), "discounts": _d(r[5])} for r in rows]
    methods = (await db.execute(select(func.coalesce(Payment.payment_method, "WALLET/OTHER"), Payment.payment_status, func.count(), func.coalesce(func.sum(Payment.amount), 0))
                                .join(Order, Order.id == Payment.order_id).where(Payment.kind == "ORDER", *cond).group_by(Payment.payment_method, Payment.payment_status))).all()
    issues = []
    no_pay = (await db.execute(select(Order.id).where(*cond, Order.payment_id.is_(None)).limit(50))).scalars().all()
    if no_pay:
        issues.append({"type": "COMPLETED_WITHOUT_PAYMENT", "count": len(no_pay), "order_ids": no_pay})
    old = datetime.now(UTC) - timedelta(hours=48)
    unpaid = (await db.execute(select(Order.id).where(*cond, Order.payment_status != "PAID", Order.completed_at < old).limit(50))).scalars().all()
    if unpaid:
        issues.append({"type": "UNPAID_OVER_48H", "count": len(unpaid), "order_ids": unpaid})
    split = (await db.execute(select(Payment.id, Payment.order_id).join(Order, Order.id == Payment.order_id).where(
        Payment.kind == "ORDER", *cond, func.abs(Payment.driver_amount + Payment.company_amount - Payment.amount) > Decimal("0.002")).limit(50))).all()
    if split:
        issues.append({"type": "SPLIT_MISMATCH", "note": "driver + company share differs from the amount", "payment_ids": [r[0] for r in split], "order_ids": [r[1] for r in split]})
    return {"month": start.strftime("%Y-%m"), "currency": "BHD", "by_service": by_service,
            "totals": {k: _d(sum(Decimal(x[k]) for x in by_service)) for k in ("gross", "commission", "driver_earnings", "discounts")},
            "payments": [{"method": m, "status": st, "count": n, "amount": _d(a)} for m, st, n, a in methods], "issues": issues, "clean": not issues}


@admin_router.get("/reconciliation")
async def reconciliation(month: str | None = None, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await reconcile(db, month)


@admin_router.get("/receivables")
async def receivables(month: str | None = None, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """What partners and corporate accounts owe for the month (invoiced, not collected per trip)."""
    start, end = _month(month)
    done = (Order.order_status == S.COMPLETED, Order.completed_at >= start, Order.completed_at < end)
    m_rows = (await db.execute(select(Merchant.id, Merchant.name, Merchant.kind, func.count(), func.coalesce(func.sum(Order.customer_total), 0))
                               .join(MerchantDelivery, MerchantDelivery.merchant_id == Merchant.id).join(Order, Order.id == MerchantDelivery.order_id)
                               .where(*done).group_by(Merchant.id, Merchant.name, Merchant.kind))).all()
    c_rows = (await db.execute(select(Corporate.id, Corporate.name, Corporate.discount_percent, func.count(), func.coalesce(func.sum(Order.customer_total), 0))
                               .join(Order, Order.corporate_id == Corporate.id).where(*done).group_by(Corporate.id, Corporate.name, Corporate.discount_percent))).all()
    merchants = [{"id": i, "name": n, "kind": k, "deliveries": c, "due": _d(t)} for i, n, k, c, t in m_rows]
    corps = [{"id": i, "name": n, "orders": c, "gross": _d(t), "discount_percent": f"{p:.2f}", "net_due": _d(Decimal(t) * (100 - p) / 100)} for i, n, p, c, t in c_rows]
    return {"month": start.strftime("%Y-%m"), "currency": "BHD", "merchants": merchants, "corporates": corps,
            "total_due": _d(sum(Decimal(x["due"]) for x in merchants) + sum(Decimal(x["net_due"]) for x in corps))}


@admin_router.get("/daily")
async def daily(days: int = 30, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Per-day series for charts: orders created, completed, cancelled, revenue and commission (UTC days)."""
    days = max(1, min(days, 180))
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    day = func.date(Order.created_at)
    rows = (await db.execute(select(day, func.count()).where(Order.created_at >= since).group_by(day).order_by(day))).all()
    by = {str(r[0]): {"date": str(r[0]), "orders": r[1], "completed": 0, "cancelled": 0, "revenue": "0.000", "commission": "0.000"} for r in rows}
    for st, key in ((S.COMPLETED, "completed"), (S.CANCELLED, "cancelled")):
        for d, n in (await db.execute(select(day, func.count()).where(Order.created_at >= since, Order.order_status == st).group_by(day))).all():
            by.setdefault(str(d), {"date": str(d), "orders": 0, "completed": 0, "cancelled": 0, "revenue": "0.000", "commission": "0.000"})[key] = n
    cday = func.date(Order.completed_at)
    for d, rev, com in (await db.execute(select(cday, func.coalesce(func.sum(Order.customer_total), 0), func.coalesce(func.sum(Order.commission), 0)).where(
            Order.completed_at >= since, Order.order_status == S.COMPLETED).group_by(cday))).all():
        x = by.setdefault(str(d), {"date": str(d), "orders": 0, "completed": 0, "cancelled": 0, "revenue": "0.000", "commission": "0.000"})
        x["revenue"], x["commission"] = _d(rev), _d(com)
    return {"days": days, "items": [by[k] for k in sorted(by)]}


@admin_router.get("/export/reconciliation.csv")
async def export_csv(month: str | None = None, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    r = await reconcile(db, month)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["month", "service", "orders", "gross", "commission", "driver_earnings", "discounts"])
    for x in r["by_service"]:
        w.writerow([r["month"], x["service"], x["orders"], x["gross"], x["commission"], x["driver_earnings"], x["discounts"]])
    w.writerow([r["month"], "TOTAL", sum(x["orders"] for x in r["by_service"]), *r["totals"].values()])
    return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="reconciliation-{r["month"]}.csv"'})

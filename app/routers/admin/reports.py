import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...constants import OrderStatus as S
from ...database import get_db
from ...deps import admin_user
from ...models import (DriverPayout, DriverWorkSession, Order,
                      Payment, ServiceArea, Subscription, User, utcnow)
from ...serializers import (dt, money)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- reports ----------------
def _local_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=ZoneInfo(settings.timezone)).astimezone(timezone.utc)


REPORTS = ("summary", "orders_by_period", "per_driver", "per_area", "no_driver", "driver_hours", "trips",
           "payments", "payouts", "subscriptions")


async def build_report(db: AsyncSession, name: str, d_from: date, d_to: date, group: str = "day") -> list[dict]:
    tz = ZoneInfo(settings.timezone)
    start, end = _local_start(d_from), _local_start(d_to + timedelta(days=1))
    orders = (await db.execute(select(Order).where(Order.created_at >= start, Order.created_at < end).order_by(Order.id))).scalars().all()
    def fmt(v, o):  # money in the order's own currency
        return money(v, o.currency_decimals)
    if name == "summary":
        done = [o for o in orders if o.order_status == S.COMPLETED]
        gross: dict[str, Decimal] = defaultdict(Decimal)
        profit: dict[str, Decimal] = defaultdict(Decimal)
        paid: dict[str, Decimal] = defaultdict(Decimal)
        drv: dict[str, Decimal] = defaultdict(Decimal)
        dec: dict[str, int] = {}
        for o in done:
            gross[o.currency] += o.final_price
            profit[o.currency] += o.commission or 0
            paid[o.currency] += o.customer_total or 0
            drv[o.currency] += o.driver_earning or 0
            dec[o.currency] = o.currency_decimals
        avg = (sum(o.actual_duration_seconds for o in done) / len(done)) if done else 0
        return [{"orders_total": len(orders), "completed": len(done),
                 "cancelled": sum(o.order_status == S.CANCELLED for o in orders),
                 "expired": sum(o.order_status == S.EXPIRED for o in orders),
                 "no_driver_now": sum(o.order_status == S.NO_DRIVER_AVAILABLE for o in orders),
                 "trip_value_by_currency": {c: money(v, dec[c]) for c, v in gross.items()},
                 "customer_total_by_currency": {c: money(v, dec[c]) for c, v in paid.items()},
                 "driver_earnings_by_currency": {c: money(v, dec[c]) for c, v in drv.items()},
                 "company_profit_by_currency": {c: money(v, dec[c]) for c, v in profit.items()},
                 "unpaid_completed_orders": sum(o.payment_status != "PAID" for o in done),
                 "avg_trip_minutes": round(avg / 60, 2),
                 "note": "Never mixed across currencies. company_profit = commission (20% by default); refunds are not modelled."}]
    if name == "orders_by_period":
        agg = defaultdict(lambda: {"orders": 0, "completed": 0, "cancelled": 0, "gross": Decimal("0"), "profit": Decimal("0"), "dec": 3})
        for o in orders:
            local = o.created_at.astimezone(tz)
            key = (local.strftime("%Y-%m") if group == "month" else local.strftime("%Y-%m-%d"), o.currency)
            a = agg[key]
            a["orders"] += 1
            a["completed"] += o.order_status == S.COMPLETED
            a["cancelled"] += o.order_status == S.CANCELLED
            a["dec"] = o.currency_decimals
            if o.order_status == S.COMPLETED:
                a["gross"] += o.final_price
                a["profit"] += o.commission or 0
        return [{"period": k[0], "currency": k[1], "orders": v["orders"], "completed": v["completed"],
                 "cancelled": v["cancelled"], "trip_value": money(v["gross"], v["dec"]),
                 "company_profit": money(v["profit"], v["dec"])} for k, v in sorted(agg.items())]
    if name == "per_driver":
        agg = defaultdict(lambda: {"trips": 0, "gross": Decimal("0"), "earn": Decimal("0"), "seconds": 0, "dec": 3})
        for o in orders:
            if o.driver_id and o.order_status == S.COMPLETED:
                a = agg[(o.driver_id, o.currency)]
                a["trips"] += 1
                a["gross"] += o.final_price
                a["earn"] += o.driver_earning or 0
                a["seconds"] += o.actual_duration_seconds or 0
                a["dec"] = o.currency_decimals
        names = dict((await db.execute(select(User.id, User.full_name).where(User.id.in_({k[0] for k in agg} or [0])))).all())
        return [{"driver_id": k[0], "driver_name": names.get(k[0]), "currency": k[1], "trips": v["trips"],
                 "trip_value": money(v["gross"], v["dec"]), "driver_earning": money(v["earn"], v["dec"]), "trip_minutes": round(v["seconds"] / 60, 2)} for k, v in agg.items()]
    if name == "per_area":
        names = dict((await db.execute(select(ServiceArea.id, ServiceArea.name))).all())
        agg = defaultdict(lambda: {"orders": 0, "completed": 0})
        for o in orders:
            agg[o.service_area_id]["orders"] += 1
            agg[o.service_area_id]["completed"] += o.order_status == S.COMPLETED
        return [{"area_id": k, "area": names.get(k), **v} for k, v in agg.items()]
    if name == "no_driver":
        return [{"order_id": o.id, "created_at": dt(o.created_at), "area_id": o.service_area_id, "status": o.order_status,
                 "pickup_address": o.pickup_address} for o in orders if o.order_status in (S.NO_DRIVER_AVAILABLE, S.EXPIRED) and not o.driver_id]
    if name == "driver_hours":
        sessions = (await db.execute(select(DriverWorkSession).where(DriverWorkSession.online_at >= start, DriverWorkSession.online_at < end))).scalars().all()
        agg = defaultdict(int)
        for s in sessions:
            agg[s.driver_id] += int(((s.offline_at or utcnow()) - s.online_at).total_seconds())
        names = dict((await db.execute(select(User.id, User.full_name).where(User.id.in_(agg.keys() or [0])))).all())
        return [{"driver_id": k, "driver_name": names.get(k), "online_hours": round(v / 3600, 2)} for k, v in agg.items()]
    if name == "trips":
        return [{"order_id": o.id, "status": o.order_status, "service_type": o.service_type, "customer_id": o.customer_id,
                 "driver_id": o.driver_id, "trip_started_at": dt(o.trip_started_at), "trip_arrived_at": dt(o.trip_arrived_at),
                 "duration_seconds": o.actual_duration_seconds, "currency": o.currency, "final_price": fmt(o.final_price, o), "night_surcharge": fmt(o.night_surcharge, o), "commission": fmt(o.commission, o),
                 "customer_total": fmt(o.customer_total, o), "driver_earning": fmt(o.driver_earning, o),
                 "price_adjusted": o.price_adjusted} for o in orders]
    if name == "payments":
        rows = (await db.execute(select(Payment).where(Payment.created_at >= start, Payment.created_at < end).order_by(Payment.id))).scalars().all()
        return [{"payment_id": p.id, "kind": p.kind, "order_id": p.order_id, "subscription_id": p.subscription_id, "customer_id": p.customer_id,
                 "currency": p.currency, "amount": money(p.amount, p.currency_decimals), "gateway_fee": money(p.gateway_fee, p.currency_decimals),
                 "driver_amount": money(p.driver_amount, p.currency_decimals), "company_amount": money(p.company_amount, p.currency_decimals),
                 "method": p.payment_method, "status": p.payment_status, "paid_at": dt(p.paid_at), "created_at": dt(p.created_at)} for p in rows]
    if name == "payouts":
        rows = (await db.execute(select(DriverPayout).where(DriverPayout.created_at >= start, DriverPayout.created_at < end).order_by(DriverPayout.id))).scalars().all()
        names = dict((await db.execute(select(User.id, User.full_name).where(User.id.in_({r.driver_id for r in rows} or [0])))).all())
        return [{"payout_id": r.id, "payment_id": r.payment_id, "driver_id": r.driver_id, "driver_name": names.get(r.driver_id), "currency": r.currency,
                 "amount": money(r.amount, r.currency_decimals), "status": r.status, "simulated": r.simulated, "reference": r.reference,
                 "failure_reason": r.failure_reason, "transferred_at": dt(r.transferred_at)} for r in rows]
    if name == "subscriptions":
        rows = (await db.execute(select(Subscription).where(Subscription.created_at >= start, Subscription.created_at < end).order_by(Subscription.id))).scalars().all()
        return [{"subscription_id": x.id, "category": x.category, "customer_id": x.customer_id, "driver_id": x.driver_id, "status": x.status,
                 "currency": x.currency, "monthly_price": money(x.monthly_price, x.currency_decimals) if x.monthly_price is not None else None,
                 "customer_total": money(x.customer_total, x.currency_decimals) if x.customer_total is not None else None,
                 "driver_earning": money(x.driver_earning, x.currency_decimals) if x.driver_earning is not None else None,
                 "company_profit": money(x.commission, x.currency_decimals) if x.commission is not None else None,
                 "paid_until": x.paid_until.isoformat() if x.paid_until else None} for x in rows]
    raise HTTPException(404, f"Unknown report. Available: {', '.join(REPORTS)}")


@router.get("/reports/{name}")
async def report(name: str, date_from: date = Query(...), date_to: date = Query(...), group: str = "day",
                 format: str = "json", admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = await build_report(db, name, date_from, date_to, group)
    if format == "json":
        return {"report": name, "from": date_from, "to": date_to, "rows": rows}
    cols = list(rows[0].keys()) if rows else []
    if format == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
        return StreamingResponse(iter(["\ufeff" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})
    if format == "xlsx":
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.append(cols)
        for r in rows:
            ws.append([r.get(c) for c in cols])
        out = io.BytesIO()
        wb.save(out)
        out.seek(0)
        return StreamingResponse(out, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                 headers={"Content-Disposition": f'attachment; filename="{name}.xlsx"'})
    raise HTTPException(422, "format must be json, csv or xlsx")

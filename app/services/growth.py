"""v27: growth & revenue figures for the admin dashboard and the investor exports (PDF / XLSX)."""
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import OrderStatus as S
from ..constants import Role
from ..models import DriverProfile, Order, Partner, PartnerStatement, Payment, User, WalletEntry
from . import settings_service

Q = Decimal("0.001")
CANCEL_LABELS = {
    "ORDER_TIMEOUT": ("انتهت المهلة دون سائق", "Timed out, no driver"), "CUSTOMER": ("ألغاه العميل", "Cancelled by customer"),
    "DRIVER": ("ألغاه السائق", "Cancelled by driver"), "ADMIN": ("ألغته الإدارة", "Cancelled by admin"), "OTHER": ("أخرى", "Other")}


def _m(v) -> str:
    return str(Decimal(v or 0).quantize(Q))


def _pct(cur, prev):
    cur, prev = float(cur or 0), float(prev or 0)
    return None if prev == 0 else round((cur - prev) / prev * 100, 1)


def _cancel_code(role: str | None, status: str) -> str:
    if status == S.EXPIRED or role == "SYSTEM":
        return "ORDER_TIMEOUT"
    return role if role in ("CUSTOMER", "DRIVER", "ADMIN") else "OTHER"


def _window(days: int):
    tz = ZoneInfo(settings.timezone)
    end_l = (datetime.now(tz) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_l = end_l - timedelta(days=days)
    return tz, start_l, end_l, start_l - timedelta(days=days)


async def compute(db: AsyncSession, days: int, lang: str = "ar") -> dict:
    tz, start_l, end_l, prev_l = _window(days)
    start, end, prev = (x.astimezone(UTC) for x in (start_l, end_l, prev_l))
    s = await settings_service.get_all(db)
    cur_name = s.get("currency_code", "BHD")

    created = (await db.execute(select(Order.created_at, Order.order_status, Order.cancelled_by_role, Order.customer_id)
                                .where(Order.created_at >= prev, Order.created_at < end))).all()
    done = (await db.execute(
        select(Order.id, Order.completed_at, Order.customer_id, Order.driver_id, Order.actual_duration_seconds, Order.discount_amount,
               Payment.amount, Payment.company_amount, Payment.driver_amount)
        .outerjoin(Payment, (Payment.order_id == Order.id) & (Payment.payment_status == "PAID"))
        .where(Order.order_status == S.COMPLETED, Order.completed_at >= prev, Order.completed_at < end))).all()
    new_users = (await db.execute(select(User.created_at).where(User.role == Role.CUSTOMER, User.created_at >= prev, User.created_at < end))).scalars().all()
    refunds = (await db.execute(select(WalletEntry.created_at, WalletEntry.amount).where(
        WalletEntry.kind == "REFUND", WalletEntry.created_at >= prev, WalletEntry.created_at < end))).all()

    def block(lo, hi):
        c = [r for r in created if lo <= r.created_at < hi]
        d = [r for r in done if lo <= r.completed_at < hi]
        cancelled = [r for r in c if r.order_status in (S.CANCELLED, S.EXPIRED)]
        per_customer = Counter(r.customer_id for r in d)
        commission = sum((Decimal(r.company_amount or 0) for r in d), Decimal(0))
        gmv = sum((Decimal(r.amount or 0) for r in d), Decimal(0))
        newc = sum(1 for t in new_users if lo <= t < hi)
        spend = Decimal(s.get("marketing_spend_monthly") or 0) * Decimal(days) / Decimal(30)
        return {
            "bookings": len(c), "completed": len(d), "commission": commission, "gmv": gmv,
            "active_drivers": len({r.driver_id for r in d if r.driver_id}), "active_customers": len(per_customer), "new_customers": newc,
            "cac": (spend / newc) if spend > 0 and newc else None,
            "repeat_rate": round(sum(1 for n in per_customer.values() if n >= 2) / len(per_customer) * 100, 1) if per_customer else 0.0,
            "completion_rate": round(len(d) / len(c) * 100, 1) if c else 0.0,
            "cancel_rate": round(len(cancelled) / len(c) * 100, 1) if c else 0.0,
            "refunds": sum((Decimal(a) for t, a in refunds if lo <= t < hi and a > 0), Decimal(0)),
            "_done": d, "_created": c, "_cancelled": cancelled,
        }

    cur, old = block(start, end), block(prev, start)
    money_keys = {"commission", "gmv", "cac", "refunds"}
    kpis = {}
    for k in ("bookings", "completed", "commission", "gmv", "active_drivers", "active_customers", "new_customers", "cac",
              "repeat_rate", "completion_rate", "cancel_rate", "refunds"):
        v, o = cur[k], old[k]
        kpis[k] = {"value": None if v is None else (_m(v) if k in money_keys else v), "change_pct": None if v is None or o is None else _pct(v, o)}

    # daily series (per day, or per week for long periods)
    weekly = days > 90
    buckets: dict = defaultdict(lambda: [0, 0, Decimal(0)])
    for r in cur["_created"]:
        buckets[_bucket(r.created_at, tz, start_l, weekly)][0] += 1
    for r in cur["_done"]:
        b = buckets[_bucket(r.completed_at, tz, start_l, weekly)]
        b[1] += 1
        b[2] += Decimal(r.company_amount or 0)
    step = 7 if weekly else 1
    daily = []
    for i in range(0, days, step):
        d0 = (start_l + timedelta(days=i)).date()
        b = buckets.get(d0, [0, 0, Decimal(0)])
        daily.append({"date": d0.isoformat(), "bookings": b[0], "completed": b[1], "commission": _m(b[2])})

    cancels = Counter(_cancel_code(r.cancelled_by_role, r.order_status) for r in cur["_cancelled"])
    li = 1 if lang == "en" else 0
    cancellations = [{"code": c, "label": CANCEL_LABELS[c][li], "count": n} for c, n in cancels.most_common()]

    # partners: share of the period's net profit; paid = statements marked paid in the period
    referral_cost = max(Decimal(0), Decimal((await db.execute(select(func.coalesce(func.sum(WalletEntry.amount), 0)).where(
        WalletEntry.kind.in_(("REFERRAL_INVITER", "REFERRAL_INVITEE", "REVOKED")), WalletEntry.created_at >= start, WalletEntry.created_at < end))).scalar_one() or 0))
    net_profit = max(Decimal(0), cur["commission"] - referral_cost)
    partners = []
    for p in (await db.execute(select(Partner).where(Partner.is_active.is_(True)).order_by(Partner.id))).scalars().all():
        paid = Decimal((await db.execute(select(func.coalesce(func.sum(PartnerStatement.amount), 0)).where(
            PartnerStatement.partner_id == p.id, PartnerStatement.status == "PAID", PartnerStatement.paid_at >= start, PartnerStatement.paid_at < end))).scalar_one() or 0)
        partners.append({"name": p.name, "percent": f"{p.percent:.2f}", "trips": cur["completed"], "share": _m(net_profit * p.percent / 100), "paid": _m(paid)})

    # investor metrics
    d = cur["_done"]
    n = len(d) or 0
    durations = [r.actual_duration_seconds for r in d if r.actual_duration_seconds]
    hours = Counter(r.completed_at.astimezone(tz).hour for r in d)
    mix = (await db.execute(select(Payment.payment_method, func.count(), func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.kind == "ORDER", Payment.payment_status == "PAID", Payment.paid_at >= start, Payment.paid_at < end).group_by(Payment.payment_method))).all()
    drivers_total = (await db.execute(select(func.count()).select_from(User).where(User.role == Role.DRIVER))).scalar_one()
    customers_total = (await db.execute(select(func.count()).select_from(User).where(User.role == Role.CUSTOMER))).scalar_one()
    wallet_liability = Decimal((await db.execute(select(func.coalesce(func.sum(WalletEntry.amount), 0)))).scalar_one() or 0)
    driver_debt = Decimal((await db.execute(select(func.coalesce(func.sum(DriverProfile.debt_balance), 0)))).scalar_one() or 0)
    promo_cost = sum((Decimal(r.discount_amount or 0) for r in d), Decimal(0))
    investor = {
        "avg_fare": _m(cur["gmv"] / n) if n else None,
        "avg_commission_per_trip": _m(cur["commission"] / n) if n else None,
        "take_rate_pct": round(float(cur["commission"] / cur["gmv"] * 100), 1) if cur["gmv"] else None,
        "driver_payouts": _m(sum((Decimal(r.driver_amount or 0) for r in d), Decimal(0))),
        "promo_discounts": _m(promo_cost), "referral_cost": _m(referral_cost), "net_profit": _m(net_profit),
        "avg_trip_minutes": round(sum(durations) / len(durations) / 60, 1) if durations else None,
        "trips_per_active_driver": round(n / cur["active_drivers"], 1) if cur["active_drivers"] else None,
        "trips_per_active_customer": round(n / cur["active_customers"], 1) if cur["active_customers"] else None,
        "customers_total": customers_total, "drivers_total": drivers_total,
        "wallet_liability": _m(wallet_liability), "driver_debt_owed_to_company": _m(driver_debt),
        "peak_hours": [{"hour": h, "trips": c} for h, c in hours.most_common(5)],
        "payment_mix": [{"method": m or "UNKNOWN", "payments": c, "amount": _m(a)} for m, c, a in mix],
    }
    return {"currency": cur_name, "days": days, "from": start_l.date().isoformat(), "to": (end_l - timedelta(days=1)).date().isoformat(),
            "kpis": kpis, "daily": daily,
            "by_country": [{"country": "BH", "completed": cur["completed"], "gmv": _m(cur["gmv"]), "commission": _m(cur["commission"])}],
            "partners": partners, "cancellations": cancellations, "investor": investor}


def _bucket(dt, tz, start_l, weekly: bool):
    d = dt.astimezone(tz).date()
    if not weekly:
        return d
    return (start_l.date() + timedelta(days=((d - start_l.date()).days // 7) * 7))

"""AI operations manager. Rule-based and deterministic: watches orders, drivers, quotes and prices, raises findings with a suggested action,
and writes management reports. It only SUGGESTS: it never moves money, bans anyone or changes an order. Every finding is audited under actor "AI"."""
import asyncio
import logging
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ..constants import OrderStatus as S
from ..database import SessionLocal
from ..models import DriverProfile, OpsFinding, OpsReport, Order, ServiceQuote, utcnow
from .audit import audit

log = logging.getLogger("ops-manager")
T = {"no_driver_min": 5, "no_driver_high_min": 10, "late_min": 20, "silent_min": 10, "long_trip_min": 60, "quote_wait_min": 30, "surge_orders": 5, "price_ratio": 4.0}
SEARCHING = [S.PENDING, S.OFFERED, S.NO_DRIVER_AVAILABLE]


def _mins(since: datetime | None, now: datetime) -> float:
    return (now - since).total_seconds() / 60 if since else 0.0


async def _add(db, found: list, kind: str, severity: str, etype: str, eid, message: str, suggestion: str | None = None, data: dict | None = None) -> None:
    """One OPEN/ACKED finding per (kind, entity): the same problem is never raised twice."""
    ex = (await db.execute(select(OpsFinding.id).where(OpsFinding.kind == kind, OpsFinding.entity_id == str(eid), OpsFinding.status.in_(["OPEN", "ACKED"])))).first()
    if ex:
        return
    f = OpsFinding(kind=kind, severity=severity, entity_type=etype, entity_id=str(eid), message=message[:400], suggestion=(suggestion or "")[:600] or None, data=data)
    db.add(f)
    await db.flush()
    audit(db, None, "AI_FINDING", "ops_finding", f.id, details={"kind": kind, "entity": f"{etype}:{eid}", "actor": "AI"})
    found.append(f)


async def _candidates(db, o: Order) -> list[str]:
    try:
        from .dispatch import eligible_drivers
        return [f"{d.full_name} (#{d.id})" for d in (await eligible_drivers(db, o))[:3]]
    except Exception:  # noqa: BLE001
        return []


async def scan(db) -> list[OpsFinding]:
    now = utcnow()
    found: list[OpsFinding] = []
    # 1 - orders nobody takes (+ intelligent-dispatch suggestion: who could take it)
    for o in (await db.execute(select(Order).where(Order.order_status.in_(SEARCHING), Order.created_at < now - timedelta(minutes=T["no_driver_min"])).limit(100))).scalars():
        m = _mins(o.created_at, now)
        cands = await _candidates(db, o)
        if o.service_code and o.service_code != "RIDES":
            sug = "Assign manually: POST /api/admin/services/orders/{id}/assign. Candidates: " + (", ".join(cands) or "none online")
        else:
            sug = ("Candidates now: " + ", ".join(cands) + ". Re-dispatch or contact them.") if cands else "No eligible driver online in the area: consider a driver incentive or telling the customer."
        await _add(db, found, "NO_DRIVER", "HIGH" if m >= T["no_driver_high_min"] else "MEDIUM", "order", o.id, f"Order #{o.id} has had no driver for {int(m)} min ({o.order_status}).", sug)
    # 2 - accepted but the driver is late
    for o in (await db.execute(select(Order).where(Order.order_status.in_([S.ACCEPTED, S.DRIVER_ON_THE_WAY]), Order.accepted_at < now - timedelta(minutes=T["late_min"])).limit(100))).scalars():
        await _add(db, found, "DRIVER_LATE", "MEDIUM", "order", o.id, f"Driver #{o.driver_id} accepted order #{o.id} {int(_mins(o.accepted_at, now))} min ago and has not arrived.",
                   "Call the driver; if unreachable, reassign the order.")
    # 3 - trips taking far longer than estimated
    for o in (await db.execute(select(Order).where(Order.order_status == S.IN_PROGRESS, Order.trip_started_at.is_not(None)).limit(200))).scalars():
        limit = max(T["long_trip_min"], 2 * (o.est_duration_min or 0))
        if _mins(o.trip_started_at, now) > limit:
            await _add(db, found, "LONG_TRIP", "LOW", "order", o.id, f"Order #{o.id} has been in progress {int(_mins(o.trip_started_at, now))} min (estimate {o.est_duration_min or '?'} min).",
                       "Check with the driver and the customer.")
    # 4 - drivers online but silent (LIVE mode without a recent location)
    for dp in (await db.execute(select(DriverProfile).where(DriverProfile.is_online.is_(True), DriverProfile.mode == "LIVE",
                                                            DriverProfile.last_location_at < now - timedelta(minutes=T["silent_min"])).limit(100))).scalars():
        await _add(db, found, "DRIVER_SILENT", "LOW", "driver", dp.user_id, f"Driver #{dp.user_id} is LIVE but sent no location for {int(_mins(dp.last_location_at, now))} min.",
                   "Probably poor signal or app closed: no action needed unless it has an active order.")
    # 5 - manual quotes waiting for pricing
    for q in (await db.execute(select(ServiceQuote).where(ServiceQuote.status == "MANUAL_PENDING", ServiceQuote.created_at < now - timedelta(minutes=T["quote_wait_min"])).limit(50))).scalars():
        await _add(db, found, "QUOTE_WAITING", "MEDIUM", "quote", q.id, f"{q.service_code} quote #{q.id} has waited {int(_mins(q.created_at, now))} min for a price.",
                   "Price it: POST /api/admin/services/quotes/{id}/price.")
    # 6 - pricing monitor: price per km far from the 7-day median
    rows = (await db.execute(select(Order.id, Order.customer_total, Order.est_distance_km, Order.completed_at).where(
        Order.order_status == S.COMPLETED, Order.completed_at > now - timedelta(days=7), Order.est_distance_km > 1, Order.customer_total > 0).limit(3000))).all()
    ppk = [(r.id, float(r.customer_total) / float(r.est_distance_km), r.completed_at) for r in rows]
    if len(ppk) >= 30:
        med = statistics.median(v for _, v, _ in ppk)
        for oid, v, at in ppk:
            if at > now - timedelta(hours=24) and (v > med * T["price_ratio"] or v < med / T["price_ratio"]):
                await _add(db, found, "PRICE_ANOMALY", "MEDIUM", "order", oid, f"Order #{oid} cost {v:.3f}/km vs a 7-day median of {med:.3f}/km.",
                           "Review the trip (wrong distance, waiting time, agreed price). Nothing was changed.")
    # 7 - demand surge per area
    areas = Counter(a for (a,) in (await db.execute(select(Order.service_area_id).where(Order.order_status.in_(SEARCHING)))).all() if a)
    for area, n in areas.items():
        if n >= T["surge_orders"]:
            await _add(db, found, "DEMAND_SURGE", "MEDIUM", "area", area, f"{n} orders are waiting for a driver in service area #{area}.",
                       "Consider a driver incentive or enabling peak pricing for this area (suggestion only).")
    return found


async def build_report(db, hours: int = 24) -> dict:
    end = utcnow()
    start = end - timedelta(hours=hours)
    base = Order.created_at >= start
    cnt = dict((await db.execute(select(Order.order_status, func.count()).where(base).group_by(Order.order_status))).all())
    total = sum(cnt.values())
    done = cnt.get(S.COMPLETED, 0)
    rev = (await db.execute(select(func.coalesce(func.sum(Order.customer_total), 0), func.coalesce(func.sum(Order.commission), 0)).where(
        Order.order_status == S.COMPLETED, Order.completed_at >= start))).one()
    accept = [(a - c).total_seconds() / 60 for c, a in (await db.execute(select(Order.created_at, Order.accepted_at).where(base, Order.accepted_at.is_not(None)).limit(5000))).all()]
    svc_col = func.coalesce(Order.service_code, "RIDES")  # one expression object: same bind param in SELECT and GROUP BY
    by_service = dict((await db.execute(select(svc_col, func.count()).where(base).group_by(svc_col))).all())
    online = (await db.execute(select(func.count()).select_from(DriverProfile).where(DriverProfile.is_online.is_(True)))).scalar_one()
    open_f = dict((await db.execute(select(OpsFinding.kind, func.count()).where(OpsFinding.status == "OPEN").group_by(OpsFinding.kind))).all())
    m = {"hours": hours, "orders": total, "completed": done, "cancelled": cnt.get(S.CANCELLED, 0), "expired": cnt.get(S.EXPIRED, 0),
         "no_driver_now": cnt.get(S.NO_DRIVER_AVAILABLE, 0), "completion_rate_pct": round(100 * done / total, 1) if total else None,
         "avg_minutes_to_accept": round(sum(accept) / len(accept), 1) if accept else None, "revenue": f"{Decimal(rev[0]):.3f}", "commission": f"{Decimal(rev[1]):.3f}",
         "by_service": by_service, "drivers_online": online, "open_findings": open_f}
    txt = (f"تقرير العمليات لآخر {hours} ساعة: {total} طلب، المكتمل {done}"
           + (f" ({m['completion_rate_pct']}%)" if total else "") + f"، الملغي {m['cancelled']}، المنتهي دون سائق {m['expired']}. "
           f"الإيراد {m['revenue']} د.ب والعمولة {m['commission']} د.ب. "
           + (f"متوسط زمن قبول الطلب {m['avg_minutes_to_accept']} دقيقة. " if accept else "")
           + f"السائقون المتصلون الآن {online}. "
           + (("ملاحظات مفتوحة: " + "، ".join(f"{k} ({v})" for k, v in open_f.items()) + ".") if open_f else "لا توجد ملاحظات مفتوحة."))
    return {"start": start, "end": end, "metrics": m, "text": txt}


async def ops_loop() -> None:
    await asyncio.sleep(30)
    while True:
        try:
            async with SessionLocal() as db:
                n = len(await scan(db))
                last = (await db.execute(select(func.max(OpsReport.created_at)))).scalar_one()
                if not last or (utcnow() - last) > timedelta(hours=20) and 2 <= (datetime.now(UTC).hour + 3) % 24 < 5:  # daily report in the 02:00-05:00 Bahrain night window
                    r = await build_report(db, 24)
                    db.add(OpsReport(period_start=r["start"], period_end=r["end"], metrics=r["metrics"], text_ar=r["text"]))
                await db.commit()
                if n:
                    log.info("ops manager raised %d finding(s)", n)
        except Exception:  # noqa: BLE001
            log.exception("ops manager cycle failed")
        await asyncio.sleep(180)

"""v24: automatic driver-fraud scan (rule based, runs hourly). Findings are FraudFlag rows for an admin to confirm or dismiss."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OrderStatus as S
from ..models import FraudFlag, Order, Referral, utcnow
from .payments import notify_admins

WINDOW_DAYS = 7
RECENT_DAYS = 7  # the same rule is not raised again for a driver within this period (even after a dismissal)
PAIR_TRIPS = 5  # same driver + same customer
SHORT_SECONDS = 120
SHORT_TRIPS = 5
PADDING_RATIO = 2.5
PADDING_MIN_SECONDS = 900
PADDING_TRIPS = 3
DRIVER_CANCELS = 5
REFERRAL_TRIPS = 2


async def _raise(db: AsyncSession, driver_id: int, rule: str, severity: str, summary: str, details: dict) -> bool:
    since = utcnow() - timedelta(days=RECENT_DAYS)
    exists = (await db.execute(select(FraudFlag.id).where(FraudFlag.driver_id == driver_id, FraudFlag.rule == rule,
                                                          FraudFlag.created_at >= since).limit(1))).first()
    if exists:
        return False
    db.add(FraudFlag(driver_id=driver_id, rule=rule, severity=severity, summary=summary[:300], details=details))
    return True


async def scan(db: AsyncSession) -> dict:
    since = utcnow() - timedelta(days=WINDOW_DAYS)
    done = (Order.order_status == S.COMPLETED, Order.completed_at >= since, Order.driver_id.is_not(None))
    new: list[tuple[str, int]] = []

    # 1) same customer again and again with the same driver (collusion for promos / referral / incentives)
    rows = (await db.execute(select(Order.driver_id, Order.customer_id, func.count(), func.min(Order.final_price),
                                    func.sum(func.coalesce(Order.promo_percent, 0) + func.coalesce(Order.promo_fixed, 0)))
                             .where(*done).group_by(Order.driver_id, Order.customer_id).having(func.count() >= PAIR_TRIPS))).all()
    for d, c, n, _, promo in rows:
        ids = list((await db.execute(select(Order.id).where(*done, Order.driver_id == d, Order.customer_id == c).limit(20))).scalars())
        if await _raise(db, d, "PAIR_REPEAT", "HIGH" if promo else "MEDIUM", f"{n} trips with the same customer in {WINDOW_DAYS} days",
                        {"customer_id": c, "trips": n, "with_promo": bool(promo), "order_ids": ids}):
            new.append(("PAIR_REPEAT", d))

    # 2) many ultra-short completed trips (fake trips)
    rows = (await db.execute(select(Order.driver_id, func.count()).where(*done, Order.actual_duration_seconds < SHORT_SECONDS)
                             .group_by(Order.driver_id).having(func.count() >= SHORT_TRIPS))).all()
    for d, n in rows:
        ids = list((await db.execute(select(Order.id).where(*done, Order.driver_id == d, Order.actual_duration_seconds < SHORT_SECONDS).limit(20))).scalars())
        if await _raise(db, d, "SHORT_TRIPS", "HIGH", f"{n} trips shorter than {SHORT_SECONDS}s in {WINDOW_DAYS} days", {"trips": n, "order_ids": ids}):
            new.append(("SHORT_TRIPS", d))

    # 3) trip time far above the route estimate (padding the time-based price)
    pad = (Order.actual_duration_seconds > PADDING_MIN_SECONDS, Order.est_duration_min > 0,
           Order.actual_duration_seconds > Order.est_duration_min * 60 * PADDING_RATIO)
    rows = (await db.execute(select(Order.driver_id, func.count()).where(*done, *pad).group_by(Order.driver_id)
                             .having(func.count() >= PADDING_TRIPS))).all()
    for d, n in rows:
        ids = list((await db.execute(select(Order.id).where(*done, *pad, Order.driver_id == d).limit(20))).scalars())
        if await _raise(db, d, "PADDED_DURATION", "MEDIUM", f"{n} trips took over {PADDING_RATIO}x the estimate", {"trips": n, "order_ids": ids}):
            new.append(("PADDED_DURATION", d))

    # 4) driver cancels after reaching the pickup point again and again
    rows = (await db.execute(select(Order.driver_id, func.count()).where(
        Order.order_status == S.CANCELLED, Order.cancelled_by_role == "DRIVER", Order.driver_arrived_at.is_not(None),
        Order.cancelled_at >= since, Order.driver_id.is_not(None)).group_by(Order.driver_id).having(func.count() >= DRIVER_CANCELS))).all()
    for d, n in rows:
        if await _raise(db, d, "DRIVER_CANCELS", "MEDIUM", f"{n} cancellations by the driver after arriving at pickup", {"cancellations": n}):
            new.append(("DRIVER_CANCELS", d))

    # 5) the driver's own invitees ride with him (referral reward farming)
    rows = (await db.execute(select(Order.driver_id, func.count(), func.count(func.distinct(Order.customer_id)))
                             .join(Referral, (Referral.inviter_id == Order.driver_id) & (Referral.invitee_id == Order.customer_id))
                             .where(*done).group_by(Order.driver_id).having(func.count() >= REFERRAL_TRIPS))).all()
    for d, n, people in rows:
        if await _raise(db, d, "OWN_REFERRALS", "HIGH", f"{n} trips with {people} customers he invited", {"trips": n, "customers": people}):
            new.append(("OWN_REFERRALS", d))

    if new:
        await notify_admins(db, "FRAUD_FLAGS", "اشتباه احتيال سائق", f"تم رصد {len(new)} مؤشرات احتيال جديدة تحتاج مراجعة.", data={"count": len(new)})
    return {"new_flags": len(new), "by_rule": {r: sum(1 for x, _ in new if x == r) for r in {x for x, _ in new}}}

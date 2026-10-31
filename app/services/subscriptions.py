"""Monthly subscriptions (WORK / BUSINESS / SCHOOL). One payment = one period (subscription_period_days, default 30).
Flow: customer requests -> admin quotes (price per route/distance + driver) -> customer pays (Tap/BenefitPay)
-> ACTIVE and trip rows are generated for the paid period -> driver completes each trip -> renew before it ends."""
from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Notification, Payment, Subscription, SubscriptionTrip, utcnow
from . import settings_service
from .notifications import notify
from .payments import create_payment
from .pricing import calc_commission

BAHRAIN = ZoneInfo("Asia/Bahrain")


def today_bh() -> date:
    return utcnow().astimezone(BAHRAIN).date()


async def quote(db: AsyncSession, sub: Subscription, price: Decimal, driver_id: int, starts_on: date) -> Payment:
    """Admin sets the monthly price for this route and the driver. Commission rules are snapshotted here."""
    s = await settings_service.get_all(db)
    sub.commission_type, sub.commission_value, sub.commission_mode = "PERCENT", Decimal(s["subscription_commission_value"]), s["commission_mode"]
    sub.monthly_price, sub.driver_id, sub.starts_on, sub.status = price, driver_id, starts_on, "QUOTED"
    sub.commission, sub.customer_total, sub.driver_earning = calc_commission(
        price, sub.commission_type, sub.commission_value, sub.commission_mode, sub.currency_decimals)
    pay = await create_payment(db, kind="SUBSCRIPTION", subscription_id=sub.id, customer_id=sub.customer_id, driver_id=driver_id,
                               amount=sub.customer_total, driver_amount=sub.driver_earning, company_amount=sub.commission,
                               currency=sub.currency, decimals=sub.currency_decimals)
    await notify(db, sub.customer_id, "SUBSCRIPTION_QUOTED", "تم تسعير اشتراكك",
                 f"{sub.customer_total:.{sub.currency_decimals}f} {sub.currency} / شهر — يرجى السداد للتفعيل")
    return pay


async def open_payment(db: AsyncSession, sub: Subscription) -> Payment | None:
    return (await db.execute(select(Payment).where(
        Payment.subscription_id == sub.id, Payment.payment_status != "PAID").order_by(Payment.id.desc()))).scalars().first()


async def renew(db: AsyncSession, sub: Subscription) -> Payment:
    """Creates the next month's payment (same snapshotted price). Reuses an unpaid one if it already exists."""
    if sub.status not in ("ACTIVE", "EXPIRED"):
        raise HTTPException(409, {"code": "NOT_RENEWABLE", "status": sub.status})
    existing = await open_payment(db, sub)
    if existing:
        return existing
    return await create_payment(db, kind="SUBSCRIPTION", subscription_id=sub.id, customer_id=sub.customer_id, driver_id=sub.driver_id,
                                amount=sub.customer_total, driver_amount=sub.driver_earning, company_amount=sub.commission,
                                currency=sub.currency, decimals=sub.currency_decimals)


async def generate_trips(db: AsyncSession, sub: Subscription, start: date, end: date) -> int:
    """Creates SCHEDULED trip rows for [start, end] on the subscription weekdays (idempotent)."""
    have = {(t.trip_date, t.leg) for t in (await db.execute(select(SubscriptionTrip).where(
        SubscriptionTrip.subscription_id == sub.id, SubscriptionTrip.trip_date >= start,
        SubscriptionTrip.trip_date <= end))).scalars()}
    n, d = 0, start
    while d <= end:
        if d.weekday() in (sub.weekdays or []):
            legs = [("GO", sub.pickup_time)] + ([("RETURN", sub.return_time)] if sub.return_trip and sub.return_time else [])
            for leg, tm in legs:
                if (d, leg) not in have:
                    db.add(SubscriptionTrip(subscription_id=sub.id, driver_id=sub.driver_id, trip_date=d, leg=leg, scheduled_time=tm))
                    n += 1
        d += timedelta(days=1)
    return n


async def on_paid(db: AsyncSession, payment: Payment) -> None:
    """Called once when a subscription payment becomes PAID: activates / extends the subscription by 30 days."""
    sub = await db.get(Subscription, payment.subscription_id)
    if not sub or sub.status in ("CANCELLED", "REJECTED"):
        return
    today = today_bh()
    if sub.paid_until and sub.paid_until >= today:
        first = sub.paid_until + timedelta(days=1)  # renewing early: the new period starts after the current one
    else:
        first = max(sub.starts_on or today, today)
    period = int((await settings_service.get_all(db))["subscription_period_days"])
    last = first + timedelta(days=period - 1)
    sub.paid_until, sub.status = last, "ACTIVE"
    await generate_trips(db, sub, first, last)
    await notify(db, sub.customer_id, "SUBSCRIPTION_ACTIVE", "تم تفعيل اشتراكك", f"ساري حتى {last.isoformat()}")
    if sub.driver_id:
        await notify(db, sub.driver_id, "SUBSCRIPTION_ASSIGNED", "اشتراك شهري جديد", f"اشتراك #{sub.id} حتى {last.isoformat()}")


async def cancel_future_trips(db: AsyncSession, sub_id: int) -> None:
    await db.execute(update(SubscriptionTrip).where(
        SubscriptionTrip.subscription_id == sub_id, SubscriptionTrip.status == "SCHEDULED",
        SubscriptionTrip.trip_date >= today_bh()).values(status="CANCELLED"))


async def sweep(db: AsyncSession) -> None:
    """Background: expire unpaid-after-period subscriptions, mark past uncompleted trips MISSED, remind about renewal."""
    today = today_bh()
    await db.execute(update(SubscriptionTrip).where(SubscriptionTrip.status == "SCHEDULED", SubscriptionTrip.trip_date < today)
                     .values(status="MISSED"))
    for sub in (await db.execute(select(Subscription).where(Subscription.status == "ACTIVE", Subscription.paid_until < today))).scalars():
        sub.status = "EXPIRED"
        await notify(db, sub.customer_id, "SUBSCRIPTION_EXPIRED", "انتهى اشتراكك", "جدّد الاشتراك لمتابعة الرحلات")
    soon = today + timedelta(days=int((await settings_service.get_all(db))["subscription_renewal_reminder_days"]))
    for sub in (await db.execute(select(Subscription).where(
            Subscription.status == "ACTIVE", Subscription.paid_until == soon))).scalars():
        msg = f"ينتهي في {sub.paid_until.isoformat()}"
        sent = (await db.execute(select(Notification.id).where(Notification.user_id == sub.customer_id,
                                                                Notification.notification_type == "SUBSCRIPTION_RENEW",
                                                                Notification.message == msg))).first()
        if not sent:
            await notify(db, sub.customer_id, "SUBSCRIPTION_RENEW", "اشتراكك ينتهي قريبًا", msg)

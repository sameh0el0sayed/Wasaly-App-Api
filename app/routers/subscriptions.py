"""Monthly subscriptions: customer (individual / parent), admin (price + driver), driver (daily trips)."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..database import get_db
from ..deps import admin_user, customer_user, driver_user, paging
from ..models import Subscription, SubscriptionTrip, User, utcnow
from ..schemas import ReasonIn, SubscriptionIn, SubscriptionQuoteIn
from ..serializers import payment_out, sub_trip_out, subscription_out
from ..services import settings_service
from ..services import subscriptions as svc
from ..services.audit import audit
from ..services.notifications import notify
from ..services.payments import notify_admins
from ..services.pricing import fits_decimals

router = APIRouter(prefix="/api/subscriptions", tags=["subscriptions"])
admin_router = APIRouter(prefix="/api/admin/subscriptions", tags=["admin-subscriptions"])
driver_router = APIRouter(prefix="/api/driver", tags=["driver-subscriptions"])


async def _mine(db, user, sub_id) -> Subscription:
    s = await db.get(Subscription, sub_id)
    if not s or s.customer_id != user.id:
        raise HTTPException(404, "Subscription not found")
    return s


async def _detail(db, s: Subscription, settings_map=None) -> dict:
    out = subscription_out(s, "customer")
    p = await svc.open_payment(db, s)
    out["open_payment"] = payment_out(p, settings_map) if p else None
    return out


# ---------------- customer ----------------

@router.post("", status_code=201)
async def request_subscription(body: SubscriptionIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Request only: the admin prices it by route/distance, then you pay the first month to activate it."""
    p = await settings_service.get_pricing(db)
    s = Subscription(customer_id=user.id, currency=p.currency, currency_decimals=p.decimals, **body.model_dump())
    db.add(s)
    await db.flush()
    await notify_admins(db, "SUBSCRIPTION_REQUEST", "طلب اشتراك جديد", f"اشتراك #{s.id} ({s.category})")
    await db.commit()
    return subscription_out(s, "customer")


@router.get("")
async def my_subscriptions(user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Subscription).where(Subscription.customer_id == user.id).order_by(Subscription.id.desc()))).scalars().all()
    return [subscription_out(s, "customer") for s in rows]


@router.get("/{sub_id}")
async def get_subscription(sub_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    s = await _mine(db, user, sub_id)
    return await _detail(db, s, await settings_service.get_all(db))


@router.get("/{sub_id}/trips")
async def subscription_trips(sub_id: int, page=Depends(paging), user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    s = await _mine(db, user, sub_id)
    limit, offset = page
    rows = (await db.execute(select(SubscriptionTrip).where(SubscriptionTrip.subscription_id == s.id)
                             .order_by(SubscriptionTrip.trip_date, SubscriptionTrip.leg).limit(limit).offset(offset))).scalars().all()
    return [sub_trip_out(t) for t in rows]


@router.post("/{sub_id}/renew")
async def renew_subscription(sub_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Creates (or returns) the unpaid payment for the next 30 days. Pay it with /api/payments/{id}/tap or /benefitpay."""
    s = await _mine(db, user, sub_id)
    p = await svc.renew(db, s)
    await db.commit()
    return payment_out(p, await settings_service.get_all(db))


@router.post("/{sub_id}/cancel")
async def cancel_subscription(sub_id: int, body: ReasonIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Stops future trips. Money already paid for the current period is not refunded automatically."""
    s = await _mine(db, user, sub_id)
    if s.status in ("CANCELLED", "REJECTED"):
        raise HTTPException(409, "Already closed")
    s.status = "CANCELLED"
    await svc.cancel_future_trips(db, s.id)
    await notify_admins(db, "SUBSCRIPTION_CANCELLED", "إلغاء اشتراك", f"اشتراك #{s.id}: {body.reason}")
    if s.driver_id:
        await notify(db, s.driver_id, "SUBSCRIPTION_CANCELLED", "تم إلغاء اشتراك", f"اشتراك #{s.id}")
    await db.commit()
    return subscription_out(s, "customer")


# ---------------- admin ----------------

@admin_router.get("")
async def admin_list(status: str | None = None, category: str | None = None, page=Depends(paging),
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(Subscription)
    if status:
        q = q.where(Subscription.status == status)
    if category:
        q = q.where(Subscription.category == category)
    rows = (await db.execute(q.order_by(Subscription.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [subscription_out(s, "admin") for s in rows]


@admin_router.get("/{sub_id}")
async def admin_get(sub_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Subscription, sub_id)
    if not s:
        raise HTTPException(404, "Subscription not found")
    return subscription_out(s, "admin")


async def _driver(db, driver_id) -> User:
    d = await db.get(User, driver_id)
    if not d or d.role != Role.DRIVER or d.account_status != AccountStatus.APPROVED:
        raise HTTPException(422, "driver_id must be an approved driver")
    return d


@admin_router.post("/{sub_id}/quote")
async def admin_quote(sub_id: int, body: SubscriptionQuoteIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Sets the monthly price for this route/distance and the driver. The customer then pays the first month."""
    s = await db.get(Subscription, sub_id)
    if not s:
        raise HTTPException(404, "Subscription not found")
    if s.status not in ("REQUESTED", "QUOTED"):
        raise HTTPException(409, {"code": "NOT_QUOTABLE", "status": s.status})
    if not fits_decimals(body.monthly_price, s.currency_decimals):
        raise HTTPException(422, f"monthly_price allows at most {s.currency_decimals} decimals")
    await _driver(db, body.driver_id)
    old = await svc.open_payment(db, s)
    if old:  # re-quote replaces the unpaid payment
        if old.payment_status in ("PENDING_VERIFICATION",):
            raise HTTPException(409, "A payment proof is awaiting confirmation")
        await db.delete(old)
        await db.flush()
    pay = await svc.quote(db, s, body.monthly_price, body.driver_id, body.starts_on or svc.today_bh())
    audit(db, admin.id, "SUBSCRIPTION_QUOTED", "subscription", s.id, details={"price": str(body.monthly_price), "driver_id": body.driver_id})
    await db.commit()
    return {**subscription_out(s, "admin"), "payment_id": pay.id}


@admin_router.post("/{sub_id}/reject")
async def admin_reject(sub_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Subscription, sub_id)
    if not s:
        raise HTTPException(404, "Subscription not found")
    if s.status not in ("REQUESTED", "QUOTED"):
        raise HTTPException(409, {"code": "NOT_REJECTABLE", "status": s.status})
    s.status, s.reject_reason = "REJECTED", body.reason
    audit(db, admin.id, "SUBSCRIPTION_REJECTED", "subscription", s.id, body.reason)
    await notify(db, s.customer_id, "SUBSCRIPTION_REJECTED", "تم رفض طلب الاشتراك", body.reason)
    await db.commit()
    return subscription_out(s, "admin")


@admin_router.post("/{sub_id}/pause")
async def admin_pause(sub_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Subscription, sub_id)
    if not s or s.status != "ACTIVE":
        raise HTTPException(409, "Only ACTIVE subscriptions can be paused")
    s.status = "PAUSED"
    await svc.cancel_future_trips(db, s.id)
    audit(db, admin.id, "SUBSCRIPTION_PAUSED", "subscription", s.id, body.reason)
    await db.commit()
    return subscription_out(s, "admin")


@admin_router.post("/{sub_id}/resume")
async def admin_resume(sub_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Subscription, sub_id)
    if not s or s.status != "PAUSED":
        raise HTTPException(409, "Only PAUSED subscriptions can be resumed")
    today = svc.today_bh()
    if s.paid_until and s.paid_until >= today:
        s.status = "ACTIVE"
        await svc.generate_trips(db, s, today, s.paid_until)
    else:
        s.status = "EXPIRED"
    audit(db, admin.id, "SUBSCRIPTION_RESUMED", "subscription", s.id)
    await db.commit()
    return subscription_out(s, "admin")


@admin_router.post("/{sub_id}/change-driver")
async def admin_change_driver(sub_id: int, driver_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    s = await db.get(Subscription, sub_id)
    if not s or s.status not in ("ACTIVE", "PAUSED", "QUOTED"):
        raise HTTPException(409, "Cannot change driver in this state")
    await _driver(db, driver_id)
    s.driver_id = driver_id
    pays = (await db.execute(select(svc.Payment).where(svc.Payment.subscription_id == s.id, svc.Payment.payment_status != "PAID"))).scalars().all()
    for p in pays:
        p.driver_id = driver_id
    for t in (await db.execute(select(SubscriptionTrip).where(SubscriptionTrip.subscription_id == s.id, SubscriptionTrip.status == "SCHEDULED",
                                                              SubscriptionTrip.trip_date >= svc.today_bh()))).scalars():
        t.driver_id = driver_id
    audit(db, admin.id, "SUBSCRIPTION_DRIVER_CHANGED", "subscription", s.id, details={"driver_id": driver_id})
    await notify(db, driver_id, "SUBSCRIPTION_ASSIGNED", "اشتراك شهري جديد", f"اشتراك #{s.id}")
    await db.commit()
    return subscription_out(s, "admin")


@admin_router.get("/{sub_id}/trips")
async def admin_trips(sub_id: int, page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    rows = (await db.execute(select(SubscriptionTrip).where(SubscriptionTrip.subscription_id == sub_id)
                             .order_by(SubscriptionTrip.trip_date, SubscriptionTrip.leg).limit(limit).offset(offset))).scalars().all()
    return [sub_trip_out(t) for t in rows]


# ---------------- driver ----------------

@driver_router.get("/subscriptions")
async def driver_subscriptions(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Subscription).where(Subscription.driver_id == user.id, Subscription.status.in_(("ACTIVE", "PAUSED")))
                             .order_by(Subscription.id.desc()))).scalars().all()
    return [subscription_out(s, "driver") for s in rows]


@driver_router.get("/subscription-trips")
async def driver_sub_trips(date: str | None = None, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Trips for one day (default: today, Bahrain time) with the rider details needed for pickup."""
    from datetime import date as _d
    day = _d.fromisoformat(date) if date else svc.today_bh()
    rows = (await db.execute(select(SubscriptionTrip, Subscription).join(Subscription, Subscription.id == SubscriptionTrip.subscription_id)
                             .where(SubscriptionTrip.driver_id == user.id, SubscriptionTrip.trip_date == day,
                                    SubscriptionTrip.status != "CANCELLED").order_by(SubscriptionTrip.scheduled_time))).all()
    out = []
    for t, s in rows:
        d = sub_trip_out(t)
        rev = t.leg == "RETURN"
        d.update({"category": s.category, "passenger_name": s.passenger_name, "school_name": s.school_name,
                  "passengers_count": s.passengers_count,
                  "from": s.destination_address if rev else s.pickup_address, "to": s.pickup_address if rev else s.destination_address})
        out.append(d)
    return out


@driver_router.post("/subscription-trips/{trip_id}/complete")
async def complete_sub_trip(trip_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(SubscriptionTrip, trip_id)
    if not t or t.driver_id != user.id:
        raise HTTPException(404, "Trip not found")
    if t.status != "SCHEDULED":
        raise HTTPException(409, {"code": "NOT_SCHEDULED", "status": t.status})
    if t.trip_date != svc.today_bh():
        raise HTTPException(409, {"code": "NOT_TODAY", "message": "Only today's trips can be completed"})
    t.status, t.completed_at = "COMPLETED", utcnow()
    await db.commit()
    return sub_trip_out(t)

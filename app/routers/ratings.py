"""Ratings: the customer rates the driver, the driver rates the customer. One rating per order per side.
A driver who collects the configured number of 1-star ratings is suspended automatically (admins are told)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import customer_user, driver_user
from ..models import Order, Rating, User, utcnow
from ..schemas import CUSTOMER_RATING_TAGS, DRIVER_RATING_TAGS, RatingIn
from ..services import settings_service
from ..services.audit import audit
from ..services.dispatch import force_offline, has_active_order
from ..services.notifications import notify
from ..services.payments import notify_admins

router = APIRouter(prefix="/api", tags=["ratings"])


def rating_out(r: Rating | None) -> dict | None:
    return None if not r else {"stars": r.stars, "tags": r.tags, "comment": r.comment, "created_at": r.created_at.isoformat()}


async def stats_for(db: AsyncSession, u: User) -> dict:
    """rating_avg / ratings_count / one_star_count (1-star count restarts after an admin reactivation). Counts only ratings received in the ACTIVE role."""
    other = Role.CUSTOMER if u.role == Role.DRIVER else Role.DRIVER
    row = (await db.execute(select(func.count(), func.avg(Rating.stars)).where(Rating.ratee_id == u.id, Rating.rater_role == other))).one()
    q = select(func.count()).where(Rating.ratee_id == u.id, Rating.rater_role == other, Rating.stars == 1)
    if u.rating_reset_at:
        q = q.where(Rating.created_at > u.rating_reset_at)
    return {"rating_avg": round(float(row[1]), 2) if row[1] is not None else None, "ratings_count": row[0],
            "one_star_count": (await db.execute(q)).scalar_one(), "auto_suspended": bool(u.auto_suspended)}


async def my_rating(db: AsyncSession, order_id: int, rater_id: int) -> dict | None:
    r = (await db.execute(select(Rating).where(Rating.order_id == order_id, Rating.rater_id == rater_id))).scalar_one_or_none()
    return rating_out(r)


async def _submit(db: AsyncSession, o: Order, rater: User, ratee_id: int, body: RatingIn, allowed_tags: set, enabled_key: str) -> dict:
    s = await settings_service.get_all(db)
    if s["ratings_enabled"] != "true" or s[enabled_key] != "true":
        raise HTTPException(409, {"code": "RATINGS_DISABLED"})
    if o.order_status != S.COMPLETED:
        raise HTTPException(409, {"code": "ORDER_NOT_COMPLETED"})
    if o.completed_at and utcnow() - o.completed_at > timedelta(hours=int(s["rating_window_hours"])):
        raise HTTPException(409, {"code": "RATING_WINDOW_CLOSED"})
    bad = [t for t in body.tags if t not in allowed_tags]
    if bad:
        raise HTTPException(422, {"code": "VALIDATION_ERROR", "errors": [{"field": "tags", "message": f"Unknown tags: {', '.join(bad)}"}]})
    if body.comment and len(body.comment) > int(s["rating_comment_max_chars"]):
        raise HTTPException(422, {"code": "VALIDATION_ERROR", "errors": [{"field": "comment", "message": "Comment too long"}]})
    r = Rating(order_id=o.id, rater_id=rater.id, ratee_id=ratee_id, rater_role=rater.role, stars=body.stars,
               tags=sorted(set(body.tags)), comment=(body.comment or "").strip() or None)
    db.add(r)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, {"code": "ALREADY_RATED"})
    ratee = await db.get(User, ratee_id)
    await _maybe_auto_suspend(db, ratee, s)
    await db.commit()
    return rating_out(r)


async def _maybe_auto_suspend(db: AsyncSession, u: User, s: dict) -> None:
    key = "auto_suspend_driver_one_star_count" if u.role == Role.DRIVER else "auto_suspend_customer_one_star_count"
    limit = int(s[key])
    if limit <= 0 or u.account_status != AccountStatus.APPROVED:
        return
    if (await stats_for(db, u))["one_star_count"] < limit:
        return
    if u.role == Role.DRIVER and await has_active_order(db, u.id):
        return  # never strand a customer mid-trip: the background sweep suspends the driver once the trip is over
    u.auto_suspend_count = (u.auto_suspend_count or 0) + 1
    ban_at = int(s["auto_ban_after_auto_suspensions"])
    banned = ban_at > 0 and u.auto_suspend_count >= ban_at
    if banned:  # repeat offender: permanent. The row (with its CPR hash and phone) stays, so re-registration is refused.
        u.account_status, u.status_reason, u.auto_suspended = AccountStatus.BANNED, "AUTO_BANNED_REPEATED_LOW_RATING", True
        u.token_version += 1  # permanent ban: sign out of every device
    else:
        u.account_status, u.status_reason, u.auto_suspended = AccountStatus.SUSPENDED, "AUTO_LOW_RATING", True
    if u.driver_profile:
        u.driver_profile.driver_status = u.account_status
    if u.role == Role.DRIVER:
        await force_offline(db, u.id)  # a trip already in progress is not touched
    audit(db, None, "AUTO_BANNED_LOW_RATING" if banned else "AUTO_SUSPENDED_LOW_RATING", "user", u.id, f"{limit} one-star rating(s)")
    await notify(db, u.id, "ACCOUNT_SUSPENDED", "تم حظر حسابك نهائيًا" if banned else "تم إيقاف حسابك مؤقتًا",
                 "بسبب تقييمات منخفضة متكررة." if banned else "بسبب تقييم منخفض. تواصل مع الإدارة لمراجعة حسابك.")
    await notify_admins(db, "AUTO_SUSPENDED", "إيقاف تلقائي بسبب التقييم", f"{u.full_name} (#{u.id}) وصل لحد التقييمات بنجمة واحدة")


@router.post("/orders/{order_id}/rating", status_code=201)
async def customer_rates_driver(order_id: int, body: RatingIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    if not o.driver_id:
        raise HTTPException(409, {"code": "ORDER_NOT_COMPLETED"})
    return await _submit(db, o, user, o.driver_id, body, CUSTOMER_RATING_TAGS, "ratings_enabled")


@router.post("/driver/orders/{order_id}/rating", status_code=201)
async def driver_rates_customer(order_id: int, body: RatingIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    o = await db.get(Order, order_id)
    if not o or o.driver_id != user.id:
        raise HTTPException(404, "Order not found")
    return await _submit(db, o, user, o.customer_id, body, DRIVER_RATING_TAGS, "driver_rates_customer_enabled")


async def sweep_auto_suspend(db: AsyncSession) -> None:
    """Background: applies the rule to drivers whose suspension was postponed because they were on a trip."""
    s = await settings_service.get_all(db)
    for role, key in ((Role.DRIVER, "auto_suspend_driver_one_star_count"), (Role.CUSTOMER, "auto_suspend_customer_one_star_count")):
        limit = int(s[key])
        if limit <= 0:
            continue
        ids = (await db.execute(select(Rating.ratee_id).join(User, User.id == Rating.ratee_id).where(
            Rating.stars == 1, User.role == role, User.account_status == AccountStatus.APPROVED)
            .group_by(Rating.ratee_id).having(func.count() >= limit))).scalars().all()
        for uid in ids:
            await _maybe_auto_suspend(db, await db.get(User, uid), s)

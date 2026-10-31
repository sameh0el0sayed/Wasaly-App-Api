from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import AccountStatus, ReviewStatus, Role
from ...database import get_db
from ...deps import admin_user, paging, super_admin_user
from ...models import AdminAuditLog, DriverServiceArea, Order, Rating, User, utcnow
from ...schemas import AdminCreateIn, ApproveIn, DebtSettleIn, ReasonIn, ReinstateIn, ResetPasswordIn
from ...security import cpr_hash, decrypt, hash_password
from ...serializers import driver_block, dt, money, user_public
from ...services import settings_service
from ...services.activity import is_online
from ...services.audit import audit
from ...services.dispatch import force_offline
from ...services.notifications import notify

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- users ----------------
@router.get("/users")
async def list_users(role: str | None = None, status: str | None = None, q: str | None = None, online: bool | None = None,
                     page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(User).where(User.role.in_([Role.CUSTOMER, Role.DRIVER]))
    if role:
        stmt = stmt.where(User.role == role)
    if status:
        stmt = stmt.where(User.account_status == status)
    win = int((await settings_service.get_all(db))["online_window_minutes"])
    if online is not None:
        edge = utcnow() - timedelta(minutes=win)
        stmt = stmt.where(User.last_seen_at >= edge) if online else stmt.where(or_(User.last_seen_at.is_(None), User.last_seen_at < edge))
    if q:
        cond = [User.full_name.ilike(f"%{q}%"), User.phone.ilike(f"%{q}%")]
        if q.isdigit() and len(q) == 9:  # exact CPR search via keyed hash
            cond.append(User.cpr_hash == cpr_hash(q))
        stmt = stmt.where(or_(*cond))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))).scalars().unique().all()
    ids = [u.id for u in rows]
    agg = {i: (c, a, o) for i, c, a, o in (await db.execute(
        select(Rating.ratee_id, func.count(), func.avg(Rating.stars), func.sum(case((Rating.stars == 1, 1), else_=0)))
        .where(Rating.ratee_id.in_(ids or [0])).group_by(Rating.ratee_id))).all()}
    rate = lambda u: {"rating_avg": round(float(agg[u.id][1]), 2) if u.id in agg else None,
                      "ratings_count": agg[u.id][0] if u.id in agg else 0, "one_star_count": int(agg[u.id][2] or 0) if u.id in agg else 0}
    return {"total": total, "items": [{**user_public(u), **rate(u), "created_at": dt(u.created_at), "online": is_online(u.last_seen_at, win), "is_online": is_online(u.last_seen_at, win), "auto_suspended": bool(u.auto_suspended),
                                          "last_seen_at": dt(u.last_seen_at), "app_open_count": u.app_open_count or 0} for u in rows]}


@router.get("/users/{user_id}")
async def get_user(user_id: int, reveal_cpr: bool = False, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user_id)
    if not u or u.role in Role.ADMINS:
        raise HTTPException(404, "User not found")
    from ..ratings import stats_for
    d = {**user_public(u), **(await stats_for(db, u)), "is_online": is_online(u.last_seen_at, int((await settings_service.get_all(db))["online_window_minutes"])), "cpr_photo": u.cpr_photo, "created_at": dt(u.created_at), "approved_at": dt(u.approved_at)}
    if reveal_cpr:
        if admin.role != Role.SUPER_ADMIN:
            raise HTTPException(403, "Only super admin can reveal full CPR")
        d["cpr"] = decrypt(u.cpr_encrypted)
        audit(db, admin.id, "REVEAL_CPR", "user", u.id)
        await db.commit()
    if u.role == Role.DRIVER:
        d["driver"] = driver_block(u, reveal=reveal_cpr)
        d["debt_balance"] = str(u.driver_profile.debt_balance or 0)
        d["service_area_ids"] = list((await db.execute(select(DriverServiceArea.service_area_id).where(DriverServiceArea.driver_id == u.id))).scalars())
    orders = (await db.execute(select(Order.id, Order.order_status, Order.created_at, Order.final_price, Order.currency, Order.currency_decimals)
                               .where(or_(Order.customer_id == u.id, Order.driver_id == u.id)).order_by(Order.id.desc()).limit(50))).all()
    d["recent_orders"] = [{"id": i, "status": s, "created_at": dt(c), "final_price": money(p, dec), "currency": cur} for i, s, c, p, cur, dec in orders]
    d["admin_actions"] = [{"action": a.action, "reason": a.reason, "admin_id": a.admin_id, "at": dt(a.created_at)} for a in
                          (await db.execute(select(AdminAuditLog).where(AdminAuditLog.entity_type == "user", AdminAuditLog.entity_id == str(u.id)).order_by(AdminAuditLog.id.desc()).limit(50))).scalars()]
    return d


async def _target(db, user_id) -> User:
    u = await db.get(User, user_id)
    if not u or u.role not in (Role.CUSTOMER, Role.DRIVER):
        raise HTTPException(404, "User not found")
    return u


async def _set_status(db, admin, u: User, status: str, reason: str | None, action: str, title: str, msg: str):
    if u.account_status == AccountStatus.BANNED and status != AccountStatus.BANNED:
        raise HTTPException(409, {"code": "PERMANENTLY_BANNED"})
    u.account_status, u.status_reason = status, reason
    if u.driver_profile:
        u.driver_profile.driver_status = status
    if status != AccountStatus.APPROVED and u.role == Role.DRIVER:
        await force_offline(db, u.id)
    audit(db, admin.id, action, "user", u.id, reason)
    await notify(db, u.id, action, title, msg + (f" السبب: {reason}" if reason else ""))


@router.post("/users/{user_id}/approve")
async def approve(user_id: int, body: ApproveIn = ApproveIn(), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, user_id)
    await _set_status(db, admin, u, AccountStatus.APPROVED, None, "ACCOUNT_APPROVED", "تمت الموافقة", "تمت الموافقة على حسابك.")
    u.approved_at, u.approved_by = utcnow(), admin.id
    if u.vehicle:
        u.vehicle.vehicle_status = AccountStatus.APPROVED
        if body.approve_pending_documents:
            for d in u.vehicle.documents:
                if d.review_status == ReviewStatus.PENDING:
                    d.review_status, d.reviewed_by, d.reviewed_at = ReviewStatus.APPROVED, admin.id, utcnow()
    await db.commit()
    return user_public(u)


@router.post("/users/{user_id}/reject")
async def reject(user_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, user_id)
    await _set_status(db, admin, u, AccountStatus.REJECTED, body.reason, "ACCOUNT_REJECTED", "تم رفض الحساب", "تم رفض حسابك.")
    await db.commit()
    return user_public(u)


@router.post("/users/{user_id}/request-correction")
async def request_correction(user_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, user_id)
    await _set_status(db, admin, u, AccountStatus.NEEDS_CORRECTION, body.reason, "ACCOUNT_NEEDS_CORRECTION", "مطلوب تعديل البيانات", "يرجى تعديل بياناتك.")
    await db.commit()
    return user_public(u)


@router.post("/users/{user_id}/suspend")
async def suspend(user_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, user_id)
    await _set_status(db, admin, u, AccountStatus.SUSPENDED, body.reason, "ACCOUNT_SUSPENDED", "تم إيقاف الحساب", "تم إيقاف حسابك.")
    await db.commit()
    return user_public(u)


@router.post("/users/{user_id}/reactivate")
async def reactivate(user_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, user_id)
    if u.auto_suspended:  # a fresh start for the 1-star rule; old ratings stay visible in the average
        u.auto_suspended, u.rating_reset_at = False, utcnow()
    await _set_status(db, admin, u, AccountStatus.APPROVED, None, "ACCOUNT_REACTIVATED", "تمت إعادة التفعيل", "تمت إعادة تفعيل حسابك.")
    await db.commit()
    return user_public(u)


@router.post("/users/{user_id}/reinstate")
async def reinstate_banned(user_id: int, body: ReinstateIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """The ONLY way out of a permanent ban: super admin only, after collecting `ban_reinstatement_fee` (a setting)."""
    u = await _target(db, user_id)
    if u.account_status != AccountStatus.BANNED:
        raise HTTPException(409, {"code": "NOT_BANNED"})
    fee = (await settings_service.get_all(db))["ban_reinstatement_fee"]
    debt_added = Decimal(0)
    if Decimal(fee) > 0 and not body.fee_received:
        if u.role != Role.DRIVER:  # only drivers earn money that can carry a negative balance
            raise HTTPException(402, {"code": "REINSTATEMENT_FEE_REQUIRED", "fee": fee})
        u.driver_profile.debt_balance = Decimal(u.driver_profile.debt_balance or 0) + Decimal(fee)
        debt_added = Decimal(fee)
    u.account_status, u.status_reason = AccountStatus.APPROVED, None
    u.auto_suspended, u.auto_suspend_count, u.rating_reset_at = False, 0, utcnow()
    if u.driver_profile:
        u.driver_profile.driver_status = AccountStatus.APPROVED
    audit(db, admin.id, "BAN_REINSTATED", "user", u.id, f"fee={fee}; debt_added={debt_added}; ref={body.payment_reference}; {body.reason}")
    await notify(db, u.id, "ACCOUNT_REACTIVATED", "تمت إعادة تفعيل حسابك", "تمت إعادة تفعيل حسابك بعد سداد الرسوم.")
    await db.commit()
    return {**user_public(u), "fee_charged": fee, "debt_added": str(debt_added),
            "debt_balance": str(u.driver_profile.debt_balance) if u.driver_profile else None}


@router.post("/drivers/{user_id}/debt/settle")
async def settle_driver_debt(user_id: int, body: DebtSettleIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """The driver paid the company directly (transfer/BenefitPay): reduce the debt."""
    u = await _target(db, user_id)
    if not u.driver_profile:
        raise HTTPException(404, "Driver not found")
    cur = Decimal(u.driver_profile.debt_balance or 0)
    amt = min(Decimal(body.amount), cur)
    u.driver_profile.debt_balance = cur - amt
    audit(db, admin.id, "DRIVER_DEBT_SETTLED", "user", u.id, f"amount={amt}; ref={body.payment_reference}")
    await db.commit()
    return {"debt_balance": str(u.driver_profile.debt_balance), "settled": str(amt)}


@router.post("/users/{user_id}/reset-password")
async def reset_password(user_id: int, body: ResetPasswordIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """OTP-free recovery: admin sets a temporary password; the user must change it at next login."""
    u = await _target(db, user_id)
    u.password_hash, u.must_change_password = hash_password(body.new_password), True
    u.token_version += 1
    u.failed_logins, u.locked_until = 0, None
    audit(db, admin.id, "PASSWORD_RESET", "user", u.id)
    await db.commit()
    return {"ok": True}


@router.post("/admins", status_code=201)
async def create_admin(body: AdminCreateIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    u = User(full_name=body.full_name, phone=body.phone, password_hash=hash_password(body.password), role=body.role,
             account_status=AccountStatus.APPROVED, approved_at=utcnow(), approved_by=admin.id)
    db.add(u)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Phone already registered")
    audit(db, admin.id, "ADMIN_CREATED", "user", u.id, details={"role": body.role})
    await db.commit()
    return user_public(u)

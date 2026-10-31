import csv
import io
import json
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import AccountStatus, CURRENCIES, OrderStatus as S, ReviewStatus, Role
from ..database import get_db
from ..deps import admin_user, paging, super_admin_user
from ..models import (AdminAuditLog, Complaint, DriverPayout, DriverProfile, DriverServiceArea, DriverWorkSession, Order,
                      OrderStatusHistory, Payment, Rating, ServiceArea, Subscription, User, Vehicle, VehicleDocument, utcnow)
from ..schemas import (AdminCreateIn, AdminReasonIn, ApproveIn, AreaIn, AreaPatch, BroadcastIn, ComplaintResolveIn, DocReviewIn,
                       DriverAreasIn, PriceAdjustIn, ReasonIn, ReinstateIn, DebtSettleIn, ResetPasswordIn, SettingsPatch)
from ..security import cpr_hash, decrypt, hash_password, normalize_phone
from ..serializers import (dt, driver_block, history_out, mask_cpr, money, order_for_admin, user_public)
from ..services import settings_service
from ..services.activity import is_online
from ..services.audit import audit
from ..services.dispatch import force_offline
from ..services.notifications import notify
from ..services.orders import close_pending_offers, load_order, transition
from ..services.pricing import fits_decimals, split_order

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- dashboard ----------------
@router.get("/dashboard")
async def dashboard(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    async def count(q):
        return (await db.execute(q)).scalar_one()
    today_start = datetime.now(ZoneInfo(settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    U, O = User, Order
    return {
        "customers_total": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER)),
        "customers_pending": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER, U.account_status == AccountStatus.PENDING_APPROVAL)),
        "drivers_approved": await count(select(func.count()).select_from(U).where(U.role == Role.DRIVER, U.account_status == AccountStatus.APPROVED)),
        "drivers_pending": await count(select(func.count()).select_from(U).where(U.role == Role.DRIVER, U.account_status == AccountStatus.PENDING_APPROVAL)),
        "users_online_now": await count(select(func.count()).select_from(U).where(U.role.in_([Role.CUSTOMER, Role.DRIVER]), U.last_seen_at >= utcnow() - timedelta(minutes=int((await settings_service.get_all(db))["online_window_minutes"])))),
        "drivers_online": await count(select(func.count()).select_from(DriverProfile).where(DriverProfile.is_online.is_(True))),
        "orders_new": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.SEARCHING))),
        "trips_active": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.DRIVER_BUSY))),
        "trips_completed_today": await count(select(func.count()).select_from(O).where(O.order_status == S.COMPLETED, O.completed_at >= today_start)),
        "orders_cancelled": await count(select(func.count()).select_from(O).where(O.order_status == S.CANCELLED)),
        "gross_trip_value_today": {c: money(t, d) for c, d, t in (await db.execute(
            select(O.currency, O.currency_decimals, func.coalesce(func.sum(O.final_price), 0))
            .where(O.order_status == S.COMPLETED, O.completed_at >= today_start)
            .group_by(O.currency, O.currency_decimals))).all()},
        "company_profit_today": {c: money(t, d) for c, d, t in (await db.execute(
            select(O.currency, O.currency_decimals, func.coalesce(func.sum(O.commission), 0))
            .where(O.order_status == S.COMPLETED, O.completed_at >= today_start)
            .group_by(O.currency, O.currency_decimals))).all()},
        "open_complaints": await count(select(func.count()).select_from(Complaint).where(Complaint.status == "OPEN")),
    }


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
    from .ratings import stats_for
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


# ---------------- drivers ----------------
@router.get("/drivers")
async def list_drivers(online: bool | None = None, status: str | None = None, page=Depends(paging),
                       admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(User).join(DriverProfile, DriverProfile.user_id == User.id).where(User.role == Role.DRIVER)
    if online is not None:
        stmt = stmt.where(DriverProfile.is_online.is_(online))
    if status:
        stmt = stmt.where(User.account_status == status)
    rows = (await db.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))).scalars().unique().all()
    return [{**user_public(u), "online": u.driver_profile.is_online, "license_expiry_date": u.driver_profile.license_expiry_date.isoformat(),
             "vehicle": {"make": u.vehicle.make, "model": u.vehicle.model, "plate_number": u.vehicle.plate_number} if u.vehicle else None} for u in rows]


@router.put("/drivers/{driver_id}/service-areas")
async def set_driver_areas(driver_id: int, body: DriverAreasIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, driver_id)
    if u.role != Role.DRIVER:
        raise HTTPException(422, "Not a driver")
    ids = set(body.service_area_ids)
    found = set((await db.execute(select(ServiceArea.id).where(ServiceArea.id.in_(ids)))).scalars())
    if ids - found:
        raise HTTPException(422, f"Unknown areas: {sorted(ids - found)}")
    await db.execute(delete(DriverServiceArea).where(DriverServiceArea.driver_id == driver_id))
    for i in ids:
        db.add(DriverServiceArea(driver_id=driver_id, service_area_id=i))
    audit(db, admin.id, "DRIVER_AREAS_SET", "user", driver_id, details={"areas": sorted(ids)})
    await db.commit()
    return {"service_area_ids": sorted(ids)}


@router.post("/documents/{doc_id}/review")
async def review_document(doc_id: int, body: DocReviewIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(VehicleDocument, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    d.review_status, d.review_reason, d.reviewed_by, d.reviewed_at = body.status, body.reason, admin.id, utcnow()
    veh = await db.get(Vehicle, d.vehicle_id)
    if body.status == "REJECTED":
        await force_offline(db, veh.driver_id)
    await notify(db, veh.driver_id, "DOCUMENT_REVIEWED", "مراجعة مستند", f"{d.document_type}: {body.status}" + (f" - {body.reason}" if body.reason else ""))
    audit(db, admin.id, "DOCUMENT_REVIEW", "document", d.id, body.reason, {"status": body.status})
    await db.commit()
    return {"id": d.id, "review_status": d.review_status}


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
        raise HTTPException(409, {"code": "PAYMENT_ALREADY_STARTED", "message": "Adjust before payment, or refund first"})
    if not o.price_adjusted:
        o.original_price = o.final_price
    old = o.final_price
    if not fits_decimals(body.new_price, o.currency_decimals):
        raise HTTPException(422, {"code": "TOO_MANY_DECIMALS", "currency": o.currency, "max_decimals": o.currency_decimals})
    o.final_price, o.price_adjusted = body.new_price, True
    o.commission, o.customer_total, o.driver_earning, cut = split_order(o, o.final_price)
    o.outside_driver_cut = cut if o.is_outside_bahrain else None
    pay = (await db.execute(select(Payment).where(Payment.order_id == o.id))).scalars().first()
    if pay:
        pay.amount, pay.driver_amount, pay.company_amount = o.customer_total, o.driver_earning, o.commission
    db.add(OrderStatusHistory(order_id=o.id, old_status=o.order_status, new_status=o.order_status, changed_by=admin.id,
                              changed_by_role=admin.role, notes=f"Price adjusted {old} -> {o.final_price}: {body.reason}"))
    audit(db, admin.id, "PRICE_ADJUSTED", "order", o.id, body.reason, {"old": str(old), "new": str(o.final_price)})
    await db.commit()
    return order_for_admin(o)


# ---------------- service areas ----------------
def _area(a: ServiceArea) -> dict:
    return {"id": a.id, "name": a.name, "boundary": a.boundary, "is_active": a.is_active}


@router.get("/service-areas")
async def list_areas(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ServiceArea).order_by(ServiceArea.id))).scalars().all()
    out = []
    for a in rows:
        n = (await db.execute(select(func.count()).select_from(Order).where(Order.service_area_id == a.id, Order.order_status == S.NO_DRIVER_AVAILABLE))).scalar_one()
        out.append({**_area(a), "orders_without_driver": n})
    return out


@router.post("/service-areas", status_code=201)
async def create_area(body: AreaIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = ServiceArea(**body.model_dump())
    db.add(a)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Area name already exists")
    audit(db, admin.id, "AREA_CREATED", "area", a.id)
    await db.commit()
    return _area(a)


@router.patch("/service-areas/{area_id}")
async def patch_area(area_id: int, body: AreaPatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = await db.get(ServiceArea, area_id)
    if not a:
        raise HTTPException(404, "Area not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    audit(db, admin.id, "AREA_UPDATED", "area", a.id, details=body.model_dump(exclude_unset=True))
    await db.commit()
    return _area(a)


# ---------------- settings ----------------
@router.get("/settings")
async def get_settings(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await settings_service.get_all(db)


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """Pricing changes apply to NEW orders only (orders keep their price snapshot)."""
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    before = await settings_service.get_all(db)
    new_cur = changes.get("currency_code", before["currency_code"])
    if new_cur != before["currency_code"] and not ("unit_price" in changes and "min_price" in changes):
        raise HTTPException(422, {"code": "PRICES_REQUIRED_WITH_CURRENCY_CHANGE",
                                  "message": "Changing currency requires unit_price and min_price in the same request"})
    decimals = CURRENCIES[new_cur]
    ctype = changes.get("commission_type", before["commission_type"])
    cval = Decimal(str(changes.get("commission_value", before["commission_value"])))
    if ctype == "PERCENT" and cval > 100:
        raise HTTPException(422, {"code": "COMMISSION_PERCENT_TOO_HIGH", "max": 100})
    if ctype == "FIXED":
        if new_cur != before["currency_code"] and "commission_value" not in changes:
            raise HTTPException(422, {"code": "COMMISSION_REQUIRED_WITH_CURRENCY_CHANGE"})
        if not fits_decimals(cval, decimals):
            raise HTTPException(422, {"code": "TOO_MANY_DECIMALS", "field": "commission_value", "currency": new_cur, "max_decimals": decimals})
    for k in ("unit_price", "min_price"):
        val = Decimal(str(changes.get(k, before[k])))
        if not fits_decimals(val, decimals):
            raise HTTPException(422, {"code": "TOO_MANY_DECIMALS", "field": k, "currency": new_cur, "max_decimals": decimals})
    for k, v in changes.items():
        val = json.dumps(v) if isinstance(v, dict) else (str(v).lower() if isinstance(v, bool) else str(v))
        await settings_service.set_value(db, k, val, admin.id)
    audit(db, admin.id, "SETTINGS_CHANGED", "settings", "global", body.reason,
          {"before": {k: before[k] for k in changes}, "after": {k: str(v) for k, v in changes.items()}})
    await db.commit()
    return await settings_service.get_all(db)


# ---------------- complaints ----------------
@router.get("/complaints")
async def complaints(status: str | None = "OPEN", type: str | None = None, page=Depends(paging),
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(Complaint)
    if status:
        stmt = stmt.where(Complaint.status == status)
    if type:
        stmt = stmt.where(Complaint.type == type)
    rows = (await db.execute(stmt.order_by(Complaint.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": c.id, "user_id": c.user_id, "order_id": c.order_id, "type": c.type, "subject": c.subject,
             "message": c.message, "status": c.status, "created_at": dt(c.created_at)} for c in rows]


@router.post("/complaints/{cid}/resolve")
async def resolve_complaint(cid: int, body: ComplaintResolveIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Complaint, cid)
    if not c:
        raise HTTPException(404, "Not found")
    c.status, c.admin_response, c.resolved_by, c.resolved_at = "RESOLVED", body.response, admin.id, utcnow()
    if c.user_id:
        await notify(db, c.user_id, "COMPLAINT_RESOLVED", "رد الإدارة", body.response, c.order_id)
    audit(db, admin.id, "COMPLAINT_RESOLVED", "complaint", c.id)
    await db.commit()
    return {"id": c.id, "status": c.status}


@router.get("/audit-logs")
async def audit_logs(action: str | None = None, page=Depends(paging), admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(AdminAuditLog)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    rows = (await db.execute(stmt.order_by(AdminAuditLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": a.id, "admin_id": a.admin_id, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
             "reason": a.reason, "details": a.details, "created_at": dt(a.created_at)} for a in rows]


# ---------------- announcements ----------------
@router.post("/notifications")
async def send_announcement(body: BroadcastIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Custom message from the admin: to CUSTOMERS, to DRIVERS, to ALL (both), or to one user. Approved accounts only."""
    q = select(User.id).where(User.account_status == AccountStatus.APPROVED)
    if body.user_id is not None:
        q = q.where(User.id == body.user_id)
    else:
        roles = {"CUSTOMERS": [Role.CUSTOMER], "DRIVERS": [Role.DRIVER], "ALL": [Role.CUSTOMER, Role.DRIVER]}[body.audience]
        q = q.where(User.role.in_(roles))
    ids = (await db.execute(q)).scalars().all()
    if not ids:
        raise HTTPException(404, "No matching approved users")
    for uid in ids:
        await notify(db, uid, "ANNOUNCEMENT", body.title, body.message)
    audit(db, admin.id, "ANNOUNCEMENT_SENT", "notification", body.audience or str(body.user_id), details={"recipients": len(ids), "title": body.title})
    await db.commit()
    return {"recipients": len(ids)}


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

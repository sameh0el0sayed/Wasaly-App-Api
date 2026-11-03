"""v15: one account, two roles (customer <-> driver) + driver applications."""
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, approved_user, current_user
from ..models import DriverApplication, DriverProfile, Order, User, Vehicle, utcnow
from ..schemas import DriverRegIn
from ..security import create_access_token, encrypt
from ..serializers import dt, user_public
from ..services import settings_service
from ..services.audit import audit
from ..services.dispatch import force_offline
from ..services.files import assert_owned
from ..services.notifications import notify
from ..services.payments import notify_admins

router = APIRouter(prefix="/api", tags=["v15-roles"])
admin_router = APIRouter(prefix="/api/admin", tags=["v15-admin"])
PENDING, APPROVED, REJECTED, CORRECTION = "PENDING_REVIEW", "APPROVED", "REJECTED", "NEEDS_CORRECTION"


class SwitchIn(BaseModel):
    role: str = Field(pattern="^(CUSTOMER|DRIVER|ADMIN|SUPER_ADMIN)$")


class ReasonBody(BaseModel):
    reason: str = Field(min_length=2, max_length=500)


def app_out(a: DriverApplication | None) -> dict | None:
    return {"id": a.id, "status": a.status, "reason": a.reason, "created_at": dt(a.created_at), "payload": a.payload} if a else None


# ---------------- switch ----------------
@router.post("/profile/switch-role")
async def switch_role(body: SwitchIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    held_admin = ({user.role} | set(user.roles or [])) & Role.ADMINS  # admin roles this account really holds (server-written only)
    s = await settings_service.get_all(db)
    if s["role_switch_enabled"] != "true" and body.role not in Role.ADMINS:  # going back to the admin role is always allowed
        raise HTTPException(403, {"code": "ROLE_SWITCH_DISABLED"})
    if body.role == user.role:
        raise HTTPException(422, {"code": "SAME_ROLE"})
    if body.role in Role.ADMINS and body.role not in held_admin:
        raise HTTPException(403, {"code": "ROLE_NOT_GRANTED"})
    if body.role == Role.CUSTOMER and user.role not in Role.ADMINS and user.role != Role.DRIVER:
        raise HTTPException(403, {"code": "ROLE_NOT_GRANTED"})
    if body.role == Role.DRIVER:
        p = user.driver_profile
        if not p or p.driver_status != AccountStatus.APPROVED or not user.vehicle:
            raise HTTPException(403, {"code": "ROLE_NOT_GRANTED",
                                      "driver_application_status": user.driver_application.status if user.driver_application else None})
    if user.role == Role.DRIVER:
        busy = (await db.execute(select(Order.id).where(Order.driver_id == user.id, Order.order_status.in_(S.DRIVER_BUSY)).limit(1))).scalar()
        if busy:
            raise HTTPException(409, {"code": "SWITCH_BLOCKED_ACTIVE_TRIP", "order_id": busy})
    elif user.role == Role.CUSTOMER:
        busy = (await db.execute(select(Order.id).where(Order.customer_id == user.id, Order.order_status.in_(S.SEARCHING | S.DRIVER_BUSY)).limit(1))).scalar()
        if busy:
            raise HTTPException(409, {"code": "SWITCH_BLOCKED_ACTIVE_ORDER", "order_id": busy})
    if user.role == Role.DRIVER:
        await force_offline(db, user.id)  # no offers while acting as a customer
    if body.role == Role.CUSTOMER:
        from .v11 import ensure_code
        await ensure_code(db, user)  # customer side is created on first switch
    user.roles = sorted({*(user.roles or []), Role.CUSTOMER, user.role, body.role})
    user.role = body.role
    await db.commit()
    await db.refresh(user)
    return {"user": user_public(user), "access_token": create_access_token(user.id, user.token_version), "token_type": "bearer"}


# ---------------- application ----------------
async def _save(db: AsyncSession, user: User, body: DriverRegIn) -> DriverApplication:
    if body.license_issue_date >= body.license_expiry_date:
        raise HTTPException(422, {"code": "INVALID_DATES"})
    if body.license_expiry_date <= date.today():
        raise HTTPException(422, {"code": "LICENSE_EXPIRED"})
    if (await db.execute(select(Vehicle.id).where(Vehicle.plate_number == body.vehicle.plate_number))).first():
        raise HTTPException(409, {"code": "PLATE_EXISTS"})
    for key in filter(None, [body.license_document, *body.vehicle.photos.values()]):
        await assert_owned(db, user.id, key, claim=True)
    a = user.driver_application or DriverApplication(user_id=user.id)
    a.payload, a.status, a.reason, a.created_at = body.model_dump(mode="json"), PENDING, None, utcnow()
    a.decided_at = a.decided_by = None
    db.add(a)
    await notify_admins(db, "DRIVER_APPLICATION_SUBMITTED", "طلب انضمام سائق", f"قدّم {user.full_name} طلبًا ليصبح سائقًا.")
    await db.commit()
    await db.refresh(a)
    return a


@router.post("/profile/driver-application", status_code=202)
async def apply_driver(body: DriverRegIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    if user.role not in (Role.CUSTOMER, Role.ADMIN, Role.SUPER_ADMIN):
        raise HTTPException(403, "Switch to customer mode first")
    if (await settings_service.get_all(db))["customer_to_driver_enabled"] != "true":
        raise HTTPException(403, {"code": "CUSTOMER_TO_DRIVER_DISABLED"})
    a = user.driver_application
    if user.driver_profile or (a and a.status == APPROVED):
        raise HTTPException(409, {"code": "ALREADY_DRIVER"})
    if a and a.status == PENDING:
        raise HTTPException(409, {"code": "APPLICATION_PENDING"})
    return {"driver_application": app_out(await _save(db, user, body))}


@router.get("/profile/driver-application")
async def get_application(user: User = Depends(current_user)):
    if not user.driver_application:
        raise HTTPException(404, "Not found")
    return {"driver_application": app_out(user.driver_application)}


@router.patch("/profile/driver-application", status_code=202)
async def resubmit_application(body: DriverRegIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    a = user.driver_application
    if not a:
        raise HTTPException(404, "Not found")
    if a.status not in (CORRECTION, REJECTED):
        raise HTTPException(409, {"code": "APPLICATION_PENDING" if a.status == PENDING else "ALREADY_DRIVER"})
    return {"driver_application": app_out(await _save(db, user, body))}


# ---------------- admin ----------------
@admin_router.get("/driver-applications")
async def list_applications(status: str | None = PENDING, limit: int = 50, offset: int = 0,
                            admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = max(1, min(limit, 200)), max(0, offset)
    stmt = select(DriverApplication)
    if status:
        stmt = stmt.where(DriverApplication.status == status)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(DriverApplication.id.desc()).limit(limit).offset(offset))).scalars().all()
    items = []
    for a in rows:
        u = await db.get(User, a.user_id)
        items.append({**app_out(a), "user": {"id": u.id, "full_name": u.full_name, "phone": u.phone, "profile_photo": u.profile_photo}})
    return {"items": items, "total": total}


async def _decide(db: AsyncSession, admin: User, aid: int, status: str, reason: str | None) -> DriverApplication:
    a = await db.get(DriverApplication, aid)
    if not a:
        raise HTTPException(404, "Not found")
    if a.status != PENDING:
        raise HTTPException(409, {"code": "NOT_PENDING_REVIEW", "status": a.status})
    a.status, a.reason, a.decided_at, a.decided_by = status, reason, utcnow(), admin.id
    return a


@admin_router.post("/driver-applications/{aid}/approve")
async def approve_application(aid: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = await _decide(db, admin, aid, APPROVED, None)
    u = await db.get(User, a.user_id)
    d = a.payload
    v = dict(d["vehicle"])
    if v.get("cargo_capacity_kg") is not None:
        v["cargo_capacity_kg"] = Decimal(str(v["cargo_capacity_kg"]))
    if (await db.execute(select(Vehicle.id).where(Vehicle.plate_number == v["plate_number"]))).first():
        raise HTTPException(409, {"code": "PLATE_EXISTS"})
    db.add(DriverProfile(user_id=u.id, license_number_encrypted=encrypt(d["license_number"]),
                         license_expiry_date=date.fromisoformat(d["license_expiry_date"]),
                         license_issue_date=date.fromisoformat(d["license_issue_date"]), license_document=d.get("license_document"),
                         gender=d.get("gender"), smoking_allowed=d.get("smoking_allowed", False), driver_status=AccountStatus.APPROVED))
    db.add(Vehicle(driver_id=u.id, vehicle_status=AccountStatus.APPROVED, **v))
    u.roles = sorted({*(u.roles or []), Role.CUSTOMER, Role.DRIVER})
    audit(db, admin.id, "DRIVER_APPLICATION_APPROVED", "user", u.id, None, details={"id": a.id})
    await notify(db, u.id, "DRIVER_APPLICATION_APPROVED", "تم اعتماد طلبك", "تم اعتماد حسابك كسائق. يمكنك التبديل إلى وضع السائق من الإعدادات.")
    await db.commit()
    return app_out(a)


@admin_router.post("/driver-applications/{aid}/reject")
async def reject_application(aid: int, body: ReasonBody, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = await _decide(db, admin, aid, REJECTED, body.reason)
    audit(db, admin.id, "DRIVER_APPLICATION_REJECTED", "user", a.user_id, body.reason, details={"id": a.id})
    await notify(db, a.user_id, "DRIVER_APPLICATION_REJECTED", "تم رفض طلب الانضمام كسائق", f"لم يتم اعتماد طلبك. السبب: {body.reason}",
                 data={"reason": body.reason})
    await db.commit()
    return app_out(a)


@admin_router.post("/driver-applications/{aid}/request-correction")
async def correction_application(aid: int, body: ReasonBody, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = await _decide(db, admin, aid, CORRECTION, body.reason)
    audit(db, admin.id, "DRIVER_APPLICATION_CORRECTION", "user", a.user_id, body.reason, details={"id": a.id})
    await notify(db, a.user_id, "DRIVER_APPLICATION_CORRECTION", "مطلوب تعديل على طلب السائق", f"يرجى تعديل بيانات الطلب وإعادة إرساله. السبب: {body.reason}",
                 data={"reason": body.reason})
    await db.commit()
    return app_out(a)

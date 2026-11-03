"""Admin edits of a user's profile, driver licence and vehicle. Every call is audited and the user is notified."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ...database import get_db
from ...deps import admin_user
from ...models import UploadedFile, User, Vehicle
from ...schemas import AdminLicenseEditIn, AdminUserEditIn, AdminVehicleEditIn
from ...security import cpr_hash, encrypt
from ...services.audit import audit
from ...services.notifications import notify
from .users import _target, get_user

router = APIRouter(prefix="/api/admin", tags=["admin"])


async def _check_keys(db: AsyncSession, keys) -> None:
    for k in filter(None, keys):
        if not await db.get(UploadedFile, k):
            raise HTTPException(422, {"code": "INVALID_FILE_KEY"})


async def _finish(db, admin, u: User, changed: list[str], reason: str):
    audit(db, admin.id, "USER_EDITED_BY_ADMIN", "user", u.id, reason, details={"fields": changed})
    await notify(db, u.id, "PROFILE_UPDATED_BY_ADMIN", "تم تحديث بياناتك",
                 "قامت الإدارة بتحديث بياناتك.")
    await db.commit()
    return await get_user(u.id, False, admin, db)


@router.patch("/users/{user_id}")
async def edit_user(user_id: int, body: AdminUserEditIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, user_id)
    data = body.model_dump(exclude_unset=True, exclude={"reason"})
    await _check_keys(db, [data.get("profile_photo"), data.get("cpr_photo")])
    if data.get("phone") and data["phone"] != u.phone:
        if (await db.execute(select(User.id).where(User.phone == data["phone"], User.id != u.id))).first():
            raise HTTPException(409, {"code": "PHONE_EXISTS"})
    if data.get("cpr"):
        h = cpr_hash(data["cpr"])
        if (await db.execute(select(User.id).where(User.cpr_hash == h, User.id != u.id))).first():
            raise HTTPException(409, {"code": "CPR_EXISTS"})
        u.cpr_encrypted, u.cpr_hash, u.cpr_last3 = encrypt(data["cpr"]), h, data["cpr"][-3:]
    for k in ("full_name", "phone", "profile_photo", "cpr_photo", "cpr_issue_date", "cpr_expiry_date"):
        if data.get(k) is not None:
            setattr(u, k, data[k])
    return await _finish(db, admin, u, sorted(data), body.reason)


async def _driver(db, user_id) -> User:
    u = await _target(db, user_id)
    if not u.driver_profile:
        raise HTTPException(404, "Driver not found")
    return u


@router.patch("/drivers/{user_id}/license")
async def edit_license(user_id: int, body: AdminLicenseEditIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _driver(db, user_id)
    data = body.model_dump(exclude_unset=True, exclude={"reason"})
    await _check_keys(db, [data.get("license_document")])
    p = u.driver_profile
    if data.get("license_number"):
        p.license_number_encrypted = encrypt(data["license_number"])
    if data.get("license_expiry_date"):
        p.license_expiry_date = data["license_expiry_date"]
    if data.get("license_issue_date"):
        p.license_issue_date = data["license_issue_date"]
    if data.get("license_document"):
        p.license_document = data["license_document"]
    return await _finish(db, admin, u, sorted(data), body.reason)


@router.patch("/drivers/{user_id}/vehicle")
async def edit_vehicle(user_id: int, body: AdminVehicleEditIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _driver(db, user_id)
    v = u.vehicle
    if not v:
        raise HTTPException(404, "Vehicle not found")
    data = body.model_dump(exclude_unset=True, exclude={"reason"})
    await _check_keys(db, (data.get("photos") or {}).values())
    if data.get("plate_number") and data["plate_number"] != v.plate_number:
        if (await db.execute(select(Vehicle.id).where(Vehicle.plate_number == data["plate_number"], Vehicle.id != v.id))).first():
            raise HTTPException(409, {"code": "PLATE_EXISTS"})
    for k, val in data.items():
        if val is not None:
            setattr(v, k, val)
    return await _finish(db, admin, u, sorted(data), body.reason)

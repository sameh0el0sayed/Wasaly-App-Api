from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import AccountStatus, Role
from ..database import get_db
from ..deps import current_user
from ..models import Complaint, DriverProfile, LoginEvent, User, UserDevice, Vehicle, VehicleDocument, utcnow
from ..schemas import ChangePasswordIn, ForgotPasswordIn, LoginIn, RegisterIn
from ..security import (cpr_hash, create_access_token, encrypt, hash_password, verify_password)
from ..serializers import user_public
from ..services import settings_service
from ..services.files import assert_owned

router = APIRouter(prefix="/api", tags=["auth"])


@router.post("/auth/register", status_code=201)
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db)):
    """Creates the account as PENDING_APPROVAL. No OTP anywhere. The returned token is a normal token;
    approval-only endpoints answer 403 ACCOUNT_NOT_APPROVED until an admin approves."""
    dup = await db.execute(select(User.id).where(or_(User.phone == body.phone, User.cpr_hash == cpr_hash(body.cpr))))
    if dup.first():
        banned = (await db.execute(select(User.id).where(or_(User.phone == body.phone, User.cpr_hash == cpr_hash(body.cpr)),
                                                         User.account_status == "BANNED"))).first()
        if banned:
            raise HTTPException(403, {"code": "ACCOUNT_BANNED", "message": "This person is permanently banned"})
        raise HTTPException(409, "Phone or CPR is already registered")
    if body.role == "CUSTOMER" and (await settings_service.get_all(db))["require_cpr_photo"] == "true" \
            and not body.cpr_photo:
        raise HTTPException(422, "CPR photo is required")
    user = User(full_name=body.full_name, phone=body.phone, cpr_encrypted=encrypt(body.cpr),
                cpr_hash=cpr_hash(body.cpr), cpr_last3=body.cpr[-3:], password_hash=hash_password(body.password),
                role=body.role, account_status=AccountStatus.PENDING_APPROVAL)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Phone or CPR is already registered")
    keys = [body.profile_photo, body.cpr_photo]
    if body.role == "DRIVER":
        keys += [body.driver.license_document, *body.driver.vehicle.photos.values()]
    for key in filter(None, keys):
        await assert_owned(db, user.id, key, claim=True)
    user.profile_photo, user.cpr_photo = body.profile_photo, body.cpr_photo
    if body.role == "DRIVER":
        d, v = body.driver, body.driver.vehicle
        if (await db.execute(select(Vehicle.id).where(Vehicle.plate_number == v.plate_number))).first():
            raise HTTPException(409, "Vehicle plate number is already registered")
        db.add(DriverProfile(user_id=user.id, license_number_encrypted=encrypt(d.license_number),
                             license_expiry_date=d.license_expiry_date, license_document=d.license_document))
        db.add(Vehicle(driver_id=user.id, **v.model_dump()))
    await db.commit()
    return {"user": user_public(user), "access_token": create_access_token(user.id, user.token_version),
            "token_type": "bearer", "message": "حسابك قيد المراجعة من الإدارة"}


@router.post("/auth/login")
async def login(body: LoginIn, request: Request, db: AsyncSession = Depends(get_db)):
    user = (await db.execute(select(User).where(User.phone == body.phone))).scalar_one_or_none()
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None))
    ua = (request.headers.get("user-agent") or "")[:300]
    if user and user.locked_until and user.locked_until > utcnow():
        raise HTTPException(429, "Too many failed attempts. Try again later.")
    if not user or not verify_password(user.password_hash, body.password):
        if user:
            db.add(LoginEvent(user_id=user.id, success=False, ip=ip, user_agent=ua, device_id=body.device_id))
            user.failed_logins += 1
            s = await settings_service.get_all(db)
            if user.failed_logins >= int(s["login_max_failed_attempts"]):
                user.locked_until = utcnow() + timedelta(minutes=int(s["login_lockout_minutes"]))
                user.failed_logins = 0
            await db.commit()
        raise HTTPException(401, "Invalid phone or password")
    user.failed_logins, user.locked_until = 0, None
    known = bool(body.device_id) and (await db.execute(select(UserDevice.id).where(
        UserDevice.user_id == user.id, UserDevice.device_id == body.device_id))).first() is not None
    db.add(LoginEvent(user_id=user.id, success=True, ip=ip, user_agent=ua, device_id=body.device_id,
                      new_device=bool(body.device_id) and not known))
    now = utcnow()
    user.last_login_at, user.last_login_ip, user.login_count, user.last_seen_at = now, ip, (user.login_count or 0) + 1, now
    await db.commit()
    return {"user": user_public(user), "access_token": create_access_token(user.id, user.token_version),
            "token_type": "bearer"}


@router.post("/auth/logout")
async def logout(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.token_version += 1  # invalidates every issued token for this user
    await db.commit()
    return {"ok": True}


@router.post("/auth/change-password")
async def change_password(body: ChangePasswordIn, user: User = Depends(current_user),
                          db: AsyncSession = Depends(get_db)):
    if not verify_password(user.password_hash, body.old_password):
        raise HTTPException(400, "Old password is incorrect")
    user.password_hash, user.must_change_password = hash_password(body.new_password), False
    user.token_version += 1
    await db.commit()
    return {"access_token": create_access_token(user.id, user.token_version), "token_type": "bearer"}


@router.post("/auth/forgot-password", status_code=202)
async def forgot_password(body: ForgotPasswordIn, db: AsyncSession = Depends(get_db)):
    """No OTP: creates a reset request that an admin handles (POST /api/admin/users/{id}/reset-password)."""
    user = (await db.execute(select(User).where(User.phone == body.phone))).scalar_one_or_none()
    if user:
        db.add(Complaint(user_id=user.id, type="PASSWORD_RESET", subject="Password reset request",
                         message=f"Phone {body.phone}, name given: {body.full_name or '-'}"))
        await db.commit()
    return {"message": "إذا كان الحساب موجودًا فسيتواصل معك المدير لإعادة تعيين كلمة المرور"}


@router.get("/account/status")
async def account_status(user: User = Depends(current_user)):
    return {"account_status": user.account_status, "reason": user.status_reason, "role": user.role,
            "must_change_password": user.must_change_password}

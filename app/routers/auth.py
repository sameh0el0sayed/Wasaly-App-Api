import hashlib
import hmac
import logging
import re
import secrets
import uuid
from datetime import date, timedelta
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import AccountStatus
from ..database import get_db
from ..deps import current_user, request_lang
from ..i18n import localize
from ..models import (
    Complaint,
    DriverProfile,
    LoginEvent,
    User,
    UserDevice,
    Vehicle,
    WhatsAppVerification,
    WhatsAppWebhookEvent,
    utcnow,
)
from ..schemas import ChangePasswordIn, ForgotPasswordIn, LoginIn, RegisterIn, WhatsAppVerificationRequestIn
from ..security import cpr_hash, create_access_token, encrypt, hash_password, normalize_phone, verify_password
from ..serializers import user_public
from ..services import sessions, settings_service
from ..services.audit import audit
from ..services.files import assert_owned
from ..services.ratelimit import limit

router = APIRouter(prefix="/api", tags=["auth"])
whatsapp_webhook_router = APIRouter(prefix="/webhooks", tags=["WhatsApp"])
WHATSAPP_OTP_TTL_SECONDS = 180
WHATSAPP_MAX_ATTEMPTS = 5
WHATSAPP_MAX_REQUESTS_PER_15_MINUTES = 3
_OTP_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹", "01234567890123456789")


def _otp_digest(verification_id: str, otp: str) -> str:
    secret = settings.secret_key.encode("utf-8")
    message = f"whatsapp-reverse-otp:{verification_id}:{otp}".encode()
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def _whatsapp_configured() -> bool:
    business_number = re.sub(r"\D", "", settings.whatsapp_business_number)
    return bool(8 <= len(business_number) <= 15 and settings.whatsapp_webhook_verify_token
                and settings.whatsapp_app_secret)


@router.post("/auth/whatsapp/request", status_code=202, dependencies=[Depends(limit("wa_otp", 10, 3600))])
async def request_whatsapp_verification(body: WhatsAppVerificationRequestIn,
                                        db: AsyncSession = Depends(get_db)):
    """Creates a short-lived OTP; the user sends it manually to the company WhatsApp number."""
    if not _whatsapp_configured():
        raise HTTPException(503, {"code": "WHATSAPP_VERIFICATION_NOT_CONFIGURED"})
    phone = body.phone
    user = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none()
    if user and user.phone_verified:
        raise HTTPException(409, {"code": "PHONE_ALREADY_VERIFIED"})
    if user and user.account_status in {"BANNED", "DELETED"}:
        raise HTTPException(403, {"code": "ACCOUNT_NOT_ELIGIBLE_FOR_VERIFICATION"})

    now = utcnow()
    recent_count = (await db.execute(select(func.count()).select_from(WhatsAppVerification).where(
        WhatsAppVerification.phone == phone,
        WhatsAppVerification.created_at >= now - timedelta(minutes=15),
    ))).scalar_one()
    if recent_count >= WHATSAPP_MAX_REQUESTS_PER_15_MINUTES:
        raise HTTPException(429, {"code": "WHATSAPP_REQUEST_RATE_LIMITED", "retry_after_seconds": 900},
                            headers={"Retry-After": "900"})

    old_sessions = (await db.execute(select(WhatsAppVerification).where(
        WhatsAppVerification.phone == phone, WhatsAppVerification.status == "PENDING",
    ).with_for_update())).scalars().all()
    for old in old_sessions:
        old.status = "EXPIRED"

    verification_id = str(uuid.uuid4())
    otp = f"{secrets.randbelow(1_000_000):06d}"
    session = WhatsAppVerification(
        verification_id=verification_id,
        user_id=user.id if user else None,
        phone=phone,
        otp_hash=_otp_digest(verification_id, otp),
        expires_at=now + timedelta(seconds=WHATSAPP_OTP_TTL_SECONDS),
        status="PENDING",
    )
    db.add(session)
    await db.commit()

    business_number = re.sub(r"\D", "", settings.whatsapp_business_number)
    message = f"VERIFY {otp}"
    return {
        "verification_id": verification_id,
        "status": "pending",
        "whatsapp_message": message,
        "whatsapp_url": f"https://wa.me/{business_number}?text={quote(message)}",
        "expires_in": WHATSAPP_OTP_TTL_SECONDS,
    }


@router.get("/auth/whatsapp/status")
async def whatsapp_verification_status(
    verification_id: str = Query(min_length=36, max_length=36),
    db: AsyncSession = Depends(get_db),
):
    session = await db.get(WhatsAppVerification, verification_id, with_for_update=True)
    if not session:
        raise HTTPException(404, {"code": "WHATSAPP_VERIFICATION_NOT_FOUND"})
    now = utcnow()
    if session.status == "PENDING" and session.expires_at <= now:
        session.status = "EXPIRED"
        await db.commit()
    if session.status != "VERIFIED":
        return {"verified": False, "status": session.status.lower(),
                "attempts_remaining": max(0, WHATSAPP_MAX_ATTEMPTS - session.attempts),
                "expires_at": session.expires_at.isoformat()}

    if session.user_id is None:
        return {"verified": True, "status": "verified", "phone": session.phone,
                "requires_registration": True}
    user = await db.get(User, session.user_id)
    if not user or not user.phone_verified:
        raise HTTPException(409, {"code": "PHONE_VERIFICATION_STATE_INVALID"})
    response = {"verified": True, "status": "verified", "user": user_public(user)}
    if session.token_issued_at is None:
        response.update({"access_token": create_access_token(user.id, user.token_version), "token_type": "bearer"})
        session.token_issued_at = now
        await db.commit()
    return response


@whatsapp_webhook_router.get("/whatsapp")
async def whatsapp_webhook_challenge(
    mode: str | None = Query(default=None, alias="hub.mode"),
    verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    challenge: str | None = Query(default=None, alias="hub.challenge"),
):
    if not settings.whatsapp_webhook_verify_token:
        raise HTTPException(503, {"code": "WHATSAPP_WEBHOOK_NOT_CONFIGURED"})
    if mode == "subscribe" and hmac.compare_digest(verify_token or "", settings.whatsapp_webhook_verify_token):
        return Response(content=challenge or "", media_type="text/plain")
    raise HTTPException(403, {"code": "WHATSAPP_WEBHOOK_VERIFICATION_FAILED"})


async def _process_whatsapp_message(db: AsyncSession, message: dict) -> None:
    message_id = message.get("id")
    sender_raw = str(message.get("from") or "")
    text = str((message.get("text") or {}).get("body") or "").translate(_OTP_DIGITS).strip()
    matched = re.fullmatch(r"VERIFY\s+(\d{6})", text, flags=re.IGNORECASE)
    if not message_id or not sender_raw or not matched:
        return
    try:
        sender = normalize_phone(sender_raw if sender_raw.startswith("+") else f"+{sender_raw}")
    except ValueError:
        return

    db.add(WhatsAppWebhookEvent(provider_message_id=str(message_id)))
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return

    now = utcnow()
    session = (await db.execute(select(WhatsAppVerification).where(
        WhatsAppVerification.phone == sender,
        WhatsAppVerification.status == "PENDING",
        WhatsAppVerification.expires_at > now,
    ).order_by(WhatsAppVerification.created_at.desc()).limit(1).with_for_update())).scalar_one_or_none()
    if not session:
        await db.commit()  # retain event idempotency even when there is no active request
        return

    session.attempts += 1
    candidate_hash = _otp_digest(session.verification_id, matched.group(1))
    if hmac.compare_digest(candidate_hash, session.otp_hash):
        session.status, session.verified_at = "VERIFIED", now
        if session.user_id is not None:
            user = await db.get(User, session.user_id)
            if (user and user.phone == sender
                    and user.account_status not in {"BANNED", "DELETED"}):
                user.phone_verified = True
                audit(db, None, "WHATSAPP_PHONE_VERIFIED", "user", user.id,
                      details={"verification_id": session.verification_id})
            else:
                session.status = "LOCKED"
        # Invalidate other pending sessions for the same phone.
        others = (await db.execute(select(WhatsAppVerification).where(
            WhatsAppVerification.phone == sender,
            WhatsAppVerification.status == "PENDING",
            WhatsAppVerification.verification_id != session.verification_id,
        ))).scalars().all()
        for other in others:
            other.status = "EXPIRED"
    elif session.attempts >= WHATSAPP_MAX_ATTEMPTS:
        session.status = "LOCKED"
    await db.commit()


@whatsapp_webhook_router.post("/whatsapp")
async def whatsapp_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    if not settings.whatsapp_app_secret:
        raise HTTPException(503, {"code": "WHATSAPP_WEBHOOK_NOT_CONFIGURED"})
    raw_body = await request.body()
    supplied_signature = request.headers.get("X-Hub-Signature-256", "")
    expected_signature = "sha256=" + hmac.new(
        settings.whatsapp_app_secret.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(supplied_signature, expected_signature):
        raise HTTPException(401, {"code": "WHATSAPP_WEBHOOK_SIGNATURE_INVALID"})
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, {"code": "INVALID_WEBHOOK_JSON"})
    try:
        for entry in payload.get("entry", []):
            for change in entry.get("changes", []):
                for message in (change.get("value") or {}).get("messages", []):
                    await _process_whatsapp_message(db, message)
    except Exception:
        await db.rollback()
        logging.getLogger("whatsapp").exception("WhatsApp webhook processing failed")
        raise HTTPException(500, {"code": "WHATSAPP_WEBHOOK_PROCESSING_FAILED"})
    return {"ok": True}


@router.post("/auth/register", status_code=201, dependencies=[Depends(limit("register", 10, 3600))])
async def register(body: RegisterIn, db: AsyncSession = Depends(get_db), lang: str = Depends(request_lang)):
    """Creates a pending account. WhatsApp reverse-OTP is enforced only when whatsapp_verification_required=true (default false)."""
    verification = None
    if (await settings_service.get_all(db))["whatsapp_verification_required"] == "true":  # off by default
        if not _whatsapp_configured():
            raise HTTPException(503, {"code": "WHATSAPP_VERIFICATION_NOT_CONFIGURED"})
        verification = (await db.get(WhatsAppVerification, body.whatsapp_verification_id, with_for_update=True)
                        if body.whatsapp_verification_id else None)
        if (not verification or verification.phone != body.phone or verification.status != "VERIFIED"
                or verification.user_id is not None or not verification.verified_at):
            raise HTTPException(409, {"code": "PHONE_VERIFICATION_REQUIRED"})
        if utcnow() - verification.verified_at > timedelta(hours=24):
            verification.status = "EXPIRED"
            await db.commit()
            raise HTTPException(409, {"code": "PHONE_VERIFICATION_EXPIRED"})
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
                role=body.role, account_status=AccountStatus.PENDING_APPROVAL,
                phone_verified=verification is not None, language=body.language or lang)
    db.add(user)
    try:
        await db.flush()
    except IntegrityError as e:
        await db.rollback()
        msg = str(getattr(e, "orig", e)).lower()
        if "cpr" in msg:
            raise HTTPException(409, {"code": "CPR_EXISTS", "message": "Phone or CPR is already registered"})
        if "phone" in msg:
            raise HTTPException(409, {"code": "PHONE_EXISTS", "message": "Phone or CPR is already registered"})
        logging.getLogger("auth").exception("register: unexpected integrity error")
        raise HTTPException(500, "Could not create the account")
    keys = [body.profile_photo, body.cpr_photo]
    if body.role == "DRIVER":
        keys += [body.driver.license_document, *body.driver.vehicle.photos.values()]
    for key in filter(None, keys):
        await assert_owned(db, user.id, key, claim=True)
    user.profile_photo, user.cpr_photo = body.profile_photo, body.cpr_photo
    if body.cpr_issue_date >= body.cpr_expiry_date or body.cpr_expiry_date <= date.today():
        raise HTTPException(422, {"code": "INVALID_CPR_DATES"})
    if body.role == "DRIVER" and body.driver and body.driver.license_issue_date >= body.driver.license_expiry_date:
        raise HTTPException(422, {"code": "INVALID_DATES"})
    user.cpr_issue_date, user.cpr_expiry_date = body.cpr_issue_date, body.cpr_expiry_date
    if body.role == "CUSTOMER":
        from .v11 import apply_referral, ensure_code
        await ensure_code(db, user)
        await apply_referral(db, user, body.referral_code, body.device_id)
    if body.role == "DRIVER":
        d, v = body.driver, body.driver.vehicle
        if (await db.execute(select(Vehicle.id).where(Vehicle.plate_number == v.plate_number))).first():
            raise HTTPException(409, "Vehicle plate number is already registered")
        db.add(DriverProfile(user_id=user.id, license_number_encrypted=encrypt(d.license_number),
                             license_expiry_date=d.license_expiry_date, license_issue_date=d.license_issue_date, license_document=d.license_document,
                             gender=d.gender, smoking_allowed=d.smoking_allowed))
        db.add(Vehicle(driver_id=user.id, **v.model_dump()))
    if verification:
        verification.user_id = user.id
        verification.status = "CONSUMED"
        audit(db, None, "WHATSAPP_PHONE_VERIFIED", "user", user.id,
              details={"verification_id": verification.verification_id})
    await db.commit()
    from ..services import command as _cmd
    _cmd.emit("info", "tenant.signup", f"تسجيل جديد: {user.full_name} ({user.role})", None, {"user_id": user.id, "role": user.role})
    from ..services.ai_review import wake
    wake()  # new member: the AI review worker wakes up if it was sleeping
    await db.refresh(user, ["driver_profile", "vehicle", "driver_application"])  # new instance: load before user_public()
    return {"user": user_public(user), **await _tokens(db, user, getattr(body, "device_id", None)),
            "phone_verified": verification is not None,
            "message": localize("تم التحقق من رقم الهاتف. حسابك الآن قيد المراجعة من الإدارة", user.language)}


@router.post("/auth/login", dependencies=[Depends(limit("login", 20, 300))])
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
                from ..services import command as _cmd
                _cmd.emit("warning", "security.login_lockout", f"قفل حساب بعد محاولات فاشلة: {user.full_name}", None, {"user_id": user.id, "ip": ip})
            await db.commit()
        raise HTTPException(401, "Invalid phone or password")
    if user.account_status == "DELETED":  # checked after the password so the state is not revealed to strangers
        raise HTTPException(403, {"code": "ACCOUNT_DELETED", "message": "هذا الحساب محذوف"})
    if user.account_status == "BANNED":
        raise HTTPException(403, {"code": "ACCOUNT_BANNED", "message": "تم حظر هذا الحساب نهائيًا", "reason": user.status_reason})
    if not user.phone_verified:
        raise HTTPException(403, {"code": "PHONE_NOT_VERIFIED", "message": "Verify your phone through WhatsApp first"})
    user.failed_logins, user.locked_until = 0, None
    known = bool(body.device_id) and (await db.execute(select(UserDevice.id).where(
        UserDevice.user_id == user.id, UserDevice.device_id == body.device_id))).first() is not None
    db.add(LoginEvent(user_id=user.id, success=True, ip=ip, user_agent=ua, device_id=body.device_id,
                      new_device=bool(body.device_id) and not known))
    now = utcnow()
    user.last_login_at, user.last_login_ip, user.login_count, user.last_seen_at = now, ip, (user.login_count or 0) + 1, now
    tokens = await _tokens(db, user, body.device_id)
    await db.commit()
    return {"user": user_public(user), **tokens}


_REFRESH_HITS: dict[int, list[float]] = {}


@router.post("/auth/refresh")
async def refresh_token(user: User = Depends(current_user)):
    """A fresh non-expiring token for the same token_version (401 comes from current_user if it changed). Never for banned accounts."""
    if user.account_status in ("BANNED", "DELETED"):
        raise HTTPException(401, {"code": "UNAUTHORIZED", "message": "Session is no longer valid"})
    import time
    now = time.time()
    hits = [x for x in _REFRESH_HITS.get(user.id, []) if now - x < 3600]
    if len(hits) >= 30:
        raise HTTPException(429, {"code": "RATE_LIMITED"})
    _REFRESH_HITS[user.id] = [*hits, now]
    return {"access_token": create_access_token(user.id, user.token_version), "token_type": "bearer"}


async def _tokens(db: AsyncSession, user: User, device_id: str | None = None) -> dict:
    tokens = await sessions.issue(db, user, device_id)
    await db.commit()
    return tokens


class RefreshIn(BaseModel):
    refresh_token: str = Field(min_length=20, max_length=200)


@router.post("/auth/token/refresh", dependencies=[Depends(limit("rtoken", 120, 3600))])
async def token_refresh(body: RefreshIn, db: AsyncSession = Depends(get_db)):
    """Rotate: returns a new access_token + refresh_token. The old refresh token stops working; reusing it revokes the session."""
    return await sessions.rotate(db, body.refresh_token)


@router.post("/auth/logout")
async def logout(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    user.token_version += 1  # invalidates every issued token for this user
    await sessions.revoke_all(db, user.id)
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


@router.post("/auth/forgot-password", status_code=202, dependencies=[Depends(limit("forgot", 5, 3600))])
async def forgot_password(body: ForgotPasswordIn, db: AsyncSession = Depends(get_db), lang: str = Depends(request_lang)):
    """No OTP: creates a reset request that an admin handles (POST /api/admin/users/{id}/reset-password)."""
    user = (await db.execute(select(User).where(User.phone == body.phone))).scalar_one_or_none()
    if user:
        db.add(Complaint(user_id=user.id, type="PASSWORD_RESET", subject="Password reset request",
                         message=f"Phone {body.phone}, name given: {body.full_name or '-'}"))
        await db.commit()
    return {"message": localize("إذا كان الحساب موجودًا فسيتواصل معك المدير لإعادة تعيين كلمة المرور", lang)}


@router.get("/account/status")
async def account_status(user: User = Depends(current_user)):
    return {"account_status": user.account_status, "phone_verified": user.phone_verified,
            "reason": user.status_reason, "role": user.role,
            "must_change_password": user.must_change_password}

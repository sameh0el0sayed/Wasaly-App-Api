"""v16: passkeys (WebAuthn). The `webauthn` package is imported lazily so the API still starts if it is missing."""
import json
import secrets
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..deps import current_user
from ..models import LoginEvent, Passkey, User, UserDevice, utcnow
from ..security import create_access_token
from ..serializers import dt, user_public
from ..services import cache, settings_service
from ..services.notifications import notify

router = APIRouter(prefix="/api", tags=["v16-passkeys"])
CHALLENGE_TTL = 300
_mem: dict[str, tuple[float, dict]] = {}
_hits: dict[str, list[float]] = {}


def _wa():
    try:
        import webauthn
        from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
        from webauthn.helpers import structs as st
        return webauthn, base64url_to_bytes, bytes_to_base64url, st
    except Exception:  # noqa
        raise HTTPException(503, {"code": "PASSKEYS_UNAVAILABLE"})


def _origins() -> list[str]:
    return [o.strip() for o in settings.passkey_origins.split(",") if o.strip()]


async def _enabled(db: AsyncSession) -> None:
    if (await settings_service.get_all(db))["passkeys_enabled"] != "true":
        raise HTTPException(403, {"code": "PASSKEYS_DISABLED"})


async def _put(data: dict) -> str:
    cid = secrets.token_urlsafe(24)
    await cache.set_json(f"wasaly:passkey:{cid}", data, CHALLENGE_TTL)
    now = time.time()
    for k in [k for k, (exp, _) in _mem.items() if exp < now]:
        _mem.pop(k, None)
    _mem[cid] = (now + CHALLENGE_TTL, data)
    return cid


async def _take(cid: str, purpose: str) -> dict:
    """Single use: the challenge is removed whatever the outcome."""
    data = await cache.get_json(f"wasaly:passkey:{cid}")
    mem = _mem.pop(cid, None)
    await cache.delete(f"wasaly:passkey:{cid}")
    if not data and mem and mem[0] >= time.time():
        data = mem[1]
    if not data or data.get("purpose") != purpose:
        raise HTTPException(400, {"code": "CHALLENGE_EXPIRED"})
    return data


def _limit(request: Request, n: int = 20) -> None:
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "?")
    now = time.time()
    hits = [t for t in _hits.get(ip, []) if now - t < 60]
    if len(hits) >= n:
        raise HTTPException(429, "Too many attempts. Try again later.")
    _hits[ip] = [*hits, now]


class RegisterVerifyIn(BaseModel):
    challenge_id: str
    credential: dict
    device_name: str | None = None


class LoginVerifyIn(BaseModel):
    challenge_id: str
    credential: dict
    device_id: str | None = None


# ---------------- registration (logged in) ----------------
@router.post("/auth/passkey/register/options")
async def register_options(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _enabled(db)
    wa, b2b, b64, st = _wa()
    existing = (await db.execute(select(Passkey).where(Passkey.user_id == user.id))).scalars().all()
    opts = wa.generate_registration_options(
        rp_id=settings.passkey_rp_id, rp_name="Wasaly", user_id=str(user.id).encode(), user_name=user.phone or str(user.id),
        user_display_name=user.full_name, timeout=60000,
        authenticator_selection=st.AuthenticatorSelectionCriteria(resident_key=st.ResidentKeyRequirement.REQUIRED,
                                                                  user_verification=st.UserVerificationRequirement.REQUIRED),
        exclude_credentials=[st.PublicKeyCredentialDescriptor(id=b2b(p.credential_id)) for p in existing])
    cid = await _put({"purpose": "register", "user_id": user.id, "challenge": b64(opts.challenge)})
    return {"challenge_id": cid, "options": json.loads(wa.options_to_json(opts))}


@router.post("/auth/passkey/register/verify", status_code=201)
async def register_verify(body: RegisterVerifyIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await _enabled(db)
    wa, b2b, b64, _ = _wa()
    ch = await _take(body.challenge_id, "register")
    if ch["user_id"] != user.id:
        raise HTTPException(400, {"code": "CHALLENGE_EXPIRED"})
    try:
        v = wa.verify_registration_response(credential=body.credential, expected_challenge=b2b(ch["challenge"]),
                                            expected_rp_id=settings.passkey_rp_id, expected_origin=_origins(), require_user_verification=True)
    except Exception:  # noqa
        raise HTTPException(400, {"code": "PASSKEY_VERIFICATION_FAILED"})
    cred_id = b64(v.credential_id)
    if (await db.execute(select(Passkey.id).where(Passkey.credential_id == cred_id))).first():
        raise HTTPException(409, {"code": "PASSKEY_EXISTS"})
    transports = (body.credential.get("response") or {}).get("transports")
    p = Passkey(user_id=user.id, credential_id=cred_id, public_key=v.credential_public_key, sign_count=v.sign_count,
                transports=transports if isinstance(transports, list) else None, device_name=(body.device_name or "")[:100] or None)
    db.add(p)
    await notify(db, user.id, "PASSKEY_ADDED", "مفتاح مرور جديد", "أُضيف مفتاح مرور جديد إلى حسابك")
    await db.commit()
    await db.refresh(p)
    return {"passkey": {"id": p.id, "device_name": p.device_name, "created_at": dt(p.created_at)}}


# ---------------- login (public) ----------------
@router.post("/auth/passkey/login/options")
async def login_options(request: Request, db: AsyncSession = Depends(get_db)):
    await _enabled(db)
    _limit(request)
    wa, _, b64, st = _wa()
    opts = wa.generate_authentication_options(rp_id=settings.passkey_rp_id, timeout=60000,
                                              user_verification=st.UserVerificationRequirement.REQUIRED)
    cid = await _put({"purpose": "login", "challenge": b64(opts.challenge)})
    return {"challenge_id": cid, "options": json.loads(wa.options_to_json(opts))}


@router.post("/auth/passkey/login/verify")
async def login_verify(body: LoginVerifyIn, request: Request, db: AsyncSession = Depends(get_db)):
    await _enabled(db)
    _limit(request)
    wa, b2b, _, _ = _wa()
    ch = await _take(body.challenge_id, "login")
    cred_id = body.credential.get("rawId") or body.credential.get("id")
    p = (await db.execute(select(Passkey).where(Passkey.credential_id == cred_id))).scalar_one_or_none() if cred_id else None
    if not p:
        raise HTTPException(404, {"code": "PASSKEY_NOT_FOUND"})
    user = await db.get(User, p.user_id)
    if not user:
        raise HTTPException(404, {"code": "PASSKEY_NOT_FOUND"})
    if user.locked_until and user.locked_until > utcnow():
        raise HTTPException(429, "Too many failed attempts. Try again later.")
    try:
        v = wa.verify_authentication_response(credential=body.credential, expected_challenge=b2b(ch["challenge"]),
                                              expected_rp_id=settings.passkey_rp_id, expected_origin=_origins(),
                                              credential_public_key=p.public_key, credential_current_sign_count=p.sign_count or 0,
                                              require_user_verification=True)
    except Exception:  # noqa
        raise HTTPException(400, {"code": "PASSKEY_VERIFICATION_FAILED"})
    if user.account_status == "DELETED":
        raise HTTPException(403, {"code": "ACCOUNT_DELETED", "message": "هذا الحساب محذوف"})
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)
    known = bool(body.device_id) and (await db.execute(select(UserDevice.id).where(
        UserDevice.user_id == user.id, UserDevice.device_id == body.device_id))).first() is not None
    db.add(LoginEvent(user_id=user.id, success=True, ip=ip, user_agent=(request.headers.get("user-agent") or "")[:300],
                      device_id=body.device_id, new_device=bool(body.device_id) and not known))
    now = utcnow()
    p.sign_count, p.last_used_at = v.new_sign_count, now
    user.failed_logins, user.locked_until = 0, None
    user.last_login_at, user.last_login_ip, user.login_count, user.last_seen_at = now, ip, (user.login_count or 0) + 1, now
    await db.commit()
    return {"user": user_public(user), "access_token": create_access_token(user.id, user.token_version), "token_type": "bearer"}


# ---------------- manage ----------------
@router.get("/profile/passkeys")
async def list_passkeys(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Passkey).where(Passkey.user_id == user.id).order_by(Passkey.id))).scalars().all()
    return {"items": [{"id": p.id, "device_name": p.device_name, "created_at": dt(p.created_at), "last_used_at": dt(p.last_used_at)} for p in rows]}


@router.delete("/profile/passkeys/{pid}", status_code=204)
async def delete_passkey(pid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    res = await db.execute(delete(Passkey).where(Passkey.id == pid, Passkey.user_id == user.id))
    if not res.rowcount:
        raise HTTPException(404, "Not found")
    await db.commit()

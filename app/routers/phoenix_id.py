"""Phoenix ID single sign-on. The old phone+password login keeps working; SSO is an additional way in.
Flow: the app does Authorization Code + PKCE with Phoenix ID, then POSTs the Phoenix access token to /api/auth/phoenix-id/exchange,
which verifies it locally (JWKS) and returns the normal Wasaly session (access + rotating refresh token)."""
import logging
from urllib.parse import urlencode

import jwt
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import Role
from ..database import get_db
from ..deps import current_user, super_admin_user
from ..models import OwnerConfig, User, utcnow
from ..security import encrypt, normalize_phone
from ..serializers import user_public
from ..services import command, kyc, sessions
from ..services import phoenix_id as pid
from ..services.audit import audit
from ..services.ratelimit import limit

log = logging.getLogger("phoenix_id")
router = APIRouter(tags=["phoenix-id"])

ROLE_OF = {Role.CUSTOMER: "customer", Role.DRIVER: "driver", Role.ADMIN: "admin", Role.SUPER_ADMIN: "admin"}
SCOPES = "openid profile phone email offline_access"
REDIRECTS = {"mobile": "online.phoenixsystems.wasaly:/oauthredirect", "web": "https://phoenixsystems.online/wasaly/auth/callback",
             "admin_web": "https://phoenixsystems.online/wasaly-admin/auth/callback"}
LOGOUT_REDIRECTS = {"mobile": "online.phoenixsystems.wasaly:/logout", "admin_web": "https://phoenixsystems.online/wasaly-admin/"}


# ------------------------------------------------------------------ public config for the apps ----------
@router.get("/api/auth/phoenix-id/config")
async def public_config():
    c = await pid.cfg()
    allowed, _why = await pid.local_login_state()
    return {"enabled": c["enabled"], "local_login_allowed": allowed, "verify_url": await kyc.verify_url(), "issuer": c["issuer"], "client_id": c["client_id"], "scopes": SCOPES, "pkce": "S256",
            "redirect_uris": REDIRECTS, "post_logout_redirect_uris": LOGOUT_REDIRECTS}


# ------------------------------------------------------------------ token exchange / link ----------
class ExchangeIn(BaseModel):
    access_token: str = Field(min_length=20, max_length=4000)
    device_id: str | None = Field(default=None, max_length=100)


async def _claims_and_info(token: str) -> tuple[dict, dict]:
    c = await pid.cfg()
    if not c["enabled"]:
        raise HTTPException(403, {"code": "SSO_DISABLED", "message": "Phoenix ID sign-in is not enabled"})
    try:
        claims = await pid.verify(token)
    except jwt.PyJWTError as e:
        log.warning("phoenix id token rejected: %s", type(e).__name__)
        raise HTTPException(401, {"code": "INVALID_TOKEN"})
    except Exception:  # noqa: BLE001 - discovery/JWKS unreachable
        raise HTTPException(503, {"code": "PHOENIX_ID_UNREACHABLE"})
    try:
        info = await pid.userinfo(token)
    except Exception:  # noqa: BLE001
        info = {}
    return claims, info


def _check_role(user: User, claims: dict) -> None:
    roles = claims.get("roles") or []
    if ROLE_OF.get(user.role) not in roles:
        raise HTTPException(403, {"code": "ROLE_NOT_ALLOWED", "message": "This Phoenix ID account has no access to this role"})


@router.post("/api/auth/phoenix-id/exchange", dependencies=[Depends(limit("pid_exchange", 30, 300))])
async def exchange(body: ExchangeIn, request: Request, db: AsyncSession = Depends(get_db)):
    return await _login(body.access_token, body.device_id, request, db)


class CallbackIn(BaseModel):
    code: str = Field(min_length=4, max_length=2000)
    code_verifier: str = Field(min_length=20, max_length=300)
    nonce: str = Field(min_length=4, max_length=300)
    redirect_uri: str = Field(max_length=300)
    device_id: str | None = Field(default=None, max_length=100)


class RefreshIn(BaseModel):
    phoenix_refresh_token: str = Field(min_length=10, max_length=4000)
    device_id: str | None = Field(default=None, max_length=100)


async def _grant(data: dict) -> dict:
    try:
        return await pid.token_request(data)
    except ValueError:
        raise HTTPException(401, {"code": "INVALID_TOKEN"})
    except Exception:  # noqa: BLE001
        raise HTTPException(503, {"code": "PHOENIX_ID_UNREACHABLE"})


async def _web_login(tok: dict, device_id, request, db, nonce: str | None = None) -> dict:
    access = tok.get("access_token")
    if not access:
        raise HTTPException(401, {"code": "INVALID_TOKEN"})
    if nonce is not None:
        try:
            idc = await pid.verify(tok.get("id_token") or "")
        except Exception:  # noqa: BLE001
            raise HTTPException(401, {"code": "INVALID_TOKEN"})
        if idc.get("nonce") != nonce:
            raise HTTPException(401, {"code": "INVALID_TOKEN"})
    out = await _login(access, device_id, request, db, expose_token=True)
    return {**out, "phoenix_refresh_token": tok.get("refresh_token"), "id_token": tok.get("id_token")}


@router.post("/api/auth/phoenix-id/callback", dependencies=[Depends(limit("pid_exchange", 30, 300))])
async def callback(body: CallbackIn, request: Request, db: AsyncSession = Depends(get_db)):
    """Web apps cannot call the Phoenix ID token endpoint (no CORS): the code is exchanged here, server to server."""
    if not (await pid.cfg())["enabled"]:
        raise HTTPException(403, {"code": "SSO_DISABLED", "message": "Phoenix ID sign-in is not enabled"})
    if body.redirect_uri not in REDIRECTS.values():
        raise HTTPException(422, {"code": "BAD_REDIRECT_URI"})
    tok = await _grant({"grant_type": "authorization_code", "code": body.code, "redirect_uri": body.redirect_uri, "code_verifier": body.code_verifier})
    return await _web_login(tok, body.device_id, request, db, nonce=body.nonce)


@router.post("/api/auth/phoenix-id/refresh", dependencies=[Depends(limit("pid_refresh", 60, 300))])
async def refresh(body: RefreshIn, request: Request, db: AsyncSession = Depends(get_db)):
    if not (await pid.cfg())["enabled"]:
        raise HTTPException(403, {"code": "SSO_DISABLED", "message": "Phoenix ID sign-in is not enabled"})
    tok = await _grant({"grant_type": "refresh_token", "refresh_token": body.phoenix_refresh_token})
    return await _web_login(tok, body.device_id, request, db)


async def _login(access_token: str, device_id, request: Request, db: AsyncSession, expose_token: bool = False) -> dict:
    claims, info = await _claims_and_info(access_token)
    sub = claims["sub"]
    user = (await db.execute(select(User).where(User.phoenix_sub == sub))).scalar_one_or_none()
    how = "sub"
    if not user and str(claims.get("system_user_id") or "").isdigit():  # id from the migration export of THIS system
        cand = await db.get(User, int(claims["system_user_id"]))
        if cand and cand.phoenix_sub is None:
            user, how = cand, "system_user_id"
    if not user and info.get("phone_number") and info.get("phone_number_verified"):  # linking only through a VERIFIED phone
        try:
            phone = normalize_phone(info["phone_number"])
        except ValueError:
            phone = None
        cand = (await db.execute(select(User).where(User.phone == phone))).scalar_one_or_none() if phone else None
        if cand and cand.phoenix_sub is None:
            user, how = cand, "verified_phone"
    if not user:
        raise HTTPException(409, {"code": "REGISTRATION_REQUIRED",
                                  "message": "No Wasaly account is linked to this Phoenix ID. Register in the app first (CPR), then link it.",
                                  "profile": {"name": info.get("name"), "phone": info.get("phone_number"), "email": info.get("email")},
                                  **({"phoenix_access_token": access_token} if expose_token else {})})
    if user.account_status in ("DELETED", "BANNED"):
        raise HTTPException(403, {"code": "ACCOUNT_" + user.account_status, "reason": user.status_reason})
    _check_role(user, claims)
    if user.phoenix_sub is None:
        user.phoenix_sub = sub
        audit(db, user.id, "PHOENIX_ID_LINKED", "user", user.id, details={"how": how})
    act = (claims.get("act") or {}).get("sub")
    if act:  # impersonation: record the real actor
        audit(db, None, "PHOENIX_ID_IMPERSONATION", "user", user.id, details={"actor_sub": act, "sid": claims.get("sid")})
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)
    now = utcnow()
    user.failed_logins, user.locked_until, user.last_login_at, user.last_login_ip, user.last_seen_at = 0, None, now, ip, now
    user.login_count = (user.login_count or 0) + 1
    kyc.apply_claims(user, claims, info)
    tokens = await sessions.issue(db, user, device_id)
    await db.commit()
    return {"user": user_public(user), **tokens, "impersonated_by": act, "sso": True}


@router.post("/api/auth/phoenix-id/link")
async def link(body: ExchangeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """A signed-in user (old login) links their Phoenix ID. Used by new customers after normal registration."""
    claims, _ = await _claims_and_info(body.access_token)
    taken = (await db.execute(select(User.id).where(User.phoenix_sub == claims["sub"], User.id != user.id))).first()
    if taken or (user.phoenix_sub and user.phoenix_sub != claims["sub"]):
        raise HTTPException(409, {"code": "ALREADY_LINKED"})
    _check_role(user, claims)
    user.phoenix_sub = claims["sub"]
    audit(db, user.id, "PHOENIX_ID_LINKED", "user", user.id, details={"how": "signed_in_user"})
    await db.commit()
    return {"linked": True}


# ------------------------------------------------------------------ single logout ----------
class LogoutIn(BaseModel):
    id_token: str | None = Field(default=None, max_length=6000)
    redirect: str = Field(default="mobile", pattern="^(mobile|admin_web)$")
    state: str | None = Field(default=None, max_length=200)


@router.post("/api/auth/phoenix-id/logout")
async def sso_logout(body: LogoutIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Revokes the local session server-side and returns the Phoenix ID end_session URL the client must open (system browser on mobile),
    so the shared Phoenix ID session is cleared and back-channel logout reaches the other systems."""
    user.token_version += 1
    await sessions.revoke_all(db, user.id)
    audit(db, user.id, "PHOENIX_ID_LOGOUT", "user", user.id)
    await db.commit()
    c = await pid.cfg()
    url = None
    if c["enabled"]:
        try:
            ep = (await pid.discovery()).get("end_session_endpoint") or f"{c['issuer']}/logout"
        except Exception:  # noqa: BLE001
            ep = f"{c['issuer']}/logout"
        q = {"client_id": c["client_id"], "post_logout_redirect_uri": LOGOUT_REDIRECTS[body.redirect]}
        if body.id_token:
            q["id_token_hint"] = body.id_token
        if body.state:
            q["state"] = body.state
        url = f"{ep}?{urlencode(q)}"
    return {"ok": True, "end_session_url": url}


# ------------------------------------------------------------------ back-channel logout ----------
@router.post("/auth/phoenix-id/backchannel-logout")
async def backchannel_logout(logout_token: str = Form(...), db: AsyncSession = Depends(get_db)):
    try:
        claims = await pid.verify(logout_token, logout=True)
    except jwt.PyJWTError:
        raise HTTPException(400, {"error": "invalid_request"})
    except Exception:  # noqa: BLE001
        raise HTTPException(503, {"error": "temporarily_unavailable"})
    user = (await db.execute(select(User).where(User.phoenix_sub == claims["sub"]))).scalar_one_or_none()
    if user:
        user.token_version += 1  # ends every access token of the user
        await sessions.revoke_all(db, user.id)  # and every refresh token
        audit(db, None, "PHOENIX_ID_BACKCHANNEL_LOGOUT", "user", user.id, details={"sid": claims.get("sid")})
        await db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ owner settings card ----------
class SettingsIn(BaseModel):
    enabled: bool | None = None
    client_id: str | None = Field(default=None, max_length=60)
    issuer: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=300)  # write-only; empty = keep current
    maintenance_login: bool | None = None
    kyc_rules: dict[str, bool] | None = None


async def _put(db, key: str, value: str, secret: bool = False):
    row = await db.get(OwnerConfig, key)
    enc = encrypt(value) if secret else value
    if row:
        row.value, row.is_secret = enc, secret
    else:
        db.add(OwnerConfig(key=key, value=enc, is_secret=secret))


async def _view() -> dict:
    c = await pid.cfg()
    allowed, why = await pid.local_login_state()
    return {"enabled": c["enabled"], "client_id": c["client_id"], "issuer": c["issuer"], "client_secret_set": bool(c["client_secret"]),
            "maintenance_login": (await command.owner_all()).get("phoenix_id_maintenance_login", "false") == "true",
            "local_login_allowed": allowed, "local_login_reason": why, "kyc_rules": await kyc.rules()}


@router.get("/api/admin/phoenix-id")
async def get_settings(admin: User = Depends(super_admin_user)):
    return await _view()


@router.put("/api/admin/phoenix-id")
async def put_settings(body: SettingsIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    changed = []
    if body.enabled is not None:
        await _put(db, "phoenix_id_enabled", "true" if body.enabled else "false")
        changed.append("enabled")
    if body.client_id:
        await _put(db, "phoenix_id_client_id", body.client_id.strip())
        changed.append("client_id")
    if body.issuer:
        await _put(db, "phoenix_id_issuer", body.issuer.strip().rstrip("/"))
        changed.append("issuer")
    if body.client_secret:
        await _put(db, "phoenix_id_client_secret", body.client_secret.strip(), secret=True)
        changed.append("client_secret")
    if body.maintenance_login is not None:
        await _put(db, "phoenix_id_maintenance_login", "true" if body.maintenance_login else "false")
        changed.append(f"maintenance_login={'on' if body.maintenance_login else 'off'}")
    for k, v in (body.kyc_rules or {}).items():
        if k in kyc.RULES:
            await _put(db, k, "true" if v else "false")
            changed.append(f"{k}={'on' if v else 'off'}")
    audit(db, admin.id, "PHOENIX_ID_SETTINGS", "settings", "phoenix_id", details={"changed": changed})  # never the secret
    await db.commit()
    command.invalidate()
    pid._disc["at"] = pid._jwks["at"] = pid._reach["at"] = 0.0
    return await _view()


@router.post("/api/admin/phoenix-id/test")
async def test_discovery(admin: User = Depends(super_admin_user)):
    command.invalidate()
    try:
        d = await pid.discovery(force=True)
        return {"ok": True, "issuer": d.get("issuer"), "jwks_uri": d.get("jwks_uri")}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": type(e).__name__}

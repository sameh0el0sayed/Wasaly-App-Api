"""SEC-001: short-lived access tokens + rotating, hashed refresh tokens with reuse detection and revocation.

access_token_minutes (setting, default 0) = 0 keeps the legacy non-expiring access token, so apps that do not yet use the
refresh token keep working. Set it to 30 once the apps call POST /api/auth/token/refresh. Refresh tokens are always issued."""
import hashlib
import secrets
import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select, update

from ..models import RefreshToken, User, utcnow
from ..security import create_access_token
from . import settings_service

REFRESH_DAYS = 180


def _hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _unauthorized() -> HTTPException:
    return HTTPException(401, {"code": "UNAUTHORIZED", "message": "Session is no longer valid"})


async def _minutes(db) -> int:
    try:
        return max(0, int((await settings_service.get_all(db)).get("access_token_minutes") or 0))
    except (TypeError, ValueError):
        return 0


async def issue(db, user: User, device_id: str | None = None, family_id: str | None = None) -> dict:
    """New access + refresh token pair (caller commits)."""
    minutes = await _minutes(db)
    raw = secrets.token_urlsafe(48)
    db.add(RefreshToken(user_id=user.id, family_id=family_id or str(uuid.uuid4()), token_hash=_hash(raw),
                        token_version=user.token_version, device_id=(device_id or None),
                        expires_at=utcnow() + timedelta(days=REFRESH_DAYS)))
    out = {"access_token": create_access_token(user.id, user.token_version, minutes), "token_type": "bearer",
           "refresh_token": raw}
    if minutes:
        out["expires_in"] = minutes * 60
    return out


async def rotate(db, raw: str) -> dict:
    """Exchange a refresh token for a new pair. A token used twice revokes its whole family."""
    now = utcnow()
    row = (await db.execute(select(RefreshToken).where(RefreshToken.token_hash == _hash(raw or "")).with_for_update())).scalar_one_or_none()
    if not row or row.revoked_at or row.expires_at <= now:
        raise _unauthorized()
    if row.used_at:  # reuse of a rotated token: assume theft, kill the family
        await db.execute(update(RefreshToken).where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
                         .values(revoked_at=now))
        await db.commit()
        raise _unauthorized()
    user = await db.get(User, row.user_id)
    if not user or user.token_version != row.token_version or user.account_status in ("BANNED", "DELETED"):
        raise _unauthorized()
    row.used_at = now
    out = await issue(db, user, row.device_id, row.family_id)
    await db.commit()
    return out


async def revoke_all(db, user_id: int) -> None:
    """Logout / password change / ban: every refresh token of the user stops working (caller commits)."""
    await db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                     .values(revoked_at=utcnow()))

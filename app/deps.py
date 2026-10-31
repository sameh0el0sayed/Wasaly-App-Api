import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from .constants import AccountStatus, Role
from .database import get_db
from .models import User, utcnow
from .security import decode_access_token

bearer = HTTPBearer(auto_error=False)
TOUCH_EVERY_SECONDS = 30  # do not write on every request


async def touch_last_seen(user: User) -> None:
    """Any authenticated request = the user is active. Own short session so GET requests (which never commit) count too."""
    now = utcnow()
    if user.last_seen_at and (now - user.last_seen_at).total_seconds() < TOUCH_EVERY_SECONDS:
        return
    try:
        from sqlalchemy import update
        from .database import SessionLocal
        async with SessionLocal() as s:
            await s.execute(update(User).where(User.id == user.id).values(last_seen_at=now))
            await s.commit()
        user.__dict__["last_seen_at"] = now
    except Exception:  # noqa  activity tracking must never break a request
        pass


async def current_user(creds: HTTPAuthorizationCredentials | None = Depends(bearer),
                       db: AsyncSession = Depends(get_db)) -> User:
    """Any authenticated user, any account status (so pending users can see their review state)."""
    if not creds:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHORIZED", "message": "Not authenticated"})
    try:
        data = decode_access_token(creds.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHORIZED", "message": "Invalid or expired token"})
    user = await db.get(User, int(data["sub"]))
    if not user or user.token_version != data.get("tv"):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "UNAUTHORIZED", "message": "Session is no longer valid"})
    await touch_last_seen(user)
    return user


async def approved_user(user: User = Depends(current_user)) -> User:
    if user.account_status != AccountStatus.APPROVED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, {"code": "ACCOUNT_NOT_APPROVED",
                                                        "account_status": user.account_status,
                                                        "reason": user.status_reason})
    return user


async def customer_user(user: User = Depends(approved_user)) -> User:
    if user.role != Role.CUSTOMER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Customers only")
    return user


async def driver_user(user: User = Depends(approved_user)) -> User:
    if user.role != Role.DRIVER or not user.driver_profile:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Drivers only")
    return user


async def admin_user(user: User = Depends(approved_user)) -> User:
    if user.role not in Role.ADMINS:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admins only")
    return user


async def super_admin_user(user: User = Depends(admin_user)) -> User:
    if user.role != Role.SUPER_ADMIN:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super admin permission required")
    return user


def paging(limit: int = 50, offset: int = 0) -> tuple[int, int]:
    return max(1, min(limit, 200)), max(0, offset)

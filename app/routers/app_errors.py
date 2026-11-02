"""Errors sent by the mobile apps, and the admin view of them."""
import json
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from datetime import time as dtime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user, optional_user, paging
from ..models import AppErrorLog, User, utcnow
from ..serializers import dt

router = APIRouter(prefix="/api", tags=["app-errors"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-app-errors"])

MAX_ITEMS, RATE_PER_MIN, RETENTION_DAYS = 50, 30, 30
LEVELS = {"fatal", "error", "warning"}
_hits: dict[str, deque] = defaultdict(deque)  # in-process limiter (one API instance)


class _Loose(BaseModel):
    model_config = ConfigDict(extra="ignore")


class DeviceInfo(_Loose):
    device_id: str | None = None
    platform: str | None = None
    model: str | None = None
    os_name: str | None = None
    os_version: str | None = None
    app_version: str | None = None
    locale: str | None = None


class ErrorItem(_Loose):
    level: str | None = None
    source: str | None = None
    error_type: str | None = None
    message: str | None = None
    stack: str | None = None
    context: dict | list | None = None
    occurred_at: str | None = None


class ErrorBatch(_Loose):
    device: DeviceInfo = DeviceInfo()
    items: list[ErrorItem] = []


def _cut(v, n: int):
    return v[:n] if isinstance(v, str) else v


def _rate_limit(key: str) -> bool:
    now, q = time.time(), _hits[key]
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        return False
    q.append(now)
    if len(_hits) > 5000:  # forget idle keys
        for k in [k for k, d in _hits.items() if not d or now - d[-1] > 60]:
            _hits.pop(k, None)
    return True


def _when(s: str | None) -> datetime:
    try:
        d = datetime.fromisoformat((s or "").replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return utcnow()


def _context(c):
    if c is None:
        return None
    raw = json.dumps(c, default=str)
    return c if len(raw) <= 20_000 else {"_truncated": True, "preview": raw[:20_000]}


@router.post("/app/errors", status_code=201)
async def report_errors(body: ErrorBatch, request: Request, user: User | None = Depends(optional_user), db: AsyncSession = Depends(get_db)):
    """Open endpoint (a crash can happen before login). A bad or expired token just means the error is stored anonymously."""
    if not body.items:
        raise HTTPException(422, {"code": "MISSING_ITEMS"})
    if len(body.items) > MAX_ITEMS:
        raise HTTPException(422, {"code": "TOO_MANY_ITEMS", "max": MAX_ITEMS})
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)
    d = body.device
    keys = [f"ip:{ip}"] + ([f"dev:{d.device_id}"] if d.device_id else [])
    if not all([_rate_limit(k) for k in keys]):
        raise HTTPException(429, {"code": "RATE_LIMIT"})
    for it in body.items:
        db.add(AppErrorLog(
            user_id=user.id if user else None, device_id=_cut(d.device_id, 100), platform=_cut(d.platform, 20), model=_cut(d.model, 120),
            os_name=_cut(d.os_name, 40), os_version=_cut(d.os_version, 40), app_version=_cut(d.app_version, 30), locale=_cut(d.locale, 20),
            level=it.level if it.level in LEVELS else "error", source=_cut(it.source or "unknown", 40), error_type=_cut(it.error_type, 120),
            message=_cut(it.message or "", 4000), stack=_cut(it.stack, 12000), context=_context(it.context),
            occurred_at=_when(it.occurred_at), ip=_cut(ip, 64)))
    await db.commit()
    return {"stored": len(body.items)}


async def purge_old(db: AsyncSession) -> None:
    await db.execute(delete(AppErrorLog).where(AppErrorLog.created_at < utcnow() - timedelta(days=RETENTION_DAYS)))


def _day(s: str, end: bool = False) -> datetime:
    try:
        return datetime.combine(datetime.fromisoformat(s).date(), dtime.max if end else dtime.min, tzinfo=UTC)
    except ValueError:
        raise HTTPException(422, {"code": "INVALID_DATE", "value": s})


@admin_router.get("/app-errors")
async def list_errors(level: str | None = None, source: str | None = None, user_id: int | None = None, app_version: str | None = None,
                      date_from: str | None = None, date_to: str | None = None, page=Depends(paging),
                      admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(AppErrorLog)
    for col, val in ((AppErrorLog.level, level), (AppErrorLog.source, source), (AppErrorLog.user_id, user_id), (AppErrorLog.app_version, app_version)):
        if val is not None:
            q = q.where(col == val)
    if date_from:
        q = q.where(AppErrorLog.created_at >= _day(date_from))
    if date_to:
        q = q.where(AppErrorLog.created_at <= _day(date_to, end=True))
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(AppErrorLog.created_at.desc(), AppErrorLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    names = dict((await db.execute(select(User.id, User.full_name).where(User.id.in_({r.user_id for r in rows if r.user_id})))).all()) if rows else {}
    return {"total": total, "items": [{
        "id": r.id, "user_id": r.user_id, "user_name": names.get(r.user_id), "level": r.level, "source": r.source, "error_type": r.error_type,
        "message": r.message, "stack": r.stack, "context": r.context, "device_id": r.device_id, "platform": r.platform, "model": r.model,
        "os_name": r.os_name, "os_version": r.os_version, "app_version": r.app_version, "locale": r.locale,
        "occurred_at": dt(r.occurred_at), "created_at": dt(r.created_at)} for r in rows]}


@admin_router.get("/app-errors/summary")
async def errors_summary(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    since = utcnow() - timedelta(hours=24)
    by_level = dict((await db.execute(select(AppErrorLog.level, func.count()).where(AppErrorLog.created_at >= since).group_by(AppErrorLog.level))).all())
    top = (await db.execute(select(AppErrorLog.source, func.count().label("n")).where(AppErrorLog.created_at >= since)
                            .group_by(AppErrorLog.source).order_by(func.count().desc()).limit(10))).all()
    return {"last_24h": {k: by_level.get(k, 0) for k in ("fatal", "error", "warning")},
            "top_sources": [{"source": s, "count": n} for s, n in top]}

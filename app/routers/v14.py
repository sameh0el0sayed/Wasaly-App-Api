"""v14: translations dictionary, driver licence renewal, product analytics."""
import hashlib
import json
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OfferStatus, Role
from ..database import SessionLocal, get_db
from ..deps import admin_user, driver_user, optional_user
from ..models import AppEvent, DriverWorkSession, OrderOffer, ProfileChange, Translation, User, utcnow
from ..services import corrections as C
from ..services import settings_service
from ..services.files import assert_owned
from ..services.payments import notify_admins

router = APIRouter(prefix="/api", tags=["v14"])
admin_router = APIRouter(prefix="/api/admin", tags=["v14-admin"])
SEED = Path(__file__).resolve().parent.parent.parent / "seeds" / "translations_seed.json"


# ======================= A. translations =======================
async def _bump(db: AsyncSession) -> int:
    s = await settings_service.get_all(db)
    v = int(s.get("i18n_version") or 0) + 1
    await settings_service.set_value(db, "i18n_version", str(v), None)
    return v


async def seed_translations() -> None:
    """First start: load the bundled dictionary when the table is empty."""
    if not SEED.exists():
        return
    async with SessionLocal() as db:
        if (await db.execute(select(func.count()).select_from(Translation))).scalar_one():
            return
        for it in json.loads(SEED.read_text(encoding="utf-8")):
            db.add(Translation(key=it["key"], ar=it.get("ar") or "", en=it.get("en") or ""))
        await _bump(db)
        await db.commit()


@router.get("/app/translations")
async def get_translations(request: Request, response: Response, lang: str = "ar", db: AsyncSession = Depends(get_db)):
    lang = "en" if lang == "en" else "ar"
    version = int((await settings_service.get_all(db))["i18n_version"] or 0)
    etag = f'"{version}-{lang}"'
    headers = {"ETag": etag, "Cache-Control": "public, max-age=86400"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    rows = (await db.execute(select(Translation.key, getattr(Translation, lang)))).all()
    response.headers.update(headers)
    return {"version": version, "lang": lang, "strings": {k: t for k, t in rows if t}}


class TranslationPatch(BaseModel):
    ar: str | None = None
    en: str | None = None


class ImportItem(BaseModel):
    key: str = Field(min_length=1, max_length=500)
    ar: str | None = None
    en: str | None = None


class ImportIn(BaseModel):
    items: list[ImportItem]
    overwrite: bool = False


def _tr(t: Translation) -> dict:
    return {"id": t.id, "key": t.key, "ar": t.ar, "en": t.en, "updated_at": t.updated_at.isoformat() if t.updated_at else None}


@admin_router.get("/translations")
async def list_translations(q: str | None = None, missing: str | None = None, limit: int = 50, offset: int = 0,
                            admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = max(1, min(limit, 500)), max(0, offset)
    stmt = select(Translation)
    if q:
        stmt = stmt.where(or_(Translation.key.ilike(f"%{q}%"), Translation.ar.ilike(f"%{q}%"), Translation.en.ilike(f"%{q}%")))
    if missing in ("en", "ar"):
        stmt = stmt.where(getattr(Translation, missing) == "")
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(Translation.id).limit(limit).offset(offset))).scalars().all()
    missing_en = (await db.execute(select(func.count()).select_from(Translation).where(Translation.en == ""))).scalar_one()
    return {"items": [_tr(t) for t in rows], "total": total, "missing_en": missing_en}


@admin_router.patch("/translations/{tid}")
async def patch_translation(tid: int, body: TranslationPatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    t = await db.get(Translation, tid)
    if not t:
        raise HTTPException(404, "Not found")
    if body.ar is not None:
        t.ar = body.ar.strip()
    if body.en is not None:
        t.en = body.en.strip()
    t.updated_at = utcnow()
    v = await _bump(db)
    await db.commit()
    return {**_tr(t), "version": v}


@admin_router.post("/translations/import")
async def import_translations(body: ImportIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    existing = {t.key: t for t in (await db.execute(select(Translation))).scalars().all()}
    created = updated = skipped = 0
    for it in body.items:
        t = existing.get(it.key)
        if not t:
            t = Translation(key=it.key, ar=(it.ar or "").strip(), en=(it.en or "").strip())
            db.add(t)
            existing[it.key] = t
            created += 1
            continue
        changed = False
        for lang in ("ar", "en"):
            new = (getattr(it, lang) or "").strip()
            if new and new != getattr(t, lang) and (body.overwrite or not getattr(t, lang)):
                setattr(t, lang, new)
                changed = True
        updated, skipped = (updated + 1, skipped) if changed else (updated, skipped + 1)
    v = 0
    if created or updated:
        v = await _bump(db)
    await db.commit()
    return {"created": created, "updated": updated, "skipped": skipped, "version": v or int((await settings_service.get_all(db))["i18n_version"] or 0)}


@admin_router.get("/translations/export")
async def export_translations(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Translation).order_by(Translation.id))).scalars().all()
    return {"items": [{"key": t.key, "ar": t.ar, "en": t.en} for t in rows], "overwrite": False}


# ======================= B. driver licence renewal =======================
class LicenseIn(BaseModel):
    issue_date: date
    expiry_date: date
    photo: str
    license_number: str | None = Field(default=None, min_length=4, max_length=30)


@router.post("/driver/license", status_code=202)
async def renew_license(body: LicenseIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Renewal goes to admin review. Offers stay blocked while expired; the account is never suspended."""
    if body.expiry_date <= date.today():
        raise HTTPException(422, {"code": "EXPIRY_IN_PAST"})
    if body.issue_date >= body.expiry_date:
        raise HTTPException(422, {"code": "INVALID_DATES"})
    if (await db.execute(select(ProfileChange.id).where(ProfileChange.user_id == user.id, ProfileChange.field == "license",
                                                         ProfileChange.status == C.PENDING).limit(1))).first():
        raise HTTPException(409, {"code": "RENEWAL_ALREADY_PENDING"})
    await assert_owned(db, user.id, body.photo)
    db.add(ProfileChange(user_id=user.id, kind=C.CHANGE, field="license", status=C.PENDING, requested_by=user.id, submitted_at=utcnow(),
                         new_value=json.dumps({"issue_date": body.issue_date.isoformat() if body.issue_date else None, "expiry_date": body.expiry_date.isoformat(), "photo": body.photo,
                                               "license_number": body.license_number})))
    await notify_admins(db, "PROFILE_CHANGE_SUBMITTED", "طلب تجديد رخصة", f"قدّم السائق {user.full_name} طلب تجديد الرخصة.")
    await db.commit()
    return {"status": "RENEWAL_PENDING"}


# ======================= C. analytics =======================
_hits: dict[str, list[float]] = defaultdict(list)


class EventIn(BaseModel):
    name: str = Field(max_length=60)
    session_id: str | None = Field(default=None, max_length=64)
    screen: str | None = Field(default=None, max_length=120)
    ts: datetime | None = None
    props: dict | None = None


class EventsIn(BaseModel):
    device_id: str | None = Field(default=None, max_length=100)
    app_version: str | None = Field(default=None, max_length=20)
    platform: str | None = Field(default=None, max_length=10)
    events: list[EventIn] = []


@router.post("/app/events", status_code=202)
async def post_events(body: EventsIn, user: User | None = Depends(optional_user), db: AsyncSession = Depends(get_db)):
    """Never blocks the app: always 202."""
    try:
        if (await settings_service.get_all(db))["analytics_enabled"] != "true":
            return {"accepted": 0}
        key, now = body.device_id or (f"u{user.id}" if user else "anon"), time.time()
        hits = [t for t in _hits[key] if now - t < 60]
        if len(hits) >= 30:
            _hits[key] = hits
            return {"accepted": 0}
        _hits[key] = [*hits, now]
        role = user.role if user and user.role in (Role.CUSTOMER, Role.DRIVER) else ("ADMIN" if user else "GUEST")
        n = 0
        for e in body.events[:50]:
            ts = e.ts
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            db.add(AppEvent(user_id=user.id if user else None, role=role, device_id=body.device_id, session_id=e.session_id, name=e.name,
                            screen=e.screen, props=e.props if isinstance(e.props, dict) and len(json.dumps(e.props)) < 2000 else None,
                            app_version=body.app_version, platform=body.platform, client_ts=ts))
            n += 1
        await db.commit()
        return {"accepted": n}
    except Exception:  # noqa  analytics must never break the app
        await db.rollback()
        return {"accepted": 0}


STEPS = ["order_start", "pickup_set", "destination_set", "price_view", "order_submit", "driver_assigned", "trip_completed"]


def _range(frm: date | None, to: date | None) -> tuple[datetime, datetime, date]:
    to = to or date.today()
    frm = frm or to - timedelta(days=29)
    return (datetime.combine(frm, datetime.min.time(), UTC), datetime.combine(to + timedelta(days=1), datetime.min.time(), UTC), to)


def _actor(user_id, device_id, session_id) -> str:
    return f"u{user_id}" if user_id else (f"d{device_id}" if device_id else f"s{session_id}")


async def _sessions(db: AsyncSession, a: datetime, b: datetime, names: list[str] | None = None):
    stmt = select(AppEvent.session_id, AppEvent.name, AppEvent.props, AppEvent.id).where(
        AppEvent.created_at >= a, AppEvent.created_at < b, AppEvent.session_id.is_not(None))
    if names:
        stmt = stmt.where(AppEvent.name.in_(names))
    seqs: dict[str, list[tuple[str, dict | None]]] = defaultdict(list)
    for sid, name, props, _ in (await db.execute(stmt.order_by(AppEvent.id))).all():
        seqs[sid].append((name, props))
    return seqs


def _reached(seq: list[tuple[str, dict | None]], service: str | None = None) -> int:
    """Number of funnel steps reached in order (0..len(STEPS))."""
    i = 0
    for name, props in seq:
        if service and name == "order_submit" and (props or {}).get("service") not in (None, service):
            continue
        if i < len(STEPS) and name == STEPS[i]:
            i += 1
    return i


@admin_router.get("/analytics/overview")
async def an_overview(frm: date | None = Query(None, alias="from"), to: date | None = None,
                      admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a, b, end = _range(frm, to)
    E = AppEvent

    async def actors(x, y):
        rows = (await db.execute(select(E.user_id, E.device_id, E.session_id).where(E.created_at >= x, E.created_at < y).distinct())).all()
        return len({_actor(*r) for r in rows})
    day_end = datetime.combine(end + timedelta(days=1), datetime.min.time(), UTC)
    rows = (await db.execute(select(E.session_id, func.min(E.created_at), func.max(E.created_at)).where(
        E.created_at >= a, E.created_at < b, E.session_id.is_not(None)).group_by(E.session_id))).all()
    mins = [(hi - lo).total_seconds() / 60 for _, lo, hi in rows]
    plat = (await db.execute(select(E.platform, func.count(func.distinct(E.device_id))).where(
        E.created_at >= a, E.created_at < b, E.platform.is_not(None)).group_by(E.platform))).all()
    ver = (await db.execute(select(E.app_version, func.count(func.distinct(E.device_id))).where(
        E.created_at >= a, E.created_at < b, E.app_version.is_not(None)).group_by(E.app_version))).all()
    new_users = (await db.execute(select(func.count()).select_from(User).where(
        User.role.in_([Role.CUSTOMER, Role.DRIVER]), User.created_at >= a, User.created_at < b))).scalar_one()
    return {"dau": await actors(day_end - timedelta(days=1), day_end), "wau": await actors(day_end - timedelta(days=7), day_end),
            "mau": await actors(day_end - timedelta(days=30), day_end), "new_users": new_users, "sessions": len(rows),
            "avg_session_minutes": round(sum(mins) / len(mins), 1) if mins else 0,
            "platforms": {k: v for k, v in plat}, "app_versions": {k: v for k, v in ver}}


@admin_router.get("/analytics/funnel")
async def an_funnel(frm: date | None = Query(None, alias="from"), to: date | None = None, service: str | None = None,
                    admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a, b, _ = _range(frm, to)
    seqs = await _sessions(db, a, b, STEPS)
    counts = [0] * len(STEPS)
    for seq in seqs.values():
        for i in range(_reached(seq, service)):
            counts[i] += 1
    out = []
    for i, name in enumerate(STEPS):
        out.append({"name": name, "users": counts[i], "pct_of_first": round(100 * counts[i] / counts[0], 1) if counts[0] else 0,
                    "pct_of_previous": round(100 * counts[i] / counts[i - 1], 1) if i and counts[i - 1] else (100 if counts[i] else 0)})
    return {"steps": out}


@admin_router.get("/analytics/drop-off")
async def an_dropoff(frm: date | None = Query(None, alias="from"), to: date | None = None,
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a, b, _ = _range(frm, to)
    seqs = await _sessions(db, a, b, [*STEPS, "order_cancel"])
    stops: dict[str, list[str]] = defaultdict(list)
    for sid, seq in seqs.items():
        n = _reached(seq)
        if 0 < n < len(STEPS):
            stops[STEPS[n - 1]].append(sid)
    items = []
    for step in STEPS[:-1]:
        sids = stops.get(step, [])
        reasons: dict[str, int] = defaultdict(int)
        for sid in sids:
            for name, props in seqs[sid]:
                if name == "order_cancel" and (props or {}).get("reason"):
                    reasons[str(props["reason"])] += 1
        items.append({"after_step": step, "sessions": len(sids),
                      "top_cancel_reasons": [{"reason": r, "count": c} for r, c in sorted(reasons.items(), key=lambda x: -x[1])[:5]]})
    return {"items": items}


@admin_router.get("/analytics/screens")
async def an_screens(frm: date | None = Query(None, alias="from"), to: date | None = None,
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a, b, _ = _range(frm, to)
    rows = (await db.execute(select(AppEvent.screen, AppEvent.user_id, AppEvent.device_id, AppEvent.session_id).where(
        AppEvent.name == "screen_view", AppEvent.created_at >= a, AppEvent.created_at < b, AppEvent.screen.is_not(None)))).all()
    views: dict[str, int] = defaultdict(int)
    users: dict[str, set] = defaultdict(set)
    for s, u, d, sid in rows:
        views[s] += 1
        users[s].add(_actor(u, d, sid))
    return {"items": [{"screen": s, "views": v, "users": len(users[s])} for s, v in sorted(views.items(), key=lambda x: -x[1])[:100]]}


@admin_router.get("/analytics/retention")
async def an_retention(cohort: str = "week", admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = utcnow() - timedelta(weeks=12)
    rows = (await db.execute(select(AppEvent.user_id, AppEvent.device_id, AppEvent.session_id, func.date(AppEvent.created_at)).where(
        AppEvent.created_at >= a).distinct())).all()
    days: dict[str, set[date]] = defaultdict(set)
    for u, d, sid, day in rows:
        days[_actor(u, d, sid)].add(day if isinstance(day, date) else date.fromisoformat(str(day)))
    monday = lambda d: d - timedelta(days=d.weekday())  # noqa: E731
    cohorts: dict[date, list[set[int]]] = defaultdict(list)
    for ds in days.values():
        start = monday(min(ds))
        cohorts[start].append({(monday(d) - start).days // 7 for d in ds})
    out = []
    for start in sorted(cohorts):
        members = cohorts[start]
        weeks = (monday(date.today()) - start).days // 7 + 1
        out.append({"start": start.isoformat(), "users": len(members),
                    "week": [round(100 * sum(1 for m in members if w in m) / len(members)) for w in range(min(weeks, 8))]})
    return {"cohorts": out[-12:]}


@admin_router.get("/analytics/drivers")
async def an_drivers(frm: date | None = Query(None, alias="from"), to: date | None = None,
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a, b, _ = _range(frm, to)
    secs = 0.0
    for on, off in (await db.execute(select(DriverWorkSession.online_at, DriverWorkSession.offline_at).where(
            DriverWorkSession.online_at >= a, DriverWorkSession.online_at < b))).all():
        secs += ((off or utcnow()) - on).total_seconds()
    offers = (await db.execute(select(OrderOffer.offer_status, OrderOffer.offered_at, OrderOffer.responded_at).where(
        OrderOffer.offered_at >= a, OrderOffer.offered_at < b))).all()
    acc = [o for o in offers if o[0] == OfferStatus.ACCEPTED]
    resp = [(o[2] - o[1]).total_seconds() for o in offers if o[2] and o[0] in (OfferStatus.ACCEPTED, OfferStatus.DECLINED)]
    return {"online_hours": round(secs / 3600, 1), "offers_shown": len(offers),
            "accept_rate": round(100 * len(acc) / len(offers), 1) if offers else 0,
            "avg_response_seconds": round(sum(resp) / len(resp), 1) if resp else 0}


async def cleanup_events(db: AsyncSession) -> None:
    """Daily: drop events older than analytics_retention_days."""
    s = await settings_service.get_all(db)
    key = date.today().isoformat()
    if s["analytics_cleanup_last_run"] == key:
        return
    days = int(s["analytics_retention_days"] or 180)
    await db.execute(delete(AppEvent).where(AppEvent.created_at < utcnow() - timedelta(days=days)))
    await settings_service.set_value(db, "analytics_cleanup_last_run", key, None)


_ = hashlib  # noqa

"""v11: admin live map, SOS, trip share link, extra stops, referrals + wallet, driver demand map, earnings goal."""
import secrets
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload, selectinload

from ..constants import OrderStatus as S
from ..constants import Role
from ..database import get_db
from ..deps import approved_user, customer_user, driver_user
from ..models import (
    DriverProfile,
    DriverServiceArea,
    DriverWorkSession,
    Order,
    OrderStop,
    Rating,
    Referral,
    ServiceArea,
    SosAlert,
    TripShare,
    User,
    Vehicle,
    WalletEntry,
    utcnow,
)
from ..services import cache, settings_service
from ..serializers import order_admin_load_options, order_driver_load_options, order_customer_load_options
from ..services.audit import audit
from ..services.geo import _haversine_m, find_area
from ..services.notifications import notify
from ..services.orders import load_order
from ..services.realtime import hub


def publish_raw(kind: str, payload: dict, user_ids: list[int] | None = None) -> None:
    hub.publish({"type": kind, **payload, "at": utcnow().isoformat()}, set(user_ids or []))


router = APIRouter(prefix="/api", tags=["v11"])
admin_router = APIRouter(prefix="/api/admin", tags=["v11-admin"])
public_router = APIRouter(tags=["v11-public"])


def _admin(user: User = Depends(approved_user)) -> User:
    if user.role not in Role.ADMINS:
        raise HTTPException(403, {"code": "FORBIDDEN"})
    return user


def _aware(d):
    return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)


def _iso(d):
    return _aware(d).isoformat() if d else None


async def _notify_admins(db: AsyncSession, ntype: str, title: str, message: str, order_id: int | None = None) -> None:
    for aid in (await db.execute(select(User.id).where(User.role.in_(list(Role.ADMINS)), User.account_status == "APPROVED"))).scalars().all():
        await notify(db, aid, ntype, title, message, order_id)


# ======================= 1) admin live map =======================
@admin_router.get("/live-map")
async def live_map(service_area_id: int | None = None, include_offline_minutes: int = 0, include_customers: bool = False,
                   customer_active_minutes: int = 30, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    from ..models import Complaint, ProfileChange
    from ..serializers import _file_url
    from ..services.license import doc_status
    now = utcnow()
    open_c: dict[int, int] = {}
    for uid, n in (await db.execute(select(Complaint.user_id, func.count()).where(Complaint.user_id.is_not(None), Complaint.status.not_in(("CLOSED", "RESOLVED"))).group_by(Complaint.user_id))).all():
        open_c[uid] = n
    sos_users = {uid for (uid,) in (await db.execute(select(SosAlert.raised_by_user_id).where(SosAlert.status != "RESOLVED"))).all()}
    renewal = {uid for (uid,) in (await db.execute(select(ProfileChange.user_id).where(ProfileChange.field == "license", ProfileChange.status == "PENDING_REVIEW"))).all()}
    busy = {o.driver_id: o.id for o in (await db.execute(select(Order).where(Order.order_status.in_(S.DRIVER_BUSY)))).scalars().all() if o.driver_id}
    areas_of: dict[int, list[int]] = {}
    for r in (await db.execute(select(DriverServiceArea))).scalars().all():
        areas_of.setdefault(r.driver_id, []).append(r.service_area_id)
    drivers = []
    fresh_locs = await cache.get_locations([d for (d,) in (await db.execute(select(DriverProfile.user_id).where(DriverProfile.is_online.is_(True)))).all()])
    q = select(User).join(DriverProfile, DriverProfile.user_id == User.id).where(User.account_status == "APPROVED").options(
        joinedload(User.vehicle),
        selectinload(User.driver_profile),
    )
    driver_users = (await db.execute(q)).scalars().unique().all()  # loaded once, reused below
    driver_ids = [u.id for u in driver_users]
    # Batch driver rating stats into a single query instead of N+1 stats_for calls
    rating_map = {}
    if driver_ids:
        rating_rows = (await db.execute(
            select(Rating.ratee_id, func.avg(Rating.stars))
            .where(Rating.ratee_id.in_(driver_ids), Rating.rater_role == Role.DRIVER)
            .group_by(Rating.ratee_id)
        )).all()
        rating_map = {rid: round(float(avg), 2) if avg is not None else None for rid, avg in rating_rows}
    for u in driver_users:
        p = u.driver_profile
        if not p.is_online:
            if not (include_offline_minutes and p.last_location_at and _aware(p.last_location_at) >= now - timedelta(minutes=include_offline_minutes)):
                continue
        if service_area_id and service_area_id not in areas_of.get(u.id, []):
            continue
        f = fresh_locs.get(u.id)
        if f:  # newest position from Redis, otherwise the last one PostgreSQL has
            lat, lng, at = f["lat"], f["lng"], datetime.fromtimestamp(f["ts"], UTC)
        else:
            lat, lng, at = p.last_lat, p.last_lng, _aware(p.last_location_at)
        age = round((now - at).total_seconds()) if at else None
        drivers.append({"id": u.id, "name": u.full_name, "phone": u.phone, "mode": p.mode, "busy": u.id in busy, "order_id": busy.get(u.id),
                        "lat": lat, "lng": lng, "location_at": _iso(at), "location_age_s": age,
                        "service_area_ids": areas_of.get(u.id, []),
                        "vehicle": {"plate_number": u.vehicle.plate_number, "vehicle_type": u.vehicle.vehicle_type} if u.vehicle else None,
                        "account_status": u.account_status, "is_online": bool(p.is_online),
                        "license_status": "RENEWAL_PENDING" if u.id in renewal else doc_status(p.license_expiry_date),
                        "open_complaints": open_c.get(u.id, 0), "open_sos": u.id in sos_users,
                        "rating_avg": rating_map.get(u.id), "profile_photo": _file_url(u.profile_photo)})
    open_sos = {a.order_id for a in (await db.execute(select(SosAlert).where(SosAlert.status != "RESOLVED"))).scalars().all()}
    oq = select(Order).where(Order.order_status.not_in(list(S.FINAL))).options(joinedload(Order.customer))
    if service_area_id:
        oq = oq.where(Order.service_area_id == service_area_id)
    orders = []
    searching: dict[int, int] = {}
    active: dict[int, int] = {}
    live_orders = (await db.execute(oq)).scalars().all()
    stops_of: dict[int, list] = {}
    if live_orders:  # one query for all stops instead of one per order
        for st in (await db.execute(select(OrderStop).where(OrderStop.order_id.in_([x.id for x in live_orders])).order_by(OrderStop.seq))).scalars().all():
            stops_of.setdefault(st.order_id, []).append(st)
    for o in live_orders:
        stops = stops_of.get(o.id, [])
        orders.append({"id": o.id, "status": o.order_status, "service_type": o.service_type,
                       "pickup": {"lat": o.pickup_latitude, "lng": o.pickup_longitude, "address": o.pickup_address},
                       "destination": {"lat": o.destination_latitude, "lng": o.destination_longitude, "address": o.destination_address},
                       "stops": [{"id": s.id, "lat": s.lat, "lng": s.lng, "address": s.address, "status": s.status} for s in stops],
                       "driver_id": o.driver_id, "customer_name": o.customer.full_name if o.customer else None, "created_at": _iso(o.created_at),
                       "is_scheduled": o.scheduled_at is not None, "sos_open": o.id in open_sos})
        if o.service_area_id:
            bucket = searching if o.order_status in S.SEARCHING else active if o.order_status in S.DRIVER_BUSY else None
            if bucket is not None:
                bucket[o.service_area_id] = bucket.get(o.service_area_id, 0) + 1
    areas = []
    for a in (await db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True)))).scalars().all():
        in_area = [d for d in drivers if a.id in d["service_area_ids"] and d["mode"] is not None]
        areas.append({"id": a.id, "name": a.name, "boundary": a.boundary, "drivers_online": len(in_area),
                      "drivers_free": len([d for d in in_area if not d["busy"]]),
                      "orders_searching": searching.get(a.id, 0), "orders_active": active.get(a.id, 0)})
    customers = []
    cust_active = 0
    if include_customers:
        window = now - timedelta(minutes=max(1, customer_active_minutes))
        live = (await db.execute(select(Order).where(Order.order_status.in_(list(S.SEARCHING) + list(S.DRIVER_BUSY))))).scalars().all()
        order_of: dict[int, Order] = {}
        for o in live:
            order_of.setdefault(o.customer_id, o)
        ids = set(order_of)
        ids |= {uid for (uid,) in (await db.execute(select(User.id).where(User.role == Role.CUSTOMER, User.last_location_at >= window))).all()}
        for u in (await db.execute(select(User).where(User.id.in_(ids), User.account_status == "APPROVED"))).scalars().unique().all() if ids else []:
            o = order_of.get(u.id)
            fresh = u.last_location_at and _aware(u.last_location_at) >= window
            if fresh:
                lat, lng, at, src = u.last_lat, u.last_lng, _aware(u.last_location_at), "LAST_APP_LOCATION"
            elif o:
                lat, lng, at, src = o.pickup_latitude, o.pickup_longitude, _aware(o.created_at), "ACTIVE_ORDER_PICKUP"
            else:
                continue
            if lat is None or lng is None:
                continue
            state = "IDLE" if not o else ("ORDERING" if o.order_status in S.SEARCHING else "ON_TRIP")
            customers.append({"id": u.id, "name": u.full_name, "phone": u.phone, "profile_photo": _file_url(u.profile_photo),
                              "lat": lat, "lng": lng, "location_at": _iso(at), "location_age_s": round((now - at).total_seconds()),
                              "source": src, "state": state, "order_id": o.id if o else None,
                              "open_complaints": open_c.get(u.id, 0), "open_sos": u.id in sos_users, "account_status": u.account_status})
        cust_active = len(customers)
    summary = {"drivers_online": len([d for d in drivers if d["is_online"]]), "drivers_busy": len([d for d in drivers if d["busy"]]),
               "customers_active": cust_active, "orders_searching": len([o for o in orders if o["status"] in S.SEARCHING]),
               "open_sos": len(open_sos), "open_complaints": sum(open_c.values())}
    out = {"server_time": now.isoformat(), "drivers": drivers, "orders": orders, "areas": areas, "summary": summary}
    if include_customers:
        out["customers"] = customers
    return out


# ======================= 2) SOS =======================
class SosIn(BaseModel):
    lat: float | None = None
    lng: float | None = None
    note: str | None = Field(default=None, max_length=500)


class LocIn(BaseModel):
    lat: float | None = None
    lng: float | None = None


class ResolveIn(BaseModel):
    resolution: str = Field(min_length=2, max_length=500)


async def _sos_order(db: AsyncSession, order_id: int, user: User) -> Order:
    o = await load_order(db, order_id)
    if not o or user.id not in (o.customer_id, o.driver_id):
        raise HTTPException(404, "Order not found")
    return o


@router.post("/orders/{order_id}/sos", status_code=201)
async def raise_sos(order_id: int, body: SosIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    s = await settings_service.get_all(db)
    if s["sos_enabled"] != "true":
        raise HTTPException(409, {"code": "SOS_DISABLED"})
    o = await _sos_order(db, order_id, user)
    recent_done = o.order_status == S.COMPLETED and o.completed_at and utcnow() - _aware(o.completed_at) <= timedelta(minutes=10)
    if o.order_status not in S.DRIVER_BUSY and not recent_done:
        raise HTTPException(409, {"code": "ORDER_NOT_ACTIVE"})
    a = (await db.execute(select(SosAlert).where(SosAlert.order_id == o.id, SosAlert.raised_by_user_id == user.id,
                                                 SosAlert.status != "RESOLVED"))).scalars().first()
    if a:  # one open alert per user per order: repeat calls just refresh the position
        if body.lat is not None:
            a.lat, a.lng = body.lat, body.lng
        await db.commit()
        return {"sos_id": a.id, "status": a.status, "admin_phone": s["support_phone"]}
    a = SosAlert(order_id=o.id, raised_by_user_id=user.id, role=user.role, lat=body.lat, lng=body.lng, note=body.note)
    db.add(a)
    await db.flush()
    await _notify_admins(db, "SOS_ALERT", "تنبيه طوارئ", f"{user.full_name} ({user.role}) رحلة #{o.id}", o.id)
    await db.commit()
    publish_raw("admin.sos", {"sos_id": a.id, "order_id": o.id})
    from ..services import command as _cmd
    _cmd.emit("critical", "sos.activated", f"طوارئ SOS - رحلة #{o.id}: {user.full_name} ({user.role})", body.note,
              {"sos_id": a.id, "order_id": o.id, "user_id": user.id, "role": user.role, "lat": body.lat, "lng": body.lng})
    return {"sos_id": a.id, "status": a.status, "admin_phone": s["support_phone"]}


@router.post("/orders/{order_id}/sos/escalate")
async def escalate_sos(order_id: int, body: LocIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    s = await settings_service.get_all(db)
    await _sos_order(db, order_id, user)
    a = (await db.execute(select(SosAlert).where(SosAlert.order_id == order_id, SosAlert.raised_by_user_id == user.id)
                          .order_by(SosAlert.id.desc()))).scalars().first()
    if not a:
        raise HTTPException(404, {"code": "SOS_NOT_FOUND"})
    if a.status == "RESOLVED":
        raise HTTPException(409, {"code": "SOS_RESOLVED"})
    a.status, a.escalated_at = "ESCALATED", utcnow()
    if body.lat is not None:
        a.lat, a.lng = body.lat, body.lng
    await _notify_admins(db, "SOS_ESCALATED", "تصعيد طوارئ", f"{user.full_name} يتصل بالشرطة - رحلة #{order_id}", order_id)
    await db.commit()
    publish_raw("admin.sos", {"sos_id": a.id, "order_id": order_id, "status": a.status})
    return {"sos_id": a.id, "status": a.status, "emergency_number": s["emergency_number"]}


@admin_router.get("/sos")
async def list_sos(status: str | None = None, limit: int = 50, offset: int = 0, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    q = select(SosAlert)
    if status:
        q = q.where(SosAlert.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(SosAlert.id.desc()).limit(min(limit, 200)).offset(offset))).scalars().all()
    items = []
    for a in rows:
        u, o = await db.get(User, a.raised_by_user_id), await db.get(Order, a.order_id)
        other = await db.get(User, o.driver_id if a.role == "CUSTOMER" else o.customer_id) if o else None
        res = await db.get(User, a.resolved_by) if a.resolved_by else None
        items.append({"id": a.id, "order_id": a.order_id, "role": a.role, "user": {"id": u.id, "name": u.full_name, "phone": u.phone},
                      "other_party": {"name": other.full_name, "phone": other.phone} if other else None,
                      "lat": a.lat, "lng": a.lng, "note": a.note, "status": a.status, "created_at": _iso(a.created_at),
                      "escalated_at": _iso(a.escalated_at), "resolved_by_name": res.full_name if res else None, "resolution": a.resolution})
    return {"total": total, "items": items}


@admin_router.post("/sos/{sos_id}/resolve")
async def resolve_sos(sos_id: int, body: ResolveIn, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    a = await db.get(SosAlert, sos_id)
    if not a:
        raise HTTPException(404, {"code": "SOS_NOT_FOUND"})
    a.status, a.resolved_by, a.resolved_at, a.resolution = "RESOLVED", admin.id, utcnow(), body.resolution
    audit(db, admin.id, "SOS_RESOLVED", "sos", a.id, body.resolution)
    await notify(db, a.raised_by_user_id, "SOS_RESOLVED", "تم التعامل مع بلاغ الطوارئ", body.resolution, a.order_id)
    await db.commit()
    return {"sos_id": a.id, "status": a.status}


# ======================= 3) trip share link =======================
def _share_url(token: str) -> str:
    from ..config import settings
    base = getattr(settings, "public_base_url", None) or "https://phoenixsystems.online/wasaly-api"
    return f"{base.rstrip('/')}/t/{token}"


def _share_expires(o: Order) -> datetime:
    end = _aware(o.completed_at or o.cancelled_at) if (o.completed_at or o.cancelled_at) else None
    return (end + timedelta(hours=2)) if end else utcnow() + timedelta(days=1)


@router.post("/orders/{order_id}/share")
async def create_share(order_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    if (await settings_service.get_all(db))["share_trip_enabled"] != "true":
        raise HTTPException(409, {"code": "SHARE_DISABLED"})
    o = await load_order(db, order_id)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    t = (await db.execute(select(TripShare).where(TripShare.order_id == o.id, TripShare.revoked.is_(False)))).scalars().first()
    if not t:
        t = TripShare(order_id=o.id, token=secrets.token_urlsafe(24))
        db.add(t)
        await db.commit()
    return {"url": _share_url(t.token), "token": t.token, "expires_at": _share_expires(o).isoformat()}


@router.delete("/orders/{order_id}/share")
async def revoke_share(order_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    for t in (await db.execute(select(TripShare).where(TripShare.order_id == o.id))).scalars().all():
        t.revoked = True
    await db.commit()
    return {"revoked": True}


def _base() -> str:
    from ..config import settings
    return (getattr(settings, "public_base_url", None) or "https://phoenixsystems.online/wasaly-api").rstrip("/")


async def _public_trip(db: AsyncSession, token: str) -> dict:
    from ..serializers import _file_url
    from .ratings import stats_for
    t = (await db.execute(select(TripShare).where(TripShare.token == token))).scalars().first()
    if not t or t.revoked:
        raise HTTPException(404, {"code": "SHARE_NOT_FOUND"})
    o = (await db.execute(select(Order).where(Order.id == t.order_id)
                          .options(selectinload(Order.driver).selectinload(User.driver_profile),
                                   selectinload(Order.driver).joinedload(User.vehicle)))).scalar_one_or_none()
    if not o:
        raise HTTPException(404, {"code": "SHARE_NOT_FOUND"})
    if utcnow() > _share_expires(o) and o.order_status in S.FINAL:
        raise HTTPException(410, {"code": "SHARE_EXPIRED"})
    drv, p = o.driver, (o.driver.driver_profile if o.driver else None)
    stops = (await db.execute(select(OrderStop).where(OrderStop.order_id == o.id).order_by(OrderStop.seq))).scalars().all()
    # matched position first (OSRM), then validated raw GPS
    loc = None
    if drv and o.order_status not in S.FINAL:
        m = await cache.get_matched_location(drv.id)
        if m and m.get("lat") is not None and m.get("ts") is not None:
            loc = (m["lat"], m["lng"], datetime.fromtimestamp(float(m["ts"]), UTC))
        elif p and p.last_lat is not None and p.last_lng is not None:
            loc = (p.last_lat, p.last_lng, p.last_location_at)
    eta = None
    if loc and o.destination_latitude is not None and o.order_status in S.DRIVER_BUSY:
        from ..services import map_matching
        live = await map_matching.live_eta(o.id, (loc[0], loc[1]),
                                           (o.destination_latitude, o.destination_longitude))
        if live is not None:
            eta = live["duration_min"]
        else:
            km = _haversine_m(loc[0], loc[1], o.destination_latitude, o.destination_longitude) / 1000
            eta = max(1, round(km * 1.3 / 35 * 60))
    v = drv.vehicle if drv else None
    timeline = [{"key": k, "at": _iso(at)} for k, at in (
        ("ACCEPTED", o.accepted_at), ("DRIVER_ON_THE_WAY", o.driver_on_the_way_at), ("ARRIVED_AT_PICKUP", o.driver_arrived_at),
        ("IN_PROGRESS", o.trip_started_at), ("DELIVERED", o.trip_arrived_at or o.completed_at)) if at]
    rating = (await stats_for(db, drv))["rating_avg"] if drv else None
    # privacy: first name only, no phone numbers, no prices, no customer details
    return {"status": o.order_status, "ended": o.order_status in S.FINAL, "timeline": timeline,
            "service_type": o.service_type, "passengers": o.passenger_count, "parcel_description": o.cargo_description,
            "driver": {"first_name": drv.full_name.split()[0], "photo_url": (_base() + _file_url(drv.profile_photo)) if drv.profile_photo else None,
                       "rating": rating, "vehicle": {"make": v.make, "model": v.model, "color": v.color, "plate_number": v.plate_number} if v else None} if drv else None,
            "vehicle": {"make": v.make, "model": v.model, "color": v.color, "plate_number": v.plate_number} if v else None,
            "driver_location": {"lat": loc[0], "lng": loc[1], "at": _iso(loc[2])} if loc else None,
            "pickup": {"lat": o.pickup_latitude, "lng": o.pickup_longitude, "address": o.pickup_address},
            "destination": {"lat": o.destination_latitude, "lng": o.destination_longitude, "address": o.destination_address},
            "stops": [{"lat": s.lat, "lng": s.lng, "address": s.address, "status": s.status} for s in stops],
            "eta_min": eta, "eta_minutes": eta, "completed_at": _iso(o.completed_at)}


@public_router.get("/api/public/trip/{token}")
@public_router.get("/api/public/trips/{token}")
async def public_trip(token: str, db: AsyncSession = Depends(get_db)):
    return await _public_trip(db, token)


_PAGE = """<!doctype html><html lang="ar" dir="rtl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Wasaly</title><link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<style>
:root{--bg:#f6f7f9;--card:#fff;--tx:#14181f;--mut:#6b7380;--ac:#1652F0;--ok:#16a34a;--bd:#e4e7ec}
@media(prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#171a21;--tx:#eef0f4;--mut:#9aa3b2;--bd:#2a2f3a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);font-family:system-ui,-apple-system,"Segoe UI",Tahoma,sans-serif}
header{display:flex;align-items:center;justify-content:space-between;padding:12px 16px}
.logo{font-weight:800;font-size:20px;color:var(--ac);letter-spacing:.3px}.lang{background:none;border:1px solid var(--bd);color:var(--tx);border-radius:8px;padding:4px 10px;cursor:pointer}
#m{height:42vh;min-height:260px}.wrap{max-width:640px;margin:0 auto;padding:12px 16px 28px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:14px;padding:14px 16px;margin-top:12px}
.st{font-size:18px;font-weight:700}.mut{color:var(--mut);font-size:13px}.eta{color:var(--ac);font-weight:600;margin-top:4px}
.tl{list-style:none;margin:0;padding:0}.tl li{display:flex;gap:10px;align-items:center;padding:6px 0}.dot{width:12px;height:12px;border-radius:50%;background:var(--bd);flex:none}
.tl li.on .dot{background:var(--ok)}.tl li.on{font-weight:600}.tl .t{margin-inline-start:auto;color:var(--mut);font-size:12px}
.drv{display:flex;gap:12px;align-items:center}.ph{width:52px;height:52px;border-radius:50%;background:var(--bd);object-fit:cover;flex:none}
.row{display:flex;gap:8px;margin-top:6px}.row b{min-width:78px;color:var(--mut);font-weight:500}.done{text-align:center;color:var(--ok);font-weight:700;font-size:17px}
footer{text-align:center;color:var(--mut);font-size:12px;padding:6px 0 22px}
</style></head><body>
<header><div class="logo">Wasaly &middot; وصلي</div><button class="lang" id="lg"></button></header>
<div id="m"></div>
<div class="wrap"><div class="card" id="s">...</div><div class="card" id="dv" hidden></div><div class="card"><ul class="tl" id="tl"></ul></div><div class="card" id="rt"></div></div>
<footer>Powered By Phoenix Systems</footer>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script>
const T="__TOKEN__",Q=new URLSearchParams(location.search);
let lang=Q.get('lang')||((navigator.language||'ar').toLowerCase().startsWith('en')?'en':'ar');
const I={ar:{ACCEPTED:'تم قبول الطلب',DRIVER_ON_THE_WAY:'السائق في الطريق',ARRIVED_AT_PICKUP:'السائق وصل إلى نقطة الاستلام',IN_PROGRESS:'الرحلة جارية',DELIVERED:'تم الوصول',
 PENDING:'جارٍ البحث عن سائق',OFFERED:'جارٍ البحث عن سائق',NO_DRIVER_AVAILABLE:'جارٍ البحث عن سائق',ARRIVED_AT_DESTINATION:'وصلت الرحلة إلى الوجهة',COMPLETED:'اكتملت الرحلة',CANCELLED:'أُلغيت الرحلة',EXPIRED:'انتهت الرحلة',
 eta:'الوصول خلال {n} دقيقة تقريبًا',driver:'السائق',car:'السيارة',plate:'اللوحة',from:'من',to:'إلى',stop:'محطة',ride:'توصيل راكب',parcel:'توصيل شحنة',pax:'عدد الركاب',desc:'الشحنة',
 done:'اكتملت الرحلة',at:'وقت الوصول',gone:'انتهت صلاحية رابط الرحلة',pick:'الاستلام',dest:'الوجهة',sw:'English'},
en:{ACCEPTED:'Order accepted',DRIVER_ON_THE_WAY:'Driver on the way',ARRIVED_AT_PICKUP:'Driver arrived at pickup',IN_PROGRESS:'Trip in progress',DELIVERED:'Arrived',
 PENDING:'Looking for a driver',OFFERED:'Looking for a driver',NO_DRIVER_AVAILABLE:'Looking for a driver',ARRIVED_AT_DESTINATION:'Arrived at destination',COMPLETED:'Trip completed',CANCELLED:'Trip cancelled',EXPIRED:'Trip ended',
 eta:'Arriving in about {n} min',driver:'Driver',car:'Car',plate:'Plate',from:'From',to:'To',stop:'Stop',ride:'Ride',parcel:'Parcel delivery',pax:'Passengers',desc:'Parcel',
 done:'Trip completed',at:'Arrival time',gone:'This trip link has expired',pick:'Pickup',dest:'Destination',sw:'العربية'}};
const KEYS=['ACCEPTED','DRIVER_ON_THE_WAY','ARRIVED_AT_PICKUP','IN_PROGRESS','DELIVERED'];
const m=L.map('m').setView([26.22,50.58],11);L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png').addTo(m);
let car,line,pins=[],last=null;
const e=s=>String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const tm=iso=>iso?new Date(iso).toLocaleTimeString(lang==='ar'?'ar-BH':'en-GB',{hour:'2-digit',minute:'2-digit'}):'';
function paint(){const d=last,t=I[lang];document.documentElement.lang=lang;document.documentElement.dir=lang==='ar'?'rtl':'ltr';document.getElementById('lg').textContent=t.sw;if(!d)return;
 let h='<div class="st">'+e(t[d.status]||d.status)+'</div>';if(d.eta_minutes)h+='<div class="eta">'+t.eta.replace('{n}',d.eta_minutes)+'</div>';
 h+='<div class="mut">'+(d.service_type==='DELIVERY'||d.service_type==='PARCEL'?t.parcel:t.ride)+(d.passengers?' · '+t.pax+': '+d.passengers:'')+'</div>';
 if(d.status==='COMPLETED'||d.status==='ARRIVED_AT_DESTINATION')h='<div class="done">'+t.done+'</div>'+(d.completed_at?'<div class="mut" style="text-align:center">'+t.at+': '+tm(d.completed_at)+'</div>':'');
 document.getElementById('s').innerHTML=h;
 const dv=document.getElementById('dv');if(d.driver){const v=d.vehicle||d.driver.vehicle;dv.hidden=false;dv.innerHTML='<div class="drv">'+(d.driver.photo_url?'<img class="ph" src="'+e(d.driver.photo_url)+'">':'<div class="ph"></div>')+'<div><b>'+e(d.driver.first_name)+'</b>'+(d.driver.rating?' ★ '+d.driver.rating:'')+(v?'<div class="mut">'+e(v.make)+' '+e(v.model)+' · '+e(v.color)+'</div><div class="mut">'+t.plate+': '+e(v.plate_number)+'</div>':'')+'</div></div>'}else dv.hidden=true;
 const seen={};(d.timeline||[]).forEach(x=>seen[x.key]=x.at);document.getElementById('tl').innerHTML=KEYS.map(k=>'<li class="'+(seen[k]?'on':'')+'"><span class="dot"></span>'+t[k]+'<span class="t">'+tm(seen[k])+'</span></li>').join('');
 let r='<div class="row"><b>'+t.from+'</b><span>'+e(d.pickup.address)+'</span></div>';(d.stops||[]).forEach(s=>r+='<div class="row"><b>'+t.stop+'</b><span>'+e(s.address)+'</span></div>');r+='<div class="row"><b>'+t.to+'</b><span>'+e(d.destination.address)+'</span></div>';
 if(d.parcel_description)r+='<div class="row"><b>'+t.desc+'</b><span>'+e(d.parcel_description)+'</span></div>';document.getElementById('rt').innerHTML=r}
function draw(d){const pts=[];if(d.pickup.lat!=null)pts.push([d.pickup.lat,d.pickup.lng]);(d.stops||[]).forEach(s=>pts.push([s.lat,s.lng]));if(d.destination.lat!=null)pts.push([d.destination.lat,d.destination.lng]);
 if(!pins.length&&pts.length){pts.forEach((p,i)=>pins.push(L.marker(p).addTo(m)));line=L.polyline(pts,{color:'#1652F0',weight:4,dashArray:'6 8'}).addTo(m);m.fitBounds(pts,{padding:[30,30]})}
 if(d.driver_location){const p=[d.driver_location.lat,d.driver_location.lng];car?car.setLatLng(p):car=L.circleMarker(p,{radius:10,color:'#fff',weight:3,fillColor:'#1652F0',fillOpacity:1}).addTo(m)}else if(car){m.removeLayer(car);car=null}}
async function tick(){try{const r=await fetch('../api/public/trips/'+T);if(r.status>=400){document.getElementById('s').innerHTML='<div class="st">'+I[lang].gone+'</div>';return}
 last=await r.json();draw(last);paint();if(last.ended&&last.status!=='ARRIVED_AT_DESTINATION')return}catch(x){}setTimeout(tick,10000)}
document.getElementById('lg').onclick=()=>{lang=lang==='ar'?'en':'ar';paint()};paint();tick();
</script></body></html>"""


@public_router.get("/t/{token}", response_class=HTMLResponse)
async def public_trip_page(token: str):
    safe = "".join(c for c in token if c.isalnum() or c in "-_")
    return HTMLResponse(_PAGE.replace("__TOKEN__", safe))


# ======================= 4) extra stops =======================
class StopIn(BaseModel):
    address: str = Field(min_length=2, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    position: str | int = "BEFORE_DESTINATION"
    note: str | None = Field(default=None, max_length=300)


def stop_out(s: OrderStop) -> dict:
    return {"id": s.id, "seq": s.seq, "address": s.address, "lat": s.lat, "lng": s.lng, "status": s.status, "note": s.note}


async def _order_with_stops(db: AsyncSession, order_id: int, user: User) -> dict:
    from ..routers.orders import _view
    if user.role in Role.ADMINS:
        o = (await db.execute(select(Order).where(Order.id == order_id).options(*order_admin_load_options()))).scalar_one_or_none()
    elif user.role == Role.DRIVER:
        o = (await db.execute(select(Order).where(Order.id == order_id).options(*order_driver_load_options()))).scalar_one_or_none()
    else:
        o = (await db.execute(select(Order).where(Order.id == order_id).options(*order_customer_load_options()))).scalar_one_or_none()
    if not o:
        raise HTTPException(404, "Order not found")
    d = _view(o, user)
    d["stops"] = [stop_out(s) for s in (await db.execute(select(OrderStop).where(OrderStop.order_id == o.id).order_by(OrderStop.seq))).scalars().all()]
    return d


@router.post("/orders/{order_id}/stops")
async def add_stop(order_id: int, body: StopIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    s = await settings_service.get_all(db)
    o = await load_order(db, order_id, for_update=True)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    if s["stops_enabled"] != "true" or o.order_status not in (S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP, S.IN_PROGRESS):
        raise HTTPException(409, {"code": "ORDER_NOT_ACTIVE"})
    stops = (await db.execute(select(OrderStop).where(OrderStop.order_id == o.id).order_by(OrderStop.seq))).scalars().all()
    if len(stops) >= int(s["max_extra_stops"]):
        raise HTTPException(409, {"code": "MAX_STOPS", "max": int(s["max_extra_stops"])})
    if not await find_area(db, body.lat, body.lng) and (await settings_service.get_pricing(db)).outside_enabled is not True:
        raise HTTPException(422, {"code": "OUTSIDE_SERVICE_AREA"})
    fee = Decimal(s["stop_fee"]).quantize(Decimal(1).scaleb(-o.currency_decimals))
    idx = len(stops) if body.position == "BEFORE_DESTINATION" else max(0, min(int(body.position), len(stops)))
    for st in stops[idx:]:
        st.seq += 1
    db.add(OrderStop(order_id=o.id, seq=idx, address=body.address, lat=body.lat, lng=body.lng, note=body.note, fee=fee))
    o.extras_fee = Decimal(o.extras_fee or 0) + fee  # time-based price keeps running, so the extra distance pays for itself
    o.stops_fee = Decimal(o.stops_fee or 0) + fee
    if o.driver_id:
        await notify(db, o.driver_id, "STOP_ADDED", "تمت إضافة محطة", f"أضاف العميل محطة: {body.address}", o.id)
    await db.commit()
    publish_raw("order.status", {"order_id": o.id}, user_ids=[u for u in (o.customer_id, o.driver_id) if u])
    return await _order_with_stops(db, o.id, user)


@router.delete("/orders/{order_id}/stops/{stop_id}")
async def remove_stop(order_id: int, stop_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id, for_update=True)
    st = await db.get(OrderStop, stop_id)
    if not o or o.customer_id != user.id or not st or st.order_id != o.id:
        raise HTTPException(404, "Stop not found")
    if st.status != "PENDING":
        raise HTTPException(409, {"code": "STOP_NOT_PENDING"})
    o.extras_fee = max(Decimal(0), Decimal(o.extras_fee or 0) - Decimal(st.fee or 0))
    o.stops_fee = max(Decimal(0), Decimal(o.stops_fee or 0) - Decimal(st.fee or 0))
    await db.delete(st)
    await db.commit()
    return await _order_with_stops(db, o.id, user)


@router.get("/orders/{order_id}/stops")
async def list_stops(order_id: int, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id)
    if not o or (user.id not in (o.customer_id, o.driver_id) and user.role not in Role.ADMINS):
        raise HTTPException(404, "Order not found")
    return {"items": [stop_out(s) for s in (await db.execute(select(OrderStop).where(OrderStop.order_id == o.id).order_by(OrderStop.seq))).scalars().all()]}


async def _driver_stop(db: AsyncSession, order_id: int, stop_id: int, user: User, new: str) -> dict:
    o = await load_order(db, order_id)
    st = await db.get(OrderStop, stop_id)
    if not o or o.driver_id != user.id or not st or st.order_id != o.id:
        raise HTTPException(404, "Stop not found")
    st.status = new
    await db.commit()
    return stop_out(st)


@router.post("/driver/orders/{order_id}/stops/{stop_id}/arrived")
async def stop_arrived(order_id: int, stop_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    return await _driver_stop(db, order_id, stop_id, user, "ARRIVED")


@router.post("/driver/orders/{order_id}/stops/{stop_id}/done")
async def stop_done(order_id: int, stop_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    return await _driver_stop(db, order_id, stop_id, user, "DONE")


# ======================= driver cancel preview =======================
@router.post("/driver/orders/{order_id}/cancel/preview")
async def driver_cancel_preview(order_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id)
    if not o or o.driver_id != user.id:
        raise HTTPException(404, "Order not found")
    return {"fee": f"{Decimal((await settings_service.get_all(db))['cancel_fee_driver_amount']):.{o.currency_decimals}f}", "currency": o.currency}


# ======================= scheduled: reschedule =======================
class RescheduleIn(BaseModel):
    scheduled_at: datetime


@router.patch("/orders/{order_id}/schedule")
async def reschedule(order_id: int, body: RescheduleIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    from ..routers.orders import _check_schedule, order_for_customer
    o = await load_order(db, order_id, for_update=True)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    if o.order_status != S.SCHEDULED:
        raise HTTPException(409, {"code": "NOT_SCHEDULED"})
    _check_schedule(await settings_service.get_all(db), body.scheduled_at)
    o.scheduled_at = _aware(body.scheduled_at)
    await db.commit()
    o = (await db.execute(select(Order).where(Order.id == o.id).options(*order_customer_load_options()))).scalar_one()
    return order_for_customer(o)


# ======================= 7) referrals + wallet =======================
def make_referral_code(name: str) -> str:
    base = "".join(c for c in name.upper() if c.isascii() and c.isalpha())[:5] or "WSL"
    return base + secrets.token_hex(2).upper()


async def ensure_code(db: AsyncSession, u: User) -> str:
    if not u.referral_code:
        u.referral_code = make_referral_code(u.full_name)
        await db.flush()
    return u.referral_code


async def apply_referral(db: AsyncSession, invitee: User, code: str | None, device_id: str | None) -> None:
    """Called at registration. Raises 4xx for an invalid / abusive code."""
    if not code:
        return
    s = await settings_service.get_all(db)
    if s["referral_enabled"] != "true":
        return
    inviter = (await db.execute(select(User).where(User.referral_code == code.strip().upper()))).scalars().first()
    if not inviter or inviter.role != Role.CUSTOMER:
        raise HTTPException(422, {"code": "REFERRAL_INVALID"})
    if inviter.id == invitee.id or (device_id and (await db.execute(select(Referral.id).where(Referral.device_id == device_id))).first()):
        raise HTTPException(422, {"code": "REFERRAL_SELF"})
    n = (await db.execute(select(func.count()).select_from(Referral).where(Referral.inviter_id == inviter.id, Referral.status != "REVOKED"))).scalar_one()
    if n >= int(s["referral_max_per_user"]):
        raise HTTPException(422, {"code": "REFERRAL_LIMIT"})
    db.add(Referral(inviter_id=inviter.id, invitee_id=invitee.id, device_id=device_id))


async def reward_referral(db: AsyncSession, customer_id: int, order: Order | None = None) -> None:
    """Called when a customer's trip payment is confirmed: pays both sides once, after the configured number of paid trips."""
    r = (await db.execute(select(Referral).where(Referral.invitee_id == customer_id, Referral.status == "REGISTERED"))).scalars().first()
    if not r:
        return
    s = await settings_service.get_all(db)
    paid = (await db.execute(select(func.count()).select_from(Order).where(Order.customer_id == customer_id, Order.payment_status == "PAID"))).scalar_one()
    if paid < int(s["referral_min_trip_to_reward"]):
        return
    ri, rv = Decimal(s["referral_reward_inviter"]), Decimal(s["referral_reward_invitee"])
    if order is not None and order.referral_id == r.id:
        rv = Decimal(0)  # the invitee already got his reward as a discount on this trip
    # Batch load users for wallet credit (avoids N+1 db.get)
    reward_users = {}
    for uid, amt, kind in ((r.inviter_id, ri, "REFERRAL_INVITER"), (customer_id, rv, "REFERRAL_INVITEE")):
        if amt <= 0:
            continue
        if uid not in reward_users:
            reward_users[uid] = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
        u = reward_users[uid]
        if u:
            u.wallet_balance = Decimal(u.wallet_balance or 0) + amt
            db.add(WalletEntry(user_id=uid, amount=amt, kind=kind, ref_id=r.id))
            await notify(db, uid, "REFERRAL_REWARD", "مكافأة إحالة", f"تمت إضافة {amt} إلى رصيدك.")
    r.status, r.reward_inviter, r.reward_invitee, r.rewarded_at = "REWARDED", ri, rv, utcnow()


@router.get("/referral")
async def my_referral(user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    s = await settings_service.get_all(db)
    code = await ensure_code(db, user)
    rows = (await db.execute(select(Referral).where(Referral.inviter_id == user.id).order_by(Referral.id.desc()))).scalars().all()
    # Batch load invitees (avoids N+1 db.get)
    invitee_ids = [r.invitee_id for r in rows]
    invitee_map = {}
    if invitee_ids:
        inv_rows = (await db.execute(select(User).where(User.id.in_(invitee_ids)))).scalars().all()
        invitee_map = {u.id: u for u in inv_rows}
    pending = []
    for r in rows:
        inv = invitee_map.get(r.invitee_id)
        pending.append({"name": (inv.full_name[:1] + "***") if inv else "***", "status": "REWARDED" if r.status == "REWARDED" else "REGISTERED"})
    await db.commit()
    amt = s["referral_reward_invitee"]
    return {"code": code, "share_text": f"استخدم كودي {code} في وصلي واحصل على خصم {amt} BHD",
            "share_url": _share_url(code).replace("/t/", "/r/"), "invited_count": len(rows),
            "rewarded_count": len([r for r in rows if r.status == "REWARDED"]),
            "earned_total": f"{sum((r.reward_inviter for r in rows if r.status == 'REWARDED'), Decimal(0)):.3f}", "pending": pending}


@router.get("/wallet")
async def my_wallet(limit: int = 20, offset: int = 0, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(WalletEntry).where(WalletEntry.user_id == user.id).order_by(WalletEntry.id.desc())
                             .limit(max(1, min(limit, 100))).offset(max(0, offset)))).scalars().all()
    return {"balance": f"{Decimal(user.wallet_balance or 0):.3f}",
            "items": [{"id": w.id, "amount": f"{w.amount:.3f}", "kind": w.kind, "note": w.note, "created_at": _iso(w.created_at)} for w in rows]}


@admin_router.get("/referrals")
async def admin_referrals(status: str | None = None, limit: int = 50, offset: int = 0, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    q = select(Referral)
    if status:
        q = q.where(Referral.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Referral.id.desc()).limit(min(limit, 200)).offset(offset))).scalars().all()
    # Batch load users (avoids N+1 db.get)
    user_ids = {r.inviter_id for r in rows} | {r.invitee_id for r in rows}
    user_map = {}
    if user_ids:
        u_rows = (await db.execute(select(User).where(User.id.in_(user_ids)))).scalars().all()
        user_map = {u.id: u for u in u_rows}
    items = []
    for r in rows:
        a, b = user_map.get(r.inviter_id), user_map.get(r.invitee_id)
        items.append({"id": r.id, "inviter": {"id": a.id, "name": a.full_name} if a else None, "invitee": {"id": b.id, "name": b.full_name} if b else None, "status": r.status,
                      "reward_inviter": str(r.reward_inviter), "reward_invitee": str(r.reward_invitee), "created_at": _iso(r.created_at)})
    return {"total": total, "items": items}


@admin_router.post("/referrals/{rid}/revoke")
async def revoke_referral(rid: int, admin: User = Depends(_admin), db: AsyncSession = Depends(get_db)):
    r = await db.get(Referral, rid)
    if not r:
        raise HTTPException(404, "Referral not found")
    if r.status == "REWARDED":  # take the credit back
        # Batch load users for wallet debit (avoids N+1 db.get)
        for uid, amt in ((r.inviter_id, r.reward_inviter), (r.invitee_id, r.reward_invitee)):
            if amt <= 0:
                continue
            u = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
            if u:
                u.wallet_balance = max(Decimal(0), Decimal(u.wallet_balance or 0) - Decimal(amt))
                db.add(WalletEntry(user_id=uid, amount=-Decimal(amt), kind="REVOKED", ref_id=r.id))
    r.status = "REVOKED"
    audit(db, admin.id, "REFERRAL_REVOKED", "referral", r.id)
    await db.commit()
    return {"id": r.id, "status": r.status}


@public_router.get("/r/{code}", response_class=HTMLResponse)
async def referral_landing(code: str, db: AsyncSession = Depends(get_db)):
    """Invite link: tries to open the app straight away (Android intent / iOS scheme) with the code; the page shows the code and buttons as a fallback."""
    from urllib.parse import quote
    safe = "".join(c for c in code if c.isalnum())[:20]
    s = await settings_service.get_all(db)
    scheme_url = f"wasaly://invite?code={safe}"
    store = s.get("android_store_url") or ""
    fallback = quote(store, safe="") if store else ""
    intent = (f"intent://invite?code={safe}#Intent;scheme=wasaly;package=com.phoenixsystems.wasaly;"
              + (f"S.browser_fallback_url={fallback};" if fallback else "") + "end")
    stores = "".join(f'<p><a href="{u}" style="color:#0a7">{t}</a></p>' for u, t in ((s.get("android_store_url"), "تحميل تطبيق Android"), (s.get("ios_store_url"), "تحميل تطبيق iPhone")) if u)
    html = (f'<!doctype html><html lang="ar" dir="rtl"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>وصلي</title><body style="font-family:system-ui;text-align:center;padding:40px"><h2>وصلي</h2>'
            f'<p>حمّل التطبيق واستخدم كود الدعوة:</p><h1 style="letter-spacing:3px">{safe}</h1>'
            f'<p><a id="open" href="{scheme_url}" style="display:inline-block;padding:12px 28px;background:#0a7;color:#fff;border-radius:10px;text-decoration:none">فتح التطبيق</a></p>{stores}'
            f'<script>(function(){{var ua=navigator.userAgent||"";var a=document.getElementById("open");'
            f'if(/android/i.test(ua)){{a.href="{intent}";}}try{{window.location.href=a.href;}}catch(e){{}}}})();</script></body></html>')
    return HTMLResponse(html)


# ======================= nearby drivers (customer map) =======================
@router.get("/nearby-drivers")
async def nearby_drivers(lat: float, lng: float, radius_km: float = 5, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Free, online LIVE drivers with a fresh position around a point. No phone / plate / full name; positions rounded to ~10 m."""
    from ..routers.ratings import stats_for
    from ..security import create_file_token
    s = await settings_service.get_all(db)
    if s["nearby_drivers_enabled"] != "true":
        return {"items": []}
    radius_km = max(0.5, min(radius_km, 10))
    now = utcnow()
    max_age = int(s["live_location_max_age_seconds"])
    profs = (await db.execute(select(DriverProfile).where(DriverProfile.is_online.is_(True), DriverProfile.mode == "LIVE"))).scalars().all()
    if not profs:
        return {"items": []}
    busy = {d for (d,) in (await db.execute(select(Order.driver_id).where(Order.order_status.in_(list(S.DRIVER_BUSY)), Order.driver_id.is_not(None)))).all()}
    fresh = await cache.get_locations([p.user_id for p in profs])
    cands = []
    for p in profs:
        if p.user_id in busy:
            continue
        f = fresh.get(p.user_id)
        if f:
            la, lo, age, hd = f["lat"], f["lng"], now.timestamp() - f["ts"], f["heading"]
        elif p.last_lat is not None and p.last_location_at:
            la, lo, age, hd = p.last_lat, p.last_lng, (now - _aware(p.last_location_at)).total_seconds(), p.last_heading
        else:
            continue
        if age > max_age:
            continue
        km = _haversine_m(lat, lng, la, lo) / 1000
        if km <= radius_km:
            cands.append((km, p.user_id, la, lo, hd))
    items = []
    cand_ids = [uid for _km, uid, la, lo, hd in sorted(cands)[:20]]
    if cand_ids:
        # Batch load users with vehicle and driver_profile (avoids N+1 db.get + lazy loads)
        cand_users = (await db.execute(
            select(User).where(User.id.in_(cand_ids))
            .options(joinedload(User.vehicle), selectinload(User.driver_profile))
        )).scalars().all()
        cand_map = {u.id: u for u in cand_users}
        # Batch rating stats
        rating_rows = (await db.execute(
            select(Rating.ratee_id, func.avg(Rating.stars))
            .where(Rating.ratee_id.in_(cand_ids), Rating.rater_role == Role.DRIVER)
            .group_by(Rating.ratee_id)
        )).all()
        rating_map = {rid: round(float(avg), 2) if avg is not None else None for rid, avg in rating_rows}
    else:
        cand_map = {}
        rating_map = {}
    for _km, uid, la, lo, hd in sorted(cands)[:20]:
        u = cand_map.get(uid)
        if not u or u.account_status != "APPROVED" or u.deleted_at or not u.vehicle:
            continue
        items.append({"id": u.id, "first_name": u.full_name.split()[0],
                      "photo_url": f"/api/files/download/{create_file_token(u.profile_photo)}" if u.profile_photo else None,
                      "lat": round(la, 4), "lng": round(lo, 4), "heading": hd, "vehicle_type": u.vehicle.vehicle_type, "rating": rating_map.get(uid)})
    return {"items": items}


# ======================= 8) driver demand map =======================
_demand_cache: dict = {"at": None, "data": None}


@router.get("/driver/demand")
async def driver_demand(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    now = utcnow()
    if _demand_cache["at"] and (now - _demand_cache["at"]).total_seconds() < 60:
        return _demand_cache["data"]
    since = now - timedelta(minutes=30)
    pts = (await db.execute(select(Order.pickup_latitude, Order.pickup_longitude).where(
        Order.pickup_latitude.is_not(None), (Order.created_at >= since) | Order.order_status.in_(list(S.SEARCHING))))).all()
    free = [p for p in (await db.execute(select(DriverProfile).where(DriverProfile.is_online.is_(True), DriverProfile.last_lat.is_not(None)))).scalars().all()]
    busy_ids = {o for (o,) in (await db.execute(select(Order.driver_id).where(Order.order_status.in_(list(S.DRIVER_BUSY)), Order.driver_id.is_not(None)))).all()}
    free = [p for p in free if p.user_id not in busy_ids]
    step = 0.007  # ~800 m
    cells: dict[tuple[int, int], int] = {}
    for la, lo in pts:
        k = (round(la / step), round(lo / step))
        cells[k] = cells.get(k, 0) + 1
    out = []
    for (i, j), n in sorted(cells.items(), key=lambda x: -x[1]):
        lat, lng = i * step, j * step
        nfree = len([p for p in free if _haversine_m(p.last_lat, p.last_lng, lat, lng) <= 800])
        level = "HIGH" if n >= 5 and nfree < n else "MEDIUM" if n >= 3 else "LOW"
        out.append({"lat": round(lat, 4), "lng": round(lng, 4), "radius_m": 800, "level": level, "orders_last_30m": n, "drivers_free": nfree})
    searching: dict[int, int] = {}
    for (aid,) in (await db.execute(select(Order.service_area_id).where(Order.order_status.in_(list(S.SEARCHING)), Order.service_area_id.is_not(None)))).all():
        searching[aid] = searching.get(aid, 0) + 1
    # Batch load all driver-service-area mappings (avoids N+1 per area)
    area_driver_rows = (await db.execute(select(DriverServiceArea.service_area_id, DriverServiceArea.driver_id))).all()
    area_drivers: dict[int, set[int]] = {}
    for aid, did in area_driver_rows:
        area_drivers.setdefault(aid, set()).add(did)
    free_ids = {p.user_id for p in free}
    areas = []
    for a in (await db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True)))).scalars().all():
        ids = area_drivers.get(a.id, set())
        nfree = len(ids & free_ids)
        ns = searching.get(a.id, 0)
        areas.append({"id": a.id, "name": a.name, "level": "HIGH" if ns > nfree and ns >= 2 else "MEDIUM" if ns > 0 else "LOW",
                      "orders_searching": ns, "drivers_free": nfree})
    data = {"generated_at": now.isoformat(), "cells": out[:60], "areas": areas}
    _demand_cache.update(at=now, data=data)
    return data


# ======================= 9) earnings summary + goal =======================
class GoalIn(BaseModel):
    daily_amount: Decimal = Field(ge=0, le=100000)


@router.get("/driver/earnings/summary")
async def earnings_summary(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    from zoneinfo import ZoneInfo
    tz = ZoneInfo("Asia/Bahrain")
    today = datetime.now(tz).date()
    start_week = today - timedelta(days=6)
    start_utc = datetime.combine(start_week, datetime.min.time(), tz).astimezone(UTC)
    rows = (await db.execute(select(Order).where(Order.driver_id == user.id, Order.order_status == S.COMPLETED,
                                                 Order.completed_at >= start_utc))).scalars().all()
    by_day: dict[date, list] = {}
    for o in rows:
        by_day.setdefault(_aware(o.completed_at).astimezone(tz).date(), []).append(o)
    cur, dec = "BHD", 3
    if rows:
        cur, dec = rows[0].currency, rows[0].currency_decimals

    def tot(ds):
        return sum((Decimal(o.driver_earning or 0) for d in ds for o in by_day.get(d, [])), Decimal(0))

    def cnt(ds):
        return sum(len(by_day.get(d, [])) for d in ds)

    days = [start_week + timedelta(days=i) for i in range(7)]

    async def minutes(since_utc):
        ws = (await db.execute(select(DriverWorkSession).where(DriverWorkSession.driver_id == user.id,
                                                               (DriverWorkSession.offline_at.is_(None)) | (DriverWorkSession.offline_at >= since_utc)))).scalars().all()
        n = utcnow()
        return round(sum(max(0, ((_aware(w.offline_at) or n) - max(_aware(w.online_at), since_utc)).total_seconds()) for w in ws) / 60)

    day_start = datetime.combine(today, datetime.min.time(), tz).astimezone(UTC)
    goal = Decimal(user.driver_profile.daily_goal or 0)
    te = tot([today])
    return {"currency": cur,
            "today": {"earnings": f"{te:.{dec}f}", "trips": cnt([today]), "online_minutes": await minutes(day_start)},
            "week": {"earnings": f"{tot(days):.{dec}f}", "trips": cnt(days), "online_minutes": await minutes(start_utc),
                     "by_day": [{"date": d.isoformat(), "earnings": f"{tot([d]):.{dec}f}", "trips": cnt([d])} for d in days]},
            "goal": {"daily_amount": f"{goal:.{dec}f}", "progress_percent": min(100, round(te / goal * 100)) if goal > 0 else 0}}


@router.put("/driver/goal")
async def set_goal(body: GoalIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    user.driver_profile.daily_goal = body.daily_amount
    await db.commit()
    return {"daily_amount": f"{body.daily_amount:.3f}"}

"""App start-up report (device info), location consent + snapshots, and the admin activity view."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..constants import OrderStatus as S
from ..constants import Role
from ..database import get_db
from ..deps import admin_user, current_user
from ..models import AppOpenEvent, DeviceToken, DriverWorkSession, LocationSnapshot, LoginEvent, Order, OrderOffer, User, UserDevice, utcnow
from ..schemas import AppOpenIn, LocationConsentIn, LocationSnapshotIn
from ..serializers import dt
from ..services import settings_service
from ..services.activity import is_online
from ..services.audit import audit

router = APIRouter(prefix="/api", tags=["activity"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-activity"])


def _ip(request: Request):
    return request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None)


@router.post("/app/open")
async def app_open(body: AppOpenIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Call once every time the app is opened (and the user is logged in). Counts usage and records the device."""
    now = utcnow()
    dev = (await db.execute(select(UserDevice).where(UserDevice.user_id == user.id, UserDevice.device_id == body.device_id))).scalar_one_or_none()
    if not dev:
        dev = UserDevice(user_id=user.id, device_id=body.device_id, open_count=0)
        db.add(dev)
    dev.platform, dev.model, dev.os_name, dev.os_version = body.platform, body.model, body.os_name, body.os_version
    dev.app_version, dev.locale, dev.last_ip, dev.last_seen_at = body.app_version, body.locale, _ip(request), now
    dev.open_count = (dev.open_count or 0) + 1
    user.app_open_count = (user.app_open_count or 0) + 1
    db.add(AppOpenEvent(user_id=user.id, device_id=body.device_id, app_version=body.app_version))
    user.last_seen_at = now
    if body.fcm_token:
        row = (await db.execute(select(DeviceToken).where(DeviceToken.token == body.fcm_token))).scalar_one_or_none()
        if row:
            row.user_id, row.platform = user.id, body.platform
        else:
            db.add(DeviceToken(user_id=user.id, token=body.fcm_token, platform=body.platform))
    await db.commit()
    return {"ok": True, "server_time": dt(now)}


@router.post("/privacy/location-consent")
async def location_consent(body: LocationConsentIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """The app must show its own explanation screen first. Withdrawing consent also deletes the stored snapshots."""
    user.location_consent, user.location_consent_at = body.granted, utcnow()
    if not body.granted:
        from sqlalchemy import delete
        await db.execute(delete(LocationSnapshot).where(LocationSnapshot.user_id == user.id))
    await db.commit()
    return {"location_consent": user.location_consent}


@router.post("/location/snapshot", status_code=201)
async def location_snapshot(body: LocationSnapshotIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """One position at an important moment (not tracking). Needs consent and the admin master switch."""
    s = await settings_service.get_all(db)
    if s["location_snapshots_enabled"] != "true":
        raise HTTPException(409, {"code": "LOCATION_DISABLED"})
    if not user.location_consent:
        raise HTTPException(403, {"code": "LOCATION_CONSENT_REQUIRED"})
    if body.order_id is not None:
        o = await db.get(Order, body.order_id)
        if not o or user.id not in (o.customer_id, o.driver_id):
            raise HTTPException(404, "Order not found")
    db.add(LocationSnapshot(user_id=user.id, event=body.event, latitude=body.latitude, longitude=body.longitude,
                            accuracy_m=body.accuracy_m, order_id=body.order_id))
    await db.commit()
    return {"ok": True}


@admin_router.get("/users/{user_id}/activity")
async def user_activity(user_id: int, include_location: bool = False, admin: User = Depends(admin_user),
                        db: AsyncSession = Depends(get_db)):
    """Everything useful about one user's usage. Location is returned only with include_location=true and is audited."""
    u = (await db.execute(select(User).where(User.id == user_id).options(selectinload(User.driver_profile)))).scalar_one_or_none()
    if not u or u.role not in (Role.CUSTOMER, Role.DRIVER):
        raise HTTPException(404, "User not found")
    s = await settings_service.get_all(db)
    window = int(s["online_window_minutes"])
    now = utcnow()
    devices = (await db.execute(select(UserDevice).where(UserDevice.user_id == u.id).order_by(UserDevice.last_seen_at.desc()))).scalars().all()
    logins = (await db.execute(select(LoginEvent).where(LoginEvent.user_id == u.id).order_by(LoginEvent.id.desc()).limit(30))).scalars().all()
    tokens = (await db.execute(select(func.count()).select_from(DeviceToken).where(DeviceToken.user_id == u.id))).scalar_one()
    out = {
        "user_id": u.id, "role": u.role, "full_name": u.full_name, "created_at": dt(u.created_at),
        "online": is_online(u.last_seen_at, window, now), "online_window_minutes": window,
        "last_seen_at": dt(u.last_seen_at), "last_login_at": dt(u.last_login_at), "last_login_ip": u.last_login_ip,
        "login_count": u.login_count or 0, "app_open_count": u.app_open_count or 0,
        "push_enabled": tokens > 0, "location_consent": u.location_consent, "location_consent_at": dt(u.location_consent_at),
        "devices": [{"device_id": d.device_id, "platform": d.platform, "model": d.model, "os": f"{d.os_name or ''} {d.os_version or ''}".strip(),
                     "app_version": d.app_version, "locale": d.locale, "last_ip": d.last_ip, "open_count": d.open_count,
                     "first_seen_at": dt(d.first_seen_at), "last_seen_at": dt(d.last_seen_at)} for d in devices],
        "recent_logins": [{"at": dt(e.created_at), "success": e.success, "ip": e.ip, "device_id": e.device_id,
                           "new_device": e.new_device, "user_agent": e.user_agent} for e in logins],
    }
    opens = (await db.execute(select(AppOpenEvent).where(AppOpenEvent.user_id == u.id).order_by(AppOpenEvent.id.desc()).limit(30))).scalars().all()
    opens30 = (await db.execute(select(func.count()).select_from(AppOpenEvent).where(
        AppOpenEvent.user_id == u.id, AppOpenEvent.created_at >= now - timedelta(days=30)))).scalar_one()
    out["is_online"] = out["online"]
    # Batch order stats into a single grouped query instead of 3 separate count queries
    order_stats = dict((await db.execute(
        select(Order.order_status, func.count())
        .where((Order.customer_id == u.id) if u.role == Role.CUSTOMER else (Order.driver_id == u.id))
        .group_by(Order.order_status)
    )).all())
    out["stats"] = {
        "app_opens": u.app_open_count or 0, "app_opens_30d": opens30, "logins": u.login_count or 0,
        "orders_total": sum(order_stats.values()),
        "completed_orders": order_stats.get(S.COMPLETED, 0),
        "cancelled_orders": order_stats.get(S.CANCELLED, 0),
        "devices": len(devices)}
    out["logins"] = out["recent_logins"]
    out["app_opens"] = [{"at": dt(e.created_at), "device_id": e.device_id, "app_version": e.app_version} for e in opens]
    out["locations"] = []
    if u.role == Role.CUSTOMER:
        # Batch customer order stats into a single grouped query
        cust_stats = dict((await db.execute(
            select(Order.order_status, Order.payment_status, func.count())
            .where(Order.customer_id == u.id)
            .group_by(Order.order_status, Order.payment_status)
        )).all())
        total = sum(v for (st, _), v in cust_stats.items())
        completed = sum(v for (st, ps), v in cust_stats.items() if st == S.COMPLETED)
        cancelled = sum(v for (st, _), v in cust_stats.items() if st == S.CANCELLED)
        unpaid = sum(v for (st, ps), v in cust_stats.items() if st == S.COMPLETED and ps != "PAID")
        out["customer_stats"] = {
            "orders_total": total,
            "completed": completed,
            "cancelled": cancelled,
            "unpaid_completed": unpaid}
    else:
        offers = dict((await db.execute(select(OrderOffer.offer_status, func.count()).where(OrderOffer.driver_id == u.id)
                                        .group_by(OrderOffer.offer_status))).all())
        sessions = (await db.execute(select(DriverWorkSession).where(DriverWorkSession.driver_id == u.id))).scalars().all()
        secs = sum(int(((x.offline_at or now) - x.online_at).total_seconds()) for x in sessions)
        total_offers = sum(offers.values())
        accepted = offers.get("ACCEPTED", 0)
        out["driver_stats"] = {
            "receiving_offers_now": bool(u.driver_profile and u.driver_profile.is_online),
            "offers_by_status": offers, "offers_total": total_offers,
            "acceptance_rate_percent": round(accepted * 100 / total_offers, 1) if total_offers else None,
            "online_hours_total": round(secs / 3600, 2), "work_sessions": len(sessions),
            "trips_completed": order_stats.get(S.COMPLETED, 0)}
    if include_location:
        snaps = (await db.execute(select(LocationSnapshot).where(LocationSnapshot.user_id == u.id)
                                  .order_by(LocationSnapshot.id.desc()).limit(50))).scalars().all()
        out["location_snapshots"] = [{"event": x.event, "latitude": x.latitude, "longitude": x.longitude, "accuracy_m": x.accuracy_m,
                                      "order_id": x.order_id, "at": dt(x.created_at)} for x in snaps]
        out["locations"] = out["location_snapshots"]
        audit(db, admin.id, "VIEW_USER_LOCATION", "user", u.id)
        await db.commit()
    return out

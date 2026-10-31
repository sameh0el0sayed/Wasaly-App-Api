import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import CURRENCIES, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import approved_user, customer_user, paging
from ..models import Complaint, Order, OrderStatusHistory, User, utcnow
from ..schemas import EstimateIn, OptionalReasonIn, OrderCreateIn, ReasonIn
from ..serializers import history_out, order_for_admin, order_for_customer, order_for_driver
from ..services import settings_service
from ..services.dispatch import dispatch_order
from ..services.estimate import money_estimate, route_guess
from ..services.extras import compute_extras, resolve_prefs
from ..services.files import assert_owned
from ..services.geo import _haversine_m, find_area, point_in_boundary
from ..services.notifications import notify
from ..services.orders import close_pending_offers, load_order, transition

router = APIRouter(prefix="/api", tags=["orders"])


def _categories(raw: str) -> list[dict]:
    try:
        items = json.loads(raw)
    except ValueError:
        return []
    return [c for c in items if c.get("is_active", True)]


@router.get("/app-config")
async def app_config(db: AsyncSession = Depends(get_db)):
    """Public (no login). The app calls this at start-up: maintenance, forced update, support contacts, feature switches."""
    s = await settings_service.get_all(db)
    p = await settings_service.get_pricing(db)
    return {"maintenance": s["maintenance_mode"] == "true", "maintenance_message": s["maintenance_message"],
            "min_app_version": s["min_app_version"], "latest_app_version": s["latest_app_version"], "update_message": s["update_message"],
            "support": {"phone": s["support_phone"], "whatsapp": s["support_whatsapp"], "email": s["support_email"]},
            "offer_timeout_seconds": int(s["offer_ttl_seconds"]),
            "ratings_enabled": s["ratings_enabled"] == "true",
            "driver_rates_customer_enabled": s["driver_rates_customer_enabled"] == "true",
            "live_mode_enabled": s["live_mode_enabled"] == "true",
            "live_location_interval_seconds": int(s["live_location_interval_seconds"]),
            "require_pickup_photo": s["require_pickup_photo"] == "true", "require_delivery_photo": s["require_delivery_photo"] == "true",
            "proof_photos_apply_to": s["proof_photos_apply_to"],
            "preference_fees": {k: s[f"pref_fee_{k}"] for k in ("ac", "smoking_allowed", "non_smoking", "female_driver", "male_driver", "large_car")},
            "preference_defaults": {"ac": s["default_pref_ac"] == "true", "smoking": s["default_pref_smoking"],
                                    "driver_gender": s["default_pref_driver_gender"], "large_car": s["default_pref_large_car"] == "true"},
            "large_car_vehicle_types": [t.strip() for t in s["large_car_vehicle_types"].split(",") if t.strip()],
            "location_snapshots_enabled": s["location_snapshots_enabled"] == "true",
            "customer_live_tracking_enabled": s["customer_live_tracking_enabled"] == "true",
            "service_categories": _categories(s["service_categories"]),
            "online_window_minutes": int(s["online_window_minutes"]),
            "currency": p.currency, "currency_decimals": p.decimals, "upload_max_mb": int(s["upload_max_mb"]),
            "payment_methods": [m for m, k in (("TAP", "tap_enabled"), ("BENEFITPAY", "benefitpay_enabled")) if s[k] == "true"],
            "cancel_after_accept_allowed": s["cancel_after_accept_allowed"] == "true",
            "server_time": utcnow().isoformat()}


@router.get("/currencies")
async def currencies():
    return [{"code": c, "decimals": d} for c, d in CURRENCIES.items()]


@router.get("/pricing")
async def pricing(db: AsyncSession = Depends(get_db)):
    p = await settings_service.get_pricing(db)
    return {"currency": p.currency, "currency_decimals": p.decimals, "unit_price": f"{p.unit_price:.{p.decimals}f}",
            "unit_minutes": p.unit_seconds // 60, "unit_seconds": p.unit_seconds,
            "min_price": f"{p.min_price:.{p.decimals}f}", "rounding": "UP",
            "outside_bahrain": {"enabled": p.outside_enabled, "driver_percent": str(p.outside_driver_percent)},
            "night_surcharge": {"enabled": p.night_enabled, "percent": str(p.night_percent), "from": p.night_start, "to": p.night_end,
                                "applies_to": "the whole trip price when the trip starts inside the window"},
            # customers only see a fee when it is added on top of the trip price
            "service_fee": ({"type": p.commission_type, "value": str(p.commission_value)} if p.commission_mode == "ADD" else None),
            "note": "Time is counted from 'start trip' to 'arrived at destination' only."}


async def _visible_order(db: AsyncSession, order_id: int, user: User) -> Order:
    o = await load_order(db, order_id)
    if not o or (user.role not in Role.ADMINS and user.id not in (o.customer_id, o.driver_id)):
        raise HTTPException(404, "Order not found")  # 404 (not 403) so ids can't be probed
    return o


def _view(o: Order, user: User) -> dict:
    if user.role in Role.ADMINS:
        return order_for_admin(o)
    return order_for_driver(o) if user.id == o.driver_id else order_for_customer(o)


async def _with_my_rating(db: AsyncSession, o: Order, user: User) -> dict:
    d = _view(o, user)
    if user.role not in Role.ADMINS:
        from .ratings import my_rating
        d["my_rating"] = await my_rating(db, o.id, user.id)
    return d


@router.post("/orders/estimate")
async def estimate(body: EstimateIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Approximate price BEFORE ordering. The final price depends on the real trip time. The app must label it 'تقديري'."""
    s = await settings_service.get_all(db)
    p = await settings_service.get_pricing(db)
    km, minutes = route_guess(body.pickup_latitude, body.pickup_longitude, body.destination_latitude,
                              body.destination_longitude, s["estimate_road_factor"], s["estimate_avg_speed_kmh"])
    outside = bool(p.outside_enabled) and not point_in_boundary(body.destination_latitude, body.destination_longitude, p.country_boundary)
    pf = resolve_prefs(body, s)
    extras, breakdown = await compute_extras(
        db, s, p.decimals, ac=pf["pref_ac"], smoking=pf["pref_smoking"], gender=pf["pref_driver_gender"], large_car=pf["pref_large_car"],
        pickup=(body.pickup_latitude, body.pickup_longitude), destination=(body.destination_latitude, body.destination_longitude))
    e = money_estimate(minutes=minutes, now=utcnow(), unit_seconds=p.unit_seconds, unit_price=p.unit_price, min_price=p.min_price,
                       decimals=p.decimals, night_enabled=p.night_enabled, night_percent=p.night_percent, night_start=p.night_start,
                       night_end=p.night_end, ctype=p.commission_type, cvalue=p.commission_value, mode=p.commission_mode,
                       outside=outside, outside_percent=p.outside_driver_percent, extras=extras)
    d = p.decimals
    area = await find_area(db, body.pickup_latitude, body.pickup_longitude)
    return {"is_estimate": True, "currency": p.currency, "currency_decimals": d, "distance_km": km, "duration_min": minutes,
            "estimated_trip_price": f"{e['price']:.{d}f}", "night_applied": e["night_applied"],
            "night_surcharge": f"{e['night_surcharge']:.{d}f}", "extras_fee": f"{extras:.{d}f}", "extras": breakdown,
            "airport_applied": any(x["key"] == "AIRPORT" for x in breakdown),
            "service_fee": f"{e['customer_total'] - e['price']:.{d}f}" if p.commission_mode == "ADD" else None,
            "estimated_total": f"{e['customer_total']:.{d}f}", "is_outside_bahrain": outside, "pickup_in_service_area": area is not None,
            "note": "تقديري: السعر النهائي يُحسب من وقت الرحلة الفعلي من بدء الرحلة حتى الوصول."}


@router.post("/orders", status_code=201)
async def create_order(body: OrderCreateIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    if body.client_request_id:  # idempotent create (double taps / retries)
        ex = (await db.execute(select(Order.id).where(Order.customer_id == user.id,
                                                      Order.client_request_id == body.client_request_id))).scalar_one_or_none()
        if ex:
            return order_for_customer(await load_order(db, ex))
    open_order = (await db.execute(select(Order.id).where(Order.customer_id == user.id,
                                                          Order.order_status.not_in(S.FINAL)).limit(1))).first()
    if open_order:
        raise HTTPException(409, {"code": "OPEN_ORDER_EXISTS", "order_id": open_order[0]})
    s_cfg = await settings_service.get_all(db)
    if s_cfg["maintenance_mode"] == "true":
        raise HTTPException(503, {"code": "MAINTENANCE_MODE", "message": s_cfg["maintenance_message"] or "الخدمة متوقفة مؤقتًا"})
    if s_cfg["block_new_order_if_unpaid"] == "true":
        unpaid = (await db.execute(select(Order.id, Order.payment_id).where(
            Order.customer_id == user.id, Order.order_status == S.COMPLETED, Order.payment_status != "PAID").limit(1))).first()
        if unpaid:
            raise HTTPException(402, {"code": "UNPAID_ORDER_EXISTS", "order_id": unpaid[0], "payment_id": unpaid[1],
                                      "message": "يرجى سداد الرحلة السابقة أولًا"})
    if body.cargo_photo:
        await assert_owned(db, user.id, body.cargo_photo)
    area_id = body.service_area_id
    if body.pickup_latitude is not None:
        area = await find_area(db, body.pickup_latitude, body.pickup_longitude)
        area_id = area.id if area else None
        if not area:
            raise HTTPException(422, {"code": "OUTSIDE_SERVICE_AREA", "message": "نقطة الالتقاء خارج مناطق الخدمة"})
    p = await settings_service.get_pricing(db)  # currency + price snapshot (new orders only)
    data = body.model_dump(exclude={"accept_capacity_terms", "service_area_id"})
    outside = bool(p.outside_enabled) and not point_in_boundary(body.destination_latitude, body.destination_longitude, p.country_boundary)
    s_all = await settings_service.get_all(db)
    est_km, est_min = route_guess(body.pickup_latitude or body.destination_latitude, body.pickup_longitude or body.destination_longitude,
                                  body.destination_latitude, body.destination_longitude,
                                  s_all["estimate_road_factor"], s_all["estimate_avg_speed_kmh"])
    pf = resolve_prefs(body, s_all)
    data.update(pf)  # defaults filled in for preferences the app did not send
    extras, breakdown = await compute_extras(
        db, s_all, p.decimals, ac=pf["pref_ac"], smoking=pf["pref_smoking"], gender=pf["pref_driver_gender"], large_car=pf["pref_large_car"],
        pickup=(body.pickup_latitude, body.pickup_longitude) if body.pickup_latitude is not None else None,
        destination=(body.destination_latitude, body.destination_longitude))
    order = Order(extras_fee=extras, extras_breakdown=breakdown, est_distance_km=est_km, est_duration_min=est_min, customer_id=user.id, service_area_id=area_id, currency=p.currency, currency_decimals=p.decimals,
                  price_per_unit=p.unit_price, unit_duration_seconds=p.unit_seconds, min_price=p.min_price,
                  commission_type=p.commission_type, commission_value=p.commission_value,
                  commission_mode=p.commission_mode, night_enabled=p.night_enabled,
                  night_percent=p.night_percent, night_start=p.night_start, night_end=p.night_end,
                  is_outside_bahrain=outside, outside_driver_percent=p.outside_driver_percent if outside else 0, **data)
    db.add(order)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Duplicate request")
    db.add(OrderStatusHistory(order_id=order.id, old_status=None, new_status=S.PENDING, changed_by=user.id,
                              changed_by_role=user.role, notes="Order created"))
    await notify(db, user.id, "ORDER_RECEIVED", "تم استلام طلبك", "نبحث لك عن سائق.", order.id)
    await dispatch_order(db, order)
    await db.commit()
    return order_for_customer(await load_order(db, order.id))


@router.get("/orders/my-orders")
async def my_orders(status: str | None = None, page=Depends(paging), user: User = Depends(approved_user),
                    db: AsyncSession = Depends(get_db)):
    limit, offset = page
    col = Order.customer_id if user.role == Role.CUSTOMER else Order.driver_id
    q = select(Order).where(col == user.id)
    if status:
        q = q.where(Order.order_status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(Order.id.desc()).limit(limit).offset(offset))).scalars().all()
    return {"total": total, "items": [_view(o, user) for o in rows]}


@router.get("/orders/{order_id}")
async def get_order(order_id: int, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    """Always returns the current server state (apps must not rely on push alone)."""
    return await _with_my_rating(db, await _visible_order(db, order_id, user), user)


@router.get("/orders/{order_id}/status-history")
async def status_history(order_id: int, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    o = await _visible_order(db, order_id, user)
    rows = (await db.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == o.id)
                             .order_by(OrderStatusHistory.id))).scalars().all()
    return [history_out(h) for h in rows]


@router.post("/orders/{order_id}/cancel")
async def cancel_order(order_id: int, body: OptionalReasonIn, user: User = Depends(customer_user),
                       db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id, for_update=True)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    if o.order_status in S.FINAL:
        return order_for_customer(o)  # idempotent
    if o.order_status in (S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION):
        raise HTTPException(409, {"code": "TRIP_ALREADY_STARTED", "message": "لا يمكن الإلغاء بعد بدء الرحلة، تواصل مع الإدارة"})
    if o.order_status in S.DRIVER_BUSY and (await settings_service.get_all(db))["cancel_after_accept_allowed"] != "true":
        raise HTTPException(409, {"code": "CANCEL_NOT_ALLOWED"})
    had_driver = o.driver_id
    await close_pending_offers(db, o.id)
    o.cancelled_at, o.cancelled_by, o.cancelled_by_role = utcnow(), user.id, user.role
    o.cancellation_reason = body.reason
    await transition(db, o, S.CANCELLED, user.id, user.role, body.reason)
    if had_driver:
        await notify(db, had_driver, "ORDER_CANCELLED", "تم إلغاء الطلب", "ألغى العميل الطلب.", o.id)
    await db.commit()
    return order_for_customer(await load_order(db, o.id))


@router.post("/orders/{order_id}/retry")
async def retry_order(order_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Re-sends offers when no driver took the order (status NO_DRIVER_AVAILABLE)."""
    o = await load_order(db, order_id, for_update=True)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    if o.order_status != S.NO_DRIVER_AVAILABLE:
        raise HTTPException(409, {"code": "RETRY_NOT_ALLOWED", "status": o.order_status})
    if o.retry_count >= int((await settings_service.get_all(db))["max_order_retries"]):
        raise HTTPException(409, {"code": "RETRY_LIMIT_REACHED", "message": "تم الوصول للحد الأقصى لإعادة المحاولة، يرجى إنشاء طلب جديد"})
    o.retry_count += 1
    await dispatch_order(db, o)
    await db.commit()
    return order_for_customer(await load_order(db, o.id))


@router.post("/orders/{order_id}/dispute", status_code=201)
async def dispute(order_id: int, body: ReasonIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    """Objection to start/end time or price (no automatic edits; admin reviews)."""
    o = await _visible_order(db, order_id, user)
    c = Complaint(user_id=user.id, order_id=o.id, type="DISPUTE", subject=f"Dispute on order #{o.id}", message=body.reason)
    db.add(c)
    await db.commit()
    return {"id": c.id, "status": c.status}


LIVE_STATUSES = {S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP, S.IN_PROGRESS}


@router.get("/orders/{order_id}/driver-location")
async def driver_location(order_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Latest LIVE position of the order's driver (no history). Poll every ~5 s while the tracking screen is open."""
    o = await load_order(db, order_id)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    if o.order_status not in LIVE_STATUSES:
        raise HTTPException(409, {"code": "ORDER_NOT_ACTIVE", "message": "Tracking is only available while the order is active"})
    s = await settings_service.get_all(db)
    p = o.driver.driver_profile if o.driver else None
    if s["customer_live_tracking_enabled"] != "true" or not p or not p.is_online or p.mode != "LIVE":
        return {"available": False, "reason": "DRIVER_NOT_LIVE"}
    if not o.driver.location_consent:
        return {"available": False, "reason": "NO_CONSENT"}
    if p.last_lat is None or p.last_lng is None or not p.last_location_at or \
            (utcnow() - p.last_location_at).total_seconds() > int(s["live_location_max_age_seconds"]):
        return {"available": False, "reason": "STALE"}
    to_dest = o.order_status == S.IN_PROGRESS
    tlat, tlng = (o.destination_latitude, o.destination_longitude) if to_dest else (o.pickup_latitude, o.pickup_longitude)
    eta = None
    if tlat is not None and tlng is not None:
        speed = max(float(s["estimate_avg_speed_kmh"]), 1.0)
        eta = max(1, round(_haversine_m(p.last_lat, p.last_lng, tlat, tlng) / 1000 / speed * 60))
    return {"available": True, "lat": p.last_lat, "lng": p.last_lng, "heading": p.last_heading,
            "updated_at": p.last_location_at.isoformat(), "eta_min": eta, "eta_target": "DESTINATION" if to_dest else "PICKUP"}

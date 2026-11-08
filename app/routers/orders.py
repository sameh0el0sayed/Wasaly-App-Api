import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings as app_cfg
from ..constants import CURRENCIES, CounterStatus, OfferStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import approved_user, customer_user, paging, request_lang
from ..i18n import localize
from ..models import Complaint, CounterOffer, Order, OrderOffer, OrderStatusHistory, Payment, Referral, User, utcnow
from ..schemas import EstimateIn, OptionalReasonIn, OrderCreateIn, PriceRaiseIn, ReasonIn
from ..serializers import driver_card, history_out, order_for_admin, order_for_customer, order_for_driver
from ..services import favorites as fav
from ..services import price_increase as pi
from ..services import settings_service
from ..services.dispatch import dispatch_order, has_active_order, work_blockers
from ..services.estimate import money_estimate, route_guess
from ..services.extras import compute_extras, resolve_prefs
from ..services.files import assert_owned
from ..services.geo import _haversine_m, find_area, point_in_boundary
from ..services.notifications import notify
from ..services.orders import close_pending_counters, close_pending_offers, load_order, transition
from ..services.promo import resolve_code

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
            "languages": ["ar", "en"], "default_language": "ar",
            "i18n": {"version": int(s["i18n_version"] or 0), "languages": ["ar", "en"], "default": "ar"},
            "deep_link": {"scheme": "wasaly", "host": "phoenixsystems.online", "path_prefix": "/wasaly-api/r/", "android_package": "com.phoenixsystems.wasaly"},
            "role_switch": {"enabled": s["role_switch_enabled"] == "true", "customer_to_driver": s["customer_to_driver_enabled"] == "true"},
            "passkeys": {"enabled": s["passkeys_enabled"] == "true", "rp_id": app_cfg.passkey_rp_id},
            "analytics": {"enabled": s["analytics_enabled"] == "true", "batch_size": 20, "flush_seconds": 30},
            "promo_enabled": s["promo_enabled"] == "true" or s["referral_enabled"] == "true",
            "customer_live_tracking_enabled": s["customer_live_tracking_enabled"] == "true",
            "service_categories": _categories(s["service_categories"]),
            "online_window_minutes": int(s["online_window_minutes"]),
            "driver_choice_enabled": s["driver_choice_enabled"] == "true", "nearby_drivers_enabled": s["nearby_drivers_enabled"] == "true",
            "sos_enabled": s["sos_enabled"] == "true", "emergency_number": s["emergency_number"],
            "sos_taps_to_activate": int(s["sos_taps_to_activate"]), "sos_tap_window_seconds": int(s["sos_tap_window_seconds"]),
            "share_trip_enabled": s["share_trip_enabled"] == "true",
            "scheduled": {"enabled": s["scheduled_enabled"] == "true", "fee_percent": int(s["scheduled_fee_percent"]),
                          "min_minutes_ahead": int(s["scheduled_min_minutes_ahead"]), "max_days_ahead": int(s["scheduled_max_days_ahead"])},
            "stops": {"enabled": s["stops_enabled"] == "true", "max": int(s["max_extra_stops"]), "fee": s["stop_fee"]},
            "referral": {"enabled": s["referral_enabled"] == "true", "reward_invitee": s["referral_reward_invitee"], "reward_inviter": s["referral_reward_inviter"]},
            "cancel_fees": {"customer_amount": s["cancel_fee_customer_amount"], "free_seconds": int(s["cancel_fee_customer_free_seconds"]),
                            "driver_amount": s["cancel_fee_driver_amount"]},
            "price_increase": {"enabled": pi.config(s)["enabled"], "max_percent": pi.config(s)["max_percent"],
                               "steps": pi.config(s)["steps"], "counter_ttl_seconds": pi.config(s)["counter_ttl_seconds"]},
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
    from ..models import OrderStop
    from .v11 import stop_out
    d["stops"] = [stop_out(s) for s in (await db.execute(select(OrderStop).where(OrderStop.order_id == o.id).order_by(OrderStop.seq))).scalars().all()]
    if user.role not in Role.ADMINS:
        from .ratings import my_rating
        d["my_rating"] = await my_rating(db, o.id, user.id)
    return d


def _check_schedule(s: dict, at) -> None:
    from datetime import UTC, timedelta
    if s["scheduled_enabled"] != "true":
        raise HTTPException(422, {"code": "SCHEDULED_DISABLED"})
    at = at if at.tzinfo else at.replace(tzinfo=UTC)
    now = utcnow()
    if at < now + timedelta(minutes=int(s["scheduled_min_minutes_ahead"])):
        raise HTTPException(422, {"code": "SCHEDULE_TOO_SOON", "min_minutes_ahead": int(s["scheduled_min_minutes_ahead"])})
    if at > now + timedelta(days=int(s["scheduled_max_days_ahead"])):
        raise HTTPException(422, {"code": "SCHEDULE_TOO_FAR", "max_days_ahead": int(s["scheduled_max_days_ahead"])})


@router.post("/orders/estimate")
async def estimate(body: EstimateIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db),
                   lang: str = Depends(request_lang)):
    """Approximate price BEFORE ordering. The final price depends on the real trip time. The app must label it 'تقديري'."""
    s = await settings_service.get_all(db)
    p = await settings_service.get_pricing(db)
    user.last_lat, user.last_lng, user.last_location_at = body.pickup_latitude, body.pickup_longitude, utcnow()  # admin radar only
    await db.commit()
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
    promo, ref = await resolve_code(db, s, body.promo_code, e["trip_price"], user.id)
    if promo or ref:  # recompute with the discount (validation needed the undiscounted trip price)
        e = money_estimate(minutes=minutes, now=utcnow(), unit_seconds=p.unit_seconds, unit_price=p.unit_price, min_price=p.min_price,
                           decimals=p.decimals, night_enabled=p.night_enabled, night_percent=p.night_percent, night_start=p.night_start,
                           night_end=p.night_end, ctype=p.commission_type, cvalue=p.commission_value, mode=p.commission_mode,
                           outside=outside, outside_percent=p.outside_driver_percent, extras=extras,
                           promo_percent=promo.percent if promo else 0, promo_fixed=ref.amount if ref else 0)
    d = p.decimals
    area = await find_area(db, body.pickup_latitude, body.pickup_longitude)
    sched_fee = Decimal(0)
    if body.scheduled_at:
        _check_schedule(s, body.scheduled_at)
        sched_fee = (e["price"] * Decimal(s["scheduled_fee_percent"]) / 100).quantize(Decimal(1).scaleb(-d))
    return {"scheduled_fee": f"{sched_fee:.{d}f}", "is_estimate": True, "currency": p.currency, "currency_decimals": d, "distance_km": km, "duration_min": minutes,
            "estimated_trip_price": f"{e['price']:.{d}f}", "night_applied": e["night_applied"],
            "night_surcharge": f"{e['night_surcharge']:.{d}f}", "extras_fee": f"{extras:.{d}f}", "extras": breakdown,
            "airport_applied": any(x["key"] == "AIRPORT" for x in breakdown),
            "promo_code": promo.code if promo else (ref.code if ref else None), "promo_percent": promo.percent if promo else None,
            "promo_fixed": f"{ref.amount:.{d}f}" if ref else None, "is_referral_code": bool(ref),
            "discount_amount": f"{e['discount']:.{d}f}",
            "service_fee": f"{e['customer_total'] + e['discount'] - e['price']:.{d}f}" if p.commission_mode == "ADD" else None,
            "estimated_total": f"{e['customer_total'] + sched_fee:.{d}f}", "is_outside_bahrain": outside, "pickup_in_service_area": area is not None,
            "note": localize("تقديري: السعر النهائي يُحسب من وقت الرحلة الفعلي من بدء الرحلة حتى الوصول.", lang)}


@router.post("/orders", status_code=201)
async def create_order(body: OrderCreateIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    if body.client_request_id:  # idempotent create (double taps / retries)
        ex = (await db.execute(select(Order.id).where(Order.customer_id == user.id,
                                                      Order.client_request_id == body.client_request_id))).scalar_one_or_none()
        if ex:
            return order_for_customer(await load_order(db, ex))
    open_order = (await db.execute(select(Order.id).where(Order.customer_id == user.id,
                                                          Order.order_status.not_in(S.FINAL | {S.SCHEDULED})).limit(1))).first()
    if open_order:
        raise HTTPException(409, {"code": "OPEN_ORDER_EXISTS", "order_id": open_order[0]})
    s_cfg = await settings_service.get_all(db)
    if s_cfg["maintenance_mode"] == "true":
        raise HTTPException(503, {"code": "MAINTENANCE_MODE", "message": s_cfg["maintenance_message"] or "الخدمة متوقفة مؤقتًا"})
    if s_cfg["block_new_order_if_unpaid"] == "true":
        unpaid = (await db.execute(select(Order.id, Order.payment_id).where(
            Order.customer_id == user.id, Order.order_status == S.COMPLETED, Order.payment_status != "PAID").limit(1))).first()
        if not unpaid:
            unpaid = (await db.execute(select(Payment.order_id, Payment.id).where(
                Payment.customer_id == user.id, Payment.kind == "CANCEL_FEE",
                Payment.payment_status.in_(["UNPAID", "PENDING_PAYMENT", "PENDING_VERIFICATION", "REJECTED", "FAILED"])).limit(1))).first()
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
    data = body.model_dump(exclude={"accept_capacity_terms", "service_area_id", "promo_code", "scheduled_at"})
    data["choose_driver"] = bool(body.choose_driver and s_cfg["driver_choice_enabled"] == "true")
    if body.preferred_driver_id:
        if not await fav.is_favorite(db, user.id, body.preferred_driver_id):
            raise HTTPException(422, {"code": "NOT_FAVORITE_DRIVER"})
        data["choose_driver"] = False  # a direct trip: the driver's acceptance assigns the order
    if body.pickup_latitude is not None and body.pickup_longitude is not None:
        user.last_lat, user.last_lng, user.last_location_at = body.pickup_latitude, body.pickup_longitude, utcnow()  # admin radar only
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
    sched_at = None
    if body.scheduled_at:
        _check_schedule(s_all, body.scheduled_at)
        from datetime import UTC
        sched_at = body.scheduled_at if body.scheduled_at.tzinfo else body.scheduled_at.replace(tzinfo=UTC)
        base_est = money_estimate(minutes=est_min, now=sched_at, unit_seconds=p.unit_seconds, unit_price=p.unit_price, min_price=p.min_price,
                                  decimals=p.decimals, night_enabled=p.night_enabled, night_percent=p.night_percent, night_start=p.night_start,
                                  night_end=p.night_end, ctype=p.commission_type, cvalue=p.commission_value, mode=p.commission_mode,
                                  outside=outside, outside_percent=p.outside_driver_percent, extras=extras)
        sched_fee = (base_est["price"] * Decimal(s_all["scheduled_fee_percent"]) / 100).quantize(Decimal(1).scaleb(-p.decimals))
        extras = extras + sched_fee  # part of the trip price, so commission rules apply as usual
        breakdown = [*breakdown, {"key": "SCHEDULED", "amount": f"{sched_fee:.{p.decimals}f}"}]
    promo, ref = None, None
    if body.promo_code and body.promo_code.strip():
        est = money_estimate(minutes=est_min, now=utcnow(), unit_seconds=p.unit_seconds, unit_price=p.unit_price, min_price=p.min_price,
                             decimals=p.decimals, night_enabled=p.night_enabled, night_percent=p.night_percent, night_start=p.night_start,
                             night_end=p.night_end, ctype=p.commission_type, cvalue=p.commission_value, mode=p.commission_mode,
                             outside=outside, outside_percent=p.outside_driver_percent, extras=extras)
        promo, ref = await resolve_code(db, s_all, body.promo_code, est["trip_price"], user.id)
    referral_id = None
    if ref:
        row = ref.row
        if not row:
            row = Referral(inviter_id=ref.inviter_id, invitee_id=user.id)
            db.add(row)
            await db.flush()
        referral_id = row.id
    order = Order(promo_code=promo.code if promo else (ref.code if ref else None), promo_percent=promo.percent if promo else None,
                  promo_fixed=ref.amount if ref else None, referral_id=referral_id,
                  extras_fee=extras, extras_breakdown=breakdown, est_distance_km=est_km, est_duration_min=est_min, customer_id=user.id, service_area_id=area_id, currency=p.currency, currency_decimals=p.decimals,
                  price_per_unit=p.unit_price, unit_duration_seconds=p.unit_seconds, min_price=p.min_price,
                  commission_type=p.commission_type, commission_value=p.commission_value,
                  commission_mode=p.commission_mode, night_enabled=p.night_enabled,
                  night_percent=p.night_percent, night_start=p.night_start, night_end=p.night_end,
                  scheduled_at=sched_at, scheduled_fee=sched_fee if sched_at else 0,
                  is_outside_bahrain=outside, outside_driver_percent=p.outside_driver_percent if outside else 0, **data)
    db.add(order)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, "Duplicate request")
    data_status = S.SCHEDULED if sched_at else S.PENDING
    order.order_status = data_status
    db.add(OrderStatusHistory(order_id=order.id, old_status=None, new_status=data_status, changed_by=user.id,
                              changed_by_role=user.role, notes="Order created"))
    if sched_at:
        await notify(db, user.id, "SCHEDULED_CONFIRMED", "تم تأكيد رحلتك المجدولة", "سنبحث لك عن سائق قبل الموعد.", order.id)
    else:
        await notify(db, user.id, "ORDER_RECEIVED", "تم استلام طلبك", "نبحث لك عن سائق.", order.id)
        await dispatch_order(db, order)
    await db.commit()
    return order_for_customer(await load_order(db, order.id))


@router.get("/favorite-drivers")
async def favorite_drivers(limit: int = 10, available_only: bool = True, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Home-screen list: drivers this customer rated 4-5 stars (newest first, online ones flagged; ?limit=10). Only drivers online and free now; ?available_only=false lists all favourites. Use an id as `preferred_driver_id` in POST /api/orders for a direct trip."""
    items = []
    for d, stars, trips in await fav.favorite_rows(db, user.id):
        free = fav.online(d) and not work_blockers(d) and not await has_active_order(db, d.id)
        if available_only and not free:
            continue  # home list: only drivers who are online and free right now
        items.append({**driver_card(d), "my_stars": stars, "trips_together": trips, "is_online": fav.online(d), "is_available": free})
        if len(items) >= max(1, min(limit, 50)):
            break
    return {"items": items}


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


def customer_cancel_fee(o: Order, s: dict) -> dict:
    """Free while searching / scheduled / inside the free window after ACCEPTED; a fee once the driver is on the way."""
    from datetime import UTC
    dec = o.currency_decimals
    amount = Decimal(s["cancel_fee_customer_amount"]).quantize(Decimal(1).scaleb(-dec))
    reason = None
    if o.order_status in (S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP):
        reason = "DRIVER_ON_THE_WAY"
    elif o.order_status == S.ACCEPTED and o.accepted_at:
        at = o.accepted_at if o.accepted_at.tzinfo else o.accepted_at.replace(tzinfo=UTC)
        if (utcnow() - at).total_seconds() > int(s["cancel_fee_customer_free_seconds"]):
            reason = "AFTER_FREE_WINDOW"
    fee = amount if reason and amount > 0 else Decimal(0)
    return {"fee": fee, "reason": reason if fee > 0 else None, "free": fee <= 0}


@router.post("/orders/{order_id}/cancel/preview")
async def cancel_preview(order_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    info = customer_cancel_fee(o, await settings_service.get_all(db))
    return {"fee": f"{info['fee']:.{o.currency_decimals}f}", "currency": o.currency, "reason": info["reason"], "free": info["free"]}


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
    s_cfg = await settings_service.get_all(db)
    fee_info = customer_cancel_fee(o, s_cfg)
    await close_pending_offers(db, o.id)
    o.cancelled_at, o.cancelled_by, o.cancelled_by_role = utcnow(), user.id, user.role
    o.cancellation_reason = body.reason
    await transition(db, o, S.CANCELLED, user.id, user.role, body.reason)
    if had_driver:
        await notify(db, had_driver, "ORDER_CANCELLED", "تم إلغاء الطلب", "ألغى العميل الطلب.", o.id)
    fee_out = None
    if had_driver and fee_info["fee"] > 0:
        from ..services.payments import create_payment
        dec = o.currency_decimals
        fee = fee_info["fee"]
        # same split as trips: the company keeps its commission (order snapshot), the driver gets the rest
        cv = Decimal(o.commission_value or 0)
        company = min(fee, fee * cv / 100 if o.commission_type == "PERCENT" else cv).quantize(Decimal(1).scaleb(-dec))
        to_driver = fee - company
        pay = await create_payment(db, kind="CANCEL_FEE", customer_id=o.customer_id, driver_id=had_driver, amount=fee,
                                   driver_amount=to_driver, company_amount=fee - to_driver, currency=o.currency, decimals=dec, order_id=o.id)
        o.cancel_fee, o.cancel_fee_payer = fee, "CUSTOMER"
        fee_out = {"amount": f"{fee:.{dec}f}", "payment_id": pay.id}
        await notify(db, o.customer_id, "CANCEL_FEE_CHARGED", "رسوم إلغاء", f"تم احتساب رسوم إلغاء {fee:.{dec}f} {o.currency}.", o.id)
        await notify(db, had_driver, "CANCEL_FEE_CHARGED", "تعويض إلغاء", "تم احتساب تعويض لك عن إلغاء العميل.", o.id)
    await db.commit()
    out = order_for_customer(await load_order(db, o.id))
    out["cancel_fee_info"] = fee_out
    return out


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


@router.post("/orders/{order_id}/raise")
async def raise_order_price(order_id: int, body: PriceRaiseIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """The customer raises the price while the order is still searching (percent over the ORIGINAL estimate)."""
    o = await load_order(db, order_id, for_update=True)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    await pi.raise_price(db, o, body.percent, pi.config(await settings_service.get_all(db)))
    await db.commit()
    return order_for_customer(await load_order(db, o.id))


async def _my_counter(db, user, order_id: int, counter_id: int) -> tuple[Order, CounterOffer]:
    o = await load_order(db, order_id, for_update=True)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    c = (await db.execute(select(CounterOffer).where(CounterOffer.id == counter_id).with_for_update())).scalar_one_or_none()
    if not c or c.order_id != o.id:
        raise HTTPException(404, "Counter-offer not found")
    return o, c


@router.get("/orders/{order_id}/counter-offers")
async def list_counter_offers(order_id: int, all: int = 0, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    o = await load_order(db, order_id)
    if not o or o.customer_id != user.id:
        raise HTTPException(404, "Order not found")
    q = select(CounterOffer).where(CounterOffer.order_id == o.id)
    if not all:
        q = q.where(CounterOffer.status == CounterStatus.PENDING, CounterOffer.expires_at > utcnow())
    rows = (await db.execute(q.order_by(CounterOffer.id.desc()))).scalars().all()
    return {"items": [await pi.counter_view(db, c) for c in rows]}


@router.post("/orders/{order_id}/counter-offers/{counter_id}/accept")
async def accept_counter_offer(order_id: int, counter_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    from ..models import DriverProfile
    from ..services.dispatch import has_active_order, work_blockers
    o, c = await _my_counter(db, user, order_id, counter_id)
    now = utcnow()
    if c.status == CounterStatus.EXPIRED or (c.status == CounterStatus.PENDING and c.expires_at <= now):
        raise HTTPException(409, {"code": "COUNTER_EXPIRED"})
    if c.status != CounterStatus.PENDING:
        raise HTTPException(409, {"code": "COUNTER_NOT_PENDING", "status": c.status})
    if o.order_status not in S.SEARCHING:
        raise HTTPException(409, {"code": "ORDER_NOT_SEARCHING", "status": o.order_status})
    driver = await db.get(User, c.driver_id)
    await db.execute(select(DriverProfile.id).where(DriverProfile.user_id == driver.id).with_for_update())
    if work_blockers(driver) or not driver.driver_profile.is_online or await has_active_order(db, driver.id):
        await close_pending_counters(db, o.id, CounterStatus.EXPIRED, only_ids=[c.id])
        await db.commit()
        raise HTTPException(409, {"code": "DRIVER_BUSY"})
    res = await db.execute(update(Order).where(Order.id == o.id, Order.driver_id.is_(None), Order.order_status.in_(list(S.SEARCHING)))
                           .values(driver_id=driver.id, accepted_at=now, price_increase_percent=c.percent))
    if res.rowcount != 1:
        raise HTTPException(409, {"code": "ORDER_NOT_SEARCHING"})
    order = await load_order(db, o.id)
    await transition(db, order, S.ACCEPTED, user.id, "CUSTOMER", f"Counter-offer +{c.percent}% accepted")
    c.status, c.responded_at = CounterStatus.ACCEPTED, now
    offer = await db.get(OrderOffer, c.offer_id)
    offer.offer_status, offer.responded_at = OfferStatus.ACCEPTED, now
    await close_pending_offers(db, order.id, except_offer_id=offer.id)  # other drivers' offers closed, their counters REJECTED
    await notify(db, driver.id, "COUNTER_ACCEPTED", "تم قبول السعر الجديد", "وافق العميل على السعر الجديد، توجّه للعميل.", order.id)
    await db.commit()
    return order_for_customer(await load_order(db, order.id))


@router.post("/orders/{order_id}/counter-offers/{counter_id}/reject")
async def reject_counter_offer(order_id: int, counter_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    o, c = await _my_counter(db, user, order_id, counter_id)
    if c.status != CounterStatus.PENDING:
        raise HTTPException(409, {"code": "COUNTER_NOT_PENDING", "status": c.status})
    await close_pending_counters(db, o.id, CounterStatus.REJECTED, only_ids=[c.id])
    await db.commit()
    return {"ok": True}


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
    from datetime import UTC, datetime

    from ..services import cache
    fresh = (await cache.get_locations([o.driver_id])).get(o.driver_id)
    if fresh:  # newest position comes from Redis
        lat, lng, at, heading = fresh["lat"], fresh["lng"], datetime.fromtimestamp(fresh["ts"], UTC), fresh["heading"]
    elif p.last_lat is not None and p.last_lng is not None and p.last_location_at:
        lat, lng, at, heading = p.last_lat, p.last_lng, p.last_location_at, p.last_heading
    else:
        return {"available": False, "reason": "STALE"}
    if (utcnow() - (at if at.tzinfo else at.replace(tzinfo=UTC))).total_seconds() > int(s["live_location_max_age_seconds"]):
        return {"available": False, "reason": "STALE"}
    to_dest = o.order_status == S.IN_PROGRESS
    tlat, tlng = (o.destination_latitude, o.destination_longitude) if to_dest else (o.pickup_latitude, o.pickup_longitude)
    eta = None
    if tlat is not None and tlng is not None:
        speed = max(float(s["estimate_avg_speed_kmh"]), 1.0)
        eta = max(1, round(_haversine_m(lat, lng, tlat, tlng) / 1000 / speed * 60))
    return {"available": True, "lat": lat, "lng": lng, "heading": heading,
            "updated_at": at.isoformat(), "eta_min": eta, "eta_target": "DESTINATION" if to_dest else "PICKUP"}

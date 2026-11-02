from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import AccountStatus, OfferStatus
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import driver_user, paging
from ..models import (
    DriverPayout,
    DriverProfile,
    DriverServiceArea,
    DriverWorkSession,
    Order,
    OrderOffer,
    UploadedFile,
    User,
    utcnow,
)
from ..schemas import (
    ArrivedIn,
    AvailabilityIn,
    DriverAreasIn,
    DriverLocationIn,
    DriverPrefsIn,
    PayoutAccountIn,
    ReasonIn,
    StartTripIn,
    VehiclePatch,
)
from ..security import decrypt, encrypt
from ..serializers import dt, money, offer_out, order_for_driver, payout_out, vehicle_full
from ..services import settings_service
from ..services.audit import audit
from ..services.dispatch import expire_offers_and_orders, force_offline, has_active_order, work_blockers
from ..services.files import assert_owned
from ..services.notifications import notify
from ..services.orders import close_pending_offers, load_order, transition
from ..services.payments import create_payment
from ..services.pricing import apply_discount, calc_price, duration_seconds, is_night_start, night_surcharge, promo_discount, split_order

router = APIRouter(prefix="/api/driver", tags=["driver"])


def _arrival_view(o) -> dict:
    d = order_for_driver(o)
    d["customer_name"] = o.customer.full_name  # for the "rate your passenger" screen
    d["my_rating"] = None
    return d


async def _proof_photo(db, user, o, file_key, kind: str, require_key: str, required_code: str) -> str | None:
    """Validates a proof photo: uploaded by this driver with kind=<kind>. Required by setting (parcel orders by default)."""
    s = await settings_service.get_all(db)
    applies = o.service_type == "DELIVERY" or s["proof_photos_apply_to"] == "ALL"
    if not file_key:
        if applies and s[require_key] == "true":
            raise HTTPException(422, {"code": required_code})
        return None
    row = await db.get(UploadedFile, file_key)
    if not row or row.owner_id != user.id or not file_key.startswith(kind + "/"):
        raise HTTPException(422, {"code": "INVALID_FILE_KEY"})
    return file_key


def _clear_location(profile) -> None:
    profile.last_lat = profile.last_lng = profile.last_location_at = profile.last_speed_kmh = profile.last_heading = None


@router.get("/availability")
async def get_availability(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    s = await settings_service.get_all(db)
    p = user.driver_profile
    return {"online": p.is_online, "mode": p.mode if p.is_online else "OFF", "blockers": work_blockers(user),
            "live_mode_enabled": s["live_mode_enabled"] == "true", "live_location_interval_seconds": int(s["live_location_interval_seconds"])}


@router.post("/availability")
async def availability(body: AvailabilityIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """mode: OFF = no offers | NORMAL = offers, no location | LIVE = offers + shares location (nearest-first dispatch).
    `online` alone still works: true -> NORMAL (or stays LIVE), false -> OFF."""
    profile = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == user.id).with_for_update())).scalar_one()
    mode = body.mode or (("LIVE" if profile.mode == "LIVE" and profile.is_online else "NORMAL") if body.online else "OFF")
    if mode != "OFF":
        blockers = work_blockers(user)
        if not user.driver_profile.license_expiry_date or blockers:
            raise HTTPException(403, {"code": "CANNOT_GO_ONLINE", "reasons": blockers})
        areas = (await db.execute(select(func.count()).select_from(DriverServiceArea)
                                  .where(DriverServiceArea.driver_id == user.id))).scalar_one()
        if not areas:
            raise HTTPException(403, {"code": "CANNOT_GO_ONLINE", "reasons": ["NO_SERVICE_AREA"]})
        if mode == "LIVE":
            if (await settings_service.get_all(db))["live_mode_enabled"] != "true":
                raise HTTPException(409, {"code": "LIVE_DISABLED"})
            if not user.location_consent:
                raise HTTPException(403, {"code": "LOCATION_CONSENT_REQUIRED"})
        if not profile.is_online:
            profile.is_online = True
            db.add(DriverWorkSession(driver_id=user.id, online_at=utcnow()))
        if mode != "LIVE":
            _clear_location(profile)  # location is kept only while LIVE
        profile.mode = mode
    else:
        # going offline never cancels or changes a trip already in progress; it only stops new offers
        profile.is_online, profile.mode = False, "OFF"
        _clear_location(profile)
        await db.execute(update(DriverWorkSession).where(DriverWorkSession.driver_id == user.id,
                                                         DriverWorkSession.offline_at.is_(None)).values(offline_at=utcnow()))
        await db.execute(update(OrderOffer).where(OrderOffer.driver_id == user.id, OrderOffer.offer_status == OfferStatus.PENDING)
                         .values(offer_status=OfferStatus.CLOSED, responded_at=utcnow()))
    await db.commit()
    return {"online": profile.is_online, "mode": profile.mode}


@router.post("/location")
async def driver_location(body: DriverLocationIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Only while mode = LIVE. Keeps the LATEST position only (no route history); used for nearest-first dispatch."""
    p = user.driver_profile
    if not p.is_online or p.mode != "LIVE":
        raise HTTPException(409, {"code": "NOT_LIVE"})
    p.last_lat, p.last_lng, p.last_location_at = body.latitude, body.longitude, utcnow()
    p.last_speed_kmh, p.last_heading = body.speed_kmh, body.heading
    await db.commit()
    return {"ok": True}


@router.get("/status")
async def driver_status(user: User = Depends(driver_user)):
    return {"online": user.driver_profile.is_online, "blockers": work_blockers(user)}


@router.get("/offers")
async def offers(list: str = "new", page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """list=new -> live offers with seconds_left (10 s);  list=unaccepted -> offers that timed out."""
    limit, offset = page
    await expire_offers_and_orders(db)
    await db.commit()
    status = OfferStatus.PENDING if list == "new" else OfferStatus.EXPIRED
    rows = (await db.execute(select(OrderOffer).where(OrderOffer.driver_id == user.id, OrderOffer.offer_status == status)
                             .order_by(OrderOffer.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [offer_out(o, user.driver_profile) for o in rows]


@router.post("/offers/{offer_id}/accept")
async def accept_offer(offer_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Race-safe: only one driver can win. The conditional UPDATE (driver_id IS NULL) is the lock."""
    await db.execute(select(DriverProfile.id).where(DriverProfile.user_id == user.id).with_for_update())  # serialize per driver
    offer = (await db.execute(select(OrderOffer).where(OrderOffer.id == offer_id, OrderOffer.driver_id == user.id)
                              .with_for_update())).scalar_one_or_none()
    if not offer:
        raise HTTPException(404, "Offer not found")
    now = utcnow()
    if offer.offer_status != OfferStatus.PENDING or offer.expires_at <= now:
        if offer.offer_status == OfferStatus.PENDING:
            offer.offer_status, offer.responded_at = OfferStatus.EXPIRED, now
            await db.flush()
            await expire_offers_and_orders(db)  # keep the order state consistent too
        await db.commit()
        raise HTTPException(409, {"code": "OFFER_EXPIRED_OR_UNAVAILABLE"})
    if work_blockers(user) or not user.driver_profile.is_online:
        raise HTTPException(403, {"code": "CANNOT_WORK", "reasons": work_blockers(user)})
    if await has_active_order(db, user.id):
        raise HTTPException(409, {"code": "ACTIVE_ORDER_EXISTS"})
    res = await db.execute(update(Order).where(Order.id == offer.order_id, Order.driver_id.is_(None),
                                               Order.order_status.in_([S.PENDING, S.OFFERED, S.NO_DRIVER_AVAILABLE]))
                           .values(driver_id=user.id, accepted_at=now))
    if res.rowcount != 1:
        offer.offer_status, offer.responded_at = OfferStatus.CLOSED, now
        await db.commit()
        raise HTTPException(409, {"code": "ORDER_ALREADY_TAKEN"})
    order = await load_order(db, offer.order_id)
    await transition(db, order, S.ACCEPTED, user.id, "DRIVER", "Driver accepted")
    offer.offer_status, offer.responded_at = OfferStatus.ACCEPTED, now
    await close_pending_offers(db, order.id, except_offer_id=offer.id)
    v = user.vehicle
    await notify(db, order.customer_id, "ORDER_ACCEPTED", "تم قبول طلبك",
                 f"السائق {user.full_name} - {v.make} {v.model} {v.color} ({v.plate_number})", order.id)
    await db.commit()
    return order_for_driver(await load_order(db, order.id))


@router.post("/offers/{offer_id}/decline")
async def decline_offer(offer_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    offer = await db.get(OrderOffer, offer_id)
    if not offer or offer.driver_id != user.id:
        raise HTTPException(404, "Offer not found")
    if offer.offer_status == OfferStatus.PENDING:
        offer.offer_status, offer.responded_at = OfferStatus.DECLINED, utcnow()
        await db.flush()
        await expire_offers_and_orders(db)
    await db.commit()
    return {"status": offer.offer_status}


async def _my_order(db, order_id, user, for_update=True) -> Order:
    o = await load_order(db, order_id, for_update=for_update)
    if not o or o.driver_id != user.id:
        raise HTTPException(404, "Order not found")
    return o


@router.post("/orders/{order_id}/on-the-way")
async def on_the_way(order_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    o = await _my_order(db, order_id, user)
    if o.order_status == S.DRIVER_ON_THE_WAY:
        return order_for_driver(o)  # idempotent retry
    await transition(db, o, S.DRIVER_ON_THE_WAY, user.id, "DRIVER")
    o.driver_on_the_way_at = utcnow()
    await notify(db, o.customer_id, "DRIVER_ON_THE_WAY", "السائق في الطريق", "السائق في طريقه إلى نقطة الالتقاء.", o.id)
    await db.commit()
    return order_for_driver(o)


@router.post("/orders/{order_id}/arrived-at-pickup")
async def arrived_at_pickup(order_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    o = await _my_order(db, order_id, user)
    if o.order_status == S.ARRIVED_AT_PICKUP:
        return order_for_driver(o)
    await transition(db, o, S.ARRIVED_AT_PICKUP, user.id, "DRIVER")
    o.driver_arrived_at = utcnow()  # waiting time is NOT billed
    await notify(db, o.customer_id, "ARRIVED_AT_PICKUP", "وصل السائق", "السائق وصل إلى نقطة الالتقاء.", o.id)
    await db.commit()
    return order_for_driver(o)


@router.post("/orders/{order_id}/start-trip")
async def start_trip(order_id: int, body: StartTripIn = StartTripIn(), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    o = await _my_order(db, order_id, user)
    if o.order_status == S.IN_PROGRESS:
        return order_for_driver(o)  # retry after network loss: never overwrite trip_started_at
    photo = await _proof_photo(db, user, o, body.pickup_photo_file_key, "PICKUP_PROOF", "require_pickup_photo", "PICKUP_PHOTO_REQUIRED")
    if photo:
        o.pickup_photo, o.pickup_photo_at = photo, utcnow()
    await transition(db, o, S.IN_PROGRESS, user.id, "DRIVER")
    o.trip_started_at = utcnow()  # SERVER clock; billing starts here
    if o.pickup_photo:
        await notify(db, o.customer_id, "ORDER_PICKED_UP", "تم استلام الطلب", "استلم السائق طلبك وبدأت الرحلة.", o.id)
    else:
        await notify(db, o.customer_id, "TRIP_STARTED", "بدأت الرحلة", "بدأت رحلتك.", o.id)
    await db.commit()
    return order_for_driver(o)


@router.post("/orders/{order_id}/arrived-at-destination")
async def arrived_at_destination(order_id: int, body: ArrivedIn = ArrivedIn(), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    o = await _my_order(db, order_id, user)
    if o.order_status in (S.ARRIVED_AT_DESTINATION, S.COMPLETED):
        return _arrival_view(o)  # idempotent: never recompute
    photo = await _proof_photo(db, user, o, body.delivery_photo_file_key, "DELIVERY_PROOF", "require_delivery_photo", "DELIVERY_PHOTO_REQUIRED")
    if photo:
        o.delivery_photo, o.delivery_photo_at = photo, utcnow()
    await transition(db, o, S.ARRIVED_AT_DESTINATION, user.id, "DRIVER")
    now = utcnow()
    o.trip_arrived_at = now
    secs = duration_seconds(o.trip_started_at, now)
    units, base = calc_price(secs, o.unit_duration_seconds, o.price_per_unit, o.min_price, o.currency_decimals)
    night = bool(o.night_enabled) and is_night_start(o.trip_started_at, ZoneInfo(settings.timezone), o.night_start, o.night_end)
    surcharge = night_surcharge(base, o.night_percent, o.currency_decimals) if night else base - base
    price = base + surcharge + (o.extras_fee or 0)  # trip price incl. night surcharge and fixed extras (airport/preferences); commission is calculated on this
    o.base_price, o.night_applied, o.night_surcharge = base, night, surcharge
    o.actual_duration_seconds, o.final_price = secs, price
    o.commission, o.customer_total, o.driver_earning, cut = split_order(o, price)
    o.discount_amount = promo_discount(base + surcharge, o.promo_percent, o.currency_decimals)  # platform pays it; driver untouched
    o.commission, o.customer_total = apply_discount(o.commission, o.customer_total, o.discount_amount)
    o.outside_driver_cut = cut if o.is_outside_bahrain else None
    pay = await create_payment(db, kind="ORDER", order_id=o.id, customer_id=o.customer_id, driver_id=o.driver_id,
                               amount=o.customer_total, driver_amount=o.driver_earning, company_amount=o.commission,
                               currency=o.currency, decimals=o.currency_decimals)
    o.payment_id = pay.id
    shown = f"{money(o.customer_total, o.currency_decimals)} {o.currency}"
    earned = f"{money(o.driver_earning, o.currency_decimals)} {o.currency}"
    await transition(db, o, S.COMPLETED, user.id, "DRIVER",
                     f"{secs}s, {units} unit(s), base {base}, night {night} +{surcharge}, extras {o.extras_fee}, trip {price}, outside {o.is_outside_bahrain} cut {o.outside_driver_cut}, commission {o.commission}, customer {o.customer_total}, driver {o.driver_earning}")
    o.completed_at = now
    if o.delivery_photo:
        await notify(db, o.customer_id, "ORDER_DELIVERED", "تم التسليم", f"تم تسليم الطلب. المبلغ المطلوب: {shown}", o.id)
    else:
        await notify(db, o.customer_id, "TRIP_COMPLETED", "اكتملت الرحلة", f"المبلغ المطلوب: {shown}", o.id)
    await notify(db, user.id, "TRIP_COMPLETED", "اكتملت الرحلة", f"أرباحك من الرحلة: {earned}", o.id)
    await db.commit()
    return _arrival_view(o)


@router.post("/orders/{order_id}/cancel")
async def driver_cancel(order_id: int, body: ReasonIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Before the trip starts only (e.g. vehicle breakdown / customer no-show). Admin handles anything later."""
    o = await _my_order(db, order_id, user)
    if o.order_status in S.FINAL:
        return order_for_driver(o)
    if o.order_status not in (S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP):
        raise HTTPException(409, {"code": "CANNOT_CANCEL_NOW"})
    o.cancelled_at, o.cancelled_by, o.cancelled_by_role, o.cancellation_reason = utcnow(), user.id, "DRIVER", body.reason
    await transition(db, o, S.CANCELLED, user.id, "DRIVER", body.reason)
    await notify(db, o.customer_id, "ORDER_CANCELLED", "تم إلغاء الطلب", f"ألغى السائق الطلب: {body.reason}", o.id)
    await db.commit()
    return order_for_driver(o)


async def _list(db, user, statuses, page):
    limit, offset = page
    rows = (await db.execute(select(Order).where(Order.driver_id == user.id, Order.order_status.in_(statuses))
                             .order_by(Order.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [order_for_driver(o) for o in rows]


@router.get("/orders/accepted")
async def accepted(page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    return await _list(db, user, [S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP, S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION], page)


@router.get("/orders/active")
async def active(page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    return await _list(db, user, [S.IN_PROGRESS], page)


@router.get("/orders/completed")
async def completed(page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    return await _list(db, user, [S.COMPLETED], page)


@router.get("/orders/cancelled")
async def cancelled(page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    return await _list(db, user, [S.CANCELLED], page)


@router.get("/work-sessions")
async def work_sessions(page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    rows = (await db.execute(select(DriverWorkSession).where(DriverWorkSession.driver_id == user.id)
                             .order_by(DriverWorkSession.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": r.id, "online_at": dt(r.online_at), "offline_at": dt(r.offline_at),
             "seconds": int(((r.offline_at or utcnow()) - r.online_at).total_seconds())} for r in rows]


@router.get("/earnings")
async def earnings(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Driver earnings per currency (after the platform commission, depending on commission_mode)."""
    rows = (await db.execute(select(Order.currency, Order.currency_decimals, func.coalesce(func.sum(Order.final_price), 0),
                                    func.coalesce(func.sum(Order.driver_earning), 0), func.count())
                             .where(Order.driver_id == user.id, Order.order_status == S.COMPLETED)
                             .group_by(Order.currency, Order.currency_decimals))).all()
    return {"by_currency": [{"currency": c, "completed_trips": n, "trips_value": money(t, d), "driver_earning": money(e, d)}
                            for c, d, t, e, n in rows]}


@router.put("/payout-account")
async def set_payout_account(body: PayoutAccountIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Where the company transfers the driver's share. Stored encrypted; only the last 4 characters are ever shown."""
    p = user.driver_profile
    p.payout_method, p.payout_account_encrypted, p.payout_holder_name = body.method, encrypt(body.account), body.holder_name
    await db.execute(update(DriverPayout).where(DriverPayout.driver_id == user.id, DriverPayout.status == "FAILED",
                                                DriverPayout.failure_reason == "NO_PAYOUT_ACCOUNT").values(status="PENDING"))
    await db.commit()
    return {"method": p.payout_method, "account": "****" + body.account[-4:], "holder_name": p.payout_holder_name}


@router.get("/service-areas")
async def my_service_areas(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """The areas the admin assigned to this driver (offers only come from these)."""
    from ..models import ServiceArea
    rows = (await db.execute(select(ServiceArea).join(DriverServiceArea, DriverServiceArea.service_area_id == ServiceArea.id)
                             .where(DriverServiceArea.driver_id == user.id).order_by(ServiceArea.name))).scalars().all()
    return {"items": [{"id": a.id, "name": a.name, "boundary": a.boundary, "is_active": a.is_active} for a in rows]}


@router.get("/service-areas/available")
async def available_service_areas(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Every active area the driver can pick from; `selected` marks the ones he works in now."""
    from ..models import ServiceArea
    mine = set((await db.execute(select(DriverServiceArea.service_area_id).where(DriverServiceArea.driver_id == user.id))).scalars())
    rows = (await db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True)).order_by(ServiceArea.name))).scalars().all()
    return {"items": [{"id": a.id, "name": a.name, "boundary": a.boundary, "selected": a.id in mine} for a in rows]}


@router.put("/service-areas")
async def set_my_service_areas(body: DriverAreasIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """The driver chooses the areas he works in (offers only come from these). At least one active area is required."""
    from sqlalchemy import delete

    from ..models import ServiceArea
    ids = set(body.service_area_ids)
    if not ids:
        raise HTTPException(422, {"code": "AREAS_REQUIRED"})
    ok = set((await db.execute(select(ServiceArea.id).where(ServiceArea.id.in_(ids), ServiceArea.is_active.is_(True)))).scalars())
    if ids - ok:
        raise HTTPException(422, {"code": "UNKNOWN_AREA", "ids": sorted(ids - ok)})
    await db.execute(delete(DriverServiceArea).where(DriverServiceArea.driver_id == user.id))
    for i in ids:
        db.add(DriverServiceArea(driver_id=user.id, service_area_id=i))
    audit(db, user.id, "DRIVER_AREAS_SELF_SET", "user", user.id, details={"areas": sorted(ids)})
    await db.commit()
    return await my_service_areas(user, db)


@router.get("/payout-account")
async def get_payout_account(user: User = Depends(driver_user)):
    p = user.driver_profile
    acc = decrypt(p.payout_account_encrypted) if p.payout_account_encrypted else None
    return {"method": p.payout_method, "account": ("****" + acc[-4:]) if acc else None, "holder_name": p.payout_holder_name}


@router.patch("/preferences")
async def set_driver_preferences(body: DriverPrefsIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """The driver declares whether smoking is allowed in his car (used to match customer preferences)."""
    user.driver_profile.smoking_allowed = body.smoking_allowed
    await db.commit()
    return {"smoking_allowed": user.driver_profile.smoking_allowed, "gender": user.driver_profile.gender}


@router.get("/balance")
async def my_balance(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """debt_balance > 0 = the driver owes the company; new earnings are deducted from it until it reaches 0."""
    from ..services import settings_service
    s = await settings_service.get_all(db)
    return {"debt_balance": str(user.driver_profile.debt_balance or 0), "currency": s["currency_code"]}


@router.get("/payouts")
async def my_payouts(page=Depends(paging), user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    rows = (await db.execute(select(DriverPayout).where(DriverPayout.driver_id == user.id)
                             .order_by(DriverPayout.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [payout_out(r) for r in rows]


@router.patch("/vehicle")
async def patch_vehicle(body: VehiclePatch, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """Any vehicle change sends the vehicle back to admin review and takes the driver offline."""
    data = body.model_dump(exclude_unset=True)
    for key in (data.get("photos") or {}).values():
        await assert_owned(db, user.id, key)
    for k, v in data.items():
        setattr(user.vehicle, k, v)
    user.vehicle.vehicle_status = AccountStatus.PENDING_APPROVAL
    await force_offline(db, user.id)
    await db.commit()
    return vehicle_full(user.vehicle)


@router.get("/orders/{order_id}")  # declared after the static /orders/accepted|active|... routes
async def get_my_order(order_id: int, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """One trip of the signed-in driver (active-trip screen / deep links). 404 when it is not theirs."""
    o = await _my_order(db, order_id, user, for_update=False)
    d = order_for_driver(o)
    from .ratings import my_rating
    d["my_rating"] = await my_rating(db, o.id, user.id)
    return d

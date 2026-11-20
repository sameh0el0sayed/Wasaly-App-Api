from decimal import Decimal

from sqlalchemy.orm import joinedload, selectinload

from .models import (
    DriverPayout,
    Order,
    OrderOffer,
    OrderStatusHistory,
    Payment,
    Subscription,
    User,
    Vehicle,
    utcnow,
)
from .security import decrypt


def dt(v):
    return v.isoformat() if v else None


def money(v, decimals: int = 3):
    return f"{Decimal(v):.{decimals}f}" if v is not None else None


def vehicle_public(v):
    if not v:
        return None
    return {"make": v.make, "model": v.model, "year": v.year, "color": v.color,
            "plate_number": v.plate_number, "has_ac": v.has_ac}


def _file_url(key):
    from .security import create_file_token
    return f"/api/files/download/{create_file_token(key)}" if key else None


def driver_card(u: User, extra: dict | None = None) -> dict:
    """What a customer may see about a driver: name, photo, car (plate...) and a 'details' block. NEVER CPR / licence images or numbers."""
    p, v = u.driver_profile, u.vehicle
    photos = (v.photos or {}) if v else {}
    main = photos.get("main") or photos.get("front") or next(iter(photos.values()), None)
    veh = None
    if v:
        veh = {**vehicle_public(v), "vehicle_type": v.vehicle_type, "passenger_capacity": v.passenger_capacity,
               "luggage_capacity": v.luggage_capacity, "is_comfortable": v.is_comfortable, "photo_url": _file_url(main)}
    card = {"id": u.id, "name": u.full_name, "photo_url": _file_url(u.profile_photo),
            "gender": p.gender if p else None, "smoking_allowed": bool(p.smoking_allowed) if p else False, "vehicle": veh}
    card["details"] = {"driver": {"name": u.full_name, "photo_url": _file_url(u.profile_photo), "gender": card["gender"],
                                  "smoking_allowed": card["smoking_allowed"], "member_since": dt(u.created_at)},
                       "vehicle": ({**veh, "photos": {k: _file_url(val) for k, val in photos.items()}} if veh else None)}
    if extra:
        card.update(extra)
        card["details"]["driver"].update({k: extra[k] for k in ("rating", "ratings_count", "trips") if k in extra})
    return card


def _estimates(o: Order) -> dict:
    """original_estimate = what the customer would pay before any increase; estimate = now (after the increase)."""
    from .services.estimate import order_estimate
    now = utcnow()
    cur, orig = order_estimate(o, now), order_estimate(o, now, 0)
    d = o.currency_decimals if o.currency_decimals is not None else 3
    return {"original_estimate": money(orig["customer_total"], d) if orig else None,
            "estimate": money(cur["customer_total"], d) if cur else None}


def _driver_estimate(o: Order) -> str | None:
    from .services.estimate import order_estimate
    est = order_estimate(o, utcnow())
    return money(est["driver_earning"], o.currency_decimals) if est else None


def order_base(o: Order) -> dict:
    d = o.currency_decimals if o.currency_decimals is not None else 3
    return {
        "id": o.id, "service_type": o.service_type, "status": o.order_status,
        "pickup_address": o.pickup_address, "pickup_latitude": o.pickup_latitude, "pickup_longitude": o.pickup_longitude,
        "destination_address": o.destination_address, "destination_latitude": o.destination_latitude,
        "destination_longitude": o.destination_longitude,
        "passenger_count": o.passenger_count, "luggage_count": o.luggage_count,
        "cargo_description": o.cargo_description, "cargo_quantity": o.cargo_quantity,
        "cargo_size_note": o.cargo_size_note,
        "cargo_weight_kg": money(o.cargo_weight_kg), "cargo_photo": o.cargo_photo,
        "customer_notes": o.customer_notes, "driver_instructions": o.driver_instructions,
        "created_at": dt(o.created_at), "accepted_at": dt(o.accepted_at),
        "driver_on_the_way_at": dt(o.driver_on_the_way_at), "driver_arrived_at": dt(o.driver_arrived_at),
        "trip_started_at": dt(o.trip_started_at), "trip_arrived_at": dt(o.trip_arrived_at),
        "actual_duration_seconds": o.actual_duration_seconds,
        "unit_price": money(o.price_per_unit, d), "unit_duration_seconds": o.unit_duration_seconds,
        "final_price": money(o.final_price, d), "base_price": money(o.base_price, d),
        "night_surcharge": money(o.night_surcharge, d),
        "preferences": {"ac": o.pref_ac, "smoking": o.pref_smoking, "driver_gender": o.pref_driver_gender, "large_car": o.pref_large_car},
        "choose_driver": bool(o.choose_driver),
        "is_scheduled": o.scheduled_at is not None, "scheduled_at": dt(o.scheduled_at), "scheduled_fee": money(o.scheduled_fee or 0, d),
        "cancel_fee": money(o.cancel_fee, d) if o.cancel_fee is not None else None, "cancel_fee_payer": o.cancel_fee_payer,
        "stops_fee": money(o.stops_fee or 0, d), "stops": [], "preferred_driver_id": o.preferred_driver_id, "price_increase_percent": o.price_increase_percent or 0, "price_increase_amount": money(o.price_increase_amount, d) if o.price_increase_amount is not None else None,
        **_estimates(o),
        "promo_code": o.promo_code, "promo_percent": o.promo_percent, "discount_amount": money(o.discount_amount, d) if o.discount_amount is not None else None,
        "extras_fee": money(o.extras_fee, d), "extras": o.extras_breakdown or [],
        "pickup_photo": o.pickup_photo, "pickup_photo_at": dt(o.pickup_photo_at),
        "delivery_photo": o.delivery_photo, "delivery_photo_at": dt(o.delivery_photo_at),
        "night_applied": o.night_applied, "is_outside_bahrain": o.is_outside_bahrain, "currency": o.currency, "currency_decimals": d,
        "payment_id": o.payment_id, "payment_method": o.payment_method, "payment_status": o.payment_status,
        "completed_at": dt(o.completed_at), "cancelled_at": dt(o.cancelled_at),
        "cancelled_by_role": o.cancelled_by_role, "cancellation_reason": o.cancellation_reason,
        "cancel_reason_code": ("ORDER_TIMEOUT" if o.cancelled_by_role == "SYSTEM" or o.order_status == "EXPIRED"
                               else (o.cancelled_by_role if o.cancelled_by_role in ("CUSTOMER", "DRIVER", "ADMIN") else None)),
        "server_time": dt(utcnow()),
    }


def order_for_customer(o: Order) -> dict:
    d = order_base(o)
    dec = o.currency_decimals
    d["customer_total"] = money(o.customer_total, dec)  # what the customer pays
    d["service_fee"] = money(o.customer_total + (o.discount_amount or 0) - o.final_price, dec) if o.commission_mode == "ADD" and o.customer_total is not None else None  # only the part added on top of the price
    d["receiver_name"], d["receiver_phone"] = o.receiver_name, o.receiver_phone
    d["driver"] = None
    if o.driver:  # never exposes CPR / license / documents
        d["driver"] = {**driver_card(o.driver), "phone": o.driver.phone}
    d["driver_live"] = bool(o.driver and o.driver.driver_profile and o.driver.driver_profile.is_online
                            and o.driver.driver_profile.mode == "LIVE")
    return d


def order_for_driver(o: Order) -> dict:
    d = order_base(o)
    dec = o.currency_decimals
    d["driver_earning"] = money(o.driver_earning, dec)  # what the driver receives
    d["estimated_driver_earning"] = _driver_estimate(o)
    d["outside_fee"] = money(o.outside_driver_cut, dec) if o.is_outside_bahrain else None  # company cut on outside-Bahrain trips
    d["platform_fee"] = money(o.commission + (o.discount_amount or 0) - (o.outside_driver_cut or 0), dec) if o.commission_mode == "DEDUCT" and o.commission is not None else None  # deducted from the trip price
    d["customer"] = {"name": o.customer.full_name, "phone": o.contact_phone or o.customer.phone}
    d["receiver_name"], d["receiver_phone"] = o.receiver_name, o.receiver_phone
    return d


def order_for_admin(o: Order) -> dict:
    d = order_base(o)
    d.update({
        "customer": {"id": o.customer_id, "name": o.customer.full_name, "phone": o.customer.phone},
        "driver": ({"id": o.driver_id, "name": o.driver.full_name, "phone": o.driver.phone,
                    "vehicle": vehicle_public(o.driver.vehicle)} if o.driver else None),
        "outside_driver_percent": str(o.outside_driver_percent), "outside_driver_cut": money(o.outside_driver_cut, o.currency_decimals),
        "commission": money(o.commission, o.currency_decimals), "customer_total": money(o.customer_total, o.currency_decimals),
        "driver_earning": money(o.driver_earning, o.currency_decimals), "commission_mode": o.commission_mode,
        "commission_type": o.commission_type, "commission_value": str(o.commission_value),
        "service_area_id": o.service_area_id, "receiver_name": o.receiver_name, "receiver_phone": o.receiver_phone,
        "estimated_driver_earning": _driver_estimate(o),
        "price_adjusted": o.price_adjusted, "original_price": money(o.original_price, o.currency_decimals),
    })
    return d


def _pickup_km(p, o):
    """Distance from the driver's last LIVE position to the pickup (straight line). None when not LIVE / no fix."""
    from .services.geo import _haversine_m
    if not p or not p.last_lat or not o.pickup_latitude:
        return None
    return round(_haversine_m(p.last_lat, p.last_lng, o.pickup_latitude, o.pickup_longitude) / 1000, 1)


def offer_out(of: OrderOffer, driver_profile=None, price_increase_enabled: bool = True) -> dict:
    from .services.estimate import order_estimate
    o = of.order
    est = order_estimate(o, utcnow())
    left = max(0.0, (of.expires_at - utcnow()).total_seconds())
    return {
        "id": of.id, "order_id": of.order_id, "status": of.offer_status, "offered_at": dt(of.offered_at),
        "expires_at": dt(of.expires_at), "seconds_left": round(left, 1), "server_time": dt(utcnow()),
        "is_estimate": True, "currency": o.currency, "currency_decimals": o.currency_decimals,
        "distance_km": o.est_distance_km, "duration_min": o.est_duration_min, "timeout_seconds": round((of.expires_at - of.offered_at).total_seconds()),
        "pickup_distance_km": _pickup_km(driver_profile, o),
        "estimated_driver_earning": money(est["driver_earning"], o.currency_decimals) if est else None,
        "is_outside_bahrain": o.is_outside_bahrain,
        "price_increase_percent": o.price_increase_percent or 0,
        "counter": ({"id": of.counter.id, "percent": of.counter.percent, "status": of.counter.status,
                     "seconds_left": round(max(0.0, (of.counter.expires_at - utcnow()).total_seconds()), 1)} if of.counter else None),
        "can_counter": bool(price_increase_enabled and of.offer_status == "PENDING" and of.expires_at > utcnow() and not of.counter),
        "order": {  # no customer personal data before acceptance
            "service_type": o.service_type, "pickup_address": o.pickup_address,
            "pickup_latitude": o.pickup_latitude, "pickup_longitude": o.pickup_longitude,
            "destination_address": o.destination_address, "destination_latitude": o.destination_latitude,
            "destination_longitude": o.destination_longitude, "passenger_count": o.passenger_count,
            "luggage_count": o.luggage_count, "cargo_description": o.cargo_description,
            "cargo_quantity": o.cargo_quantity, "cargo_weight_kg": money(o.cargo_weight_kg),
            "customer_notes": o.customer_notes,
            "preferences": {"ac": o.pref_ac, "smoking": o.pref_smoking, "driver_gender": o.pref_driver_gender, "large_car": o.pref_large_car},
            "extras_fee": money(o.extras_fee, o.currency_decimals),
        },
    }


def history_out(h: OrderStatusHistory) -> dict:
    return {"id": h.id, "old_status": h.old_status, "new_status": h.new_status, "changed_by": h.changed_by,
            "changed_by_role": h.changed_by_role, "changed_at": dt(h.changed_at), "notes": h.notes}


def mask_cpr(u: User) -> str:
    return f"******{u.cpr_last3}" if u.cpr_last3 else ""


def rel(u, name: str):
    """A relationship only if it is already loaded (async code must never lazy-load); None otherwise."""
    from sqlalchemy import inspect
    return None if name in inspect(u).unloaded else getattr(u, name)


def roles_of(u: User) -> list[str]:
    """Roles the account holds (the active one is `u.role`). A driver profile means DRIVER; every non-admin may act as CUSTOMER."""
    r = set(u.roles or []) | {u.role}
    if r & {"ADMIN", "SUPER_ADMIN"}:
        r.add("CUSTOMER")  # an admin account may also act as a customer
    dp = rel(u, "driver_profile")
    if dp and dp.driver_status == "APPROVED":
        r |= {"DRIVER", "CUSTOMER"}
    return sorted(r)


def user_public(u: User) -> dict:
    from .services.license import verification
    return {"id": u.id, "full_name": u.full_name, "phone": u.phone, "role": u.role,
            "account_status": u.account_status, "phone_verified": u.phone_verified, "status_reason": u.status_reason,
            "profile_photo": u.profile_photo, "must_change_password": u.must_change_password,
            "cpr_masked": mask_cpr(u), "language": u.language or "ar",
            "verification": verification(u), "kyc_level": u.kyc_level or "none", "verified_photo_url": u.kyc_picture, "avatar_locked": bool(u.kyc_picture), "org_kyc_level": u.org_kyc_level,
            "roles": roles_of(u), "active_role": u.role,
            "driver_application": ({"id": da.id, "status": da.status, "reason": da.reason, "created_at": dt(da.created_at)}
                                    if (da := rel(u, "driver_application")) else None)}


def vehicle_full(v) -> dict | None:
    if not v:
        return None
    return {"id": v.id, "make": v.make, "model": v.model, "year": v.year, "color": v.color,
            "plate_number": v.plate_number, "vehicle_type": v.vehicle_type,
            "passenger_capacity": v.passenger_capacity, "luggage_capacity": v.luggage_capacity,
            "cargo_capacity_kg": money(v.cargo_capacity_kg), "has_ac": v.has_ac, "is_comfortable": v.is_comfortable,
            "photos": v.photos, "vehicle_status": v.vehicle_status,
            "documents": [{"id": d.id, "document_type": d.document_type, "file_key": d.file_path,
                           "expiry_date": d.expiry_date.isoformat() if d.expiry_date else None,
                           "review_status": d.review_status, "review_reason": d.review_reason} for d in v.documents]}


def driver_block(u: User, reveal: bool = False) -> dict | None:
    p = u.driver_profile
    if not p:
        return None
    lic = decrypt(p.license_number_encrypted)
    return {"driver_status": p.driver_status, "is_online": p.is_online,
            "license_number": lic if reveal else "****" + lic[-3:],
            "license_expiry_date": p.license_expiry_date.isoformat(), "license_document": p.license_document,
            "gender": p.gender, "gender_is_default": p.gender is None, "smoking_allowed": p.smoking_allowed,
            "vehicle": vehicle_full(u.vehicle)}


def payment_out(p, settings_map: dict | None = None, admin: bool = False) -> dict:
    d = p.currency_decimals
    out = {"id": p.id, "kind": p.kind, "order_id": p.order_id, "subscription_id": p.subscription_id,
           "amount": money(p.amount, d), "wallet_amount": money(p.wallet_amount or 0, d), "amount_due": money(p.amount - (p.wallet_amount or 0), d), "gateway_fee": money(p.gateway_fee, d), "charged_amount": money(p.charged_amount, d), "fee_category": p.fee_category, "fee_detail": p.fee_detail, "card_brand": p.card_brand,
           "currency": p.currency, "currency_decimals": d, "payment_method": p.payment_method,
           "payment_status": p.payment_status, "tap_url": p.tap_url if p.payment_status == "PENDING_PAYMENT" else None,
           "proof_file": p.proof_file, "reference": p.transaction_reference, "reject_reason": p.reject_reason,
           "paid_at": dt(p.paid_at), "created_at": dt(p.created_at)}
    if settings_map is not None:
        out["benefitpay_account"] = {"name": settings_map.get("benefitpay_account_name"),
                                     "number": settings_map.get("benefitpay_account_number"),
                                     "instructions": settings_map.get("benefitpay_instructions")}
        out["methods"] = [m for m, k in (("TAP", "tap_enabled"), ("BENEFIT", "benefit_enabled"), ("BENEFITPAY", "benefitpay_enabled")) if settings_map.get(k) == "true" and not (m in ("TAP", "BENEFIT") and settings_map.get("live_test") == "true")]
    if admin:
        out.update({"customer_id": p.customer_id, "driver_id": p.driver_id, "driver_amount": money(p.driver_amount, d),
                    "company_amount": money(p.company_amount, d), "verified_by": p.verified_by})
    return out


def payout_out(r) -> dict:
    return {"id": r.id, "payment_id": r.payment_id, "driver_id": r.driver_id, "amount": money(r.amount, r.currency_decimals),
            "debt_offset": money(r.debt_offset or 0, r.currency_decimals),
            "currency": r.currency, "status": r.status, "simulated": r.simulated, "reference": r.reference,
            "failure_reason": r.failure_reason, "created_at": dt(r.created_at), "transferred_at": dt(r.transferred_at)}


def subscription_out(s, view: str = "customer") -> dict:
    d = s.currency_decimals
    out = {"id": s.id, "category": s.category, "status": s.status, "passenger_name": s.passenger_name,
           "school_name": s.school_name, "passengers_count": s.passengers_count, "pickup_address": s.pickup_address,
           "destination_address": s.destination_address, "weekdays": s.weekdays, "pickup_time": s.pickup_time,
           "return_trip": s.return_trip, "return_time": s.return_time, "notes": s.notes, "currency": s.currency,
           "currency_decimals": d, "starts_on": s.starts_on.isoformat() if s.starts_on else None,
           "paid_until": s.paid_until.isoformat() if s.paid_until else None, "reject_reason": s.reject_reason,
           "created_at": dt(s.created_at)}
    if view == "customer":
        out["monthly_total"] = money(s.customer_total, d) if s.customer_total is not None else None
        out["service_fee"] = (money(s.customer_total - s.monthly_price, d)
                              if s.customer_total is not None and s.commission_mode == "ADD" else None)
        out["driver_id"] = s.driver_id
    elif view == "driver":
        out["monthly_earning"] = money(s.driver_earning, d) if s.driver_earning is not None else None
        out["pickup_latitude"], out["pickup_longitude"] = s.pickup_latitude, s.pickup_longitude
        out["destination_latitude"], out["destination_longitude"] = s.destination_latitude, s.destination_longitude
    else:  # admin
        out.update({"customer_id": s.customer_id, "driver_id": s.driver_id, "monthly_price": money(s.monthly_price, d) if s.monthly_price is not None else None,
                    "customer_total": money(s.customer_total, d) if s.customer_total is not None else None,
                    "driver_earning": money(s.driver_earning, d) if s.driver_earning is not None else None,
                    "commission": money(s.commission, d) if s.commission is not None else None,
                    "commission_value": str(s.commission_value), "commission_mode": s.commission_mode})
    return out


def sub_trip_out(t) -> dict:
    return {"id": t.id, "subscription_id": t.subscription_id, "date": t.trip_date.isoformat(), "leg": t.leg,
            "time": t.scheduled_time, "status": t.status, "completed_at": dt(t.completed_at), "note": t.note}


# =====================================================================
# Eager-loading option builders
# Call these in router queries: select(Order).options(*order_load_options())
# =====================================================================

def order_customer_load_options():
    """Eager load for order_for_customer(): driver + driver_profile."""
    return [
        selectinload(Order.driver).selectinload(User.driver_profile),
    ]


def order_driver_load_options():
    """Eager load for order_for_driver(): customer."""
    return [
        joinedload(Order.customer),
    ]


def order_admin_load_options():
    """Eager load for order_for_admin(): customer, driver, driver.vehicle."""
    return [
        joinedload(Order.customer),
        selectinload(Order.driver).selectinload(User.driver_profile),
        selectinload(Order.driver).joinedload(User.vehicle),
    ]


def order_full_load_options():
    """All order relationships (admin detail view)."""
    return [
        joinedload(Order.customer),
        selectinload(Order.driver).selectinload(User.driver_profile),
        selectinload(Order.driver).joinedload(User.vehicle).selectinload(Vehicle.documents),
    ]


def offer_load_options():
    """Eager load for offer_out(): order, counter."""
    return [
        joinedload(OrderOffer.order),
        selectinload(OrderOffer.counter),
    ]


def driver_full_load_options():
    """Eager load for driver_card()/driver_block(): driver_profile, vehicle, vehicle.documents, driver_application."""
    return [
        selectinload(User.driver_profile),
        selectinload(User.vehicle).selectinload(Vehicle.documents),
        selectinload(User.driver_application),
    ]


def driver_list_load_options():
    """Eager load for driver list views: driver_profile, vehicle."""
    return [
        selectinload(User.driver_profile),
        selectinload(User.vehicle),
    ]


def payment_admin_load_options():
    """Eager load for admin payment views: customer, driver, order, subscription."""
    return [
        joinedload(Payment.customer),
        joinedload(Payment.driver),
        joinedload(Payment.order),
        joinedload(Payment.subscription),
    ]


def payout_admin_load_options():
    """Eager load for admin payout views: driver, driver.driver_profile, payment."""
    return [
        joinedload(DriverPayout.driver).selectinload(User.driver_profile),
        joinedload(DriverPayout.payment),
    ]


def subscription_driver_load_options():
    """Eager load for subscription driver view."""
    return [
        selectinload(Subscription.driver),
    ]




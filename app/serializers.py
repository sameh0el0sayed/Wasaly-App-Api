from decimal import Decimal

from .models import Order, OrderOffer, OrderStatusHistory, User, utcnow
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
        "promo_code": o.promo_code, "promo_percent": o.promo_percent, "discount_amount": money(o.discount_amount, d) if o.discount_amount is not None else None,
        "extras_fee": money(o.extras_fee, d), "extras": o.extras_breakdown or [],
        "pickup_photo": o.pickup_photo, "pickup_photo_at": dt(o.pickup_photo_at),
        "delivery_photo": o.delivery_photo, "delivery_photo_at": dt(o.delivery_photo_at),
        "night_applied": o.night_applied, "is_outside_bahrain": o.is_outside_bahrain, "currency": o.currency, "currency_decimals": d,
        "payment_id": o.payment_id, "payment_method": o.payment_method, "payment_status": o.payment_status,
        "completed_at": dt(o.completed_at), "cancelled_at": dt(o.cancelled_at),
        "cancelled_by_role": o.cancelled_by_role, "cancellation_reason": o.cancellation_reason,
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
        d["driver"] = {"name": o.driver.full_name, "phone": o.driver.phone, "vehicle": vehicle_public(o.driver.vehicle)}
    d["driver_live"] = bool(o.driver and o.driver.driver_profile and o.driver.driver_profile.is_online
                            and o.driver.driver_profile.mode == "LIVE")
    return d


def order_for_driver(o: Order) -> dict:
    d = order_base(o)
    dec = o.currency_decimals
    d["driver_earning"] = money(o.driver_earning, dec)  # what the driver receives
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
        "price_adjusted": o.price_adjusted, "original_price": money(o.original_price, o.currency_decimals),
    })
    return d


def _pickup_km(p, o):
    """Distance from the driver's last LIVE position to the pickup (straight line). None when not LIVE / no fix."""
    from .services.geo import _haversine_m
    if not p or not p.last_lat or not o.pickup_latitude:
        return None
    return round(_haversine_m(p.last_lat, p.last_lng, o.pickup_latitude, o.pickup_longitude) / 1000, 1)


def offer_out(of: OrderOffer, driver_profile=None) -> dict:
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


def user_public(u: User) -> dict:
    return {"id": u.id, "full_name": u.full_name, "phone": u.phone, "role": u.role,
            "account_status": u.account_status, "status_reason": u.status_reason,
            "profile_photo": u.profile_photo, "must_change_password": u.must_change_password,
            "cpr_masked": mask_cpr(u)}


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
           "amount": money(p.amount, d), "gateway_fee": money(p.gateway_fee, d), "charged_amount": money(p.charged_amount, d),
           "currency": p.currency, "currency_decimals": d, "payment_method": p.payment_method,
           "payment_status": p.payment_status, "tap_url": p.tap_url if p.payment_status == "PENDING_PAYMENT" else None,
           "proof_file": p.proof_file, "reference": p.transaction_reference, "reject_reason": p.reject_reason,
           "paid_at": dt(p.paid_at), "created_at": dt(p.created_at)}
    if settings_map is not None:
        out["benefitpay_account"] = {"name": settings_map.get("benefitpay_account_name"),
                                     "number": settings_map.get("benefitpay_account_number"),
                                     "instructions": settings_map.get("benefitpay_instructions")}
        out["methods"] = [m for m, k in (("TAP", "tap_enabled"), ("BENEFITPAY", "benefitpay_enabled")) if settings_map.get(k) == "true"]
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

import json
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import CURRENCIES
from ..models import AppSetting

DEFAULTS = {
    "wallet_auto_apply": "true",
    "android_store_url": "",
    "ios_store_url": "",
    "passkeys_enabled": "true",
    "role_switch_enabled": "true",
    "customer_to_driver_enabled": "true",
    "i18n_version": "0",
    "license_expiry_warn_days": "30,7,1",
    "license_expiry_block_offers": "true",
    "analytics_enabled": "true",
    "analytics_retention_days": "180",
    "license_job_last_run": "",
    "partners_last_generated": "",
    "analytics_cleanup_last_run": "",
    "currency_code": "BHD",
    "unit_price": "0.500",
    "unit_duration_seconds": "300",
    "min_price": "0.500",
    "night_surcharge_enabled": "true",
    "night_surcharge_percent": "25",
    "night_start": "00:00",
    "night_end": "06:00",
    "outside_bahrain_enabled": "true",
    "outside_bahrain_driver_percent": "25",  # extra cut taken from the DRIVER share on trips ending outside Bahrain
    "country_boundary": json.dumps({"type": "polygon", "points": [[25.50, 50.37], [25.50, 50.85], [26.35, 50.85], [26.35, 50.37]]}),
    "estimate_road_factor": "1.35",  # straight-line km x factor = road km (estimates only)
    "estimate_avg_speed_kmh": "35",  # average city speed used to guess duration (estimates only)
    "tap_fee_percent": "0",   # gateway fee charged to the customer on top (Tap)
    "tap_fee_fixed": "0",
    "tap_enabled": "true",
    "benefitpay_enabled": "true",
    "benefitpay_instructions": "",  # text shown to the customer, e.g. "حوّل المبلغ ثم ارفع صورة التحويل"
    "benefitpay_account_name": "",
    "benefitpay_account_number": "",
    "block_new_order_if_unpaid": "true",
    "subscription_commission_value": "30",  # company % on monthly subscriptions (same mode as trips)
    "commission_type": "PERCENT",   # PERCENT | FIXED
    "commission_value": "30",       # 30 = 30% when PERCENT, or an amount when FIXED
    "commission_mode": "ADD",       # DEDUCT (taken from the price) | ADD (added on top of the price)
    "offer_ttl_seconds": "10",  # how long each driver has to accept an offer
    "offer_max_drivers": "0",   # 0 = offer to every eligible driver in the pickup area; N = only the first N
    "order_timeout_seconds": "600",
    "subscription_period_days": "30",  # length of one paid subscription period
    "subscription_renewal_reminder_days": "3",  # remind the customer this many days before it ends
    "login_max_failed_attempts": "5",
    "login_lockout_minutes": "15",
    "upload_max_mb": "8",
    "support_phone": "",
    "support_whatsapp": "",
    "support_email": "",
    "maintenance_mode": "false",  # true = new orders are refused with 503 MAINTENANCE_MODE
    "maintenance_message": "",
    "min_app_version_android": "1.0.0",  # apps older than this must show the update screen
    "min_app_version_ios": "1.0.0",
    "cancel_after_accept_allowed": "true",
    "require_cpr_photo": "false",
    "doc_expiry_warning_days": "14",
    "min_app_version": "1.0.0",  # one value for both platforms (the app sends nothing else)
    "latest_app_version": "1.0.0",
    "update_message": "",
    "ratings_enabled": "true",
    "driver_rates_customer_enabled": "true",
    "rating_window_hours": "48",  # how long after COMPLETED a rating is still accepted
    "rating_comment_max_chars": "500",
    "auto_suspend_driver_one_star_count": "1",  # drivers: suspend automatically after N one-star ratings (0 = off)
    "default_driver_gender": "MALE",  # used for drivers whose gender was not set (old accounts / not provided)
    "default_pref_ac": "false", "default_pref_smoking": "ANY", "default_pref_driver_gender": "ANY", "default_pref_large_car": "false",  # when the app sends no value
    "require_pickup_photo": "true", "require_delivery_photo": "true",  # driver proof photos
    "proof_photos_apply_to": "DELIVERY",  # DELIVERY = parcel orders only (no photos of passengers) | ALL = every order
    "sos_enabled": "true",
    "emergency_number": "999",
    "sos_taps_to_activate": "3",
    "sos_tap_window_seconds": "3",
    "share_trip_enabled": "true",
    "cancel_fee_customer_amount": "0.500",
    "cancel_fee_customer_free_seconds": "120",
    "cancel_fee_driver_amount": "0.500",
    "cancel_fee_driver_to": "COMPANY",
    "cancel_fee_customer_to_driver_percent": "100",
    "scheduled_enabled": "true",
    "scheduled_fee_percent": "25",
    "scheduled_min_minutes_ahead": "30",
    "scheduled_max_days_ahead": "7",
    "scheduled_dispatch_minutes_before": "20",
    "stops_enabled": "true",
    "max_extra_stops": "3",
    "stop_fee": "0.200",
    "referral_enabled": "true",
    "referral_reward_inviter": "0.500",
    "referral_reward_invitee": "0.500",
    "referral_reward_type": "CREDIT",
    "referral_max_per_user": "50",
    "referral_min_trip_to_reward": "1",
    "nearby_drivers_enabled": "true",  # customers see free LIVE drivers around them (GET /api/nearby-drivers)
    "driver_choice_enabled": "true",  # customers may choose their driver among those who respond (order.choose_driver)
    "price_increase_enabled": "true",  # customer raise + driver counter-offers (inDrive style)
    "max_price_increase_percent": "50",  # max total increase over the ORIGINAL estimate
    "price_increase_steps": "10,25,50",  # quick buttons in the app
    "counter_offer_ttl_seconds": "60",  # how long the customer has to answer a driver's price request
    "promo_enabled": "true",  # master switch for promo codes
    "customer_live_tracking_enabled": "true",  # customers can see their driver's live position during an active order
    "service_categories": '[{"key": "DOCUMENTS", "title_ar": "مستندات", "title_en": "Documents", "icon": "description", "color": "#1652F0", "prefill": {"cargo_description": "مستندات", "pref_large_car": false}, "is_active": true}, {"key": "PARCEL", "title_ar": "طرد", "title_en": "Parcel", "icon": "inventory_2", "color": "#6D4AFF", "prefill": {}, "is_active": true}, {"key": "SHOP", "title_ar": "طلب محل", "title_en": "Shop order", "icon": "shopping_bag", "color": "#16A36A", "prefill": {}, "is_active": true}, {"key": "LARGE", "title_ar": "أغراض كبيرة", "title_en": "Large items", "icon": "airport_shuttle", "color": "#E59A0B", "prefill": {"pref_large_car": true}, "is_active": true}, {"key": "AIRPORT", "title_ar": "مطار وسفر", "title_en": "Airport & travel", "icon": "flight", "color": "#4FA3FF", "prefill": {"pref_large_car": true}, "is_active": true}]',  # JSON list shown by the customer app (reorder / hide / rename)
    "large_car_vehicle_types": "SUV,VAN",  # vehicle_type values that count as a 'large car for travel and luggage'
    "pref_fee_ac": "0", "pref_fee_smoking_allowed": "0", "pref_fee_non_smoking": "0",  # fixed fee per customer preference (system currency)
    "pref_fee_female_driver": "0", "pref_fee_male_driver": "0", "pref_fee_large_car": "0",
    "account_deletion_grace_days": "30",  # deleted accounts can be restored by an admin until this many days pass, then they are anonymized
    "ban_reinstatement_fee": "50",  # fee (in the system currency) charged before the SUPER ADMIN may lift a permanent ban
    "auto_ban_after_auto_suspensions": "2",  # Nth automatic suspension becomes a permanent ban (0 = never)
    "auto_suspend_customer_one_star_count": "0",  # customers: same rule (0 = off; abusable by drivers, so off by default)
    "live_mode_enabled": "true",  # master switch for the driver LIVE mode (location sharing)
    "live_location_interval_seconds": "10",  # how often the app sends POST /api/driver/location while LIVE
    "live_location_max_age_seconds": "120",  # an older position is ignored for dispatch
    "dispatch_nearest_first": "true",  # offers go to the drivers nearest the pickup first (needs LIVE location)
    "dispatch_max_pickup_km": "0",  # 0 = no limit. N = LIVE drivers farther than N km from the pickup get no offer
    "max_order_retries": "3",  # how many times a customer can press "retry" on one order
    "driver_auto_offline_minutes": "0",  # 0 = off. N = a driver with no app activity for N minutes is set offline
    "online_window_minutes": "15",  # a user counts as ONLINE if any request reached the server within this window
    "activity_retention_days": "90",  # login history and location snapshots older than this are deleted
    "location_snapshots_enabled": "true",  # master switch; users must also give consent in the app
}


class Pricing(NamedTuple):
    currency: str
    decimals: int
    unit_price: Decimal
    unit_seconds: int
    min_price: Decimal
    commission_type: str
    commission_value: Decimal
    commission_mode: str
    night_enabled: bool
    night_percent: Decimal
    night_start: str
    night_end: str
    outside_enabled: bool = True
    outside_driver_percent: Decimal = Decimal(0)
    country_boundary: dict | None = None


SETTINGS_CACHE_KEY = "wasaly:settings:v1"


async def get_all(db: AsyncSession) -> dict[str, str]:
    """All settings (defaults + overrides). Read through Redis (300 s, invalidated by every admin change) when it is configured."""
    from . import cache
    hit = await cache.get_json(SETTINGS_CACHE_KEY)
    if hit is not None:
        return hit
    rows = (await db.execute(select(AppSetting))).scalars().all()
    data = dict(DEFAULTS)
    data.update({r.key: r.value for r in rows})
    await cache.set_json(SETTINGS_CACHE_KEY, data, 300)
    return data


async def get_pricing(db: AsyncSession) -> Pricing:
    s = await get_all(db)
    cur = s["currency_code"]
    return Pricing(cur, CURRENCIES[cur], Decimal(s["unit_price"]), int(s["unit_duration_seconds"]), Decimal(s["min_price"]),
                   s["commission_type"], Decimal(s["commission_value"]), s["commission_mode"],
                   s["night_surcharge_enabled"] == "true", Decimal(s["night_surcharge_percent"]), s["night_start"], s["night_end"],
                   s["outside_bahrain_enabled"] == "true", Decimal(s["outside_bahrain_driver_percent"]), json.loads(s["country_boundary"]))


async def set_value(db: AsyncSession, key: str, value: str, user_id: int | None):
    from . import cache
    await cache.delete(SETTINGS_CACHE_KEY)  # admin change -> next read reloads (the caller commits right after)
    row = await db.get(AppSetting, key)
    if row:
        row.value, row.updated_by = value, user_id
    else:
        db.add(AppSetting(key=key, value=value, updated_by=user_id))

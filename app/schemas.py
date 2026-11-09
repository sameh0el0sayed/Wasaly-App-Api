import re as _re
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from .security import normalize_phone, validate_cpr


def _phone(v):
    return normalize_phone(v) if v else v


class VehicleIn(BaseModel):
    make: str = Field(min_length=1, max_length=60)
    model: str = Field(min_length=1, max_length=60)
    year: int = Field(ge=1990, le=2100)
    color: str = Field(min_length=1, max_length=30)
    plate_number: str = Field(min_length=1, max_length=20)
    vehicle_type: str = "SEDAN"
    passenger_capacity: int = Field(ge=1, le=14)
    luggage_capacity: int = Field(default=0, ge=0, le=20)
    cargo_capacity_kg: Decimal | None = Field(default=None, ge=0)
    has_ac: bool = True
    is_comfortable: bool = True
    photos: dict[str, str] = {}  # front/back/left/right/interior/trunk -> file_key


class VehiclePatch(BaseModel):
    color: str | None = None
    passenger_capacity: int | None = Field(default=None, ge=1, le=14)
    luggage_capacity: int | None = Field(default=None, ge=0, le=20)
    cargo_capacity_kg: Decimal | None = Field(default=None, ge=0)
    has_ac: bool | None = None
    is_comfortable: bool | None = None
    photos: dict[str, str] | None = None


class DriverRegIn(BaseModel):
    license_number: str = Field(min_length=3, max_length=40)
    license_expiry_date: date
    license_issue_date: date
    license_document: str | None = None
    gender: Literal["MALE", "FEMALE"] | None = None  # needed to receive orders that ask for a male/female driver
    smoking_allowed: bool = False
    vehicle: VehicleIn


class RegisterIn(BaseModel):
    role: Literal["CUSTOMER", "DRIVER"]
    whatsapp_verification_id: str | None = Field(default=None, min_length=36, max_length=36)  # only used when whatsapp_verification_required=true
    language: Literal["ar", "en"] | None = None  # default: Accept-Language header, else ar
    full_name: str = Field(min_length=2, max_length=120)
    phone: str
    cpr: str
    password: str = Field(min_length=8, max_length=128)
    password_confirm: str
    profile_photo: str | None = None
    cpr_photo: str | None = None
    cpr_issue_date: date
    cpr_expiry_date: date
    driver: DriverRegIn | None = None
    referral_code: str | None = Field(default=None, max_length=16)
    device_id: str | None = Field(default=None, max_length=100)

    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v))
    v_cpr = field_validator("cpr")(lambda cls, v: validate_cpr(v))

    @model_validator(mode="after")
    def _check(self):
        if self.password != self.password_confirm:
            raise ValueError("Passwords do not match")
        if self.role == "DRIVER" and not self.driver:
            raise ValueError("Driver registration requires license and vehicle data")
        if self.role == "CUSTOMER" and self.driver:
            raise ValueError("Customer registration must not include driver data")
        return self


class LoginIn(BaseModel):
    phone: str
    password: str
    device_id: str | None = Field(default=None, max_length=100)
    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v))


class WhatsAppVerificationRequestIn(BaseModel):
    phone: str

    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v))


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


class ForgotPasswordIn(BaseModel):
    phone: str
    full_name: str | None = None
    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v))


class ProfilePatch(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    profile_photo: str | None = None
    cpr_photo: str | None = None
    language: Literal["ar", "en"] | None = None  # language of notifications and pushes


class DocumentIn(BaseModel):
    document_type: str = Field(min_length=2, max_length=40)
    file_key: str
    expiry_date: date | None = None


class LicenseRenewIn(BaseModel):
    license_expiry_date: date
    license_document: str


class AvailabilityIn(BaseModel):
    online: bool | None = None
    mode: Literal["OFF", "NORMAL", "LIVE"] | None = None

    @model_validator(mode="after")
    def v_one(self):
        if self.online is None and self.mode is None:
            raise ValueError("send online or mode")
        return self


class PreferencesIn(BaseModel):
    """None = "not sent": the server uses the admin-defined default (settings default_pref_*). Sending a value always wins."""
    pref_ac: bool | None = None
    pref_smoking: Literal["ANY", "ALLOWED", "NOT_ALLOWED"] | None = None
    pref_driver_gender: Literal["ANY", "FEMALE", "MALE"] | None = None
    pref_large_car: bool | None = None


class PromoIn(BaseModel):
    code: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_-]+$")
    percent: int = Field(ge=1, le=100)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    min_order_amount: Decimal | None = Field(default=None, ge=0)
    is_active: bool = True

    @field_validator("code")
    @classmethod
    def _upper(cls, v):
        return v.upper()

    @model_validator(mode="after")
    def _window(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class PromoPatch(BaseModel):
    percent: int | None = Field(default=None, ge=1, le=100)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    min_order_amount: Decimal | None = Field(default=None, ge=0)
    is_active: bool | None = None


class OrderCreateIn(PreferencesIn):
    promo_code: str | None = Field(default=None, max_length=40)
    service_type: Literal["PERSON", "DELIVERY"]
    pickup_address: str = Field(min_length=2, max_length=300)
    pickup_latitude: float | None = Field(default=None, ge=-90, le=90)
    pickup_longitude: float | None = Field(default=None, ge=-180, le=180)
    destination_address: str = Field(min_length=2, max_length=300)
    destination_latitude: float | None = Field(default=None, ge=-90, le=90)
    destination_longitude: float | None = Field(default=None, ge=-180, le=180)
    service_area_id: int | None = None  # used when pickup coordinates are not available
    contact_phone: str | None = None
    customer_notes: str | None = Field(default=None, max_length=1000)
    client_request_id: str | None = Field(default=None, max_length=64)  # idempotency key
    # PERSON
    passenger_count: int | None = Field(default=None, ge=1, le=14)
    luggage_count: int | None = Field(default=0, ge=0, le=20)
    # DELIVERY
    cargo_description: str | None = Field(default=None, max_length=1000)
    cargo_quantity: int | None = Field(default=None, ge=1)
    cargo_size_note: str | None = Field(default=None, max_length=200)
    cargo_weight_kg: Decimal | None = Field(default=None, ge=0)
    cargo_photo: str | None = None
    receiver_name: str | None = Field(default=None, max_length=120)
    receiver_phone: str | None = None
    driver_instructions: str | None = Field(default=None, max_length=1000)
    accept_capacity_terms: bool = False
    scheduled_at: datetime | None = None  # scheduled trip (ISO, UTC or with offset)
    preferred_driver_id: int | None = None  # direct trip with a driver the customer rated 4-5 stars (see GET /api/favorite-drivers)
    choose_driver: bool = True  # default ON: the customer picks among the drivers who respond; send false for "first driver to accept wins"

    v_phones = field_validator("contact_phone", "receiver_phone")(lambda cls, v: _phone(v))

    @model_validator(mode="after")
    def _check(self):
        if (self.pickup_latitude is None) != (self.pickup_longitude is None):
            raise ValueError("Provide both pickup latitude and longitude")
        if self.destination_latitude is None or self.destination_longitude is None:
            raise ValueError("Destination latitude and longitude are required")
        if self.pickup_latitude is None and not self.service_area_id:
            raise ValueError("Pickup coordinates or service_area_id are required")
        if self.service_type == "PERSON" and not self.passenger_count:
            raise ValueError("passenger_count is required for passenger transport")
        if self.service_type == "DELIVERY":
            if not self.cargo_description:
                raise ValueError("cargo_description is required for delivery")
            if not self.accept_capacity_terms:
                raise ValueError("Customer must acknowledge that acceptance depends on vehicle capacity and law")
        return self


class ReasonIn(BaseModel):
    reason: str = Field(min_length=2, max_length=1000)


class PaymentConfirmIn(BaseModel):
    reference: str | None = Field(default=None, min_length=1, max_length=100)
    review_notes: dict[str, str | bool | None] | None = None


class OptionalReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ComplaintIn(BaseModel):
    subject: str = Field(min_length=2, max_length=200)
    message: str = Field(min_length=2, max_length=3000)
    order_id: int | None = None
    type: Literal["COMPLAINT", "NO_SHOW_DRIVER", "NO_SHOW_CUSTOMER", "VEHICLE_BREAKDOWN"] = "COMPLAINT"


class DeviceIn(BaseModel):
    token: str = Field(min_length=10, max_length=300)
    platform: Literal["android", "ios", "web"] = "android"


class SignIn(BaseModel):
    file_key: str


# ---- admin ----
class AdminReasonIn(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)


class ApproveIn(BaseModel):
    approve_pending_documents: bool = True


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=128)


class DocReviewIn(BaseModel):
    status: Literal["APPROVED", "REJECTED"]
    reason: str | None = None


class DriverAreasIn(BaseModel):
    service_area_ids: list[int]


def _check_boundary(v):
    if v is None:
        return v
    t = v.get("type")
    if t == "polygon":
        pts = v.get("points") or []
        if len(pts) < 3 or any(len(p) != 2 for p in pts):
            raise ValueError("polygon needs >= 3 [lat,lng] points")
    elif t == "circle":
        c = v.get("center")
        if not c or len(c) != 2 or float(v.get("radius_m", 0)) <= 0:
            raise ValueError("circle needs center [lat,lng] and radius_m > 0")
    else:
        raise ValueError("boundary.type must be 'polygon' or 'circle'")
    return v


class AreaIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    boundary: dict | None = None
    is_active: bool = True
    v_boundary = field_validator("boundary")(lambda cls, v: _check_boundary(v))


class AdminUserEditIn(BaseModel):
    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = None
    cpr: str | None = None
    profile_photo: str | None = None
    cpr_photo: str | None = None
    cpr_issue_date: date | None = None
    cpr_expiry_date: date | None = None
    reason: str = Field(min_length=2, max_length=500)
    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v) if v is not None else v)
    v_cpr = field_validator("cpr")(lambda cls, v: validate_cpr(v) if v is not None else v)


class AdminLicenseEditIn(BaseModel):
    license_number: str | None = Field(default=None, min_length=1, max_length=60)
    license_expiry_date: date | None = None
    license_issue_date: date | None = None
    license_document: str | None = None
    reason: str = Field(min_length=2, max_length=500)


class AdminVehicleEditIn(BaseModel):
    make: str | None = Field(default=None, min_length=1, max_length=60)
    model: str | None = Field(default=None, min_length=1, max_length=60)
    year: int | None = Field(default=None, ge=1990, le=2100)
    color: str | None = Field(default=None, min_length=1, max_length=30)
    plate_number: str | None = Field(default=None, min_length=1, max_length=20)
    vehicle_type: str | None = Field(default=None, max_length=30)
    passenger_capacity: int | None = Field(default=None, ge=1, le=14)
    luggage_capacity: int | None = Field(default=None, ge=0, le=20)
    cargo_capacity_kg: Decimal | None = Field(default=None, ge=0)
    has_ac: bool | None = None
    is_comfortable: bool | None = None
    photos: dict[str, str] | None = None
    reason: str = Field(min_length=2, max_length=500)


class CorrectionFieldIn(BaseModel):
    field: str = Field(min_length=3, max_length=40)
    reason: str = Field(min_length=2, max_length=500)


class CorrectionRequestIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)  # general message
    fields: list[CorrectionFieldIn] = Field(min_length=1, max_length=20)


class CorrectionSubmitItem(BaseModel):
    id: int
    value: str = Field(max_length=500)


class CorrectionSubmitIn(BaseModel):
    items: list[CorrectionSubmitItem] = []


class ChangeReviewIn(BaseModel):
    status: Literal["ACCEPTED", "REJECTED"]
    reason: str | None = Field(default=None, max_length=500)


class CprChangeIn(BaseModel):
    cpr_number: str | None = None
    cpr_photo: str | None = None
    cpr_issue_date: date | None = None
    cpr_expiry_date: date | None = None

    @field_validator("cpr_number")
    @classmethod
    def _cpr(cls, v):
        return validate_cpr(v) if v else v


class PhoneChangeIn(BaseModel):
    phone: str
    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v))


class PriceRaiseIn(BaseModel):
    percent: int = Field(ge=1, le=300)


class AreaBulkIn(BaseModel):
    areas: list[AreaIn] = Field(min_length=1, max_length=200)


class AreaPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    boundary: dict | None = None
    is_active: bool | None = None
    v_boundary = field_validator("boundary")(lambda cls, v: _check_boundary(v))


_HEX = r"^#[0-9A-Fa-f]{6}$"
SCREENS = {"new_order", "pricing", "support"}
PREFILL_KEYS = {"cargo_description", "pref_ac", "pref_smoking", "pref_driver_gender", "pref_large_car", "service_type"}


class ServiceCategory(BaseModel):
    key: str = Field(pattern=r"^[A-Z0-9_]{2,30}$")
    title_ar: str = Field(min_length=1, max_length=60)
    title_en: str = Field(min_length=1, max_length=60)
    icon: str = Field(min_length=1, max_length=40)
    color: str = Field(pattern=_HEX)
    prefill: dict = {}
    is_active: bool = True

    @field_validator("prefill")
    @classmethod
    def _prefill(cls, v):
        bad = set(v) - PREFILL_KEYS
        if bad:
            raise ValueError(f"unknown prefill keys: {sorted(bad)}")
        return v


class SettingsPatch(BaseModel):
    currency_code: str | None = None
    night_surcharge_enabled: bool | None = None
    tap_fee_percent: Decimal | None = Field(default=None, ge=0, le=20)
    tap_fee_fixed: Decimal | None = Field(default=None, ge=0)
    estimate_road_factor: Decimal | None = Field(default=None, ge=1, le=3)
    estimate_avg_speed_kmh: Decimal | None = Field(default=None, ge=5, le=120)
    tap_enabled: bool | None = None
    live_test: bool | None = None
    live_test_message: str | None = Field(default=None, max_length=300)
    benefitpay_enabled: bool | None = None
    benefitpay_instructions: str | None = Field(default=None, max_length=500)
    benefitpay_account_name: str | None = Field(default=None, max_length=120)
    benefitpay_account_number: str | None = Field(default=None, max_length=60)
    block_new_order_if_unpaid: bool | None = None
    subscription_commission_value: Decimal | None = Field(default=None, ge=0, le=100)
    outside_bahrain_enabled: bool | None = None
    outside_bahrain_driver_percent: Decimal | None = Field(default=None, ge=0, le=100)
    country_boundary: dict | None = None
    night_surcharge_percent: Decimal | None = Field(default=None, ge=0, le=100)
    night_start: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    night_end: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    commission_type: Literal["PERCENT", "FIXED"] | None = None
    commission_value: Decimal | None = Field(default=None, ge=0)
    commission_mode: Literal["DEDUCT", "ADD"] | None = None
    unit_price: Decimal | None = Field(default=None, gt=0)
    unit_duration_seconds: int | None = Field(default=None, ge=60)
    min_price: Decimal | None = Field(default=None, ge=0)
    offer_ttl_seconds: int | None = Field(default=None, ge=5, le=120)
    offer_max_drivers: int | None = Field(default=None, ge=0, le=500)
    order_timeout_seconds: int | None = Field(default=None, ge=30)
    subscription_period_days: int | None = Field(default=None, ge=7, le=92)
    subscription_renewal_reminder_days: int | None = Field(default=None, ge=1, le=30)
    login_max_failed_attempts: int | None = Field(default=None, ge=3, le=20)
    login_lockout_minutes: int | None = Field(default=None, ge=1, le=1440)
    upload_max_mb: int | None = Field(default=None, ge=1, le=25)
    support_phone: str | None = Field(default=None, max_length=30)
    support_whatsapp: str | None = Field(default=None, max_length=30)
    support_email: str | None = Field(default=None, max_length=120)
    maintenance_mode: bool | None = None
    maintenance_message: str | None = Field(default=None, max_length=300)
    min_app_version_android: str | None = Field(default=None, pattern=r"^\d+(\.\d+){1,2}$")
    min_app_version_ios: str | None = Field(default=None, pattern=r"^\d+(\.\d+){1,2}$")
    cancel_after_accept_allowed: bool | None = None
    require_cpr_photo: bool | None = None
    doc_expiry_warning_days: int | None = Field(default=None, ge=1, le=120)
    min_app_version: str | None = Field(default=None, pattern=r"^\d+(\.\d+){1,2}$")
    latest_app_version: str | None = Field(default=None, pattern=r"^\d+(\.\d+){1,2}$")
    update_message: str | None = Field(default=None, max_length=300)
    ratings_enabled: bool | None = None
    driver_rates_customer_enabled: bool | None = None
    rating_window_hours: int | None = Field(default=None, ge=1, le=720)
    rating_comment_max_chars: int | None = Field(default=None, ge=50, le=2000)
    auto_suspend_driver_one_star_count: int | None = Field(default=None, ge=0, le=20)
    default_driver_gender: Literal["MALE", "FEMALE"] | None = None
    default_pref_ac: bool | None = None
    default_pref_smoking: Literal["ANY", "ALLOWED", "NOT_ALLOWED"] | None = None
    default_pref_driver_gender: Literal["ANY", "FEMALE", "MALE"] | None = None
    default_pref_large_car: bool | None = None
    customer_live_tracking_enabled: bool | None = None
    promo_enabled: bool | None = None
    price_increase_enabled: bool | None = None
    driver_choice_enabled: bool | None = None
    nearby_drivers_enabled: bool | None = None
    sos_enabled: bool | None = None
    emergency_number: str | None = None
    sos_taps_to_activate: int | None = None
    sos_tap_window_seconds: int | None = None
    share_trip_enabled: bool | None = None
    cancel_fee_customer_amount: str | None = None
    cancel_fee_customer_free_seconds: int | None = None
    cancel_fee_driver_amount: str | None = None
    cancel_fee_driver_to: str | None = None
    cancel_fee_customer_to_driver_percent: int | None = None
    scheduled_enabled: bool | None = None
    scheduled_fee_percent: int | None = None
    scheduled_min_minutes_ahead: int | None = None
    scheduled_max_days_ahead: int | None = None
    scheduled_dispatch_minutes_before: int | None = None
    stops_enabled: bool | None = None
    max_extra_stops: int | None = None
    stop_fee: str | None = None
    referral_enabled: bool | None = None
    referral_reward_inviter: str | None = None
    referral_reward_invitee: str | None = None
    referral_reward_type: str | None = None
    referral_max_per_user: int | None = None
    referral_min_trip_to_reward: int | None = None
    max_price_increase_percent: int | None = Field(default=None, ge=1, le=300)
    price_increase_steps: str | None = Field(default=None, pattern=r"^\d{1,3}(,\d{1,3}){0,7}$")
    counter_offer_ttl_seconds: int | None = Field(default=None, ge=10, le=600)
    service_categories: list[ServiceCategory] | None = Field(default=None, max_length=12)
    require_pickup_photo: bool | None = None
    require_delivery_photo: bool | None = None
    proof_photos_apply_to: Literal["DELIVERY", "ALL"] | None = None
    large_car_vehicle_types: str | None = Field(default=None, max_length=200)
    pref_fee_ac: Decimal | None = Field(default=None, ge=0, le=1000)
    pref_fee_smoking_allowed: Decimal | None = Field(default=None, ge=0, le=1000)
    pref_fee_non_smoking: Decimal | None = Field(default=None, ge=0, le=1000)
    pref_fee_female_driver: Decimal | None = Field(default=None, ge=0, le=1000)
    pref_fee_male_driver: Decimal | None = Field(default=None, ge=0, le=1000)
    pref_fee_large_car: Decimal | None = Field(default=None, ge=0, le=1000)
    account_deletion_grace_days: int | None = Field(default=None, ge=0, le=365)
    ban_reinstatement_fee: Decimal | None = Field(default=None, ge=0, le=100000)
    auto_ban_after_auto_suspensions: int | None = Field(default=None, ge=0, le=10)
    auto_suspend_customer_one_star_count: int | None = Field(default=None, ge=0, le=20)
    live_mode_enabled: bool | None = None
    live_location_interval_seconds: int | None = Field(default=None, ge=3, le=120)
    live_location_max_age_seconds: int | None = Field(default=None, ge=15, le=3600)
    dispatch_nearest_first: bool | None = None
    dispatch_max_pickup_km: Decimal | None = Field(default=None, ge=0, le=200)
    max_order_retries: int | None = Field(default=None, ge=0, le=20)
    driver_auto_offline_minutes: int | None = Field(default=None, ge=0, le=1440)
    online_window_minutes: int | None = Field(default=None, ge=1, le=240)
    activity_retention_days: int | None = Field(default=None, ge=7, le=730)
    location_snapshots_enabled: bool | None = None
    reason: str = Field(min_length=2, max_length=500)

    @field_validator("country_boundary")
    @classmethod
    def _cb(cls, v):
        return _check_boundary(v)

    @field_validator("currency_code")
    @classmethod
    def _cur(cls, v):
        from .constants import CURRENCIES
        if v is None:
            return v
        v = v.upper()
        if v not in CURRENCIES:
            raise ValueError(f"Unsupported currency. Supported: {', '.join(CURRENCIES)}")
        return v


class PriceAdjustIn(BaseModel):
    new_price: Decimal = Field(ge=0)
    reason: str = Field(min_length=3, max_length=1000)


class ComplaintResolveIn(BaseModel):
    response: str = Field(min_length=2, max_length=3000)


class AdminCreateIn(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str
    password: str = Field(min_length=8, max_length=128)
    role: Literal["ADMIN", "SUPER_ADMIN", "AI"] = "ADMIN"
    v_phone = field_validator("phone")(lambda cls, v: normalize_phone(v))


class BenefitPayIn(BaseModel):
    proof_file_key: str  # screenshot of the BenefitPay transfer (uploaded with POST /api/uploads)
    reference: str | None = Field(default=None, max_length=100)  # transfer reference number, if shown in the app


class PayoutAccountIn(BaseModel):
    method: Literal["IBAN", "BENEFITPAY"]
    account: str = Field(min_length=4, max_length=60)  # IBAN or BenefitPay number
    holder_name: str = Field(min_length=2, max_length=120)


class MarkTransferredIn(BaseModel):
    reference: str = Field(min_length=2, max_length=120)




class SubscriptionIn(BaseModel):
    category: Literal["WORK", "BUSINESS", "SCHOOL"]
    passenger_name: str | None = Field(default=None, max_length=120)  # student / employee
    school_name: str | None = Field(default=None, max_length=160)
    passengers_count: int = Field(default=1, ge=1, le=8)
    pickup_address: str = Field(min_length=3, max_length=300)
    pickup_latitude: float | None = Field(default=None, ge=-90, le=90)
    pickup_longitude: float | None = Field(default=None, ge=-180, le=180)
    destination_address: str = Field(min_length=3, max_length=300)
    destination_latitude: float | None = Field(default=None, ge=-90, le=90)
    destination_longitude: float | None = Field(default=None, ge=-180, le=180)
    weekdays: list[int] = Field(min_length=1, max_length=7)  # 0=Monday .. 6=Sunday
    pickup_time: str
    return_trip: bool = False
    return_time: str | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("weekdays")
    @classmethod
    def v_days(cls, v):
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("weekdays must be 0..6")
        return sorted(set(v))

    @field_validator("pickup_time", "return_time")
    @classmethod
    def v_time(cls, v):
        if v is not None and not _re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", v):
            raise ValueError("time must be HH:MM")
        return v

    @model_validator(mode="after")
    def v_rules(self):
        if self.return_trip and not self.return_time:
            raise ValueError("return_time is required when return_trip is true")
        if self.category == "SCHOOL" and not (self.passenger_name and self.school_name):
            raise ValueError("SCHOOL subscriptions need passenger_name (student) and school_name")
        return self


class SubscriptionQuoteIn(BaseModel):
    monthly_price: Decimal = Field(gt=0, le=100000)  # price of the trip service per month, before company commission
    driver_id: int
    starts_on: date | None = None  # default: today


class BroadcastIn(BaseModel):
    audience: Literal["CUSTOMERS", "DRIVERS", "ALL"] | None = None  # everyone of a kind, or both
    user_id: int | None = None  # or one specific user
    title: str = Field(min_length=2, max_length=120)
    message: str = Field(min_length=2, max_length=500)

    @model_validator(mode="after")
    def v_target(self):
        if (self.audience is None) == (self.user_id is None):
            raise ValueError("send either audience or user_id")
        return self


class EstimateIn(PreferencesIn):
    scheduled_at: datetime | None = None
    promo_code: str | None = Field(default=None, max_length=40)
    pickup_latitude: float = Field(ge=-90, le=90)
    pickup_longitude: float = Field(ge=-180, le=180)
    destination_latitude: float = Field(ge=-90, le=90)
    destination_longitude: float = Field(ge=-180, le=180)


class BannerIn(BaseModel):
    title_ar: str = Field(min_length=1, max_length=160)
    title_en: str | None = Field(default=None, max_length=160)
    subtitle_ar: str | None = Field(default=None, max_length=300)
    subtitle_en: str | None = Field(default=None, max_length=300)
    image_file_key: str | None = Field(default=None, max_length=120)
    bg_color: str | None = Field(default=None, pattern=_HEX)
    action: dict | None = None  # {"type": NONE|URL|SCREEN, "value": ...}
    audience: Literal["CUSTOMER", "DRIVER", "ALL"] = "ALL"
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    sort_order: int = Field(default=0, ge=0, le=1000)
    is_active: bool = True

    @field_validator("action")
    @classmethod
    def _action(cls, v):
        if v is None:
            return {"type": "NONE", "value": None}
        t, val = v.get("type", "NONE"), v.get("value")
        if t not in ("NONE", "URL", "SCREEN"):
            raise ValueError("action.type must be NONE, URL or SCREEN")
        if t == "URL" and not (isinstance(val, str) and val.startswith("https://")):
            raise ValueError("URL actions must start with https://")
        if t == "SCREEN" and val not in SCREENS:
            raise ValueError(f"SCREEN value must be one of {sorted(SCREENS)}")
        return {"type": t, "value": val if t != "NONE" else None}

    @model_validator(mode="after")
    def _window(self):
        if self.starts_at and self.ends_at and self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class BannerPatch(BannerIn):
    title_ar: str | None = Field(default=None, min_length=1, max_length=160)
    audience: Literal["CUSTOMER", "DRIVER", "ALL"] | None = None
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    is_active: bool | None = None


class PlaceIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    label: Literal["HOME", "WORK", "OTHER"]
    name: str = Field(min_length=1, max_length=100)
    address: str = Field(min_length=1, max_length=300)
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class PlacePatch(BaseModel):
    label: Literal["HOME", "WORK", "OTHER"] | None = None
    name: str | None = Field(default=None, min_length=1, max_length=100)
    address: str | None = Field(default=None, min_length=1, max_length=300)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)


class StartTripIn(BaseModel):
    pickup_photo_file_key: str | None = Field(default=None, max_length=120)


class ArrivedIn(BaseModel):
    delivery_photo_file_key: str | None = Field(default=None, max_length=120)


class DriverPrefsIn(BaseModel):
    smoking_allowed: bool


class DriverGenderIn(BaseModel):
    gender: Literal["MALE", "FEMALE"]
    reason: str = Field(min_length=2, max_length=300)


class AirportIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    boundary: dict
    fee: Decimal = Field(ge=0, le=10000)
    is_active: bool = True
    v_boundary = field_validator("boundary")(lambda cls, v: _check_boundary(v))


class AirportPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=100)
    boundary: dict | None = None
    fee: Decimal | None = Field(default=None, ge=0, le=10000)
    is_active: bool | None = None
    v_boundary = field_validator("boundary")(lambda cls, v: _check_boundary(v))


class AccountDeleteIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


class ReinstateIn(BaseModel):
    reason: str = Field(min_length=2, max_length=500)
    fee_received: bool = False  # True = fee already collected now. False = driver: fee becomes a debt (negative balance)
    payment_reference: str | None = Field(default=None, min_length=2, max_length=100)


class DebtSettleIn(BaseModel):
    amount: Decimal = Field(gt=0, le=100000)
    payment_reference: str = Field(min_length=2, max_length=100)


class AppOpenIn(BaseModel):
    device_id: str = Field(min_length=6, max_length=100)  # random id the app generates once per install
    platform: Literal["android", "ios", "web"] = "android"
    model: str | None = Field(default=None, max_length=120)
    os_name: str | None = Field(default=None, max_length=40)
    os_version: str | None = Field(default=None, max_length=40)
    app_version: str | None = Field(default=None, max_length=30)
    locale: str | None = Field(default=None, max_length=20)
    fcm_token: str | None = Field(default=None, min_length=10, max_length=300)


class LocationConsentIn(BaseModel):
    granted: bool


class LocationSnapshotIn(BaseModel):
    event: Literal["APP_OPEN", "LOGIN", "ORDER_CREATED", "OFFER_ACCEPTED", "DRIVER_ON_THE_WAY", "ARRIVED_AT_PICKUP",
                   "TRIP_STARTED", "TRIP_ARRIVED", "DRIVER_ONLINE"]
    model_config = ConfigDict(populate_by_name=True)
    latitude: float = Field(ge=-90, le=90, validation_alias=AliasChoices("lat", "latitude"))
    longitude: float = Field(ge=-180, le=180, validation_alias=AliasChoices("lng", "longitude"))
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    order_id: int | None = None


class DriverLocationIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    latitude: float = Field(ge=-90, le=90, validation_alias=AliasChoices("lat", "latitude"))
    longitude: float = Field(ge=-180, le=180, validation_alias=AliasChoices("lng", "longitude"))
    accuracy_m: float | None = Field(default=None, ge=0, le=100000)
    speed_kmh: float | None = Field(default=None, ge=0, le=400)
    heading: float | None = Field(default=None, ge=0, le=360)


CUSTOMER_RATING_TAGS = {"LATE", "RUDE", "DAMAGED", "NO_CONTACT", "UNSAFE_DRIVING", "FAST", "POLITE", "CAREFUL", "GOOD_CONTACT"}
DRIVER_RATING_TAGS = {"NOT_READY", "WRONG_ADDRESS", "NO_ANSWER", "RUDE", "READY", "ACCURATE_ADDRESS", "FRIENDLY"}


class RatingIn(BaseModel):
    stars: int = Field(ge=1, le=5)
    tags: list[str] = Field(default_factory=list, max_length=10)
    comment: str | None = Field(default=None, max_length=2000)

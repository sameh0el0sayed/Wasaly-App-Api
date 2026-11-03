from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator

from .constants import AccountStatus, OfferStatus, OrderStatus, ReviewStatus
from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Always store/return timezone-aware UTC datetimes (works on SQLite and PostgreSQL)."""
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    full_name: Mapped[str] = mapped_column(String(120))
    phone: Mapped[str | None] = mapped_column(String(20), unique=True, index=True, nullable=True)  # NULL after anonymization
    cpr_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    cpr_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True, nullable=True)
    cpr_last3: Mapped[str | None] = mapped_column(String(3), nullable=True)
    cpr_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    cpr_expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), index=True)
    account_status: Mapped[str] = mapped_column(String(30), index=True, default=AccountStatus.PENDING_APPROVAL)
    status_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    profile_photo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    cpr_photo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    referral_code: Mapped[str | None] = mapped_column(String(16), unique=True, index=True, nullable=True)
    wallet_balance: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, server_default="0")
    language: Mapped[str] = mapped_column(String(2), default="ar", server_default="ar")  # notification language (ar | en)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    token_version: Mapped[int] = mapped_column(Integer, default=0)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)  # any authenticated request
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_login_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    login_count: Mapped[int] = mapped_column(Integer, default=0)
    app_open_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_suspend_count: Mapped[int] = mapped_column(Integer, default=0)
    rating_reset_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)  # 1-star counting restarts after an admin reactivation
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    deleted_prev_status: Mapped[str | None] = mapped_column(String(30), nullable=True)  # restored to this by an admin
    anonymized_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    location_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    location_consent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    driver_profile: Mapped["DriverProfile | None"] = relationship(
        back_populates="user", uselist=False, lazy="selectin")
    vehicle: Mapped["Vehicle | None"] = relationship(
        back_populates="driver", uselist=False, lazy="selectin")
    roles: Mapped[list | None] = mapped_column(JSON, nullable=True)  # roles granted to the account; `role` is the ACTIVE one
    driver_application: Mapped["DriverApplication | None"] = relationship(
        "DriverApplication", primaryjoin="User.id == DriverApplication.user_id", foreign_keys="DriverApplication.user_id",
        uselist=False, lazy="selectin", viewonly=True)


class DriverProfile(Base):
    __tablename__ = "driver_profiles"
    gender: Mapped[str | None] = mapped_column(String(8), nullable=True)  # MALE | FEMALE, set at registration, changed by admin only
    license_issue_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    smoking_allowed: Mapped[bool] = mapped_column(Boolean, default=False)  # driver's car allows smoking
    daily_goal: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, server_default="0")  # earnings goal per day (0 = none)
    debt_balance: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # owed to the company; deducted from payouts before any transfer
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    license_number_encrypted: Mapped[str] = mapped_column(Text)
    license_expiry_date: Mapped[date] = mapped_column(Date)
    license_document: Mapped[str | None] = mapped_column(String(255), nullable=True)
    license_document_back: Mapped[str | None] = mapped_column(String(255), nullable=True)
    has_open_corrections: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    driver_status: Mapped[str] = mapped_column(String(30), default=AccountStatus.PENDING_APPROVAL)
    payout_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # IBAN | BENEFITPAY
    payout_account_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    payout_holder_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    mode: Mapped[str] = mapped_column(String(10), default="OFF")  # OFF | NORMAL (offers, no location) | LIVE (offers + shares location)
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)  # only while LIVE; cleared otherwise. No history is kept.
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_location_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    last_speed_kmh: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_heading: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    user: Mapped[User] = relationship(back_populates="driver_profile")


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    make: Mapped[str] = mapped_column(String(60))
    model: Mapped[str] = mapped_column(String(60))
    year: Mapped[int] = mapped_column(Integer)
    color: Mapped[str] = mapped_column(String(30))
    plate_number: Mapped[str] = mapped_column(String(20), unique=True)
    vehicle_type: Mapped[str] = mapped_column(String(30), default="SEDAN")
    passenger_capacity: Mapped[int] = mapped_column(Integer)
    luggage_capacity: Mapped[int] = mapped_column(Integer, default=0)
    cargo_capacity_kg: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    has_ac: Mapped[bool] = mapped_column(Boolean, default=True)
    is_comfortable: Mapped[bool] = mapped_column(Boolean, default=True)
    photos: Mapped[dict] = mapped_column(JSON, default=dict)  # front/back/left/right/interior/trunk
    vehicle_status: Mapped[str] = mapped_column(String(30), default=AccountStatus.PENDING_APPROVAL)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    driver: Mapped[User] = relationship(back_populates="vehicle")
    documents: Mapped[list["VehicleDocument"]] = relationship(lazy="selectin", order_by="VehicleDocument.id")


class VehicleDocument(Base):
    __tablename__ = "vehicle_documents"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"), index=True)
    document_type: Mapped[str] = mapped_column(String(40))
    file_path: Mapped[str] = mapped_column(String(255))
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    review_status: Mapped[str] = mapped_column(String(20), default=ReviewStatus.PENDING)
    review_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ServiceArea(Base):
    __tablename__ = "service_areas"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    # {"type":"polygon","points":[[lat,lng],...]} or {"type":"circle","center":[lat,lng],"radius_m":1500}
    boundary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PromoCode(Base):
    """Percentage discount on the trip price, paid by the platform (driver earnings are unchanged)."""
    __tablename__ = "promo_codes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(40), unique=True, index=True)
    percent: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    min_order_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class ProfileChange(Base):
    """An admin-reviewed data change. kind=CORRECTION: admin asked the user to fix a field (status OPEN -> SUBMITTED ->
    ACCEPTED). kind=CHANGE: the user asked to change a sensitive field (PENDING_REVIEW -> ACCEPTED | REJECTED).
    The account is never locked by either; the new value is only applied when an admin accepts it."""
    __tablename__ = "profile_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(12))  # CORRECTION | CHANGE
    field: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True)
    new_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    reviewed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Banner(Base):
    """Home-screen banner shown in the apps (admin managed)."""
    __tablename__ = "banners"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title_ar: Mapped[str] = mapped_column(String(160))
    title_en: Mapped[str | None] = mapped_column(String(160), nullable=True)
    subtitle_ar: Mapped[str | None] = mapped_column(String(300), nullable=True)
    subtitle_en: Mapped[str | None] = mapped_column(String(300), nullable=True)
    image_file_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    bg_color: Mapped[str | None] = mapped_column(String(7), nullable=True)
    action_type: Mapped[str] = mapped_column(String(10), default="NONE")  # NONE | URL | SCREEN
    action_value: Mapped[str | None] = mapped_column(String(300), nullable=True)
    audience: Mapped[str] = mapped_column(String(10), default="ALL")  # CUSTOMER | DRIVER | ALL
    starts_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class SavedPlace(Base):
    __tablename__ = "saved_places"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    label: Mapped[str] = mapped_column(String(5))  # HOME | WORK | OTHER
    name: Mapped[str] = mapped_column(String(100))
    address: Mapped[str] = mapped_column(String(300))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AirportZone(Base):
    """Airport area (same boundary format as service areas). Trips starting or ending inside pay `fee` on top of the normal price."""
    __tablename__ = "airport_zones"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    boundary: Mapped[dict] = mapped_column(JSON)  # {"type":"circle","center":[lat,lng],"radius_m":2500} or polygon
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class DriverServiceArea(Base):
    __tablename__ = "driver_service_areas"
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    service_area_id: Mapped[int] = mapped_column(ForeignKey("service_areas.id"), primary_key=True)


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (UniqueConstraint("customer_id", "client_request_id", name="uq_order_client_request"),
                      Index("ix_orders_status_created", "order_status", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    service_area_id: Mapped[int | None] = mapped_column(ForeignKey("service_areas.id"), nullable=True)
    client_request_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    service_type: Mapped[str] = mapped_column(String(20))
    pickup_address: Mapped[str] = mapped_column(String(300))
    pickup_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    pickup_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_address: Mapped[str] = mapped_column(String(300))
    destination_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    passenger_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    luggage_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cargo_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    cargo_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cargo_size_note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    cargo_weight_kg: Mapped[Decimal | None] = mapped_column(Numeric(8, 2), nullable=True)
    cargo_photo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    receiver_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    receiver_phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    driver_instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    customer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_status: Mapped[str] = mapped_column(String(30), default=OrderStatus.PENDING)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    driver_on_the_way_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    driver_arrived_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    trip_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    trip_arrived_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    actual_duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="BHD")  # snapshot at creation
    currency_decimals: Mapped[int] = mapped_column(Integer, default=3)
    price_per_unit: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    unit_duration_seconds: Mapped[int] = mapped_column(Integer)
    min_price: Mapped[Decimal] = mapped_column(Numeric(12, 3))
    final_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # trip price (time based)
    base_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # time price before night surcharge
    night_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # snapshot
    night_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)  # snapshot
    night_start: Mapped[str] = mapped_column(String(5), default="00:00")  # snapshot (local time)
    night_end: Mapped[str] = mapped_column(String(5), default="06:00")  # snapshot (local time)
    night_applied: Mapped[bool] = mapped_column(Boolean, default=False)
    night_surcharge: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    est_distance_km: Mapped[float | None] = mapped_column(Float, nullable=True)  # estimate shown before the trip (not used for pricing)
    est_duration_min: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # customer preferences (hard requirements for dispatch) + the fixed extras they cost, snapshotted at creation
    pickup_photo: Mapped[str | None] = mapped_column(String(120), nullable=True)  # driver proof: before start-trip
    pickup_photo_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    delivery_photo: Mapped[str | None] = mapped_column(String(120), nullable=True)  # driver proof: before arrived-at-destination
    delivery_photo_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    pref_ac: Mapped[bool] = mapped_column(Boolean, default=False)
    pref_smoking: Mapped[str] = mapped_column(String(12), default="ANY")  # ANY | ALLOWED | NOT_ALLOWED
    pref_driver_gender: Mapped[str] = mapped_column(String(8), default="ANY")  # ANY | FEMALE | MALE
    pref_large_car: Mapped[bool] = mapped_column(Boolean, default=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)  # scheduled trip: dispatch starts shortly before
    scheduled_fee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, server_default="0")
    cancel_fee: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    cancel_fee_payer: Mapped[str | None] = mapped_column(String(10), nullable=True)  # CUSTOMER | DRIVER
    stops_fee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, server_default="0")
    preferred_driver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)  # direct trip with a favourite driver
    choose_driver: Mapped[bool] = mapped_column(Boolean, default=True, server_default=false())  # customer picks among the drivers who respond
    price_increase_percent: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # 0..max over the ORIGINAL estimate (raise / accepted counter-offer)
    price_increase_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # set at trip end
    promo_code: Mapped[str | None] = mapped_column(String(40), nullable=True)  # snapshot: later edits never change old orders
    promo_percent: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    promo_fixed: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # fixed discount (referral code)
    referral_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # referral whose code was used on this order
    extras_fee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # airport + preference fees, part of the trip price
    extras_breakdown: Mapped[list] = mapped_column(JSON, default=list)
    is_outside_bahrain: Mapped[bool] = mapped_column(Boolean, default=False)  # destination outside the country boundary
    outside_driver_percent: Mapped[Decimal] = mapped_column(Numeric(6, 2), default=0)  # snapshot
    outside_driver_cut: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    commission_type: Mapped[str] = mapped_column(String(10), default="PERCENT")  # snapshot
    commission_value: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # snapshot (% or fixed amount)
    commission_mode: Mapped[str] = mapped_column(String(10), default="DEDUCT")  # snapshot: DEDUCT | ADD
    commission: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # company profit
    customer_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # what the customer pays
    driver_earning: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # what the driver gets
    original_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    price_adjusted: Mapped[bool] = mapped_column(Boolean, default=False)
    payment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # TAP | BENEFITPAY (cash is not allowed)
    payment_status: Mapped[str] = mapped_column(String(24), default="UNPAID")
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    cancelled_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    cancelled_by_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    cancellation_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    customer: Mapped[User] = relationship(foreign_keys=[customer_id], lazy="selectin")
    driver: Mapped[User | None] = relationship(foreign_keys=[driver_id], lazy="selectin")


class OrderOffer(Base):
    __tablename__ = "order_offers"
    __table_args__ = (Index("ix_offer_driver_status", "driver_id", "offer_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    offered_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    offer_status: Mapped[str] = mapped_column(String(20), default=OfferStatus.PENDING)
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    order: Mapped[Order] = relationship(lazy="selectin")
    counter: Mapped["CounterOffer | None"] = relationship(lazy="selectin", uselist=False)


class CounterOffer(Base):
    """A driver asks for a higher price on an offer (percent over the ORIGINAL estimate); the customer accepts or rejects."""
    __tablename__ = "counter_offers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    offer_id: Mapped[int] = mapped_column(ForeignKey("order_offers.id"), unique=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    percent: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8), default="COUNTER", server_default="COUNTER")  # COUNTER = asks a higher price | ACCEPT = takes the request at the current price
    status: Mapped[str] = mapped_column(String(10), default="PENDING", index=True)  # PENDING | ACCEPTED | REJECTED | EXPIRED
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    responded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    old_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    new_status: Mapped[str] = mapped_column(String(30))
    changed_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # NULL = system
    changed_by_role: Mapped[str] = mapped_column(String(20), default="SYSTEM")
    changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DriverWorkSession(Base):
    __tablename__ = "driver_work_sessions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    online_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    offline_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    notification_type: Mapped[str] = mapped_column(String(40))
    title: Mapped[str] = mapped_column(String(150))
    message: Mapped[str] = mapped_column(Text)
    related_order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DeviceToken(Base):
    __tablename__ = "device_tokens"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    token: Mapped[str] = mapped_column(String(300), unique=True)
    platform: Mapped[str] = mapped_column(String(20), default="android")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Payment(Base):
    """Money owed by a customer for an order or a subscription period. All money goes to the COMPANY account first."""
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(15), default="ORDER")  # ORDER | SUBSCRIPTION
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), index=True, nullable=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), index=True, nullable=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 3))  # what the customer owes (customer_total), excluding gateway fee
    currency: Mapped[str] = mapped_column(String(3), default="BHD")
    currency_decimals: Mapped[int] = mapped_column(Integer, default=3)
    driver_amount: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # driver share, paid out after confirmation
    company_amount: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # company keeps (commission)
    wallet_amount: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0, server_default="0")  # paid from the customer wallet
    gateway_fee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # Tap fee, paid by the customer
    charged_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # amount + gateway_fee (Tap)
    payment_method: Mapped[str | None] = mapped_column(String(20), nullable=True)  # TAP | BENEFITPAY
    # UNPAID | PENDING_PAYMENT (Tap link issued) | PENDING_VERIFICATION (BenefitPay proof sent) | PAID | REJECTED | FAILED
    payment_status: Mapped[str] = mapped_column(String(24), default="UNPAID", index=True)
    transaction_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)  # Tap charge id / customer transfer ref
    tap_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    proof_file: Mapped[str | None] = mapped_column(String(255), nullable=True)  # BenefitPay screenshot
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class DriverPayout(Base):
    """The driver's share, transferred from the company account after the payment is confirmed."""
    __tablename__ = "driver_payouts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payment_id: Mapped[int] = mapped_column(ForeignKey("payments.id"), unique=True)
    driver_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 3))  # what is actually transferred (after debt_offset)
    debt_offset: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)  # kept by the company against the driver's debt
    currency: Mapped[str] = mapped_column(String(3))
    currency_decimals: Mapped[int] = mapped_column(Integer, default=3)
    status: Mapped[str] = mapped_column(String(15), default="PENDING", index=True)  # PENDING | TRANSFERRED | FAILED | OFFSET
    simulated: Mapped[bool] = mapped_column(Boolean, default=False)  # True = no real money moved
    reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    transferred_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Subscription(Base):
    """Monthly subscription for a fixed route (work / business / school). Holder = an approved customer (individual or parent)."""
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    service_area_id: Mapped[int | None] = mapped_column(ForeignKey("service_areas.id"), nullable=True)
    category: Mapped[str] = mapped_column(String(15))  # WORK | BUSINESS | SCHOOL
    passenger_name: Mapped[str | None] = mapped_column(String(120), nullable=True)  # student / employee name
    school_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    passengers_count: Mapped[int] = mapped_column(Integer, default=1)
    pickup_address: Mapped[str] = mapped_column(String(300))
    pickup_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    pickup_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_address: Mapped[str] = mapped_column(String(300))
    destination_latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    destination_longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    weekdays: Mapped[list] = mapped_column(JSON, default=list)  # 0=Monday .. 6=Sunday
    pickup_time: Mapped[str] = mapped_column(String(5), default="07:00")  # local time HH:MM
    return_trip: Mapped[bool] = mapped_column(Boolean, default=False)
    return_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # REQUESTED -> QUOTED (admin set price+driver) -> ACTIVE (paid) ; PAUSED / EXPIRED / CANCELLED / REJECTED
    status: Mapped[str] = mapped_column(String(15), default="REQUESTED", index=True)
    currency: Mapped[str] = mapped_column(String(3), default="BHD")
    currency_decimals: Mapped[int] = mapped_column(Integer, default=3)
    monthly_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)  # set by admin per route/distance
    commission_type: Mapped[str] = mapped_column(String(10), default="PERCENT")  # snapshot at quote time
    commission_value: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    commission_mode: Mapped[str] = mapped_column(String(10), default="ADD")
    customer_total: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    driver_earning: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    commission: Mapped[Decimal | None] = mapped_column(Numeric(12, 3), nullable=True)
    paid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    starts_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    reject_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class SubscriptionTrip(Base):
    __tablename__ = "subscription_trips"
    __table_args__ = (UniqueConstraint("subscription_id", "trip_date", "leg", name="uq_sub_trip"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), index=True)
    driver_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    trip_date: Mapped[date] = mapped_column(Date, index=True)
    leg: Mapped[str] = mapped_column(String(8), default="GO")  # GO | RETURN
    scheduled_time: Mapped[str] = mapped_column(String(5))
    status: Mapped[str] = mapped_column(String(12), default="SCHEDULED")  # SCHEDULED | COMPLETED | MISSED | CANCELLED
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity_type: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppSetting(Base):
    __tablename__ = "app_settings"
    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Complaint(Base):
    __tablename__ = "complaints"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    # COMPLAINT | DISPUTE | NO_SHOW_DRIVER | NO_SHOW_CUSTOMER | VEHICLE_BREAKDOWN | PASSWORD_RESET | DELETION_REQUEST
    type: Mapped[str] = mapped_column(String(30), default="COMPLAINT")
    subject: Mapped[str] = mapped_column(String(200))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="OPEN")
    admin_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class UploadedFile(Base):
    __tablename__ = "uploaded_files"
    file_key: Mapped[str] = mapped_column(String(120), primary_key=True)
    owner_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    original_name: Mapped[str] = mapped_column(String(200))
    content_type: Mapped[str] = mapped_column(String(80))
    size: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class UserDevice(Base):
    """A phone/tablet the user has used. Reported by the app at start-up (POST /api/app/open)."""
    __tablename__ = "user_devices"
    __table_args__ = (UniqueConstraint("user_id", "device_id", name="uq_user_device"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(100))  # app-generated install id
    platform: Mapped[str] = mapped_column(String(20), default="android")
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(20), nullable=True)
    last_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    open_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LoginEvent(Base):
    __tablename__ = "login_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    success: Mapped[bool] = mapped_column(Boolean, default=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(300), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    new_device: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class LocationSnapshot(Base):
    """A position reported at an important moment (NOT continuous tracking). Needs the user's consent; auto-deleted."""
    __tablename__ = "location_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    event: Mapped[str] = mapped_column(String(30))
    latitude: Mapped[float] = mapped_column(Float)
    longitude: Mapped[float] = mapped_column(Float)
    accuracy_m: Mapped[float | None] = mapped_column(Float, nullable=True)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class Rating(Base):
    """One rating per order per rater: the customer rates the driver, the driver rates the customer."""
    __tablename__ = "ratings"
    __table_args__ = (UniqueConstraint("order_id", "rater_id", name="uq_rating_order_rater"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    rater_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    ratee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    rater_role: Mapped[str] = mapped_column(String(20))
    stars: Mapped[int] = mapped_column(Integer)
    tags: Mapped[list] = mapped_column(JSON, default=list)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class AppErrorLog(Base):
    """Errors reported by the mobile apps (crashes, failed API calls, push/Firebase problems). Kept 30 days."""
    __tablename__ = "app_error_logs"
    __table_args__ = (Index("ix_app_error_level_created", "level", "created_at"),
                      Index("ix_app_error_user_created", "user_id", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(20), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)
    os_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    os_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    locale: Mapped[str | None] = mapped_column(String(20), nullable=True)
    level: Mapped[str] = mapped_column(String(10))  # fatal | error | warning
    source: Mapped[str] = mapped_column(String(40), index=True)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    message: Mapped[str] = mapped_column(Text)
    stack: Mapped[str | None] = mapped_column(Text, nullable=True)
    context: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AppOpenEvent(Base):
    __tablename__ = "app_open_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(30), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)


class SosAlert(Base):
    __tablename__ = "sos_alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    raised_by_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(10))
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="OPEN", index=True)  # OPEN | ESCALATED | RESOLVED
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    escalated_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    resolution: Mapped[str | None] = mapped_column(String(500), nullable=True)


class TripShare(Base):
    __tablename__ = "trip_shares"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    token: Mapped[str] = mapped_column(String(48), unique=True, index=True)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class OrderStop(Base):
    __tablename__ = "order_stops"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    address: Mapped[str] = mapped_column(String(300))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(10), default="PENDING")  # PENDING | ARRIVED | DONE
    fee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    added_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Referral(Base):
    __tablename__ = "referrals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    inviter_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    invitee_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="REGISTERED")  # REGISTERED | REWARDED | REVOKED
    reward_inviter: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    reward_invitee: Mapped[Decimal] = mapped_column(Numeric(12, 3), default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    rewarded_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class WalletEntry(Base):
    __tablename__ = "wallet_entries"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 3))  # + credit / - spend
    kind: Mapped[str] = mapped_column(String(20))  # REFERRAL_INVITER | REFERRAL_INVITEE | REVOKED | SPEND
    ref_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Translation(Base):
    """UI text dictionary. `key` is the Arabic source text exactly as written in the app."""
    __tablename__ = "translations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(500), unique=True)
    ar: Mapped[str] = mapped_column(Text, default="")
    en: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class AppEvent(Base):
    """Product analytics event sent by the apps (no personal data in props)."""
    __tablename__ = "app_events"
    __table_args__ = (Index("ix_app_events_name_created", "name", "created_at"),
                      Index("ix_app_events_user_created", "user_id", "created_at"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    role: Mapped[str] = mapped_column(String(10), default="GUEST")
    device_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(60))
    screen: Mapped[str | None] = mapped_column(String(120), nullable=True)
    props: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(20), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(10), nullable=True)
    client_ts: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class DriverApplication(Base):
    """A customer applying to also drive. One row per user (resubmitting updates it)."""
    __tablename__ = "driver_applications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="PENDING_REVIEW")  # PENDING_REVIEW | APPROVED | REJECTED | NEEDS_CORRECTION
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class Passkey(Base):
    """WebAuthn credential (Face ID / fingerprint / screen lock). credential_id is base64url."""
    __tablename__ = "passkeys"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    credential_id: Mapped[str] = mapped_column(String(512), unique=True)
    public_key: Mapped[bytes] = mapped_column(LargeBinary)
    sign_count: Mapped[int] = mapped_column(Integer, default=0)
    transports: Mapped[list | None] = mapped_column(JSON, nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

"""Driver eligibility, offer dispatch (10 s offers), expiry handling and document enforcement."""
import logging
from datetime import date, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, CounterStatus, OfferStatus, ReviewStatus, Role
from ..constants import OrderStatus as S
from ..models import CounterOffer, DriverProfile, DriverServiceArea, DriverWorkSession, Notification, Order, OrderOffer, User, utcnow
from . import settings_service
from .notifications import notify
from .orders import close_pending_counters, close_pending_offers, transition

log = logging.getLogger("dispatch")


def work_blockers(user: User, today: date | None = None) -> list[str]:
    """Reasons a driver may NOT be online. Empty list = allowed to work."""
    today = today or date.today()
    r: list[str] = []
    p, v = user.driver_profile, user.vehicle
    if user.account_status != AccountStatus.APPROVED:
        r.append("ACCOUNT_NOT_APPROVED")
    if not p or p.driver_status != AccountStatus.APPROVED:
        r.append("DRIVER_NOT_APPROVED")
    if p and p.has_open_corrections:
        r.append("CORRECTIONS_REQUIRED")
    if p and p.license_expiry_date < today:
        r.append("LICENSE_EXPIRED")
    if not v or v.vehicle_status != AccountStatus.APPROVED:
        r.append("VEHICLE_NOT_APPROVED")
    if v:
        latest: dict[str, object] = {}
        for d in v.documents:  # ordered by id -> last wins = latest upload per type
            latest[d.document_type] = d
        for d in latest.values():
            if d.review_status != ReviewStatus.APPROVED:
                r.append(f"DOCUMENT_NOT_APPROVED:{d.document_type}")
            elif d.expiry_date and d.expiry_date < today:
                r.append(f"DOCUMENT_EXPIRED:{d.document_type}")
    return r


async def has_active_order(db: AsyncSession, driver_id: int) -> bool:
    q = select(Order.id).where(Order.driver_id == driver_id, Order.order_status.in_(S.DRIVER_BUSY)).limit(1)
    return (await db.execute(q)).first() is not None


async def eligible_drivers(db: AsyncSession, order: Order) -> list[User]:
    busy = select(Order.driver_id).where(Order.driver_id.is_not(None), Order.order_status.in_(S.DRIVER_BUSY))
    pending = select(OrderOffer.driver_id).where(OrderOffer.order_id == order.id,
                                                 OrderOffer.offer_status.in_([OfferStatus.PENDING, OfferStatus.COUNTERED]))
    stmt = (select(User)
            .join(DriverProfile, DriverProfile.user_id == User.id)
            .join(DriverServiceArea, DriverServiceArea.driver_id == User.id)
            .where(DriverServiceArea.service_area_id == order.service_area_id,
                   User.role == Role.DRIVER, User.account_status == AccountStatus.APPROVED,
                   DriverProfile.is_online.is_(True),
                   User.id.not_in(busy), User.id.not_in(pending),
                   User.id.not_in(select(OrderOffer.driver_id).where(OrderOffer.order_id == order.id, OrderOffer.offer_status == OfferStatus.ACCEPTED))))  # a driver who cancelled is not re-offered
    if order.preferred_driver_id:  # direct trip: only the driver the customer picked
        stmt = stmt.where(User.id == order.preferred_driver_id)
    out = []
    cfg = await settings_service.get_all(db)
    default_gender = cfg["default_driver_gender"]
    large_types = {t.strip().upper() for t in cfg["large_car_vehicle_types"].split(",") if t.strip()}
    for u in (await db.execute(stmt)).scalars().unique().all():
        if work_blockers(u):
            continue
        v, prof = u.vehicle, u.driver_profile
        # customer preferences are hard requirements
        if order.pref_ac and not v.has_ac:
            continue
        if order.pref_smoking == "ALLOWED" and not prof.smoking_allowed:
            continue
        if order.pref_smoking == "NOT_ALLOWED" and prof.smoking_allowed:
            continue
        if order.pref_driver_gender in ("MALE", "FEMALE") and (prof.gender or default_gender) != order.pref_driver_gender:
            continue
        if order.pref_large_car and (v.vehicle_type or "").upper() not in large_types:
            continue
        if order.service_type == "PERSON" and order.passenger_count and order.passenger_count > v.passenger_capacity:
            continue
        if order.service_type == "DELIVERY" and order.cargo_weight_kg and v.cargo_capacity_kg is not None \
                and order.cargo_weight_kg > v.cargo_capacity_kg:
            continue
        out.append(u)
    return out


def rank_by_distance(drivers: list, order: Order, s: dict) -> list:
    """Nearest driver to the pickup first. Only LIVE drivers with a fresh position have a distance; the others (NORMAL
    mode or a stale fix) come after them in their original order. dispatch_max_pickup_km drops LIVE drivers that are too far."""
    from .geo import _haversine_m
    if s["dispatch_nearest_first"] != "true" or order.pickup_latitude is None:
        return drivers
    max_age = timedelta(seconds=int(s["live_location_max_age_seconds"]))
    limit_km = float(s["dispatch_max_pickup_km"])
    now = utcnow()
    scored, rest = [], []
    for d in drivers:
        p = d.driver_profile
        if p.mode == "LIVE" and p.last_lat is not None and p.last_location_at and now - p.last_location_at <= max_age:
            km = _haversine_m(p.last_lat, p.last_lng, order.pickup_latitude, order.pickup_longitude) / 1000
            if limit_km > 0 and km > limit_km:
                continue
            scored.append((km, d))
        else:
            rest.append(d)
    return [d for _, d in sorted(scored, key=lambda x: x[0])] + rest


async def dispatch_order(db: AsyncSession, order: Order) -> int:
    """Creates timed offers (offer_ttl_seconds setting, default 10 s) for every eligible driver in the pickup area. Returns number of offers."""
    drivers = await eligible_drivers(db, order) if order.service_area_id else []
    drivers = rank_by_distance(drivers, order, await settings_service.get_all(db))
    if not drivers:
        if order.order_status != S.NO_DRIVER_AVAILABLE:
            await transition(db, order, S.NO_DRIVER_AVAILABLE, None, "SYSTEM", "No eligible driver online in area")
        await notify(db, order.customer_id, "NO_DRIVER", "لا يوجد سائق متاح",
                     "لم نجد سائقًا حتى الآن، يمكنك إعادة المحاولة أو إلغاء الطلب.", order.id)
        return 0
    s = await settings_service.get_all(db)
    if int(s["offer_max_drivers"]) > 0:
        drivers = drivers[:int(s["offer_max_drivers"])]
    ttl = int(s["offer_ttl_seconds"])
    now = utcnow()
    exp = now + timedelta(seconds=ttl)
    for d in drivers:
        db.add(OrderOffer(order_id=order.id, driver_id=d.id, offered_at=now, expires_at=exp))
        await notify(db, d.id, "NEW_OFFER", "طلب جديد", f"طلب جديد من {order.pickup_address}", order.id)
    if order.order_status != S.OFFERED:
        await transition(db, order, S.OFFERED, None, "SYSTEM", f"Offered to {len(drivers)} driver(s)")
    return len(drivers)


async def expire_offers_and_orders(db: AsyncSession) -> None:
    now = utcnow()
    s_all = await settings_service.get_all(db)
    # 1) offers whose time (offer_ttl_seconds) is over -> EXPIRED (driver's "unaccepted" list)
    expired = (await db.execute(select(OrderOffer).where(OrderOffer.offer_status == OfferStatus.PENDING,
                                                         OrderOffer.expires_at <= now))).scalars().all()
    for o in expired:
        o.offer_status, o.responded_at = OfferStatus.EXPIRED, now
        ttl_text = round((o.expires_at - o.offered_at).total_seconds())
        await notify(db, o.driver_id, "OFFER_EXPIRED", "انتهت مهلة العرض", f"لم يتم قبول الطلب خلال {ttl_text} ثوانٍ.", o.order_id)
    # 1b) counter-offers the customer did not answer in time -> EXPIRED (the driver is told)
    late = (await db.execute(select(CounterOffer).where(CounterOffer.status == CounterStatus.PENDING,
                                                        CounterOffer.expires_at <= now))).scalars().all()
    for order_id in {c.order_id for c in late}:
        await close_pending_counters(db, order_id, CounterStatus.EXPIRED, only_ids=[c.id for c in late if c.order_id == order_id])
    await db.flush()
    # 2) orders still OFFERED with no live offer -> NO_DRIVER_AVAILABLE (customer may retry; order itself NOT cancelled)
    live = select(OrderOffer.id).where(OrderOffer.order_id == Order.id,
                                       OrderOffer.offer_status.in_([OfferStatus.PENDING, OfferStatus.COUNTERED])).exists()
    orphans = (await db.execute(select(Order).where(Order.order_status == S.OFFERED, ~live).with_for_update())).scalars().all()
    for order in orphans:
        await transition(db, order, S.NO_DRIVER_AVAILABLE, None, "SYSTEM", "All offers expired")
        await notify(db, order.customer_id, "NO_DRIVER", "لا يوجد سائق متاح",
                     "لم يقبل أي سائق الطلب حتى الآن. يمكنك إعادة المحاولة.", order.id)
    # 2b) scheduled trips whose dispatch time has come
    lead = timedelta(minutes=int(s_all["scheduled_dispatch_minutes_before"]))
    due = (await db.execute(select(Order).where(Order.order_status == S.SCHEDULED, Order.scheduled_at <= now + lead).with_for_update())).scalars().all()
    for order in due:
        await notify(db, order.customer_id, "SCHEDULED_DISPATCHING", "نبحث لك عن سائق", "حان موعد رحلتك المجدولة، نبحث عن سائق الآن.", order.id)
        await dispatch_order(db, order)
    remind = (await db.execute(select(Order).where(Order.order_status == S.SCHEDULED, Order.scheduled_at <= now + timedelta(minutes=60),
                                                   Order.scheduled_at > now + lead))).scalars().all()
    for order in remind:
        if not (await db.execute(select(Notification.id).where(Notification.related_order_id == order.id,
                                                               Notification.notification_type == "SCHEDULED_REMINDER").limit(1))).first():
            await notify(db, order.customer_id, "SCHEDULED_REMINDER", "تذكير برحلتك", "رحلتك المجدولة بعد أقل من ساعة.", order.id)
    # 3) orders that stayed driver-less longer than the configured timeout -> EXPIRED
    timeout = int((await settings_service.get_all(db))["order_timeout_seconds"])
    cutoff = now - timedelta(seconds=timeout)
    stale = (await db.execute(select(Order).where(Order.order_status.in_(S.SEARCHING),
                                                  or_(and_(Order.scheduled_at.is_(None), Order.created_at <= cutoff),
                                                      and_(Order.scheduled_at.is_not(None), Order.scheduled_at <= cutoff))))).scalars().all()
    for order in stale:
        await close_pending_offers(db, order.id)
        await transition(db, order, S.EXPIRED, None, "SYSTEM", "Order timeout")
        await notify(db, order.customer_id, "ORDER_EXPIRED", "انتهت صلاحية الطلب",
                     "انتهت مهلة الطلب دون إسناده لسائق.", order.id)


async def force_offline(db: AsyncSession, driver_id: int) -> None:
    await db.execute(update(DriverProfile).where(DriverProfile.user_id == driver_id).values(
        is_online=False, mode="OFF", last_lat=None, last_lng=None, last_location_at=None, last_speed_kmh=None, last_heading=None))
    await db.execute(update(DriverWorkSession).where(DriverWorkSession.driver_id == driver_id,
                                                     DriverWorkSession.offline_at.is_(None))
                     .values(offline_at=utcnow()))
    await db.execute(update(OrderOffer).where(OrderOffer.driver_id == driver_id,
                                              OrderOffer.offer_status.in_([OfferStatus.PENDING, OfferStatus.COUNTERED]))
                     .values(offer_status=OfferStatus.CLOSED, responded_at=utcnow()))
    await db.execute(update(CounterOffer).where(CounterOffer.driver_id == driver_id, CounterOffer.status == CounterStatus.PENDING)
                     .values(status=CounterStatus.EXPIRED, responded_at=utcnow()))


async def enforce_driver_documents(db: AsyncSession, warn: bool = False) -> None:
    """Takes drivers with expired license/docs offline; optionally sends expiry warnings (once per day)."""
    drivers = (await db.execute(select(User).join(DriverProfile, DriverProfile.user_id == User.id)
                                .where(User.role == Role.DRIVER, User.account_status == AccountStatus.APPROVED))
               ).scalars().unique().all()
    warn_days = int((await settings_service.get_all(db))["doc_expiry_warning_days"])
    today = date.today()
    for u in drivers:
        p = u.driver_profile
        blockers = [b for b in work_blockers(u) if "EXPIRED" in b]
        if p.is_online and blockers:
            await force_offline(db, u.id)
            await notify(db, u.id, "DOCUMENT_EXPIRED", "مستند منتهي", "تم إيقاف عملك حتى تجديد المستند واعتماده.")
        if warn:
            soon = []
            if today <= p.license_expiry_date <= today + timedelta(days=warn_days):
                soon.append("license")
            for d in (u.vehicle.documents if u.vehicle else []):
                if d.expiry_date and today <= d.expiry_date <= today + timedelta(days=warn_days):
                    soon.append(d.document_type)
            if soon:
                since = utcnow() - timedelta(hours=24)
                dup = (await db.execute(select(Notification.id).where(
                    Notification.user_id == u.id, Notification.notification_type == "DOCUMENT_EXPIRING",
                    Notification.created_at >= since).limit(1))).first()
                if not dup:
                    await notify(db, u.id, "DOCUMENT_EXPIRING", "تنبيه انتهاء مستند",
                                 "ستنتهي صلاحية: " + ", ".join(soon))

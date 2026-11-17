"""Unified services (Pickup / Freight / Recovery): catalog, quotes, booking, provider authorisation and admin management.
Rides keep their own flow and pricing. Prices come only from the admin-editable pricing profiles."""
from datetime import timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, customer_user, driver_user
from ..models import (
    Order,
    OrderStatusHistory,
    PricingProfile,
    ProviderCategory,
    ServiceCategory,
    ServiceQuote,
    User,
    VehicleCategory,
    utcnow,
)
from ..services import settings_service
from ..services.audit import audit
from ..services.catalog import is_priced, price_service
from ..services.dispatch import dispatch_order
from ..services.estimate import route_estimate
from ..services.geo import find_area
from ..services.notifications import notify
from .orders import _check_schedule

router = APIRouter(prefix="/api/services", tags=["services-v30"])
admin_router = APIRouter(prefix="/api/admin/services", tags=["services-v30-admin"])
NEW = ("PICKUP", "FREIGHT", "RECOVERY")


class Stop(BaseModel):
    address: str = Field(max_length=300)
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class QuoteIn(BaseModel):
    vehicle_category_id: int
    pickup_address: str = Field(min_length=2, max_length=300)
    pickup_latitude: float = Field(ge=-90, le=90)
    pickup_longitude: float = Field(ge=-180, le=180)
    destination_address: str = Field(min_length=2, max_length=300)
    destination_latitude: float = Field(ge=-90, le=90)
    destination_longitude: float = Field(ge=-180, le=180)
    extra_stops: list[Stop] = Field(default_factory=list, max_length=5)
    scheduled_at: str | None = None
    contact_phone: str | None = Field(default=None, max_length=20)
    receiver_name: str | None = Field(default=None, max_length=120)
    receiver_phone: str | None = Field(default=None, max_length=20)
    notes: str | None = Field(default=None, max_length=1000)
    photos: list[str] = Field(default_factory=list, max_length=8)
    # cargo (Pickup / Freight)
    webhook_url: str | None = Field(default=None, max_length=500)  # merchant API only: called when a manual quote gets its price
    cargo_description: str | None = Field(default=None, max_length=1000)
    cargo_type: str | None = Field(default=None, max_length=60)
    items_count: int | None = Field(default=None, ge=1, le=1000)
    cargo_weight_kg: Decimal | None = Field(default=None, ge=0)
    cargo_length_cm: int | None = Field(default=None, ge=0)
    cargo_width_cm: int | None = Field(default=None, ge=0)
    cargo_height_cm: int | None = Field(default=None, ge=0)
    loading_required: bool = False
    unloading_required: bool = False
    helpers_count: int = Field(default=0, ge=0, le=10)
    equipment: list[str] = Field(default_factory=list, max_length=10)
    special_handling: str | None = Field(default=None, max_length=500)
    permits_note: str | None = Field(default=None, max_length=500)  # freight: permits / documents required
    # recovery
    vehicle_make_model: str | None = Field(default=None, max_length=120)
    vehicle_condition: str | None = Field(default=None, max_length=300)
    can_move: bool | None = None
    emergency: bool = False


def _vc(v: VehicleCategory) -> dict:
    return {"id": v.id, "code": v.code, "name_ar": v.name_ar, "name_en": v.name_en, "max_weight_kg": float(v.max_weight_kg) if v.max_weight_kg else None,
            "max_length_cm": v.max_length_cm, "max_width_cm": v.max_width_cm, "max_height_cm": v.max_height_cm, "equipment": v.equipment or []}


@router.get("")
async def catalog(db: AsyncSession = Depends(get_db)):
    """Main service-selection screen: the active categories with their vehicle categories. No prices here."""
    cats = (await db.execute(select(ServiceCategory).where(ServiceCategory.is_active.is_(True)).order_by(ServiceCategory.sort_order))).scalars().all()
    vcs = (await db.execute(select(VehicleCategory).where(VehicleCategory.is_active.is_(True)).order_by(VehicleCategory.id))).scalars().all()
    return {"items": [{"code": c.code, "name_ar": c.name_ar, "name_en": c.name_en, "config": c.config or {},
                       "vehicle_categories": [_vc(v) for v in vcs if v.service_code == c.code]} for c in cats]}


def _check_capacity(v: VehicleCategory, b: QuoteIn) -> None:
    if v.max_weight_kg and b.cargo_weight_kg and b.cargo_weight_kg > v.max_weight_kg:
        raise HTTPException(422, {"code": "VEHICLE_TOO_SMALL", "field": "cargo_weight_kg"})
    for val, lim, f in ((b.cargo_length_cm, v.max_length_cm, "cargo_length_cm"), (b.cargo_width_cm, v.max_width_cm, "cargo_width_cm"),
                        (b.cargo_height_cm, v.max_height_cm, "cargo_height_cm")):
        if val and lim and val > lim:
            raise HTTPException(422, {"code": "VEHICLE_TOO_SMALL", "field": f})
    missing = [e for e in b.equipment if e not in (v.equipment or [])]
    if missing:
        raise HTTPException(422, {"code": "EQUIPMENT_NOT_AVAILABLE", "equipment": missing})


@router.post("/{code}/quote", status_code=201)
async def create_quote(code: str, body: QuoteIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    code = code.upper()
    svc = await db.get(ServiceCategory, code)
    if code not in NEW or not svc:
        raise HTTPException(404, {"code": "SERVICE_NOT_FOUND"})
    if not svc.is_active:
        raise HTTPException(409, {"code": "SERVICE_DISABLED"})
    v = await db.get(VehicleCategory, body.vehicle_category_id)
    if not v or v.service_code != code or not v.is_active:
        raise HTTPException(422, {"code": "INVALID_VEHICLE_CATEGORY"})
    _check_capacity(v, body)
    if body.scheduled_at:
        from datetime import UTC, datetime
        at = datetime.fromisoformat(body.scheduled_at.replace("Z", "+00:00"))
        _check_schedule(await settings_service.get_all(db), at if at.tzinfo else at.replace(tzinfo=UTC))
    area = await find_area(db, body.pickup_latitude, body.pickup_longitude)
    if not area:
        raise HTTPException(422, {"code": "OUTSIDE_SERVICE_AREA"})
    prof = await db.get(PricingProfile, code)
    params = (prof.params if prof else {}) or {}
    s = await settings_service.get_all(db)
    pts = [(body.pickup_latitude, body.pickup_longitude), *[(x.latitude, x.longitude) for x in body.extra_stops],
           (body.destination_latitude, body.destination_longitude)]
    km, minutes = 0.0, 0
    for a, b in zip(pts, pts[1:], strict=False):
        k, m = await route_estimate(a[0], a[1], b[0], b[1], s["estimate_road_factor"], s["estimate_avg_speed_kmh"])
        km, minutes = km + k, minutes + m
    km = round(km, 1)
    req = body.model_dump(mode="json")
    req.update({"distance_km": km, "duration_min": minutes, "service_area_id": area.id})
    manual = (not is_priced(params) or bool(params.get("always_quote"))
              or (params.get("quote_over_weight_kg") and body.cargo_weight_kg and body.cargo_weight_kg > Decimal(str(params["quote_over_weight_kg"])))
              or (params.get("quote_over_km") and km > float(params["quote_over_km"])))
    pset = await settings_service.get_pricing(db)
    q = ServiceQuote(customer_id=user.id, service_code=code, vehicle_category_id=v.id, request=req, currency=pset.currency)
    if manual:
        q.status = "MANUAL_PENDING"
    else:
        q.price, q.breakdown = price_service(params, Decimal(v.price_multiplier or 1), km=km, minutes=minutes, decimals=pset.decimals,
                                             loading=body.loading_required, unloading=body.unloading_required, helpers=body.helpers_count,
                                             extra_stops=len(body.extra_stops), equipment=body.equipment, emergency=body.emergency)
        q.status = "QUOTED"
        q.expires_at = utcnow() + timedelta(minutes=int(params.get("quote_valid_minutes") or 30))
    db.add(q)
    await db.flush()
    if manual:
        from ..services.payments import notify_admins
        await notify_admins(db, "QUOTE_REQUEST", "طلب عرض سعر جديد", f"{svc.name_ar} - طلب #{q.id}")
    await db.commit()
    return _quote_out(q)


def _quote_out(q: ServiceQuote) -> dict:
    return {"id": q.id, "service_code": q.service_code, "vehicle_category_id": q.vehicle_category_id, "status": q.status, "is_estimate": False,
            "price": f"{q.price:.3f}" if q.price is not None else None, "currency": q.currency, "breakdown": q.breakdown or [],
            "distance_km": (q.request or {}).get("distance_km"), "duration_min": (q.request or {}).get("duration_min"),
            "expires_at": q.expires_at.isoformat() if q.expires_at else None, "order_id": q.order_id, "admin_note": q.admin_note,
            "note": "السعر المعروض هو السعر النهائي المتفق عليه عند التأكيد. أي تغيير يحتاج موافقتك."}


async def _mine(db, user: User, quote_id: int) -> ServiceQuote:
    q = await db.get(ServiceQuote, quote_id)
    if not q or q.customer_id != user.id:
        raise HTTPException(404, {"code": "QUOTE_NOT_FOUND"})
    return q


@router.get("/quotes/{quote_id}")
async def get_quote(quote_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    q = await _mine(db, user, quote_id)
    if q.status == "QUOTED" and q.expires_at and q.expires_at < utcnow():
        q.status = "EXPIRED"
        await db.commit()
    return _quote_out(q)


@router.post("/quotes/{quote_id}/decline")
async def decline_quote(quote_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    q = await _mine(db, user, quote_id)
    if q.status in ("QUOTED", "MANUAL_PENDING"):
        q.status = "DECLINED"
        await db.commit()
    return _quote_out(q)


@router.post("/quotes/{quote_id}/accept", status_code=201)
async def accept_quote(quote_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """The customer confirms the shown price: only now the booking is created and dispatched."""
    q = (await db.execute(select(ServiceQuote).where(ServiceQuote.id == quote_id).with_for_update())).scalar_one_or_none()
    if not q or q.customer_id != user.id:
        raise HTTPException(404, {"code": "QUOTE_NOT_FOUND"})
    if q.status == "ACCEPTED" and q.order_id:
        return {"order_id": q.order_id}  # idempotent
    if q.status != "QUOTED" or q.price is None:
        raise HTTPException(409, {"code": "QUOTE_NOT_AVAILABLE", "status": q.status})
    if q.expires_at and q.expires_at < utcnow():
        q.status = "EXPIRED"
        await db.commit()
        raise HTTPException(409, {"code": "QUOTE_EXPIRED"})
    svc = await db.get(ServiceCategory, q.service_code)
    if not svc or not svc.is_active:
        raise HTTPException(409, {"code": "SERVICE_DISABLED"})
    open_order = (await db.execute(select(Order.id).where(Order.customer_id == user.id, Order.order_status.not_in(S.FINAL | {S.SCHEDULED})).limit(1))).first()
    if open_order:
        raise HTTPException(409, {"code": "OPEN_ORDER_EXISTS", "order_id": open_order[0]})
    cfg = await settings_service.get_all(db)
    if cfg["maintenance_mode"] == "true":
        raise HTTPException(503, {"code": "MAINTENANCE_MODE"})
    r, p = q.request, await settings_service.get_pricing(db)
    prof = await db.get(PricingProfile, q.service_code)
    from datetime import UTC, datetime
    sched = None
    if r.get("scheduled_at"):
        sched = datetime.fromisoformat(r["scheduled_at"].replace("Z", "+00:00"))
        sched = sched if sched.tzinfo else sched.replace(tzinfo=UTC)
    details = {k: r.get(k) for k in ("cargo_type", "items_count", "cargo_length_cm", "cargo_width_cm", "cargo_height_cm", "loading_required",
                                       "unloading_required", "helpers_count", "equipment", "special_handling", "permits_note", "vehicle_make_model",
                                       "vehicle_condition", "can_move", "emergency", "extra_stops", "photos")}
    order = Order(
        customer_id=user.id, service_area_id=r.get("service_area_id"), service_type="DELIVERY", service_code=q.service_code,
        vehicle_category_id=q.vehicle_category_id, quote_id=q.id, agreed_price=q.price, service_details=details,
        pickup_address=r["pickup_address"], pickup_latitude=r["pickup_latitude"], pickup_longitude=r["pickup_longitude"],
        destination_address=r["destination_address"], destination_latitude=r["destination_latitude"], destination_longitude=r["destination_longitude"],
        contact_phone=r.get("contact_phone"), cargo_description=(r.get("cargo_description") or r.get("vehicle_make_model") or svc.name_ar)[:1000],
        cargo_quantity=r.get("items_count"), cargo_weight_kg=r.get("cargo_weight_kg"), receiver_name=r.get("receiver_name"),
        receiver_phone=r.get("receiver_phone"), driver_instructions=r.get("special_handling"), customer_notes=r.get("notes"),
        currency=p.currency, currency_decimals=p.decimals, price_per_unit=p.unit_price, unit_duration_seconds=p.unit_seconds, min_price=p.min_price,
        commission_type=(prof.commission_type if prof and prof.commission_type else p.commission_type),
        commission_value=(prof.commission_value if prof and prof.commission_value is not None else p.commission_value),
        commission_mode=(prof.commission_mode if prof and prof.commission_mode else p.commission_mode),
        est_distance_km=r.get("distance_km"), est_duration_min=r.get("duration_min"), scheduled_at=sched, choose_driver=False)
    db.add(order)
    await db.flush()
    status = S.SCHEDULED if sched else S.PENDING
    order.order_status = status
    db.add(OrderStatusHistory(order_id=order.id, old_status=None, new_status=status, changed_by=user.id, changed_by_role=user.role,
                              notes=f"{q.service_code} booking (quote #{q.id})"))
    q.status, q.order_id = "ACCEPTED", order.id
    await notify(db, user.id, "ORDER_RECEIVED", "تم استلام طلبك", "نبحث لك عن مزوّد مناسب.", order.id)
    if not sched:
        await dispatch_order(db, order)
    await db.commit()
    return {"order_id": order.id, "service_code": q.service_code, "agreed_price": f"{q.price:.3f}", "status": status}


# ---------------- provider authorisation ----------------
class ProviderIn(BaseModel):
    service_code: str
    vehicle_category_id: int
    documents: dict | None = None


@router.post("/provider/apply", status_code=201)
async def provider_apply(body: ProviderIn, user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    """A driver asks to serve a category. Stays PENDING_APPROVAL: unapproved providers never receive bookings of that service."""
    code = body.service_code.upper()
    v = await db.get(VehicleCategory, body.vehicle_category_id)
    if code not in NEW or not v or v.service_code != code:
        raise HTTPException(422, {"code": "INVALID_VEHICLE_CATEGORY"})
    row = (await db.execute(select(ProviderCategory).where(ProviderCategory.driver_id == user.id, ProviderCategory.service_code == code))).scalar_one_or_none()
    if row and row.status == "APPROVED":
        raise HTTPException(409, {"code": "ALREADY_APPROVED"})
    if row:
        row.vehicle_category_id, row.documents, row.status, row.note = v.id, body.documents, "PENDING_APPROVAL", None
    else:
        row = ProviderCategory(driver_id=user.id, service_code=code, vehicle_category_id=v.id, documents=body.documents)
        db.add(row)
    await db.flush()
    from ..services.payments import notify_admins
    await notify_admins(db, "PROVIDER_CATEGORY", "طلب اعتماد مزوّد", f"{user.full_name}: {code}")
    await db.commit()
    return {"id": row.id, "service_code": code, "status": row.status}


@router.get("/provider/mine")
async def provider_mine(user: User = Depends(driver_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProviderCategory).where(ProviderCategory.driver_id == user.id))).scalars().all()
    return {"items": [{"id": r.id, "service_code": r.service_code, "vehicle_category_id": r.vehicle_category_id, "status": r.status, "note": r.note} for r in rows]}


# ---------------- admin ----------------
class CategoryPatch(BaseModel):
    name_ar: str | None = Field(default=None, max_length=80)
    name_en: str | None = Field(default=None, max_length=80)
    is_active: bool | None = None
    sort_order: int | None = None
    config: dict | None = None


class VehiclePatch(BaseModel):
    name_ar: str | None = Field(default=None, max_length=80)
    name_en: str | None = Field(default=None, max_length=80)
    max_weight_kg: Decimal | None = None
    max_length_cm: int | None = None
    max_width_cm: int | None = None
    max_height_cm: int | None = None
    equipment: list[str] | None = None
    price_multiplier: Decimal | None = Field(default=None, gt=0)
    is_active: bool | None = None


class VehicleCreate(VehiclePatch):
    service_code: str
    code: str = Field(min_length=2, max_length=30)
    name_ar: str = Field(max_length=80)
    name_en: str = Field(max_length=80)


class ProfileIn(BaseModel):
    params: dict
    commission_type: str | None = Field(default=None, pattern="^(PERCENT|FIXED)$")
    commission_value: Decimal | None = Field(default=None, ge=0)
    commission_mode: str | None = Field(default=None, pattern="^(ADD|DEDUCT)$")


class PriceIn(BaseModel):
    price: Decimal = Field(gt=0)
    note: str | None = Field(default=None, max_length=300)
    valid_minutes: int = Field(default=120, ge=5, le=10080)


@admin_router.get("")
async def admin_overview(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    cats = (await db.execute(select(ServiceCategory).order_by(ServiceCategory.sort_order))).scalars().all()
    vcs = (await db.execute(select(VehicleCategory).order_by(VehicleCategory.id))).scalars().all()
    profs = {p.service_code: p for p in (await db.execute(select(PricingProfile))).scalars()}
    return {"items": [{"code": c.code, "name_ar": c.name_ar, "name_en": c.name_en, "is_active": c.is_active, "sort_order": c.sort_order, "config": c.config or {},
                       "vehicle_categories": [{**_vc(v), "price_multiplier": float(v.price_multiplier), "is_active": v.is_active} for v in vcs if v.service_code == c.code],
                       "pricing": ({"params": profs[c.code].params, "commission_type": profs[c.code].commission_type,
                                    "commission_value": float(profs[c.code].commission_value) if profs[c.code].commission_value is not None else None,
                                    "commission_mode": profs[c.code].commission_mode} if c.code in profs else None)} for c in cats]}


@admin_router.patch("/{code}")
async def admin_patch_category(code: str, body: CategoryPatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(ServiceCategory, code.upper())
    if not c:
        raise HTTPException(404, "Service not found")
    data = body.model_dump(exclude_unset=True)
    if data.get("is_active") and c.code in NEW:  # a service cannot go live without a price model
        prof = await db.get(PricingProfile, c.code)
        if not prof or not is_priced(prof.params or {}):
            raise HTTPException(409, {"code": "PRICING_NOT_CONFIGURED"})
    for k, v in data.items():
        setattr(c, k, v)
    audit(db, admin.id, "SERVICE_UPDATED", "service", c.code, details=data)
    await db.commit()
    return {"ok": True}


@admin_router.put("/{code}/pricing")
async def admin_put_pricing(code: str, body: ProfileIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    code = code.upper()
    if code not in NEW:
        raise HTTPException(404, "Service not found")
    for k, v in body.params.items():
        if k not in ("equipment_fees", "always_quote") and not str(v).replace(".", "", 1).isdigit():
            raise HTTPException(422, {"code": "INVALID_PRICING_VALUE", "field": k})
    prof = await db.get(PricingProfile, code)
    old = dict(prof.params) if prof else None
    if not prof:
        prof = PricingProfile(service_code=code)
        db.add(prof)
    prof.params, prof.commission_type, prof.commission_value, prof.commission_mode = body.params, body.commission_type, body.commission_value, body.commission_mode
    audit(db, admin.id, "SERVICE_PRICING_CHANGED", "service", code, details={"old": old, "new": body.params})
    await db.commit()
    return {"ok": True}


@admin_router.post("/vehicle-categories", status_code=201)
async def admin_create_vehicle(body: VehicleCreate, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    code = body.service_code.upper()
    if code not in NEW or not await db.get(ServiceCategory, code):
        raise HTTPException(404, "Service not found")
    data = body.model_dump(exclude={"service_code"}, exclude_unset=True)
    v = VehicleCategory(service_code=code, **data)
    db.add(v)
    audit(db, admin.id, "VEHICLE_CATEGORY_CREATED", "vehicle_category", code, details=data)
    await db.commit()
    return _vc(v)


@admin_router.patch("/vehicle-categories/{vid}")
async def admin_patch_vehicle(vid: int, body: VehiclePatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    v = await db.get(VehicleCategory, vid)
    if not v:
        raise HTTPException(404, "Vehicle category not found")
    data = body.model_dump(exclude_unset=True)
    for k, val in data.items():
        setattr(v, k, val)
    audit(db, admin.id, "VEHICLE_CATEGORY_UPDATED", "vehicle_category", vid, details={k: str(x) for k, x in data.items()})
    await db.commit()
    return {"ok": True}


@admin_router.get("/providers")
async def admin_providers(status: str | None = "PENDING_APPROVAL", admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    q = select(ProviderCategory, User).join(User, User.id == ProviderCategory.driver_id)
    if status:
        q = q.where(ProviderCategory.status == status)
    rows = (await db.execute(q.order_by(ProviderCategory.id).limit(200))).all()
    return {"items": [{"id": p.id, "driver_id": u.id, "driver_name": u.full_name, "phone": u.phone, "service_code": p.service_code,
                       "vehicle_category_id": p.vehicle_category_id, "status": p.status, "documents": p.documents or {}, "note": p.note} for p, u in rows]}


class ProviderReview(BaseModel):
    approve: bool
    note: str | None = Field(default=None, max_length=300)


@admin_router.post("/providers/{pid}/review")
async def admin_review_provider(pid: int, body: ProviderReview, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(ProviderCategory, pid)
    if not p:
        raise HTTPException(404, "Not found")
    did, code = p.driver_id, p.service_code
    p.status, p.note, p.reviewed_by = ("APPROVED" if body.approve else "REJECTED"), body.note, admin.id
    audit(db, admin.id, "PROVIDER_CATEGORY_REVIEWED", "provider_category", pid, details={"approve": body.approve, "service": code, "driver": did})
    await notify(db, did, "PROVIDER_CATEGORY", "تم اعتماد خدمتك" if body.approve else "تم رفض طلب الخدمة", body.note or code)
    await db.commit()
    return {"ok": True, "status": "APPROVED" if body.approve else "REJECTED"}


@admin_router.get("/quotes")
async def admin_quotes(status: str | None = "MANUAL_PENDING", admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    q = select(ServiceQuote)
    if status:
        q = q.where(ServiceQuote.status == status)
    rows = (await db.execute(q.order_by(ServiceQuote.id.desc()).limit(200))).scalars().all()
    return {"items": [{**_quote_out(x), "customer_id": x.customer_id, "request": x.request} for x in rows]}


@admin_router.post("/quotes/{qid}/price")
async def admin_price_quote(qid: int, body: PriceIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Manual (Freight/complex) quotation: the customer must still approve it."""
    q = await db.get(ServiceQuote, qid)
    if not q or q.status not in ("MANUAL_PENDING", "QUOTED"):
        raise HTTPException(409, {"code": "QUOTE_NOT_PRICEABLE"})
    cid = q.customer_id
    q.price, q.status, q.admin_note = body.price, "QUOTED", body.note
    q.breakdown = [{"key": "MANUAL_QUOTE", "amount": f"{body.price:.3f}"}]
    q.expires_at = utcnow() + timedelta(minutes=body.valid_minutes)
    audit(db, admin.id, "QUOTE_PRICED", "quote", qid, details={"price": str(body.price)})
    from ..services.merchant import queue_quote_event
    await queue_quote_event(db, q)
    await notify(db, cid, "QUOTE_READY", "عرض السعر جاهز", f"{body.price:.3f} {q.currency} - يرجى المراجعة والموافقة.")
    await db.commit()
    return _quote_out(q)


class AssignIn(BaseModel):
    driver_id: int


@admin_router.post("/orders/{order_id}/assign")
async def admin_manual_dispatch(order_id: int, body: AssignIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Manual dispatch (freight / recovery): only an APPROVED provider of this service with a fitting vehicle category."""
    from ..services.dispatch import eligible_drivers
    from ..services.orders import load_order
    o = await load_order(db, order_id, for_update=True)
    if not o or not o.service_code:
        raise HTTPException(404, "Order not found")
    if o.order_status not in (S.PENDING, S.OFFERED, S.NO_DRIVER_AVAILABLE):
        raise HTTPException(409, {"code": "ORDER_NOT_ASSIGNABLE", "status": o.order_status})
    elig = {u.id for u in await eligible_drivers(db, o)}
    if body.driver_id not in elig:
        raise HTTPException(422, {"code": "DRIVER_NOT_ELIGIBLE"})
    from ..models import OrderOffer
    ttl = int((await settings_service.get_all(db))["offer_ttl_seconds"])
    db.add(OrderOffer(order_id=o.id, driver_id=body.driver_id, offered_at=utcnow(), expires_at=utcnow() + timedelta(seconds=max(ttl, 60))))
    if o.order_status != S.OFFERED:
        from ..services.orders import transition
        await transition(db, o, S.OFFERED, admin.id, "ADMIN", f"Manual dispatch to driver {body.driver_id}")
    audit(db, admin.id, "MANUAL_DISPATCH", "order", order_id, details={"driver_id": body.driver_id})
    await notify(db, body.driver_id, "NEW_OFFER", "طلب جديد لك", "أرسلت لك الإدارة طلبًا مباشرًا.", o.id)
    await db.commit()
    return {"ok": True}

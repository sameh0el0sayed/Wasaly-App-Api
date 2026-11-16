"""Merchant delivery API: businesses (Phoenix POS restaurants) request Wasaly drivers; status pushed back by signed webhook."""
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import Merchant, MerchantDelivery, Order, User, utcnow
from ..schemas import EstimateIn, OrderCreateIn
from ..security import encrypt, hash_password
from ..services import merchant as M
from ..services.audit import audit
from ..services.notifications import notify
from ..services.orders import close_pending_offers, load_order, transition
from ..services.ratelimit import limit
from .orders import create_order, estimate

admin_router = APIRouter(prefix="/api/v1/admin", tags=["merchant-admin"])
router = APIRouter(prefix="/api/v1/merchant", tags=["merchant"], dependencies=[Depends(limit("merchant_api", 600, 60))])


def ok(data) -> dict:
    return {"success": True, "data": data}


# ---------------- admin ----------------
class MerchantIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=32)


class MerchantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None


def _m(m: Merchant) -> dict:
    return {"id": m.id, "name": m.name, "phone": m.phone, "is_active": m.is_active, "created_at": m.created_at.isoformat()}


@admin_router.post("/merchants", status_code=201)
async def create_merchant(body: MerchantIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    mid = str(uuid.uuid4())
    user = User(full_name=body.name.strip(), phone="M" + mid.replace("-", "")[:12], password_hash=hash_password(secrets.token_urlsafe(24)),
                role=Role.CUSTOMER, account_status=AccountStatus.APPROVED, phone_verified=True, approved_at=utcnow())
    db.add(user)
    await db.flush()
    api_key, secret = "wk_live_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    m = Merchant(id=mid, name=body.name.strip(), phone=body.phone, user_id=user.id, api_key_hash=M.hash_key(api_key),
                 signing_secret_encrypted=encrypt(secret), created_by=admin.id)
    db.add(m)
    audit(db, admin.id, "MERCHANT_CREATED", "merchant", mid, details={"name": m.name})
    await db.commit()
    return {**_m(m), "api_key": api_key, "signing_secret": secret}  # shown ONCE


@admin_router.post("/merchants/{mid}/rotate-key")
async def rotate_key(mid: str, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    m = await db.get(Merchant, mid)
    if not m:
        raise HTTPException(404, "Merchant not found")
    api_key = "wk_live_" + secrets.token_urlsafe(32)
    m.api_key_hash = M.hash_key(api_key)
    audit(db, admin.id, "MERCHANT_KEY_ROTATED", "merchant", mid)
    await db.commit()
    return {"id": m.id, "api_key": api_key}


@admin_router.get("/merchants")
async def list_merchants(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Merchant).order_by(Merchant.created_at.desc()))).scalars().all()
    return {"items": [_m(m) for m in rows]}


@admin_router.get("/merchants/{mid}")
async def get_merchant(mid: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    m = await db.get(Merchant, mid)
    if not m:
        raise HTTPException(404, "Merchant not found")
    return _m(m)


@admin_router.patch("/merchants/{mid}")
async def patch_merchant(mid: str, body: MerchantPatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    m = await db.get(Merchant, mid)
    if not m:
        raise HTTPException(404, "Merchant not found")
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(m, k, v)
    audit(db, admin.id, "MERCHANT_UPDATED", "merchant", mid, details=data)
    await db.commit()
    return _m(m)


@admin_router.get("/merchants/{mid}/invoice")
async def merchant_invoice(mid: str, month: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Monthly bill: completed deliveries of the month (YYYY-MM, UTC) and the total delivery fees."""
    m = await db.get(Merchant, mid)
    if not m:
        raise HTTPException(404, "Merchant not found")
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as e:
        raise HTTPException(422, "month must be YYYY-MM") from e
    end = start.replace(year=start.year + (start.month == 12), month=1 if start.month == 12 else start.month + 1)
    rows = (await db.execute(select(Order, MerchantDelivery).join(MerchantDelivery, MerchantDelivery.order_id == Order.id)
                             .where(MerchantDelivery.merchant_id == mid, Order.order_status == S.COMPLETED,
                                    Order.completed_at >= start, Order.completed_at < end).order_by(Order.completed_at))).all()
    total = sum((o.customer_total or Decimal(0) for o, _ in rows), Decimal(0))
    return {"merchant": _m(m), "month": month, "count": len(rows), "currency": "BHD", "total": f"{total:.3f}",
            "items": [{"delivery_id": d.id, "external_number": d.external_number, "completed_at": o.completed_at.isoformat(),
                       "fee": f"{(o.customer_total or 0):.3f}"} for o, d in rows]}


# ---------------- merchant API ----------------
async def merchant_auth(x_merchant_key: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> Merchant:
    m = None
    if x_merchant_key:
        m = (await db.execute(select(Merchant).where(Merchant.api_key_hash == M.hash_key(x_merchant_key)))).scalar_one_or_none()
    if not m or not m.is_active:
        raise HTTPException(401, {"code": "INVALID_MERCHANT_KEY"})
    return m


class Point(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    address: str | None = Field(default=None, max_length=300)
    lat: Decimal | None = None
    lng: Decimal | None = None


class DeliveryRequest(BaseModel):
    external_ref: str = Field(min_length=1, max_length=100)
    external_number: str | None = Field(default=None, max_length=60)
    merchant_name: str | None = Field(default=None, max_length=120)
    pickup: Point
    dropoff: Point
    items_summary: str | None = Field(default=None, max_length=500)
    order_total: Decimal | None = None
    currency: str | None = Field(default="BHD", max_length=3)
    notes: str | None = Field(default=None, max_length=500)
    webhook_url: str | None = Field(default=None, max_length=500)


def _coords(r: DeliveryRequest) -> tuple[float, float, float, float]:
    for p, who in ((r.pickup, "pickup"), (r.dropoff, "dropoff")):
        if p.lat is None or p.lng is None:
            raise HTTPException(422, {"code": "NO_COORDINATES", "message": f"{who}.lat and {who}.lng are required"})
    return float(r.pickup.lat), float(r.pickup.lng), float(r.dropoff.lat), float(r.dropoff.lng)


async def _quote(db, user: User, r: DeliveryRequest) -> dict:
    plat, plng, dlat, dlng = _coords(r)
    try:
        return await estimate(EstimateIn(pickup_latitude=plat, pickup_longitude=plng, destination_latitude=dlat, destination_longitude=dlng),
                              user, db, "ar")
    except ValidationError as e:
        raise HTTPException(422, {"code": "INVALID_REQUEST", "message": str(e)[:200]}) from e


@router.get("/me")
async def me(m: Merchant = Depends(merchant_auth)):
    return ok({"id": m.id, "name": m.name})


@router.post("/deliveries/quote")
async def quote(body: DeliveryRequest, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    user = await db.get(User, m.user_id)
    q = await _quote(db, user, body)
    if not q.get("pickup_in_service_area"):
        raise HTTPException(422, {"code": "OUTSIDE_SERVICE_AREA"})
    return ok({"price": q["estimated_total"], "currency": q["currency"], "eta_minutes": q["duration_min"], "distance_km": f"{q['distance_km']}"})


@router.post("/deliveries", status_code=201)
async def create_delivery(body: DeliveryRequest, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    mid, mname, muser_id = m.id, m.name, m.user_id
    # idempotent on external_ref while the previous delivery is not cancelled
    prev = (await db.execute(select(MerchantDelivery).where(MerchantDelivery.merchant_id == mid, MerchantDelivery.external_ref == body.external_ref)
                             .order_by(MerchantDelivery.created_at.desc()))).scalars().first()
    if prev:
        po = await db.get(Order, prev.order_id)
        if po.order_status != S.CANCELLED:
            return ok(await _view(db, prev, po))
    if body.webhook_url and not M.safe_webhook_url(body.webhook_url):
        raise HTTPException(422, {"code": "INVALID_WEBHOOK_URL"})
    user = await db.get(User, muser_id)
    q = await _quote(db, user, body)
    if not q.get("pickup_in_service_area"):
        raise HTTPException(422, {"code": "OUTSIDE_SERVICE_AREA"})
    plat, plng, dlat, dlng = _coords(body)
    label = f"{body.merchant_name or mname} #{body.external_number or body.external_ref[:8]}"
    notes = "الدفع مسدد مسبقًا من الجهة الطالبة، لا تحصّل أي مبلغ من المستلم. / Prepaid by the requester: do NOT collect any cash."
    if body.notes:
        notes = f"{body.notes}\n{notes}"
    try:
        oc = OrderCreateIn(
            service_type="DELIVERY", pickup_address=(body.pickup.address or body.pickup.name or label)[:300], pickup_latitude=plat, pickup_longitude=plng,
            destination_address=(body.dropoff.address or body.dropoff.name or label)[:300], destination_latitude=dlat, destination_longitude=dlng,
            contact_phone=body.pickup.phone, cargo_description=f"{label}: {body.items_summary or '-'}"[:1000], cargo_quantity=1,
            receiver_name=body.dropoff.name, receiver_phone=body.dropoff.phone, driver_instructions=notes[:1000], accept_capacity_terms=True,
            choose_driver=False, client_request_id=f"m:{body.external_ref}"[:64] if not prev else None)
    except ValidationError as e:
        raise HTTPException(422, {"code": "INVALID_REQUEST", "message": str(e)[:300]}) from e
    out = await create_order(oc, user, db)
    md = MerchantDelivery(id=str(uuid.uuid4()), merchant_id=mid, order_id=out["id"], external_ref=body.external_ref,
                          external_number=body.external_number, webhook_url=body.webhook_url, items_summary=body.items_summary,
                          order_total=str(body.order_total) if body.order_total is not None else None, quoted_price=q["estimated_total"])
    db.add(md)
    await db.commit()
    return ok({"id": md.id, "status": "searching", "price": md.quoted_price, "currency": q["currency"], "tracking_url": None,
               "eta_minutes": q["duration_min"]})


async def _view(db, md: MerchantDelivery, order: Order) -> dict:
    d = await M.build_delivery(db, md, order)
    return {"id": d["id"], "status": d["status"], "price": d["price"], "currency": d["currency"], "tracking_url": d["tracking_url"],
            "eta_minutes": d["eta_minutes"], "external_ref": d["external_ref"], "driver": d["driver"]}


async def _mine(db, m: Merchant, delivery_id: str) -> MerchantDelivery:
    md = await db.get(MerchantDelivery, delivery_id)
    if not md or md.merchant_id != m.id:
        raise HTTPException(404, {"code": "DELIVERY_NOT_FOUND"})
    return md


@router.get("/deliveries/{delivery_id}")
async def get_delivery(delivery_id: str, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    md = await _mine(db, m, delivery_id)
    return ok(await M.build_delivery(db, md))


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/deliveries/{delivery_id}/cancel")
async def cancel_delivery(delivery_id: str, body: CancelIn | None = None, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    md = await _mine(db, m, delivery_id)
    muser_id = m.user_id
    o = await load_order(db, md.order_id, for_update=True)
    if o.order_status in S.FINAL:
        return ok({"id": md.id, "status": M.final_status(o)})
    if o.order_status in (S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION):
        raise HTTPException(409, {"code": "CANNOT_CANCEL"})
    reason = (body.reason if body else None) or "Cancelled by the merchant"
    had_driver = o.driver_id
    await close_pending_offers(db, o.id)
    o.cancelled_at, o.cancelled_by, o.cancelled_by_role, o.cancellation_reason = utcnow(), muser_id, Role.CUSTOMER, reason
    await transition(db, o, S.CANCELLED, muser_id, Role.CUSTOMER, reason)
    if had_driver:
        await notify(db, had_driver, "ORDER_CANCELLED", "تم إلغاء الطلب", "ألغت الجهة الطالبة الطلب.", o.id)
    await db.commit()
    return ok({"id": md.id, "status": "cancelled"})

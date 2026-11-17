"""Merchant delivery API: businesses (Phoenix POS restaurants) request Wasaly drivers; status pushed back by signed webhook."""
import secrets
import uuid
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import DriverProfile, Merchant, MerchantDelivery, MerchantKey, Order, ServiceQuote, User, utcnow
from ..schemas import EstimateIn, OrderCreateIn
from ..security import decrypt, encrypt, hash_password
from ..services import merchant as M
from ..services.audit import audit
from ..services.notifications import notify
from ..services.orders import close_pending_offers, load_order, transition
from ..services.ratelimit import _hit, limit
from .orders import create_order, estimate
from .services_v30 import QuoteIn, accept_quote, create_quote, get_quote

admin_router = APIRouter(prefix="/api/v1/admin", tags=["merchant-admin"])
router = APIRouter(prefix="/api/v1/merchant", tags=["merchant"], dependencies=[Depends(limit("merchant_api", 6000, 60))])  # per IP (platforms share IPs); the per-account limit is in merchant_auth
KINDS = ("MERCHANT", "AGGREGATOR", "SHIPPING")
MODES = ("STANDARD", "FIXED", "DISTANCE")


def ok(data) -> dict:
    return {"success": True, "data": data}


# ---------------- admin ----------------
class MerchantIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    kind: str = "MERCHANT"
    pricing_mode: str = "STANDARD"
    fixed_fee: Decimal | None = Field(default=None, ge=0)
    base_fee: Decimal | None = Field(default=None, ge=0)
    per_km: Decimal | None = Field(default=None, ge=0)
    rate_limit_per_min: int = Field(default=600, ge=10, le=20000)


class MerchantPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=32)
    is_active: bool | None = None
    kind: str | None = None
    pricing_mode: str | None = None
    fixed_fee: Decimal | None = Field(default=None, ge=0)
    base_fee: Decimal | None = Field(default=None, ge=0)
    per_km: Decimal | None = Field(default=None, ge=0)
    rate_limit_per_min: int | None = Field(default=None, ge=10, le=20000)


def _m(m: Merchant) -> dict:
    return {"id": m.id, "name": m.name, "phone": m.phone, "is_active": m.is_active, "created_at": m.created_at.isoformat(), "kind": m.kind,
            "pricing_mode": m.pricing_mode, "fixed_fee": None if m.fixed_fee is None else f"{m.fixed_fee:.3f}",
            "base_fee": None if m.base_fee is None else f"{m.base_fee:.3f}", "per_km": None if m.per_km is None else f"{m.per_km:.3f}",
            "rate_limit_per_min": m.rate_limit_per_min}


def _check_cfg(kind, mode, fixed, base, per_km) -> None:
    if kind is not None and kind not in KINDS:
        raise HTTPException(422, f"kind must be one of {KINDS}")
    if mode is not None and mode not in MODES:
        raise HTTPException(422, f"pricing_mode must be one of {MODES}")
    if mode == "FIXED" and fixed is None:
        raise HTTPException(422, "fixed_fee is required for FIXED pricing")
    if mode == "DISTANCE" and (base is None or per_km is None):
        raise HTTPException(422, "base_fee and per_km are required for DISTANCE pricing")


@admin_router.post("/merchants", status_code=201)
async def create_merchant(body: MerchantIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    _check_cfg(body.kind, body.pricing_mode, body.fixed_fee, body.base_fee, body.per_km)
    mid = str(uuid.uuid4())
    user = User(full_name=body.name.strip(), phone="M" + mid.replace("-", "")[:12], password_hash=hash_password(secrets.token_urlsafe(24)),
                role=Role.CUSTOMER, account_status=AccountStatus.APPROVED, phone_verified=True, approved_at=utcnow())
    db.add(user)
    await db.flush()
    api_key, secret = "wk_live_" + secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    m = Merchant(id=mid, name=body.name.strip(), phone=body.phone, user_id=user.id, api_key_hash=M.hash_key(api_key),
                 signing_secret_encrypted=encrypt(secret), created_by=admin.id, kind=body.kind, pricing_mode=body.pricing_mode,
                 fixed_fee=body.fixed_fee, base_fee=body.base_fee, per_km=body.per_km, rate_limit_per_min=body.rate_limit_per_min)
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


class KeyIn(BaseModel):
    label: str | None = Field(default=None, max_length=60)
    sandbox: bool = False  # test key: simulated deliveries, no real orders, no drivers


@admin_router.post("/merchants/{mid}/keys", status_code=201)
async def add_key(mid: str, body: KeyIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """Extra API key (e.g. one per system or environment). Shown once; revoke it alone without touching the others."""
    if not await db.get(Merchant, mid):
        raise HTTPException(404, "Merchant not found")
    raw = ("wk_test_" if body.sandbox else "wk_live_") + secrets.token_urlsafe(32)
    k = MerchantKey(id=str(uuid.uuid4()), merchant_id=mid, key_hash=M.hash_key(raw), label=body.label, is_sandbox=body.sandbox)
    db.add(k)
    audit(db, admin.id, "MERCHANT_KEY_ADDED", "merchant", mid, details={"key_id": k.id, "label": body.label})
    await db.commit()
    return {"key_id": k.id, "label": k.label, "sandbox": k.is_sandbox, "api_key": raw}


@admin_router.get("/merchants/{mid}/keys")
async def list_keys(mid: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(MerchantKey).where(MerchantKey.merchant_id == mid))).scalars().all()
    return {"items": [{"key_id": k.id, "label": k.label, "sandbox": k.is_sandbox, "is_active": k.is_active, "created_at": k.created_at.isoformat()} for k in rows]}


@admin_router.delete("/merchants/{mid}/keys/{kid}")
async def revoke_key(mid: str, kid: str, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    k = await db.get(MerchantKey, kid)
    if not k or k.merchant_id != mid:
        raise HTTPException(404, "Key not found")
    k.is_active = False
    audit(db, admin.id, "MERCHANT_KEY_REVOKED", "merchant", mid, details={"key_id": kid})
    await db.commit()
    return {"ok": True}


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
    _check_cfg(data.get("kind"), data.get("pricing_mode"), data.get("fixed_fee", m.fixed_fee), data.get("base_fee", m.base_fee), data.get("per_km", m.per_km))
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
    sub: dict[str, dict] = {}
    for o, d in rows:
        k = d.sub_merchant_ref or "-"
        x = sub.setdefault(k, {"sub_merchant_ref": d.sub_merchant_ref, "name": d.sub_merchant_name, "count": 0, "total": Decimal(0)})
        x["count"] += 1
        x["total"] += o.customer_total or Decimal(0)
    return {"merchant": _m(m), "month": month, "count": len(rows), "currency": "BHD", "total": f"{total:.3f}",
            "by_sub_merchant": [{**x, "total": f"{x['total']:.3f}"} for x in sub.values()],
            "items": [{"delivery_id": d.id, "external_number": d.external_number, "sub_merchant_ref": d.sub_merchant_ref, "completed_at": o.completed_at.isoformat(),
                       "fee": f"{(o.customer_total or 0):.3f}"} for o, d in rows]}


# ---------------- merchant API ----------------
async def merchant_auth(request: Request, x_merchant_key: str | None = Header(default=None), db: AsyncSession = Depends(get_db)) -> Merchant:
    m, sandbox = None, False
    if x_merchant_key:
        h = M.hash_key(x_merchant_key)
        m = (await db.execute(select(Merchant).where(Merchant.api_key_hash == h))).scalar_one_or_none()
        if not m:
            row = (await db.execute(select(Merchant, MerchantKey.is_sandbox).join(MerchantKey, MerchantKey.merchant_id == Merchant.id).where(
                MerchantKey.key_hash == h, MerchantKey.is_active.is_(True)))).first()
            m, sandbox = (row[0], row[1]) if row else (None, False)
    if not m or not m.is_active:
        raise HTTPException(401, {"code": "INVALID_MERCHANT_KEY"})
    if await _hit(f"rl:merchant:{m.id}", 60) > m.rate_limit_per_min:
        raise HTTPException(429, {"code": "RATE_LIMITED", "retry_after_seconds": 60}, headers={"Retry-After": "60"})
    request.state.sandbox = sandbox
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
    merchant_name: str | None = Field(default=None, max_length=120)  # restaurant / branch name (platforms)
    sub_merchant_ref: str | None = Field(default=None, max_length=100)  # restaurant / branch id on the platform: the invoice is split by it
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


def _sbx(request: Request) -> bool:
    return bool(getattr(request.state, "sandbox", False))


def _sbx_state(delivery_id: str) -> dict:
    """Simulated delivery: the id carries its creation time, so no data is stored. searching 0-30 s, assigned 30-60 s, picked_up 60-120 s, delivered after."""
    try:
        _, ref, ts, cancelled = delivery_id.split("~")
        age = datetime.now(UTC).timestamp() - int(ts)
    except ValueError as e:
        raise HTTPException(404, {"code": "DELIVERY_NOT_FOUND"}) from e
    st = "cancelled" if cancelled == "1" else "searching" if age < 30 else "assigned" if age < 60 else "picked_up" if age < 120 else "delivered"
    drv = {"name": "Test Driver", "phone": "+97300000000", "vehicle": "Test Car 000000"} if st in ("assigned", "picked_up", "delivered") else None
    return {"id": delivery_id, "external_ref": ref, "status": st, "driver": drv, "price": "1.500", "currency": "BHD", "tracking_url": None, "eta_minutes": 10, "sandbox": True}


def custom_price(m: Merchant, km) -> Decimal | None:
    """Negotiated price of this account (None = standard Wasaly delivery price)."""
    if m.pricing_mode == "FIXED" and m.fixed_fee is not None:
        return m.fixed_fee.quantize(Decimal("0.001"))
    if m.pricing_mode == "DISTANCE" and m.base_fee is not None and m.per_km is not None:
        v = m.base_fee + m.per_km * Decimal(str(km or 0))
        return max(v, m.fixed_fee or Decimal(0)).quantize(Decimal("0.001"))
    return None


@router.get("/me")
async def me(m: Merchant = Depends(merchant_auth)):
    return ok({"id": m.id, "name": m.name, "kind": m.kind, "pricing_mode": m.pricing_mode})


@router.post("/deliveries/quote")
async def quote(body: DeliveryRequest, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    if _sbx(request):
        return ok({"price": "1.500", "currency": "BHD", "eta_minutes": 10, "distance_km": "3.0", "sandbox": True})
    user = await db.get(User, m.user_id)
    q = await _quote(db, user, body)
    if not q.get("pickup_in_service_area"):
        raise HTTPException(422, {"code": "OUTSIDE_SERVICE_AREA"})
    cp = custom_price(m, q["distance_km"])
    return ok({"price": f"{cp:.3f}" if cp is not None else q["estimated_total"], "currency": q["currency"], "eta_minutes": q["duration_min"], "distance_km": f"{q['distance_km']}"})


@router.post("/deliveries", status_code=201)
async def create_delivery(body: DeliveryRequest, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    if _sbx(request):
        return ok(_sbx_state(f"sbx~{body.external_ref[:60]}~{int(datetime.now(UTC).timestamp())}~0"))
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
    cp = custom_price(m, q["distance_km"])
    if cp is not None:  # negotiated price: fixed for the driver's completion (same mechanism as agreed service prices)
        (await load_order(db, out["id"])).agreed_price = cp
        q["estimated_total"] = f"{cp:.3f}"
    md = MerchantDelivery(id=str(uuid.uuid4()), merchant_id=mid, order_id=out["id"], external_ref=body.external_ref,
                          sub_merchant_ref=body.sub_merchant_ref, sub_merchant_name=body.merchant_name,
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
async def get_delivery(delivery_id: str, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    if _sbx(request):
        return ok(_sbx_state(delivery_id))
    md = await _mine(db, m, delivery_id)
    return ok(await M.build_delivery(db, md))


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/deliveries/{delivery_id}/cancel")
async def cancel_delivery(delivery_id: str, request: Request, body: CancelIn | None = None, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    if _sbx(request):
        d = delivery_id.split("~")
        return ok(_sbx_state("~".join(d[:3] + ["1"])) if len(d) == 4 else _sbx_state(delivery_id))
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


class BatchIn(BaseModel):
    deliveries: list[DeliveryRequest] = Field(min_length=1, max_length=20)


@router.post("/deliveries/batch")
async def create_batch(body: BatchIn, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    """Up to 20 deliveries per call; each succeeds or fails on its own (same idempotency on external_ref)."""
    out = []
    for i, d in enumerate(body.deliveries):
        try:
            r = await create_delivery(d, request, m, db)
            out.append({"index": i, "external_ref": d.external_ref, "ok": True, "delivery": r["data"]})
        except HTTPException as e:
            await db.rollback()
            out.append({"index": i, "external_ref": d.external_ref, "ok": False, "error": e.detail})
    return ok({"results": out, "created": sum(1 for r in out if r["ok"])})


# ---------------- services (Pickup / Freight / Recovery) through the same key ----------------
@router.get("/services")
async def merchant_services(m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    from .services_v30 import catalog
    return ok(await catalog(db))


@router.post("/services/{code}/quote", status_code=201)
async def service_quote(code: str, body: QuoteIn, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    """Freight / Pickup / Recovery quote with weight, dimensions, vehicle category... Big or unpriced jobs come back MANUAL_PENDING: poll GET until QUOTED."""
    if _sbx(request):
        raise HTTPException(400, {"code": "SANDBOX_NOT_SUPPORTED", "message": "Use deliveries in sandbox; services need a live key."})
    user = await db.get(User, m.user_id)
    res = await create_quote(code.upper(), body, user, db)
    if body.webhook_url:  # tell the partner when a manual quote gets its price
        if not M.safe_webhook_url(body.webhook_url):
            raise HTTPException(422, {"code": "INVALID_WEBHOOK_URL"})
        q = await db.get(ServiceQuote, res["id"])
        q.request = {**(q.request or {}), "merchant_id": m.id, "webhook_url": body.webhook_url}
        await db.commit()
    return ok(res)


@router.get("/services/quotes/{quote_id}")
async def service_quote_get(quote_id: int, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    if _sbx(request):
        raise HTTPException(400, {"code": "SANDBOX_NOT_SUPPORTED"})
    return ok(await get_quote(quote_id, await db.get(User, m.user_id), db))


class AcceptIn(BaseModel):
    external_ref: str = Field(min_length=1, max_length=100)
    external_number: str | None = Field(default=None, max_length=60)
    sub_merchant_ref: str | None = Field(default=None, max_length=100)
    webhook_url: str | None = Field(default=None, max_length=500)


@router.post("/services/quotes/{quote_id}/accept", status_code=201)
async def service_quote_accept(quote_id: int, body: AcceptIn, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    """Confirm the quoted price: creates the booking and links it to your reference (status webhooks and the monthly invoice work as for deliveries)."""
    if _sbx(request):
        raise HTTPException(400, {"code": "SANDBOX_NOT_SUPPORTED"})
    if body.webhook_url and not M.safe_webhook_url(body.webhook_url):
        raise HTTPException(422, {"code": "INVALID_WEBHOOK_URL"})
    mid, uid = m.id, m.user_id
    prev = (await db.execute(select(MerchantDelivery).where(MerchantDelivery.merchant_id == mid, MerchantDelivery.external_ref == body.external_ref))).scalars().first()
    if prev:
        return ok(await _view(db, prev, await db.get(Order, prev.order_id)))
    res = await accept_quote(quote_id, await db.get(User, uid), db)
    md = MerchantDelivery(id=str(uuid.uuid4()), merchant_id=mid, order_id=res["order_id"], external_ref=body.external_ref,
                          external_number=body.external_number, sub_merchant_ref=body.sub_merchant_ref, webhook_url=body.webhook_url,
                          quoted_price=res.get("agreed_price"))
    db.add(md)
    await db.commit()
    return ok({"id": md.id, "status": "searching", "price": md.quoted_price, "currency": "BHD", "service_code": res.get("service_code")})


@router.get("/deliveries/{delivery_id}/location")
async def delivery_location(delivery_id: str, request: Request, m: Merchant = Depends(merchant_auth), db: AsyncSession = Depends(get_db)):
    """Live driver position for tracking screens (poll every 10-15 s). Null unless the driver is on this delivery and sharing location (LIVE, fresh < 2 min)."""
    if _sbx(request):
        return ok({"lat": 26.2285, "lng": 50.586, "updated_at": datetime.now(UTC).isoformat(), "sandbox": True})
    md = await _mine(db, m, delivery_id)
    o = await db.get(Order, md.order_id)
    if not o.driver_id or o.order_status not in (S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP, S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION):
        return ok(None)
    dp = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == o.driver_id))).scalar_one_or_none()
    if not dp or dp.mode != "LIVE" or dp.last_lat is None or not dp.last_location_at or (utcnow() - dp.last_location_at).total_seconds() > 120:
        return ok(None)
    return ok({"lat": dp.last_lat, "lng": dp.last_lng, "updated_at": dp.last_location_at.isoformat()})


class WebhookTestIn(BaseModel):
    url: str = Field(max_length=500)


@router.post("/webhooks/test")
async def webhook_test(body: WebhookTestIn, m: Merchant = Depends(merchant_auth)):
    """Send one signed sample event to your URL so you can verify the X-Wasaly-Signature check (HMAC-SHA256 of the raw body with your signing secret)."""
    import hashlib
    import hmac
    import json

    import httpx
    if not M.safe_webhook_url(body.url):
        raise HTTPException(422, {"code": "INVALID_WEBHOOK_URL"})
    raw = json.dumps({"event_id": str(uuid.uuid4()), "event": "driver_assigned", "delivery_id": "test", "external_ref": "TEST-1", "status": "assigned",
                      "driver": {"name": "Test Driver", "phone": "+97300000000", "vehicle": "Test Car 000000"}, "price": "1.500", "tracking_url": None,
                      "eta_minutes": 10, "occurred_at": datetime.now(UTC).isoformat(), "test": True}, separators=(",", ":")).encode()
    sig = hmac.new(decrypt(m.signing_secret_encrypted).encode(), raw, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            r = await c.post(body.url, content=raw, headers={"Content-Type": "application/json", "X-Wasaly-Signature": sig})
        return ok({"delivered": 200 <= r.status_code < 300, "http_status": r.status_code})
    except Exception as e:  # noqa: BLE001
        return ok({"delivered": False, "error": str(e)[:200]})

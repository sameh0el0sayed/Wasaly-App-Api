"""Partner portal API: staff of a partner (delivery platform, shipping company, business) sees ONLY its own deliveries, invoices, keys and team.
Staff log in with a normal Wasaly account (POST /auth/login); corporate accounts use /api/corporate/* with the same login."""
import secrets
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, customer_user, super_admin_user
from ..models import (
    Corporate,
    CorporateMember,
    Merchant,
    MerchantDelivery,
    MerchantKey,
    MerchantMember,
    MerchantWebhookEvent,
    Order,
    User,
    utcnow,
)
from ..security import hash_password
from ..services import merchant as M
from ..services.audit import audit
from . import merchant_api as MA

router = APIRouter(prefix="/api/portal", tags=["partner-portal"])
admin_router = APIRouter(prefix="/api/v1/admin/merchants", tags=["partner-portal-admin"])
ROLES = ("OWNER", "MANAGER", "VIEWER")
GROUPS = {"searching": [S.SCHEDULED, S.PENDING, S.OFFERED, S.NO_DRIVER_AVAILABLE], "assigned": [S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP],
          "picked_up": [S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION], "delivered": [S.COMPLETED], "cancelled": [S.CANCELLED], "failed": [S.EXPIRED]}


async def _member(db, mid: str, user: User, need: tuple[str, ...] = ROLES) -> tuple[Merchant, MerchantMember]:
    m = await db.get(Merchant, mid)
    mm = (await db.execute(select(MerchantMember).where(MerchantMember.merchant_id == mid, MerchantMember.user_id == user.id,
                                                        MerchantMember.is_active.is_(True)))).scalar_one_or_none()
    if not m or not mm or mm.role not in need:
        raise HTTPException(403, {"code": "PORTAL_ACCESS_DENIED"})
    return m, mm


def _d(db_row) -> dict:
    o, md = db_row
    return {"id": md.id, "external_ref": md.external_ref, "external_number": md.external_number, "sub_merchant_ref": md.sub_merchant_ref,
            "sub_merchant_name": md.sub_merchant_name, "status": M.final_status(o), "price": None if o.customer_total is None else f"{o.customer_total:.3f}",
            "quoted_price": md.quoted_price, "pickup": o.pickup_address, "dropoff": o.destination_address, "created_at": md.created_at.isoformat(),
            "completed_at": o.completed_at.isoformat() if o.completed_at else None}


@router.get("/me")
async def me(user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    ms = (await db.execute(select(Merchant, MerchantMember.role).join(MerchantMember, MerchantMember.merchant_id == Merchant.id).where(
        MerchantMember.user_id == user.id, MerchantMember.is_active.is_(True), Merchant.is_active.is_(True)))).all()
    cs = (await db.execute(select(Corporate, CorporateMember.role).join(CorporateMember, CorporateMember.corporate_id == Corporate.id).where(
        CorporateMember.user_id == user.id, CorporateMember.is_active.is_(True), Corporate.is_active.is_(True)))).all()
    return {"user": {"id": user.id, "name": user.full_name},
            "partners": [{"id": m.id, "name": m.name, "kind": m.kind, "role": r, "pricing_mode": m.pricing_mode} for m, r in ms],
            "corporates": [{"id": c.id, "name": c.name, "role": r} for c, r in cs]}


@router.get("/merchants/{mid}/summary")
async def summary(mid: str, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user)
    now = datetime.now(UTC)
    day0, month0 = now.replace(hour=0, minute=0, second=0, microsecond=0), now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    base = select(Order.order_status, func.count()).join(MerchantDelivery, MerchantDelivery.order_id == Order.id).where(MerchantDelivery.merchant_id == mid)
    today = dict((await db.execute(base.where(MerchantDelivery.created_at >= day0).group_by(Order.order_status))).all())
    spend = (await db.execute(select(func.count(), func.coalesce(func.sum(Order.customer_total), 0)).join(MerchantDelivery, MerchantDelivery.order_id == Order.id).where(
        MerchantDelivery.merchant_id == mid, Order.order_status == S.COMPLETED, Order.completed_at >= month0))).one()
    active = sum(n for st, n in (await db.execute(base.group_by(Order.order_status))).all() if st not in S.FINAL)
    return {"today": {g: sum(today.get(s, 0) for s in sts) for g, sts in GROUPS.items()}, "active_now": active,
            "month": {"delivered": spend[0], "total": f"{spend[1]:.3f}", "currency": "BHD"}}


@router.get("/merchants/{mid}/deliveries")
async def deliveries(mid: str, status: str | None = None, sub_merchant_ref: str | None = None, q: str | None = None, date_from: str | None = None,
                     date_to: str | None = None, page: int = 1, page_size: int = 25, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user)
    cond = [MerchantDelivery.merchant_id == mid]
    if status:
        if status not in GROUPS:
            raise HTTPException(422, f"status must be one of {list(GROUPS)}")
        cond.append(Order.order_status.in_(GROUPS[status]))
    if sub_merchant_ref:
        cond.append(MerchantDelivery.sub_merchant_ref == sub_merchant_ref)
    if q:
        like = f"%{q.strip()[:60]}%"
        cond.append(MerchantDelivery.external_ref.ilike(like) | MerchantDelivery.external_number.ilike(like))
    try:
        if date_from:
            cond.append(MerchantDelivery.created_at >= datetime.fromisoformat(date_from).replace(tzinfo=UTC))
        if date_to:
            cond.append(MerchantDelivery.created_at < datetime.fromisoformat(date_to).replace(tzinfo=UTC))
    except ValueError as e:
        raise HTTPException(422, "dates must be YYYY-MM-DD") from e
    size, pg = max(1, min(page_size, 100)), max(1, page)
    j = select(Order, MerchantDelivery).join(MerchantDelivery, MerchantDelivery.order_id == Order.id).where(*cond)
    total = (await db.execute(select(func.count()).select_from(j.subquery()))).scalar_one()
    rows = (await db.execute(j.order_by(MerchantDelivery.created_at.desc()).limit(size).offset((pg - 1) * size))).all()
    return {"total": total, "page": pg, "page_size": size, "items": [_d(r) for r in rows]}


@router.get("/merchants/{mid}/deliveries/{did}")
async def delivery(mid: str, did: str, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    m, _ = await _member(db, mid, user)
    md = await db.get(MerchantDelivery, did)
    if not md or md.merchant_id != mid:
        raise HTTPException(404, {"code": "DELIVERY_NOT_FOUND"})
    o = await db.get(Order, md.order_id)
    ev = (await db.execute(select(MerchantWebhookEvent).where(MerchantWebhookEvent.delivery_id == did).order_by(MerchantWebhookEvent.occurred_at))).scalars().all()
    return {**_d((o, md)), "delivery": await M.build_delivery(db, md, o), "items_summary": md.items_summary, "order_total": md.order_total,
            "webhooks": [{"event": M.EVENT_OF.get(e.raw_status), "state": e.state, "attempts": e.attempts, "last_error": e.last_error, "at": e.occurred_at.isoformat()} for e in ev]}


@router.get("/merchants/{mid}/deliveries/{did}/location")
async def delivery_location(mid: str, did: str, request: Request, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    m, _ = await _member(db, mid, user)
    return await MA.delivery_location(did, request, m, db)


class CancelIn(BaseModel):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/merchants/{mid}/deliveries/{did}/cancel")
async def cancel(mid: str, did: str, request: Request, body: CancelIn | None = None, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    m, _ = await _member(db, mid, user, ("OWNER", "MANAGER"))
    return await MA.cancel_delivery(did, request, body, m, db)


@router.get("/merchants/{mid}/invoice")
async def invoice(mid: str, month: str, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER", "MANAGER"))
    return await MA.merchant_invoice(mid, month, user, db)


@router.get("/merchants/{mid}/webhook-log")
async def webhook_log(mid: str, state: str | None = None, limit: int = 50, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER", "MANAGER"))
    q = (select(MerchantWebhookEvent, MerchantDelivery.external_ref).join(MerchantDelivery, MerchantDelivery.id == MerchantWebhookEvent.delivery_id)
         .where(MerchantDelivery.merchant_id == mid).order_by(MerchantWebhookEvent.occurred_at.desc()).limit(min(limit, 200)))
    if state:
        q = q.where(MerchantWebhookEvent.state == state)
    return {"items": [{"event_id": e.id, "external_ref": ref, "event": M.EVENT_OF.get(e.raw_status), "state": e.state, "attempts": e.attempts,
                       "last_error": e.last_error, "at": e.occurred_at.isoformat()} for e, ref in (await db.execute(q)).all()]}


# ---- developers: keys ----
class KeyIn(BaseModel):
    label: str | None = Field(default=None, max_length=60)
    sandbox: bool = False


@router.get("/merchants/{mid}/keys")
async def keys(mid: str, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER",))
    rows = (await db.execute(select(MerchantKey).where(MerchantKey.merchant_id == mid))).scalars().all()
    return {"items": [{"key_id": k.id, "label": k.label, "sandbox": k.is_sandbox, "is_active": k.is_active, "created_at": k.created_at.isoformat()} for k in rows]}


@router.post("/merchants/{mid}/keys", status_code=201)
async def add_key(mid: str, body: KeyIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER",))
    raw = ("wk_test_" if body.sandbox else "wk_live_") + secrets.token_urlsafe(32)
    k = MerchantKey(id=str(uuid.uuid4()), merchant_id=mid, key_hash=M.hash_key(raw), label=body.label, is_sandbox=body.sandbox)
    db.add(k)
    audit(db, user.id, "PORTAL_KEY_ADDED", "merchant", mid, details={"key_id": k.id, "sandbox": body.sandbox})
    await db.commit()
    return {"key_id": k.id, "label": k.label, "sandbox": k.is_sandbox, "api_key": raw}  # shown once


@router.delete("/merchants/{mid}/keys/{kid}")
async def revoke_key(mid: str, kid: str, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER",))
    k = await db.get(MerchantKey, kid)
    if not k or k.merchant_id != mid:
        raise HTTPException(404, "Key not found")
    k.is_active = False
    audit(db, user.id, "PORTAL_KEY_REVOKED", "merchant", mid, details={"key_id": kid})
    await db.commit()
    return {"ok": True}


@router.post("/merchants/{mid}/webhooks/test")
async def webhook_test(mid: str, body: MA.WebhookTestIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    m, _ = await _member(db, mid, user, ("OWNER", "MANAGER"))
    return await MA.webhook_test(body, m)


# ---- team ----
class MemberIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    role: str = "VIEWER"


@router.get("/merchants/{mid}/team")
async def team(mid: str, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER",))
    rows = (await db.execute(select(MerchantMember, User).join(User, User.id == MerchantMember.user_id).where(MerchantMember.merchant_id == mid))).all()
    return {"items": [{"user_id": u.id, "name": u.full_name, "phone": u.phone, "role": mm.role, "is_active": mm.is_active} for mm, u in rows]}


async def _set_member(db, mid: str, phone: str, role: str) -> MerchantMember:
    if role not in ROLES:
        raise HTTPException(422, f"role must be one of {ROLES}")
    u = (await db.execute(select(User).where(User.phone == phone.strip()))).scalar_one_or_none()
    if not u:
        raise HTTPException(404, {"code": "USER_NOT_FOUND", "message": "The person must have a Wasaly account first"})
    mm = (await db.execute(select(MerchantMember).where(MerchantMember.merchant_id == mid, MerchantMember.user_id == u.id))).scalar_one_or_none()
    if mm:
        mm.role, mm.is_active = role, True
    else:
        mm = MerchantMember(merchant_id=mid, user_id=u.id, role=role)
        db.add(mm)
    await db.flush()
    return mm


@router.post("/merchants/{mid}/team", status_code=201)
async def add_member(mid: str, body: MemberIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER",))
    mm = await _set_member(db, mid, body.phone, body.role)
    audit(db, user.id, "PORTAL_MEMBER_SET", "merchant", mid, details={"user_id": mm.user_id, "role": mm.role})
    await db.commit()
    return {"user_id": mm.user_id, "role": mm.role}


@router.delete("/merchants/{mid}/team/{uid}")
async def remove_member(mid: str, uid: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _member(db, mid, user, ("OWNER",))
    mm = (await db.execute(select(MerchantMember).where(MerchantMember.merchant_id == mid, MerchantMember.user_id == uid))).scalar_one_or_none()
    if not mm or uid == user.id:
        raise HTTPException(404, "Member not found (you cannot remove yourself)")
    mm.is_active = False
    audit(db, user.id, "PORTAL_MEMBER_REMOVED", "merchant", mid, details={"user_id": uid})
    await db.commit()
    return {"ok": True}


# ---- admin: create portal access ----
class PortalUserIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    role: str = "OWNER"
    full_name: str | None = Field(default=None, max_length=120)  # when the person has no Wasaly account yet
    password: str | None = Field(default=None, min_length=8, max_length=72)


@admin_router.post("/{mid}/portal-users", status_code=201)
async def admin_portal_user(mid: str, body: PortalUserIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """Gives a person access to the partner portal. If the phone has no account, one is created (CUSTOMER, approved) with the given password."""
    if not await db.get(Merchant, mid):
        raise HTTPException(404, "Merchant not found")
    phone = body.phone.strip()
    if not (await db.execute(select(User.id).where(User.phone == phone))).first():
        if not body.full_name or not body.password:
            raise HTTPException(422, {"code": "NEEDS_NAME_AND_PASSWORD", "message": "No account for this phone: send full_name and password"})
        db.add(User(full_name=body.full_name.strip(), phone=phone, password_hash=hash_password(body.password), role=Role.CUSTOMER,
                    account_status=AccountStatus.APPROVED, phone_verified=True, approved_at=utcnow()))
        await db.flush()
    mm = await _set_member(db, mid, phone, body.role)
    audit(db, admin.id, "PORTAL_ACCESS_GRANTED", "merchant", mid, details={"user_id": mm.user_id, "role": mm.role})
    await db.commit()
    return {"user_id": mm.user_id, "role": mm.role}


@admin_router.get("/{mid}/portal-users")
async def admin_portal_users(mid: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(MerchantMember, User).join(User, User.id == MerchantMember.user_id).where(MerchantMember.merchant_id == mid))).all()
    return {"items": [{"user_id": u.id, "name": u.full_name, "phone": u.phone, "role": mm.role, "is_active": mm.is_active} for mm, u in rows]}

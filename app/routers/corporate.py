"""Corporate accounts: employees book on one account, monthly statement with the agreed discount, optional monthly credit limit."""
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, customer_user
from ..models import Corporate, CorporateMember, Order, User
from ..schemas import OrderCreateIn
from ..services.audit import audit
from .orders import create_order

admin_router = APIRouter(prefix="/api/admin/corporates", tags=["corporate-admin"])
router = APIRouter(prefix="/api/corporate", tags=["corporate"])
ROLES = ("OWNER", "MANAGER", "EMPLOYEE")
TYPES = ("MOVING", "FREIGHT", "RETAIL", "WAREHOUSE", "CONSTRUCTION", "DEALERSHIP", "CAR_RENTAL", "FLEET", "OTHER")
Q3 = Decimal("0.001")


def _month(month: str | None) -> tuple[datetime, datetime]:
    try:
        start = datetime.strptime(month, "%Y-%m").replace(tzinfo=UTC) if month else datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    except ValueError as e:
        raise HTTPException(422, "month must be YYYY-MM") from e
    end = start.replace(year=start.year + (start.month == 12), month=1 if start.month == 12 else start.month + 1)
    return start, end


def _c(c: Corporate) -> dict:
    return {"id": c.id, "name": c.name, "cr_number": c.cr_number, "business_type": c.business_type, "contact_phone": c.contact_phone,
            "contact_email": c.contact_email, "discount_percent": f"{c.discount_percent:.2f}",
            "monthly_limit": None if c.monthly_limit is None else f"{c.monthly_limit:.3f}", "account_manager": c.account_manager, "is_active": c.is_active}


async def _member(db, corporate_id: int, user_id: int) -> CorporateMember | None:
    return (await db.execute(select(CorporateMember).where(CorporateMember.corporate_id == corporate_id,
                                                           CorporateMember.user_id == user_id, CorporateMember.is_active.is_(True)))).scalar_one_or_none()


async def month_used(db, corporate_id: int, start: datetime, end: datetime) -> Decimal:
    q = select(func.coalesce(func.sum(func.coalesce(Order.customer_total, Order.agreed_price, 0)), 0)).where(
        Order.corporate_id == corporate_id, Order.order_status != S.CANCELLED, Order.created_at >= start, Order.created_at < end)
    return Decimal((await db.execute(q)).scalar_one())


async def check_booking_allowed(db, user_id: int, corporate_id: int) -> None:
    c = await db.get(Corporate, corporate_id)
    if not c or not c.is_active or not await _member(db, corporate_id, user_id):
        raise HTTPException(403, {"code": "NOT_CORPORATE_MEMBER"})
    if c.monthly_limit is not None:
        start, end = _month(None)
        if await month_used(db, corporate_id, start, end) >= c.monthly_limit:
            raise HTTPException(402, {"code": "CORPORATE_LIMIT_REACHED", "message": "تم بلوغ الحد الشهري للحساب المؤسسي"})


async def statement(db, c: Corporate, month: str | None) -> dict:
    start, end = _month(month)
    rows = (await db.execute(select(Order, User.full_name).join(User, User.id == Order.customer_id).where(
        Order.corporate_id == c.id, Order.order_status == S.COMPLETED, Order.completed_at >= start, Order.completed_at < end)
        .order_by(Order.completed_at))).all()
    gross = sum((o.customer_total or Decimal(0) for o, _ in rows), Decimal(0))
    disc = (gross * c.discount_percent / 100).quantize(Q3, ROUND_HALF_UP)
    by: dict[str, dict] = {}
    for o, n in rows:
        d = by.setdefault(n, {"employee": n, "count": 0, "total": Decimal(0)})
        d["count"] += 1
        d["total"] += o.customer_total or Decimal(0)
    return {"corporate": _c(c), "month": start.strftime("%Y-%m"), "currency": "BHD", "count": len(rows), "gross": f"{gross:.3f}",
            "discount_percent": f"{c.discount_percent:.2f}", "discount": f"{disc:.3f}", "net_due": f"{gross - disc:.3f}",
            "by_employee": [{**d, "total": f"{d['total']:.3f}"} for d in by.values()],
            "items": [{"order_id": o.id, "employee": n, "service": o.service_code or o.service_type, "completed_at": o.completed_at.isoformat(),
                       "from": o.pickup_address, "to": o.destination_address, "amount": f"{(o.customer_total or 0):.3f}"} for o, n in rows]}


async def _add_member(db, corporate_id: int, phone: str, role: str) -> CorporateMember:
    if role not in ROLES:
        raise HTTPException(422, f"role must be one of {ROLES}")
    u = (await db.execute(select(User).where(User.phone == phone.strip()))).scalar_one_or_none()
    if not u:
        raise HTTPException(404, {"code": "USER_NOT_FOUND", "message": "المستخدم غير مسجل في وصلي"})
    m = (await db.execute(select(CorporateMember).where(CorporateMember.corporate_id == corporate_id, CorporateMember.user_id == u.id))).scalar_one_or_none()
    if m:
        m.role, m.is_active = role, True
    else:
        m = CorporateMember(corporate_id=corporate_id, user_id=u.id, role=role)
        db.add(m)
    await db.flush()
    return m


class CorporateIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    owner_phone: str = Field(min_length=6, max_length=20)
    cr_number: str | None = Field(default=None, max_length=40)
    business_type: str | None = None
    contact_phone: str | None = Field(default=None, max_length=32)
    contact_email: str | None = Field(default=None, max_length=160)
    discount_percent: Decimal = Field(default=Decimal(0), ge=0, le=100)
    monthly_limit: Decimal | None = Field(default=None, ge=0)
    account_manager: str | None = Field(default=None, max_length=120)


class CorporatePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    cr_number: str | None = None
    business_type: str | None = None
    contact_phone: str | None = None
    contact_email: str | None = None
    discount_percent: Decimal | None = Field(default=None, ge=0, le=100)
    monthly_limit: Decimal | None = Field(default=None, ge=0)
    clear_monthly_limit: bool = False
    account_manager: str | None = None
    is_active: bool | None = None


class MemberIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    role: str = "EMPLOYEE"


# ---------------- admin ----------------
@admin_router.post("", status_code=201)
async def create_corporate(body: CorporateIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    if body.business_type and body.business_type not in TYPES:
        raise HTTPException(422, f"business_type must be one of {TYPES}")
    c = Corporate(created_by=admin.id, **body.model_dump(exclude={"owner_phone"}))
    db.add(c)
    await db.flush()
    await _add_member(db, c.id, body.owner_phone, "OWNER")
    audit(db, admin.id, "CORPORATE_CREATED", "corporate", c.id, details={"name": c.name})
    await db.commit()
    return _c(c)


@admin_router.get("")
async def list_corporates(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return [_c(c) for c in (await db.execute(select(Corporate).order_by(Corporate.id.desc()))).scalars()]


@admin_router.patch("/{cid}")
async def patch_corporate(cid: int, body: CorporatePatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Corporate, cid)
    if not c:
        raise HTTPException(404, "Corporate not found")
    data = body.model_dump(exclude_unset=True, exclude={"clear_monthly_limit"})
    if body.clear_monthly_limit:
        data["monthly_limit"] = None
    for k, v in data.items():
        setattr(c, k, v)
    audit(db, admin.id, "CORPORATE_UPDATED", "corporate", cid, details={k: str(v) for k, v in data.items()})
    await db.commit()
    return _c(c)


@admin_router.post("/{cid}/members", status_code=201)
async def admin_add_member(cid: int, body: MemberIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    if not await db.get(Corporate, cid):
        raise HTTPException(404, "Corporate not found")
    m = await _add_member(db, cid, body.phone, body.role)
    audit(db, admin.id, "CORPORATE_MEMBER_SET", "corporate", cid, details={"user_id": m.user_id, "role": m.role})
    await db.commit()
    return {"user_id": m.user_id, "role": m.role}


@admin_router.get("/{cid}/statement")
async def admin_statement(cid: int, month: str | None = None, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Corporate, cid)
    if not c:
        raise HTTPException(404, "Corporate not found")
    return await statement(db, c, month)


# ---------------- customer side ----------------
async def _manager(db, cid: int, user: User) -> Corporate:
    c = await db.get(Corporate, cid)
    m = await _member(db, cid, user.id) if c else None
    if not c or not m or m.role not in ("OWNER", "MANAGER"):
        raise HTTPException(403, {"code": "CORPORATE_MANAGER_ONLY"})
    return c


@router.get("/mine")
async def my_corporates(user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Corporate, CorporateMember.role).join(CorporateMember, CorporateMember.corporate_id == Corporate.id).where(
        CorporateMember.user_id == user.id, CorporateMember.is_active.is_(True), Corporate.is_active.is_(True)))).all()
    start, end = _month(None)
    return [{**_c(c), "my_role": r, "used_this_month": f"{await month_used(db, c.id, start, end):.3f}"} for c, r in rows]


@router.get("/{cid}/members")
async def members(cid: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _manager(db, cid, user)
    rows = (await db.execute(select(CorporateMember, User).join(User, User.id == CorporateMember.user_id).where(CorporateMember.corporate_id == cid))).all()
    return [{"user_id": u.id, "name": u.full_name, "phone": u.phone, "role": m.role, "is_active": m.is_active} for m, u in rows]


@router.post("/{cid}/members", status_code=201)
async def add_member(cid: int, body: MemberIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _manager(db, cid, user)
    if body.role == "OWNER":
        raise HTTPException(403, "Only admin can add owners")
    m = await _add_member(db, cid, body.phone, body.role)
    audit(db, user.id, "CORPORATE_MEMBER_SET", "corporate", cid, details={"user_id": m.user_id, "role": m.role})
    await db.commit()
    return {"user_id": m.user_id, "role": m.role}


@router.delete("/{cid}/members/{uid}")
async def remove_member(cid: int, uid: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    await _manager(db, cid, user)
    m = await _member(db, cid, uid)
    if not m or m.role == "OWNER":
        raise HTTPException(404, "Member not found")
    m.is_active = False
    audit(db, user.id, "CORPORATE_MEMBER_REMOVED", "corporate", cid, details={"user_id": uid})
    await db.commit()
    return {"ok": True}


@router.get("/{cid}/orders")
async def corporate_orders(cid: int, month: str | None = None, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Corporate, cid)
    m = await _member(db, cid, user.id) if c else None
    if not m:
        raise HTTPException(403, {"code": "NOT_CORPORATE_MEMBER"})
    start, end = _month(month)
    q = select(Order, User.full_name).join(User, User.id == Order.customer_id).where(Order.corporate_id == cid, Order.created_at >= start, Order.created_at < end)
    if m.role == "EMPLOYEE":
        q = q.where(Order.customer_id == user.id)
    rows = (await db.execute(q.order_by(Order.id.desc()).limit(500))).all()
    return [{"order_id": o.id, "employee": n, "service": o.service_code or o.service_type, "status": o.order_status, "from": o.pickup_address,
             "to": o.destination_address, "amount": None if o.customer_total is None else f"{o.customer_total:.3f}", "created_at": o.created_at.isoformat()} for o, n in rows]


@router.get("/{cid}/statement")
async def my_statement(cid: int, month: str | None = None, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    return await statement(db, await _manager(db, cid, user), month)


class BulkIn(BaseModel):
    orders: list[OrderCreateIn] = Field(min_length=1, max_length=20)


@router.post("/{cid}/orders/bulk")
async def bulk_orders(cid: int, body: BulkIn, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Up to 20 bookings in one call (managers/owners). Each is created independently; the result lists success or the error per item."""
    await _manager(db, cid, user)
    out = []
    for i, item in enumerate(body.orders):
        item.corporate_id = cid
        try:
            r = await create_order(item, user, db)
            out.append({"index": i, "ok": True, "order_id": r.get("id") if isinstance(r, dict) else None})
        except HTTPException as e:
            await db.rollback()
            out.append({"index": i, "ok": False, "error": e.detail})
    return {"results": out, "created": sum(1 for r in out if r["ok"])}

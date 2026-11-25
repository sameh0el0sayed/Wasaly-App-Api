"""Merchant sign-up requests from the public Wasaly landing page, followed up by the admins."""
import re
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user
from ..models import MerchantLead, User, utcnow
from ..serializers import dt
from ..services import command
from ..services.audit import audit
from ..services.payments import notify_admins
from ..services.ratelimit import _hit, client_ip

router = APIRouter(prefix="/api/public", tags=["merchant-leads"])
admin_router = APIRouter(prefix="/api/admin/merchant-leads", tags=["merchant-leads"])
STATUSES = ("NEW", "CONTACTED", "CONVERTED", "REJECTED")


class LeadIn(BaseModel):
    name: str = Field(min_length=2, max_length=100)
    business: str = Field(min_length=2, max_length=150)
    phone: str = Field(min_length=6, max_length=30)
    area: str | None = Field(default=None, max_length=100)
    message: str | None = Field(default=None, max_length=2000)


def _digits(p: str) -> str:
    return re.sub(r"\D", "", p)


def _out(x: MerchantLead) -> dict:
    return {"id": x.id, "name": x.name, "business": x.business, "phone": x.phone, "area": x.area, "message": x.message,
            "status": x.status, "note": x.note, "created_at": dt(x.created_at), "updated_at": dt(x.updated_at)}


@router.post("/merchant-leads", status_code=201)
async def create_lead(body: LeadIn, request: Request, db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    if await _hit(f"rl:lead:{ip}", 3600) > 5:
        raise HTTPException(429, {"code": "LEAD_RATE_LIMITED", "message": "Too many requests. Please try again later."}, headers={"Retry-After": "3600"})
    pn = _digits(body.phone)
    if len(pn) < 6:
        raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "Invalid phone number"})
    since = utcnow() - timedelta(days=1)
    if (await db.execute(select(func.count()).select_from(MerchantLead).where(MerchantLead.phone_norm == pn, MerchantLead.created_at >= since))).scalar_one() >= 3:
        raise HTTPException(429, {"code": "LEAD_RATE_LIMITED", "message": "Too many requests. Please try again later."}, headers={"Retry-After": "86400"})
    x = MerchantLead(name=body.name.strip(), business=body.business.strip(), phone=body.phone.strip(), phone_norm=pn, area=(body.area or "").strip() or None,
                     message=(body.message or "").strip() or None, ip=ip, user_agent=(request.headers.get("user-agent") or "")[:300])
    db.add(x)
    await db.flush()
    await notify_admins(db, "MERCHANT_LEAD", "New merchant request", f"{x.business} ({x.area or 'no area'}) asked to join Wasaly.", data={"lead_id": x.id})
    await db.commit()
    command.emit("info", "wasaly.merchant_lead.created", "New merchant lead", x.business, {"id": x.id, "business": x.business, "area": x.area, "created_at": dt(x.created_at)})
    return {"id": x.id, "status": x.status, "created_at": dt(x.created_at)}


@admin_router.get("")
async def list_leads(status: str | None = None, q: str | None = None, page: int = 1, page_size: int = 20,
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    page, page_size = max(page, 1), min(max(page_size, 1), 100)
    cond = []
    if status:
        if status not in STATUSES:
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": "Unknown status"})
        cond.append(MerchantLead.status == status)
    if q and q.strip():
        like = f"%{q.strip()}%"
        d = _digits(q)
        cond.append(or_(MerchantLead.name.ilike(like), MerchantLead.business.ilike(like), MerchantLead.area.ilike(like), *( [MerchantLead.phone_norm.contains(d)] if d else [])))
    total = (await db.execute(select(func.count()).select_from(MerchantLead).where(*cond))).scalar_one()
    rows = (await db.execute(select(MerchantLead).where(*cond).order_by(MerchantLead.id.desc()).limit(page_size).offset((page - 1) * page_size))).scalars().all()
    return {"items": [_out(x) for x in rows], "total": total, "page": page, "page_size": page_size}


class LeadPatch(BaseModel):
    status: str | None = Field(default=None, pattern="^(NEW|CONTACTED|CONVERTED|REJECTED)$")
    note: str | None = Field(default=None, max_length=1000)


@admin_router.patch("/{lead_id}")
async def update_lead(lead_id: int, body: LeadPatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    x = await db.get(MerchantLead, lead_id)
    if not x:
        raise HTTPException(404, {"code": "LEAD_NOT_FOUND", "message": "Merchant lead not found"})
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        setattr(x, k, v)
    x.updated_at = utcnow()
    audit(db, admin.id, "MERCHANT_LEAD_UPDATED", "merchant_lead", lead_id, details={k: str(v)[:80] for k, v in data.items()})
    await db.commit()
    return _out(x)

"""v24: driver fraud flags (admin)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user, paging, super_admin_user
from ..models import FraudFlag, User, utcnow
from ..serializers import dt
from ..services import fraud
from ..services.audit import audit

admin_router = APIRouter(prefix="/api/admin/fraud", tags=["v24-admin"])


class ResolveIn(BaseModel):
    status: str = Field(pattern="^(CONFIRMED|DISMISSED)$")
    note: str | None = Field(default=None, max_length=500)


def _out(f: FraudFlag, name: str | None) -> dict:
    return {"id": f.id, "driver": {"id": f.driver_id, "name": name}, "rule": f.rule, "severity": f.severity, "summary": f.summary,
            "details": f.details, "status": f.status, "note": f.note, "reviewed_by": f.reviewed_by, "reviewed_at": dt(f.reviewed_at),
            "created_at": dt(f.created_at)}


@admin_router.get("/flags")
async def list_flags(status: str | None = "OPEN", severity: str | None = None, rule: str | None = None, driver_id: int | None = None,
                     page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(FraudFlag, User.full_name).join(User, User.id == FraudFlag.driver_id)
    for col, v in ((FraudFlag.status, status), (FraudFlag.severity, severity), (FraudFlag.rule, rule), (FraudFlag.driver_id, driver_id)):
        if v:
            q = q.where(col == v)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(FraudFlag.id.desc()).limit(limit).offset(offset))).all()
    return {"total": total, "items": [_out(f, n) for f, n in rows]}


@admin_router.patch("/flags/{flag_id}")
async def resolve_flag(flag_id: int, body: ResolveIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    f = await db.get(FraudFlag, flag_id)
    if not f:
        raise HTTPException(404, {"code": "FLAG_NOT_FOUND"})
    if f.status != "OPEN":
        raise HTTPException(409, {"code": "FLAG_ALREADY_RESOLVED"})
    f.status, f.note, f.reviewed_by, f.reviewed_at = body.status, body.note, admin.id, utcnow()
    audit(db, admin.id, "FRAUD_FLAG_RESOLVED", "fraud_flag", f.id, reason=body.note, details={"status": body.status, "rule": f.rule, "driver_id": f.driver_id})
    name = (await db.get(User, f.driver_id)).full_name
    await db.commit()
    return _out(f, name)


@admin_router.post("/scan")
async def scan_now(admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    out = await fraud.scan(db)
    await db.commit()
    return out


@admin_router.get("/summary")
async def summary(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(FraudFlag.severity, func.count()).where(FraudFlag.status == "OPEN").group_by(FraudFlag.severity))).all()
    return {"open": sum(n for _, n in rows), "by_severity": {s: n for s, n in rows}}

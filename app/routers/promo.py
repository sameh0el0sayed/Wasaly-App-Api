from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import PromoCode, User
from ..schemas import PromoIn, PromoPatch
from ..serializers import dt, money
from ..services.audit import audit

router = APIRouter(prefix="/api/admin/promo-codes", tags=["promo"])


def promo_out(p: PromoCode) -> dict:
    return {"id": p.id, "code": p.code, "percent": p.percent, "starts_at": dt(p.starts_at), "ends_at": dt(p.ends_at),
            "min_order_amount": money(p.min_order_amount) if p.min_order_amount is not None else None, "is_active": p.is_active}


@router.get("")
async def list_promos(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(PromoCode).order_by(PromoCode.id.desc()))).scalars().all()
    return {"items": [promo_out(p) for p in rows]}


@router.post("", status_code=201)
async def create_promo(body: PromoIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    p = PromoCode(**body.model_dump())
    db.add(p)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(409, {"code": "PROMO_EXISTS", "message": "Code already exists"})
    audit(db, admin.id, "PROMO_CREATE", "promo_code", p.id, details={"code": p.code, "percent": p.percent})
    await db.commit()
    return promo_out(p)


async def _get(db: AsyncSession, promo_id: int) -> PromoCode:
    p = await db.get(PromoCode, promo_id)
    if not p:
        raise HTTPException(404, "Promo code not found")
    return p


@router.patch("/{promo_id}")
async def patch_promo(promo_id: int, body: PromoPatch, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    p = await _get(db, promo_id)
    data = body.model_dump(exclude_unset=True)
    for k, v in data.items():
        if v is None and k in ("percent", "is_active"):
            continue
        setattr(p, k, v)
    if p.starts_at and p.ends_at and p.ends_at <= p.starts_at:
        raise HTTPException(422, "ends_at must be after starts_at")
    audit(db, admin.id, "PROMO_UPDATE", "promo_code", p.id, details={"fields": sorted(data)})
    await db.commit()
    return promo_out(p)


@router.delete("/{promo_id}", status_code=204)
async def delete_promo(promo_id: int, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    p = await _get(db, promo_id)  # orders keep their own snapshot, so deleting never changes old orders
    await db.delete(p)
    audit(db, admin.id, "PROMO_DELETE", "promo_code", promo_id, details={"code": p.code})
    await db.commit()


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import OrderStatus as S
from ...database import get_db
from ...deps import admin_user
from ...models import (Order,
                      ServiceArea, User)
from ...schemas import (AreaIn, AreaPatch)
from ...services.audit import audit

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- service areas ----------------
def _area(a: ServiceArea) -> dict:
    return {"id": a.id, "name": a.name, "boundary": a.boundary, "is_active": a.is_active}


@router.get("/service-areas")
async def list_areas(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ServiceArea).order_by(ServiceArea.id))).scalars().all()
    out = []
    for a in rows:
        n = (await db.execute(select(func.count()).select_from(Order).where(Order.service_area_id == a.id, Order.order_status == S.NO_DRIVER_AVAILABLE))).scalar_one()
        out.append({**_area(a), "orders_without_driver": n})
    return out


@router.post("/service-areas", status_code=201)
async def create_area(body: AreaIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = ServiceArea(**body.model_dump())
    db.add(a)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Area name already exists")
    audit(db, admin.id, "AREA_CREATED", "area", a.id)
    await db.commit()
    return _area(a)


@router.patch("/service-areas/{area_id}")
async def patch_area(area_id: int, body: AreaPatch, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = await db.get(ServiceArea, area_id)
    if not a:
        raise HTTPException(404, "Area not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(a, k, v)
    audit(db, admin.id, "AREA_UPDATED", "area", a.id, details=body.model_dump(exclude_unset=True))
    await db.commit()
    return _area(a)

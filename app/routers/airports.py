"""Admin: airport zones (fee on top of the normal price for trips that start or end inside) + driver gender (admin-controlled)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import AirportZone, User
from ..schemas import AirportIn, AirportPatch, DriverGenderIn
from ..services.audit import audit

router = APIRouter(prefix="/api/admin", tags=["admin-airports"])


def _out(z: AirportZone) -> dict:
    return {"id": z.id, "name": z.name, "boundary": z.boundary, "fee": f"{z.fee:.3f}", "is_active": z.is_active}


@router.get("/airport-zones")
async def list_airports(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return [_out(z) for z in (await db.execute(select(AirportZone).order_by(AirportZone.id))).scalars()]


@router.post("/airport-zones", status_code=201)
async def create_airport(body: AirportIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    z = AirportZone(**body.model_dump())
    db.add(z)
    try:
        await db.flush()
    except IntegrityError:
        raise HTTPException(409, "Airport name already exists")
    audit(db, admin.id, "AIRPORT_ZONE_CREATED", "airport_zone", z.id, details={"fee": f"{z.fee:.3f}"})
    await db.commit()
    return _out(z)


@router.patch("/airport-zones/{zone_id}")
async def patch_airport(zone_id: int, body: AirportPatch, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    z = await db.get(AirportZone, zone_id)
    if not z:
        raise HTTPException(404, "Airport zone not found")
    changes = body.model_dump(exclude_unset=True)
    for k, v in changes.items():
        setattr(z, k, v)
    audit(db, admin.id, "AIRPORT_ZONE_UPDATED", "airport_zone", z.id, details={k: str(v) for k, v in changes.items() if k != "boundary"})
    await db.commit()
    return _out(z)


@router.delete("/airport-zones/{zone_id}")
async def delete_airport(zone_id: int, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    z = await db.get(AirportZone, zone_id)
    if not z:
        raise HTTPException(404, "Airport zone not found")
    await db.delete(z)  # old orders keep their snapshotted fee
    audit(db, admin.id, "AIRPORT_ZONE_DELETED", "airport_zone", zone_id)
    await db.commit()
    return {"deleted": True}


@router.patch("/drivers/{user_id}/gender")
async def set_driver_gender(user_id: int, body: DriverGenderIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Only an admin changes a driver's gender (it decides which customers can request him), always with a reason."""
    u = await db.get(User, user_id)
    if not u or not u.driver_profile:
        raise HTTPException(404, "Driver not found")
    audit(db, admin.id, "DRIVER_GENDER_SET", "user", u.id, body.reason, details={"from": u.driver_profile.gender, "to": body.gender})
    u.driver_profile.gender = body.gender
    await db.commit()
    return {"id": u.id, "gender": body.gender}

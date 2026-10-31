
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import Role
from ...database import get_db
from ...deps import admin_user, paging
from ...models import DriverProfile, DriverServiceArea, ServiceArea, User, Vehicle, VehicleDocument, utcnow
from ...schemas import DocReviewIn, DriverAreasIn
from ...serializers import user_public
from ...services.audit import audit
from ...services.dispatch import force_offline
from ...services.notifications import notify
from .users import _target  # noqa: E402

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- drivers ----------------
@router.get("/drivers")
async def list_drivers(online: bool | None = None, status: str | None = None, page=Depends(paging),
                       admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(User).join(DriverProfile, DriverProfile.user_id == User.id).where(User.role == Role.DRIVER)
    if online is not None:
        stmt = stmt.where(DriverProfile.is_online.is_(online))
    if status:
        stmt = stmt.where(User.account_status == status)
    rows = (await db.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))).scalars().unique().all()
    return [{**user_public(u), "online": u.driver_profile.is_online, "license_expiry_date": u.driver_profile.license_expiry_date.isoformat(),
             "vehicle": {"make": u.vehicle.make, "model": u.vehicle.model, "plate_number": u.vehicle.plate_number} if u.vehicle else None} for u in rows]


@router.put("/drivers/{driver_id}/service-areas")
async def set_driver_areas(driver_id: int, body: DriverAreasIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, driver_id)
    if u.role != Role.DRIVER:
        raise HTTPException(422, "Not a driver")
    ids = set(body.service_area_ids)
    found = set((await db.execute(select(ServiceArea.id).where(ServiceArea.id.in_(ids)))).scalars())
    if ids - found:
        raise HTTPException(422, f"Unknown areas: {sorted(ids - found)}")
    await db.execute(delete(DriverServiceArea).where(DriverServiceArea.driver_id == driver_id))
    for i in ids:
        db.add(DriverServiceArea(driver_id=driver_id, service_area_id=i))
    audit(db, admin.id, "DRIVER_AREAS_SET", "user", driver_id, details={"areas": sorted(ids)})
    await db.commit()
    return {"service_area_ids": sorted(ids)}


@router.post("/documents/{doc_id}/review")
async def review_document(doc_id: int, body: DocReviewIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    d = await db.get(VehicleDocument, doc_id)
    if not d:
        raise HTTPException(404, "Document not found")
    d.review_status, d.review_reason, d.reviewed_by, d.reviewed_at = body.status, body.reason, admin.id, utcnow()
    veh = await db.get(Vehicle, d.vehicle_id)
    if body.status == "REJECTED":
        await force_offline(db, veh.driver_id)
    await notify(db, veh.driver_id, "DOCUMENT_REVIEWED", "مراجعة مستند", f"{d.document_type}: {body.status}" + (f" - {body.reason}" if body.reason else ""))
    audit(db, admin.id, "DOCUMENT_REVIEW", "document", d.id, body.reason, {"status": body.status})
    await db.commit()
    return {"id": d.id, "review_status": d.review_status}

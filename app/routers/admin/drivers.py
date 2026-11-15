
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import OrderStatus
from ...database import get_db
from ...deps import admin_user, paging
from ...models import DriverProfile, DriverServiceArea, Order, ProfileChange, ServiceArea, User, Vehicle, VehicleDocument, utcnow
from ...serializers import driver_list_load_options
from ...schemas import DocReviewIn, DriverAreasIn
from ...security import decrypt
from ...serializers import money, user_public
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
    stmt = select(User).join(DriverProfile, DriverProfile.user_id == User.id)
    if online is not None:
        stmt = stmt.where(DriverProfile.is_online.is_(online))
    if status:
        stmt = stmt.where(User.account_status == status)
    stmt = stmt.options(
        selectinload(User.driver_profile),
        selectinload(User.vehicle),
    )
    rows = (await db.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))).scalars().unique().all()
    return [{**user_public(u), "online": u.driver_profile.is_online, "license_expiry_date": u.driver_profile.license_expiry_date.isoformat(),
             "vehicle": {"make": u.vehicle.make, "model": u.vehicle.model, "plate_number": u.vehicle.plate_number} if u.vehicle else None} for u in rows]


async def _driver_areas(db, driver_id: int) -> list[dict]:
    rows = (await db.execute(select(ServiceArea).join(DriverServiceArea, DriverServiceArea.service_area_id == ServiceArea.id)
                             .where(DriverServiceArea.driver_id == driver_id).order_by(ServiceArea.name))).scalars().all()
    return [{"id": a.id, "name": a.name, "boundary": a.boundary, "is_active": a.is_active} for a in rows]


async def _driver(db, driver_id: int) -> User:
    u = await _target(db, driver_id)
    if not u.driver_profile:
        raise HTTPException(404, "Driver not found")
    return u


@router.get("/drivers/{driver_id}/service-areas")
async def get_driver_areas(driver_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _driver(db, driver_id)
    items = await _driver_areas(db, u.id)
    return {"driver_id": u.id, "driver_name": u.full_name, "service_area_ids": [a["id"] for a in items], "items": items}


@router.get("/drivers/{driver_id}")
async def get_driver_full(driver_id: int, reveal_cpr: bool = False, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Everything about one driver: account, licence, vehicle + documents, areas, payout (masked), debt, stats, open corrections."""
    from .users import get_user
    u = await _driver(db, driver_id)
    d = await get_user(u.id, reveal_cpr, admin, db)
    p = u.driver_profile
    acc = decrypt(p.payout_account_encrypted) if p.payout_account_encrypted else None
    d["service_areas"] = await _driver_areas(db, u.id)
    d["payout"] = {"method": p.payout_method, "account": ("****" + acc[-4:]) if acc else None, "holder_name": p.payout_holder_name}
    d["mode"] = p.mode
    d["last_location"] = {"lat": p.last_lat, "lng": p.last_lng} if p.last_lat is not None else None
    by_status = dict((await db.execute(select(Order.order_status, func.count()).where(Order.driver_id == u.id).group_by(Order.order_status))).all())
    earned = (await db.execute(select(func.coalesce(func.sum(Order.driver_earning), 0)).where(
        Order.driver_id == u.id, Order.order_status == OrderStatus.COMPLETED))).scalar_one()
    d["stats"] = {"orders_by_status": by_status, "total_orders": sum(by_status.values()), "total_earnings": money(earned)}
    d["corrections"] = [{"id": c.id, "field": c.field, "reason": c.reason, "status": c.status} for c in (await db.execute(
        select(ProfileChange).where(ProfileChange.user_id == u.id, ProfileChange.status.in_(["OPEN", "SUBMITTED", "PENDING_REVIEW"])).order_by(ProfileChange.id))).scalars()]
    return d


@router.put("/drivers/{driver_id}/service-areas")
async def set_driver_areas(driver_id: int, body: DriverAreasIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await _target(db, driver_id)
    if not u.driver_profile:
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

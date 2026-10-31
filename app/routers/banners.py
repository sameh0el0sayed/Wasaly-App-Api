from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import Role
from ..database import get_db
from ..deps import admin_user, optional_user, super_admin_user
from ..models import Banner, User, utcnow
from ..schemas import BannerIn, BannerPatch
from ..serializers import dt
from ..services.audit import audit

router = APIRouter(prefix="/api", tags=["banners"])


def banner_out(b: Banner, admin: bool = False) -> dict:
    d = {"id": b.id, "title_ar": b.title_ar, "title_en": b.title_en, "subtitle_ar": b.subtitle_ar, "subtitle_en": b.subtitle_en,
         "image_file_key": b.image_file_key, "bg_color": b.bg_color,
         "action": {"type": b.action_type, "value": b.action_value}, "audience": b.audience,
         "starts_at": dt(b.starts_at), "ends_at": dt(b.ends_at), "sort_order": b.sort_order}
    if admin:
        d["is_active"] = b.is_active
    return d


@router.get("/banners")
async def list_banners(audience: str | None = Query(default=None, pattern="^(CUSTOMER|DRIVER)$"),
                       user: User | None = Depends(optional_user), db: AsyncSession = Depends(get_db)):
    # anonymous callers only ever see ALL; logged-in callers default to their own role
    if user is None:
        wanted = "ALL"
    else:
        wanted = audience or (user.role if user.role in (Role.CUSTOMER, Role.DRIVER) else "ALL")
    now = utcnow()
    q = select(Banner).where(Banner.is_active.is_(True),
                             or_(Banner.starts_at.is_(None), Banner.starts_at <= now),
                             or_(Banner.ends_at.is_(None), Banner.ends_at > now))
    q = q.where(Banner.audience == "ALL") if wanted == "ALL" else q.where(Banner.audience.in_([wanted, "ALL"]))
    rows = (await db.execute(q.order_by(Banner.sort_order, Banner.id).limit(10))).scalars().all()
    return {"items": [banner_out(b) for b in rows]}


@router.get("/admin/banners")
async def admin_list(_: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Banner).order_by(Banner.sort_order, Banner.id))).scalars().all()
    return {"items": [banner_out(b, admin=True) for b in rows]}


def _apply(b: Banner, data: dict) -> None:
    action = data.pop("action", None)
    if action is not None:
        b.action_type, b.action_value = action["type"], action["value"]
    for k, v in data.items():
        setattr(b, k, v)


@router.post("/admin/banners", status_code=201)
async def create_banner(body: BannerIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    b = Banner()
    _apply(b, body.model_dump())
    db.add(b)
    await db.flush()
    audit(db, admin.id, "BANNER_CREATE", "banner", b.id)
    await db.commit()
    return banner_out(b, admin=True)


@router.patch("/admin/banners/{banner_id}")
async def patch_banner(banner_id: int, body: BannerPatch, admin: User = Depends(super_admin_user),
                       db: AsyncSession = Depends(get_db)):
    b = await db.get(Banner, banner_id)
    if not b:
        raise HTTPException(404, "Banner not found")
    data = body.model_dump(exclude_unset=True)
    if "action" in data and data["action"] is None:
        data.pop("action")
    _apply(b, data)
    if b.starts_at and b.ends_at and b.ends_at <= b.starts_at:
        raise HTTPException(422, "ends_at must be after starts_at")
    audit(db, admin.id, "BANNER_UPDATE", "banner", b.id, details={"fields": sorted(data)})
    await db.commit()
    return banner_out(b, admin=True)


@router.delete("/admin/banners/{banner_id}", status_code=204)
async def delete_banner(banner_id: int, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    b = await db.get(Banner, banner_id)
    if not b:
        raise HTTPException(404, "Banner not found")
    await db.delete(b)
    audit(db, admin.id, "BANNER_DELETE", "banner", banner_id)
    await db.commit()

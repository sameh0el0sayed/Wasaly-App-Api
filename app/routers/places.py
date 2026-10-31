from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import approved_user
from ..models import SavedPlace, User
from ..schemas import PlaceIn, PlacePatch

router = APIRouter(prefix="/api/places", tags=["places"])
MAX_PLACES = 20


def place_out(p: SavedPlace) -> dict:
    return {"id": p.id, "label": p.label, "name": p.name, "address": p.address, "lat": p.latitude, "lng": p.longitude}


async def _mine(db: AsyncSession, user: User, place_id: int) -> SavedPlace:
    p = await db.get(SavedPlace, place_id)
    if not p or p.user_id != user.id:
        raise HTTPException(404, "Place not found")
    return p


async def _single_label(db: AsyncSession, user: User, label: str, keep_id: int | None):
    """HOME and WORK are unique per user: the existing one is returned so it can be replaced."""
    if label == "OTHER":
        return None
    q = select(SavedPlace).where(SavedPlace.user_id == user.id, SavedPlace.label == label)
    if keep_id:
        q = q.where(SavedPlace.id != keep_id)
    return (await db.execute(q.limit(1))).scalar_one_or_none()


@router.get("")
async def list_places(user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(SavedPlace).where(SavedPlace.user_id == user.id).order_by(SavedPlace.id))).scalars().all()
    return {"items": [place_out(p) for p in rows]}


@router.post("", status_code=201)
async def create_place(body: PlaceIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    existing = await _single_label(db, user, body.label, None)
    if existing:  # upsert for HOME / WORK
        p = existing
    else:
        count = (await db.execute(select(func.count()).select_from(SavedPlace).where(SavedPlace.user_id == user.id))).scalar_one()
        if count >= MAX_PLACES:
            raise HTTPException(409, {"code": "PLACES_LIMIT", "message": f"Maximum {MAX_PLACES} saved places"})
        p = SavedPlace(user_id=user.id)
        db.add(p)
    p.label, p.name, p.address, p.latitude, p.longitude = body.label, body.name, body.address, body.lat, body.lng
    await db.commit()
    await db.refresh(p)
    return place_out(p)


@router.patch("/{place_id}")
async def patch_place(place_id: int, body: PlacePatch, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    p = await _mine(db, user, place_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("label") and data["label"] != p.label and await _single_label(db, user, data["label"], p.id):
        raise HTTPException(409, {"code": "LABEL_TAKEN", "message": f"You already have a {data['label']} place"})
    for k, v in data.items():
        if v is None:
            continue
        setattr(p, {"lat": "latitude", "lng": "longitude"}.get(k, k), v)
    await db.commit()
    return place_out(p)


@router.delete("/{place_id}", status_code=204)
async def delete_place(place_id: int, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    p = await _mine(db, user, place_id)
    await db.delete(p)
    await db.commit()

"""Vehicle classes (STANDARD / FAMILY / VIP): admin-editable list, price factor, nearest-driver ETA per class."""
import json
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus
from ..constants import OrderStatus as S
from ..models import DriverProfile, Order, User, Vehicle

DEFAULT = "STANDARD"


def all_classes(s: dict) -> list[dict]:
    try:
        items = json.loads(s.get("vehicle_classes") or "[]")
    except ValueError:
        return []
    return [c for c in items if isinstance(c, dict) and c.get("code")]


def enabled(s: dict) -> list[dict]:
    return [c for c in all_classes(s) if c.get("enabled", True)]


def resolve(s: dict, code: str | None) -> dict:
    """Class for a new order. No code = STANDARD (old apps). An unknown or disabled code -> 422."""
    if not code:
        return next((c for c in all_classes(s) if c["code"] == DEFAULT), {"code": DEFAULT, "price_factor": 1.0})
    c = next((c for c in enabled(s) if c["code"] == code.upper()), None)
    if not c:
        raise HTTPException(422, {"code": "VEHICLE_CLASS_UNAVAILABLE"})
    return c


def factor(c: dict) -> Decimal:
    return Decimal(str(c.get("price_factor", 1))).quantize(Decimal("0.0001"))


def serves(s: dict, vehicle_class: str | None, order_class: str | None) -> bool:
    """Can a driver whose car is `vehicle_class` take an order of `order_class`?"""
    v, o = (vehicle_class or DEFAULT).upper(), (order_class or DEFAULT).upper()
    return v == o or (v == "VIP" and o == DEFAULT and s.get("vip_serves_standard") == "true")


async def class_etas(db: AsyncSession, s: dict, lat: float, lng: float) -> dict[str, int | None]:
    """Minutes for the nearest online FREE driver of each class (straight line at the average city speed); None = nobody online."""
    from ..services.geo import _haversine_m
    busy = select(Order.driver_id).where(Order.driver_id.is_not(None), Order.order_status.in_(S.DRIVER_BUSY))
    rows = (await db.execute(select(Vehicle.vehicle_class, DriverProfile.last_lat, DriverProfile.last_lng)
                             .join(DriverProfile, DriverProfile.user_id == Vehicle.driver_id)
                             .join(User, User.id == Vehicle.driver_id)
                             .where(DriverProfile.is_online.is_(True), DriverProfile.last_lat.is_not(None), User.account_status == AccountStatus.APPROVED,
                                    Vehicle.vehicle_status == AccountStatus.APPROVED, Vehicle.driver_id.not_in(busy)))).all()
    speed = max(float(s.get("estimate_avg_speed_kmh") or 30), 5.0)
    best: dict[str, float] = {}
    for vc, dlat, dlng in rows:
        d = _haversine_m(lat, lng, dlat, dlng) / 1000
        for cls in (vc or DEFAULT, ):
            for tgt in (cls, DEFAULT if serves(s, cls, DEFAULT) else None):
                if tgt and (tgt not in best or d < best[tgt]):
                    best[tgt] = d
    return {c["code"]: (max(1, round(best[c["code"]] / speed * 60)) if c["code"] in best else None) for c in enabled(s)}

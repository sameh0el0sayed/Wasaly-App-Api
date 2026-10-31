import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import ServiceArea


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def point_in_boundary(lat: float, lng: float, boundary: dict | None) -> bool:
    if not boundary:
        return False
    if boundary.get("type") == "circle":
        c = boundary["center"]
        return _haversine_m(lat, lng, c[0], c[1]) <= float(boundary["radius_m"])
    pts = boundary.get("points") or []
    inside = False
    j = len(pts) - 1
    for i in range(len(pts)):
        yi, xi = pts[i]
        yj, xj = pts[j]
        if (xi > lng) != (xj > lng) and lat < (yj - yi) * (lng - xi) / ((xj - xi) or 1e-12) + yi:
            inside = not inside
        j = i
    return inside


async def find_area(db: AsyncSession, lat: float, lng: float) -> ServiceArea | None:
    areas = (await db.execute(select(ServiceArea).where(ServiceArea.is_active.is_(True)))).scalars().all()
    for a in areas:
        if point_in_boundary(lat, lng, a.boundary):
            return a
    return None

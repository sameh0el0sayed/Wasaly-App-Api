import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import ServiceArea


def h3_cell(lat: float, lng: float, resolution: int | None = None) -> str | None:
    """H3 cell of a (matched) coordinate. Local library, no network; None when h3 is unavailable."""
    try:
        import h3

        return h3.latlng_to_cell(lat, lng, int(resolution if resolution is not None else settings.h3_resolution))
    except Exception:  # noqa: BLE001 - optional dependency
        return None


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

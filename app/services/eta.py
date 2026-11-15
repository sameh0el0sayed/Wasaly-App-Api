"""Live ETA from the self-hosted OSRM routing API (no paid routing service).

Results are cached per order + H3 cell so a driver reporting every few seconds
does not translate into a route request per update.
"""
from ..config import settings
from ..models import utcnow
from . import cache
from .geo import h3_cell


async def eta_to_destination(client, order_id: int, src: tuple[float, float],
                             dst: tuple[float, float]) -> dict | None:
    """Road duration/distance from `src` to `dst`. None when disabled or OSRM is unavailable."""
    if not settings.eta_enabled:
        return None
    cell = h3_cell(src[0], src[1]) or f"{src[0]:.3f},{src[1]:.3f}"
    key = f"wasaly:eta:{order_id}:{cell}"
    cached = await cache.get_json(key)
    if cached:
        return cached
    res = await client.route(src, dst)
    if not res.ok:
        return None
    out = {"distance_m": round(res.distance_m), "duration_s": round(res.duration_s),
           "duration_min": max(1, round(res.duration_s / 60)), "at": utcnow().isoformat()}
    await cache.set_json(key, out, settings.eta_cache_seconds)
    return out

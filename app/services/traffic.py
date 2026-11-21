"""Live road traffic on a route via TomTom (key from owner config `tomtom_api_key`; empty = feature silently off).

route_traffic(origin, destination) -> {eta_min, normal_min, delay_min, level: none|light|heavy, incidents[], checked_at}
Cached in Redis per (origin 300 m grid, destination 300 m grid, 5-minute bucket); incidents per bbox grid for 5 minutes.
Hard daily cap per TomTom API (2,200 of the 2,500 free requests); over the cap or on any failure -> level none + OSRM ETA.
Nothing is stored with user data: only cache entries and counters.
"""
import logging
import math
import time
from datetime import timedelta

import httpx

from ..models import utcnow
from . import cache, command

log = logging.getLogger("traffic")
DAILY_CAP = 2200
ICON = {1: "accident", 6: "jam", 7: "lane_closed", 8: "closure", 9: "roadworks"}
_http: httpx.AsyncClient | None = None


async def api_key() -> str:
    return ((await command.owner_all()).get("tomtom_api_key") or "").strip()


def _client() -> httpx.AsyncClient:
    global _http
    if _http is None:
        _http = httpx.AsyncClient(timeout=4)
    return _http


def _day() -> str:
    return (utcnow() + timedelta(hours=3)).strftime("%Y%m%d")  # Asia/Bahrain day


async def _allow(api: str) -> bool:
    """Count one TomTom request against today's cap. False when the cap is reached (or Redis is unavailable)."""
    r = await cache.client()
    if r is None:
        return False
    try:
        key = f"wasaly:tomtom:{api}:{_day()}"
        n = await r.incr(key)
        if n == 1:
            await r.expire(key, 172800)
        return n <= DAILY_CAP
    except Exception:  # noqa: BLE001
        return False


async def usage() -> dict:
    """Requests made today per TomTom API (for the PCC quota view)."""
    r = await cache.client()
    out = {"day": _day(), "cap": DAILY_CAP, "routing": 0, "incidents": 0}
    if r is None:
        return out
    try:
        for api in ("routing", "incidents"):
            out[api] = int(await r.get(f"wasaly:tomtom:{api}:{_day()}") or 0)
    except Exception:  # noqa: BLE001
        pass
    return out


def _m(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180
    a = math.sin((lat2 - lat1) * p / 2) ** 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lng2 - lng1) * p / 2) ** 2
    return 12742000 * math.asin(math.sqrt(a))


def _near(lat: float, lng: float, pts: list, limit: float = 150.0) -> bool:
    return any(_m(lat, lng, p[0], p[1]) <= limit for p in pts)


def _grid(p: tuple[float, float]) -> str:
    return f"{int(p[0] / 0.0027)},{int(p[1] / 0.003)}"


async def _fallback(origin, dest) -> dict:
    eta = None
    try:
        from . import map_matching
        if map_matching._client is not None:
            res = await map_matching._client.route(origin, dest)
            if res.ok:
                eta = max(1, round(res.duration_s / 60))
    except Exception:  # noqa: BLE001
        pass
    return {"eta_min": eta, "normal_min": eta, "delay_min": 0, "level": "none", "incidents": [], "checked_at": utcnow().isoformat()}


async def _incidents(key: str, pts: list) -> list:
    lats, lngs = [p[0] for p in pts], [p[1] for p in pts]
    pad_lat, pad_lng = 0.0027, 0.003  # ~300 m
    bbox = (min(lngs) - pad_lng, min(lats) - pad_lat, max(lngs) + pad_lng, max(lats) + pad_lat)
    ck = f"wasaly:traffic:i:{bbox[0]:.2f},{bbox[1]:.2f},{bbox[2]:.2f},{bbox[3]:.2f}:{int(time.time() // 300)}"
    raw = await cache.get_json(ck)
    if raw is None:
        if not await _allow("incidents"):
            return []
        fields = "{incidents{type,geometry{type,coordinates},properties{iconCategory,magnitudeOfDelay,events{description,code},from,to,delay,roadNumbers}}}"
        try:
            r = await _client().get("https://api.tomtom.com/traffic/services/5/incidentDetails",
                                    params={"bbox": ",".join(f"{v:.5f}" for v in bbox), "fields": fields, "language": "ar-SA",
                                            "timeValidityFilter": "present", "key": key})
            r.raise_for_status()
            raw = r.json().get("incidents", [])
        except Exception as e:  # noqa: BLE001
            log.warning("tomtom incidents failed: %s", type(e).__name__)
            return []
        await cache.set_json(ck, raw, 600)
    out = []
    for inc in raw:
        pr = inc.get("properties") or {}
        kind = ICON.get(pr.get("iconCategory"))
        if not kind:
            continue
        g = inc.get("geometry") or {}
        c = g.get("coordinates") or []
        flat = [c] if (c and isinstance(c[0], (int, float))) else c  # Point or LineString
        coords = [(q[1], q[0]) for q in flat if isinstance(q, (list, tuple)) and len(q) >= 2]
        hit = next((q for q in coords if _near(q[0], q[1], pts)), None)
        if not hit:
            continue
        ev = (pr.get("events") or [{}])[0]
        out.append({"type": kind, "description": ev.get("description") or "", "road": ", ".join(pr.get("roadNumbers") or []),
                    "from": pr.get("from") or "", "to": pr.get("to") or "", "delay_min": round((pr.get("delay") or 0) / 60),
                    "lat": hit[0], "lng": hit[1], "_mag": pr.get("magnitudeOfDelay") or 0})
    return out


def _level(delay_min: int, incidents: list) -> str:
    heavy = delay_min >= 10 or any(i["type"] in ("accident", "closure") or (i["type"] == "jam" and i["_mag"] >= 3) for i in incidents)
    if heavy:
        return "heavy"
    return "light" if (3 <= delay_min <= 9 or incidents) else "none"


async def route_traffic(origin: tuple[float, float], destination: tuple[float, float]) -> dict:
    """Never raises. Feature off / quota reached / TomTom down -> level none with the OSRM ETA."""
    try:
        key = await api_key()
        if not key or None in origin or None in destination:
            return await _fallback(origin, destination) if key else {"eta_min": None, "normal_min": None, "delay_min": 0, "level": "none", "incidents": [], "checked_at": utcnow().isoformat()}
        ck = f"wasaly:traffic:r:{_grid(origin)}:{_grid(destination)}:{int(time.time() // 300)}"
        hit = await cache.get_json(ck)
        if hit:
            return hit
        if not await _allow("routing"):
            return await _fallback(origin, destination)
        r = await _client().get(f"https://api.tomtom.com/routing/1/calculateRoute/{origin[0]},{origin[1]}:{destination[0]},{destination[1]}/json",
                                params={"traffic": "true", "departAt": "now", "routeRepresentation": "polyline", "computeTravelTimeFor": "all", "key": key})
        r.raise_for_status()
        route = r.json()["routes"][0]
        sm = route["summary"]
        pts = [(p["latitude"], p["longitude"]) for leg in route.get("legs", []) for p in leg.get("points", [])]
        delay = round((sm.get("trafficDelayInSeconds") or 0) / 60)
        incs = await _incidents(key, pts) if pts else []
        incs.sort(key=lambda i: (-i["_mag"], -i["delay_min"]))
        level = _level(delay, incs)  # needs _mag, so before it is dropped
        for i in incs:
            i.pop("_mag", None)
        res = {"eta_min": max(1, round(sm["travelTimeInSeconds"] / 60)), "normal_min": max(1, round(sm.get("noTrafficTravelTimeInSeconds", sm["travelTimeInSeconds"]) / 60)),
               "delay_min": delay, "level": level, "incidents": incs[:3], "checked_at": utcnow().isoformat()}
        await cache.set_json(ck, res, 600)
        return res
    except Exception as e:  # noqa: BLE001
        log.warning("route_traffic failed: %s", type(e).__name__)
        return await _fallback(origin, destination)



async def offer_traffic(order, driver_pos) -> dict | None:
    """Ride offer / acceptance payload: {to_pickup, to_dropoff}, each {level, delay_min, eta_min, incidents[<=3]}. None when TomTom is not configured."""
    if not await api_key():
        return None
    pick = (order.pickup_latitude, order.pickup_longitude)
    drop = (order.destination_latitude, order.destination_longitude)

    def slim(t):
        return {k: t[k] for k in ("level", "delay_min", "eta_min", "incidents")}
    return {"to_pickup": slim(await route_traffic(driver_pos, pick)) if driver_pos else None,
            "to_dropoff": slim(await route_traffic(pick, drop)) if None not in drop else None}


_trip_at: dict[int, float] = {}


async def check_trip(order_id: int, customer_id: int, lat: float, lng: float, dest: tuple) -> None:
    """IN_PROGRESS: every 2 minutes; notify the customer once per trip when the level rises to heavy. Never raises."""
    try:
        if time.time() - _trip_at.get(order_id, 0) < 120 or None in dest:
            return
        _trip_at[order_id] = time.time()
        if len(_trip_at) > 2000:
            _trip_at.clear()
        t = await route_traffic((lat, lng), dest)
        nk = f"wasaly:traffic:notified:{order_id}"
        if t["level"] != "heavy" or await cache.get_json(nk):
            return
        await cache.set_json(nk, 1, 21600)
        from sqlalchemy import select

        from ..database import SessionLocal
        from ..models import Translation, User
        from .notifications import notify
        async with SessionLocal() as db:
            lang = (await db.execute(select(User.language).where(User.id == customer_id))).scalar_one_or_none() or "ar"
            inc = t["incidents"][0]["type"] if t["incidents"] else "jam"
            rows = {k: (a, e) for k, a, e in (await db.execute(select(Translation.key, Translation.ar, Translation.en).where(
                Translation.key.in_(["traffic.notify_heavy_title", "traffic.notify_heavy_body", f"traffic.incident.{inc}"])))).all()}
            pick = 1 if lang == "en" else 0
            body = rows.get("traffic.notify_heavy_body", ("", ""))[pick].replace("{min}", str(max(t["delay_min"], 1))).replace("{reason}", rows.get(f"traffic.incident.{inc}", ("", ""))[pick])
            await notify(db, customer_id, "TRAFFIC_HEAVY", rows.get("traffic.notify_heavy_title", ("", ""))[pick], body, order_id)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("traffic trip check failed")

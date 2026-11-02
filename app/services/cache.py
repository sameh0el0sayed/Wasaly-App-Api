"""Optional Redis (REDIS_URL). Every function degrades to 'no cache' when Redis is unset or down: PostgreSQL stays the source of truth."""
import json
import logging
import time

from ..config import settings

log = logging.getLogger("cache")
_client = None
_down_until = 0.0

# set the location only when it is newer than the stored one (atomic), refresh the TTL
_LUA_SET_LOC = """
local cur = redis.call('HGET', KEYS[1], 'ts')
if cur and tonumber(cur) >= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], 'ts', ARGV[1], 'lat', ARGV[2], 'lng', ARGV[3], 'speed', ARGV[4], 'heading', ARGV[5])
redis.call('EXPIRE', KEYS[1], ARGV[6])
return 1
"""


def enabled() -> bool:
    return bool(settings.redis_url)


async def client():
    """Shared async client (connection pool, timeouts). None when disabled or in the 10 s back-off after a failure."""
    global _client, _down_until
    if not settings.redis_url or time.time() < _down_until:
        return None
    if _client is None:
        try:
            import redis.asyncio as aioredis
            _client = aioredis.from_url(settings.redis_url, max_connections=20, socket_timeout=0.5, socket_connect_timeout=0.5,
                                        decode_responses=True, health_check_interval=30)
        except Exception:  # noqa
            log.exception("redis init failed")
            _down_until = time.time() + 10
            return None
    return _client


def _fail(e: Exception) -> None:
    global _down_until
    _down_until = time.time() + 10
    log.warning("redis unavailable: %s", e)


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:  # noqa
            pass
        _client = None


async def ping() -> str:
    if not settings.redis_url:
        return "disabled"
    r = await client()
    if r is None:
        return "down"
    try:
        await r.ping()
        return "ok"
    except Exception as e:  # noqa
        _fail(e)
        return "down"


async def get_json(key: str):
    r = await client()
    if r is None:
        return None
    try:
        v = await r.get(key)
        return json.loads(v) if v else None
    except Exception as e:  # noqa
        _fail(e)
        return None


async def set_json(key: str, value, ttl: int) -> None:
    r = await client()
    if r is None:
        return
    try:
        await r.set(key, json.dumps(value), ex=ttl)
    except Exception as e:  # noqa
        _fail(e)


async def delete(*keys: str) -> None:
    r = await client()
    if r is None:
        return
    try:
        await r.delete(*keys)
    except Exception as e:  # noqa
        _fail(e)


# ---------- latest driver location ----------
def _loc_key(driver_id: int) -> str:
    return f"wasaly:driver:location:{driver_id}"


async def set_location(driver_id: int, lat: float, lng: float, speed, heading) -> bool:
    """True when stored in Redis (the caller may then skip most PostgreSQL writes). False -> caller writes PostgreSQL."""
    r = await client()
    if r is None:
        return False
    try:
        ts = int(time.time() * 1000)
        await r.eval(_LUA_SET_LOC, 1, _loc_key(driver_id), ts, lat, lng, "" if speed is None else speed,
                     "" if heading is None else heading, settings.location_ttl_seconds)
        return True
    except Exception as e:  # noqa
        _fail(e)
        return False


async def get_locations(driver_ids: list[int]) -> dict[int, dict]:
    """{driver_id: {lat,lng,ts(epoch s),speed,heading}} for the drivers that have a fresh (non-expired) Redis location."""
    r = await client()
    if r is None or not driver_ids:
        return {}
    try:
        pipe = r.pipeline()
        for d in driver_ids:
            pipe.hgetall(_loc_key(d))
        rows = await pipe.execute()
    except Exception as e:  # noqa
        _fail(e)
        return {}
    out = {}
    for d, h in zip(driver_ids, rows, strict=True):
        if h and h.get("lat"):
            out[d] = {"lat": float(h["lat"]), "lng": float(h["lng"]), "ts": int(h["ts"]) / 1000,
                      "speed": float(h["speed"]) if h.get("speed") else None, "heading": float(h["heading"]) if h.get("heading") else None}
    return out


async def clear_location(driver_id: int) -> None:
    await delete(_loc_key(driver_id))

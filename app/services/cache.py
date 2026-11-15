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
    """Forget everything about a driver's location (offline / signed out)."""
    await delete(_loc_key(driver_id), _raw_key(driver_id), _matched_key(driver_id),
                 _road_key(driver_id), _history_key(driver_id), _customer_key(driver_id))


# ---------- map matching: raw + matched location, road state, history ----------
# Raw GPS and matched GPS are kept separately (acceptance criteria). Every write is
# guarded by the observation timestamp so an older asynchronous match result can
# never overwrite a newer fix.
_LUA_SET_IF_NEWER = """
local cur = redis.call('HGET', KEYS[1], 'ts')
if cur and tonumber(cur) >= tonumber(ARGV[1]) then return 0 end
redis.call('HSET', KEYS[1], unpack(ARGV, 2, #ARGV - 1))
redis.call('EXPIRE', KEYS[1], ARGV[#ARGV])
return 1
"""


def _raw_key(driver_id: int) -> str:
    return f"wasaly:driver:raw_location:{driver_id}"


def _matched_key(driver_id: int) -> str:
    return f"wasaly:driver:matched_location:{driver_id}"


def _road_key(driver_id: int) -> str:
    return f"wasaly:driver:road_state:{driver_id}"


def _history_key(driver_id: int) -> str:
    return f"wasaly:driver:location_history:{driver_id}"


def _customer_key(driver_id: int) -> str:
    return f"wasaly:driver:customer:{driver_id}"


async def _set_if_newer(key: str, ts_ms: int, fields: list, ttl: int) -> bool:
    r = await client()
    if r is None:
        return False
    try:
        return bool(await r.eval(_LUA_SET_IF_NEWER, 1, key, ts_ms, *fields, ttl))
    except Exception as e:  # noqa
        _fail(e)
        return False


async def set_raw_location(driver_id: int, lat: float, lng: float, ts_ms: int, speed=None, heading=None,
                           accuracy=None, seq: int = 0) -> bool:
    return await _set_if_newer(_raw_key(driver_id), ts_ms,
                              ["lat", lat, "lng", lng, "speed", "" if speed is None else speed,
                               "heading", "" if heading is None else heading,
                               "accuracy", "" if accuracy is None else accuracy, "seq", seq],
                              settings.location_ttl_seconds)


async def get_raw_location(driver_id: int) -> dict | None:
    return await _hgetall(_raw_key(driver_id))


async def set_matched_location(driver_id: int, lat: float, lng: float, ts_ms: int, confidence=None,
                               road_name=None, h3=None, seq: int = 0, status: str = "MATCHED") -> bool:
    return await _set_if_newer(_matched_key(driver_id), ts_ms,
                              ["lat", lat, "lng", lng, "confidence", "" if confidence is None else confidence,
                               "road_name", "" if road_name is None else road_name,
                               "h3", "" if h3 is None else h3, "seq", seq, "status", status],
                              settings.location_ttl_seconds)


async def _hgetall(key: str) -> dict | None:
    r = await client()
    if r is None:
        return None
    try:
        h = await r.hgetall(key)
    except Exception as e:  # noqa
        _fail(e)
        return None
    return _decode_hash(h) if h else None


async def get_matched_location(driver_id: int) -> dict | None:
    return await _hgetall(_matched_key(driver_id))


async def get_matched_locations(driver_ids: list[int]) -> dict[int, dict]:
    """{driver_id: {lat,lng,ts,speed,heading,confidence,road_name,h3,status}} for drivers with a matched fix."""
    r = await client()
    if r is None or not driver_ids:
        return {}
    try:
        pipe = r.pipeline()
        for d in driver_ids:
            pipe.hgetall(_matched_key(d))
        rows = await pipe.execute()
    except Exception as e:  # noqa
        _fail(e)
        return {}
    out = {}
    for d, h in zip(driver_ids, rows, strict=True):
        if h and h.get("lat"):
            out[d] = _decode_hash(h)
    return out


async def set_road_state(driver_id: int, *, road_name=None, direction=None, confidence=None, h3=None,
                         status=None, matched_at_ms: int | None = None) -> bool:
    r = await client()
    if r is None:
        return False
    try:
        fields = {"road_name": "" if road_name is None else road_name,
                  "direction": "" if direction is None else direction,
                  "confidence": "" if confidence is None else confidence,
                  "h3": "" if h3 is None else h3,
                  "status": "" if status is None else status,
                  "matched_at": matched_at_ms or int(time.time() * 1000)}
        await r.hset(_road_key(driver_id), mapping=fields)
        await r.expire(_road_key(driver_id), settings.location_ttl_seconds)
        return True
    except Exception as e:  # noqa
        _fail(e)
        return False


async def get_road_state(driver_id: int) -> dict | None:
    return await _hgetall(_road_key(driver_id))


async def push_location_history(driver_id: int, point: dict) -> bool:
    """Rolling raw+matched history (newest first) for diagnostics; bounded by settings."""
    r = await client()
    if r is None:
        return False
    try:
        pipe = r.pipeline()
        pipe.lpush(_history_key(driver_id), json.dumps(point))
        pipe.ltrim(_history_key(driver_id), 0, max(0, settings.location_history_max_points - 1))
        pipe.expire(_history_key(driver_id), settings.location_history_ttl_seconds)
        await pipe.execute()
        return True
    except Exception as e:  # noqa
        _fail(e)
        return False


async def get_location_history(driver_id: int, limit: int | None = None) -> list[dict]:
    r = await client()
    if r is None:
        return []
    try:
        rows = await r.lrange(_history_key(driver_id), 0, (limit or settings.location_history_max_points) - 1)
    except Exception as e:  # noqa
        _fail(e)
        return []
    out = []
    for row in rows:
        try:
            out.append(json.loads(row))
        except Exception:  # noqa
            continue
    return out


async def set_active_customer(driver_id: int, customer_id: int) -> None:
    """Who receives this driver's matched-location stream while a trip is active."""
    r = await client()
    if r is None:
        return
    try:
        await r.set(_customer_key(driver_id), customer_id, ex=settings.location_ttl_seconds * 4)
    except Exception as e:  # noqa
        _fail(e)


async def get_active_customer(driver_id: int) -> int | None:
    r = await client()
    if r is None:
        return None
    try:
        v = await r.get(_customer_key(driver_id))
        return int(v) if v else None
    except Exception as e:  # noqa
        _fail(e)
        return None


async def clear_active_customer(driver_id: int) -> None:
    await delete(_customer_key(driver_id))


def _decode_hash(h: dict) -> dict:
    """Redis hashes come back as strings; keep numbers as floats and leave blanks as None."""
    out = {}
    for k, v in h.items():
        if v == "" or v is None:
            out[k] = None
        else:
            try:
                out[k] = float(v)
            except (TypeError, ValueError):
                out[k] = v
    if out.get("ts") is not None:
        out["ts"] = out["ts"] / 1000  # epoch seconds, like the raw location key
    return out

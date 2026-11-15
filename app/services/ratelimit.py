"""Distributed fixed-window rate limiting (Redis INCR + EXPIRE), shared by every API process.

Redis down -> falls back to a per-process in-memory counter (still throttles, just not across processes);
it never blocks legitimate users because of an infrastructure failure. Windows are short, so nobody is locked out for long."""
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from . import cache

_mem: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    return fwd or (request.client.host if request.client else "unknown")


async def _hit(key: str, window: int) -> int:
    r = await cache.client()
    if r is not None:
        try:
            n = await r.incr(key)
            if n == 1:
                await r.expire(key, window)
            return int(n)
        except Exception as e:  # noqa: BLE001
            cache._fail(e)
    now = time.time()
    count, reset = _mem[key]
    if now >= reset:
        count, reset = 0, now + window
    count += 1
    _mem[key] = (count, reset)
    if len(_mem) > 20000:  # keep the fallback bounded
        for k in [k for k, (_, t) in _mem.items() if t < now]:
            _mem.pop(k, None)
    return count


def limit(name: str, max_hits: int, window: int):
    """FastAPI dependency: at most `max_hits` calls per client IP per `window` seconds."""
    async def dep(request: Request) -> None:
        n = await _hit(f"rl:{name}:{client_ip(request)}", window)
        if n > max_hits:
            raise HTTPException(429, {"code": "RATE_LIMITED", "retry_after_seconds": window},
                                headers={"Retry-After": str(window)})
    return dep

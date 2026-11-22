"""In-process wake-ups for the background loops (single API worker).

Loops sleep up to IDLE_SECONDS (30 s) instead of polling every few seconds. Code that creates time-critical work
(offers with a 10 s TTL, counter-offers, webhook outbox rows) calls `<wakeup>.at(ts)` / `.soon()` so the loop wakes
exactly when needed. Hints may arrive before the request commits, so callers can pass several hints.
"""
import asyncio
import time

IDLE_SECONDS = 30.0
MIN_SECONDS = 0.5


class Wakeup:
    def __init__(self) -> None:
        self._event: asyncio.Event | None = None
        self._hints: set[float] = set()

    def _ev(self) -> asyncio.Event:
        if self._event is None:
            self._event = asyncio.Event()
        return self._event

    def at(self, ts: float) -> None:
        """Wake the loop at unix time `ts` (or now if it is already past)."""
        self._hints.add(ts)
        if len(self._hints) > 500:
            now = time.time()
            self._hints = {h for h in self._hints if h > now} or {now}
        self._ev().set()

    def soon(self, *delays: float) -> None:
        now = time.time()
        for d in delays or (0.0,):
            self.at(now + d)

    async def sleep(self, deadline: float | None = None, idle: float = IDLE_SECONDS) -> None:
        """Sleeps until the nearest of: `deadline`, a pending hint, now + idle."""
        ev = self._ev()
        while True:
            now = time.time()
            due = any(h <= now for h in self._hints)
            self._hints = {h for h in self._hints if h > now}
            target = min([now + idle] + ([deadline] if deadline is not None else []) + list(self._hints))
            if due or target - now < MIN_SECONDS:
                await asyncio.sleep(MIN_SECONDS)
                return
            ev.clear()
            try:
                await asyncio.wait_for(ev.wait(), target - now)
            except TimeoutError:
                return


dispatch = Wakeup()   # offers / counter-offers expiry, scheduled dispatch, order timeout
webhooks = Wakeup()   # merchant webhook outbox

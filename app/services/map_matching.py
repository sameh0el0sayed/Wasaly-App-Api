"""Real-time GPS map matching (self-hosted OSRM + OpenStreetMap).

Pipeline, fully asynchronous and non-blocking for the GPS endpoint:

    POST /api/driver/location
        -> validate + movement sanity check
        -> store the raw fix in Redis
        -> enqueue the driver
    worker(s)
        -> rolling observation window (settings.map_match_window_seconds)
        -> OSRM /match/v1/drive against the locally hosted road network
        -> application confidence (OSRM confidence x GPS accuracy x snap distance)
        -> matched location + road state + history in Redis
        -> SSE broadcast to the customer on the active trip (and admins)

If OSRM is unavailable, or the confidence is too low, the validated raw fix is
kept and the status is DEGRADED. An older match result can never overwrite a
newer fix (Redis compares the observation timestamp).
"""
import asyncio
import logging
import time

from sqlalchemy import select

from ..config import settings
from ..constants import OrderStatus as S
from ..database import SessionLocal
from ..models import Order, utcnow
from . import cache, eta
from .geo import _haversine_m, h3_cell
from .gps import (
    DEGRADED,
    OK,
    REJECTED,
    GpsObservation,
    GpsVerdict,
    ObservationWindow,
    bearing_deg,
    check_movement,
    validate_fix,
)
from .osrm import OsrmClient
from .realtime import hub

log = logging.getLogger("map_matching")

_queue: asyncio.Queue | None = None
_queued: set[int] = set()  # drivers already waiting: one pending job per driver (the rolling window keeps the newest points)
_dropped = 0  # jobs skipped because the queue was full (raw GPS stays the fallback)
_oldest: dict[int, float] = {}  # driver -> enqueue time, for oldest-job-age
QUEUE_MAX = 5000
_windows: dict[int, ObservationWindow] = {}
_seq: dict[int, int] = {}
_last_match: dict[int, float] = {}
_last_broadcast: dict[int, tuple] = {}  # driver -> (status, lat, lng, heading)
_client: OsrmClient | None = None
_workers: list[asyncio.Task] = []
_started = False


def enabled() -> bool:
    return bool(settings.map_matching_enabled) and _started


async def start() -> None:
    """Start the matching workers (idempotent). No-op when the feature is disabled."""
    global _queue, _client, _started, _workers
    if not settings.map_matching_enabled or _started:
        return
    _queue = asyncio.Queue(maxsize=QUEUE_MAX)
    _client = OsrmClient()
    n = max(1, int(settings.osrm_match_workers))
    _workers = [asyncio.create_task(_worker_loop()) for _ in range(n)]
    _started = True
    log.info("GPS map matching enabled: %s worker(s), OSRM=%s", n, _client.base_url)


async def stop() -> None:
    global _started, _workers, _client
    if not _started:
        return
    for w in _workers:
        w.cancel()
    for w in _workers:
        try:
            await w
        except asyncio.CancelledError:
            pass
    _workers = []
    _started = False
    if _client is not None:
        await _client.aclose()
        _client = None
    log.info("GPS map matching stopped")


def clear_driver(driver_id: int) -> None:
    """Drop all in-memory state for a driver (went offline / signed out)."""
    w = _windows.pop(driver_id, None)
    if w is not None:
        w.clear()
    _seq.pop(driver_id, None)
    _last_match.pop(driver_id, None)
    _last_broadcast.pop(driver_id, None)


def _enqueue(driver_id: int) -> None:
    """Never blocks and never grows without bound: duplicates are collapsed, a full queue drops the job."""
    global _dropped
    if driver_id in _queued:
        return
    try:
        _queue.put_nowait(driver_id)
    except asyncio.QueueFull:
        _dropped += 1
        return
    _queued.add(driver_id)
    _oldest[driver_id] = time.time()


def queue_stats() -> dict:
    """Queue depth, oldest pending job age (s) and dropped-job count, for monitoring."""
    oldest = min(_oldest.values()) if _oldest else None
    return {"depth": _queue.qsize() if _queue is not None else 0,
            "oldest_job_age_s": round(time.time() - oldest, 2) if oldest else 0.0, "dropped": _dropped}


async def ingest(driver_id: int, body) -> dict:
    """Validate one GPS fix, store it raw and enqueue it for matching.

    Never raises and never waits for OSRM, so the driver app is never blocked.
    """
    now_ms = int(time.time() * 1000)
    verdict = validate_fix(body.latitude, body.longitude, body.accuracy_m, body.speed_kmh, body.heading)
    if verdict.status == REJECTED:
        return {"status": REJECTED, "reason": verdict.reason}
    seq = _seq.get(driver_id, 0) + 1
    _seq[driver_id] = seq
    obs = GpsObservation(driver_id=driver_id, lat=body.latitude, lng=body.longitude, ts_ms=now_ms,
                         speed_kmh=body.speed_kmh, heading=body.heading, accuracy_m=body.accuracy_m, seq=seq)
    # raw is always stored (diagnostics + fallback), even when matching is disabled
    await cache.set_raw_location(driver_id, obs.lat, obs.lng, obs.ts_ms, obs.speed_kmh,
                                 obs.heading, obs.accuracy_m, obs.seq)
    if not enabled():
        return {"status": verdict.status, "reason": verdict.reason, "seq": seq}
    win = _windows.setdefault(driver_id, ObservationWindow())
    prev = win.points()[-1] if len(win) else None
    if prev is not None:
        movement = check_movement(prev, obs)
        if movement.status == REJECTED:
            return {"status": REJECTED, "reason": movement.reason, "seq": seq}
        if movement.reason == "STATIONARY" and verdict.status == OK:
            verdict = GpsVerdict(DEGRADED, "STATIONARY")
    win.add(obs)
    _enqueue(driver_id)
    return {"status": verdict.status, "reason": verdict.reason, "seq": seq}


async def _worker_loop() -> None:
    """Batch whatever is queued, then match each driver once."""
    while True:
        first = await _queue.get()
        pending = {first}
        while True:
            try:
                pending.add(_queue.get_nowait())
                _queue.task_done()
            except asyncio.QueueEmpty:
                break
        _queue.task_done()
        for d in pending:  # a driver may be queued again from now on (it is being processed with its newest window)
            _queued.discard(d)
            _oldest.pop(d, None)
        for driver_id in pending:
            try:
                await _process(driver_id)
            except Exception:  # noqa: BLE001 - one bad driver must not kill the worker
                log.exception("map matching failed driver=%s", driver_id)


async def _process(driver_id: int) -> None:
    win = _windows.get(driver_id)
    if win is None:
        return
    points = win.points()
    if len(points) < settings.map_match_min_points:
        return
    now = time.monotonic()
    if now - _last_match.get(driver_id, 0.0) < settings.osrm_match_min_interval_seconds:
        return
    _last_match[driver_id] = now
    latest = points[-1]
    res = await _client.match(points)
    if not res.ok or not res.snapped:
        await _apply(driver_id, points, None, DEGRADED, res.error or "MATCH_FAILED")
        return
    snapped = res.snapped[-1]
    snap_dist = _haversine_m(latest.lat, latest.lng, snapped.lat, snapped.lng)
    conf = _confidence(res.confidence, latest.accuracy_m, snap_dist)
    if conf < settings.map_match_confidence_threshold or snap_dist > settings.map_match_max_snap_distance_m:
        await _apply(driver_id, points, None, DEGRADED, "LOW_CONFIDENCE")
        return
    await _apply(driver_id, points, snapped, "MATCHED", None, conf)


def _confidence(osrm_conf, accuracy_m, snap_dist_m) -> float:
    """OSRM confidence adjusted by GPS accuracy and how far the point was snapped."""
    c = 1.0 if osrm_conf is None else max(0.0, min(1.0, float(osrm_conf)))
    if accuracy_m is not None:
        acc = max(0.0, 1.0 - float(accuracy_m) / settings.map_match_max_accuracy_m)
        c *= 0.5 + 0.5 * acc
    if snap_dist_m is not None:
        snap = max(0.0, 1.0 - float(snap_dist_m) / settings.map_match_max_snap_distance_m)
        c *= 0.5 + 0.5 * snap
    return round(min(1.0, max(0.0, c)), 3)


async def _apply(driver_id: int, points, snapped, status: str, reason: str | None,
                 conf: float | None = None) -> None:
    latest = points[-1]
    if snapped is None:  # fallback: keep the validated raw fix, flagged degraded
        lat, lng, road_name = latest.lat, latest.lng, None
        conf = conf if conf is not None else 0.0
    else:
        lat, lng, road_name = snapped.lat, snapped.lng, snapped.road_name
    cell = h3_cell(lat, lng)
    direction = latest.heading
    if snapped is not None and len(points) >= 2:
        b = bearing_deg(points[-2].lat, points[-2].lng, lat, lng)
        if b is not None:
            direction = b
    # ts = the observation timestamp, so a late match can never beat a newer fix
    await cache.set_matched_location(driver_id, lat, lng, latest.ts_ms, conf, road_name, cell,
                                     latest.seq, status)
    await cache.set_road_state(driver_id, road_name=road_name, direction=direction, confidence=conf,
                               h3=cell, status=status)
    await cache.push_location_history(driver_id, {"lat": lat, "lng": lng, "ts": latest.ts_ms,
                                                  "confidence": conf, "status": status,
                                                  "road_name": road_name, "h3": cell})
    # do not spam the same position: skip when nothing meaningful moved/turned
    prev = _last_broadcast.get(driver_id)
    if prev is not None and prev[0] == status:
        moved = _haversine_m(prev[1], prev[2], lat, lng)
        turned = (prev[3] is None or direction is None or
                  abs((prev[3] - direction + 180) % 360 - 180) >= 30)
        if moved < settings.map_match_min_movement_m and not turned:
            return
    _last_broadcast[driver_id] = (status, lat, lng, direction)
    await _broadcast(driver_id, lat, lng, direction, latest, conf, road_name, cell, status)


async def live_location(driver_id: int) -> dict | None:
    """Latest matched location, falling back to the validated raw fix."""
    loc = await cache.get_matched_location(driver_id)
    if loc and loc.get("lat") is not None:
        return loc
    return await cache.get_raw_location(driver_id)


async def live_eta(order_id: int, src: tuple[float, float], dst: tuple[float, float]) -> dict | None:
    if _client is None or not settings.eta_enabled:
        return None
    return await eta.eta_to_destination(_client, order_id, src, dst)


async def _active_order(driver_id: int):
    async with SessionLocal() as db:
        return (await db.execute(
            select(Order.id, Order.customer_id, Order.destination_latitude, Order.destination_longitude,
                   Order.order_status, Order.pickup_latitude, Order.pickup_longitude)
            .where(Order.driver_id == driver_id, Order.order_status.in_(S.DRIVER_BUSY))
            .order_by(Order.id.desc()).limit(1))).first()


async def _live_card_eta(order_row, lat: float, lng: float) -> None:
    """Feed the live order card: ETA to pickup before the trip starts, to the destination during it. Never raises."""
    try:
        from . import live_order
        st = order_row.order_status
        if st in (S.ACCEPTED, S.DRIVER_ON_THE_WAY):
            dst = (order_row.pickup_latitude, order_row.pickup_longitude)
        elif st == S.IN_PROGRESS:
            dst = (order_row.destination_latitude, order_row.destination_longitude)
        else:
            return
        if dst[0] is None or dst[1] is None:
            return
        e = await eta.eta_to_destination(_client, order_row.id, (lat, lng), dst) if st == S.IN_PROGRESS else await eta.eta_to_destination(_client, -order_row.id, (lat, lng), dst)
        if e is not None:
            live_order.on_eta(order_row.id, e["duration_min"])
    except Exception:  # noqa: BLE001
        pass


async def _broadcast(driver_id: int, lat: float, lng: float, heading, latest: GpsObservation,
                     conf: float, road_name, cell, status: str) -> None:
    customer_id = await cache.get_active_customer(driver_id)
    order_row = None
    if settings.eta_enabled or customer_id is None:
        order_row = await _active_order(driver_id)
        if customer_id is None and order_row is not None:
            customer_id = order_row.customer_id
    payload = {"type": "driver.location.matched", "driver_id": driver_id,
               "order_id": order_row.id if order_row is not None else None,
               "lat": lat, "lng": lng, "heading": heading, "speed": latest.speed_kmh,
               "accuracy_m": latest.accuracy_m, "confidence": conf, "status": status,
               "road_name": road_name, "h3": cell, "seq": latest.seq,
               "at": utcnow().isoformat()}
    if settings.eta_enabled and order_row is not None and order_row.destination_latitude is not None:
        e = await eta.eta_to_destination(_client, order_row.id, (lat, lng),
                                         (order_row.destination_latitude, order_row.destination_longitude))
        if e is not None:
            payload["eta"] = e
    if settings.eta_enabled and order_row is not None:
        await _live_card_eta(order_row, lat, lng)
    hub.publish(payload, {customer_id} if customer_id else set())

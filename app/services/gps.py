"""GPS validation, sanity checks and rolling observation windows.

Pure functions and in-memory state only: no database, no Redis, no network. The
map-matching worker feeds OSRM with the ordered points collected here.
"""
from collections import deque
from dataclasses import dataclass
from math import atan2, cos, degrees, isfinite, radians, sin

from ..config import settings
from .geo import _haversine_m

OK = "OK"
DEGRADED = "DEGRADED"  # usable, but flagged (poor accuracy / mismatch)
REJECTED = "REJECTED"  # dropped, never stored or matched

MAX_SPEED_KMH = 250.0  # a car cannot legitimately exceed this; bad GPS can
MAX_JUMP_M = 5000.0  # a single update cannot teleport further than this
MAX_JUMP_SECONDS = 60.0


@dataclass(frozen=True)
class GpsObservation:
    driver_id: int
    lat: float
    lng: float
    ts_ms: int
    speed_kmh: float | None = None
    heading: float | None = None
    accuracy_m: float | None = None
    seq: int = 0


@dataclass
class GpsVerdict:
    status: str
    reason: str | None = None


def validate_fix(lat, lng, accuracy_m=None, speed_kmh=None, heading=None,
                 max_accuracy_m: float | None = None) -> GpsVerdict:
    """Range/sanity validation of one GPS fix. Degraded != rejected: a poor fix is
    still stored raw (diagnostics / fallback) but is never trusted for snapping."""
    max_accuracy = max_accuracy_m if max_accuracy_m is not None else settings.map_match_max_accuracy_m
    if not isfinite(lat) or not isfinite(lng):
        return GpsVerdict(REJECTED, "INVALID_COORDINATES")
    if not -90 <= lat <= 90 or not -180 <= lng <= 180:
        return GpsVerdict(REJECTED, "OUT_OF_RANGE")
    if lat == 0 and lng == 0:
        return GpsVerdict(REJECTED, "NULL_ISLAND")
    if speed_kmh is not None and (not isfinite(speed_kmh) or speed_kmh < 0 or speed_kmh > MAX_SPEED_KMH):
        return GpsVerdict(REJECTED, "INVALID_SPEED")
    if heading is not None and (not isfinite(heading) or heading < 0 or heading >= 360):
        return GpsVerdict(REJECTED, "INVALID_HEADING")
    if accuracy_m is None:
        return GpsVerdict(DEGRADED, "NO_ACCURACY")
    if not isfinite(accuracy_m) or accuracy_m < 0:
        return GpsVerdict(REJECTED, "INVALID_ACCURACY")
    if accuracy_m > max_accuracy:
        return GpsVerdict(DEGRADED, "LOW_ACCURACY")
    return GpsVerdict(OK)


def check_movement(prev: GpsObservation, cur: GpsObservation,
                   min_movement_m: float | None = None) -> GpsVerdict:
    """Reject impossible movement between two consecutive fixes of one driver."""
    min_movement = min_movement_m if min_movement_m is not None else settings.map_match_min_movement_m
    dt = (cur.ts_ms - prev.ts_ms) / 1000
    if dt <= 0:
        return GpsVerdict(REJECTED, "OUT_OF_ORDER")
    dist = _haversine_m(prev.lat, prev.lng, cur.lat, cur.lng)
    if dist > MAX_JUMP_M and dt < MAX_JUMP_SECONDS:
        return GpsVerdict(REJECTED, "IMPOSSIBLE_JUMP")
    implied = dist / dt * 3.6  # km/h
    limit = max((cur.speed_kmh or 0) * 3, 120)
    if implied > limit:
        return GpsVerdict(DEGRADED, "SPEED_MISMATCH")
    if dist < min_movement:
        return GpsVerdict(OK, "STATIONARY")
    return GpsVerdict(OK)


def bearing_deg(lat1, lng1, lat2, lng2) -> float | None:
    """Compass bearing from point 1 to point 2 (0..360), or None when identical."""
    if _haversine_m(lat1, lng1, lat2, lng2) < settings.map_match_min_movement_m:
        return None
    p1, p2 = radians(lat1), radians(lat2)
    dl = radians(lng2 - lng1)
    y = sin(dl) * cos(p2)
    x = cos(p1) * sin(p2) - sin(p1) * cos(p2) * cos(dl)
    return (degrees(atan2(y, x)) + 360) % 360


class ObservationWindow:
    """Rolling, time-ordered GPS window for one driver (OSRM match input)."""

    def __init__(self, max_points: int | None = None, max_age_seconds: float | None = None) -> None:
        self.max_points = max_points or settings.map_match_max_points
        self.max_age_seconds = max_age_seconds or settings.map_match_window_seconds
        self._points: deque[GpsObservation] = deque()
        self._latest_ts = 0

    def add(self, obs: GpsObservation) -> bool:
        """Append a fix. Out-of-order fixes are ignored (never let an old fix in)."""
        if self._points and obs.ts_ms < self._latest_ts:
            return False
        self._points.append(obs)
        self._latest_ts = max(self._latest_ts, obs.ts_ms)
        self._prune()
        return True

    def _prune(self) -> None:
        cutoff = self._latest_ts - int(self.max_age_seconds * 1000)
        while self._points and self._points[0].ts_ms < cutoff:
            self._points.popleft()
        while len(self._points) > self.max_points:
            self._points.popleft()

    def points(self) -> list[GpsObservation]:
        self._prune()
        return list(self._points)

    def clear(self) -> None:
        self._points.clear()
        self._latest_ts = 0

    def __len__(self) -> int:
        return len(self._points)


def is_stationary(points: list[GpsObservation], min_movement_m: float | None = None) -> bool:
    """True when the window shows no meaningful movement (parked / traffic jam)."""
    min_movement = min_movement_m if min_movement_m is not None else settings.map_match_min_movement_m
    if len(points) < 2:
        return True
    first, last = points[0], points[-1]
    return _haversine_m(first.lat, first.lng, last.lat, last.lng) < min_movement

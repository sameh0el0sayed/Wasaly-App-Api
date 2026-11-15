"""Self-hosted OSRM client (map matching + routing).

Only ever talks to the locally hosted OSRM container (OpenStreetMap data). There
is no paid API, no public demo server and no per-request charge. Every call fails
fast and the caller falls back to validated raw GPS.
"""
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

from ..config import settings


class OsrmError(Exception):
    pass


@dataclass
class SnappedPoint:
    lat: float
    lng: float
    ts_ms: int
    road_name: str | None = None
    confidence: float | None = None


@dataclass
class MatchResult:
    ok: bool
    confidence: float | None = None
    snapped: list[SnappedPoint] = field(default_factory=list)
    error: str | None = None


@dataclass
class RouteResult:
    ok: bool
    distance_m: float | None = None
    duration_s: float | None = None
    error: str | None = None


class OsrmClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = (base_url or settings.osrm_base_url).rstrip("/")
        self.timeout = timeout or settings.osrm_timeout_seconds
        self._client = client

    async def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=1.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def match(self, points) -> MatchResult:
        """Match an ordered GPS trace to the OSM road network.

        `tracepoints` stays aligned with `points` (1:1, null for an unmatched fix),
        which is why `tidy` is deliberately not used.
        """
        if len(points) < 2:
            return MatchResult(False, error="NOT_ENOUGH_POINTS")
        coords = ";".join(f"{p.lng},{p.lat}" for p in points)
        timestamps = ";".join(str(int(p.ts_ms // 1000)) for p in points)
        params = {"geometries": "geojson", "overview": "false", "annotations": "false",
                  "gaps": "ignore", "timestamps": timestamps}
        url = f"{self.base_url}/match/v1/drive/{coords}?{urlencode(params)}"
        try:
            r = await (await self._http()).get(url)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001 - any failure means "map matching unavailable"
            return MatchResult(False, error=f"OSRM_ERROR:{type(e).__name__}")
        if data.get("code") != "Ok":
            return MatchResult(False, error=f"OSRM_{data.get('code')}")
        return self._parse_match(data, points)

    @staticmethod
    def _parse_match(data: dict, points) -> MatchResult:
        tracepoints = data.get("tracepoints") or []
        matchings = data.get("matchings") or []
        snapped: list[SnappedPoint] = []
        confidences: list[float] = []
        for i, tp in enumerate(tracepoints):
            if not tp or i >= len(points):
                continue
            loc = tp.get("location")  # [lng, lat]
            if not loc or len(loc) < 2:
                continue
            mi = tp.get("matchings_index")
            conf = None
            if isinstance(mi, int) and 0 <= mi < len(matchings):
                raw = matchings[mi].get("confidence")
                if raw is not None:
                    conf = float(raw)
                    confidences.append(conf)
            snapped.append(SnappedPoint(lat=float(loc[1]), lng=float(loc[0]), ts_ms=points[i].ts_ms,
                                        road_name=tp.get("name"), confidence=conf))
        if not snapped:
            return MatchResult(False, error="NO_MATCHED_POINTS")
        overall = min(confidences) if confidences else None
        return MatchResult(True, confidence=overall, snapped=snapped)

    async def route(self, src: tuple[float, float], dst: tuple[float, float]) -> RouteResult:
        """Road route between two (lat, lng) points; used for live ETA."""
        coords = f"{src[1]},{src[0]};{dst[1]},{dst[0]}"
        params = {"overview": "false", "annotations": "false", "steps": "false"}
        url = f"{self.base_url}/route/v1/drive/{coords}?{urlencode(params)}"
        try:
            r = await (await self._http()).get(url)
            r.raise_for_status()
            data = r.json()
        except Exception as e:  # noqa: BLE001
            return RouteResult(False, error=f"OSRM_ERROR:{type(e).__name__}")
        if data.get("code") != "Ok":
            return RouteResult(False, error=f"OSRM_{data.get('code')}")
        routes = data.get("routes") or []
        if not routes:
            return RouteResult(False, error="NO_ROUTE")
        return RouteResult(True, distance_m=float(routes[0]["distance"]),
                           duration_s=float(routes[0]["duration"]))

    async def health(self) -> bool:
        """OSRM has no /health endpoint; a trivial route doubles as a liveness probe."""
        return (await self.route((26.22, 50.57), (26.2201, 50.5701))).ok

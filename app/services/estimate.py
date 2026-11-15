"""Pre-trip ESTIMATES. The real price is always computed from the actual trip time (start-trip -> arrived).
The app never calculates anything: it shows these server numbers and labels them as estimates."""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..config import settings
from .geo import _haversine_m
from .pricing import (
    apply_discount,
    calc_price,
    fixed_discount,
    increase_amount,
    is_night_start,
    night_surcharge,
    promo_discount,
    split_rules,
)


def route_guess(lat1, lng1, lat2, lng2, road_factor: str, speed_kmh: str) -> tuple[float, int]:
    """No routing engine: straight-line distance x road factor, duration from an average city speed."""
    km = _haversine_m(lat1, lng1, lat2, lng2) / 1000 * float(road_factor)
    minutes = max(1, round(km / float(speed_kmh) * 60))
    return round(km, 1), minutes


async def route_estimate(lat1, lng1, lat2, lng2, road_factor: str, speed_kmh: str) -> tuple[float, int]:
    """Road distance/duration from the self-hosted OSRM when enabled; falls back to route_guess on any failure."""
    from ..config import settings as _cfg
    if _cfg.map_matching_enabled:
        from .osrm import OsrmClient
        try:
            r = await OsrmClient(timeout=1.5).route((lat1, lng1), (lat2, lng2))
        except Exception:  # noqa: BLE001
            r = None
        if r and r.ok and r.distance_m is not None and r.duration_s is not None:
            return round(r.distance_m / 1000, 1), max(1, round(r.duration_s / 60))
    return route_guess(lat1, lng1, lat2, lng2, road_factor, speed_kmh)


def money_estimate(*, minutes: int, now: datetime, unit_seconds: int, unit_price: Decimal, min_price: Decimal, decimals: int,
                   night_enabled: bool, night_percent: Decimal, night_start: str, night_end: str,
                   ctype: str, cvalue: Decimal, mode: str, outside: bool, outside_percent: Decimal,
                   extras: Decimal = Decimal(0), promo_percent=0, increase_percent=0, promo_fixed=0) -> dict:
    _, base = calc_price(minutes * 60, unit_seconds, unit_price, min_price, decimals)
    night = bool(night_enabled) and is_night_start(now, ZoneInfo(settings.timezone), night_start, night_end)
    extra = night_surcharge(base, night_percent, decimals) if night else Decimal(0).quantize(base)
    inc = increase_amount(base + extra, increase_percent, decimals)  # customer raise / accepted counter-offer (not on the extras)
    price = base + extra + inc + Decimal(extras or 0)  # extras = airport + preference fees (not multiplied by the night surcharge)
    commission, total, driver, cut = split_rules(price, ctype, cvalue, mode, outside, outside_percent, decimals)
    discount = fixed_discount(base + extra + inc, promo_fixed, decimals) if promo_fixed else promo_discount(base + extra + inc, promo_percent, decimals)  # trip price only, never the extras
    commission, total = apply_discount(commission, total, discount)
    return {"base": base, "night_applied": night, "night_surcharge": extra, "extras": Decimal(extras or 0), "price": price, "commission": commission,
            "customer_total": total, "driver_earning": driver, "outside_cut": cut, "discount": discount, "trip_price": base + extra + inc, "increase": inc}


def order_estimate(o, now: datetime, percent: int | None = None) -> dict | None:
    """Estimate for a stored order, from the rules snapshotted on it."""
    if not o.est_duration_min:
        return None
    return money_estimate(minutes=o.est_duration_min, now=now, unit_seconds=o.unit_duration_seconds, unit_price=o.price_per_unit,
                          min_price=o.min_price, decimals=o.currency_decimals, night_enabled=o.night_enabled,
                          night_percent=o.night_percent, night_start=o.night_start, night_end=o.night_end,
                          ctype=o.commission_type, cvalue=o.commission_value, mode=o.commission_mode,
                          outside=bool(o.is_outside_bahrain), outside_percent=o.outside_driver_percent, extras=o.extras_fee or 0, promo_percent=o.promo_percent or 0, promo_fixed=o.promo_fixed or 0,
                          increase_percent=(o.price_increase_percent or 0) if percent is None else percent)

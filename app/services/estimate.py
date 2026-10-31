"""Pre-trip ESTIMATES. The real price is always computed from the actual trip time (start-trip -> arrived).
The app never calculates anything: it shows these server numbers and labels them as estimates."""
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..config import settings
from .geo import _haversine_m
from .pricing import calc_price, is_night_start, night_surcharge, split_rules


def route_guess(lat1, lng1, lat2, lng2, road_factor: str, speed_kmh: str) -> tuple[float, int]:
    """No routing engine: straight-line distance x road factor, duration from an average city speed."""
    km = _haversine_m(lat1, lng1, lat2, lng2) / 1000 * float(road_factor)
    minutes = max(1, round(km / float(speed_kmh) * 60))
    return round(km, 1), minutes


def money_estimate(*, minutes: int, now: datetime, unit_seconds: int, unit_price: Decimal, min_price: Decimal, decimals: int,
                   night_enabled: bool, night_percent: Decimal, night_start: str, night_end: str,
                   ctype: str, cvalue: Decimal, mode: str, outside: bool, outside_percent: Decimal) -> dict:
    _, base = calc_price(minutes * 60, unit_seconds, unit_price, min_price, decimals)
    night = bool(night_enabled) and is_night_start(now, ZoneInfo(settings.timezone), night_start, night_end)
    extra = night_surcharge(base, night_percent, decimals) if night else Decimal(0).quantize(base)
    price = base + extra
    commission, total, driver, cut = split_rules(price, ctype, cvalue, mode, outside, outside_percent, decimals)
    return {"base": base, "night_applied": night, "night_surcharge": extra, "price": price, "commission": commission,
            "customer_total": total, "driver_earning": driver, "outside_cut": cut}


def order_estimate(o, now: datetime) -> dict | None:
    """Estimate for a stored order, from the rules snapshotted on it."""
    if not o.est_duration_min:
        return None
    return money_estimate(minutes=o.est_duration_min, now=now, unit_seconds=o.unit_duration_seconds, unit_price=o.price_per_unit,
                          min_price=o.min_price, decimals=o.currency_decimals, night_enabled=o.night_enabled,
                          night_percent=o.night_percent, night_start=o.night_start, night_end=o.night_end,
                          ctype=o.commission_type, cvalue=o.commission_value, mode=o.commission_mode,
                          outside=bool(o.is_outside_bahrain), outside_percent=o.outside_driver_percent)

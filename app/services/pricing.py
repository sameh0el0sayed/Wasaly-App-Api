"""Server-side pricing, currency-agnostic.
Default rule: 0.500 per 5 minutes or part thereof, minimum 0.500 (BHD). The currency, unit price, unit length and
minimum all come from settings and are snapshotted on each order when it is created."""
from datetime import datetime, time
from decimal import ROUND_HALF_UP, Decimal

UNIT_SECONDS = 300
UNIT_PRICE = Decimal("0.500")
MIN_PRICE = Decimal("0.500")


def quantum(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def fits_decimals(value: Decimal, decimals: int) -> bool:
    return value == value.quantize(quantum(decimals))


def duration_seconds(started: datetime, arrived: datetime) -> int:
    return max(0, int((arrived - started).total_seconds()))


def calc_price(seconds: int, unit_seconds: int = UNIT_SECONDS, unit_price: Decimal = UNIT_PRICE,
               min_price: Decimal = MIN_PRICE, decimals: int = 3) -> tuple[int, Decimal]:
    """units = ceil(seconds / unit_seconds) using integer math (no float rounding); at least 1 unit."""
    q = quantum(decimals)
    units = max(1, -(-int(seconds) // unit_seconds))
    price = max((unit_price * units).quantize(q, ROUND_HALF_UP), min_price.quantize(q, ROUND_HALF_UP))
    return units, price


def calc_commission(price: Decimal, ctype: str, cvalue: Decimal, mode: str, decimals: int = 3
                    ) -> tuple[Decimal, Decimal, Decimal]:
    """Returns (commission, customer_total, driver_earning).
    PERCENT: commission = price * value / 100.   FIXED: commission = value.
    DEDUCT : customer pays price,            driver gets price - commission (commission capped at price).
    ADD    : customer pays price + commission, driver gets price."""
    q = quantum(decimals)
    raw = price * cvalue / Decimal(100) if ctype == "PERCENT" else cvalue
    commission = raw.quantize(q, ROUND_HALF_UP)
    if mode == "ADD":
        return commission, (price + commission).quantize(q), price.quantize(q)
    commission = min(commission, price)
    return commission, price.quantize(q), (price - commission).quantize(q)


def _hm(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


def is_night_start(start: datetime, tz, night_start: str = "00:00", night_end: str = "06:00") -> bool:
    """True when the trip START (local time) is inside the daily night window [night_start, night_end).
    Supports windows that cross midnight (e.g. 22:00 -> 06:00)."""
    t = start.astimezone(tz).time()
    ns, ne = _hm(night_start), _hm(night_end)
    if ns == ne:
        return False
    return (ns <= t < ne) if ns < ne else (t >= ns or t < ne)


def night_surcharge(base_price: Decimal, percent: Decimal, decimals: int = 3) -> Decimal:
    """Full surcharge on the whole trip price: base 2.000 and 25% -> 0.500 (price becomes 2.500)."""
    return (base_price * percent / Decimal(100)).quantize(quantum(decimals), ROUND_HALF_UP)


def outside_driver_cut(price: Decimal, percent: Decimal, driver_earning: Decimal, decimals: int = 3) -> Decimal:
    """Extra company cut taken from the DRIVER share on trips ending outside Bahrain (percent of the trip price).
    Never more than the driver would otherwise receive."""
    if percent <= 0:
        return Decimal(0).quantize(quantum(decimals))
    cut = (price * percent / Decimal(100)).quantize(quantum(decimals), ROUND_HALF_UP)
    return min(cut, driver_earning)


def split_rules(price: Decimal, ctype: str, cvalue: Decimal, mode: str, outside: bool, outside_percent: Decimal,
                decimals: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(company_commission, customer_total, driver_earning, outside_driver_cut) from explicit rules."""
    commission, customer_total, driver = calc_commission(price, ctype, cvalue, mode, decimals)
    cut = Decimal(0).quantize(quantum(decimals))
    if outside:
        cut = outside_driver_cut(price, outside_percent, driver, decimals)
        driver -= cut
        commission += cut
    return commission, customer_total, driver, cut


def promo_discount(trip_price: Decimal, percent, decimals: int = 3) -> Decimal:
    """Percentage promo on the trip price only (base + night surcharge; never on airport/preference extras)."""
    q = quantum(decimals)
    pct = Decimal(str(percent or 0))
    if pct <= 0:
        return Decimal(0).quantize(q)
    return min((trip_price * pct / Decimal(100)).quantize(q, ROUND_HALF_UP), trip_price)


def apply_discount(commission: Decimal, customer_total: Decimal, discount: Decimal) -> tuple[Decimal, Decimal]:
    """The platform pays for the promo: the customer pays less, the driver is untouched, the company keeps less."""
    return commission - discount, customer_total - discount


def split_order(o, price: Decimal) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Full money split of an order from its snapshotted rules."""
    return split_rules(price, o.commission_type, o.commission_value, o.commission_mode,
                       bool(o.is_outside_bahrain), o.outside_driver_percent, o.currency_decimals)

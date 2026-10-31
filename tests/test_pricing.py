from decimal import Decimal

import pytest

from app.services.pricing import calc_price

CASES = [  # (seconds, expected BHD) -- the mandatory table from the spec
    (1, "0.500"), (300, "0.500"), (301, "1.000"), (600, "1.000"), (601, "1.500"), (900, "1.500"),
    (901, "2.000"), (1200, "2.000"), (21 * 60, "2.500"), (25 * 60, "2.500"), (26 * 60, "3.000"),
    (30 * 60, "3.000"), (31 * 60, "3.500"), (0, "0.500"),
]


@pytest.mark.parametrize("secs,expected", CASES)
def test_price_table(secs, expected):
    assert calc_price(secs)[1] == Decimal(expected)


def test_one_second_over_boundary_rounds_up():
    assert calc_price(300)[0] == 1 and calc_price(301)[0] == 2


def test_custom_settings_apply():
    assert calc_price(601, 600, Decimal("1.000"), Decimal("1.000"))[1] == Decimal("2.000")


from app.services.pricing import calc_commission


def test_commission_percent_deduct():
    assert calc_commission(Decimal("1.500"), "PERCENT", Decimal("20"), "DEDUCT") == (Decimal("0.300"), Decimal("1.500"), Decimal("1.200"))


def test_commission_percent_add():
    assert calc_commission(Decimal("1.500"), "PERCENT", Decimal("20"), "ADD") == (Decimal("0.300"), Decimal("1.800"), Decimal("1.500"))


def test_commission_rounding_and_cap():
    # 20% of 0.500 = 0.100 ; fixed fee larger than price is capped in DEDUCT mode
    assert calc_commission(Decimal("0.500"), "PERCENT", Decimal("20"), "DEDUCT")[0] == Decimal("0.100")
    assert calc_commission(Decimal("0.500"), "FIXED", Decimal("2"), "DEDUCT") == (Decimal("0.500"), Decimal("0.500"), Decimal("0.000"))
    assert calc_commission(Decimal("3.75"), "PERCENT", Decimal("20"), "DEDUCT", 2)[0] == Decimal("0.75")


from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.services.pricing import is_night_start, night_surcharge

BH = ZoneInfo("Asia/Bahrain")


def L(h, m, day=30):  # local Bahrain time
    return datetime(2026, 10, day, h, m, tzinfo=BH)


def test_is_night_start_window():
    assert is_night_start(L(0, 0), BH) and is_night_start(L(3, 30), BH) and is_night_start(L(5, 59), BH)
    assert not is_night_start(L(6, 0), BH) and not is_night_start(L(12, 0), BH) and not is_night_start(L(23, 59), BH)
    assert is_night_start(datetime(2026, 10, 29, 21, 0, tzinfo=UTC), BH)  # 00:00 local from UTC input


def test_is_night_start_custom_window_crossing_midnight():
    assert is_night_start(L(23, 0), BH, "22:00", "06:00") and is_night_start(L(1, 0), BH, "22:00", "06:00")
    assert not is_night_start(L(21, 59), BH, "22:00", "06:00")


def test_night_surcharge_full_percent():
    assert night_surcharge(Decimal("2.000"), Decimal(25)) == Decimal("0.500")   # 2.000 -> 2.500
    assert night_surcharge(Decimal("1.500"), Decimal(25)) == Decimal("0.375")
    assert night_surcharge(Decimal("3.75"), Decimal(25), 2) == Decimal("0.94")

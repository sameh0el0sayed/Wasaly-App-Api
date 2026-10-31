"""Fixed extras on top of the time price: airport fee + customer-preference fees. Known before the trip, snapshotted on the order."""
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AirportZone
from .geo import point_in_boundary
from .pricing import quantum


def _fee(s: dict, key: str, decimals: int) -> Decimal:
    return Decimal(s[key]).quantize(quantum(decimals))


async def compute_extras(db: AsyncSession, s: dict, decimals: int, *, ac: bool, smoking: str, gender: str, large_car: bool,
                         pickup: tuple | None, destination: tuple | None) -> tuple[Decimal, list[dict]]:
    """Returns (total, breakdown). One airport fee per trip (the highest zone touched by the pickup or the destination)."""
    items: list[tuple[str, str, Decimal]] = []
    zones = (await db.execute(select(AirportZone).where(AirportZone.is_active.is_(True)))).scalars().all()
    hit = [z for z in zones for pt in (pickup, destination) if pt and point_in_boundary(pt[0], pt[1], z.boundary)]
    if hit:
        z = max(hit, key=lambda z: z.fee)
        items.append(("AIRPORT", z.name, Decimal(z.fee).quantize(quantum(decimals))))
    wanted = [(ac, "AC", "pref_fee_ac"), (smoking == "ALLOWED", "SMOKING_ALLOWED", "pref_fee_smoking_allowed"),
              (smoking == "NOT_ALLOWED", "NON_SMOKING", "pref_fee_non_smoking"), (gender == "FEMALE", "FEMALE_DRIVER", "pref_fee_female_driver"),
              (gender == "MALE", "MALE_DRIVER", "pref_fee_male_driver"), (large_car, "LARGE_CAR", "pref_fee_large_car")]
    for on, key, setting in wanted:
        if on:
            items.append((key, key, _fee(s, setting, decimals)))
    items = [i for i in items if i[2] > 0]
    total = sum((i[2] for i in items), Decimal(0).quantize(quantum(decimals)))
    return total, [{"key": k, "label": label, "amount": f"{amt:.{decimals}f}"} for k, label, amt in items]


def resolve_prefs(body, s: dict) -> dict:
    """Applies the admin-defined defaults to preferences the app did not send."""
    return {"pref_ac": body.pref_ac if body.pref_ac is not None else s["default_pref_ac"] == "true",
            "pref_smoking": body.pref_smoking or s["default_pref_smoking"],
            "pref_driver_gender": body.pref_driver_gender or s["default_pref_driver_gender"],
            "pref_large_car": body.pref_large_car if body.pref_large_car is not None else s["default_pref_large_car"] == "true"}

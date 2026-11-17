"""Unified services: default catalog (all NEW services start DISABLED) and the configurable pricing engine.

Seeded numbers are INDICATIVE starting values for the Bahrain/GCC market, inserted only when missing and editable from
the admin dashboard. Nothing here is applied to customers until an admin enables the service. Rides pricing is untouched."""
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select

from ..database import SessionLocal
from ..models import PricingProfile, ServiceCategory, VehicleCategory

D = Decimal
SERVICES = [("RIDES", "رحلات الركاب", "Rides", True, 1), ("PICKUP", "نقل خفيف (بيك أب)", "Pickup", False, 2),
            ("FREIGHT", "شحن ثقيل", "Freight", False, 3), ("RECOVERY", "سحب وإنقاذ مركبات", "Recovery", False, 4)]
VEHICLES = {
    "PICKUP": [("SMALL_PICKUP", "بيك أب صغير", "Small pickup", 800, 200, 160, 120, 1.0, []),
               ("LARGE_PICKUP", "بيك أب كبير", "Large pickup", 1500, 250, 180, 150, 1.3, []),
               ("VAN", "فان نقل", "Cargo van", 1200, 300, 170, 170, 1.4, [])],
    "FREIGHT": [("TRUCK_3T", "شاحنة 3 طن", "3-ton truck", 3000, 450, 200, 220, 1.0, []),
                ("TRUCK_7T", "شاحنة 7 طن", "7-ton truck", 7000, 600, 240, 250, 1.6, ["LIFT"]),
                ("TRUCK_HEAVY", "شاحنة ثقيلة / تريلا", "Heavy truck / trailer", 20000, 1300, 250, 270, 2.6, ["LIFT", "CRANE"])],
    "RECOVERY": [("FLATBED", "سطحة عادية", "Flatbed", 3500, 600, 220, None, 1.0, ["WINCH"]),
                 ("FLATBED_LOW", "سطحة منخفضة (سيارات رياضية)", "Low-clearance flatbed", 3000, 600, 220, None, 1.3, ["WINCH", "LOW_RAMP"]),
                 ("MOTO_CARRIER", "ناقل دراجات", "Motorcycle carrier", 800, 250, 150, None, 0.6, [])],
}
PROFILES = {  # BHD, indicative - review in the admin dashboard before enabling a service
    "PICKUP": {"base_fare": "2.000", "per_km": "0.250", "per_minute": "0.050", "min_fare": "4.000", "loading_fee": "2.000", "unloading_fee": "2.000",
               "helper_fee": "5.000", "per_extra_stop": "1.500", "waiting_per_minute": "0.100", "free_waiting_minutes": "10", "demand_multiplier": "1",
               "quote_valid_minutes": "30", "equipment_fees": {}},
    "FREIGHT": {"base_fare": "15.000", "per_km": "0.600", "per_minute": "0.100", "min_fare": "25.000", "loading_fee": "10.000", "unloading_fee": "10.000",
                "helper_fee": "8.000", "per_extra_stop": "5.000", "waiting_per_minute": "0.300", "free_waiting_minutes": "30", "demand_multiplier": "1",
                "quote_valid_minutes": "120", "quote_over_weight_kg": "7000", "quote_over_km": "60", "equipment_fees": {"LIFT": "15.000", "CRANE": "40.000"}},
    "RECOVERY": {"base_fare": "5.000", "per_km": "0.400", "per_minute": "0.000", "min_fare": "5.000", "emergency_fee": "5.000", "waiting_per_minute": "0.200",
                 "free_waiting_minutes": "15", "demand_multiplier": "1", "quote_valid_minutes": "30", "equipment_fees": {"WINCH": "8.000", "LOW_RAMP": "5.000"}},
}


async def seed_catalog() -> None:
    """Idempotent: only inserts what is missing, never overwrites an admin's edits."""
    async with SessionLocal() as db:
        have = {c.code for c in (await db.execute(select(ServiceCategory))).scalars()}
        for code, ar, en, active, order in SERVICES:
            if code not in have:
                db.add(ServiceCategory(code=code, name_ar=ar, name_en=en, is_active=active, sort_order=order, config={}))
        await db.flush()
        vc = {(v.service_code, v.code) for v in (await db.execute(select(VehicleCategory))).scalars()}
        for svc, rows in VEHICLES.items():
            for code, ar, en, kg, ln, wd, ht, mult, eq in rows:
                if (svc, code) not in vc:
                    db.add(VehicleCategory(service_code=svc, code=code, name_ar=ar, name_en=en, max_weight_kg=kg, max_length_cm=ln, max_width_cm=wd,
                                           max_height_cm=ht, price_multiplier=D(str(mult)), equipment=eq))
        pp = {p.service_code for p in (await db.execute(select(PricingProfile))).scalars()}
        for svc, params in PROFILES.items():
            if svc not in pp:
                db.add(PricingProfile(service_code=svc, params=params))
        rec = await db.get(PricingProfile, "RECOVERY")  # owner decision: flatbed starts at 5 BHD (only replaces the untouched first seed)
        if rec and rec.params.get("base_fare") == "10.000" and rec.params.get("min_fare") == "12.000":
            rec.params = {**rec.params, "base_fare": "5.000", "min_fare": "5.000"}
        await db.commit()


def _d(params: dict, key: str) -> Decimal:
    try:
        return D(str(params.get(key) or 0))
    except Exception:  # noqa: BLE001
        return D(0)


def price_service(params: dict, multiplier: Decimal, *, km: float, minutes: int, decimals: int = 3, loading: bool = False, unloading: bool = False,
                  helpers: int = 0, extra_stops: int = 0, equipment: list[str] | None = None, emergency: bool = False) -> tuple[Decimal, list[dict]]:
    """Every rate comes from the profile (admin-editable). Missing rate = 0. Returns (price, breakdown)."""
    q = D(1).scaleb(-decimals)
    parts: list[tuple[str, Decimal]] = []
    core = _d(params, "base_fare") + _d(params, "per_km") * D(str(km)) + _d(params, "per_minute") * D(minutes)
    core *= multiplier  # the vehicle category scales the distance/time part
    parts.append(("DISTANCE_TIME", core))
    if loading:
        parts.append(("LOADING", _d(params, "loading_fee")))
    if unloading:
        parts.append(("UNLOADING", _d(params, "unloading_fee")))
    if helpers:
        parts.append(("HELPERS", _d(params, "helper_fee") * helpers))
    if extra_stops:
        parts.append(("EXTRA_STOPS", _d(params, "per_extra_stop") * extra_stops))
    fees = params.get("equipment_fees") or {}
    for code in equipment or []:
        parts.append((f"EQUIPMENT_{code}", D(str(fees.get(code) or 0))))
    if emergency:
        parts.append(("EMERGENCY", _d(params, "emergency_fee")))
    subtotal = sum((a for _, a in parts), D(0))
    floor = _d(params, "min_fare")
    if subtotal < floor:
        parts.append(("MIN_FARE_ADJUSTMENT", floor - subtotal))
        subtotal = floor
    demand = _d(params, "demand_multiplier") or D(1)
    if demand != 1:
        parts.append(("DEMAND", subtotal * (demand - 1)))
        subtotal *= demand
    return subtotal.quantize(q, ROUND_HALF_UP), [{"key": k, "amount": f"{a.quantize(q, ROUND_HALF_UP)}"} for k, a in parts if a]


def is_priced(params: dict) -> bool:
    """A profile with no rates at all cannot produce a price: the request goes to manual quoting."""
    return any(_d(params, k) > 0 for k in ("base_fare", "per_km", "per_minute", "min_fare"))

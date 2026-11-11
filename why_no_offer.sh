cd /opt/wasaly-api && docker compose exec -T api python - <<'PYEOF'
"""Why didn't drivers get the last orders? Read-only: changes nothing.
Run inside the api container:  docker compose exec -T api python - < why_no_offer.py"""
import asyncio
from datetime import date, timedelta

from sqlalchemy import select

from app.constants import Role
from app.database import SessionLocal
from app.models import DriverServiceArea, Order, OrderOffer, OrderStatusHistory, ServiceArea, User, utcnow
from app.services import settings_service
from app.services.dispatch import has_active_order, work_blockers
from app.services.geo import _haversine_m, find_area


async def main():
    async with SessionLocal() as db:
        cfg = await settings_service.get_all(db)
        print("== settings ==")
        for k in ("order_timeout_seconds", "offer_ttl_seconds", "offer_max_drivers", "dispatch_nearest_first",
                  "dispatch_max_pickup_km", "live_location_max_age_seconds", "default_driver_gender"):
            print(f"  {k} = {cfg.get(k)}")

        areas = (await db.execute(select(ServiceArea).order_by(ServiceArea.id))).scalars().all()
        print("\n== service areas ==")
        for a in areas:
            print(f"  #{a.id} {a.name!r} active={a.is_active} boundary={a.boundary}")

        orders = (await db.execute(select(Order).order_by(Order.id.desc()).limit(3))).scalars().all()
        drivers = (await db.execute(select(User).where(User.role == Role.DRIVER))).scalars().all()
        links = (await db.execute(select(DriverServiceArea))).scalars().all()
        by_driver = {}
        for l in links:
            by_driver.setdefault(l.driver_id, set()).add(l.service_area_id)

        print("\n== drivers ==")
        for d in drivers:
            p, v = d.driver_profile, d.vehicle
            print(f"  driver #{d.id} {d.full_name!r} phone={d.phone} account={d.account_status} "
                  f"online={p.is_online if p else None} mode={p.mode if p else None} areas={sorted(by_driver.get(d.id, []))}")
            if p:
                age = (utcnow() - p.last_location_at).total_seconds() if p.last_location_at else None
                print(f"      last_location={p.last_lat},{p.last_lng} age_s={age and round(age)} gender={p.gender} "
                      f"smoking={p.smoking_allowed}")
            if v:
                print(f"      vehicle type={v.vehicle_type} seats={v.passenger_capacity} cargo_kg={v.cargo_capacity_kg} ac={v.has_ac}")
            print(f"      blockers={work_blockers(d) or 'none'}  busy_with_order={await has_active_order(db, d.id)}")

        for o in orders:
            print(f"\n== order #{o.id} status={o.order_status} type={o.service_type} created={o.created_at}")
            print(f"   pickup={o.pickup_address!r} ({o.pickup_latitude},{o.pickup_longitude}) service_area_id={o.service_area_id}")
            if o.pickup_latitude is not None:
                fa = await find_area(db, o.pickup_latitude, o.pickup_longitude)
                print(f"   pickup is inside area: {fa and (fa.id, fa.name)}")
            print(f"   prefs: ac={o.pref_ac} smoking={o.pref_smoking} gender={o.pref_driver_gender} large={o.pref_large_car} "
                  f"passengers={o.passenger_count} cargo_kg={o.cargo_weight_kg}")
            offers = (await db.execute(select(OrderOffer).where(OrderOffer.order_id == o.id))).scalars().all()
            print(f"   offers: {[(x.driver_id, x.offer_status) for x in offers] or 'NONE'}")
            hist = (await db.execute(select(OrderStatusHistory).where(OrderStatusHistory.order_id == o.id)
                                     .order_by(OrderStatusHistory.id))).scalars().all()
            for h in hist:
                print(f"     history: {h.changed_at} {h.old_status} -> {h.new_status} {h.notes or ''}")
            for d in drivers:
                p, v = d.driver_profile, d.vehicle
                why = []
                if o.service_area_id not in by_driver.get(d.id, set()):
                    why.append(f"NOT_ASSIGNED_TO_AREA {o.service_area_id} (driver areas={sorted(by_driver.get(d.id, []))})")
                if not p or not p.is_online:
                    why.append("OFFLINE")
                why += work_blockers(d)
                if p and v:
                    if o.pref_ac and not v.has_ac:
                        why.append("NO_AC")
                    if o.pref_smoking == "ALLOWED" and not p.smoking_allowed:
                        why.append("SMOKING")
                    if o.pref_smoking == "NOT_ALLOWED" and p.smoking_allowed:
                        why.append("SMOKING")
                    if o.pref_driver_gender in ("MALE", "FEMALE") and (p.gender or cfg["default_driver_gender"]) != o.pref_driver_gender:
                        why.append(f"GENDER {p.gender or cfg['default_driver_gender']} != {o.pref_driver_gender}")
                    if o.service_type == "PERSON" and o.passenger_count and o.passenger_count > v.passenger_capacity:
                        why.append(f"SEATS {o.passenger_count} > {v.passenger_capacity}")
                    if p.mode == "LIVE" and p.last_lat is not None and o.pickup_latitude is not None:
                        km = _haversine_m(p.last_lat, p.last_lng, o.pickup_latitude, o.pickup_longitude) / 1000
                        lim = float(cfg["dispatch_max_pickup_km"])
                        fresh = p.last_location_at and utcnow() - p.last_location_at <= timedelta(
                            seconds=int(cfg["live_location_max_age_seconds"]))
                        if fresh and lim > 0 and km > lim:
                            why.append(f"TOO_FAR {km:.1f}km > {lim}km")
                print(f"   driver #{d.id}: {'ELIGIBLE' if not why else 'excluded -> ' + '; '.join(why)}")


asyncio.run(main())
PYEOF

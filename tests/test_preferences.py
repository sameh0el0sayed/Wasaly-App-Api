from sqlalchemy import update

from app.database import SessionLocal
from app.models import Vehicle
from tests.conftest import DEST, PICKUP, auth, order_body, setup_world
from tests.test_flow import _complete_trip

AIRPORT = {"name": "Test Airport", "fee": "2", "boundary": {"type": "circle", "center": [26.22, 50.57], "radius_m": 1000}}


async def _patch(client, admin, **kw):
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={**kw, "reason": "tests"})
    assert r.status_code == 200, r.text


async def _order(client, ct, **kw):
    r = await client.post("/api/orders", headers=auth(ct), json=order_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


async def test_airport_fee_is_added_to_estimate_and_final_price(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    assert (await client.post("/api/admin/airport-zones", headers=auth(admin), json=AIRPORT)).status_code == 201
    est = (await client.post("/api/orders/estimate", headers=auth(ct), json={**PICKUP, **DEST})).json()
    assert est["airport_applied"] is True and est["extras_fee"] == "2.000" and est["extras"][0]["key"] == "AIRPORT"
    o = await _order(client, ct)
    assert o["extras_fee"] == "2.000"
    done = await _complete_trip(client, o["id"], dt, 20)  # 20 min = 2.000 time price + 2.000 airport fee
    assert done["final_price"] == "4.000"
    # a trip that does not touch the airport pays nothing extra
    await _patch(client, admin, offer_ttl_seconds=10)
    far = {"pickup_latitude": 26.245, "pickup_longitude": 50.53, "destination_latitude": 26.24, "destination_longitude": 50.54}
    assert (await client.post("/api/orders/estimate", headers=auth(ct), json=far)).json()["airport_applied"] is False


async def test_airport_admin_rules(client):
    admin, _, (cid, ct), _ = await setup_world(client)
    assert (await client.post("/api/admin/airport-zones", headers=auth(ct), json=AIRPORT)).status_code == 403
    zid = (await client.post("/api/admin/airport-zones", headers=auth(admin), json=AIRPORT)).json()["id"]
    assert (await client.patch(f"/api/admin/airport-zones/{zid}", headers=auth(admin), json={"fee": "3.5"})).json()["fee"] == "3.500"
    assert len((await client.get("/api/admin/airport-zones", headers=auth(admin))).json()) == 1
    assert (await client.delete(f"/api/admin/airport-zones/{zid}", headers=auth(admin))).status_code == 200


async def test_large_car_preference_is_required_and_charged(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _patch(client, admin, pref_fee_large_car=1.5)
    o = await _order(client, ct, pref_large_car=True)
    assert o["status"] == "NO_DRIVER_AVAILABLE"  # the only car is a sedan
    async with SessionLocal() as db:
        await db.execute(update(Vehicle).where(Vehicle.driver_id == did).values(vehicle_type="SUV"))
        await db.commit()
    r = await client.post(f"/api/orders/{o['id']}/retry", headers=auth(ct))
    assert r.json()["status"] == "OFFERED" and r.json()["extras_fee"] == "1.500"
    assert r.json()["preferences"]["large_car"] is True
    offer = (await client.get("/api/driver/offers", headers=auth(dt))).json()[0]
    assert offer["order"]["preferences"]["large_car"] is True and offer["order"]["extras_fee"] == "1.500"


async def test_gender_smoking_and_ac_preferences(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    # gender: the driver has none -> cannot get a "female driver" order until an admin sets it
    o = await _order(client, ct, pref_driver_gender="FEMALE")
    assert o["status"] == "NO_DRIVER_AVAILABLE"
    assert (await client.patch(f"/api/admin/drivers/{did}/gender", headers=auth(dt), json={"gender": "FEMALE", "reason": "docs checked"})).status_code == 403
    assert (await client.patch(f"/api/admin/drivers/{did}/gender", headers=auth(admin), json={"gender": "FEMALE", "reason": "docs checked"})).status_code == 200
    assert (await client.post(f"/api/orders/{o['id']}/retry", headers=auth(ct))).json()["status"] == "OFFERED"
    await client.post(f"/api/orders/{o['id']}/cancel", headers=auth(ct), json={"reason": "test"})
    # smoking: driver default = not allowed
    o2 = await _order(client, ct, pref_smoking="ALLOWED")
    assert o2["status"] == "NO_DRIVER_AVAILABLE"
    await client.post(f"/api/orders/{o2['id']}/cancel", headers=auth(ct), json={"reason": "test"})
    assert (await client.patch("/api/driver/preferences", headers=auth(dt), json={"smoking_allowed": True})).json()["smoking_allowed"] is True
    o3 = await _order(client, ct, pref_smoking="ALLOWED")
    assert o3["status"] == "OFFERED"
    await client.post(f"/api/orders/{o3['id']}/cancel", headers=auth(ct), json={"reason": "test"})
    o3b = await _order(client, ct, pref_smoking="NOT_ALLOWED")
    assert o3b["status"] == "NO_DRIVER_AVAILABLE"
    await client.post(f"/api/orders/{o3b['id']}/cancel", headers=auth(ct), json={"reason": "test"})
    # AC
    async with SessionLocal() as db:
        await db.execute(update(Vehicle).where(Vehicle.driver_id == did).values(has_ac=False))
        await db.commit()
    assert (await _order(client, ct, pref_ac=True))["status"] == "NO_DRIVER_AVAILABLE"


async def test_preferences_default_to_no_extras(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    o = await _order(client, ct)
    assert o["extras_fee"] == "0.000" and o["extras"] == [] and o["preferences"]["smoking"] == "ANY"
    cfg = (await client.get("/api/app-config")).json()
    assert cfg["preference_fees"]["large_car"] == "0" and "SUV" in cfg["large_car_vehicle_types"]


async def test_defaults_driver_gender_male_and_admin_defined_order_defaults(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    # a driver with no gender counts as MALE (setting default_driver_gender)
    o = await _order(client, ct, pref_driver_gender="MALE")
    assert o["status"] == "OFFERED"
    await client.post(f"/api/orders/{o['id']}/cancel", headers=auth(ct), json={"reason": "test"})
    cfg = (await client.get("/api/app-config")).json()
    assert cfg["preference_defaults"] == {"ac": False, "smoking": "ANY", "driver_gender": "ANY", "large_car": False}
    # admin changes the defaults: an order that sends nothing now asks for a female driver; sending a value still wins
    await _patch(client, admin, default_pref_driver_gender="FEMALE")
    o2 = await _order(client, ct)
    assert o2["preferences"]["driver_gender"] == "FEMALE" and o2["status"] == "NO_DRIVER_AVAILABLE"
    await client.post(f"/api/orders/{o2['id']}/cancel", headers=auth(ct), json={"reason": "test"})
    o3 = await _order(client, ct, pref_driver_gender="ANY")
    assert o3["preferences"]["driver_gender"] == "ANY" and o3["status"] == "OFFERED"
    # estimate uses the same defaults
    await _patch(client, admin, default_pref_driver_gender="ANY", default_driver_gender="FEMALE", pref_fee_male_driver=1)
    est = (await client.post("/api/orders/estimate", headers=auth(ct), json={**PICKUP, **DEST, "pref_driver_gender": "MALE"})).json()
    assert est["extras_fee"] == "1.000"

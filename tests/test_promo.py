from datetime import timedelta

from app.models import utcnow
from tests.conftest import auth, order_body, setup_world
from tests.test_flow import _complete_trip

EST = {"pickup_latitude": 26.22, "pickup_longitude": 50.57, "destination_latitude": 26.25, "destination_longitude": 50.6}


async def _promo(client, admin, **kw):
    r = await client.post("/api/admin/promo-codes", headers=auth(admin), json={"code": "welcome10", "percent": 10, **kw})
    assert r.status_code == 201, r.text
    return r.json()


async def test_promo_admin_validation_and_permissions(client):
    admin, _, (cid, ct), _ = await setup_world(client)
    p = await _promo(client, admin)
    assert p["code"] == "WELCOME10"  # normalised to upper case
    assert (await client.post("/api/admin/promo-codes", headers=auth(admin), json={"code": "Welcome10", "percent": 5})).status_code == 409
    for bad in ({"percent": 0}, {"percent": 101}, {"code": "a b"}, {"starts_at": "2030-01-02T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z"}):
        r = await client.post("/api/admin/promo-codes", headers=auth(admin), json={"code": "OTHER1", "percent": 10, **bad})
        assert r.status_code == 422, bad
    assert (await client.post("/api/admin/promo-codes", headers=auth(ct), json={"code": "HACK", "percent": 90})).status_code == 403
    assert (await client.get("/api/admin/promo-codes", headers=auth(ct))).status_code == 403
    r = await client.patch(f"/api/admin/promo-codes/{p['id']}", headers=auth(admin), json={"percent": 15, "min_order_amount": "1.000"})
    assert r.json()["percent"] == 15 and r.json()["min_order_amount"] == "1.000"
    assert len((await client.get("/api/admin/promo-codes", headers=auth(admin))).json()["items"]) == 1
    assert (await client.delete(f"/api/admin/promo-codes/{p['id']}", headers=auth(admin))).status_code == 204
    assert (await client.delete(f"/api/admin/promo-codes/{p['id']}", headers=auth(admin))).status_code == 404


async def test_promo_rules_on_estimate_and_order(client):
    admin, _, (cid, ct), _ = await setup_world(client)
    await _promo(client, admin)

    async def err(code, **extra):
        r = await client.post("/api/orders/estimate", headers=auth(ct), json={**EST, "promo_code": code, **extra})
        return r.status_code, (r.json().get("detail") or {}).get("code")

    assert await err("nope") == (422, "PROMO_INVALID")
    r = await client.post("/api/orders/estimate", headers=auth(ct), json={**EST, "promo_code": " welcome10 "})
    j = r.json()
    assert r.status_code == 200 and j["promo_code"] == "WELCOME10" and j["promo_percent"] == 10 and float(j["discount_amount"]) > 0
    plain = (await client.post("/api/orders/estimate", headers=auth(ct), json=EST)).json()
    assert plain["discount_amount"] == "0.000" and plain["promo_code"] is None
    assert float(j["estimated_total"]) < float(plain["estimated_total"])
    # window / min order / switch
    await _promo(client, admin, code="OLD1", ends_at=(utcnow() - timedelta(days=1)).isoformat())
    await _promo(client, admin, code="SOON1", starts_at=(utcnow() + timedelta(days=1)).isoformat())
    await _promo(client, admin, code="BIG1", min_order_amount="50.000")
    assert await err("OLD1") == (422, "PROMO_EXPIRED")
    assert await err("SOON1") == (422, "PROMO_NOT_STARTED")
    assert await err("BIG1") == (422, "PROMO_MIN_ORDER")
    assert (await client.post("/api/orders", headers=auth(ct), json=order_body(promo_code="BIG1"))).status_code == 422
    await client.patch("/api/admin/settings", headers=auth(admin), json={"promo_enabled": False, "reason": "tests"})
    assert (await client.get("/api/app-config")).json()["promo_enabled"] is False
    assert await err("WELCOME10") == (422, "PROMO_DISABLED")


async def test_promo_discount_is_paid_by_platform_and_excludes_extras(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)  # plain: 20% DEDUCT commission, night off
    await _promo(client, admin)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"pref_fee_ac": "0.400", "reason": "tests"})
    r = await client.post("/api/orders", headers=auth(ct), json=order_body(promo_code="WELCOME10", pref_ac=True))
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    assert r.json()["promo_code"] == "WELCOME10" and r.json()["promo_percent"] == 10
    # the code is edited afterwards: the order keeps its snapshot
    code_id = (await client.get("/api/admin/promo-codes", headers=auth(admin))).json()["items"][0]["id"]
    await client.patch(f"/api/admin/promo-codes/{code_id}", headers=auth(admin), json={"percent": 90})
    done = await _complete_trip(client, oid, dt, 11)  # 11 min -> trip 1.500 + AC 0.400 = 1.900
    assert done["final_price"] == "1.900" and done["extras_fee"] == "0.400"
    assert done["discount_amount"] == "0.150"          # 10% of 1.500 only (extras excluded)
    assert done["driver_earning"] == "1.520"           # unchanged: 1.900 - 20% commission (0.380)
    assert done["platform_fee"] == "0.380"             # the driver still sees the normal fee
    mine = (await client.get(f"/api/orders/{oid}", headers=auth(ct))).json()
    assert mine["customer_total"] == "1.750"           # customer pays 1.900 - 0.150
    pay = (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()
    assert pay["commission"] == "0.230"                # platform keeps 0.380 - 0.150
    assert pay["promo_code"] == "WELCOME10" and pay["discount_amount"] == "0.150"
    # payment rows add up: driver + company == amount
    from sqlalchemy import select

    from app.database import SessionLocal
    from app.models import Payment
    async with SessionLocal() as db:
        p = (await db.execute(select(Payment).where(Payment.order_id == oid))).scalar_one()
        assert p.amount == p.driver_amount + p.company_amount

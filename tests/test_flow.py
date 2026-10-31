import asyncio
from datetime import UTC, timedelta

from sqlalchemy import update

from app.database import SessionLocal
from app.models import Order, OrderOffer, utcnow
from app.models import utcnow as real_utcnow

from .conftest import KHOBAR, auth, login, make_area, order_body, register_customer, register_driver, setup_world


async def test_no_otp_endpoints(client):
    paths = (await client.get("/openapi.json")).json()["paths"]
    assert not [p for p in paths if "otp" in p.lower()]


async def test_pending_customer_cannot_order_and_sees_status(client):
    _, ctoken, _ = await register_customer(client)
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body())
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ACCOUNT_NOT_APPROVED"
    r = await client.get("/api/account/status", headers=auth(ctoken))
    assert r.json()["account_status"] == "PENDING_APPROVAL"


async def test_pending_driver_cannot_go_online(client):
    _, dtoken, _ = await register_driver(client)
    r = await client.post("/api/driver/availability", headers=auth(dtoken), json={"online": True})
    assert r.status_code == 403


async def test_duplicate_cpr_and_phone_rejected(client):
    await register_customer(client)
    r = await client.post("/api/auth/register", json={"role": "CUSTOMER", "full_name": "Xavier", "phone": "33000001",
                                                       "cpr": "900100001", "password": "Passw0rd!", "password_confirm": "Passw0rd!"})
    assert r.status_code == 409


async def test_full_trip_and_pricing_from_server_clock(client):
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body())
    assert r.status_code == 201 and r.json()["status"] == "OFFERED", r.text
    oid = r.json()["id"]
    offers = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()
    assert len(offers) == 1 and offers[0]["seconds_left"] <= 10
    assert "phone" not in str(offers[0]["order"]).lower()
    r = await client.post(f"/api/driver/offers/{offers[0]['id']}/accept", headers=auth(dtoken))
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"

    # cannot skip steps
    r = await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dtoken))
    assert r.status_code == 409
    for step in ("on-the-way", "arrived-at-pickup"):
        assert (await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dtoken))).status_code == 200
    r = await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dtoken))
    assert r.status_code == 200 and r.json()["trip_started_at"]
    started = r.json()["trip_started_at"]
    # idempotent retry never moves the start time
    assert (await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dtoken))).json()["trip_started_at"] == started

    # pretend the trip started 11 minutes ago -> 3 units -> 1.500
    async with SessionLocal() as db:
        await db.execute(update(Order).where(Order.id == oid).values(trip_started_at=utcnow() - timedelta(minutes=11)))
        await db.commit()
    r = await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dtoken))
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    assert r.json()["final_price"] == "1.500" and r.json()["currency"] == "BHD"
    c = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()
    assert c["final_price"] == "1.500" and c["driver"]["vehicle"]["plate_number"]
    assert "cpr" not in str(c).lower()
    hist = (await client.get(f"/api/orders/{oid}/status-history", headers=auth(ctoken))).json()
    assert [h["new_status"] for h in hist][-1] == "COMPLETED" and len(hist) >= 7
    # server ignores any client-supplied price
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body(final_price=0.001))
    assert r.json()["final_price"] is None


async def test_only_one_driver_wins_the_order(client):
    admin, area, (cid, ctoken), drivers = await setup_world(client, drivers=3)
    oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    offer_ids = []
    for _, t in drivers:
        offer_ids.append((await client.get("/api/driver/offers", headers=auth(t))).json()[0]["id"])
    results = await asyncio.gather(*[client.post(f"/api/driver/offers/{o}/accept", headers=auth(t))
                                     for o, (_, t) in zip(offer_ids, drivers)])
    codes = sorted(r.status_code for r in results)
    assert codes == [200, 409, 409], codes
    o = (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()
    assert o["status"] == "ACCEPTED" and o["driver"]


async def test_offer_expires_after_10s_and_moves_to_unaccepted(client):
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
    async with SessionLocal() as db:
        await db.execute(update(OrderOffer).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    r = await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dtoken))
    assert r.status_code == 409
    assert (await client.get("/api/driver/offers", headers=auth(dtoken))).json() == []
    un = (await client.get("/api/driver/offers?list=unaccepted", headers=auth(dtoken))).json()
    assert len(un) == 1 and un[0]["status"] == "EXPIRED"
    # the CUSTOMER'S order is not cancelled, only 'no driver yet'; retry re-offers
    o = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()
    assert o["status"] == "NO_DRIVER_AVAILABLE"
    r = await client.post(f"/api/orders/{oid}/retry", headers=auth(ctoken))
    assert r.json()["status"] == "OFFERED"


async def test_offers_only_to_drivers_in_pickup_area(client):
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    other = await make_area(client, admin, "Far Away")
    await client.put(f"/api/admin/drivers/{did}/service-areas", headers=auth(admin), json={"service_area_ids": [other]})
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body())
    assert r.json()["status"] == "NO_DRIVER_AVAILABLE"
    assert (await client.get("/api/driver/offers", headers=auth(dtoken))).json() == []


async def test_expired_license_blocks_driver(client):
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    from datetime import date

    from app.models import DriverProfile
    async with SessionLocal() as db:
        await db.execute(update(DriverProfile).where(DriverProfile.user_id == did).values(license_expiry_date=date(2020, 1, 1)))
        await db.commit()
    from app.services.dispatch import enforce_driver_documents
    async with SessionLocal() as db:
        await enforce_driver_documents(db)
        await db.commit()
    r = await client.post("/api/driver/availability", headers=auth(dtoken), json={"online": True})
    assert r.status_code == 403 and "LICENSE_EXPIRED" in r.json()["detail"]["reasons"]


async def test_permissions_and_isolation(client):
    admin, area, (cid, ctoken), _ = await setup_world(client)
    assert (await client.get("/api/admin/users", headers=auth(ctoken))).status_code == 403
    oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    c2, t2, _ = await register_customer(client, "Other")
    await client.post(f"/api/admin/users/{c2}/approve", headers=auth(admin), json={})
    assert (await client.get(f"/api/orders/{oid}", headers=auth(t2))).status_code == 404
    assert (await client.post(f"/api/orders/{oid}/cancel", headers=auth(t2), json={})).status_code == 404
    u = (await client.get(f"/api/admin/users/{cid}", headers=auth(admin))).json()
    assert u["cpr_masked"].startswith("******") and "cpr" not in u


async def test_customer_cancel_and_double_order_guard(client):
    admin, area, (cid, ctoken), _ = await setup_world(client)
    oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    assert (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).status_code == 409
    r = await client.post(f"/api/orders/{oid}/cancel", headers=auth(ctoken), json={"reason": "changed mind"})
    assert r.json()["status"] == "CANCELLED"
    assert (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).status_code == 201


async def test_price_settings_super_admin_only_and_audited(client):
    admin, area, (cid, ctoken), _ = await setup_world(client)
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={"unit_price": "0.600", "reason": "test"})
    assert r.status_code == 200 and r.json()["unit_price"] == "0.600"
    logs = (await client.get("/api/admin/audit-logs?action=SETTINGS_CHANGED", headers=auth(admin))).json()
    assert logs and logs[0]["reason"] == "test"
    plain = await client.post("/api/admin/admins", headers=auth(admin), json={"full_name": "Op", "phone": "35000000", "password": "Passw0rd!", "role": "ADMIN"})
    t = await login(client, "35000000", "Passw0rd!")
    assert (await client.patch("/api/admin/settings", headers=auth(t), json={"unit_price": "9", "reason": "nope"})).status_code == 403


async def test_reports_and_csv(client):
    admin, area, (cid, ctoken), _ = await setup_world(client)
    r = await client.get("/api/admin/reports/summary?date_from=2020-01-01&date_to=2099-01-01", headers=auth(admin))
    assert r.status_code == 200 and r.json()["rows"][0]["orders_total"] == 0
    r = await client.get("/api/admin/reports/trips?date_from=2020-01-01&date_to=2099-01-01&format=csv", headers=auth(admin))
    assert r.status_code == 200 and "text/csv" in r.headers["content-type"]
    assert (await client.get("/api/admin/dashboard", headers=auth(admin))).status_code == 200


async def test_login_lockout(client):
    await register_customer(client)
    for _ in range(5):
        r = await client.post("/api/auth/login", json={"phone": "33000001", "password": "wrong-pass"})
    r = await client.post("/api/auth/login", json={"phone": "33000001", "password": "Passw0rd!"})
    assert r.status_code == 429


async def _complete_trip(client, oid, dtoken, minutes):
    offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
    await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dtoken))
    for step in ("on-the-way", "arrived-at-pickup", "start-trip"):
        await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dtoken))
    async with SessionLocal() as db:
        await db.execute(update(Order).where(Order.id == oid).values(trip_started_at=utcnow() - timedelta(minutes=minutes)))
        await db.commit()
    return (await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dtoken))).json()


async def test_multi_currency_by_setting(client):
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    # 1) default BHD order, 11 min -> 1.500 BHD
    oid1 = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    r1 = await _complete_trip(client, oid1, dtoken, 11)
    assert (r1["currency"], r1["final_price"]) == ("BHD", "1.500")

    # 2) changing currency without new prices is refused (0.500 BHD is not 0.500 USD)
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={"currency_code": "USD", "reason": "go global"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PRICES_REQUIRED_WITH_CURRENCY_CHANGE"
    # too many decimals for USD
    r = await client.patch("/api/admin/settings", headers=auth(admin),
                           json={"currency_code": "USD", "unit_price": "1.255", "min_price": "1.25", "reason": "too precise"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "TOO_MANY_DECIMALS"
    r = await client.patch("/api/admin/settings", headers=auth(admin),
                           json={"currency_code": "usd", "unit_price": "1.25", "min_price": "1.25", "reason": "go global"})
    assert r.status_code == 200 and r.json()["currency_code"] == "USD"
    pr = (await client.get("/api/pricing")).json()
    assert pr["currency"] == "USD" and pr["currency_decimals"] == 2 and pr["unit_price"] == "1.25"

    # 3) new order uses USD, 2 decimals: 11 min -> 3 units x 1.25 = 3.75
    oid2 = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    r2 = await _complete_trip(client, oid2, dtoken, 11)
    assert (r2["currency"], r2["final_price"]) == ("USD", "3.75")

    # 4) old order keeps its own currency and price
    old = (await client.get(f"/api/orders/{oid1}", headers=auth(ctoken))).json()
    assert (old["currency"], old["final_price"]) == ("BHD", "1.500")

    # 5) reports never mix currencies; unsupported currency rejected
    s = (await client.get("/api/admin/reports/summary?date_from=2020-01-01&date_to=2099-01-01", headers=auth(admin))).json()
    assert s["rows"][0]["trip_value_by_currency"] == {"BHD": "1.500", "USD": "3.75"}
    assert s["rows"][0]["company_profit_by_currency"] == {"BHD": "0.300", "USD": "0.75"}
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={"currency_code": "XYZ", "reason": "bad"})
    assert r.status_code == 422
    assert {"code": "JPY", "decimals": 0} in (await client.get("/api/currencies")).json()


async def test_commission_deduct_then_add_from_settings(client):
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    # default: 20% DEDUCT -> trip 1.500 : customer pays 1.500, driver 1.200, company 0.300
    oid1 = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    r = await _complete_trip(client, oid1, dtoken, 11)
    assert (r["final_price"], r["driver_earning"], r["platform_fee"]) == ("1.500", "1.200", "0.300")
    c = (await client.get(f"/api/orders/{oid1}", headers=auth(ctoken))).json()
    assert (c["customer_total"], c["service_fee"]) == ("1.500", None)
    a = (await client.get(f"/api/admin/orders/{oid1}", headers=auth(admin))).json()
    assert (a["commission"], a["customer_total"], a["driver_earning"], a["commission_mode"]) == ("0.300", "1.500", "1.200", "DEDUCT")

    # switch to ADD from settings -> new orders: customer pays 1.800, driver 1.500
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={"commission_mode": "ADD", "reason": "add on top"})
    assert r.status_code == 200 and r.json()["commission_mode"] == "ADD"
    pr = (await client.get("/api/pricing")).json()
    assert pr["service_fee"] == {"type": "PERCENT", "value": "20"}
    oid2 = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    r = await _complete_trip(client, oid2, dtoken, 11)
    assert (r["final_price"], r["driver_earning"], r["platform_fee"]) == ("1.500", "1.500", None)
    c = (await client.get(f"/api/orders/{oid2}", headers=auth(ctoken))).json()
    assert (c["customer_total"], c["service_fee"]) == ("1.800", "0.300")
    # old order untouched
    assert (await client.get(f"/api/orders/{oid1}", headers=auth(ctoken))).json()["customer_total"] == "1.500"

    # payment amount = what the customer pays; adjust price recalculates split
    r = await client.post(f"/api/admin/orders/{oid2}/adjust-price", headers=auth(admin), json={"new_price": "2.000", "reason": "dispute"})
    assert (r.json()["commission"], r.json()["customer_total"], r.json()["driver_earning"]) == ("0.400", "2.400", "2.000")

    # validation: percent > 100 rejected; fixed commission is possible
    assert (await client.patch("/api/admin/settings", headers=auth(admin), json={"commission_value": "150", "reason": "bad"})).status_code == 422
    r = await client.patch("/api/admin/settings", headers=auth(admin),
                           json={"commission_type": "FIXED", "commission_value": "0.200", "reason": "fixed fee"})
    assert r.status_code == 200
    # driver earnings endpoint
    e = (await client.get("/api/driver/earnings", headers=auth(dtoken))).json()["by_currency"][0]
    assert e["currency"] == "BHD" and e["completed_trips"] == 2



async def test_night_surcharge_full_when_trip_starts_at_night(client, monkeypatch):
    from datetime import datetime

    import app.routers.driver as drv
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    r = await client.patch("/api/admin/settings", headers=auth(admin),
                           json={"night_surcharge_enabled": True, "commission_mode": "ADD", "reason": "night on, add mode"})
    assert r.json()["night_surcharge_percent"] == "25" and r.json()["night_end"] == "06:00"
    assert "starts inside the window" in (await client.get("/api/pricing")).json()["night_surcharge"]["applies_to"]

    async def run_trip(start_utc, end_utc):
        oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
        offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
        await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dtoken))
        for step in ("on-the-way", "arrived-at-pickup", "start-trip"):
            await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dtoken))
        async with SessionLocal() as db:
            await db.execute(update(Order).where(Order.id == oid).values(trip_started_at=start_utc))
            await db.commit()
        monkeypatch.setattr(drv, "utcnow", lambda: end_utc)
        res = (await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dtoken))).json()
        monkeypatch.setattr(drv, "utcnow", real_utcnow)  # do not leak the fake clock into the next trip
        return oid, res

    # 01:00 -> 01:20 Bahrain (22:00 -> 22:20 UTC): 20 min = 4 units = 2.000 -> night +25% = 2.500
    oid, r = await run_trip(datetime(2026, 10, 29, 22, 0, tzinfo=UTC), datetime(2026, 10, 29, 22, 20, tzinfo=UTC))
    assert (r["base_price"], r["night_surcharge"], r["night_applied"], r["final_price"]) == ("2.000", "0.500", True, "2.500")
    # ADD mode: driver takes 2.500, company profit 20% of 2.500 = 0.500 added on top, customer pays 3.000
    assert (r["driver_earning"],) == ("2.500",)
    c = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()
    assert (c["customer_total"], c["service_fee"]) == ("3.000", "0.500")
    a = (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()
    assert (a["commission"], a["customer_total"], a["driver_earning"]) == ("0.500", "3.000", "2.500")

    # trip starting at 06:10 Bahrain (03:10 UTC) is a normal trip: 2.000, no surcharge, driver 2.000, customer 2.400
    oid2, r2 = await run_trip(datetime(2026, 10, 30, 3, 10, tzinfo=UTC), datetime(2026, 10, 30, 3, 30, tzinfo=UTC))
    assert (r2["night_surcharge"], r2["night_applied"], r2["final_price"], r2["driver_earning"]) == ("0.000", False, "2.000", "2.000")
    assert (await client.get(f"/api/orders/{oid2}", headers=auth(ctoken))).json()["customer_total"] == "2.400"

    # DEDUCT mode keeps the same price but the commission comes out of it
    await client.patch("/api/admin/settings", headers=auth(admin), json={"commission_mode": "DEDUCT", "reason": "deduct"})
    oid3, r3 = await run_trip(datetime(2026, 10, 29, 22, 0, tzinfo=UTC), datetime(2026, 10, 29, 22, 20, tzinfo=UTC))
    assert (r3["final_price"], r3["driver_earning"], r3["platform_fee"]) == ("2.500", "2.000", "0.500")


async def test_factory_defaults_30_percent_add_with_night_surcharge(client, monkeypatch):
    from datetime import datetime

    import app.routers.driver as drv
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client, plain=False)
    pr = (await client.get("/api/pricing")).json()
    assert pr["night_surcharge"]["enabled"] is True and pr["night_surcharge"]["percent"] == "25"
    assert pr["service_fee"] == {"type": "PERCENT", "value": "30"}  # default mode ADD: customers see the fee

    async def trip(start_utc, end_utc):
        oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
        offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
        await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dtoken))
        for step in ("on-the-way", "arrived-at-pickup", "start-trip"):
            await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dtoken))
        async with SessionLocal() as db:
            await db.execute(update(Order).where(Order.id == oid).values(trip_started_at=start_utc))
            await db.commit()
        monkeypatch.setattr(drv, "utcnow", lambda: end_utc)
        res = (await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dtoken))).json()
        monkeypatch.setattr(drv, "utcnow", real_utcnow)
        return oid, res

    # night (01:00-01:20 Bahrain): 2.000 + 25% = 2.500 ; company 30% of 2.500 = 0.750 on top ; driver takes 2.500
    oid, r = await trip(datetime(2026, 10, 29, 22, 0, tzinfo=UTC), datetime(2026, 10, 29, 22, 20, tzinfo=UTC))
    assert (r["base_price"], r["night_surcharge"], r["final_price"], r["driver_earning"]) == ("2.000", "0.500", "2.500", "2.500")
    c = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()
    assert (c["customer_total"], c["service_fee"]) == ("3.250", "0.750")
    a = (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()
    assert (a["commission"], a["commission_mode"], a["commission_value"]) == ("0.750", "ADD", "30.000")

    # day (06:10-06:30 Bahrain): 2.000 ; company 30% = 0.600 ; customer pays 2.600 ; driver 2.000
    oid2, r2 = await trip(datetime(2026, 10, 30, 3, 10, tzinfo=UTC), datetime(2026, 10, 30, 3, 30, tzinfo=UTC))
    assert (r2["night_applied"], r2["final_price"], r2["driver_earning"]) == (False, "2.000", "2.000")
    c2 = (await client.get(f"/api/orders/{oid2}", headers=auth(ctoken))).json()
    assert (c2["customer_total"], c2["service_fee"]) == ("2.600", "0.600")


async def test_outside_bahrain_customer_30_driver_minus_25_with_night(client, monkeypatch):
    from datetime import datetime

    import app.routers.driver as drv
    admin, area, (cid, ctoken), [(did, dtoken)] = await setup_world(client, plain=False)  # factory defaults
    pr = (await client.get("/api/pricing")).json()
    assert pr["outside_bahrain"] == {"enabled": True, "driver_percent": "25"}

    # destination coordinates are mandatory
    body = order_body()
    body.pop("destination_latitude"); body.pop("destination_longitude")
    assert (await client.post("/api/orders", headers=auth(ctoken), json=body)).status_code == 422

    async def trip(start_utc, end_utc, **kw):
        oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body(**kw))).json()["id"]
        offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
        await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dtoken))
        for step in ("on-the-way", "arrived-at-pickup", "start-trip"):
            await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dtoken))
        async with SessionLocal() as db:
            await db.execute(update(Order).where(Order.id == oid).values(trip_started_at=start_utc))
            await db.commit()
        monkeypatch.setattr(drv, "utcnow", lambda: end_utc)
        res = (await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dtoken))).json()
        monkeypatch.setattr(drv, "utcnow", real_utcnow)
        return oid, res

    day_s = datetime(2026, 10, 30, 9, 0, tzinfo=UTC)   # 12:00 Bahrain
    night_s = datetime(2026, 10, 29, 22, 0, tzinfo=UTC)  # 01:00 Bahrain

    # inside Bahrain, day: company 30% only
    oid, r = await trip(day_s, day_s + timedelta(minutes=20))
    assert (r["final_price"], r["driver_earning"], r["is_outside_bahrain"], r["outside_fee"]) == ("2.000", "2.000", False, None)

    # outside Bahrain, day: base 2.000 -> customer 2.600 (+30%), driver 1.500 (-25%), company 1.100
    oid, r = await trip(day_s, day_s + timedelta(minutes=20), **KHOBAR)
    assert (r["final_price"], r["driver_earning"], r["outside_fee"], r["is_outside_bahrain"]) == ("2.000", "1.500", "0.500", True)
    c = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()
    assert (c["customer_total"], c["service_fee"]) == ("2.600", "0.600")
    a = (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()
    assert (a["commission"], a["customer_total"], a["driver_earning"], a["outside_driver_cut"]) == ("1.100", "2.600", "1.500", "0.500")

    # outside Bahrain, night: 2.000 + 25% = 2.500 ; customer +30% = 3.250 ; driver 2.500 - 25% = 1.875 ; company 1.375
    oid, r = await trip(night_s, night_s + timedelta(minutes=20), **KHOBAR)
    assert (r["final_price"], r["night_surcharge"], r["driver_earning"], r["outside_fee"]) == ("2.500", "0.500", "1.875", "0.625")
    a = (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()
    assert (a["commission"], a["customer_total"]) == ("1.375", "3.250")

    # the 25% driver cut and the boundary are settings
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={"outside_bahrain_driver_percent": "10", "reason": "ten"})
    assert r.json()["outside_bahrain_driver_percent"] == "10"
    oid, r = await trip(day_s, day_s + timedelta(minutes=20), **KHOBAR)
    assert (r["driver_earning"], r["outside_fee"]) == ("1.800", "0.200")

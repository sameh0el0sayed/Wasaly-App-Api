from datetime import timedelta

from sqlalchemy import update

from app.database import SessionLocal
from app.models import Order, User, utcnow
from tests.conftest import auth, login, order_body, register_driver, setup_world
from tests.test_flow import _complete_trip


async def _set(client, admin, **kw):
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={**kw, "reason": "tests"})
    assert r.status_code == 200, r.text


async def _new_order(client, ctoken, **kw):
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _finish(client, ctoken, dtoken):
    oid = await _new_order(client, ctoken)
    await _complete_trip(client, oid, dtoken, 10)
    return oid


async def test_live_mode_rules(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _set(client, admin, live_mode_enabled=False)
    r = await client.post("/api/driver/availability", headers=auth(dt), json={"mode": "LIVE"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "LIVE_DISABLED"
    await _set(client, admin, live_mode_enabled=True)
    r = await client.post("/api/driver/availability", headers=auth(dt), json={"mode": "LIVE"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LOCATION_CONSENT_REQUIRED"
    r = await client.post("/api/driver/location", headers=auth(dt), json={"lat": 26.22, "lng": 50.57})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "NOT_LIVE"
    await client.post("/api/privacy/location-consent", headers=auth(dt), json={"granted": True})
    assert (await client.post("/api/driver/availability", headers=auth(dt), json={"mode": "LIVE"})).status_code == 200
    assert (await client.post("/api/driver/location", headers=auth(dt), json={"lat": 26.22, "lng": 50.57})).status_code in (200, 204)
    async with SessionLocal() as db:
        u = await db.get(User, did)
        await db.refresh(u, ["driver_profile"])
        assert u.driver_profile.last_lat == 26.22
    await client.post("/api/driver/availability", headers=auth(dt), json={"mode": "OFF"})
    async with SessionLocal() as db:
        u = await db.get(User, did)
        await db.refresh(u, ["driver_profile"])
        assert u.driver_profile.last_lat is None and u.driver_profile.mode == "OFF"


async def test_nearest_driver_first_and_pickup_distance(client):
    admin, area, (cid, ct), [(d1, t1)] = await setup_world(client)
    await _set(client, admin, live_mode_enabled=True)
    d2, t2, _ = await register_driver(client, "Near")
    await client.post(f"/api/admin/users/{d2}/approve", headers=auth(admin), json={})
    await client.put(f"/api/admin/drivers/{d2}/service-areas", headers=auth(admin), json={"service_area_ids": [area]})
    for t, lat in ((t1, 26.245), (t2, 26.2205)):  # pickup is 26.22, 50.57
        await client.post("/api/privacy/location-consent", headers=auth(t), json={"granted": True})
        assert (await client.post("/api/driver/availability", headers=auth(t), json={"mode": "LIVE"})).status_code == 200
        await client.post("/api/driver/location", headers=auth(t), json={"lat": lat, "lng": 50.57})
    await _set(client, admin, offer_max_drivers=1)
    await _new_order(client, ct)
    assert (await client.get("/api/driver/offers", headers=auth(t1))).json() == []
    offers = (await client.get("/api/driver/offers", headers=auth(t2))).json()
    assert len(offers) == 1 and offers[0]["pickup_distance_km"] < 1 and offers[0]["timeout_seconds"] == 10


async def test_max_pickup_km_drops_far_live_driver(client):
    admin, _, (cid, ct), [(d1, t1)] = await setup_world(client)
    await _set(client, admin, live_mode_enabled=True, dispatch_max_pickup_km=2)
    await client.post("/api/privacy/location-consent", headers=auth(t1), json={"granted": True})
    await client.post("/api/driver/availability", headers=auth(t1), json={"mode": "LIVE"})
    await client.post("/api/driver/location", headers=auth(t1), json={"lat": 26.249, "lng": 50.57})  # ~3 km away
    await _new_order(client, ct)
    assert (await client.get("/api/driver/offers", headers=auth(t1))).json() == []


async def test_ratings_flow_and_errors(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _set(client, admin, auto_suspend_driver_one_star_count=0)
    oid = await _new_order(client, ct)
    url = f"/api/orders/{oid}/rating"
    r = await client.post(url, headers=auth(ct), json={"stars": 5})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ORDER_NOT_COMPLETED"
    await _complete_trip(client, oid, dt, 10)
    assert (await client.post(url, headers=auth(ct), json={"stars": 6})).status_code == 422
    assert (await client.post(url, headers=auth(ct), json={"stars": 4, "tags": ["NOPE"]})).status_code == 422
    assert (await client.post(url, headers=auth(ct), json={"stars": 4, "comment": "x" * 501})).status_code == 422
    r = await client.post(url, headers=auth(ct), json={"stars": 4, "tags": ["POLITE"], "comment": "good"})
    assert r.status_code == 201, r.text
    assert (await client.post(url, headers=auth(ct), json={"stars": 5})).json()["detail"]["code"] == "ALREADY_RATED"
    g = (await client.get(f"/api/orders/{oid}", headers=auth(ct))).json()
    assert g["my_rating"]["stars"] == 4 and g["my_rating"]["tags"] == ["POLITE"]
    r = await client.post(f"/api/driver/orders/{oid}/rating", headers=auth(dt), json={"stars": 5, "tags": ["READY"]})
    assert r.status_code == 201, r.text
    assert (await client.post(f"/api/driver/orders/{oid}/rating", headers=auth(dt), json={"stars": 5, "tags": ["POLITE"]})).status_code in (409, 422)
    d = (await client.get(f"/api/admin/users/{did}", headers=auth(admin))).json()
    assert d["rating_avg"] == 4 and d["ratings_count"] == 1 and d["one_star_count"] == 0
    # disabled
    await _set(client, admin, ratings_enabled=False)
    o2 = await _new_order(client, ct)
    await _complete_trip(client, o2, dt, 10)
    r = await client.post(f"/api/orders/{o2}/rating", headers=auth(ct), json={"stars": 5})
    assert r.json()["detail"]["code"] == "RATINGS_DISABLED"


async def test_rating_window_closed(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid = await _finish(client, ct, dt)
    async with SessionLocal() as db:
        await db.execute(update(Order).where(Order.id == oid).values(completed_at=utcnow() - timedelta(hours=49)))
        await db.commit()
    r = await client.post(f"/api/orders/{oid}/rating", headers=auth(ct), json={"stars": 5})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "RATING_WINDOW_CLOSED"


async def test_one_star_auto_suspends_driver_and_reactivate_resets(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid = await _finish(client, ct, dt)
    assert (await client.post(f"/api/orders/{oid}/rating", headers=auth(ct), json={"stars": 1, "tags": ["RUDE"]})).status_code == 201
    u = (await client.get(f"/api/admin/users/{did}", headers=auth(admin))).json()
    assert u["account_status"] == "SUSPENDED" and u["auto_suspended"] is True
    assert (await client.get("/api/driver/offers", headers=auth(dt))).status_code == 403
    assert (await client.post(f"/api/admin/users/{did}/reactivate", headers=auth(admin), json={})).status_code == 200
    u = (await client.get(f"/api/admin/users/{did}", headers=auth(admin))).json()
    assert u["account_status"] == "APPROVED" and u["auto_suspended"] is False and u["one_star_count"] == 0


async def test_customer_not_auto_suspended_by_default(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid = await _finish(client, ct, dt)
    await client.post(f"/api/driver/orders/{oid}/rating", headers=auth(dt), json={"stars": 1, "tags": ["RUDE"]})
    assert (await client.get(f"/api/admin/users/{cid}", headers=auth(admin))).json()["account_status"] == "APPROVED"


async def test_app_open_events_in_activity(client):
    admin, _, (cid, ct), _ = await setup_world(client)
    await client.post("/api/app/open", headers=auth(ct), json={"device_id": "device-001", "platform": "android", "app_version": "1.0.0"})
    a = (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()
    assert a["is_online"] is True and a["stats"]["app_opens"] == 1 and len(a["app_opens"]) == 1 and a["stats"]["devices"] == 1


async def test_second_auto_suspension_is_permanent_ban(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    o1 = await _finish(client, ct, dt)
    await client.post(f"/api/orders/{o1}/rating", headers=auth(ct), json={"stars": 1})
    assert (await client.get(f"/api/admin/users/{did}", headers=auth(admin))).json()["account_status"] == "SUSPENDED"
    assert (await client.post(f"/api/admin/users/{did}/reactivate", headers=auth(admin), json={})).status_code == 200
    await client.post("/api/driver/availability", headers=auth(dt), json={"online": True})
    o2 = await _finish(client, ct, dt)
    await client.post(f"/api/orders/{o2}/rating", headers=auth(ct), json={"stars": 1})
    u = (await client.get(f"/api/admin/users/{did}", headers=auth(admin))).json()
    assert u["account_status"] == "BANNED"
    for path in ("reactivate", "approve"):
        r = await client.post(f"/api/admin/users/{did}/{path}", headers=auth(admin), json={})
        assert r.status_code == 409 and r.json()["detail"]["code"] == "PERMANENTLY_BANNED"
    # same CPR / phone can never register again
    r = await client.post("/api/auth/register", json={
        "role": "CUSTOMER", "full_name": "Again", "phone": u["phone"], "cpr": "123456789",
        "password": "Passw0rd!", "password_confirm": "Passw0rd!"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ACCOUNT_BANNED"


async def test_live_mode_on_by_default(client):
    admin, _, _, _ = await setup_world(client)
    assert (await client.get("/api/app-config")).json()["live_mode_enabled"] is True


async def test_reinstated_driver_fee_becomes_debt_deducted_from_payouts(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _set(client, admin, auto_ban_after_auto_suspensions=1)
    o = await _finish(client, ct, dt)
    await client.post(f"/api/orders/{o}/rating", headers=auth(ct), json={"stars": 1})
    assert (await client.get(f"/api/admin/users/{did}", headers=auth(admin))).json()["account_status"] == "BANNED"
    # a normal (non-super) admin cannot reinstate
    r = await client.post("/api/admin/admins", headers=auth(admin), json={"full_name": "Staff", "phone": "33990000", "password": "Passw0rd!Ab", "role": "ADMIN"})
    if r.status_code in (200, 201):
        st = await login(client, "33990000", "Passw0rd!Ab")
        assert (await client.post(f"/api/admin/users/{did}/reinstate", headers=auth(st), json={"reason": "x1"})).status_code == 403
    r = await client.post(f"/api/admin/users/{did}/reinstate", headers=auth(admin), json={"reason": "appeal accepted"})
    assert r.status_code == 200 and r.json()["debt_added"] == "50" or r.json()["debt_added"].startswith("50")
    assert float((await client.get("/api/driver/balance", headers=auth(dt))).json()["debt_balance"]) == 50.0
    assert (await client.post(f"/api/admin/users/{did}/reinstate", headers=auth(admin), json={"reason": "again"})).json()["detail"]["code"] == "NOT_BANNED"
    # next paid trip: the whole driver share goes to the debt, nothing is transferred
    await client.post("/api/driver/availability", headers=auth(dt), json={"online": True})
    o2 = await _new_order(client, ct)
    done = await _complete_trip(client, o2, dt, 10)
    pid = (await client.get(f"/api/orders/{o2}", headers=auth(ct))).json()["payment_id"]
    up = await client.post("/api/uploads", headers=auth(ct), files={"file": ("p.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")}, data={"kind": "payment"})
    await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ct), json={"proof_file_key": up.json()["file_key"], "reference": "BP1"})
    assert (await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(admin))).status_code == 200
    p = (await client.get("/api/driver/payouts", headers=auth(dt))).json()
    assert p and float(p[0]["debt_offset"]) > 0 and float(p[0]["amount"]) == 0 and p[0]["status"] == "OFFSET"
    assert float((await client.get("/api/driver/balance", headers=auth(dt))).json()["debt_balance"]) < 50.0
    # admin records a direct settlement
    bal = float((await client.get("/api/driver/balance", headers=auth(dt))).json()["debt_balance"])
    r = await client.post(f"/api/admin/drivers/{did}/debt/settle", headers=auth(admin), json={"amount": bal, "payment_reference": "BP123"})
    assert r.status_code == 200 and float(r.json()["debt_balance"]) == 0


async def test_banned_customer_reinstatement_needs_fee_collected(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _set(client, admin, auto_suspend_customer_one_star_count=1, auto_ban_after_auto_suspensions=1)
    o = await _finish(client, ct, dt)
    await client.post(f"/api/driver/orders/{o}/rating", headers=auth(dt), json={"stars": 1, "tags": ["RUDE"]})
    assert (await client.get(f"/api/admin/users/{cid}", headers=auth(admin))).json()["account_status"] == "BANNED"
    r = await client.post(f"/api/admin/users/{cid}/reinstate", headers=auth(admin), json={"reason": "appeal"})
    assert r.status_code == 402 and r.json()["detail"]["code"] == "REINSTATEMENT_FEE_REQUIRED"
    r = await client.post(f"/api/admin/users/{cid}/reinstate", headers=auth(admin), json={"reason": "appeal", "fee_received": True, "payment_reference": "REF9"})
    assert r.status_code == 200 and r.json()["account_status"] == "APPROVED"


async def test_bulk_service_areas_idempotent(client):
    from .conftest import ADMIN, auth, login
    admin = await login(client, ADMIN["phone"], ADMIN["password"])
    body = {"areas": [
        {"name": "Manama", "boundary": {"type": "circle", "center": [26.2285, 50.586], "radius_m": 4000}},
        {"name": "Riffa", "boundary": {"type": "circle", "center": [26.13, 50.555], "radius_m": 6000}}]}
    r = await client.post("/api/admin/service-areas/bulk", headers=auth(admin), json=body)
    assert r.status_code == 201 and len(r.json()["created"]) == 2
    r = await client.post("/api/admin/service-areas/bulk", headers=auth(admin), json=body)
    assert r.json()["created"] == [] and r.json()["skipped_existing"] == ["Manama", "Riffa"]
    bad = {"areas": [{"name": "X1", "boundary": {"type": "circle", "center": [1, 2], "radius_m": 0}}]}
    assert (await client.post("/api/admin/service-areas/bulk", headers=auth(admin), json=bad)).status_code == 422


async def test_admin_edit_user_license_vehicle(client):
    from .conftest import ADMIN, auth, login, register_customer, register_driver
    admin = await login(client, ADMIN["phone"], ADMIN["password"])
    cid, ctoken, _ = await register_customer(client)
    did, dtoken, _ = await register_driver(client)
    did2, _, _ = await register_driver(client)
    h = auth(admin)
    # reason is required; non super-admin refused
    assert (await client.patch(f"/api/admin/users/{cid}", headers=h, json={"full_name": "New Name"})).status_code == 422
    assert (await client.patch(f"/api/admin/users/{cid}", headers=auth(ctoken), json={"full_name": "x", "reason": "no"})).status_code == 403
    r = await client.patch(f"/api/admin/users/{cid}", headers=h, json={"full_name": "New Name", "phone": "33991234", "cpr": "123456789", "reason": "fix typo"})
    assert r.status_code == 200 and r.json()["full_name"] == "New Name" and r.json()["phone"] == "+97333991234"
    # duplicates
    r = await client.patch(f"/api/admin/users/{did}", headers=h, json={"phone": "33991234", "reason": "dup"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PHONE_EXISTS"
    r = await client.patch(f"/api/admin/users/{did}", headers=h, json={"cpr": "123456789", "reason": "dup"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "CPR_EXISTS"
    assert (await client.patch(f"/api/admin/users/{did}", headers=h, json={"profile_photo": "nope/x.jpg", "reason": "bad"})).status_code == 422
    # admins cannot be edited here
    assert (await client.patch("/api/admin/users/1", headers=h, json={"full_name": "Boss", "reason": "no"})).status_code == 404
    # licence + vehicle
    r = await client.patch(f"/api/admin/drivers/{did}/license", headers=h, json={"license_number": "LIC-999", "license_expiry_date": "2031-05-05", "reason": "renewed"})
    assert r.status_code == 200 and r.json()["driver"]["license_expiry_date"] == "2031-05-05"
    r = await client.patch(f"/api/admin/drivers/{did}/vehicle", headers=h, json={"color": "Black", "passenger_capacity": 6, "reason": "update"})
    assert r.status_code == 200 and r.json()["driver"]["vehicle"]["color"] == "Black"
    plate = (await client.get(f"/api/admin/users/{did2}", headers=h)).json()["driver"]["vehicle"]["plate_number"]
    r = await client.patch(f"/api/admin/drivers/{did}/vehicle", headers=h, json={"plate_number": plate, "reason": "dup"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLATE_EXISTS"
    assert (await client.patch(f"/api/admin/drivers/{cid}/license", headers=h, json={"license_number": "x", "reason": "no"})).status_code == 404
    # user notified + audited
    n = (await client.get("/api/notifications", headers=auth(dtoken))).json()
    assert any(x["type"] == "PROFILE_UPDATED_BY_ADMIN" for x in n["items"])

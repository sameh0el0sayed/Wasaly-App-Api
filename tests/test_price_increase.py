from datetime import timedelta
from decimal import Decimal

from sqlalchemy import update

from app.database import SessionLocal
from app.models import CounterOffer, Order, OrderOffer, utcnow

from .conftest import auth, order_body, setup_world


async def _order(client, ct, **kw):
    r = await client.post("/api/orders", headers=auth(ct), json=order_body(**kw))
    assert r.status_code == 201, r.text
    return r.json()


async def _offers(client, dt):
    return (await client.get("/api/driver/offers", headers=auth(dt))).json()


async def _types(client, token):
    return [n["type"] for n in (await client.get("/api/notifications", headers=auth(token))).json()["items"]]


async def test_customer_raise_changes_offers_estimate_and_final_fare(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    cfg = (await client.get("/api/app-config")).json()["price_increase"]
    assert cfg == {"enabled": True, "max_percent": 50, "steps": [10, 25, 50], "counter_ttl_seconds": 60}
    o = await _order(client, ct, choose_driver=False)
    assert o["price_increase_percent"] == 0 and o["original_estimate"] == o["estimate"]
    before = (await _offers(client, dt))[0]
    r = await client.post(f"/api/orders/{o['id']}/raise", headers=auth(ct), json={"percent": 25})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["price_increase_percent"] == 25 and new["original_estimate"] == o["estimate"]
    assert Decimal(new["estimate"]) == (Decimal(o["estimate"]) * Decimal("1.25")).quantize(Decimal("0.001"))
    offers = await _offers(client, dt)  # old offer closed, a fresh one with the higher earning
    assert len(offers) == 1 and offers[0]["id"] != before["id"] and offers[0]["price_increase_percent"] == 25
    assert Decimal(offers[0]["estimated_driver_earning"]) > Decimal(before["estimated_driver_earning"])
    # validation
    for pct, code in ((25, "RAISE_NOT_HIGHER"), (20, "RAISE_NOT_HIGHER"), (60, "RAISE_TOO_HIGH")):
        r = await client.post(f"/api/orders/{o['id']}/raise", headers=auth(ct), json={"percent": pct})
        assert r.status_code == 422 and r.json()["detail"]["code"] == code
    assert (await client.post(f"/api/orders/{o['id']}/raise", headers=auth(dt), json={"percent": 30})).status_code == 403
    await client.patch("/api/admin/settings", headers=auth(admin), json={"price_increase_enabled": False, "reason": "off"})
    r = await client.post(f"/api/orders/{o['id']}/raise", headers=auth(ct), json={"percent": 40})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PRICE_INCREASE_DISABLED"
    await client.patch("/api/admin/settings", headers=auth(admin), json={"price_increase_enabled": True, "reason": "on"})

    # the increase is part of the final fare: 20 min = 2.000 base, +25% = 2.500, driver 2.000 after 20% commission
    assert (await client.post(f"/api/driver/offers/{offers[0]['id']}/accept", headers=auth(dt))).status_code == 200
    for step in ("on-the-way", "arrived-at-pickup", "start-trip"):
        await client.post(f"/api/driver/orders/{o['id']}/{step}", headers=auth(dt))
    async with SessionLocal() as db:
        await db.execute(update(Order).where(Order.id == o["id"]).values(trip_started_at=utcnow() - timedelta(minutes=20)))
        await db.commit()
    r = await client.post(f"/api/driver/orders/{o['id']}/arrived-at-destination", headers=auth(dt))
    assert r.status_code == 200, r.text
    done = (await client.get(f"/api/orders/{o['id']}", headers=auth(ct))).json()
    assert done["final_price"] == "2.500" and done["price_increase_amount"] == "0.500" and done["customer_total"] == "2.500"
    assert (await client.get(f"/api/driver/orders/{o['id']}", headers=auth(dt))).json()["driver_earning"] == "2.000"
    # not searching any more
    r = await client.post(f"/api/orders/{o['id']}/raise", headers=auth(ct), json={"percent": 50})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ORDER_NOT_SEARCHING"


async def test_driver_counter_offer_accept_and_reject_others(client):
    admin, _, (cid, ct), ds = await setup_world(client, drivers=2)
    (d1, t1), (d2, t2) = ds
    o = await _order(client, ct)
    offer1 = (await _offers(client, t1))[0]
    assert offer1["can_counter"] is True and offer1["counter"] is None
    r = await client.post(f"/api/driver/offers/{offer1['id']}/counter", headers=auth(t1), json={"percent": 20})
    assert r.status_code == 200, r.text
    co = r.json()["counter_offer"]
    assert co["status"] == "PENDING" and co["percent"] == 20 and co["driver"]["id"] == d1 and co["driver"]["vehicle"]["plate_number"]
    r = await client.post(f"/api/driver/offers/{offer1['id']}/counter", headers=auth(t1), json={"percent": 30})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "COUNTER_EXISTS"
    again = (await _offers(client, t1))[0]  # still listed, now with the counter and no new request possible
    assert again["status"] == "COUNTERED" and again["counter"]["percent"] == 20 and again["can_counter"] is False
    offer2 = (await _offers(client, t2))[0]
    r = await client.post(f"/api/driver/offers/{offer2['id']}/counter", headers=auth(t2), json={"percent": 60})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "RAISE_TOO_HIGH"
    assert (await client.post(f"/api/driver/offers/{offer2['id']}/counter", headers=auth(t2), json={"percent": 30})).status_code == 200
    assert (await client.post(f"/api/driver/offers/{offer1['id']}/counter", headers=auth(t2), json={"percent": 30})).status_code == 404
    assert "COUNTER_OFFER" in await _types(client, ct)

    items = (await client.get(f"/api/orders/{o['id']}/counter-offers", headers=auth(ct))).json()["items"]
    assert sorted(i["percent"] for i in items) == [20, 30] and all(i["seconds_left"] > 0 and i["new_estimate"] for i in items)
    mine = next(i for i in items if i["percent"] == 20)
    assert Decimal(mine["new_estimate"]) == (Decimal(o["estimate"]) * Decimal("1.2")).quantize(Decimal("0.001"))
    assert (await client.get(f"/api/orders/{o['id']}/counter-offers", headers=auth(t1))).status_code == 403

    r = await client.post(f"/api/orders/{o['id']}/counter-offers/{mine['id']}/accept", headers=auth(ct))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ACCEPTED" and r.json()["price_increase_percent"] == 20 and r.json()["driver"]["name"] == "Driver0"
    assert "COUNTER_ACCEPTED" in await _types(client, t1) and "COUNTER_REJECTED" in await _types(client, t2)
    r = await client.post(f"/api/orders/{o['id']}/counter-offers/{mine['id']}/accept", headers=auth(ct))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "COUNTER_NOT_PENDING"
    assert (await client.get(f"/api/orders/{o['id']}/counter-offers", headers=auth(ct))).json()["items"] == []
    allc = (await client.get(f"/api/orders/{o['id']}/counter-offers?all=1", headers=auth(ct))).json()["items"]
    assert sorted(i["status"] for i in allc) == ["ACCEPTED", "REJECTED"]
    assert (await client.get(f"/api/driver/orders/{o['id']}", headers=auth(t1))).json()["price_increase_percent"] == 20


async def test_counter_reject_and_expiry_keep_order_alive(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    o = await _order(client, ct)
    offer = (await _offers(client, dt))[0]
    c1 = (await client.post(f"/api/driver/offers/{offer['id']}/counter", headers=auth(dt), json={"percent": 10})).json()["counter_offer"]
    r = await client.post(f"/api/orders/{o['id']}/counter-offers/{c1['id']}/reject", headers=auth(ct))
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert "COUNTER_REJECTED" in await _types(client, dt)

    # a second driver-less wait: counter pending keeps the order OFFERED even after the offer's own time ran out
    await client.post(f"/api/orders/{o['id']}/raise", headers=auth(ct), json={"percent": 10})
    offer = (await _offers(client, dt))[0]
    c2 = (await client.post(f"/api/driver/offers/{offer['id']}/counter", headers=auth(dt), json={"percent": 30})).json()["counter_offer"]
    async with SessionLocal() as db:
        await db.execute(update(OrderOffer).where(OrderOffer.id == offer["id"]).values(expires_at=utcnow() - timedelta(seconds=5)))
        await db.commit()
    await _offers(client, dt)  # runs the expiry job
    assert (await client.get(f"/api/orders/{o['id']}", headers=auth(ct))).json()["status"] == "OFFERED"
    # the customer does not answer in time -> counter expires, driver told, order goes back to NO_DRIVER_AVAILABLE
    async with SessionLocal() as db:
        await db.execute(update(CounterOffer).where(CounterOffer.id == c2["id"]).values(expires_at=utcnow() - timedelta(seconds=1)))
        await db.commit()
    r = await client.post(f"/api/orders/{o['id']}/counter-offers/{c2['id']}/accept", headers=auth(ct))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "COUNTER_EXPIRED"
    await _offers(client, dt)
    assert "COUNTER_EXPIRED" in await _types(client, dt)
    assert (await client.get(f"/api/orders/{o['id']}", headers=auth(ct))).json()["status"] == "NO_DRIVER_AVAILABLE"
    # cancelling the order closes whatever is left
    assert (await client.post(f"/api/orders/{o['id']}/cancel", headers=auth(ct), json={})).status_code == 200


async def test_customer_chooses_driver_with_card_and_details(client):
    admin, _, (cid, ct), ds = await setup_world(client, drivers=2)
    (d1, t1), (d2, t2) = ds
    up = await client.post("/api/uploads", headers=auth(t1), files={"file": ("p.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")}, data={"kind": "profile"})
    key = up.json()["file_key"]
    await client.patch("/api/profile", headers=auth(t1), json={"profile_photo": key})
    await client.patch(f"/api/admin/drivers/{d1}/vehicle", headers=auth(admin), json={"photos": {"main": key, "interior": key}, "reason": "photos"})
    # default (no flag): the first driver to accept gets the order, as before
    o = await _order(client, ct, choose_driver=False, client_request_id="a1")
    assert o["choose_driver"] is False
    r = await client.post(f"/api/driver/offers/{(await _offers(client, t2))[0]['id']}/accept", headers=auth(t2))
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"
    await client.post(f"/api/orders/{o['id']}/cancel", headers=auth(ct), json={})

    o = await _order(client, ct, client_request_id="a2")  # default is choose_driver=True
    assert o["choose_driver"] is True
    r1 = await client.post(f"/api/driver/offers/{(await _offers(client, t1))[0]['id']}/accept", headers=auth(t1))
    assert r1.status_code == 200 and r1.json()["status"] == "WAITING_CUSTOMER"
    r2 = await client.post(f"/api/driver/offers/{(await _offers(client, t2))[0]['id']}/accept", headers=auth(t2))
    assert r2.json()["status"] == "WAITING_CUSTOMER"
    assert (await client.get(f"/api/orders/{o['id']}", headers=auth(ct))).json()["status"] == "OFFERED"  # nobody assigned yet
    assert "DRIVER_AVAILABLE" in await _types(client, ct)
    items = (await client.get(f"/api/orders/{o['id']}/counter-offers", headers=auth(ct))).json()["items"]
    assert len(items) == 2 and all(i["kind"] == "ACCEPT" and i["price_changed"] is False for i in items)
    card = next(i for i in items if i["driver"]["id"] == d1)
    assert card["driver"]["photo_url"].startswith("/api/files/download/") and card["driver"]["vehicle"]["photo_url"]
    assert card["driver"]["vehicle"]["plate_number"] and "has_ac" in card["driver"]["vehicle"] and "smoking_allowed" in card["driver"]
    assert set(card["details"]["vehicle"]["photos"]) == {"main", "interior"}
    assert "cpr" not in str(card).lower() and "license" not in str(card).lower()
    assert (await client.get(card["driver"]["photo_url"])).status_code == 200
    # the customer picks one; the other is told
    r = await client.post(f"/api/orders/{o['id']}/counter-offers/{card['id']}/accept", headers=auth(ct))
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED" and r.json()["price_increase_percent"] == 0
    assert "COUNTER_ACCEPTED" in await _types(client, t1) and "COUNTER_REJECTED" in await _types(client, t2)
    # the switch can be turned off by the admin
    await client.patch("/api/admin/settings", headers=auth(admin), json={"driver_choice_enabled": False, "reason": "off"})
    assert (await client.get("/api/app-config")).json()["driver_choice_enabled"] is False


async def test_driver_edits_nonessential_vehicle_data_without_review(client):
    admin, _, (cid, ct), ds = await setup_world(client, drivers=1)
    (d1, t1) = ds[0]
    up = await client.post("/api/uploads", headers=auth(t1), files={"file": ("p.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")}, data={"kind": "vehicle"})
    key = up.json()["file_key"]
    r = await client.patch("/api/driver/vehicle", headers=auth(t1), json={"photos": {"interior": key, "main": key}, "color": "Black", "has_ac": False})
    assert r.status_code == 200, r.text
    v = r.json()
    assert v["color"] == "Black" and v["has_ac"] is False and set(v["photos"]) >= {"interior", "main"}
    assert v["vehicle_status"] == "APPROVED"  # no re-review
    r = await client.patch("/api/driver/vehicle", headers=auth(t1), json={"photos": {"interior": ""}})
    assert "interior" not in r.json()["photos"] and "main" in r.json()["photos"]
    # essential fields are not accepted here (ignored)
    r = await client.patch("/api/driver/vehicle", headers=auth(t1), json={"plate_number": "999999"})
    assert r.json()["plate_number"] != "999999"


async def test_favorite_drivers_direct_trip_and_cpr_review(client):
    from app.models import Rating
    admin, _, (cid, ct), ds = await setup_world(client, drivers=2)
    (d1, t1), (d2, t2) = ds
    # an order for a driver who was never rated 4-5 cannot be direct
    r = await client.post("/api/orders", headers=auth(ct), json=order_body(preferred_driver_id=d1, client_request_id="f0"))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "NOT_FAVORITE_DRIVER"
    async with SessionLocal() as db:  # d1 rated 5, d2 rated 3 by the customer
        db.add_all([Rating(order_id=1, rater_id=cid, ratee_id=d1, rater_role="CUSTOMER", stars=5),
                    Rating(order_id=2, rater_id=cid, ratee_id=d2, rater_role="CUSTOMER", stars=3)])
        await db.commit()
    items = (await client.get("/api/favorite-drivers", headers=auth(ct))).json()["items"]
    assert [i["id"] for i in items] == [d1] and items[0]["is_online"] is True and items[0]["is_available"] is True and items[0]["my_stars"] == 5
    assert items[0]["vehicle"]["plate_number"]
    # direct trip: only d1 gets the offer, and his acceptance assigns the order at once
    o = await _order(client, ct, preferred_driver_id=d1, client_request_id="f1")
    assert o["preferred_driver_id"] == d1 and o["choose_driver"] is False
    assert await _offers(client, t2) == []
    r = await client.post(f"/api/driver/offers/{(await _offers(client, t1))[0]['id']}/accept", headers=auth(t1))
    assert r.status_code == 200 and r.json()["status"] == "ACCEPTED"
    assert (await client.get(f"/api/orders/{o['id']}", headers=auth(ct))).json()["driver"]["name"]
    # profile: name/language direct, CPR card goes to review, phone is not editable here
    up = await client.post("/api/uploads", headers=auth(ct), files={"file": ("c.png", b"\x89PNG\r\n\x1a\n" + b"0" * 64, "image/png")}, data={"kind": "cpr"})
    key = up.json()["file_key"]
    r = await client.patch("/api/profile", headers=auth(ct), json={"full_name": "New Name", "language": "en", "cpr_photo": key})
    assert r.status_code == 200 and r.json()["full_name"] == "New Name" and r.json()["language"] == "en"
    prof = (await client.get("/api/profile", headers=auth(ct))).json()
    assert [(c["field"]) for c in prof["pending_changes"]] == ["cpr_photo"]
    r = await client.post("/api/profile/cpr/request", headers=auth(ct), json={"cpr_number": "900101123"})
    assert r.status_code == 202, r.text
    prof = (await client.get("/api/profile", headers=auth(ct))).json()
    assert sorted(c["field"] for c in prof["pending_changes"]) == ["cpr_number", "cpr_photo"]


async def test_favorites_home_list_only_available(client):
    from app.models import Rating
    admin, _, (cid, ct), [(d1, t1)] = await setup_world(client)
    async with SessionLocal() as db:
        db.add(Rating(order_id=1, rater_id=cid, ratee_id=d1, rater_role="CUSTOMER", stars=4))
        await db.commit()
    assert len((await client.get("/api/favorite-drivers", headers=auth(ct))).json()["items"]) == 1
    await client.post("/api/driver/availability", headers=auth(t1), json={"online": False})
    assert (await client.get("/api/favorite-drivers", headers=auth(ct))).json()["items"] == []
    all_ = (await client.get("/api/favorite-drivers?available_only=false", headers=auth(ct))).json()["items"]
    assert len(all_) == 1 and all_[0]["is_available"] is False

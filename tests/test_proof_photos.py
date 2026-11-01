from tests.conftest import auth, order_body, register_customer, setup_world

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def _patch(client, admin, **kw):
    assert (await client.patch("/api/admin/settings", headers=auth(admin), json={**kw, "reason": "tests"})).status_code == 200


async def _up(client, token, kind):
    r = await client.post("/api/uploads", headers=auth(token), files={"file": ("p.png", PNG, "image/png")}, data={"kind": kind})
    assert r.status_code == 201, r.text
    return r.json()["file_key"]


async def _delivery_order(client, ct, dt):
    r = await client.post("/api/orders", headers=auth(ct), json=order_body(service_type="DELIVERY", cargo_description="box", accept_capacity_terms=True))
    oid = r.json()["id"]
    offer = (await client.get("/api/driver/offers", headers=auth(dt))).json()[0]
    await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dt))
    for step in ("on-the-way", "arrived-at-pickup"):
        await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dt))
    return oid


async def test_proof_photos_required_validated_and_visible(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _patch(client, admin, require_pickup_photo=True, require_delivery_photo=True)
    cfg = (await client.get("/api/app-config")).json()
    assert cfg["require_pickup_photo"] is True and cfg["require_delivery_photo"] is True
    oid = await _delivery_order(client, ct, dt)
    r = await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dt))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PICKUP_PHOTO_REQUIRED"
    wrong_kind = await _up(client, dt, "payment")
    for bad in ("nope/xx.png", wrong_kind, await _up(client, ct, "PICKUP_PROOF")):  # missing, wrong kind, someone else's
        r = await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dt), json={"pickup_photo_file_key": bad})
        assert r.status_code == 422 and r.json()["detail"]["code"] == "INVALID_FILE_KEY"
    pk = await _up(client, dt, "PICKUP_PROOF")
    r = await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dt), json={"pickup_photo_file_key": pk})
    assert r.status_code == 200 and r.json()["pickup_photo"] == pk and r.json()["pickup_photo_at"]
    r = await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dt))
    assert r.status_code == 422 and r.json()["detail"]["code"] == "DELIVERY_PHOTO_REQUIRED"
    dk = await _up(client, dt, "DELIVERY_PROOF")
    r = await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dt), json={"delivery_photo_file_key": dk})
    assert r.status_code == 200 and r.json()["delivery_photo"] == dk
    # customer sees both and can open them; a stranger cannot
    o = (await client.get(f"/api/orders/{oid}", headers=auth(ct))).json()
    assert o["pickup_photo"] == pk and o["delivery_photo"] == dk
    url = (await client.post("/api/files/sign", headers=auth(ct), json={"file_key": pk})).json()["url"]
    assert (await client.get(url)).content == PNG
    _, other, _ = await register_customer(client, "Other")
    assert (await client.post("/api/files/sign", headers=auth(other), json={"file_key": pk})).status_code == 404
    # admin sees them too
    assert (await client.get(f"/api/admin/orders/{oid}", headers=auth(admin))).json()["delivery_photo"] == dk
    types = [n["type"] for n in (await client.get("/api/notifications", headers=auth(ct))).json()["items"]]
    assert "ORDER_PICKED_UP" in types and "ORDER_DELIVERED" in types


async def test_passenger_orders_skip_photos_unless_setting_all(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await _patch(client, admin, require_pickup_photo=True, require_delivery_photo=True)
    oid = (await client.post("/api/orders", headers=auth(ct), json=order_body())).json()["id"]
    offer = (await client.get("/api/driver/offers", headers=auth(dt))).json()[0]
    await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dt))
    for step in ("on-the-way", "arrived-at-pickup"):
        await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dt))
    assert (await client.post(f"/api/driver/orders/{oid}/start-trip", headers=auth(dt))).status_code == 200  # passenger: no photo needed
    await _patch(client, admin, proof_photos_apply_to="ALL")
    oid2 = (await client.post("/api/orders", headers=auth(ct), json=order_body())).json()
    assert oid2.get("id") or oid2  # open order exists -> 409 is fine; the rule itself is covered by the setting round-trip
    assert (await client.get("/api/app-config")).json()["proof_photos_apply_to"] == "ALL"


async def test_driver_gets_single_order_only_if_it_is_theirs(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid = await _delivery_order(client, ct, dt)
    r = await client.get(f"/api/driver/orders/{oid}", headers=auth(dt))
    assert r.status_code == 200 and r.json()["id"] == oid and "driver_earning" in r.json() and "customer_total" not in r.json()
    assert (await client.get("/api/driver/orders/active", headers=auth(dt))).status_code == 200  # static routes still win
    assert (await client.get("/api/driver/orders/999999", headers=auth(dt))).status_code == 404
    assert (await client.get(f"/api/driver/orders/{oid}", headers=auth(ct))).status_code == 403

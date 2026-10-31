import json
from datetime import timedelta

from app.models import DriverProfile, utcnow
from tests.conftest import auth, order_body, register_customer, setup_world

BANNER = {"title_ar": "خصم 20%", "title_en": "20% off", "bg_color": "#1652F0", "audience": "CUSTOMER",
          "action": {"type": "SCREEN", "value": "new_order"}, "sort_order": 2}


async def test_banners_audience_window_validation_and_permissions(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    # validation
    for bad in ({"action": {"type": "URL", "value": "http://x.com"}}, {"action": {"type": "SCREEN", "value": "nope"}},
                {"bg_color": "red"}, {"starts_at": "2030-01-02T00:00:00Z", "ends_at": "2030-01-01T00:00:00Z"}):
        r = await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, **bad})
        assert r.status_code == 422, bad
    assert (await client.post("/api/admin/banners", headers=auth(ct), json=BANNER)).status_code == 403
    b1 = (await client.post("/api/admin/banners", headers=auth(admin), json=BANNER)).json()
    b2 = (await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, "title_ar": "للجميع", "audience": "ALL", "sort_order": 1})).json()
    await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, "title_ar": "سايقين", "audience": "DRIVER"})
    await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, "title_ar": "منتهي", "ends_at": (utcnow() - timedelta(days=1)).isoformat()})
    await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, "title_ar": "قادم", "starts_at": (utcnow() + timedelta(days=1)).isoformat()})
    await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, "title_ar": "مخفي", "is_active": False})
    items = (await client.get("/api/banners", headers=auth(ct))).json()["items"]
    assert [i["id"] for i in items] == [b2["id"], b1["id"]]  # sorted, only live window, customer + ALL
    assert items[1]["action"] == {"type": "SCREEN", "value": "new_order"}
    assert [i["title_ar"] for i in (await client.get("/api/banners", headers=auth(dt))).json()["items"]] == ["للجميع", "سايقين"]
    assert [i["title_ar"] for i in (await client.get("/api/banners")).json()["items"]] == ["للجميع"]  # anonymous => ALL only
    assert len((await client.get("/api/banners?audience=DRIVER", headers=auth(ct))).json()["items"]) == 2
    assert len((await client.get("/api/admin/banners", headers=auth(admin))).json()["items"]) == 6
    r = await client.patch(f"/api/admin/banners/{b1['id']}", headers=auth(admin), json={"is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert (await client.delete(f"/api/admin/banners/{b2['id']}", headers=auth(admin))).status_code == 204
    assert (await client.delete(f"/api/admin/banners/{b2['id']}", headers=auth(admin))).status_code == 404


async def test_banner_image_can_be_signed_by_any_user(client):
    admin, _, (cid, ct), _ = await setup_world(client)
    key = (await client.post("/api/uploads", headers=auth(admin), files={"file": ("b.png", b"\x89PNG\r\n\x1a\n" + b"1" * 32, "image/png")},
                             data={"kind": "banner"})).json()["file_key"]
    assert (await client.post("/api/files/sign", headers=auth(ct), json={"file_key": key})).status_code == 404
    await client.post("/api/admin/banners", headers=auth(admin), json={**BANNER, "image_file_key": key})
    assert (await client.post("/api/files/sign", headers=auth(ct), json={"file_key": key})).status_code == 200


async def test_saved_places(client):
    _, _, (cid, ct), _ = await setup_world(client)
    p = {"label": "HOME", "name": "البيت", "address": "Seef", "lat": 26.2, "lng": 50.5}
    a = (await client.post("/api/places", headers=auth(ct), json=p)).json()
    b = (await client.post("/api/places", headers=auth(ct), json={**p, "address": "Juffair"})).json()
    assert a["id"] == b["id"] and b["address"] == "Juffair"  # HOME upsert
    w = (await client.post("/api/places", headers=auth(ct), json={**p, "label": "WORK", "name": "الشغل"})).json()
    assert (await client.patch(f"/api/places/{w['id']}", headers=auth(ct), json={"label": "HOME"})).status_code == 409
    r = await client.patch(f"/api/places/{w['id']}", headers=auth(ct), json={"lat": 26.3, "name": "Office"})
    assert r.json()["lat"] == 26.3 and r.json()["name"] == "Office"
    assert (await client.post("/api/places", headers=auth(ct), json={**p, "lat": 99})).status_code == 422
    for i in range(18):
        assert (await client.post("/api/places", headers=auth(ct), json={**p, "label": "OTHER", "name": f"p{i}"})).status_code == 201
    r = await client.post("/api/places", headers=auth(ct), json={**p, "label": "OTHER"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PLACES_LIMIT"
    assert len((await client.get("/api/places", headers=auth(ct))).json()["items"]) == 20
    _, other, _ = await register_customer(client, "Other")
    assert (await client.delete(f"/api/places/{w['id']}", headers=auth(other))).status_code in (403, 404)
    assert (await client.delete(f"/api/places/{w['id']}", headers=auth(ct))).status_code == 204
    assert len((await client.get("/api/places", headers=auth(ct))).json()["items"]) == 19


async def test_service_categories_in_app_config_and_settings(client):
    admin, _, _, _ = await setup_world(client)
    cats = (await client.get("/api/app-config")).json()["service_categories"]
    assert [c["key"] for c in cats] == ["DOCUMENTS", "PARCEL", "SHOP", "LARGE", "AIRPORT"]
    new = [{**cats[1], "title_ar": "طرود"}, {**cats[0], "is_active": False}]
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={"service_categories": new, "reason": "tests"})
    assert r.status_code == 200, r.text
    got = (await client.get("/api/app-config")).json()["service_categories"]
    assert [c["key"] for c in got] == ["PARCEL"] and got[0]["title_ar"] == "طرود"  # reordered + hidden inactive
    for bad in ([{**cats[0], "color": "blue"}], [{**cats[0], "prefill": {"hack": 1}}]):
        assert (await client.patch("/api/admin/settings", headers=auth(admin), json={"service_categories": bad, "reason": "tests"})).status_code == 422


async def _active_order(client, ct, dt):
    await client.post("/api/orders", headers=auth(ct), json=order_body())
    offer = (await client.get("/api/driver/offers", headers=auth(dt))).json()[0]
    await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dt))
    return offer["order_id"]


async def test_customer_live_tracking(client, db_session=None):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid = await _active_order(client, ct, dt)
    url = f"/api/orders/{oid}/driver-location"
    assert (await client.get(f"/api/orders/{oid}", headers=auth(ct))).json()["driver_live"] is False
    assert (await client.get(url, headers=auth(ct))).json() == {"available": False, "reason": "DRIVER_NOT_LIVE"}
    # driver goes LIVE with consent
    await client.post("/api/privacy/location-consent", headers=auth(dt), json={"granted": True})
    assert (await client.post("/api/driver/availability", headers=auth(dt), json={"mode": "LIVE"})).status_code == 200
    r = await client.get(url, headers=auth(ct))
    assert r.json()["available"] is False and r.json()["reason"] == "STALE"  # no fix yet
    assert (await client.post("/api/driver/location", headers=auth(dt), json={"lat": 26.22, "lng": 50.57, "heading": 90})).status_code in (200, 204)
    r = (await client.get(url, headers=auth(ct))).json()
    assert r["available"] is True and r["lat"] == 26.22 and r["eta_target"] == "PICKUP" and r["eta_min"] >= 1 and r["heading"] == 90
    assert (await client.get(f"/api/orders/{oid}", headers=auth(ct))).json()["driver_live"] is True
    # someone else's order
    _, other, _ = await register_customer(client, "Other")
    assert (await client.get(url, headers=auth(other))).status_code in (403, 404)
    # kill switch
    await client.patch("/api/admin/settings", headers=auth(admin), json={"customer_live_tracking_enabled": False, "reason": "tests"})
    assert (await client.get(url, headers=auth(ct))).json()["available"] is False
    # finished order is not trackable
    await client.patch("/api/admin/settings", headers=auth(admin), json={"customer_live_tracking_enabled": True, "reason": "tests"})
    await client.post(f"/api/orders/{oid}/cancel", headers=auth(ct), json={"reason": "x"})
    r = await client.get(url, headers=auth(ct))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ORDER_NOT_ACTIVE"

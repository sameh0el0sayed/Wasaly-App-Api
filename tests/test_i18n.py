from app.i18n import PAIRS, localize, localize_detail, parse_accept_language, to_en
from tests.conftest import auth, order_body, setup_world

EST = {"pickup_latitude": 26.22, "pickup_longitude": 50.57, "destination_latitude": 26.25, "destination_longitude": 50.6}


def test_translation_unit():
    assert to_en("تم إلغاء الطلب") == "Order cancelled"
    assert to_en("طلب جديد من Seef") == "New order from Seef"
    assert to_en("ألغى السائق الطلب: ازدحام") == "The driver cancelled the order: ازدحام"  # user text passes through
    assert to_en("تمت الموافقة على حسابك. السبب: x") == "Your account has been approved. Reason: x"
    assert to_en("اشتراك #5") == "Subscription #5" and to_en("اشتراك #5 حتى 2026-12-01") == "Subscription #5 until 2026-12-01"
    assert to_en("1.500 BHD (تجريبي)") == "1.500 BHD (test)"
    assert to_en("نص حر") == "نص حر" and localize("تم إلغاء الطلب", "ar") == "تم إلغاء الطلب"
    assert localize_detail({"code": "X", "message": "كود الخصم غير صحيح", "order_id": 3}, "en") == {"code": "X", "message": "Invalid promo code", "order_id": 3}
    assert localize_detail("plain english", "en") == "plain english"
    assert parse_accept_language("en-US,en;q=0.9,ar;q=0.8") == "en" and parse_accept_language("ar") == "ar"
    assert parse_accept_language("fr") is None and parse_accept_language(None) is None
    assert len({a for a, _ in PAIRS}) == len(PAIRS)  # no duplicate Arabic keys


async def test_errors_notes_and_notifications_follow_language(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    en, ar = {**auth(ct), "Accept-Language": "en"}, auth(ct)
    # error messages: header decides, code never changes
    r = await client.post("/api/orders/estimate", headers=en, json={**EST, "promo_code": "NOPE"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "PROMO_INVALID" and r.json()["detail"]["message"] == "Invalid promo code"
    r = await client.post("/api/orders/estimate", headers=ar, json={**EST, "promo_code": "NOPE"})
    assert r.json()["detail"]["message"] == "كود الخصم غير صحيح"  # default stays Arabic
    # estimate note
    assert (await client.post("/api/orders/estimate", headers=en, json=EST)).json()["note"].startswith("Estimate:")
    assert (await client.post("/api/orders/estimate", headers=ar, json=EST)).json()["note"].startswith("تقديري")
    # notification language = the user's saved language
    me = (await client.get("/api/profile", headers=ar)).json()
    assert me["language"] == "ar"
    await client.post("/api/orders", headers=ar, json=order_body())
    first = (await client.get("/api/notifications", headers=ar)).json()["items"]
    assert any(n["title"] == "تم استلام طلبك" for n in first)
    r = await client.patch("/api/profile", headers=ar, json={"language": "en"})
    assert r.status_code == 200 and r.json()["language"] == "en"
    assert (await client.patch("/api/profile", headers=ar, json={"language": "fr"})).status_code == 422
    offer = (await client.get("/api/driver/offers", headers=auth(dt))).json()[0]
    await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dt))
    titles = [n["title"] for n in (await client.get("/api/notifications", headers=ar)).json()["items"]]
    assert "Your order was accepted" in titles and "تم استلام طلبك" in titles  # old ones keep the language they were written in
    drv = [n["title"] for n in (await client.get("/api/notifications", headers=auth(dt))).json()["items"]]
    assert "طلب جديد" in drv  # the driver never changed language: still Arabic


async def test_register_stores_language_from_header_or_body(client):
    base = {"role": "CUSTOMER", "full_name": "Sam", "password": "Passw0rd!", "password_confirm": "Passw0rd!"}
    r = await client.post("/api/auth/register", headers={"Accept-Language": "en-US"}, json={**base, "phone": "33771001", "cpr": "900101771"})
    assert r.status_code == 201 and r.json()["user"]["language"] == "en" and r.json()["message"] == "Your account is under review"
    r = await client.post("/api/auth/register", headers={"Accept-Language": "en"}, json={**base, "phone": "33771002", "cpr": "900101772", "language": "ar"})
    assert r.json()["user"]["language"] == "ar"
    r = await client.post("/api/auth/register", json={**base, "phone": "33771003", "cpr": "900101773"})
    assert r.json()["user"]["language"] == "ar"

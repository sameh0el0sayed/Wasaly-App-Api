from datetime import timedelta

from sqlalchemy import update

from app.database import SessionLocal
from app.models import Order, utcnow
from app.services import tap as tap_mod

from .conftest import KHOBAR, auth, order_body, setup_world
from .test_cloudinary_storage import fake  # noqa: F401

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82")


async def _done_trip(client, ctoken, dtoken, minutes=20, **kw):
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body(**kw))
    assert r.status_code == 201, r.text
    oid = r.json()["id"]
    offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
    await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dtoken))
    for step in ("on-the-way", "arrived-at-pickup", "start-trip"):
        await client.post(f"/api/driver/orders/{oid}/{step}", headers=auth(dtoken))
    async with SessionLocal() as db:
        await db.execute(update(Order).where(Order.id == oid).values(trip_started_at=utcnow() - timedelta(minutes=minutes)))
        await db.commit()
    r = await client.post(f"/api/driver/orders/{oid}/arrived-at-destination", headers=auth(dtoken))
    assert r.status_code == 200, r.text
    return oid, r.json()


async def _setup_payout(client, dtoken):
    r = await client.put("/api/driver/payout-account", headers=auth(dtoken),
                         json={"method": "IBAN", "account": "BH67BMAG00001299123456", "holder_name": "Driver Zero"})
    assert r.status_code == 200 and r.json()["account"] == "****3456"


async def test_benefitpay_proof_admin_confirm_then_driver_payout(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    await _setup_payout(client, dtoken)
    oid, _ = await _done_trip(client, ctoken, dtoken)  # 20 min = 2.000 ; 20% DEDUCT -> driver 1.600 company 0.400
    order = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()
    pid = order["payment_id"]
    pay = (await client.get(f"/api/payments/{pid}", headers=auth(ctoken))).json()
    assert pay["amount"] == "2.000" and pay["payment_status"] == "UNPAID" and pay["methods"] == ["TAP", "BENEFITPAY"]

    # cash does not exist
    r = await client.post(f"/api/payments/{pid}/cash", headers=auth(ctoken))
    assert r.status_code in (404, 405)

    up = await client.post("/api/uploads", headers=auth(ctoken), files={"file": ("p.png", PNG, "image/png")}, data={"kind": "payment"})
    assert up.status_code == 201, up.text
    r = await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ctoken),
                          json={"proof_file_key": up.json()["file_key"], "reference": "BP123"})
    assert r.status_code == 200 and r.json()["payment_status"] == "PENDING_VERIFICATION"
    # no payout before confirmation
    assert (await client.get("/api/admin/payouts", headers=auth(admin))).json() == []
    # only admins confirm
    assert (await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(ctoken))).status_code == 403
    r = await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(admin))
    assert r.status_code == 200 and r.json()["payment_status"] == "PAID"
    # second confirm is refused, still one payout
    assert (await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(admin))).status_code == 409
    po = (await client.get("/api/admin/payouts", headers=auth(admin))).json()
    assert len(po) == 1 and po[0]["amount"] == "1.600" and po[0]["status"] == "TRANSFERRED" and po[0]["simulated"] is True
    mine = (await client.get("/api/driver/payouts", headers=auth(dtoken))).json()
    assert len(mine) == 1
    assert (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_status"] == "PAID"


async def test_benefitpay_reject_then_resubmit(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]
    up = (await client.post("/api/uploads", headers=auth(ctoken), files={"file": ("p.png", PNG, "image/png")})).json()["file_key"]
    await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ctoken), json={"proof_file_key": up})
    r = await client.post(f"/api/admin/payments/{pid}/reject", headers=auth(admin), json={"reason": "المبلغ غير مطابق"})
    assert r.json()["payment_status"] == "REJECTED"
    r = await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ctoken), json={"proof_file_key": up})
    assert r.status_code == 200 and r.json()["payment_status"] == "PENDING_VERIFICATION"


async def test_payout_waits_for_account_then_retry(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]
    up = (await client.post("/api/uploads", headers=auth(ctoken), files={"file": ("p.png", PNG, "image/png")})).json()["file_key"]
    await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ctoken), json={"proof_file_key": up})
    await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(admin))
    po = (await client.get("/api/admin/payouts", headers=auth(admin))).json()[0]
    assert po["status"] == "FAILED" and po["failure_reason"] == "NO_PAYOUT_ACCOUNT"
    await _setup_payout(client, dtoken)
    r = await client.post(f"/api/admin/payouts/{po['id']}/retry", headers=auth(admin))
    assert r.json()["status"] == "TRANSFERRED"


async def test_tap_flow_fee_on_customer_and_verified_webhook(client, monkeypatch):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    await _setup_payout(client, dtoken)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"tap_fee_percent": "2.5", "reason": "tap fee"})
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]

    charges = {}

    async def fake_create(payload):
        charges["payload"] = payload
        return {"id": "chg_1", "status": "INITIATED", "transaction": {"url": "https://tap.test/pay/chg_1"}}

    state = {"status": "INITIATED", "amount": 2.05}

    async def fake_retrieve(cid_):
        return {"id": cid_, "status": state["status"], "amount": state["amount"], "currency": "BHD",
                "reference": {"transaction": f"PAY-{pid}"}}

    monkeypatch.setattr(tap_mod, "create_charge", fake_create)
    monkeypatch.setattr(tap_mod, "retrieve_charge", fake_retrieve)

    r = await client.post(f"/api/payments/{pid}/tap", headers=auth(ctoken))
    assert r.status_code == 200, r.text
    j = r.json()
    # 2.000 + 2.5% = 2.050 ; the fee is paid by the customer, so driver/company shares are untouched
    assert (j["amount"], j["gateway_fee"], j["charged_amount"], j["payment_status"]) == ("2.000", "0.050", "2.050", "PENDING_PAYMENT")
    assert j["tap_url"].startswith("https://tap.test") and charges["payload"]["amount"] == 2.05

    # webhook while Tap still says INITIATED -> nothing happens
    assert (await client.post("/api/payments/tap/webhook", json={"id": "chg_1"})).status_code == 200
    assert (await client.get(f"/api/payments/{pid}", headers=auth(ctoken))).json()["payment_status"] == "PENDING_PAYMENT"

    # a forged webhook cannot mark it paid: truth comes from Tap. Wrong amount -> not paid
    state.update(status="CAPTURED", amount=1.0)
    await client.post("/api/payments/tap/webhook", json={"id": "chg_1"})
    assert (await client.get(f"/api/payments/{pid}", headers=auth(ctoken))).json()["payment_status"] == "PENDING_PAYMENT"

    state["amount"] = 2.05
    await client.post("/api/payments/tap/webhook", json={"id": "chg_1"})
    await client.post("/api/payments/tap/webhook", json={"id": "chg_1"})  # retry from Tap: idempotent
    assert (await client.get(f"/api/payments/{pid}", headers=auth(ctoken))).json()["payment_status"] == "PAID"
    po = (await client.get("/api/admin/payouts", headers=auth(admin))).json()
    assert len(po) == 1 and po[0]["amount"] == "1.600"


async def test_tap_failure_marks_failed_and_customer_can_retry(client, monkeypatch):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]

    async def fake_create(payload):
        return {"id": "chg_2", "transaction": {"url": "https://tap.test/2"}}

    async def fake_retrieve(c):
        return {"id": c, "status": "DECLINED", "amount": 2.0, "currency": "BHD", "reference": {"transaction": f"PAY-{pid}"}}

    monkeypatch.setattr(tap_mod, "create_charge", fake_create)
    monkeypatch.setattr(tap_mod, "retrieve_charge", fake_retrieve)
    await client.post(f"/api/payments/{pid}/tap", headers=auth(ctoken))
    await client.post("/api/payments/tap/webhook", json={"id": "chg_2"})
    assert (await client.get(f"/api/payments/{pid}", headers=auth(ctoken))).json()["payment_status"] == "FAILED"
    assert (await client.post(f"/api/payments/{pid}/tap", headers=auth(ctoken))).status_code == 200


async def test_tap_not_configured_and_foreign_payment(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]
    r = await client.post(f"/api/payments/{pid}/tap", headers=auth(ctoken))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "TAP_NOT_CONFIGURED"
    assert (await client.get(f"/api/payments/{pid}", headers=auth(dtoken))).status_code in (403, 404)


async def test_unpaid_order_blocks_new_order_when_enabled(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    oid, _ = await _done_trip(client, ctoken, dtoken)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"block_new_order_if_unpaid": True, "reason": "enable block"})
    r = await client.post("/api/orders", headers=auth(ctoken), json=order_body())
    assert r.status_code == 402 and r.json()["detail"]["code"] == "UNPAID_ORDER_EXISTS"


async def test_outside_bahrain_payment_split(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"commission_value": "30", "commission_mode": "ADD", "reason": "add30"})
    oid, _ = await _done_trip(client, ctoken, dtoken, **KHOBAR)  # base 2.000: customer 2.600, driver 1.500, company 1.100
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]
    p = [x for x in (await client.get("/api/admin/payments", headers=auth(admin))).json() if x["id"] == pid][0]
    assert (p["amount"], p["driver_amount"], p["company_amount"]) == ("2.600", "1.500", "1.100")


async def test_new_reports_available(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    await _done_trip(client, ctoken, dtoken)
    for name in ("payments", "payouts", "subscriptions", "summary"):
        r = await client.get(f"/api/admin/reports/{name}?date_from=2020-01-01&date_to=2099-01-01", headers=auth(admin))
        assert r.status_code == 200, (name, r.text)
    rows = (await client.get("/api/admin/reports/payments?date_from=2020-01-01&date_to=2099-01-01", headers=auth(admin))).json()["rows"]
    assert len(rows) == 1 and rows[0]["status"] == "UNPAID"


async def test_push_sent_to_registered_device_on_payment_confirmed(client, monkeypatch):
    import asyncio

    from app.services import notifications
    sent = []

    async def fake_push(token, title, body, data):
        sent.append((token, data))
        return "ok"

    monkeypatch.setattr(notifications, "send_push", fake_push)
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    assert (await client.post("/api/devices", headers=auth(ctoken), json={"token": "fcm-token-customer-1", "platform": "android"})).status_code == 201
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]
    up = (await client.post("/api/uploads", headers=auth(ctoken), files={"file": ("p.png", PNG, "image/png")})).json()["file_key"]
    await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ctoken), json={"proof_file_key": up})
    await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(admin))
    await asyncio.sleep(0.05)
    assert any(t == "fcm-token-customer-1" and d["type"] == "PAYMENT_CONFIRMED" for t, d in sent)


async def test_admin_announcements_by_audience(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)

    async def count(token):
        return (await client.get("/api/notifications", headers=auth(token))).json()["items"]

    def ann(items):
        return [n for n in items if n["type"] == "ANNOUNCEMENT"]

    r = await client.post("/api/admin/notifications", headers=auth(admin), json={"audience": "DRIVERS", "title": "للسائقين", "message": "تنبيه للسائقين فقط"})
    assert r.json()["recipients"] == 1
    assert len(ann(await count(dtoken))) == 1 and len(ann(await count(ctoken))) == 0
    await client.post("/api/admin/notifications", headers=auth(admin), json={"audience": "CUSTOMERS", "title": "للعملاء", "message": "تنبيه للعملاء فقط"})
    assert len(ann(await count(ctoken))) == 1 and len(ann(await count(dtoken))) == 1
    r = await client.post("/api/admin/notifications", headers=auth(admin), json={"audience": "ALL", "title": "للكل", "message": "تنبيه للجميع"})
    assert r.json()["recipients"] == 2
    assert len(ann(await count(ctoken))) == 2 and len(ann(await count(dtoken))) == 2
    r = await client.post("/api/admin/notifications", headers=auth(admin), json={"user_id": cid, "title": "لك وحدك", "message": "رسالة خاصة"})
    assert r.json()["recipients"] == 1
    # both target kinds at once, or none, are rejected; customers cannot send
    assert (await client.post("/api/admin/notifications", headers=auth(admin), json={"audience": "ALL", "user_id": cid, "title": "xx", "message": "yy"})).status_code == 422
    assert (await client.post("/api/admin/notifications", headers=auth(ctoken), json={"audience": "ALL", "title": "xx", "message": "yy"})).status_code == 403


async def test_cloudinary_storage_end_to_end_via_api(client, fake, tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    admin, _, (cid, ctoken), _ = await setup_world(client, drivers=0)
    up = await client.post("/api/uploads", headers=auth(ctoken), files={"file": ("p.png", PNG, "image/png")})
    key = up.json()["file_key"]
    assert any(k.endswith(key) for k in fake)  # stored in Cloudinary, not on disk
    assert list(tmp_path.rglob("*.png")) == []
    url = (await client.post("/api/files/sign", headers=auth(ctoken), json={"file_key": key})).json()["url"]
    r = await client.get(url)
    assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"


async def test_payment_details_come_from_settings_via_api(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={
        "benefitpay_account_name": "Phoenix Co", "benefitpay_account_number": "33001122",
        "benefitpay_instructions": "حوّل ثم ارفع الصورة", "tap_fee_percent": "2.5", "reason": "payment details"})
    assert r.status_code == 200, r.text
    m = (await client.get("/api/payments/methods", headers=auth(ctoken))).json()
    assert m["methods"] == ["TAP", "BENEFITPAY"] and m["benefitpay"]["account_number"] == "33001122" and m["tap"]["fee_percent"] == "2.5"
    await client.patch("/api/admin/settings", headers=auth(admin), json={"tap_enabled": False, "reason": "disable tap"})
    assert (await client.get("/api/payments/methods", headers=auth(ctoken))).json()["methods"] == ["BENEFITPAY"]
    oid, _ = await _done_trip(client, ctoken, dtoken)
    pid = (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["payment_id"]
    r = await client.post(f"/api/payments/{pid}/tap", headers=auth(ctoken))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "METHOD_DISABLED"
    assert (await client.get(f"/api/payments/{pid}", headers=auth(ctoken))).json()["benefitpay_account"]["name"] == "Phoenix Co"


async def test_estimate_endpoint_and_driver_offer_fields(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    body = {"pickup_latitude": 26.22, "pickup_longitude": 50.57, "destination_latitude": 26.21, "destination_longitude": 50.58}
    r = await client.post("/api/orders/estimate", headers=auth(ctoken), json=body)
    assert r.status_code == 200, r.text
    e = r.json()
    assert e["is_estimate"] is True and e["currency"] == "BHD" and e["distance_km"] > 0 and e["duration_min"] >= 1
    assert e["pickup_in_service_area"] is True and e["is_outside_bahrain"] is False
    assert float(e["estimated_total"]) >= 0.5 and "تقديري" in e["note"]
    # outside Bahrain costs more for the customer
    far = await client.post("/api/orders/estimate", headers=auth(ctoken), json={**body, "destination_latitude": 26.30, "destination_longitude": 50.21})
    assert far.json()["is_outside_bahrain"] is True and float(far.json()["estimated_total"]) > float(e["estimated_total"])
    # the driver's offer carries distance / duration / estimated earning (plain world: 20% DEDUCT)
    assert (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).status_code == 201
    offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
    assert offer["is_estimate"] is True and offer["distance_km"] > 0 and offer["duration_min"] >= 1
    assert float(offer["estimated_driver_earning"]) > 0 and offer["currency"] == "BHD"
    assert (await client.post("/api/orders/estimate", headers=auth(dtoken), json=body)).status_code == 403
    assert (await client.post("/api/orders/estimate", headers=auth(ctoken), json={"pickup_latitude": 26.2})).status_code == 422


async def test_offer_seconds_and_other_settings_are_configurable(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    cfg = (await client.get("/api/app-config")).json()  # public, no login
    assert cfg["offer_timeout_seconds"] == 10 and cfg["maintenance"] is False
    r = await client.patch("/api/admin/settings", headers=auth(admin), json={
        "offer_ttl_seconds": 25, "support_phone": "+97333000000", "reason": "tune settings"})
    assert r.status_code == 200 and r.json()["offer_ttl_seconds"] == "25"
    assert (await client.patch("/api/admin/settings", headers=auth(admin), json={"offer_ttl_seconds": 2, "reason": "too low"})).status_code == 422
    cfg = (await client.get("/api/app-config")).json()
    assert cfg["offer_timeout_seconds"] == 25 and cfg["support"]["phone"] == "+97333000000"
    # new offers use the new duration
    assert (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).status_code == 201
    offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
    assert 20 < offer["seconds_left"] <= 25
    # maintenance mode blocks new orders only
    await client.patch("/api/admin/settings", headers=auth(admin), json={"maintenance_mode": True, "maintenance_message": "صيانة", "reason": "maintenance"})
    cid2, ctoken2, _ = await __import__("tests.conftest", fromlist=["x"]).register_customer(client, "Other")
    await client.post(f"/api/admin/users/{cid2}/approve", headers=auth(admin), json={})
    r = await client.post("/api/orders", headers=auth(ctoken2), json=order_body())
    assert r.status_code == 503 and r.json()["detail"]["code"] == "MAINTENANCE_MODE"

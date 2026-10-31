from datetime import timedelta

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import AdminAuditLog, LocationSnapshot, User, utcnow
from app.services import activity

from .conftest import auth, order_body, setup_world

DEV = {"device_id": "install-abc-123", "platform": "android", "model": "Pixel 8", "os_name": "Android", "os_version": "15",
       "app_version": "1.2.0", "locale": "ar-BH"}


async def _age_last_seen(uid, minutes):
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.id == uid).values(last_seen_at=utcnow() - timedelta(minutes=minutes)))
        await db.commit()


async def test_device_usage_counts_and_online_by_any_request(client):
    admin, _, (cid, ctoken), _ = await setup_world(client)
    # login from a (new) device is recorded
    r = await client.post("/api/auth/login", json={"phone": "33000001", "password": "Passw0rd!", "device_id": DEV["device_id"]})
    assert r.status_code == 200
    for _ in range(3):
        assert (await client.post("/api/app/open", headers=auth(ctoken), json={**DEV, "fcm_token": "fcm-token-abcdef-1"})).status_code == 200
    a = (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()
    assert a["app_open_count"] == 3 and a["login_count"] == 1 and a["online"] is True and a["push_enabled"] is True
    assert a["devices"][0]["model"] == "Pixel 8" and a["devices"][0]["open_count"] == 3 and a["devices"][0]["os"] == "Android 15"
    assert a["recent_logins"][0]["new_device"] is True and a["recent_logins"][0]["success"] is True
    # second login from the SAME device is not "new"
    await client.post("/api/auth/login", json={"phone": "33000001", "password": "Passw0rd!", "device_id": DEV["device_id"]})
    a = (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()
    assert a["recent_logins"][0]["new_device"] is False and a["login_count"] == 2
    # failed login is logged too
    await client.post("/api/auth/login", json={"phone": "33000001", "password": "wrong-pass", "device_id": "other-dev-1"})
    a = (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()
    assert a["recent_logins"][0]["success"] is False

    # 15 minutes without any request -> offline ; list filter works
    await _age_last_seen(cid, 16)
    assert (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()["online"] is False
    ids = lambda r: [u["id"] for u in r.json()["items"]]
    assert cid in ids(await client.get("/api/admin/users?online=false", headers=auth(admin)))
    assert cid not in ids(await client.get("/api/admin/users?online=true", headers=auth(admin)))
    # ANY authenticated request (even a plain GET) brings the user back online
    assert (await client.get("/api/account/status", headers=auth(ctoken))).status_code == 200
    assert (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()["online"] is True
    assert cid in ids(await client.get("/api/admin/users?online=true", headers=auth(admin)))
    # the window is a setting
    await _age_last_seen(cid, 16)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"online_window_minutes": 30, "reason": "window 30"})
    assert (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()["online"] is True


async def test_location_needs_consent_audited_and_deleted_on_withdraw(client):
    admin, _, (cid, ctoken), _ = await setup_world(client)
    snap = {"event": "ORDER_CREATED", "latitude": 26.22, "longitude": 50.57, "accuracy_m": 12}
    r = await client.post("/api/location/snapshot", headers=auth(ctoken), json=snap)
    assert r.status_code == 403 and r.json()["detail"]["code"] == "LOCATION_CONSENT_REQUIRED"
    assert (await client.post("/api/privacy/location-consent", headers=auth(ctoken), json={"granted": True})).json()["location_consent"] is True
    assert (await client.post("/api/location/snapshot", headers=auth(ctoken), json=snap)).status_code == 201
    assert (await client.post("/api/location/snapshot", headers=auth(ctoken), json={**snap, "event": "NOPE"})).status_code == 422
    # not shown unless asked, and asking is audited
    a = (await client.get(f"/api/admin/users/{cid}/activity", headers=auth(admin))).json()
    assert "location_snapshots" not in a and a["location_consent"] is True
    a = (await client.get(f"/api/admin/users/{cid}/activity?include_location=true", headers=auth(admin))).json()
    assert a["location_snapshots"][0]["event"] == "ORDER_CREATED" and a["location_snapshots"][0]["latitude"] == 26.22
    async with SessionLocal() as db:
        assert (await db.execute(select(AdminAuditLog.id).where(AdminAuditLog.action == "VIEW_USER_LOCATION"))).first()
    # admin master switch
    await client.patch("/api/admin/settings", headers=auth(admin), json={"location_snapshots_enabled": False, "reason": "off"})
    assert (await client.post("/api/location/snapshot", headers=auth(ctoken), json=snap)).status_code == 409
    # withdrawing consent deletes what was stored
    await client.post("/api/privacy/location-consent", headers=auth(ctoken), json={"granted": False})
    async with SessionLocal() as db:
        assert (await db.execute(select(LocationSnapshot.id).where(LocationSnapshot.user_id == cid))).first() is None


async def test_offer_ttl_retry_limit_and_driver_auto_offline_settings(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    # offer time is a setting (default 10 s)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"offer_ttl_seconds": 30, "reason": "ttl 30"})
    assert (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).status_code == 201
    offer = (await client.get("/api/driver/offers", headers=auth(dtoken))).json()[0]
    assert 10 < offer["seconds_left"] <= 30
    assert (await client.get("/api/app-config")).json()["offer_timeout_seconds"] == 30
    assert (await client.patch("/api/admin/settings", headers=auth(admin), json={"offer_ttl_seconds": 2, "reason": "too low"})).status_code == 422

    # idle driver goes offline automatically (setting), 0 = disabled
    await client.patch("/api/admin/settings", headers=auth(admin), json={"driver_auto_offline_minutes": 5, "reason": "idle 5"})
    await _age_last_seen(did, 10)
    activity._last_minute = 0
    async with SessionLocal() as db:
        await activity.sweep(db)
        await db.commit()
    async with SessionLocal() as db:
        from app.models import DriverProfile
        assert (await db.execute(select(DriverProfile.is_online).where(DriverProfile.user_id == did))).scalar_one() is False


async def test_order_retry_limit_setting(client):
    admin, _, (cid, ctoken), _ = await setup_world(client, drivers=0)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"max_order_retries": 1, "reason": "one retry"})
    oid = (await client.post("/api/orders", headers=auth(ctoken), json=order_body())).json()["id"]
    assert (await client.get(f"/api/orders/{oid}", headers=auth(ctoken))).json()["status"] == "NO_DRIVER_AVAILABLE"
    assert (await client.post(f"/api/orders/{oid}/retry", headers=auth(ctoken))).status_code == 200
    r = await client.post(f"/api/orders/{oid}/retry", headers=auth(ctoken))
    assert r.status_code == 409 and r.json()["detail"]["code"] == "RETRY_LIMIT_REACHED"

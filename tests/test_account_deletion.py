from datetime import timedelta

from sqlalchemy import update

from app.database import SessionLocal
from app.models import UploadedFile, User, utcnow
from app.services.account_deletion import anonymize_due
from tests.conftest import auth, order_body, setup_world
from tests.test_flow import _complete_trip

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def test_delete_blocked_by_active_order_and_unpaid(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid = (await client.post("/api/orders", headers=auth(ct), json=order_body())).json()["id"]
    r = await client.post("/api/account/delete", headers=auth(ct), json={})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "ACTIVE_ORDER_EXISTS"
    await _complete_trip(client, oid, dt, 10)  # completed but not paid
    r = await client.post("/api/account/delete", headers=auth(ct), json={})
    d = r.json()["detail"]
    assert r.status_code == 409 and d["code"] == "UNPAID_ORDER_EXISTS" and d["order_id"] == oid and d["payment_id"]


async def test_delete_logs_out_blocks_login_and_admin_restore(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await client.post("/api/app/open", headers=auth(ct), json={"device_id": "device-01", "fcm_token": "tok-aaaaaaaaaaaa"})
    r = await client.post("/api/account/delete", headers=auth(ct), json={"reason": "leaving"})
    assert r.status_code == 200 and r.json()["message"] == "تم حذف حسابك"
    r = await client.get("/api/account/status", headers=auth(ct))  # old token
    assert r.status_code == 401 and r.json()["detail"]["code"] == "UNAUTHORIZED"
    r = await client.post("/api/auth/login", json={"phone": "33000001", "password": "Passw0rd!"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "ACCOUNT_DELETED"
    # admin restore inside the window
    assert (await client.post(f"/api/admin/users/{cid}/restore", headers=auth(admin))).json()["account_status"] == "APPROVED"
    assert (await client.post(f"/api/admin/users/{cid}/restore", headers=auth(admin))).json()["detail"]["code"] == "NOT_DELETED"
    assert (await client.post("/api/auth/login", json={"phone": "33000001", "password": "Passw0rd!"})).status_code == 200


async def test_driver_delete_goes_offline_and_anonymize_job_after_30_days(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    up = await client.post("/api/uploads", headers=auth(dt), files={"file": ("p.png", PNG, "image/png")}, data={"kind": "doc"})
    key = up.json()["file_key"]
    assert (await client.post("/api/account/delete", headers=auth(dt), json={})).status_code == 200
    async with SessionLocal() as db:
        u = await db.get(User, did)
        assert u.account_status == "DELETED" and u.driver_profile.is_online is False and u.driver_profile.mode == "OFF"
        assert await anonymize_due(db) == 0  # still inside the grace period
        await db.execute(update(User).where(User.id == did).values(deleted_at=utcnow() - timedelta(days=31)))
        await db.commit()
        assert await anonymize_due(db) == 1
    async with SessionLocal() as db:
        u = await db.get(User, did)
        assert u.full_name == "مستخدم محذوف" and u.phone is None and u.cpr_hash is None and u.anonymized_at
        assert await db.get(UploadedFile, key) is None
    assert (await client.post(f"/api/admin/users/{did}/restore", headers=auth(admin))).json()["detail"]["code"] == "RESTORE_WINDOW_CLOSED"


async def test_banned_user_keeps_identifiers_after_delete(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    await client.patch("/api/admin/settings", headers=auth(admin), json={"auto_ban_after_auto_suspensions": 1, "reason": "tests"})
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.id == cid).values(account_status="BANNED"))
        await db.commit()
    assert (await client.post("/api/account/delete", headers=auth(ct), json={})).status_code == 200
    async with SessionLocal() as db:
        await db.execute(update(User).where(User.id == cid).values(deleted_at=utcnow() - timedelta(days=31)))
        await db.commit()
        await anonymize_due(db)
    async with SessionLocal() as db:
        u = await db.get(User, cid)
        assert u.phone is not None and u.cpr_hash is not None and u.full_name == "مستخدم محذوف"

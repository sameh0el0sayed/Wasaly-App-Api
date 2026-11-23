"""PCC system modes (PUT /control/v1/mode): payments_frozen and emergency must really refuse money and writes."""
import re

import pytest

from app.main import app
from app.routers.control import guard
from app.services import modes
from tests.conftest import ADMIN, auth, login, register_customer

MONEY_WORDS = re.compile(r"pay|wallet|topup|subscri|invoice|checkout|refund|payout|settle|statement|tender|charge|benefit|/tap|debt|cash", re.I)


@pytest.fixture(autouse=True)
def _pcc_guard():
    app.dependency_overrides[guard] = lambda: None  # HMAC itself is covered elsewhere; here the real endpoint behaviour is tested
    yield
    app.dependency_overrides.pop(guard, None)
    modes.invalidate()


async def set_mode(client, mode, method="PUT"):
    r = await client.request(method, "/control/v1/mode", json={"mode": mode, "message": ""})
    assert r.status_code == 200, r.text
    assert r.json()["applied"] is True and r.json()["ok"] is True
    return r.json()["mode"]


def test_every_money_route_is_covered():
    """Fails when a write endpoint that looks like money handling is neither frozen nor deliberately allowed."""
    missing = []
    for r in app.routes:
        methods = (getattr(r, "methods", None) or set()) - {"GET", "HEAD", "OPTIONS"}
        if not methods or r.path.startswith("/control/v1") or not MONEY_WORDS.search(r.path):
            continue
        concrete = re.sub(r"\{[^}]+\}", "1", r.path)
        if not any(modes.is_money(m, concrete) for m in methods) and r.path not in modes.MONEY_ALLOW:
            missing.append((sorted(methods), r.path))
    assert not missing, f"money-looking writes not covered by payments_frozen: {missing}"


@pytest.mark.parametrize("alias,expected", [("disabled", "stopped"), ("off", "stopped"), ("payments_off", "payments_frozen"),
                                            ("FREEZE", "payments_frozen"), ("lockdown", "emergency"), ("kill", "emergency"), ("normal", "normal")])
async def test_aliases(client, alias, expected):
    assert await set_mode(client, alias, method="POST") == expected
    assert (await client.get("/control/v1/mode")).json()["mode"] == expected


async def test_payments_frozen_blocks_every_money_path_and_normal_restores(client):
    uid, tok, phone = await register_customer(client)
    admin = await login(client, ADMIN["phone"], ADMIN["password"])
    await set_mode(client, "payments_frozen")
    for method, path in [("POST", "/api/payments/1/tap"), ("POST", "/api/payments/1/benefit"), ("POST", "/api/payments/1/benefitpay"),
                         ("POST", "/api/subscriptions"), ("POST", "/api/subscriptions/1/renew"), ("POST", "/api/admin/payments/1/confirm"),
                         ("POST", "/api/admin/payouts/1/retry"), ("POST", "/api/admin/payouts/1/mark-transferred"),
                         ("POST", "/api/admin/partners/statements/1/pay"), ("POST", "/api/admin/partners/statements/1/pay-tap"),
                         ("POST", f"/api/admin/users/{uid}/wallet"), ("POST", "/api/admin/drivers/1/debt/settle")]:
        r = await client.request(method, path, json={}, headers=auth(admin if "/admin/" in path else tok))
        assert r.status_code == 423 and r.json()["detail"]["code"] == "PAYMENTS_FROZEN", (path, r.status_code, r.text)
    # webhooks of payments already started and reads keep working
    assert (await client.post("/api/payments/tap/webhook", json={})).status_code != 423
    assert (await client.get("/api/app-config")).status_code == 200
    await set_mode(client, "normal")
    r = await client.post("/api/payments/999/tap", headers=auth(tok))
    assert r.status_code not in (423, 503)  # reaches the real handler again (404 for the unknown payment)


async def test_emergency_is_read_only_revokes_sessions_and_refuses_users(client):
    uid, tok, phone = await register_customer(client)
    admin = await login(client, ADMIN["phone"], ADMIN["password"])
    await set_mode(client, "emergency")
    # payments frozen
    r = await client.post("/api/payments/1/tap", headers=auth(tok))
    assert r.status_code == 423 and r.json()["detail"]["code"] == "PAYMENTS_FROZEN"
    # every other write is refused (end user and admin), reads of end users fail because the session was revoked
    r = await client.post("/api/orders", json={}, headers=auth(tok))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "EMERGENCY_READ_ONLY"
    r = await client.post("/api/auth/register", json={})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "EMERGENCY_READ_ONLY"
    assert (await client.post("/api/admin/promo-codes", json={}, headers=auth(admin))).status_code == 503
    assert (await client.get("/api/profile", headers=auth(tok))).status_code in (401, 403)
    # end-user sign-in refused, admin sign-in allowed
    r = await client.post("/api/auth/login", json={"phone": phone, "password": "Passw0rd!"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "EMERGENCY_LOCKDOWN"
    assert await login(client, ADMIN["phone"], ADMIN["password"])
    # control and health stay reachable, normal restores everything without a redeploy
    assert (await client.get("/control/v1/mode")).json()["mode"] == "emergency"
    await set_mode(client, "normal")
    assert (await client.post("/api/auth/login", json={"phone": phone, "password": "Passw0rd!"})).status_code == 200


async def test_stopped_refuses_user_traffic_but_not_admin_or_control(client):
    uid, tok, phone = await register_customer(client)
    await set_mode(client, "stopped")
    r = await client.get("/api/profile", headers=auth(tok))
    assert r.status_code == 503 and r.json()["detail"]["code"] == "SERVICE_STOPPED"
    assert (await client.get("/control/v1/mode")).status_code == 200
    assert await login(client, ADMIN["phone"], ADMIN["password"])

import json

import pytest

from app import seed
from app.config import settings
from app.database import SessionLocal
from tests.conftest import auth, login


async def test_seed_creates_loginable_demo_users_and_is_idempotent(client, monkeypatch):
    vals = dict(seed_demo_data=True, seed_admin_phone="0097330000001", seed_admin_password="admin123123", seed_customer_phone="33100001",
                seed_customer_cpr="900101001", seed_customer_name="C", seed_customer_password="Demo@12345", seed_driver_phone="33200001",
                seed_driver_cpr="850101002", seed_driver_name="D", seed_driver_password="Demo@12345", seed_driver_license="L1",
                seed_area_points="[[26.1,50.45],[26.1,50.7],[26.3,50.7],[26.3,50.45]]",
                seed_driver_vehicle=json.dumps({"make": "T", "model": "C", "year": 2020, "color": "W", "plate_number": "999", "passenger_capacity": 4}),
                seed_driver_payout_iban="BH67BMAG00001299123456")
    for k, v in vals.items():
        monkeypatch.setattr(settings, k, v)
    for _ in range(2):
        async with SessionLocal() as db:
            await seed.seed_demo(db)
    admin = await login(client, "0097330000001".replace("00973", "+973"), "admin123123")
    users = (await client.get("/api/admin/users?role=DRIVER", headers=auth(admin))).json()["items"]
    assert len(users) == 1 and users[0]["account_status"] == "APPROVED"
    assert (await client.get("/api/driver/status", headers=auth(await login(client, "+97333200001", "Demo@12345")))).status_code == 200
    assert (await client.get("/api/account/status", headers=auth(await login(client, "+97333100001", "Demo@12345")))).status_code == 200


def test_production_refuses_defaults(monkeypatch):
    monkeypatch.setattr(settings, "app_env", "production")
    with pytest.raises(RuntimeError):
        seed.assert_safe_environment()

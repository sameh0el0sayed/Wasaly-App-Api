import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_tmp}/test.db"
os.environ["RUN_BACKGROUND_TASKS"] = "false"
os.environ["UPLOAD_DIR"] = f"{_tmp}/storage"
os.environ["STORAGE_BACKEND"] = os.environ.get("TEST_STORAGE_BACKEND", "local")
os.environ["SECRET_KEY"] = "test-secret"
# tests must not depend on a developer's local .env
for k, v in {"APP_ENV": "development", "SEED_DEMO_DATA": "false", "FIRST_ADMIN_PHONE": "+97300000000", "FIRST_ADMIN_PASSWORD": "ChangeMe!12345",
             "FIRST_ADMIN_MUST_CHANGE_PASSWORD": "true", "FIREBASE_CREDENTIALS": "", "CLOUDINARY_CLOUD_NAME": "", "CLOUDINARY_API_KEY": "",
             "CLOUDINARY_API_SECRET": "", "CLOUDINARY_URL": ""}.items():
    os.environ[k] = v
os.environ["PAYOUT_PROVIDER"] = "simulated"

import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app, bootstrap  # noqa: E402

_n = {"i": 0}
ADMIN = {"phone": "+97300000000", "password": "ChangeMe!12345"}


@pytest_asyncio.fixture
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await bootstrap()
    _n["i"] = 0
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def auth(token):
    return {"Authorization": f"Bearer {token}"}


async def login(client, phone, password):
    r = await client.post("/api/auth/login", json={"phone": phone, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


async def register_customer(client, name="Ali"):
    _n["i"] += 1
    i = _n["i"]
    r = await client.post("/api/auth/register", json={
        "role": "CUSTOMER", "full_name": name, "phone": f"3300{i:04d}", "cpr": f"9001{i:05d}",
        "password": "Passw0rd!", "password_confirm": "Passw0rd!"})
    assert r.status_code == 201, r.text
    return r.json()["user"]["id"], r.json()["access_token"], f"+9733300{i:04d}"[:0] + f"3300{i:04d}"


async def register_driver(client, name="Driver", seats=4):
    _n["i"] += 1
    i = _n["i"]
    r = await client.post("/api/auth/register", json={
        "role": "DRIVER", "full_name": name, "phone": f"3400{i:04d}", "cpr": f"8501{i:05d}",
        "password": "Passw0rd!", "password_confirm": "Passw0rd!",
        "driver": {"license_number": f"LIC{i}", "license_expiry_date": "2099-01-01",
                   "vehicle": {"make": "Toyota", "model": "Camry", "year": 2020, "color": "White",
                               "plate_number": f"{i}1234", "passenger_capacity": seats, "has_ac": True}}})
    assert r.status_code == 201, r.text
    return r.json()["user"]["id"], r.json()["access_token"], f"3400{i:04d}"


SEEF = [[26.20, 50.52], [26.20, 50.62], [26.25, 50.62], [26.25, 50.52]]
PICKUP = {"pickup_latitude": 26.22, "pickup_longitude": 50.57}
DEST = {"destination_latitude": 26.21, "destination_longitude": 50.58}  # inside Bahrain
KHOBAR = {"destination_latitude": 26.30, "destination_longitude": 50.21}  # Saudi Arabia (outside Bahrain)


async def make_area(client, admin_token, name="Seef"):
    r = await client.post("/api/admin/service-areas", headers=auth(admin_token),
                          json={"name": name, "boundary": {"type": "polygon", "points": SEEF}})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def setup_world(client, drivers=1, plain=True):
    """admin + area + approved customer + N approved online drivers."""
    admin = await login(client, ADMIN["phone"], ADMIN["password"])
    area = await make_area(client, admin)
    cid, ctoken, _ = await register_customer(client)
    await client.post(f"/api/admin/users/{cid}/approve", headers=auth(admin), json={})
    # most flow tests assume plain daytime pricing; night surcharge has its own tests
    r = await client.patch("/api/admin/settings", headers=auth(admin),
                           json={"block_new_order_if_unpaid": False, "reason": "tests setup"})
    assert r.status_code == 200, r.text
    if plain:  # most flow tests assume plain daytime pricing with 20% DEDUCT; defaults have their own test
        r = await client.patch("/api/admin/settings", headers=auth(admin),
                               json={"night_surcharge_enabled": False, "commission_value": "20", "commission_mode": "DEDUCT", "reason": "tests"})
        assert r.status_code == 200, r.text
    ds = []
    for k in range(drivers):
        did, dtoken, _ = await register_driver(client, f"Driver{k}")
        assert (await client.post(f"/api/admin/users/{did}/approve", headers=auth(admin), json={})).status_code == 200
        r = await client.put(f"/api/admin/drivers/{did}/service-areas", headers=auth(admin), json={"service_area_ids": [area]})
        assert r.status_code == 200, r.text
        r = await client.post("/api/driver/availability", headers=auth(dtoken), json={"online": True})
        assert r.status_code == 200, r.text
        ds.append((did, dtoken))
    return admin, area, (cid, ctoken), ds


def order_body(**kw):
    b = {"service_type": "PERSON", "pickup_address": "Seef Mall", "destination_address": "Manama Souq",
         "passenger_count": 2, **PICKUP, **DEST}
    b.update(kw)
    return b

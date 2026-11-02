from datetime import timedelta

from .conftest import ADMIN, auth, login, register_customer

DEV = {"device_id": "dev-1", "platform": "android", "model": "Samsung SM-A515F", "os_name": "Android", "os_version": "14", "app_version": "1.0.0+41", "locale": "ar-BH"}


def item(**kw):
    return {"level": "error", "source": "push.init", "error_type": "FirebaseException", "message": "boom", "stack": "#0 x",
            "context": {"project": "wasaly"}, "occurred_at": "2026-11-02T01:58:00Z", **kw}


async def test_report_and_browse_errors(client):
    admin = await login(client, ADMIN["phone"], ADMIN["password"])
    uid, token, _ = await register_customer(client, "Reporter")
    # anonymous, valid token, and a garbage token all store (never 401)
    assert (await client.post("/api/app/errors", json={"device": DEV, "items": [item()]})).json() == {"stored": 1}
    assert (await client.post("/api/app/errors", headers=auth(token), json={"device": DEV, "items": [item(level="fatal", source="uncaught"), item(level="weird", source="api", message="x" * 5000, stack="s" * 13000, context={"big": "y" * 30000})]})).status_code == 201
    assert (await client.post("/api/app/errors", headers=auth("garbage"), json={"device": {**DEV, "device_id": "dev-2"}, "items": [item(level="warning", source="api")]})).status_code == 201
    assert (await client.post("/api/app/errors", json={"device": DEV, "items": []})).status_code == 422
    r = await client.post("/api/app/errors", json={"device": DEV, "items": [item()] * 51})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "TOO_MANY_ITEMS"

    h = auth(admin)
    r = (await client.get("/api/admin/app-errors", headers=h)).json()
    assert r["total"] == 4
    by_src = {i["id"]: i for i in r["items"]}
    mine = [i for i in r["items"] if i["user_id"] == uid]
    assert len(mine) == 2 and mine[0]["user_name"] == "Reporter"
    big = next(i for i in r["items"] if i["source"] == "api" and i["user_id"] == uid)
    assert big["level"] == "error" and len(big["message"]) == 4000 and len(big["stack"]) == 12000 and big["context"]["_truncated"] is True
    assert (await client.get("/api/admin/app-errors?level=fatal", headers=h)).json()["total"] == 1
    assert (await client.get("/api/admin/app-errors?source=api&user_id=" + str(uid), headers=h)).json()["total"] == 1
    assert (await client.get("/api/admin/app-errors?app_version=1.0.0%2B41", headers=h)).json()["total"] == 4
    assert (await client.get("/api/admin/app-errors?date_from=2999-01-01", headers=h)).json()["total"] == 0
    assert (await client.get("/api/admin/app-errors?limit=2", headers=h)).json()["items"].__len__() == 2
    s = (await client.get("/api/admin/app-errors/summary", headers=h)).json()
    assert s["last_24h"] == {"fatal": 1, "error": 2, "warning": 1} and s["top_sources"][0]["count"] >= 1
    assert (await client.get("/api/admin/app-errors", headers=auth(token))).status_code == 403
    assert by_src


async def test_rate_limit_and_retention(client):
    from app.database import SessionLocal
    from app.models import AppErrorLog, utcnow
    from app.routers import app_errors
    app_errors._hits.clear()
    codes = [(await client.post("/api/app/errors", json={"device": {"device_id": "flood"}, "items": [item()]})).status_code for _ in range(31)]
    assert codes[:30] == [201] * 30 and codes[30] == 429
    app_errors._hits.clear()
    async with SessionLocal() as db:
        old = (await db.execute(__import__("sqlalchemy").select(AppErrorLog).limit(1))).scalars().first()
        old.created_at = utcnow() - timedelta(days=31)
        await db.commit()
        await app_errors.purge_old(db)
        await db.commit()
        left = (await db.execute(__import__("sqlalchemy").select(__import__("sqlalchemy").func.count()).select_from(AppErrorLog))).scalar_one()
    assert left == 29

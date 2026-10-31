import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _alembic(db, *args):
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db}"}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env, capture_output=True, text=True)


def test_migrations_apply_and_match_models(tmp_path):
    db = tmp_path / "m.db"
    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    chk = _alembic(db, "check")  # fails when a model changed without a new migration
    assert chk.returncode == 0, chk.stdout + chk.stderr


async def test_order_status_change_publishes_event(client):
    from app.services import events
    from tests.conftest import auth, order_body, setup_world
    seen = []

    @events.on(events.OrderStatusChanged)
    async def spy(e):
        seen.append((e.old_status, e.new_status))

    @events.on(events.OrderStatusChanged)
    async def broken(e):  # a failing handler must not break the request
        raise RuntimeError("boom")

    try:
        _, _, (cid, ct), [(did, dt)] = await setup_world(client)
        r = await client.post("/api/orders", headers=auth(ct), json=order_body())
        assert r.status_code == 201
        assert seen and seen[0][0] == "PENDING"
    finally:
        events.clear()

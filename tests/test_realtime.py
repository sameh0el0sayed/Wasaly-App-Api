import asyncio

from app.constants import AccountStatus
from app.database import SessionLocal
from app.models import Order
from app.routers.stream import event_stream
from app.services.realtime import hub
from tests.conftest import auth, order_body, register_customer, setup_world
from tests.test_subscriptions import BODY


def _drain(conn):
    out = []
    while not conn.queue.empty():
        out.append(conn.queue.get_nowait())
    return out


class FakeRequest:
    async def is_disconnected(self):
        return False


async def test_order_events_reach_owner_driver_and_admin_only(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    oid_, other, _ = await register_customer(client, "Other")
    c_admin, c_cust, c_drv = hub.connect(999999, True), hub.connect(cid, False), hub.connect(did, False)
    c_other = hub.connect(oid_, False)
    try:
        r = await client.post("/api/orders", headers=auth(ct), json=order_body())
        oid = r.json()["id"]
        offer = (await client.get("/api/driver/offers", headers=auth(dt))).json()[0]
        await client.post(f"/api/driver/offers/{offer['id']}/accept", headers=auth(dt))
        await client.post(f"/api/driver/orders/{oid}/on-the-way", headers=auth(dt))
        for c in (c_admin, c_cust, c_drv):
            evs = _drain(c)
            types = [e["type"] for e in evs]
            assert "order.created" in types or c is c_drv  # the driver only hears once the order is theirs
            assert any(e["type"] == "order.status" and e["new_status"] == "DRIVER_ON_THE_WAY" and e["order_id"] == oid for e in evs), c
        assert _drain(c_other) == []  # a stranger never hears about it
    finally:
        for c in (c_admin, c_cust, c_drv, c_other):
            hub.disconnect(c)


async def test_nothing_is_published_on_rollback_and_events_follow_commit(client):
    admin, _, (cid, ct), _ = await setup_world(client)
    conn = hub.connect(cid, False)
    try:
        oid = (await client.post("/api/orders", headers=auth(ct), json=order_body())).json()["id"]
        _drain(conn)
        async with SessionLocal() as db:
            o = await db.get(Order, oid)
            o.order_status = "CANCELLED"
            await db.flush()
            assert conn.queue.empty()  # not before commit
            await db.rollback()
        assert conn.queue.empty()  # and never after a rollback
        async with SessionLocal() as db:
            o = await db.get(Order, oid)
            o.order_status = "CANCELLED"
            await db.commit()
        evs = _drain(conn)
        assert len(evs) == 1 and evs[0]["new_status"] == "CANCELLED"
    finally:
        hub.disconnect(conn)


async def test_subscription_events(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    conn = hub.connect(cid, False)
    try:
        r = await client.post("/api/subscriptions", headers=auth(ct), json=BODY)
        assert r.status_code == 201, r.text
        sid = r.json()["id"]
        assert any(e["type"] == "subscription.created" and e["subscription_id"] == sid for e in _drain(conn))
        q = await client.post(f"/api/admin/subscriptions/{sid}/quote", headers=auth(admin), json={"monthly_price": "40", "driver_id": did})
        assert q.status_code == 200, q.text
        assert any(e["type"] == "subscription.status" and e["new_status"] == "QUOTED" for e in _drain(conn))
    finally:
        hub.disconnect(conn)


async def test_stream_endpoint_requires_login_and_generator_pings_and_cleans_up(client):
    assert (await client.get("/api/stream")).status_code == 401
    admin, _, (cid, ct), _ = await setup_world(client)

    class U:
        id, role = cid, "CUSTOMER"
        account_status = AccountStatus.APPROVED

    before = len(hub.connections)
    gen = event_stream(FakeRequest(), U(), ping_seconds=0.05)
    first = await gen.__anext__()
    assert "connected" in first and len(hub.connections) == before + 1
    assert (await gen.__anext__()).startswith(": ping")
    hub.publish({"type": "order.status", "order_id": 1}, {cid})
    msg = await gen.__anext__()
    assert msg.startswith("event: order.status\ndata: ") and msg.endswith("\n\n")
    await gen.aclose()
    await asyncio.sleep(0)
    assert len(hub.connections) == before  # disconnect cleans up

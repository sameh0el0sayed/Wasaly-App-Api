from datetime import timedelta

from sqlalchemy import select, update

from app.database import SessionLocal
from app.models import Subscription, SubscriptionTrip
from app.services import subscriptions as svc

from .conftest import auth, register_customer, register_driver, setup_world
from .test_payments import PNG

BODY = {"category": "SCHOOL", "passenger_name": "Sara", "school_name": "Bahrain School", "pickup_address": "Seef, Block 428",
        "destination_address": "Bahrain School, Juffair", "weekdays": [0, 1, 2, 3, 6], "pickup_time": "06:45",
        "return_trip": True, "return_time": "13:30"}


async def _pay_by_benefitpay(client, ctoken, admin, pid):
    up = (await client.post("/api/uploads", headers=auth(ctoken), files={"file": ("p.png", PNG, "image/png")})).json()["file_key"]
    r = await client.post(f"/api/payments/{pid}/benefitpay", headers=auth(ctoken), json={"proof_file_key": up})
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/admin/payments/{pid}/confirm", headers=auth(admin))
    assert r.status_code == 200, r.text


async def test_subscription_request_quote_pay_activate_and_driver_completes(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    # school needs the student and school names
    bad = {**BODY}
    bad.pop("passenger_name")
    assert (await client.post("/api/subscriptions", headers=auth(ctoken), json=bad)).status_code == 422
    r = await client.post("/api/subscriptions", headers=auth(ctoken), json=BODY)
    assert r.status_code == 201 and r.json()["status"] == "REQUESTED" and r.json()["monthly_total"] is None
    sid = r.json()["id"]

    # customers cannot price their own subscription
    q = {"monthly_price": "40", "driver_id": did}
    assert (await client.post(f"/api/admin/subscriptions/{sid}/quote", headers=auth(ctoken), json=q)).status_code == 403
    # driver_id must be a driver
    assert (await client.post(f"/api/admin/subscriptions/{sid}/quote", headers=auth(admin), json={"monthly_price": "40", "driver_id": cid})).status_code == 422
    r = await client.post(f"/api/admin/subscriptions/{sid}/quote", headers=auth(admin), json=q)
    assert r.status_code == 200, r.text
    a = r.json()
    # default subscription commission 30%, mode from settings (tests use plain 20% mode DEDUCT -> mode only; value is 30)
    assert (a["monthly_price"], a["commission_value"], a["status"]) == ("40.000", "30", "QUOTED")
    assert a["customer_total"] == ("40.000" if a["commission_mode"] == "DEDUCT" else "52.000")
    assert a["driver_earning"] == ("28.000" if a["commission_mode"] == "DEDUCT" else "40.000")

    mine = (await client.get(f"/api/subscriptions/{sid}", headers=auth(ctoken))).json()
    pid = mine["open_payment"]["id"]
    assert mine["open_payment"]["kind"] == "SUBSCRIPTION" and mine["open_payment"]["amount"] == a["customer_total"]
    await client.put("/api/driver/payout-account", headers=auth(dtoken),
                     json={"method": "IBAN", "account": "BH67BMAG00001299123456", "holder_name": "Driver Zero"})
    await _pay_by_benefitpay(client, ctoken, admin, pid)

    s = (await client.get(f"/api/subscriptions/{sid}", headers=auth(ctoken))).json()
    assert s["status"] == "ACTIVE" and s["open_payment"] is None
    trips = (await client.get(f"/api/subscriptions/{sid}/trips?limit=200", headers=auth(ctoken))).json()
    legs = {t["leg"] for t in trips}
    assert legs == {"GO", "RETURN"} and all(t["status"] == "SCHEDULED" for t in trips)
    assert len(trips) % 2 == 0 and 38 <= len(trips) <= 46  # 5 weekdays x ~4.3 weeks x 2 legs
    po = (await client.get("/api/admin/payouts", headers=auth(admin))).json()
    assert len(po) == 1 and po[0]["amount"] == a["driver_earning"]

    # driver: sees only his trips, can only complete today's
    today = svc.today_bh()
    async with SessionLocal() as db:  # make sure there is a trip today and one in the future
        t0 = (await db.execute(select(SubscriptionTrip).where(
            SubscriptionTrip.subscription_id == sid, SubscriptionTrip.trip_date == today, SubscriptionTrip.leg == "GO"))).scalars().first()
        if not t0:
            t0 = SubscriptionTrip(subscription_id=sid, driver_id=did, trip_date=today, leg="GO", scheduled_time="06:45")
            db.add(t0)
        tf = SubscriptionTrip(subscription_id=sid, driver_id=did, trip_date=today + timedelta(days=100), leg="GO", scheduled_time="07:00")
        db.add(tf)
        await db.commit()
        today_id, future = t0.id, tf.id
    day = (await client.get("/api/driver/subscription-trips", headers=auth(dtoken))).json()
    assert any(t["id"] == today_id and t["passenger_name"] == "Sara" for t in day)
    assert (await client.post(f"/api/driver/subscription-trips/{future}/complete", headers=auth(dtoken))).json()["detail"]["code"] == "NOT_TODAY"
    other = (await register_driver(client, "Other"))[1]
    assert (await client.post(f"/api/driver/subscription-trips/{today_id}/complete", headers=auth(other))).status_code in (403, 404)
    r = await client.post(f"/api/driver/subscription-trips/{today_id}/complete", headers=auth(dtoken))
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"
    assert (await client.post(f"/api/driver/subscription-trips/{today_id}/complete", headers=auth(dtoken))).status_code == 409

    # renewal: next month is a new payment, the period extends by 30 days from the end of the current one
    before = (await client.get(f"/api/subscriptions/{sid}", headers=auth(ctoken))).json()["paid_until"]
    r = await client.post(f"/api/subscriptions/{sid}/renew", headers=auth(ctoken))
    assert r.status_code == 200
    assert (await client.post(f"/api/subscriptions/{sid}/renew", headers=auth(ctoken))).json()["id"] == r.json()["id"]
    await _pay_by_benefitpay(client, ctoken, admin, r.json()["id"])
    after = (await client.get(f"/api/subscriptions/{sid}", headers=auth(ctoken))).json()["paid_until"]
    from datetime import date
    assert date.fromisoformat(after) - date.fromisoformat(before) == timedelta(days=30)

    # expiry sweep
    async with SessionLocal() as db:
        await db.execute(update(Subscription).where(Subscription.id == sid).values(paid_until=today - timedelta(days=1)))
        await db.commit()
        await svc.sweep(db)
        await db.commit()
    assert (await client.get(f"/api/subscriptions/{sid}", headers=auth(ctoken))).json()["status"] == "EXPIRED"


async def test_subscription_cancel_pause_and_isolation(client):
    admin, _, (cid, ctoken), [(did, dtoken)] = await setup_world(client)
    other_c = await register_customer(client, "Other")
    sid = (await client.post("/api/subscriptions", headers=auth(ctoken), json={**BODY, "category": "WORK", "return_trip": False,
                                                                              "return_time": None})).json()["id"]
    assert (await client.get(f"/api/subscriptions/{sid}", headers=auth(other_c[1]))).status_code in (403, 404)
    r = await client.post(f"/api/admin/subscriptions/{sid}/reject", headers=auth(admin), json={"reason": "خارج المنطقة"})
    assert r.json()["status"] == "REJECTED"
    sid2 = (await client.post("/api/subscriptions", headers=auth(ctoken), json={**BODY, "category": "BUSINESS", "return_trip": False,
                                                                               "return_time": None})).json()["id"]
    await client.post(f"/api/admin/subscriptions/{sid2}/quote", headers=auth(admin), json={"monthly_price": "30", "driver_id": did})
    pid = (await client.get(f"/api/subscriptions/{sid2}", headers=auth(ctoken))).json()["open_payment"]["id"]
    await _pay_by_benefitpay(client, ctoken, admin, pid)
    assert (await client.post(f"/api/admin/subscriptions/{sid2}/pause", headers=auth(admin), json={"reason": "سفر العميل"})).json()["status"] == "PAUSED"
    assert (await client.post(f"/api/admin/subscriptions/{sid2}/resume", headers=auth(admin))).json()["status"] == "ACTIVE"
    r = await client.post(f"/api/subscriptions/{sid2}/cancel", headers=auth(ctoken), json={"reason": "لم أعد أحتاجه"})
    assert r.json()["status"] == "CANCELLED"
    trips = (await client.get(f"/api/admin/subscriptions/{sid2}/trips?limit=200", headers=auth(admin))).json()
    today = svc.today_bh().isoformat()
    assert all(t["status"] in ("CANCELLED", "SCHEDULED") for t in trips)
    assert all(t["status"] == "CANCELLED" for t in trips if t["date"] >= today)

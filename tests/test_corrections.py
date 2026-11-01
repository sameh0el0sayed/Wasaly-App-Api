from .conftest import auth, register_customer, setup_world

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


async def _up(client, token, kind="misc"):
    r = await client.post("/api/uploads", headers=auth(token), files={"file": ("p.png", PNG, "image/png")}, data={"kind": kind})
    assert r.status_code == 201, r.text
    return r.json()["file_key"]


async def test_correction_does_not_lock_but_blocks_going_online(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    h = auth(admin)
    r = await client.post(f"/api/admin/users/{did}/request-correction", headers=h, json={
        "reason": "please fix", "fields": [{"field": "cpr_photo", "reason": "blurry"}, {"field": "vehicle.photos.interior", "reason": "missing"}]})
    assert r.status_code == 200, r.text
    assert r.json()["account_status"] == "APPROVED" and len(r.json()["required_corrections"]) == 2
    # login still works, profile lists the corrections
    prof = (await client.get("/api/profile", headers=auth(dt))).json()
    assert [c["field"] for c in prof["required_corrections"]] == ["cpr_photo", "vehicle.photos.interior"]
    # offline -> cannot go online while corrections are open
    await client.post("/api/driver/availability", headers=auth(dt), json={"online": False})
    r = await client.post("/api/driver/availability", headers=auth(dt), json={"online": True})
    assert r.status_code == 403 and "CORRECTIONS_REQUIRED" in r.json()["detail"]["reasons"]
    # invalid field / customer-only restriction
    bad = await client.post(f"/api/admin/users/{did}/request-correction", headers=h, json={"fields": [{"field": "nope", "reason": "xx"}]})
    assert bad.status_code == 422
    bad = await client.post(f"/api/admin/users/{cid}/request-correction", headers=h, json={"fields": [{"field": "vehicle.plate_number", "reason": "xx"}]})
    assert bad.status_code == 422

    items = (await client.get("/api/corrections", headers=auth(dt))).json()["items"]
    assert len(items) == 2 and all(i["status"] == "OPEN" for i in items)
    assert (await client.post("/api/corrections/submit", headers=auth(dt), json={"items": []})).status_code == 422
    assert (await client.post("/api/corrections/submit", headers=auth(dt), json={"items": [{"id": items[0]["id"], "value": "nope/x.png"}]})).status_code == 422
    k1, k2 = await _up(client, dt), await _up(client, dt)
    r = await client.post("/api/corrections/submit", headers=auth(dt), json={"items": [{"id": items[0]["id"], "value": k1}, {"id": items[1]["id"], "value": k2}]})
    assert r.status_code == 200, r.text
    assert (await client.post("/api/corrections/submit", headers=auth(dt), json={"items": [{"id": items[0]["id"], "value": k1}]})).status_code == 409
    # submitted (not yet accepted): value NOT applied, driver may go online again
    assert (await client.post("/api/driver/availability", headers=auth(dt), json={"online": True})).status_code == 200

    pend = (await client.get("/api/admin/corrections?status=SUBMITTED", headers=h)).json()
    assert pend["total"] == 2 and pend["items"][0]["new_url"]
    a, b = sorted(pend["items"], key=lambda x: x["id"])
    # reject needs a reason, goes back to OPEN
    assert (await client.post(f"/api/admin/corrections/{a['id']}/review", headers=h, json={"status": "REJECTED"})).status_code == 422
    r = await client.post(f"/api/admin/corrections/{a['id']}/review", headers=h, json={"status": "REJECTED", "reason": "still blurry"})
    assert r.status_code == 200 and r.json()["status"] == "OPEN"
    await client.post("/api/driver/availability", headers=auth(dt), json={"online": False})
    r = await client.post("/api/driver/availability", headers=auth(dt), json={"online": True})
    assert r.status_code == 403
    # resubmit + accept everything -> applied and no more blockers
    k3 = await _up(client, dt)
    await client.post("/api/corrections/submit", headers=auth(dt), json={"items": [{"id": a["id"], "value": k3}]})
    assert (await client.post(f"/api/admin/corrections/{a['id']}/review", headers=h, json={"status": "ACCEPTED"})).json()["status"] == "ACCEPTED"
    assert (await client.post(f"/api/admin/corrections/{b['id']}/review", headers=h, json={"status": "ACCEPTED"})).json()["status"] == "ACCEPTED"
    d = (await client.get(f"/api/admin/users/{did}", headers=h)).json()
    assert d["cpr_photo"] == k3 and d["driver"]["vehicle"]["photos"]["interior"] == k2
    assert (await client.post("/api/driver/availability", headers=auth(dt), json={"online": True})).status_code == 200
    assert (await client.get("/api/profile", headers=auth(dt))).json()["required_corrections"] == []


async def test_text_correction_and_phone_change_need_admin_approval(client):
    admin, _, (cid, ct), [(did, dt)] = await setup_world(client)
    h = auth(admin)
    # text correction: past licence date is refused
    r = await client.post(f"/api/admin/users/{did}/request-correction", headers=h, json={"fields": [{"field": "license.expiry_date", "reason": "renewed?"}]})
    cor = r.json()["required_corrections"][0]["id"]
    r = await client.post("/api/corrections/submit", headers=auth(dt), json={"items": [{"id": cor, "value": "2001-01-01"}]})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "LICENSE_EXPIRED"
    await client.post("/api/corrections/submit", headers=auth(dt), json={"items": [{"id": cor, "value": "2040-02-02"}]})
    await client.post(f"/api/admin/corrections/{cor}/review", headers=h, json={"status": "ACCEPTED"})
    assert (await client.get(f"/api/admin/users/{did}", headers=h)).json()["driver"]["license_expiry_date"] == "2040-02-02"

    # phone change: pending, old number keeps working, admin accepts
    r = await client.post("/api/profile/phone/request", headers=auth(ct), json={"phone": "39998888"})
    assert r.status_code == 202, r.text
    assert (await client.get("/api/profile", headers=auth(ct))).json()["pending_changes"][0]["new_value"] == "+97339998888"
    me = (await client.get("/api/profile", headers=auth(ct))).json()
    assert me["phone"] != "+97339998888"
    pend = (await client.get("/api/admin/pending-changes?status=PENDING_REVIEW", headers=h)).json()
    assert pend["total"] == 1 and pend["items"][0]["new_value"] == "+97339998888"
    pid = pend["items"][0]["id"]
    assert (await client.post(f"/api/admin/pending-changes/{pid}/review", headers=h, json={"status": "ACCEPTED"})).status_code == 200
    assert (await client.get("/api/profile", headers=auth(ct))).json()["phone"] == "+97339998888"
    assert (await client.post(f"/api/admin/pending-changes/{pid}/review", headers=h, json={"status": "ACCEPTED"})).status_code == 409
    # duplicate number refused up front; rejected request keeps the old number
    _, other, _ = await register_customer(client, "Other")
    r = await client.post("/api/profile/phone/request", headers=auth(other), json={"phone": "39998888"})
    assert r.status_code == 409 and r.json()["detail"]["code"] == "PHONE_EXISTS"
    await client.post("/api/profile/phone/request", headers=auth(other), json={"phone": "39997777"})
    pid2 = (await client.get("/api/admin/pending-changes?status=PENDING_REVIEW", headers=h)).json()["items"][0]["id"]
    r = await client.post(f"/api/admin/pending-changes/{pid2}/review", headers=h, json={"status": "REJECTED", "reason": "not verified"})
    assert r.status_code == 200 and r.json()["status"] == "REJECTED"

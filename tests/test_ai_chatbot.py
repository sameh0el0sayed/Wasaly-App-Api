from .conftest import ADMIN, auth, login


async def test_ai_reviewer_can_approve_pending_customer_and_is_limited(client):
    root = await login(client, ADMIN["phone"], ADMIN["password"])
    created = await client.post("/api/admin/admins", headers=auth(root), json={
        "full_name": "AI Reviewer", "phone": "35500001", "password": "ReviewPass!123", "role": "AI"
    })
    assert created.status_code == 201, created.text
    reviewer = await login(client, "35500001", "ReviewPass!123")
    registered = await client.post("/api/auth/register", json={
        "role": "CUSTOMER", "full_name": "Pending Chat User", "phone": "33000001", "cpr": "900100001",
        "password": "Passw0rd!", "password_confirm": "Passw0rd!",
        "cpr_issue_date": "2020-01-01", "cpr_expiry_date": "2030-01-01"
    })
    assert registered.status_code == 201, registered.text
    customer_id, pending_token = registered.json()["user"]["id"], registered.json()["access_token"]

    response = await client.post("/api/chat", headers=auth(pending_token), json={"message": "Hello"})
    assert response.status_code == 403

    queue = await client.get("/api/admin/ai/users", headers=auth(reviewer))
    assert queue.status_code == 200 and any(row["id"] == customer_id for row in queue.json())
    approved = await client.post(f"/api/admin/users/{customer_id}/approve", headers=auth(reviewer), json={})
    assert approved.status_code == 200 and approved.json()["account_status"] == "APPROVED"

    assert (await client.get("/api/admin/dashboard", headers=auth(reviewer))).status_code == 403
    assert (await client.get("/api/admin/ai/knowledge", headers=auth(reviewer))).status_code == 200


async def test_chat_uses_gemini_and_blocks_other_users_conversation(client, monkeypatch):
    import app.routers.chat as chat_module

    root = await login(client, ADMIN["phone"], ADMIN["password"])
    registered = await client.post("/api/auth/register", json={
        "role": "CUSTOMER", "full_name": "Chat Customer", "phone": "33000002", "cpr": "900100002",
        "password": "Passw0rd!", "password_confirm": "Passw0rd!",
        "cpr_issue_date": "2020-01-01", "cpr_expiry_date": "2030-01-01"
    })
    assert registered.status_code == 201, registered.text
    customer_id, token = registered.json()["user"]["id"], registered.json()["access_token"]
    assert (await client.post(f"/api/admin/users/{customer_id}/approve", headers=auth(root), json={})).status_code == 200

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"candidates": [{"content": {"parts": [{"text": "استخدم تقدير السعر في التطبيق."}]}}]}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, json):
            assert "gemini-3.8-flash:generateContent" in url
            assert headers["x-goog-api-key"] == "test-key"
            assert json["generationConfig"] == {
                "maxOutputTokens": 1024,
                "thinkingConfig": {"thinkingLevel": "low"},
            }
            return FakeResponse()

    monkeypatch.setattr(chat_module.settings, "gemini_api_key", "test-key")
    monkeypatch.setattr(chat_module.settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(chat_module.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    first = await client.post("/api/chat", headers=auth(token), json={"message": "كم السعر؟"})
    assert first.status_code == 200 and first.json()["success"] is True
    assert first.json()["reply"] == "استخدم تقدير السعر في التطبيق."
    cid = first.json()["conversation_id"]
    second = await client.post("/api/chat", headers=auth(token), json={"message": "شكرا", "conversation_id": cid})
    assert second.status_code == 200 and second.json()["conversation_id"] == cid

    other = await client.post("/api/auth/register", json={
        "role": "CUSTOMER", "full_name": "Other Customer", "phone": "33000003", "cpr": "900100003",
        "password": "Passw0rd!", "password_confirm": "Passw0rd!",
        "cpr_issue_date": "2020-01-01", "cpr_expiry_date": "2030-01-01"
    })
    other_id, other_token = other.json()["user"]["id"], other.json()["access_token"]
    await client.post(f"/api/admin/users/{other_id}/approve", headers=auth(root), json={})
    stolen = await client.post("/api/chat", headers=auth(other_token), json={"message": "Hello", "conversation_id": cid})
    assert stolen.status_code == 404

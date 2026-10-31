"""Cloudinary backend against a fake Cloudinary (httpx MockTransport) that verifies the request signatures."""
import hashlib
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi import HTTPException

from app.config import settings
from app.services import files

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64
SECRET = "test-secret-xyz"


def _sig(d):
    s = "&".join(f"{k}={v}" for k, v in sorted(d.items()))
    return hashlib.sha1((s + SECRET).encode()).hexdigest()


@pytest.fixture
def fake(monkeypatch):
    store = {}

    def handler(req: httpx.Request):
        if req.url.path.endswith("/raw/upload"):
            from email.parser import BytesParser
            from email.policy import default
            msg = BytesParser(policy=default).parsebytes(b"Content-Type: " + req.headers["content-type"].encode() + b"\r\n\r\n" + req.content)
            fields = {}
            for part in msg.iter_parts():
                name = part.get_param("name", header="content-disposition")
                fields["_file" if name == "file" else name] = part.get_payload(decode=True) if name == "file" else part.get_content().strip()
            sig, pub = fields["signature"], fields["public_id"]
            assert fields["api_key"] == "key123" and fields["type"] == "authenticated"
            assert sig == _sig({"public_id": pub, "timestamp": fields["timestamp"], "type": "authenticated"})
            store[pub] = fields["_file"]
            return httpx.Response(200, json={"public_id": pub})
        if req.url.path.endswith("/raw/download"):
            q = {k: v[0] for k, v in parse_qs(req.url.query.decode()).items()}
            sig = q.pop("signature"); q.pop("api_key")
            if sig != _sig(q):
                return httpx.Response(401)
            return httpx.Response(200, content=store[q["public_id"]]) if q["public_id"] in store else httpx.Response(404)
        return httpx.Response(400)

    monkeypatch.setattr(settings, "storage_backend", "cloudinary")
    monkeypatch.setattr(settings, "cloudinary_cloud_name", "democloud")
    monkeypatch.setattr(settings, "cloudinary_api_key", "key123")
    monkeypatch.setattr(settings, "cloudinary_api_secret", SECRET)
    monkeypatch.setattr(files, "_transport", httpx.MockTransport(handler))
    return store


async def test_roundtrip_private_and_signed(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    await files.put_bytes("docs/a.png", PNG, "image/png")
    assert list(fake) == ["wasaly/docs/a.png"]
    assert await files.get_bytes("docs/a.png") == PNG
    assert list(tmp_path.rglob("*")) == []  # nothing on the app server's disk
    with pytest.raises(HTTPException) as e:
        await files.get_bytes("docs/missing.png")
    assert e.value.status_code == 404


async def test_startup_fails_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "storage_backend", "cloudinary")
    monkeypatch.setattr(settings, "cloudinary_api_secret", "")
    with pytest.raises(RuntimeError):
        await files.ensure_storage()

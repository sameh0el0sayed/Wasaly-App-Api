import hashlib
import os
import time
import uuid
from pathlib import Path

import httpx
from fastapi import HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..models import UploadedFile

ALLOWED = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf"}
MAGIC = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"RIFF": "image/webp", b"%PDF": "application/pdf"}


def storage_root() -> Path:
    p = Path(settings.upload_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sign(params: dict) -> str:
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v not in (None, ""))
    return hashlib.sha1((to_sign + settings.cloudinary_api_secret).encode()).hexdigest()


def _public_id(key: str) -> str:
    folder = settings.cloudinary_folder.strip("/")
    return f"{folder}/{key}" if folder else key


def _api(path: str) -> str:
    return f"https://api.cloudinary.com/v1_1/{settings.cloudinary_cloud_name}/{path}"


_transport = None  # tests inject an httpx transport here


def _http() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30, transport=_transport)


async def ensure_storage() -> None:
    """Startup check: fail loudly on missing Cloudinary settings so files are never silently lost."""
    if settings.cloudinary_url and not settings.cloudinary_cloud_name:
        from urllib.parse import urlparse
        u = urlparse(settings.cloudinary_url)
        settings.cloudinary_api_key, settings.cloudinary_api_secret, settings.cloudinary_cloud_name = u.username or "", u.password or "", u.hostname or ""
    if settings.storage_backend == "cloudinary" and not (
            settings.cloudinary_cloud_name and settings.cloudinary_api_key and settings.cloudinary_api_secret):
        raise RuntimeError("STORAGE_BACKEND=cloudinary needs CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY and "
                           "CLOUDINARY_API_SECRET (or set STORAGE_BACKEND=local for development)")


async def put_bytes(key: str, data: bytes, content_type: str) -> None:
    """cloudinary: private ('authenticated') raw asset, never public; downloads go through signed API links.
    local: file on disk (dev only)."""
    if settings.storage_backend == "cloudinary":
        params = {"public_id": _public_id(key), "timestamp": int(time.time()), "type": "authenticated"}
        form = {**params, "api_key": settings.cloudinary_api_key, "signature": _sign(params)}
        async with _http() as c:
            r = await c.post(_api("raw/upload"), data=form, files={"file": (key.rsplit("/", 1)[-1], data, content_type)})
        if r.status_code != 200:
            raise HTTPException(502, "File storage is unavailable, try again")
        return
    path = storage_root() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


async def get_bytes(key: str) -> bytes:
    if settings.storage_backend == "cloudinary":
        params = {"public_id": _public_id(key), "timestamp": int(time.time()), "type": "authenticated"}
        q = {**params, "api_key": settings.cloudinary_api_key, "signature": _sign(params)}
        async with _http() as c:
            r = await c.get(_api("raw/download"), params=q)
        if r.status_code == 404:
            raise HTTPException(404, "File not found")
        if r.status_code != 200:
            raise HTTPException(502, "File storage is unavailable, try again")
        return r.content
    return resolve(key).read_bytes()


async def delete_object(key: str) -> None:
    """Best effort: a missing file is fine."""
    if settings.storage_backend == "cloudinary":
        params = {"public_id": _public_id(key), "timestamp": int(time.time()), "type": "authenticated"}
        async with _http() as c:
            await c.post(_api("raw/destroy"), data={**params, "api_key": settings.cloudinary_api_key, "signature": _sign(params)})
        return
    try:
        resolve(key).unlink()
    except HTTPException:
        pass


async def save_upload(db: AsyncSession, owner_id: int | None, file: UploadFile, kind: str) -> UploadedFile:
    from . import settings_service
    limit_mb = int((await settings_service.get_all(db))["upload_max_mb"])
    data = await file.read(limit_mb * 1024 * 1024 + 1)
    if len(data) > limit_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {limit_mb} MB")
    sniffed = next((t for sig, t in MAGIC.items() if data.startswith(sig)), None)
    if sniffed not in ALLOWED:
        raise HTTPException(415, "Only JPEG, PNG, WEBP or PDF files are allowed")
    safe_kind = "".join(c for c in kind if c.isalnum() or c in "-_")[:30] or "misc"
    key = f"{safe_kind}/{uuid.uuid4().hex}{ALLOWED[sniffed]}"
    await put_bytes(key, data, sniffed)
    row = UploadedFile(file_key=key, owner_id=owner_id, original_name=(file.filename or "file")[:200],
                       content_type=sniffed, size=len(data))
    db.add(row)
    await db.flush()
    return row


async def assert_owned(db: AsyncSession, user_id: int, file_key: str, claim: bool = False) -> None:
    """A referenced file_key must belong to the user. With claim=True (registration) a file uploaded
    anonymously through /api/uploads/registration is claimed by this user."""
    row = await db.get(UploadedFile, file_key)
    if row and row.owner_id == user_id:
        return
    if claim and row and row.owner_id is None:
        row.owner_id = user_id
        return
    raise HTTPException(422, f"Unknown or foreign file_key: {file_key}")


def resolve(file_key: str) -> Path:
    root = storage_root().resolve()
    p = (root / file_key).resolve()
    if root not in p.parents or not p.exists() or not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    return p

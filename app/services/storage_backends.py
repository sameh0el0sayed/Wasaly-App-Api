"""Storage backends (Strategy pattern). Add a backend = add a class and register it in BACKENDS."""
import hashlib
import os
import time
from pathlib import Path
from typing import Protocol

import httpx
from fastapi import HTTPException

from ..config import settings

_transport = None  # tests inject an httpx transport here


class StorageBackend(Protocol):
    async def put(self, key: str, data: bytes, content_type: str) -> None: ...
    async def get(self, key: str) -> bytes: ...
    async def delete(self, key: str) -> None: ...


def storage_root() -> Path:
    p = Path(settings.upload_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p


def resolve(file_key: str) -> Path:
    root = storage_root().resolve()
    p = (root / file_key).resolve()
    if root not in p.parents or not p.exists() or not os.path.isfile(p):
        raise HTTPException(404, "File not found")
    return p


class LocalStorage:
    """Files on disk (development / tests only)."""

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        path = storage_root() / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    async def get(self, key: str) -> bytes:
        return resolve(key).read_bytes()

    async def delete(self, key: str) -> None:
        try:
            resolve(key).unlink()
        except HTTPException:
            pass


class CloudinaryStorage:
    """Private ('authenticated') raw assets: no public URL ever exists; downloads go through signed API links."""

    @staticmethod
    def _sign(params: dict) -> str:
        to_sign = "&".join(f"{k}={v}" for k, v in sorted(params.items()) if v not in (None, ""))
        return hashlib.sha1((to_sign + settings.cloudinary_api_secret).encode()).hexdigest()

    @staticmethod
    def _public_id(key: str) -> str:
        folder = settings.cloudinary_folder.strip("/")
        return f"{folder}/{key}" if folder else key

    @staticmethod
    def _api(path: str) -> str:
        return f"https://api.cloudinary.com/v1_1/{settings.cloudinary_cloud_name}/{path}"

    @staticmethod
    def _http() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=30, transport=_transport)

    def _signed(self, key: str) -> dict:
        params = {"public_id": self._public_id(key), "timestamp": int(time.time()), "type": "authenticated"}
        return {**params, "api_key": settings.cloudinary_api_key, "signature": self._sign(params)}

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        async with self._http() as c:
            r = await c.post(self._api("raw/upload"), data=self._signed(key),
                             files={"file": (key.rsplit("/", 1)[-1], data, content_type)})
        if r.status_code != 200:
            raise HTTPException(502, "File storage is unavailable, try again")

    async def get(self, key: str) -> bytes:
        async with self._http() as c:
            r = await c.get(self._api("raw/download"), params=self._signed(key))
        if r.status_code == 404:
            raise HTTPException(404, "File not found")
        if r.status_code != 200:
            raise HTTPException(502, "File storage is unavailable, try again")
        return r.content

    async def delete(self, key: str) -> None:
        """Best effort: a missing file is fine."""
        async with self._http() as c:
            await c.post(self._api("raw/destroy"), data=self._signed(key))


BACKENDS: dict[str, type] = {"cloudinary": CloudinaryStorage, "local": LocalStorage}


def backend() -> StorageBackend:
    try:
        return BACKENDS[settings.storage_backend]()
    except KeyError:
        raise RuntimeError(f"Unknown STORAGE_BACKEND '{settings.storage_backend}' (use one of {sorted(BACKENDS)})") from None

"""Thin Tap Payments client (https://developers.tap.company). Money is never trusted from a webhook body:
every payment is confirmed by RETRIEVING the charge from Tap with the secret key."""
import httpx
from fastapi import HTTPException

from ..config import settings


async def _headers() -> dict:
    from . import command
    key = await command.tap_secret()  # owner config (DB) first, .env as fallback
    if not key:
        raise HTTPException(503, {"code": "TAP_NOT_CONFIGURED", "message": "Set the Tap secret key (Phoenix Command or TAP_SECRET_KEY)"})
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def create_charge(payload: dict) -> dict:
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.post(f"{settings.tap_base_url}/charges", json=payload, headers=await _headers())
    if r.status_code // 100 != 2:
        raise HTTPException(502, {"code": "TAP_CHARGE_FAILED", "status": r.status_code})
    return r.json()


async def retrieve_charge(charge_id: str) -> dict:
    async with httpx.AsyncClient(timeout=25) as c:
        r = await c.get(f"{settings.tap_base_url}/charges/{charge_id}", headers=await _headers())
    if r.status_code // 100 != 2:
        raise HTTPException(502, {"code": "TAP_RETRIEVE_FAILED", "status": r.status_code})
    return r.json()

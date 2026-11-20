"""Phoenix ID (OIDC) support: discovery/JWKS cache, local RS256 verification, owner-editable settings (DB first, .env fallback)."""
import logging
import os
import time

import httpx
import jwt

from . import command

log = logging.getLogger("phoenix_id")
DEFAULT_ISSUER = "https://id.phoenixsystems.online"
CLIENT_ID = "wasaly"
_disc: dict = {"at": 0.0, "v": None}
_jwks: dict = {"at": 0.0, "keys": {}}
_reach: dict = {"at": 0.0, "ok": False}
_down_since: dict = {"t": None}
EMERGENCY_KEY = "wasaly:local_login"  # redis key; SET wasaly:local_login 1 EX 3600 -> local login allowed for 1 hour


async def cfg() -> dict:
    o = await command.owner_all()
    return {"enabled": o.get("phoenix_id_enabled", "false") == "true",
            "client_id": o.get("phoenix_id_client_id") or CLIENT_ID,
            "issuer": (o.get("phoenix_id_issuer") or DEFAULT_ISSUER).rstrip("/"),
            "client_secret": o.get("phoenix_id_client_secret") or os.environ.get("PHOENIX_ID_CLIENT_SECRET", "")}


async def discovery(force: bool = False) -> dict:
    c = await cfg()
    if not force and _disc["v"] and time.time() - _disc["at"] < 3600:
        return _disc["v"]
    async with httpx.AsyncClient(timeout=4) as h:
        r = await h.get(f"{c['issuer']}/.well-known/openid-configuration")
        r.raise_for_status()
        _disc.update(at=time.time(), v=r.json())
    return _disc["v"]


async def _load_jwks(force: bool = False) -> dict:
    if not force and _jwks["keys"] and time.time() - _jwks["at"] < 3600:
        return _jwks["keys"]
    d = await discovery()
    async with httpx.AsyncClient(timeout=4) as h:
        r = await h.get(d.get("jwks_uri") or f"{(await cfg())['issuer']}/.well-known/jwks.json")
        r.raise_for_status()
    keys = {}
    for k in r.json().get("keys", []):
        if k.get("kty") == "RSA" and k.get("kid"):
            keys[k["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(k)
    _jwks.update(at=time.time(), keys=keys)
    return keys


async def verify(token: str, *, logout: bool = False) -> dict:
    """Verify iss, aud (client_id), exp and the RS256 signature locally with the cached JWKS. Raises jwt.PyJWTError."""
    c = await cfg()
    kid = jwt.get_unverified_header(token).get("kid")
    keys = await _load_jwks()
    if kid not in keys:
        keys = await _load_jwks(force=True)  # key rotation
    if kid not in keys:
        raise jwt.InvalidTokenError("unknown kid")
    claims = jwt.decode(token, keys[kid], algorithms=["RS256"], audience=c["client_id"], issuer=c["issuer"],
                        options={"require": ["exp", "iss", "aud", "sub"]})
    if logout:
        if "http://schemas.openid.net/event/backchannel-logout" not in (claims.get("events") or {}) or "nonce" in claims:
            raise jwt.InvalidTokenError("not a logout token")
    return claims


async def userinfo(access_token: str) -> dict:
    d = await discovery()
    async with httpx.AsyncClient(timeout=4) as h:
        r = await h.get(d.get("userinfo_endpoint") or f"{(await cfg())['issuer']}/userinfo", headers={"Authorization": f"Bearer {access_token}"})
        r.raise_for_status()
        return r.json()


async def reachable() -> bool:
    """Cached 60 s; used by the PCC health page."""
    if time.time() - _reach["at"] < (60 if _reach["ok"] else 15):
        return _reach["ok"]
    try:
        await discovery(force=True)
        ok = True
    except Exception:  # noqa: BLE001
        ok = False
    _reach.update(at=time.time(), ok=ok)
    if ok:
        _down_since["t"] = None
    elif _down_since["t"] is None:
        _down_since["t"] = time.time()
    return ok


async def _emergency() -> bool:
    from . import cache
    r = await cache.client()
    if r is None:
        return False
    try:
        return bool(await r.exists(EMERGENCY_KEY))
    except Exception:  # noqa: BLE001
        return False


async def local_login_state() -> tuple[bool, str]:
    """(allowed, reason). reason: sso_off | maintenance | phoenix_id_down | emergency | sso_only."""
    c = await cfg()
    if not c["enabled"]:
        return True, "sso_off"
    if (await command.owner_all()).get("phoenix_id_maintenance_login", "false") == "true":
        return True, "maintenance"
    if not await reachable() and _down_since["t"] and time.time() - _down_since["t"] >= 60:
        return True, "phoenix_id_down"
    if await _emergency():
        return True, "emergency"
    return False, "sso_only"

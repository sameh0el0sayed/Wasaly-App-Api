"""Phoenix Command & Control (PCC) link: HMAC verification, outbound events, owner unified config, ring-buffer logs.
Everything here is best-effort: a missing link or a failing dashboard never breaks a user request."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import time
import traceback
from collections import deque
from datetime import UTC, datetime

import httpx
from sqlalchemy import select

from ..config import settings
from ..database import SessionLocal
from ..models import CommandLink, OwnerConfig, User
from ..security import decrypt

log = logging.getLogger("command")
SYSTEM_CODE = "wasaly"
ACTOR_NAME = "phoenix-command"
CACHE_TTL = 60

# ---------------------------------------------------------------- link (HMAC secret) ----------
_link_cache: dict = {"at": 0.0, "v": None}


def invalidate() -> None:
    _link_cache["at"] = 0.0
    _owner_cache["at"] = 0.0


async def get_link() -> dict | None:
    """The saved link (decrypted secret) or None. Cached 60 s; invalidated on save."""
    if time.time() - _link_cache["at"] < CACHE_TTL:
        return _link_cache["v"]
    v = None
    try:
        async with SessionLocal() as db:
            row = (await db.execute(select(CommandLink).limit(1))).scalar_one_or_none()
            if row and row.secret_encrypted:
                v = {"key_id": row.key_id, "secret": decrypt(row.secret_encrypted), "allowed_ips": row.allowed_ips or "",
                     "command_url": row.command_url, "enabled": bool(row.enabled)}
    except Exception:  # noqa: BLE001 - table may not exist yet (first deploy)
        v = None
    _link_cache.update(at=time.time(), v=v)
    return v


def sign(secret: str, method: str, path: str, body: bytes, ts: str) -> str:
    msg = f"{ts}.{method.upper()}.{path}.{hashlib.sha256(body or b'').hexdigest()}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def canonical_path(path: str) -> str | None:
    i = path.find("/control/v1")
    return path[i:].split("?")[0] if i >= 0 else None


def ip_allowed(allowed: str, ip: str | None) -> bool:
    items = [x.strip() for x in (allowed or "").replace("\n", ",").split(",") if x.strip()]
    if not items:
        return True
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in ipaddress.ip_network(i, strict=False) for i in items)
    except ValueError:
        return False


def verify_reason(link: dict, method: str, path: str, body: bytes, key: str | None, ts: str | None, sig: str | None, ip: str | None) -> str | None:
    """None = valid, otherwise a short reason (logged server-side only; the client always gets a bare 401)."""
    key, ts, sig = (key or "").strip(), (ts or "").strip(), (sig or "").strip().lower()
    if not key or not hmac.compare_digest(key, link["key_id"].strip()):
        return "key_id_mismatch"
    try:
        t = float(ts)
    except ValueError:
        return "bad_timestamp"
    if t > 1e12:  # tolerate milliseconds
        t /= 1000
    skew = abs(time.time() - t)
    if skew > 300:
        return f"timestamp_skew_{int(skew)}s"
    p = canonical_path(path)
    if not p:
        return "bad_path"
    secret = link["secret"].strip()
    for tsv in {ts, str(int(t))}:  # sign over the timestamp exactly as sent
        if hmac.compare_digest(sign(secret, method, p, body, tsv), sig):
            return None if ip_allowed(link["allowed_ips"], ip) else f"ip_not_allowed_{ip}"
    return "signature_mismatch"


def verify(link: dict, method: str, path: str, body: bytes, key: str | None, ts: str | None, sig: str | None, ip: str | None) -> bool:
    return verify_reason(link, method, path, body, key, ts, sig, ip) is None


def new_secret() -> str:
    return secrets.token_hex(32)  # 64 hex chars


# ---------------------------------------------------------------- outbound events ----------
_last_error_emit: dict[str, float] = {}
_bg: set = set()


async def _post(link: dict, payload: dict) -> tuple[bool, str]:
    path = f"/ingest/{SYSTEM_CODE}/events"
    body = json.dumps(payload, ensure_ascii=False, default=str).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Phoenix-Key": link["key_id"], "X-Phoenix-Timestamp": ts,
               "X-Phoenix-Signature": sign(link["secret"], "POST", path, body, ts)}
    url = f"{link['command_url'].rstrip('/')}/api/v1{path}"
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.post(url, content=body, headers=headers)
        return r.status_code // 100 == 2, f"HTTP {r.status_code}"
    except Exception as e:  # noqa: BLE001
        return False, type(e).__name__


async def _emit(severity: str, kind: str, title: str, body: str | None, data: dict | None) -> None:
    link = await get_link()
    if not link or not link["enabled"]:
        return
    await _post(link, {"severity": severity, "kind": kind, "title": title, "body": body, "data": data or {}})


def emit(severity: str, kind: str, title: str, body: str | None = None, data: dict | None = None) -> None:
    """Fire-and-forget. Safe to call from any request/handler; never raises, never blocks."""
    try:
        t = asyncio.get_running_loop().create_task(_emit(severity, kind, title, body, data))
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    except RuntimeError:  # no running loop (sync context): skip
        pass
    except Exception:  # noqa: BLE001
        log.debug("emit skipped", exc_info=True)


def emit_error(etype: str, path: str, tb: str) -> None:
    """error.unhandled: rate limited to 1 per minute per error type."""
    now = time.time()
    if now - _last_error_emit.get(etype, 0) < 60:
        return
    _last_error_emit[etype] = now
    emit("critical", "error.unhandled", f"خطأ غير متوقع: {etype}", f"{path}\n{tb[-800:]}", {"path": path, "type": etype})


async def send_test() -> dict:
    link = await get_link()
    if not link:
        return {"ok": False, "error": "NOT_CONFIGURED"}
    ok, info = await _post(link, {"severity": "info", "kind": "system.test", "title": "اختبار الاتصال من وصلي", "body": None, "data": {}})
    return {"ok": ok, "detail": info}


# ---------------------------------------------------------------- ring buffer of warnings/errors ----------
class RingHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.buf: deque = deque(maxlen=1000)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            if record.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*record.exc_info))[-1500:]
            self.buf.append({"time": datetime.fromtimestamp(record.created, UTC).isoformat(), "level": record.levelname.lower(), "message": msg[:3000]})
        except Exception:  # noqa: BLE001
            pass


ring = RingHandler()


def install_ring() -> None:
    root = logging.getLogger()
    if ring not in root.handlers:
        root.addHandler(ring)


# ---------------------------------------------------------------- owner unified config ----------
SECRET_KEYS = {"tap_secret_key", "tap_webhook_secret", "gemini_api_key", "apns_private_key", "tomtom_api_key"}
_owner_cache: dict = {"at": 0.0, "v": {}}
maint: dict = {"at": 0.0, "on": False, "msg": ""}  # maintenance flag cache (5 s), used by the middleware


async def owner_all() -> dict[str, str]:
    if time.time() - _owner_cache["at"] < CACHE_TTL:
        return _owner_cache["v"]
    v: dict[str, str] = {}
    try:
        async with SessionLocal() as db:
            for r in (await db.execute(select(OwnerConfig))).scalars():
                try:
                    v[r.key] = decrypt(r.value) if (r.is_secret and r.value) else r.value
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        v = {}
    _owner_cache.update(at=time.time(), v=v)
    return v


async def effective(key: str, env_value):
    """Owner config (DB) wins over .env; empty = not set -> env fallback."""
    v = (await owner_all()).get(key, "")
    return v if v != "" else env_value


async def gemini() -> dict:
    o = await owner_all()
    try:
        timeout = float(o.get("gemini_timeout_seconds") or settings.gemini_timeout_seconds)
    except ValueError:
        timeout = settings.gemini_timeout_seconds
    enabled = o.get("ai_enabled", "true") != "false"
    return {"key": (o.get("gemini_api_key") or settings.gemini_api_key) if enabled else "",
            "model": o.get("gemini_model") or settings.gemini_model,
            "fallbacks": [m.strip() for m in (o.get("gemini_fallback_models") or "").split(",") if m.strip()],
            "timeout": timeout}


async def tap_secret() -> str:
    return await effective("tap_secret_key", settings.tap_secret_key)


# ---------------------------------------------------------------- hidden actor for audit ----------
async def actor(db) -> User:
    """Hidden SUPER_ADMIN user whose name shows in every audit row written on behalf of PCC."""
    from ..constants import AccountStatus, Role
    from ..security import hash_password
    u = (await db.execute(select(User).where(User.full_name == ACTOR_NAME, User.role == Role.SUPER_ADMIN))).scalars().first()
    if u:
        return u
    u = User(full_name=ACTOR_NAME, phone=None, password_hash=hash_password(secrets.token_urlsafe(32)), role=Role.SUPER_ADMIN,
             account_status=AccountStatus.APPROVED, phone_verified=True)
    db.add(u)
    await db.flush()
    return u


# ---------------------------------------------------------------- periodic watcher + event handlers ----------
_seen: set = set()


async def watch_loop() -> None:
    """Hourly: subscriptions expiring in 3 days / past due -> events (each once per day)."""
    from datetime import date, timedelta

    from ..models import Subscription
    while True:
        try:
            await asyncio.sleep(120)
            link = await get_link()
            if link and link["enabled"]:
                today = date.today()
                async with SessionLocal() as db:
                    rows = (await db.execute(select(Subscription).where(Subscription.status == "ACTIVE", Subscription.paid_until.is_not(None),
                                                                        Subscription.paid_until <= today + timedelta(days=3)))).scalars().all()
                for s_ in rows:
                    past = s_.paid_until < today
                    k = ("past" if past else "soon", s_.id, today)
                    if k in _seen:
                        continue
                    _seen.add(k)
                    emit("warning", "subscription.past_due" if past else "subscription.expiring",
                         f"اشتراك #{s_.id} " + ("متأخر السداد" if past else "ينتهي قريبًا"), None,
                         {"subscription_id": s_.id, "customer_id": s_.customer_id, "paid_until": s_.paid_until.isoformat()})
                if len(_seen) > 5000:
                    _seen.clear()
            await asyncio.sleep(3480)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("command watch loop")
            await asyncio.sleep(600)


def register_handlers() -> None:
    from ..constants import OrderStatus as S
    from . import events

    @events.on(events.OrderStatusChanged)
    async def _no_driver(ev: events.OrderStatusChanged) -> None:
        if ev.new_status in (S.NO_DRIVER_AVAILABLE, S.EXPIRED) and ev.old_status != ev.new_status:
            emit("warning", "trip.no_driver", f"لا يوجد سائق للرحلة #{ev.order_id}", None, {"order_id": ev.order_id, "status": ev.new_status})

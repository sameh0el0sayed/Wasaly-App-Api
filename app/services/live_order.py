"""Live order card (Android ongoing notification + iOS Live Activity), customers and drivers.
Triggers: every order status change and driver ETA changes (<= 1 push / 60 s / order, only when eta_min changes).
Android: FCM DATA-only message type=live_order. iOS: APNs liveactivity push to the stored activity tokens. Best-effort, never raises."""
import asyncio
import logging
import os
import time
from datetime import timedelta

import httpx
import jwt
from sqlalchemy import delete, select

from ..constants import OrderStatus as S
from ..database import SessionLocal
from ..models import DeviceToken, LiveActivityToken, Order, Translation, User, utcnow
from . import command, events, settings_service
from .notifications import _firebase

log = logging.getLogger("live_order")
BAHRAIN = timedelta(hours=3)  # Asia/Bahrain has no DST
END_NOW = {S.CANCELLED, S.EXPIRED, S.NO_DRIVER_AVAILABLE}
ACTIVE = {S.PENDING, S.OFFERED, S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP, S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION, S.COMPLETED}
STEP = {S.PENDING: 0, S.OFFERED: 0, S.ACCEPTED: 1, S.DRIVER_ON_THE_WAY: 1, S.ARRIVED_AT_PICKUP: 2, S.IN_PROGRESS: 3, S.ARRIVED_AT_DESTINATION: 3, S.COMPLETED: 4}
TEXT_KEY = {S.PENDING: "searching", S.OFFERED: "searching", S.ACCEPTED: "accepted", S.DRIVER_ON_THE_WAY: "on_the_way", S.ARRIVED_AT_PICKUP: "arrived",
            S.IN_PROGRESS: "in_progress", S.ARRIVED_AT_DESTINATION: "arrived_dest", S.COMPLETED: "completed"}

_tr: dict = {"at": 0.0, "v": {}}
_last: dict[int, tuple[float, int | None]] = {}  # order_id -> (last ETA push time, last eta_min)
_bg: set = set()
_jwt: dict = {"at": 0.0, "v": "", "sig": ""}


async def _strings() -> dict:
    if time.time() - _tr["at"] < 120 and _tr["v"]:
        return _tr["v"]
    try:
        async with SessionLocal() as db:
            rows = (await db.execute(select(Translation.key, Translation.ar, Translation.en).where(Translation.key.like("v45.live.%")))).all()
        _tr.update(at=time.time(), v={k: {"ar": a, "en": e} for k, a, e in rows})
    except Exception:  # noqa: BLE001
        log.exception("live_order translations")
    return _tr["v"]


def _t(strings: dict, key: str, lang: str, *args) -> str:
    row = strings.get(key) or {}
    s = row.get("en" if lang == "en" else "ar") or row.get("ar") or key
    for i, a in enumerate(args):
        s = s.replace(f"{{{i}}}", str(a))
    return s


def progress_of(status: str, eta: int | None) -> int:
    def by_eta(base: int, span: int) -> int:
        return base if eta is None else round(base + span * (1 - min(max(eta, 0), 30) / 30))
    return {S.PENDING: 4, S.OFFERED: 8, S.ACCEPTED: 12, S.ARRIVED_AT_PICKUP: 34, S.ARRIVED_AT_DESTINATION: 94, S.COMPLETED: 100}.get(status) \
        or (by_eta(20, 13) if status == S.DRIVER_ON_THE_WAY else by_eta(50, 44) if status == S.IN_PROGRESS else 0)


def build_payload(order, status: str, eta: int | None, role: str, lang: str, strings: dict) -> dict:
    key = TEXT_KEY[status]
    if status == S.IN_PROGRESS and order.service_type != "PERSON":
        key = "delivering"
    text = _t(strings, "v45.live." + ("d_" if role == "driver" else "") + key, lang)
    completed = status == S.COMPLETED
    clock = ""
    if eta is not None and not completed:
        at = utcnow() + timedelta(minutes=eta) + BAHRAIN
        clock = "(" + at.strftime("%I:%M %p").lstrip("0") + ")"
    return {"order_id": str(order.id), "step": STEP[status], "progress": progress_of(status, eta), "eta_min": eta,
            "headline": _t(strings, "v45.live.done_headline", lang) if completed else (_t(strings, "v45.live.minutes", lang, eta) if eta is not None else _t(strings, "v45.live.headline", lang)),
            "clock": clock, "text": text, "end": completed, "end_now": False, "rtl": lang != "en", "channel": _t(strings, "v45.live.channel", lang)}


def _fcm_sync(token: str, data: dict) -> str:
    from firebase_admin import exceptions, messaging
    try:
        messaging.send(messaging.Message(token=token, data=data, android=messaging.AndroidConfig(priority="high")), app=_firebase())
        return "ok"
    except (messaging.UnregisteredError, exceptions.InvalidArgumentError):
        return "invalid"
    except Exception:  # noqa: BLE001
        log.exception("live_order FCM send failed")
        return "error"


async def _apns_jwt(key_id: str, team_id: str, pem: str) -> str:
    sig = f"{key_id}:{team_id}:{len(pem)}"
    if _jwt["v"] and _jwt["sig"] == sig and time.time() - _jwt["at"] < 3000:
        return _jwt["v"]
    tok = jwt.encode({"iss": team_id, "iat": int(time.time())}, pem, algorithm="ES256", headers={"kid": key_id})
    _jwt.update(at=time.time(), v=tok, sig=sig)
    return tok


async def _apns_send(cfg: dict, token: str, body: dict, priority: int) -> str:
    headers = {"authorization": f"bearer {await _apns_jwt(cfg['key_id'], cfg['team_id'], cfg['pem'])}", "apns-push-type": "liveactivity",
               "apns-topic": f"{cfg['bundle']}.push-type.liveactivity", "apns-priority": str(priority)}
    async with httpx.AsyncClient(http2=True, timeout=5) as h:
        for host in ("api.push.apple.com", "api.sandbox.push.apple.com"):  # dev builds produce sandbox tokens
            r = await h.post(f"https://{host}/3/device/{token}", json=body, headers=headers)
            if r.status_code == 200:
                return "ok"
            reason = (r.json() or {}).get("reason", "") if r.headers.get("content-type", "").startswith("application/json") else ""
            if reason == "BadDeviceToken" and host == "api.push.apple.com":
                continue
            if reason in ("Unregistered", "ExpiredToken", "BadDeviceToken", "DeviceTokenNotForTopic"):
                return "invalid"
            log.warning("APNs live activity %s: %s", r.status_code, reason)
            return "error"
    return "error"


async def _apns_cfg(s: dict) -> dict | None:
    o = await command.owner_all()
    pem = (o.get("apns_private_key") or os.environ.get("APNS_PRIVATE_KEY", "")).replace("\\n", "\n")
    kid, tid = s.get("apns_key_id", ""), s.get("apns_team_id", "")
    if not (pem and kid and tid):
        return None  # APNs not configured: skip silently
    return {"pem": pem, "key_id": kid, "team_id": tid, "bundle": s.get("ios_bundle_id") or "com.phoenixsystems.wasaly"}


async def push_order(order_id: int, eta_min: int | None = None, eta_only: bool = False) -> None:
    try:
        async with SessionLocal() as db:
            s = await settings_service.get_all(db)
            if s.get("live_order_enabled", "true") != "true":
                return
            order = await db.get(Order, order_id)
            if not order:
                return
            status = order.order_status
            if status not in ACTIVE and status not in END_NOW:
                return
            users = [(order.customer_id, "customer")] + ([(order.driver_id, "driver")] if order.driver_id else [])
            strings = await _strings()
            apns = await _apns_cfg(s)
            now = int(time.time())
            for uid, role in users:
                u = await db.get(User, uid)
                lang = (u.language if u else None) or "ar"
                if status in END_NOW:
                    p = {"order_id": str(order.id), "step": 0, "progress": 0, "eta_min": None, "headline": "", "clock": "", "text": "", "end": False,
                         "end_now": True, "rtl": lang != "en", "channel": _t(strings, "v45.live.channel", lang)}
                else:
                    p = build_payload(order, status, eta_min, role, lang, strings)
                data = {"type": "live_order", **{k: ("true" if v is True else "false" if v is False else "" if v is None else str(v)) for k, v in p.items()}}
                toks = (await db.execute(select(DeviceToken.token).where(DeviceToken.user_id == uid, DeviceToken.platform == "android"))).scalars().all()
                if toks and _firebase():
                    for t in toks:
                        if await asyncio.to_thread(_fcm_sync, t, data) == "invalid":
                            await db.execute(delete(DeviceToken).where(DeviceToken.token == t))
                if apns:
                    ended = bool(p["end"] or p["end_now"])
                    aps = {"timestamp": now, "event": "end" if ended else "update",
                           "content-state": {k: p[k] for k in ("headline", "clock", "text", "step", "progress", "rtl")}}
                    if ended:
                        aps["dismissal-date"] = now + (600 if p["end"] else 0)
                    live = (await db.execute(select(LiveActivityToken).where(LiveActivityToken.order_id == order_id, LiveActivityToken.user_id == uid))).scalars().all()
                    for lt in live:
                        if await _apns_send(apns, lt.token, {"aps": aps}, 5 if eta_only else 10) == "invalid":
                            await db.delete(lt)
            if status in S.FINAL or status in END_NOW:
                await db.execute(delete(LiveActivityToken).where(LiveActivityToken.order_id == order_id))
                _last.pop(order_id, None)
            await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("live_order push failed for order %s", order_id)


def _spawn(coro) -> None:
    try:
        t = asyncio.get_running_loop().create_task(coro)
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    except RuntimeError:
        pass


def on_eta(order_id: int, eta_min: int | None) -> None:
    """Called when a fresh driver ETA is known. Throttled: only when eta_min changed and >= 60 s since the last push."""
    if eta_min is None:
        return
    at, last = _last.get(order_id, (0.0, None))
    if last == eta_min or time.time() - at < 60:
        return
    _last[order_id] = (time.time(), eta_min)
    _spawn(push_order(order_id, eta_min, eta_only=True))


def register_handlers() -> None:
    @events.on(events.OrderStatusChanged)
    async def _live_status(ev: events.OrderStatusChanged) -> None:
        if ev.new_status != ev.old_status:
            _last[ev.order_id] = (time.time(), None)
            _spawn(push_order(ev.order_id))


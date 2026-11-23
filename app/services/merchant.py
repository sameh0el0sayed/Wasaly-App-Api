"""Merchant delivery API support: status mapping, signed webhook outbox + sender worker, merchant lookup."""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import uuid
from datetime import UTC, timedelta
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select

from ..constants import OrderStatus as S
from ..database import SessionLocal
from ..models import Merchant, MerchantDelivery, MerchantWebhookEvent, Order, User, Vehicle, utcnow
from ..security import decrypt
from . import events, wakeup

log = logging.getLogger("merchant")
BACKOFF = [60, 300, 1800, 7200]  # seconds: retry after 1m, 5m, 30m, 2h, then FAILED


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def coarse_status(order_status: str) -> str:
    if order_status in (S.ACCEPTED, S.DRIVER_ON_THE_WAY, S.ARRIVED_AT_PICKUP):
        return "assigned"
    if order_status in (S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION):
        return "picked_up"
    if order_status == S.COMPLETED:
        return "delivered"
    if order_status == S.CANCELLED:
        return "cancelled"
    if order_status == S.EXPIRED:
        return "failed"
    return "searching"  # SCHEDULED / PENDING / OFFERED / NO_DRIVER_AVAILABLE


def final_status(order: Order) -> str:
    """Cancelled by the platform itself (no driver found / timeout) is reported as failed, not cancelled."""
    st = coarse_status(order.order_status)
    if st == "cancelled" and order.cancelled_by_role == "SYSTEM":
        return "failed"
    return st


def safe_webhook_url(url: str | None) -> bool:
    """http(s) only, never localhost / private networks (SSRF guard)."""
    if not url:
        return False
    try:
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if u.scheme not in ("http", "https") or not host or host in ("localhost",) or host.endswith((".local", ".internal")):
            return False
        try:
            ip = ipaddress.ip_address(host)
            return not (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved)
        except ValueError:
            return True
    except Exception:  # noqa: BLE001
        return False


async def is_merchant_user(db, user_id: int) -> bool:
    return (await db.execute(select(Merchant.id).where(Merchant.user_id == user_id))).first() is not None


async def build_delivery(db, md: MerchantDelivery, order: Order | None = None) -> dict:
    """Delivery shape shared by GET /deliveries/{id} and the webhook payload."""
    order = order or await db.get(Order, md.order_id)
    status = final_status(order)
    driver = None
    if order.driver_id and status in ("assigned", "picked_up", "delivered"):
        u = await db.get(User, order.driver_id)
        veh = (await db.execute(select(Vehicle).where(Vehicle.driver_id == order.driver_id))).scalar_one_or_none()
        driver = {"name": u.full_name if u else None, "phone": u.phone if u else None,
                  "vehicle": f"{veh.make} {veh.model} {veh.plate_number}".strip() if veh else None}
    dec = order.currency_decimals
    price = order.customer_total if order.customer_total is not None else None
    return {"id": md.id, "external_ref": md.external_ref, "status": status, "driver": driver,
            "price": f"{price:.{dec}f}" if price is not None else (md.quoted_price or "0.000"), "currency": order.currency,
            "tracking_url": None, "eta_minutes": order.est_duration_min}


EVENT_OF = {S.ACCEPTED: "driver_assigned", S.DRIVER_ON_THE_WAY: "driver_on_the_way", S.ARRIVED_AT_PICKUP: "arrived_at_pickup", S.IN_PROGRESS: "picked_up",
            S.ARRIVED_AT_DESTINATION: "arrived_at_destination", S.COMPLETED: "delivered", S.CANCELLED: "cancelled", S.EXPIRED: "failed"}


@events.on(events.OrderStatusChanged)
async def _on_order_status(ev: events.OrderStatusChanged) -> None:
    """Queue a webhook when the merchant-facing status really changed. Own session: runs inside another request's transaction."""
    async with SessionLocal() as db:
        md = (await db.execute(select(MerchantDelivery).where(MerchantDelivery.order_id == ev.order_id))).scalar_one_or_none()
        if not md:
            return
        new = coarse_status(ev.new_status)
        if not md.webhook_url or ev.new_status not in EVENT_OF and new == md.last_status:
            return
        md.last_status = new
        db.add(MerchantWebhookEvent(id=str(uuid.uuid4()), delivery_id=md.id, raw_status=ev.new_status))
        await db.commit()
    wakeup.webhooks.soon()


async def queue_quote_event(db, q) -> None:
    """A manual quote of a partner got its price: tell the partner (only when it registered a webhook_url with the quote)."""
    r = q.request or {}
    if r.get("webhook_url") and r.get("merchant_id"):
        db.add(MerchantWebhookEvent(id=str(uuid.uuid4()), merchant_id=r["merchant_id"], quote_id=q.id, webhook_url=r["webhook_url"], raw_status="QUOTE_PRICED"))
        wakeup.webhooks.soon(1, 5)  # caller commits later


async def _send(db, ev: MerchantWebhookEvent) -> None:
    if ev.quote_id is not None:
        from ..models import ServiceQuote
        q = await db.get(ServiceQuote, ev.quote_id)
        merchant = await db.get(Merchant, ev.merchant_id)
        url = ev.webhook_url
        body = {"event_id": ev.id, "event": "quote_priced", "quote_id": q.id, "service_code": q.service_code, "status": q.status,
                "price": f"{q.price:.3f}" if q.price is not None else None, "currency": q.currency,
                "expires_at": q.expires_at.isoformat() if q.expires_at else None, "occurred_at": ev.occurred_at.isoformat()}
    else:
        md = await db.get(MerchantDelivery, ev.delivery_id)
        merchant = await db.get(Merchant, md.merchant_id)
        order = await db.get(Order, md.order_id)
        payload = await build_delivery(db, md, order)
        url = md.webhook_url
        body = {"event_id": ev.id, "event": EVENT_OF.get(ev.raw_status), "delivery_id": md.id, "external_ref": md.external_ref, "status": payload["status"],
                "driver": payload["driver"], "price": payload["price"], "tracking_url": payload["tracking_url"],
                "eta_minutes": payload["eta_minutes"], "occurred_at": ev.occurred_at.isoformat()}
    raw = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    sig = hmac.new(decrypt(merchant.signing_secret_encrypted).encode(), raw, hashlib.sha256).hexdigest()
    ev.attempts += 1
    try:
        if not safe_webhook_url(url):
            raise ValueError("unsafe webhook url")
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as c:
            r = await c.post(url, content=raw, headers={"Content-Type": "application/json", "X-Wasaly-Signature": sig})
        if 200 <= r.status_code < 300:
            ev.state, ev.last_error = "SENT", None
            return
        raise RuntimeError(f"HTTP {r.status_code}")
    except Exception as e:  # noqa: BLE001
        ev.last_error = str(e)[:200]
        if ev.attempts > len(BACKOFF):
            ev.state = "FAILED"
        else:
            ev.next_attempt_at = utcnow() + timedelta(seconds=BACKOFF[ev.attempts - 1])


async def webhook_loop() -> None:
    while True:
        from . import modes as _modes
        if await _modes.emergency():  # emergency: background jobs are paused
            await asyncio.sleep(10)
            continue
        try:
            async with SessionLocal() as db:
                rows = (await db.execute(select(MerchantWebhookEvent).where(
                    MerchantWebhookEvent.state == "PENDING", MerchantWebhookEvent.next_attempt_at <= utcnow())
                    .order_by(MerchantWebhookEvent.occurred_at).limit(20).with_for_update(skip_locked=True))).scalars().all()
                for ev in rows:
                    await _send(db, ev)
                await db.commit()
                nxt = 0.0 if len(rows) == 20 else (await db.execute(select(func.min(MerchantWebhookEvent.next_attempt_at)).where(
                    MerchantWebhookEvent.state == "PENDING"))).scalar()
            if nxt:  # full batch (0.0) -> next batch right away; else sleep until the next retry is due (max 30 s, woken on new events)
                nxt = nxt.replace(tzinfo=nxt.tzinfo or UTC).timestamp()
            await wakeup.webhooks.sleep(nxt)
        except Exception:  # noqa: BLE001
            log.exception("merchant webhook loop error")
            await asyncio.sleep(30)

"""Real-time push to connected apps over Server-Sent Events.

Changes are detected where they happen (SQLAlchemy flush) but only published AFTER the transaction commits, so a client that
refetches on an event always sees the new state; a rollback publishes nothing. In-process hub: with more than one server
instance put Redis pub/sub behind `Hub.publish` (nothing else changes).
"""
import asyncio
import json
import logging
from dataclasses import dataclass, field

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session

from ..models import CounterOffer, Order, Subscription, utcnow

log = logging.getLogger("realtime")
QUEUE_SIZE = 100


@dataclass(eq=False)
class Connection:
    user_id: int
    is_admin: bool
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_SIZE))


class Hub:
    def __init__(self) -> None:
        self.connections: set[Connection] = set()

    def connect(self, user_id: int, is_admin: bool) -> Connection:
        c = Connection(user_id, is_admin)
        self.connections.add(c)
        return c

    def disconnect(self, c: Connection) -> None:
        self.connections.discard(c)

    def publish(self, payload: dict, audience: set[int]) -> None:
        """Admins receive everything; other users only events that name them. Never blocks, never raises."""
        for c in list(self.connections):
            if not (c.is_admin or c.user_id in audience):
                continue
            if c.queue.full():  # slow client: drop the oldest event (the app refetches on reconnect anyway)
                c.queue.get_nowait()
            c.queue.put_nowait(payload)


hub = Hub()


def sse_format(payload: dict) -> str:
    return f"event: {payload['type']}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


# ---------- change detection (flush) + publish (commit) ----------
def _queue(session: Session, payload: dict, audience: set[int | None]) -> None:
    session.info.setdefault("rt_events", []).append((payload, {a for a in audience if a}))


@event.listens_for(Session, "after_flush")
def _collect(session: Session, _ctx) -> None:
    try:
        now = utcnow().isoformat()
        for o in session.new:
            if isinstance(o, Order):
                _queue(session, {"type": "order.created", "order_id": o.id, "status": o.order_status, "customer_id": o.customer_id, "at": now},
                       {o.customer_id})
            elif isinstance(o, CounterOffer):
                _queue(session, {"type": "order.counter_offer", "order_id": o.order_id, "counter_id": o.id, "customer_id": o.customer_id, "at": now},
                       {o.customer_id})
            elif isinstance(o, Subscription):
                _queue(session, {"type": "subscription.created", "subscription_id": o.id, "status": o.status, "customer_id": o.customer_id, "at": now},
                       {o.customer_id, o.driver_id})
        for o in session.dirty:
            if isinstance(o, Order):
                h = inspect(o).attrs.order_status.history
                if h.has_changes() and h.deleted:
                    _queue(session, {"type": "order.status", "order_id": o.id, "old_status": h.deleted[0], "new_status": o.order_status,
                                     "customer_id": o.customer_id, "driver_id": o.driver_id, "at": now}, {o.customer_id, o.driver_id})
                elif inspect(o).attrs.price_increase_percent.history.has_changes():  # price raised while still searching
                    _queue(session, {"type": "order.status", "order_id": o.id, "old_status": o.order_status, "new_status": o.order_status,
                                     "customer_id": o.customer_id, "driver_id": o.driver_id, "at": now}, {o.customer_id, o.driver_id})
            elif isinstance(o, Subscription):
                h = inspect(o).attrs.status.history
                if h.has_changes() and h.deleted:
                    _queue(session, {"type": "subscription.status", "subscription_id": o.id, "old_status": h.deleted[0], "new_status": o.status,
                                     "customer_id": o.customer_id, "driver_id": o.driver_id, "at": now}, {o.customer_id, o.driver_id})
    except Exception:  # noqa: BLE001 - realtime must never break a write
        log.exception("could not collect realtime events")


@event.listens_for(Session, "after_commit")
def _publish(session: Session) -> None:
    for payload, audience in session.info.pop("rt_events", []):
        hub.publish(payload, audience)


@event.listens_for(Session, "after_rollback")
def _drop(session: Session) -> None:
    session.info.pop("rt_events", None)

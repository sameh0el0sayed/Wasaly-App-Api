"""Tiny in-process event bus (Observer pattern).

Services publish facts ("an order changed state"); independent handlers react. Today handlers run in the same
process and transaction scope; later `publish` can hand the event to Redis/RabbitMQ (outbox) without touching callers.
"""
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

log = logging.getLogger("events")
Handler = Callable[..., Awaitable[None]]
_handlers: dict[type, list[Handler]] = defaultdict(list)


@dataclass(frozen=True)
class OrderStatusChanged:
    order_id: int
    old_status: str
    new_status: str
    actor_id: int | None
    actor_role: str


def on(event_type: type):
    def register(fn: Handler) -> Handler:
        _handlers[event_type].append(fn)
        return fn
    return register


async def publish(event) -> None:
    """A failing handler is logged and never breaks the request that published the event."""
    for handler in list(_handlers[type(event)]):
        try:
            await handler(event)
        except Exception:  # noqa: BLE001
            log.exception("event handler %s failed for %s", handler.__name__, event)


def clear() -> None:  # for tests
    _handlers.clear()

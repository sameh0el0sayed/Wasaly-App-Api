from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import CounterStatus, OfferStatus
from ..constants import OrderStatus as S
from ..models import CounterOffer, Order, OrderOffer, OrderStatusHistory, utcnow
from . import events

ALLOWED: dict[str, set[str]] = {
    S.PENDING: {S.OFFERED, S.NO_DRIVER_AVAILABLE, S.CANCELLED, S.EXPIRED},
    S.OFFERED: {S.ACCEPTED, S.NO_DRIVER_AVAILABLE, S.CANCELLED, S.EXPIRED},
    S.NO_DRIVER_AVAILABLE: {S.OFFERED, S.CANCELLED, S.EXPIRED},
    S.ACCEPTED: {S.DRIVER_ON_THE_WAY, S.CANCELLED},
    S.DRIVER_ON_THE_WAY: {S.ARRIVED_AT_PICKUP, S.CANCELLED},
    S.ARRIVED_AT_PICKUP: {S.IN_PROGRESS, S.CANCELLED},
    S.IN_PROGRESS: {S.ARRIVED_AT_DESTINATION, S.CANCELLED},
    S.ARRIVED_AT_DESTINATION: {S.COMPLETED},
    S.COMPLETED: set(),
    S.CANCELLED: set(),
    S.EXPIRED: set(),
}


def can_transition(old: str, new: str) -> bool:
    return new in ALLOWED.get(old, set())


async def transition(db: AsyncSession, order: Order, new: str, actor_id: int | None, actor_role: str = "SYSTEM",
                     notes: str | None = None, exceptional: bool = False) -> None:
    """The ONLY place that changes order_status. Every change is written to order_status_history.
    `exceptional=True` (admin override) skips sequence validation but still logs."""
    old = order.order_status
    if not exceptional and not can_transition(old, new):
        raise HTTPException(409, {"code": "INVALID_TRANSITION", "from": old, "to": new})
    order.order_status = new
    db.add(OrderStatusHistory(order_id=order.id, old_status=old, new_status=new, changed_by=actor_id,
                              changed_by_role=actor_role, changed_at=utcnow(), notes=notes))
    await db.flush()
    await events.publish(events.OrderStatusChanged(order.id, old, new, actor_id, actor_role))


async def load_order(db: AsyncSession, order_id: int, for_update: bool = False) -> Order | None:
    stmt = select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def close_pending_offers(db: AsyncSession, order_id: int, except_offer_id: int | None = None,
                               status: str = OfferStatus.CLOSED) -> None:
    stmt = (update(OrderOffer).where(OrderOffer.order_id == order_id,
                                     OrderOffer.offer_status.in_([OfferStatus.PENDING, OfferStatus.COUNTERED]))
            .values(offer_status=status, responded_at=utcnow()))
    if except_offer_id:
        stmt = stmt.where(OrderOffer.id != except_offer_id)
    await db.execute(stmt)
    await close_pending_counters(db, order_id, CounterStatus.REJECTED, except_offer_id=except_offer_id)


COUNTER_TEXT = {
    CounterStatus.REJECTED: ("COUNTER_REJECTED", "تم رفض طلب السعر", "لم يوافق العميل على السعر الأعلى."),
    CounterStatus.EXPIRED: ("COUNTER_EXPIRED", "انتهت مهلة طلب السعر", "لم يرد العميل على طلب السعر في الوقت المحدد."),
}


async def close_pending_counters(db: AsyncSession, order_id: int, status: str, except_offer_id: int | None = None,
                                 only_ids: list[int] | None = None) -> None:
    """PENDING counter-offers of an order -> REJECTED / EXPIRED; their drivers are told. Their offers are closed too."""
    from .notifications import notify
    q = select(CounterOffer).where(CounterOffer.order_id == order_id, CounterOffer.status == CounterStatus.PENDING)
    if except_offer_id:
        q = q.where(CounterOffer.offer_id != except_offer_id)
    if only_ids is not None:
        q = q.where(CounterOffer.id.in_(only_ids))
    ntype, title, msg = COUNTER_TEXT[status]
    now = utcnow()
    for c in (await db.execute(q.with_for_update())).scalars().all():
        c.status, c.responded_at = status, now
        offer = await db.get(OrderOffer, c.offer_id)
        if offer and offer.offer_status == OfferStatus.COUNTERED:
            offer.offer_status = OfferStatus.EXPIRED if status == CounterStatus.EXPIRED else OfferStatus.CLOSED
            offer.responded_at = now
        await notify(db, c.driver_id, ntype, title, msg, order_id)


def aware(dt: datetime | None) -> datetime | None:
    return dt

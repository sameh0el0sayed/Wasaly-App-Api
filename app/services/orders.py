from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OfferStatus, OrderStatus as S
from ..models import Order, OrderOffer, OrderStatusHistory, utcnow

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


async def load_order(db: AsyncSession, order_id: int, for_update: bool = False) -> Order | None:
    stmt = select(Order).where(Order.id == order_id).execution_options(populate_existing=True)
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def close_pending_offers(db: AsyncSession, order_id: int, except_offer_id: int | None = None,
                               status: str = OfferStatus.CLOSED) -> None:
    stmt = (update(OrderOffer).where(OrderOffer.order_id == order_id,
                                     OrderOffer.offer_status == OfferStatus.PENDING)
            .values(offer_status=status, responded_at=utcnow()))
    if except_offer_id:
        stmt = stmt.where(OrderOffer.id != except_offer_id)
    await db.execute(stmt)


def aware(dt: datetime | None) -> datetime | None:
    return dt

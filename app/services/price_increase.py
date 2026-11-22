"""Price increase, inDrive style: the customer raises the price while searching, or a driver asks for a higher price on an
offer (counter-offer) and the customer accepts or rejects. An increase is a PERCENT over the ORIGINAL estimate, stored on the
order and applied to the estimate and to the final fare (see pricing.increase_amount)."""
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import CounterStatus, OfferStatus
from ..constants import OrderStatus as S
from ..models import CounterOffer, Order, OrderOffer, User, utcnow
from ..serializers import _pickup_km, driver_card, dt, money
from . import wakeup
from .estimate import order_estimate
from .notifications import notify


def config(s: dict) -> dict:
    steps = [int(x) for x in s["price_increase_steps"].split(",") if x.strip().isdigit()]
    return {"enabled": s["price_increase_enabled"] == "true", "max_percent": int(s["max_price_increase_percent"]),
            "steps": steps, "counter_ttl_seconds": int(s["counter_offer_ttl_seconds"])}


def check_percent(cfg: dict, percent: int, current: int = 0) -> None:
    if not cfg["enabled"]:
        raise HTTPException(422, {"code": "PRICE_INCREASE_DISABLED"})
    if percent > cfg["max_percent"]:
        raise HTTPException(422, {"code": "RAISE_TOO_HIGH", "max_percent": cfg["max_percent"]})
    if percent <= (current or 0):
        raise HTTPException(422, {"code": "RAISE_NOT_HIGHER", "current_percent": current or 0})


def new_estimate(o: Order, percent: int) -> str | None:
    est = order_estimate(o, utcnow(), percent)
    return money(est["customer_total"], o.currency_decimals) if est else None


async def raise_price(db: AsyncSession, order: Order, percent: int, cfg: dict) -> None:
    """Customer raises the price: old offers and counters are dropped, drivers get fresh offers with the new earning."""
    from .dispatch import dispatch_order
    from .orders import close_pending_counters, close_pending_offers
    if order.order_status not in S.SEARCHING:
        raise HTTPException(409, {"code": "ORDER_NOT_SEARCHING", "status": order.order_status})
    check_percent(cfg, percent, order.price_increase_percent)
    order.price_increase_percent = percent
    await close_pending_counters(db, order.id, CounterStatus.EXPIRED)
    await close_pending_offers(db, order.id)
    await db.flush()
    await dispatch_order(db, order)


async def counter_view(db: AsyncSession, c: CounterOffer) -> dict:
    order = await db.get(Order, c.order_id)
    driver = await db.get(User, c.driver_id)
    from ..routers.ratings import stats_for
    st = await stats_for(db, driver)
    trips = (await db.execute(select(func.count()).select_from(Order).where(Order.driver_id == driver.id, Order.order_status == S.COMPLETED))).scalar_one()
    left = max(0.0, (c.expires_at - utcnow()).total_seconds()) if c.status == CounterStatus.PENDING else 0.0
    card = driver_card(driver, {"rating": st["rating_avg"], "ratings_count": st["ratings_count"], "trips": trips,
                                "pickup_distance_km": _pickup_km(driver.driver_profile, order)})
    return {"id": c.id, "order_id": c.order_id, "offer_id": c.offer_id, "kind": c.kind, "status": c.status, "percent": c.percent,
            "price_changed": c.kind == "COUNTER", "new_estimate": new_estimate(order, c.percent), "currency": order.currency,
            "expires_at": dt(c.expires_at), "seconds_left": round(left, 1),
            "driver": {k: v for k, v in card.items() if k != "details"}, "details": card["details"]}


async def create_counter(db: AsyncSession, offer: OrderOffer, driver: User, percent: int, cfg: dict, kind: str = "COUNTER") -> CounterOffer:
    now = utcnow()
    if offer.counter:
        raise HTTPException(409, {"code": "COUNTER_EXISTS"})
    if offer.offer_status != OfferStatus.PENDING:
        raise HTTPException(409, {"code": "OFFER_NOT_PENDING"})
    if offer.expires_at <= now:
        raise HTTPException(409, {"code": "OFFER_EXPIRED"})
    order = offer.order
    if order.order_status not in S.SEARCHING:
        raise HTTPException(409, {"code": "OFFER_NOT_PENDING"})
    if kind == "COUNTER":
        check_percent(cfg, percent, order.price_increase_percent)
    c = CounterOffer(order_id=order.id, offer_id=offer.id, driver_id=driver.id, customer_id=order.customer_id, percent=percent, kind=kind,
                     status=CounterStatus.PENDING, created_at=now, expires_at=now + timedelta(seconds=cfg["counter_ttl_seconds"]))
    offer.offer_status = OfferStatus.COUNTERED
    db.add(c)
    wakeup.dispatch.at(c.expires_at.timestamp() + 0.2)
    await db.flush()
    price = new_estimate(order, percent)
    if kind == "ACCEPT":
        await notify(db, order.customer_id, "DRIVER_AVAILABLE", "سائق جاهز لطلبك", f"{driver.full_name} قبل طلبك بسعر {price} {order.currency}", order.id)
    else:
        await notify(db, order.customer_id, "COUNTER_OFFER", "سائق يطلب سعرًا أعلى", f"{driver.full_name} يطلب {price} {order.currency}", order.id)
    return c

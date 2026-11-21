"""Live traffic on the route of an order (TomTom via app.services.traffic). Customer: while searching and after pickup."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import current_user
from ..models import Order, User
from ..services import cache, traffic

router = APIRouter(prefix="/api/orders", tags=["traffic"])


async def driver_pos(driver_id: int):
    h = await cache.get_matched_location(driver_id) or await cache.get_raw_location(driver_id)
    return (float(h["lat"]), float(h["lng"])) if h and h.get("lat") not in (None, "") else None


@router.get("/{order_id}/traffic")
async def order_traffic(order_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Searching/before pickup: pickup -> dropoff. IN_PROGRESS: driver's current position -> dropoff. Refresh at most every 2 minutes."""
    o = await db.get(Order, order_id)
    if not o or user.id not in (o.customer_id, o.driver_id):
        raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": "Order not found"})
    dest = (o.destination_latitude, o.destination_longitude)
    src = (o.pickup_latitude, o.pickup_longitude)
    if o.order_status in (S.IN_PROGRESS, S.ARRIVED_AT_DESTINATION) and o.driver_id:
        src = await driver_pos(o.driver_id) or src
    return {"order_id": o.id, "refresh_after_s": 120, **await traffic.route_traffic(src, dest)}

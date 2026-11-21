"""Live order card: store the iOS Live Activity push token for the order's customer or assigned driver."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import current_user
from ..models import LiveActivityToken, Order, User

router = APIRouter(prefix="/api/orders", tags=["live-order"])


class LiveActivityIn(BaseModel):
    token: str = Field(min_length=8, max_length=300)
    platform: str = "ios"


@router.post("/{order_id}/live-activity")
async def register_live_activity(order_id: int, body: LiveActivityIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    order = await db.get(Order, order_id)
    if not order or user.id not in (order.customer_id, order.driver_id):
        raise HTTPException(404, {"code": "ORDER_NOT_FOUND", "message": "Order not found"})
    row = (await db.execute(select(LiveActivityToken).where(LiveActivityToken.order_id == order_id, LiveActivityToken.user_id == user.id))).scalar_one_or_none()
    if row:
        row.token = body.token.strip()
    else:
        db.add(LiveActivityToken(order_id=order_id, user_id=user.id, token=body.token.strip()))
    await db.commit()
    return {"ok": True}

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...constants import AccountStatus, Role
from ...constants import OrderStatus as S
from ...database import get_db
from ...deps import admin_user
from ...models import Complaint, DriverProfile, Order, User, utcnow
from ...serializers import money
from ...services import settings_service

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- dashboard ----------------
@router.get("/dashboard")
async def dashboard(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    async def count(q):
        return (await db.execute(q)).scalar_one()
    today_start = datetime.now(ZoneInfo(settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    U, O = User, Order
    return {
        "customers_total": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER)),
        "customers_pending": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER, U.account_status == AccountStatus.PENDING_APPROVAL)),
        "drivers_approved": await count(select(func.count()).select_from(U).where(U.role == Role.DRIVER, U.account_status == AccountStatus.APPROVED)),
        "drivers_pending": await count(select(func.count()).select_from(U).where(U.role == Role.DRIVER, U.account_status == AccountStatus.PENDING_APPROVAL)),
        "users_online_now": await count(select(func.count()).select_from(U).where(U.role.in_([Role.CUSTOMER, Role.DRIVER]), U.last_seen_at >= utcnow() - timedelta(minutes=int((await settings_service.get_all(db))["online_window_minutes"])))),
        "drivers_online": await count(select(func.count()).select_from(DriverProfile).where(DriverProfile.is_online.is_(True))),
        "orders_new": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.SEARCHING))),
        "trips_active": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.DRIVER_BUSY))),
        "trips_completed_today": await count(select(func.count()).select_from(O).where(O.order_status == S.COMPLETED, O.completed_at >= today_start)),
        "orders_cancelled": await count(select(func.count()).select_from(O).where(O.order_status == S.CANCELLED)),
        "gross_trip_value_today": {c: money(t, d) for c, d, t in (await db.execute(
            select(O.currency, O.currency_decimals, func.coalesce(func.sum(O.final_price), 0))
            .where(O.order_status == S.COMPLETED, O.completed_at >= today_start)
            .group_by(O.currency, O.currency_decimals))).all()},
        "company_profit_today": {c: money(t, d) for c, d, t in (await db.execute(
            select(O.currency, O.currency_decimals, func.coalesce(func.sum(O.commission), 0))
            .where(O.order_status == S.COMPLETED, O.completed_at >= today_start)
            .group_by(O.currency, O.currency_decimals))).all()},
        "open_complaints": await count(select(func.count()).select_from(Complaint).where(Complaint.status == "OPEN")),
    }

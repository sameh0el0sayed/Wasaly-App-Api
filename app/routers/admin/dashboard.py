from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...config import settings
from ...constants import AccountStatus, Role
from ...constants import OrderStatus as S
from ...database import get_db
from ...deps import admin_user
from ...models import Complaint, DriverApplication, DriverProfile, Order, ProfileChange, User, utcnow
from ...serializers import money
from ...services import settings_service
from ...services.license import warn_days

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- dashboard ----------------
@router.get("/dashboard")
async def dashboard(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    async def count(q):
        return (await db.execute(q)).scalar_one()
    today_start = datetime.now(ZoneInfo(settings.timezone)).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    U, O = User, Order
    tz = ZoneInfo(settings.timezone)
    day0 = today_start.astimezone(tz).date()
    week_start = (today_start.astimezone(tz) - timedelta(days=6)).astimezone(UTC)
    done = (await db.execute(select(O.completed_at, O.final_price, O.commission, O.currency, O.currency_decimals).where(
        O.order_status == S.COMPLETED, O.completed_at >= week_start))).all()
    rev, cnt, prof = [0.0] * 7, [0] * 7, [0.0] * 7  # last 7 local days, oldest first, today last
    yest: dict = {}
    for at, price, comm, cur, dec in done:
        i = 6 - (day0 - at.astimezone(tz).date()).days
        if 0 <= i <= 6:
            rev[i] += float(price or 0)
            cnt[i] += 1
            prof[i] += float(comm or 0)
        if i == 5:
            y = yest.setdefault(cur, [0.0, 0.0, dec])
            y[0] += float(price or 0)
            y[1] += float(comm or 0)
    return {
        "revenue_yesterday": {c: money(v[0], v[2]) for c, v in yest.items()},
        "company_profit_yesterday": {c: money(v[1], v[2]) for c, v in yest.items()},
        "orders_yesterday": cnt[5],
        "orders_searching": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.SEARCHING))),
        "series": {"revenue": [round(x, 3) for x in rev], "orders": cnt, "profit": [round(x, 3) for x in prof]},
        "customers_total": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER)),
        "customers_pending": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER, U.account_status == AccountStatus.PENDING_APPROVAL)),
        "drivers_approved": await count(select(func.count()).select_from(U).where(U.id.in_(select(DriverProfile.user_id)), U.account_status == AccountStatus.APPROVED)),
        "drivers_pending": await count(select(func.count()).select_from(U).where(U.id.in_(select(DriverProfile.user_id)), U.account_status == AccountStatus.PENDING_APPROVAL)),
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
        "licenses_expired": await count(select(func.count()).select_from(DriverProfile).where(DriverProfile.license_expiry_date < date.today())),
        "licenses_expiring": await count(select(func.count()).select_from(DriverProfile).where(
            DriverProfile.license_expiry_date >= date.today(),
            DriverProfile.license_expiry_date <= date.today() + timedelta(days=max(warn_days((await settings_service.get_all(db))["license_expiry_warn_days"]) or [30])))),
        "driver_applications_pending": await count(select(func.count()).select_from(DriverApplication).where(DriverApplication.status == "PENDING_REVIEW")),
        "pending_corrections": await count(select(func.count()).select_from(ProfileChange).where(ProfileChange.status.in_(("SUBMITTED", "PENDING_REVIEW")))),
        "open_complaints": await count(select(func.count()).select_from(Complaint).where(Complaint.status == "OPEN")),
    }

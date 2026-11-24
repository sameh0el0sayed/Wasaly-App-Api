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
async def dashboard(period: str = "today", admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """period=today|7d|30d. Money/count fields are totals of the period, *_yesterday the previous period of the same length, series = daily points."""
    async def count(q):
        return (await db.execute(q)).scalar_one()
    if period not in ("today", "7d", "30d"):
        period = "today"
    n = {"today": 1, "7d": 7, "30d": 30}[period]
    npts = 7 if period == "today" else n  # series length
    U, O = User, Order
    tz = ZoneInfo(settings.timezone)
    today_local = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    day0 = today_local.date()
    period_start = (today_local - timedelta(days=n - 1)).astimezone(UTC)
    span = max(npts, 2 * n)
    done = (await db.execute(select(O.completed_at, O.final_price, O.commission, O.currency, O.currency_decimals).where(
        O.order_status == S.COMPLETED, O.completed_at >= (today_local - timedelta(days=span - 1)).astimezone(UTC)))).all()
    rev, cnt, prof = [0.0] * npts, [0] * npts, [0.0] * npts  # daily values, oldest first
    prev: dict = {}
    cur_t: dict = {}
    cur_n = prev_n = 0
    for at, price, comm, cur, dec in done:
        age = (day0 - at.astimezone(tz).date()).days  # 0 = today
        i = npts - 1 - age
        if 0 <= i < npts:
            rev[i] += float(price or 0)
            cnt[i] += 1
            prof[i] += float(comm or 0)
        if age < n:
            c = cur_t.setdefault(cur, [0.0, 0.0, dec])
            c[0] += float(price or 0)
            c[1] += float(comm or 0)
            cur_n += 1
        elif age < 2 * n:
            y = prev.setdefault(cur, [0.0, 0.0, dec])
            y[0] += float(price or 0)
            y[1] += float(comm or 0)
            prev_n += 1
    new_orders = await count(select(func.count()).select_from(O).where(O.created_at >= period_start)) if period != "today" else None
    return {
        "period": period,
        "revenue_yesterday": {c: money(v[0], v[2]) for c, v in prev.items()},
        "company_profit_yesterday": {c: money(v[1], v[2]) for c, v in prev.items()},
        "orders_yesterday": prev_n,
        "orders_searching": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.SEARCHING))),
        "series": {"revenue": [round(x, 3) for x in rev], "orders": cnt, "profit": [round(x, 3) for x in prof]},
        "customers_total": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER)),
        "customers_pending": await count(select(func.count()).select_from(U).where(U.role == Role.CUSTOMER, U.account_status == AccountStatus.PENDING_APPROVAL)),
        "drivers_approved": await count(select(func.count()).select_from(U).where(U.id.in_(select(DriverProfile.user_id)), U.account_status == AccountStatus.APPROVED)),
        "drivers_pending": await count(select(func.count()).select_from(U).where(U.id.in_(select(DriverProfile.user_id)), U.account_status == AccountStatus.PENDING_APPROVAL)),
        "users_online_now": await count(select(func.count()).select_from(U).where(U.role.in_([Role.CUSTOMER, Role.DRIVER]), U.last_seen_at >= utcnow() - timedelta(minutes=int((await settings_service.get_all(db))["online_window_minutes"])))),
        "drivers_online": await count(select(func.count()).select_from(DriverProfile).where(DriverProfile.is_online.is_(True))),
        "orders_new": new_orders if new_orders is not None else await count(select(func.count()).select_from(O).where(O.order_status.in_(S.SEARCHING))),
        "trips_active": await count(select(func.count()).select_from(O).where(O.order_status.in_(S.DRIVER_BUSY))),
        "trips_completed_today": cur_n,
        "orders_cancelled": await count(select(func.count()).select_from(O).where(O.order_status == S.CANCELLED)),
        "gross_trip_value_today": {c: money(v[0], v[2]) for c, v in cur_t.items()},
        "company_profit_today": {c: money(v[1], v[2]) for c, v in cur_t.items()},
        "licenses_expired": await count(select(func.count()).select_from(DriverProfile).where(DriverProfile.license_expiry_date < date.today())),
        "licenses_expiring": await count(select(func.count()).select_from(DriverProfile).where(
            DriverProfile.license_expiry_date >= date.today(),
            DriverProfile.license_expiry_date <= date.today() + timedelta(days=max(warn_days((await settings_service.get_all(db))["license_expiry_warn_days"]) or [30])))),
        "driver_applications_pending": await count(select(func.count()).select_from(DriverApplication).where(DriverApplication.status == "PENDING_REVIEW")),
        "pending_corrections": await count(select(func.count()).select_from(ProfileChange).where(ProfileChange.status.in_(("SUBMITTED", "PENDING_REVIEW")))),
        "open_complaints": await count(select(func.count()).select_from(Complaint).where(Complaint.status == "OPEN")),
    }

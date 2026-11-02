"""Favourite drivers = drivers the customer's latest rating for was 4 or 5 stars (a later low rating removes the driver)."""
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..models import DriverProfile, Order, Rating, User


async def favorite_rows(db: AsyncSession, customer_id: int) -> list[tuple[User, int, int]]:
    """[(driver, stars_of_latest_rating, completed_trips_together)], newest rating first."""
    rows = (await db.execute(select(Rating).where(Rating.rater_id == customer_id, Rating.rater_role == Role.CUSTOMER)
                             .order_by(Rating.created_at.desc(), Rating.id.desc()))).scalars().all()
    latest: dict[int, int] = {}
    for r in rows:
        latest.setdefault(r.ratee_id, r.stars)
    out = []
    for driver_id, stars in latest.items():
        if stars < 4:
            continue
        d = await db.get(User, driver_id)
        if not d or d.role != Role.DRIVER or d.account_status != AccountStatus.APPROVED or d.deleted_at or not d.vehicle:
            continue
        trips = (await db.execute(select(func.count()).select_from(Order).where(
            Order.customer_id == customer_id, Order.driver_id == driver_id, Order.order_status == S.COMPLETED))).scalar_one()
        out.append((d, stars, trips))
    return out


async def is_favorite(db: AsyncSession, customer_id: int, driver_id: int) -> bool:
    return any(d.id == driver_id for d, _, _ in await favorite_rows(db, customer_id))


def online(d: User) -> bool:
    p: DriverProfile | None = d.driver_profile
    return bool(p and p.is_online)

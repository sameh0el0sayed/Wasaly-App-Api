"""Favourite drivers = drivers the customer's latest rating for was 4 or 5 stars (a later low rating removes the driver)."""
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
    driver_ids = [did for did, stars in latest.items() if stars >= 4]
    if not driver_ids:
        return []
    # Batch load all favorite drivers with relationships eager-loaded (avoids N+1)
    drivers = (await db.execute(
        select(User).where(User.id.in_(driver_ids))
        .options(
            selectinload(User.driver_profile),
            selectinload(User.vehicle),
        )
    )).scalars().all()
    driver_map = {d.id: d for d in drivers}
    # Batch count completed trips per driver for this customer (avoids N+1)
    trip_counts = dict((await db.execute(
        select(Order.driver_id, func.count())
        .where(Order.customer_id == customer_id, Order.driver_id.in_(driver_ids),
               Order.order_status == S.COMPLETED)
        .group_by(Order.driver_id)
    )).all())
    today = date.today()
    out = []
    for driver_id, stars in latest.items():
        if stars < 4:
            continue
        d = driver_map.get(driver_id)
        if not d or not d.driver_profile or d.account_status != AccountStatus.APPROVED or d.deleted_at or not d.vehicle \
                or (d.driver_profile and d.driver_profile.license_expiry_date < today):
            continue
        trips = trip_counts.get(driver_id, 0)
        out.append((d, stars, trips))
    return out


async def is_favorite(db: AsyncSession, customer_id: int, driver_id: int) -> bool:
    return any(d.id == driver_id for d, _, _ in await favorite_rows(db, customer_id))


def online(d: User) -> bool:
    p: DriverProfile | None = d.driver_profile
    return bool(p and p.is_online)

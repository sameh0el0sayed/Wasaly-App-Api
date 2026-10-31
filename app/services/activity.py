"""User activity: who is online, which devices, how often the app is used, consented location snapshots, retention."""
import time
from datetime import timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AppOpenEvent, DriverProfile, LocationSnapshot, LoginEvent, User, utcnow
from . import settings_service
from .dispatch import force_offline

_last_minute = 0.0
_last_hour = 0.0


def is_online(last_seen_at, window_minutes: int, now=None) -> bool:
    """Online = ANY authenticated request within the window (default 15 minutes). No heartbeat."""
    if not last_seen_at:
        return False
    return (now or utcnow()) - last_seen_at <= timedelta(minutes=window_minutes)


async def sweep(db: AsyncSession) -> None:
    """Background: auto-offline idle drivers (setting) every minute, delete old history every hour."""
    global _last_minute, _last_hour
    t = time.time()
    if t - _last_minute < 60:
        return
    _last_minute = t
    from ..routers.ratings import sweep_auto_suspend
    await sweep_auto_suspend(db)
    s = await settings_service.get_all(db)
    idle = int(s["driver_auto_offline_minutes"])
    if idle > 0:
        cutoff = utcnow() - timedelta(minutes=idle)
        ids = (await db.execute(select(DriverProfile.user_id).join(User, User.id == DriverProfile.user_id).where(
            DriverProfile.is_online.is_(True), (User.last_seen_at.is_(None)) | (User.last_seen_at < cutoff)))).scalars().all()
        for uid in ids:
            await force_offline(db, uid)
    if t - _last_hour >= 3600:
        _last_hour = t
        old = utcnow() - timedelta(days=int(s["activity_retention_days"]))
        await db.execute(delete(LocationSnapshot).where(LocationSnapshot.created_at < old))
        await db.execute(delete(LoginEvent).where(LoginEvent.created_at < old))
        await db.execute(delete(AppOpenEvent).where(AppOpenEvent.created_at < old))

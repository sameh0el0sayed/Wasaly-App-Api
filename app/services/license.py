"""Driver licence status + daily reminder job (licence validity is the government's matter: we warn and stop offers, never suspend)."""
from datetime import date
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..constants import AccountStatus
from ..models import AppSetting, DriverProfile, User, utcnow
from . import settings_service
from .notifications import notify
from .payments import notify_admins


def warn_days(raw: str) -> list[int]:
    return sorted({int(x) for x in (raw or "").replace(" ", "").split(",") if x.isdigit()}, reverse=True)


def doc_status(expiry: date | None, today: date | None = None, warn_max: int = 30) -> str:
    if not expiry:
        return "UNKNOWN"
    left = (expiry - (today or date.today())).days
    return "EXPIRED" if left < 0 else ("EXPIRING" if left <= warn_max else "VALID")


def verification(u: User, warn_max: int = 30) -> dict:
    """Is the person verified? Approved account + ID card (CPR) not expired (+ driving licence not expired for drivers)."""
    p = u.driver_profile
    idc = {"issue_date": u.cpr_issue_date.isoformat() if u.cpr_issue_date else None,
           "expiry_date": u.cpr_expiry_date.isoformat() if u.cpr_expiry_date else None, "status": doc_status(u.cpr_expiry_date, warn_max=warn_max)}
    out = {"identity": idc}
    ok = u.account_status == AccountStatus.APPROVED and idc["status"] != "EXPIRED"
    if p:
        lic = {"issue_date": p.license_issue_date.isoformat() if p.license_issue_date else None,
               "expiry_date": p.license_expiry_date.isoformat(), "status": doc_status(p.license_expiry_date, warn_max=warn_max)}
        out["license"] = lic
        ok = ok and lic["status"] != "EXPIRED"
    out["verified"] = ok
    return out


def info(p: DriverProfile, pending: bool = False, today: date | None = None, warn_max: int = 30) -> dict:
    today = today or date.today()
    left = (p.license_expiry_date - today).days
    status = "EXPIRED" if left < 0 else ("EXPIRING" if left <= warn_max else "VALID")
    if pending:
        status = "RENEWAL_PENDING"
    return {"issue_date": p.license_issue_date.isoformat() if p.license_issue_date else None,
            "expiry_date": p.license_expiry_date.isoformat(), "status": status, "days_left": left}


async def daily_job(db: AsyncSession, force: bool = False) -> None:
    """Runs once a day after 09:00 Asia/Bahrain."""
    now = utcnow().astimezone(ZoneInfo(settings.timezone))
    key = now.date().isoformat()
    s = await settings_service.get_all(db)
    if not force and (now.hour < 9 or s["license_job_last_run"] == key):
        return
    days = warn_days(s["license_expiry_warn_days"])
    today = now.date()
    drivers = (await db.execute(select(User).join(DriverProfile, DriverProfile.user_id == User.id)
                                .where(User.account_status == AccountStatus.APPROVED))).scalars().unique().all()
    for u in drivers:
        p = u.driver_profile
        left = (p.license_expiry_date - today).days
        if left in days:
            await notify(db, u.id, "LICENSE_EXPIRING", "رخصتك تنتهي قريبًا",
                         f"تنتهي رخصة القيادة بعد {left} يوم. حدّثها لتستمر في استقبال الطلبات.",
                         data={"days_left": left, "expiry_date": p.license_expiry_date.isoformat()})
        elif left == 0:
            await notify(db, u.id, "LICENSE_EXPIRED", "انتهت رخصتك",
                         "انتهت رخصة القيادة، وتوقف إرسال الطلبات إليك حتى تحديثها.",
                         data={"expiry_date": p.license_expiry_date.isoformat()})
        if left == 0:  # once, on the expiry day
            await notify_admins(db, "LICENSE_EXPIRED_ADMIN", "رخصة سائق منتهية", f"انتهت رخصة السائق {u.full_name}.", {"driver_id": u.id})
    row = await db.get(AppSetting, "license_job_last_run")
    if row:
        row.value = key
    else:
        db.add(AppSetting(key="license_job_last_run", value=key))
    from . import cache
    await cache.delete(settings_service.SETTINGS_CACHE_KEY)

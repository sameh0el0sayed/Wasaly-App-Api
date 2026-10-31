"""Development seed: demo admin, customer and driver. Everything comes from .env (see .env.example); refused in production."""
import json
import logging
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import settings
from .constants import AccountStatus, Role
from .models import AirportZone, DriverProfile, DriverServiceArea, ServiceArea, User, Vehicle, utcnow
from .security import cpr_hash, encrypt, hash_password, normalize_phone

log = logging.getLogger("seed")


def assert_safe_environment() -> None:
    """In production the API refuses to start with dev defaults or demo seed."""
    if settings.app_env != "production":
        return
    problems = []
    if settings.seed_demo_data:
        problems.append("SEED_DEMO_DATA must be false")
    if settings.secret_key == "dev-secret-change-me":
        problems.append("SECRET_KEY is the development default")
    if not settings.fernet_key:
        problems.append("FERNET_KEY is empty")
    if settings.first_admin_password == "ChangeMe!12345":
        problems.append("FIRST_ADMIN_PASSWORD is the development default")
    if problems:
        raise RuntimeError("Unsafe production configuration: " + "; ".join(problems))


async def _exists(db: AsyncSession, phone: str) -> bool:
    return (await db.execute(select(User.id).where(User.phone == phone))).first() is not None


async def seed_demo(db: AsyncSession) -> None:
    if not settings.seed_demo_data:
        return
    now = utcnow()
    if settings.seed_admin_phone and settings.seed_admin_password:
        phone = normalize_phone(settings.seed_admin_phone)
        if not await _exists(db, phone):
            db.add(User(full_name=settings.seed_admin_name, phone=phone, role=Role.ADMIN, password_hash=hash_password(settings.seed_admin_password),
                        account_status=AccountStatus.APPROVED, approved_at=now))
            log.warning("Seeded demo admin %s", phone)
    area = None
    if settings.seed_area_points:
        area = (await db.execute(select(ServiceArea).where(ServiceArea.name == settings.seed_area_name))).scalar_one_or_none()
        if not area:
            area = ServiceArea(name=settings.seed_area_name, boundary={"type": "polygon", "points": json.loads(settings.seed_area_points)})
            db.add(area)
            await db.flush()
    if settings.seed_customer_phone and settings.seed_customer_password:
        phone = normalize_phone(settings.seed_customer_phone)
        if not await _exists(db, phone):
            db.add(User(full_name=settings.seed_customer_name, phone=phone, role=Role.CUSTOMER, cpr_encrypted=encrypt(settings.seed_customer_cpr),
                        cpr_hash=cpr_hash(settings.seed_customer_cpr), cpr_last3=settings.seed_customer_cpr[-3:],
                        password_hash=hash_password(settings.seed_customer_password), account_status=AccountStatus.APPROVED, approved_at=now))
            log.warning("Seeded demo customer %s", phone)
    if settings.seed_driver_phone and settings.seed_driver_password:
        phone = normalize_phone(settings.seed_driver_phone)
        if not await _exists(db, phone):
            u = User(full_name=settings.seed_driver_name, phone=phone, role=Role.DRIVER, cpr_encrypted=encrypt(settings.seed_driver_cpr),
                     cpr_hash=cpr_hash(settings.seed_driver_cpr), cpr_last3=settings.seed_driver_cpr[-3:],
                     password_hash=hash_password(settings.seed_driver_password), account_status=AccountStatus.APPROVED, approved_at=now)
            db.add(u)
            await db.flush()
            prof = DriverProfile(user_id=u.id, license_number_encrypted=encrypt(settings.seed_driver_license),
                                 license_expiry_date=date.fromisoformat(settings.seed_driver_license_expiry), driver_status=AccountStatus.APPROVED,
                                 gender=settings.seed_driver_gender or None)
            if settings.seed_driver_payout_iban:
                prof.payout_method, prof.payout_account_encrypted, prof.payout_holder_name = "IBAN", encrypt(settings.seed_driver_payout_iban), settings.seed_driver_name
            db.add(prof)
            if settings.seed_driver_vehicle:
                db.add(Vehicle(driver_id=u.id, vehicle_status=AccountStatus.APPROVED, **json.loads(settings.seed_driver_vehicle)))
            if area:
                db.add(DriverServiceArea(driver_id=u.id, service_area_id=area.id))
            log.warning("Seeded demo driver %s", phone)
    if settings.seed_airport_name and settings.seed_airport_boundary:
        if not (await db.execute(select(AirportZone.id).where(AirportZone.name == settings.seed_airport_name))).first():
            db.add(AirportZone(name=settings.seed_airport_name, boundary=json.loads(settings.seed_airport_boundary),
                               fee=Decimal(settings.seed_airport_fee)))
    await db.commit()

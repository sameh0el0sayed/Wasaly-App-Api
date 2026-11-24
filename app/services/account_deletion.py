"""Account deletion: soft-delete now (reversible by an admin), anonymize after the grace period (setting)."""
import logging
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus
from ..models import (
    AppOpenEvent,
    DeviceToken,
    LocationSnapshot,
    LoginEvent,
    Notification,
    SavedPlace,
    UploadedFile,
    User,
    UserDevice,
    VehicleDocument,
    utcnow,
)
from . import settings_service
from .files import delete_object

log = logging.getLogger("account_deletion")
KEEP_PREFIXES = ("payment/", "PICKUP_PROOF/", "DELIVERY_PROOF/")  # evidence that stays with the order/payment
DELETED_NAME = "Deleted user"


async def purge_devices(db: AsyncSession, user_id: int) -> None:
    await db.execute(delete(UserDevice).where(UserDevice.user_id == user_id))
    await db.execute(delete(DeviceToken).where(DeviceToken.user_id == user_id))


async def anonymize(db: AsyncSession, u: User) -> None:
    keys = [k for k in (await db.execute(select(UploadedFile.file_key).where(UploadedFile.owner_id == u.id))).scalars()
            if not k.startswith(KEEP_PREFIXES)]
    if u.driver_profile and u.driver_profile.license_document:
        keys.append(u.driver_profile.license_document)
    if u.vehicle:
        keys += [d.file_path for d in (await db.execute(select(VehicleDocument).where(VehicleDocument.vehicle_id == u.vehicle.id))).scalars()]
        keys += list((u.vehicle.photos or {}).values())
    for k in {k for k in keys if k and not k.startswith(KEEP_PREFIXES)}:
        try:
            await delete_object(k)
        except Exception:  # noqa - a storage hiccup must not stop the privacy job; the row is removed anyway
            log.exception("could not delete stored file %s", k)
    await db.execute(delete(UploadedFile).where(UploadedFile.owner_id == u.id, *[~UploadedFile.file_key.like(p + "%") for p in KEEP_PREFIXES]))
    await db.execute(delete(LocationSnapshot).where(LocationSnapshot.user_id == u.id))
    await db.execute(delete(LoginEvent).where(LoginEvent.user_id == u.id))
    await db.execute(delete(AppOpenEvent).where(AppOpenEvent.user_id == u.id))
    await db.execute(delete(SavedPlace).where(SavedPlace.user_id == u.id))
    await db.execute(delete(Notification).where(Notification.user_id == u.id))
    await purge_devices(db, u.id)
    u.full_name, u.profile_photo, u.cpr_photo = DELETED_NAME, None, None
    if u.account_status == AccountStatus.DELETED and u.status_reason != "BANNED_BEFORE_DELETE":
        u.phone, u.cpr_encrypted, u.cpr_hash, u.cpr_last3 = None, None, None, None
    # a banned person keeps phone + CPR hash so deleting the account can never be used to escape the ban
    if u.driver_profile:
        p = u.driver_profile
        p.license_number_encrypted, p.license_document = "", None
        p.payout_account_encrypted = p.payout_holder_name = p.payout_method = None
    if u.vehicle:
        await db.execute(delete(VehicleDocument).where(VehicleDocument.vehicle_id == u.vehicle.id))
        u.vehicle.photos, u.vehicle.plate_number = {}, f"DELETED-{u.id}"
    u.anonymized_at = utcnow()


async def anonymize_due(db: AsyncSession) -> int:
    """Daily job: DELETED accounts older than the grace period lose their personal data. Orders and payments stay."""
    days = int((await settings_service.get_all(db))["account_deletion_grace_days"])
    cutoff = utcnow() - timedelta(days=days)
    users = (await db.execute(select(User).where(User.account_status == AccountStatus.DELETED, User.anonymized_at.is_(None),
                                                 User.deleted_at < cutoff))).scalars().unique().all()
    for u in users:
        await anonymize(db, u)
    if users:
        await db.commit()
    return len(users)

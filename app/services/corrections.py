"""Admin-requested corrections and user-requested sensitive changes (phone). Never locks the account."""
from datetime import date

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import Role
from ..models import ProfileChange, User, Vehicle, utcnow
from ..security import cpr_hash, decrypt, encrypt, normalize_phone, validate_cpr
from .files import assert_owned

OPEN, SUBMITTED, ACCEPTED, PENDING = "OPEN", "SUBMITTED", "ACCEPTED", "PENDING_REVIEW"
CORRECTION, CHANGE = "CORRECTION", "CHANGE"

# field -> (Arabic label, kind of value, drivers only)
FIELDS: dict[str, tuple[str, str, bool]] = {
    "profile_photo": ("الصورة الشخصية", "file", False),
    "cpr_photo": ("صورة البطاقة", "file", False),
    "cpr_number": ("رقم البطاقة", "text", False),
    "phone": ("رقم الهاتف", "text", False),
    "cpr_dates": ("تاريخا إصدار وانتهاء البطاقة", "text", False),
    "license": ("تجديد الرخصة", "text", True),
    "license.photo_front": ("صورة الرخصة (الأمام)", "file", True),
    "license.photo_back": ("صورة الرخصة (الخلف)", "file", True),
    "license.number": ("رقم الرخصة", "text", True),
    "license.expiry_date": ("تاريخ انتهاء الرخصة", "text", True),
    "vehicle.plate_number": ("رقم اللوحة", "text", True),
    "vehicle.photos.main": ("صورة السيارة الرئيسية", "file", True),
    "vehicle.photos.front": ("صورة السيارة (الأمام)", "file", True),
    "vehicle.photos.back": ("صورة السيارة (الخلف)", "file", True),
    "vehicle.photos.side": ("صورة السيارة (الجانب)", "file", True),
    "vehicle.photos.interior": ("صورة السيارة (من الداخل)", "file", True),
}
CORRECTABLE = [f for f in FIELDS if f not in ("phone", "license", "cpr_dates")]


def label(field: str) -> str:
    return FIELDS.get(field, (field, "text", False))[0]


def is_file(field: str) -> bool:
    return FIELDS[field][1] == "file"


def check_field(u: User, field: str) -> None:
    if field not in FIELDS or (FIELDS[field][2] and u.role != Role.DRIVER):
        raise HTTPException(422, {"code": "INVALID_FIELD", "field": field})


def current_value(u: User, field: str) -> str | None:
    """File key, or the (masked) text of the present value."""
    p, v = u.driver_profile, u.vehicle
    if field == "profile_photo":
        return u.profile_photo
    if field == "cpr_photo":
        return u.cpr_photo
    if field == "cpr_number":
        return "******" + (u.cpr_last3 or "")
    if field == "phone":
        return u.phone
    if p and field == "license":
        return p.license_expiry_date.isoformat()
    if p and field == "license.photo_front":
        return p.license_document
    if p and field == "license.photo_back":
        return p.license_document_back
    if p and field == "license.number":
        return "****" + decrypt(p.license_number_encrypted)[-3:]
    if p and field == "license.expiry_date":
        return p.license_expiry_date.isoformat()
    if v and field == "vehicle.plate_number":
        return v.plate_number
    if v and field.startswith("vehicle.photos."):
        return (v.photos or {}).get(field.rsplit(".", 1)[1])
    return None


async def refresh_flag(db: AsyncSession, user_id: int) -> None:
    """Driver availability: any OPEN correction blocks going online (CORRECTIONS_REQUIRED)."""
    u = await db.get(User, user_id)
    if u and u.driver_profile:
        n = (await db.execute(select(func.count()).select_from(ProfileChange).where(
            ProfileChange.user_id == user_id, ProfileChange.kind == CORRECTION, ProfileChange.status == OPEN))).scalar_one()
        u.driver_profile.has_open_corrections = n > 0


async def validate_value(db: AsyncSession, u: User, field: str, value: str) -> str:
    """Checks a submitted value (file key / text) and returns its normalised form."""
    value = (value or "").strip()
    if not value:
        raise HTTPException(422, {"code": "MISSING_ITEMS", "field": field})
    try:
        if is_file(field):
            await assert_owned(db, u.id, value)
        elif field == "cpr_number":
            value = validate_cpr(value)
        elif field == "phone":
            value = normalize_phone(value)
        elif field == "license.expiry_date":
            if date.fromisoformat(value) < date.today():
                raise HTTPException(422, {"code": "LICENSE_EXPIRED", "field": field})
    except ValueError as e:
        raise HTTPException(422, {"code": "INVALID_VALUE", "message": str(e), "field": field})
    return value


async def apply(db: AsyncSession, u: User, c: ProfileChange) -> None:
    """Writes the accepted value. Raises 409 when it collides with another account."""
    f, val = c.field, c.new_value or ""
    p, v = u.driver_profile, u.vehicle
    if f == "profile_photo":
        u.profile_photo = val
    elif f == "cpr_photo":
        u.cpr_photo = val
    elif f == "cpr_number":
        h = cpr_hash(val)
        if (await db.execute(select(User.id).where(User.cpr_hash == h, User.id != u.id))).first():
            raise HTTPException(409, {"code": "CPR_EXISTS"})
        u.cpr_encrypted, u.cpr_hash, u.cpr_last3 = encrypt(val), h, val[-3:]
    elif f == "cpr_dates":
        import json
        d = json.loads(val)
        u.cpr_issue_date = date.fromisoformat(d["issue_date"]) if d.get("issue_date") else u.cpr_issue_date
        u.cpr_expiry_date = date.fromisoformat(d["expiry_date"]) if d.get("expiry_date") else u.cpr_expiry_date
    elif f == "phone":
        if (await db.execute(select(User.id).where(User.phone == val, User.id != u.id))).first():
            raise HTTPException(409, {"code": "PHONE_EXISTS"})
        u.phone = val
    elif p and f == "license":
        import json
        d = json.loads(val)
        p.license_issue_date = date.fromisoformat(d["issue_date"]) if d.get("issue_date") else p.license_issue_date
        p.license_expiry_date = date.fromisoformat(d["expiry_date"])
        p.license_document = d["photo"]
        if d.get("license_number"):
            p.license_number_encrypted = encrypt(d["license_number"])
    elif p and f == "license.photo_front":
        p.license_document = val
    elif p and f == "license.photo_back":
        p.license_document_back = val
    elif p and f == "license.number":
        p.license_number_encrypted = encrypt(val)
    elif p and f == "license.expiry_date":
        p.license_expiry_date = date.fromisoformat(val)
    elif v and f == "vehicle.plate_number":
        if (await db.execute(select(Vehicle.id).where(Vehicle.plate_number == val, Vehicle.id != v.id))).first():
            raise HTTPException(409, {"code": "PLATE_EXISTS"})
        v.plate_number = val
    elif v and f.startswith("vehicle.photos."):
        v.photos = {**(v.photos or {}), f.rsplit(".", 1)[1]: val}
    else:
        raise HTTPException(422, {"code": "INVALID_FIELD", "field": f})


def row_out(c: ProfileChange, u: User | None = None) -> dict:
    from ..serializers import dt
    d = {"id": c.id, "field": c.field, "label": label(c.field), "reason": c.reason, "status": c.status,
         "requested_at": dt(c.requested_at), "submitted_at": dt(c.submitted_at)}
    if c.kind == CHANGE:
        d["new_value"] = c.new_value
    return d


def touch(c: ProfileChange, admin_id: int) -> None:
    c.reviewed_by, c.reviewed_at = admin_id, utcnow()

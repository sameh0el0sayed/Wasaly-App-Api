from decimal import Decimal
from typing import NamedTuple

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PromoCode, Referral, User, utcnow


def _err(code: str, message: str) -> HTTPException:
    return HTTPException(422, {"code": code, "message": message})


async def validate_promo(db: AsyncSession, settings: dict, raw_code: str | None, trip_price: Decimal) -> PromoCode | None:
    """Returns the usable promo (or None when no code was sent). `trip_price` = estimated base + night surcharge."""
    if not raw_code or not raw_code.strip():
        return None
    if settings.get("promo_enabled") != "true":
        raise _err("PROMO_DISABLED", "أكواد الخصم غير متاحة حاليًا")
    promo = (await db.execute(select(PromoCode).where(PromoCode.code == raw_code.strip().upper()))).scalar_one_or_none()
    if not promo or not promo.is_active:
        raise _err("PROMO_INVALID", "كود الخصم غير صحيح")
    now = utcnow()
    if promo.starts_at and now < promo.starts_at:
        raise _err("PROMO_NOT_STARTED", "كود الخصم لم يبدأ بعد")
    if promo.ends_at and now >= promo.ends_at:
        raise _err("PROMO_EXPIRED", "انتهت صلاحية كود الخصم")
    if promo.min_order_amount is not None and trip_price < promo.min_order_amount:
        raise _err("PROMO_MIN_ORDER", f"الحد الأدنى للطلب لاستخدام الكود {promo.min_order_amount:.3f}")
    return promo


class ReferralUse(NamedTuple):
    code: str
    inviter_id: int
    amount: Decimal
    row: Referral | None


async def resolve_code(db: AsyncSession, settings: dict, raw_code: str | None, trip_price: Decimal, customer_id: int):
    """A code is either a promo code (percent, platform-paid) or another customer's referral code (fixed discount for the invitee;
    the inviter is credited after the trip is completed and paid). Returns (promo | None, ReferralUse | None)."""
    if not raw_code or not raw_code.strip():
        return None, None
    code = raw_code.strip().upper()
    if (await db.execute(select(PromoCode.id).where(PromoCode.code == code))).first():
        return await validate_promo(db, settings, code, trip_price), None
    inviter = (await db.execute(select(User).where(User.referral_code == code))).scalars().first()
    if not inviter:
        return await validate_promo(db, settings, code, trip_price), None  # raises the usual PROMO_* error
    if settings.get("referral_enabled") != "true":
        raise _err("PROMO_INVALID", "كود الخصم غير صحيح")
    if inviter.id == customer_id:
        raise _err("REFERRAL_SELF", "لا يمكنك استخدام كود الدعوة الخاص بك")
    row = (await db.execute(select(Referral).where(Referral.invitee_id == customer_id))).scalars().first()
    if row and (row.inviter_id != inviter.id or row.status != "REGISTERED"):
        raise _err("REFERRAL_ALREADY_USED", "استخدمتَ كود دعوة من قبل")
    if not row:
        n = (await db.execute(select(func.count()).select_from(Referral).where(Referral.inviter_id == inviter.id, Referral.status != "REVOKED"))).scalar_one()
        if n >= int(settings["referral_max_per_user"]):
            raise _err("REFERRAL_LIMIT", "بلغ هذا الكود الحد الأقصى للاستخدام")
    amount = Decimal(settings["referral_reward_invitee"])
    if amount <= 0:
        raise _err("PROMO_INVALID", "كود الخصم غير صحيح")
    return None, ReferralUse(code, inviter.id, amount, row)

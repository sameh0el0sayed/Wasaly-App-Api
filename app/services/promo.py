from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import PromoCode, utcnow


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

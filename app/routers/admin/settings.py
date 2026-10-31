import json
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import CURRENCIES
from ...database import get_db
from ...deps import admin_user, super_admin_user
from ...models import (User)
from ...schemas import (SettingsPatch)
from ...services import settings_service
from ...services.audit import audit
from ...services.pricing import fits_decimals

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- settings ----------------
@router.get("/settings")
async def get_settings(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await settings_service.get_all(db)


@router.patch("/settings")
async def patch_settings(body: SettingsPatch, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """Pricing changes apply to NEW orders only (orders keep their price snapshot)."""
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    before = await settings_service.get_all(db)
    new_cur = changes.get("currency_code", before["currency_code"])
    if new_cur != before["currency_code"] and not ("unit_price" in changes and "min_price" in changes):
        raise HTTPException(422, {"code": "PRICES_REQUIRED_WITH_CURRENCY_CHANGE",
                                  "message": "Changing currency requires unit_price and min_price in the same request"})
    decimals = CURRENCIES[new_cur]
    ctype = changes.get("commission_type", before["commission_type"])
    cval = Decimal(str(changes.get("commission_value", before["commission_value"])))
    if ctype == "PERCENT" and cval > 100:
        raise HTTPException(422, {"code": "COMMISSION_PERCENT_TOO_HIGH", "max": 100})
    if ctype == "FIXED":
        if new_cur != before["currency_code"] and "commission_value" not in changes:
            raise HTTPException(422, {"code": "COMMISSION_REQUIRED_WITH_CURRENCY_CHANGE"})
        if not fits_decimals(cval, decimals):
            raise HTTPException(422, {"code": "TOO_MANY_DECIMALS", "field": "commission_value", "currency": new_cur, "max_decimals": decimals})
    for k in ("unit_price", "min_price"):
        val = Decimal(str(changes.get(k, before[k])))
        if not fits_decimals(val, decimals):
            raise HTTPException(422, {"code": "TOO_MANY_DECIMALS", "field": k, "currency": new_cur, "max_decimals": decimals})
    for k, v in changes.items():
        val = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else (str(v).lower() if isinstance(v, bool) else str(v))
        await settings_service.set_value(db, k, val, admin.id)
    audit(db, admin.id, "SETTINGS_CHANGED", "settings", "global", body.reason,
          {"before": {k: before[k] for k in changes}, "after": {k: str(v) for k, v in changes.items()}})
    await db.commit()
    return await settings_service.get_all(db)

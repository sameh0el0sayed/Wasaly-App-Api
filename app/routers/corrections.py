"""User side: see and submit the corrections an admin asked for, and request a phone change (admin approves)."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import current_user, request_lang
from ..i18n import localize
from ..models import ProfileChange, User, utcnow
from ..schemas import CorrectionSubmitIn, PhoneChangeIn
from ..security import create_file_token
from ..services import corrections as C
from ..services.payments import notify_admins

router = APIRouter(prefix="/api", tags=["corrections"])


@router.get("/corrections")
async def my_corrections(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(ProfileChange).where(
        ProfileChange.user_id == user.id, ProfileChange.kind == C.CORRECTION,
        ProfileChange.status.in_([C.OPEN, C.SUBMITTED])).order_by(ProfileChange.id))).scalars().all()
    items = []
    for c in rows:
        cur = C.current_value(user, c.field)
        d = C.row_out(c)
        if C.is_file(c.field):
            d["current_value_url"] = f"/api/files/download/{create_file_token(cur)}" if cur else None
        else:
            d["current_value"] = cur
        items.append(d)
    return {"items": items}


@router.post("/corrections/submit")
async def submit_corrections(body: CorrectionSubmitIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if not body.items:
        raise HTTPException(422, {"code": "MISSING_ITEMS"})
    done = []
    for it in body.items:
        c = await db.get(ProfileChange, it.id)
        if not c or c.user_id != user.id or c.kind != C.CORRECTION:
            raise HTTPException(404, "Correction not found")
        if c.status != C.OPEN:
            raise HTTPException(409, {"code": "CORRECTION_NOT_OPEN", "id": c.id})
        c.new_value = await C.validate_value(db, user, c.field, it.value)
        c.status, c.submitted_at = C.SUBMITTED, utcnow()
        done.append(c)
    await C.refresh_flag(db, user.id)
    await notify_admins(db, "CORRECTION_SUBMITTED", "تم إرسال تصحيح بيانات", "أرسل مستخدم تصحيحًا للمراجعة.")
    await db.commit()
    return {"items": [C.row_out(c) for c in done]}


@router.post("/profile/phone/request", status_code=202)
async def request_phone_change(body: PhoneChangeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                               lang: str = Depends(request_lang)):
    """Phone changes need admin approval (no OTP). The current number keeps working until an admin accepts."""
    if body.phone == user.phone:
        raise HTTPException(409, {"code": "SAME_PHONE"})
    if (await db.execute(select(User.id).where(User.phone == body.phone, User.id != user.id))).first():
        raise HTTPException(409, {"code": "PHONE_EXISTS"})
    old = (await db.execute(select(ProfileChange).where(
        ProfileChange.user_id == user.id, ProfileChange.field == "phone", ProfileChange.status == C.PENDING))).scalars().all()
    for o in old:  # one pending phone request at a time: the new one replaces it
        await db.delete(o)
    c = ProfileChange(user_id=user.id, kind=C.CHANGE, field="phone", status=C.PENDING, new_value=body.phone,
                      requested_by=user.id, submitted_at=utcnow())
    db.add(c)
    await notify_admins(db, "PROFILE_CHANGE_SUBMITTED", "طلب تغيير بيانات", "طلب مستخدم تغيير رقم هاتفه.")
    await db.commit()
    return {"message": localize("The request was sent for review", lang), "pending": C.row_out(c)}

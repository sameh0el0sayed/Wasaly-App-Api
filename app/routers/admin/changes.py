"""Admin side of corrections (field-level, never locks the account) and pending sensitive changes."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import Role
from ...database import get_db
from ...deps import admin_user, ai_review_user, paging
from ...models import ProfileChange, User
from ...schemas import ChangeReviewIn, CorrectionRequestIn
from ...security import create_file_token
from ...serializers import user_public
from ...services import corrections as C
from ...services.audit import audit
from ...services.notifications import notify
from .users import _target

router = APIRouter(prefix="/api/admin", tags=["admin"])


def _url(key):
    return f"/api/files/download/{create_file_token(key)}" if key else None


@router.post("/users/{user_id}/request-correction")
async def request_correction(user_id: int, body: CorrectionRequestIn, admin: User = Depends(ai_review_user), db: AsyncSession = Depends(get_db)):
    """Asks for specific fields to be fixed. The account stays as it is; a driver cannot go online until all are submitted."""
    u = await _target(db, user_id)
    if admin.role == Role.AI and (
        u.role != Role.CUSTOMER or any(f.field not in {"profile_photo", "cpr_photo"} for f in body.fields)
    ):
        raise HTTPException(403, {"code": "AI_CUSTOMER_DOCUMENT_CORRECTION_ONLY"})
    for f in body.fields:
        if f.field == "phone":
            raise HTTPException(422, {"code": "INVALID_FIELD", "field": f.field})
        C.check_field(u, f.field)
    for f in body.fields:
        c = (await db.execute(select(ProfileChange).where(
            ProfileChange.user_id == u.id, ProfileChange.kind == C.CORRECTION, ProfileChange.field == f.field,
            ProfileChange.status.in_([C.OPEN, C.SUBMITTED])))).scalars().first()
        if c:
            c.reason, c.status, c.new_value, c.submitted_at = f.reason, C.OPEN, None, None
        else:
            db.add(ProfileChange(user_id=u.id, kind=C.CORRECTION, field=f.field, reason=f.reason, status=C.OPEN, requested_by=admin.id))
    await db.flush()
    await C.refresh_flag(db, u.id)
    audit(db, admin.id, "CORRECTION_REQUESTED", "user", u.id, body.reason, details={"fields": [f.field for f in body.fields]})
    msg = "يرجى مراجعة البيانات المطلوب تصحيحها وإعادة إرسالها."
    await notify(db, u.id, "CORRECTION_REQUIRED", "مطلوب تصحيح بيانات", msg + (f" السبب: {body.reason}" if body.reason else ""))
    await db.commit()
    rows = (await db.execute(select(ProfileChange).where(
        ProfileChange.user_id == u.id, ProfileChange.kind == C.CORRECTION, ProfileChange.status.in_([C.OPEN, C.SUBMITTED])).order_by(ProfileChange.id))).scalars().all()
    return {**user_public(u), "required_corrections": [C.row_out(c) for c in rows]}


async def _listing(db, kind: str, status: str | None, page):
    limit, offset = page
    q = select(ProfileChange).where(ProfileChange.kind == kind)
    if status:
        q = q.where(ProfileChange.status == status)
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(ProfileChange.id.desc()).limit(limit).offset(offset))).scalars().all()
    items = []
    for c in rows:
        u = await db.get(User, c.user_id)
        d = {**C.row_out(c), "user_id": u.id, "user_name": u.full_name, "kind": c.kind}
        old = C.current_value(u, c.field)
        if C.is_file(c.field):
            d["old_url"], d["new_url"] = _url(old), _url(c.new_value)
        else:
            d["old_value"], d["new_value"] = old, c.new_value
        items.append(d)
    return {"total": total, "items": items}


async def _review(db, admin, c: ProfileChange | None, kind: str, body: ChangeReviewIn):
    pending = C.SUBMITTED if kind == C.CORRECTION else C.PENDING
    if not c or c.kind != kind:
        raise HTTPException(404, "Not found")
    if c.status != pending:
        raise HTTPException(409, {"code": "NOT_PENDING_REVIEW", "status": c.status})
    u = await db.get(User, c.user_id)
    pre = "CORRECTION" if kind == C.CORRECTION else "PROFILE_CHANGE"
    if body.status == "ACCEPTED":
        await C.apply(db, u, c)
        c.status = C.ACCEPTED
        title, msg = ("تم قبول التصحيح", "تم قبول البيانات المرسلة.") if kind == C.CORRECTION else ("تم قبول طلب التغيير", "Your details were updated.")
    else:
        if not body.reason:
            raise HTTPException(422, {"code": "REASON_REQUIRED"})
        c.reason = body.reason
        if kind == C.CORRECTION:  # goes back to the user to send again
            c.status, c.new_value, c.submitted_at = C.OPEN, None, None
            title, msg = "تم رفض التصحيح", "يرجى إعادة إرسال البيانات."
        else:
            c.status = "REJECTED"
            title, msg = "تم رفض طلب التغيير", "لم تتم الموافقة على التغيير."
    C.touch(c, admin.id)
    await db.flush()
    await C.refresh_flag(db, u.id)
    audit(db, admin.id, f"{pre}_{body.status}", "user", u.id, body.reason, details={"field": c.field, "id": c.id})
    if c.field == "license":
        if body.status == "ACCEPTED":
            await notify(db, u.id, "LICENSE_RENEWED", "تم تجديد الرخصة", "تم اعتماد رخصتك الجديدة، ويمكنك استقبال الطلبات.")
        else:
            await notify(db, u.id, "LICENSE_RENEWAL_REJECTED", "تم رفض تجديد الرخصة", f"لم يتم اعتماد طلب تجديد الرخصة. السبب: {body.reason}",
                         data={"reason": body.reason})
    else:
        await notify(db, u.id, f"{pre}_{body.status}", title, msg + (f" السبب: {body.reason}" if body.status == "REJECTED" else ""))
    await db.commit()
    return {**C.row_out(c), "user_id": u.id}


@router.get("/corrections")
async def list_corrections(status: str | None = None, page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await _listing(db, C.CORRECTION, status, page)


@router.post("/corrections/{cid}/review")
async def review_correction(cid: int, body: ChangeReviewIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await _review(db, admin, await db.get(ProfileChange, cid), C.CORRECTION, body)


@router.get("/pending-changes")
async def list_pending(status: str | None = None, page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await _listing(db, C.CHANGE, status, page)


@router.post("/pending-changes/{cid}/review")
async def review_pending(cid: int, body: ChangeReviewIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await _review(db, admin, await db.get(ProfileChange, cid), C.CHANGE, body)

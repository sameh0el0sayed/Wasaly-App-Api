import jwt
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..database import get_db
from ..deps import approved_user, current_user, paging, request_lang
from ..i18n import localize
from ..models import (
    Banner,
    Complaint,
    DeviceToken,
    Notification,
    Order,
    UploadedFile,
    User,
    VehicleDocument,
    utcnow,
)
from ..schemas import ComplaintIn, CprChangeIn, DeviceIn, DocumentIn, LicenseRenewIn, ProfilePatch, ReasonIn, SignIn
from ..security import create_file_token, decode_file_token
from ..serializers import driver_block, dt, user_public
from ..services.dispatch import force_offline
from ..services.files import assert_owned, get_bytes, save_upload

router = APIRouter(prefix="/api", tags=["profile"])


@router.get("/profile")
async def get_profile(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    from ..models import ProfileChange
    from ..services import corrections as C
    d = user_public(user)
    d["created_at"] = dt(user.created_at)
    rows = (await db.execute(select(ProfileChange).where(ProfileChange.user_id == user.id, ProfileChange.status.in_([C.OPEN, C.SUBMITTED, C.PENDING]))
                             .order_by(ProfileChange.id))).scalars().all()
    d["required_corrections"] = [C.row_out(c) for c in rows if c.kind == C.CORRECTION]
    d["pending_changes"] = [{"field": c.field, "new_value": c.new_value, "status": c.status, "submitted_at": dt(c.submitted_at)}
                            for c in rows if c.kind == C.CHANGE]
    if user.role == Role.DRIVER:
        d["driver"] = driver_block(user)
    return d


@router.patch("/profile")
async def patch_profile(body: ProfilePatch, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Only non-sensitive fields. CPR / phone changes require admin review (see /profile/sensitive-change-request)."""
    data = body.model_dump(exclude_unset=True)
    cpr_photo = data.pop("cpr_photo", None)
    if data.get("profile_photo"):
        await assert_owned(db, user.id, data["profile_photo"])
    if data.get("language") is None:
        data.pop("language", None)
    if data.get("full_name") is None:
        data.pop("full_name", None)
    for k, v in data.items():  # everything except phone and CPR is edited directly
        setattr(user, k, v)
    if cpr_photo:  # the CPR card goes to admin review; the old one stays until accepted
        await _queue_change(db, user, "cpr_photo", cpr_photo)
    await db.commit()
    return user_public(user)


async def _queue_change(db: AsyncSession, user: User, field: str, value: str) -> None:
    from ..models import ProfileChange
    from ..services import corrections as C
    from ..services.payments import notify_admins
    if field in ("cpr_photo",):
        await assert_owned(db, user.id, value)
    old = (await db.execute(select(ProfileChange).where(ProfileChange.user_id == user.id, ProfileChange.field == field,
                                                        ProfileChange.status == C.PENDING))).scalars().all()
    for o in old:  # one pending request per field: the new one replaces it
        await db.delete(o)
    db.add(ProfileChange(user_id=user.id, kind=C.CHANGE, field=field, status=C.PENDING, new_value=value,
                         requested_by=user.id, submitted_at=utcnow()))
    await notify_admins(db, "PROFILE_CHANGE_SUBMITTED", "طلب تغيير بيانات", "طلب مستخدم تغيير بياناته.")


@router.post("/profile/cpr/request", status_code=202)
async def request_cpr_change(body: CprChangeIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                             lang: str = Depends(request_lang)):
    """CPR number / CPR card changes need admin approval. The current values keep working until an admin accepts."""
    if not body.cpr_number and not body.cpr_photo:
        raise HTTPException(422, {"code": "NOTHING_TO_CHANGE"})
    if body.cpr_number:
        from ..security import cpr_hash
        if (await db.execute(select(User.id).where(User.cpr_hash == cpr_hash(body.cpr_number), User.id != user.id))).first():
            raise HTTPException(409, {"code": "CPR_EXISTS"})
        await _queue_change(db, user, "cpr_number", body.cpr_number)
    if body.cpr_photo:
        await _queue_change(db, user, "cpr_photo", body.cpr_photo)
    await db.commit()
    return {"message": localize("تم إرسال الطلب للمراجعة", lang)}


@router.post("/profile/sensitive-change-request", status_code=202)
async def sensitive_change(body: ReasonIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                           lang: str = Depends(request_lang)):
    db.add(Complaint(user_id=user.id, type="SENSITIVE_CHANGE", subject="Sensitive data change request", message=body.reason))
    await db.commit()
    return {"message": localize("تم إرسال الطلب للمراجعة", lang)}


@router.post("/profile/deletion-request", status_code=202)
async def deletion_request(body: ReasonIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                           lang: str = Depends(request_lang)):
    db.add(Complaint(user_id=user.id, type="DELETION_REQUEST", subject="Account suspension/deletion request", message=body.reason))
    await db.commit()
    return {"message": localize("تم إرسال طلبك للإدارة", lang)}


# ---------- driver-side profile updates ----------
@router.post("/profile/documents", status_code=201)
async def upload_document(body: DocumentIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.role != Role.DRIVER or not user.vehicle:
        raise HTTPException(403, "Drivers only")
    await assert_owned(db, user.id, body.file_key)
    doc = VehicleDocument(vehicle_id=user.vehicle.id, document_type=body.document_type,
                          file_path=body.file_key, expiry_date=body.expiry_date)
    db.add(doc)
    if user.account_status == AccountStatus.NEEDS_CORRECTION:
        user.account_status, user.status_reason = AccountStatus.PENDING_APPROVAL, None
        user.driver_profile.driver_status = AccountStatus.PENDING_APPROVAL
    await db.commit()
    return {"id": doc.id, "review_status": doc.review_status}


@router.post("/profile/license", status_code=200)
async def renew_license(body: LicenseRenewIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.role != Role.DRIVER:
        raise HTTPException(403, "Drivers only")
    await assert_owned(db, user.id, body.license_document)
    p = user.driver_profile
    p.license_expiry_date, p.license_document = body.license_expiry_date, body.license_document
    p.driver_status = AccountStatus.PENDING_APPROVAL  # re-approval needed
    await force_offline(db, user.id)
    await db.commit()
    return {"driver_status": p.driver_status}


# ---------- uploads / files ----------
@router.post("/uploads/registration", status_code=201)
async def upload_registration_file(kind: str = Form("misc"), file: UploadFile = File(...),
                                   db: AsyncSession = Depends(get_db)):
    """Anonymous upload used during registration; the file is claimed by the user at /auth/register."""
    row = await save_upload(db, None, file, kind)
    await db.commit()
    return {"file_key": row.file_key}


@router.post("/uploads", status_code=201)
async def upload_file(kind: str = Form("misc"), file: UploadFile = File(...),
                      user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = await save_upload(db, user.id, file, kind)
    await db.commit()
    return {"file_key": row.file_key, "size": row.size, "content_type": row.content_type}


@router.post("/files/sign")
async def sign_file(body: SignIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """Returns a short-lived (5 min) URL. Owner or admin only."""
    row = await db.get(UploadedFile, body.file_key)
    if not row:
        raise HTTPException(404, "File not found")
    if row.owner_id != user.id and user.role not in Role.ADMINS:
        proof = (await db.execute(select(Order.id).where(
            (Order.pickup_photo == body.file_key) | (Order.delivery_photo == body.file_key),
            (Order.customer_id == user.id) | (Order.driver_id == user.id)).limit(1))).first()
        banner = (await db.execute(select(Banner.id).where(Banner.image_file_key == body.file_key,
                                                           Banner.is_active.is_(True)).limit(1))).first()
        if not proof and not banner:  # proof photos: people on the order only; banner images: any signed-in user
            raise HTTPException(404, "File not found")
    return {"url": f"/api/files/download/{create_file_token(body.file_key)}", "expires_in_seconds": 300}


@router.get("/files/download/{token}")
async def download_file(token: str, db: AsyncSession = Depends(get_db)):
    try:
        key = decode_file_token(token)
    except jwt.PyJWTError:
        raise HTTPException(403, "Link expired or invalid")
    row = await db.get(UploadedFile, key)
    if not row:
        raise HTTPException(404, "File not found")
    return Response(await get_bytes(key), media_type=row.content_type, headers={"Cache-Control": "private, no-store"})


# ---------- notifications / devices / complaints ----------
@router.get("/notifications")
async def list_notifications(unread_only: bool = False, page=Depends(paging), user: User = Depends(current_user),
                             db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        q = q.where(Notification.is_read.is_(False))
    rows = (await db.execute(q.order_by(Notification.id.desc()).limit(limit).offset(offset))).scalars().all()
    unread = (await db.execute(select(func.count()).select_from(Notification).where(
        Notification.user_id == user.id, Notification.is_read.is_(False)))).scalar_one()
    return {"unread": unread, "items": [{"id": n.id, "type": n.notification_type, "title": n.title, "message": n.message,
                                         "order_id": n.related_order_id, "is_read": n.is_read,
                                         "created_at": dt(n.created_at)} for n in rows]}


@router.post("/notifications/read-all")
async def read_all(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    await db.execute(update(Notification).where(Notification.user_id == user.id).values(is_read=True))
    await db.commit()
    return {"ok": True}


@router.post("/notifications/{nid}/read")
async def read_one(nid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    n = await db.get(Notification, nid)
    if not n or n.user_id != user.id:
        raise HTTPException(404, "Not found")
    n.is_read = True
    await db.commit()
    return {"ok": True}


@router.post("/devices", status_code=201)
async def register_device(body: DeviceIn, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(DeviceToken).where(DeviceToken.token == body.token))).scalar_one_or_none()
    if row:
        row.user_id, row.platform = user.id, body.platform
    else:
        db.add(DeviceToken(user_id=user.id, token=body.token, platform=body.platform))
    await db.commit()
    return {"ok": True}


@router.post("/complaints", status_code=201)
async def create_complaint(body: ComplaintIn, user: User = Depends(approved_user), db: AsyncSession = Depends(get_db)):
    if body.order_id:
        o = await db.get(Order, body.order_id)
        if not o or user.id not in (o.customer_id, o.driver_id):
            raise HTTPException(404, "Order not found")
    c = Complaint(user_id=user.id, order_id=body.order_id, type=body.type, subject=body.subject, message=body.message)
    db.add(c)
    await db.commit()
    return {"id": c.id, "status": c.status}


@router.get("/complaints/mine")
async def my_complaints(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Complaint).where(Complaint.user_id == user.id).order_by(Complaint.id.desc()))).scalars().all()
    return [{"id": c.id, "type": c.type, "subject": c.subject, "message": c.message, "status": c.status,
             "admin_response": c.admin_response, "created_at": dt(c.created_at)} for c in rows]

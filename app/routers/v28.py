"""v28: admin delete (partners, service areas) and bulk approve of pending reviews (password-confirmed)."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import (
    DriverServiceArea,
    Order,
    Partner,
    PartnerStatement,
    Payment,
    ProfileChange,
    ServiceArea,
    User,
    Vehicle,
    VehicleDocument,
    utcnow,
)
from ..schemas import ChangeReviewIn, DocReviewIn, PaymentConfirmIn
from ..security import create_file_token, verify_password
from ..services import corrections as C
from ..services.audit import audit
from ..services.ratelimit import _hit
from .admin.changes import _review
from .admin.drivers import review_document
from .payments import confirm_payment

router = APIRouter(prefix="/api/admin", tags=["v28-admin"])
KINDS = ("driver_photos", "pending_changes", "corrections", "benefitpay")
Kind = Literal["driver_photos", "pending_changes", "corrections", "benefitpay"]


# ---------------- delete ----------------
@router.delete("/partners/{partner_id}")
async def delete_partner(partner_id: int, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Partner, partner_id)
    if not p or p.deleted_at:
        raise HTTPException(404, "Partner not found")
    unpaid = (await db.execute(select(func.count()).select_from(PartnerStatement)
                               .where(PartnerStatement.partner_id == p.id, PartnerStatement.status == "PENDING"))).scalar_one()
    if unpaid:
        raise HTTPException(409, {"code": "PARTNER_HAS_UNPAID"})
    p.deleted_at, p.is_active = utcnow(), False  # statements and history stay
    audit(db, admin.id, "PARTNER_DELETED", "partner", p.id, details={"name": p.name})
    await db.commit()
    return {"ok": True}


@router.delete("/service-areas/{area_id}")
async def delete_area(area_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    a = await db.get(ServiceArea, area_id)
    if not a or a.deleted_at:
        raise HTTPException(404, "Area not found")
    active = (await db.execute(select(func.count()).select_from(Order)
                               .where(Order.service_area_id == a.id, Order.order_status.notin_(list(S.FINAL))))).scalar_one()
    if active:
        raise HTTPException(409, {"code": "AREA_IN_USE"})
    await db.execute(delete(DriverServiceArea).where(DriverServiceArea.service_area_id == a.id))
    name = a.name
    a.deleted_at, a.is_active = utcnow(), False
    a.name = f"{name} (deleted #{a.id})"[:100]  # frees the unique name
    audit(db, admin.id, "AREA_DELETED", "area", a.id, details={"name": name})
    await db.commit()
    return {"ok": True}


# ---------------- bulk approve ----------------
def _url(key):
    return f"/api/files/download/{create_file_token(key)}" if key else None


def _t(admin: User, ar: str, en: str) -> str:
    return en if (admin.language or "ar") == "en" else ar


def _pending_q(kind: str):
    if kind == "driver_photos":
        return select(VehicleDocument).where(VehicleDocument.review_status == "PENDING").order_by(VehicleDocument.id)
    if kind == "pending_changes":
        return select(ProfileChange).where(ProfileChange.kind == C.CHANGE, ProfileChange.status == C.PENDING).order_by(ProfileChange.id)
    if kind == "corrections":
        return select(ProfileChange).where(ProfileChange.kind == C.CORRECTION, ProfileChange.status == C.SUBMITTED).order_by(ProfileChange.id)
    return select(Payment).where(Payment.payment_method == "BENEFITPAY", Payment.payment_status == "PENDING_VERIFICATION").order_by(Payment.id)


@router.get("/bulk-approve/summary")
async def bulk_summary(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return {k: (await db.execute(select(func.count()).select_from(_pending_q(k).order_by(None).subquery()))).scalar_one() for k in KINDS}


@router.get("/bulk-approve/{kind}")
async def bulk_list(kind: Kind, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(_pending_q(kind).limit(200))).scalars().all()
    items = []
    for r in rows:
        if kind == "driver_photos":
            veh = await db.get(Vehicle, r.vehicle_id)
            u = await db.get(User, veh.driver_id) if veh else None
            items.append({"id": r.id, "title": u.full_name if u else "-", "subtitle": r.document_type, "images": [x for x in [_url(r.file_path)] if x],
                          "fields": [{"label": _t(admin, "نوع المستند", "Document"), "value": r.document_type},
                                     {"label": _t(admin, "تاريخ الانتهاء", "Expiry"), "value": str(r.expiry_date) if r.expiry_date else "-"}]})
        elif kind in ("pending_changes", "corrections"):
            u = await db.get(User, r.user_id)
            old, new = C.current_value(u, r.field), r.new_value
            if C.is_file(r.field):
                imgs, fields = [x for x in [_url(old), _url(new)] if x], []
            else:
                imgs, fields = [], [{"label": _t(admin, "القديم", "Old"), "value": str(old or "-")},
                                    {"label": _t(admin, "الجديد", "New"), "value": str(new or "-")}]
            items.append({"id": r.id, "title": u.full_name, "subtitle": C.label(r.field), "images": imgs, "fields": fields})
        else:
            u = await db.get(User, r.customer_id)
            items.append({"id": r.id, "title": u.full_name if u else "-", "subtitle": f"{r.amount} {r.currency}", "images": [x for x in [_url(r.proof_file)] if x],
                          "fields": [{"label": _t(admin, "الطلب", "Order"), "value": str(r.order_id or "-")},
                                     {"label": _t(admin, "المبلغ", "Amount"), "value": f"{r.amount} {r.currency}"}]})
    return {"items": items}


class BulkIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=200)
    password: str = Field(min_length=1, max_length=200)


@router.post("/bulk-approve/{kind}")
async def bulk_approve(kind: Kind, body: BulkIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    admin_id, admin_name, lang = admin.id, admin.full_name, admin.language
    if await _hit(f"rl:bulkpw:{admin_id}", 900) > 5:  # 5 attempts / 15 min per admin
        raise HTTPException(429, {"code": "RATE_LIMITED", "retry_after_seconds": 900})
    if not verify_password(admin.password_hash, body.password):
        raise HTTPException(403, {"code": "INVALID_PASSWORD"})
    note = f"Instant approval by admin {admin_name}" if (lang or "ar") == "en" else f"موافقة فورية من المدير {admin_name}"
    approved, failed = [], []
    for rid in dict.fromkeys(body.ids):
        try:
            if kind == "driver_photos":
                d = await db.get(VehicleDocument, rid)
                if not d:
                    raise HTTPException(404, "Not found")
                if d.review_status != "PENDING":
                    raise HTTPException(409, {"code": "NOT_PENDING"})
                await review_document(rid, DocReviewIn(status="APPROVED", reason=note), admin, db)
            elif kind in ("pending_changes", "corrections"):
                c = await db.get(ProfileChange, rid)
                await _review(db, admin, c, C.CHANGE if kind == "pending_changes" else C.CORRECTION,
                              ChangeReviewIn(status="ACCEPTED", reason=note))
            else:
                await confirm_payment(rid, PaymentConfirmIn(review_notes={"approval_note": note}), admin, db)
            approved.append(rid)
        except HTTPException as e:
            await db.rollback()
            await db.refresh(admin)  # rollback expires loaded attributes
            code = e.detail.get("code") if isinstance(e.detail, dict) else None
            if e.status_code == 404:
                code = "NOT_FOUND"
            elif e.status_code == 409:
                code = "NOT_PENDING"
            failed.append({"id": rid, "code": code or "ERROR"})
        except Exception:  # noqa: BLE001 - one bad item must not stop the batch
            await db.rollback()
            await db.refresh(admin)
            failed.append({"id": rid, "code": "ERROR"})
    audit(db, admin_id, "BULK_APPROVED", "bulk", 0, details={"kind": kind, "count": len(approved), "ids": approved})
    await db.commit()
    return {"approved": approved, "failed": failed}

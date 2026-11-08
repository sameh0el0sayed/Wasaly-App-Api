"""v21: AI review dashboard (heartbeat, cycles, results) read from the worker's audit rows."""
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..deps import super_admin_user
from ..models import AdminAuditLog, Payment, User
from ..serializers import _file_url, dt
from ..services import ai_review

admin_router = APIRouter(prefix="/api/admin/ai/review", tags=["v21-ai-review"])

REASON_CODES = ["USER_NOT_PENDING_REVIEW", "MISSING_CPR_DATA", "CPR_FILE_NOT_FOUND", "MISSING_DRIVER_LICENSE", "LICENSE_FILE_NOT_FOUND",
                "ACCOUNT_MISMATCH", "AMOUNT_MISMATCH", "CURRENCY_MISMATCH", "REFERENCE_MISSING"]
_ACTIONS = {"user": "AI_IDENTITY_REVIEW", "payment": "AI_RECEIPT_REVIEW", "correction": "AI_CORRECTION_REVIEW"}
_ID_KEY = {"user": "user_id", "payment": "payment_id", "correction": "correction_id"}


def _n(v) -> int:
    return len(v) if isinstance(v, list) else int(v or 0)


@admin_router.get("/heartbeat")
async def heartbeat(admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    st = ai_review.STATE
    now = datetime.now(UTC)
    hb = st["last_heartbeat_at"]
    fresh = bool(hb) and (now - datetime.fromisoformat(hb)).total_seconds() < 150
    last = (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == "AI_REVIEW_CYCLE").order_by(AdminAuditLog.id.desc()).limit(1))).scalar_one_or_none()
    d = last.details if last else {}
    last_status = st["last_cycle_status"] or (("error" if str(d.get("status", "")).endswith("error") or str(d.get("status", "")).startswith("disabled") else "completed") if last else None)
    last_error = st["last_error"] or d.get("error_message") or d.get("error")
    if st["running"]:
        worker = "running"
    elif last_status == "error":
        worker = "error"
    elif fresh or (last and (now - last.created_at.replace(tzinfo=last.created_at.tzinfo or UTC)).total_seconds() < 2 * max(60, settings.ai_review_interval_seconds)):
        worker = "waiting"
    else:
        worker = "unknown"
    return {"worker_status": worker, "last_heartbeat_at": hb or (dt(last.created_at) if last else None), "next_review_at": st["next_review_at"],
            "review_interval_seconds": max(60, settings.ai_review_interval_seconds), "last_cycle_status": last_status, "last_error": last_error}


@admin_router.get("/cycles")
async def cycles(limit: int = 20, offset: int = 0, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    q = select(AdminAuditLog).where(AdminAuditLog.action == "AI_REVIEW_CYCLE")
    total = (await db.execute(select(func.count()).select_from(q.subquery()))).scalar_one()
    rows = (await db.execute(q.order_by(AdminAuditLog.id.desc()).limit(max(1, min(limit, 100))).offset(max(0, offset)))).scalars().all()
    items = []
    for r in rows:
        d = r.details or {}
        items.append({"cycle_id": d.get("cycle_id") or f"audit-{r.id}", "status": d.get("status"), "started_at": d.get("started_at") or dt(r.created_at),
                      "finished_at": dt(r.created_at),
                      "users_checked": d.get("users_checked", 0), "users_approved": _n(d.get("users_approved")), "users_not_approved": _n(d.get("users_not_approved")),
                      "users_errors": _n(d.get("users_errors")), "corrections_checked": d.get("corrections_checked", 0),
                      "corrections_accepted": _n(d.get("corrections_accepted")), "corrections_rejected": _n(d.get("corrections_rejected")),
                      "corrections_errors": _n(d.get("corrections_errors")), "payments_checked": d.get("payments_checked", 0),
                      "payments_confirmed": _n(d.get("payments_confirmed")), "payments_not_confirmed": _n(d.get("payments_not_confirmed")),
                      "payments_errors": _n(d.get("payments_errors"))})
    return {"items": items, "total": total}


def _item(kind: str, r: AdminAuditLog, names: dict) -> dict:
    d = r.details or {}
    uid = d.get("user_id")
    return {"item_type": kind, "item_id": d.get(_ID_KEY[kind]), "user_id": uid, "user_name": names.get(uid),
            "result": d.get("result"), "reason": d.get("reason") or d.get("correction_reason"), "reason_codes": d.get("reason_codes") or [],
            "reviewed_at": dt(r.created_at)}


async def _names(db: AsyncSession, kind: str, rows: list) -> dict:
    ids = set()
    for r in rows:
        d = r.details or {}
        if kind == "payment" and d.get("payment_id"):
            p = await db.get(Payment, d["payment_id"])
            if p:
                d["user_id"] = p.customer_id  # shown to the admin; stored copy is not modified
        if d.get("user_id"):
            ids.add(d["user_id"])
    return {i: n for i, n in (await db.execute(select(User.id, User.full_name).where(User.id.in_(ids)))).all()} if ids else {}


@admin_router.get("/results")
async def results(type: str = "user", result: str | None = None, limit: int = 30, offset: int = 0,
                  admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    if type not in _ACTIONS:
        raise HTTPException(422, {"code": "BAD_TYPE"})
    q = select(AdminAuditLog).where(AdminAuditLog.action == _ACTIONS[type])
    rows = (await db.execute(q.order_by(AdminAuditLog.id.desc()))).scalars().all()
    if result:
        rows = [r for r in rows if (r.details or {}).get("result") == result]
    total = len(rows)
    page = rows[max(0, offset):max(0, offset) + max(1, min(limit, 100))]
    names = await _names(db, type, page)
    return {"items": [_item(type, r, names) for r in page], "total": total, "reason_codes": REASON_CODES}


@admin_router.get("/results/{item_type}/{item_id}")
async def result_detail(item_type: str, item_id: int, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    if item_type not in _ACTIONS:
        raise HTTPException(422, {"code": "BAD_TYPE"})
    rows = [r for r in (await db.execute(select(AdminAuditLog).where(AdminAuditLog.action == _ACTIONS[item_type],
                                                                      AdminAuditLog.entity_id == str(item_id)).order_by(AdminAuditLog.id.desc()))).scalars().all()]
    if not rows:
        raise HTTPException(404, "No AI review for this item")
    r = rows[0]
    d = r.details or {}
    names = await _names(db, item_type, [r])
    out = _item(item_type, r, names)
    comps = []
    notes = d.get("review_notes")
    if isinstance(notes, dict):
        comps = [{"field": "beneficiary_account", "extracted_value": notes.get("beneficiary_account"), "expected_value": notes.get("expected_account"), "matched": notes.get("account_match")},
                 {"field": "payment_amount", "extracted_value": notes.get("amount"), "expected_value": notes.get("expected_amount"), "matched": notes.get("amount_match")},
                 {"field": "currency", "extracted_value": notes.get("currency"), "expected_value": notes.get("expected_currency"), "matched": notes.get("currency_match")}]
    for k, v in (d.get("field_matches") or {}).items():
        comps.append({"field": k.removesuffix("_match"), "extracted_value": None, "expected_value": None, "matched": bool(v)})
    url = None
    if item_type == "payment":
        p = await db.get(Payment, item_id)
        url = _file_url(p.proof_file) if p and p.proof_file else None
    elif item_type == "user":
        u = await db.get(User, item_id)
        url = _file_url(u.cpr_photo) if u and u.cpr_photo else None
    out.update({"confidence": d.get("confidence"), "comparisons": comps, "document_url": url, "audit_ids": [x.id for x in rows]})
    return out

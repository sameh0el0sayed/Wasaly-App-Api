
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ...constants import AccountStatus, Role
from ...database import get_db
from ...deps import admin_user, paging, super_admin_user
from ...models import AdminAuditLog, Complaint, User, utcnow
from ...schemas import BroadcastIn, ComplaintResolveIn
from ...serializers import dt
from ...services.audit import audit
from ...services.notifications import notify

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------------- complaints ----------------
@router.get("/complaints")
async def complaints(status: str | None = "OPEN", type: str | None = None, page=Depends(paging),
                     admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(Complaint)
    if status:
        stmt = stmt.where(Complaint.status == status)
    if type:
        stmt = stmt.where(Complaint.type == type)
    rows = (await db.execute(stmt.order_by(Complaint.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": c.id, "user_id": c.user_id, "order_id": c.order_id, "type": c.type, "subject": c.subject,
             "message": c.message, "status": c.status, "created_at": dt(c.created_at)} for c in rows]


@router.post("/complaints/{cid}/resolve")
async def resolve_complaint(cid: int, body: ComplaintResolveIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    c = await db.get(Complaint, cid)
    if not c:
        raise HTTPException(404, "Not found")
    c.status, c.admin_response, c.resolved_by, c.resolved_at = "RESOLVED", body.response, admin.id, utcnow()
    if c.user_id:
        await notify(db, c.user_id, "COMPLAINT_RESOLVED", "رد الإدارة", body.response, c.order_id)
    audit(db, admin.id, "COMPLAINT_RESOLVED", "complaint", c.id)
    await db.commit()
    return {"id": c.id, "status": c.status}


_SECRET_KEYS = {"cpr", "cpr_number", "password", "token", "secret", "iban", "otp", "access_token"}


def _clean(v):
    """Details shown to the admin app: plain values, no secrets and no full CPR."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if k.lower() not in _SECRET_KEYS}
    if isinstance(v, list):
        return [_clean(x) for x in v]
    return v


@router.get("/audit-logs")
async def audit_logs(action: str | None = None, q: str | None = None, entity_type: str | None = None, actor: str | None = None,
                     exclude: str | None = "AI_REVIEW_HEARTBEAT", date_from: date | None = Query(default=None, alias="from"),
                     date_to: date | None = Query(default=None, alias="to"), page=Depends(paging),
                     admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    from ...config import settings as cfg
    from ...security import normalize_phone
    from .reports import _local_start
    limit, offset = page
    ai_id = (await db.execute(select(User.id).where(User.phone == normalize_phone(cfg.ai_service_phone)))).scalar_one_or_none() if cfg.ai_service_phone else None
    stmt = select(AdminAuditLog)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    if exclude:
        stmt = stmt.where(AdminAuditLog.action.not_in([x.strip() for x in exclude.split(",") if x.strip()]))
    if entity_type:
        stmt = stmt.where(AdminAuditLog.entity_type == entity_type)
    if date_from:
        stmt = stmt.where(AdminAuditLog.created_at >= _local_start(date_from))
    if date_to:
        stmt = stmt.where(AdminAuditLog.created_at < _local_start(date_to + timedelta(days=1)))
    ai_cond = (AdminAuditLog.action.like("AI\\_%", escape="\\")) | ((AdminAuditLog.admin_id == ai_id) if ai_id else false())
    if actor == "AI":
        stmt = stmt.where(ai_cond)
    elif actor == "ADMIN":
        stmt = stmt.where(AdminAuditLog.admin_id.is_not(None), ~ai_cond)
    elif actor == "SYSTEM":
        stmt = stmt.where(AdminAuditLog.admin_id.is_(None), ~ai_cond)
    if q:
        like = f"%{q.strip()}%"
        names = select(User.id).where(User.full_name.ilike(like))
        stmt = stmt.where(AdminAuditLog.action.ilike(like) | AdminAuditLog.entity_id.ilike(like)
                          | AdminAuditLog.entity_type.ilike(like) | AdminAuditLog.admin_id.in_(names))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one()
    rows = (await db.execute(stmt.order_by(AdminAuditLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    uids = {a.admin_id for a in rows if a.admin_id} | {int(a.entity_id) for a in rows if a.entity_type == "user" and str(a.entity_id).isdigit()}
    names = {i: n for i, n in (await db.execute(select(User.id, User.full_name).where(User.id.in_(uids)))).all()} if uids else {}

    def label(a):
        eid = str(a.entity_id)
        if a.entity_type == "user":
            return names.get(int(eid)) if eid.isdigit() else eid
        return {"order": "Order #", "payment": "Payment #", "correction": "Correction #", "complaint": "Complaint #",
                "partner": "Partner #", "partner_statement": "Statement #"}.get(a.entity_type, f"{a.entity_type} ") + eid

    items = []
    for a in rows:
        is_ai = (a.action or "").startswith("AI_") or (ai_id is not None and a.admin_id == ai_id)
        kind = "AI" if is_ai else ("ADMIN" if a.admin_id else "SYSTEM")
        items.append({"id": a.id, "admin_id": a.admin_id, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
                      "reason": a.reason, "created_at": dt(a.created_at), "details": _clean(a.details),
                      "actor": {"id": a.admin_id, "name": names.get(a.admin_id) if a.admin_id else ("AI Review" if is_ai else "System"), "kind": kind},
                      "entity": {"type": a.entity_type, "id": str(a.entity_id), "label": label(a)}})
    return {"items": items, "total": total}


@router.get("/audit-logs/actions")
async def audit_actions(admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """Every action code present in the log, for the app's label/icon dictionary."""
    rows = (await db.execute(select(AdminAuditLog.action, func.count()).group_by(AdminAuditLog.action).order_by(AdminAuditLog.action))).all()
    return {"items": [{"action": a, "count": n} for a, n in rows]}


# ---------------- announcements ----------------
@router.post("/notifications")
async def send_announcement(body: BroadcastIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Custom message from the admin: to CUSTOMERS, to DRIVERS, to ALL (both), or to one user. Approved accounts only."""
    q = select(User.id).where(User.account_status == AccountStatus.APPROVED)
    if body.user_id is not None:
        q = q.where(User.id == body.user_id)
    else:
        roles = {"CUSTOMERS": [Role.CUSTOMER], "DRIVERS": [Role.DRIVER], "ALL": [Role.CUSTOMER, Role.DRIVER]}[body.audience]
        q = q.where(User.role.in_(roles))
    ids = (await db.execute(q)).scalars().all()
    if not ids:
        raise HTTPException(404, "No matching approved users")
    for uid in ids:
        await notify(db, uid, "ANNOUNCEMENT", body.title, body.message)
    audit(db, admin.id, "ANNOUNCEMENT_SENT", "notification", body.audience or str(body.user_id), details={"recipients": len(ids), "title": body.title})
    await db.commit()
    return {"recipients": len(ids)}

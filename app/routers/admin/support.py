
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
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


@router.get("/audit-logs")
async def audit_logs(action: str | None = None, page=Depends(paging), admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    stmt = select(AdminAuditLog)
    if action:
        stmt = stmt.where(AdminAuditLog.action == action)
    rows = (await db.execute(stmt.order_by(AdminAuditLog.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [{"id": a.id, "admin_id": a.admin_id, "action": a.action, "entity_type": a.entity_type, "entity_id": a.entity_id,
             "reason": a.reason, "details": a.details, "created_at": dt(a.created_at)} for a in rows]


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

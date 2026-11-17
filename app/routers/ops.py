"""Admin view of the AI operations manager: findings (suggestions), on-demand scan and management reports."""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user
from ..models import OpsFinding, OpsReport, User, utcnow
from ..services import ops_manager
from ..services.audit import audit

admin_router = APIRouter(prefix="/api/admin/ops", tags=["ops-manager"])


def _f(f: OpsFinding) -> dict:
    return {"id": f.id, "kind": f.kind, "severity": f.severity, "entity_type": f.entity_type, "entity_id": f.entity_id, "message": f.message,
            "suggestion": f.suggestion, "status": f.status, "created_at": f.created_at.isoformat(), "actor": "AI"}


@admin_router.get("/findings")
async def findings(status: str | None = "OPEN", kind: str | None = None, limit: int = 100, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    q = select(OpsFinding).order_by(OpsFinding.id.desc()).limit(min(limit, 500))
    if status:
        q = q.where(OpsFinding.status == status)
    if kind:
        q = q.where(OpsFinding.kind == kind)
    return {"items": [_f(f) for f in (await db.execute(q)).scalars()]}


@admin_router.post("/findings/{fid}/{action}")
async def act(fid: int, action: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """action: ack | resolve | dismiss. Only changes the finding itself."""
    st = {"ack": "ACKED", "resolve": "RESOLVED", "dismiss": "DISMISSED"}.get(action)
    f = await db.get(OpsFinding, fid)
    if not st or not f:
        raise HTTPException(404, "Finding or action not found")
    f.status, f.resolved_by, f.resolved_at = st, admin.id, utcnow()
    audit(db, admin.id, "OPS_FINDING_" + st, "ops_finding", fid)
    await db.commit()
    return _f(f)


@admin_router.post("/scan")
async def scan_now(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    new = await ops_manager.scan(db)
    await db.commit()
    return {"new": [_f(f) for f in new]}


@admin_router.get("/report")
async def report(hours: int = 24, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Live management report for the last `hours` (1..720)."""
    r = await ops_manager.build_report(db, max(1, min(hours, 720)))
    return {"start": r["start"].isoformat(), "end": r["end"].isoformat(), "metrics": r["metrics"], "text": r["text"]}


@admin_router.get("/reports")
async def saved_reports(limit: int = 30, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(OpsReport).order_by(OpsReport.id.desc()).limit(min(limit, 100)))).scalars().all()
    return {"items": [{"id": r.id, "period_end": r.period_end.isoformat(), "metrics": r.metrics, "text": r.text_ar} for r in rows]}

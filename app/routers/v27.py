"""v27: growth & revenue dashboard + investor exports (admin)."""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import admin_user, request_lang
from ..models import User
from ..services import growth, growth_export

admin_router = APIRouter(prefix="/api/admin/growth", tags=["v27-admin"])
PERIODS = {7, 30, 90, 365}


def _days(days: int) -> int:
    if days not in PERIODS:
        raise HTTPException(422, {"code": "INVALID_PERIOD", "allowed": sorted(PERIODS)})
    return days


@admin_router.get("")
async def growth_report(request: Request, days: int = Query(30), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    return await growth.compute(db, _days(days), request_lang(request))


@admin_router.get("/export")
async def growth_export_file(format: str = Query("pdf", pattern="^(pdf|xlsx)$"), days: int = Query(30), lang: str = Query("ar", pattern="^(ar|en)$"),
                             admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    g = await growth.compute(db, _days(days), lang)
    name = f"wasaly-growth-{g['from']}_{g['to']}.{format}"
    if format == "xlsx":
        body, mt = growth_export.to_xlsx(g, lang), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        body, mt = growth_export.to_pdf(g, lang), "application/pdf"
    return Response(body, media_type=mt, headers={"Content-Disposition": f'attachment; filename="{name}"'})

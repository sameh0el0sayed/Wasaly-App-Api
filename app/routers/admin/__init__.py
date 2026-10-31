"""Admin API, split by area. `router` aggregates every part under /api/admin."""
from fastapi import APIRouter

from . import areas, dashboard, drivers, orders, reports, settings, support, users

router = APIRouter()
for part in (dashboard, users, drivers, orders, areas, settings, support, reports):
    router.include_router(part.router)

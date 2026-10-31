import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .config import settings
from .constants import AccountStatus, Role
from .database import Base, SessionLocal, engine
from .models import User, utcnow
from .routers import (
    account_deletion,
    activity,
    admin,
    airports,
    auth,
    banners,
    driver,
    orders,
    payments,
    places,
    profile,
    ratings,
    subscriptions,
)
from .security import hash_password, normalize_phone
from .services.activity import sweep as activity_sweep
from .services.dispatch import enforce_driver_documents, expire_offers_and_orders
from .services.subscriptions import sweep as subscriptions_sweep

log = logging.getLogger("app")


def run_migrations() -> None:
    """`alembic upgrade head` (runs in a thread because alembic/env.py starts its own event loop)."""
    from alembic.config import Config

    from alembic import command
    command.upgrade(Config(str(Path(__file__).resolve().parent.parent / "alembic.ini")), "head")


async def bootstrap() -> None:
    """Schema (Alembic in production, create_all for dev/tests) and the first super admin."""
    if settings.app_env == "production":
        await asyncio.to_thread(run_migrations)
    else:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    from .seed import assert_safe_environment, seed_demo
    from .services.files import ensure_storage
    assert_safe_environment()
    await ensure_storage()
    async with SessionLocal() as db:
        phone = normalize_phone(settings.first_admin_phone)
        if not (await db.execute(select(User.id).where(User.phone == phone))).first():
            db.add(User(full_name=settings.first_admin_name, phone=phone, role=Role.SUPER_ADMIN,
                        password_hash=hash_password(settings.first_admin_password),
                        account_status=AccountStatus.APPROVED, approved_at=utcnow(),
                        must_change_password=settings.first_admin_must_change_password))
            await db.commit()
            log.warning("First super admin created: %s", phone)
        await seed_demo(db)


async def background_loop() -> None:
    last_warn = 0.0
    while True:
        try:
            async with SessionLocal() as db:
                await expire_offers_and_orders(db)
                warn = time.time() - last_warn > 3600
                await enforce_driver_documents(db, warn=warn)
                await activity_sweep(db)
                if warn:  # hourly
                    await subscriptions_sweep(db)
                await db.commit()
                if warn:
                    last_warn = time.time()
        except Exception:  # noqa
            log.exception("background loop error")
        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await bootstrap()
    task = asyncio.create_task(background_loop()) if settings.run_background_tasks else None
    yield
    if task:
        task.cancel()


app = FastAPI(title="Bahrain Transport & Delivery API", version="1.0.0", lifespan=lifespan,
              description="Backend for customer app, driver app and admin dashboard. No OTP. Driver LIVE mode shares location only with consent. "
                          "All prices, times and permissions are enforced server-side.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errs = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": {"code": "VALIDATION_ERROR", "errors": errs}})


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok", "server_time": utcnow().isoformat()}


for r in (auth.router, profile.router, orders.router, driver.router, payments.webhook_router, payments.router,
          payments.admin_router, subscriptions.router, subscriptions.admin_router, subscriptions.driver_router,
          airports.router, banners.router, places.router, account_deletion.router, account_deletion.admin_router, activity.router, activity.admin_router, ratings.router, admin.router):
    app.include_router(r)

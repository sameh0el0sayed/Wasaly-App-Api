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
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .constants import AccountStatus, Role
from .database import Base, SessionLocal, engine
from .i18n import localize_detail, parse_accept_language
from .models import User, utcnow
from .routers import (
    account_deletion,
    activity,
    admin,
    airports,
    app_errors,
    auth,
    banners,
    chat,
    corporate,
    corrections,
    driver,
    merchant_api,
    ops,
    orders,
    payments,
    places,
    profile,
    promo,
    ratings,
    services_v30,
    stream,
    subscriptions,
    v11,
    v14,
    v15,
    v16,
    v20,
    v21,
    v24,
    v27,
    v28,
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
    # Database migrations are managed explicitly during deployment.
    # Do not run Alembic automatically against an existing production database.
    if settings.app_env != "production":
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
                if warn:  # hourly checks; each runs once a day
                    from .routers.v14 import cleanup_events
                    from .services.license import daily_job
                    await daily_job(db)
                    await cleanup_events(db)
                    from .routers.v20 import monthly_job
                    await monthly_job(db)
                    from .services.fraud import scan as fraud_scan
                    await fraud_scan(db)
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
    from .routers.v14 import seed_translations
    await seed_translations()
    from .services.catalog import seed_catalog
    try:
        await seed_catalog()
    except Exception:  # noqa: BLE001 - tables may not exist yet during a first migration
        log.exception("catalog seed skipped")
    tasks = []
    if settings.run_background_tasks:
        log.info("Background tasks enabled; AI review interval_seconds=%s", settings.ai_review_interval_seconds)
        tasks.append(asyncio.create_task(background_loop()))
        from .services.ai_review import review_loop
        tasks.append(asyncio.create_task(review_loop()))
        from .services.merchant import webhook_loop
        tasks.append(asyncio.create_task(webhook_loop()))
        from .services.ops_manager import ops_loop
        tasks.append(asyncio.create_task(ops_loop()))
    else:
        log.warning("Background tasks disabled; AI review worker will not start")
    from .services import map_matching
    await map_matching.start()  # no-op unless MAP_MATCHING_ENABLED=true
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    from .services import cache, map_matching
    await map_matching.stop()
    await cache.close()


app = FastAPI(title="Bahrain Transport & Delivery API", version="1.0.0", lifespan=lifespan, root_path="/wasaly-api",
              description="Backend for customer app, driver app and admin dashboard. New registrations use WhatsApp reverse-OTP; the backend does not send OTP messages. Driver LIVE mode shares location only with consent. "
                          "All prices, times and permissions are enforced server-side.")
app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(request: Request, exc: StarletteHTTPException):
    """Same body as FastAPI's default, with message/note translated when the app sends Accept-Language: en."""
    lang = parse_accept_language(request.headers.get("accept-language")) or "ar"
    return JSONResponse({"detail": localize_detail(exc.detail, lang)}, status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError):
    errs = [{"field": ".".join(str(x) for x in e["loc"][1:]), "message": e["msg"]} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": {"code": "VALIDATION_ERROR", "errors": errs}})


@app.get("/health", tags=["system"])
async def health():
    from .services import cache
    return {"status": "ok", "server_time": utcnow().isoformat(), "redis": await cache.ping()}


_REQ = {"total": 0, "errors_5xx": 0, "slow_over_1s": 0, "latency_ms_sum": 0}  # in-process counters (reset on restart)


@app.get("/health/details", tags=["system"])
async def health_details():
    """Operational snapshot for monitoring/alerts: DB + Redis + OSRM latency, map-matching queue, request counters. No personal data."""
    from sqlalchemy import text

    from .database import SessionLocal
    from .services import cache, map_matching, osrm
    out: dict = {"status": "ok", "server_time": utcnow().isoformat(), "requests": {**_REQ}}
    t0 = time.time()
    try:
        async with SessionLocal() as s:
            await s.execute(text("SELECT 1"))
        out["db_ms"] = round((time.time() - t0) * 1000, 1)
    except Exception:  # noqa: BLE001
        out["status"], out["db_ms"] = "degraded", None
    t0 = time.time()
    out["redis"] = await cache.ping()
    out["redis_ms"] = round((time.time() - t0) * 1000, 1)
    if settings.map_matching_enabled:
        t0 = time.time()
        ok = await osrm.OsrmClient(timeout=2).health()
        out["osrm"] = {"ok": ok, "ms": round((time.time() - t0) * 1000, 1), "queue": map_matching.queue_stats()}
        if not ok:
            out["status"] = "degraded"  # the API stays usable: raw GPS fallback
    return out


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """Correlation id on every response (X-Request-ID); no bodies, tokens or coordinates are logged."""
    import uuid
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t0 = time.time()
    response = await call_next(request)
    response.headers["X-Request-ID"] = rid
    ms = round((time.time() - t0) * 1000)
    _REQ["total"] += 1
    _REQ["latency_ms_sum"] += ms
    _REQ["errors_5xx"] += response.status_code >= 500
    _REQ["slow_over_1s"] += ms > 1000
    if ms > 1000 or response.status_code >= 500:
        log.warning("slow/error request id=%s %s %s -> %s in %d ms", rid, request.method, request.url.path, response.status_code, ms)
    return response


for r in (auth.router, auth.whatsapp_webhook_router, app_errors.router, app_errors.admin_router, profile.router, corrections.router, chat.router, orders.router, driver.router, payments.webhook_router, payments.router,
          payments.admin_router, subscriptions.router, subscriptions.admin_router, subscriptions.driver_router,
          airports.router, promo.router, stream.router, banners.router, places.router, account_deletion.router, account_deletion.admin_router, activity.router, activity.admin_router, ratings.router, v11.router, v11.admin_router, v11.public_router, v14.router, v14.admin_router, v15.router, v15.admin_router, v16.router, v20.admin_router, v21.admin_router, v24.admin_router, v27.admin_router, v28.router, merchant_api.router, merchant_api.admin_router, services_v30.router, services_v30.admin_router, corporate.router, corporate.admin_router, ops.admin_router, admin.router):
    app.include_router(r)

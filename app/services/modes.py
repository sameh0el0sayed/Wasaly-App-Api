"""System modes pushed by PCC (PUT /control/v1/mode). Stored in the database (app_settings.system_mode) so they survive restarts,
redeploys and replicas; every request reads them through a 5 s cache.

  normal | stopped | maintenance | payments_frozen | emergency
Money paths (MONEY_PATHS) answer 423 PAYMENTS_FROZEN in payments_frozen and emergency. In emergency every other write answers
503 EMERGENCY_READ_ONLY, end-user tokens are revoked, end-user sign-in is refused and background jobs are paused.
Keep MONEY_PATHS complete: tests/test_modes.py walks every route and fails when a money-looking write is not covered.
"""
import re
import time

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from ..constants import Role
from ..database import SessionLocal

MODES = ("normal", "stopped", "maintenance", "payments_frozen", "emergency")
ALIASES = {"disabled": "stopped", "stop": "stopped", "off": "stopped", "payments_off": "payments_frozen", "freeze": "payments_frozen",
           "frozen": "payments_frozen", "lockdown": "emergency", "kill": "emergency"}
_C: dict = {"at": 0.0, "mode": "normal", "msg": ""}


def normalize(raw) -> str | None:
    m = str(raw or "").strip().lower().replace("-", "_")
    m = ALIASES.get(m, m)
    return m if m in MODES else None


async def current() -> tuple[str, str]:
    """(mode, message); cached 5 s, keeps the last known value when the database cannot be read."""
    if time.time() - _C["at"] > 5:
        try:
            from . import settings_service
            async with SessionLocal() as db:
                s = await settings_service.get_all(db)
            _C.update(mode=normalize(s.get("system_mode")) or "normal", msg=s.get("maintenance_message") or "")
        except Exception:  # noqa: BLE001
            pass
        _C["at"] = time.time()
    return _C["mode"], _C["msg"]


def invalidate() -> None:
    _C["at"] = 0.0


# Every endpoint that takes or moves money. Provider webhooks of payments already started are NOT here on purpose.
MONEY_PATHS = [re.compile(p) for p in (
    r"^/api/payments/[^/]+/(tap|benefit|benefitpay)$",             # customer pays: card/Tap, Benefit, BenefitPay
    r"^/api/admin/payments/[^/]+/confirm$",                        # cash / bank confirmation
    r"^/api/admin/payouts/[^/]+/(retry|mark-transferred)$",        # driver payouts
    r"^/api/subscriptions$", r"^/api/subscriptions/[^/]+/renew$",  # subscription purchase / renewal
    r"^/api/admin/partners/statements/[^/]+/(pay|pay-tap|pay-tap/retry)$",
    r"^/api/admin/users/[^/]+/wallet$",                            # wallet credit/debit
    r"^/api/admin/drivers/[^/]+/debt/settle$",
)]
# Money-looking writes that deliberately stay open (documented; the route-walk test knows them)
MONEY_ALLOW = {"/api/payments/tap/webhook", "/api/admin/payments/{payment_id}/reject", "/api/driver/payout-account",
               "/api/subscriptions/{sub_id}/cancel", "/api/admin/subscriptions/{sub_id}/quote", "/api/admin/subscriptions/{sub_id}/reject",
               "/api/admin/subscriptions/{sub_id}/pause", "/api/admin/subscriptions/{sub_id}/resume", "/api/admin/subscriptions/{sub_id}/change-driver",
               "/api/driver/subscription-trips/{trip_id}/complete", "/api/admin/promo-codes", "/api/admin/promo-codes/{promo_id}",
               "/api/admin/partners/statements/generate"}
_OPEN = ("/control/v1", "/health")
_STOP_OPEN = ("/api/admin", "/api/v1/admin", "/api/auth", "/auth/phoenix-id", "/api/app-config", "/docs", "/openapi.json")
_EM_OPEN = ("/api/auth/login", "/api/auth/phoenix-id/", "/auth/phoenix-id/", "/api/auth/token/refresh", "/api/auth/passkey/login/", "/api/auth/logout")


def is_money(method: str, path: str) -> bool:
    return method in ("POST", "PUT", "PATCH", "DELETE") and any(p.match(path) for p in MONEY_PATHS)


def verdict(mode: str, method: str, path: str, msg: str):
    """A JSONResponse that refuses the request in this mode, or None to let it through."""
    if mode == "normal" or method == "OPTIONS" or path.startswith(_OPEN):
        return None
    if mode in ("payments_frozen", "emergency") and is_money(method, path):
        return JSONResponse({"detail": msg or "الدفع متوقف مؤقتًا", "code": "PAYMENTS_FROZEN"}, status_code=423)
    if mode == "stopped" and not path.startswith(_STOP_OPEN):
        return JSONResponse({"detail": msg or "الخدمة متوقفة", "code": "SERVICE_STOPPED", "stopped": True}, status_code=503)
    if mode == "emergency" and method not in ("GET", "HEAD") and not path.startswith(_EM_OPEN):
        return JSONResponse({"detail": msg or "النظام في وضع الطوارئ (قراءة فقط)", "code": "EMERGENCY_READ_ONLY"}, status_code=503)
    return None


async def frozen() -> bool:
    return (await current())[0] in ("payments_frozen", "emergency")


async def emergency() -> bool:
    return (await current())[0] == "emergency"


async def deny_end_user(user) -> None:
    """Emergency: only admins may hold a session or sign in."""
    if user.role not in Role.ADMINS and await emergency():
        raise HTTPException(403, {"code": "EMERGENCY_LOCKDOWN", "message": "النظام في وضع الطوارئ"})

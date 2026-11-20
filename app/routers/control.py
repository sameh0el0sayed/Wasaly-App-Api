"""Phoenix Command & Control (PCC) contract: everything under /control/v1, signed with HMAC (see services/command.py).
A thin layer over the existing admin functions; every mutation is audited with actor "phoenix-command"."""
import json
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..models import (
    AdminAuditLog,
    Complaint,
    DriverProfile,
    Order,
    OwnerConfig,
    Payment,
    Rating,
    Subscription,
    User,
    utcnow,
)
from ..schemas import ReasonIn, ReinstateIn, SettingsPatch
from ..security import encrypt
from ..services import command, settings_service
from ..services.audit import audit
from ..services.notifications import notify
from ..services.payments import finalize_paid
from .admin import orders as admin_orders
from .admin import settings as admin_settings
from .admin import users as admin_users

TZ = ZoneInfo("Asia/Bahrain")
PER_PAGE = 50


async def guard(request: Request) -> None:
    link = await command.get_link()
    if not link or not link["enabled"]:
        raise HTTPException(503, "control link not configured")
    body = await request.body()
    ip = (request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else None))
    why = command.verify_reason(link, request.method, request.url.path, body, request.headers.get("x-phoenix-key"),
                                request.headers.get("x-phoenix-timestamp"), request.headers.get("x-phoenix-signature"), ip)
    if why:
        command.log.warning("control 401 %s %s reason=%s ip=%s", request.method, request.url.path, why, ip)
        raise HTTPException(401, "Unauthorized")


router = APIRouter(prefix="/control/v1", tags=["control"], dependencies=[Depends(guard)])


async def actor_dep(db: AsyncSession = Depends(get_db)) -> User:
    return await command.actor(db)


# ---------------------------------------------------------------- helpers ----------
def m(x) -> float:
    return float(Decimal(str(x or 0)).quantize(Decimal("0.001"), ROUND_HALF_UP))


async def vat_rate(db) -> Decimal:
    o = await command.owner_all()
    try:
        return Decimal(o.get("tax_vat_rate") or (await settings_service.get_all(db))["tap_vat_percent"]) / 100
    except Exception:  # noqa: BLE001
        return Decimal("0.10")


def money_row(net, rate: Decimal) -> dict:
    n = Decimal(str(net or 0)).quantize(Decimal("0.001"), ROUND_HALF_UP)
    v = (n * rate).quantize(Decimal("0.001"), ROUND_HALF_UP)
    return {"net": m(n), "vat": m(v), "gross": m(n + v)}


def iso(d) -> str | None:
    return d.astimezone(UTC).isoformat() if d else None


def day_range(frm: str | None, to: str | None) -> tuple[datetime, datetime]:
    def p(s):
        try:
            return date.fromisoformat(s)
        except (TypeError, ValueError):
            raise HTTPException(422, "from/to must be YYYY-MM-DD")
    t = p(to) if to else datetime.now(TZ).date()
    f = p(frm) if frm else t - timedelta(days=29)
    return (datetime.combine(f, datetime.min.time(), TZ).astimezone(UTC),
            datetime.combine(t + timedelta(days=1), datetime.min.time(), TZ).astimezone(UTC))


def page_of(page: int) -> tuple[int, int]:
    page = max(1, page)
    return PER_PAGE, (page - 1) * PER_PAGE


def pcc_status(u: User) -> str:
    return {"APPROVED": "active", "SUSPENDED": "suspended", "BANNED": "banned", "DELETED": "cancelled", "REJECTED": "cancelled"}.get(u.account_status, "suspended")


STATUS_BACK = {"active": ["APPROVED"], "suspended": ["SUSPENDED", "PENDING_APPROVAL", "NEEDS_CORRECTION"], "banned": ["BANNED"], "cancelled": ["DELETED", "REJECTED"]}


def tenant_out(u: User) -> dict:
    return {"id": str(u.id), "name": u.full_name, "email": None, "phone": u.phone, "plan": "driver" if u.role == Role.DRIVER else "customer",
            "type": "driver" if u.role == Role.DRIVER else "customer", "status": pcc_status(u), "account_status": u.account_status,
            "created_at": iso(u.created_at)}


async def body_json(request: Request) -> dict:
    raw = await request.body()
    if not raw:
        return {}
    try:
        v = json.loads(raw)
    except ValueError:
        raise HTTPException(422, "invalid JSON")
    if not isinstance(v, dict):
        raise HTTPException(422, "JSON object expected")
    return v


def need(d: dict, k: str):
    v = d.get(k)
    if v in (None, ""):
        raise HTTPException(422, {"code": "FIELD_REQUIRED", "field": k})
    return v


def pcc_audit(db, a: User, action: str, etype: str, eid, reason: str | None = None, details: dict | None = None):
    audit(db, a.id, f"PCC_{action}", etype, eid, reason, {"via": "phoenix-command", **(details or {})})


# ---------------------------------------------------------------- health / metrics / revenue ----------
@router.get("/health")
async def health(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        await db.execute(text("SELECT 1"))
        dbs = "ok"
    except Exception:  # noqa: BLE001
        dbs = "down"
    s = await settings_service.get_all(db) if dbs == "ok" else {}
    from ..services import phoenix_id as pid
    sso = (await pid.cfg())["enabled"]
    return {"status": "ok" if dbs == "ok" else "degraded", "version": request.app.version, "maintenance": s.get("maintenance_mode") == "true", "db": dbs,
            "sso_enabled": sso, "phoenix_id_reachable": await pid.reachable() if sso else False}


@router.get("/metrics")
async def metrics(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params
    start, end = day_range(q.get("from"), q.get("to"))
    rate = await vat_rate(db)
    paid = (await db.execute(select(func.coalesce(func.sum(Payment.company_amount), 0), func.coalesce(func.sum(Payment.amount), 0))
                             .where(Payment.payment_status == "PAID", Payment.paid_at >= start, Payment.paid_at < end))).one()
    rev = money_row(paid[0], rate)
    orders = (await db.execute(select(func.count()).select_from(Order).where(Order.created_at >= start, Order.created_at < end))).scalar() or 0
    cancelled = (await db.execute(select(func.count()).select_from(Order).where(Order.created_at >= start, Order.created_at < end,
                                                                               Order.order_status == S.CANCELLED))).scalar() or 0
    active_c = (await db.execute(select(func.count(func.distinct(Order.customer_id))).where(Order.created_at >= start, Order.created_at < end))).scalar() or 0
    active_d = (await db.execute(select(func.count(func.distinct(Order.driver_id))).where(Order.created_at >= start, Order.created_at < end,
                                                                                          Order.driver_id.is_not(None)))).scalar() or 0
    signups = (await db.execute(select(func.count()).select_from(User).where(User.role.in_([Role.CUSTOMER, Role.DRIVER]),
                                                                            User.created_at >= start, User.created_at < end))).scalar() or 0
    failed = (await db.execute(select(func.count()).select_from(Payment).where(Payment.payment_status.in_(["FAILED", "REJECTED"]),
                                                                              Payment.updated_at >= start, Payment.updated_at < end))).scalar() or 0
    mrr = (await db.execute(select(func.coalesce(func.sum(Subscription.commission), 0)).where(Subscription.status == "ACTIVE"))).scalar()
    online = (await db.execute(select(func.count()).select_from(DriverProfile).where(DriverProfile.is_online.is_(True)))).scalar() or 0
    waits = (await db.execute(select(Order.accepted_at, Order.driver_arrived_at).where(
        Order.created_at >= start, Order.created_at < end, Order.accepted_at.is_not(None), Order.driver_arrived_at.is_not(None)))).all()
    avg_pick = round(sum((b - a).total_seconds() for a, b in waits) / len(waits) / 60, 1) if waits else 0
    return {"revenue_net": rev["net"], "vat": rev["vat"], "revenue_gross": rev["gross"], "active_customers": active_c, "active_tenants": active_d,
            "orders": orders, "mrr": m(mrr), "new_signups": signups, "failed_payments": failed,
            "extra": {"السائقون المتصلون": online, "رحلات الفترة": orders, "متوسط وقت وصول السائق (دقيقة)": avg_pick,
                      "نسبة الإلغاء %": round(cancelled * 100 / orders, 1) if orders else 0,
                      "إجمالي المدفوعات": m(paid[1])}}


@router.get("/revenue")
async def revenue(request: Request, db: AsyncSession = Depends(get_db)):
    q = request.query_params
    start, end = day_range(q.get("from"), q.get("to"))
    interval = q.get("interval", "day")
    if interval not in ("hour", "day", "month"):
        raise HTTPException(422, "interval must be hour|day|month")
    rate = await vat_rate(db)
    rows = (await db.execute(select(Payment.paid_at, Payment.company_amount).where(
        Payment.payment_status == "PAID", Payment.paid_at >= start, Payment.paid_at < end))).all()
    buckets: dict[datetime, Decimal] = defaultdict(Decimal)
    for at, amt in rows:
        loc = at.astimezone(TZ)
        k = {"hour": loc.replace(minute=0, second=0, microsecond=0), "day": loc.replace(hour=0, minute=0, second=0, microsecond=0),
             "month": loc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)}[interval]
        buckets[k] += Decimal(amt or 0)
    return {"series": [{"t": iso(k), **money_row(v, rate)} for k, v in sorted(buckets.items())]}


# ---------------------------------------------------------------- tenants (customers + drivers) ----------
@router.get("/tenants")
async def tenants(request: Request, q: str | None = None, status: str | None = None, type: str | None = None, page: int = 1,
                  db: AsyncSession = Depends(get_db)):
    limit, offset = page_of(page)
    roles = {"customer": [Role.CUSTOMER], "driver": [Role.DRIVER]}.get(type or "", [Role.CUSTOMER, Role.DRIVER])
    stmt = select(User).where(User.role.in_(roles))
    if status:
        if status not in STATUS_BACK:
            raise HTTPException(422, "bad status")
        stmt = stmt.where(User.account_status.in_(STATUS_BACK[status]))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(or_(User.full_name.ilike(like), User.phone.ilike(like)))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await db.execute(stmt.order_by(User.id.desc()).limit(limit).offset(offset))).scalars().all()
    return {"items": [tenant_out(u) for u in rows], "total": total}


async def _user(db, uid: int, role: str | None = None) -> User:
    u = await db.get(User, uid)
    if not u or u.role not in (Role.CUSTOMER, Role.DRIVER) or (role and u.role != role):
        raise HTTPException(404, "Not found")
    return u


@router.get("/tenants/{tid}")
async def tenant_detail(tid: int, db: AsyncSession = Depends(get_db)):
    u = await _user(db, tid)
    d = await admin_users.get_user(u.id, False, u, db)
    subs = (await db.execute(select(Subscription).where(Subscription.customer_id == u.id).order_by(Subscription.id.desc()).limit(10))).scalars().all()
    return {**tenant_out(u), "detail": d, "subscriptions": [sub_out(s_, None) for s_ in subs], "last_activity": iso(u.last_seen_at),
            "usage": {"orders": len(d.get("recent_orders", [])), "login_count": u.login_count, "app_open_count": u.app_open_count}}


async def _apply_status(db: AsyncSession, a: User, u: User, status: str, reason: str | None) -> dict:
    reason = (reason or "PCC").strip()
    if len(reason) < 2:
        reason = "PCC"
    if status == "active":
        if u.account_status == AccountStatus.BANNED:
            await admin_users.reinstate_banned(u.id, ReinstateIn(reason=reason, fee_received=False), a, db)
        else:
            await admin_users.reactivate(u.id, a, db)
    elif status == "suspended":
        await admin_users.suspend(u.id, ReasonIn(reason=reason), a, db)
    elif status == "banned":
        await admin_users._set_status(db, a, u, AccountStatus.BANNED, reason, "ACCOUNT_BANNED", "تم حظر الحساب", "تم حظر حسابك نهائيًا.")
        await db.commit()
    elif status == "cancelled":
        await admin_users._set_status(db, a, u, AccountStatus.REJECTED, reason, "ACCOUNT_REJECTED", "تم إلغاء الحساب", "تم إلغاء حسابك.")
        await db.commit()
    else:
        raise HTTPException(422, "status must be active|suspended|banned|cancelled")
    await db.refresh(u)
    return tenant_out(u)


@router.post("/tenants/{tid}/status")
async def tenant_status(tid: int, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    return await _apply_status(db, a, await _user(db, tid), str(need(b, "status")), b.get("reason"))


@router.post("/tenants/{tid}/extend")
async def tenant_extend(tid: int, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    """Customers: extends the paid period of their route subscriptions. Drivers have no period to extend."""
    b = await body_json(request)
    try:
        days = int(need(b, "days"))
    except (TypeError, ValueError):
        raise HTTPException(422, "days must be an integer")
    if not 1 <= days <= 365:
        raise HTTPException(422, "days must be 1..365")
    u = await _user(db, tid)
    if u.role != Role.CUSTOMER:
        raise HTTPException(409, {"code": "NOT_APPLICABLE", "message": "Drivers have no period to extend"})
    subs = (await db.execute(select(Subscription).where(Subscription.customer_id == u.id, Subscription.status.in_(["ACTIVE", "PAUSED", "EXPIRED"])))).scalars().all()
    if not subs:
        raise HTTPException(409, {"code": "NO_SUBSCRIPTION"})
    today = datetime.now(TZ).date()
    for s_ in subs:
        s_.paid_until = max(s_.paid_until or today, today) + timedelta(days=days)
        if s_.status == "EXPIRED":
            s_.status = "ACTIVE"
    pcc_audit(db, a, "TENANT_EXTENDED", "user", u.id, details={"days": days, "subscriptions": [s_.id for s_ in subs]})
    await db.commit()
    return {"extended": [{"id": str(s_.id), "current_period_end": s_.paid_until.isoformat()} for s_ in subs]}


@router.post("/tenants/{tid}/impersonate")
async def tenant_impersonate(tid: int):
    raise HTTPException(501, {"code": "IMPERSONATION_NOT_SUPPORTED", "message": "Wasaly does not issue one-time login links"})


# ---------------------------------------------------------------- plans (configuration, not products) ----------
PLAN_KEYS = {
    "TRIP_COMMISSION": ("trip-commission", "Trip commission", "عمولة الرحلات", ("commission_type", "commission_value", "commission_mode")),
    "ROUTE_SUBSCRIPTION": ("route-subscription", "Route subscription commission", "عمولة الاشتراكات", ("subscription_commission_value", "subscription_period_days")),
}


async def plans_view(db) -> list[dict]:
    s = await settings_service.get_all(db)
    out = []
    for code, (pid, name, name_ar, keys) in PLAN_KEYS.items():
        out.append({"id": pid, "code": code, "name": name, "name_ar": name_ar, "price_monthly": 0, "price_annual": 0, "currency": s["currency_code"],
                    "trial_days": 0, "limits": {k: s[k] for k in keys}, "features": [], "is_active": True})
    return out


@router.get("/plans")
async def plans(db: AsyncSession = Depends(get_db)):
    return {"items": await plans_view(db), "total": len(PLAN_KEYS)}


@router.post("/plans")
async def plans_create():
    raise HTTPException(501, {"code": "PLANS_ARE_FIXED", "message": "Wasaly plans are commission settings; edit them with PUT"})


@router.put("/plans/{pid}")
async def plans_update(pid: str, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    code = next((c for c, v in PLAN_KEYS.items() if pid in (v[0], c)), None)
    if not code:
        raise HTTPException(404, "Plan not found")
    limits = b.get("limits") or {}
    allowed = PLAN_KEYS[code][3]
    bad = set(limits) - set(allowed)
    if bad:
        raise HTTPException(422, {"code": "UNKNOWN_LIMITS", "keys": sorted(bad)})
    await _patch_settings(db, a, limits)
    return next(p for p in await plans_view(db) if p["code"] == code)


# ---------------------------------------------------------------- subscriptions ----------
SUB_STATUS = {"ACTIVE": "active", "PAUSED": "suspended", "EXPIRED": "past_due", "CANCELLED": "cancelled", "REJECTED": "cancelled", "REQUESTED": "trialing", "QUOTED": "trialing"}
SUB_BACK = {"active": ["ACTIVE"], "suspended": ["PAUSED"], "past_due": ["EXPIRED"], "cancelled": ["CANCELLED", "REJECTED"], "trialing": ["REQUESTED", "QUOTED"]}


def sub_out(s_: Subscription, name: str | None) -> dict:
    st = SUB_STATUS.get(s_.status, "suspended")
    if st == "active" and s_.paid_until and s_.paid_until < datetime.now(TZ).date():
        st = "past_due"
    return {"id": str(s_.id), "tenant_id": str(s_.customer_id), "tenant_name": name, "plan": "ROUTE_SUBSCRIPTION", "plan_name": f"اشتراك {s_.category}",
            "price": m(s_.customer_total or s_.monthly_price), "status": st, "current_period_end": s_.paid_until.isoformat() if s_.paid_until else None,
            "trial_ends_at": None, "created_at": iso(s_.created_at)}


@router.get("/subscriptions")
async def subscriptions(status: str | None = None, page: int = 1, db: AsyncSession = Depends(get_db)):
    limit, offset = page_of(page)
    stmt = select(Subscription, User.full_name).join(User, User.id == Subscription.customer_id)
    if status:
        if status not in SUB_BACK:
            raise HTTPException(422, "bad status")
        stmt = stmt.where(Subscription.status.in_(SUB_BACK[status]))
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await db.execute(stmt.order_by(Subscription.id.desc()).limit(limit).offset(offset))).all()
    return {"items": [sub_out(s_, n) for s_, n in rows], "total": total}


@router.post("/subscriptions/{sid}/{action}")
async def subscription_action(sid: int, action: str, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    if action == "change-plan":
        raise HTTPException(501, {"code": "PLANS_ARE_GLOBAL", "message": "Route subscriptions share one commission configuration"})
    target = {"activate": "ACTIVE", "suspend": "PAUSED", "cancel": "CANCELLED"}.get(action)
    if not target:
        raise HTTPException(404, "Unknown action")
    s_ = await db.get(Subscription, sid)
    if not s_:
        raise HTTPException(404, "Subscription not found")
    if action == "activate" and s_.status not in ("PAUSED", "EXPIRED"):
        raise HTTPException(409, {"code": "NOT_ACTIVATABLE", "status": s_.status})
    if s_.status in ("CANCELLED", "REJECTED"):
        raise HTTPException(409, {"code": "ALREADY_FINAL", "status": s_.status})
    old, s_.status = s_.status, target
    pcc_audit(db, a, f"SUBSCRIPTION_{action.upper()}", "subscription", s_.id, details={"from": old, "to": target})
    await notify(db, s_.customer_id, f"SUBSCRIPTION_{target}", "تحديث على اشتراكك", f"أصبحت حالة اشتراكك: {target}")
    await db.commit()
    return sub_out(s_, None)


# ---------------------------------------------------------------- payments / invoices ----------
PAY_STATUS = {"PAID": "paid", "UNPAID": "pending", "PENDING_PAYMENT": "pending", "PENDING_VERIFICATION": "awaiting_review", "FAILED": "failed", "REJECTED": "failed"}
PAY_BACK = {"paid": ["PAID"], "pending": ["UNPAID", "PENDING_PAYMENT"], "awaiting_review": ["PENDING_VERIFICATION"], "failed": ["FAILED", "REJECTED"], "refunded": []}


def pay_method(p: Payment) -> str | None:
    if p.payment_method == "TAP":
        return "tap"
    if p.payment_method == "BENEFITPAY":
        return "benefitpay"
    return "wallet" if Decimal(p.wallet_amount or 0) > 0 else None


def pay_out(p: Payment, name: str | None, rate: Decimal) -> dict:
    return {"id": str(p.id), "reference": p.transaction_reference or f"P{p.id}", "customer_name": name, "method": pay_method(p),
            **money_row(p.amount, rate), "currency": p.currency, "status": PAY_STATUS.get(p.payment_status, "pending"),
            "kind": p.kind.lower(), "order_id": p.order_id, "created_at": iso(p.created_at)}


@router.get("/payments")
async def payments(request: Request, status: str | None = None, page: int = 1, db: AsyncSession = Depends(get_db)):
    limit, offset = page_of(page)
    q = request.query_params
    start, end = day_range(q.get("from"), q.get("to")) if (q.get("from") or q.get("to")) else (None, None)
    stmt = select(Payment, User.full_name).join(User, User.id == Payment.customer_id)
    if status:
        if status not in PAY_BACK:
            raise HTTPException(422, "bad status")
        stmt = stmt.where(Payment.payment_status.in_(PAY_BACK[status] or ["__none__"]))
    if start:
        stmt = stmt.where(Payment.created_at >= start, Payment.created_at < end)
    total = (await db.execute(select(func.count()).select_from(stmt.subquery()))).scalar() or 0
    rows = (await db.execute(stmt.order_by(Payment.id.desc()).limit(limit).offset(offset))).all()
    rate = await vat_rate(db)
    return {"items": [pay_out(p, n, rate) for p, n in rows], "total": total}


@router.post("/payments/{pid}/{action}")
async def payment_action(pid: int, action: str, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    p = await db.get(Payment, pid)
    if not p:
        raise HTTPException(404, "Payment not found")
    if action == "refund":
        raise HTTPException(501, {"code": "REFUND_NOT_SUPPORTED", "message": "Refunds are handled manually in Wasaly"})
    if action == "approve":
        if p.payment_status != "PENDING_VERIFICATION":
            raise HTTPException(409, {"code": "NOT_AWAITING_CONFIRMATION", "status": p.payment_status})
        if not await finalize_paid(db, p, a.id):
            raise HTTPException(409, "Already paid")
        pcc_audit(db, a, "PAYMENT_APPROVED", "payment", p.id)
    elif action == "reject":
        reason = str(need(b, "reason"))
        if p.payment_status != "PENDING_VERIFICATION":
            raise HTTPException(409, {"code": "NOT_AWAITING_CONFIRMATION", "status": p.payment_status})
        p.payment_status, p.reject_reason, p.verified_by = "REJECTED", reason, a.id
        await notify(db, p.customer_id, "PAYMENT_REJECTED", "تم رفض إيصال الدفع", reason, p.order_id)
        pcc_audit(db, a, "PAYMENT_REJECTED", "payment", p.id, reason)
    elif action == "mark-paid":
        if p.payment_status == "PAID":
            raise HTTPException(409, "Already paid")
        if not await finalize_paid(db, p, a.id):
            raise HTTPException(409, "Already paid")
        pcc_audit(db, a, "PAYMENT_MARKED_PAID", "payment", p.id, b.get("reason"))
    else:
        raise HTTPException(404, "Unknown action")
    await db.commit()
    name = (await db.get(User, p.customer_id)).full_name
    return pay_out(p, name, await vat_rate(db))


@router.get("/invoices")
async def invoices():
    return {"items": [], "total": 0}  # Wasaly has no invoicing concept: fares are paid per trip


# ---------------------------------------------------------------- settings / unified config / maintenance ----------
_MASK_HINTS = ("secret", "password", "token", "api_key", "private")


def _mask(k: str, v: str) -> str:
    if v and any(h in k for h in _MASK_HINTS):
        return "••••" + v[-4:]
    return v


@router.get("/settings")
async def get_settings(db: AsyncSession = Depends(get_db)):
    return {k: _mask(k, v) for k, v in (await settings_service.get_all(db)).items()}


async def _patch_settings(db, a: User, changes: dict) -> None:
    defaults = settings_service.DEFAULTS
    unknown = [k for k in changes if k not in defaults]
    if unknown:
        raise HTTPException(422, {"code": "UNKNOWN_SETTINGS", "keys": unknown})
    typed = {k: v for k, v in changes.items() if k in SettingsPatch.model_fields}
    if typed:
        try:
            body = SettingsPatch(**typed, reason="PCC")  # type: ignore[call-arg]
        except Exception as e:  # noqa: BLE001
            raise HTTPException(422, {"code": "VALIDATION_ERROR", "message": str(e)[:300]})
        await admin_settings.patch_settings(body, a, db)
    for k, v in changes.items():
        if k in typed:
            continue
        d = defaults[k]
        if d in ("true", "false"):
            if not isinstance(v, bool) and str(v).lower() not in ("true", "false"):
                raise HTTPException(422, {"code": "BOOLEAN_EXPECTED", "key": k})
            val = str(v).lower()
        elif isinstance(v, (dict, list)):
            val = json.dumps(v, ensure_ascii=False)
        else:
            val = str(v)
            if d.replace(".", "", 1).replace("-", "", 1).isdigit():
                try:
                    Decimal(val)
                except Exception:  # noqa: BLE001
                    raise HTTPException(422, {"code": "NUMBER_EXPECTED", "key": k})
        await settings_service.set_value(db, k, val, a.id)
        audit(db, a.id, "SETTINGS_CHANGED", "settings", k, "PCC", {"via": "phoenix-command", "after": {k: val}})
    await db.commit()


@router.patch("/settings")
async def patch_settings(request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    if not b:
        raise HTTPException(422, "empty body")
    masked = [k for k, v in b.items() if isinstance(v, str) and v.startswith("••••")]
    for k in masked:
        b.pop(k)  # a masked echo means "unchanged"
    await _patch_settings(db, a, b)
    return {"applied": list(b)}


OWNER_MAP = {
    "payment": {"tap_enabled": ("setting", "tap_enabled"), "tap_secret_key": ("secret", None), "tap_public_key": ("owner", None),
                "tap_merchant_id": ("owner", None), "tap_webhook_secret": ("secret", None), "benefitpay_enabled": ("setting", "benefitpay_enabled"),
                "benefitpay_account_name": ("setting", "benefitpay_account_name"), "benefitpay_account_number": ("setting", "benefitpay_account_number"),
                "bank_name": ("owner", None), "bank_account_name": ("owner", None), "iban": ("owner", None), "currency": ("owner", None)},
    "ai": {"provider": ("owner", "ai_provider"), "gemini_api_key": ("secret", None), "model": ("owner", "gemini_model"),
           "fallback_models": ("owner", "gemini_fallback_models"), "timeout_seconds": ("owner", "gemini_timeout_seconds"), "enabled": ("owner", "ai_enabled")},
    "tax": {"vat_rate": ("setting", "tap_vat_percent"), "vat_mode": ("owner", None), "vat_number": ("owner", None)},
    "company": {"name": ("owner", "company_name"), "cr_number": ("owner", None), "support_phone": ("setting", "support_phone"),
                "support_email": ("setting", "support_email"), "support_whatsapp": ("setting", "support_whatsapp")},
}


@router.put("/unified-config")
async def unified_config(request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    applied: list[str] = []
    setting_changes: dict = {}
    owner_rows: dict[str, tuple[str, bool]] = {}
    for group, fields in OWNER_MAP.items():
        sect = b.get(group)
        if sect is None:
            continue
        if not isinstance(sect, dict):
            raise HTTPException(422, {"code": "OBJECT_EXPECTED", "group": group})
        for fld, val in sect.items():
            if fld not in fields:
                raise HTTPException(422, {"code": "UNKNOWN_FIELD", "field": f"{group}.{fld}"})
            if val is None or val == "":
                continue  # empty string = keep current
            kind, key = fields[fld]
            if isinstance(val, str) and val.startswith("••••"):
                continue
            if kind == "setting":
                setting_changes[key] = val
            else:
                okey = key or (fld if group in ("payment",) else f"{group}_{fld}")
                owner_rows[okey] = (str(val).lower() if isinstance(val, bool) else str(val), kind == "secret")
            applied.append(f"{group}.{fld}")
    if setting_changes:
        await _patch_settings(db, a, setting_changes)
    for k, (v, secret) in owner_rows.items():
        row = await db.get(OwnerConfig, k)
        enc = encrypt(v) if secret else v
        if row:
            row.value, row.is_secret = enc, secret
        else:
            db.add(OwnerConfig(key=k, value=enc, is_secret=secret))
    pcc_audit(db, a, "UNIFIED_CONFIG", "settings", "unified", details={"applied": applied})  # field names only, never values
    await db.commit()
    command.invalidate()
    return {"applied": applied}


@router.post("/maintenance")
async def maintenance(request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    if not isinstance(b.get("enabled"), bool):
        raise HTTPException(422, {"code": "FIELD_REQUIRED", "field": "enabled"})
    await settings_service.set_value(db, "maintenance_mode", "true" if b["enabled"] else "false", a.id)
    if "message" in b:
        await settings_service.set_value(db, "maintenance_message", str(b.get("message") or "")[:300], a.id)
    pcc_audit(db, a, "MAINTENANCE", "settings", "maintenance", details={"enabled": b["enabled"]})
    await db.commit()
    command.maint["at"] = 0.0
    return {"enabled": b["enabled"], "message": (await settings_service.get_all(db))["maintenance_message"]}


# ---------------------------------------------------------------- logs / audit / tickets ----------
@router.get("/logs")
async def logs(level: str = "error", limit: int = 200):
    order = {"warning": 0, "error": 1, "critical": 2}
    floor = order.get(level.lower(), 1)
    items = [x for x in reversed(command.ring.buf) if order.get(x["level"], 0) >= floor][: max(1, min(limit, 1000))]
    return {"items": items, "total": len(items)}


@router.get("/audit-logs")
async def audit_logs(page: int = 1, db: AsyncSession = Depends(get_db)):
    limit, offset = page_of(page)
    total = (await db.execute(select(func.count()).select_from(AdminAuditLog))).scalar() or 0
    rows = (await db.execute(select(AdminAuditLog, User.full_name).outerjoin(User, User.id == AdminAuditLog.admin_id)
                             .order_by(AdminAuditLog.id.desc()).limit(limit).offset(offset))).all()
    return {"items": [{"created_at": iso(r.created_at), "actor": n or ("system" if r.admin_id is None else f"admin#{r.admin_id}"), "action": r.action,
                       "target": f"{r.entity_type}:{r.entity_id}", "reason": r.reason} for r, n in rows], "total": total}


@router.get("/tickets")
async def tickets(status: str = "open", db: AsyncSession = Depends(get_db)):
    stmt = select(Complaint, User.full_name).outerjoin(User, User.id == Complaint.user_id)
    stmt = stmt.where(Complaint.status == "OPEN") if status == "open" else stmt.where(Complaint.status != "OPEN")
    rows = (await db.execute(stmt.order_by(Complaint.id.desc()).limit(200))).all()
    return {"items": [{"id": str(c.id), "subject": c.subject, "message": c.message, "tenant_name": n, "priority": "high" if c.type in ("DISPUTE", "VEHICLE_BREAKDOWN") else "normal",
                       "status": "open" if c.status == "OPEN" else "closed", "created_at": iso(c.created_at)} for c, n in rows], "total": len(rows)}


@router.post("/tickets/{tid}/reply")
async def ticket_reply(tid: int, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    msg = str(need(b, "message"))
    c = await db.get(Complaint, tid)
    if not c:
        raise HTTPException(404, "Ticket not found")
    c.status, c.admin_response, c.resolved_by, c.resolved_at = "RESOLVED", msg, a.id, utcnow()
    if c.user_id:
        await notify(db, c.user_id, "COMPLAINT_RESOLVED", "رد الإدارة", msg, c.order_id)
    pcc_audit(db, a, "TICKET_REPLY", "complaint", c.id, details={"by": b.get("by")})
    await db.commit()
    return {"id": str(c.id), "status": "closed"}


# ---------------------------------------------------------------- Wasaly: radar / drivers / trips ----------
_radar: dict = {"at": 0.0, "v": None}


@router.get("/radar")
async def radar(db: AsyncSession = Depends(get_db)):
    if _radar["v"] is not None and time.time() - _radar["at"] < 2:
        return _radar["v"]
    now = utcnow()
    since = now - timedelta(hours=24)
    users = (await db.execute(select(User).join(DriverProfile, DriverProfile.user_id == User.id)
                              .where(or_(DriverProfile.is_online.is_(True), User.last_seen_at >= since))
                              .options(selectinload(User.vehicle)).limit(2000))).scalars().unique().all()
    busy = set((await db.execute(select(Order.driver_id).where(Order.order_status.in_(S.DRIVER_BUSY), Order.driver_id.is_not(None)))).scalars())
    ids = [u.id for u in users]
    ratings = dict((await db.execute(select(Rating.ratee_id, func.avg(Rating.stars)).where(Rating.ratee_id.in_(ids)).group_by(Rating.ratee_id))).all()) if ids else {}
    drivers, stats = [], {"online": 0, "busy": 0, "offline": 0}
    for u in users:
        p = u.driver_profile
        st = "busy" if u.id in busy else ("online" if p.is_online else "offline")
        stats[st] += 1
        v = u.vehicle
        drivers.append({"id": str(u.id), "name": u.full_name, "phone": u.phone, "lat": p.last_lat, "lng": p.last_lng, "heading": p.last_heading or 0, "status": st,
                        "account_status": {"APPROVED": "active", "SUSPENDED": "suspended", "BANNED": "banned"}.get(u.account_status, "suspended"),
                        "vehicle": f"{v.make} {v.model} {v.plate_number}" if v else "", "rating": round(float(ratings.get(u.id) or 0), 2),
                        "updated_at": iso(p.last_location_at or u.last_seen_at)})
    active = (await db.execute(select(Order).where(Order.order_status.in_(S.DRIVER_BUSY | S.SEARCHING)).options(selectinload(Order.customer)).limit(500))).scalars().all()
    trips = [{"id": str(o.id), "status": o.order_status, "driver_id": str(o.driver_id) if o.driver_id else None, "customer_name": o.customer.full_name if o.customer else None,
              "pickup": {"lat": o.pickup_latitude, "lng": o.pickup_longitude}, "dropoff": {"lat": o.destination_latitude, "lng": o.destination_longitude},
              "price": m(o.customer_total or o.final_price or o.agreed_price), "created_at": iso(o.created_at)} for o in active]
    recent = (await db.execute(select(Order.pickup_latitude, Order.pickup_longitude).where(
        Order.pickup_latitude.is_not(None), or_(Order.order_status.in_(S.SEARCHING), Order.created_at >= now - timedelta(minutes=30))).limit(1000))).all()
    cells: dict[tuple[float, float], int] = defaultdict(int)
    for la, lo in recent:
        cells[(round(la, 2), round(lo, 2))] += 1  # ~1 km cells
    out = {"drivers": drivers, "trips": trips, "demand": [{"lat": k[0], "lng": k[1], "weight": w} for k, w in cells.items()],
           "stats": {**stats, "active_trips": sum(1 for o in active if o.order_status in S.DRIVER_BUSY), "waiting_orders": sum(1 for o in active if o.order_status in S.SEARCHING)}}
    _radar.update(at=time.time(), v=out)
    return out


@router.get("/drivers/{did}")
async def driver_detail(did: int, db: AsyncSession = Depends(get_db)):
    u = await _user(db, did, Role.DRIVER)
    d = await admin_users.get_user(u.id, False, u, db)
    avg = (await db.execute(select(func.avg(Rating.stars), func.count()).where(Rating.ratee_id == u.id))).one()
    return {**tenant_out(u), "profile": d, "wallet_balance": m(u.wallet_balance), "debt_balance": m(u.driver_profile.debt_balance if u.driver_profile else 0),
            "rating": {"average": round(float(avg[0] or 0), 2), "count": avg[1]}, "last_trips": d.get("recent_orders", [])[:20]}


@router.post("/drivers/{did}/status")
async def driver_status(did: int, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    st = str(need(b, "status"))
    if st not in ("active", "suspended", "banned"):
        raise HTTPException(422, "status must be active|suspended|banned")
    return await _apply_status(db, a, await _user(db, did, Role.DRIVER), st, b.get("reason"))


@router.get("/trips/{oid}")
async def trip_detail(oid: int, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    return await admin_orders.get_order(oid, a, db)


@router.post("/trips/{oid}/cancel")
async def trip_cancel(oid: int, request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    b = await body_json(request)
    reason = str(b.get("reason") or "PCC").strip()
    return await admin_orders.admin_cancel(oid, ReasonIn(reason=reason if len(reason) >= 2 else "PCC"), a, db)


# ---------------------------------------------------------------- Phoenix ID migration export ----------
@router.get("/users/export")
async def users_export(a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    """One-off export for 'User migration' in PCC. Contains password hashes (argon2id): reachable only with the HMAC key."""
    role_of = {Role.CUSTOMER: "customer", Role.DRIVER: "driver", Role.ADMIN: "admin", Role.SUPER_ADMIN: "admin"}
    rows = (await db.execute(select(User).where(User.role.in_(list(role_of)), User.phone.is_not(None), User.account_status != "DELETED").order_by(User.id))).scalars().all()
    pcc_audit(db, a, "USERS_EXPORTED", "user", "all", details={"count": len(rows)})
    await db.commit()
    return [{"system_user_id": str(u.id), "phone": u.phone, "email": None, "email_verified": False, "name": u.full_name, "name_ar": u.full_name,
             "password_hash": u.password_hash, "hash_algo": "argon2id", "roles": [role_of[u.role]], "tenant_ids": []} for u in rows]


# ---------------------------------------------------------------- Phoenix ID SSO switch (PCC pushes settings server-to-server) ----------
@router.get("/sso")
async def sso_get():
    from ..services import phoenix_id as pid
    c = await pid.cfg()
    sec = c["client_secret"]
    allowed, why = await pid.local_login_state()
    return {"maintenance_login": (await command.owner_all()).get("phoenix_id_maintenance_login", "false") == "true",
            "local_login_allowed": allowed, "local_login_reason": why, "enabled": c["enabled"], "client_id": c["client_id"], "issuer": c["issuer"], "secret_set": bool(sec), "secret_last4": sec[-4:] if sec else None,
            "phoenix_id_reachable": await pid.reachable(), "login_button_visible": c["enabled"]}


@router.put("/sso")
async def sso_put(request: Request, a: User = Depends(actor_dep), db: AsyncSession = Depends(get_db)):
    from ..services import phoenix_id as pid
    from .phoenix_id import _put
    b = await body_json(request)
    if "enabled" in b and not isinstance(b["enabled"], bool):
        raise HTTPException(422, {"code": "BOOLEAN_EXPECTED", "field": "enabled"})
    changed = []
    if "enabled" in b:
        await _put(db, "phoenix_id_enabled", "true" if b["enabled"] else "false")
        changed.append("enabled")
    for k, key in (("client_id", "phoenix_id_client_id"), ("issuer", "phoenix_id_issuer")):
        if b.get(k):
            await _put(db, key, str(b[k]).strip().rstrip("/"))
            changed.append(k)
    if "maintenance_login" in b:
        if not isinstance(b["maintenance_login"], bool):
            raise HTTPException(422, {"code": "BOOLEAN_EXPECTED", "field": "maintenance_login"})
        await _put(db, "phoenix_id_maintenance_login", "true" if b["maintenance_login"] else "false")
        changed.append(f"maintenance_login={'on' if b['maintenance_login'] else 'off'}")
    if b.get("client_secret"):  # absent or empty = keep current
        await _put(db, "phoenix_id_client_secret", str(b["client_secret"]).strip(), secret=True)
        changed.append("client_secret")
    pcc_audit(db, a, "SSO_SETTINGS", "settings", "phoenix_id", details={"changed": changed, "pcc_actor": request.headers.get("x-phoenix-actor", "")[:100]})
    await db.commit()
    command.invalidate()
    pid._disc["at"] = pid._jwks["at"] = pid._reach["at"] = 0.0
    return {"ok": True}

"""v20: company partners (monthly percentage of net profit) + admin wallet top-ups for customers and drivers."""
import re
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..deps import admin_user, super_admin_user
from ..models import Partner, PartnerPayout, PartnerStatement, Payment, User, WalletEntry, utcnow
from ..serializers import dt
from ..services import live_test, settings_service
from ..services.audit import audit
from ..services.notifications import notify
from ..services.payouts import transfer

admin_router = APIRouter(prefix="/api/admin", tags=["v20-admin"])
Q = Decimal("0.001")
MAX_WALLET_OP = Decimal("10000")


def _m(v) -> str:
    return f"{Decimal(v or 0):.3f}"


# ------------------------------- partners -------------------------------
def _valid_iban(v: str | None) -> str | None:
    """Bahrain IBAN: BH + 2 check digits + 4 bank letters + 14 alphanumerics (22 chars), mod-97 checked."""
    if v is None or not v.strip():
        return None
    iban = re.sub(r"\s", "", v).upper()
    if not re.fullmatch(r"BH\d{2}[A-Z]{4}[A-Z0-9]{14}", iban) or \
            int("".join(str(int(c, 36)) for c in iban[4:] + iban[:4])) % 97 != 1:
        raise ValueError("INVALID_IBAN")
    return iban


class PartnerIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    percent: Decimal = Field(ge=0, le=100)
    phone: str | None = Field(default=None, max_length=32)
    notes: str | None = None
    is_active: bool = True
    iban: str | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    beneficiary_name: str | None = Field(default=None, max_length=120)
    tap_destination_id: str | None = Field(default=None, max_length=80)
    _iban = field_validator("iban")(_valid_iban)


class PartnerPatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    percent: Decimal | None = Field(default=None, ge=0, le=100)
    phone: str | None = None
    notes: str | None = None
    is_active: bool | None = None
    iban: str | None = None
    bank_name: str | None = Field(default=None, max_length=120)
    beneficiary_name: str | None = Field(default=None, max_length=120)
    tap_destination_id: str | None = Field(default=None, max_length=80)
    _iban = field_validator("iban")(_valid_iban)


class PayTapIn(BaseModel):
    confirm: bool


class PayIn(BaseModel):
    reference: str | None = Field(default=None, max_length=120)


def _p(p: Partner) -> dict:
    return {"id": p.id, "name": p.name, "percent": f"{p.percent:.2f}", "phone": p.phone, "notes": p.notes,
            "is_active": p.is_active, "iban": p.iban, "bank_name": p.bank_name, "beneficiary_name": p.beneficiary_name,
            "tap_destination_id": p.tap_destination_id, "created_at": dt(p.created_at)}


def _payout(x: PartnerPayout | None) -> dict | None:
    if not x:
        return None
    return {"id": x.id, "status": x.status, "provider": x.provider, "provider_ref": x.provider_ref,
            "failure_reason": x.failure_reason, "created_at": dt(x.created_at), "transferred_at": dt(x.transferred_at)}


async def _latest_payouts(db: AsyncSession, ids: list[int]) -> dict[int, PartnerPayout]:
    if not ids:
        return {}
    rows = (await db.execute(select(PartnerPayout).where(PartnerPayout.statement_id.in_(ids)).order_by(PartnerPayout.id))).scalars().all()
    return {r.statement_id: r for r in rows}  # later rows overwrite earlier ones -> latest attempt


def _st(s: PartnerStatement, name: str | None = None, payout: PartnerPayout | None = None) -> dict:
    return {"payout": _payout(payout), "pay_method": s.pay_method, **_st_base(s, name)}


def _st_base(s: PartnerStatement, name: str | None = None) -> dict:
    return {"id": s.id, "partner_id": s.partner_id, "partner_name": name, "period": s.period,
            "gross_commission": _m(s.gross_commission), "referral_cost": _m(s.referral_cost), "base_amount": _m(s.base_amount),
            "percent": f"{s.percent:.2f}", "amount": _m(s.amount), "status": s.status, "paid_at": dt(s.paid_at),
            "paid_by": s.paid_by, "paid_ref": s.paid_ref}


async def _check_total(db: AsyncSession, exclude_id: int | None, new_percent: Decimal, active: bool) -> None:
    if not active:
        return
    q = select(func.coalesce(func.sum(Partner.percent), 0)).where(Partner.is_active.is_(True))
    if exclude_id:
        q = q.where(Partner.id != exclude_id)
    total = Decimal((await db.execute(q)).scalar_one() or 0) + Decimal(new_percent)
    if total > 100:
        raise HTTPException(422, {"code": "PARTNER_PERCENT_EXCEEDS_100", "total": f"{total:.2f}"})


def _period_bounds(period: str) -> tuple[datetime, datetime]:
    try:
        y, m = (int(x) for x in period.split("-"))
        tz = ZoneInfo(settings.timezone)
        start = datetime(y, m, 1, tzinfo=tz)
        end = datetime(y + (m == 12), 1 if m == 12 else m + 1, 1, tzinfo=tz)
    except Exception:  # noqa
        raise HTTPException(422, {"code": "BAD_PERIOD", "message": "period must be YYYY-MM"})
    return start, end


async def month_profit(db: AsyncSession, period: str) -> dict:
    """Net company profit of a month = commissions of PAID payments (already net of promo discounts)
    minus referral rewards credited in that month (revoked rewards are added back), never below zero."""
    start, end = _period_bounds(period)
    gross = Decimal((await db.execute(select(func.coalesce(func.sum(Payment.company_amount), 0)).where(
        Payment.payment_status == "PAID", Payment.paid_at >= start, Payment.paid_at < end))).scalar_one() or 0)
    ref = Decimal((await db.execute(select(func.coalesce(func.sum(WalletEntry.amount), 0)).where(
        WalletEntry.kind.in_(("REFERRAL_INVITER", "REFERRAL_INVITEE", "REVOKED")),
        WalletEntry.created_at >= start, WalletEntry.created_at < end))).scalar_one() or 0)
    ref = max(Decimal(0), ref)
    net = max(Decimal(0), gross - ref)
    return {"period": period, "gross_commission": gross.quantize(Q), "referral_cost": ref.quantize(Q), "net_profit": net.quantize(Q)}


@admin_router.get("/partners")
async def list_partners(admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Partner).where(Partner.deleted_at.is_(None)).order_by(Partner.id))).scalars().all()
    return {"items": [_p(p) for p in rows], "active_percent_total": f"{sum((p.percent for p in rows if p.is_active), Decimal(0)):.2f}"}


@admin_router.post("/partners", status_code=201)
async def create_partner(body: PartnerIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    await _check_total(db, None, body.percent, body.is_active)
    p = Partner(name=body.name.strip(), percent=body.percent, phone=body.phone, notes=body.notes, is_active=body.is_active)
    db.add(p)
    await db.flush()
    audit(db, admin.id, "PARTNER_CREATE", "partner", p.id, details={"percent": str(body.percent)})
    await db.commit()
    return _p(p)


@admin_router.patch("/partners/{partner_id}")
async def update_partner(partner_id: int, body: PartnerPatch, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Partner, partner_id)
    if not p:
        raise HTTPException(404, "Partner not found")
    data = body.model_dump(exclude_unset=True)
    await _check_total(db, p.id, data.get("percent", p.percent), data.get("is_active", p.is_active))
    for k, v in data.items():
        setattr(p, k, v.strip() if isinstance(v, str) and k == "name" else v)
    audit(db, admin.id, "PARTNER_UPDATE", "partner", p.id, details={k: str(v) for k, v in data.items()})
    await db.commit()
    return _p(p)


@admin_router.get("/partners/profit")
async def profit_preview(month: str, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """Preview of a month: net profit and each active partner's share (nothing is saved)."""
    prof = await month_profit(db, month)
    partners = (await db.execute(select(Partner).where(Partner.is_active.is_(True)).order_by(Partner.id))).scalars().all()
    return {**{k: (_m(v) if isinstance(v, Decimal) else v) for k, v in prof.items()},
            "partners": [{"partner_id": p.id, "name": p.name, "percent": f"{p.percent:.2f}",
                          "amount": _m((prof["net_profit"] * p.percent / 100).quantize(Q, ROUND_HALF_UP))} for p in partners]}


async def generate_statements(db: AsyncSession, period: str, regenerate: bool = False) -> list[PartnerStatement]:
    """Creates the missing statement of every active partner for the month. PENDING ones are recomputed when
    regenerate=True; PAID ones are never touched."""
    prof = await month_profit(db, period)
    partners = (await db.execute(select(Partner).where(Partner.is_active.is_(True)))).scalars().all()
    existing = {s.partner_id: s for s in (await db.execute(select(PartnerStatement).where(PartnerStatement.period == period))).scalars().all()}
    out = []
    for p in partners:
        amount = (prof["net_profit"] * p.percent / 100).quantize(Q, ROUND_HALF_UP)
        s = existing.get(p.id)
        if s and (s.status == "PAID" or not regenerate):
            out.append(s)
            continue
        if not s:
            s = PartnerStatement(partner_id=p.id, period=period, percent=p.percent)
            db.add(s)
        s.gross_commission, s.referral_cost, s.base_amount = prof["gross_commission"], prof["referral_cost"], prof["net_profit"]
        s.percent, s.amount = p.percent, amount
        out.append(s)
    await db.flush()
    return out


@admin_router.post("/partners/statements/generate")
async def generate(month: str, regenerate: bool = False, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    rows = await generate_statements(db, month, regenerate)
    audit(db, admin.id, "PARTNER_STATEMENTS_GENERATE", "partner_statement", month, details={"regenerate": regenerate})
    await db.commit()
    names = {p.id: p.name for p in (await db.execute(select(Partner))).scalars().all()}
    po = await _latest_payouts(db, [s.id for s in rows])
    return {"items": [_st(s, names.get(s.partner_id), po.get(s.id)) for s in rows]}


@admin_router.get("/partners/statements")
async def list_statements(month: str | None = None, partner_id: int | None = None, status: str | None = None,
                          admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    q = select(PartnerStatement, Partner.name).join(Partner, Partner.id == PartnerStatement.partner_id)
    if month:
        q = q.where(PartnerStatement.period == month)
    if partner_id:
        q = q.where(PartnerStatement.partner_id == partner_id)
    if status:
        q = q.where(PartnerStatement.status == status)
    rows = (await db.execute(q.order_by(PartnerStatement.period.desc(), PartnerStatement.id))).all()
    pending = sum((s.amount for s, _ in rows if s.status == "PENDING"), Decimal(0))
    po = await _latest_payouts(db, [s.id for s, _ in rows])
    return {"items": [_st(s, n, po.get(s.id)) for s, n in rows], "pending_total": _m(pending)}


@admin_router.post("/partners/statements/{statement_id}/pay")
async def pay_statement(statement_id: int, body: PayIn = PayIn(), admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    s = (await db.execute(select(PartnerStatement).where(PartnerStatement.id == statement_id).with_for_update())).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Statement not found")
    if s.status == "PAID":
        raise HTTPException(409, {"code": "ALREADY_PAID"})
    last = (await _latest_payouts(db, [s.id])).get(s.id)
    if last and last.status == "PENDING":
        raise HTTPException(409, {"code": "PAYOUT_IN_PROGRESS"})
    s.status, s.paid_at, s.paid_by, s.paid_ref, s.pay_method = "PAID", utcnow(), admin.id, body.reference, "MANUAL"
    audit(db, admin.id, "PARTNER_STATEMENT_PAID", "partner_statement", s.id, details={"amount": str(s.amount), "ref": body.reference})
    await db.commit()
    p = await db.get(Partner, s.partner_id)
    return _st(s, p.name if p else None, (await _latest_payouts(db, [s.id])).get(s.id))


async def _run_tap_payout(db: AsyncSession, statement_id: int, admin: User, retry: bool) -> dict:
    s = (await db.execute(select(PartnerStatement).where(PartnerStatement.id == statement_id).with_for_update())).scalar_one_or_none()
    if not s:
        raise HTTPException(404, "Statement not found")
    if s.status == "PAID":
        raise HTTPException(409, {"code": "ALREADY_PAID"})
    last = (await _latest_payouts(db, [s.id])).get(s.id)
    if last and last.status == "PENDING":
        raise HTTPException(409, {"code": "PAYOUT_IN_PROGRESS"})
    if retry and (not last or last.status != "FAILED"):
        raise HTTPException(409, {"code": "NOTHING_TO_RETRY"})
    live_test.block_tap(await settings_service.get_all(db))
    if settings.payout_provider == "none":
        raise HTTPException(503, {"code": "TAP_PAYOUTS_NOT_CONFIGURED"})
    p = await db.get(Partner, s.partner_id)
    amount = Decimal(s.amount or 0).quantize(Q)
    if amount <= 0:
        raise HTTPException(422, {"code": "AMOUNT_ZERO"})
    if not p or not (p.iban or p.tap_destination_id):
        raise HTTPException(422, {"code": "PARTNER_NO_BANK_DETAILS"})
    cur = (await settings_service.get_all(db)).get("currency") or "BHD"
    po = PartnerPayout(statement_id=s.id, amount=amount, status="PENDING", provider="TAP", requested_by=admin.id)
    db.add(po)
    await db.flush()
    audit(db, admin.id, "PARTNER_PAYOUT_REQUESTED", "partner_statement", s.id,
          details={"statement_id": s.id, "partner_id": p.id, "amount": str(amount)})
    ref = f"PARTNER-{s.id}-{po.id}"
    if p.tap_destination_id:
        method, account = "TAP_DESTINATION", p.tap_destination_id
    else:
        method, account = "IBAN", p.iban
    res = await transfer(method, account, p.beneficiary_name or p.name, amount, cur, ref)
    po.simulated = res.simulated
    if res.status == "TRANSFERRED":
        po.status, po.provider_ref, po.transferred_at = "TRANSFERRED", res.reference or ref, utcnow()
        s.status, s.paid_at, s.paid_by, s.paid_ref, s.pay_method = "PAID", utcnow(), admin.id, po.provider_ref, "TAP"
        audit(db, admin.id, "PARTNER_PAYOUT_TRANSFERRED", "partner_statement", s.id,
              details={"statement_id": s.id, "partner_id": p.id, "amount": str(amount), "provider_ref": po.provider_ref})
    elif res.status == "FAILED":
        po.status, po.failure_reason = "FAILED", res.error
        audit(db, admin.id, "PARTNER_PAYOUT_FAILED", "partner_statement", s.id,
              details={"statement_id": s.id, "partner_id": p.id, "amount": str(amount), "provider_ref": res.error})
    else:  # provider accepted it but has not settled yet
        po.provider_ref = res.reference or ref
    await db.commit()
    return _st(s, p.name, po)


@admin_router.post("/partners/statements/{statement_id}/pay-tap")
async def pay_statement_tap(statement_id: int, body: PayTapIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    if not body.confirm:
        raise HTTPException(422, {"code": "CONFIRM_REQUIRED"})
    return await _run_tap_payout(db, statement_id, admin, retry=False)


@admin_router.post("/partners/statements/{statement_id}/pay-tap/retry")
async def pay_statement_tap_retry(statement_id: int, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    return await _run_tap_payout(db, statement_id, admin, retry=True)


async def monthly_job(db: AsyncSession) -> None:
    """Night window 02:00-05:00 (Asia/Bahrain, gated by the caller): creates last month's statements once, from the 1st."""
    now = utcnow().astimezone(ZoneInfo(settings.timezone))
    if now.hour < 2:
        return
    y, m = (now.year - 1, 12) if now.month == 1 else (now.year, now.month - 1)
    period = f"{y:04d}-{m:02d}"
    if (await settings_service.get_all(db))["partners_last_generated"] == period:
        return
    await generate_statements(db, period)
    await settings_service.set_value(db, "partners_last_generated", period, None)


# ------------------------------- admin wallet -------------------------------
class WalletOpIn(BaseModel):
    amount: Decimal = Field(gt=0)
    direction: str = Field(pattern="^(CREDIT|DEBIT)$")
    reason: str = Field(min_length=3, max_length=300)


@admin_router.post("/users/{user_id}/wallet")
async def wallet_op(user_id: int, body: WalletOpIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    amt = body.amount.quantize(Q, ROUND_HALF_UP)
    if amt <= 0 or amt > MAX_WALLET_OP:
        raise HTTPException(422, {"code": "BAD_AMOUNT", "max": str(MAX_WALLET_OP)})
    u = (await db.execute(select(User).where(User.id == user_id).with_for_update())).scalar_one_or_none()
    if not u:
        raise HTTPException(404, "User not found")
    bal = Decimal(u.wallet_balance or 0)
    if body.direction == "DEBIT":
        if amt > bal:
            raise HTTPException(409, {"code": "INSUFFICIENT_WALLET", "balance": _m(bal)})
        signed, kind = -amt, "ADMIN_DEBIT"
    else:
        signed, kind = amt, "ADMIN_CREDIT"
    u.wallet_balance = bal + signed
    db.add(WalletEntry(user_id=u.id, amount=signed, kind=kind, note=body.reason.strip(), admin_id=admin.id))
    audit(db, admin.id, "WALLET_" + body.direction, "user", u.id, reason=body.reason, details={"amount": str(amt), "balance_after": str(u.wallet_balance)})
    if kind == "ADMIN_CREDIT":
        await notify(db, u.id, "WALLET_CREDIT", "تم إيداع رصيد في محفظتك", f"أُضيف {amt:.3f} إلى محفظتك.", data={"amount": str(amt)})
    else:
        await notify(db, u.id, "WALLET_DEBIT", "تم خصم رصيد من محفظتك", f"خُصم {amt:.3f} من محفظتك.", data={"amount": str(amt)})
    await db.commit()
    return {"user_id": u.id, "balance": _m(u.wallet_balance), "entry": {"amount": _m(signed), "kind": kind}}


@admin_router.get("/users/{user_id}/wallet")
async def wallet_view(user_id: int, limit: int = 30, offset: int = 0, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user_id)
    if not u:
        raise HTTPException(404, "User not found")
    rows = (await db.execute(select(WalletEntry).where(WalletEntry.user_id == user_id).order_by(WalletEntry.id.desc())
                             .limit(max(1, min(limit, 100))).offset(max(0, offset)))).scalars().all()
    return {"user_id": u.id, "balance": _m(u.wallet_balance),
            "items": [{"id": w.id, "amount": _m(w.amount), "kind": w.kind, "note": w.note, "admin_id": w.admin_id, "created_at": dt(w.created_at)} for w in rows]}

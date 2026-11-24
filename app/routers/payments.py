"""Payments: Tap (online, automatic) and BenefitPay (screenshot + admin confirmation). Cash does not exist.
All money lands in the company account; the driver share is paid out after the payment is confirmed."""
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..deps import admin_user, ai_review_user, customer_user, paging
from ..models import AdminAuditLog, DriverPayout, Payment, User, utcnow
from ..schemas import BenefitPayIn, MarkTransferredIn, PaymentConfirmIn, ReasonIn
from ..security import create_file_token, decrypt
from ..serializers import dt, money, payment_admin_load_options, payment_out, payout_admin_load_options, payout_out
from ..services import live_test, settings_service, tap
from ..services.audit import audit
from ..services.files import assert_owned
from ..services.payments import FEE_CATEGORIES, attempt_payout, finalize_paid, notify_admins, tap_fee_detail

log = logging.getLogger("payments")
router = APIRouter(prefix="/api/payments", tags=["payments"])
webhook_router = APIRouter(prefix="/api/payments/tap", tags=["payments"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-payments"])

TAP_FAILED = {"FAILED", "DECLINED", "CANCELLED", "ABANDONED", "RESTRICTED", "VOID", "TIMEDOUT", "UNKNOWN"}


async def _mine(db: AsyncSession, user: User, payment_id: int) -> Payment:
    p = await db.get(Payment, payment_id)
    if not p or p.customer_id != user.id:
        raise HTTPException(404, "Payment not found")
    return p


@router.get("/methods")
async def payment_methods(user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """What the payment screen shows: enabled methods, Tap fee, and the company BenefitPay account. All from settings."""
    s = await settings_service.get_all(db)
    vat, share = Decimal(s["tap_vat_percent"]) / 100 + 1, Decimal(s["tap_monthly_share"])
    cats = [{"code": c, "label": lbl, "fee_percent": s[f"{k}_percent"], "fee_fixed": s[f"{k}_fixed"]} for c, (k, lbl) in FEE_CATEGORIES.items()]
    legacy = lambda k: {"fee_percent": f"{Decimal(s[f'{k}_percent']) * vat:.3f}", "fee_fixed": f"{(Decimal(s[f'{k}_fixed']) + share) * vat:.3f}"}  # noqa: E731  percent and fixed incl. VAT and the per-transaction share
    return {"methods": [m for m, on in (("TAP", live_test.tap_on(s)), ("BENEFIT", live_test.benefit_on(s)), ("BENEFITPAY", s["benefitpay_enabled"] == "true")) if on],
            "tap": {**legacy("fee_card_local"), "vat_percent": s["tap_vat_percent"], "per_transaction_fixed": s["tap_monthly_share"], "categories": cats,
                    "formula": "(amount x percent + fixed + per_transaction_fixed) x (1 + vat_percent/100)"},
            "benefit": legacy("fee_benefit"),
            "benefitpay": {"free": True, "fee_percent": "0", "fee_fixed": "0", "account_name": s["benefitpay_account_name"], "account_number": s["benefitpay_account_number"],
                           "instructions": s["benefitpay_instructions"]}}


@router.get("/mine")
async def my_payments(status: str | None = None, page=Depends(paging), user: User = Depends(customer_user),
                      db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(Payment).where(Payment.customer_id == user.id)
    if status:
        q = q.where(Payment.payment_status == status)
    rows = (await db.execute(q.order_by(Payment.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [payment_out(p) for p in rows]


@router.get("/{payment_id}")
async def get_payment(payment_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    p = await _mine(db, user, payment_id)
    return payment_out(p, await settings_service.get_all(db))


class TapIn(BaseModel):
    card_type: str = "LOCAL"  # LOCAL | REGIONAL | GLOBAL (also Amex); Apple/Google/Samsung Pay: the type of the card behind them


@router.get("/{payment_id}/fees")
async def payment_fees(payment_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Exact Tap fee for this payment per method/card type, so the screen shows the final number BEFORE the customer chooses."""
    p = await _mine(db, user, payment_id)
    s = await settings_service.get_all(db)
    due = p.amount - (p.wallet_amount or 0)
    return {"amount_due": f"{due:.{p.currency_decimals}f}", "currency": p.currency,
            "options": [{**(d := tap_fee_detail(due, s, p.currency_decimals, c)), "charged": f"{due + Decimal(d['total']):.{p.currency_decimals}f}"} for c in FEE_CATEGORIES],
            "no_fee_methods": ["BENEFITPAY", "WALLET"]}


@router.post("/{payment_id}/tap")
async def pay_with_tap(payment_id: int, body: TapIn | None = None, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Creates a Tap charge and returns the hosted payment page URL. The gateway fee is added to what the customer pays."""
    p = await _mine(db, user, payment_id)
    if p.payment_status in ("PAID", "PENDING_VERIFICATION"):
        raise HTTPException(409, {"code": "PAYMENT_NOT_OPEN", "status": p.payment_status})
    s = await settings_service.get_all(db)
    live_test.block_tap(s)
    if s["tap_enabled"] != "true":
        raise HTTPException(409, {"code": "METHOD_DISABLED", "method": "TAP"})
    cat = (body.card_type if body else "LOCAL").upper()
    if cat not in ("LOCAL", "REGIONAL", "GLOBAL"):
        raise HTTPException(422, {"code": "INVALID_CARD_TYPE"})
    return await _tap_charge(db, user, p, s, "TAP", "src_all", cat)


@router.post("/{payment_id}/benefit")
async def pay_with_benefit(payment_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Benefit through the Tap gateway (hosted page, confirmed automatically). Fee: benefit_fee_* settings."""
    p = await _mine(db, user, payment_id)
    if p.payment_status in ("PAID", "PENDING_VERIFICATION"):
        raise HTTPException(409, {"code": "PAYMENT_NOT_OPEN", "status": p.payment_status})
    s = await settings_service.get_all(db)
    live_test.block_tap(s)
    if s["benefit_enabled"] != "true":
        raise HTTPException(409, {"code": "METHOD_DISABLED", "method": "BENEFIT"})
    return await _tap_charge(db, user, p, s, "BENEFIT", "src_bh.benefit", "BENEFIT")


async def _tap_charge(db: AsyncSession, user: User, p: Payment, s: dict, method: str, source: str, category: str):
    due = p.amount - (p.wallet_amount or 0)  # the wallet already covered part of it
    detail = tap_fee_detail(due, s, p.currency_decimals, category)
    fee = Decimal(detail["total"])
    charged = due + fee
    base = settings.public_base_url.rstrip("/")
    ref = f"PAY-{p.id}"
    charge = await tap.create_charge({
        "amount": float(charged), "currency": p.currency, "threeDSecure": True, "save_card": False,
        "description": f"Payment #{p.id}", "reference": {"transaction": ref, "order": ref},
        "customer": {"first_name": user.full_name, "phone": {"country_code": "973", "number": user.phone.lstrip("+")[-8:]}},
        "source": {"id": source},
        "post": {"url": f"{base}/api/payments/tap/webhook"},
        "redirect": {"url": f"{base}/api/payments/tap/return"}})
    url = (charge.get("transaction") or {}).get("url")
    if not charge.get("id") or not url:
        raise HTTPException(502, {"code": "TAP_BAD_RESPONSE"})
    p.payment_method, p.payment_status = method, "PENDING_PAYMENT"
    p.gateway_fee, p.charged_amount, p.transaction_reference, p.tap_url = fee, charged, charge["id"], url
    p.fee_category, p.fee_detail = category, detail
    p.reject_reason = None
    await db.commit()
    return payment_out(p)


@router.post("/{payment_id}/benefitpay")
async def pay_with_benefitpay(payment_id: int, body: BenefitPayIn, user: User = Depends(customer_user),
                              db: AsyncSession = Depends(get_db)):
    """Customer transfers to the company BenefitPay account and uploads the screenshot; an admin confirms it."""
    p = await _mine(db, user, payment_id)
    if p.payment_status in ("PAID", "PENDING_VERIFICATION"):
        raise HTTPException(409, {"code": "PAYMENT_NOT_OPEN", "status": p.payment_status})
    if (await settings_service.get_all(db))["benefitpay_enabled"] != "true":
        raise HTTPException(409, {"code": "METHOD_DISABLED", "method": "BENEFITPAY"})
    await assert_owned(db, user.id, body.proof_file_key)
    p.payment_method, p.payment_status = "BENEFITPAY", "PENDING_VERIFICATION"
    p.proof_file, p.transaction_reference, p.reject_reason = body.proof_file_key, body.reference, None
    p.gateway_fee, p.charged_amount = Decimal(0), p.amount - (p.wallet_amount or 0)
    await notify_admins(db, "PAYMENT_PROOF", "إيصال BenefitPay جديد", f"دفعة #{p.id} بانتظار التأكيد")
    from ..services import command as _cmd
    _cmd.emit("info", "payment.pending_review", f"إيصال BenefitPay بانتظار المراجعة: دفعة #{p.id}", None, {"payment_id": p.id, "amount": str(p.amount)})
    await db.commit()
    from ..services.ai_review import wake
    wake()
    return payment_out(p)


# ---------------- Tap callbacks (no auth: the truth is always re-fetched from Tap) ----------------

async def _verify_and_apply(db: AsyncSession, charge_id: str) -> Payment | None:
    p = (await db.execute(select(Payment).where(Payment.transaction_reference == charge_id,
                                                Payment.payment_method.in_(["TAP", "BENEFIT"])))).scalars().first()
    if not p:
        return None
    charge = await tap.retrieve_charge(charge_id)
    status = str(charge.get("status", "")).upper()
    ref = (charge.get("reference") or {}).get("transaction")
    try:
        amount_ok = Decimal(str(charge.get("amount"))) == p.charged_amount and charge.get("currency") == p.currency
    except Exception:  # noqa
        amount_ok = False
    if status == "CAPTURED":
        if ref != f"PAY-{p.id}" or not amount_ok:
            log.error("Tap charge %s does not match payment %s", charge_id, p.id)
            await notify_admins(db, "PAYMENT_MISMATCH", "دفعة Tap غير مطابقة", f"payment #{p.id} charge {charge_id}")
            return p
        src = charge.get("source") or {}
        brand = str(src.get("payment_method") or (charge.get("card") or {}).get("brand") or "").upper()[:30] or None
        p.card_brand = brand
        if brand and ((brand.startswith("AMERICAN") and p.fee_category not in ("GLOBAL", None)) or (brand == "BENEFIT" and p.fee_category not in ("BENEFIT", None))):
            await notify_admins(db, "FEE_MISMATCH", "Card type differs from the one selected", f"payment #{p.id}: chosen {p.fee_category}, Tap reports {brand}")
        await finalize_paid(db, p, None, p.payment_method)
    elif status in TAP_FAILED and p.payment_status == "PENDING_PAYMENT":
        p.payment_status = "FAILED"
        p.reject_reason = f"TAP_{status}"
        from ..services import command as _cmd
        _cmd.emit("warning", "payment.failed", f"فشل دفع Tap: دفعة #{p.id}", f"TAP_{status}", {"payment_id": p.id, "amount": str(p.amount)})
    return p


@webhook_router.post("/webhook")
async def tap_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        data = await request.json()
        charge_id = data.get("id")
        if charge_id:
            await _verify_and_apply(db, str(charge_id))
        await db.commit()
    except Exception:  # noqa  never let Tap retry-storm us; failures are re-checked on the return page / by admin
        log.exception("tap webhook")
        await db.rollback()
    return {"ok": True}


@webhook_router.get("/return", response_class=HTMLResponse)
async def tap_return(tap_id: str | None = None, db: AsyncSession = Depends(get_db)):
    """Landing page after the hosted payment page. The app reads the real status from GET /api/payments/{id}."""
    status = "UNKNOWN"
    if tap_id:
        try:
            p = await _verify_and_apply(db, tap_id)
            await db.commit()
            status = p.payment_status if p else "UNKNOWN"
        except Exception:  # noqa
            await db.rollback()
    msg = {"PAID": "Payment successful ✔", "FAILED": "The payment failed"}.get(status, "Verifying the payment...")
    return f'<html dir="rtl" lang="ar"><meta name="viewport" content="width=device-width"><body style="font-family:sans-serif;text-align:center;padding:48px"><h2>{msg}</h2><p>يمكنك العودة إلى التطبيق.</p></body></html>'


# ---------------- admin ----------------

def _file_url(key):
    return f"/api/files/download/{create_file_token(key)}" if key else None


def _person(u: User | None) -> dict | None:
    return {"id": u.id, "full_name": u.full_name, "phone": u.phone, "role": u.role} if u else None


async def admin_payment_view(db: AsyncSession, p: Payment, s: dict | None = None) -> dict:
    """What an admin needs to confirm money: the proof image, who paid, for which order/subscription, and the company account to compare with."""
    s = s if s is not None else await settings_service.get_all(db)
    out = payment_out(p, admin=True)
    out["proof_url"] = _file_url(p.proof_file)
    out["customer"] = _person(p.customer)
    out["driver"] = _person(p.driver) if p.driver else None
    vb = await db.get(User, p.verified_by) if p.verified_by else None
    out["verified_by_name"] = vb.full_name if vb else None
    review_log = (await db.execute(select(AdminAuditLog).where(
        AdminAuditLog.entity_type == "payment", AdminAuditLog.entity_id == str(p.id),
        AdminAuditLog.action == "PAYMENT_CONFIRMED",
    ).order_by(AdminAuditLog.id.desc()).limit(1))).scalar_one_or_none()
    out["review_notes"] = (review_log.details or {}).get("review_notes") if review_log else None
    out["expected_amount"] = money(p.amount - (p.wallet_amount or 0), p.currency_decimals)
    out["company_account"] = {"name": s.get("benefitpay_account_name"), "number": s.get("benefitpay_account_number")}
    out["order"] = None
    if p.order_id and p.order:
        o = p.order
        out["order"] = {"id": o.id, "service_type": o.service_type, "order_status": o.order_status,
                        "pickup_address": o.pickup_address, "destination_address": o.destination_address,
                        "final_price": money(o.final_price, o.currency_decimals),
                        "customer_total": money(o.customer_total, o.currency_decimals) if o.customer_total is not None else None,
                        "completed_at": dt(o.completed_at), "created_at": dt(o.created_at)}
    out["subscription"] = None
    if p.subscription_id and p.subscription:
        sub = p.subscription
        out["subscription"] = {"id": sub.id, "category": sub.category, "status": sub.status, "passenger_name": sub.passenger_name,
                               "pickup_address": sub.pickup_address, "destination_address": sub.destination_address}
    return out


async def admin_payout_view(db: AsyncSession, r: DriverPayout) -> dict:
    out = payout_out(r)
    drv = r.driver
    out["driver"] = _person(drv)
    prof = drv.driver_profile if drv else None
    acc = decrypt(prof.payout_account_encrypted) if prof and prof.payout_account_encrypted else None
    out["payout_account"] = {"method": prof.payout_method if prof else None, "account": ("****" + acc[-4:]) if acc else None,
                             "holder_name": prof.payout_holder_name if prof else None}
    pay = r.payment
    out["payment"] = {"id": pay.id, "kind": pay.kind, "order_id": pay.order_id, "amount": money(pay.amount, pay.currency_decimals),
                      "payment_method": pay.payment_method, "paid_at": dt(pay.paid_at)} if pay else None
    return out

@admin_router.get("/payments")
async def admin_payments(status: str | None = None, method: str | None = None, kind: str | None = None,
                         page=Depends(paging), admin: User = Depends(ai_review_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(Payment)
    if admin.role == "AI":
        q = q.where(Payment.payment_method == "BENEFITPAY", Payment.payment_status == "PENDING_VERIFICATION")
    for col, val in ((Payment.payment_status, status), (Payment.payment_method, method), (Payment.kind, kind)):
        if val:
            q = q.where(col == val)
    q = q.options(*payment_admin_load_options())
    rows = (await db.execute(q.order_by(Payment.id.desc()).limit(limit).offset(offset))).scalars().all()
    s = await settings_service.get_all(db)
    return [await admin_payment_view(db, p, s) for p in rows]


@admin_router.get("/payments/{payment_id}")
async def admin_payment_detail(payment_id: int, admin: User = Depends(ai_review_user), db: AsyncSession = Depends(get_db)):
    p = await db.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    if admin.role == "AI" and (p.payment_method != "BENEFITPAY" or p.payment_status != "PENDING_VERIFICATION"):
        raise HTTPException(404, "Payment not found")
    return await admin_payment_view(db, p)


@admin_router.post("/payments/{payment_id}/confirm")
async def confirm_payment(payment_id: int, body: PaymentConfirmIn | None = None,
                          admin: User = Depends(ai_review_user), db: AsyncSession = Depends(get_db)):
    """BenefitPay only: after checking the screenshot against the company account. Triggers the driver payout."""
    p = await db.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    if p.payment_method != "BENEFITPAY" or p.payment_status != "PENDING_VERIFICATION":
        raise HTTPException(409, {"code": "NOT_AWAITING_CONFIRMATION", "status": p.payment_status})
    if not await finalize_paid(db, p, admin.id):
        raise HTTPException(409, "Already paid")
    details = None
    if body and body.reference:
        p.transaction_reference = body.reference.strip()
    if body and body.review_notes:
        details = {"review_notes": body.review_notes}
    audit(db, admin.id, "PAYMENT_CONFIRMED", "payment", p.id, details=details)
    await db.commit()
    return await admin_payment_view(db, p)


@admin_router.post("/payments/{payment_id}/reject")
async def reject_payment(payment_id: int, body: ReasonIn, admin: User = Depends(ai_review_user), db: AsyncSession = Depends(get_db)):
    from ..services.notifications import notify
    p = await db.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    if admin.role == "AI" and p.payment_method != "BENEFITPAY":
        raise HTTPException(404, "Payment not found")
    if p.payment_status != "PENDING_VERIFICATION":
        raise HTTPException(409, {"code": "NOT_AWAITING_CONFIRMATION", "status": p.payment_status})
    p.payment_status, p.reject_reason, p.verified_by = "REJECTED", body.reason, admin.id
    audit(db, admin.id, "PAYMENT_REJECTED", "payment", p.id, body.reason)
    await notify(db, p.customer_id, "PAYMENT_REJECTED", "تم رفض إيصال الدفع", body.reason, p.order_id)
    await db.commit()
    return await admin_payment_view(db, p)


@admin_router.get("/payouts")
async def admin_payouts(status: str | None = None, driver_id: int | None = None, page=Depends(paging),
                        admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(DriverPayout)
    if status:
        q = q.where(DriverPayout.status == status)
    if driver_id:
        q = q.where(DriverPayout.driver_id == driver_id)
    q = q.options(*payout_admin_load_options())
    rows = (await db.execute(q.order_by(DriverPayout.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [await admin_payout_view(db, r) for r in rows]


async def _payout(db, payout_id) -> DriverPayout:
    r = await db.get(DriverPayout, payout_id)
    if not r:
        raise HTTPException(404, "Payout not found")
    if r.status == "TRANSFERRED":
        raise HTTPException(409, "Already transferred")
    return r


@admin_router.post("/payouts/{payout_id}/retry")
async def retry_payout(payout_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    r = await _payout(db, payout_id)
    await attempt_payout(db, r)
    audit(db, admin.id, "PAYOUT_RETRY", "payout", r.id)
    await db.commit()
    return await admin_payout_view(db, r)


@admin_router.post("/payouts/{payout_id}/mark-transferred")
async def mark_transferred(payout_id: int, body: MarkTransferredIn, admin: User = Depends(admin_user),
                           db: AsyncSession = Depends(get_db)):
    """Used when payouts are done manually from the bank app (PAYOUT_PROVIDER=none)."""
    from ..services.notifications import notify
    r = await _payout(db, payout_id)
    r.status, r.reference, r.simulated, r.failure_reason, r.transferred_at = "TRANSFERRED", body.reference, False, None, utcnow()
    audit(db, admin.id, "PAYOUT_MARKED_TRANSFERRED", "payout", r.id, details={"reference": body.reference})
    await notify(db, r.driver_id, "PAYOUT_SENT", "تم تحويل أرباحك", f"{r.amount:.{r.currency_decimals}f} {r.currency}")
    await db.commit()
    return await admin_payout_view(db, r)

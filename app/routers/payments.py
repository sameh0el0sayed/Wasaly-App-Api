"""Payments: Tap (online, automatic) and BenefitPay (screenshot + admin confirmation). Cash does not exist.
All money lands in the company account; the driver share is paid out after the payment is confirmed."""
import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import get_db
from ..deps import admin_user, customer_user, paging
from ..models import DriverPayout, Payment, User
from ..schemas import BenefitPayIn, MarkTransferredIn, ReasonIn
from ..serializers import payment_out, payout_out
from ..services import settings_service, tap
from ..services.audit import audit
from ..services.files import assert_owned
from ..services.payments import attempt_payout, finalize_paid, notify_admins, tap_fee
from ..models import utcnow

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
    return {"methods": [m for m, on in (("TAP", s["tap_enabled"] == "true"), ("BENEFITPAY", s["benefitpay_enabled"] == "true")) if on],
            "tap": {"fee_percent": s["tap_fee_percent"], "fee_fixed": s["tap_fee_fixed"]},
            "benefitpay": {"account_name": s["benefitpay_account_name"], "account_number": s["benefitpay_account_number"],
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


@router.post("/{payment_id}/tap")
async def pay_with_tap(payment_id: int, user: User = Depends(customer_user), db: AsyncSession = Depends(get_db)):
    """Creates a Tap charge and returns the hosted payment page URL. The gateway fee is added to what the customer pays."""
    p = await _mine(db, user, payment_id)
    if p.payment_status in ("PAID", "PENDING_VERIFICATION"):
        raise HTTPException(409, {"code": "PAYMENT_NOT_OPEN", "status": p.payment_status})
    s = await settings_service.get_all(db)
    if s["tap_enabled"] != "true":
        raise HTTPException(409, {"code": "METHOD_DISABLED", "method": "TAP"})
    fee = tap_fee(p.amount, s, p.currency_decimals)
    charged = p.amount + fee
    base = settings.public_base_url.rstrip("/")
    ref = f"PAY-{p.id}"
    charge = await tap.create_charge({
        "amount": float(charged), "currency": p.currency, "threeDSecure": True, "save_card": False,
        "description": f"Payment #{p.id}", "reference": {"transaction": ref, "order": ref},
        "customer": {"first_name": user.full_name, "phone": {"country_code": "973", "number": user.phone.lstrip("+")[-8:]}},
        "source": {"id": "src_all"},
        "post": {"url": f"{base}/api/payments/tap/webhook"},
        "redirect": {"url": f"{base}/api/payments/tap/return"}})
    url = (charge.get("transaction") or {}).get("url")
    if not charge.get("id") or not url:
        raise HTTPException(502, {"code": "TAP_BAD_RESPONSE"})
    p.payment_method, p.payment_status = "TAP", "PENDING_PAYMENT"
    p.gateway_fee, p.charged_amount, p.transaction_reference, p.tap_url = fee, charged, charge["id"], url
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
    p.gateway_fee, p.charged_amount = Decimal(0), p.amount
    await notify_admins(db, "PAYMENT_PROOF", "إيصال BenefitPay جديد", f"دفعة #{p.id} بانتظار التأكيد")
    await db.commit()
    return payment_out(p)


# ---------------- Tap callbacks (no auth: the truth is always re-fetched from Tap) ----------------

async def _verify_and_apply(db: AsyncSession, charge_id: str) -> Payment | None:
    p = (await db.execute(select(Payment).where(Payment.transaction_reference == charge_id,
                                                Payment.payment_method == "TAP"))).scalars().first()
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
        await finalize_paid(db, p, None, "TAP")
    elif status in TAP_FAILED and p.payment_status == "PENDING_PAYMENT":
        p.payment_status = "FAILED"
        p.reject_reason = f"TAP_{status}"
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
    msg = {"PAID": "تم الدفع بنجاح ✔", "FAILED": "فشلت عملية الدفع"}.get(status, "جارٍ التحقق من الدفع…")
    return f'<html dir="rtl" lang="ar"><meta name="viewport" content="width=device-width"><body style="font-family:sans-serif;text-align:center;padding:48px"><h2>{msg}</h2><p>يمكنك العودة إلى التطبيق.</p></body></html>'


# ---------------- admin ----------------

@admin_router.get("/payments")
async def admin_payments(status: str | None = None, method: str | None = None, kind: str | None = None,
                         page=Depends(paging), admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(Payment)
    for col, val in ((Payment.payment_status, status), (Payment.payment_method, method), (Payment.kind, kind)):
        if val:
            q = q.where(col == val)
    rows = (await db.execute(q.order_by(Payment.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [payment_out(p, admin=True) for p in rows]


@admin_router.post("/payments/{payment_id}/confirm")
async def confirm_payment(payment_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    """BenefitPay only: after checking the screenshot against the company account. Triggers the driver payout."""
    p = await db.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    if p.payment_method != "BENEFITPAY" or p.payment_status != "PENDING_VERIFICATION":
        raise HTTPException(409, {"code": "NOT_AWAITING_CONFIRMATION", "status": p.payment_status})
    if not await finalize_paid(db, p, admin.id):
        raise HTTPException(409, "Already paid")
    audit(db, admin.id, "PAYMENT_CONFIRMED", "payment", p.id)
    await db.commit()
    return payment_out(p, admin=True)


@admin_router.post("/payments/{payment_id}/reject")
async def reject_payment(payment_id: int, body: ReasonIn, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    from ..services.notifications import notify
    p = await db.get(Payment, payment_id)
    if not p:
        raise HTTPException(404, "Payment not found")
    if p.payment_status != "PENDING_VERIFICATION":
        raise HTTPException(409, {"code": "NOT_AWAITING_CONFIRMATION", "status": p.payment_status})
    p.payment_status, p.reject_reason, p.verified_by = "REJECTED", body.reason, admin.id
    audit(db, admin.id, "PAYMENT_REJECTED", "payment", p.id, body.reason)
    await notify(db, p.customer_id, "PAYMENT_REJECTED", "تم رفض إيصال الدفع", body.reason, p.order_id)
    await db.commit()
    return payment_out(p, admin=True)


@admin_router.get("/payouts")
async def admin_payouts(status: str | None = None, driver_id: int | None = None, page=Depends(paging),
                        admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    limit, offset = page
    q = select(DriverPayout)
    if status:
        q = q.where(DriverPayout.status == status)
    if driver_id:
        q = q.where(DriverPayout.driver_id == driver_id)
    rows = (await db.execute(q.order_by(DriverPayout.id.desc()).limit(limit).offset(offset))).scalars().all()
    return [payout_out(r) for r in rows]


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
    return payout_out(r)


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
    return payout_out(r)

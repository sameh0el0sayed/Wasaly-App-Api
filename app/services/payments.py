import logging
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import Role
from ..models import DriverPayout, DriverProfile, Order, Payment, User, utcnow
from ..security import decrypt
from . import live_test, payouts, settings_service
from .notifications import notify
from .pricing import quantum

log = logging.getLogger("payments")

OPEN_STATES = ("UNPAID", "PENDING_PAYMENT", "PENDING_VERIFICATION", "REJECTED", "FAILED")


async def notify_admins(db: AsyncSession, ntype: str, title: str, message: str, data: dict | None = None) -> None:
    ids = (await db.execute(select(User.id).where(User.role.in_(list(Role.ADMINS))))).scalars().all()
    for uid in ids:
        await notify(db, uid, ntype, title, message, data=data)


def tap_fee(amount: Decimal, s: dict, decimals: int, prefix: str = "tap") -> Decimal:
    """Gateway fee paid by the CUSTOMER: amount x percent% + fixed (settings tap_fee_* or benefit_fee_*)."""
    fee = amount * Decimal(s[f"{prefix}_fee_percent"]) / Decimal(100) + Decimal(s[f"{prefix}_fee_fixed"])
    return fee.quantize(quantum(decimals), ROUND_HALF_UP)


FEE_CATEGORIES = {"BENEFIT": ("fee_benefit", "Benefit"), "LOCAL": ("fee_card_local", "Local Visa/Mastercard"),
                  "REGIONAL": ("fee_card_regional", "Regional Visa/Mastercard"), "GLOBAL": ("fee_card_global", "Global Visa/Mastercard/Amex")}


def tap_fee_detail(amount: Decimal, s: dict, decimals: int, category: str) -> dict:
    """What the customer pays on top for a Tap payment: (amount x percent + fixed + per-transaction share of Tap's monthly fee) + VAT.
    Every number comes from settings and is returned so the app can show it."""
    key = FEE_CATEGORIES[category][0]
    pct, fixed = Decimal(s[f"{key}_percent"]), Decimal(s[f"{key}_fixed"])
    share, vat_pct = Decimal(s["tap_monthly_share"]), Decimal(s["tap_vat_percent"])
    q = quantum(decimals)
    pct_part = (amount * pct / 100).quantize(q, ROUND_HALF_UP)
    sub = pct_part + fixed + share
    vat = (sub * vat_pct / 100).quantize(q, ROUND_HALF_UP)
    f = lambda v: f"{v:.{decimals}f}"  # noqa: E731
    return {"category": category, "label": FEE_CATEGORIES[category][1], "percent": str(pct), "percent_amount": f(pct_part), "fixed": f(fixed),
            "monthly_share": f(share), "subtotal": f(sub), "vat_percent": str(vat_pct), "vat": f(vat), "total": f(sub + vat)}


async def create_payment(db: AsyncSession, *, kind: str, customer_id: int, driver_id: int | None, amount: Decimal,
                         driver_amount: Decimal, company_amount: Decimal, currency: str, decimals: int,
                         order_id: int | None = None, subscription_id: int | None = None) -> Payment:
    p = Payment(kind=kind, order_id=order_id, subscription_id=subscription_id, customer_id=customer_id,
                driver_id=driver_id, amount=amount, driver_amount=driver_amount, company_amount=company_amount,
                currency=currency, currency_decimals=decimals, payment_status="UNPAID")
    db.add(p)
    await db.flush()
    return p


async def apply_wallet(db: AsyncSession, pay: Payment) -> None:
    """Uses the customer's wallet balance first. A fully covered payment is settled at once (method WALLET)."""
    if pay.kind != "ORDER":
        return
    from . import modes
    if await modes.frozen():  # payments frozen: the wallet must not settle anything either
        return
    if (await settings_service.get_all(db))["wallet_auto_apply"] != "true":
        return
    from ..models import WalletEntry
    u = (await db.execute(select(User).where(User.id == pay.customer_id).with_for_update())).scalar_one_or_none()
    bal = Decimal(u.wallet_balance or 0) if u else Decimal(0)
    use = min(bal, Decimal(pay.amount)).quantize(quantum(pay.currency_decimals), ROUND_HALF_UP)
    if use <= 0:
        return
    u.wallet_balance = bal - use
    db.add(WalletEntry(user_id=u.id, amount=-use, kind="SPEND", ref_id=pay.id))
    pay.wallet_amount = use
    await db.flush()
    if use >= Decimal(pay.amount):
        await finalize_paid(db, pay, None, method="WALLET")


async def finalize_paid(db: AsyncSession, payment: Payment, verified_by: int | None, method: str | None = None) -> bool:
    """Marks the payment PAID exactly once (webhook / return page / admin confirm may race), then pays the driver."""
    now = utcnow()
    vals = {"payment_status": "PAID", "paid_at": now, "verified_by": verified_by, "reject_reason": None}
    if method:
        vals["payment_method"] = method
    res = await db.execute(update(Payment).where(Payment.id == payment.id, Payment.payment_status != "PAID").values(**vals))
    if res.rowcount != 1:
        return False
    await db.refresh(payment)
    if payment.kind == "ORDER" and payment.order_id:
        order = await db.get(Order, payment.order_id)
        order.payment_status, order.payment_method = "PAID", payment.payment_method
        from ..routers.v11 import reward_referral
        await reward_referral(db, payment.customer_id, order)
    elif payment.kind == "SUBSCRIPTION":
        from .subscriptions import on_paid
        await on_paid(db, payment)
    await notify(db, payment.customer_id, "PAYMENT_CONFIRMED", "تم تأكيد الدفع",
                 f"تم استلام {payment.amount:.{payment.currency_decimals}f} {payment.currency}", payment.order_id)
    if payment.driver_id and payment.driver_amount and payment.driver_amount > 0:
        prof = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == payment.driver_id).with_for_update())).scalar_one_or_none()
        debt = Decimal(prof.debt_balance or 0) if prof else Decimal(0)
        offset = min(debt, Decimal(payment.driver_amount)) if debt > 0 else Decimal(0)
        if offset > 0:  # the driver owes the company (e.g. ban reinstatement fee): nothing is transferred until it is cleared
            prof.debt_balance = debt - offset
        payout = DriverPayout(payment_id=payment.id, driver_id=payment.driver_id, amount=Decimal(payment.driver_amount) - offset,
                              debt_offset=offset, currency=payment.currency, currency_decimals=payment.currency_decimals)
        db.add(payout)
        await db.flush()
        if payout.amount > 0:
            await attempt_payout(db, payout)
        else:
            payout.status, payout.transferred_at = "OFFSET", utcnow()
        if offset > 0:
            await notify(db, payment.driver_id, "DEBT_DEDUCTED", "خصم من أرباحك",
                         f"تم خصم {offset:.{payment.currency_decimals}f} {payment.currency} من أرباحك لسداد مبلغ مستحق للشركة. المتبقي: {prof.debt_balance:.{payment.currency_decimals}f}")
    return True


async def attempt_payout(db: AsyncSession, payout: DriverPayout) -> None:
    if payout.status == "OFFSET" or payout.amount <= 0:
        return  # fully kept against the driver's debt
    if live_test.is_live_test(await settings_service.get_all(db)):
        return  # live test: no automatic transfers, stays pending for manual handling
    prof = (await db.execute(select(DriverProfile).where(DriverProfile.user_id == payout.driver_id))).scalar_one_or_none()
    driver = await db.get(User, payout.driver_id)
    account = decrypt(prof.payout_account_encrypted) if prof and prof.payout_account_encrypted else None
    res = await payouts.transfer(prof.payout_method if prof else None, account, prof.payout_holder_name if prof else None,
                                 payout.amount, payout.currency, f"PAYOUT-{payout.id}")
    payout.status, payout.reference, payout.failure_reason, payout.simulated = res.status, res.reference, res.error, res.simulated
    if res.status == "TRANSFERRED":
        payout.transferred_at = utcnow()
        await notify(db, payout.driver_id, "PAYOUT_SENT", "تم تحويل أرباحك",
                     f"{payout.amount:.{payout.currency_decimals}f} {payout.currency}" + (" (تجريبي)" if res.simulated else ""))
    elif res.error == "NO_PAYOUT_ACCOUNT":
        await notify(db, payout.driver_id, "PAYOUT_NEEDS_ACCOUNT", "أضف حساب التحويل",
                     "أضف رقم الحساب لاستلام أرباحك.")
        await notify_admins(db, "PAYOUT_FAILED", "تحويل سائق معلّق", f"السائق {driver.full_name if driver else payout.driver_id}: لا يوجد حساب تحويل")
    elif res.status == "FAILED":
        await notify_admins(db, "PAYOUT_FAILED", "فشل تحويل سائق", f"payout #{payout.id}: {res.error}")

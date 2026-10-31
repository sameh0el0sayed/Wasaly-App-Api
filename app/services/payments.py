import logging
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import Role
from ..models import DriverPayout, DriverProfile, Order, Payment, User, utcnow
from ..security import decrypt
from . import payouts, settings_service
from .notifications import notify
from .pricing import quantum

log = logging.getLogger("payments")

OPEN_STATES = ("UNPAID", "PENDING_PAYMENT", "PENDING_VERIFICATION", "REJECTED", "FAILED")


async def notify_admins(db: AsyncSession, ntype: str, title: str, message: str) -> None:
    ids = (await db.execute(select(User.id).where(User.role.in_(list(Role.ADMINS))))).scalars().all()
    for uid in ids:
        await notify(db, uid, ntype, title, message)


def tap_fee(amount: Decimal, s: dict, decimals: int) -> Decimal:
    """Gateway fee paid by the CUSTOMER: amount x percent% + fixed (settings tap_fee_percent / tap_fee_fixed)."""
    fee = amount * Decimal(s["tap_fee_percent"]) / Decimal(100) + Decimal(s["tap_fee_fixed"])
    return fee.quantize(quantum(decimals), ROUND_HALF_UP)


async def create_payment(db: AsyncSession, *, kind: str, customer_id: int, driver_id: int | None, amount: Decimal,
                         driver_amount: Decimal, company_amount: Decimal, currency: str, decimals: int,
                         order_id: int | None = None, subscription_id: int | None = None) -> Payment:
    p = Payment(kind=kind, order_id=order_id, subscription_id=subscription_id, customer_id=customer_id,
                driver_id=driver_id, amount=amount, driver_amount=driver_amount, company_amount=company_amount,
                currency=currency, currency_decimals=decimals, payment_status="UNPAID")
    db.add(p)
    await db.flush()
    return p


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

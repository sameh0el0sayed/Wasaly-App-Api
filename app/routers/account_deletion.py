"""POST /api/account/delete (user) and POST /api/admin/users/{id}/restore (admin, inside the grace period)."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..constants import AccountStatus, Role
from ..constants import OrderStatus as S
from ..database import get_db
from ..deps import admin_user, current_user
from ..i18n import localize
from ..models import Order, Subscription, User, utcnow
from ..schemas import AccountDeleteIn
from ..services import settings_service
from ..services.account_deletion import purge_devices
from ..services.audit import audit
from ..services.dispatch import force_offline, has_active_order

router = APIRouter(prefix="/api", tags=["account"])
admin_router = APIRouter(prefix="/api/admin", tags=["admin-account"])


@router.post("/account/delete")
async def delete_account(body: AccountDeleteIn = AccountDeleteIn(), user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    if user.role not in (Role.CUSTOMER, Role.DRIVER):
        raise HTTPException(403, "Admin accounts cannot be deleted here")
    if user.account_status == AccountStatus.DELETED:
        raise HTTPException(401, {"code": "UNAUTHORIZED"})
    if user.role == Role.CUSTOMER:
        active = (await db.execute(select(Order.id).where(Order.customer_id == user.id, Order.order_status.not_in(S.FINAL)).limit(1))).first()
    else:
        active = await has_active_order(db, user.id)
    if active:
        raise HTTPException(409, {"code": "ACTIVE_ORDER_EXISTS", "message": "لا يمكن حذف الحساب أثناء وجود طلب جارٍ"})
    if user.role == Role.CUSTOMER:
        unpaid = (await db.execute(select(Order.id, Order.payment_id).where(
            Order.customer_id == user.id, Order.order_status == S.COMPLETED, Order.payment_status != "PAID").limit(1))).first()
        if unpaid:
            raise HTTPException(409, {"code": "UNPAID_ORDER_EXISTS", "message": "يوجد مبلغ غير مدفوع", "order_id": unpaid[0], "payment_id": unpaid[1]})
    col = Subscription.customer_id if user.role == Role.CUSTOMER else Subscription.driver_id
    if (await db.execute(select(func.count()).select_from(Subscription).where(col == user.id, Subscription.status == "ACTIVE"))).scalar_one():
        raise HTTPException(409, {"code": "ACTIVE_SUBSCRIPTION_EXISTS", "message": "يوجد اشتراك ساري"})
    if user.driver_profile and (user.driver_profile.debt_balance or 0) > 0:
        raise HTTPException(409, {"code": "DEBT_OUTSTANDING", "message": "يوجد مبلغ مستحق للشركة"})
    user.deleted_prev_status = user.account_status
    user.account_status, user.status_reason, user.deleted_at = AccountStatus.DELETED, "BANNED_BEFORE_DELETE" if user.deleted_prev_status == AccountStatus.BANNED else "USER_REQUEST", utcnow()
    user.token_version += 1  # logs the user out of every device
    await purge_devices(db, user.id)
    if user.driver_profile:
        user.driver_profile.driver_status = AccountStatus.DELETED
        await force_offline(db, user.id)  # mode = OFF, live location erased, open offers closed
    audit(db, None, "ACCOUNT_DELETED", "user", user.id, body.reason)
    await db.commit()
    return {"message": localize("تم حذف حسابك", user.language)}


@admin_router.post("/users/{user_id}/restore")
async def restore_account(user_id: int, admin: User = Depends(admin_user), db: AsyncSession = Depends(get_db)):
    u = await db.get(User, user_id)
    if not u or u.role not in (Role.CUSTOMER, Role.DRIVER):
        raise HTTPException(404, "User not found")
    if u.account_status != AccountStatus.DELETED:
        raise HTTPException(409, {"code": "NOT_DELETED"})
    days = int((await settings_service.get_all(db))["account_deletion_grace_days"])
    if u.anonymized_at or not u.deleted_at or utcnow() - u.deleted_at > timedelta(days=days):
        raise HTTPException(409, {"code": "RESTORE_WINDOW_CLOSED"})
    status = u.deleted_prev_status or AccountStatus.APPROVED
    u.account_status, u.status_reason, u.deleted_at, u.deleted_prev_status = status, None, None, None
    if u.driver_profile:
        u.driver_profile.driver_status = status
    audit(db, admin.id, "ACCOUNT_RESTORED", "user", u.id)
    await db.commit()
    return {"id": u.id, "account_status": u.account_status}

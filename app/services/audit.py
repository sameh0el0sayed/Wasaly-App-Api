from sqlalchemy.ext.asyncio import AsyncSession

from ..models import AdminAuditLog


def audit(db: AsyncSession, admin_id: int | None, action: str, entity_type: str, entity_id,
          reason: str | None = None, details: dict | None = None) -> None:
    db.add(AdminAuditLog(admin_id=admin_id, action=action, entity_type=entity_type,
                         entity_id=str(entity_id), reason=reason, details=details))

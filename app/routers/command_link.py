"""Super-admin settings page 'ربط لوحة Phoenix Command': the shared secret lives here (encrypted in the DB), never in env."""
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import get_db
from ..deps import super_admin_user
from ..models import CommandLink, User
from ..security import decrypt, encrypt
from ..services import command
from ..services.audit import audit

router = APIRouter(prefix="/api/admin/command-link", tags=["command-link"])


class LinkIn(BaseModel):
    shared_secret: str | None = Field(default=None, max_length=200)  # empty / omitted = keep current
    key_id: str | None = Field(default=None, min_length=1, max_length=60)
    allowed_ips: str | None = Field(default=None, max_length=500)
    command_url: str | None = Field(default=None, max_length=300)
    enabled: bool | None = None


async def _row(db: AsyncSession) -> CommandLink:
    r = (await db.execute(select(CommandLink).limit(1))).scalar_one_or_none()
    if not r:
        r = CommandLink(key_id="phoenix-command", command_url="https://phoenixsystems.online/control-api", enabled=False)
        db.add(r)
        await db.flush()
    return r


def _view(r: CommandLink) -> dict:
    last4 = decrypt(r.secret_encrypted)[-4:] if r.secret_encrypted else None
    return {"key_id": r.key_id, "secret_set": bool(r.secret_encrypted), "shared_secret_masked": f"••••{last4}" if last4 else "",
            "allowed_ips": r.allowed_ips or "", "command_url": r.command_url, "enabled": bool(r.enabled)}


@router.get("")
async def get_link(admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    return _view(await _row(db))


@router.put("")
async def put_link(body: LinkIn, admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    r = await _row(db)
    changed = []
    if body.shared_secret and not body.shared_secret.startswith("••••"):
        r.secret_encrypted = encrypt(body.shared_secret.strip())
        changed.append("shared_secret")
    for f in ("key_id", "allowed_ips", "command_url", "enabled"):
        v = getattr(body, f)
        if v is not None and v != getattr(r, f):
            setattr(r, f, v.strip() if isinstance(v, str) else v)
            changed.append(f)
    audit(db, admin.id, "COMMAND_LINK_UPDATED", "command_link", 1, details={"changed": changed})  # never the secret itself
    await db.commit()
    command.invalidate()
    return _view(r)


@router.post("/generate")
async def generate(admin: User = Depends(super_admin_user), db: AsyncSession = Depends(get_db)):
    """A fresh 64-hex secret, returned ONCE in full. The owner pastes it into the dashboard."""
    r = await _row(db)
    secret = command.new_secret()
    r.secret_encrypted = encrypt(secret)
    audit(db, admin.id, "COMMAND_LINK_KEY_ROTATED", "command_link", 1)
    await db.commit()
    command.invalidate()
    return {"secret": secret, **_view(r)}


@router.post("/test")
async def test(admin: User = Depends(super_admin_user)):
    command.invalidate()
    return await command.send_test()

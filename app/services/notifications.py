import asyncio
import logging

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import settings
from ..database import SessionLocal
from ..i18n import localize
from ..models import DeviceToken, Notification, User

log = logging.getLogger("notifications")


async def notify(db: AsyncSession, user_id: int, ntype: str, title: str, message: str,
                 order_id: int | None = None, data: dict | None = None) -> None:
    """Stores an in-app notification and triggers push. The app must always re-fetch state from the API."""
    lang = (await db.execute(select(User.language).where(User.id == user_id))).scalar_one_or_none() or "ar"
    title, message = localize(title, lang), localize(message, lang)  # stored (and pushed) in the recipient's language
    db.add(Notification(user_id=user_id, notification_type=ntype, title=title, message=message,
                        related_order_id=order_id, data=data))
    tokens = (await db.execute(select(DeviceToken.token).where(DeviceToken.user_id == user_id))).scalars().all()
    for t in tokens:  # fire-and-forget: a slow/unreachable FCM must never slow down the API request
        task = asyncio.create_task(send_push(t, title, message, {"type": ntype, "order_id": str(order_id or ""), **{k: str(v) for k, v in (data or {}).items()}}))
        _pending.add(task)
        task.add_done_callback(_pending.discard)


_pending: set = set()
_app = None
_tried = False


def _firebase():
    """Lazy init from FIREBASE_CREDENTIALS (path to the service-account JSON). Returns None when push is not configured."""
    global _app, _tried
    if _tried:
        return _app
    _tried = True
    import json
    import os
    if settings.firebase_credentials_json:
        info = json.loads(settings.firebase_credentials_json)
    elif settings.firebase_credentials and os.path.isfile(settings.firebase_credentials):
        info = settings.firebase_credentials
    else:
        log.warning("Firebase service account not found: push disabled, in-app notifications still work")
        return None
    import firebase_admin
    from firebase_admin import credentials
    try:
        _app = firebase_admin.initialize_app(credentials.Certificate(info))
    except Exception:  # noqa - a bad key must never break the API
        log.exception("Firebase init failed: push disabled")
        return None
    log.info("Firebase push enabled (project %s)", _app.project_id)
    return _app


def _send_sync(token: str, title: str, body: str, data: dict) -> str:
    """Returns 'ok', 'invalid' (token is dead: delete it) or 'error'."""
    from firebase_admin import exceptions, messaging
    try:
        messaging.send(messaging.Message(
            token=token, notification=messaging.Notification(title=title, body=body),
            data={k: str(v) for k, v in data.items()},
            android=messaging.AndroidConfig(
                priority="high",
                notification=messaging.AndroidNotification(channel_id="new_offers") if data.get("type") == "NEW_OFFER" else None),
            apns=messaging.APNSConfig(headers={"apns-priority": "10"})), app=_app)
        return "ok"
    except messaging.UnregisteredError:
        return "invalid"
    except exceptions.InvalidArgumentError:
        return "invalid"
    except Exception:  # noqa
        log.exception("FCM send failed")
        return "error"


async def send_push(token: str, title: str, body: str, data: dict) -> str:
    """Sends through Firebase Admin SDK. Never raises: push is only a hint, the app re-fetches state from the API."""
    try:
        if not _firebase():
            return "disabled"
        result = await asyncio.to_thread(_send_sync, token, title, body, data)
    except Exception:  # noqa
        log.exception("push setup failed")
        return "error"
    if result == "invalid":
        async with SessionLocal() as db:  # dead token (app uninstalled / token rotated)
            await db.execute(delete(DeviceToken).where(DeviceToken.token == token))
            await db.commit()
    return result

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from ..constants import Role
from ..deps import approved_user
from ..models import User
from ..services.realtime import hub, sse_format

router = APIRouter(prefix="/api", tags=["realtime"])
PING_SECONDS = 15


async def event_stream(request: Request, user: User, ping_seconds: float = PING_SECONDS) -> AsyncIterator[str]:
    conn = hub.connect(user.id, user.role in Role.ADMINS)
    try:
        yield "retry: 3000\n: connected\n\n"
        while not await request.is_disconnected():
            try:
                payload = await asyncio.wait_for(conn.queue.get(), timeout=ping_seconds)
                yield sse_format(payload)
            except TimeoutError:
                yield ": ping\n\n"  # keeps proxies from closing an idle connection
    finally:
        hub.disconnect(conn)


@router.get("/stream")
async def stream(request: Request, user: User = Depends(approved_user)):
    """Server-Sent Events for any signed-in user. Admins get every order/subscription event; customers and drivers get only their own.
    Events are small notifications (type, id, status); the app refetches the order/subscription it names.
    Send the usual `Authorization: Bearer` header (EventSource cannot; use an SSE client that supports headers)."""
    return StreamingResponse(event_stream(request, user), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from app.services.sse_bus import sse_bus

router = APIRouter(prefix="/events", tags=["SSE"])


@router.get(
    "/subscribe",
    summary="Subscribe to real-time payment events",
    description=(
        "Long-lived SSE stream. Client receives `email_received`, "
        "`payment_status_changed`, and `reconciliation_completed` events. "
        "A `: keepalive` comment is sent every ~25 s to keep the connection alive."
    ),
)
async def subscribe_sse():
    queue = sse_bus.subscribe()
    return StreamingResponse(
        sse_bus.event_stream(queue),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # disable nginx buffering
        },
    )

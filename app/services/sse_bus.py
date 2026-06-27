import asyncio
import json
import logging
from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


class SSEBus:
    """In-process broadcast bus for SSE clients.

    All connected clients receive every published event (broadcast semantics).
    Slow clients whose queues fill up are automatically dropped.
    """

    def __init__(self):
        self._queues: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        try:
            self._queues.remove(queue)
        except ValueError:
            pass

    async def publish(self, event_type: str, data: dict) -> None:
        dead: list[asyncio.Queue] = []
        for q in self._queues:
            try:
                q.put_nowait({"event": event_type, "data": data})
            except asyncio.QueueFull:
                logger.warning("SSE queue full — dropping slow subscriber")
                dead.append(q)
        for q in dead:
            self.unsubscribe(q)

    async def event_stream(self, queue: asyncio.Queue) -> AsyncGenerator[str, None]:
        """Async generator consumed by FastAPI StreamingResponse."""
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(queue.get(), timeout=25)
                    yield f"event: {msg['event']}\ndata: {json.dumps(msg['data'])}\n\n"
                except asyncio.TimeoutError:
                    # Keepalive comment so proxies/browsers don't close the connection.
                    yield ": keepalive\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            self.unsubscribe(queue)


# Module-level singleton shared across the whole process.
sse_bus = SSEBus()

# -*- coding: utf-8 -*-
"""Event bus for broadcasting server-side events (SSE).

All events are broadcast as BusEvent instances with the opencode-style format:
    {"id": "<event_id>", "type": "<event_type>", "properties": {...}}

The SSE wire format mirrors opencode exactly: each event is encoded as
    event: message\\n
    id: <event_id>\\n
    data: {"id":...,"type":...,"properties":...}\\n\\n
"""

from __future__ import annotations

import asyncio
import orjson
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Event ID generator (opencode-style ascending evt_ IDs) ──────────

_id_lock = threading.Lock()
_id_counter = 0


def create_id() -> str:
    """Generate an opencode-style ascending event ID (``evt_`` prefix)."""
    global _id_counter
    with _id_lock:
        _id_counter += 1
        counter = _id_counter
    ts_hex = format(int(time.time() * 1000), "012x")
    cnt_hex = format(counter, "06x")
    return f"evt_{ts_hex}{cnt_hex}"


@dataclass
class BusEvent:
    """A structured event to be broadcast via SSE.

    Format matches opencode protocol:
        {"id": "evt_...", "type": "<event_type>", "properties": {...}}
    """

    type: str
    properties: Dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=create_id)

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "type": self.type, "properties": self.properties}

    def to_json(self) -> str:
        return orjson.dumps(self.to_dict()).decode("utf-8")

    def to_sse(self) -> str:
        """Encode as an opencode-compatible SSE frame.

        opencode uses a named ``message`` event plus an ``id`` line so the
        client can populate the ``Last-Event-ID`` header on reconnect.
        """
        return f"event: message\nid: {self.id}\ndata: {self.to_json()}\n\n"


# Upper bound on per-subscriber event backlogs.
#
# ``emit`` already handles ``asyncio.QueueFull`` ("SSE queue full, dropping
# event"), but the queues were created unbounded, so that branch could never
# fire and a stalled SSE client accumulated every event -- full tool outputs
# included -- for the lifetime of the process.  2048 events is far above the
# drain rate of a healthy client (it consumes continuously and only waits on
# the socket), so reaching the cap means the client is gone or wedged.
_ASYNC_QUEUE_MAXSIZE = 2048


class EventBus:
    """Simple pub/sub event bus supporting both sync and async subscribers."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: List[Callable[[BusEvent], None]] = []
        self._async_queues: List[asyncio.Queue[Optional[BusEvent]]] = []

    def subscribe(self, callback: Callable[[BusEvent], None]) -> Callable[[], None]:
        """Subscribe with a sync callback. Returns an unsubscribe function."""
        with self._lock:
            self._subscribers.append(callback)

        def _unsub() -> None:
            with self._lock:
                try:
                    self._subscribers.remove(callback)
                except ValueError:
                    pass

        return _unsub

    def create_async_queue(
        self, maxsize: int = _ASYNC_QUEUE_MAXSIZE
    ) -> asyncio.Queue[Optional[BusEvent]]:
        """Create a bounded asyncio queue that receives all events.

        Event ``id``s are advisory: the SSE endpoint does not replay a
        missed range on reconnect, so a dropped event is a gap either way.
        The bound exists purely so a stalled client cannot pin the whole
        server's event history in memory.

        Bounded on purpose: :meth:`emit` drops events for a subscriber that
        cannot keep up (see ``_ASYNC_QUEUE_MAXSIZE``) instead of retaining them
        forever.
        """
        q: asyncio.Queue[Optional[BusEvent]] = asyncio.Queue(maxsize=maxsize)
        with self._lock:
            self._async_queues.append(q)
        return q

    @staticmethod
    def wake_queue(q: asyncio.Queue[Optional[BusEvent]]) -> None:
        """Push the ``None`` sentinel, making room first if the queue is full.

        The sentinel terminates an SSE generator; it must never be lost to
        backpressure, so the oldest pending event is discarded when needed.
        """
        # Bounded retries: an emit storm on another thread could otherwise
        # keep refilling the queue under us indefinitely.
        for _ in range(8):
            try:
                q.put_nowait(None)
                return
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - concurrent drain
                    return
            except Exception:  # pragma: no cover - closed loop/queue
                return

    def remove_async_queue(self, q: asyncio.Queue[Optional[BusEvent]]) -> None:
        with self._lock:
            try:
                self._async_queues.remove(q)
            except ValueError:
                pass

    def get_all_queues(self) -> List[asyncio.Queue[Optional[BusEvent]]]:
        with self._lock:
            return list(self._async_queues)

    def emit(self, event: BusEvent) -> None:
        """Emit an event to all subscribers."""
        with self._lock:
            subs = list(self._subscribers)
            queues = list(self._async_queues)

        for cb in subs:
            try:
                cb(event)
            except Exception:
                logger.debug("Event subscriber error", exc_info=True)

        for q in queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                logger.warning("SSE queue full, dropping event")
            except Exception:
                logger.debug("Queue put error", exc_info=True)

    def emit_type(self, event_type: str, **properties: Any) -> None:
        """Convenience: emit by type name and keyword properties."""
        self.emit(BusEvent(type=event_type, properties=properties))


# Global singleton
bus = EventBus()

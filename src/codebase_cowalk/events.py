"""Event bus.

Two channels:

1. **In-process pub/sub** — used by the HTTP server's SSE endpoint to push live
   updates to the browser when Claude appends a block, marks a chunk analyzed, etc.

2. **Cross-process JSONL log** — used by the Monitor: every user-driven event in
   the browser (rerequest, comment, status toggle) gets appended to events.jsonl,
   and the codebase-cowalk-tail script forwards new lines to Claude as Monitor
   notifications.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from .paths import events_path


# ---------------------------------------------------------------------------
# Cross-process JSONL log (Monitor channel)
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def append_event_log(event: dict[str, Any]) -> None:
    """Append a JSON line to the global events.jsonl. The codebase-cowalk-tail
    script tails this file and emits each line to Monitor."""
    line = json.dumps({"ts": time.time(), **event}, ensure_ascii=False)
    p = events_path()
    with _log_lock:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()


# ---------------------------------------------------------------------------
# In-process pub/sub for SSE
# ---------------------------------------------------------------------------


class SSEHub:
    """A tiny pub/sub. One hub per session — kept alive by the HTTP server."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()

    async def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=256)
        async with self._lock:
            self._subscribers.append(q)
        return q

    async def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        async with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    async def publish(self, event_type: str, data: dict[str, Any]) -> None:
        payload = f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        async with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(payload)
            except asyncio.QueueFull:
                pass

    def publish_sync(self, event_type: str, data: dict[str, Any]) -> None:
        """Schedule a publish from a non-async context (e.g. MCP tool handler)."""
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            return
        if loop.is_running():
            asyncio.ensure_future(self.publish(event_type, data))
        else:
            loop.run_until_complete(self.publish(event_type, data))

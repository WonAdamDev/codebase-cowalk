"""Per-session HTTP server.

Runs in a background thread with its own asyncio loop, separate from the MCP stdio
loop. Exposes:

- `GET  /`             — full page (Jinja-rendered, hydrates state.json)
- `GET  /static/*`     — CSS/JS
- `GET  /sse`          — Server-Sent Events stream for live updates
- `POST /api/status`   — toggle ✓/🚩/❓
- `POST /api/comment`  — add a freeform comment
- `POST /api/rerequest`— enqueue a re-explanation request

User actions also append to the global events.jsonl, which the codebase-cowalk-tail
script tails for the Monitor.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from aiohttp import web

from .events import SSEHub, append_event_log
from .renderer import build_state, render_index
from .store import SessionStore


class HttpServer:
    def __init__(self, store: SessionStore, port: int, *, read_only: bool = False) -> None:
        self.store = store
        self.port = port
        self.read_only = read_only
        self.hub = SSEHub()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._stopped = threading.Event()

    # public ----------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._serve, name=f"cowalk-http-{self.port}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._loop and self._runner:
            asyncio.run_coroutine_threadsafe(self._runner.cleanup(), self._loop)
        if self._loop:
            self._loop.call_soon_threadsafe(self._loop.stop)

    def publish(self, event_type: str, data: dict[str, Any]) -> None:
        """Schedule an SSE publish from any thread."""
        if not self._loop:
            return
        asyncio.run_coroutine_threadsafe(self.hub.publish(event_type, data), self._loop)

    # internals -------------------------------------------------------------

    def _serve(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = self._build_app()
        runner = web.AppRunner(app)
        self._runner = runner
        self._loop.run_until_complete(runner.setup())
        site = web.TCPSite(runner, "127.0.0.1", self.port)
        self._loop.run_until_complete(site.start())
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(runner.cleanup())
            self._loop.close()

    def _build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/sse", self._sse)
        app.router.add_post("/api/status", self._api_status)
        app.router.add_post("/api/comment", self._api_comment)
        app.router.add_post("/api/rerequest", self._api_rerequest)
        static_dir = Path(__file__).parent / "static"
        app.router.add_static("/static", path=str(static_dir), show_index=False)
        return app

    # state assembly --------------------------------------------------------

    def _build_state(self) -> dict[str, Any]:
        session = self.store.get_session() or {}
        chunks = self.store.list_chunks()
        chunks_full = [self.store.get_chunk(c["id"]) for c in chunks]
        chunks_full = [c for c in chunks_full if c]
        chunk_payload = []
        chunk_code_by_id: dict[str, str] = {}
        blocks_by_chunk: dict[str, list[dict[str, Any]]] = {}
        comments_by_chunk: dict[str, list[dict[str, Any]]] = {}
        for c in chunks_full:
            chunk_code_by_id[c["id"]] = c["code"]
            blocks_by_chunk[c["id"]] = self.store.list_blocks(c["id"])
            comments_by_chunk[c["id"]] = self.store.list_comments(c["id"])
            chunk_payload.append({
                "id": c["id"],
                "parent_id": c.get("parent_id"),
                "file_path": c["file_path"],
                "symbol_path": c.get("symbol_path"),
                "language": c.get("language"),
                "line_start": c["line_start"],
                "line_end": c["line_end"],
                "status": c["status"],
                "review_status": c.get("review_status"),
                "diff_added_lines": c.get("diff_added_lines"),
                "diff_removed_lines": c.get("diff_removed_lines"),
                "has_diff": bool(c.get("diff_added_lines") or c.get("diff_removed_lines")),
                "sequence": c["sequence"],
                "lesson_order": c.get("lesson_order"),
                "layer": c.get("layer"),
            })
        # File-level snapshots for whole-file context. The UI shows the entire
        # file in the center pane and highlights the active chunk's line range.
        # Chunks whose file_path has no snapshot fall back to chunk_code_by_id.
        file_sources_by_path: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        for c in chunks_full:
            fp = c["file_path"]
            if fp in seen:
                continue
            seen.add(fp)
            snap = self.store.get_file_snapshot(fp)
            if snap:
                file_sources_by_path[fp] = {
                    "content": snap["content"],
                    "line_count": snap["line_count"],
                    "language": snap.get("language"),
                }
        return build_state(
            session=session,
            chunks=chunk_payload,
            blocks_by_chunk=blocks_by_chunk,
            comments_by_chunk=comments_by_chunk,
            progress=self.store.progress(),
            chunk_code_by_id=chunk_code_by_id,
            file_sources_by_path=file_sources_by_path,
        )

    # handlers --------------------------------------------------------------

    async def _index(self, request: web.Request) -> web.Response:
        state = self._build_state()
        html = render_index(state, is_live=not self.read_only, read_only=self.read_only)
        return web.Response(text=html, content_type="text/html")

    async def _sse(self, request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
        await resp.prepare(request)
        q = await self.hub.subscribe()
        try:
            await resp.write(b": connected\n\n")
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20.0)
                    await resp.write(payload.encode("utf-8"))
                except asyncio.TimeoutError:
                    await resp.write(b": ping\n\n")
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await self.hub.unsubscribe(q)
        return resp

    async def _api_status(self, request: web.Request) -> web.Response:
        if self.read_only:
            return web.Response(status=403, text="read-only")
        body = await request.json()
        chunk_id = body["chunk_id"]
        status = body.get("status") or None
        self.store.mark_chunk_review(chunk_id, status)
        self.store.touch()
        append_event_log({
            "session_id": self.store.session_id,
            "type": "status",
            "chunk_id": chunk_id,
            "status": status,
        })
        chunk = self.store.get_chunk(chunk_id)
        if chunk:
            await self.hub.publish("chunk_status", {
                "id": chunk_id,
                "review_status": chunk.get("review_status"),
            })
        await self.hub.publish("progress", self.store.progress())
        return web.json_response({"ok": True})

    async def _api_comment(self, request: web.Request) -> web.Response:
        if self.read_only:
            return web.Response(status=403, text="read-only")
        body = await request.json()
        chunk_id = body["chunk_id"]
        text = body["body"]
        self.store.add_comment(chunk_id, text)
        self.store.touch()
        comments = self.store.list_comments(chunk_id)
        latest = comments[-1] if comments else None
        append_event_log({
            "session_id": self.store.session_id,
            "type": "comment",
            "chunk_id": chunk_id,
            "body": text,
        })
        if latest:
            await self.hub.publish("comment_added", {"chunk_id": chunk_id, **latest})
        return web.json_response({"ok": True})

    async def _api_rerequest(self, request: web.Request) -> web.Response:
        if self.read_only:
            return web.Response(status=403, text="read-only")
        body = await request.json()
        chunk_id = body["chunk_id"]
        note = body.get("note") or ""
        self.store.push_event("rerequest", chunk_id, {"note": note})
        append_event_log({
            "session_id": self.store.session_id,
            "type": "rerequest",
            "chunk_id": chunk_id,
            "note": note,
            "session_slug": (self.store.get_session() or {}).get("slug"),
        })
        return web.json_response({"ok": True})

"""codebase-cowalk MCP server.

Exposes the tools that the walk-codebase / export-codewalk / view-codewalk skills
call. State lives in per-session sqlite DBs under ${CLAUDE_PLUGIN_DATA}/sessions/.
HTTP servers (one per active session) live in background threads.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP

from .chunker import ChunkSpec, chunk_files, hash_code, split_chunk_by_ranges
from .export import (
    cleanup_view,
    export_session as do_export,
    import_export as do_import_export,
    open_export as do_open_export,
)
from .http_server import HttpServer
from .session import (
    ScopeEntry,
    find_free_port,
    ingest_scope,
    make_session_id,
    make_slug,
    parse_scope_entries,
    scope_summary,
)
from .store import (
    SessionStore,
    delete_session as do_delete_session,
    find_session_by_slug,
    list_all_sessions,
    rename_session as do_rename_session,
)


mcp = FastMCP("codebase-cowalk")

# session_id -> running HttpServer (live or read-only view)
_servers: dict[str, HttpServer] = {}
# session_id -> extract dir (for view sessions)
_view_extract_dirs: dict[str, str] = {}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _ensure_workdir(workdir: str | None) -> Path:
    if workdir:
        return Path(workdir).resolve()
    return Path(os.getcwd()).resolve()


def _publish(session_id: str, event_type: str, data: dict[str, Any]) -> None:
    srv = _servers.get(session_id)
    if srv:
        srv.publish(event_type, data)


def _store(session_id: str) -> SessionStore:
    return SessionStore(session_id)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------


@mcp.tool()
def propose_scope(files: list[Any], workdir: str | None = None) -> dict[str, Any]:
    """Parse a candidate scope with Tree-sitter and return a preview without
    persisting anything. Use this before `start_session` so the user can confirm
    the chunk count before paying token cost.

    `files` is a list of either bare paths (strings) or dicts of the form
    `{path, line_ranges?, diff_added_lines?, diff_removed_lines?}`. Paths can be
    relative to `workdir` (or, if omitted, the current working directory).
    """
    wd = _ensure_workdir(workdir)
    entries = parse_scope_entries(files)
    paths: list[Path] = []
    for e in entries:
        p = Path(e.path)
        if not p.is_absolute():
            p = (wd / e.path).resolve()
        if p.exists() and p.is_file():
            paths.append(p)
    chunks = chunk_files(paths)
    return {
        "summary": scope_summary(chunks),
        "chunks_preview": [
            {
                "id_preview": c.chunk_id,
                "file_path": c.file_path,
                "symbol_path": c.symbol_path,
                "language": c.language,
                "line_start": c.line_start,
                "line_end": c.line_end,
            }
            for c in chunks
        ],
        "files_resolved": [str(p) for p in paths],
        "files_missing": [e.path for e in entries if not (wd / e.path).resolve().exists()],
    }


@mcp.tool()
def start_session(
    request: str,
    scope: list[Any] | None = None,
    workdir: str | None = None,
    diff_mode: bool = False,
    vcs: str | None = None,
    resume: str | None = None,
) -> dict[str, Any]:
    """Start a new codewalk session (or resume an existing one by slug or id) and
    spin up an HTTP server on a free port. Returns the URL to give to the user.

    To resume, pass `resume=<slug or session_id>` and omit `scope`.
    """
    if resume:
        # accept either a session_id or a slug
        candidate_store = SessionStore(resume)
        if candidate_store.get_session():
            session_id = resume
        else:
            maybe_id = find_session_by_slug(resume)
            if not maybe_id:
                raise ValueError(f"unknown session to resume: {resume}")
            session_id = maybe_id
        store = SessionStore(session_id)
    else:
        if scope is None:
            raise ValueError("scope is required for a new session (omit only when resume= is set)")
        wd = _ensure_workdir(workdir)
        session_id = make_session_id()
        slug = make_slug(request)
        store = SessionStore(session_id)
        entries = parse_scope_entries(scope)
        chunks = ingest_scope(store, wd, entries)
        store.init_session(
            slug=slug,
            request=request,
            workdir=str(wd),
            diff_mode=diff_mode,
            vcs=vcs,
            scope_summary=scope_summary(chunks),
        )

    # bring up HTTP server
    if session_id in _servers:
        _servers[session_id].stop()
    port = find_free_port()
    srv = HttpServer(store, port=port)
    srv.start()
    _servers[session_id] = srv

    sess = store.get_session() or {}
    return {
        "session_id": session_id,
        "slug": sess.get("slug"),
        "port": port,
        "url": f"http://localhost:{port}",
        "progress": store.progress(),
        "chunk_count": len(store.list_chunks()),
    }


@mcp.tool()
def list_chunks(session_id: str) -> list[dict[str, Any]]:
    """List all chunks in a session (id, file, symbol, status, line range)."""
    return _store(session_id).list_chunks()


@mcp.tool()
def get_chunk(session_id: str, chunk_id: str) -> dict[str, Any] | None:
    """Fetch a chunk's full record including its source code snapshot. Use this
    to read code before pushing explanation blocks for it."""
    return _store(session_id).get_chunk(chunk_id)


@mcp.tool()
def append_block(
    session_id: str,
    chunk_id: str,
    block_type: str,
    content: str,
    line_ref_start: int | None = None,
    line_ref_end: int | None = None,
    new_version: bool = False,
) -> dict[str, Any]:
    """Append an explanation block to a chunk and push it to the live page.

    `block_type` should be one of:
      summary, intent, behavior, risk, related, diagram, code_ref, warning, note

    Set `new_version=True` to start a new version of the chunk's blocks (used
    when the user requested a re-explanation — old blocks are preserved as
    history). Within the same version, call append_block multiple times in the
    order you want them displayed.
    """
    valid = {"summary", "intent", "behavior", "risk", "related", "diagram", "code_ref", "warning", "note"}
    if block_type not in valid:
        raise ValueError(f"invalid block_type {block_type!r}; expected one of {sorted(valid)}")
    store = _store(session_id)
    if new_version:
        version = store.bump_version(chunk_id)
    else:
        cur = store.current_version(chunk_id)
        version = cur if cur > 0 else 1
    block_id = store.append_block(
        chunk_id=chunk_id,
        version=version,
        block_type=block_type,
        content=content,
        line_ref_start=line_ref_start,
        line_ref_end=line_ref_end,
    )
    store.touch()
    blocks = store.list_blocks(chunk_id)
    latest = blocks[-1]
    _publish(session_id, "block_added", {"chunk_id": chunk_id, **latest})
    return {"block_id": block_id, "version": version}


@mcp.tool()
def split_chunk(
    session_id: str,
    chunk_id: str,
    ranges: list[list[int]],
) -> list[dict[str, Any]]:
    """Split a chunk into smaller sub-chunks at the given line ranges. Marks the
    parent as `status='split'` and adds children with ids `<parent>.1`, `.2`, ...

    `ranges` is a list of `[line_start, line_end]` pairs (1-indexed, inclusive,
    in the same coordinate system as the parent chunk).
    """
    store = _store(session_id)
    parent = store.get_chunk(chunk_id)
    if not parent:
        raise ValueError(f"unknown chunk {chunk_id}")
    parent_spec = ChunkSpec(
        chunk_id=parent["id"],
        file_path=parent["file_path"],
        symbol_path=parent.get("symbol_path"),
        language=parent.get("language"),
        line_start=parent["line_start"],
        line_end=parent["line_end"],
        code=parent["code"],
        code_hash=parent["code_hash"],
        sequence=parent["sequence"],
    )
    range_tuples = [(int(r[0]), int(r[1])) for r in ranges]
    children = split_chunk_by_ranges(parent_spec, range_tuples)
    for child in children:
        store.add_chunk(
            chunk_id=child.chunk_id,
            file_path=child.file_path,
            symbol_path=child.symbol_path,
            language=child.language,
            line_start=child.line_start,
            line_end=child.line_end,
            code=child.code,
            code_hash=child.code_hash,
            sequence=child.sequence,
            parent_id=parent["id"],
        )
        _publish(session_id, "chunk_added", {
            "id": child.chunk_id,
            "parent_id": parent["id"],
            "file_path": child.file_path,
            "symbol_path": child.symbol_path,
            "language": child.language,
            "line_start": child.line_start,
            "line_end": child.line_end,
            "status": "pending",
            "review_status": None,
            "sequence": child.sequence,
            "code": child.code,
        })
    store.mark_chunk_status(parent["id"], "split")
    _publish(session_id, "chunk_status", {"id": parent["id"], "status": "split"})
    store.touch()
    return [
        {
            "id": c.chunk_id,
            "file_path": c.file_path,
            "line_start": c.line_start,
            "line_end": c.line_end,
        }
        for c in children
    ]


@mcp.tool()
def set_chunk_analyzed(session_id: str, chunk_id: str) -> dict[str, Any]:
    """Mark a chunk as analyzed (no more blocks coming for now). Updates the
    progress bars on the live page."""
    store = _store(session_id)
    store.mark_chunk_status(chunk_id, "analyzed")
    store.touch()
    _publish(session_id, "chunk_status", {"id": chunk_id, "status": "analyzed"})
    _publish(session_id, "progress", store.progress())
    return {"ok": True}


@mcp.tool()
def add_chunk(
    session_id: str,
    file_path: str,
    line_start: int,
    line_end: int,
    code: str | None = None,
    symbol_path: str | None = None,
    language: str | None = None,
) -> dict[str, Any]:
    """Manually register a chunk that the Tree-sitter chunker did not produce.
    Rare — only use when you need to insert a non-AST region or a chunk for a
    file outside the original scope."""
    store = _store(session_id)
    existing = store.list_chunks()
    seq = max((c["sequence"] for c in existing), default=-1) + 1
    chunk_id = f"c-{seq:04d}"
    if code is None:
        try:
            text = Path(file_path).read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines(keepends=True)
            code = "".join(lines[line_start - 1 : line_end])
        except OSError as exc:
            raise ValueError(f"cannot read file {file_path}: {exc}")
    store.add_chunk(
        chunk_id=chunk_id,
        file_path=file_path,
        symbol_path=symbol_path,
        language=language,
        line_start=line_start,
        line_end=line_end,
        code=code,
        code_hash=hash_code(code),
        sequence=seq,
    )
    _publish(session_id, "chunk_added", {
        "id": chunk_id, "parent_id": None, "file_path": file_path, "symbol_path": symbol_path,
        "language": language, "line_start": line_start, "line_end": line_end,
        "status": "pending", "review_status": None, "sequence": seq, "code": code,
    })
    return {"chunk_id": chunk_id}


@mcp.tool()
def pop_event(session_id: str) -> dict[str, Any] | None:
    """Pop the next pending user event (rerequest / status / comment). Returns
    None when the queue is empty. Combine with the Monitor stream: the Monitor
    notifies you when something is in the queue; pop_event drains it."""
    return _store(session_id).pop_event()


@mcp.tool()
def list_sessions() -> list[dict[str, Any]]:
    """List all known codewalk sessions (across all working directories), most
    recently updated first."""
    return list_all_sessions()


@mcp.tool()
def end_session(session_id: str) -> dict[str, Any]:
    """Stop the HTTP server for a session. The DB is preserved on disk and can
    be reopened with `start_session(resume=...)`."""
    srv = _servers.pop(session_id, None)
    if srv:
        srv.stop()
    extract = _view_extract_dirs.pop(session_id, None)
    if extract:
        cleanup_view(extract)
    return {"ok": True}


@mcp.tool()
def delete_session(session_id: str) -> dict[str, Any]:
    """Permanently delete a session's data. Stops the server first if running."""
    srv = _servers.pop(session_id, None)
    if srv:
        srv.stop()
    ok = do_delete_session(session_id)
    return {"ok": ok}


@mcp.tool()
def rename_session(session_id: str, new_slug: str) -> dict[str, Any]:
    """Rename a session's slug (used in URLs and exported filenames)."""
    ok = do_rename_session(session_id, new_slug)
    return {"ok": ok}


@mcp.tool()
def export_session(slug: str | None = None, session_id: str | None = None,
                   formats: list[str] | None = None) -> dict[str, Any]:
    """Export a session to .cwlk archive and self-contained .html. Pass either
    `slug` or `session_id`. Default formats: ["cwlk", "html"]."""
    if not session_id:
        if not slug:
            raise ValueError("provide slug or session_id")
        sid = find_session_by_slug(slug)
        if not sid:
            raise ValueError(f"unknown slug {slug}")
        session_id = sid
    return do_export(session_id, formats)


@mcp.tool()
def open_export(path: str) -> dict[str, Any]:
    """Open an exported `.cwlk` file in read-only viewer mode. Spins up a fresh
    HTTP server on a free port and returns the URL."""
    info = do_open_export(path)
    extract_dir = info["extract_dir"]
    session_id = info["session_id"]
    # point the SessionStore at the unpacked DB by symlink/copy: extract dir already has session.db
    # We need SessionStore.db_path to resolve to extract_dir/session.db. Easiest: copy the DB into
    # session_dir(session_id) so SessionStore picks it up by id naturally.
    from .paths import session_dir as session_dir_fn
    target = session_dir_fn(session_id) / "session.db"
    if not target.exists():
        import shutil
        shutil.copy(Path(extract_dir) / "session.db", target)
    store = SessionStore(session_id)
    if session_id in _servers:
        _servers[session_id].stop()
    port = find_free_port()
    srv = HttpServer(store, port=port, read_only=True)
    srv.start()
    _servers[session_id] = srv
    _view_extract_dirs[session_id] = extract_dir
    return {
        "session_id": session_id,
        "slug": info["slug"],
        "port": port,
        "url": f"http://localhost:{port}",
    }


@mcp.tool()
def import_codewalk(path: str) -> dict[str, Any]:
    """Import a `.cwlk` archive into a new editable session and start its HTTP
    server.

    Use this when you want to take someone else's exported codewalk and run
    your own independent review on top of it: their explanation blocks are
    preserved (no need to wait for re-analysis), but review status and comments
    start fresh under your name.

    For purely viewing someone else's review (no editing), use `open_export`.
    """
    info = do_import_export(path)
    session_id = info["session_id"]
    store = SessionStore(session_id)
    if session_id in _servers:
        _servers[session_id].stop()
    port = find_free_port()
    srv = HttpServer(store, port=port)
    srv.start()
    _servers[session_id] = srv
    return {
        "session_id": session_id,
        "slug": info["slug"],
        "original_slug": info["original_slug"],
        "port": port,
        "url": f"http://localhost:{port}",
        "chunk_count": len(store.list_chunks()),
    }


@mcp.tool()
def end_view(session_id: str | None = None, slug: str | None = None) -> dict[str, Any]:
    """Stop a read-only viewer session and clean up its temp directory."""
    sid = session_id
    if not sid and slug:
        sid = find_session_by_slug(slug)
    if not sid:
        return {"ok": False}
    srv = _servers.pop(sid, None)
    if srv:
        srv.stop()
    extract = _view_extract_dirs.pop(sid, None)
    if extract:
        cleanup_view(extract)
    return {"ok": True}


# ---------------------------------------------------------------------------
# entry
# ---------------------------------------------------------------------------
# `mcp` (a FastMCP instance) is exported. `__main__.main()` calls `mcp.run()`,
# which sets up stdio transport and blocks until the client disconnects.

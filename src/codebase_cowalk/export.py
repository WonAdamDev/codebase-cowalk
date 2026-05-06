"""Export, view (read-only), and import (re-walk) for `.cwlk` archives."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from .paths import exports_root, session_dir
from .renderer import render_static_export
from .store import SessionStore


def export_session(session_id: str, formats: list[str] | None = None) -> dict[str, Any]:
    formats = formats or ["cwlk", "html"]
    store = SessionStore(session_id)
    session = store.get_session()
    if not session:
        raise ValueError(f"unknown session {session_id}")
    slug = session["slug"]
    out_dir = exports_root()
    result: dict[str, str] = {}

    state = _build_full_state(store)

    if "html" in formats:
        html = render_static_export(state)
        html_path = out_dir / f"{slug}.html"
        html_path.write_text(html, encoding="utf-8")
        result["html"] = str(html_path)

    if "cwlk" in formats:
        cwlk_path = out_dir / f"{slug}.cwlk"
        with zipfile.ZipFile(cwlk_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.write(store.db_path, arcname="session.db")
            zf.writestr("manifest.json", json.dumps({
                "slug": slug,
                "session_id": session_id,
                "format": "codebase-cowalk/v0.1",
                "created_at": session.get("created_at"),
                "updated_at": session.get("updated_at"),
            }, indent=2))
        result["cwlk"] = str(cwlk_path)

    return result


def open_export(path: str | Path) -> dict[str, Any]:
    """Unpack a .cwlk into a temporary read-only working dir. Returns the
    extracted session_id and a temp directory path; the caller (server.py) wires
    a read-only HttpServer to that session DB on a free port."""
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(str(src))
    tmp_root = Path(tempfile.mkdtemp(prefix="cowalk-view-"))
    with zipfile.ZipFile(src, "r") as zf:
        zf.extractall(tmp_root)
    manifest = json.loads((tmp_root / "manifest.json").read_text(encoding="utf-8"))
    return {"session_id": manifest["session_id"], "slug": manifest["slug"], "extract_dir": str(tmp_root)}


def cleanup_view(extract_dir: str | Path) -> None:
    p = Path(extract_dir)
    if p.exists() and p.name.startswith("cowalk-view-"):
        shutil.rmtree(p, ignore_errors=True)


def import_export(path: str | Path) -> dict[str, Any]:
    """Import a `.cwlk` into a new, editable session.

    Original chunks and explanation blocks are copied (so the reviewer doesn't
    have to wait for re-analysis). Review state and comments are reset, so the
    new reviewer starts with a clean slate. Returns the new `session_id` and
    `slug`.
    """
    from .session import make_session_id  # local import: avoid cycle

    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(str(src))
    tmp = Path(tempfile.mkdtemp(prefix="cowalk-import-"))
    try:
        with zipfile.ZipFile(src, "r") as zf:
            zf.extractall(tmp)
        manifest = json.loads((tmp / "manifest.json").read_text(encoding="utf-8"))
        original_slug = manifest.get("slug", "imported")

        new_session_id = make_session_id()
        ts = time.strftime("%Y%m%d-%H%M%S", time.localtime())
        new_slug = f"{original_slug}-imported-{ts}"

        src_db = tmp / "session.db"
        if not src_db.exists():
            raise ValueError("imported archive has no session.db")

        dst_store = SessionStore(new_session_id)
        src_cx = sqlite3.connect(str(src_db))
        src_cx.row_factory = sqlite3.Row
        try:
            session_row = src_cx.execute("SELECT * FROM session").fetchone()
            if not session_row:
                raise ValueError("source DB has no session row")

            dst_store.init_session(
                slug=new_slug,
                request=f"imported from {original_slug}: {session_row['request']}",
                workdir=session_row["workdir"],
                diff_mode=bool(session_row["diff_mode"]),
                vcs=session_row["vcs"],
                scope_summary=(
                    json.loads(session_row["scope_summary"]) if session_row["scope_summary"] else None
                ),
            )

            for r in src_cx.execute("SELECT * FROM chunks ORDER BY sequence ASC").fetchall():
                dst_store.add_chunk(
                    chunk_id=r["id"],
                    file_path=r["file_path"],
                    symbol_path=r["symbol_path"],
                    language=r["language"],
                    line_start=r["line_start"],
                    line_end=r["line_end"],
                    code=r["code"],
                    code_hash=r["code_hash"],
                    sequence=r["sequence"],
                    parent_id=r["parent_id"],
                    diff_added_lines=(json.loads(r["diff_added_lines"]) if r["diff_added_lines"] else None),
                    diff_removed_lines=(json.loads(r["diff_removed_lines"]) if r["diff_removed_lines"] else None),
                )
                # preserve analyzed / split state, but never review_status
                if r["status"] and r["status"] != "pending":
                    dst_store.mark_chunk_status(r["id"], r["status"])

            for r in src_cx.execute(
                "SELECT * FROM blocks ORDER BY chunk_id, version, sequence"
            ).fetchall():
                dst_store.append_block(
                    chunk_id=r["chunk_id"],
                    version=r["version"],
                    block_type=r["block_type"],
                    content=r["content"],
                    line_ref_start=r["line_ref_start"],
                    line_ref_end=r["line_ref_end"],
                )
        finally:
            src_cx.close()

        return {
            "session_id": new_session_id,
            "slug": new_slug,
            "original_slug": original_slug,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _build_full_state(store: SessionStore) -> dict[str, Any]:
    """Produce the same `state` shape as HttpServer._build_state, but freestanding
    (no aiohttp imports needed)."""
    session = store.get_session() or {}
    chunks = store.list_chunks()
    chunks_full = [store.get_chunk(c["id"]) for c in chunks]
    chunks_full = [c for c in chunks_full if c]
    chunk_payload = []
    chunk_code_by_id: dict[str, str] = {}
    blocks_by_chunk: dict[str, list[dict[str, Any]]] = {}
    comments_by_chunk: dict[str, list[dict[str, Any]]] = {}
    for c in chunks_full:
        chunk_code_by_id[c["id"]] = c["code"]
        blocks_by_chunk[c["id"]] = store.list_blocks(c["id"])
        comments_by_chunk[c["id"]] = store.list_comments(c["id"])
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
        })
    return {
        "session": session,
        "chunks": chunk_payload,
        "blocks_by_chunk": blocks_by_chunk,
        "comments_by_chunk": comments_by_chunk,
        "progress": store.progress(),
        "chunk_code_by_id": chunk_code_by_id,
    }

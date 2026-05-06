"""Export and view (.cwlk archive + self-contained HTML)."""

from __future__ import annotations

import json
import shutil
import tempfile
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

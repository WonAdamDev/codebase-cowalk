"""HTML rendering.

Two output modes:

- **live**: page is served by `http_server`. Initial HTML carries the current state;
  thereafter SSE pushes incremental updates and the page mutates client-side.
- **static**: a single self-contained HTML string with all CSS/JS/data inlined,
  used for the `.html` artifact in `export_session`.

Both modes share the same Jinja templates; the template branches on `is_live`.
"""

from __future__ import annotations

import importlib.resources
import json
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


def _templates_dir() -> Path:
    return Path(__file__).parent / "templates"


def _static_dir() -> Path:
    return Path(__file__).parent / "static"


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(_templates_dir())),
        autoescape=select_autoescape(["html"]),
    )


def render_index(state: dict[str, Any], *, is_live: bool, read_only: bool = False) -> str:
    env = _env()
    tpl = env.get_template("index.html")
    if is_live and not read_only:
        css_inline = None
        js_inline = None
    else:
        css_inline = (_static_dir() / "app.css").read_text(encoding="utf-8")
        js_inline = (_static_dir() / "app.js").read_text(encoding="utf-8")
    return tpl.render(
        state=state,
        state_json=json.dumps(state, ensure_ascii=False),
        is_live=is_live,
        read_only=read_only,
        css_inline=css_inline,
        js_inline=js_inline,
    )


def render_static_export(state: dict[str, Any]) -> str:
    """Self-contained HTML for sharing. No external requests, no SSE."""
    return render_index(state, is_live=False, read_only=True)


def build_state(
    session: dict[str, Any],
    chunks: list[dict[str, Any]],
    blocks_by_chunk: dict[str, list[dict[str, Any]]],
    comments_by_chunk: dict[str, list[dict[str, Any]]],
    progress: dict[str, int],
    chunk_code_by_id: dict[str, str],
    file_sources_by_path: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the JSON state object that the page consumes (both live and static)."""
    return {
        "session": session,
        "chunks": chunks,
        "blocks_by_chunk": blocks_by_chunk,
        "comments_by_chunk": comments_by_chunk,
        "progress": progress,
        "chunk_code_by_id": chunk_code_by_id,
        "file_sources_by_path": file_sources_by_path or {},
    }

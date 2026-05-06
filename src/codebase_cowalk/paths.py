"""Resolve plugin data paths.

In production, ${CLAUDE_PLUGIN_DATA} is set by Claude Code and forwarded to us via the
CODEBASE_COWALK_DATA env var (see .mcp.json). When running outside the plugin (tests,
local dev, `codebase-cowalk-tail` standalone), we fall back to ~/.codebase-cowalk/.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_root() -> Path:
    raw = os.environ.get("CODEBASE_COWALK_DATA") or os.environ.get("CLAUDE_PLUGIN_DATA")
    if raw:
        root = Path(raw)
    else:
        root = Path.home() / ".codebase-cowalk"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sessions_root() -> Path:
    p = data_root() / "sessions"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cache_root() -> Path:
    p = data_root() / "cache"
    p.mkdir(parents=True, exist_ok=True)
    return p


def exports_root() -> Path:
    p = data_root() / "exports"
    p.mkdir(parents=True, exist_ok=True)
    return p


def events_path() -> Path:
    """Single global events.jsonl that the Monitor tails for the whole plugin.

    Each line is a JSON object with at least {session_id, type, ...}, so the consumer
    can fan it out to the right session.
    """
    p = data_root() / "events.jsonl"
    if not p.exists():
        p.touch()
    return p


def session_dir(session_id: str) -> Path:
    p = sessions_root() / session_id
    p.mkdir(parents=True, exist_ok=True)
    return p

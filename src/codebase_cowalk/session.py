"""Session lifecycle: id/slug generation, scope reduction, chunk ingestion."""

from __future__ import annotations

import re
import socket
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .chunker import ChunkSpec, chunk_files
from .store import SessionStore


def make_session_id() -> str:
    return "s_" + uuid.uuid4().hex[:12]


def make_slug(request: str, when: float | None = None) -> str:
    when = when or time.time()
    base = re.sub(r"[^a-zA-Z0-9-]+", "-", request.strip().lower()).strip("-")
    base = re.sub(r"-{2,}", "-", base)
    if not base:
        base = "codewalk"
    base = base[:48]
    ts = time.strftime("%Y%m%d-%H%M%S", time.localtime(when))
    return f"{base}-{ts}"


def find_free_port() -> int:
    """Ask the OS for a free port. Tiny race window between bind/release and the
    HTTP server picking it up; in practice this is fine for localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass
class ScopeEntry:
    """One entry in a proposed scope. Either a whole file, or a file with line ranges."""

    path: str
    line_ranges: list[tuple[int, int]] = field(default_factory=list)
    diff_added_lines: list[int] = field(default_factory=list)
    diff_removed_lines: list[int] = field(default_factory=list)


def parse_scope_entries(raw: list[Any]) -> list[ScopeEntry]:
    out: list[ScopeEntry] = []
    for r in raw:
        if isinstance(r, str):
            out.append(ScopeEntry(path=r))
        elif isinstance(r, dict):
            out.append(
                ScopeEntry(
                    path=str(r["path"]),
                    line_ranges=[tuple(rr) for rr in r.get("line_ranges", [])],
                    diff_added_lines=list(r.get("diff_added_lines", [])),
                    diff_removed_lines=list(r.get("diff_removed_lines", [])),
                )
            )
    return out


def ingest_scope(
    store: SessionStore,
    workdir: Path,
    scope: list[ScopeEntry],
) -> list[ChunkSpec]:
    """Resolve scope entries to absolute paths, run the chunker, persist chunks."""
    paths: list[Path] = []
    diff_index: dict[str, ScopeEntry] = {}
    for e in scope:
        p = Path(e.path)
        if not p.is_absolute():
            p = (workdir / e.path).resolve()
        if p.exists() and p.is_file():
            paths.append(p)
            diff_index[str(p)] = e

    chunks = chunk_files(paths)

    # If the scope entry restricts to specific line ranges, filter chunks that overlap.
    filtered: list[ChunkSpec] = []
    for c in chunks:
        e = diff_index.get(c.file_path)
        if e and e.line_ranges:
            keep = any(_overlaps(c.line_start, c.line_end, rs, re_) for rs, re_ in e.line_ranges)
            if not keep:
                continue
        if e:
            # attach diff lines if any
            c.diff_added_lines = [
                ln for ln in e.diff_added_lines if c.line_start <= ln <= c.line_end
            ] or None
            c.diff_removed_lines = [
                ln for ln in e.diff_removed_lines if c.line_start <= ln <= c.line_end
            ] or None
        filtered.append(c)

    # Re-number sequences after filtering.
    for new_seq, c in enumerate(filtered):
        c.sequence = new_seq
        c.chunk_id = f"c-{new_seq:04d}"

    for c in filtered:
        store.add_chunk(
            chunk_id=c.chunk_id,
            file_path=c.file_path,
            symbol_path=c.symbol_path,
            language=c.language,
            line_start=c.line_start,
            line_end=c.line_end,
            code=c.code,
            code_hash=c.code_hash,
            sequence=c.sequence,
            parent_id=c.parent_id,
            diff_added_lines=c.diff_added_lines,
            diff_removed_lines=c.diff_removed_lines,
        )
    return filtered


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return not (a_end < b_start or b_end < a_start)


def scope_summary(chunks: list[ChunkSpec]) -> dict[str, Any]:
    by_lang: dict[str, int] = {}
    files: set[str] = set()
    total_lines = 0
    for c in chunks:
        files.add(c.file_path)
        if c.language:
            by_lang[c.language] = by_lang.get(c.language, 0) + 1
        total_lines += c.line_end - c.line_start + 1
    return {
        "files": len(files),
        "chunks": len(chunks),
        "total_lines": total_lines,
        "languages": by_lang,
    }

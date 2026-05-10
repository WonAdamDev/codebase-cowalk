"""Session-local sqlite store for chunks, blocks, status, comments, and event queue.

One DB per session, lives at ${session_dir}/session.db. The schema is the
authoritative artifact — everything in HTML and exports is rendered from this DB.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .paths import session_dir

SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL,
    request TEXT NOT NULL,
    scope_summary TEXT,                 -- json: {files, lines, languages}
    diff_mode INTEGER NOT NULL DEFAULT 0,
    vcs TEXT,                           -- "git" | "p4" | null
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    workdir TEXT NOT NULL,              -- absolute path of the analyzed codebase
    mode TEXT NOT NULL DEFAULT 'review' -- review | onboard
);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    parent_id TEXT,
    file_path TEXT NOT NULL,
    symbol_path TEXT,                   -- e.g. "MyClass.method_name"
    language TEXT,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    code TEXT NOT NULL,                 -- snapshot of the source
    code_hash TEXT NOT NULL,
    diff_added_lines TEXT,              -- json array of relative line numbers
    diff_removed_lines TEXT,            -- ditto
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | analyzed | split
    review_status TEXT,                 -- ok | suspicious | unknown | null
    sequence INTEGER NOT NULL,
    created_at REAL NOT NULL,
    lesson_order INTEGER,               -- onboard mode: learning order across the whole session
    layer TEXT                          -- onboard mode: e.g. "foundations" / "core" / "systems" / "wiring"
);

CREATE INDEX IF NOT EXISTS idx_chunks_seq ON chunks(sequence);
CREATE INDEX IF NOT EXISTS idx_chunks_parent ON chunks(parent_id);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);

CREATE TABLE IF NOT EXISTS file_snapshots (
    file_path TEXT PRIMARY KEY,         -- absolute path, matches chunks.file_path
    content TEXT NOT NULL,              -- the full file source at session start
    line_count INTEGER NOT NULL,
    language TEXT,
    captured_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS blocks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL,
    version INTEGER NOT NULL,           -- versioned: re-explanations bump version
    block_type TEXT NOT NULL,           -- summary | intent | behavior | risk | related | diagram | code_ref | warning | note
    content TEXT NOT NULL,              -- markdown (or mermaid for diagram)
    line_ref_start INTEGER,             -- for code_ref blocks
    line_ref_end INTEGER,
    sequence INTEGER NOT NULL,          -- order within the chunk's blocks at this version
    created_at REAL NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id)
);

CREATE INDEX IF NOT EXISTS idx_blocks_chunk ON blocks(chunk_id, version, sequence);

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chunk_id TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY (chunk_id) REFERENCES chunks(id)
);

CREATE INDEX IF NOT EXISTS idx_comments_chunk ON comments(chunk_id);

CREATE TABLE IF NOT EXISTS events_inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,                 -- rerequest | comment | status
    chunk_id TEXT,
    payload TEXT,                       -- json
    consumed INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_unconsumed ON events_inbox(consumed, id);
"""


class SessionStore:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self.db_path = session_dir(session_id) / "session.db"
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn() as cx:
            cx.executescript(SCHEMA)
            self._migrate(cx)

    @staticmethod
    def _migrate(cx: sqlite3.Connection) -> None:
        """Additive migrations for DBs created before a column existed.

        SQLite's CREATE TABLE IF NOT EXISTS skips the body entirely if the table
        is present, so columns added later need explicit ALTER TABLE here.
        """

        def has_col(table: str, col: str) -> bool:
            return any(r["name"] == col for r in cx.execute(f"PRAGMA table_info({table})").fetchall())

        if not has_col("session", "mode"):
            cx.execute("ALTER TABLE session ADD COLUMN mode TEXT NOT NULL DEFAULT 'review'")
        if not has_col("chunks", "lesson_order"):
            cx.execute("ALTER TABLE chunks ADD COLUMN lesson_order INTEGER")
        if not has_col("chunks", "layer"):
            cx.execute("ALTER TABLE chunks ADD COLUMN layer TEXT")
        # Indexes that reference columns added above must be created after the
        # ALTERs, otherwise sqlite errors on old DBs that pre-date those columns.
        cx.execute("CREATE INDEX IF NOT EXISTS idx_chunks_lesson ON chunks(lesson_order)")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        cx = sqlite3.connect(str(self.db_path), isolation_level=None)  # autocommit
        cx.row_factory = sqlite3.Row
        try:
            cx.execute("PRAGMA foreign_keys = ON")
            cx.execute("PRAGMA journal_mode = WAL")
            yield cx
        finally:
            cx.close()

    # session ---------------------------------------------------------------

    def init_session(
        self,
        slug: str,
        request: str,
        workdir: str,
        diff_mode: bool,
        vcs: str | None,
        scope_summary: dict[str, Any] | None,
        mode: str = "review",
    ) -> None:
        if mode not in ("review", "onboard"):
            raise ValueError(f"invalid session mode: {mode!r}")
        now = time.time()
        with self._conn() as cx:
            cx.execute(
                "INSERT OR REPLACE INTO session "
                "(id, slug, request, scope_summary, diff_mode, vcs, created_at, updated_at, workdir, mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.session_id,
                    slug,
                    request,
                    json.dumps(scope_summary) if scope_summary else None,
                    1 if diff_mode else 0,
                    vcs,
                    now,
                    now,
                    workdir,
                    mode,
                ),
            )

    def set_session_mode(self, mode: str) -> None:
        if mode not in ("review", "onboard"):
            raise ValueError(f"invalid session mode: {mode!r}")
        with self._conn() as cx:
            cx.execute(
                "UPDATE session SET mode = ?, updated_at = ? WHERE id = ?",
                (mode, time.time(), self.session_id),
            )

    def get_session(self) -> dict[str, Any] | None:
        with self._conn() as cx:
            row = cx.execute("SELECT * FROM session WHERE id = ?", (self.session_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            if d.get("scope_summary"):
                d["scope_summary"] = json.loads(d["scope_summary"])
            d["diff_mode"] = bool(d["diff_mode"])
            return d

    def touch(self) -> None:
        with self._conn() as cx:
            cx.execute("UPDATE session SET updated_at = ? WHERE id = ?", (time.time(), self.session_id))

    # chunks ----------------------------------------------------------------

    def add_chunk(
        self,
        chunk_id: str,
        file_path: str,
        symbol_path: str | None,
        language: str | None,
        line_start: int,
        line_end: int,
        code: str,
        code_hash: str,
        sequence: int,
        parent_id: str | None = None,
        diff_added_lines: list[int] | None = None,
        diff_removed_lines: list[int] | None = None,
    ) -> None:
        with self._conn() as cx:
            cx.execute(
                "INSERT OR REPLACE INTO chunks "
                "(id, parent_id, file_path, symbol_path, language, line_start, line_end, code, code_hash, "
                " diff_added_lines, diff_removed_lines, status, sequence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
                (
                    chunk_id,
                    parent_id,
                    file_path,
                    symbol_path,
                    language,
                    line_start,
                    line_end,
                    code,
                    code_hash,
                    json.dumps(diff_added_lines) if diff_added_lines is not None else None,
                    json.dumps(diff_removed_lines) if diff_removed_lines is not None else None,
                    sequence,
                    time.time(),
                ),
            )

    def list_chunks(self) -> list[dict[str, Any]]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT id, parent_id, file_path, symbol_path, language, line_start, line_end, "
                "       status, review_status, sequence, lesson_order, layer "
                "FROM chunks ORDER BY sequence ASC"
            ).fetchall()
            return [dict(r) for r in rows]

    def set_chunk_meta(
        self,
        chunk_id: str,
        lesson_order: int | None = None,
        layer: str | None = None,
    ) -> None:
        """Update onboard-mode chunk metadata. Only fields explicitly passed are touched."""
        sets: list[str] = []
        args: list[Any] = []
        if lesson_order is not None:
            sets.append("lesson_order = ?")
            args.append(int(lesson_order))
        if layer is not None:
            sets.append("layer = ?")
            args.append(str(layer))
        if not sets:
            return
        args.append(chunk_id)
        with self._conn() as cx:
            cx.execute(f"UPDATE chunks SET {', '.join(sets)} WHERE id = ?", args)

    def get_chunk(self, chunk_id: str) -> dict[str, Any] | None:
        with self._conn() as cx:
            row = cx.execute("SELECT * FROM chunks WHERE id = ?", (chunk_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            for k in ("diff_added_lines", "diff_removed_lines"):
                if d.get(k):
                    d[k] = json.loads(d[k])
            return d

    def mark_chunk_status(self, chunk_id: str, status: str) -> None:
        with self._conn() as cx:
            cx.execute("UPDATE chunks SET status = ? WHERE id = ?", (status, chunk_id))

    def mark_chunk_review(self, chunk_id: str, review_status: str | None) -> None:
        with self._conn() as cx:
            cx.execute("UPDATE chunks SET review_status = ? WHERE id = ?", (review_status, chunk_id))

    # blocks ----------------------------------------------------------------

    def current_version(self, chunk_id: str) -> int:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT MAX(version) AS v FROM blocks WHERE chunk_id = ?",
                (chunk_id,),
            ).fetchone()
            return int(row["v"]) if row and row["v"] is not None else 0

    def bump_version(self, chunk_id: str) -> int:
        return self.current_version(chunk_id) + 1

    def append_block(
        self,
        chunk_id: str,
        version: int,
        block_type: str,
        content: str,
        line_ref_start: int | None = None,
        line_ref_end: int | None = None,
    ) -> int:
        with self._conn() as cx:
            seq_row = cx.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS s FROM blocks WHERE chunk_id = ? AND version = ?",
                (chunk_id, version),
            ).fetchone()
            seq = int(seq_row["s"])
            cur = cx.execute(
                "INSERT INTO blocks (chunk_id, version, block_type, content, line_ref_start, line_ref_end, sequence, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, version, block_type, content, line_ref_start, line_ref_end, seq, time.time()),
            )
            return int(cur.lastrowid)

    def list_blocks(self, chunk_id: str) -> list[dict[str, Any]]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT id, version, block_type, content, line_ref_start, line_ref_end, sequence, created_at "
                "FROM blocks WHERE chunk_id = ? ORDER BY version ASC, sequence ASC",
                (chunk_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # file snapshots --------------------------------------------------------

    def add_file_snapshot(
        self,
        file_path: str,
        content: str,
        language: str | None = None,
    ) -> None:
        """Persist the full source of a file at session-start time.

        Idempotent: re-saving the same path overwrites the prior snapshot. The UI
        reads from here so the reviewer always sees the code as it was when the
        codewalk began, even if the working tree changes later.
        """
        with self._conn() as cx:
            cx.execute(
                "INSERT OR REPLACE INTO file_snapshots "
                "(file_path, content, line_count, language, captured_at) VALUES (?, ?, ?, ?, ?)",
                (file_path, content, content.count("\n") + 1, language, time.time()),
            )

    def get_file_snapshot(self, file_path: str) -> dict[str, Any] | None:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT file_path, content, line_count, language, captured_at "
                "FROM file_snapshots WHERE file_path = ?",
                (file_path,),
            ).fetchone()
            return dict(row) if row else None

    def list_file_snapshots(self) -> list[dict[str, Any]]:
        """All snapshots without their content (cheap, for indexing). Use get_file_snapshot for content."""
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT file_path, line_count, language, captured_at FROM file_snapshots"
            ).fetchall()
            return [dict(r) for r in rows]

    def has_file_snapshot(self, file_path: str) -> bool:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT 1 FROM file_snapshots WHERE file_path = ? LIMIT 1", (file_path,)
            ).fetchone()
            return row is not None

    # comments --------------------------------------------------------------

    def add_comment(self, chunk_id: str, body: str) -> int:
        with self._conn() as cx:
            cur = cx.execute(
                "INSERT INTO comments (chunk_id, body, created_at) VALUES (?, ?, ?)",
                (chunk_id, body, time.time()),
            )
            return int(cur.lastrowid)

    def list_comments(self, chunk_id: str) -> list[dict[str, Any]]:
        with self._conn() as cx:
            rows = cx.execute(
                "SELECT id, body, created_at FROM comments WHERE chunk_id = ? ORDER BY created_at ASC",
                (chunk_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # events inbox ----------------------------------------------------------

    def push_event(self, event_type: str, chunk_id: str | None, payload: dict[str, Any] | None) -> int:
        with self._conn() as cx:
            cur = cx.execute(
                "INSERT INTO events_inbox (type, chunk_id, payload, created_at) VALUES (?, ?, ?, ?)",
                (event_type, chunk_id, json.dumps(payload) if payload else None, time.time()),
            )
            return int(cur.lastrowid)

    def pop_event(self) -> dict[str, Any] | None:
        with self._conn() as cx:
            row = cx.execute(
                "SELECT id, type, chunk_id, payload, created_at FROM events_inbox "
                "WHERE consumed = 0 ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            cx.execute("UPDATE events_inbox SET consumed = 1 WHERE id = ?", (row["id"],))
            d = dict(row)
            if d.get("payload"):
                d["payload"] = json.loads(d["payload"])
            return d

    # progress --------------------------------------------------------------

    def progress(self) -> dict[str, int]:
        with self._conn() as cx:
            total = cx.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE status != 'split'"
            ).fetchone()["c"]
            analyzed = cx.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE status = 'analyzed'"
            ).fetchone()["c"]
            reviewed = cx.execute(
                "SELECT COUNT(*) AS c FROM chunks WHERE review_status IS NOT NULL AND status != 'split'"
            ).fetchone()["c"]
            return {"total": int(total), "analyzed": int(analyzed), "reviewed": int(reviewed)}


def list_all_sessions() -> list[dict[str, Any]]:
    """Walk every session DB and pull out a session-row summary."""
    out: list[dict[str, Any]] = []
    from .paths import sessions_root

    for sub in sessions_root().iterdir():
        if not sub.is_dir():
            continue
        db = sub / "session.db"
        if not db.exists():
            continue
        try:
            cx = sqlite3.connect(str(db))
            cx.row_factory = sqlite3.Row
            row = cx.execute("SELECT * FROM session").fetchone()
            if row:
                d = dict(row)
                if d.get("scope_summary"):
                    d["scope_summary"] = json.loads(d["scope_summary"])
                d["diff_mode"] = bool(d["diff_mode"])
                # add progress
                p = cx.execute("SELECT COUNT(*) AS c FROM chunks WHERE status != 'split'").fetchone()
                a = cx.execute("SELECT COUNT(*) AS c FROM chunks WHERE status = 'analyzed'").fetchone()
                d["progress"] = {"total": int(p["c"]), "analyzed": int(a["c"])}
                out.append(d)
            cx.close()
        except sqlite3.DatabaseError:
            continue
    out.sort(key=lambda d: d.get("updated_at", 0), reverse=True)
    return out


def find_session_by_slug(slug: str) -> str | None:
    for s in list_all_sessions():
        if s.get("slug") == slug:
            return s["id"]
    return None


def delete_session(session_id: str) -> bool:
    """Remove the session directory entirely. Returns True on success."""
    import shutil
    from .paths import sessions_root

    p = sessions_root() / session_id
    if not p.exists():
        return False
    shutil.rmtree(p, ignore_errors=True)
    return True


def rename_session(session_id: str, new_slug: str) -> bool:
    db = session_dir(session_id) / "session.db"
    if not db.exists():
        return False
    cx = sqlite3.connect(str(db))
    try:
        cx.execute("UPDATE session SET slug = ?, updated_at = ? WHERE id = ?", (new_slug, time.time(), session_id))
        cx.commit()
        return True
    finally:
        cx.close()

"""Tree-sitter chunker.

Splits source files into chunks aligned to function/method/class boundaries.
Module-level code (imports + top-level statements) becomes a single leading chunk.
Anything not covered by a recognized node falls into a 'gap' chunk so coverage stays complete.

LLM-driven re-splitting (split_chunk MCP tool) lets Claude carve large functions
into smaller named sub-chunks at runtime.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

# tree_sitter_language_pack provides pre-compiled grammars for many languages.
try:
    from tree_sitter_language_pack import get_parser  # type: ignore
except Exception:  # pragma: no cover - optional at import time
    get_parser = None  # type: ignore


# extension -> language id used by tree-sitter-language-pack
EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".cc": "cpp",
    ".hpp": "cpp",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".h": "cpp",
    ".c": "c",
    ".cs": "csharp",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
}


# Per-language node types we treat as a chunk boundary. Grammar names differ across
# languages; this map is intentionally explicit so we can audit it.
CHUNK_NODE_TYPES: dict[str, set[str]] = {
    "python": {"function_definition", "class_definition", "decorated_definition"},
    "typescript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "type_alias_declaration",
    },
    "tsx": {
        "function_declaration",
        "method_definition",
        "class_declaration",
        "interface_declaration",
        "enum_declaration",
        "type_alias_declaration",
    },
    "javascript": {
        "function_declaration",
        "method_definition",
        "class_declaration",
    },
    "cpp": {
        "function_definition",
        "class_specifier",
        "struct_specifier",
        "namespace_definition",
        "template_declaration",
    },
    "c": {"function_definition", "struct_specifier"},
    "csharp": {
        "method_declaration",
        "class_declaration",
        "struct_declaration",
        "interface_declaration",
        "enum_declaration",
        "constructor_declaration",
        "destructor_declaration",
        "property_declaration",
    },
    "go": {"function_declaration", "method_declaration", "type_declaration"},
    "rust": {"function_item", "impl_item", "struct_item", "enum_item", "trait_item"},
    "java": {"method_declaration", "class_declaration", "interface_declaration", "enum_declaration"},
    "ruby": {"method", "class", "module"},
    "php": {"function_definition", "method_declaration", "class_declaration"},
    "swift": {"function_declaration", "class_declaration", "struct_declaration", "protocol_declaration"},
    "kotlin": {"function_declaration", "class_declaration", "object_declaration"},
}


# Identifier-bearing field names — used to extract the symbol name out of a node.
# Most grammars expose the name via a field called "name".
NAME_FIELDS = ("name", "declarator")


# A chunk longer than this is auto-subdivided at top-level statement groups
# inside its body, so a 400-line god-function gets reviewable parts even
# without the LLM calling split_chunk. Set to 0 to disable.
LONG_CHUNK_LINES = 200

# When subdividing, target this many lines per sub-chunk.
SUBDIVIDE_TARGET_LINES = 80

# Body field names per language. Tree-sitter grammars expose the function/class
# body via different field names; for the languages we list, "body" works for
# most of them, but a couple of grammars use a different name.
BODY_FIELDS = ("body", "block", "compound_statement", "declaration_list")


@dataclass
class ChunkSpec:
    """A pre-store representation of a chunk."""

    chunk_id: str
    file_path: str
    symbol_path: str | None
    language: str | None
    line_start: int          # 1-indexed inclusive
    line_end: int            # 1-indexed inclusive
    code: str
    code_hash: str
    sequence: int
    parent_id: str | None = None
    diff_added_lines: list[int] | None = None
    diff_removed_lines: list[int] | None = None


def detect_language(path: Path) -> str | None:
    return EXTENSION_TO_LANGUAGE.get(path.suffix.lower())


def hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8", errors="replace")).hexdigest()[:16]


def _node_text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def _node_name(node, source: bytes) -> str | None:
    for field_name in NAME_FIELDS:
        try:
            child = node.child_by_field_name(field_name)
        except Exception:
            child = None
        if child is not None:
            text = _node_text(child, source)
            # for C/C++ declarators, the identifier may be nested
            text = text.strip().split("(")[0].strip()
            if text:
                return text
    # fallback: first identifier child
    for c in node.children:
        if c.type in {"identifier", "type_identifier", "field_identifier"}:
            return _node_text(c, source)
    return None


def _find_body_node(node):
    """Find the first child of `node` that looks like a body (block/compound)."""
    for f in BODY_FIELDS:
        try:
            child = node.child_by_field_name(f)
        except Exception:
            child = None
        if child is not None:
            return child
    # generic fallback: first child whose type name contains "block" or "body"
    for c in node.children:
        if "block" in c.type or "body" in c.type or c.type == "compound_statement":
            return c
    return None


def _maybe_subdivide_long_chunk(spec: "ChunkSpec", parser, base_seq: int) -> list["ChunkSpec"]:
    """If a chunk exceeds LONG_CHUNK_LINES, try to break its body into
    statement-sized sub-chunks. The parent is replaced wholesale by the children
    (they fully cover the parent's source). Sub-chunks are returned as a list
    starting at sequence number `base_seq`. If no useful split is found, returns
    [spec] with its original sequence.

    Sub-chunks do not set parent_id (the auto-split is invisible in the UI tree;
    LLM-driven split_chunk is what creates a parent/child relationship).
    """
    if LONG_CHUNK_LINES <= 0:
        return [spec]
    line_count = spec.line_end - spec.line_start + 1
    if line_count <= LONG_CHUNK_LINES:
        return [spec]
    if parser is None:
        return [spec]

    try:
        src = spec.code.encode("utf-8", errors="replace")
        tree = parser.parse(src)
        root = tree.root_node
    except Exception:
        return [spec]

    # find the outermost named definition node (function/class) inside the chunk
    def_node = None
    chunk_types = CHUNK_NODE_TYPES.get(spec.language or "", set())
    for c in root.children:
        if c.type in chunk_types:
            def_node = c
            break
    if def_node is None:
        def_node = root

    body = _find_body_node(def_node)
    if body is None:
        return [spec]

    statements = [c for c in body.children if c.is_named and c.type not in {"comment"}]
    if len(statements) < 2:
        return [spec]

    target = max(40, SUBDIVIDE_TARGET_LINES)
    groups: list[list] = []
    current: list = []
    current_lines = 0
    for st in statements:
        st_lines = st.end_point[0] - st.start_point[0] + 1
        if current and current_lines + st_lines > target:
            groups.append(current)
            current = [st]
            current_lines = st_lines
        else:
            current.append(st)
            current_lines += st_lines
    if current:
        groups.append(current)

    if len(groups) < 2:
        return [spec]

    parent_lines = spec.code.splitlines(keepends=True)
    base_symbol = spec.symbol_path or ""
    body_local_start = body.start_point[0]
    body_local_end = body.end_point[0]
    out: list[ChunkSpec] = []
    seq = base_seq

    # 1) leading prelude (signature + decorators + before-body lines)
    if body_local_start > 0:
        prelude = "".join(parent_lines[: body_local_start])
        if prelude.strip():
            out.append(
                ChunkSpec(
                    chunk_id=f"c-{seq:04d}",
                    file_path=spec.file_path,
                    symbol_path=f"{base_symbol} (prelude)" if base_symbol else None,
                    language=spec.language,
                    line_start=spec.line_start,
                    line_end=spec.line_start + body_local_start - 1,
                    code=prelude,
                    code_hash=hash_code(prelude),
                    sequence=seq,
                )
            )
            seq += 1

    # 2) statement groups
    for i, group in enumerate(groups, start=1):
        first_stmt = group[0]
        last_stmt = group[-1]
        local_start = first_stmt.start_point[0]
        local_end = last_stmt.end_point[0]
        snippet = "".join(parent_lines[local_start : local_end + 1])
        if not snippet.strip():
            continue
        out.append(
            ChunkSpec(
                chunk_id=f"c-{seq:04d}",
                file_path=spec.file_path,
                symbol_path=f"{base_symbol} #{i}" if base_symbol else f"part {i}",
                language=spec.language,
                line_start=spec.line_start + local_start,
                line_end=spec.line_start + local_end,
                code=snippet,
                code_hash=hash_code(snippet),
                sequence=seq,
            )
        )
        seq += 1

    # 3) trailing close-brace / postlude (e.g. `}` of class)
    if body_local_end < len(parent_lines) - 1:
        postlude = "".join(parent_lines[body_local_end + 1 :])
        if postlude.strip():
            out.append(
                ChunkSpec(
                    chunk_id=f"c-{seq:04d}",
                    file_path=spec.file_path,
                    symbol_path=f"{base_symbol} (closing)" if base_symbol else None,
                    language=spec.language,
                    line_start=spec.line_start + body_local_end + 1,
                    line_end=spec.line_end,
                    code=postlude,
                    code_hash=hash_code(postlude),
                    sequence=seq,
                )
            )
            seq += 1

    if len(out) < 2:
        return [spec]
    return out


def _walk_top_level(root, types: set[str]):
    """Yield top-level nodes whose type is in `types`. Walks recursively but stops
    descent once a chunk node is found, so nested functions become their own chunks
    only at the boundary level we care about (module top + class body)."""
    out = []

    def visit(node, depth: int):
        for child in node.children:
            if child.type in types:
                out.append(child)
                # for class-like nodes, descend one more level so methods become
                # their own chunks. We do this by visiting the body field if present.
                body = None
                for f in ("body",):
                    try:
                        body = child.child_by_field_name(f)
                    except Exception:
                        body = None
                    if body is not None:
                        break
                if body is not None:
                    visit(body, depth + 1)
            else:
                visit(child, depth + 1)

    visit(root, 0)
    return out


def chunk_file(path: Path, sequence_start: int = 0, code_override: str | None = None) -> tuple[list[ChunkSpec], int]:
    """Parse a file with Tree-sitter and split it into chunks.

    Returns (chunks, next_sequence). Chunk IDs are formatted as `c-0000`. Sequence
    numbers are global across the session, threaded through `sequence_start`.

    If the file's language is unsupported (no grammar in the language pack) or
    Tree-sitter is unavailable, falls back to a single whole-file chunk.
    """
    language = detect_language(path)
    text = code_override if code_override is not None else path.read_text(encoding="utf-8", errors="replace")
    chunks: list[ChunkSpec] = []
    seq = sequence_start

    def make_id(s: int) -> str:
        return f"c-{s:04d}"

    if not language or get_parser is None:
        # whole-file chunk fallback
        chunks.append(_whole_file_chunk(path, text, language, seq))
        return chunks, seq + 1

    try:
        parser = get_parser(language)
    except Exception:
        chunks.append(_whole_file_chunk(path, text, language, seq))
        return chunks, seq + 1

    source = text.encode("utf-8", errors="replace")
    tree = parser.parse(source)
    root = tree.root_node
    types = CHUNK_NODE_TYPES.get(language, set())
    if not types:
        chunks.append(_whole_file_chunk(path, text, language, seq))
        return chunks, seq + 1

    nodes = _walk_top_level(root, types)
    nodes.sort(key=lambda n: n.start_byte)

    lines = text.splitlines(keepends=True)
    cursor_byte = 0
    cursor_line = 0  # 0-indexed

    def emit(start_byte: int, end_byte: int, start_line: int, end_line: int, symbol: str | None) -> None:
        nonlocal seq
        if start_byte >= end_byte:
            return
        line_start_idx = start_line
        line_end_idx = end_line
        snippet = "".join(lines[line_start_idx : line_end_idx + 1])
        if not snippet.strip():
            return
        spec = ChunkSpec(
            chunk_id=make_id(seq),
            file_path=str(path),
            symbol_path=symbol,
            language=language,
            line_start=line_start_idx + 1,
            line_end=line_end_idx + 1,
            code=snippet,
            code_hash=hash_code(snippet),
            sequence=seq,
        )
        produced = _maybe_subdivide_long_chunk(spec, parser, base_seq=seq)
        chunks.extend(produced)
        seq += len(produced)

    for node in nodes:
        node_start_line = node.start_point[0]
        node_end_line = node.end_point[0]
        # gap chunk: anything between cursor and this node
        if node_start_line > cursor_line:
            emit(
                start_byte=cursor_byte,
                end_byte=node.start_byte,
                start_line=cursor_line,
                end_line=node_start_line - 1,
                symbol=None,
            )
        symbol = _node_name(node, source)
        emit(
            start_byte=node.start_byte,
            end_byte=node.end_byte,
            start_line=node_start_line,
            end_line=node_end_line,
            symbol=symbol,
        )
        cursor_line = node_end_line + 1
        cursor_byte = node.end_byte

    # trailing gap
    if cursor_line < len(lines):
        emit(
            start_byte=cursor_byte,
            end_byte=len(source),
            start_line=cursor_line,
            end_line=len(lines) - 1,
            symbol=None,
        )

    if not chunks:
        chunks.append(_whole_file_chunk(path, text, language, seq))
        seq += 1

    return chunks, seq


def _whole_file_chunk(path: Path, text: str, language: str | None, seq: int) -> ChunkSpec:
    lines = text.splitlines() or [""]
    return ChunkSpec(
        chunk_id=f"c-{seq:04d}",
        file_path=str(path),
        symbol_path=None,
        language=language,
        line_start=1,
        line_end=len(lines),
        code=text,
        code_hash=hash_code(text),
        sequence=seq,
    )


def chunk_files(paths: Iterable[Path]) -> list[ChunkSpec]:
    """Chunk a list of files in order, threading global sequence numbers."""
    out: list[ChunkSpec] = []
    seq = 0
    for p in paths:
        try:
            file_chunks, seq = chunk_file(p, sequence_start=seq)
        except (OSError, UnicodeDecodeError):
            continue
        out.extend(file_chunks)
    return out


def split_chunk_by_ranges(
    parent: ChunkSpec, ranges: list[tuple[int, int]], next_sub_index_start: int = 1
) -> list[ChunkSpec]:
    """Carve a parent chunk into sub-chunks at given (line_start, line_end) ranges.

    Ranges are absolute file lines (same coordinate system as parent.line_start/end).
    Sub-chunk ids are `<parent_id>.<n>`.
    """
    out: list[ChunkSpec] = []
    parent_lines = parent.code.splitlines(keepends=True)

    def slice_for(local_start_idx: int, local_end_idx: int) -> str:
        return "".join(parent_lines[local_start_idx : local_end_idx + 1])

    n = next_sub_index_start
    for r_start, r_end in ranges:
        local_start = r_start - parent.line_start
        local_end = r_end - parent.line_start
        if local_start < 0 or local_end >= len(parent_lines) or local_start > local_end:
            continue
        snippet = slice_for(local_start, local_end)
        out.append(
            ChunkSpec(
                chunk_id=f"{parent.chunk_id}.{n}",
                file_path=parent.file_path,
                symbol_path=parent.symbol_path,
                language=parent.language,
                line_start=r_start,
                line_end=r_end,
                code=snippet,
                code_hash=hash_code(snippet),
                sequence=parent.sequence,  # children inherit parent sequence; renderer uses a tree
                parent_id=parent.chunk_id,
            )
        )
        n += 1
    return out

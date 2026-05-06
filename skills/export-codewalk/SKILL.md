---
name: export-codewalk
description: Export a completed codewalk session to a portable file (.cwlk) and a self-contained HTML so the reviewer can share their findings with teammates. Invoke this only when the user explicitly asks to export, share, or save a codewalk.
disable-model-invocation: true
---

# export-codewalk

Export a finished codewalk session into two artifacts that can be shared with teammates:

1. **`<slug>.cwlk`** — a zip archive containing the session sqlite DB, all chunk source snapshots, all explanation blocks, and the user's review state (✓/🚩/❓ marks + freeform comments). Recipients open this with `/codebase-cowalk:view-codewalk`.
2. **`<slug>.html`** — a single self-contained HTML file (all CSS/JS/data inlined). Recipients can open this directly in any browser. Read-only, no live server needed.

The user invoked this with: **"$ARGUMENTS"**

## Workflow

1. Parse the argument as a session slug. If empty, call `mcp__codebase-cowalk__list_sessions` and ask the user which one to export.
2. Call `mcp__codebase-cowalk__export_session(slug=<slug>, formats=["cwlk", "html"])`.
3. The MCP server returns absolute paths for both artifacts. Tell the user:

   > Exported:
   > - `D:\path\to\<slug>.cwlk` (share with teammates who use Claude Code)
   > - `D:\path\to\<slug>.html` (share with anyone — opens in any browser)

## What gets exported

- Every chunk's source code snapshot (preserving the code state at review time even if the original files change later)
- Every explanation block (with version history if the user requested re-explanations)
- The user's review state per chunk (✓ / 🚩 / ❓ + freeform comments)
- Session metadata (slug, original request, scope summary, VCS info, review timestamps)

## What does NOT get exported

- The MCP server's per-session HTTP server (recipients run their own via `view-codewalk`)
- Any path traversal outside the session — only the chunks that were actually walked through
- API keys, system identifiers, or anything outside the session DB

## After export

Suggest to the user:

- The reviewer of the recipient side can run `/codebase-cowalk:view-codewalk path/to/<slug>.cwlk` to get the exact same 3-pane interactive layout (read-only).
- For non-Claude-Code teammates, just send the `.html` file.

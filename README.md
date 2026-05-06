# codebase-cowalk

Walk through a codebase **chunk by chunk in your browser**, with Claude Code pushing
interactive explanations to a live HTML page as it analyzes. Built for the
"a human now has to review LLM-written code" problem: instead of re-reading every
line yourself, let Claude narrate the change set chunk-by-chunk, and you skim,
mark ✓ / 🚩 / ❓, and leave reviewer notes — all in a single browser tab.

This is a **Claude Code plugin** that ships an MCP server, three skills, and an
HTML viewer. The MCP server is intentionally LLM-unaware: Claude Code (via the
plugin's skills) does the actual reading and explaining, the MCP server just
chunks files, persists state, renders HTML, and serves the page.

> **Status:** v0.1, alpha. Schema and CLI may shift before v1.

---

## Why

Reviewing LLM-generated code with a long chat reply is awkward — you scroll past
the same code over and over, and there's no per-chunk record of what you've
checked. `codebase-cowalk` flips it: the code stays put, the explanation comes
to it, and your review state (status + comments) is captured per chunk, exportable
as a `.cwlk` file so you can share findings with teammates.

## What you get

- **3 skills**, namespaced to the plugin:
  - `/codebase-cowalk:walk-codebase <natural language>` — main entry. Walks
    through whatever the user describes (a recent change set, a module, a
    whole repo).
  - `/codebase-cowalk:export-codewalk <slug>` — exports a finished session to
    `<slug>.cwlk` (full data) and `<slug>.html` (self-contained, opens in any
    browser).
  - `/codebase-cowalk:view-codewalk <path.cwlk>` — read-only viewer for a
    `.cwlk` someone shared with you.

- **A 3-pane HTML viewer**: file/chunk tree on the left, code in the middle,
  Claude's explanation blocks + your status toggles + comments on the right.
  Live updates via Server-Sent Events while Claude analyzes; works offline in
  the static export.

- **A live event channel back to Claude**: when you click _re-explain_ in the
  page, the request goes through the plugin's Monitor straight into Claude's
  context — Claude reacts the same turn.

## How it fits together

```
┌──────────────────┐                ┌─────────────────────────────────┐
│   Claude Code    │   stdio        │  codebase-cowalk MCP server     │
│  (your session)  │ ◄────────────► │  - chunker (Tree-sitter)        │
│                  │   tool calls   │  - sqlite store                 │
│  walk-codebase   │                │  - aiohttp HTTP server (SSE)    │
│  skill           │                │  - export / view                │
└────────┬─────────┘                └───────────┬─────────────────────┘
         │                                       │ http://localhost:<free port>
         │       Monitor stream                  ▼
         │  (rerequest, comment events)   ┌──────────────┐
         └────────────────────────────────│  Browser     │
                                          │  (3-pane UI) │
                                          └──────────────┘
```

The MCP server is launched lazily by Claude Code when the plugin is enabled
(`uvx --from ${CLAUDE_PLUGIN_ROOT} codebase-cowalk-mcp`). It runs in-process
with a background HTTP server per active session. When the Claude Code session
ends, everything dies with it; session state on disk persists for resume later.

## Install

### From the marketplace (recommended)

This repo is both a plugin **and** its own single-plugin marketplace
(`cowalk-marketplace`). Inside Claude Code:

```
/plugin marketplace add WonAdamDev/codebase-cowalk
/plugin install codebase-cowalk@cowalk-marketplace
```

After install, `/help` will show three commands:

- `/codebase-cowalk:walk-codebase`
- `/codebase-cowalk:export-codewalk`
- `/codebase-cowalk:view-codewalk`

Updates: `/plugin marketplace update cowalk-marketplace` then
`/plugin update codebase-cowalk@cowalk-marketplace`.

### Local development install

For hacking on the plugin itself, point Claude Code at a local checkout:

```bash
git clone https://github.com/WonAdamDev/codebase-cowalk.git
claude --plugin-dir ./codebase-cowalk
```

Or as a local marketplace (so install/update flows match production):

```
/plugin marketplace add ./codebase-cowalk
/plugin install codebase-cowalk@cowalk-marketplace
```

`/reload-plugins` picks up code changes without a session restart.

### Requirements

- **Claude Code v2.1.105+** (for plugin Monitors)
- **uv** on `PATH` (the MCP server runs via `uvx`)
- **Python 3.11+** (auto-resolved by uv)
- A working tree to walk. git or perforce; for whole-codebase walks, neither
  is needed.

The first invocation of the MCP server takes a few seconds while uv resolves
and installs dependencies into a cache; subsequent starts are near-instant.

## Usage

### Reviewing a recent change set (the main use case)

```
/codebase-cowalk:walk-codebase the item-spawn refactor I just merged
```

Claude will:
1. Run `git diff` (or `p4` — it'll ask once if it can't tell) to find what changed.
2. Show you the proposed scope (e.g. "42 chunks across 8 files, ~5 minutes").
3. After you confirm, spin up an HTTP server on a free port and tell you the URL.
4. Open it in your browser. Watch the page fill in.

While Claude works, you can:
- Click any chunk on the left, even ones not yet analyzed.
- Toggle ✓ / 🚩 / ❓ as you review each.
- Add freeform notes per chunk.
- Click _re-explain_ on anything that feels off — Claude picks that up via the
  Monitor and pushes a new explanation version (old version preserved as history).

### Reviewing a module or whole repo

Same skill, different prompt — no diff mode:

```
/codebase-cowalk:walk-codebase explain the auth module under src/auth/
/codebase-cowalk:walk-codebase walk me through this whole repo
```

For large scopes (>500 chunks) you'll get an explicit confirm prompt before
analysis starts, so token cost can't sneak up on you.

### Exporting and sharing

```
/codebase-cowalk:export-codewalk item-spawn-refactor-20260506-1432
```

Produces two files in `${CLAUDE_PLUGIN_DATA}/exports/`:
- `<slug>.cwlk` — share with another Claude Code user; they open it via
  `view-codewalk` to get the full 3-pane interactive layout (read-only).
- `<slug>.html` — share with anyone; opens in any browser, no Claude required.

### Viewing someone else's export

```
/codebase-cowalk:view-codewalk path/to/item-spawn-refactor-20260506-1432.cwlk
```

A read-only HTTP server starts on a free port. Status toggles, the comment
input, and the re-explain button are all hidden. The original reviewer's
marks and notes are visible exactly as they made them.

### Resuming and managing past sessions

Inside any walk-codebase invocation Claude can call `mcp__codebase-cowalk__list_sessions`
to find what you've worked on, and resume one with:

```
/codebase-cowalk:walk-codebase resume item-spawn-refactor-20260506-1432
```

## Privacy

This plugin does **not** send your code anywhere except through the Claude Code
session you are already using. All chunking, storage, HTML rendering, and HTTP
serving happens on your local machine. The HTTP server binds to `127.0.0.1`
only and uses a port assigned by the OS. The export `.cwlk` is a plain zip you
control — share it deliberately.

If your organization is sensitive to where review artifacts live, point
`${CLAUDE_PLUGIN_DATA}` at an approved location (Claude Code computes this
from the install scope; see [Plugins reference][plugins-ref]).

## Design decisions worth knowing

- **Snapshot, not pointer**: when a chunk enters the session, its source code
  is copied into the session's sqlite DB. The review record stays valid even
  if the underlying files change later, and the export is fully portable.
- **Versioned blocks**: re-explanations preserve history. Old blocks render
  faded; the latest version is highlighted. Click through to see what changed.
- **Block as a tool, not a fixed schema**: Claude appends blocks
  (summary / intent / behavior / risk / related / diagram / code_ref / warning /
  note) like tool calls. Not every chunk gets every block — only the ones the
  chunk actually warrants.
- **MCP server is LLM-unaware**: zero Anthropic-API calls in this server. All
  reading and explaining is your Claude Code session's own work.
- **Free port + per-session isolation**: open as many sessions in parallel as
  you want. Each gets its own port and its own sqlite DB.

## Languages supported (chunker)

Tree-sitter via `tree-sitter-language-pack`. First-class support:
Python, TypeScript, JavaScript, TSX, C/C++, C#, Go, Rust, Java, Ruby, PHP,
Swift, Kotlin. Files in unsupported languages fall back to a single
whole-file chunk so coverage stays complete.

## License

MIT — see [LICENSE](LICENSE).

[plugins-ref]: https://code.claude.com/docs/en/plugins-reference

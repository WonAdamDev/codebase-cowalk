---
name: walk-codebase
description: Walk through a codebase or a recent change set chunk by chunk, pushing interactive explanations to a live HTML page in the user's browser. Use when the user wants to review LLM-generated code, get an explanation of what was just implemented, audit a refactor, or otherwise have Claude explain code in a structured per-chunk way (rather than a single long chat response). Triggers on phrases like "review this codebase", "walk me through what you just changed", "explain this refactor chunk by chunk", "set up a codewalk for the recent changes", "I want to verify the LLM-generated code". Argument is a free-form natural-language description of what to walk through (e.g. "the item spawn refactor I just merged", "the auth module", "everything under src/inventory/").
---

# walk-codebase

Set up an interactive, browser-based codewalk so the user can review code chunk-by-chunk while you push explanations to the page.

The user invoked this with: **"$ARGUMENTS"**

## What this skill does

You will:

1. **Interpret the user's request** to decide what to walk through (a recent change set, a specific module, a whole repository, etc.).
2. **Detect the version control system** (git or perforce) for the working tree, ask the user once if neither is obvious.
3. **Propose a scope** — list the files / symbols / lines to be analyzed — and ask the user to confirm before doing expensive work.
4. **Run the analysis** chunk by chunk, pushing explanations to the live HTML page as you go.
5. **Stay reactive** — the Monitor stream delivers user reactions (rerequest, comments, status changes) from the page back to you.

## Required tools

This plugin's MCP server (`codebase-cowalk`) provides the tools you will use. They are all prefixed with `mcp__codebase-cowalk__` in your tool list.

Key tools:

| Tool                             | Purpose                                                                                                                  |
| :------------------------------- | :----------------------------------------------------------------------------------------------------------------------- |
| `start_session`                  | Create a new codewalk session. Takes a natural-language `request` and a `scope` (list of file/symbol/range entries). Returns `session_id`, `slug`, `port`, and `url` of the live HTML page. |
| `propose_scope`                  | Given a list of files (or files+ranges), parse them with Tree-sitter, return a chunk preview without running explanations yet. Use this to show the user what is about to be analyzed. |
| `add_chunk`                      | Manually register a chunk that Tree-sitter didn't pick up (rare — only if you need to insert a non-AST region). |
| `split_chunk`                    | Split an existing chunk into smaller sub-chunks (use when a function is too large or has distinct logical regions worth explaining separately). |
| `list_chunks`                    | List chunks in the current session (id, file, symbol, line range, status). |
| `get_chunk`                      | Fetch a chunk's source code as a string, ready for you to read and explain. |
| `append_block`                   | Append an explanation block to a chunk. `block_type` is one of: `summary`, `intent`, `behavior`, `risk`, `related`, `diagram`, `code_ref`, `warning`, `note`. `content` is markdown (Mermaid for `diagram`). Push as many blocks per chunk as the chunk warrants — not every chunk needs every block type. |
| `set_chunk_analyzed`             | Mark a chunk as analyzed (no more blocks coming for now). |
| `pop_event`                      | Read the next pending user event from the page (rerequest, comment, etc.). Returns `null` if the queue is empty. The Monitor stream will also notify you when new events arrive. |
| `end_session`                    | Shut down this session's HTTP server. |

## Workflow

### Step 1 — interpret the request

Read the user's request (above). Decide:

- **Is this scoped to a recent change set?** ("the refactor I just did", "this PR", "what we changed today") → use git/p4 to find changed files.
- **Is this a specific subtree or module?** ("the auth module", "src/inventory/") → use file globs.
- **Is this the whole codebase?** ("explain this project", "walk me through the repo") → use the project root, with `.gitignore` and conventional excludes (`node_modules`, `.venv`, `dist`, `build`, lock files, binaries) applied.

If you cannot tell which VCS is in use and the scope depends on changes, ask the user once: "Is this a git or perforce repository?" Save the answer for the rest of the session.

### Step 2 — gather files

Use `Bash` for VCS queries:

- **git**: `git status --porcelain`, `git diff --name-only main...HEAD`, `git log --since=...`, etc.
- **perforce**: `p4 opened`, `p4 describe <changelist>`, `p4 diff -ds`, etc.
- **no-VCS / whole tree**: `Glob` for files, then filter with `.gitignore` + standard excludes.

### Step 3 — propose the scope

Call `propose_scope(files=[...])`. The MCP server parses with Tree-sitter and returns a preview: total chunks, total lines, by-language breakdown.

Show the user a one-paragraph summary like:

> "I'm about to walk through **42 chunks** across **8 files** in `src/inventory/spawn/` (mostly C++, some Python). Estimated analysis: ~5 minutes. Confirm?"

If the chunk count exceeds **500**, you MUST get explicit confirmation before proceeding (token cost control).

### Step 4 — start the session

Once the user confirms, call:

```
start_session(
    request="<the user's original natural-language request>",
    scope=<the list returned by propose_scope, or a refined version>,
    diff_mode=<true if scope is a change set, false otherwise>,
)
```

You get back `{ session_id, slug, port, url }`. Tell the user the URL and recommend opening it in their browser. Example:

> Live codewalk: **http://localhost:54321** — open this in your browser. The page will fill in as I push explanations.

### Step 5 — analyze chunks

Loop over chunks (`list_chunks` then `get_chunk` for each):

1. Read the chunk's source code.
2. Decide which blocks are warranted. Don't force every block on every chunk. **Always include at least `summary`.** Add `intent` for non-obvious code, `risk` if you spot anything suspicious, `diagram` (Mermaid) when a flow is non-trivial, `related` to link to other chunks the user will want to jump to, `warning` for something the reviewer must not miss, `note` as a freeform fallback.
3. Call `append_block` once per block, in the order you want them displayed.
4. If a chunk is too large or has distinct sections, call `split_chunk` first and then explain the children.
5. Call `set_chunk_analyzed(chunk_id)` when done.

Use **subagents (`Agent` tool)** to parallelize chunk analysis for large scopes — each subagent handles one chunk in its own context, then returns the blocks for you to push via `append_block`. This keeps your main context small.

### Step 6 — react to user events

The plugin's Monitor delivers events from the HTML page as notifications. When you see one (or proactively check with `pop_event`), handle it:

- `rerequest`: user asked for a re-explanation. Re-read the chunk, optionally with the user's note as extra context. Push new blocks (the page preserves history — old blocks remain visible as previous versions).
- `comment`: user wrote a freeform comment. Acknowledge if relevant, optionally push a `note` block in response.
- `status`: user toggled ✓ / 🚩 / ❓. No action required, just be aware.

### Step 7 — wrap up

When the user says they're done (or you've finished the entire scope and there are no pending events):

- Tell them how to export: `/codebase-cowalk:export-codewalk <slug>`.
- They can resume later with: `/codebase-cowalk:walk-codebase resume <slug>`.
- Call `end_session(session_id)` only if they explicitly ask to shut it down. Otherwise leave the HTTP server running until the Claude Code session ends.

## Block type reference (what each one renders as)

| Block type | When to use                                                                       | Visual treatment                          |
| :--------- | :-------------------------------------------------------------------------------- | :---------------------------------------- |
| `summary`  | One-line TL;DR of the chunk. Always include.                                      | Page header band                          |
| `intent`   | Why this code exists (motivation, design decision)                                | Standard markdown                         |
| `behavior` | What the code does (inputs, outputs, side effects)                                | Standard markdown                         |
| `risk`     | LLM-flagged concerns the human should focus on                                    | Yellow accent                             |
| `related`  | Links to other chunk IDs in the same session (`[chunk_id]` markdown links work)   | Standard markdown with chunk-jump links   |
| `diagram`  | Mermaid block for non-trivial control flow                                        | Rendered as Mermaid                       |
| `code_ref` | A specific line range in the chunk this block is about                            | Highlights those lines in the code pane   |
| `warning`  | "Reviewer must not miss this"                                                     | Red accent, expanded by default           |
| `note`     | Anything else                                                                     | Plain                                     |

## Resume mode

If the user invokes this skill with `resume <slug>`, call `start_session(resume=<slug>)` instead of creating a new one. The HTTP server will be re-launched on a fresh port and the existing chunks/blocks/states will be served.

## Import mode

If the user wants to do their own review on top of a `.cwlk` archive someone shared (e.g. "I want to verify what Alice walked through"), call `mcp__codebase-cowalk__import_codewalk(path=<.cwlk path>)` instead of starting from scratch. The original chunks and explanation blocks come along; review state and comments start fresh so the new reviewer's marks aren't conflated with the original author's. For a purely passive read of someone else's review, use `/codebase-cowalk:view-codewalk` instead — that's read-only.

## Things to NOT do

- Don't render explanations directly in chat. The whole point is the HTML page. Tell the user the URL and push to the page.
- Don't skip `propose_scope` for large scopes. The user must approve token cost.
- Don't paste large chunks of source code in chat.
- Don't fabricate file paths or symbols. If unsure, run `Bash(ls)` or `Glob` first.

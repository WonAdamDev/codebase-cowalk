---
name: walk-codebase
description: Walk through a codebase or a recent change set chunk by chunk, pushing interactive explanations to a live HTML page in the user's browser. Operates in two modes — **review** (default; auditing LLM-generated code, refactors, recent diffs) and **onboard** (curriculum-style learning of an unfamiliar codebase, with chunks ordered by `lesson_order` inside coarse `layer` groups). This is a strictly READ-ONLY skill — never edit, write, or refactor source files while running it, even if you spot bugs or improvements (surface them as `risk`/`warning` blocks instead). Use review-mode triggers like "review this codebase", "walk me through what you just changed", "audit this refactor", "verify the LLM-generated code". Use onboard-mode triggers like "I just cloned this — walk me through it", "help me understand this codebase", "onboard me to this project", "teach me how this works", "give me a tour". Argument is a free-form natural-language description of what to walk through (e.g. "the item spawn refactor I just merged", "the auth module", "everything under src/inventory/", "this whole repo, I'm new to it").
---

# walk-codebase

Set up an interactive, browser-based codewalk so the user can review code chunk-by-chunk while you push explanations to the page.

The user invoked this with: **"$ARGUMENTS"**

## What this skill does

You will:

1. **Interpret the user's request** to decide what to walk through (a recent change set, a specific module, a whole repository, etc.) **and which mode fits** — review (audit LLM-written code) or onboard (learn an unfamiliar codebase). See "Mode selection" below.
2. **Detect the version control system** (git or perforce) for the working tree, ask the user once if neither is obvious.
3. **Propose a scope** — list the files / symbols / lines to be analyzed — and ask the user to confirm before doing expensive work. In onboard mode, also propose the **layer plan and reading order** at the same time.
4. **Run the analysis** chunk by chunk, pushing explanations to the live HTML page as you go. In onboard mode, also call `set_chunk_meta` per chunk so the left tree groups by layer and the lesson nav buttons work.
5. **Stay reactive** — the Monitor stream delivers user reactions (rerequest, comments, status changes) from the page back to you.

## Required tools

This plugin's MCP server (`codebase-cowalk`) provides the tools you will use. They are all prefixed with `mcp__codebase-cowalk__` in your tool list.

Key tools:

| Tool                             | Purpose                                                                                                                  |
| :------------------------------- | :----------------------------------------------------------------------------------------------------------------------- |
| `start_session`                  | Create a new codewalk session. Takes a natural-language `request`, a `scope` (list of file/symbol/range entries), and an optional `mode` (`"review"` default or `"onboard"`). Returns `session_id`, `slug`, `port`, and `url` of the live HTML page. |
| `propose_scope`                  | Given a list of files (or files+ranges), parse them with Tree-sitter, return a chunk preview without running explanations yet. Use this to show the user what is about to be analyzed. |
| `add_chunk`                      | Manually register a chunk that Tree-sitter didn't pick up (rare — only if you need to insert a non-AST region). |
| `split_chunk`                    | Split an existing chunk into smaller sub-chunks (use when a function is too large or has distinct logical regions worth explaining separately). |
| `list_chunks`                    | List chunks in the current session (id, file, symbol, line range, status, lesson_order, layer). |
| `get_chunk`                      | Fetch a chunk's source code as a string, ready for you to read and explain. Returns just the chunk's snippet — for surrounding context use `get_file_source`. |
| `get_file_source`                | Return the full snapshot of a file captured at session start. Use to read imports, sibling functions, type definitions, or anything outside a chunk's bounds when explaining it. The browser pane shows exactly the same snapshot with the active chunk highlighted, so what you read here is what the user is looking at. |
| `list_file_snapshots`            | List every file captured for the session (path, line_count, language) without their content. Cheap; use for indexing/discovery. |
| `append_block`                   | Append an explanation block to a chunk. `block_type` is one of: `summary`, `intent`, `behavior`, `risk`, `related`, `diagram`, `code_ref`, `warning`, `note`. `content` is markdown (Mermaid for `diagram`). Push as many blocks per chunk as the chunk warrants — not every chunk needs every block type. |
| `set_chunk_analyzed`             | Mark a chunk as analyzed (no more blocks coming for now). |
| `set_chunk_meta`                 | **Onboard mode.** Attach `lesson_order` (1-based int, global) and/or `layer` (string — convention: `foundations` / `core` / `systems` / `game` / `wiring`) to a chunk. The page sorts the left tree by layer then by lesson_order, and the "Next lesson" button steps through the chain. Calling this in review-mode sessions stores the values but the page ignores them. |
| `set_session_mode`               | Switch a session's mode mid-walk (`"review"` ↔ `"onboard"`). Useful when the user starts in one mode and pivots — combine with `set_chunk_meta` to populate the curriculum. |
| `pop_event`                      | Read the next pending user event from the page (rerequest, comment, etc.). Returns `null` if the queue is empty. The Monitor stream will also notify you when new events arrive. |
| `end_session`                    | Shut down this session's HTTP server. |

## Mode selection

Before doing anything else, pick the mode. The two modes share infrastructure
but differ in what you produce per chunk and how the page presents the result.

- **review** (default) — auditing code the user (or an LLM) just wrote/changed.
  Output emphasizes `risk`/`warning` blocks, `behavior` summaries of changes,
  diff-aware explanations. Left tree is grouped by file. Use this when the
  request is about *what changed* or *whether it's correct*.
- **onboard** — teaching a codebase to someone who has not used it before.
  Output emphasizes `intent` (why does this exist?), `behavior` (what does it
  do at a glance?), `diagram` (control/data flow), `related` (where to look
  next), and a coherent reading order. Left tree is grouped by `layer` and
  ordered by `lesson_order`. Use this when the request is about *how the
  codebase fits together* or *where to start reading*.

Heuristics for picking automatically:

| User-provided cue                                                              | Mode    |
| :----------------------------------------------------------------------------- | :------ |
| "review", "audit", "verify", "check", "did I break anything", "look at my PR"  | review  |
| "the diff", "what changed", "this refactor", "the bug fix I just did"          | review  |
| "I just cloned this", "onboard me", "teach me", "explain this project to me"   | onboard |
| "where do I start", "give me a tour", "I'm new here", "help me understand"     | onboard |
| Ambiguous and small scope (single module / file)                               | review  |
| Ambiguous and whole-repo scope, especially for unfamiliar code                 | onboard |

If still genuinely ambiguous, ask the user once: "Are you reviewing code you
already know, or trying to learn this codebase from scratch?"

## Workflow

### Step 1 — interpret the request

Read the user's request (above). Decide:

- **Mode**: review vs onboard (see "Mode selection" above).
- **Is this scoped to a recent change set?** ("the refactor I just did", "this PR", "what we changed today") → use git/p4 to find changed files. Almost always implies review mode.
- **Is this a specific subtree or module?** ("the auth module", "src/inventory/") → use file globs. Either mode possible.
- **Is this the whole codebase?** ("explain this project", "walk me through the repo") → use the project root, with `.gitignore` and conventional excludes (`node_modules`, `.venv`, `dist`, `build`, lock files, binaries) applied. Most often onboard mode.

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
    mode=<"review" or "onboard", default "review">,
)
```

You get back `{ session_id, slug, port, url }`. Tell the user the URL and recommend opening it in their browser. Example:

> Live codewalk: **http://localhost:54321** — open this in your browser. The page will fill in as I push explanations.

In **onboard** mode, immediately after `start_session` returns and **before** Step 5 analysis, do the planning pass described in "Onboard mode workflow" below: detect entry points, decide layers, and assign `lesson_order` to every chunk via `set_chunk_meta`. The page will start grouping/ordering as soon as the metadata lands.

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

## Onboard mode workflow

This is the curriculum-style mode. Your job is to turn a codebase into a
sensible *reading order* with coarse layers, so a learner can move from "I
just cloned this" to "I have a working mental model" without being dropped
into a 500-chunk alphabetic file dump.

Run this **after** `start_session(mode="onboard", ...)` returns, and **before**
analyzing chunks. The order is: chunks land → metadata is assigned → analysis
starts. The page picks up the metadata live, so the layer grouping appears
before the first explanation block does.

### Step O1 — find the entry points

Read just enough of the codebase to identify where execution actually starts.
Use `Bash`/`Glob`/`Grep` and `get_file_source` (snapshots are already saved).
Common shapes:

- C/C++: `int main(`, `WinMain`, `App::Run`, `GameLoop::Tick`, `Engine::Init`
- Python: `if __name__ == "__main__"`, `manage.py`, FastAPI `app = FastAPI()`, Click `@cli.command`
- Node/TS: `bin/*.js`, `index.ts`, `server.ts`, top-level `app.listen`
- Web frontends: route definitions, top-level `App` component, router config
- Game engines: scene/level constructors, main loop tick

Don't over-read at this stage — the goal is just to anchor the rest of the
plan. One or two entry points is usually plenty.

### Step O2 — choose layers

Group the codebase into **3–6 coarse layers** that a learner should consume
in order. Conventional names the UI knows about (used to sort the left tree):

| Layer name      | Typical contents                                                             |
| :-------------- | :--------------------------------------------------------------------------- |
| `foundations`   | Constants, primitive types, shared math/utils, project-wide invariants       |
| `core`          | Central data structures, the abstraction every other layer depends on (ECS World, scheduler, IoC container, ORM, etc.) |
| `systems`       | Logic that operates on the core abstraction (game systems, services, jobs)   |
| `game` / `app`  | Domain-specific composition: scenes, screens, business workflows             |
| `wiring`        | Entry points, bootstrap, DI registration, configuration loading              |

You can use other layer names when the conventional set doesn't fit (e.g.
`parser`, `runtime`, `cli` for a compiler) — the UI falls back to alphabetical
for unknown layers. **Stay coarse.** If you find yourself wanting eight layers
you're probably designing modules, not a reading order; collapse some.

### Step O3 — assign `lesson_order`

For each chunk in the session, decide **what reading order makes the chunk
understandable when the learner gets there**, given everything they have read
so far. Rules of thumb:

- A chunk's prerequisites should already have lower `lesson_order`.
- Within a layer, simpler/more-foundational comes first.
- Hot-paths and "you'll see this everywhere" abstractions come earlier than
  edge-case handlers.
- Tests, fixtures, and dev tooling can stay at the end (or get no `lesson_order`
  if you don't want to walk them at all — those chunks are filtered out of the
  Lesson nav, but still appear in the layer they belong to).

Don't try to be perfect. The user can always click ahead. Concretely: assign
`lesson_order = 1, 2, 3, ...` globally across the whole session (NOT per
layer — the UI re-groups by layer for you, but uses the global order to
break ties and to drive the Next-lesson button).

Use `set_chunk_meta(chunk_id, lesson_order=N, layer="...")` once per chunk.
You can batch this in a tight loop after deciding the plan; it's cheap.

### Step O4 — show the user the plan, then analyze

Before you start pushing explanation blocks, summarize the plan in chat:

> Onboard plan for **\<repo name\>**:
> 1. **foundations** (12 chunks) — constants, math utils, the entity handle type
> 2. **core** (28 chunks) — the ECS World, ComponentStorage, EventBus
> 3. **systems** (60 chunks) — movement, spawning, weapons, pickups
> 4. **game** (40 chunks) — scenes, level wiring
> 5. **wiring** (8 chunks) — `main`, asset loading, run loop
>
> First lesson is `core/Constants.h`. Confirm before I start explaining?

After confirmation, proceed with Step 5 chunk analysis as usual, but **walk
chunks in `lesson_order`**, not in tree-sitter sequence. The block emphasis
shifts toward `intent` / `behavior` / `diagram` / `related` rather than
`risk` / `warning` (still surface real risks if you spot them, but onboard's
job is comprehension, not audit).

For non-trivial layers, push a `diagram` block (Mermaid) onto the *first*
chunk of that layer summarizing how the layer hangs together. This gives the
learner an anchor before they descend into details.

### Step O5 — react to user events the same way

`pop_event` semantics are unchanged. In onboard mode, treat
`status: "unknown"` as a strong signal the learner is lost on that chunk —
consider proactively pushing extra `intent`/`related` blocks even without an
explicit `rerequest`.

If the user mid-walk says "I'm getting it, can you switch to actual review
mode now?", call `set_session_mode(session_id, "review")`; the layer headers
disappear and the tree returns to file grouping without restarting analysis.

## Resume mode

If the user invokes this skill with `resume <slug>`, call `start_session(resume=<slug>)` instead of creating a new one. The HTTP server will be re-launched on a fresh port and the existing chunks/blocks/states will be served.

## Import mode

If the user wants to do their own review on top of a `.cwlk` archive someone shared (e.g. "I want to verify what Alice walked through"), call `mcp__codebase-cowalk__import_codewalk(path=<.cwlk path>)` instead of starting from scratch. The original chunks and explanation blocks come along; review state and comments start fresh so the new reviewer's marks aren't conflated with the original author's. For a purely passive read of someone else's review, use `/codebase-cowalk:view-codewalk` instead — that's read-only.

## Things to NOT do

- **Do NOT modify any source files.** This skill is strictly read-only. Do not call `Edit`, `Write`, `NotebookEdit`, `sed`/`awk`, or any other mutating tool on the working tree, even if you spot a bug, typo, dead code, or "obvious" improvement while reading a chunk. The user's job during a codewalk is to *decide* what to change — not yours. If you spot an issue, surface it as a `risk` or `warning` block on the relevant chunk via `append_block`. The only writes you should perform are MCP `append_block` / `set_chunk_analyzed` / session-state calls into the cowalk DB. If the user explicitly asks mid-walk for you to apply a fix, pause the walk, confirm the scope of the edit, and treat that as a separate task outside the codewalk skill.
- Don't render explanations directly in chat. The whole point is the HTML page. Tell the user the URL and push to the page.
- Don't skip `propose_scope` for large scopes. The user must approve token cost.
- Don't paste large chunks of source code in chat.
- Don't fabricate file paths or symbols. If unsure, run `Bash(ls)` or `Glob` first.

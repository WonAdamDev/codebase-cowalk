---
name: view-codewalk
description: Open a previously-exported `.cwlk` file in read-only viewer mode. Invoke this only when the user explicitly asks to view, open, or look at a `.cwlk` file someone shared with them.
disable-model-invocation: true
---

# view-codewalk

Open a `.cwlk` file in read-only mode. The reviewer's marks, comments, and explanation blocks are all displayed exactly as the original author saw them — but no edits, re-requests, or new analysis are possible.

The user invoked this with: **"$ARGUMENTS"**

## Workflow

1. Parse the argument as a path to a `.cwlk` file. If empty or invalid, ask the user for the path.
2. Call `mcp__codebase-cowalk__open_export(path=<path>)`. The server unpacks the archive into a temporary working directory and starts a read-only HTTP server on a free port.
3. Return the URL to the user:

   > Opening **`<slug>.cwlk`** (READ-ONLY) at **http://localhost:54322** — exported by `<author>` on `<date>`.

## What read-only mode disables

- The ✓ / 🚩 / ❓ status toggles
- The "Re-explain" button
- The comment input field
- The Monitor stream / event queue (no events accepted from the page)

A read-only banner is shown at the top of every page so the viewer cannot mistake it for a live session.

## When the viewer closes

Call `mcp__codebase-cowalk__end_view(slug=<slug>)` when the user says they're done. The temporary working directory is cleaned up.

## Things this skill does NOT do

- It does not let the viewer add their own marks or comments. (If they want to do their own review, suggest they run `/codebase-cowalk:walk-codebase` on the same codebase to start a fresh independent session.)
- It does not phone home or upload anything. The whole flow stays on the local machine.

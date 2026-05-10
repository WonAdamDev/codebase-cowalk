// codebase-cowalk client.
//
// State is hydrated from the JSON in #initial-state on first load. In live mode
// we open an SSE stream to /sse and mutate the state in place; in static mode
// (export) we just render once.

(function () {
  "use strict";

  const initial = JSON.parse(document.getElementById("initial-state").textContent);
  const LIVE = window.__COWALK_LIVE__ === true;
  const READ_ONLY = window.__COWALK_READ_ONLY__ === true;

  const state = {
    session: initial.session,
    chunks: initial.chunks || [],
    blocksByChunk: initial.blocks_by_chunk || {},
    commentsByChunk: initial.comments_by_chunk || {},
    progress: initial.progress || { total: 0, analyzed: 0, reviewed: 0 },
    chunkCodeById: initial.chunk_code_by_id || {},
    fileSourcesByPath: initial.file_sources_by_path || {},
    activeChunkId: null,
    filter: { text: "", status: "" },
  };

  // ---- DOM refs ---------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const tree = $("tree");
  const chunkTitle = $("chunk-title");
  const chunkMeta = $("chunk-meta");
  const chunkCode = $("chunk-code").querySelector("code");
  const blocksEl = $("blocks");
  const commentsEl = $("comments");
  const filterInput = $("filter-input");
  const filterStatus = $("filter-status");
  const themeToggle = $("theme-toggle");
  const barAnalyzed = $("bar-analyzed");
  const barReviewed = $("bar-reviewed");
  const lblAnalyzed = $("lbl-analyzed");
  const lblReviewed = $("lbl-reviewed");
  const commentInput = $("comment-input");

  // ---- minimal markdown renderer --------------------------------------

  function escapeHtml(s) {
    return s.replace(/[&<>"']/g, (c) => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#39;",
    }[c]));
  }

  function renderMarkdown(src) {
    // strip leading/trailing newlines
    src = src.replace(/^\n+|\n+$/g, "");
    const lines = src.split("\n");
    let out = [];
    let inCode = false;
    let codeBuf = [];
    let codeLang = "";
    let inList = false;

    function closeList() {
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
    }

    function inlineMd(s) {
      s = escapeHtml(s);
      // chunk reference [c-0042] -> link
      s = s.replace(/\[(c-\d{4}(?:\.\d+)?)\]/g, (_, id) =>
        `<a href="#" class="chunk-link" data-chunk="${id}">[${id}]</a>`
      );
      // [text](url)
      s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (_, t, u) =>
        `<a href="${u}" target="_blank" rel="noopener">${t}</a>`
      );
      // inline code
      s = s.replace(/`([^`]+)`/g, (_, c) => `<code>${c}</code>`);
      // bold
      s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
      // italic (avoid eating ** that we already replaced)
      s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      return s;
    }

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      // fenced code blocks
      const fenceMatch = /^```(\w*)\s*$/.exec(line);
      if (fenceMatch) {
        if (!inCode) {
          closeList();
          inCode = true;
          codeBuf = [];
          codeLang = fenceMatch[1] || "";
        } else {
          out.push(
            `<pre data-lang="${codeLang}"><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`
          );
          inCode = false;
        }
        continue;
      }
      if (inCode) {
        codeBuf.push(line);
        continue;
      }
      // heading
      const h = /^(#{1,6})\s+(.+)$/.exec(line);
      if (h) {
        closeList();
        const lvl = h[1].length;
        out.push(`<h${lvl}>${inlineMd(h[2])}</h${lvl}>`);
        continue;
      }
      // list item
      const li = /^[-*]\s+(.+)$/.exec(line);
      if (li) {
        if (!inList) {
          out.push("<ul>");
          inList = true;
        }
        out.push(`<li>${inlineMd(li[1])}</li>`);
        continue;
      }
      // blank line -> paragraph break
      if (!line.trim()) {
        closeList();
        out.push("");
        continue;
      }
      closeList();
      out.push(`<p>${inlineMd(line)}</p>`);
    }
    if (inCode) {
      out.push(`<pre><code>${escapeHtml(codeBuf.join("\n"))}</code></pre>`);
    }
    closeList();
    return out.join("\n");
  }

  // ---- left tree -------------------------------------------------------

  function chunkMatchesFilter(c) {
    const text = state.filter.text.trim().toLowerCase();
    if (text) {
      const hay = `${c.file_path} ${c.symbol_path || ""}`.toLowerCase();
      if (!hay.includes(text)) return false;
    }
    const st = state.filter.status;
    if (!st) return true;
    if (st === "unreviewed") return !c.review_status;
    if (st === "diff") {
      const meta = state.chunkCodeById[c.id];
      // diff info isn't in code map; fall back to checking blocks list
      return c.has_diff === true;
    }
    return c.review_status === st;
  }

  // Conventional layer order for onboard mode. Unknown layers are appended
  // after these in alphabetical order.
  const LAYER_ORDER = ["foundations", "core", "systems", "game", "wiring"];
  function layerRank(name) {
    const i = LAYER_ORDER.indexOf((name || "").toLowerCase());
    return i === -1 ? 1000 + (name || "zzz").charCodeAt(0) / 1000 : i;
  }

  function currentMode() {
    return (state.session && state.session.mode) || "review";
  }

  // List of chunks the user is meant to walk through in order, used for
  // "previous/next lesson" navigation in onboard mode. Chunks without a
  // lesson_order are excluded from this list (so the buttons skip over them).
  function lessonChain() {
    return state.chunks
      .filter((c) => c.status !== "split" && Number.isFinite(c.lesson_order))
      .sort((a, b) => a.lesson_order - b.lesson_order);
  }

  function buildChunkNode(c) {
    const el = document.createElement("div");
    el.className = "tree-chunk";
    if (c.status !== "analyzed") el.classList.add("unanalyzed");
    if (c.id === state.activeChunkId) el.classList.add("active");
    el.dataset.chunk = c.id;

    const stIcon = document.createElement("span");
    stIcon.className = "tree-chunk-status " + (c.review_status || "");
    stIcon.textContent =
      c.review_status === "ok" ? "✓" :
      c.review_status === "suspicious" ? "🚩" :
      c.review_status === "unknown" ? "❓" : "·";
    el.appendChild(stIcon);

    if (currentMode() === "onboard" && Number.isFinite(c.lesson_order)) {
      const lesson = document.createElement("span");
      lesson.className = "tree-chunk-lesson";
      lesson.textContent = c.lesson_order;
      el.appendChild(lesson);
    }

    const name = document.createElement("span");
    name.className = "tree-chunk-name";
    name.textContent = c.symbol_path || `lines ${c.line_start}-${c.line_end}`;
    el.appendChild(name);

    const meta = document.createElement("span");
    meta.className = "tree-chunk-meta";
    meta.textContent = `${c.line_end - c.line_start + 1}L`;
    el.appendChild(meta);

    el.addEventListener("click", () => activate(c.id));
    return el;
  }

  function renderTreeByFile() {
    const byFile = {};
    for (const c of state.chunks) {
      if (c.status === "split") continue;
      if (!chunkMatchesFilter(c)) continue;
      (byFile[c.file_path] ||= []).push(c);
    }
    const files = Object.keys(byFile).sort();
    if (!files.length) {
      tree.innerHTML = '<div class="empty">no chunks match the filter</div>';
      return;
    }
    for (const f of files) {
      const head = document.createElement("div");
      head.className = "tree-file";
      head.textContent = shortenPath(f);
      head.title = f;
      tree.appendChild(head);
      for (const c of byFile[f]) tree.appendChild(buildChunkNode(c));
    }
  }

  function renderTreeByLayer() {
    const byLayer = {};
    for (const c of state.chunks) {
      if (c.status === "split") continue;
      if (!chunkMatchesFilter(c)) continue;
      const key = c.layer || "(unassigned)";
      (byLayer[key] ||= []).push(c);
    }
    const layers = Object.keys(byLayer).sort((a, b) => layerRank(a) - layerRank(b));
    if (!layers.length) {
      tree.innerHTML = '<div class="empty">no chunks match the filter</div>';
      return;
    }
    for (const layer of layers) {
      const head = document.createElement("div");
      head.className = "tree-layer";
      head.textContent = layer;
      tree.appendChild(head);
      const lessonsFirst = byLayer[layer].slice().sort((a, b) => {
        const ao = Number.isFinite(a.lesson_order) ? a.lesson_order : Infinity;
        const bo = Number.isFinite(b.lesson_order) ? b.lesson_order : Infinity;
        if (ao !== bo) return ao - bo;
        return a.sequence - b.sequence;
      });
      // Sub-group by file under each layer so the user still sees structure.
      let lastFile = null;
      for (const c of lessonsFirst) {
        if (c.file_path !== lastFile) {
          const sub = document.createElement("div");
          sub.className = "tree-file tree-file-sub";
          sub.textContent = shortenPath(c.file_path);
          sub.title = c.file_path;
          tree.appendChild(sub);
          lastFile = c.file_path;
        }
        tree.appendChild(buildChunkNode(c));
      }
    }
  }

  function renderTree() {
    tree.innerHTML = "";
    if (currentMode() === "onboard") renderTreeByLayer();
    else renderTreeByFile();
    renderLessonNav();
  }

  function shortenPath(p) {
    const parts = p.replace(/\\/g, "/").split("/");
    if (parts.length <= 3) return p;
    return ".../" + parts.slice(-3).join("/");
  }

  // ---- center code ----------------------------------------------------

  // Render the *whole file* the chunk lives in, then dim everything outside
  // the chunk's [line_start..line_end] so the reviewer can see surrounding
  // context (imports, sibling functions) without losing focus on the chunk.
  // If a file snapshot is unavailable (legacy session, manual chunk for an
  // out-of-scope file), fall back to rendering only the chunk snippet.
  function renderCode(c) {
    chunkCode.innerHTML = "";
    const fileSrc = state.fileSourcesByPath[c.file_path];
    const added = new Set(c.diff_added_lines || []);
    const removed = new Set(c.diff_removed_lines || []);

    if (fileSrc && typeof fileSrc.content === "string") {
      const lines = fileSrc.content.split("\n");
      // trailing empty string from a final newline — drop it so we don't render a phantom line
      if (lines.length && lines[lines.length - 1] === "") lines.pop();
      const frag = document.createDocumentFragment();
      for (let i = 0; i < lines.length; i++) {
        const lineNum = i + 1;
        const span = document.createElement("span");
        span.className = "code-line";
        span.dataset.ln = lineNum;
        if (lineNum >= c.line_start && lineNum <= c.line_end) {
          span.classList.add("in-chunk");
          if (lineNum === c.line_start) span.classList.add("chunk-start");
          if (lineNum === c.line_end) span.classList.add("chunk-end");
        } else {
          span.classList.add("out-of-chunk");
        }
        if (added.has(lineNum)) span.classList.add("added");
        if (removed.has(lineNum)) span.classList.add("removed");
        span.textContent = lines[i] + "\n";
        frag.appendChild(span);
      }
      chunkCode.appendChild(frag);
      // Scroll the first line of the chunk into view, near the top of the pane.
      const target = chunkCode.querySelector(`.code-line[data-ln="${c.line_start}"]`);
      if (target) {
        // requestAnimationFrame so layout has happened before scrollIntoView measures
        requestAnimationFrame(() =>
          target.scrollIntoView({ behavior: "auto", block: "start" })
        );
      }
      return;
    }

    // Fallback: chunk-only view (legacy / out-of-scope file).
    const code = state.chunkCodeById[c.id] || "";
    const lines = code.split("\n");
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    for (let i = 0; i < lines.length; i++) {
      const lineNum = c.line_start + i;
      const span = document.createElement("span");
      span.className = "code-line in-chunk";
      span.dataset.ln = lineNum;
      if (added.has(lineNum)) span.classList.add("added");
      if (removed.has(lineNum)) span.classList.add("removed");
      span.textContent = lines[i] + "\n";
      chunkCode.appendChild(span);
    }
  }

  function renderHeader(c) {
    chunkTitle.textContent = c.symbol_path
      ? `${c.symbol_path}`
      : `${shortenPath(c.file_path)}:${c.line_start}-${c.line_end}`;
    const parts = [
      `<span>${shortenPath(c.file_path)}</span>`,
      `<span>L${c.line_start}-${c.line_end}</span>`,
    ];
    if (c.language) parts.push(`<span>${c.language}</span>`);
    if (c.status === "analyzed") parts.push(`<span style="color:var(--status-ok)">analyzed</span>`);
    chunkMeta.innerHTML = parts.join(" · ");
  }

  // ---- right blocks ---------------------------------------------------

  function renderBlocks(c) {
    const blocks = state.blocksByChunk[c.id] || [];
    blocksEl.innerHTML = "";
    if (!blocks.length) {
      blocksEl.innerHTML = '<div class="empty">No explanation blocks yet.</div>';
    }
    const maxVersion = blocks.reduce((m, b) => Math.max(m, b.version), 0);
    for (const b of blocks) {
      const isOlder = b.version < maxVersion;
      const wrap = document.createElement("div");
      wrap.className = "block " + b.block_type + (isOlder ? " older" : "");
      const head = document.createElement("div");
      head.className = "block-head";
      head.innerHTML = `<span>${b.block_type}</span>${maxVersion > 1 ? `<span class="block-version-tag">v${b.version}</span>` : ""}`;
      wrap.appendChild(head);
      const body = document.createElement("div");
      body.className = "block-content";
      if (b.block_type === "diagram") {
        body.innerHTML = `<pre>${escapeHtml(b.content)}</pre>`;
      } else {
        body.innerHTML = renderMarkdown(b.content);
      }
      // chunk-link clicks
      body.querySelectorAll(".chunk-link").forEach((a) => {
        a.addEventListener("click", (e) => {
          e.preventDefault();
          activate(a.dataset.chunk);
        });
      });
      wrap.appendChild(body);
      // line refs
      if (b.line_ref_start) {
        const refBtn = document.createElement("button");
        refBtn.className = "action-btn";
        refBtn.textContent = `lines ${b.line_ref_start}-${b.line_ref_end || b.line_ref_start}`;
        refBtn.style.marginTop = "4px";
        refBtn.addEventListener("click", () => highlightLines(b.line_ref_start, b.line_ref_end || b.line_ref_start));
        wrap.appendChild(refBtn);
      }
      blocksEl.appendChild(wrap);
    }
  }

  function renderComments(c) {
    const comments = state.commentsByChunk[c.id] || [];
    commentsEl.innerHTML = "";
    for (const cm of comments) {
      const el = document.createElement("div");
      el.className = "comment";
      const t = new Date(cm.created_at * 1000).toLocaleString();
      el.innerHTML = `<span>${escapeHtml(cm.body)}</span><span class="comment-time">${t}</span>`;
      commentsEl.appendChild(el);
    }
  }

  function renderStatusToggle(c) {
    document.querySelectorAll(".status-btn").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.status === (c.review_status || ""));
    });
  }

  function highlightLines(start, end) {
    document.querySelectorAll(".code-line.highlight").forEach((e) => e.classList.remove("highlight"));
    document.querySelectorAll(".code-line").forEach((e) => {
      const ln = +e.dataset.ln;
      if (ln >= start && ln <= end) e.classList.add("highlight");
    });
    const first = document.querySelector(`.code-line[data-ln="${start}"]`);
    if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  // ---- activate -------------------------------------------------------

  function activate(chunkId) {
    const c = state.chunks.find((c) => c.id === chunkId);
    if (!c) return;
    state.activeChunkId = chunkId;
    document.querySelectorAll(".tree-chunk").forEach((e) => {
      e.classList.toggle("active", e.dataset.chunk === chunkId);
    });
    // Make sure the active chunk is visible in the left tree (it can be off-screen
    // if the user just stepped a lesson via the keyboard / next button).
    const treeNode = document.querySelector(`.tree-chunk[data-chunk="${chunkId}"]`);
    if (treeNode && typeof treeNode.scrollIntoView === "function") {
      treeNode.scrollIntoView({ block: "nearest" });
    }
    renderHeader(c);
    renderCode(c);
    renderBlocks(c);
    renderComments(c);
    renderStatusToggle(c);
    renderLessonNav();
  }

  // ---- progress -------------------------------------------------------

  function renderProgress() {
    const p = state.progress;
    const total = Math.max(1, p.total);
    barAnalyzed.style.width = `${(p.analyzed / total) * 100}%`;
    barReviewed.style.width = `${(p.reviewed / total) * 100}%`;
    lblAnalyzed.textContent = `${p.analyzed}/${p.total}`;
    lblReviewed.textContent = `${p.reviewed}/${p.total}`;
  }

  // ---- onboard lesson navigation -------------------------------------

  function renderLessonNav() {
    let nav = document.getElementById("lesson-nav");
    if (currentMode() !== "onboard") {
      if (nav) nav.remove();
      document.body.classList.remove("mode-onboard");
      return;
    }
    document.body.classList.add("mode-onboard");
    const chain = lessonChain();
    if (!chain.length) {
      if (nav) nav.remove();
      return;
    }
    if (!nav) {
      nav = document.createElement("div");
      nav.id = "lesson-nav";
      nav.innerHTML = `
        <button id="lesson-prev" class="action-btn" title="Previous lesson">◀</button>
        <span id="lesson-label">Lesson —</span>
        <button id="lesson-next" class="action-btn" title="Next lesson">▶</button>
      `;
      const actions = document.getElementById("chunk-actions");
      actions.parentNode.insertBefore(nav, actions);
      document.getElementById("lesson-prev").addEventListener("click", () => stepLesson(-1));
      document.getElementById("lesson-next").addEventListener("click", () => stepLesson(+1));
    }
    const label = document.getElementById("lesson-label");
    const idx = chain.findIndex((c) => c.id === state.activeChunkId);
    if (idx === -1) {
      label.textContent = `Lesson — / ${chain.length}`;
    } else {
      const lo = chain[idx].lesson_order;
      label.textContent = `Lesson ${idx + 1} / ${chain.length} (#${lo})`;
    }
    document.getElementById("lesson-prev").disabled = idx <= 0;
    document.getElementById("lesson-next").disabled = idx === -1 ? chain.length === 0 : idx >= chain.length - 1;
  }

  function stepLesson(delta) {
    const chain = lessonChain();
    if (!chain.length) return;
    let idx = chain.findIndex((c) => c.id === state.activeChunkId);
    if (idx === -1) idx = delta > 0 ? -1 : chain.length;
    const next = idx + delta;
    if (next < 0 || next >= chain.length) return;
    activate(chain[next].id);
  }

  // ---- AJAX ----------------------------------------------------------

  async function postJSON(url, body) {
    if (READ_ONLY) return;
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(await r.text());
    return r.json();
  }

  // ---- SSE -----------------------------------------------------------

  function connectSSE() {
    if (!LIVE) return;
    const es = new EventSource("/sse");
    es.addEventListener("chunk_added", (e) => {
      const data = JSON.parse(e.data);
      const idx = state.chunks.findIndex((c) => c.id === data.id);
      if (idx >= 0) state.chunks[idx] = data;
      else state.chunks.push(data);
      state.chunkCodeById[data.id] = data.code;
      renderTree();
    });
    es.addEventListener("block_added", (e) => {
      const data = JSON.parse(e.data);
      (state.blocksByChunk[data.chunk_id] ||= []).push(data);
      if (state.activeChunkId === data.chunk_id) {
        renderBlocks(state.chunks.find((c) => c.id === data.chunk_id));
      }
    });
    es.addEventListener("chunk_status", (e) => {
      const data = JSON.parse(e.data);
      const c = state.chunks.find((c) => c.id === data.id);
      if (!c) return;
      Object.assign(c, data);
      renderTree();
      if (state.activeChunkId === data.id) {
        renderHeader(c);
        renderStatusToggle(c);
      }
    });
    es.addEventListener("progress", (e) => {
      state.progress = JSON.parse(e.data);
      renderProgress();
    });
    es.addEventListener("comment_added", (e) => {
      const data = JSON.parse(e.data);
      (state.commentsByChunk[data.chunk_id] ||= []).push(data);
      if (state.activeChunkId === data.chunk_id) {
        renderComments(state.chunks.find((c) => c.id === data.chunk_id));
      }
    });
    es.addEventListener("chunk_meta", (e) => {
      const data = JSON.parse(e.data);
      const c = state.chunks.find((c) => c.id === data.id);
      if (!c) return;
      if (data.lesson_order !== undefined) c.lesson_order = data.lesson_order;
      if (data.layer !== undefined) c.layer = data.layer;
      renderTree();
    });
    es.addEventListener("session_mode", (e) => {
      const data = JSON.parse(e.data);
      state.session = state.session || {};
      state.session.mode = data.mode;
      renderTree();
    });
    es.onerror = () => {
      // browser auto-reconnects; nothing to do
    };
  }

  // ---- wire UI -------------------------------------------------------

  filterInput.addEventListener("input", () => {
    state.filter.text = filterInput.value;
    renderTree();
  });
  filterStatus.addEventListener("change", () => {
    state.filter.status = filterStatus.value;
    renderTree();
  });
  themeToggle.addEventListener("click", () => {
    document.documentElement.classList.toggle("force-light");
  });

  if (!READ_ONLY) {
    document.querySelectorAll(".status-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        if (!state.activeChunkId) return;
        const status = btn.dataset.status || null;
        await postJSON("/api/status", { chunk_id: state.activeChunkId, status });
      });
    });
    $("btn-rerequest").addEventListener("click", async () => {
      if (!state.activeChunkId) return;
      const note = prompt("Optional note for Claude (what would you like clarified?):") || "";
      await postJSON("/api/rerequest", { chunk_id: state.activeChunkId, note });
      alert("Re-explanation requested. Claude will pick this up shortly.");
    });
    $("btn-comment").addEventListener("click", async () => {
      if (!state.activeChunkId) return;
      const body = commentInput.value.trim();
      if (!body) return;
      await postJSON("/api/comment", { chunk_id: state.activeChunkId, body });
      commentInput.value = "";
    });
  }

  // ---- bootstrap -----------------------------------------------------

  renderTree();
  renderProgress();
  // In onboard mode, start at lesson 1 if Claude has populated lesson orders;
  // otherwise fall back to the first chunk like review mode.
  let bootChunkId = null;
  if (state.chunks.length) {
    if (currentMode() === "onboard") {
      const chain = lessonChain();
      bootChunkId = (chain[0] || state.chunks[0]).id;
    } else {
      bootChunkId = state.chunks[0].id;
    }
  }
  if (bootChunkId) activate(bootChunkId);
  connectSSE();
})();

/* Gnomon — deliverable ledger UI.
 *
 * Served ONLY while deliverable_ledger is ON (the server injects the script
 * tag into "/" and gates this file behind the same flag; it is deliberately NOT under
 * /static). It adds, without touching index.html or app.js:
 *   - a banner under the header: "N unseen deliverables" (click -> the tab);
 *   - a "Deliverables" tab: a history list (Unseen / All), per-row "Mark seen", "Mark all seen".
 * The page never opens, fetches or renders a file itself (4B is the viewer). The per-row "Open" button
 * (deliverable_open, 2026-10-03) only asks the SERVER, by deliverable ID, to open the entry with the
 * operating system's opener and mark it seen; no path is ever sent. The server says whether to show it
 * (the list response's open_enabled).
 *
 * buildView() is pure (no DOM) and tested under node (tests/test_rm4a_deliverable_ledger.py).
 * All DOM is built with textContent — no HTML strings. Design tokens come from styles.css
 * (macOS dark, system sans, no monospaced fonts).
 *
 * UMD: window.GmDeliverableLedger in the browser, module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDeliverableLedger = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const POLL_MS = 30000;
  const LIST_LIMIT = 500;
  const LABEL_MAX = 200;
  const UNKNOWN_AUTHOR = "__unknown__";                 // the server's value for "no author recorded"
  const AUTHOR_KEY = "gm.deliverables.author";           // localStorage: the chosen author filter, per browser

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }

  function splitPath(p) {
    const s = String(p || "");
    const i = s.lastIndexOf("/");
    return i < 0 ? { dir: "", base: s } : { dir: s.slice(0, i + 1), base: s.slice(i + 1) };
  }

  // One label per writer: "main" or "<agent type>: <description> (<id8>)".
  function writerLabel(w) {
    if (!w || !w.agent_id) return "main";
    // Defence in depth: the server already cuts both to 200 characters; a label is TEXT only.
    const t = String(w.agent_type || "subagent").slice(0, LABEL_MAX);
    const d = w.description ? ": " + String(w.description).slice(0, LABEL_MAX) : "";
    return t + d + " (" + String(w.agent_id).slice(0, 8) + ")";
  }

  function whenText(iso) {
    if (!iso) return "unknown time";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  // The author filter's choices: "All", then each author in the ledger (the ledger's `instance`), with
  // entries that have no recorded author under "Unknown author" (never hidden). value "" = All.
  function authorOptions(resp) {
    const list = resp && Array.isArray(resp.authors) ? resp.authors : [];
    const opts = [{ value: "", label: "All authors" }];
    list.forEach(function (a) {
      const unknown = a.author === UNKNOWN_AUTHOR;
      opts.push({ value: String(a.author), label: (unknown ? "Unknown author" : String(a.author).slice(0, LABEL_MAX)) +
        " (" + (a.count || 0) + ")" });
    });
    return opts;
  }

  // Per-browser memory of the choice. Storage can be missing, disabled or full: every access is guarded and
  // a failure just means "no memory" (All), never an error.
  function loadSavedAuthor(win) {
    try { const v = win.localStorage.getItem(AUTHOR_KEY); return typeof v === "string" ? v : ""; }
    catch (e) { return ""; }
  }
  function saveAuthor(win, v) {
    try { win.localStorage.setItem(AUTHOR_KEY, v); return true; } catch (e) { return false; }
  }

  // resp: the GET /api/deliverables body -> a view model (plain data).
  function buildView(resp) {
    if (!resp || typeof resp !== "object") return { error: "no deliverables response (the server returned nothing)" };
    if (resp.enabled === false) return { disabled: true, message: "the deliverable ledger is off" };
    const entries = Array.isArray(resp.entries) ? resp.entries : [];
    const unseen = typeof resp.unseen_count === "number" ? resp.unseen_count : 0;
    const rows = entries.map(function (e) {
      const sp = splitPath(e.path);
      const seen = !!e.seen;
      const writers = (Array.isArray(e.writers) ? e.writers : []).map(writerLabel);
      return {
        id: e.id,
        path: e.path,
        dir: sp.dir,
        base: sp.base,
        action: e.action === "new" ? "new" : "modified",
        actionCls: "gm-ledger-chip is-" + (e.action === "new" ? "new" : "modified"),
        session: e.session,
        instance: e.instance || "unknown instance",
        author: e.instance || UNKNOWN_AUTHOR,
        count: e.count || 1,
        when: whenText(e.last_ts),
        seen: seen,
        writers: writers,
        cls: "gm-row gm-ledger-row" + (seen ? " is-seen" : " is-unseen"),
        meta: (e.instance || "unknown instance") + " · " + e.session + " · " +
          plural(e.count || 1, "write") + " · last " + whenText(e.last_ts) +
          (writers.length ? " · by " + writers.join(", ") : ""),
      };
    });
    const scan = resp.scan || {};
    const notes = [];
    if (scan.complete === false) {
      notes.push("scan incomplete (" + (scan.stop_reasons || []).join(", ") + "): the list is a lower bound; it fills in on the next refresh");
    }
    (scan.problems || []).forEach(function (p) { notes.push(p.session + ": " + p.status); });
    if (scan.persisted === false) notes.push("the ledger could not be saved to disk: this list is from the in-memory scan");
    (resp.warnings || []).forEach(function (w) { notes.push(String(w)); });
    return {
      viewer: resp.viewer,
      filter: resp.filter,
      unseen: unseen,
      total: resp.total || 0,
      truncated: !!resp.truncated,
      rows: rows,
      openEnabled: resp.open_enabled === true,
      author: typeof resp.author === "string" ? resp.author : "",
      authorOptions: authorOptions(resp),
      bannerText: unseen > 0 ? plural(unseen, "unseen deliverable") : "",
      summary: plural(resp.total || 0, "entry") + " shown · " + unseen + " unseen",
      notes: notes,
    };
  }

  const CSS = [
    ".gm-ledger-banner{display:none;margin:0;padding:var(--gm-pad-sm) var(--gm-pad-lg);background:var(--gm-elevated-bg);",
    "border-bottom:1px solid var(--gm-border);color:var(--gm-text-primary);font-size:var(--gm-fs-sm);cursor:pointer}",
    ".gm-ledger-banner.is-on{display:block}",
    ".gm-ledger-banner b{color:var(--gm-accent-hover)}",
    ".gm-ledger-chip{display:inline-block;padding:1px 8px;margin-right:var(--gm-pad-sm);border-radius:var(--gm-radius-sm);",
    "font-size:var(--gm-fs-sm);border:1px solid var(--gm-border)}",
    ".gm-ledger-chip.is-new{color:var(--gm-live);border-color:var(--gm-live)}",
    ".gm-ledger-chip.is-modified{color:var(--gm-accent-hover);border-color:var(--gm-accent)}",
    ".gm-ledger-row.is-seen{opacity:.6}",
    ".gm-ledger-row .gm-ledger-path{word-break:break-all}",
    ".gm-ledger-dir{color:var(--gm-text-secondary)}",
    ".gm-ledger-note{color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);margin:var(--gm-pad-xs) 0}",
    ".gm-ledger-note.is-warn{color:var(--gm-danger)}",
    ".gm-ledger-author{background:var(--gm-elevated-bg);color:var(--gm-text-primary);border:1px solid var(--gm-border);",
    "border-radius:var(--gm-radius-sm);padding:2px 8px;font-family:var(--gm-font);font-size:var(--gm-fs-sm)}",
  ].join("\n");

  function el(doc, tag, cls, text) {
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // els: {list, status, notes}; view from buildView; handlers: {onSeen(id), onOpen(id)}.
  function render(els, view, doc, handlers) {
    els.list.textContent = "";
    els.notes.textContent = "";
    if (view.error) { els.status.textContent = view.error; els.list.appendChild(el(doc, "div", "gm-empty", view.error)); return; }
    if (view.disabled) { els.status.textContent = view.message; return; }
    els.status.textContent = view.summary + (view.truncated ? " (more not shown)" : "");
    view.notes.forEach(function (t) { els.notes.appendChild(el(doc, "div", "gm-ledger-note is-warn", t)); });
    if (!view.rows.length) {
      els.list.appendChild(el(doc, "div", "gm-empty", view.filter === "unseen" ? "Nothing unseen." : "No deliverables recorded yet."));
      return;
    }
    view.rows.forEach(function (r) {
      const row = el(doc, "div", r.cls);
      const main = el(doc, "div", "gm-row-main");
      const title = el(doc, "div", "gm-row-title");
      title.appendChild(el(doc, "span", r.actionCls, r.action));
      const path = el(doc, "span", "gm-ledger-path");
      path.appendChild(el(doc, "span", "gm-ledger-dir", r.dir));
      path.appendChild(el(doc, "span", "", r.base));
      title.appendChild(path);
      main.appendChild(title);
      main.appendChild(el(doc, "div", "gm-row-sub", r.meta));
      row.appendChild(main);
      if (view.openEnabled && handlers.onOpen) {
        const o = el(doc, "button", "gm-btn gm-ledger-open", "Open");
        o.addEventListener("click", function () { handlers.onOpen(r.id); });
        row.appendChild(o);
      }
      if (!r.seen) {
        const b = el(doc, "button", "gm-btn", "Mark seen");
        b.addEventListener("click", function () { handlers.onSeen(r.id); });
        row.appendChild(b);
      }
      els.list.appendChild(row);
    });
  }

  // ---- browser wiring (never runs under node) -------------------------------------
  function init(win) {
    const doc = win.document;
    if (doc.getElementById("gm-ledger-banner")) return;
    const style = doc.createElement("style");
    style.textContent = CSS;
    doc.head.appendChild(style);

    const banner = el(doc, "div", "gm-ledger-banner");
    banner.id = "gm-ledger-banner";
    const header = doc.querySelector(".gm-header");
    if (header && header.parentNode) header.parentNode.insertBefore(banner, header.nextSibling);
    else doc.body.insertBefore(banner, doc.body.firstChild);

    const tab = el(doc, "button", "gm-tab", "Deliverables");
    tab.setAttribute("data-tab", "deliverables");
    const tabbar = doc.getElementById("tabbar");
    if (tabbar) tabbar.appendChild(tab);

    const panel = el(doc, "section", "gm-panel");
    panel.id = "panel-deliverables";
    const head = el(doc, "div", "gm-section-head");
    head.appendChild(el(doc, "h2", "", "Deliverables"));
    const seg = el(doc, "div", "gm-seg");
    const bUn = el(doc, "button", "gm-seg-btn is-active", "Unseen");
    const bAll = el(doc, "button", "gm-seg-btn", "All");
    seg.appendChild(bUn); seg.appendChild(bAll);
    head.appendChild(seg);
    const authorSel = el(doc, "select", "gm-select gm-ledger-author");
    authorSel.setAttribute("aria-label", "Filter by authoring agent");
    authorSel.title = "Filter by authoring agent";
    head.appendChild(authorSel);
    const bRef = el(doc, "button", "gm-btn", "Refresh");
    const bAllSeen = el(doc, "button", "gm-btn", "Mark all seen");
    head.appendChild(bRef); head.appendChild(bAllSeen);
    const status = el(doc, "span", "gm-status", "");
    head.appendChild(status);
    const notes = el(doc, "div", "");
    const list = el(doc, "div", "gm-list");
    panel.appendChild(head); panel.appendChild(notes); panel.appendChild(list);
    const main = doc.querySelector("main.gm-content");
    if (main) main.appendChild(panel);

    const state = { filter: "unseen", author: loadSavedAuthor(win), unseenIds: [] };
    function fillAuthors(v) {
      authorSel.textContent = "";
      authorOptions({ authors: v.rawAuthors }).forEach(function (o) {
        const opt = el(doc, "option", "", o.label);
        opt.value = o.value;
        authorSel.appendChild(opt);
      });
      authorSel.value = state.author;
    }
    const els = { list: list, status: status, notes: notes };

    function activate() {
      doc.querySelectorAll(".gm-tab").forEach(function (t) { t.classList.toggle("is-active", t === tab); });
      doc.querySelectorAll(".gm-panel").forEach(function (p) { p.classList.toggle("is-active", p === panel); });
      load();
    }
    tab.addEventListener("click", activate);
    banner.addEventListener("click", activate);

    async function get(q, signal) {
      const res = await win.fetch("/api/deliverables?" + q, signal && typeof signal.aborted === "boolean" ? { signal: signal } : undefined);
      if (!res.ok) throw new Error("deliverables request failed: HTTP " + res.status);
      return res.json();
    }
    async function post(body) {
      const res = await win.fetch("/api/deliverables", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) {
        let d = "";
        try { d = String((await res.json()).detail || ""); } catch (e) { /* no body */ }
        throw new Error("HTTP " + res.status + (d ? ": " + d : ""));
      }
      return res.json();
    }
    async function postOpen(body) {
      const res = await win.fetch("/api/deliverables/open", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (!res.ok) {
        let d = "";
        try { d = String((await res.json()).detail || ""); } catch (e) { /* no body */ }
        throw new Error("HTTP " + res.status + (d ? ": " + d : ""));
      }
      return res.json();
    }
    // Never drop a failure silently: surface it in the status line and as a red note.
    function showError(prefix, e) {
      const msg = prefix + ": " + String((e && e.message) || e);
      status.textContent = msg;
      notes.textContent = "";
      notes.appendChild(el(doc, "div", "gm-ledger-note is-warn", msg));
    }
    function setBanner(v) {
      banner.textContent = "";
      if (v && v.bannerText) {
        banner.appendChild(el(doc, "b", "", v.bannerText));
        banner.appendChild(doc.createTextNode(" — click to review"));
        banner.classList.add("is-on");
      } else {
        banner.classList.remove("is-on");
      }
    }
    async function poll(signal) {
      try { setBanner(buildView(await get("filter=unseen&limit=1", signal))); } catch (e) { /* the tab shows errors loudly */ }
    }
    async function load(retried) {
      let v;
      try {
        const resp = await get("filter=" + state.filter + "&limit=" + LIST_LIMIT +
          (state.author ? "&author=" + encodeURIComponent(state.author) : ""));
        v = buildView(resp);
        v.rawAuthors = Array.isArray(resp.authors) ? resp.authors : [];
        // A remembered author that is no longer in the ledger would show an empty list for no visible
        // reason: fall back to All (once) and say nothing is lost.
        if (state.author && !retried && !v.error && !v.disabled &&
            !v.rawAuthors.some(function (a) { return String(a.author) === state.author; })) {
          state.author = ""; saveAuthor(win, "");
          return load(true);
        }
      }
      catch (e) { v = { error: String(e.message || e) }; }
      if (!v.error && !v.disabled) fillAuthors(v);
      state.unseenIds = (v.rows || []).filter(function (r) { return !r.seen; }).map(function (r) { return r.id; });
      render(els, v, doc, { onSeen: async function (id) {
        try { await post({ ids: [id] }); } catch (e) { showError("Mark seen failed", e); return; }
        load(); poll();
      }, onOpen: async function (id) {
        // The server opens it (by id) and marks it seen; a refusal is shown, never dropped.
        try { await postOpen({ id: id }); } catch (e) { showError("Open failed", e); return; }
        load(); poll();
      } });
      if (!v.error) setBanner({ bannerText: v.bannerText || (v.unseen ? plural(v.unseen, "unseen deliverable") : "") });
    }
    bUn.addEventListener("click", function () { state.filter = "unseen"; bUn.classList.add("is-active"); bAll.classList.remove("is-active"); load(); });
    bAll.addEventListener("click", function () { state.filter = "all"; bAll.classList.add("is-active"); bUn.classList.remove("is-active"); load(); });
    authorSel.addEventListener("change", function () {
      state.author = authorSel.value || ""; saveAuthor(win, state.author); load();
    });
    bRef.addEventListener("click", function () { load(); });
    bAllSeen.addEventListener("click", async function () {
      // With an author filter on, "Mark all seen" means the entries SHOWN (never another author's).
      try {
        if (state.author) { if (state.unseenIds.length) await post({ ids: state.unseenIds }); }
        else await post({ all: true });
      } catch (e) { showError("Mark all seen failed", e); return; }
      load(); poll();
    });
    poll();
    win.setInterval(win.GmPoll ? win.GmPoll.wrap("ledger", poll) : poll, POLL_MS);   // poll-pileup fix: in-flight guard
  }

  if (typeof window !== "undefined" && window.document) {
    if (window.document.readyState === "loading") window.document.addEventListener("DOMContentLoaded", function () { init(window); });
    else init(window);
  }

  return { authorOptions: authorOptions, loadSavedAuthor: loadSavedAuthor, saveAuthor: saveAuthor,
           UNKNOWN_AUTHOR: UNKNOWN_AUTHOR, AUTHOR_KEY: AUTHOR_KEY, buildView: buildView, render: render, splitPath: splitPath, init: init, writerLabel: writerLabel, POLL_MS: POLL_MS };
});

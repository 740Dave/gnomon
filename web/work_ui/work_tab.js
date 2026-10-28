/* Gnomon — the Work tab (roadmap), READ-ONLY.
 *
 * Served ONLY while open_work_list is ON (the server injects the script tag into "/" and gates this file behind the same flag;
 * it is deliberately NOT under /static). Without touching index.html or app.js it adds:
 *   - a "Work" tab, laid out like the Apps tab. Top: the operator's declared status (state, text, who told whom, age; "not renewed" past 24 h;
 *     "status unreadable" when unknown) and ONE "Waiting on you" list across all agents (agent, item, age, a link to its DSD or
 *     "no DSD filed"). Left: every agent with its count of open items and badges (waiting on you / paused / held), "no open items",
 *     "verbs not available", or the list's fault; a "Retired" group for lists whose agent is no longer known. Right: the selected agent's
 *     items (title, waiting-on, age, reminder interval own or default / "no reminder", last reminder, reminders in the last 24 h, standing /
 *     paused / held, "says requested by ...", a mark on any interval under 60 min) and that agent's reminder turns in the last 24 h;
 *   - a count of open items beside each agent's name in the places the page already lists agents (Profiles, Live sessions, DSD list).
 * There is NO control that writes anything: no input, no add / edit / done / drop / status button. The only buttons are Refresh and the DSD links
 * (which open the existing DSD tab).
 * Everything an agent wrote (titles, notes, waiting-on text, status text and source) is drawn with textContent: never as HTML.
 * The poll (GmPoll.wrap, 10 s) refreshes the list AND the selected agent's pane; an unchanged response re-renders nothing; a re-render keeps
 * both scroll positions; the time the data was READ is shown ("as of ...", g102).
 *
 * The pure view builders (buildOverview, buildAgent, ageText, intervalText, statusView) have no DOM and are tested under node
 * (tests/test_owl_s2_ui.py). UMD: window.GmWorkTab in the browser, module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmWorkTab = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const POLL_MS = 10000;
  const SHORT_INTERVAL_MIN = 60;
  // Display caps = the write-time bounds of agent-written text. A forged log can carry more; the page never draws it.
  const CAP = { title: 200, note: 500, waiting: 96, status: 200, source: 200, setter: 40, id: 64, reason: 300, error: 500 };
  const DSD_ID = /^dsd-[0-9a-f]{6,32}$/;      // the DSD code's own id shape (gnomon/dsd/notice.py NOTICE_DSD_ID_RE)
  function cap(v, n) {
    if (v == null) return "";
    let s = typeof v === "string" ? v : String(v);
    if (s.length <= n) return s;
    s = s.slice(0, Math.max(0, n - 1));
    const last = s.charCodeAt(s.length - 1);
    if (last >= 0xD800 && last <= 0xDBFF) s = s.slice(0, -1);
    return s + "\u2026";
  }
  function isDsdId(v) { return typeof v === "string" && DSD_ID.test(v); }
  // a blocked-by value: {dsd: id} only for a well-formed DSD id; anything else is plain text and says so
  function blockedView(v) {
    if (v == null || v === "") return null;
    return isDsdId(v) ? { dsd: v, text: v } : { dsd: null, text: cap(v, CAP.id) + " (not a DSD id)" };
  }

  function ageText(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    if (s < 60) return s + " s";
    const m = Math.floor(s / 60);
    if (m < 60) return m + " min";
    const h = Math.floor(m / 60);
    if (h < 48) return h + " h " + (m % 60) + " min";
    return Math.floor(h / 24) + " d " + (h % 24) + " h";
  }

  function clockText(epoch) {
    const d = new Date(Number(epoch) * 1000);
    return isNaN(d.getTime()) ? "unknown" : d.toISOString().slice(11, 19) + "Z";
  }

  function intervalText(it) {
    if (it.no_reminder_text) return it.no_reminder_text;
    const n = it.interval_min;
    if (n == null) return "no interval";
    return "every " + n + " min (" + (it.interval_source === "own" ? "its own" : "the default") + ")";
  }

  // the declared status block (read-only): resp.status is api.operator_status()
  function statusView(st, label) {
    const who = cap(label || "the operator", CAP.setter);
    if (!st || typeof st !== "object") return { cls: "is-warn", text: "status unavailable" };
    if (st.state === "unknown") {
      return { cls: "is-warn", text: "status unreadable: " + who + " is NOT treated as in office; held items stay silent until a new status is declared" };
    }
    if (!st.declared) return { cls: "is-note", text: "no status declared for " + who + " (shown by the agents as only what they see him do)" };
    const age = ageText(st.age_seconds);
    let text = who + ": " + (st.state === "away" ? "away" + (st.text ? ": " + cap(st.text, CAP.status) : "") : "in office") +
      " · declared " + age + " ago" + (st.as_told_to ? ", as told to " + cap(st.as_told_to, CAP.setter) : "") + (st.source ? " · source: " + cap(st.source, CAP.source) : "");
    if (st.not_renewed) text += " · not renewed (" + cap(st.not_renewed_text || "older than 24 h", CAP.status) + ")";
    return { cls: st.not_renewed || st.state === "away" ? "is-away" : "is-note", text: text };
  }

  function countText(a) {
    if (a.state === "verbs not available") return "verbs not available";
    if (a.state === "list not intact") return a.error && /is busy/.test(String(a.error)) ? "list busy, retrying" : "list not readable";
    if (a.state === "no open items" || !a.open) return "no open items";
    return a.open + (a.open === 1 ? " open item" : " open items");
  }

  // resp: GET /api/work -> the whole left side + the top list
  function buildOverview(resp) {
    if (!resp || typeof resp !== "object") return { error: "no Work response (the server returned nothing)" };
    if (resp.ok === false) return { error: cap(resp.error || "the Work list failed", CAP.error) };
    const label = cap(resp.operator_label || "the operator", CAP.setter);
    const agents = Array.isArray(resp.agents) ? resp.agents : [];
    function row(a) {
      const badges = [];
      if (a.waiting_on_operator) badges.push({ text: a.waiting_on_operator + " waiting on " + label, cls: "gm-work-badge is-wait" });
      if (a.paused) badges.push({ text: a.paused + " paused", cls: "gm-work-badge is-paused" });
      if (a.held) badges.push({ text: a.held + " held", cls: "gm-work-badge is-held" });
      const faults = [];
      if (a.state === "list not intact") faults.push("This list could not be read: " + cap(a.error || "it failed verification", CAP.error));
      if (a.state === "verbs not available") faults.push("This agent's session cannot call the work verbs" + (a.verbs_unavailable_reason ? " (" + cap(a.verbs_unavailable_reason, CAP.reason) + ")" : ""));
      if (a.log_warning) faults.push("Warning: " + cap(a.log_warning, CAP.reason));
      return { id: a.instance_id, name: cap(a.instance_id, CAP.id), countText: countText(a), open: a.open || 0, state: a.state, badges: badges, faults: faults,
        retired: !!a.retired, reminders24h: a.reminders_24h || 0 };
    }
    const rows = agents.map(row);
    return {
      readAt: resp.read_at || "", asOf: "as of " + clockText(resp.read_at_epoch),
      status: statusView(resp.status, label),
      waiting: (Array.isArray(resp.waiting_on_you) ? resp.waiting_on_you : []).map(function (w) {
        const b = blockedView(w.blocked_by);
        return { agent: w.agent, agentText: cap(w.agent, CAP.id), id: w.id, title: cap(w.title, CAP.title), ageText: ageText(w.age_seconds),
          dsdId: b ? b.dsd : null, dsdText: b ? b.text : "no DSD filed" };
      }),
      active: rows.filter(function (r) { return !r.retired; }),
      retired: rows.filter(function (r) { return r.retired; }),
      label: label, empty: rows.length === 0,
    };
  }

  // resp: GET /api/work/<id> -> the right-hand pane
  function buildAgent(resp) {
    if (!resp || typeof resp !== "object") return { error: "no response for this agent" };
    if (resp.ok === false) return { error: cap(resp.error || resp.detail || "the request failed", CAP.error) };
    const label = cap(resp.operator_label || "the operator", CAP.setter);
    const v = { id: resp.instance_id, title: cap(resp.instance_id, CAP.id) + (resp.retired ? " (retired)" : ""), asOf: "as of " + clockText(resp.read_at_epoch), notes: [], items: [],
      totals: "", state: resp.state };
    if (resp.state === "verbs not available") { v.notes.push({ cls: "gm-work-warn", text: "This agent's session cannot call the work verbs" + (resp.verbs_unavailable_reason ? ": " + cap(resp.verbs_unavailable_reason, CAP.reason) : "") + ". It is NOT the same as having no open items." }); return v; }
    if (resp.state === "list not intact") { v.notes.push({ cls: "gm-work-warn", text: "This agent's list could not be read: " + cap(resp.error || "it failed verification", CAP.error) }); return v; }
    if (resp.log_warning) v.notes.push({ cls: "gm-work-warn", text: "Warning: " + cap(resp.log_warning, CAP.reason) });
    v.totals = (resp.open || 0) + " open · " + (resp.reminders_24h || 0) + " reminder turn" + (resp.reminders_24h === 1 ? "" : "s") + " in the last 24 h";
    if (!resp.open) { v.notes.push({ cls: "gm-empty", text: "no open items" }); return v; }
    v.items = (resp.items || []).map(function (it) {
      const tags = [];
      if (it.standing) tags.push({ text: "standing", cls: "gm-work-badge is-standing" });
      if (it.paused_text) tags.push({ text: cap(it.paused_text, CAP.reason), cls: "gm-work-badge is-paused" });
      if (it.held_text) tags.push({ text: cap(it.held_text, CAP.reason), cls: "gm-work-badge is-held" });
      if (it.refused_text) tags.push({ text: cap(it.refused_text, CAP.reason), cls: "gm-work-badge is-paused" });
      if (it.hold_stored_text) tags.push({ text: cap(it.hold_stored_text, CAP.reason), cls: "gm-work-badge is-note" });
      const rows = [
        { key: "Waiting on", value: cap(it.waiting_text || it.waiting_on, CAP.waiting) },
        { key: "Age", value: ageText(it.age_seconds) },
        { key: "Reminder", value: intervalText(it) + (it.short_interval_mark ? "  ⚠ under " + SHORT_INTERVAL_MIN + " min" : ""), mark: !!it.short_interval_mark },
        { key: "Last reminder", value: it.last_reminder_age_seconds == null ? "never" : ageText(it.last_reminder_age_seconds) + " ago" },
        { key: "Reminders, last 24 h", value: String(it.reminders_24h || 0) },
      ];
      if (it.says_requested_by) rows.push({ key: "Requested by", value: "says requested by " + cap(it.says_requested_by, CAP.id) });
      const bv = blockedView(it.blocked_by);
      if (bv) rows.push({ key: "Blocked by DSD", value: bv.text, dsd: bv.dsd });
      else if (it.waiting_on === "operator") rows.push({ key: "Blocked by DSD", value: "no DSD filed" });
      if (it.note) rows.push({ key: "Note", value: cap(it.note, CAP.note) });
      return { id: cap(it.id, CAP.id), title: cap(it.title, CAP.title), tags: tags, rows: rows, short: !!it.short_interval_mark };
    });
    return v;
  }

  // the instance id written in a list row's text, or null
  function idFromText(t) {
    const m = /([A-Za-z0-9][A-Za-z0-9._-]{0,63})\s*\)?\s*(?:\[[a-z]+\])?\s*$/.exec(String(t || "").trim());
    return m ? m[1] : null;
  }

  const CSS = [
    ".gm-work-status{padding:var(--gm-pad-sm) var(--gm-pad-md);border-radius:var(--gm-radius-sm);border:1px solid var(--gm-border);",
    "font-size:var(--gm-fs-sm);margin:var(--gm-pad-sm) 0;color:var(--gm-text-secondary)}",
    ".gm-work-status.is-away{color:var(--gm-accent-hover);border-color:var(--gm-accent)}",
    ".gm-work-status.is-warn{color:var(--gm-danger);border-color:var(--gm-danger)}",
    ".gm-work-section{margin:var(--gm-pad-md) 0 var(--gm-pad-xs);color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);",
    "text-transform:uppercase;letter-spacing:.04em}",
    ".gm-work-badge{display:inline-block;padding:1px 8px;margin-left:4px;border-radius:var(--gm-radius-sm);font-size:var(--gm-fs-sm);",
    "border:1px solid var(--gm-border);color:var(--gm-text-secondary);white-space:nowrap}",
    ".gm-work-badge.is-wait{color:var(--gm-accent-hover);border-color:var(--gm-accent)}",
    ".gm-work-badge.is-paused,.gm-work-badge.is-held{color:var(--gm-secondary);border-color:var(--gm-secondary)}",
    ".gm-work-kv{display:grid;grid-template-columns:minmax(150px,max-content) 1fr;gap:4px var(--gm-pad-md);font-size:var(--gm-fs-sm)}",
    ".gm-work-kv dt{color:var(--gm-text-secondary)}.gm-work-kv dd{margin:0;color:var(--gm-text-primary);word-break:break-word}",
    ".gm-work-kv dd.is-mark{color:var(--gm-danger)}",
    ".gm-work-item{border:1px solid var(--gm-border);border-radius:var(--gm-radius-sm);padding:var(--gm-pad-sm);margin:var(--gm-pad-sm) 0}",
    ".gm-work-item.is-short{border-color:var(--gm-danger)}",
    ".gm-work-warn{color:var(--gm-danger);font-size:var(--gm-fs-sm);margin:4px 0}",
    ".gm-work-note{color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);margin:4px 0}",
    ".gm-work-link{color:var(--gm-accent-hover);cursor:pointer;text-decoration:underline;background:none;border:0;padding:0;font:inherit}",
    "#panel-work .gm-row-title,#panel-work .gm-row-sub,#panel-work .gm-work-note,#panel-work .gm-work-warn{overflow-wrap:anywhere}",
    ".gm-work-count{margin-left:6px;color:var(--gm-text-secondary);font-size:var(--gm-fs-sm)}",
  ].join("\n");

  function el(doc, tag, cls, text) {
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  // ---- browser wiring -----------------------------------------------------------------------
  function init(win) {
    const doc = win.document;
    if (doc.getElementById("panel-work")) return;
    const style = doc.createElement("style");
    style.textContent = CSS;
    doc.head.appendChild(style);

    const tab = el(doc, "button", "gm-tab", "Work");
    tab.setAttribute("data-tab", "work");
    const tabbar = doc.getElementById("tabbar");
    if (tabbar) tabbar.appendChild(tab);

    const panel = el(doc, "section", "gm-panel");
    panel.id = "panel-work";
    const head = el(doc, "div", "gm-section-head");
    head.appendChild(el(doc, "h2", "", "Work"));
    const bRef = el(doc, "button", "gm-btn", "Refresh");
    head.appendChild(bRef);
    const asOf = el(doc, "span", "gm-status", "");
    asOf.id = "work-as-of";
    head.appendChild(asOf);
    panel.appendChild(head);
    const statusBox = el(doc, "div", "gm-work-status", "");
    statusBox.id = "work-status";
    panel.appendChild(statusBox);
    const top = el(doc, "div", "");
    top.id = "work-waiting";
    panel.appendChild(top);
    const gv = el(doc, "div", "gm-gv");
    const left = el(doc, "div", "gm-gv-left");
    const right = el(doc, "div", "gm-gv-right");
    const list = el(doc, "div", "gm-list gm-gv-list");
    list.id = "work-agents";
    left.appendChild(list);
    right.id = "work-detail";
    right.appendChild(el(doc, "div", "gm-empty", "Select an agent on the left."));
    gv.appendChild(left); gv.appendChild(right);
    panel.appendChild(gv);
    const main = doc.querySelector("main.gm-content");
    if (main) main.appendChild(panel);

    const state = { selected: null, overviewSig: null, detailSig: null, detailSeq: 0, last: null, detailNote: null };
    // what a pane DISPLAYS, minus the read time: the signature that decides whether anything must be redrawn (ages are already minute-text)
    function viewSig(v) { const o = {}; Object.keys(v).forEach(function (k) { if (k !== "asOf" && k !== "readAt") o[k] = v[k]; }); return JSON.stringify(o); }

    function activate() {
      doc.querySelectorAll(".gm-tab").forEach(function (t) { t.classList.toggle("is-active", t === tab); });
      doc.querySelectorAll(".gm-panel").forEach(function (p) { p.classList.toggle("is-active", p === panel); });
      loadOverview();
      if (state.selected) pollDetail();
    }
    tab.addEventListener("click", activate);

    async function get(url, signal) {
      const opt = { method: "GET", headers: {} };
      if (signal && typeof signal.aborted === "boolean") opt.signal = signal;
      const res = await win.fetch(url, opt);
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      if (!res.ok) throw new Error(data && (data.error || data.detail) ? String(data.error || data.detail) : "HTTP " + res.status);
      return data;
    }

    // Open a DSD through what the DSD tab already has: its list row (data-dsd-id), else its id-lookup box (a separate flag), else say so.
    function openDsd(id) {
      const dsdTab = doc.querySelector('.gm-tab[data-tab="dsd"]');
      if (dsdTab) dsdTab.click();
      let tries = 0;
      (function attempt() {
        if (!isDsdId(id)) { asOf.textContent = "not a DSD id"; return; }
        // exact id equality over the rows' own data: no selector, URL or HTML is ever built from an item's text
        const rows = doc.querySelectorAll(".gm-dsd-item");
        let item = null;
        for (let i = 0; i < rows.length; i++) { if (rows[i].getAttribute("data-dsd-id") === id) { item = rows[i]; break; } }
        if (item) { item.click(); return; }
        if (tries++ < 10 && typeof win.setTimeout === "function") { win.setTimeout(attempt, 300); return; }
        const input = doc.getElementById("dsd-id-lookup-input");
        const btn = doc.getElementById("dsd-id-lookup-open");
        if (input && btn) { input.value = id; btn.click(); return; }
        asOf.textContent = "could not open " + id + ": it is not in the DSD list";
      })();
    }
    function dsdLink(id) {
      if (!isDsdId(id)) return doc.createTextNode(cap(id, CAP.id) + " (not a DSD id)");
      const b = el(doc, "button", "gm-work-link", id);
      b.type = "button";
      b.setAttribute("data-dsd", id);
      b.addEventListener("click", function () { openDsd(id); });
      return b;
    }

    function renderTop(v) {
      statusBox.className = "gm-work-status " + v.status.cls;
      statusBox.textContent = v.status.text;
      asOf.textContent = v.asOf;
      top.textContent = "";
      top.appendChild(el(doc, "div", "gm-work-section", "Waiting on you (" + v.waiting.length + ")"));
      if (!v.waiting.length) { top.appendChild(el(doc, "div", "gm-work-note", "Nothing is waiting on " + v.label + ".")); return; }
      v.waiting.forEach(function (w) {
        const r = el(doc, "div", "gm-row");
        r.setAttribute("data-waiting", w.agent + ":" + w.id);
        const m = el(doc, "div", "gm-row-main");
        const t = el(doc, "div", "gm-row-title");
        t.appendChild(el(doc, "span", "gm-id", w.agentText));
        t.appendChild(doc.createTextNode("  " + w.title));
        m.appendChild(t);
        const sub = el(doc, "div", "gm-row-sub", "open " + w.ageText + " · ");
        if (w.dsdId) sub.appendChild(dsdLink(w.dsdId)); else sub.appendChild(doc.createTextNode(w.dsdText));
        m.appendChild(sub);
        r.appendChild(m);
        r.addEventListener("click", function (ev) { if (ev.target && ev.target.getAttribute && ev.target.getAttribute("data-dsd")) return; select(w.agent); });
        top.appendChild(r);
      });
    }

    function renderList(v) {
      list.textContent = "";
      if (v.empty) { list.appendChild(el(doc, "div", "gm-empty", "No agents are known yet.")); return; }
      function addRows(rows) {
        rows.forEach(function (r) {
          const row = el(doc, "div", "gm-row gm-gv-item" + (r.id === state.selected ? " is-selected" : ""));
          row.setAttribute("data-agent", r.id);
          const m = el(doc, "div", "gm-row-main");
          const t = el(doc, "div", "gm-row-title");
          t.appendChild(el(doc, "span", "", r.name + " "));
          r.badges.forEach(function (b) { t.appendChild(el(doc, "span", b.cls, b.text)); });
          m.appendChild(t);
          m.appendChild(el(doc, "div", "gm-row-sub", r.countText));
          r.faults.forEach(function (f) { m.appendChild(el(doc, "div", "gm-work-warn", f)); });
          row.appendChild(m);
          row.addEventListener("click", function () { select(r.id); });
          list.appendChild(row);
        });
      }
      list.appendChild(el(doc, "div", "gm-work-section", "Agents"));
      addRows(v.active);
      if (v.retired.length) { list.appendChild(el(doc, "div", "gm-work-section", "Retired (" + v.retired.length + ")")); addRows(v.retired); }
    }

    function renderAgent(v) {
      right.textContent = "";
      if (v.error) { right.appendChild(el(doc, "div", "gm-empty", v.error)); return; }
      right.appendChild(el(doc, "h2", "", v.title));
      state.detailBase = v.asOf + (v.totals ? " · " + v.totals : "");
      state.detailNote = el(doc, "div", "gm-work-note", state.detailBase);
      right.appendChild(state.detailNote);
      v.notes.forEach(function (n) { right.appendChild(el(doc, "div", n.cls, n.text)); });
      v.items.forEach(function (it) {
        const box = el(doc, "div", "gm-work-item" + (it.short ? " is-short" : ""));
        box.setAttribute("data-item", it.id);
        const t = el(doc, "div", "gm-row-title", it.id + "  " + it.title);
        it.tags.forEach(function (g) { t.appendChild(el(doc, "span", g.cls, g.text)); });
        box.appendChild(t);
        const dl = el(doc, "dl", "gm-work-kv");
        it.rows.forEach(function (r) {
          dl.appendChild(el(doc, "dt", "", r.key));
          const dd = el(doc, "dd", r.mark ? "is-mark" : "");
          if (r.dsd) dd.appendChild(dsdLink(r.dsd)); else dd.textContent = r.value;
          dl.appendChild(dd);
        });
        box.appendChild(dl);
        right.appendChild(box);
      });
    }

    // a re-render keeps the scroll position of the pane it replaces (the Apps tab wipes it; this does not)
    function keepScroll(node, fn) {
      const top = node.scrollTop;
      fn();
      try { node.scrollTop = top; } catch (e) { /* no layout under a stub */ }
    }

    function select(id) { state.selected = id; state.detailSig = null; loadDetail(id); if (state.last) keepScroll(list, function () { renderList(state.last); }); }

    async function loadOverview(signal) {
      let sig = null;
      let v;
      try { const resp = await get("/api/work", signal); v = buildOverview(resp); sig = v.error ? null : viewSig(v); state.counts = v.error ? null : countsOf(resp); decorateAll(); }
      catch (e) { if (signal && signal.aborted) return; v = { error: cap(e.message || e, CAP.error) }; }
      if (v.error) {
        // a failed read never blanks what was last read well (g102): it stays, marked with its own read time. The signature is cleared so the
        // NEXT good read always redraws (no click needed). Counts beside names are removed: nothing, rather than an old number.
        state.overviewSig = null; state.counts = null; decorateAll();
        if (state.last) { asOf.textContent = state.last.asOf + " · last refresh failed: " + v.error; return; }
        list.textContent = ""; list.appendChild(el(doc, "div", "gm-empty", v.error));
        statusBox.className = "gm-work-status is-warn"; statusBox.textContent = "The Work list could not be read: " + v.error; return;
      }
      asOf.textContent = v.asOf;                          // the read time moves with EVERY successful read, even an unchanged one
      if (sig === state.overviewSig) return;              // nothing changed: leave both panes alone
      state.overviewSig = sig;
      state.last = v;
      keepScroll(list, function () { renderList(v); });
      renderTop(v);
    }

    async function loadDetail(id) {
      const seq = ++state.detailSeq;
      let v, sig = null;
      try { const resp = await get("/api/work/" + encodeURIComponent(id)); v = buildAgent(resp); sig = viewSig(v); }
      catch (e) { v = { error: cap(e.message || e, CAP.error) }; }
      if (seq !== state.detailSeq) return;
      state.detailSig = sig;
      keepScroll(right, function () { renderAgent(v); });
    }

    // the poll refreshes the SELECTED agent too; an unchanged response re-renders nothing
    async function pollDetail(signal) {
      const id = state.selected;
      if (!id) return;
      const seq = state.detailSeq;
      try {
        const resp = await get("/api/work/" + encodeURIComponent(id), signal);
        if (seq !== state.detailSeq || id !== state.selected) return;
        const v = buildAgent(resp);
        const sig = viewSig(v);
        if (sig === state.detailSig) {                          // nothing displayed changed: leave the pane alone, move only the read time
          if (state.detailNote && !v.error) { state.detailBase = v.asOf + (v.totals ? " · " + v.totals : ""); state.detailNote.textContent = state.detailBase; }
          return;
        }
        state.detailSig = sig;
        keepScroll(right, function () { renderAgent(v); });
      } catch (e) {
        if (signal && signal.aborted) return;
        // the pane keeps its last good render, marked; the signature is cleared so the next good read redraws it without a click
        state.detailSig = null;
        if (state.detailNote && state.detailBase) state.detailNote.textContent = state.detailBase + " · last refresh failed: " + cap(e.message || e, CAP.error);
      }
    }

    // ---- the counts beside agent names where the page already lists agents ----------------------
    function countsOf(resp) {
      const m = {};
      (resp.agents || []).forEach(function (a) { m[a.instance_id] = a; });
      return m;
    }
    function countLabel(a) {
      if (!a) return "no open items";
      if (a.state === "verbs not available") return "verbs not available";
      if (a.state === "list not intact") return "list not readable";
      return a.open ? a.open + " open" : "no open items";
    }
    function decorate(host, pick) {
      if (!host) return;
      if (!state.counts) {                                  // no good read: show nothing rather than an old number
        host.querySelectorAll("[data-work-count]").forEach(function (n) { if (n.parentNode) n.parentNode.removeChild(n); });
        return;
      }
      host.querySelectorAll(pick.row).forEach(function (row) {
        const have = row.querySelector("[data-work-count]");
        const id = pick.idOf(row);
        if (have) {                                         // refresh the number in place (only when it changed: no mutation loop)
          const txt = countLabel(state.counts[have.getAttribute("data-work-count")]);
          if (have.textContent !== txt) have.textContent = txt;
          return;
        }
        if (!id) return;
        const s = el(doc, "span", "gm-work-count", countLabel(state.counts[id]));
        s.setAttribute("data-work-count", id);
        (pick.anchor(row) || row).appendChild(s);
      });
    }
    const PLACES = [
      { host: "#profiles-list", row: ".gm-row", idOf: function (r) { const n = r.querySelector(".gm-row-title .gm-id"); return n ? idFromText(n.textContent) : null; },
        anchor: function (r) { return r.querySelector(".gm-row-title"); } },
      { host: "#sessions-list", row: ".gm-row", idOf: function (r) { const t = r.querySelector(".gm-row-title"); return t && t.firstChild ? idFromText(t.firstChild.textContent) : null; },
        anchor: function (r) { return r.querySelector(".gm-row-title"); } },
      { host: "#dsd-list", row: ".gm-dsd-item", idOf: function (r) { const k = r.querySelector(".gm-dsd-item-kind"); return k ? idFromText(k.textContent) : null; },
        anchor: function (r) { return r.querySelector(".gm-dsd-item-kind"); } },
    ];
    let deco = false;
    function decorateAll() {
      if (deco) return;
      deco = true;
      try { PLACES.forEach(function (p) { decorate(doc.querySelector(p.host), p); }); } finally { deco = false; }
    }
    if (typeof win.MutationObserver === "function") {
      const obs = new win.MutationObserver(function () { decorateAll(); });
      PLACES.forEach(function (p) { const h = doc.querySelector(p.host); if (h) obs.observe(h, { childList: true, subtree: true }); });
    }

    async function poll(signal) {
      if (doc.hidden) return;                                // a hidden browser tab does not poll
      try { await loadOverview(signal); } catch (e) { /* shown when opened */ }
      if (panel.classList.contains("is-active")) await pollDetail(signal);
    }

    bRef.addEventListener("click", function () { state.overviewSig = null; loadOverview(); if (state.selected) { state.detailSig = null; loadDetail(state.selected); } });
    poll();
    win.setInterval(win.GmPoll ? win.GmPoll.wrap("work", poll) : poll, POLL_MS);   // poll-pileup fix: in-flight guard
  }

  if (typeof window !== "undefined" && window.document) {
    if (window.document.readyState === "loading") window.document.addEventListener("DOMContentLoaded", function () { init(window); });
    else init(window);
  }

  return { buildOverview: buildOverview, buildAgent: buildAgent, statusView: statusView, ageText: ageText, intervalText: intervalText,
    countText: countText, idFromText: idFromText, init: init, POLL_MS: POLL_MS };
});

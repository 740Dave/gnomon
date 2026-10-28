/* Gnomon — the Scheduler and Run Log tabs (stage 1a).
 *
 * Served ONLY while scheduler is ON (the server injects the script tag into "/" and gates this file behind the
 * same flag; it is deliberately NOT under /static). Without touching index.html or app.js it adds two tabs:
 *   - "Scheduler": a Pending section at the TOP (proposals awaiting the operator's confirm, each with Confirm and Decline),
 *     then the confirmed jobs (title, owner, next run in plain words, last result, enabled), an explicit empty state,
 *     and a detail view (schedule, next 5 runs, descriptor, steps, full cleaned text, pin state, latest runs) with
 * Pause / Enable and Cancel. NO form creates or edits a job: agents propose (requirement-view-only-authoring);
 *   - "Run Log": every run across all jobs, newest first, filtered by job and result, each opening to its receipt.
 *   - a banner of flagged runs (failure, miss, skip, refusal). In stage 1a the "flag" is the Run Log entry plus this
 * banner (the alert service is not built).
 * "OK (exit 0, unverified)" style results get their own colour; NEVER-RUN, refused, FAIL, MISSED and SKIPPED render in
 * the failure class so a job that never fired cannot look healthy.
 *
 * Pure view builders (buildList, buildDetail, buildRunLog, resultChip, newArrivals) have no DOM and are tested under
 * node. All DOM is built with textContent: no HTML strings. UMD: window.GmSchedulerTab / module.exports.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmSchedulerTab = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const POLL_MS = 10000;
  const FAILURE = { "FAIL": 1, "MISSED": 1, "SKIPPED": 1, "NEVER-RUN": 1, "refused": 1 };

  function resultChip(result, shown) {
    const r = String(result || "unknown");
    let cls = "gm-sch-chip is-neutral";
    if (r === "OK") cls = "gm-sch-chip is-ok";
    else if (r === "OK-UNVERIFIED") cls = "gm-sch-chip is-unverified";
    else if (FAILURE[r]) cls = "gm-sch-chip is-failure";
    else if (r === "WAITING" || r === "waiting") cls = "gm-sch-chip is-waiting";
    return { text: String(shown || r), cls: cls, failure: !!FAILURE[r] };
  }

  function whenText(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  function buildList(resp) {
    if (!resp || typeof resp !== "object") return { error: "no Scheduler response (the server returned nothing)" };
    if (resp.ok === false) return { error: String(resp.error || "the Scheduler list failed") };
    const jobs = Array.isArray(resp.jobs) ? resp.jobs : [];
    const pending = Array.isArray(resp.pending) ? resp.pending : [];
    const banner = Array.isArray(resp.banner) ? resp.banner : [];
    return {
      rows: jobs.map(function (j) {
        const chip = resultChip(j.last_result, j.last_shown);
        return { id: j.id, title: j.title || j.id, chip: chip, failure: chip.failure || !!j.failure_class,
          sub: (j.owner || "?") + " · " + (j.schedule || "") + " · next: " + (j.next_run || "none") +
            (j.enabled === false ? " · paused" : "") };
      }),
      pending: pending.map(function (p) {
        return { id: p.id, title: (p.kind === "removal" ? "Remove: " : p.kind === "change" ? "Change: " : "New: ") + (p.title || p.id),
          sub: "proposed by " + (p.proposed_by || "unknown") + (p.proposed_by_verified ? "" : " (unverified)") + " · " +
            whenText(p.proposed_at) + " · " + (p.schedule || "") };
      }),
      empty: jobs.length === 0,
      emptyText: "No jobs are scheduled yet. An agent proposes one and it appears under Pending for your confirmation.",
      pendingCount: pending.length,
      banner: banner.map(function (b) {
        return { text: (b.job_id || "?") + ": " + (b.result || "") + (b.reason ? " (" + b.reason + ")" : "") + " · " + whenText(b.ts) };
      }),
    };
  }

  function newArrivals(prevIds, pending) {
    const prev = new Set(prevIds || []);
    return (pending || []).filter(function (p) { return !prev.has(p.id); }).map(function (p) { return p.id; });
  }

  // The detail view has TWO independent panels: Pending (the PROPOSED job, from resp.proposal) and Job (the CONFIRMED
  // job, from resp.job). A change proposal carries both; the Pending panel must show the proposal's own rows and cleaned
  // text, with the old values only in the "What changed" lines (fix round 3, P1-1). `rows` / `text` are kept as the
  // Job panel's (or, with no job, the Pending panel's) for callers that read the single-panel shape.
  function buildDetail(resp) {
    if (!resp || typeof resp !== "object") return { error: "no detail response" };
    if (resp.ok === false) return { error: String(resp.error || resp.detail || "the detail request failed") };
    const pr = resp.proposal || null, j = resp.job || null;
    const v = { id: resp.id, proposal: pr, job: j, runs: resp.runs || [] };
    v.title = (j && j.title) || (pr && pr.title) || resp.id;
    v.pendingTitle = pr ? (pr.title || resp.id) : "";
    v.pendingRows = pr ? [
      { key: "Proposed by", value: (pr.proposed_by || "unknown") + (pr.proposed_by_verified ? "" : " (unverified)") },
      { key: "Kind", value: pr.kind },
      { key: "Owner", value: pr.owner || "" },
      { key: "Schedule", value: pr.schedule },
      { key: "Target session", value: pr.target || "" },
    ] : [];
    v.pendingText = pr ? (pr.text || "") : "";
    v.changes = ((pr && pr.changes) || []).map(function (c) {
      return { field: c.field, old: c.old, new: c.new, line: c.field + ": " + c.old + " -> " + c.new };
    });
    v.removes = !!(pr && pr.removes);
    v.jobRows = [];
    v.jobText = "";
    if (j) {
      v.jobRows = [
        { key: "Owner", value: j.owner }, { key: "Action", value: j.action && j.action.type },
        { key: "Target session", value: j.action && j.action.target_instance_id },
        { key: "Schedule", value: j.schedule },
        { key: "Next runs (UTC)", value: j.done ? "done" : (j.next_runs && j.next_runs.length ? j.next_runs.join(", ") : "not scheduled") },
        { key: "Confirmed", value: whenText(j.confirmed_at) },
        { key: "Pin", value: j.pin && j.pin.ok ? "matches what you confirmed" : "DOES NOT MATCH: refused (" + (j.pin && j.pin.failed) + ")" },
        { key: "Enabled", value: j.enabled ? "yes" : "paused" },
      ];
      v.jobText = (j.action && j.action.text) || "";
      v.descriptor = j.descriptor; v.steps = j.steps || []; v.pinOk = !!(j.pin && j.pin.ok); v.enabled = !!j.enabled; v.done = !!j.done;
    }
    v.rows = j ? v.jobRows : v.pendingRows;
    v.text = j ? v.jobText : v.pendingText;
    v.digest = pr && pr.digest;
    v.pinSha = j && j.pin && j.pin.sha256;
    v.hasPending = !!pr;
    v.isJob = !!j;
    v.runs = (resp.runs || []).map(function (r) { return buildRunRow(r); });
    return v;
  }

  // The body a button posts. confirm / decline echo the digest of the Pending row THIS page rendered (the server
  // refuses a different one: the confirm is bound to what the operator saw); cancel echoes the pinned definition sha.
  function actionBody(v, op) {
    if ((op === "confirm" || op === "decline") && v && v.digest) return { seen: v.digest };
    if (op === "cancel" && v && v.pinSha) return { seen: v.pinSha };
    return {};
  }

  function buildRunRow(r) {
    const chip = resultChip(r.result, (r.outcome ? r.outcome + " · " : "") + (r.result || ""));
    return { run_id: r.run_id, job_id: r.job_id, chip: chip, failure: chip.failure,
      title: (r.job_id || "?") + " · " + (r.trigger || "") + " · slot " + (r.slot_utc || ""),
      sub: whenText(r.ts) + (r.reason ? " · " + r.reason : "") + (r.flagged ? " · FLAGGED" : "") };
  }

  function buildRunLog(resp) {
    if (!resp || typeof resp !== "object") return { error: "no Run Log response" };
    if (resp.ok === false) return { error: String(resp.error || "the Run Log failed") };
    const chains = resp.chains || {};
    const broken = Object.keys(chains).filter(function (k) { return chains[k] && chains[k].ok === false; });
    return {
      rows: (resp.runs || []).map(buildRunRow),
      empty: !(resp.runs || []).length,
      emptyText: "No runs yet. A run is recorded when a scheduled reminder fires, waits, is skipped, missed or refused.",
      warnings: broken.map(function (k) { return "Tamper check FAILED for the run record of " + k + ": line " + chains[k].breaks.map(function (b) { return b.line; }).join(", "); })
        .concat(resp.audit_chain && resp.audit_chain.ok === false ? ["Tamper check FAILED for the scheduler audit log: line " + resp.audit_chain.breaks.map(function (b) { return b.line; }).join(", ")] : []),
      filterResults: (resp.filters && resp.filters.results) || [],
    };
  }

  const CSS = [
    ".gm-sch-banner{display:none;padding:var(--gm-pad-sm) var(--gm-pad-lg);background:var(--gm-elevated-bg);",
    "border-bottom:1px solid var(--gm-border);color:var(--gm-text-primary);font-size:var(--gm-fs-sm);cursor:pointer}",
    ".gm-sch-banner.is-on{display:block}.gm-sch-banner b{color:var(--gm-accent-hover)}",
    ".gm-sch-chip{display:inline-block;padding:1px 8px;border-radius:var(--gm-radius-sm);font-size:var(--gm-fs-sm);",
    "border:1px solid var(--gm-border);color:var(--gm-text-secondary);white-space:nowrap}",
    ".gm-sch-chip.is-ok{color:var(--gm-live);border-color:var(--gm-live)}",
    ".gm-sch-chip.is-unverified{color:var(--gm-secondary);border-color:var(--gm-secondary);border-style:dashed}",
    ".gm-sch-chip.is-failure{color:var(--gm-danger);border-color:var(--gm-danger)}",
    ".gm-sch-chip.is-waiting{color:var(--gm-accent-hover);border-color:var(--gm-accent)}",
    ".gm-sch-section{margin:var(--gm-pad-md) 0 var(--gm-pad-xs);color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);",
    "text-transform:uppercase;letter-spacing:.04em}",
    ".gm-sch-kv{display:grid;grid-template-columns:minmax(140px,max-content) 1fr;gap:4px var(--gm-pad-md);font-size:var(--gm-fs-sm)}",
    ".gm-sch-kv dt{color:var(--gm-text-secondary)}.gm-sch-kv dd{margin:0;color:var(--gm-text-primary);word-break:break-word}",
    ".gm-sch-raw{white-space:pre-wrap;font-family:var(--gm-font);font-size:var(--gm-fs-sm);color:var(--gm-text-primary);",
    "background:var(--gm-elevated-bg);border-radius:var(--gm-radius-sm);padding:var(--gm-pad-sm);max-height:320px;overflow:auto}",
    ".gm-sch-controls{display:flex;gap:var(--gm-pad-sm);align-items:center;flex-wrap:wrap;margin:var(--gm-pad-sm) 0}",
    ".gm-sch-warn{color:var(--gm-danger);font-size:var(--gm-fs-sm);margin:4px 0}",
    ".gm-sch-note{color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);margin:4px 0}",
    ".gm-sch-filter{display:flex;gap:var(--gm-pad-sm);margin:var(--gm-pad-sm) 0}",
    ".gm-sch-filter input,.gm-sch-filter select{font-family:var(--gm-font);font-size:var(--gm-fs-sm);background:var(--gm-elevated-bg);",
    "color:var(--gm-text-primary);border:1px solid var(--gm-border);border-radius:var(--gm-radius-sm);padding:4px 8px}",
  ].join("\n");

  function el(doc, tag, cls, text) {
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  }

  function init(win) {
    const doc = win.document;
    if (doc.getElementById("gm-sch-banner")) return;
    const style = doc.createElement("style");
    style.textContent = CSS;
    doc.head.appendChild(style);
    const banner = el(doc, "div", "gm-sch-banner"); banner.id = "gm-sch-banner";
    const header = doc.querySelector(".gm-header");
    if (header && header.parentNode) header.parentNode.insertBefore(banner, header.nextSibling);
    else doc.body.insertBefore(banner, doc.body.firstChild);
    const tabbar = doc.getElementById("tabbar");
    const main = doc.querySelector("main.gm-content");
    const state = { selected: null, seenPending: null, csrf: null, runFilterJob: "", runFilterResult: "" };

    function mkTab(label, key) {
      const tab = el(doc, "button", "gm-tab", label); tab.setAttribute("data-tab", key);
      if (tabbar) tabbar.appendChild(tab);
      const panel = el(doc, "section", "gm-panel"); panel.id = "panel-" + key;
      if (main) main.appendChild(panel);
      return { tab: tab, panel: panel };
    }
    const sch = mkTab("Scheduler", "scheduler"), log = mkTab("Run Log", "runlog");
    function activate(which) {
      doc.querySelectorAll(".gm-tab").forEach(function (t) { t.classList.toggle("is-active", t === which.tab); });
      doc.querySelectorAll(".gm-panel").forEach(function (p) { p.classList.toggle("is-active", p === which.panel); });
      banner.classList.remove("is-on");
    }

    // --- Scheduler panel
    const gv = el(doc, "div", "gm-gv"), left = el(doc, "div", "gm-gv-left"), right = el(doc, "div", "gm-gv-right");
    const head = el(doc, "div", "gm-section-head"); head.appendChild(el(doc, "h2", "", "Scheduler"));
    const bRef = el(doc, "button", "gm-btn", "Refresh"); head.appendChild(bRef);
    const status = el(doc, "span", "gm-status", ""); head.appendChild(status);
    left.appendChild(head);
    const list = el(doc, "div", "gm-list gm-gv-list"); left.appendChild(list);
    right.appendChild(el(doc, "div", "gm-empty", "Select a job on the left."));
    gv.appendChild(left); gv.appendChild(right); sch.panel.appendChild(gv);

    // --- Run Log panel
    const lhead = el(doc, "div", "gm-section-head"); lhead.appendChild(el(doc, "h2", "", "Run Log"));
    const lref = el(doc, "button", "gm-btn", "Refresh"); lhead.appendChild(lref);
    const filt = el(doc, "div", "gm-sch-filter");
    const fJob = el(doc, "input", ""); fJob.type = "text"; fJob.placeholder = "filter by job id";
    const fRes = el(doc, "select", ""); fRes.appendChild(el(doc, "option", "", "any result")).value = "";
    ["OK", "OK-UNVERIFIED", "FAIL", "MISSED", "SKIPPED", "WAITING"].forEach(function (r) { const o = el(doc, "option", "", r); o.value = r; fRes.appendChild(o); });
    filt.appendChild(fJob); filt.appendChild(fRes);
    const logList = el(doc, "div", "gm-list"), receipt = el(doc, "div", "gm-sch-raw");
    log.panel.appendChild(lhead); log.panel.appendChild(filt); log.panel.appendChild(logList); log.panel.appendChild(receipt);

    async function req(method, url, body) {
      const opt = { method: method, headers: {} };
      if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
      if (method !== "GET") {
        if (!state.csrf) { try { const l = await win.fetch("/api/scheduler"); state.csrf = (await l.json()).csrf; } catch (e) { /* refused below */ } }
        opt.headers["X-Gnomon-CSRF"] = state.csrf || "";
      }
      const res = await win.fetch(url, opt);
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      if (data && typeof data.csrf === "string") state.csrf = data.csrf;
      if (!res.ok) { const err = new Error(data && (data.error || data.detail) ? String(data.error || data.detail) : "HTTP " + res.status); err.data = data; throw err; }
      return data;
    }
    function toast(m) { status.textContent = m; }
    function kv(rows) { const dl = el(doc, "dl", "gm-sch-kv"); rows.forEach(function (p) { dl.appendChild(el(doc, "dt", "", p.key)); dl.appendChild(el(doc, "dd", "", p.value == null ? "" : String(p.value))); }); return dl; }

    function renderList(v) {
      list.textContent = "";
      if (v.error) { list.appendChild(el(doc, "div", "gm-empty", v.error)); return; }
      if (v.banner.length) { list.appendChild(el(doc, "div", "gm-sch-section", "Flagged (" + v.banner.length + ", last 24 h)")); v.banner.forEach(function (b) { list.appendChild(el(doc, "div", "gm-sch-warn", b.text)); }); }
      list.appendChild(el(doc, "div", "gm-sch-section", "Pending (" + v.pendingCount + ")"));
      if (!v.pending.length) list.appendChild(el(doc, "div", "gm-sch-note", "No proposals are waiting."));
      v.pending.forEach(function (p) {
        const row = el(doc, "div", "gm-row gm-gv-item"), m = el(doc, "div", "gm-row-main");
        m.appendChild(el(doc, "div", "gm-row-title", p.title)); m.appendChild(el(doc, "div", "gm-row-sub", p.sub));
        row.appendChild(m); row.addEventListener("click", function () { state.selected = p.id; loadDetail(p.id); }); list.appendChild(row);
      });
      list.appendChild(el(doc, "div", "gm-sch-section", "Jobs"));
      if (v.empty) list.appendChild(el(doc, "div", "gm-empty", v.emptyText));
      v.rows.forEach(function (r) {
        const row = el(doc, "div", "gm-row gm-gv-item"), m = el(doc, "div", "gm-row-main"), t = el(doc, "div", "gm-row-title");
        t.appendChild(el(doc, "span", "", r.title + " ")); t.appendChild(el(doc, "span", r.chip.cls, r.chip.text));
        m.appendChild(t); m.appendChild(el(doc, "div", "gm-row-sub", r.sub)); row.appendChild(m);
        row.addEventListener("click", function () { state.selected = r.id; loadDetail(r.id); }); list.appendChild(row);
      });
    }

    function renderDetail(v) {
      right.textContent = "";
      if (v.error) { right.appendChild(el(doc, "div", "gm-empty", v.error)); return; }
      right.appendChild(el(doc, "h2", "", v.title));
      if (v.hasPending) {
        right.appendChild(el(doc, "div", "gm-sch-note", "Nothing is scheduled and nothing fires until you confirm. Confirming asks for your Mac password / Touch ID, and never fires the job."));
        right.appendChild(el(doc, "div", "gm-sch-section", "Proposed" + (v.pendingTitle ? ": " + v.pendingTitle : "")));
        right.appendChild(kv(v.pendingRows));
        if (v.removes) right.appendChild(el(doc, "div", "gm-sch-warn", "Confirming REMOVES this job."));
        if (v.changes.length) {
          right.appendChild(el(doc, "div", "gm-sch-section", "What changed (old -> new)"));
          v.changes.forEach(function (c) { right.appendChild(el(doc, "div", "gm-sch-note", c.line)); });
        }
        right.appendChild(el(doc, "div", "gm-sch-section", "The note that would be typed (cleaned)"));
        right.appendChild(el(doc, "div", "gm-sch-raw", v.pendingText));
        const ctl = el(doc, "div", "gm-sch-controls");
        const bC = el(doc, "button", "gm-btn gm-btn-primary", "Confirm"), bD = el(doc, "button", "gm-btn", "Decline");
        bC.addEventListener("click", function () { act(v.id, "confirm", actionBody(v, "confirm")); }); bD.addEventListener("click", function () { act(v.id, "decline", actionBody(v, "decline")); });
        ctl.appendChild(bC); ctl.appendChild(bD); right.appendChild(ctl);
      }
      if (v.isJob) {
        if (v.hasPending) right.appendChild(el(doc, "div", "gm-sch-section", "Currently confirmed (unchanged until you confirm the proposal above)"));
        if (!v.pinOk) right.appendChild(el(doc, "div", "gm-sch-warn", "This job is REFUSED: its definition no longer matches what you confirmed. It will not fire until you confirm a new proposal."));
        right.appendChild(kv(v.jobRows));
        right.appendChild(el(doc, "div", "gm-sch-section", "What it does"));
        right.appendChild(el(doc, "div", "gm-sch-note", v.descriptor || ""));
        (v.steps || []).forEach(function (s, i) { right.appendChild(el(doc, "div", "gm-sch-note", (i + 1) + ". " + s)); });
        right.appendChild(el(doc, "div", "gm-sch-section", "The note (cleaned text; the delivered line starts with the scheduler label)"));
        right.appendChild(el(doc, "div", "gm-sch-raw", v.jobText));
        const ctl = el(doc, "div", "gm-sch-controls");
        const bP = el(doc, "button", "gm-btn", v.enabled ? "Pause" : "Enable"), bX = el(doc, "button", "gm-btn", "Cancel job");
        bP.addEventListener("click", function () { act(v.id, v.enabled ? "pause" : "enable"); });
        bX.addEventListener("click", function () { if (win.confirm("Cancel the job " + v.id + "? Its run history stays in the Run Log.")) act(v.id, "cancel", actionBody(v, "cancel")); });
        ctl.appendChild(bP); ctl.appendChild(bX); right.appendChild(ctl);
        right.appendChild(el(doc, "div", "gm-sch-section", "Latest runs"));
        if (!v.runs.length) right.appendChild(el(doc, "div", "gm-sch-note", "No run yet."));
        v.runs.forEach(function (r) { const d = el(doc, "div", "gm-sch-note", ""); d.appendChild(el(doc, "span", r.chip.cls, r.chip.text)); d.appendChild(doc.createTextNode(" " + r.sub)); right.appendChild(d); });
      }
    }

    function renderRunLog(v) {
      logList.textContent = ""; receipt.textContent = "";
      if (v.error) { logList.appendChild(el(doc, "div", "gm-empty", v.error)); return; }
      v.warnings.forEach(function (w) { logList.appendChild(el(doc, "div", "gm-sch-warn", w)); });
      if (v.empty) logList.appendChild(el(doc, "div", "gm-empty", v.emptyText));
      v.rows.forEach(function (r) {
        const row = el(doc, "div", "gm-row gm-gv-item"), m = el(doc, "div", "gm-row-main"), t = el(doc, "div", "gm-row-title");
        t.appendChild(el(doc, "span", "", r.title + " ")); t.appendChild(el(doc, "span", r.chip.cls, r.chip.text));
        m.appendChild(t); m.appendChild(el(doc, "div", "gm-row-sub", r.sub)); row.appendChild(m);
        row.addEventListener("click", async function () {
          try { const d = await req("GET", "/api/scheduler/runs/" + encodeURIComponent(r.job_id) + "/" + encodeURIComponent(r.run_id)); receipt.textContent = JSON.stringify(d.receipt, null, 2); }
          catch (e) { receipt.textContent = "receipt unavailable: " + String(e.message || e); }
        });
        logList.appendChild(row);
      });
    }

    async function act(id, op, body) {
      try {
        if (op === "confirm") toast("A Mac password / Touch ID prompt is waiting for you (2 minutes)...");
        const r = await req("POST", "/api/scheduler/" + encodeURIComponent(id) + "/" + op, body || {});
        toast(op + ": " + (r && (r.status || (r.ok ? "done" : ""))));
      } catch (e) {
        toast(op + " refused: " + String(e.message || e));
        // a stale confirm: show the CURRENT version of the proposal for a fresh look
        if (e.data && e.data.stale && e.data.proposal) { loadList(); loadDetail(id); return; }
      }
      loadList(); if (state.selected) loadDetail(state.selected);
    }
    async function loadDetail(id) { let v; try { v = buildDetail(await req("GET", "/api/scheduler/" + encodeURIComponent(id))); } catch (e) { v = { error: String(e.message || e) }; } renderDetail(v); }
    async function loadList() { let v; try { v = buildList(await req("GET", "/api/scheduler")); } catch (e) { v = { error: String(e.message || e) }; } renderList(v); noticeFrom(v); }
    async function loadLog() {
      let v; const q = []; if (fJob.value) q.push("job=" + encodeURIComponent(fJob.value)); if (fRes.value) q.push("result=" + encodeURIComponent(fRes.value));
      try { v = buildRunLog(await req("GET", "/api/scheduler/runlog" + (q.length ? "?" + q.join("&") : ""))); } catch (e) { v = { error: String(e.message || e) }; }
      renderRunLog(v);
    }
    function noticeFrom(v) {
      if (!v || v.error) return;
      const ids = v.pending.map(function (p) { return p.id; });
      const fresh = state.seenPending === null ? ids : newArrivals(state.seenPending, v.pending);
      if (fresh.length && !sch.panel.classList.contains("is-active")) {
        banner.textContent = ""; banner.appendChild(el(doc, "b", "", fresh.length + " new scheduler proposal" + (fresh.length === 1 ? "" : "s")));
        banner.appendChild(doc.createTextNode(" waiting for your confirm — click to review")); banner.classList.add("is-on");
      }
      state.seenPending = ids;
    }
    async function poll(signal) { try { const v = buildList(await req("GET", "/api/scheduler")); noticeFrom(v); if (sch.panel.classList.contains("is-active")) renderList(v); } catch (e) { /* shown loudly when opened */ } }

    sch.tab.addEventListener("click", function () { activate(sch); loadList(); });
    log.tab.addEventListener("click", function () { activate(log); loadLog(); });
    banner.addEventListener("click", function () { activate(sch); loadList(); });
    bRef.addEventListener("click", function () { loadList(); if (state.selected) loadDetail(state.selected); });
    lref.addEventListener("click", loadLog); fJob.addEventListener("change", loadLog); fRes.addEventListener("change", loadLog);
    poll();
    win.setInterval(win.GmPoll ? win.GmPoll.wrap("scheduler", poll) : poll, POLL_MS);
  }

  if (typeof window !== "undefined" && window.document) {
    if (window.document.readyState === "loading") window.document.addEventListener("DOMContentLoaded", function () { init(window); });
    else init(window);
  }

  return { actionBody: actionBody, buildList: buildList, buildDetail: buildDetail, buildRunLog: buildRunLog, buildRunRow: buildRunRow,
    resultChip: resultChip, newArrivals: newArrivals, init: init, POLL_MS: POLL_MS };
});

/* Gnomon — the Apps tab (roadmap).
 *
 * Served ONLY while apps_tab is ON (the server injects the script tag into "/" and gates this file behind
 * the same flag; it is deliberately NOT under /static). Without touching index.html or app.js it adds:
 *   - an "Apps" tab: a master/detail page. Left: a "Pending" section at the TOP (proposals awaiting the operator's
 *     confirm), then the installed apps (name, status chip, scope), with an explicit empty state;
 *   - the detail page: the manifest rendered (fields + the raw JSON), the host settings (run at boot, scope) and
 *     the user_editable settings as form fields, the path values with their access marks, Start / Stop / Open
 *     (Open only with ui_path, enabled only while running), the live status, the last start and stop with WHO,
 *     the actions (Run asks for a confirm), the backups to record, and Propose uninstall;
 *   - a proposal's detail page: the manifest, the paths to fill, Confirm and Decline. The approval screen never
 * shows the port;
 *   - a notice banner when a new proposal arrives.
 *
 * The pure view builders (buildList, buildDetail, statusChip, fieldFor, newArrivals, collectValues) have no DOM
 * and are tested under node (tests/test_appshost_ui.py). All DOM is built with textContent: no HTML strings.
 * Design tokens come from styles.css (macOS dark, system sans, no monospaced fonts).
 *
 * UMD: window.GmAppsTab in the browser, module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmAppsTab = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const POLL_MS = 10000;
  const STATUS_CLASS = {
    stopped: "is-stopped", starting: "is-starting", running: "is-running", "port-conflict": "is-conflict",
    failed: "is-failed", refused: "is-refused", pending: "is-pending",
  };

  function statusChip(status, label) {
    const s = String(status || "unknown");
    return { text: String(label || s), cls: "gm-apps-chip " + (STATUS_CLASS[s] || "is-unknown") };
  }

  function whenText(iso) {
    if (!iso) return "never";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  function actorText(a) {
    if (!a) return "never";
    return whenText(a.ts) + " by " + (a.actor || "unknown") + (a.actor_verified ? "" : " (unverified)") +
      (a.outcome && a.outcome !== "ok" ? " · " + a.outcome : "");
  }

  // resp: GET /api/apps -> the master list view.
  function buildList(resp) {
    if (!resp || typeof resp !== "object") return { error: "no Apps response (the server returned nothing)" };
    if (resp.ok === false) return { error: String(resp.error || "the Apps list failed") };
    const apps = Array.isArray(resp.apps) ? resp.apps : [];
    const pending = Array.isArray(resp.pending) ? resp.pending : [];
    return {
      rows: apps.map(function (a) {
        return { id: a.id, name: a.name || a.id, chip: statusChip(a.status, a.label), scope: a.scope || "all",
          sub: "scope: " + (a.scope || "all") + (a.headless ? " · headless" : "") };
      }),
      pending: pending.map(function (p) {
        const kind = p.kind === "uninstall" ? "uninstall" : "install";
        return { id: p.id, name: p.name || p.id, kind: kind,
          title: (kind === "uninstall" ? "Uninstall: " : "Install: ") + (p.name || p.id),
          sub: "proposed by " + (p.proposed_by || "unknown") + (p.proposed_by_verified ? "" : " (unverified)") +
            " · " + whenText(p.proposed_at) + (p.valid === false ? " · INVALID: " + (p.invalid_reason || "") : ""),
          valid: p.valid !== false };
      }),
      empty: apps.length === 0,
      emptyText: "No apps are installed yet. An app is installed when you confirm a proposal in the Pending section.",
      pendingCount: pending.length,
      note: resp.workspace_note || "",
    };
  }

  // the ids in `pending` that were not in `prevIds` (the notice)
  function newArrivals(prevIds, pending) {
    const prev = new Set(prevIds || []);
    return (pending || []).filter(function (p) { return !prev.has(p.id + ":" + p.kind); })
      .map(function (p) { return p.id + ":" + p.kind; });
  }

  // one user_editable setting -> the form control to draw
  function fieldFor(s) {
    const t = s.type;
    if (t === "yes/no") return { control: "checkbox", value: !!s.value };
    if (t === "number") return { control: "number", value: s.value };
    if (t === "choice") return { control: "select", value: s.value, options: (s.options || []).slice() };
    if (t === "path") return { control: "text", value: s.value == null ? "" : String(s.value), placeholder: "absolute " + (s.kind || "file") + " path" };
    return { control: "text", value: s.value == null ? "" : String(s.value) };
  }

  function manifestRows(m) {
    if (!m) return [];
    const rows = [
      ["Name", m.name], ["Version", m.version], ["What it does", m.purpose], ["Owner", m.owner], ["Id", m.id],
      ["Scope (manifest ceiling)", m.scope], ["Starts at boot by default", m.run_at_boot_default ? "yes" : "no"],
      ["Start command", (m.start || []).join(" ")],
    ];
    if (m.port_arg) rows.push(["Port argument", m.port_arg.join(" ")]);
    if (m.health_path) rows.push(["Health URL path", m.health_path]);
    if (m.activity_path) rows.push(["Activity URL path", m.activity_path]);
    rows.push(["UI", m.ui_path ? m.ui_path : "headless (no UI)"]);
    if ((m.requires || []).length) rows.push(["Requires", m.requires.map(function (r) { return r.id + " (" + r.mode + ")"; }).join(", ")]);
    if ((m.requires_files || []).length) rows.push(["Files that must exist", m.requires_files.join(", ")]);
    if ((m.backup_before_first_start || []).length) rows.push(["Backup before first start", m.backup_before_first_start.join(", ")]);
    if ((m.licences || []).length) rows.push(["Licences", m.licences.map(function (l) { return l.name; }).join(", ")]);
    if (m.hardware) rows.push(["Hardware", m.hardware.device_class]);
    if (m.stop) rows.push(["Stop", m.stop.signal + ", then SIGKILL after " + m.stop.grace + " s"]);
    if (m.ui_token) rows.push(["UI token", m.ui_token]);
    const env = m.env || {};
    Object.keys(env).forEach(function (k) { rows.push(["Env " + k, env[k]]); });
    return rows.map(function (r) { return { key: r[0], value: r[1] == null ? "" : String(r[1]) }; });
  }

  // resp: GET /api/apps/<id> -> the detail view.
  function buildDetail(resp) {
    if (!resp || typeof resp !== "object") return { error: "no detail response" };
    if (resp.ok === false) return { error: String(resp.error || resp.detail || "the detail request failed") };
    const m = resp.manifest || null;
    const v = { consentKey: consentKeyOf(resp),  seen: resp.seen || null, id: resp.app_id, manifest: m, manifestRows: manifestRows(m), manifestRaw: resp.manifest_raw || "",
      manifestError: resp.manifest_error || null, title: m ? m.name + " " + m.version : resp.app_id };
    if (!resp.installed) {
      v.isProposal = true;
      v.proposal = resp.proposal || {};
      v.valid = resp.valid !== false;
      v.invalidReason = resp.invalid_reason || "";
      v.toFill = (resp.to_fill || []).map(function (p) {
        return { name: p.name, label: p.name + " (" + p.kind + ", " + (p.access === "read_only" ? "read only" : "writes") +
          (p.optional ? ", optional " + p.flag : "") + ")", description: p.description, access: p.access,
          optional: !!p.optional };
      });
      v.userFields = m ? (m.user_editable || []).map(function (s) {
        return { name: s.name, type: s.type, description: s.description, field: fieldFor({ type: s.type, value: s.default, options: s.options, kind: s.kind }) };
      }) : [];
      v.runAtBoot = m ? !!m.run_at_boot_default : false;
      v.scope = m ? m.scope : "all";
      v.showPort = false;   // the approval screen never shows the port
      v.canConfirm = v.valid && !!m;
      return v;
    }
    const st = resp.status || {};
    v.isProposal = false;
    v.chip = statusChip(st.status, st.label);
    v.status = st.status;
    v.statusDetail = st.detail || "";
    v.warnings = st.warnings || [];
    v.logTail = Array.isArray(st.log_tail) ? st.log_tail : [];
    v.holder = st.holder || null;
    v.headless = m ? !!m.headless : true;
    v.showOpen = !v.headless;                                  // Open only when the manifest has ui_path
    v.openEnabled = v.showOpen && st.status === "running";     // never a window onto a dead port
    v.canStart = st.status !== "running" && st.status !== "starting";
    v.canStop = st.status === "running" || st.status === "starting";
    v.port = resp.port;
    // the full address comes from the server (the same builder the Open action uses), never rebuilt here
    v.uiUrl = (v.showOpen && typeof resp.ui_url === "string" && resp.ui_url) ? resp.ui_url : null;
    v.uiTokenNote = !!(v.uiUrl && m && m.ui_token);
    v.uiLinkLive = !!v.uiUrl && st.status === "running";      // clickable only while the app is up
    v.lastStart = actorText(resp.last_start);
    v.lastStop = actorText(resp.last_stop);
    const s = resp.settings || {};
    v.runAtBoot = !!s.run_at_boot;
    v.scope = s.scope || "all";
    v.scopeCeiling = s.scope_ceiling || "all";
    v.paths = (s.paths || []).map(function (p) {
      return { name: p.name, value: p.value || "", access: p.access,
        accessText: p.access === "read_only" ? "read only" : "writes", kind: p.kind, optional: !!p.optional,
        flag: p.flag, description: p.description };
    });
    v.userFields = (s.user || []).map(function (u) {
      return { name: u.name, type: u.type, description: u.description, field: fieldFor(u) };
    });
    v.actions = m ? (m.actions || []).map(function (a) {
      return { name: a.name, text: a.argv.join(" "), requiresStopped: !!a.requires_stopped,
        disabled: !!a.requires_stopped && v.canStop };
    }) : [];
    v.licences = resp.licences || [];
    const backups = resp.backups || {};
    v.backupsNeeded = m ? (m.backup_before_first_start || []).map(function (n) {
      const r = backups[n];
      return { name: n, recorded: !!r, text: r ? (r.note || ("recorded " + whenText(r.recorded_at) + " at " + (r.backup_path || ""))) : "no backup recorded" };
    }) : [];
    v.change = resp.change || null;
    v.needsReconfirm = st.reason === "manifest-changed" || st.reason === "code-changed";
    v.pendingUninstall = !!resp.pending_uninstall;
    return v;
  }

  // What an operator approves by clicking Confirm / Re-confirm: everything in the response except the live-status
  // keys. A poll tick never swaps this content under a reading operator (g102 / round-3 F4).
  function consentKeyOf(resp) {
    const o = {};
    Object.keys(resp).forEach(function (k) {
      if (["status", "port", "ui_url", "last_start", "last_stop", "pending_uninstall", "uninstall_proposal"].indexOf(k) < 0) o[k] = resp[k];
    });
    return JSON.stringify(o);
  }

  // the form state the page posts (pure: tests feed it plain objects)
  function collectValues(state) {
    const out = { run_at_boot: !!state.runAtBoot, scope: String(state.scope || "all"), paths: {}, user: {} };
    Object.keys(state.paths || {}).forEach(function (k) { out.paths[k] = String(state.paths[k] || ""); });
    Object.keys(state.user || {}).forEach(function (k) {
      const f = state.user[k];
      if (f.type === "number") {
        const n = Number(f.value);
        out.user[k] = (f.value === "" || isNaN(n)) ? f.value : n;
      } else if (f.type === "yes/no") out.user[k] = !!f.value;
      else out.user[k] = f.value;
    });
    return out;
  }

  const CSS = [
    ".gm-apps-banner{display:none;padding:var(--gm-pad-sm) var(--gm-pad-lg);background:var(--gm-elevated-bg);",
    "border-bottom:1px solid var(--gm-border);color:var(--gm-text-primary);font-size:var(--gm-fs-sm);cursor:pointer}",
    ".gm-apps-banner.is-on{display:block}",
    ".gm-apps-banner b{color:var(--gm-accent-hover)}",
    ".gm-apps-chip{display:inline-block;padding:1px 8px;border-radius:var(--gm-radius-sm);font-size:var(--gm-fs-sm);",
    "border:1px solid var(--gm-border);color:var(--gm-text-secondary);white-space:nowrap}",
    ".gm-apps-chip.is-running{color:var(--gm-live);border-color:var(--gm-live)}",
    ".gm-apps-chip.is-starting{color:var(--gm-accent-hover);border-color:var(--gm-accent)}",
    ".gm-apps-chip.is-failed,.gm-apps-chip.is-refused,.gm-apps-chip.is-conflict{color:var(--gm-danger);border-color:var(--gm-danger)}",
    ".gm-apps-chip.is-pending{color:var(--gm-secondary);border-color:var(--gm-secondary)}",
    ".gm-apps-section{margin:var(--gm-pad-md) 0 var(--gm-pad-xs);color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);",
    "text-transform:uppercase;letter-spacing:.04em}",
    ".gm-apps-kv{display:grid;grid-template-columns:minmax(140px,max-content) 1fr;gap:4px var(--gm-pad-md);font-size:var(--gm-fs-sm)}",
    ".gm-apps-kv dt{color:var(--gm-text-secondary)}.gm-apps-kv dd{margin:0;color:var(--gm-text-primary);word-break:break-word}",
    ".gm-apps-raw{white-space:pre-wrap;font-family:var(--gm-font);font-size:var(--gm-fs-sm);color:var(--gm-text-secondary);",
    "background:var(--gm-elevated-bg);border-radius:var(--gm-radius-sm);padding:var(--gm-pad-sm);max-height:320px;overflow:auto}",
    ".gm-apps-controls{display:flex;gap:var(--gm-pad-sm);align-items:center;flex-wrap:wrap;margin:var(--gm-pad-sm) 0}",
    ".gm-apps-field{display:flex;gap:var(--gm-pad-sm);align-items:center;margin:4px 0;font-size:var(--gm-fs-sm)}",
    ".gm-apps-field label{min-width:180px;color:var(--gm-text-secondary)}",
    ".gm-apps-field input[type=text],.gm-apps-field input[type=number],.gm-apps-field select{flex:1;font-family:var(--gm-font);",
    "font-size:var(--gm-fs-sm);background:var(--gm-elevated-bg);color:var(--gm-text-primary);border:1px solid var(--gm-border);",
    "border-radius:var(--gm-radius-sm);padding:4px 8px}",
    ".gm-apps-access{font-size:var(--gm-fs-sm);padding:0 6px;border-radius:var(--gm-radius-sm);border:1px solid var(--gm-border)}",
    ".gm-apps-access.is-read_only{color:var(--gm-secondary)}.gm-apps-access.is-writes{color:var(--gm-accent-hover);border-color:var(--gm-accent)}",
    ".gm-apps-warn{color:var(--gm-danger);font-size:var(--gm-fs-sm);margin:4px 0}",
    ".gm-apps-note{color:var(--gm-text-secondary);font-size:var(--gm-fs-sm);margin:4px 0}",
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
    if (doc.getElementById("gm-apps-banner")) return;
    const style = doc.createElement("style");
    style.textContent = CSS;
    doc.head.appendChild(style);

    const banner = el(doc, "div", "gm-apps-banner");
    banner.id = "gm-apps-banner";
    const header = doc.querySelector(".gm-header");
    if (header && header.parentNode) header.parentNode.insertBefore(banner, header.nextSibling);
    else doc.body.insertBefore(banner, doc.body.firstChild);

    const tab = el(doc, "button", "gm-tab", "Apps");
    tab.setAttribute("data-tab", "apps");
    const tabbar = doc.getElementById("tabbar");
    if (tabbar) tabbar.appendChild(tab);

    const panel = el(doc, "section", "gm-panel");
    panel.id = "panel-apps";
    const gv = el(doc, "div", "gm-gv");
    const left = el(doc, "div", "gm-gv-left");
    const right = el(doc, "div", "gm-gv-right");
    const head = el(doc, "div", "gm-section-head");
    head.appendChild(el(doc, "h2", "", "Apps"));
    const bRef = el(doc, "button", "gm-btn", "Refresh");
    head.appendChild(bRef);
    const status = el(doc, "span", "gm-status", "");
    head.appendChild(status);
    left.appendChild(head);
    const propose = el(doc, "div", "gm-apps-field");
    const pIn = el(doc, "input", "");
    pIn.type = "text";
    pIn.placeholder = "Propose an install: absolute path of the app's manifest";
    const pBtn = el(doc, "button", "gm-btn", "Propose");
    propose.appendChild(pIn); propose.appendChild(pBtn);
    left.appendChild(propose);
    const list = el(doc, "div", "gm-list gm-gv-list");
    left.appendChild(list);
    right.appendChild(el(doc, "div", "gm-empty", "Select an app on the left."));
    gv.appendChild(left); gv.appendChild(right);
    panel.appendChild(gv);
    const main = doc.querySelector("main.gm-content");
    if (main) main.appendChild(panel);

    const state = { selected: null, seenPending: null, csrf: null, detailSeq: 0, detailSig: null, baseline: null, shownConsent: false, shownKey: null, stale: false, notRefreshed: false, consentBtns: [], live: null, asOfEl: null, staleEl: null, asOfText: "" };

    function activate() {
      doc.querySelectorAll(".gm-tab").forEach(function (t) { t.classList.toggle("is-active", t === tab); });
      doc.querySelectorAll(".gm-panel").forEach(function (p) { p.classList.toggle("is-active", p === panel); });
      banner.classList.remove("is-on");
      loadList();
      if (state.selected) pollDetail();                    // F6: no stale reading on re-entry; same gate as a tick (never wipes unsaved input, never swaps a consent pane)
    }
    tab.addEventListener("click", activate);
    banner.addEventListener("click", activate);

    async function req(method, url, body, signal) {
      const opt = { method: method, headers: {} };
      if (signal && typeof signal.aborted === "boolean") opt.signal = signal;   // pollers only (GmPoll.wrap)
      if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
      if (method !== "GET") {
        if (!state.csrf) { try { const l = await win.fetch("/api/apps"); state.csrf = (await l.json()).csrf; } catch (e) { /* refused below */ } }
        opt.headers["X-Gnomon-CSRF"] = state.csrf || "";
      }
      const res = await win.fetch(url, opt);
      let data = null;
      try { data = await res.json(); } catch (e) { data = null; }
      if (data && typeof data.csrf === "string") state.csrf = data.csrf;
      if (!res.ok) {
        const msg = data && (data.error || data.detail) ? String(data.error || data.detail) : "HTTP " + res.status;
        const err = new Error(msg); err.data = data; throw err;
      }
      return data;
    }
    function toast(msg) { status.textContent = msg; }

    function renderList(v) {
      list.textContent = "";
      if (v.error) { list.appendChild(el(doc, "div", "gm-empty", v.error)); return; }
      // the Pending section is ALWAYS at the top
      list.appendChild(el(doc, "div", "gm-apps-section", "Pending (" + v.pendingCount + ")"));
      if (!v.pending.length) list.appendChild(el(doc, "div", "gm-apps-note", "No proposals are waiting."));
      v.pending.forEach(function (p) {
        const row = el(doc, "div", "gm-row gm-gv-item");
        const mainEl = el(doc, "div", "gm-row-main");
        const t = el(doc, "div", "gm-row-title");
        const chip = statusChip("pending", p.kind === "uninstall" ? "pending uninstall" : "pending install");
        t.appendChild(el(doc, "span", chip.cls, chip.text));
        t.appendChild(doc.createTextNode(" " + p.title));
        mainEl.appendChild(t);
        mainEl.appendChild(el(doc, "div", "gm-row-sub", p.sub));
        row.appendChild(mainEl);
        row.addEventListener("click", function () { state.selected = p.id; loadDetail(p.id); });
        list.appendChild(row);
      });
      list.appendChild(el(doc, "div", "gm-apps-section", "Installed"));
      if (v.empty) { list.appendChild(el(doc, "div", "gm-empty", v.emptyText)); }
      v.rows.forEach(function (r) {
        const row = el(doc, "div", "gm-row gm-gv-item");
        const mainEl = el(doc, "div", "gm-row-main");
        const t = el(doc, "div", "gm-row-title");
        t.appendChild(el(doc, "span", "", r.name + " "));
        t.appendChild(el(doc, "span", r.chip.cls, r.chip.text));
        mainEl.appendChild(t);
        mainEl.appendChild(el(doc, "div", "gm-row-sub", r.sub));
        row.appendChild(mainEl);
        row.addEventListener("click", function () { state.selected = r.id; loadDetail(r.id); });
        list.appendChild(row);
      });
      if (v.note) list.appendChild(el(doc, "div", "gm-apps-note", v.note));
    }

    function kv(pairs) {
      const dl = el(doc, "dl", "gm-apps-kv");
      pairs.forEach(function (p) { dl.appendChild(el(doc, "dt", "", p.key)); dl.appendChild(el(doc, "dd", "", p.value)); });
      return dl;
    }

    function inputFor(f) {
      const fd = f.field;
      let input;
      if (fd.control === "checkbox") { input = el(doc, "input", ""); input.type = "checkbox"; input.checked = !!fd.value; }
      else if (fd.control === "select") {
        input = el(doc, "select", "");
        fd.options.forEach(function (o) { const op = el(doc, "option", "", o); op.value = o; if (o === fd.value) op.selected = true; input.appendChild(op); });
      } else { input = el(doc, "input", ""); input.type = fd.control === "number" ? "number" : "text"; input.value = fd.value == null ? "" : String(fd.value); if (fd.placeholder) input.placeholder = fd.placeholder; }
      input.setAttribute("data-setting", f.name);
      input.setAttribute("data-type", f.type);
      return input;
    }

    function readForm(box) {
      const st = { runAtBoot: false, scope: "all", paths: {}, user: {} };
      const rab = box.querySelector("[data-host=run_at_boot]");
      if (rab) st.runAtBoot = !!rab.checked;
      const sc = box.querySelector("[data-host=scope]");
      if (sc) st.scope = sc.value;
      box.querySelectorAll("[data-path]").forEach(function (i) { st.paths[i.getAttribute("data-path")] = i.value; });
      box.querySelectorAll("[data-setting]").forEach(function (i) {
        const t = i.getAttribute("data-type");
        st.user[i.getAttribute("data-setting")] = { type: t, value: t === "yes/no" ? i.checked : i.value };
      });
      return collectValues(st);
    }

    function hostFields(box, v) {
      const f1 = el(doc, "div", "gm-apps-field");
      f1.appendChild(el(doc, "label", "", "Start at boot"));
      const rab = el(doc, "input", ""); rab.type = "checkbox"; rab.checked = !!v.runAtBoot; rab.setAttribute("data-host", "run_at_boot");
      f1.appendChild(rab); box.appendChild(f1);
      const f2 = el(doc, "div", "gm-apps-field");
      f2.appendChild(el(doc, "label", "", "Scope (all, or one workspace)"));
      const sc = el(doc, "input", ""); sc.type = "text"; sc.value = v.scope; sc.setAttribute("data-host", "scope");
      f2.appendChild(sc); box.appendChild(f2);
      if (v.scopeCeiling && v.scopeCeiling !== "all") box.appendChild(el(doc, "div", "gm-apps-note", "The manifest limits this app to workspace " + v.scopeCeiling + "; it cannot be moved or widened."));
    }

    function renderDetail(v) {
      right.textContent = "";
      state.consentBtns = []; state.live = null; state.stale = false; state.notRefreshed = false; state.asOfEl = null; state.staleEl = null;
      renderBody(v);
      state.shownConsent = !v.error && !!(v.isProposal || v.needsReconfirm);
      state.shownKey = v.consentKey || null;
      state.baseline = formSnapshot();            // EVERY render path (proposal, error, installed) sets the baseline
      if (!v.error && right.firstChild) {
        state.asOfEl = el(doc, "div", "gm-apps-note", "");
        right.insertBefore(state.asOfEl, right.firstChild);
        state.asOfText = "as of " + new Date().toISOString().slice(11, 19) + "Z";
        showAsOf();
      }
    }
    function showAsOf() {
      if (state.asOfEl) state.asOfEl.textContent = state.asOfText + (state.notRefreshed ? " · not refreshed: unsaved input" : "");
    }
    function markStale() {
      if (state.stale) return;
      state.stale = true;
      state.consentBtns.forEach(function (b) { b.disabled = true; });
      state.staleEl = el(doc, "div", "gm-apps-warn", "This changed since you opened it. Click Refresh to see the current version");
      if (state.asOfEl && state.asOfEl.parentNode) right.insertBefore(state.staleEl, state.asOfEl.nextSibling);
      else right.insertBefore(state.staleEl, right.firstChild);
    }
    function applyLive(v) {       // status chip and Start/Stop/Open only; never the consent content
      const L = state.live;
      if (!L || v.error || v.isProposal) return;
      L.chip.className = v.chip.cls; L.chip.textContent = v.chip.text;
      L.bS.disabled = !v.canStart; L.bX.disabled = !v.canStop;
      if (L.bO) L.bO.disabled = !v.openEnabled;
    }

    function renderBody(v) {
      if (v.error) { right.appendChild(el(doc, "div", "gm-empty", v.error)); return; }
      right.appendChild(el(doc, "h2", "", v.title));
      if (v.isProposal) {
        right.appendChild(el(doc, "div", "gm-apps-note", "Proposed by " + (v.proposal.proposed_by || "unknown") + (v.proposal.proposed_by_verified ? "" : " (unverified)") + ". Nothing is installed until you confirm."));
        if (!v.valid) right.appendChild(el(doc, "div", "gm-apps-warn", "This proposal is invalid and cannot be confirmed: " + v.invalidReason));
        right.appendChild(el(doc, "div", "gm-apps-section", "What it is"));
        right.appendChild(kv(v.manifestRows));
        const form = el(doc, "div", "");
        right.appendChild(el(doc, "div", "gm-apps-section", "Settings"));
        hostFields(form, v);
        if (v.toFill.length) form.appendChild(el(doc, "div", "gm-apps-section", "Paths to fill"));
        v.toFill.forEach(function (p) {
          const f = el(doc, "div", "gm-apps-field");
          f.appendChild(el(doc, "label", "", p.label));
          const i = el(doc, "input", ""); i.type = "text"; i.placeholder = p.description; i.setAttribute("data-path", p.name);
          f.appendChild(i);
          form.appendChild(f);
        });
        v.userFields.forEach(function (u) {
          const f = el(doc, "div", "gm-apps-field");
          f.appendChild(el(doc, "label", "", u.name + (u.description ? " — " + u.description : "")));
          f.appendChild(inputFor(u)); form.appendChild(f);
        });
        right.appendChild(form);
        const ctl = el(doc, "div", "gm-apps-controls");
        const bC = el(doc, "button", "gm-btn gm-btn-primary", "Confirm"); bC.disabled = !v.canConfirm; state.consentBtns.push(bC);
        const bD = el(doc, "button", "gm-btn", "Decline");
        bC.addEventListener("click", function () { if (state.stale) return; act(v.id, "confirm", { values: readForm(form), seen: v.seen }); });
        bD.addEventListener("click", function () { act(v.id, "decline", {}); });
        ctl.appendChild(bC); ctl.appendChild(bD); right.appendChild(ctl);
        right.appendChild(el(doc, "div", "gm-apps-section", "Manifest (raw)"));
        right.appendChild(el(doc, "div", "gm-apps-raw", v.manifestRaw));
        return;
      }
      const ctl = el(doc, "div", "gm-apps-controls");
      const chipEl = el(doc, "span", v.chip.cls, v.chip.text);
      ctl.appendChild(chipEl);
      const bS = el(doc, "button", "gm-btn gm-btn-primary", "Start"); bS.disabled = !v.canStart;
      const bX = el(doc, "button", "gm-btn", "Stop"); bX.disabled = !v.canStop;
      bS.addEventListener("click", function () { act(v.id, "start", {}); });
      bX.addEventListener("click", function () { act(v.id, "stop", {}); });
      ctl.appendChild(bS); ctl.appendChild(bX);
      state.live = { chip: chipEl, bS: bS, bX: bX, bO: null };
      if (v.showOpen) {
        const bO = el(doc, "button", "gm-btn", "Open"); bO.disabled = !v.openEnabled; state.live.bO = bO;
        bO.addEventListener("click", function () { act(v.id, "open", {}); });
        ctl.appendChild(bO);
      }
      right.appendChild(ctl);
      if (v.statusDetail) right.appendChild(el(doc, "div", v.status === "refused" || v.status === "port-conflict" || v.status === "failed" ? "gm-apps-warn" : "gm-apps-note", v.statusDetail));
      v.warnings.forEach(function (w) { right.appendChild(el(doc, "div", "gm-apps-warn", w)); });
      if (v.logTail.length) { right.appendChild(el(doc, "div", "gm-apps-section", "Last log lines")); right.appendChild(el(doc, "div", "gm-apps-raw", v.logTail.join("\n"))); }
      if (v.needsReconfirm) {
        right.appendChild(el(doc, "div", "gm-apps-section", "What changed since you confirmed"));
        const c = v.change || {};
        if (c.manifest && c.manifest.diff) right.appendChild(el(doc, "div", "gm-apps-raw", c.manifest.diff));
        if (c.code) {
          (c.code.outside_root || []).forEach(function (f) { right.appendChild(el(doc, "div", "gm-apps-note", "outside the app root, " + f.change + ": " + f.path)); });
          (c.code.files || []).forEach(function (f) { right.appendChild(el(doc, "div", "gm-apps-note", f.change + ": " + f.path)); if (f.diff) right.appendChild(el(doc, "div", "gm-apps-raw", f.diff)); });
          if (c.code.error) right.appendChild(el(doc, "div", "gm-apps-warn", c.code.error));
        }
        (c.paths || []).forEach(function (p) { right.appendChild(el(doc, "div", "gm-apps-note", "path value " + p.name + ": " + JSON.stringify(p.confirmed) + " -> " + JSON.stringify(p.now))); });
        if (c.venv) right.appendChild(el(doc, "div", "gm-apps-note", "venv: " + JSON.stringify(c.venv.confirmed) + " -> " + JSON.stringify(c.venv.now)));
        const bR = el(doc, "button", "gm-btn gm-btn-primary", "Re-confirm");
        state.consentBtns.push(bR);
        bR.addEventListener("click", function () { if (state.stale) return; act(v.id, "confirm", { seen: v.seen }); });
        right.appendChild(bR);
      }
      right.appendChild(el(doc, "div", "gm-apps-section", "Last start / stop"));
      const kvEl = kv([{ key: "Last start", value: v.lastStart }, { key: "Last stop", value: v.lastStop },
        { key: "Port", value: v.port == null ? "none (no port)" : String(v.port) }]);
      if (v.uiUrl) {
        kvEl.appendChild(el(doc, "dt", "", "Link"));
        const dd = el(doc, "dd", "");
        if (v.uiLinkLive) {
          const a = el(doc, "a", "gm-apps-link", v.uiUrl);
          a.href = v.uiUrl; a.target = "_blank"; a.rel = "noopener";
          a.setAttribute("href", v.uiUrl);
          dd.appendChild(a);
        } else dd.textContent = v.uiUrl;
        if (v.uiTokenNote) dd.appendChild(el(doc, "div", "gm-apps-note", "Open adds this app's access token"));
        kvEl.appendChild(dd);
      }
      right.appendChild(kvEl);
      const form = el(doc, "div", "");
      right.appendChild(el(doc, "div", "gm-apps-section", "Settings"));
      hostFields(form, v);
      v.paths.forEach(function (p) {
        const f = el(doc, "div", "gm-apps-field");
        f.appendChild(el(doc, "label", "", p.name + (p.optional ? " (optional, " + p.flag + ")" : "")));
        f.appendChild(el(doc, "span", "gm-apps-access is-" + p.access, p.accessText));
        const i = el(doc, "input", ""); i.type = "text"; i.value = p.value; i.placeholder = p.description; i.setAttribute("data-path", p.name);
        f.appendChild(i); form.appendChild(f);
      });
      v.userFields.forEach(function (u) {
        const f = el(doc, "div", "gm-apps-field");
        f.appendChild(el(doc, "label", "", u.name + (u.description ? " — " + u.description : "")));
        f.appendChild(inputFor(u)); form.appendChild(f);
      });
      const bSave = el(doc, "button", "gm-btn", "Save settings");
      bSave.addEventListener("click", function () { act(v.id, "settings", { settings: readForm(form) }); });
      form.appendChild(bSave);
      right.appendChild(form);
      if (v.licences.length) {
        right.appendChild(el(doc, "div", "gm-apps-section", "Licences"));
        v.licences.forEach(function (l) { right.appendChild(el(doc, "div", l.state === "ok" ? "gm-apps-note" : "gm-apps-warn", l.name + ": " + l.message)); });
      }
      if (v.backupsNeeded.length) {
        right.appendChild(el(doc, "div", "gm-apps-section", "Backups before the first start"));
        v.backupsNeeded.forEach(function (b) {
          const f = el(doc, "div", "gm-apps-field");
          f.appendChild(el(doc, "label", "", b.name + ": " + b.text));
          if (!b.recorded) {
            const i = el(doc, "input", ""); i.type = "text"; i.placeholder = "absolute path of an existing backup";
            const bb = el(doc, "button", "gm-btn", "Record backup");
            bb.addEventListener("click", function () { act(v.id, "backup", { name: b.name, backup_path: i.value }); });
            f.appendChild(i); f.appendChild(bb);
          }
          right.appendChild(f);
        });
      }
      if (v.actions.length) {
        right.appendChild(el(doc, "div", "gm-apps-section", "Actions (one-off commands; never run at start)"));
        v.actions.forEach(function (a) {
          const f = el(doc, "div", "gm-apps-field");
          f.appendChild(el(doc, "label", "", a.name + (a.requiresStopped ? " (app must be stopped)" : "")));
          f.appendChild(el(doc, "span", "gm-apps-note", a.text));
          const b = el(doc, "button", "gm-btn", "Run");
          b.disabled = a.disabled;
          b.addEventListener("click", function () {
            if (!win.confirm("Run the action " + a.name + " now?\n\n" + a.text)) return;
            act(v.id, "actions/" + a.name, { confirm: true });
          });
          f.appendChild(b); right.appendChild(f);
        });
      }
      right.appendChild(el(doc, "div", "gm-apps-section", "Manifest"));
      right.appendChild(kv(v.manifestRows));
      if (v.manifestError) right.appendChild(el(doc, "div", "gm-apps-warn", v.manifestError));
      right.appendChild(el(doc, "div", "gm-apps-raw", v.manifestRaw));
      const bU = el(doc, "button", "gm-btn", v.pendingUninstall ? "Uninstall proposed (confirm in Pending)" : "Propose uninstall");
      bU.disabled = v.pendingUninstall;
      bU.addEventListener("click", function () { act(v.id, "uninstall", {}); });
      right.appendChild(bU);
    }

    // Unsaved-input rule: a poll tick never re-renders the detail pane while ANY input in it differs from what the
    // last render put there (or has focus); a tick that finds the response unchanged does not re-render either.
    function formControls() {     // every form control the pane renders: input (text, number, checkbox, radio), select, textarea
      return [].concat(Array.prototype.slice.call(right.querySelectorAll("input")),
        Array.prototype.slice.call(right.querySelectorAll("select")),
        Array.prototype.slice.call(right.querySelectorAll("textarea")));
    }
    function formSnapshot() {
      return formControls().map(function (i) { return [String(i.value), !!i.checked]; });
    }
    function paneDirty() {
      const now = formSnapshot(), base = state.baseline || [];
      if (now.length !== base.length) return true;
      for (let k = 0; k < now.length; k++) if (now[k][0] !== base[k][0] || now[k][1] !== base[k][1]) return true;
      const ae = doc.activeElement;     // a field the operator is typing into counts as unsaved
      return !!ae && formControls().indexOf(ae) >= 0;
    }

    async function act(id, op, body) {
      try {
        if (op === "confirm" || op === "settings") toast("A Mac password / Touch ID prompt is waiting for you (2 minutes)...");
        const r = await req("POST", "/api/apps/" + encodeURIComponent(id) + "/" + op, body);
        toast(op + ": " + (r && (r.label || r.status || (r.ok ? "done" : ""))) );
      } catch (e) { toast(op + " refused: " + String(e.message || e)); }
      loadList();
      if (state.selected) loadDetail(state.selected);
    }

    async function loadDetail(id) {
      let v;
      const seq = ++state.detailSeq;
      let sig = null;
      try { const resp = await req("GET", "/api/apps/" + encodeURIComponent(id)); sig = JSON.stringify(resp); v = buildDetail(resp); }
      catch (e) { v = { error: String(e.message || e) }; }
      if (seq !== state.detailSeq) return;      // a newer user load started while this one was in flight
      state.detailSig = sig;
      renderDetail(v);
    }

    // The poll's share: refresh the SELECTED app's detail so a pane opened while the app was starting follows it up.
    async function pollDetail(signal) {
      const id = state.selected;
      if (!id) return;
      const seq = state.detailSeq;      // a poll never bumps it: only a user load supersedes
      try {
        const resp = await req("GET", "/api/apps/" + encodeURIComponent(id), undefined, signal);
        if (seq !== state.detailSeq || id !== state.selected) return;   // superseded by a user load / another selection
        const sig = JSON.stringify(resp);
        if (sig === state.detailSig) {                                  // nothing changed: leave the pane alone
          if (state.notRefreshed) { state.notRefreshed = false; showAsOf(); }
          return;
        }
        const v = buildDetail(resp);
        if (state.shownConsent) {                                       // F4: never change what the operator is about to approve
          state.detailSig = sig;
          if (v.error || v.consentKey !== state.shownKey) markStale(); else applyLive(v);
          return;
        }
        if (paneDirty()) { state.notRefreshed = true; showAsOf(); return; }   // never wipe unsaved input; next tick retries
        state.detailSig = sig;
        renderDetail(v);
      } catch (e) { /* the pane keeps its last good render */ }
    }

    async function loadList() {
      let v;
      try { v = buildList(await req("GET", "/api/apps")); }
      catch (e) { v = { error: String(e.message || e) }; }
      renderList(v);
      noticeFrom(v);
    }

    function noticeFrom(v) {
      if (!v || v.error) return;
      const ids = v.pending.map(function (p) { return p.id + ":" + p.kind; });
      {
        // first load: every waiting proposal is news; later: only the ones that arrived since the last poll
        const fresh = state.seenPending === null ? ids : newArrivals(state.seenPending, v.pending);
        if (fresh.length && !panel.classList.contains("is-active")) {
          banner.textContent = "";
          banner.appendChild(el(doc, "b", "", fresh.length + " new app proposal" + (fresh.length === 1 ? "" : "s")));
          banner.appendChild(doc.createTextNode(" waiting for your confirm — click to review"));
          banner.classList.add("is-on");
        }
      }
      state.seenPending = ids;
    }

    async function poll(signal) {
      try { const v = buildList(await req("GET", "/api/apps", undefined, signal)); noticeFrom(v); if (panel.classList.contains("is-active")) renderList(v); }
      catch (e) { /* the tab shows errors loudly when opened */ }
      if (panel.classList.contains("is-active")) await pollDetail(signal);
    }

    bRef.addEventListener("click", function () { loadList(); if (state.selected) loadDetail(state.selected); });
    pBtn.addEventListener("click", async function () {
      try { await req("POST", "/api/apps", { manifest_path: pIn.value }); toast("proposed: confirm it in Pending"); }
      catch (e) { toast("proposal refused: " + String(e.message || e)); }
      loadList();
    });
    poll();
    win.setInterval(win.GmPoll ? win.GmPoll.wrap("apps", poll) : poll, POLL_MS);   // poll-pileup fix: in-flight guard
  }

  if (typeof window !== "undefined" && window.document) {
    if (window.document.readyState === "loading") window.document.addEventListener("DOMContentLoaded", function () { init(window); });
    else init(window);
  }

  return { buildList: buildList, buildDetail: buildDetail, statusChip: statusChip, fieldFor: fieldFor,
    newArrivals: newArrivals, collectValues: collectValues, manifestRows: manifestRows, init: init, POLL_MS: POLL_MS };
});

/* Gnomon web frontend — the THIN browser client.
 *
 * All logic lives in the reused Python Controller behind /api. This script only
 * fetches JSON, renders it, and posts a launch TRIGGER (the daemon opens the
 * terminal — the page never does). No framework; small enough to read.
 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  // chip logic (web/static/notice_chips.js, node-tested).
  const NC = window.GmNoticeChips || null;
  // build-stamp chip logic (web/static/build_chip.js, node-tested).
  const BC = window.GmBuildChip || null;
  // quiescent chip logic (web/static/quiescent_chip.js, node-tested).
  const QC = window.GmQuiescentChip || null;
  // idle-snooze chip logic (web/static/idle_snooze_chip.js, node-tested).
  const SC = window.GmIdleSnoozeChip || null;
  // skill-sync chip logic (web/static/skill_sync_chip.js, node-tested).
  const KC = window.GmSkillSyncChip || null;
  // go-live smoke chip logic (web/static/golive_chip.js, node-tested).
  const GC = window.GmGoliveChip || null;

  // A chip span built with textContent ONLY (never innerHTML with data).
  function noticeChipEl(chip) {
    const el = document.createElement("span");
    el.className = chip.cls;
    el.textContent = chip.text;
    if (chip.title) el.title = chip.title;
    return el;
  }

  // --- toast ---------------------------------------------------------------
  let toastTimer = null;
  function toast(msg, isError) {
    const el = $("#toast");
    el.textContent = msg;
    el.classList.toggle("is-error", !!isError);
    el.classList.add("is-shown");
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("is-shown"), 4000);
  }

  // --- api -----------------------------------------------------------------
  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (!res.ok) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        if (body && body.detail) detail = body.detail;
      } catch (e) { /* non-JSON error body */ }
      throw new Error(detail);
    }
    return res.json();
  }

  // --- tabs ----------------------------------------------------------------
  function activateTab(name) {
    document.querySelectorAll(".gm-tab").forEach((t) => {
      t.classList.toggle("is-active", t.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".gm-panel").forEach((p) => {
      p.classList.toggle("is-active", p.id === "panel-" + name);
    });
    if (name === "dsd") syncDsdIdLookup();   // GM-RM-X74: pick up a flag flipped in the Config tab
  }

  function initTabs() {
    document.querySelectorAll(".gm-tab").forEach((tab) => {
      tab.addEventListener("click", () => activateTab(tab.getAttribute("data-tab")));
    });
  }

  // --- header / health -----------------------------------------------------
  async function loadHealth(signal) {
    try {
      const h = await api("/api/health", pollOpts(signal));
      $("#hub-info").textContent = h.hub_url
        ? "hub " + h.hub_url
        : "hub not started";
      // the dashboard server's own staleness (null when current / flag off).
      const holder = $("#build-info");
      if (holder) {
        holder.textContent = "";
        const chip = BC ? BC.serverBuildChip(h.build, window.location.port) : null;
        if (chip) holder.appendChild(noticeChipEl(chip));
      }
    } catch (e) {
      if (signal && signal.aborted) return;   // a timed-out poll: keep what is shown, the next tick retries
      $("#hub-info").textContent = "backend unreachable";
    }
  }

  // --- confirm-proof F1(c): the banner shown while a SECURITY flag is OFF (textContent only) ------------------
  async function loadSecurityBanner(signal) {
    const el = $("#security-banner");
    if (!el) return;
    let res = null;
    try { res = await api("/api/security-warnings", pollOpts(signal)); } catch (e) { if (signal && signal.aborted) return; res = null; }
    const ws = res && Array.isArray(res.warnings) ? res.warnings : [];
    el.textContent = ws.map((w) => String(w.text || w.flag)).join("   |   ");
    el.hidden = ws.length === 0;
  }

  // --- start-time go-live smoke chip ------------------------------
  // The header chip ("go-live: 18/18 PASS" / "go-live: 3 FAIL"); clicking it
  // expands the failing lines. textContent only — evidence strings are data.
  let goliveOpen = false;
  async function loadGolive(signal) {
    const holder = $("#golive-info");
    const detail = $("#golive-detail");
    if (!holder || !GC) return;
    let res = null;
    try {
      res = await api("/api/golive", pollOpts(signal));
    } catch (e) {
      if (signal && signal.aborted) return;   // a timed-out poll: keep what is shown, the next tick retries
      res = null;
    }
    holder.textContent = "";
    const chip = GC.goliveChip(res);
    const lines = GC.failingLines(res);
    if (chip) {
      const el = noticeChipEl(chip);
      el.setAttribute("role", "button");
      el.tabIndex = 0;
      const toggle = () => {
        goliveOpen = !goliveOpen;
        renderGoliveDetail(detail, GC.failingLines(res));
      };
      el.addEventListener("click", toggle);
      el.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); toggle(); }
      });
      holder.appendChild(el);
    } else {
      goliveOpen = false;
    }
    renderGoliveDetail(detail, lines);
  }

  function renderGoliveDetail(detail, lines) {
    if (!detail) return;
    detail.textContent = "";
    if (!goliveOpen) { detail.hidden = true; return; }
    const shown = lines.length ? lines : ["No failing go-live checks."];
    shown.forEach((line) => {
      const row = document.createElement("div");
      row.className = "gm-golive-line";
      row.textContent = line;
      detail.appendChild(row);
    });
    detail.hidden = false;
  }

  // --- profiles ------------------------------------------------------------
  async function loadProfiles() {
    const list = $("#profiles-list");
    try {
      const rows = await api("/api/profiles");
      list.innerHTML = "";
      if (!rows.length) {
        list.innerHTML =
          '<div class="gm-empty">No profiles yet. Create one under ' +
          "~/gnomon-data/agents/&lt;id&gt;/profile.json.</div>";
        $("#profiles-status").textContent = "0 profiles";
        return;
      }
      rows.forEach((p) => list.appendChild(profileRow(p)));
      $("#profiles-status").textContent =
        rows.length + (rows.length === 1 ? " profile" : " profiles");
    } catch (e) {
      list.innerHTML = '<div class="gm-error">Could not load profiles: ' +
        escapeHtml(e.message) + "</div>";
    }
  }

  function profileRow(p) {
    const row = document.createElement("div");
    row.className = "gm-row";

    const main = document.createElement("div");
    main.className = "gm-row-main";
    const title = document.createElement("div");
    title.className = "gm-row-title";
    if (!p.readable) {
      title.innerHTML =
        '<span class="gm-id">' + escapeHtml(p.instance_id) + "</span> " +
        '<span class="gm-unreadable">[unreadable profile.json]</span>';
    } else if (p.display_name) {
      title.innerHTML =
        escapeHtml(p.display_name) +
        '  <span class="gm-id">(' + escapeHtml(p.instance_id) + ")</span>";
    } else {
      title.innerHTML = '<span class="gm-id">' + escapeHtml(p.instance_id) + "</span>";
    }
    if (p.readable) {
      // a small "read" button right of the name -> read-only def viewer modal.
      const readBtn = document.createElement("button");
      readBtn.className = "gm-btn gm-btn-read";
      readBtn.textContent = "read";
      readBtn.title = "Read this agent's definition (read-only)";
      readBtn.addEventListener("click", () => openDefViewer(p.instance_id));
      title.appendChild(document.createTextNode(" "));
      title.appendChild(readBtn);
    }
    main.appendChild(title);

    const subBits = [];
    if (p.identity_class) subBits.push(p.identity_class);
    if (p.model) subBits.push(p.model);
    if (subBits.length) {
      const sub = document.createElement("div");
      sub.className = "gm-row-sub";
      sub.textContent = subBits.join("  ·  ");
      main.appendChild(sub);
    }
    const defLine = profileDefSizeLine(p);
    if (defLine) main.appendChild(defLine);
    row.appendChild(main);

    const launch = document.createElement("button");
    launch.className = "gm-btn gm-btn-primary";
    launch.textContent = "Launch";
    launch.addEventListener("click", () => doLaunch(p.instance_id, launch));
    row.appendChild(launch);

    return row;
  }

  // --- agent-def size (operator ruling) ---------------------
  // A plain reminder number, never a warning; above 60K chars a quiet neutral
  // "large" style. Formatting is in def_size.js (window.GmDefSize, node-tested).
  const DefSize = window.GmDefSize || null;
  if (!DefSize) console.error("def_size.js did not load — agent-def sizes are hidden");

  function defSizeChip(size, prefix) {
    if (!DefSize || !size) return null;
    const chip = document.createElement("span");
    chip.className = DefSize.sizeClass(size);
    chip.textContent = (prefix ? prefix + " " : "") + DefSize.formatDefSize(size);
    if (size.approx_basis) chip.title = "tokens: " + size.approx_basis;
    return chip;
  }

  function profileDefSizeLine(p) {
    const ad = p.agent_def;
    if (!p.readable || !ad) return null;
    const line = document.createElement("div");
    line.className = "gm-row-sub gm-defsize-line";
    const label = document.createElement("span");
    label.textContent = "Agent def: ";
    line.appendChild(label);
    const rendered = defSizeChip(ad.latest_render, "rendered");
    const source = defSizeChip(ad.source_size, "source");
    if (rendered) {
      rendered.title = (ad.latest_render.path || "") + " · " + (rendered.title || "");
      line.appendChild(rendered);
    }
    if (rendered && source) line.appendChild(document.createTextNode("  ·  "));
    if (source) {
      source.title = (ad.source || "") + " · " + (source.title || "");
      line.appendChild(source);
    }
    if (!rendered && !source) {
      const none = document.createElement("span");
      none.textContent = ad.source_error ? "source unreadable (" + ad.source_error + ")"
        : "no claude_md_source configured";
      line.appendChild(none);
      return line;
    }
    // The optimizer agent is DEPLOYMENT data (defaults/optimizer.json): no optimizer, no action offered.
    if (ad.optimizer) {
      const opt = document.createElement("button");
      opt.className = "gm-btn gm-btn-link";
      opt.textContent = "Optimize with " + ad.optimizer;
      opt.addEventListener("click", () => openDefOptimize(p.instance_id, opt));
      line.appendChild(opt);
    }
    return line;
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (e) {
      return false;
    }
  }

  function copyBlock(pane, label, text) {
    const head = document.createElement("div");
    head.className = "gm-defopt-label";
    head.textContent = label;
    pane.appendChild(head);
    const ta = document.createElement("textarea");
    ta.className = "gm-defopt-text";
    ta.readOnly = true;
    ta.rows = Math.min(10, text.split("\n").length + 1);
    ta.value = text;
    pane.appendChild(ta);
    const btn = document.createElement("button");
    btn.className = "gm-btn";
    btn.textContent = "Copy";
    btn.addEventListener("click", async () => {
      if (await copyText(text)) {
        toast(label + " copied.");
      } else {
        ta.focus();
        ta.select();
        toast("Clipboard unavailable — the text is selected; press Cmd-C.", true);
      }
    });
    pane.appendChild(btn);
  }

  // "Optimize with <optimizer>": the web launch route takes no custom prompt, so
  // this shows the prompt + a copyable command through the EXISTING agent_launch
  // CLI (or, when the optimizer is already live, says so: paste the prompt there).
  async function openDefOptimize(instanceId, btn) {
    const pane = $("#defopt-panel");
    if (btn) btn.disabled = true;
    try {
      const r = await api("/api/agent-def/optimize/" + encodeURIComponent(instanceId));
      pane.innerHTML = "";
      const head = document.createElement("div");
      head.className = "gm-section-head";
      const h = document.createElement("h2");
      h.textContent = "Optimize " + instanceId + "'s agent def with " + (r.optimizer_profile || "the optimizer");
      head.appendChild(h);
      const close = document.createElement("button");
      close.className = "gm-btn";
      close.textContent = "Close";
      close.addEventListener("click", () => { pane.innerHTML = ""; pane.classList.remove("is-open"); });
      head.appendChild(close);
      pane.appendChild(head);
      const note = document.createElement("p");
      note.className = "gm-defopt-note";
      if (r.live_session) {
        note.textContent = r.optimizer_profile + " is live (" + r.live_session.session_dir +
          "). Paste the prompt below into that session — or run the command to start another.";
      } else {
        note.textContent = "No live " + (r.optimizer_profile || "optimizer") + " session. " + r.reason;
      }
      pane.appendChild(note);
      if (!r.optimizer_profile_exists) {
        const warn = document.createElement("p");
        warn.className = "gm-defopt-note";
        warn.textContent = "There is no readable '" + r.optimizer_profile +
          "' profile on this host; the command will be refused until one exists.";
        pane.appendChild(warn);
      }
      copyBlock(pane, "Prompt", r.prompt);
      copyBlock(pane, "Command (run on the dashboard host)", r.command);
      pane.classList.add("is-open");
      pane.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (e) {
      toast("Could not prepare the optimization request for " + instanceId + ": " + e.message, true);
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  // --- read-only def viewer + "Run Macro Expanders" preview ---------------
  // Everything from the server (def text, expansion text, statuses) is untrusted: textContent only.
  // defViewer = {closed, previewId} for the ONE open viewer (null when none). Every preview id the
  // server hands back is DELETEd exactly when it stops being the live one: on Close / backdrop, on a
  // re-run (the previous id), and when a POST resolves into an already-closed viewer (F1).
  let defViewer = null;

  function deleteDefPreview(pid) {
    if (!pid) return;
    fetch("/api/def-view/preview/" + encodeURIComponent(pid), { method: "DELETE" })
      .catch(() => { /* swept by age on the next preview / server start */ });
  }

  function closeDefViewer() {
    const ov = document.getElementById("defview-overlay");
    const v = defViewer;
    defViewer = null;
    if (ov) ov.remove();
    if (v) {
      v.closed = true;
      const pid = v.previewId;
      v.previewId = null;
      deleteDefPreview(pid);
    }
  }

  async function openDefViewer(instanceId) {
    closeDefViewer();
    const ov = document.createElement("div");
    ov.id = "defview-overlay";
    ov.className = "gm-defview-overlay";
    const box = document.createElement("div");
    box.className = "gm-defview-box";
    const head = document.createElement("div");
    head.className = "gm-defview-head";
    const h = document.createElement("h3");
    h.textContent = "Definition: " + instanceId + " (read-only)";
    head.appendChild(h);
    const btns = document.createElement("div");
    btns.className = "gm-defview-btns";
    const runBtn = document.createElement("button");
    runBtn.className = "gm-btn gm-btn-primary";
    runBtn.textContent = "Run Macro Expanders";
    const closeBtn = document.createElement("button");
    closeBtn.className = "gm-btn";
    closeBtn.textContent = "Close";
    btns.appendChild(runBtn);
    btns.appendChild(closeBtn);
    head.appendChild(btns);
    const summary = document.createElement("div");
    summary.className = "gm-defview-summary";
    summary.hidden = true;
    const body = document.createElement("div");
    body.className = "gm-defview-body";
    box.appendChild(head);
    box.appendChild(summary);
    box.appendChild(body);
    ov.appendChild(box);
    document.body.appendChild(ov);
    const viewer = { closed: false, previewId: null };
    defViewer = viewer;
    closeBtn.addEventListener("click", closeDefViewer);
    ov.addEventListener("click", (ev) => { if (ev.target === ov) closeDefViewer(); });

    body.textContent = "Loading…";
    try {
      const raw = await api("/api/def-view/" + encodeURIComponent(instanceId) + "/raw");
      body.textContent = raw.text;
    } catch (e) {
      body.textContent = "Could not load the definition: " + e.message;
    }

    runBtn.addEventListener("click", async () => {
      runBtn.disabled = true;
      const orig = runBtn.textContent;
      runBtn.textContent = "Running…";
      try {
        // F2: a re-run replaces the previous preview, so delete its dir first.
        const prev = viewer.previewId;
        viewer.previewId = null;
        deleteDefPreview(prev);
        const r = await api("/api/def-view/" + encodeURIComponent(instanceId) + "/preview",
                            { method: "POST" });
        if (viewer.closed) {  // F1: closed while the POST was in flight -> toss the late dir now
          deleteDefPreview(r.preview_id);
          return;
        }
        viewer.previewId = r.preview_id;
        h.textContent = "Expanded definition: " + instanceId + " (preview, read-only)";
        renderDefPreviewSummary(summary, r);
        body.textContent = r.text;
      } catch (e) {
        summary.hidden = false;
        summary.textContent = "Preview failed: " + e.message;
      } finally {
        runBtn.disabled = false;
        runBtn.textContent = orig;
      }
    });
  }

  function renderDefPreviewSummary(el, r) {
    el.hidden = false;
    el.textContent = "";
    const c = r.counts || {};
    const line = document.createElement("div");
    const bad = (c.failed || 0) > 0 || (r.render_status && !["ok", "none"].includes(r.render_status));
    line.className = bad ? "gm-defview-line gm-defview-bad" : "gm-defview-line";
    line.textContent = "Render: " + r.render_status + "  ·  macros: " + (c.ok || 0) + " ok, " +
      (c["empty-ok"] || 0) + " empty-ok, " + (c.failed || 0) + " FAILED  ·  " + r.chars + " chars";
    el.appendChild(line);
    if (r.reason) {
      const rs = document.createElement("div");
      rs.className = "gm-defview-line gm-defview-bad";
      rs.textContent = r.reason;
      el.appendChild(rs);
    }
    (r.macros || []).forEach((m) => {
      const d = document.createElement("div");
      d.className = m.status === "failed" ? "gm-defview-line gm-defview-bad" : "gm-defview-line";
      let t = String(m.status).toUpperCase() + "  " + m.query;
      if (m.matched !== null && m.matched !== undefined) t += "  (matched " + m.matched + ")";
      if (m.status === "failed" && m.reason) t += "  — " + m.reason;
      d.textContent = t;
      el.appendChild(d);
    });
    (r.warnings || []).forEach((w) => {
      const d = document.createElement("div");
      d.className = "gm-defview-line";
      d.textContent = "warning: " + w;
      el.appendChild(d);
    });
  }

  async function doLaunch(instanceId, btn) {
    btn.disabled = true;
    const original = btn.textContent;
    btn.textContent = "Launching…";
    try {
      const r = await api("/api/launch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ instance_id: instanceId }),
      });
      toast("Launched " + instanceId + " → a terminal opened on the host.");
      if (r.lts_expansion_pending && r.lts_expansion_pending.length) {
        toast(
          "Launched " + instanceId + " (note: " +
            r.lts_expansion_pending.length + " lts marker(s) left un-expanded)"
        );
      }
      loadSessions();
    } catch (e) {
      toast("Launch failed for " + instanceId + ": " + e.message, true);
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  }

  // --- live sessions -------------------------------------------------------
  // An AbortSignal from GmPoll.wrap (poller calls only); anything else (a user/boot call has none) => no signal.
  function pollOpts(signal) {
    return signal && typeof signal.aborted === "boolean" ? { signal: signal } : undefined;
  }

  async function loadSessions(signal) {
    const list = $("#sessions-list");
    try {
      const rows = await api("/api/sessions", pollOpts(signal));
      list.innerHTML = "";
      if (!rows.length) {
        list.innerHTML = '<div class="gm-empty">No sessions on disk yet.</div>';
        $("#sessions-status").textContent = "";
        return;
      }
      rows.forEach((s) => list.appendChild(sessionRow(s)));
      const liveN = rows.filter((s) => s.live).length;
      $("#sessions-status").textContent =
        liveN + " live · " + rows.length + " total";
    } catch (e) {
      if (signal && signal.aborted) return;   // a timed-out poll: keep what is shown, the next tick retries
      list.innerHTML = '<div class="gm-error">Liveness scan error: ' +
        escapeHtml(e.message) + "</div>";
    }
  }

  function sessionRow(s) {
    const row = document.createElement("div");
    row.className = "gm-row";

    const dot = document.createElement("span");
    dot.className = "gm-dot" + (s.live ? " is-live" : "");
    row.appendChild(dot);

    const main = document.createElement("div");
    main.className = "gm-row-main";
    const title = document.createElement("div");
    title.className = "gm-row-title";
    const state = s.live ? "live" : "ended";
    title.innerHTML =
      escapeHtml(s.instance_id) +
      '  <span class="gm-id">[' + state + "]</span>";
    main.appendChild(title);
    const sub = document.createElement("div");
    sub.className = "gm-row-sub";
    sub.textContent = s.reason || s.session_dir || "";
    main.appendChild(sub);
    row.appendChild(main);

    const defChip = defSizeChip(s.agent_def, "def");
    if (defChip) {
      if (s.agent_def.path) defChip.title = s.agent_def.path + " · " + (defChip.title || "");
      row.appendChild(defChip);
    }

    // held-notice chip (names the held kind; absent when notice_hold is empty).
    const hold = NC ? NC.holdChip(s.notice_hold) : null;
    if (hold) row.appendChild(noticeChipEl(hold));

    // a stuck modal hold is loud (null when none / flag off).
    const mhold = NC && NC.modalHoldChip ? NC.modalHoldChip(s.modal_hold) : null;
    if (mhold) row.appendChild(noticeChipEl(mhold));

    // "WAITING ON YOU" while this agent's terminal waits on a permission prompt (the key is absent with the flag off).
    const wchip = NC && NC.waitingChip ? NC.waitingChip(s.waiting) : null;
    if (wchip) row.appendChild(noticeChipEl(wchip));

    // stale / unknown build chip, on the ROW (list view), live sessions only.
    const buildChip = BC ? BC.sessionBuildChip(s) : null;
    if (buildChip) row.appendChild(noticeChipEl(buildChip));

    // "quiescent: <reason> since <time>" (null when working / flag off).
    const qChip = QC ? QC.quiescentChip(s.quiescent) : null;
    if (qChip) row.appendChild(noticeChipEl(qChip));

    // "idle alerts dismissed until operator returns: <reason>" (null when none / flag off).
    const snoozeChip = SC ? SC.idleSnoozeChip(s.idle_snooze) : null;
    if (snoozeChip) row.appendChild(noticeChipEl(snoozeChip));

    // "skills applied: N written" (null when never called / flag off).
    const skillChip = KC ? KC.skillSyncChip(s.skill_sync) : null;
    if (skillChip) row.appendChild(noticeChipEl(skillChip));

    if (s.used_percentage !== null && s.used_percentage !== undefined) {
      const badge = document.createElement("span");
      badge.className = "gm-badge";
      badge.textContent = "ctx " + Math.round(s.used_percentage) + "%";
      row.appendChild(badge);
    }
    return row;
  }

  // --- DSD tab (dual pane) -------------------------------------
  //
  // Thin browser client over the reused DsdTabController behind /api/dsd. All
  // gates (context guard, completeness gate, escape-hatch-requires-a-comment,
  // single-select v1) are enforced SERVER-SIDE; the Save-disabled state here is a
  // convenience mirror, never the authority (the server refuses a partial save
  // even if this UI is bypassed).

  let dsdSelected = null;      // dsd_id currently shown on the right
  let dsdForm = null;          // the presented form model (for the completeness mirror)

  // In-progress-answer protection (operator-observed defect 2026-09-29: an arrival
  // mid-answer auto-switched the page and the answers were lost). Pure logic is
  // in dsd_draft.js (window.GmDsdDraft, node-tested). Drafts live in
  // localStorage per dsd_id; every access is try/catch'd inside the store, so a
  // missing/blocked storage only means "no drafts", never a broken page.
  const DsdDraft = window.GmDsdDraft || null;
  if (!DsdDraft) {
    // Fail loud: without the module the page still works, but arrivals are held
    // (never auto-switched) so an in-progress form is never silently wiped.
    console.error("dsd_draft.js did not load — DSD draft protection is OFF");
  }
  const dsdDrafts = DsdDraft
    ? DsdDraft.makeDraftStore(() => window.localStorage)
    : { load: () => null, save: () => false, clear: () => false };
  let dsdHeldArrivals = [];    // arrivals held behind the banner (newest first)

  // DSD review activity -> operator presence (operator, 2026-09-29). Fire-and-forget:
  // sendBeacon (or a keepalive fetch) of {dsd_id} ONLY — never a choice or a
  // comment — never awaited, so it can never add latency to input. The server
  // turns it into a review beacon every harness's stall nudge reads.
  function sendDsdActivityBeacon(payload) {
    const body = JSON.stringify({ dsd_id: String(payload && payload.dsd_id) });
    try {
      if (navigator.sendBeacon &&
          navigator.sendBeacon("/api/dsd/activity", new Blob([body], { type: "application/json" }))) {
        return;
      }
    } catch (e) { /* fall through to fetch */ }
    try {
      fetch("/api/dsd/activity", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: body, keepalive: true,
      }).catch(() => {});
    } catch (e) { /* presence is advisory */ }
  }
  const dsdReviewActivity = DsdDraft && DsdDraft.makeReviewActivity
    ? DsdDraft.makeReviewActivity(sendDsdActivityBeacon)
    : { note: () => false };

  // { dec_id: {option_id, comment} } with the comment RAW (untrimmed) — the
  // draft shape. currentSelections() below trims for the submit payload.
  function currentDraftSelections() {
    const sel = {};
    if (!dsdForm) return sel;
    dsdForm.decisions.forEach((dec) => {
      const radio = document.querySelector(
        'input[name="dsd-' + cssEscape(dec.id) + '"]:checked');
      const box = document.querySelector(
        'textarea.gm-dsd-comment[data-dec-id="' + cssEscape(dec.id) + '"]');
      sel[dec.id] = { option_id: radio ? radio.value : null, comment: box ? box.value : "" };
    });
    return sel;
  }

  // Every radio change / comment keystroke: re-mirror the gate AND save the draft.
  function onDsdFormChange() {
    reevaluateGate();
    if (!dsdForm) return;
    dsdReviewActivity.note(dsdForm.dsd_id);   // throttled, fire-and-forget, {dsd_id} only
    const saved = dsdDrafts.save(dsdForm.dsd_id, dsdForm, currentDraftSelections());
    showDsdDraftWarning(saved);
  }

  // A failed draft save (quota full, storage blocked/unavailable) is shown IN
  // the form, never swallowed: an older draft may be what a reopen restores.
  function showDsdDraftWarning(saved) {
    const el = $("#dsd-draft-warn");
    if (!el) return;
    el.textContent = saved ? "" :
      "⚠ Your draft could not be saved (browser storage full or unavailable). " +
      "These answers exist only on this page until you press Save Answers.";
    el.classList.toggle("is-shown", !saved);
  }

  // Called before ANY re-render of #dsd-detail: flush the presented form's draft
  // (belt to the per-change save) and forget the form, so no path drops input.
  function leaveDsdForm() {
    if (dsdForm) dsdDrafts.save(dsdForm.dsd_id, dsdForm, currentDraftSelections());
    dsdForm = null;
  }

  function applyDsdSelections(sel) {
    Object.keys(sel).forEach((decId) => {
      const s = sel[decId];
      document.querySelectorAll('input[name="dsd-' + cssEscape(decId) + '"]')
        .forEach((r) => { r.checked = s.option_id != null && r.value === s.option_id; });
      const box = document.querySelector(
        'textarea.gm-dsd-comment[data-dec-id="' + cssEscape(decId) + '"]');
      if (box) box.value = s.comment || "";
    });
  }

  // --- the non-blocking "new DSD arrived" banner (fixed; visible on any tab) ---
  function dsdArrivalBannerEl() {
    let el = $("#dsd-arrival");
    if (el) return el;
    el = document.createElement("div");
    el.className = "gm-dsd-arrival";
    el.id = "dsd-arrival";
    el.setAttribute("role", "status");
    const txt = document.createElement("span");
    txt.className = "gm-dsd-arrival-text";
    el.appendChild(txt);
    const open = document.createElement("button");
    open.className = "gm-btn gm-btn-primary";
    open.textContent = "Open";
    open.addEventListener("click", () => {
      const it = dsdHeldArrivals[0];
      hideDsdArrivalBanner();
      if (!it) return;
      activateTab("dsd");
      loadDsdBanner();
      openDsd(it);   // the current form's draft is already saved; it restores on reopen
    });
    el.appendChild(open);
    const dismiss = document.createElement("button");
    dismiss.className = "gm-btn";
    dismiss.textContent = "Dismiss";
    dismiss.addEventListener("click", hideDsdArrivalBanner);
    el.appendChild(dismiss);
    document.body.appendChild(el);
    return el;
  }

  function showDsdArrivalBanner(arrivals) {
    const seen = new Set(dsdHeldArrivals.map((a) => a.dsd_id));
    dsdHeldArrivals = arrivals.filter((a) => !seen.has(a.dsd_id)).concat(dsdHeldArrivals);
    const el = dsdArrivalBannerEl();
    const newest = dsdHeldArrivals[0];
    el.querySelector(".gm-dsd-arrival-text").textContent = DsdDraft
      ? DsdDraft.arrivalBannerText(newest, dsdHeldArrivals.length - 1) + " —"
      : "New DSD from " + newest.instance_id + " arrived —";
    el.classList.add("is-shown");
  }

  function hideDsdArrivalBanner() {
    dsdHeldArrivals = [];
    const el = $("#dsd-arrival");
    if (el) el.classList.remove("is-shown");
  }

  async function loadDsdBanner() {
    const el = $("#dsd-banner");
    try {
      const s = await api("/api/dsd/status");
      const w = s.writer;
      if (w && w.live && w.live.live === false) {
        el.textContent = w.live.banner || "lts-engine not live";
        el.classList.add("is-warn");
      } else if (w && w.live && w.live.live) {
        el.textContent =
          "lts-engine live · corpus " + w.live.corpus_id + " registered + queryable";
        el.classList.remove("is-warn");
      } else if (w) {
        el.textContent = "writer: " + w.kind;
        el.classList.remove("is-warn");
      } else {
        el.textContent = "";
      }
    } catch (e) {
      el.textContent = "DSD status unavailable: " + e.message;
      el.classList.add("is-warn");
    }
  }

  let dsdListSig = null;  // signature of the last RENDERED list (audit LOW)

  // --- DSD sent / answered time stamps (pure formatting: dsd_senttime.js) ----------
  // Each stamp is a span carrying its inputs as data-* attributes; refreshDsdStamps()
  // re-derives the text from the CURRENT clock, so the relative age moves with the
  // existing 5 s list poll even when the list itself is not rebuilt.
  const ST = (typeof GmDsdSentTime !== "undefined") ? GmDsdSentTime : null;

  function dsdStampEl(kind, epoch, iso, source) {
    const el = document.createElement("span");
    el.className = "gm-dsd-sent";
    el.setAttribute("data-stamp-kind", kind);   // "sent" | "answered"
    el.setAttribute("data-stamp-epoch", epoch == null ? "" : String(epoch));
    el.setAttribute("data-stamp-iso", iso || "");
    el.setAttribute("data-stamp-source", source || "");
    paintDsdStamp(el, Date.now());
    return el;
  }

  function paintDsdStamp(el, nowMs) {
    if (!ST) return;
    const kind = el.getAttribute("data-stamp-kind");
    const raw = el.getAttribute("data-stamp-epoch");
    const epoch = raw === "" ? null : Number(raw);
    const iso = el.getAttribute("data-stamp-iso");
    const d = kind === "answered"
      ? ST.describeAnswered(epoch, iso, el.getAttribute("data-stamp-source"), nowMs)
      : ST.describe(epoch, iso, nowMs, "sent");
    if (!d) { el.textContent = ""; el.hidden = true; el.removeAttribute("title"); return; }
    el.hidden = false;
    el.textContent = d.text;
    if (d.title) el.title = d.title; else el.removeAttribute("title");
  }

  // The detail-page stamps line: sent (always) + answered (browse, when carried).
  function dsdTimesEl(view) {
    const wrap = document.createElement("div");
    wrap.className = "gm-dsd-times";
    wrap.appendChild(dsdStampEl("sent", view.created_epoch, view.created_iso));
    if ("answered_epoch" in view)
      wrap.appendChild(dsdStampEl("answered", view.answered_epoch, view.answered_iso, view.answered_source));
    return wrap;
  }

  function refreshDsdStamps() {
    const now = Date.now();
    document.querySelectorAll(".gm-dsd-sent").forEach((el) => paintDsdStamp(el, now));
  }

  async function loadDsdList(signal) {
    const list = $("#dsd-list");
    try {
      const rows = await api("/api/dsd/list", pollOpts(signal));
      dsdInstanceById = {};
      rows.forEach((it) => { dsdInstanceById[it.dsd_id] = it.instance_id; });
      // keep an open browse pane's chip current (acceptance-2: normal polling)
      const sel = rows.find((it) => it.dsd_id === dsdSelected);
      if (sel) setDetailNoticeChip(sel.dsd_id, sel.notice);
      // Audit LOW: an unchanged poll must NOT rebuild (focus / clicks survive).
      const sig = NC ? NC.dsdListSignature(rows, dsdSelected) : null;
      if (NC && !NC.shouldRebuildList(dsdListSig, sig)) {
        refreshDsdStamps();
        await loadNoticeWarning(signal);
        return;
      }
      const active = document.activeElement;
      const focusedId = active && list.contains(active)
        ? active.getAttribute("data-dsd-id") : null;
      list.innerHTML = "";
      dsdListSig = sig;
      if (!rows.length) {
        list.innerHTML =
          '<div class="gm-empty">No DSDs yet. Agents send them via ' +
          "send_dsd_for_human_review.</div>";
      } else {
        renderDsdSections(list, rows);
        if (focusedId) {
          const again = Array.from(list.querySelectorAll("[data-dsd-id]"))
            .find((el) => el.getAttribute("data-dsd-id") === focusedId);
          if (again) again.focus();
        }
      }
    } catch (e) {
      if (signal && signal.aborted) return;   // a timed-out poll: keep what is shown, the next tick retries
      dsdListSig = null;  // an error pane is not a rendered list
      list.innerHTML =
        '<div class="gm-error">Could not load DSDs: ' + escapeHtml(e.message) + "</div>";
    }
    await loadNoticeWarning(signal);
  }

  // pre-review B2 (count only): dsd_id -> instance_id of the rows on screen, and the held-notice labels painted
  // beside each agent's name (only when /api/dsd/notices carries held_notices, i.e. dsd_notice_inline is ON).
  let dsdInstanceById = {};
  function paintHeldNotices(summary) {
    const list = $("#dsd-list");
    if (!list || !NC) return;
    list.querySelectorAll(".gm-dsd-item-held").forEach((el) => el.remove());      // stale labels never survive a poll
    const inst = summary && summary.available && summary.instances ? summary.instances : null;
    if (!inst) return;
    const done = {};                                                                 // one label per agent, on its first row
    list.querySelectorAll(".gm-dsd-item[data-dsd-id]").forEach((btn) => {
      const name = dsdInstanceById[btn.getAttribute("data-dsd-id")];
      if (!name || done[name]) return;
      const row = inst[name];
      const label = row ? NC.heldNoticeLabel(row.held_notices, row.held_notices_error) : "";
      const kind = btn.querySelector(".gm-dsd-item-kind");
      if (!label || !kind) return;
      done[name] = true;
      const span = document.createElement("span");
      span.className = "gm-dsd-item-held";
      span.textContent = "  (" + label + ")";
      span.title = row.held_notices_error ? "The count of notices held for " + name + " could not be read: " + row.held_notices_error
        : "Answer notices for " + name + " that its session has been holding for over 2 minutes";
      kind.appendChild(span);
    });
  }

  // quiet warning: the notice queue's expired / corrupt counts.
  async function loadNoticeWarning(signal) {
    const el = $("#dsd-notice-warn");
    if (!el || !NC) return;
    let text = "";
    try {
      const summary = await api("/api/dsd/notices", pollOpts(signal));
      text = NC.noticeWarning(summary);
      paintHeldNotices(summary);
    } catch (e) {
      if (signal && signal.aborted) return;   // a timed-out poll: keep what is shown, the next tick retries
      paintHeldNotices(null);                 // a failed poll must not leave a stale count on screen
      text = "";  // advisory only; never disrupt the list
    }
    el.textContent = text;
    el.hidden = !text;
  }

  // The browse pane's notice chip, updated in place (no re-render of the pane).
  function setDetailNoticeChip(dsdId, notice) {
    const holder = $("#dsd-detail-notice");
    if (!holder || holder.getAttribute("data-dsd-id") !== dsdId) return;
    holder.textContent = "";
    const chip = NC ? NC.dsdNoticeChip(notice) : null;
    if (chip) holder.appendChild(noticeChipEl(chip));
  }

  // Pending (always open) / Answered <24 hours Ago / Answered Previously
  // (one closed accordion per sender). Grouping + 24 h boundary are the server's
  // (answer-time keyed, re-evaluated per poll); open/closed is remembered per
  // browser (Pending ignores it).
  function renderDsdSections(list, rows) {
    const DS = window.GmDsdSections;
    if (!DS) { rows.forEach((it) => list.appendChild(dsdListItem(it))); return; }
    const state = DS.loadState();
    const accordion = (id, title, defaultOpen, cls) => {
      const d = document.createElement("details");
      d.className = "gm-dsd-acc " + cls;
      d.open = DS.isOpen(state, id, defaultOpen);
      d.setAttribute("data-acc-id", id);
      const sm = document.createElement("summary");
      sm.textContent = title;
      d.appendChild(sm);
      d.addEventListener("toggle", () => {
        const cur = DS.loadState();
        cur[id] = d.open;
        DS.saveState(cur);
      });
      return d;
    };
    const fill = (host, items) => {
      if (!items.length) {
        const e = document.createElement("div");
        e.className = "gm-empty gm-dsd-acc-empty";
        e.textContent = "None";
        host.appendChild(e);
      }
      items.forEach((it) => host.appendChild(dsdListItem(it)));
    };
    DS.buildSections(rows).forEach((sec) => {
      const title = DS.headerText(sec.label, sec.count);
      if (sec.alwaysOpen) {
        // Pending: a plain section, no <details>, no collapse control.
        const box = document.createElement("div");
        box.className = "gm-dsd-acc gm-dsd-acc-pending";
        box.setAttribute("data-acc-id", sec.id);
        const h = document.createElement("div");
        h.className = "gm-dsd-acc-head";
        h.textContent = title;
        box.appendChild(h);
        fill(box, sec.items);
        list.appendChild(box);
        return;
      }
      const acc = accordion(sec.id, title, sec.defaultOpen, "gm-dsd-acc-top");
      if (sec.id === "previous") {
        if (!sec.groups.length) fill(acc, []);
        sec.groups.forEach((g) => {
          const sub = accordion(g.id, DS.headerText(g.label, g.count), g.defaultOpen,
                                "gm-dsd-acc-sender");
          fill(sub, g.items);
          acc.appendChild(sub);
        });
      } else {
        fill(acc, sec.items);
      }
      list.appendChild(acc);
    });
  }

  function dsdListItem(it) {
    const btn = document.createElement("button");
    btn.className = "gm-dsd-item";
    btn.setAttribute("data-dsd-id", it.dsd_id);
    if (it.dsd_id === dsdSelected) btn.classList.add("is-selected");
    const kindLabel = { pending: "● pending", answered: "✓ answered",
                        unrenderable: "▲ non-form", withdrawn: "✕ withdrawn",
                        replaced: "↺ replaced" }[it.kind] || it.kind;
    btn.innerHTML =
      '<span class="gm-dsd-item-kind is-' + escapeHtml(it.kind) + '">' +
      escapeHtml(kindLabel) + "  " + escapeHtml(it.instance_id) + "</span>" +
      '<span class="gm-dsd-item-title">' + escapeHtml(it.title) + "</span>";
    const stamps = document.createElement("span");
    stamps.className = "gm-dsd-item-times";
    stamps.appendChild(dsdStampEl("sent", it.created_epoch, it.created_iso));
    if (it.kind === "answered")
      stamps.appendChild(dsdStampEl("answered", it.answered_epoch, it.answered_iso, it.answered_source));
    btn.appendChild(stamps);
    // answered-but-notice-still-queued (or delivered / expired) chip.
    const chip = NC ? NC.dsdNoticeChip(it.notice) : null;
    if (chip) {
      const wrap = document.createElement("span");
      wrap.className = "gm-dsd-item-notice";
      wrap.appendChild(noticeChipEl(chip));
      btn.appendChild(wrap);
    }
    btn.addEventListener("click", () => openDsd(it));
    return btn;
  }

  function openDsd(it) {
    dsdSelected = it.dsd_id;
    loadDsdList();  // re-render to move the selected highlight
    if (it.kind === "answered") {
      renderBrowse(it.dsd_id);
    } else if (it.kind === "pending") {
      renderPresent(it.dsd_id);
    } else if (it.kind === "withdrawn" || it.kind === "replaced") {
      // pre-review B1: the agent withdrew / replaced it; shown, never silently gone, and not open for an answer.
      const m = it.mark || {};
      renderDetailMessage(
        "DSD " + it.dsd_id + " was " + it.kind + " by " + it.instance_id +
        (m.reason ? " (" + m.reason + ")" : "") + (m.replaced_by ? "; the new one is " + m.replaced_by : "") +
        ". It is not waiting for an answer.");
    } else {
      renderDetailMessage(
        "DSD " + it.dsd_id + " carries a non-markdown payload and cannot be " +
        "rendered as a form. It was captured durably by the hub.");
    }
  }

  // --- open a DSD by id (dsd_id_lookup, default OFF) -----------------------------------
  // The box is built by script ONLY while the flag is on, so with the flag off the DSD tab's DOM is exactly
  // what it was. The id is cleaned by GmDsdIdLookup.normalise (pure, tested); the open reuses openDsd, i.e.
  // the very routes a list click uses (present for a pending DSD, browse for an answered one).
  const IDL = (typeof GmDsdIdLookup !== "undefined") ? GmDsdIdLookup : null;

  function dsdIdLookupMessage(text, bad, choices) {
    const m = $("#dsd-id-lookup-msg");
    if (!m) return;
    m.textContent = text || "";
    m.classList.toggle("is-bad", !!(text && bad));
    m.hidden = !text;
    (choices || []).forEach((id) => {   // clickable choices (a pasted text that names several DSDs)
      const b = document.createElement("button");
      b.className = "gm-btn gm-dsd-idlookup-choice";
      b.setAttribute("data-choice-id", id);
      b.textContent = id;
      b.addEventListener("click", () => { $("#dsd-id-lookup-input").value = id; openDsdById(); });
      m.appendChild(document.createTextNode(" "));
      m.appendChild(b);
    });
  }

  // Nothing was opened: the pane on the right must not keep showing a DSD that looks like the answer.
  function dsdIdLookupClearPane(text) {
    dsdSelected = null;
    renderDetailMessage(text, true);
    loadDsdList();   // moves the selected highlight off
  }

  // (g1 / g23): one beacon per use of the box, so the usage report can tell it from a list click. The
  // route is the existing web-api feature mechanism (feature_sites.WEB_ROUTES); a failure is never shown.
  // JSON body + content type: the route refuses anything else (and a foreign Origin). Awaited, so the server's
  // /api/dsd list cache is already invalidated when the list is fetched next; a failure is never shown.
  async function dsdIdLookupBeacon() {
    try {
      await fetch("/api/dsd/id-lookup", { method: "POST", keepalive: true,
        headers: { "Content-Type": "application/json" }, body: "{}" });
    } catch (e) { /* telemetry only */ }
  }

  async function openDsdById() {
    const input = $("#dsd-id-lookup-input");
    if (!input || !IDL) return;
    await dsdIdLookupBeacon();
    const n = IDL.normalise(input.value);
    if (!n.ok && n.ids && n.ids.length > 1) {
      dsdIdLookupMessage("This text names " + n.ids.length + " different DSDs. Pick one:", true, n.ids);
      dsdIdLookupClearPane("Nothing opened: the pasted text names " + n.ids.length + " different DSDs (" + n.ids.join(", ") + ").");
      return;
    }
    if (!n.ok) {
      const t = n.cleaned
        ? "No DSD id found in \"" + n.cleaned + "\". An id looks like dsd-1a2b3c4d5e4."
        : "Type or paste a DSD id, for example dsd-1a2b3c4d5e4.";
      dsdIdLookupMessage(t, true);
      dsdIdLookupClearPane("Nothing opened. " + t);
      return;
    }
    let rows;
    try {
      rows = await api("/api/dsd/list");
    } catch (e) {
      dsdIdLookupMessage("Could not look up " + n.id + ": " + e.message, true);
      dsdIdLookupClearPane("Nothing opened: could not look up " + n.id + ".");
      return;
    }
    const row = rows.find((it) => it.dsd_id === n.id);
    if (row) {
      dsdIdLookupMessage("", false);
      input.value = n.id;   // show the cleaned id
      openDsd(row);
      return;
    }
    // Not in the freshly fetched list: that does NOT mean it does not exist (a withdrawn / replaced / superseded /
    // unfinished record is hidden from the list). It must NEVER be opened as an answerable form: present() refuses
    // only the four exact status words, so a record hidden under another spelling ("withdrawn: <reason>") would be
    // served as a form. An unlisted id is not opened at all, not even read-only (X74 fix round 3): browse is asked only
    // to tell "exists but is not in the DSD list" from "not found".
    let browse = null;
    try { browse = await api("/api/dsd/browse/" + encodeURIComponent(n.id)); } catch (e) { browse = null; }
    if (browse && browse.ok) {
      // An unlisted id is NEVER opened, not even read-only: the list is the authority on what is answered, and an
      // "answered" view of a hidden record (browse serves any non-canonical status) would mislabel it.
      const why = n.id + " exists but is not in the DSD list, so it is not opened (it is hidden: " +
        "withdrawn, replaced, superseded or unfinished). Its status text is not available here.";
      dsdIdLookupMessage(why, true);
      dsdIdLookupClearPane(why);
      return;
    }
    if (browse && !/^no DSD record for /.test(browse.message || "")) {
      const why = n.id + " exists but is not open for an answer: " + (browse.message || "(no reason given)");
      dsdIdLookupMessage(why, true);
      dsdIdLookupClearPane(why);
      return;
    }
    const nf = "No DSD with the id " + n.id + " was found.";
    dsdIdLookupMessage(nf, true);
    dsdIdLookupClearPane(nf);
  }

  async function syncDsdIdLookup() {
    if (!IDL) return;
    let on = false;
    try {
      const data = await api("/api/flags");
      on = ((data && data.flags) || []).some((f) => f.name === "dsd_id_lookup" && f.value === true);
    } catch (e) { on = false; }
    const existing = $("#dsd-id-lookup");
    if (!on) { if (existing) existing.remove(); return; }
    if (existing) return;
    const head = document.querySelector("#panel-dsd .gm-dsd-left .gm-section-head");
    if (!head) return;
    const box = document.createElement("div");
    box.id = "dsd-id-lookup";
    box.className = "gm-dsd-idlookup";
    box.innerHTML =
      '<div class="gm-dsd-idlookup-row">' +
      '<input type="text" class="gm-config-input" id="dsd-id-lookup-input" autocomplete="off" ' +
      'spellcheck="false" placeholder="Open a DSD by id, e.g. dsd-1a2b3c4d5e4" aria-label="DSD id" />' +
      '<button class="gm-btn" id="dsd-id-lookup-open">Open</button></div>' +
      '<div class="gm-dsd-idlookup-msg" id="dsd-id-lookup-msg" hidden></div>';
    head.insertAdjacentElement("afterend", box);
    $("#dsd-id-lookup-open").addEventListener("click", openDsdById);
    $("#dsd-id-lookup-input").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") { ev.preventDefault(); openDsdById(); }
    });
  }

  function renderDetailMessage(text, bad) {
    leaveDsdForm();
    const d = $("#dsd-detail");
    d.innerHTML = "";
    const div = document.createElement("div");
    div.className = bad ? "gm-dsd-refused" : "gm-empty";
    div.textContent = text;
    d.appendChild(div);
  }

  async function renderPresent(dsdId) {
    leaveDsdForm();
    const d = $("#dsd-detail");
    d.innerHTML = '<div class="gm-empty">Loading…</div>';
    let view;
    try {
      view = await api("/api/dsd/present/" + encodeURIComponent(dsdId));
    } catch (e) {
      renderDetailMessage("Could not present DSD: " + e.message, true);
      return;
    }
    if (!view.ok || !view.form) {
      // guard-before-render: an empty/whitespace/context-less DSD lands here.
      renderDetailMessage(view.message, true);
      dsdForm = null;
      return;
    }
    dsdForm = view.form;
    d.innerHTML = "";

    const head = document.createElement("div");
    head.className = "gm-dsd-detail-head";
    head.innerHTML =
      "<h3>DSD for " + escapeHtml(view.form.instance_id) + "</h3>" +
      '<div class="gm-dsd-meta">' + escapeHtml(view.form.dsd_id) + " · " +
      view.form.decisions.length + " decision(s)</div>";
    head.appendChild(dsdTimesEl(view));
    d.appendChild(head);
    if (typeof GmDsdDocs !== "undefined") GmDsdDocs.mount(d, view.documents, view.dsd_id, { api, toast });  // GM-RM-X46

    let lastTheme = null;
    view.form.decisions.forEach((dec) => {
      if (dec.theme && dec.theme !== lastTheme) {
        const t = document.createElement("div");
        t.className = "gm-dsd-theme";
        t.textContent = "— " + dec.theme + " —";
        d.appendChild(t);
        lastTheme = dec.theme;
      }
      d.appendChild(decisionCard(dec));
    });

    const gate = document.createElement("div");
    gate.className = "gm-dsd-gate";
    gate.id = "dsd-gate";
    d.appendChild(gate);

    const warn = document.createElement("div");
    warn.className = "gm-dsd-draft-warn";
    warn.id = "dsd-draft-warn";
    warn.setAttribute("role", "alert");
    d.appendChild(warn);

    const save = document.createElement("button");
    save.className = "gm-btn gm-btn-primary";
    save.id = "dsd-save";
    save.textContent = "Save Answers";
    save.addEventListener("click", () => submitDsd(dsdId));
    d.appendChild(save);

    // Restore this DSD's saved draft (if any) over the unset form.
    const draft = dsdDrafts.load(view.form.dsd_id);
    if (draft && DsdDraft) {
      applyDsdSelections(DsdDraft.restoreSelections(view.form, draft));
      const note = document.createElement("div");
      note.className = "gm-dsd-draft-note";
      note.textContent = "Restored your unsaved answers for this DSD" +
        (draft.saved_at ? " (draft from " + new Date(draft.saved_at).toLocaleString() + ")" : "") +
        ".";
      head.appendChild(note);
    }

    reevaluateGate();
  }

  function decisionCard(dec) {
    const card = document.createElement("div");
    card.className = "gm-dsd-card";
    card.dataset.decId = dec.id;

    const q = document.createElement("div");
    q.className = "gm-dsd-q";
    q.textContent = (dec.blocker ? "⛔ " : "") + dec.id + " · " + dec.question;
    card.appendChild(q);

    const wti = document.createElement("div");
    wti.className = "gm-dsd-ctx";
    wti.textContent = "What this is: " + dec.what_this_is;
    card.appendChild(wti);
    const wim = document.createElement("div");
    wim.className = "gm-dsd-ctx";
    wim.textContent = "Why it matters: " + dec.why_it_matters;
    card.appendChild(wim);
    if (typeof GmDsdDocs !== "undefined") GmDsdDocs.mount(card, dec.documents, dsdForm && dsdForm.dsd_id, { api, toast });  // GM-RM-X46

    if (dec.multi) {
      const m = document.createElement("div");
      m.className = "gm-dsd-multi-note";
      m.textContent = "[multi-select parsed but GATED to single-select in v1]";
      card.appendChild(m);
    }

    dec.options.forEach((opt) => {
      const row = document.createElement("label");
      row.className = "gm-dsd-opt";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "dsd-" + dec.id;
      radio.value = opt.option_id;
      // NO pre-selection (operator, 2026-09-29): every radio starts unset, so assent
      // to the recommendation is an explicit, captured choice. The legacy
      // is_preselected flag is deliberately ignored.
      radio.checked = false;
      radio.addEventListener("change", onDsdFormChange);
      row.appendChild(radio);

      const lab = document.createElement("span");
      lab.className = "gm-dsd-opt-label";
      lab.textContent = opt.label;
      if (opt.tradeoffs) {
        const tr = document.createElement("span");
        tr.className = "gm-dsd-opt-trade";
        tr.textContent = "  —  " + opt.tradeoffs;
        lab.appendChild(tr);
      }
      row.appendChild(lab);

      // Badge only (never a selection). is_preselected is honoured HERE alone, as
      // the badge source a pre-2026-09-29 server still sends (a reloaded page
      // against a not-yet-restarted :8770); the new server always sends false.
      if (opt.is_recommended || opt.is_preselected) {
        const chip = document.createElement("span");
        chip.className = "gm-dsd-chip";
        chip.textContent = "recommended";
        row.appendChild(chip);
      }
      card.appendChild(row);
    });

    if (!dec.has_recommendation) {
      const nr = document.createElement("div");
      nr.className = "gm-dsd-noreco";
      nr.textContent = "(no recommendation in this DSD — choose explicitly)";
      card.appendChild(nr);
    }

    const cl = document.createElement("div");
    cl.className = "gm-dsd-comment-label";
    cl.textContent = "Comment:";
    card.appendChild(cl);
    const box = document.createElement("textarea");
    box.className = "gm-dsd-comment";
    box.dataset.decId = dec.id;
    box.addEventListener("input", onDsdFormChange);
    card.appendChild(box);

    return card;
  }

  function currentSelections() {
    // { dec_id: {option_id, comment} } — mirrors the CTk _current_selections.
    const sel = {};
    if (!dsdForm) return sel;
    dsdForm.decisions.forEach((dec) => {
      const radio = document.querySelector(
        'input[name="dsd-' + cssEscape(dec.id) + '"]:checked');
      const box = document.querySelector(
        'textarea.gm-dsd-comment[data-dec-id="' + cssEscape(dec.id) + '"]');
      sel[dec.id] = {
        option_id: radio ? radio.value : null,
        comment: box ? box.value.trim() : "",
      };
    });
    return sel;
  }

  // A local completeness MIRROR of the server rule (server is the authority):
  // every decision needs an EXPLICIT choice (nothing is pre-selected, so the
  // recommendation is never counted); the escape hatch requires a comment. Used
  // only to toggle the Save button + hint — it never decides the save. The rule
  // itself lives in dsd_draft.js (node-tested); the inline copy is the fallback
  // when that module failed to load.
  function localCompleteness(sel) {
    if (DsdDraft) return DsdDraft.completeness(dsdForm, sel);
    const reasons = [];
    if (!dsdForm) return { ok: false, reasons };
    dsdForm.decisions.forEach((dec) => {
      const s = sel[dec.id] || {};
      if (!s.option_id) {
        reasons.push(dec.id + ": no option selected");
        return;
      }
      if (s.option_id === dec.escape_hatch_id && !(s.comment || "").trim()) {
        reasons.push(dec.id + ": the escape hatch requires a comment");
      }
    });
    return { ok: reasons.length === 0, reasons };
  }

  function reevaluateGate() {
    const sel = currentSelections();
    const { ok, reasons } = localCompleteness(sel);
    const save = $("#dsd-save");
    const gate = $("#dsd-gate");
    if (save) save.disabled = !ok;
    if (gate) gate.textContent = ok ? "" : "Save disabled — " + reasons.slice(0, 4).join("; ");
  }

  // the operator's real in-tab input (never content), sent with every submit.
  const dsdInput = window.GmDsdInputActivity ? window.GmDsdInputActivity.createTracker() : null;
  if (dsdInput) {
    const panel = document.getElementById("panel-dsd");
    if (panel) window.GmDsdInputActivity.attach(dsdInput, panel);
  }

  async function submitDsd(dsdId) {
    const save = $("#dsd-save");
    if (save) save.disabled = true;
    let result;
    try {
      result = await api("/api/dsd/submit/" + encodeURIComponent(dsdId), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ selections: currentSelections(),
          activity: dsdInput ? dsdInput.summary(Date.now()) : null }),
      });
    } catch (e) {
      renderDetailMessage("Save failed: " + e.message +
        "\nYour answers are kept as a draft — reopen this DSD to continue.", true);
      return;
    }
    loadDsdList();
    loadDsdBanner();
    if (result.ok) {
      dsdDrafts.clear(dsdId);   // answered: the draft has nothing left to protect
      dsdForm = null;           // so the render below does not re-save it
      const injectBit = result.injected
        ? "root " + result.root_node_id + " injected to the agent."
        : "agent not live — inject skipped; it will pull " + result.root_node_id +
          " from the graph.";
      let extra = "";
      if (result.live && result.live.live === false) extra = "\n⚠ " + result.live.banner;
      renderDetailMessage(
        "Answer saved + queryable. Minted " + result.minted_node_ids.length +
        " node(s); " + injectBit + extra);
      toast("DSD answer saved for " + dsdId);
    } else {
      // ⛔ SERVER-SIDE gate refusal (completeness / escape-hatch / single-select).
      const failures = (result.gate_failures || []).join("\n");
      renderDetailMessage(
        "Save refused:\n" + result.message + (failures ? "\n" + failures : "") +
        "\nYour answers are kept as a draft — reopen this DSD to continue.", true);
    }
  }

  async function renderBrowse(dsdId) {
    leaveDsdForm();
    const d = $("#dsd-detail");
    d.innerHTML = '<div class="gm-empty">Loading…</div>';
    let view;
    try {
      view = await api("/api/dsd/browse/" + encodeURIComponent(dsdId));
    } catch (e) {
      renderDetailMessage("Could not browse DSD: " + e.message, true);
      return;
    }
    if (!view.ok) {
      renderDetailMessage(view.message, true);
      return;
    }
    d.innerHTML = "";
    const head = document.createElement("div");
    head.className = "gm-dsd-detail-head";
    head.innerHTML =
      "<h3>Answered DSD — " + escapeHtml(view.instance_id) + " (read-only)</h3>" +
      '<div class="gm-dsd-meta">root node: ' +
      escapeHtml(view.root_node_id || "(none)") + "</div>";
    head.appendChild(dsdTimesEl(view));
    const noticeHolder = document.createElement("div");
    noticeHolder.id = "dsd-detail-notice";
    noticeHolder.className = "gm-dsd-detail-notice";
    noticeHolder.setAttribute("data-dsd-id", view.dsd_id);
    head.appendChild(noticeHolder);
    d.appendChild(head);
    setDetailNoticeChip(view.dsd_id, view.notice);
    if (typeof GmDsdDocs !== "undefined") GmDsdDocs.mount(d, view.documents, view.dsd_id, { api, toast }, { showScope: true });  // GM-RM-X46

    const ss = view.supersession;
    if (ss) {
      const sdiv = document.createElement("div");
      if (ss.superseded) {
        sdiv.className = "gm-dsd-supersession is-superseded";
        sdiv.innerHTML =
          "⚠ SUPERSEDED by " + escapeHtml(ss.referrers.join(", ")) +
          "  [" + escapeHtml(ss.confidence) + "]";
      } else {
        sdiv.className = "gm-dsd-supersession is-current";
        sdiv.innerHTML =
          "current — no overrule found  [" + escapeHtml(ss.confidence) + " · " +
          escapeHtml(ss.mechanism) + "]";
      }
      const cav = document.createElement("span");
      cav.className = "gm-dsd-caveat";
      cav.textContent = "caveat: " + ss.note;
      sdiv.appendChild(cav);
      d.appendChild(sdiv);
    }

    view.answers.forEach((a) => {
      const card = document.createElement("div");
      card.className = "gm-dsd-answer";
      const q = document.createElement("div");
      q.className = "gm-dsd-q";
      q.textContent = a.decision_id + " · " + a.question;
      card.appendChild(q);
      const yc = document.createElement("div");
      yc.className = "gm-dsd-yourcall";
      yc.textContent = "Your call: " + (a.your_call || "(unanswered)");
      card.appendChild(yc);
      if (a.comment) {
        const c = document.createElement("div");
        c.className = "gm-dsd-answer-comment";
        c.textContent = "Comment: " + a.comment;
        card.appendChild(c);
      }
      d.appendChild(card);
    });
  }

  // auto-switch on a newly-ARRIVED pending DSD (operator-requested; server primes the
  // watcher so DSDs already on disk when the page opens do not trigger a switch).
  const DSD_ARRIVAL_POLL_MS = 3000;
  async function pollDsdArrivals(signal) {
    let data;
    try {
      data = await api("/api/dsd/arrivals", pollOpts(signal));
    } catch (e) {
      return;  // a poll hiccup must never disrupt the page
    }
    if (!data.arrivals || !data.arrivals.length) return;
    // Never hijack a form in progress: the decision (none / hold / switch) is
    // DsdDraft.decideArrival, node-tested. With operator input on the presented
    // form the arrival is HELD behind a banner; otherwise today's operator-requested
    // auto-switch. (No module => any presented form is held, never wiped.)
    const decision = DsdDraft
      ? DsdDraft.decideArrival(data.arrivals, dsdForm, currentDraftSelections())
      : { action: dsdForm ? "hold" : "switch", arrival: data.arrivals[0] };
    loadDsdBanner();
    if (decision.action === "hold") {
      loadDsdList();             // the arrival shows in the list; the form is untouched
      showDsdArrivalBanner(data.arrivals);
      return;
    }
    if (decision.action !== "switch") return;
    hideDsdArrivalBanner();
    activateTab("dsd");          // focus the DSD tab
    loadDsdList();
    openDsd(decision.arrival);   // select + present the arrival
  }

  function cssEscape(s) {
    if (window.CSS && CSS.escape) return CSS.escape(s);
    return String(s).replace(/["\\\]]/g, "\\$&");
  }

  // --- Config tab (slice 4) -------------------------------------
  //
  // Thin browser client over the reused ConfigController behind /api/config. All
  // validation (field parsing, the non-empty MCP set, the D9 measurement-server refusal)
  // is enforced SERVER-SIDE; a refused Save surfaces the server's message. The
  // page never decides whether a config is valid.

  // Client-side editing state for the master-detail Config tab. Every field's
  // current string value lives in cfgState.values keyed by FieldSpec.key (incl.
  // dotted stall_inject.<sub> keys). Switching groups re-renders the detail pane
  // from this state, so edits in one group survive visiting another; Save posts
  // the WHOLE state through /api/config (server is the only validator).
  const cfgState = {
    fields: [],          // [{key,label,kind,help,unit,value,reserved}]
    fieldByKey: {},      // key -> field
    values: {},          // key -> current string value being edited
    groups: [],          // [{key,title,field_keys,surface?}]
    templates: {},       // {name: command-template} being edited
    endpoints: {},       // read-only mcp endpoints
    selected: null,      // selected group key
  };

  async function loadConfig() {
    const note = $("#config-note");
    try {
      const data = await api("/api/config");
      note.textContent =
        "Machine-specific settings so one build runs on any machine. Persisted to " +
        data.store_path + (data.config_exists ? "" : " (no file yet — showing defaults)") +
        ". Root changes apply on next app start; other changes on the next launch.";
      cfgState.fields = data.fields || [];
      cfgState.fieldByKey = {};
      cfgState.values = {};
      cfgState.fields.forEach((f) => {
        cfgState.fieldByKey[f.key] = f;
        cfgState.values[f.key] = f.value;
      });
      cfgState.groups = (data.field_groups || []).slice();
      // a read-only "Feature flags" group, rendered from /api/flags.
      cfgState.groups.push({ key: "flags", title: "Feature flags", field_keys: [], surface: "flags" });
      // a read-only "Verb policy" group, rendered from /api/verb-policy.
      cfgState.groups.push({ key: "verb_policy", title: "Verb policy", field_keys: [], surface: "verb_policy" });
      cfgState.templates = Object.assign({}, data.custom_terminal_templates || {});
      cfgState.endpoints = data.mcp_endpoints || {};
      if (!cfgState.selected || !cfgState.groups.some((g) => g.key === cfgState.selected)) {
        cfgState.selected = cfgState.groups.length ? cfgState.groups[0].key : null;
      }
      renderConfigGroups();
      renderConfigDetail();
      $("#config-status").textContent = "";
    } catch (e) {
      $("#config-groups").innerHTML =
        '<div class="gm-error">Could not load configuration: ' + escapeHtml(e.message) + "</div>";
      $("#config-detail").innerHTML = "";
    }
  }

  function renderConfigGroups() {
    const box = $("#config-groups");
    box.innerHTML = "";
    if (!cfgState.groups.length) {
      box.innerHTML = '<div class="gm-empty">(no settings groups)</div>';
      return;
    }
    cfgState.groups.forEach((g) => {
      const item = document.createElement("button");
      item.className = "gm-gv-item gm-config-group";
      if (g.key === cfgState.selected) item.classList.add("is-selected");
      item.dataset.group = g.key;
      const title = document.createElement("span");
      title.className = "gm-gv-item-title";
      title.textContent = g.title;
      item.appendChild(title);
      item.addEventListener("click", () => {
        cfgState.selected = g.key;
        renderConfigGroups();
        renderConfigDetail();
      });
      box.appendChild(item);
    });
  }

  // Render one control for a field into `parent`, wired to cfgState.values.
  function renderConfigField(parent, key) {
    const f = cfgState.fieldByKey[key];
    if (!f) return null;
    const wrap = document.createElement("div");
    wrap.className = "gm-config-field";
    wrap.dataset.fieldWrap = f.key;

    const lab = document.createElement("label");
    lab.className = "gm-config-label";
    lab.setAttribute("for", "cfg-" + f.key);
    lab.textContent = f.label;
    if (f.unit) {
      const u = document.createElement("span");
      u.className = "gm-config-unit";
      u.textContent = f.unit;
      lab.appendChild(u);
    }
    if (f.reserved) {
      const r = document.createElement("span");
      r.className = "gm-config-reserved";
      r.textContent = "no live consumer";
      lab.appendChild(r);
    }
    wrap.appendChild(lab);

    let control;
    if (f.kind === "bool") {
      // macOS-style toggle (checkbox + track). Serializes to "true"/"false".
      const tog = document.createElement("label");
      tog.className = "gm-toggle";
      control = document.createElement("input");
      control.type = "checkbox";
      control.checked = String(cfgState.values[f.key]).toLowerCase() === "true";
      const track = document.createElement("span");
      track.className = "gm-toggle-track";
      tog.appendChild(control);
      tog.appendChild(track);
      control.dataset.key = f.key;
      control.id = "cfg-" + f.key;
      control.addEventListener("change", () => {
        cfgState.values[f.key] = control.checked ? "true" : "false";
        markUnsaved();
        if (f.key === "stall_inject.enabled") applyStallEnabled();
      });
      wrap.appendChild(tog);
    } else if (f.kind === "multiline") {
      control = document.createElement("textarea");
      control.className = "gm-config-input gm-config-textarea";
      control.id = "cfg-" + f.key;
      control.dataset.key = f.key;
      control.rows = 4;
      control.value = cfgState.values[f.key] || "";
      control.addEventListener("input", () => {
        cfgState.values[f.key] = control.value;
        markUnsaved();
      });
      wrap.appendChild(control);
    } else {
      control = document.createElement("input");
      control.type = f.kind === "int" ? "number" : "text";
      control.className = "gm-config-input";
      control.id = "cfg-" + f.key;
      control.dataset.key = f.key;
      control.value = cfgState.values[f.key] || "";
      control.addEventListener("input", () => {
        cfgState.values[f.key] = control.value;
        markUnsaved();
      });
      wrap.appendChild(control);
    }

    if (f.help) {
      const h = document.createElement("div");
      h.className = "gm-config-help";
      h.textContent = f.help;
      wrap.appendChild(h);
    }
    parent.appendChild(wrap);
    return wrap;
  }

  function renderConfigDetail() {
    const pane = $("#config-detail");
    pane.innerHTML = "";
    const group = cfgState.groups.find((g) => g.key === cfgState.selected);
    if (!group) {
      pane.innerHTML = '<div class="gm-empty">Select a settings group on the left.</div>';
      return;
    }
    const head = document.createElement("div");
    head.className = "gm-gv-head";
    const h3 = document.createElement("h3");
    h3.textContent = group.title;
    head.appendChild(h3);
    pane.appendChild(head);

    (group.field_keys || []).forEach((key) => renderConfigField(pane, key));

    if (group.surface === "terminal_templates") renderTemplatesSurface(pane);
    if (group.surface === "mcp_endpoints") renderEndpointsSurface(pane);
    if (group.surface === "flags") renderFlagsSurface(pane);
    if (group.surface === "verb_policy") renderVerbPolicySurface(pane);

    // Stall inject: the Enabled toggle greys the rest of the pane when off.
    if (group.key === "stall") applyStallEnabled();
  }

  // Grey + disable every stall control except the Enabled toggle when off.
  function applyStallEnabled() {
    const pane = $("#config-detail");
    if (!pane) return;
    const on = String(cfgState.values["stall_inject.enabled"]).toLowerCase() === "true";
    pane.querySelectorAll("[data-field-wrap]").forEach((wrap) => {
      if (wrap.dataset.fieldWrap === "stall_inject.enabled") return;
      wrap.classList.toggle("is-disabled", !on);
      wrap.querySelectorAll("input, textarea").forEach((c) => { c.disabled = !on; });
    });
  }

  function renderTemplatesSurface(pane) {
    const tplHead = document.createElement("div");
    tplHead.className = "gm-config-subhead";
    tplHead.textContent = "Custom terminal templates";
    pane.appendChild(tplHead);

    const tplBox = document.createElement("div");
    tplBox.className = "gm-config-templates";
    tplBox.id = "config-templates";
    pane.appendChild(tplBox);
    renderConfigTemplates();

    const addbar = document.createElement("div");
    addbar.className = "gm-config-addbar";
    const nameIn = document.createElement("input");
    nameIn.type = "text";
    nameIn.className = "gm-config-input gm-config-tpl-name";
    nameIn.id = "config-tpl-name";
    nameIn.placeholder = "name (e.g. kitty)";
    const cmdIn = document.createElement("input");
    cmdIn.type = "text";
    cmdIn.className = "gm-config-input gm-config-tpl-cmd";
    cmdIn.id = "config-tpl-cmd";
    cmdIn.placeholder = "command template, e.g. kitty @ launch --cwd {session_dir} sh -c '{inner_command}'";
    const addBtn = document.createElement("button");
    addBtn.className = "gm-btn";
    addBtn.textContent = "Add";
    addBtn.addEventListener("click", () => {
      const name = nameIn.value.trim();
      const cmd = cmdIn.value.trim();
      if (!name || !cmd) {
        toast("A custom template needs both a name and a command.", true);
        return;
      }
      cfgState.templates[name] = cmd;
      nameIn.value = "";
      cmdIn.value = "";
      renderConfigTemplates();
      $("#config-status").textContent = "template added (unsaved)";
    });
    addbar.appendChild(nameIn);
    addbar.appendChild(cmdIn);
    addbar.appendChild(addBtn);
    pane.appendChild(addbar);
  }

  function renderEndpointsSurface(pane) {
    const eps = cfgState.endpoints || {};
    const epHead = document.createElement("div");
    epHead.className = "gm-config-subhead";
    epHead.textContent = "MCP endpoints (read-only — wired at launch)";
    pane.appendChild(epHead);
    const epBox = document.createElement("div");
    epBox.className = "gm-config-endpoints";
    const names = Object.keys(eps);
    if (!names.length) {
      epBox.innerHTML = '<div class="gm-empty">(none)</div>';
    } else {
      names.forEach((name) => {
        const row = document.createElement("div");
        row.className = "gm-config-endpoint";
        row.textContent = name + " : " + JSON.stringify(eps[name]);
        epBox.appendChild(row);
      });
    }
    pane.appendChild(epBox);
  }

  // the feature flags, READ-ONLY (set flags in the machine config).
  // Every cell is built with textContent — flag data never reaches innerHTML.
  async function renderFlagsSurface(pane) {
    const head = document.createElement("div");
    head.className = "gm-config-subhead";
    head.textContent = "Feature flags (read-only — set flags in the machine config; no-op hook reporting also has a toggle under Harness / advanced)";
    pane.appendChild(head);
    const box = document.createElement("div");
    box.className = "gm-config-endpoints gm-flags-box";
    pane.appendChild(box);
    let data;
    try {
      data = await api("/api/flags");
    } catch (e) {
      const err = document.createElement("div");
      err.className = "gm-error";
      err.textContent = "Could not load feature flags: " + e.message;
      box.appendChild(err);
      return;
    }
    const rows = (data && data.flags) || [];
    if (!rows.length) {
      box.innerHTML = '<div class="gm-empty">(no flags)</div>';
      return;
    }
    const table = document.createElement("table");
    table.className = "gm-flags-table";
    const thead = document.createElement("tr");
    // the column order is exact (operator 2026-09-30).
    ["Flag", "What it gates", "Default", "Source", "State"].forEach((t) => {
      const th = document.createElement("th");
      th.textContent = t;
      thead.appendChild(th);
    });
    table.appendChild(thead);
    rows.forEach((f) => {
      const tr = document.createElement("tr");
      tr.className = f.value ? "is-on" : "is-off";
      [f.name, f.description + (f.known ? "" : " (not in the registry)"),
       f.default ? "on" : "off", f.source, f.value ? "on" : "off"].forEach((v, i) => {
        const td = document.createElement("td");
        td.textContent = String(v);
        if (i === 0) td.className = "gm-flag-name";
        if (i === 4) td.className = "gm-flag-state";
        tr.appendChild(td);
      });
      table.appendChild(tr);
    });
    box.appendChild(table);
  }

  // the effective verb policy, READ-ONLY. Served by the loader's own merge
  // (/api/verb-policy); every cell is textContent, and there is no input or save control.
  async function renderVerbPolicySurface(pane) {
    const mk = (tag, cls, text) => {
      const e = document.createElement(tag);
      if (cls) e.className = cls;
      if (text !== undefined) e.textContent = text;
      return e;
    };
    pane.appendChild(mk("div", "gm-config-subhead",
      "Verb policy (read-only — edit the TOML files on the machine, not here)"));
    const box = mk("div", "gm-config-endpoints gm-vp-box");
    pane.appendChild(box);
    let d;
    try {
      d = await api("/api/verb-policy");
    } catch (e) {
      box.appendChild(mk("div", "gm-error", "Could not load the verb policy: " + e.message));
      return;
    }
    if (d.invalid) {
      box.appendChild(mk("div", "gm-error",
        "POLICY INVALID: every governed verb is DENIED until the override is fixed. " + (d.invalid_reason || "")));
    }
    ["default", "override"].forEach((k) => {
      const f = d.files[k];
      if (f && f.symlink) box.appendChild(mk("div", "gm-error gm-vp-symlink",
        "SYMLINK: the " + k + " policy file " + f.path + " is a symlink to " + f.resolved));
    });
    (d.errors || []).forEach((m) => box.appendChild(mk("div", "gm-error", m)));
    (d.narrowing || []).forEach((m) => box.appendChild(mk("div", "gm-notice-warn", "narrowing override: " + m)));
    const list = (a) => (a && a.length ? a.join(", ") : "(none)");
    // F6 (X39 r2): render EVERY rule key the payload carries (a new verb_policy.Rule field shows
    // up without an edit here): known fields first in their historical order, then the rest
    // alphabetically. Keys that are not rule fields (card-level) are skipped. textContent only.
    const RULE_KNOWN = ["always", "others", "when", "require_ack", "alert_to"];
    const NOT_RULE = ["source", "source_path", "targets", "warning", "confirm_window_seconds"];
    const fmt = (x) => (Array.isArray(x) ? list(x)
      : (x !== null && typeof x === "object") ? JSON.stringify(x) : String(x));
    const ruleLines = (r) => {
      const rest = Object.keys(r).filter((k) => RULE_KNOWN.indexOf(k) < 0 && NOT_RULE.indexOf(k) < 0).sort();
      return RULE_KNOWN.filter((k) => k in r).concat(rest).map((k) => k + ": " + fmt(r[k]));
    };
    const names = Object.keys(d.verbs || {});
    if (!names.length) box.appendChild(mk("div", d.invalid ? "gm-error" : "gm-empty", d.invalid
      ? "No verb is usable: every governed verb is DENIED (policy invalid, fail-closed)."
      : "(no verbs in the effective policy)"));
    names.forEach((name) => {
      const v = d.verbs[name];
      const card = mk("div", "gm-vp-verb");
      const h = mk("div", "gm-vp-verb-head");
      h.appendChild(mk("span", "gm-vp-verb-name", name));
      h.appendChild(mk("span", "gm-vp-src is-" + v.source, v.source === "override" ? "machine override" : "shipped default"));
      card.appendChild(h);
      ruleLines(v).concat(["confirm window: " + v.confirm_window_seconds + " s"]).forEach((l) =>
        card.appendChild(mk("div", "gm-vp-line", l)));
      if (v.warning) card.appendChild(mk("div", "gm-vp-line", "warning: " + v.warning));
      Object.keys(v.targets || {}).forEach((t) => {
        card.appendChild(mk("div", "gm-vp-target", "target " + t + " — " + ruleLines(v.targets[t]).join("; ")));
      });
      box.appendChild(card);
    });
    const raw = (label, f) => {
      const det = mk("details", "gm-vp-raw");
      det.appendChild(mk("summary", "", label + " — " + f.path));
      det.appendChild(mk("pre", "gm-vp-pre",
        f.present ? f.text : (f.error ? "(unreadable: " + f.error + ")" : "none")));
      box.appendChild(det);
    };
    raw("Shipped default", d.files.default);
    raw("Machine override", d.files.override);
  }

  function markUnsaved() {
    $("#config-status").textContent = "unsaved changes";
  }

  function renderConfigTemplates() {
    const box = $("#config-templates");
    if (!box) return;
    box.innerHTML = "";
    const names = Object.keys(cfgState.templates).sort();
    if (!names.length) {
      box.innerHTML =
        '<div class="gm-empty">(none — built-ins iterm2 / terminal_app always available)</div>';
      return;
    }
    names.forEach((name) => {
      const row = document.createElement("div");
      row.className = "gm-config-template";
      const txt = document.createElement("span");
      txt.className = "gm-config-template-text";
      txt.textContent = name + " : " + cfgState.templates[name];
      row.appendChild(txt);
      const rm = document.createElement("button");
      rm.className = "gm-btn gm-config-tpl-remove";
      rm.textContent = "Remove";
      rm.addEventListener("click", () => {
        delete cfgState.templates[name];
        renderConfigTemplates();
        $("#config-status").textContent = "removed " + name + " (unsaved)";
      });
      row.appendChild(rm);
      box.appendChild(row);
    });
  }

  async function saveConfig() {
    const btn = $("#config-save");
    // Post the WHOLE edited state (all field keys, incl. dotted stall sub-fields)
    // + the desired templates map. The server validates every field and the D9
    // guard; the browser is never the gate.
    const fields = Object.assign({}, cfgState.values);
    btn.disabled = true;
    try {
      const r = await api("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ fields: fields, custom_terminal_templates: cfgState.templates }),
      });
      $("#config-status").textContent = "saved → " + r.saved_to;
      toast("Configuration saved.");
      loadConfig();
    } catch (e) {
      // ⛔ SERVER-SIDE refusal (validation / D9 measurement-server refusal) — surface it, save nothing.
      $("#config-status").textContent = "not saved";
      toast("Configuration not saved: " + e.message, true);
    } finally {
      btn.disabled = false;
    }
  }

  // --- Graph viewer: corpus tabs (declared by the deployment) + PROCs tab (master / detail) ---
  //
  // Read-only windows onto the LIVE decision graph. The backend queries the
  // in-process lts-engine store (LtsEngine.find / .node); this code only groups
  // the master list and reformats a node's properties + body into readable HTML.
  // Loaded lazily on first tab open (the first backend call builds the store).

  // gv[<tab id>] holds one tab's state. A corpus tab (declared in defaults/corpus-tabs.json, built at boot by
  // buildCorpusTabs) carries its declaration in `corpusTab`; the PROCs tab is built in.
  const gv = {
    procs: { loaded: false, data: null, selected: null, known: {} },
  };

  // [ID] references inside property values / body text become links when the
  // id is a row in the current tab, and a plain id chip otherwise.
  const REF_RE = /\[([A-Z][A-Z0-9_-]*)\]/g;

  function humanKey(k) {
    const s = String(k).replace(/-/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  // escape, then lift `code` spans (sans font — no monospace) and [ID] refs.
  function richText(text, tab) {
    let h = escapeHtml(text);
    h = h.replace(/`([^`]+)`/g, '<code class="gm-gv-code">$1</code>');
    h = h.replace(REF_RE, (m, id) => refChip(id, tab));
    return h;
  }

  function refChip(id, tab) {
    const known = tab && gv[tab] && gv[tab].known[id];
    if (known) {
      return '<a href="#" class="gm-gv-ref is-link" data-tab-ref="' + tab +
        '" data-ref="' + escapeHtml(id) + '">' + escapeHtml(id) + "</a>";
    }
    return '<span class="gm-gv-ref">' + escapeHtml(id) + "</span>";
  }

  function wireRefs(root) {
    root.querySelectorAll("a.gm-gv-ref.is-link").forEach((a) => {
      a.addEventListener("click", (ev) => {
        ev.preventDefault();
        const tab = a.getAttribute("data-tab-ref");
        const id = a.getAttribute("data-ref");
        if (gv[tab] && gv[tab].corpusTab) openCorpusNode(tab, id);
        else if (tab === "procs") {
          const k = gv.procs.known[id];
          if (k) openProc(k.corpus, id);
        }
      });
    });
  }

  function gvBanner(el, msg, warn) {
    el.textContent = msg || "";
    el.classList.toggle("is-warn", !!warn);
  }

  function gvItem(title, id, selected, onClick, extraClass) {
    const btn = document.createElement("button");
    btn.className = "gm-gv-item" + (extraClass ? " " + extraClass : "");
    if (selected) btn.classList.add("is-selected");
    btn.dataset.id = id;
    btn.innerHTML =
      '<span class="gm-gv-item-title">' + escapeHtml(title) + "</span>" +
      '<span class="gm-gv-item-id">' + escapeHtml(id) + "</span>";
    btn.addEventListener("click", onClick);
    return btn;
  }

  function gvGroup(label, note, count) {
    const g = document.createElement("div");
    g.className = "gm-gv-group";
    g.innerHTML =
      '<div class="gm-gv-group-label">' + escapeHtml(label) +
      (count !== undefined ? ' <span class="gm-gv-count">' + count + "</span>" : "") +
      "</div>" +
      (note ? '<div class="gm-gv-group-note">' + escapeHtml(note) + "</div>" : "");
    return g;
  }

  function markSelected(listEl, id) {
    listEl.querySelectorAll(".gm-gv-item").forEach((b) => {
      b.classList.toggle("is-selected", b.dataset.id === id);
    });
  }

  // a property value (str or list of str) -> HTML
  function propValueHtml(v, tab) {
    if (Array.isArray(v)) {
      return "<ul class=\"gm-gv-bullets\">" +
        v.map((x) => "<li>" + richText(x, tab) + "</li>").join("") + "</ul>";
    }
    return '<div class="gm-gv-val">' + richText(v, tab) + "</div>";
  }

  function propsBlock(rows, tab, heading) {
    if (!rows.length) return null;
    const wrap = document.createElement("div");
    wrap.className = "gm-gv-section";
    let h = heading ? '<div class="gm-gv-subhead">' + escapeHtml(heading) + "</div>" : "";
    h += '<dl class="gm-gv-props">';
    rows.forEach((p) => {
      h += "<dt>" + escapeHtml(humanKey(p.key)) + "</dt><dd>" +
        propValueHtml(p.value, tab) + "</dd>";
    });
    h += "</dl>";
    wrap.innerHTML = h;
    return wrap;
  }

  // Source .lts prose is hard-wrapped; join a single newline inside a paragraph,
  // but keep blank lines and lines that start a list / table / heading / quote.
  function reflow(text) {
    return String(text).replace(/([^\n])\n(?![\n\-*|#>]|\d+[.)] )/g, "$1 ");
  }

  function bodyBlock(body, tab, heading) {
    if (!body) return null;
    const wrap = document.createElement("div");
    wrap.className = "gm-gv-section";
    wrap.innerHTML =
      (heading ? '<div class="gm-gv-subhead">' + escapeHtml(heading) + "</div>" : "") +
      '<div class="gm-gv-body">' + richText(reflow(body), tab) + "</div>";
    return wrap;
  }

  function edgesBlock(edges, tab) {
    const layers = Object.keys(edges || {});
    if (!layers.length) return null;
    const wrap = document.createElement("div");
    wrap.className = "gm-gv-section";
    let h = '<div class="gm-gv-subhead">Graph edges</div>';
    layers.forEach((layer) => {
      const e = edges[layer] || {};
      [["forward", "points to"], ["reverse", "referenced by"]].forEach(([dir, label]) => {
        const ids = e[dir] || [];
        if (!ids.length) return;
        h += '<div class="gm-gv-edge-row"><span class="gm-gv-edge-label">' +
          escapeHtml(layer + " · " + label) + "</span> " +
          ids.map((id) => refChip(id, tab)).join(" ") + "</div>";
      });
    });
    wrap.innerHTML = h;
    return wrap;
  }

  function detailHead(d, chips) {
    const head = document.createElement("div");
    head.className = "gm-gv-head";
    const lr = d.line_range && d.line_range.length
      ? " · lines " + d.line_range[0] + "–" + d.line_range[d.line_range.length - 1] : "";
    head.innerHTML =
      "<h3>" + escapeHtml(d.title || d.id) + "</h3>" +
      '<div class="gm-gv-meta">' + escapeHtml(d.id) + " · " + escapeHtml(d.corpus) +
      (d.source_file ? " · " + escapeHtml(d.source_file) + escapeHtml(lr) : "") + "</div>" +
      (chips && chips.length
        ? '<div class="gm-gv-chips">' + chips.map((c) =>
            '<span class="gm-gv-chip' + (c.cls ? " " + c.cls : "") + '">' +
            escapeHtml(c.text) + "</span>").join("") + "</div>"
        : "");
    return head;
  }

  function detailMessage(el, text, bad) {
    el.innerHTML = "";
    const div = document.createElement("div");
    div.className = bad ? "gm-error" : "gm-empty";
    div.textContent = text;
    el.appendChild(div);
  }

  // --- Corpus tabs (declared by the deployment; the data is GmCorpusTabs' input) ---

  function corpusRosterProps(tabId) {
    const r = gv[tabId].data && gv[tabId].data.roster;
    return r ? r.properties : [];
  }

  function buildCorpusTab(tab) {
    const id = tab.id;
    gv[id] = { corpusTab: tab, loaded: false, data: null, groupBy: (tab.group_by || [])[0] || null,
               selected: null, known: {} };
    const button = document.createElement("button");
    button.className = "gm-tab";
    button.setAttribute("data-tab", id);
    button.textContent = tab.label;
    const procsButton = document.querySelector('.gm-tab[data-tab="procs"]');
    procsButton.parentNode.insertBefore(button, procsButton);
    button.addEventListener("click", () => {
      activateTab(id);
      if (!gv[id].loaded) { loadGraphStatusInto($("#ct-" + id + "-banner")); loadCorpusTab(id); }
    });

    const panel = document.createElement("section");
    panel.className = "gm-panel";
    panel.id = "panel-" + id;
    const seg = (tab.group_by || []).length > 1
      ? '<div class="gm-seg" id="ct-' + id + '-groupby">' + tab.group_by.map((g, i) =>
          '<button class="gm-seg-btn' + (i === 0 ? " is-active" : "") + '" data-group="' + escapeHtml(g) + '">' +
          escapeHtml(GmCorpusTabs.humanKey(g)) + "</button>").join("") + "</div>"
      : "";
    panel.innerHTML =
      '<div class="gm-gv-banner" id="ct-' + id + '-banner"></div>' +
      '<div class="gm-gv"><div class="gm-gv-left"><div class="gm-section-head"><h2>' + escapeHtml(tab.label) +
      "</h2>" + seg + '<button class="gm-btn" id="ct-' + id + '-refresh">Refresh</button></div>' +
      '<div class="gm-list gm-gv-list" id="ct-' + id + '-list"><div class="gm-empty">Loading ' +
      escapeHtml(tab.item_noun) + "…</div></div></div>" +
      '<div class="gm-gv-right" id="ct-' + id + '-detail"><div class="gm-empty">Select an entry on the left.</div></div></div>';
    const procsPanel = $("#panel-procs");
    procsPanel.parentNode.insertBefore(panel, procsPanel);
    $("#ct-" + id + "-refresh").addEventListener("click", () => loadCorpusTab(id));
    panel.querySelectorAll("#ct-" + id + "-groupby .gm-seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        gv[id].groupBy = b.getAttribute("data-group");
        panel.querySelectorAll("#ct-" + id + "-groupby .gm-seg-btn").forEach((x) =>
          x.classList.toggle("is-active", x === b));
        renderCorpusList(id);
      });
    });
  }

  // The deployment's tab declarations: none by default (Gnomon ships no corpus tab).
  async function buildCorpusTabs() {
    let cfg;
    try {
      cfg = await api("/api/corpus-tabs");
    } catch (e) { return; }  // no tabs offered; the rest of the dashboard is unaffected
    (cfg.tabs || []).forEach(buildCorpusTab);
  }

  async function loadCorpusTab(id) {
    const tab = gv[id].corpusTab;
    const list = $("#ct-" + id + "-list");
    const banner = $("#ct-" + id + "-banner");
    list.innerHTML = '<div class="gm-empty">Querying the ' + escapeHtml(tab.corpus) + " corpus…</div>";
    try {
      const data = await api("/api/corpus-tabs/" + encodeURIComponent(id));
      gv[id].data = data;
      gv[id].loaded = true;
      gv[id].known = {};
      if (data.roster) gv[id].known[data.roster.id] = true;
      data.items.forEach((g) => { gv[id].known[g.id] = true; });
      gvBanner(banner, "live · corpus " + data.corpus + " · " + data.items.length + " " + tab.item_noun +
        (data.roster ? " + roster" : "") + " (" + data.total_nodes + " nodes" +
        (data.complete ? ", complete" : ", INCOMPLETE") + ")", !data.complete);
      renderCorpusList(id);
    } catch (e) {
      gvBanner(banner, "⛔ " + e.message, true);
      list.innerHTML = '<div class="gm-error">Could not load ' + escapeHtml(tab.item_noun) + ": " +
        escapeHtml(e.message) + "</div>";
    }
  }

  function renderCorpusList(id) {
    const st = gv[id];
    const tab = st.corpusTab;
    const list = $("#ct-" + id + "-list");
    const data = st.data;
    if (!data) return;
    list.innerHTML = "";
    if (data.roster) {
      list.appendChild(gvItem(data.roster.title, data.roster.id,
        st.selected === data.roster.id, () => openCorpusNode(id, data.roster.id), "is-roster"));
    }
    const groups = st.groupBy
      ? GmCorpusTabs.groupItems(data.items, st.groupBy, tab, corpusRosterProps(id))
      : [{ key: "", label: "", note: "", items: data.items }];
    groups.forEach((g) => {
      if (st.groupBy) list.appendChild(gvGroup(g.label, g.note, g.items.length));
      g.items.forEach((it) => {
        list.appendChild(gvItem(it.title, it.id, st.selected === it.id, () => openCorpusNode(id, it.id)));
      });
    });
    if (!data.items.length) {
      list.insertAdjacentHTML("beforeend", '<div class="gm-empty">No ' + escapeHtml(tab.item_noun) + " found.</div>");
    }
  }

  async function openCorpusNode(id, nodeId) {
    gv[id].selected = nodeId;
    markSelected($("#ct-" + id + "-list"), nodeId);
    const d = $("#ct-" + id + "-detail");
    d.innerHTML = '<div class="gm-empty">Loading…</div>';
    let node;
    try {
      node = await api("/api/corpus-tabs/" + encodeURIComponent(id) + "/" + encodeURIComponent(nodeId));
    } catch (e) {
      detailMessage(d, "Could not load " + nodeId + ": " + e.message, true);
      return;
    }
    if (gv[id].selected !== nodeId) return;  // a later click won
    renderCorpusDetail(id, node);
  }

  function renderCorpusDetail(id, node) {
    const d = $("#ct-" + id + "-detail");
    d.innerHTML = "";
    const m = GmCorpusTabs.detailModel(node, gv[id].corpusTab);
    d.appendChild(detailHead(node, m.chips));
    if (m.lede) {
      const lede = document.createElement("div");
      lede.className = "gm-gv-lede";
      lede.innerHTML = '<div class="gm-gv-lede-label">' + escapeHtml(m.lede.label) + "</div>" +
        richText(m.lede.text, id);
      d.appendChild(lede);
    }
    if (m.chipList) {
      const sec = document.createElement("div");
      sec.className = "gm-gv-section";
      sec.innerHTML = '<div class="gm-gv-subhead">' + escapeHtml(m.chipList.label) + '</div><div class="gm-gv-chips">' +
        m.chipList.items.map((r) => '<span class="gm-gv-chip">' + escapeHtml(r) + "</span>").join("") + "</div>";
      d.appendChild(sec);
    }
    const fb = propsBlock(m.featured, id);
    if (fb) d.appendChild(fb);
    const rb = propsBlock(m.rest, id, m.restHeading);
    if (rb) d.appendChild(rb);
    const bb = bodyBlock(node.body, id, "Body");
    if (bb) d.appendChild(bb);
    const eb = edgesBlock(node.edges, id);
    if (eb) d.appendChild(eb);
    wireRefs(d);
  }

  // --- PROCs ---

  const STEP_HIDDEN = ["part-of", "next-step"];  // order + nesting already convey these

  async function loadProcs() {
    const list = $("#procs-list");
    const banner = $("#procs-banner");
    list.innerHTML = '<div class="gm-empty">Querying every registered corpus…</div>';
    try {
      const data = await api("/api/procs");
      gv.procs.data = data;
      gv.procs.loaded = true;
      gv.procs.known = {};
      data.groups.forEach((g) => g.procs.forEach((p) => {
        gv.procs.known[p.id] = { corpus: g.corpus };
      }));
      const f = data.funnel;
      gvBanner(banner, "live · " + f.total_matched + " procedures across " +
        data.groups.length + " corpora · searched " + f.corpora_in_scope +
        " corpora / " + f.nodes_in_scope + " nodes · " +
        (f.complete ? "complete" : "INCOMPLETE") +
        (f.stale_corpora && f.stale_corpora.length
          ? " · ⚠ stale: " + f.stale_corpora.join(", ") : ""),
        !f.complete || (f.stale_corpora && f.stale_corpora.length));
      renderProcList();
    } catch (e) {
      gvBanner(banner, "⛔ " + e.message, true);
      list.innerHTML = '<div class="gm-error">Could not load procedures: ' +
        escapeHtml(e.message) + "</div>";
    }
  }

  function renderProcList() {
    const list = $("#procs-list");
    const data = gv.procs.data;
    if (!data) return;
    list.innerHTML = "";
    if (!data.groups.length) {
      list.innerHTML = '<div class="gm-empty">No procedures in any registered corpus.</div>';
      return;
    }
    data.groups.forEach((g) => {
      list.appendChild(gvGroup(g.corpus, "", g.procs.length));
      g.procs.forEach((p) => {
        list.appendChild(gvItem(p.title, p.id, gv.procs.selected === p.id,
          () => openProc(g.corpus, p.id)));
      });
    });
  }

  async function openProc(corpus, id) {
    gv.procs.selected = id;
    markSelected($("#procs-list"), id);
    const d = $("#procs-detail");
    d.innerHTML = '<div class="gm-empty">Loading…</div>';
    let node;
    try {
      node = await api("/api/procs/" + encodeURIComponent(corpus) + "/" +
        encodeURIComponent(id));
    } catch (e) {
      detailMessage(d, "Could not load " + id + ": " + e.message, true);
      return;
    }
    if (gv.procs.selected !== id) return;
    renderProcDetail(node);
  }

  function renderProcDetail(node) {
    const d = $("#procs-detail");
    d.innerHTML = "";
    const chips = [];
    node.properties.forEach((p) => {
      if ((p.key === "lifecycle-position" || p.key === "status") && typeof p.value === "string") {
        chips.push({ text: p.value, cls: "is-accent" });
      }
      if (p.key === "authority" && typeof p.value === "string") {
        chips.push({ text: "authority: " + p.value });
      }
    });
    d.appendChild(detailHead(node, chips));

    const bb = bodyBlock(node.body, "procs");
    if (bb) d.appendChild(bb);
    const pb = propsBlock(node.properties, "procs", "Properties");
    if (pb) d.appendChild(pb);

    const kids = node.children || [];
    if (kids.length) {
      const sec = document.createElement("div");
      sec.className = "gm-gv-section";
      sec.innerHTML = '<div class="gm-gv-subhead">Steps (' + kids.length + ")</div>";
      kids.forEach((k, i) => {
        const card = document.createElement("div");
        card.className = "gm-gv-step";
        let h = '<div class="gm-gv-step-title"><span class="gm-gv-step-n">' + (i + 1) +
          "</span>" + escapeHtml(k.title || k.id) +
          ' <span class="gm-gv-item-id">' + escapeHtml(k.id) + "</span></div>";
        const rows = k.properties.filter((p) => STEP_HIDDEN.indexOf(p.key) < 0);
        if (rows.length) {
          h += '<dl class="gm-gv-props">';
          rows.forEach((p) => {
            h += "<dt>" + escapeHtml(humanKey(p.key)) + "</dt><dd>" +
              propValueHtml(p.value, "procs") + "</dd>";
          });
          h += "</dl>";
        }
        if (k.body) h += '<div class="gm-gv-body">' + richText(reflow(k.body), "procs") + "</div>";
        card.innerHTML = h;
        sec.appendChild(card);
      });
      d.appendChild(sec);
    }

    const eb = edgesBlock(node.edges, "procs");
    if (eb) d.appendChild(eb);
    wireRefs(d);
  }

  async function loadGraphStatusInto(bannerEl) {
    // A cheap liveness line shown before the first list load returns.
    try {
      const s = await api("/api/graph/status");
      if (!s.live) gvBanner(bannerEl, "⛔ lts-engine not live — " + s.error, true);
    } catch (e) { /* the list load reports its own error */ }
  }

  function initGraphTabs() {
    const procsTab = document.querySelector('.gm-tab[data-tab="procs"]');
    buildCorpusTabs();
    if (procsTab) procsTab.addEventListener("click", () => {
      if (!gv.procs.loaded) { loadGraphStatusInto($("#procs-banner")); loadProcs(); }
    });
    $("#procs-refresh").addEventListener("click", loadProcs);
  }

  // --- Knowledge tab: usage matrix (panel a) ---------------
  const ku = { scope: "intelligence", loaded: false };

  async function loadUsage() {
    const K = window.GmKnowledgeUsage;
    const els = { matrix: $("#ku-matrix"), never: $("#ku-never"),
                  denominator: $("#ku-denominator"), status: $("#ku-status") };
    if (!K) { els.matrix.textContent = "⛔ knowledge_usage.js did not load"; return; }
    els.status.textContent = "indexing…";
    let view;
    try {
      view = K.buildView(await api("/api/usage?scope=" + encodeURIComponent(ku.scope)));
    } catch (e) {
      view = { error: "usage report failed: " + e.message };
    }
    ku.loaded = true;
    K.render(els, view, document);
  }

  function initKnowledgeTab() {
    const tab = document.querySelector('.gm-tab[data-tab="knowledge"]');
    if (!tab) return;
    tab.addEventListener("click", () => { if (!ku.loaded) loadUsage(); });
    $("#ku-refresh").addEventListener("click", loadUsage);
    document.querySelectorAll("#ku-scope .gm-seg-btn").forEach((b) => {
      b.addEventListener("click", () => {
        ku.scope = b.getAttribute("data-scope");
        document.querySelectorAll("#ku-scope .gm-seg-btn").forEach((x) =>
          x.classList.toggle("is-active", x === b));
        loadUsage();
      });
    });
  }

  // --- util ----------------------------------------------------------------
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // --- boot ----------------------------------------------------------------
  const LIVE_REFRESH_MS = 5000; // parity with CTk AgentsTab.REFRESH_MS
  const HEALTH_REFRESH_MS = 30000; // GM-RM-1A: matches buildstamp.CURRENT_TTL_S
  const GOLIVE_REFRESH_MS = 10000; // GM-RM-1D: pending -> result within ~10 s
  const DSD_LIST_POLL_MS = 5000; // GM-RM-4D: notice queued -> delivered, by polling

  // Poll-pileup fix: every setInterval poller goes through GmPoll.wrap (poll_guard.js): it skips its tick while
  // its previous request is in flight, and at most POLL_MAX pollers run at once (the Safari 6-slot budget).
  // No module => unguarded (today's behaviour). User-initiated calls use the plain functions.
  function guarded(name, fn) {
    return window.GmPoll ? window.GmPoll.wrap(name, fn) : fn;
  }

  function boot() {
    initTabs();
    initKnowledgeTab();
    loadHealth();
    loadProfiles();
    loadSessions();
    $("#btn-refresh").addEventListener("click", () => {
      loadProfiles();
      loadSessions();
    });
    // Live-sessions auto-refresh, matching the CTk tab's 5s timer.
    setInterval(guarded("loadSessions", loadSessions), LIVE_REFRESH_MS);
    // re-read health so the "dashboard running stale build" chip appears
    // without a page reload (the server's current-tree id is cached ~30 s anyway).
    setInterval(guarded("loadHealth", loadHealth), HEALTH_REFRESH_MS);
    // the start-time go-live smoke chip (in-memory server read, cheap).
    loadGolive();
    setInterval(guarded("loadGolive", loadGolive), GOLIVE_REFRESH_MS);
    loadSecurityBanner();
    setInterval(guarded("loadSecurityBanner", loadSecurityBanner), 30000);

    // DSD tab (dual pane).
    loadDsdBanner();
    loadDsdList();
    syncDsdIdLookup();   // GM-RM-X74 (flag-gated; builds nothing while dsd_id_lookup is off)
    $("#dsd-refresh").addEventListener("click", () => {
      loadDsdBanner();
      loadDsdList();
    });
    // Prime the arrival watcher (first poll returns nothing), then poll for
    // NEW pending DSDs to auto-switch to (operator-requested; CTk DsdArrivalWatcher).
    pollDsdArrivals();
    setInterval(guarded("pollDsdArrivals", pollDsdArrivals), DSD_ARRIVAL_POLL_MS);
    // acceptance-2: the list (and its notice chips) follows the queue by
    // normal polling — no manual refresh. Read-only server side (NoticeQueue.peek).
    setInterval(guarded("loadDsdList", loadDsdList), DSD_LIST_POLL_MS);

    // Config tab (slice 4).
    loadConfig();
    $("#config-reload").addEventListener("click", loadConfig);
    $("#config-save").addEventListener("click", saveConfig);

    // Corpus tabs (deployment-declared) + the PROCs graph-viewer tab (lazy: load on first open).
    initGraphTabs();
  }

  document.addEventListener("DOMContentLoaded", boot);
})();

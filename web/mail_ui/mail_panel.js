/* Gnomon — the Mail tab (V1: agent mail tool).
 *
 * Served ONLY while agent_mail is ON (the server injects the script tag into "/" and gates this file behind
 * the same flag; it is deliberately NOT under /static). It adds a "Mail" tab with:
 *   - the connection state (account, scopes, "Connect mail" and "Disconnect");
 *   - the operator-only configuration (account, label prefix, the draft recipient allow-list, signature template, per-agent
 *     overrides as JSON, the hourly write cap) saved through POST /api/mail/config;
 *   - the mail activity panel: who, when, which message id / subject, draft recipients. NEVER a body.
 *
 * Every dynamic value (incl. message subjects, which are EXTERNAL text) is set with textContent: no HTML strings.
 * The pure builders (buildStatus, buildActivityRows, parseConfigForm) have no DOM and are tested under node
 * (tests/test_x53_mail.py). Design tokens come from styles.css (macOS dark, system sans, no monospaced fonts).
 *
 * UMD: window.GmMailPanel in the browser, module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmMailPanel = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const POLL_MS = 10000;

  function buildStatus(st) {
    if (!st || typeof st !== "object") return { error: "no mail status (the server returned nothing)" };
    const lines = [];
    let chip = "not connected";
    let cls = "is-stopped";
    if (st.token_error) { chip = "token problem"; cls = "is-failed"; lines.push(String(st.token_error)); }
    else if (st.connected && st.scopes_ok === false) { chip = "REFUSED scopes"; cls = "is-failed"; lines.push(String(st.scope_error || "")); }
    else if (st.connected) { chip = "connected"; cls = "is-running"; }
    if (st.legacy_token_found) lines.push(String(st.legacy_token_message || "A token at the old location was NOT used: reconnect."));
    if (st.config_error) lines.push("Config problem: " + st.config_error);
    if (!st.client_file_present) lines.push("No OAuth client file yet (docs/mail-v1-setup.md, step 2).");
    return {
      chip: { text: chip, cls: "gm-apps-chip " + cls },
      account: st.account || "", scopes: Array.isArray(st.scopes) ? st.scopes.map(function (s) { return String(s).split("/").pop(); }) : [],
      allowlistCount: st.allowlist_count || 0, notes: lines,
      canConnect: !st.connected, canDisconnect: !!st.connected,
    };
  }

  function whenText(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    return isNaN(d.getTime()) ? String(iso) : d.toLocaleString();
  }

  // rows: GET /api/mail/activity -> display rows. Subjects and recipients are plain strings (set via textContent).
  function buildActivityRows(resp) {
    const rows = resp && Array.isArray(resp.rows) ? resp.rows : [];
    return rows.map(function (r) {
      return {
        when: whenText(r.ts), who: String(r.instance || ""), op: String(r.op || ""),
        decision: String(r.decision || "") + (r.reason ? " (" + r.reason + ")" : ""),
        message: String(r.message_id || (Array.isArray(r.message_ids) ? r.message_ids.length + " message(s)" : "")),
        subject: String(r.subject || ""),
        recipients: Array.isArray(r.recipients) ? r.recipients.join(", ") : "",
        refused: r.decision && r.decision !== "ok",
      };
    });
  }

  function lines(text) {
    return String(text || "").split(/\r?\n/).map(function (l) { return l.trim(); }).filter(function (l) { return l; });
  }

  // form -> the whole-config object POST /api/mail/config validates. Returns {config} or {error}.
  function parseConfigForm(f) {
    function obj(txt, what) {
      const t = String(txt || "").trim();
      if (!t) return {};
      let v;
      try { v = JSON.parse(t); } catch (e) { throw new Error(what + " is not valid JSON"); }
      if (!v || typeof v !== "object" || Array.isArray(v)) throw new Error(what + " must be a JSON object");
      return v;
    }
    try {
      return { config: {
        account: String(f.account || "").trim(), label_prefix: String(f.label_prefix || "").trim(),
        allowlist: lines(f.allowlist), signature_template: String(f.signature_template || ""),
        plus_map: obj(f.plus_map, "Plus-address overrides"), display_names: obj(f.display_names, "Display names"),
        signatures: obj(f.signatures, "Signature overrides"),
        max_writes_per_hour: parseInt(f.max_writes_per_hour, 10),
      } };
    } catch (e) { return { error: String(e.message || e) }; }
  }

  function configToForm(c) {
    c = c || {};
    return {
      account: c.account || "", label_prefix: c.label_prefix || "", allowlist: (c.allowlist || []).join("\n"),
      signature_template: c.signature_template || "",
      plus_map: JSON.stringify(c.plus_map || {}, null, 2), display_names: JSON.stringify(c.display_names || {}, null, 2),
      signatures: JSON.stringify(c.signatures || {}, null, 2), max_writes_per_hour: String(c.max_writes_per_hour || 20),
    };
  }

  function el(doc, tag, cls, text) {
    const e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  function mount(win) {
    const doc = win.document;
    if (!doc || doc.getElementById("panel-mail")) return;
    const tab = el(doc, "button", "gm-tab", "Mail");
    tab.setAttribute("data-tab", "mail");
    const tabbar = doc.getElementById("tabbar");
    if (tabbar) tabbar.appendChild(tab);
    const panel = el(doc, "section", "gm-panel");
    panel.id = "panel-mail";
    const main = doc.querySelector("main.gm-content");
    if (main) main.appendChild(panel);

    const head = el(doc, "div", "gm-section-head");
    head.appendChild(el(doc, "h2", "", "Agent mail"));
    const chip = el(doc, "span", "gm-apps-chip is-stopped", "…");
    head.appendChild(chip);
    const bConnect = el(doc, "button", "gm-btn", "Connect mail");
    const bDisc = el(doc, "button", "gm-btn", "Disconnect");
    const bRef = el(doc, "button", "gm-btn", "Refresh");
    head.appendChild(bConnect); head.appendChild(bDisc); head.appendChild(bRef);
    const msg = el(doc, "div", "gm-status", "");
    panel.appendChild(head);
    panel.appendChild(msg);
    const info = el(doc, "div", "gm-empty", "");
    panel.appendChild(info);

    panel.appendChild(el(doc, "h3", "", "Configuration (operator only)"));
    const fields = {};
    function field(key, label, multiline, help) {
      const wrap = el(doc, "div", "gm-apps-field");
      wrap.appendChild(el(doc, "label", "", label));
      const inp = el(doc, multiline ? "textarea" : "input", "");
      if (multiline) inp.rows = 4; else inp.type = "text";
      wrap.appendChild(inp);
      if (help) wrap.appendChild(el(doc, "div", "gm-empty", help));
      panel.appendChild(wrap);
      fields[key] = inp;
    }
    field("account", "Account address", false, "The operator's Gmail/Workspace address. Plus-addresses are derived from it.");
    field("label_prefix", "Label prefix", false, "An agent's label is <prefix><agent>, e.g. agents/alpha-architect.");
    field("allowlist", "Draft recipient allow-list (one address per line)", true,
          "Empty = every draft is refused. Add Pat, Sam and yourself here; nothing is hard-coded.");
    field("signature_template", "Signature template", true, "{display_name} is replaced per agent.");
    field("display_names", "Display names (JSON, agent -> name)", true, "Default derives Alpha-Architect from alpha-architect.");
    field("plus_map", "Plus-address overrides (JSON, agent -> address)", true, "");
    field("signatures", "Signature overrides (JSON, agent -> text)", true, "");
    field("max_writes_per_hour", "Max drafts + deliveries per agent per hour", false, "");
    const bSave = el(doc, "button", "gm-btn", "Save configuration");
    panel.appendChild(bSave);

    panel.appendChild(el(doc, "h3", "", "Activity (ids and subjects only, never bodies)"));
    const table = el(doc, "div", "gm-list", "");
    panel.appendChild(table);

    const state = { csrf: null };
    function say(t, bad) { msg.textContent = t; msg.className = "gm-status" + (bad ? " is-error" : ""); }

    async function req(method, url, body) {
      const opt = { method: method, headers: {} };
      if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
      if (method !== "GET") opt.headers["X-Gnomon-CSRF"] = state.csrf || "";
      const r = await win.fetch(url, opt);
      let j = null;
      try { j = await r.json(); } catch (e) { j = { ok: false, error: "HTTP " + r.status }; }
      if (!r.ok && j && j.ok === undefined) j = { ok: false, error: j.detail || ("HTTP " + r.status) };
      return j;
    }

    function renderRows(rows) {
      while (table.firstChild) table.removeChild(table.firstChild);
      if (!rows.length) { table.appendChild(el(doc, "div", "gm-empty", "No mail activity yet.")); return; }
      rows.forEach(function (r) {
        const line = el(doc, "div", "gm-list-row" + (r.refused ? " is-refused" : ""), "");
        line.textContent = [r.when, r.who, r.op, r.decision, r.message, r.subject, r.recipients].filter(function (x) { return x; }).join("  ·  ");
        table.appendChild(line);
      });
    }

    async function load() {
      try {
        const st = await req("GET", "/api/mail/status");
        state.csrf = st.csrf || state.csrf;
        const v = buildStatus(st);
        if (v.error) { say(v.error, true); return; }
        chip.textContent = v.chip.text; chip.className = v.chip.cls;
        info.textContent = (v.account ? "Account: " + v.account + ". " : "") +
          (v.scopes.length ? "Scopes: " + v.scopes.join(", ") + ". " : "") + v.notes.join(" ");
        bConnect.disabled = !v.canConnect; bDisc.disabled = !v.canDisconnect;
        const cfg = await req("GET", "/api/mail/config");
        const form = configToForm(cfg);
        Object.keys(form).forEach(function (k) { if (fields[k] && doc.activeElement !== fields[k]) fields[k].value = form[k]; });
        renderRows(buildActivityRows(await req("GET", "/api/mail/activity?limit=100")));
      } catch (e) { say("Mail status failed: " + e, true); }
    }

    tab.addEventListener("click", function () {
      doc.querySelectorAll(".gm-tab").forEach(function (t) { t.classList.toggle("is-active", t === tab); });
      doc.querySelectorAll(".gm-panel").forEach(function (p) { p.classList.toggle("is-active", p === panel); });
      load();
    });
    bRef.addEventListener("click", load);
    bConnect.addEventListener("click", async function () {
      const r = await req("POST", "/api/mail/connect", {});
      if (r && r.ok && r.auth_url) { win.open(r.auth_url, "_blank"); say("Consent opened in your browser. Return here and press Refresh."); }
      else say("Connect failed: " + ((r && r.error) || "unknown"), true);
    });
    bDisc.addEventListener("click", async function () {
      if (!win.confirm("Revoke the Google token and delete it from this machine?")) return;
      const r = await req("POST", "/api/mail/disconnect", {});
      say(r && r.ok ? ("Disconnected. Revoked at Google: " + !!r.revoked + (r.warning ? ". " + r.warning : "")) : "Disconnect failed: " + ((r && r.error) || "unknown"), !(r && r.ok));
      load();
    });
    bSave.addEventListener("click", async function () {
      const vals = {};
      Object.keys(fields).forEach(function (k) { vals[k] = fields[k].value; });
      const p = parseConfigForm(vals);
      if (p.error) { say(p.error, true); return; }
      const r = await req("POST", "/api/mail/config", p.config);
      say(r && r.ok ? "Configuration saved." : "Save refused: " + ((r && r.error) || "unknown"), !(r && r.ok));
      if (r && r.ok) load();
    });
    win.setInterval(function () { if (panel.classList.contains("is-active")) load(); }, POLL_MS);
  }

  if (typeof window !== "undefined" && window.document) {
    if (window.document.readyState === "loading") window.document.addEventListener("DOMContentLoaded", function () { mount(window); });
    else mount(window);
  }
  return { buildStatus: buildStatus, buildActivityRows: buildActivityRows, parseConfigForm: parseConfigForm, configToForm: configToForm, mount: mount };
});

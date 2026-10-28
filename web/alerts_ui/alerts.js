/* Gnomon - (core) the alert column.
 *
 * A stack of alerts at the top right of the page, newest on top, in the manner of macOS Notification Center:
 * a close box on every alert, "Dismiss All", "+k more" when there are more than MAX_VISIBLE. Loaded ONLY when
 * alerts is on (the server injects this script and alerts.css into "/"; flag off = neither exists).
 *
 * PLAIN TEXT ONLY: every alert string is placed with textContent. This file contains no HTML-string sink at
 * all (tests/test_x50_alerts.py greps for the whole list of them). A link is a dashboard TAB id; opening it
 * clicks that tab's own button and dismisses the alert.
 *
 * UMD: window.GmAlerts in the browser (the column starts itself), module.exports under node (the pure helpers
 * only, for tests).
 */
(function (root, factory) {
  "use strict";
  const api = factory(root);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmAlerts = api;
})(typeof self !== "undefined" ? self : this, function (root) {
  "use strict";

  const MAX_VISIBLE = 5;
  const POLL_MS = 4000;
  const SEVERITIES = ["info", "warning", "error"];

  // ---- pure helpers (node-tested) ----

  function severityClass(sev) {
    return "gm-alert-" + (SEVERITIES.indexOf(sev) >= 0 ? sev : "info");
  }

  // alerts: newest first. -> {shown: [...], more: k}. expanded shows everything.
  function split(alerts, max, expanded) {
    const list = Array.isArray(alerts) ? alerts : [];
    if (expanded || list.length <= max) return { shown: list.slice(), more: 0 };
    return { shown: list.slice(0, max), more: list.length - max };
  }

  function ageText(ts, nowSec) {
    if (typeof ts !== "number" || typeof nowSec !== "number" || !isFinite(ts) || !isFinite(nowSec)) return "";
    const d = Math.max(0, Math.round(nowSec - ts));
    if (d < 45) return "now";
    if (d < 3600) return Math.max(1, Math.round(d / 60)) + " min ago";
    if (d < 86400) return Math.round(d / 3600) + " h ago";
    return Math.round(d / 86400) + " d ago";
  }

  // The link is a tab id; anything else is not offered.
  function linkTab(link) {
    return typeof link === "string" && /^[a-z][a-z0-9-]{0,30}$/.test(link) ? link : null;
  }

  function signature(alerts, expanded) {
    return (expanded ? "x:" : "c:") + (Array.isArray(alerts) ? alerts.map((a) => a.id).join(",") : "");
  }

  // ---- browser part ----

  const state = { alerts: [], csrf: "", expanded: false, sig: null, now: 0, failures: 0, error: "" };

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }

  function container() {
    let c = document.getElementById("gm-alerts");
    if (c) return c;
    c = el("div", "gm-alerts");
    c.id = "gm-alerts";
    c.setAttribute("role", "region");
    c.setAttribute("aria-label", "Alerts");
    c.setAttribute("aria-live", "polite");
    document.body.appendChild(c);
    return c;
  }

  async function post(path, payload) {
    const r = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Gnomon-CSRF": state.csrf },
      body: JSON.stringify(payload || {}),
    });
    return r.ok;
  }

  async function dismissOne(id) {
    state.alerts = state.alerts.filter((a) => a.id !== id);   // optimistic
    render();
    try { await post("/api/alerts/" + encodeURIComponent(id) + "/dismiss"); } catch (e) { /* next poll shows truth */ }
    refresh();
  }

  // F6: Dismiss All dismisses exactly the alerts this page is SHOWING (not those behind "+k more", and not any that
  // arrived after the last poll); the server dismisses only the ids we send.
  function displayedIds() {
    return split(state.alerts, MAX_VISIBLE, state.expanded).shown.map((a) => a.id);
  }

  async function dismissAll() {
    const ids = displayedIds();
    const gone = new Set(ids);
    state.alerts = state.alerts.filter((a) => !gone.has(a.id));
    render();
    try { await post("/api/alerts/dismiss-all", { ids: ids }); } catch (e) { /* next poll shows truth */ }
    refresh();
  }

  function openLink(a) {
    const tab = linkTab(a.link);
    if (tab) {
      const btn = document.querySelector('.gm-tab[data-tab="' + tab + '"]');
      if (btn) btn.click();
    }
    dismissOne(a.id);
  }

  function card(a) {
    const c = el("div", "gm-alert " + severityClass(a.severity));
    c.setAttribute("data-alert-id", a.id);
    c.setAttribute("role", a.severity === "info" ? "status" : "alert");
    const main = el("div", "gm-alert-main");
    main.appendChild(el("div", "gm-alert-title", a.title));
    if (a.body) main.appendChild(el("div", "gm-alert-body", a.body));
    const meta = el("div", "gm-alert-meta", (a.source || "") + (a.source ? " · " : "") + ageText(a.ts, state.now));
    main.appendChild(meta);
    if (linkTab(a.link)) {
      const open = el("button", "gm-btn gm-alert-open", "Open");
      open.type = "button";
      open.addEventListener("click", () => openLink(a));
      main.appendChild(open);
    }
    c.appendChild(main);
    const x = el("button", "gm-alert-close", "×");
    x.type = "button";
    x.setAttribute("aria-label", "Dismiss alert");
    x.title = "Dismiss";
    x.addEventListener("click", () => dismissOne(a.id));
    c.appendChild(x);
    return c;
  }

  function render() {
    const c = container();
    const sig = signature(state.alerts, state.expanded);
    if (sig === state.sig && (state.alerts.length || state.failures || state.error)) return;   // unchanged: do not rebuild (no flicker)
    state.sig = sig;
    c.textContent = "";
    if (!state.alerts.length && !state.failures && !state.error) { c.classList.remove("is-shown"); return; }
    c.classList.add("is-shown");
    if (state.error) {   // F-D1(c): an unusable store / a failing poll is shown, never swallowed
      c.appendChild(el("div", "gm-alerts-failures gm-alerts-error", "Alerts unavailable: " + state.error));
    }
    if (state.failures) {   // F3/F-D3: producers that failed to raise an alert, last 24 h only
      c.appendChild(el("div", "gm-alerts-failures", state.failures + " alert producer failure" +
        (state.failures === 1 ? "" : "s") + " in the last 24 h (see producer-failures.jsonl)"));
    }
    if (!state.alerts.length) return;
    const head = el("div", "gm-alerts-head");
    head.appendChild(el("span", "gm-alerts-count", state.alerts.length + (state.alerts.length === 1 ? " alert" : " alerts")));
    const all = el("button", "gm-btn gm-alerts-dismiss-all", "Dismiss All");
    all.type = "button";
    all.addEventListener("click", dismissAll);
    head.appendChild(all);
    c.appendChild(head);
    const parts = split(state.alerts, MAX_VISIBLE, state.expanded);
    parts.shown.forEach((a) => c.appendChild(card(a)));
    if (parts.more > 0 || (state.expanded && state.alerts.length > MAX_VISIBLE)) {
      const more = el("button", "gm-alerts-more",
        parts.more > 0 ? "+" + parts.more + " more" : "Show fewer");
      more.type = "button";
      more.addEventListener("click", () => { state.expanded = !state.expanded; state.sig = null; render(); });
      c.appendChild(more);
    }
  }

  function setError(msg) {
    if (msg !== state.error) { state.error = msg; state.sig = null; render(); }
  }

  async function refresh() {
    try {
      const r = await fetch("/api/alerts", { headers: { Accept: "application/json" } });
      if (!r.ok) { setError("alert poll failed: HTTP " + r.status); return; }
      const body = await r.json();
      state.csrf = typeof body.csrf === "string" ? body.csrf : state.csrf;
      state.now = typeof body.now === "number" ? body.now : Date.now() / 1000;
      state.alerts = Array.isArray(body.alerts) ? body.alerts : [];
      const pf = body.producer_failures && typeof body.producer_failures.total === "number" ? body.producer_failures.total : 0;
      if (pf !== state.failures) { state.failures = pf; state.sig = null; }
      setError(typeof body.store_error === "string" ? body.store_error : "");
      render();
    } catch (e) { setError("alert poll failed: " + (e && e.message ? e.message : e)); }
  }

  function start() {
    refresh();
    setInterval(refresh, POLL_MS);
  }

  if (typeof document !== "undefined" && !(typeof module === "object" && module.exports)) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }

  return { MAX_VISIBLE, severityClass, split, ageText, linkTab, signature };
});

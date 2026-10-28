/* Gnomon — the "a deny guard could not start" banner (gnomon/guard_banner.py; flag guard_start_failure_banner).
 *
 * Served ONLY while the flag is ON (the server injects the script tag into "/" and gates this file behind the same flag; it is deliberately NOT
 * under /static). Without touching index.html, app.js or styles.css it puts ONE red banner at the very top of the page, above the tab bar, so it
 * shows on EVERY tab. The server decides what it says (the exact text, from the deny guard's start-up failure log); this script only draws it.
 *
 *   - state "failures"   -> the banner: the text and, below it, one plain sentence;
 *   - state "unreadable" -> the banner with the could-not-be-read text (a reading that could not be taken is never shown as nothing, g112);
 *   - state "none"       -> no banner element at all;
 *   - an answer this script does not understand -> the could-not-be-read banner (never "nothing");
 *   - a 200 answer whose body is not JSON -> the could-not-be-read banner (never nothing);
 *   - a poll that FAILED (network error, timeout, a non-200) -> what is shown stays and the next tick retries (g102); the page's own header already says when
 *     the backend is unreachable.
 * It refreshes with the page's poll (GmPoll.wrap, 10 s, hidden tab skipped) and clears by itself: there is NO dismiss, close or snooze control
 * and no handler of any kind on the banner (a test fails the build if one appears). Everything drawn is textContent: never HTML (g107).
 *
 * UMD: window.GmGuardBanner in the browser, module.exports under node (the pure view builder is tested without a DOM).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmGuardBanner = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const POLL_MS = 10000;
  const CAP = 600;
  const BANNER_ID = "gm-guard-banner";
  const NOT_UNDERSTOOD = "The deny guard's failure log could not be read: the dashboard got an answer it could not understand. " +
    "Treat it as if a guard could not start.";
  const CSS = [
    "#gm-guard-banner{position:sticky;top:0;z-index:1000;padding:var(--gm-pad-sm,8px) var(--gm-pad-lg,16px);background:var(--gm-danger,#d70015);color:#fff;" +
      "border-bottom:2px solid #fff;font-size:var(--gm-fs-md,14px);font-weight:600}",
    "#gm-guard-banner .gm-guard-banner-detail{margin-top:2px;font-size:var(--gm-fs-sm,12px);font-weight:400}",
  ].join("\n");

  function cap(v) {
    const s = typeof v === "string" ? v : String(v == null ? "" : v);
    return s.length <= CAP ? s : s.slice(0, CAP - 1) + "…";
  }

  // res: the body of GET /api/guard-start-failures -> null (no banner) or {text, detail}
  function bannerView(res) {
    if (!res || typeof res !== "object") return { text: NOT_UNDERSTOOD, detail: "" };
    if (res.state === "none") return null;
    if ((res.state === "failures" || res.state === "unreadable") && typeof res.text === "string" && res.text !== "") {
      return { text: cap(res.text), detail: typeof res.detail === "string" ? cap(res.detail) : "" };
    }
    return { text: NOT_UNDERSTOOD, detail: "" };
  }

  function render(doc, view) {
    let el = doc.getElementById(BANNER_ID);
    if (!view) { if (el && el.parentNode) el.parentNode.removeChild(el); return null; }
    if (!el) {
      if (!doc.getElementById("gm-guard-banner-style")) {      // the style appears with the first banner, so a page with nothing to say is untouched
        const style = doc.createElement("style");
        style.id = "gm-guard-banner-style";
        style.textContent = CSS;
        doc.head.appendChild(style);
      }
      el = doc.createElement("div");
      el.id = BANNER_ID;
      el.setAttribute("role", "alert");
      el.appendChild(doc.createElement("div")).className = "gm-guard-banner-text";
      el.appendChild(doc.createElement("div")).className = "gm-guard-banner-detail";
      doc.body.insertBefore(el, doc.body.firstChild);
    }
    el.firstChild.textContent = view.text;
    el.lastChild.textContent = view.detail;
    el.lastChild.hidden = view.detail === "";
    return el;
  }

  let started = false;
  function init(win) {
    const doc = win.document;
    if (started) return;
    started = true;

    async function poll(signal) {
      if (doc.hidden) return;                                // a hidden browser tab does not poll
      let r;
      try {
        const opt = { cache: "no-store" };
        if (signal && typeof signal.aborted === "boolean") opt.signal = signal;
        r = await win.fetch("/api/guard-start-failures", opt);
      } catch (e) {
        return;                                              // a failed request keeps what is shown; the next tick retries
      }
      if (!r || !r.ok) return;                               // a non-200 is a failed request too
      let res = null;
      try {
        res = await r.json();
      } catch (e) {
        if (signal && signal.aborted) return;                // the poll timed out while the body was coming: a failed request
        res = null;                                          // a 200 whose body is not JSON: a reading that could not be taken (bannerView(null))
      }
      render(doc, bannerView(res));
    }
    poll();
    win.setInterval(win.GmPoll ? win.GmPoll.wrap("guardBanner", poll) : poll, POLL_MS);   // poll-pileup fix: in-flight guard
  }

  if (typeof window !== "undefined" && window.document) {
    if (window.document.readyState === "loading") window.document.addEventListener("DOMContentLoaded", function () { init(window); });
    else init(window);
  }

  return { bannerView: bannerView, render: render, init: init, POLL_MS: POLL_MS, BANNER_ID: BANNER_ID };
});

/* Gnomon — go-live smoke header chip (pure logic, no DOM).
 *
 * goliveChip(res)     /api/golive -> the header chip, or null (flag off / not started /
 *                     cancelled / no result).
 *                       done -> "go-live: P pass · F fail · S skip" (rm1a2: SKIPs are
 *                               never hidden). is-pass ONLY when F == 0 and P > 0;
 *                               any FAIL, or an all-SKIP run (P == 0), is loud (is-fail).
 *                       done, no checks at all -> "go-live: 0 checks" (is-fail)
 *                       scheduled    -> "go-live: pending"
 *                       running      -> "go-live: running"
 *                       error        -> "go-live: error"        (loud: is-fail)
 * failingLines(res)   the FAIL checks as "check — evidence" strings (the click-to-expand
 *                     detail). Rendered by app.js with textContent only.
 *
 * UMD: window.GmGoliveChip in the browser, module.exports under node
 * (tests/test_rm1d_golive_on_start.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmGoliveChip = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function num(v) {
    return typeof v === "number" && isFinite(v) ? v : 0;
  }

  function checksOf(res) {
    return res && Array.isArray(res.checks) ? res.checks : [];
  }

  function goliveChip(res) {
    if (!res || typeof res !== "object" || res.enabled !== true) return null;
    const when = typeof res.finished_at === "string" ? " at " + res.finished_at : "";
    if (res.state === "scheduled") {
      return { text: "go-live: pending", cls: "gm-golive-chip is-pending",
        title: "The start-time go-live smoke runs ~" + num(res.delay_s) + " s after the dashboard starts." };
    }
    if (res.state === "running") {
      return { text: "go-live: running", cls: "gm-golive-chip is-pending",
        title: "The start-time go-live smoke is running." };
    }
    if (res.state === "error") {
      return { text: "go-live: error", cls: "gm-golive-chip is-fail",
        title: "The go-live smoke could not run: " + String(res.error || "unknown error") };
    }
    if (res.state !== "done") return null;
    const c = res.counts && typeof res.counts === "object" ? res.counts : {};
    const pass = num(c.PASS), fail = num(c.FAIL), skip = num(c.SKIP);
    const build = " (build " + String(res.build_id || "?") + ")";
    if (pass + fail + skip === 0) {
      return { text: "go-live: 0 checks", cls: "gm-golive-chip is-fail",
        title: "The go-live smoke finished" + when + " but reported no checks at all" + build +
          " — nothing was verified." };
    }
    const text = "go-live: " + pass + " pass · " + fail + " fail · " + skip + " skip";
    if (fail > 0) {
      return { text: text, cls: "gm-golive-chip is-fail",
        title: fail + " go-live check(s) FAILED" + when + build + ". Click to see the failing lines." };
    }
    if (pass === 0) {
      return { text: text, cls: "gm-golive-chip is-fail",
        title: "Every go-live check was SKIPPED" + when + build + " — nothing was verified." };
    }
    return {
      text: text,
      cls: "gm-golive-chip is-pass",
      title: "Start-time go-live smoke: " + pass + " PASS, " + skip + " SKIP" + when + build + ".",
    };
  }

  function failingLines(res) {
    const out = [];
    checksOf(res).forEach(function (c) {
      if (c && c.status === "FAIL") out.push(String(c.check) + " — " + String(c.evidence || ""));
    });
    if (res && res.state === "error" && res.error) out.push("error — " + String(res.error));
    return out;
  }

  return { goliveChip: goliveChip, failingLines: failingLines };
});

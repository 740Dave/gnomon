/* Gnomon — quiescent chip (pure logic, no DOM).
 *
 * /api/sessions' `quiescent` ({reason, since_ts, label} | null, gated by
 * quiescent_state) -> "quiescent: <reason> since <time>" on the session
 * row. Distinct from the held-notice chip (a nudge/notice waiting) and from an
 * actively-working session (no chip).
 *
 * The session-row CALL SITE is app.js (owned by another builder at build time):
 *   index.html:  <script src="/static/quiescent_chip.js"></script>  (before app.js)
 *   app.js row:  const qc = window.GmQuiescentChip ? GmQuiescentChip.quiescentChip(s.quiescent) : null;
 *                if (qc) row.appendChild(noticeChipEl(qc));
 *
 * Returns plain strings; the caller renders with textContent only.
 * UMD: window.GmQuiescentChip in the browser, module.exports under node
 * (tests/test_rm3b_quiescent.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmQuiescentChip = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // "2026-09-29 14:02" in the viewer's local time; "" for a non-number.
  function formatClock(epochSec) {
    if (typeof epochSec !== "number" || !isFinite(epochSec)) return "";
    const d = new Date(epochSec * 1000);
    return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate()) +
      " " + pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  // q: the /api/sessions row's `quiescent`. -> {text, cls, title} | null
  function quiescentChip(q) {
    if (!q || typeof q !== "object" || typeof q.reason !== "string" || !q.reason) return null;
    const since = formatClock(q.since_ts);
    return {
      text: "quiescent: " + q.reason + (since ? " since " + since : ""),
      cls: "gm-quiescent-chip",
      title: "The agent declared its work complete. Stall nudges are paused until the " +
        "operator types, a DSD answer or hub message arrives, or the agent takes another " +
        "turn (safety valve 8 h). Context guidance still reaches it.",
    };
  }

  return { quiescentChip: quiescentChip, formatClock: formatClock };
});

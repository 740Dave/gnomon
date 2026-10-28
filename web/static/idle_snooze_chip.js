/* Gnomon - idle-snooze chip (pure logic, no DOM).
 *
 * /api/sessions' `idle_snooze` ({state, reason?, since_ts?, until_ts?, label} | null, gated by
 * idle_snooze) -> "idle alerts dismissed until operator returns: <reason>" (state dismissed)
 * or "idle alerts snoozed until <time>" on the session row, so the operator sees who dismissed, when
 * and why. Returns plain strings; the caller renders with textContent only.
 *
 *   index.html:  <script src="/static/idle_snooze_chip.js"></script>  (before app.js)
 *   app.js row:  const sc = window.GmIdleSnoozeChip ? GmIdleSnoozeChip.idleSnoozeChip(s.idle_snooze) : null;
 *                if (sc) row.appendChild(noticeChipEl(sc));
 *
 * UMD: window.GmIdleSnoozeChip in the browser, module.exports under node
 * (tests/test_x48_idle_snooze.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmIdleSnoozeChip = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function pad2(n) {
    return (n < 10 ? "0" : "") + n;
  }

  // "14:02" in the viewer's local time; "" for a non-number.
  function formatClock(epochSec) {
    if (typeof epochSec !== "number" || !isFinite(epochSec)) return "";
    const d = new Date(epochSec * 1000);
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes());
  }

  // q: the /api/sessions row's `idle_snooze`. -> {text, cls, title} | null
  function idleSnoozeChip(q) {
    if (!q || typeof q !== "object") return null;
    if (q.state === "dismissed" && typeof q.reason === "string" && q.reason) {
      const since = formatClock(q.since_ts);
      return {
        text: "idle alerts dismissed until operator returns: " + q.reason + (since ? " (since " + since + ")" : ""),
        cls: "gm-idle-snooze-chip",
        title: "The agent confirmed it has nothing left to do. Idle/stall/resume nudges are off until " +
          "operator activity in any session, or new work reaches the agent (an inbox file, a DSD " +
          "answer, an operator message). Context guidance, DSD notices and operator messages still arrive.",
      };
    }
    if (q.state === "snoozed") {
      const until = formatClock(q.until_ts);
      if (!until) return null;
      return {
        text: "idle alerts snoozed until " + until,
        cls: "gm-idle-snooze-chip",
        title: "The agent snoozed its idle nudge once (60 min). It is not dismissed.",
      };
    }
    return null;
  }

  return { idleSnoozeChip: idleSnoozeChip, formatClock: formatClock };
});

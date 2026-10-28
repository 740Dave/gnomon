/* Gnomon — notice chips (pure logic, no DOM).
 *
 * DSD "queued for <instance>": an ANSWERED DSD whose answer notice is
 *           still in the hub's durable queue (not yet ACKed by the harness) shows
 *           "notice queued for <instance> since <time>"; once the harness's
 *           written record exists, "notice delivered <time>"; a notice that aged
 *           out undelivered, "notice expired undelivered (<instance>)". No state
 *           knowable -> no chip. The queue's expired / corrupt counts are a QUIET
 *           one-line warning, never an alarm colour.
 * held-notice chip: /api/sessions' notice_hold rendered on the session
 *           row, NAMING the held notice kind(s); absent when notice_hold is empty.
 *
 * Every function returns plain strings/objects; app.js puts them on the page
 * with textContent only (no innerHTML with data).
 *
 * UMD: window.GmNoticeChips in the browser, module.exports under node
 * (tests/test_rm4d_x2_notice_display.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmNoticeChips = api;
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

  // notice: the /api/dsd/list row's `notice` (or /api/dsd/browse's). -> {text, cls, title} | null
  function dsdNoticeChip(notice) {
    if (!notice || typeof notice !== "object") return null;
    const inst = typeof notice.instance_id === "string" ? notice.instance_id : "";
    if (notice.state === "queued") {
      const since = formatClock(notice.since_epoch);
      return {
        text: "notice queued for " + inst + (since ? " since " + since : ""),
        cls: "gm-notice-chip is-queued",
        title: "Answered. The answer notice is waiting in the durable queue until " +
          inst + "'s harness delivers and acknowledges it.",
      };
    }
    if (notice.state === "delivered") {
      const at = formatClock(notice.delivered_epoch);
      if (!at) return null;
      return {
        text: "notice delivered " + at,
        cls: "gm-notice-chip is-delivered",
        title: "The answer notice was typed into " + inst + "'s session at " + at + ".",
      };
    }
    if (notice.state === "expired") {
      return {
        text: "notice expired undelivered (" + inst + ")",
        cls: "gm-notice-chip is-expired",
        title: "The answer notice aged out of the queue before " + inst +
          " received it. The answer itself is saved.",
      };
    }
    return null;
  }

  // summary: /api/dsd/notices. -> "" when clean or unavailable.
  function noticeWarning(summary) {
    if (!summary || !summary.available) return "";
    const parts = [];
    if (summary.total_expired > 0) parts.push(summary.total_expired + " expired");
    if (summary.total_corrupt > 0) parts.push(summary.total_corrupt + " corrupt");
    return parts.length ? "notice queue: " + parts.join(" · ") : "";
  }

  // pre-review B2 (count only): /api/dsd/notices instances[x] -> "2 notices held" | "count unavailable" | "" (nothing to say).
  // An ERROR (held_notices null with held_notices_error) is shown as one, never as zero.
  function heldNoticeLabel(n, err) {
    if (n === null && typeof err === "string" && err) return "count unavailable";
    if (typeof n !== "number" || !isFinite(n) || n < 1) return "";
    const k = Math.floor(n);
    return k + (k === 1 ? " notice held" : " notices held");
  }

  // nh: /api/sessions row's notice_hold. -> {text, cls, title} | null
  function holdChip(nh) {
    if (!nh || typeof nh !== "object" || !(nh.held > 0) || typeof nh.label !== "string") {
      return null;
    }
    const kinds = Array.isArray(nh.kinds) ? nh.kinds.filter((k) => typeof k === "string" && k) : [];
    let text = nh.label;
    if (kinds.length === 1) {
      text = kinds[0] + " " + nh.label;
    } else if (kinds.length > 1) {
      text = kinds.join(", ") + " " + nh.label.replace(/^notice held/, "notices held");
    }
    return {
      text: text,
      cls: "gm-hold-chip",
      title: nh.held + " agent-facing notice(s) held until the operator's line allows delivery",
    };
  }

  // mh: /api/sessions row's modal_hold (a STUCK modal hold). -> {text, cls, title} | null
  function modalHoldChip(mh) {
    if (!mh || typeof mh !== "object" || typeof mh.label !== "string") return null;
    return {
      text: "HOLD STUCK: " + mh.label,
      cls: "gm-hold-chip",
      title: "harness text is held behind a dialog that may still be open; close it, or \\approve --into-dialog in the session",
    };
  }

  // the wording for each dialog kind the route may send (a closed list mirrored from waiting_alert.KIND_LABEL; a test
  // pins the two together). Any other value is shown as 'unknown', never dropped (checklist g142).
  const WAITING_KINDS = {
    "permission-prompt": "a permission prompt",
    "AskUserQuestion": "a question",
    "ExitPlanMode": "a plan approval",
  };

  // w: /api/sessions row's waiting (the prompt this agent is waiting on you for; the key exists only with the flag on).
  // -> {text, cls, title} | null. Everything is plain text (the caller sets textContent / title, never innerHTML). A
  // confidential session carries no question, so the chip shows the kind only.
  function waitingChip(w) {
    if (!w || typeof w !== "object") return null;
    const known = typeof w.dialog === "string" && Object.prototype.hasOwnProperty.call(WAITING_KINDS, w.dialog);
    let text = "WAITING ON YOU: " + (known ? WAITING_KINDS[w.dialog] : "unknown");
    const left = w.seconds_left;
    if (typeof left === "number" && isFinite(left) && left >= 0) {
      text += " (" + Math.floor(left / 60) + ":" + ("0" + Math.floor(left % 60)).slice(-2) + " left)";
    }
    const q = (!w.confidential && typeof w.question === "string" && w.question) ? w.question : "";
    return {
      text: text,
      cls: "gm-hold-chip gm-waiting-chip",
      title: q || "this agent's terminal is waiting on you",
    };
  }

  // Audit LOW (2026-09-29): the DSD list re-polls every 5s. Its signature covers
  // everything the list RENDERS (order, ids, kinds, titles, notice states, the
  // selected row) and nothing else, so an unchanged poll skips the DOM rebuild
  // (keyboard focus and in-flight clicks survive).
  function dsdListSignature(rows, selectedId) {
    const parts = (Array.isArray(rows) ? rows : []).map((it) => {
      const n = it && it.notice;
      return [
        it && it.dsd_id, it && it.kind, it && it.title,
        n ? [n.state, n.instance_id, n.since_epoch, n.delivered_epoch] : null,
        // the accordion a row sits in (section / sender / answer time)
        it && it.section, it && it.sender, it && it.answered_epoch,
      ];
    });
    return JSON.stringify([selectedId === undefined ? null : selectedId, parts]);
  }

  function shouldRebuildList(prevSig, nextSig) {
    return prevSig === null || prevSig === undefined || prevSig !== nextSig;
  }

  return {
    dsdListSignature: dsdListSignature,
    shouldRebuildList: shouldRebuildList,
    formatClock: formatClock,
    dsdNoticeChip: dsdNoticeChip,
    noticeWarning: noticeWarning,
    heldNoticeLabel: heldNoticeLabel,
    holdChip: holdChip,
    modalHoldChip: modalHoldChip,
    waitingChip: waitingChip,
  };
});

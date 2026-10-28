/* Gnomon - DSD sent / answered time stamps (pure logic, no DOM, no clock of its own).
 *
 * "sent Fri 4:43 PM · 2h ago"  - LOCAL time (the browser's zone) + a relative age.
 * Hover (title) = the full UTC ISO time from the API. The clock (nowMs) is ALWAYS
 * injected by the caller, so the relative part is testable and the poll can refresh it.
 *
 * Unusable input (null / undefined / 0 / negative / non-numeric / NaN) -> "sent time
 * unknown": never an epoch-zero (1970) date and never a guessed one.
 *
 * UMD: window.GmDsdSentTime in the browser, module.exports under node
 * (tests/test_web_dsd_senttime.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDsdSentTime = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const MIN_EPOCH = 946684800; // 2000-01-01Z, same floor as gnomon.dsd.senttime
  const MAX_EPOCH = 253402300799; // year 9999, same ceiling as gnomon.dsd.senttime
  const DAY_MS = 86400000;

  // Strict typeof number on purpose: the API (dsd.senttime.sent_fields) only ever sends
  // a float or null for created_epoch, never a string, so no numeric-string coercion here
  // (Python's clean_epoch is lenient because it reads hand-edited JSON on disk).
  function usable(epoch) {
    return typeof epoch === "number" && isFinite(epoch) &&
      epoch >= MIN_EPOCH && epoch <= MAX_EPOCH;
  }

  // "just now" < 1 min, then "5m ago", "2h ago", "3d ago". A stamp in the future
  // (clock skew) gives "" - the local time alone is shown, not a negative age.
  function relativeAge(epoch, nowMs) {
    if (!usable(epoch) || typeof nowMs !== "number" || !isFinite(nowMs)) return "";
    const s = Math.floor((nowMs - epoch * 1000) / 1000);
    if (s < -300) return "";
    if (s < 60) return "just now";
    const m = Math.floor(s / 60);
    if (m < 60) return m + "m ago";
    const h = Math.floor(m / 60);
    if (h < 48) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }

  // "Fri 4:43 PM"; once the stamp is 6+ days old the weekday is ambiguous, so the
  // date is added: "Fri Sep 25, 4:43 PM". `tz` (optional) is for tests only.
  function localTime(epoch, nowMs, tz) {
    if (!usable(epoch)) return "";
    const d = new Date(epoch * 1000);
    const base = tz ? { timeZone: tz } : {};
    const old = typeof nowMs === "number" && (nowMs - epoch * 1000) >= 6 * DAY_MS;
    const parts = old
      ? Object.assign({ weekday: "short", month: "short", day: "numeric" }, base)
      : Object.assign({ weekday: "short" }, base);
    const day = d.toLocaleDateString("en-US", parts);
    const clock = d.toLocaleTimeString("en-US", Object.assign({ hour: "numeric", minute: "2-digit" }, base));
    return old ? day.replace(/^(\w+),? /, "$1 ") + ", " + clock : day + " " + clock;
  }

  // {text, title}. verb "sent" | "answered". approx marks an approximate answer time.
  function describe(epoch, iso, nowMs, verb, opts) {
    const o = opts || {};
    if (!usable(epoch)) return { text: verb + " time unknown", title: "" };
    const rel = relativeAge(epoch, nowMs);
    const text = verb + " " + (o.approx ? "~" : "") + localTime(epoch, nowMs, o.tz) + (rel ? " · " + rel : "");
    return { text: text, title: iso || "" };
  }

  // The answered stamp: only when the record carries an answer time we can stand behind.
  //   recorded        -> exact
  //   derived-marker  -> approximate (second precision from the write-back marker)
  //   healed-from-sent / missing -> null: healed-from-sent is the SENT time copied by a
  //   migration, so showing it as "answered" would be wrong; the caller shows nothing.
  function describeAnswered(epoch, iso, source, nowMs, opts) {
    if (!usable(epoch)) return null;
    if (source === "recorded") return describe(epoch, iso, nowMs, "answered", opts);
    if (source === "derived-marker")
      return describe(epoch, iso, nowMs, "answered", Object.assign({}, opts, { approx: true }));
    return null;
  }

  return { usable: usable, relativeAge: relativeAge, localTime: localTime,
           describe: describe, describeAnswered: describeAnswered };
});

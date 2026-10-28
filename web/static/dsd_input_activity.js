/* Gnomon - DSD tab input-activity tracker (checklist g76).
 *
 * Records the operator's REAL keyboard / pointer input inside the DSD tab (keydown, pointerdown, mousedown, wheel,
 * input). Only events the browser itself generated count: a script-dispatched event has isTrusted === false and
 * is ignored. Keeps a count + first/last time (never any key or content). summary(now) is what the submit sends:
 * {count, first_ms_ago, last_ms_ago}; the SERVER decides (it refuses a submit with no recent activity).
 * The clock (nowMs) is injected by the caller. UMD: window.GmDsdInputActivity / module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDsdInputActivity = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const EVENTS = ["keydown", "pointerdown", "mousedown", "wheel", "input"];

  function createTracker() {
    let count = 0, first = null, last = null;
    return {
      // ev: a DOM event (or a test double); nowMs: the caller's clock.
      note: function (ev, nowMs) {
        if (!ev || ev.isTrusted !== true) return false;      // synthetic input never counts
        if (typeof nowMs !== "number" || !isFinite(nowMs)) return false;
        count += 1;
        if (first === null) first = nowMs;
        last = nowMs;
        return true;
      },
      summary: function (nowMs) {
        if (count === 0 || typeof nowMs !== "number" || !isFinite(nowMs)) return { count: 0, first_ms_ago: null, last_ms_ago: null };
        return { count: count, first_ms_ago: Math.max(0, Math.round(nowMs - first)), last_ms_ago: Math.max(0, Math.round(nowMs - last)) };
      },
      reset: function () { count = 0; first = null; last = null; },
    };
  }

  // Attach to a DOM element (the DSD panel). Capture phase so a handler that stops propagation cannot hide input.
  function attach(tracker, target, clock) {
    const now = clock || Date.now;
    const fn = function (ev) { tracker.note(ev, now()); };
    EVENTS.forEach(function (name) { target.addEventListener(name, fn, true); });
    return function detach() { EVENTS.forEach(function (name) { target.removeEventListener(name, fn, true); }); };
  }

  return { EVENTS: EVENTS, createTracker: createTracker, attach: attach };
});

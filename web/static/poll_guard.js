/* Gnomon — poller in-flight guard (poll-pileup fix, 2026-10-04). Pure logic, no DOM.
 *
 * The page polls with setInterval, which does NOT wait for the previous request. When the server was slow,
 * ticks piled up, filled Safari's 6-connections-per-host pool, and a click (DSD present / Submit) queued
 * client-side behind them.
 *
 * wrap(name, fn)  -> a function to hand to setInterval. It SKIPS its tick when
 *                    (a) the same poller's previous call has not settled (a poller never has two requests
 *                        outstanding), or
 *                    (b) POLL_MAX pollers are already in flight (the connection BUDGET: 6 slots per host,
 *                        POLL_MAX for pollers, the rest always free for a user-initiated request).
 *                    fn must return the promise of its request (an async function does).
 *                    (c) never forever: each call gets an AbortController and a POLL_TIMEOUT_MS timer. On timeout
 *                        the request is aborted (the signal is passed to fn as its FIRST argument; fn hands it to
 *                        fetch), the slot and the in-flight flag are freed, and the next tick tries again. The slot
 *                        is freed even if fn ignores the signal. 20 s = about 7x the slowest healthy poll measured
 *                        (2.8 s cold, 0.01-0.3 s warm) and well under the 50-68 s pile-up. A hung server can
 *                        therefore never freeze the pollers; USER-initiated requests are never given this timeout
 *                        (a slow Submit must not be aborted).
 * Interval lengths are unchanged; a skipped tick just means the next one is the refresh.
 * User-initiated calls go to the unwrapped function and never touch this gate.
 */
(function (root, factory) {
  if (typeof module === "object" && module.exports) module.exports = factory();
  else root.GmPoll = factory();
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";
  const POLL_MAX = 4;
  const POLL_TIMEOUT_MS = 20000;
  const inflight = {};
  const wrapped = {};      // name -> the first wrapper (a name is wrapped once)
  let count = 0;

  function wrap(name, fn, max, timeoutMs) {
    // Two pollers sharing a name would share one in-flight flag, and the second would silently never run.
    // Loud, and the FIRST wrapper is kept (the one already ticking).
    if (Object.prototype.hasOwnProperty.call(wrapped, name)) {
      if (typeof console !== "undefined" && console.error) {
        console.error("GmPoll.wrap: poller name '" + name + "' is already wrapped; keeping the first, ignoring this one");
      }
      return wrapped[name];
    }
    const cap = max || POLL_MAX;
    const limit = timeoutMs || POLL_TIMEOUT_MS;
    const guardedFn = function () {
      if (inflight[name] || count >= cap) return undefined;   // skip this tick
      inflight[name] = true;
      count += 1;
      let finished = false;
      const ctl = typeof AbortController === "function" ? new AbortController() : null;
      let timer = null;
      const done = function () {
        if (finished) return;
        finished = true;
        clearTimeout(timer);
        inflight[name] = false;
        count -= 1;
      };
      timer = setTimeout(function () {
        if (ctl) { try { ctl.abort(); } catch (e) { /* already settled */ } }
        done();                                               // free the slot even if fn ignores the signal
      }, limit);
      let p;
      try { p = fn.apply(this, [ctl ? ctl.signal : undefined].concat(Array.prototype.slice.call(arguments))); }
      catch (e) { done(); throw e; }
      return Promise.resolve(p).then(function (v) { done(); return v; }, function (e) { done(); throw e; });
    };
    wrapped[name] = guardedFn;
    return guardedFn;
  }

  function state() { return { count: count, inflight: Object.assign({}, inflight), max: POLL_MAX }; }
  return { wrap: wrap, state: state, POLL_MAX: POLL_MAX, POLL_TIMEOUT_MS: POLL_TIMEOUT_MS };
});

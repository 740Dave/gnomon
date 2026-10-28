/* Gnomon — build-stamp chips (pure logic, no DOM).
 *
 * sessionBuildChip(row)  /api/sessions row -> a chip on the session ROW (the list
 *                        view itself, acceptance-3) when a LIVE session's launch.json
 *                        build_id differs from the tree on disk now ("stale build —
 *                        relaunch to update"), or when it has no stamp at all
 * ("unknown build" — every session launched before).
 *                        No chip when current, ended, flag off (row.build null), or
 *                        the current tree could not be measured.
 * serverBuildChip(b, port)  /api/health `build` -> the header chip when the :8770
 *                        process itself runs an older build than the tree on disk.
 *
 * Neutral styling on purpose: staleness is informational, not an alarm.
 * UMD: window.GmBuildChip in the browser, module.exports under node
 * (tests/test_rm1a_build_stamp.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmBuildChip = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function isId(v) {
    return typeof v === "string" && v.length > 0;
  }

  function sessionBuildChip(row) {
    if (!row || typeof row !== "object" || !row.live) return null;
    const b = row.build;
    if (!b || typeof b !== "object") return null;
    if (!isId(b.current_id)) return null;
    if (!isId(b.id)) {
      return {
        text: "unknown build",
        cls: "gm-build-chip is-unknown",
        title: "This session's launch.json has no build stamp (launched before build " +
          "stamping). The dashboard cannot tell which code it runs. Current tree: " + b.current_id,
      };
    }
    if (b.id === b.current_id) return null;
    return {
      text: "stale build — relaunch to update",
      cls: "gm-build-chip is-stale",
      title: "Launched on build " + b.id + "; the code on disk is now build " +
        b.current_id + ". Relaunch the session to run the current code.",
    };
  }

  function serverBuildChip(b, port) {
    if (!b || typeof b !== "object" || b.stale !== true) return null;
    const where = (typeof port === "string" && port) || (typeof port === "number" && port)
      ? ":" + port : "the dashboard";
    return {
      text: "dashboard running stale build — restart " + where,
      cls: "gm-build-chip is-stale",
      title: "This dashboard server started on build " + b.id + "; the code on disk is now build " +
        b.current_id + ". Restart the dashboard to serve the current code.",
    };
  }

  return {
    sessionBuildChip: sessionBuildChip,
    serverBuildChip: serverBuildChip,
  };
});

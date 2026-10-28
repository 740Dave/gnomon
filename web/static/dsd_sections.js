/* Gnomon - DSD master-pane sections (pure logic, no DOM).
 *
 * Three accordions over the /api/dsd/list rows:
 *   "Pending"                 always open, no collapse control (ignores saved state)
 *   "Answered <24 hours Ago"  rows the SERVER put in section "recent"
 *   "Answered Previously"     section "previous", one accordion per sender, each
 *                             closed by default
 * The server owns the 24 h boundary and the ordering (keyed on the ANSWER time) and
 * re-evaluates them on every list call; this module only shapes the already
 * ordered rows and remembers the user's open/closed choices (localStorage, every
 * access wrapped in try/catch - a blocked/throwing store never breaks the list).
 *
 * UMD: window.GmDsdSections in the browser, module.exports under node
 * (tests/test_web_dsd_sections.py).
 *
 * Defaults: Pending always open; "Answered <24 hours Ago" open;
 * "Answered Previously" CLOSED (older answered DSDs are rarely re-examined),
 * and so are its sender sub-accordions.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDsdSections = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const LABELS = {
    pending: "Pending",
    recent: "Answered <24 hours Ago",
    previous: "Answered Previously",
    withdrawn: "Withdrawn or replaced by the agent",
  };
  const STORE_KEY = "gm.dsd.accordions.v1";

  // Group the (already server-ordered) rows. Returns
  // [{id, key, label, count, alwaysOpen, defaultOpen, items, groups}] - always the
  // three sections, in order, even when empty (the headers show counts).
  function buildSections(rows) {
    const list = Array.isArray(rows) ? rows : [];
    const pick = (s) => list.filter((r) => r && r.section === s);
    const pend = pick("pending");
    const recent = pick("recent");
    const prev = pick("previous");
    const groups = [];
    const byLabel = {};
    prev.forEach((r) => {
      const s = r.sender || r.instance_id || "?";
      if (!byLabel[s]) {
        byLabel[s] = { id: "previous:" + s, sender: s, label: s, count: 0,
                       defaultOpen: false, items: [] };
        groups.push(byLabel[s]);
      }
      byLabel[s].items.push(r);
      byLabel[s].count += 1;
    });
    const marked = pick("withdrawn");
    const out = [
      { id: "pending", key: "pending", label: LABELS.pending, count: pend.length,
        alwaysOpen: true, defaultOpen: true, items: pend, groups: [] },
      { id: "recent", key: "recent", label: LABELS.recent, count: recent.length,
        alwaysOpen: false, defaultOpen: true, items: recent, groups: [] },
      { id: "previous", key: "previous", label: LABELS.previous, count: prev.length,
        alwaysOpen: false, defaultOpen: false, items: [], groups: groups },
    ];
    // pre-review B1: a fourth section ONLY when an agent withdrew or replaced something (else exactly the three above).
    if (marked.length) {
      out.push({ id: "withdrawn", key: "withdrawn", label: LABELS.withdrawn, count: marked.length,
                 alwaysOpen: false, defaultOpen: false, items: marked, groups: [] });
    }
    return out;
  }

  function headerText(label, count) { return label + " (" + count + ")"; }

  function loadState(storage) {
    try {
      const raw = (storage || root.localStorage).getItem(STORE_KEY);
      const o = raw ? JSON.parse(raw) : {};
      return o && typeof o === "object" && !Array.isArray(o) ? o : {};
    } catch (e) { return {}; }
  }

  function saveState(state, storage) {
    try { (storage || root.localStorage).setItem(STORE_KEY, JSON.stringify(state)); }
    catch (e) { /* private mode / quota: the choice just isn't remembered */ }
  }

  // Pending is ALWAYS open whatever is stored.
  function isOpen(state, id, defaultOpen) {
    if (id === "pending") return true;
    const v = state && state[id];
    return typeof v === "boolean" ? v : !!defaultOpen;
  }

  // What the list RENDERS from a row, for the no-rebuild signature: section,
  // sender and answer time move a row between accordions.
  function rowSigPart(it) {
    return [it && it.section, it && it.sender, it && it.answered_epoch];
  }

  return {
    LABELS: LABELS, STORE_KEY: STORE_KEY,
    buildSections: buildSections, headerText: headerText,
    loadState: loadState, saveState: saveState, isOpen: isOpen,
    rowSigPart: rowSigPart,
  };
});

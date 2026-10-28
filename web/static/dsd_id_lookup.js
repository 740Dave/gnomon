/* Gnomon - DSD tab "open by id": the pure id normaliser, no DOM, no network.
 *
 * The id format is the one the hub MINTS: ``dsd-<hex>`` where <hex> is the millisecond clock in hex
 * (gnomon/hub/mcp.py ``dsd_id = f"dsd-{ms:x}"``, 11 hex digits today) and the one the store VALIDATES
 * (gnomon/dsd/inbox.py ``_DSD_ID_RX = \Adsd-[0-9a-f]{6,32}\Z``). This file uses the validator's
 * bounds, not the current length, so an id never fails here that the store would accept.
 *
 * normalise(raw) -> {ok, id, cleaned, ids}
 *   ok      true when a DSD id was found
 *   id      "dsd-<hex>" (lower-case) when ok, else null
 *   cleaned the text to show the operator: the id when ok, otherwise the input with invisible characters
 *           removed, trimmed and shortened (never empty-looking when something was typed)
 * Accepts: dsd-<hex>, dsd_<hex>, DEC-DSD-<HEX> and DEC-DSD-<HEX>-D<n> (an answer-node id), in any case,
 * wrapped in quotes / backticks / brackets / trailing punctuation, with stray or non-breaking spaces,
 * zero-width characters and soft hyphens (U+200B-200D, U+2060, U+FEFF, U+00AD) anywhere, look-alike dashes
 * (U+2010-2015, U+2212) read as "-", or inside a longer pasted line. A line naming two or more DIFFERENT ids is
 * ambiguous: ok:false and `ids` lists them. When the WHOLE input is one id, spaces anywhere inside it are removed
 * ("dsd- 1a2b 3c4d 5e4", "dsd 1a2b3c4d5e4"), and a BARE hex of 6-32 digits is accepted ("1a2b3c4d5e4", what a
 * double-click on an id selects). A sentence is never glued together; a bare hex inside a sentence is not an id.
 * Whether the id EXISTS is decided by the caller against the store (exact match only).
 *
 * UMD: window.GmDsdIdLookup in the browser, module.exports under node (tests/test_web_dsd_id_lookup.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDsdIdLookup = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const HEX_MIN = 6, HEX_MAX = 32;   // = _DSD_ID_RX in gnomon/dsd/inbox.py
  const HEX = "[0-9a-f]{" + HEX_MIN + "," + HEX_MAX + "}";
  const ID_RX = new RegExp("(?<![0-9a-z])dsd[-_](" + HEX + ")(?![0-9a-z])", "g");
  // the WHOLE input is one id once every space and quote-like character is gone: dsd-<hex>, dsd_<hex>, "dsd <hex>",
  // DEC-DSD-<hex>[-D<n>]  (a space anywhere inside the hex is thereby removed too)
  const WHOLE_ID_RX = new RegExp("^(?:dec[-_])?dsd[-_]?(" + HEX + ")(?:[-_]d\\d+)?$");
  const WHOLE_BARE_RX = new RegExp("^(" + HEX + ")$");     // double-click selects only the hex: accept it alone
  const ZERO_WIDTH = /[\u200B-\u200D\u2060\uFEFF\u00AD]/g;     // incl. the soft hyphen
  const DASHES = /[\u2010-\u2015\u2212]/g;                       // look-alike dashes -> "-"
  const ANY_SPACE = /[\s\u00A0\u2000-\u200A\u202F\u205F\u3000]+/g;
  const JUNK = /[\s"'`\u2018\u2019\u201C\u201D<>()\[\]{}.,;:!?*]+/g;
  const SHOW_MAX = 60;

  function strip(raw) {
    return String(raw === null || raw === undefined ? "" : raw)
      .replace(ZERO_WIDTH, "")
      .replace(DASHES, "-")
      .replace(ANY_SPACE, " ")
      .trim();
  }

  // normalise(raw) -> {ok, id, cleaned, ids}
  //   ids: every DISTINCT id the text names, in order. Two or more -> ok:false (ambiguous: never pick one silently).
  function normalise(raw) {
    const spaced = strip(raw).toLowerCase();
    // Several separate, complete-looking ids ("1a2b3c4d5e4 1a11b0c0ffe") must never be glued into one long hex:
    // when EVERY space-separated piece is itself a whole id (or a whole bare hex), they are listed, not joined.
    const pieces = spaced.split(JUNK).filter(Boolean);
    if (pieces.length > 1 && /^(?:dec[-_])?dsd$/.test(pieces[0])) pieces.shift();   // "dsd <hex> <hex>"
    if (pieces.length > 1) {
      const own = pieces.map((t) => { const q = WHOLE_ID_RX.exec(t) || WHOLE_BARE_RX.exec(t); return q ? "dsd-" + q[1] : null; });
      if (own.every(Boolean)) {
        const ids = own.filter((x, i) => own.indexOf(x) === i);
        if (ids.length === 1) return { ok: true, id: ids[0], cleaned: ids[0], ids: ids };
        return { ok: false, id: null, cleaned: strip(raw).slice(0, SHOW_MAX), ids: ids };
      }
    }
    const joined = spaced.replace(JUNK, "");
    let m = WHOLE_ID_RX.exec(joined) || WHOLE_BARE_RX.exec(joined);
    if (m) {
      const id = "dsd-" + m[1];
      return { ok: true, id: id, cleaned: id, ids: [id] };
    }
    const ids = [];
    for (const x of spaced.matchAll(ID_RX)) {
      const id = "dsd-" + x[1];
      if (ids.indexOf(id) < 0) ids.push(id);
    }
    if (ids.length === 1) return { ok: true, id: ids[0], cleaned: ids[0], ids: ids };
    let shown = strip(raw);
    if (shown.length > SHOW_MAX) shown = shown.slice(0, SHOW_MAX) + "\u2026";
    return { ok: false, id: null, cleaned: shown, ids: ids };
  }

  return { normalise: normalise, HEX_MIN: HEX_MIN, HEX_MAX: HEX_MAX };
});

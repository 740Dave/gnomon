/* Gnomon — agent-def SIZE display (pure logic, no DOM).
 *
 * Operator ruling (2026-09-29): "No warning: show agent-def SIZE
 * on the dashboard (as a reminder, and a place to launch a def-optimization
 * session with the optimizer agent)." The size is a plain number; above 60K chars it
 * takes a quiet neutral "large" style, never an alarm colour.
 *
 * A size object is the server's agent_def.size_info shape:
 *   { chars, approx_tokens, approx, approx_basis, large }
 * approx_tokens is chars/4 — an APPROXIMATION, always labelled "(approx.)".
 *
 * UMD: window.GmDefSize in the browser, module.exports under node
 * (tests/test_web_agent_def_size.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDefSize = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function groupThousands(n) {
    return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  function compactTokens(n) {
    if (n >= 1000) return (Math.round(n / 100) / 10).toFixed(1) + "K";
    return String(Math.round(n));
  }

  // "70,763 chars · ~17.7K tokens (approx.)"; "" when no size is known.
  function formatDefSize(size) {
    if (!size || typeof size.chars !== "number") return "";
    let out = groupThousands(size.chars) + " chars";
    if (typeof size.approx_tokens === "number") {
      out += " · ~" + compactTokens(size.approx_tokens) + " tokens (approx.)";
    }
    return out;
  }

  // CSS class for the size chip: neutral, plus "is-large" above the threshold.
  function sizeClass(size) {
    return "gm-defsize" + (size && size.large ? " is-large" : "");
  }

  return { formatDefSize: formatDefSize, sizeClass: sizeClass };
});

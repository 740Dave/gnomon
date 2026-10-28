/* Gnomon — Knowledge tab, panel (a): harness usage matrix.
 *
 * /api/usage (the X7 report, scoped to the agent lineages by default)
 * -> a view model -> a table. Every gap is a SIGNAL to investigate, never a
 * defect; every column carries its denominator; a zero at a low-denominator
 * lineage renders "0?" muted (not a confident zero); categories the harness
 * cannot observe are labelled UNOBSERVABLE, not unused.
 *
 * buildView() is pure (no DOM) and is tested under node
 * (tests/test_rmx7b_knowledge_panel.py). render() builds DOM with textContent
 * only — no HTML strings.
 *
 * UMD: window.GmKnowledgeUsage in the browser, module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmKnowledgeUsage = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const SIGNAL = "signal — investigate";

  function plural(n, word) {
    return n + " " + word + (n === 1 ? "" : "s");
  }

  function family(cat) {
    const i = cat.indexOf(":");
    return i < 0 ? cat : cat.slice(0, i);
  }

  // report: the /api/usage body. -> view model (plain data).
  function buildView(report) {
    if (!report || typeof report !== "object") {
      return { error: "no usage report (the server returned nothing)" };
    }
    if (report.enabled === false) {
      return { disabled: true, message: String(report.message || ("enable " + (report.flag || "the flag"))) };
    }
    const lineages = Array.isArray(report.lineages) ? report.lineages : [];
    const den = report.denominator || {};
    const tp = report.test_pollution_excluded || {};
    const label = report.label || SIGNAL;

    const columns = lineages.map(function (lin) {
      const d = den[lin] || { sessions: 0, records: 0, low_denominator: true };
      const parts = [plural(d.sessions, "session"), plural(d.records, "record")];
      if (d.low_denominator) parts.push("low-denominator");
      if (tp[lin]) parts.push(tp[lin] + " test-pollution excluded");
      return {
        lineage: lin,
        header: lin,
        sub: parts.join(" · "),
        cls: "gm-ku-col" + (d.low_denominator ? " is-low" : ""),
        low: !!d.low_denominator,
      };
    });

    // (category, lineage) -> the zero-where-peers-nonzero flag
    const flagged = {};
    const zf = (report.flags && report.flags.zero_where_peers_nonzero) || [];
    zf.forEach(function (f) { flagged[f.category + "\u0000" + f.lineage] = f; });

    let signalCount = 0;
    const cats = Array.isArray(report.categories) ? report.categories : [];
    const matrix = report.matrix || {};
    const rows = cats.map(function (cat) {
      const row = matrix[cat] || {};
      const cells = columns.map(function (col) {
        const n = typeof row[col.lineage] === "number" ? row[col.lineage] : 0;
        const f = flagged[cat + "\u0000" + col.lineage];
        let cls = "gm-ku-cell";
        let text = String(n);
        let title = cat + " · " + col.lineage + ": " + n;
        if (n === 0 && col.low) {
          text = "0?";
          cls += " is-muted";
          title += " (low denominator: not a confident zero)";
        } else if (n === 0) {
          cls += " is-zero";
        }
        if (f) {
          signalCount += 1;
          cls += " is-signal";
          const peers = Object.keys(f.peers_nonzero || {}).map(function (k) {
            return k + "=" + f.peers_nonzero[k];
          }).join(", ");
          title += " — " + (f.label || label) + ": zero here while peers use it (" + peers + ")";
        }
        return { text: text, cls: cls, title: title };
      });
      return { category: cat, family: family(cat), cells: cells };
    });

    const ns = (report.flags && report.flags.never_seen) || [];
    const neverSeen = ns.map(function (f) {
      if (f.observable === false) {
        return {
          category: f.category,
          text: f.category + " — UNOBSERVABLE: " + (f.note || "the harness writes no record for it"),
          cls: "gm-ku-never is-unobservable",
        };
      }
      return {
        category: f.category,
        text: f.category + " — never seen (" + (f.label || label) + ")",
        cls: "gm-ku-never",
      };
    });

    const below = ((report.flags && report.flags.below_expected_rate) || []).map(function (f) {
      return f.category + " · " + f.lineage + ": " + f.rate_per_session + "/session < " +
        f.min_per_session + (f.low_denominator ? " (low-denominator)" : "") + " — " + (f.label || label);
    });

    const polluted = Object.keys(tp).map(function (k) { return k + "=" + tp[k]; });
    return {
      scope: report.scope || "intelligence",
      label: label,
      columns: columns,
      rows: rows,
      neverSeen: neverSeen,
      below: below,
      signalCount: signalCount,
      summary: plural(report.records_in_window || 0, "record") + " · " +
        plural(columns.length, "lineage") + " · " + plural(signalCount, "signal") +
        (polluted.length ? " · test pollution excluded: " + polluted.join(", ") : ""),
    };
  }

  function el(doc, tag, cls, text) {
    const e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined && text !== null) e.textContent = String(text);
    return e;
  }

  function clear(node) {
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  // els: {matrix, never, denominator, status}; doc: the document. textContent only.
  function render(els, view, doc) {
    doc = doc || document;
    [els.matrix, els.never, els.denominator].forEach(function (n) { if (n) clear(n); });
    if (view.error || view.disabled) {
      if (els.status) els.status.textContent = "";
      const msg = view.error ? "⛔ " + view.error : view.message;
      els.matrix.appendChild(el(doc, "div", "gm-empty", msg));
      return;
    }
    if (els.status) els.status.textContent = view.summary;
    if (els.denominator) {
      els.denominator.appendChild(el(doc, "div", "gm-ku-legend",
        "Every highlighted cell is a " + view.label + ", never a defect. " +
        "'0?' = zero at a low-denominator lineage (fewer sessions than the threshold) — not a confident zero."));
    }
    if (!view.columns.length) {
      els.matrix.appendChild(el(doc, "div", "gm-empty", "No harness records in this scope yet."));
    } else {
      const table = el(doc, "table", "gm-ku-table");
      const thead = el(doc, "thead");
      const hr = el(doc, "tr");
      hr.appendChild(el(doc, "th", "gm-ku-cat", "category"));
      view.columns.forEach(function (c) {
        const th = el(doc, "th", c.cls);
        th.appendChild(el(doc, "div", "gm-ku-col-name", c.header));
        th.appendChild(el(doc, "div", "gm-ku-col-sub", c.sub));
        hr.appendChild(th);
      });
      thead.appendChild(hr);
      table.appendChild(thead);
      const tbody = el(doc, "tbody");
      let fam = null;
      view.rows.forEach(function (r) {
        const tr = el(doc, "tr", r.family !== fam && fam !== null ? "gm-ku-row is-family-start" : "gm-ku-row");
        fam = r.family;
        tr.appendChild(el(doc, "td", "gm-ku-cat", r.category));
        r.cells.forEach(function (c) {
          const td = el(doc, "td", c.cls, c.text);
          td.title = c.title;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      els.matrix.appendChild(table);
    }
    if (els.never) {
      els.never.appendChild(el(doc, "h3", "gm-ku-subhead",
        "Known to the harness, never seen (" + view.neverSeen.length + ")"));
      view.neverSeen.forEach(function (n) { els.never.appendChild(el(doc, "div", n.cls, n.text)); });
      if (view.below.length) {
        els.never.appendChild(el(doc, "h3", "gm-ku-subhead", "Below declared expected rate"));
        view.below.forEach(function (t) { els.never.appendChild(el(doc, "div", "gm-ku-never", t)); });
      }
    }
  }

  return { buildView: buildView, render: render, SIGNAL: SIGNAL };
});

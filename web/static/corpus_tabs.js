/* Gnomon - corpus-tab presentation logic (pure, no DOM; X13 S5-3).
 *
 * A "corpus tab" is declared by the DEPLOYMENT (defaults/corpus-tabs.json, served by /api/corpus-tabs); Gnomon ships
 * none. Every deployment-specific name (the corpus, the roster node, the grouping properties, the chip rules) is data in
 * that declaration; this module only turns (tab declaration, server payload) into what app.js renders:
 *
 *   groupItems(items, by, tab, rosterProps) -> [{key, label, note, items}]   the grouped master list
 *   detailModel(node, tab)                  -> {chips, lede, chipList, featured, rest, restHeading}
 *
 * UMD: window.GmCorpusTabs in the browser, module.exports under node (tests/test_x13s5_3_corpus_tabs.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmCorpusTabs = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  function humanKey(k) {
    const s = String(k).replace(/-/g, " ");
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  function fill(template, value) {
    return String(template).split("{value}").join(String(value));
  }

  // A roster property's string value (the group notes come from the roster node itself).
  function rosterProp(rosterProps, key) {
    const p = (rosterProps || []).find((x) => x && x.key === key);
    return p && typeof p.value === "string" ? p.value : "";
  }

  // Group `items` by the property `by`; groups sorted by key; an unset value is "(unset)".
  function groupItems(items, by, tab, rosterProps) {
    const groups = {};
    (items || []).forEach((it) => {
      const k = (it && it[by]) || "(unset)";
      (groups[k] = groups[k] || []).push(it);
    });
    const labels = (tab && tab.group_labels) || {};
    const notes = (tab && tab.group_notes) || {};
    return Object.keys(groups).sort().map((k) => ({
      key: k,
      label: labels[by] ? fill(labels[by], k) : humanKey(k),
      note: notes[by] ? rosterProp(rosterProps, fill(notes[by], k)) : "",
      items: groups[k],
    }));
  }

  // The detail pane model for one node.
  function detailModel(node, tab) {
    const pmap = {};
    ((node && node.properties) || []).forEach((p) => { pmap[p.key] = p.value; });
    const chips = [];
    (tab.chips || []).forEach((c) => {
      const v = pmap[c.property];
      if (!v || typeof v !== "string") return;
      const text = c.text ? fill(c.text, v) : (c.humanize ? humanKey(v) : v);
      let cls = "";
      if (c.accent) cls = "is-accent";
      else if ((c.warn_values || []).indexOf(v) >= 0) cls = "is-warn";
      chips.push({ text: text, cls: cls });
    });
    let lede = null;
    if (tab.lede && typeof pmap[tab.lede.property] === "string") {
      lede = { label: tab.lede.label, text: pmap[tab.lede.property] };
    }
    let chipList = null;
    if (tab.chip_list && typeof pmap[tab.chip_list.property] === "string") {
      const strip = tab.chip_list.strip_prefix || "";
      chipList = {
        label: tab.chip_list.label,
        items: pmap[tab.chip_list.property].split(",").map((r) => r.trim()).filter(Boolean)
          .map((r) => (strip && r.indexOf(strip) === 0 ? r.slice(strip.length) : r)),
      };
    }
    const listKey = tab.chip_list ? tab.chip_list.property : null;
    const featured = (tab.featured || []).filter((k) => k !== listKey && k in pmap)
      .map((k) => ({ key: k, value: pmap[k] }));
    const hidden = (tab.featured || []).concat(tab.in_header || []);
    const rest = ((node && node.properties) || []).filter((p) => hidden.indexOf(p.key) < 0);
    const restHeading = tab.roster_id && node && node.id === tab.roster_id
      ? tab.roster_heading : tab.other_heading;
    return { chips: chips, lede: lede, chipList: chipList, featured: featured, rest: rest, restHeading: restHeading };
  }

  return { humanKey: humanKey, groupItems: groupItems, detailModel: detailModel };
});

/* Gnomon - DSD documents: an Open button per attached document.
 *
 * A DSD (and each decision) can list documents with a `**Documents:** path; path` line.
 * The server sends, per document, {scope, index, name, path, openable, refusal,
 * refusal_reason, sent_status, sent_sha256, warning, changed, missing_now}. This module
 * shows ONE button per document labelled with the BASENAME ONLY; the full path is in the
 * tooltip. It never sends a path: Open posts {dsd_id, scope, index} and the server opens
 * only a document the stored record lists (policy re-checked server-side; the button
 * state here is advisory).
 *
 * "changed since sent" = the server compared the file's CURRENT sha256 with the one
 * recorded when the DSD was sent. A missing-at-send / unrecorded / refused document
 * shows its warning loudly.
 *
 * UMD: window.GmDsdDocs in the browser, module.exports under node
 * (tests/test_web_dsd_documents.py). The DOM-building function takes the document
 * object as a parameter so the node tests can drive it with a tiny stub.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDsdDocs = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // The label shown on a button: the basename only, whatever the server sent.
  function basename(s) {
    const t = String(s == null ? "" : s).replace(/\\/g, "/").replace(/\/+$/, "");
    const i = t.lastIndexOf("/");
    return i >= 0 ? t.slice(i + 1) : t;
  }

  // The server keys this view reads or deliberately ignores (g50: a new server key fails the
  // real-payload test until this list, and the view, decide what to do with it).
  const KNOWN_KEYS = [
    "scope", "index", "name", "path", "sent_status", "sent_sha256", "sent_size", "sent_mtime",
    "warning", "openable", "refusal", "refusal_reason", "current_sha256", "changed", "missing_now",
  ];

  // Pure view model of one document.
  function buttonModel(doc, opts) {
    const d = doc || {};
    const showScope = !!(opts && opts.showScope);
    const name = d.name ? basename(d.name) : basename(d.path);
    const notes = [];
    if (d.changed) notes.push("changed since sent");
    if (d.missing_now) notes.push("missing now");
    if (d.sent_status && d.sent_status !== "ok") notes.push("not recorded at send: " + d.sent_status);
    const disabled = d.openable === false;
    let title = String(d.path || "");
    if (disabled && d.refusal) title += "\n\nCannot open (" + d.refusal + "): " + (d.refusal_reason || "");
    if (d.changed) title += "\n\nThis file's content differs from what was sent with the DSD.";
    if (d.warning) title += "\n\n" + d.warning;
    return {
      label: "Open " + name,
      name: name,
      title: title,
      disabled: disabled,
      changed: !!d.changed,
      notes: notes,
      warning: String(d.warning || ""),
      scope: String(d.scope || ""),
      scopeLabel: showScope ? (d.scope === "top" ? "DSD" : String(d.scope || "")) : "",
      index: d.index,
    };
  }

  // POST the Open request. Returns {ok, message}. `apiFn(path, opts)` is the page's
  // fetch wrapper (throws Error(detail) on a non-2xx).
  async function openDocument(apiFn, dsdId, doc) {
    try {
      await apiFn("/api/dsd/documents/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ dsd_id: dsdId, scope: doc.scope, index: doc.index }),
      });
      return { ok: true, message: "" };
    } catch (e) {
      return { ok: false, message: String((e && e.message) || e) };
    }
  }

  // Build the documents panel into `host`. `ctx` = {api, toast}. `opts.showScope` adds a
  // scope prefix (the browse view lists every scope in one panel). No-op for no documents.
  function mount(host, docs, dsdId, ctx, opts, documentObj) {
    const dom = documentObj || (typeof document !== "undefined" ? document : null);
    if (!host || !dom || !Array.isArray(docs) || docs.length === 0) return null;
    const panel = dom.createElement("div");
    panel.className = "gm-dsd-docs";
    docs.forEach((doc) => {
      const m = buttonModel(doc, opts);
      const row = dom.createElement("div");
      row.className = "gm-dsd-doc" + (m.changed ? " is-changed" : "") + (m.warning ? " has-warning" : "");
      if (m.scopeLabel) {
        const sc = dom.createElement("span");
        sc.className = "gm-dsd-doc-scope";
        sc.textContent = m.scopeLabel;
        row.appendChild(sc);
      }
      const btn = dom.createElement("button");
      btn.className = "gm-btn gm-dsd-doc-btn";
      btn.type = "button";
      btn.textContent = m.label;
      btn.title = m.title;
      btn.disabled = m.disabled;
      btn.addEventListener("click", async () => {
        btn.disabled = true;
        const r = await openDocument(ctx.api, dsdId, doc);
        btn.disabled = m.disabled;
        if (ctx.toast) ctx.toast(r.ok ? "Opened " + m.name : "Could not open " + m.name + ": " + r.message, !r.ok);
      });
      row.appendChild(btn);
      m.notes.forEach((n) => {
        const b = dom.createElement("span");
        b.className = "gm-dsd-doc-badge" + (n === "changed since sent" ? " is-changed" : "");
        b.textContent = n;
        row.appendChild(b);
      });
      panel.appendChild(row);
    });
    host.appendChild(panel);
    return panel;
  }

  return { basename, buttonModel, openDocument, mount, KNOWN_KEYS };
});

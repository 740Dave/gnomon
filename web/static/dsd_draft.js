/* Gnomon — DSD in-progress-answer protection (pure logic, no DOM).
 *
 * operator-observed defect 2026-09-29: a DSD arriving mid-answer auto-switched the
 * page and every chosen answer + comment was lost. This module holds the pure
 * decisions app.js consults, kept DOM-free so they are behaviour-tested under
 * node (tests/test_web_dsd_draft.py):
 *
 *   hasOperatorInput / shouldAutoSwitch — never hijack a form with input;
 *   completeness — the pure Save-gate mirror (the server is the authority);
 *   serializeDraft / parseDraft / restoreSelections — per-dsd_id drafts;
 *   makeReviewActivity — the throttled "operator is reviewing <dsd_id>" beacon
 *     (first interaction immediately, then at most 1 / 10 s per dsd_id; the
 *     payload is {dsd_id} ONLY — never a choice or a comment);
 *   makeDraftStore — localStorage wrapper; EVERY access is try/catch'd, and an
 *     unavailable/throwing/full storage degrades to "no drafts", never a throw.
 *
 * A "selections" map is { dec_id: { option_id, comment } } — the same shape as
 * app.js currentSelections(), except comments are kept RAW (untrimmed) so a
 * restored draft is exactly what was typed.
 *
 * UMD: window.GmDsdDraft in the browser, module.exports under node.
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmDsdDraft = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const KEY_PREFIX = "gm.dsd.draft.";
  const VERSION = 1;

  function draftKey(dsdId) {
    return KEY_PREFIX + String(dsdId);
  }

  // NO PRE-SELECTION (operator, 2026-09-29): every radio starts UNSET, so the
  // operator's assent to a recommendation is an explicit, captured choice. The
  // recommendation is only a badge; nothing here reads the legacy
  // is_preselected flag (a stale server may still send it).
  function recommendedOptionId(dec) {
    if (dec && dec.recommended_id != null) return dec.recommended_id;
    const opts = (dec && dec.options) || [];
    for (let i = 0; i < opts.length; i++) {
      if (opts[i].is_recommended) return opts[i].option_id;
    }
    return null;
  }

  function hasOption(dec, optionId) {
    return ((dec && dec.options) || []).some((o) => o.option_id === optionId);
  }

  // Operator input = on ANY decision, ANY chosen option (the recommendation
  // included — nothing is pre-selected any more), or a non-blank comment.
  function hasOperatorInput(form, selections) {
    if (!form || !Array.isArray(form.decisions)) return false;
    const sel = selections || {};
    return form.decisions.some((dec) => {
      const s = sel[dec.id] || {};
      if (s.option_id != null && s.option_id !== "") return true;
      return String(s.comment || "").trim() !== "";
    });
  }

  // The Save-gate MIRROR of the server rule (the server is the authority):
  // every decision needs an EXPLICIT choice (the recommendation is never an
  // answer by default); the escape hatch counts as a choice but requires a
  // non-blank comment. Returns { ok, reasons }.
  function completeness(form, selections) {
    const reasons = [];
    if (!form || !Array.isArray(form.decisions)) return { ok: false, reasons };
    const sel = selections || {};
    form.decisions.forEach((dec) => {
      const s = sel[dec.id] || {};
      if (s.option_id == null || s.option_id === "") {
        reasons.push(dec.id + ": no option selected");
        return;
      }
      if (s.option_id === dec.escape_hatch_id && !String(s.comment || "").trim()) {
        reasons.push(dec.id + ": the escape hatch requires a comment");
      }
    });
    return { ok: reasons.length === 0, reasons };
  }

  // Auto-switch to an arriving DSD only when the presented form (if any) has no
  // operator input. A pristine form keeps today's operator-requested auto-switch.
  function shouldAutoSwitch(form, selections) {
    return !hasOperatorInput(form, selections);
  }

  // The poll's whole decision: nothing arrived -> "none"; the presented form has
  // operator input -> "hold" (banner, no switch); else -> "switch" to the newest.
  function decideArrival(arrivals, form, selections) {
    if (!Array.isArray(arrivals) || !arrivals.length) return { action: "none", arrival: null };
    return { action: shouldAutoSwitch(form, selections) ? "switch" : "hold",
             arrival: arrivals[0] };
  }

  // Compact fingerprint of a decision's option ids + labels (djb2 over the
  // joined text). A draft choice is only restored while this still matches.
  function labelsFingerprint(dec) {
    const txt = ((dec && dec.options) || [])
      .map((o) => o.option_id + "\u0001" + o.label).join("\u0002");
    let h = 5381;
    for (let i = 0; i < txt.length; i++) h = ((h * 33) ^ txt.charCodeAt(i)) >>> 0;
    return h.toString(16);
  }

  function arrivalBannerText(arrival, moreCount) {
    const who = (arrival && arrival.instance_id) || "an agent";
    let t = "New DSD from " + who + " arrived";
    if (moreCount > 0) t += " (+" + moreCount + " more)";
    return t;
  }

  function serializeDraft(dsdId, selections, savedAt, form) {
    const fps = {};
    ((form && form.decisions) || []).forEach((dec) => { fps[dec.id] = labelsFingerprint(dec); });
    const clean = {};
    Object.keys(selections || {}).forEach((k) => {
      const s = selections[k] || {};
      clean[k] = {
        option_id: s.option_id == null ? null : String(s.option_id),
        comment: String(s.comment || ""),
      };
      if (fps[k]) clean[k].fp = fps[k];
    });
    return JSON.stringify({ v: VERSION, dsd_id: String(dsdId),
                            saved_at: savedAt || null, selections: clean });
  }

  // Returns { dsd_id, saved_at, selections } or null for anything unusable:
  // missing, corrupt JSON, wrong version, wrong dsd_id, wrong shape.
  function parseDraft(raw, dsdId) {
    if (raw == null) return null;
    let d;
    try {
      d = JSON.parse(raw);
    } catch (e) {
      return null;
    }
    if (!d || typeof d !== "object" || d.v !== VERSION) return null;
    if (d.dsd_id !== String(dsdId)) return null;
    if (!d.selections || typeof d.selections !== "object") return null;
    return { dsd_id: d.dsd_id, saved_at: d.saved_at || null, selections: d.selections };
  }

  // The selections to APPLY to `form`: EXACTLY the draft where it still fits
  // the form, UNSET elsewhere (the pristine form checks nothing). A draft option
  // id the form no longer offers resets to unset; draft entries for decisions
  // the form no longer has are dropped (they stay in storage untouched until
  // next save). Drafts saved by the pre-2026-09-29 page (when the
  // recommendation was pre-checked) restore exactly what they hold — same key,
  // same v:1 shape.
  function restoreSelections(form, draft) {
    const out = {};
    if (!form || !Array.isArray(form.decisions)) return out;
    const ds = (draft && draft.selections) || {};
    form.decisions.forEach((dec) => {
      const s = ds[dec.id];
      let optionId = null;
      let comment = "";
      if (s && typeof s === "object") {
        // labels changed since the draft (fp mismatch): the choice may now mean
        // something else, so it resets to unset; the comment stays.
        const fresh = !s.fp || s.fp === labelsFingerprint(dec);
        if (fresh && s.option_id != null && hasOption(dec, s.option_id)) optionId = s.option_id;
        comment = typeof s.comment === "string" ? s.comment : "";
      }
      out[dec.id] = { option_id: optionId, comment: comment };
    });
    return out;
  }

  // DSD review activity -> operator presence (operator, 2026-09-29). note(dsdId) calls
  // send({dsd_id}) immediately on the first interaction with a DSD and then at
  // most once per `interval` ms per dsd_id (leading-edge throttle; the next
  // keystroke after the window sends again). send is fire-and-forget: its
  // result is ignored and a throw is swallowed, so input handling never waits on
  // or breaks because of presence. Returns whether a beacon was sent.
  const REVIEW_ACTIVITY_INTERVAL_MS = 10000;
  function makeReviewActivity(send, opts) {
    const o = opts || {};
    const interval = typeof o.interval === "number" ? o.interval : REVIEW_ACTIVITY_INTERVAL_MS;
    const now = typeof o.now === "function" ? o.now : () => Date.now();
    const last = {};
    return {
      note(dsdId) {
        if (dsdId == null || dsdId === "") return false;
        const id = String(dsdId);
        const t = now();
        const prev = last[id];
        if (prev != null && t - prev < interval) return false;
        last[id] = t;
        try {
          send({ dsd_id: id });
        } catch (e) {
          /* presence is advisory; never break the form */
        }
        return true;
      },
    };
  }

  // storageGetter() returns a Storage-like object (or throws / returns null —
  // e.g. Safari private mode, a disabled-storage policy). Resolved per call so
  // one failure never poisons the page. Return values: save/clear -> true when
  // the storage write happened, false otherwise; load -> draft or null.
  function makeDraftStore(storageGetter) {
    function storage() {
      try {
        const s = storageGetter();
        return s && typeof s.getItem === "function" ? s : null;
      } catch (e) {
        return null;
      }
    }
    return {
      load(dsdId) {
        const s = storage();
        if (!s) return null;
        try {
          return parseDraft(s.getItem(draftKey(dsdId)), dsdId);
        } catch (e) {
          return null;
        }
      },
      // Saves when the form has input; REMOVES the draft when the operator has
      // returned the form to pristine (nothing left worth restoring).
      save(dsdId, form, selections) {
        const s = storage();
        if (!s) return false;
        try {
          if (!hasOperatorInput(form, selections)) {
            s.removeItem(draftKey(dsdId));
          } else {
            s.setItem(draftKey(dsdId),
                      serializeDraft(dsdId, selections, new Date().toISOString(), form));
          }
          return true;
        } catch (e) {
          return false;  // quota / security error: the page keeps working
        }
      },
      clear(dsdId) {
        const s = storage();
        if (!s) return false;
        try {
          s.removeItem(draftKey(dsdId));
          return true;
        } catch (e) {
          return false;
        }
      },
    };
  }

  return {
    KEY_PREFIX, draftKey, recommendedOptionId, hasOperatorInput, completeness, shouldAutoSwitch,
    decideArrival, makeReviewActivity, REVIEW_ACTIVITY_INTERVAL_MS, labelsFingerprint, arrivalBannerText, serializeDraft, parseDraft, restoreSelections, makeDraftStore,
  };
});

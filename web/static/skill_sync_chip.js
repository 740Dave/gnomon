/* Gnomon - skill-sync chip (pure logic, no DOM).
 *
 * /api/sessions' `skill_sync` ({decision, ok, label, counts, written, blocked, corpus} | null, gated by
 * sync_my_skills) -> a chip such as "skills applied: 1 written" on the session row, so the operator
 * sees which agent generated skills and what was refused. Plain strings only; the caller renders with
 * textContent. Counts and skill names only, never file content.
 *
 *   index.html:  <script src="/static/skill_sync_chip.js"></script>  (before app.js)
 *   app.js row:  const kc = window.GmSkillSyncChip ? GmSkillSyncChip.skillSyncChip(s.skill_sync) : null;
 *                if (kc) row.appendChild(noticeChipEl(kc));
 *
 * UMD: window.GmSkillSyncChip in the browser, module.exports under node (tests/test_x49_sync_my_skills.py).
 */
(function (root, factory) {
  "use strict";
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.GmSkillSyncChip = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // q: the /api/sessions row's `skill_sync`. -> {text, cls, title} | null
  function skillSyncChip(q) {
    if (!q || typeof q !== "object" || typeof q.label !== "string" || !q.label) return null;
    const names = Array.isArray(q.written) ? q.written.filter(function (x) { return typeof x === "string"; }) : [];
    const blocked = Array.isArray(q.blocked) ? q.blocked.filter(function (x) { return typeof x === "string"; }) : [];
    let title = "The agent's last sync_my_skills call" + (q.corpus ? " (corpus " + q.corpus + ")" : "") + ".";
    if (names.length) title += " Written: " + names.join(", ") + ".";
    if (blocked.length) title += " Left untouched (name taken or contract refused): " + blocked.join(", ") + ".";
    if (q.ok === false) title += " The call did not succeed.";
    return { text: q.label, cls: "gm-skill-sync-chip", title: title };
  }

  return { skillSyncChip: skillSyncChip };
});

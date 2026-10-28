"""the Apps tab poll refreshes the SELECTED app's detail pane, so
Open (and the full link) follow an app up from starting to running without a select or a Refresh click; the pane
shows the full UI link from the server (the same builder the Open action uses); a poll tick never wipes unsaved
input; the in-flight guard (GmPoll.wrap) still stops requests stacking. Node behaviour tests on a DOM stub; the
server half runs the real AppsHost on the launchd double."""

import copy
import json
import os
import shutil
import subprocess

import pytest

from appshost_support import ROOT, Env
from gnomon.appshost.host import Actor
from gnomon.feature_events import VIEWER_ACTOR

NODE = shutil.which("node")
JS = os.path.join(ROOT, "web", "apps_ui", "apps_tab.js")
POLLGUARD = os.path.join(ROOT, "web", "static", "poll_guard.js")
operator = Actor(VIEWER_ACTOR, False)

_MANIFEST = {"id": "adj", "name": "Adj", "version": "1", "purpose": "p", "owner": "owner-b", "schema_version": 1, "scope": "all",
             "run_at_boot_default": False, "start": ["../run.sh"], "port_arg": ["--port", "{port}"], "health_path": "/h",
             "ui_path": "/ui/", "headless": False, "paths": [], "backup_before_first_start": [], "requires": [],
             "requires_files": [], "licences": [], "hardware": None, "user_editable": [], "env": {},
             "stop": {"signal": "SIGTERM", "grace": 10}, "ui_token": None, "actions": []}
INSTALLED = {"ok": True, "app_id": "adj", "installed": True, "port": 8079, "manifest_raw": "{}", "manifest": _MANIFEST,
             "ui_url": "http://127.0.0.1:8079/ui/",
             "status": {"status": "starting", "label": "starting", "warnings": []}, "last_start": None, "last_stop": None,
             "settings": {"run_at_boot": False, "scope": "all", "scope_ceiling": "all", "paths": [],
                          "user": [{"name": "who", "type": "text", "value": "owner", "default": "owner", "options": [], "kind": None, "description": ""},
                    {"name": "mode", "type": "choice", "value": "a", "default": "a", "options": ["a", "b"], "kind": None, "description": ""}]},
             "licences": [], "backups": {}}
PROPOSAL = {"ok": True, "app_id": "adj", "installed": False, "valid": True, "invalid_reason": None,
            "proposal": {"kind": "install", "proposed_by": "x", "proposed_by_verified": False}, "manifest": _MANIFEST,
            "manifest_raw": "{}", "to_fill": [{"name": "store", "description": "s", "kind": "file", "access": "writes", "optional": False, "flag": None}]}

_PROG = r"""
const fs = require('fs');
const A = require(process.argv[1]);
const Z = new Function('module', 'exports', fs.readFileSync(process.argv[2], 'utf8'));
const zm = { exports: {} }; Z(zm, zm.exports); const ZP = zm.exports;
function mk(tag) {
  const n = { tag, children: [], className: '', attrs: {}, listeners: {}, id: '', _text: '', value: '', checked: false, disabled: false, type: '', placeholder: '', href: '' };
  Object.defineProperty(n, 'textContent', { get() { return n._text + n.children.map(c => c.textContent).join(''); }, set(v) { n.children = []; n._text = String(v); } });
  const cls = () => new Set(n.className.split(' ').filter(Boolean));
  n.classList = { add(c) { const s = cls(); s.add(c); n.className = [...s].join(' '); }, remove(c) { const s = cls(); s.delete(c); n.className = [...s].join(' '); },
    contains(c) { return cls().has(c); }, toggle(c, on) { const s = cls(); const w = on === undefined ? !s.has(c) : !!on; w ? s.add(c) : s.delete(c); n.className = [...s].join(' '); } };
  n.appendChild = c => { n.children.push(c); c.parent = n; return c; };
  n.insertBefore = (c, ref) => { const i = n.children.indexOf(ref); n.children.splice(i < 0 ? n.children.length : i, 0, c); c.parent = n; return c; };
  Object.defineProperty(n, 'nextSibling', { get() { const p = n.parent; return p ? (p.children[p.children.indexOf(n) + 1] || null) : null; } });
  Object.defineProperty(n, 'parentNode', { get() { return n.parent || null; } });
  Object.defineProperty(n, 'firstChild', { get() { return n.children[0] || null; } });
  n.addEventListener = (t, f) => { (n.listeners[t] = n.listeners[t] || []).push(f); };
  n.setAttribute = (k, v) => { n.attrs[k] = String(v); };
  n.getAttribute = k => n.attrs[k];
  n.click = async () => { for (const f of (n.listeners.click || [])) await f({}); };
  n.querySelector = sel => { let r = null; walk(n, x => { if (!r && x !== n && matches(x, sel)) r = x; }); return r; };
  n.querySelectorAll = sel => { const o = []; walk(n, x => { if (x !== n && matches(x, sel)) o.push(x); }); return o; };
  return n;
}
function walk(n, f) { f(n); n.children.forEach(c => walk(c, f)); }
function matches(n, sel) {
  const m = sel.match(/^([a-z]*)((?:\.[\w-]+)*)(?:\[([\w-]+)(?:="?([\w-]+)"?)?\])?$/);
  if (!m) return false;
  if (m[1] && n.tag !== m[1]) return false;
  if (!m[2].split('.').filter(Boolean).every(c => n.classList.contains(c))) return false;
  if (m[3] && !(m[3] in n.attrs)) return false;
  if (m[3] && m[4] !== undefined && n.attrs[m[3]] !== m[4]) return false;
  return true;
}
const body = mk('body'), head = mk('head');
const header = mk('header'); header.className = 'gm-header';
const tabbar = mk('nav'); tabbar.id = 'tabbar';
const main = mk('main'); main.className = 'gm-content';
const p0 = mk('section'); p0.className = 'gm-panel is-active';
[header, tabbar, main].forEach(x => body.appendChild(x)); main.appendChild(p0);
const doc = { body, head, readyState: 'complete', createElement: mk,
  createTextNode: t => { const n = mk('#text'); n._text = t; return n; },
  getElementById(id) { let r = null; walk(body, n => { if (n.id === id) r = n; }); return r; },
  querySelector(sel) { let r = null; walk(body, n => { if (!r && matches(n, sel)) r = n; }); return r; },
  querySelectorAll(sel) { const o = []; walk(body, n => { if (matches(n, sel)) o.push(n); }); return o; } };
const SCEN = __SCEN__;
let detail = SCEN.detail;               // what GET /api/apps/adj returns right now (mutable by the script)
let failDetail = false;
let holdDetail = null;                  // when set, GET /api/apps/adj waits on this promise
const log = [];
const intervals = [];
const win = { document: doc, setInterval: (f) => { intervals.push(f); return 1; }, confirm: () => true,
  GmPoll: { wrap: (n, f) => ZP.wrap(n, f, 4, 60000) },
  fetch: async (url, opts) => {
    log.push([url, (opts && opts.method) || 'GET', !!(opts && opts.signal), opts && opts.body]);
    if (url === '/api/apps') return { ok: true, status: 200, json: async () => ({ ok: true, csrf: 'c', apps: [{ id: 'adj', name: 'Adj', status: 'running', label: 'running', scope: 'all' }], pending: SCEN.pending || [] }) };
    if (url === '/api/apps/adj') { if (holdDetail) await holdDetail; if (failDetail) return { ok: false, status: 500, json: async () => ({ error: 'boom' }) }; const d = detail; return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(d)) }; }
    return { ok: true, status: 200, json: async () => ({ ok: true }) };
  } };
const tick = async () => { for (let i = 0; i < 12; i++) await new Promise(r => setImmediate(r)); };
const buttons = (root, label) => { const o = []; walk(root, n => { if (n.tag === 'button' && n.textContent === label) o.push(n); }); return o; };
const anchors = root => { const o = []; walk(root, n => { if (n.tag === 'a') o.push(n); }); return o; };
const detailGets = () => log.filter(c => c[0] === '/api/apps/adj' && c[1] === 'GET').length;
(async () => {
  A.init(win); await tick();
  await doc.querySelector('.gm-tab[data-tab="apps"]').click(); await tick();
  const panel = doc.getElementById('panel-apps');
  let item = null; walk(panel, n => { if (!item && n.className.includes('gm-gv-item')) item = n; });
  await item.click(); await tick();
  const poll = intervals[0];
  const out = {};
  __SCRIPT__
  console.log(JSON.stringify(out));
  process.exit(0);
})();
"""


def run(scen, script):
    prog = _PROG.replace("__SCEN__", json.dumps(scen)).replace("__SCRIPT__", script)
    r = subprocess.run([NODE, "-e", prog, JS, POLLGUARD], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _with(**status):
    d = copy.deepcopy(INSTALLED)
    d["status"] = dict(d["status"], **status)
    return d


pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def test_detail_going_from_starting_to_running_enables_open_on_one_poll_tick():
    out = run({"detail": INSTALLED}, r"""
      out.before = buttons(panel, 'Open')[0].disabled;
      const gets0 = detailGets();
      detail = SCEN_RUNNING;
      await poll(); await tick();
      out.after = buttons(panel, 'Open')[0].disabled;
      out.detailGetsDuringTick = detailGets() - gets0;
      out.detailGetCarriedSignal = log.filter(c => c[0] === '/api/apps/adj' && c[1] === 'GET').slice(-1)[0][2];
    """.replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["before"] is True and out["after"] is False
    assert out["detailGetsDuringTick"] == 1 and out["detailGetCarriedSignal"] is True


def test_link_is_shown_clickable_when_running_text_when_not_and_absent_when_headless():
    out = run({"detail": _with(status="running", label="running")}, r"""
      const a1 = anchors(panel);
      out.running = a1.map(a => [a.href, a.textContent, a.target]);
      detail = SCEN_STOPPED; await poll(); await tick();
      out.stoppedAnchors = anchors(panel).length; out.stoppedText = panel.textContent.includes('http://127.0.0.1:8079/ui/');
      detail = SCEN_HEADLESS; await poll(); await tick();
      out.headlessAnchors = anchors(panel).length; out.headlessText = panel.textContent.includes('http://127.0.0.1');
    """.replace("SCEN_STOPPED", json.dumps(_with(status="stopped", label="stopped"))).replace("SCEN_HEADLESS", json.dumps(_headless())))
    assert out["running"] == [["http://127.0.0.1:8079/ui/", "http://127.0.0.1:8079/ui/", "_blank"]]
    assert out["stoppedAnchors"] == 0 and out["stoppedText"] is True            # shown as text, not clickable
    assert out["headlessAnchors"] == 0 and out["headlessText"] is False


def _headless():
    d = _with(status="running", label="running")
    d["manifest"] = dict(d["manifest"], ui_path=None, headless=True)
    d["ui_url"] = None
    return d


def test_a_poll_tick_does_not_wipe_unsaved_settings_input_and_applies_once_clean():
    out = run({"detail": INSTALLED}, r"""
      const inp = panel.querySelector('input[type]') && null;
      let who = null; walk(panel, n => { if (n.tag === 'input' && n.type === 'text' && n.attrs['data-setting'] === 'who') who = n; });
      out.found = !!who;
      who.value = 'typed-but-unsaved';
      detail = SCEN_RUNNING; await poll(); await tick();
      let who2 = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who2 = n; });
      out.sameNode = who2 === who; out.value = who2.value;
      out.openStillDisabled = buttons(panel, 'Open')[0].disabled;     // skipped tick: pane untouched
      who.value = 'owner';                                            // operator reverts: pane clean again
      await poll(); await tick();
      out.openAfterClean = buttons(panel, 'Open')[0].disabled;
    """.replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["found"] and out["sameNode"] is True and out["value"] == "typed-but-unsaved"
    assert out["openStillDisabled"] is True and out["openAfterClean"] is False


def test_an_unchanged_detail_is_not_re_rendered_by_the_tick():
    out = run({"detail": INSTALLED}, r"""
      const bO = buttons(panel, 'Open')[0];
      await poll(); await tick();
      out.sameButton = buttons(panel, 'Open')[0] === bO;
    """)
    assert out["sameButton"] is True


def test_the_in_flight_guard_holds_a_slow_detail_request_is_not_stacked():
    out = run({"detail": INSTALLED}, r"""
      let release; holdDetail = new Promise(r => { release = r; });
      const g0 = detailGets();
      const p1 = poll();                      // list resolves, detail request now waits on holdDetail
      await tick();
      const p2 = poll(); const p3 = poll(); await tick();
      out.detailGetsWhileSlow = detailGets() - g0;
      out.listGetsWhileSlow = log.filter(c => c[0] === '/api/apps' && c[1] === 'GET').length;
      release(); holdDetail = null; await p1; await tick();
      const g1 = detailGets();
      await poll(); await tick();
      out.detailGetsAfterFree = detailGets() - g1;
    """)
    assert out["detailGetsWhileSlow"] == 1            # three ticks, one detail request in flight
    assert out["detailGetsAfterFree"] == 1            # the slot frees afterwards


# ---------------------------------------------------------------- server: one URL builder for Open and detail
def test_detail_ui_url_equals_the_url_the_open_action_hands_the_browser(tmp_path):
    e = Env(tmp_path)
    try:
        a = e.install(e.manifest("viewer", ui_path="/ui/"))
        e.host.start(a, operator)
        e.wait_status(a, "running")
        d = e.host.detail(a)
        r = e.host.open(a, operator)
        assert d["ui_url"] == r["url"] == e.opened[-1] == f"http://127.0.0.1:{e.host._port(a)}/ui/"
        assert "token" not in d["ui_url"]
    finally:
        e.cleanup()


def test_detail_ui_url_is_none_for_a_headless_app(tmp_path):
    e = Env(tmp_path)
    try:
        a = e.install(e.manifest("daemon", mode="sleep", with_port=False))
        assert e.host.detail(a)["ui_url"] is None
    finally:
        e.cleanup()


def test_a_poll_tick_does_not_wipe_a_changed_select_and_applies_once_reverted():
    out = run({"detail": INSTALLED}, r"""
      const sels = []; walk(panel, n => { if (n.tag === 'select') sels.push(n); });
      out.selects = sels.length;
      const sel = sels[0]; const orig = sel.value;
      sel.value = 'b';
      detail = SCEN_RUNNING; await poll(); await tick();
      const again = []; walk(panel, n => { if (n.tag === 'select') again.push(n); });
      out.sameNode = again[0] === sel; out.value = again[0].value;
      out.openStillDisabled = buttons(panel, 'Open')[0].disabled;
      sel.value = orig; await poll(); await tick();
      out.openAfterRevert = buttons(panel, 'Open')[0].disabled;
    """.replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["selects"] == 1 and out["sameNode"] is True and out["value"] == "b"
    assert out["openStillDisabled"] is True and out["openAfterRevert"] is False


def test_a_focused_but_unchanged_control_blocks_the_tick():
    out = run({"detail": INSTALLED}, r"""
      let who = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who = n; });
      doc.activeElement = who;                       # placeholder replaced below
      detail = SCEN_RUNNING; await poll(); await tick();
      out.blocked = buttons(panel, 'Open')[0].disabled;
      doc.activeElement = null; await poll(); await tick();
      out.freed = buttons(panel, 'Open')[0].disabled;
    """.replace("#", "//").replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["blocked"] is True and out["freed"] is False


def test_token_note_appears_beside_the_link_only_when_the_manifest_declares_ui_token():
    tok = _with(status="running", label="running")
    tok["manifest"] = dict(tok["manifest"], ui_token="open-only")
    out = run({"detail": tok}, r"""
      out.withTok = panel.textContent.includes("Open adds this app's access token");
      out.href = anchors(panel).map(a => a.href);
    """)
    plain = run({"detail": _with(status="running", label="running")}, r"""
      out.withTok = panel.textContent.includes("Open adds this app's access token");""")
    assert out["withTok"] is True and out["href"] == ["http://127.0.0.1:8079/ui/"]   # link stays token-free
    assert plain["withTok"] is False


# ---------------------------------------------------------------- round 3 (Diamond F1-F6, architect rulings)
def _reconfirm(tree="t1", diff="-old\n+new", status="refused"):
    d = copy.deepcopy(INSTALLED)
    d["status"] = {"status": status, "label": status, "reason": "code-changed", "warnings": []}
    d["change"] = {"code": {"files": [{"change": "modified", "path": "a.py", "diff": diff}]}}
    d["seen"] = {"manifest_sha256": "m1", "tree_sha256": tree}
    return d


def test_f4_a_changed_proposal_is_not_swapped_under_the_reader_notice_shows_confirm_disabled():
    pend = [{"id": "adj", "kind": "install", "name": "Adj", "proposed_by": "x", "valid": True}]
    out = run({"detail": PROPOSAL, "pending": pend}, r"""
      const store = panel.querySelector('[data-path=store]');
      store.value = '/data/typed.sqlite';
      const textBefore = panel.textContent.replace(/as of [0-9:]+Z/, '');
      out.confirmBefore = buttons(panel, 'Confirm')[0].disabled;
      detail = SCEN_CHANGED; await poll(); await tick();
      out.sameNode = panel.querySelector('[data-path=store]') === store; out.value = store.value;
      out.shownUnchanged = panel.textContent.replace(/as of [0-9:]+Z/, '').replace(NOTICE, '') === textBefore;
      out.notice = panel.textContent.includes(NOTICE);
      out.confirmAfter = buttons(panel, 'Confirm')[0].disabled;
      await buttons(panel, 'Confirm')[0].click(); await tick();
      out.confirmPosts = log.filter(c => c[0] === '/api/apps/adj/confirm').length;
    """.replace("SCEN_CHANGED", json.dumps(dict(PROPOSAL, valid=False, invalid_reason="changed under you")))
        .replace("NOTICE", json.dumps("This changed since you opened it. Click Refresh to see the current version")))
    assert out["confirmBefore"] is False and out["sameNode"] is True and out["value"] == "/data/typed.sqlite"
    assert out["shownUnchanged"] is True and out["notice"] is True
    assert out["confirmAfter"] is True and out["confirmPosts"] == 0


def test_f4_reconfirm_diff_and_seen_digest_are_not_swapped_chip_and_buttons_still_update():
    out = run({"detail": _reconfirm()}, r"""
      const NOTICE = "This changed since you opened it. Click Refresh to see the current version";
      out.diffBefore = panel.textContent.includes('+new');
      // 1) only the status changes (consent content identical): chip/buttons update, no notice, Re-confirm stays live
      detail = SCEN_STATUS; await poll(); await tick();
      out.chipAfterStatusOnly = panel.textContent.includes('stopped');
      out.noticeStatusOnly = panel.textContent.includes(NOTICE);
      out.reconfirmStatusOnly = buttons(panel, 'Re-confirm')[0].disabled;
      // 2) the diff and the digest change: shown content kept, notice, Re-confirm disabled, no post carries the new digest
      detail = SCEN_DIFF; await poll(); await tick();
      out.diffKept = panel.textContent.includes('+new') && !panel.textContent.includes('+EVIL');
      out.notice = panel.textContent.includes(NOTICE);
      out.reconfirmDisabled = buttons(panel, 'Re-confirm')[0].disabled;
      await buttons(panel, 'Re-confirm')[0].click(); await tick();
      out.posts = log.filter(c => c[0] === '/api/apps/adj/confirm').map(c => c[3]);
    """.replace("SCEN_STATUS", json.dumps(dict(_reconfirm(), status={"status": "stopped", "label": "stopped", "reason": "code-changed", "warnings": []})))
        .replace("SCEN_DIFF", json.dumps(dict(_reconfirm(tree="t2", diff="-old\n+EVIL"), status={"status": "stopped", "label": "stopped", "reason": "code-changed", "warnings": []}))))
    assert out["diffBefore"] is True
    assert out["chipAfterStatusOnly"] is True and out["noticeStatusOnly"] is False and out["reconfirmStatusOnly"] is False
    assert out["diffKept"] is True and out["notice"] is True and out["reconfirmDisabled"] is True
    assert out["posts"] == []                                   # the new digest t2 was never posted


def test_f4_user_refresh_clears_the_stale_notice_and_shows_the_current_version():
    pend = [{"id": "adj", "kind": "install", "name": "Adj", "proposed_by": "x", "valid": True}]
    out = run({"detail": PROPOSAL, "pending": pend}, r"""
      detail = SCEN_CHANGED; await poll(); await tick();
      const bRef = (function () { let r = null; walk(panel, n => { if (!r && n.tag === 'button' && n.textContent === 'Refresh') r = n; }); return r; })();
      out.refreshFound = !!bRef;
      if (bRef) { await bRef.click(); await tick(); }
      out.noticeGone = !panel.textContent.includes("This changed since you opened it");
      out.currentShown = panel.textContent.includes('changed under you');
      out.confirmDisabledByInvalid = buttons(panel, 'Confirm')[0].disabled;   // invalid proposal: disabled for its own reason
    """.replace("SCEN_CHANGED", json.dumps(dict(PROPOSAL, valid=False, invalid_reason="changed under you"))))
    assert out["refreshFound"] and out["noticeGone"] is True and out["currentShown"] is True


def test_f2_an_error_pane_recovers_on_the_next_successful_tick():
    out = run({"detail": INSTALLED}, r"""
      let bRef = null; walk(panel, n => { if (!bRef && n.tag === 'button' && n.textContent === 'Refresh') bRef = n; });
      failDetail = true; await bRef.click(); await tick();
      out.errorShown = panel.textContent.includes('boom') && buttons(panel, 'Open').length === 0;
      failDetail = false; await poll(); await tick();
      out.recovered = buttons(panel, 'Open').length === 1;
    """)
    assert out["errorShown"] is True and out["recovered"] is True


def test_f3_the_pane_shows_as_of_time_and_a_skipped_tick_says_not_refreshed_unsaved_input():
    out = run({"detail": INSTALLED}, r"""
      const asOf = () => (panel.textContent.match(/as of \d{2}:\d{2}:\d{2}Z/) || [null])[0];
      out.asOf = asOf();
      out.noFlagYet = !panel.textContent.includes('not refreshed');
      let who = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who = n; });
      who.value = 'typed';
      detail = SCEN_RUNNING; await poll(); await tick();
      out.flagged = panel.textContent.includes('not refreshed: unsaved input') && asOf() === out.asOf;
      who.value = 'owner'; await poll(); await tick();
      out.cleared = !panel.textContent.includes('not refreshed');
      out.opened = buttons(panel, 'Open')[0].disabled;
    """.replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["asOf"] and out["noFlagYet"] is True
    assert out["flagged"] is True and out["cleared"] is True and out["opened"] is False


def test_f6_entering_the_tab_with_an_app_selected_loads_its_detail_immediately():
    out = run({"detail": INSTALLED}, r"""
      detail = SCEN_RUNNING;
      const g0 = detailGets();
      await doc.querySelector('.gm-tab[data-tab="apps"]').click(); await tick();
      out.gets = detailGets() - g0;
      out.openEnabled = buttons(panel, 'Open')[0].disabled === false;
    """.replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["gets"] == 1 and out["openEnabled"] is True


# ---------------------------------------------------------------- round 4 (delta Diamond G1): tab entry obeys the tick's gate
def test_g1_tab_entry_keeps_a_typed_settings_value_and_says_not_refreshed():
    out = run({"detail": INSTALLED}, r"""
      let who = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who = n; });
      who.value = 'typed-unsaved';
      detail = SCEN_RUNNING;
      await doc.querySelector('.gm-tab[data-tab="apps"]').click(); await tick();      // switch away and back = tab entry
      let who2 = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who2 = n; });
      out.same = who2 === who; out.value = who2.value;
      out.flagged = panel.textContent.includes('not refreshed: unsaved input');
    """.replace("SCEN_RUNNING", json.dumps(_with(status="running", label="running"))))
    assert out["same"] is True and out["value"] == "typed-unsaved" and out["flagged"] is True


def test_g1_tab_entry_keeps_typed_proposal_paths_and_shows_the_stale_notice():
    pend = [{"id": "adj", "kind": "install", "name": "Adj", "proposed_by": "x", "valid": True}]
    out = run({"detail": PROPOSAL, "pending": pend}, r"""
      const store = panel.querySelector('[data-path=store]');
      store.value = '/data/typed.sqlite';
      detail = SCEN_CHANGED;
      await doc.querySelector('.gm-tab[data-tab="apps"]').click(); await tick();
      const s2 = panel.querySelector('[data-path=store]');
      out.same = s2 === store; out.value = s2 && s2.value;
      out.notice = panel.textContent.includes("This changed since you opened it");
      out.confirmDisabled = buttons(panel, 'Confirm')[0].disabled;
    """.replace("SCEN_CHANGED", json.dumps(dict(PROPOSAL, valid=False, invalid_reason="changed under you"))))
    assert out["same"] is True and out["value"] == "/data/typed.sqlite"
    assert out["notice"] is True and out["confirmDisabled"] is True


def test_g1_the_explicit_refresh_button_still_forces_a_full_rerender():
    out = run({"detail": INSTALLED}, r"""
      let who = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who = n; });
      who.value = 'typed-unsaved';
      let bRef = null; walk(panel, n => { if (!bRef && n.tag === 'button' && n.textContent === 'Refresh') bRef = n; });
      await bRef.click(); await tick();
      let who2 = null; walk(panel, n => { if (n.tag === 'input' && n.attrs['data-setting'] === 'who') who2 = n; });
      out.rerendered = who2 !== who; out.value = who2.value;
    """)
    assert out["rerendered"] is True and out["value"] == "owner"

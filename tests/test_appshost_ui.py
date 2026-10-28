"""Apps tab UI (web/apps_ui/apps_tab.js) under node: the pure view builders and the DOM wiring on a
minimal DOM stub (specs/13). Names carry ac-* ids. The glass checks (ac-page-glass,
ac-cold-agent-glass) are LIVE-ONLY and not claimed here."""

import json
import os
import shutil
import subprocess

import pytest

from appshost_support import ROOT

NODE = shutil.which("node")
JS = os.path.join(ROOT, "web", "apps_ui", "apps_tab.js")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")

DETAIL_INSTALLED = {
    "ok": True, "app_id": "adj", "installed": True, "port": 8079, "manifest_raw": "{\"id\": \"adj\"}",
    "status": {"status": "running", "label": "running", "warnings": []},
    "last_start": {"ts": "2026-10-02T10:00:00Z", "actor": "beta-analyst", "actor_verified": False, "outcome": "ok"},
    "last_stop": None,
    "manifest": {"id": "adj", "name": "Example Reviewer", "version": "2a.1", "purpose": "adjudicate", "owner": "owner-b",
                 "schema_version": 1, "scope": "all", "run_at_boot_default": False, "start": ["../run.sh"],
                 "port_arg": ["--port", "{port}"], "health_path": "/h", "health_check": None, "ui_path": "/ui/",
                 "headless": False, "paths": [], "backup_before_first_start": ["store"], "requires": [{"id": "fl-image", "mode": "check"}],
                 "requires_files": [], "licences": [], "hardware": None, "user_editable": [], "env": {},
                 "stop": {"signal": "SIGTERM", "grace": 10}, "ui_token": "open-only",
                 "actions": [{"name": "import_v1", "argv": ["../run.sh", "--import-v1", "{path:v1_import}"], "requires_stopped": True, "description": ""}]},
    "settings": {"run_at_boot": True, "scope": "all", "scope_ceiling": "all",
                 "paths": [{"name": "fl_root", "value": "/fl", "access": "read_only", "kind": "dir", "optional": False, "flag": None, "description": "FL"},
                           {"name": "store", "value": "/s.db", "access": "writes", "kind": "file", "optional": False, "flag": None, "description": "store"}],
                 "user": [{"name": "who", "type": "text", "value": "owner", "default": "owner", "options": [], "kind": None, "description": ""},
                          {"name": "n", "type": "number", "value": 2, "default": 2, "options": [], "kind": None, "description": ""},
                          {"name": "b", "type": "yes/no", "value": True, "default": False, "options": [], "kind": None, "description": ""},
                          {"name": "c", "type": "choice", "value": "a", "default": "a", "options": ["a", "b"], "kind": None, "description": ""},
                          {"name": "p", "type": "path", "value": "/x", "default": "/x", "options": [], "kind": "file", "description": ""}]},
    "licences": [], "backups": {"store": {"recorded_at": "2026-10-02T09:00:00Z", "backup_path": "/b"}},
}
DETAIL_PROPOSAL = {"ok": True, "app_id": "adj", "installed": False, "valid": True, "invalid_reason": None,
                   "proposal": {"kind": "install", "proposed_by": "beta-analyst", "proposed_by_verified": False},
                   "manifest": DETAIL_INSTALLED["manifest"], "manifest_raw": "{}",
                   "to_fill": [{"name": "store", "description": "s", "kind": "file", "access": "writes", "optional": False, "flag": None}]}


def run(prog):
    r = subprocess.run([NODE, "-e", prog, JS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_ac_page_list_rows_chips_scope_and_explicit_empty_state():
    out = run("""const A = require(process.argv[1]);
      console.log(JSON.stringify({empty: A.buildList({ok: true, apps: [], pending: []}),
        full: A.buildList({ok: true, apps: [{id: 'a', name: 'A', status: 'refused', label: 'refused(licence)', scope: 'alpha'}],
          pending: [{id: 'p', kind: 'install', name: 'P', proposed_by: 'ds', valid: true}]})}));""")
    assert out["empty"]["empty"] is True and "No apps are installed" in out["empty"]["emptyText"]
    row = out["full"]["rows"][0]
    assert row["name"] == "A" and row["chip"]["text"] == "refused(licence)" and "is-refused" in row["chip"]["cls"]
    assert row["scope"] == "alpha" and out["full"]["pendingCount"] == 1


def test_ac_page_detail_view_model_has_every_required_part():
    out = run("const A = require(process.argv[1]); console.log(JSON.stringify(A.buildDetail(%s)));" % json.dumps(DETAIL_INSTALLED))
    keys = [r["key"] for r in out["manifestRows"]]
    assert {"Name", "Version", "What it does", "Owner"} <= set(keys)                 # the manifest rendered
    assert out["manifestRaw"]
    assert out["runAtBoot"] is True and out["scope"] == "all"                         # host fields
    assert [f["field"]["control"] for f in out["userFields"]] == ["text", "number", "checkbox", "select", "text"]
    assert out["canStop"] is True and out["canStart"] is False and out["chip"]["text"] == "running"
    assert out["showOpen"] is True and out["openEnabled"] is True
    assert "beta-analyst (unverified)" in out["lastStart"] and out["lastStop"] == "never"
    assert [(p["name"], p["accessText"]) for p in out["paths"]] == [("fl_root", "read only"), ("store", "writes")]
    assert out["actions"][0]["name"] == "import_v1" and out["actions"][0]["disabled"] is True   # requires_stopped


def test_ac_headless_no_open_and_open_disabled_unless_running():
    d = json.loads(json.dumps(DETAIL_INSTALLED))
    d["manifest"]["ui_path"], d["manifest"]["headless"] = None, True
    s = json.loads(json.dumps(DETAIL_INSTALLED))
    s["status"] = {"status": "stopped", "label": "stopped", "warnings": []}
    out = run("const A = require(process.argv[1]); console.log(JSON.stringify([A.buildDetail(%s), A.buildDetail(%s)]));"
              % (json.dumps(d), json.dumps(s)))
    assert out[0]["showOpen"] is False and out[0]["canStop"] is True
    assert out[1]["showOpen"] is True and out[1]["openEnabled"] is False and out[1]["canStart"] is True


def test_ac_port_not_on_confirm_screen_view_model():
    out = run("const A = require(process.argv[1]); console.log(JSON.stringify(A.buildDetail(%s)));" % json.dumps(DETAIL_PROPOSAL))
    assert out["isProposal"] is True and out["showPort"] is False and out["canConfirm"] is True
    assert "8079" not in json.dumps({k: v for k, v in out.items() if k not in ("manifest",)})
    assert out["toFill"][0]["access"] == "writes"


def test_ac_setting_types_drawn_as_form_fields_and_collected_typed():
    out = run("""const A = require(process.argv[1]);
      console.log(JSON.stringify({f: ['text','number','yes/no','choice','path'].map(t => A.fieldFor({type: t, value: t === 'yes/no', options: ['a','b'], kind: 'dir'})),
        v: A.collectValues({runAtBoot: true, scope: 'ws', paths: {store: '/s'}, user: {n: {type: 'number', value: '3'}, b: {type: 'yes/no', value: 1}, t: {type: 'text', value: 'x'}}})}));""")
    assert [f["control"] for f in out["f"]] == ["text", "number", "checkbox", "select", "text"]
    assert out["f"][3]["options"] == ["a", "b"] and "dir" in out["f"][4]["placeholder"]
    assert out["v"] == {"run_at_boot": True, "scope": "ws", "paths": {"store": "/s"}, "user": {"n": 3, "b": True, "t": "x"}}


def test_new_arrivals_for_the_notice():
    out = run("""const A = require(process.argv[1]);
      console.log(JSON.stringify(A.newArrivals(['a:install'], [{id: 'a', kind: 'install'}, {id: 'b', kind: 'install'}, {id: 'a', kind: 'uninstall'}])));""")
    assert out == ["b:install", "a:uninstall"]


_DOM = r"""
const A = require(process.argv[1]);
function mk(tag) {
  const n = { tag, children: [], className: '', attrs: {}, listeners: {}, id: '', _text: '', value: '', checked: false, disabled: false, type: '', placeholder: '', selected: false };
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
const calls = [];
let pending = [{ id: 'adj', kind: 'install', name: 'Example Reviewer', proposed_by: 'beta-analyst', valid: true }];
const PROPOSAL = __PROPOSAL__, INSTALLED = __INSTALLED__;
const win = { document: doc, setInterval() {}, confirm: () => true, fetch: async (url, opts) => {
  calls.push([url, (opts && opts.method) || 'GET', opts && opts.body]);
  if (url === '/api/apps') return { ok: true, status: 200, json: async () => ({ ok: true, apps: [], pending, empty: true }) };
  if (url === '/api/apps/adj') return { ok: true, status: 200, json: async () => (pending.length ? PROPOSAL : INSTALLED) };
  return { ok: true, status: 200, json: async () => ({ ok: true }) };
} };
const tick = async () => { for (let i = 0; i < 8; i++) await new Promise(r => setImmediate(r)); };
const textOf = n => n.textContent;
const buttons = (root, label) => { const o = []; walk(root, n => { if (n.tag === 'button' && n.textContent === label) o.push(n); }); return o; };
(async () => {
  A.init(win); A.init(win);
  await tick();
  const out = {};
  const banner = doc.getElementById('gm-apps-banner');
  out.bannerOn = banner.classList.contains('is-on'); out.bannerText = textOf(banner);
  out.tabs = doc.querySelectorAll('.gm-tab').length;
  const tab = doc.querySelector('.gm-tab[data-tab="apps"]');
  await tab.click(); await tick();
  const panel = doc.getElementById('panel-apps');
  out.active = panel.classList.contains('is-active') && !p0.classList.contains('is-active');
  const listText = textOf(panel);
  out.pendingFirst = listText.indexOf('Pending (1)') >= 0 && listText.indexOf('Pending') < listText.indexOf('Installed');
  out.emptyState = listText.includes('No apps are installed yet');
  let item = null; walk(panel, n => { if (!item && n.className.includes('gm-gv-item')) item = n; });
  await item.click(); await tick();
  out.proposalText = textOf(panel);
  const storeInput = panel.querySelector('[data-path=store]');
  storeInput.value = '/data/store.sqlite';
  pending = [];
  await buttons(panel, 'Confirm')[0].click(); await tick();
  out.confirmPost = calls.filter(c => c[0] === '/api/apps/adj/confirm').map(c => JSON.parse(c[2]));
  out.installedText = textOf(panel);
  out.hasStartStop = buttons(panel, 'Start').length === 1 && buttons(panel, 'Stop').length === 1;
  out.openButtons = buttons(panel, 'Open').length;
  await buttons(panel, 'Stop')[0].click(); await tick();
  out.stopPosted = calls.some(c => c[0] === '/api/apps/adj/stop' && c[1] === 'POST');
  const run = buttons(panel, 'Run');
  out.runDisabled = run.length === 1 && run[0].disabled === true;
  console.log(JSON.stringify(out));
})();
"""


def test_ac_page_list_detail_pending_and_confirm_through_the_dom():
    prog = _DOM.replace("__PROPOSAL__", json.dumps(DETAIL_PROPOSAL)).replace("__INSTALLED__", json.dumps(DETAIL_INSTALLED))
    out = run(prog)
    assert out["bannerOn"] and "1 new app proposal" in out["bannerText"]          # ac-pending-section: the notice
    assert out["tabs"] == 1 and out["active"] is True                            # init twice adds one tab
    assert out["pendingFirst"] and out["emptyState"]                             # Pending at the TOP; empty state
    assert "Confirm" in out["proposalText"] and "Decline" in out["proposalText"] and "Paths to fill" in out["proposalText"]
    assert "8079" not in out["proposalText"]                                     # no port on the approval screen
    assert out["confirmPost"][0]["values"]["paths"] == {"store": "/data/store.sqlite"}
    t = out["installedText"]
    for part in ("Example Reviewer 2a.1", "running", "Start at boot", "read only", "writes", "Last start",
                 "beta-analyst (unverified)", "import_v1", "Manifest", "What it does"):
        assert part in t, part
    assert out["hasStartStop"] and out["openButtons"] == 1 and out["stopPosted"] and out["runDisabled"]

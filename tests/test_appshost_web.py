"""web routes (the Apps tab) — flag gate, the operator's page actions end to end, JSON-only POSTs, no token in
any response, no embedding (specs/13, -INSTALL, -NON-GOALS). Hermetic: tmp DATA root, the launchd
double, a tmp machine config; never the live server. Names carry ac-* ids."""

import json
import os
import sys

import pytest

from appshost_support import PYTHON, Env, ROOT

_WEB = os.path.join(ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon import flags  # noqa: E402
from gnomon.appshost.verb_tools import FLAG  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402

J = {"Content-Type": "application/json"}
BASE = "http://127.0.0.1:8770"
ROUTES = ["/api/apps", "/api/apps/x1", "/api/apps/x1/start", "/api/apps/x1/stop", "/api/apps/x1/open",
          "/api/apps/x1/confirm", "/api/apps/x1/decline", "/api/apps/x1/settings", "/api/apps/x1/backup",
          "/api/apps/x1/uninstall", "/api/apps/x1/actions/x2", "/apps-ui/apps_tab.js"]


@pytest.fixture
def world(tmp_path, monkeypatch):
    e = Env(tmp_path)
    store = ConfigStore(os.path.join(e.tmp, "config.json"))

    def flag(on):
        store.save(GnomonConfig(flags={FLAG: on}))
    flag(True)
    monkeypatch.setattr(srv, "_config_store", store)
    monkeypatch.setattr(srv, "_apps_host_override", e.host)
    # the page's own origin and its CSRF token (round 2, F4): every Apps POST needs both
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._APPS_CSRF})
    yield c, e, flag
    e.cleanup()


def test_flag_registered_default_off():
    assert flags.KNOWN_FLAGS[FLAG].default is False


def test_flag_off_every_route_is_a_plain_404_and_the_shell_is_byte_identical(world):
    c, e, flag = world
    flag(False)
    for path in ROUTES:
        for m in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"):
            r = c.request(m, path, headers=J, content=b"{}" if m in ("POST", "PUT", "PATCH") else None)
            assert r.status_code == 404, (m, path, r.status_code)
    shell = c.get("/").content
    assert shell == open(os.path.join(srv.STATIC_DIR, "index.html"), "rb").read()
    assert b"apps_tab.js" not in shell
    assert "/api/apps" not in json.dumps(c.get("/openapi.json").json())


def test_flag_on_injects_the_script_and_serves_it(world):
    c, e, flag = world
    shell = c.get("/").text
    assert srv.APPS_UI_SCRIPT_TAG in shell
    r = c.get("/apps-ui/apps_tab.js")
    assert r.status_code == 200 and "GmAppsTab" in r.text
    assert c.get("/apps-ui/other.js").status_code == 404


def test_ac_page_list_empty_state_and_rows(world):
    c, e, flag = world
    body = c.get("/api/apps").json()
    assert body["apps"] == [] and body["empty"] is True and body["pending"] == []
    e.install(e.manifest("listed", scope="ws-a"))
    rows = c.get("/api/apps").json()["apps"]
    assert [(r["name"], r["status"], r["scope"]) for r in rows] == [("Fixture listed", "stopped", "ws-a")]


def test_posts_require_json_content_type(world):
    c, e, flag = world
    a = e.install(e.manifest("ctype"))
    for path in ("/api/apps", f"/api/apps/{a}/start", f"/api/apps/{a}/confirm"):
        r = c.post(path, content=b"{}", headers={"Content-Type": "text/plain"})
        assert r.status_code == 415, path
    assert e.launchd.procs == {}


def test_ac_pending_section_end_to_end_propose_confirm_start_open_stop_through_the_page(world):
    c, e, flag = world
    mp = e.manifest("pagey", ui_path="/ui", ui_token={"mode": "open-only"},
                    user_editable=[{"name": "who", "type": "text", "default": "owner"}])
    from gnomon.appshost import verbs as V
    assert V.dispatch(e.host, "app_propose_install", {"manifest_path": mp}, "beta-analyst")["ok"]
    lst = c.get("/api/apps").json()
    assert [p["id"] for p in lst["pending"]] == ["pagey"]
    d = c.get("/api/apps/pagey").json()
    assert d["installed"] is False and "port" not in d                              # ac-port-not-on-confirm-screen
    r = c.post("/api/apps/pagey/confirm", headers=J, json={"values": {"run_at_boot": True, "user": {"who": "user-a"}},
                                                           "seen": d["seen"]})
    assert r.status_code == 200 and r.json()["installed"] is True
    assert c.get("/api/apps").json()["pending"] == []
    assert c.post("/api/apps/pagey/open", headers=J, json={}).status_code == 409    # not running: no window
    r = c.post("/api/apps/pagey/start", headers=J, json={})
    assert r.status_code == 200, r.text
    e.wait_status("pagey", "running")
    r = c.post("/api/apps/pagey/open", headers=J, json={})
    tok = open(e.host.paths.ui_token("pagey")).read().strip()
    assert r.status_code == 200 and tok not in r.text and tok in e.opened[-1]
    d = c.get("/api/apps/pagey").json()
    assert tok not in json.dumps(d)
    assert d["last_start"]["actor"] == "operator-page" and d["last_start"]["actor_verified"] is False
    assert d["settings"]["run_at_boot"] is True and d["settings"]["user"][0]["value"] == "user-a"
    r = c.post("/api/apps/pagey/stop", headers=J, json={})
    assert r.json()["was_running"] is True
    assert c.get("/api/apps/pagey").json()["last_stop"]["actor"] == "operator-page"


def test_settings_backup_actions_decline_and_uninstall_routes(world):
    c, e, flag = world
    out = os.path.join(e.tmp, "act.out")
    mp = e.manifest("routes", paths=[{"name": "store", "description": "s", "kind": "file", "access": "writes"}],
                    backup_before_first_start=["store"],
                    actions=[{"name": "touch", "argv": [PYTHON, "act.py", out]}])
    with open(os.path.join(os.path.dirname(mp), "act.py"), "w") as fh:        # round 3c: a script under the app root
        fh.write("import sys\nopen(sys.argv[1], 'w').write('x\\n')\n")
    store = os.path.join(e.tmp, "store.db")
    open(store, "w").write("d")
    e.install(mp, {"paths": {"store": store}})
    r = c.post("/api/apps/routes/settings", headers=J, json={"settings": {"scope": "Bad Scope"}})
    assert r.status_code == 409 and "scope" in r.json()["detail"]
    r = c.post("/api/apps/routes/settings", headers=J, json={"settings": {"scope": "ws-b"}})
    assert r.status_code == 200 and r.json()["changed"] == ["scope"]
    assert c.post("/api/apps/routes/start", headers=J, json={}).json()["reason"] == "backup"
    open(store + ".bak", "w").write("d")
    assert c.post("/api/apps/routes/backup", headers=J, json={"name": "store", "backup_path": store + ".bak"}).status_code == 200
    assert c.post("/api/apps/routes/actions/touch", headers=J, json={}).status_code == 409   # no confirm
    r = c.post("/api/apps/routes/actions/touch", headers=J, json={"confirm": True})
    assert r.status_code == 200
    for t in e.host.action_threads:
        t.join(10)
    assert open(out).read() == "x\n"
    assert c.post("/api/apps/routes/uninstall", headers=J, json={}).status_code == 200
    assert c.get("/api/apps").json()["pending"][0]["kind"] == "uninstall"
    assert c.post("/api/apps/routes/decline", headers=J, json={}).json()["declined"] == "uninstall"
    assert c.get("/api/apps").json()["apps"][0]["id"] == "routes"


def _js_code(path):
    """The file with /* */ and // comments removed (a claim about CODE, not about prose)."""
    import re
    text = open(path, encoding="utf-8").read()
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return "\n".join(l.split("//")[0] if "//" in l and "http" not in l else l for l in text.splitlines())


def test_ac_open_new_window_no_app_ui_is_embedded_anywhere():
    """Embedding is withdrawn for good: no iframe/embed/object in the shell or the Apps tab code."""
    for p in (os.path.join(srv.STATIC_DIR, "index.html"), os.path.join(srv.APPS_UI_DIR, "apps_tab.js")):
        code = _js_code(p).lower()
        for bad in ("iframe", "<embed", "<object", "createelement(\"embed", "createelement(\"object"):
            assert bad not in code, (p, bad)


def test_ac_four_controls_one_build_no_second_flag_splits_them():
    code = _js_code(os.path.join(srv.APPS_UI_DIR, "apps_tab.js"))
    assert FLAG not in code and "/api/flags" not in code
    for ctl in ('"Start"', '"Stop"', '"Start at boot"', "statusChip"):
        assert ctl in code, ctl
    import inspect
    src = inspect.getsource(srv._apps_enabled)
    assert "FLAG" in src and src.count("_flag(") == 1


def test_g41_every_apps_route_is_mapped_or_no_event():
    from gnomon import feature_sites as FS
    known = set(FS.all_web_routes()) | set(FS.WEB_NO_EVENT)
    for r in srv.app.routes:
        p = getattr(r, "path", "")
        if p.startswith("/api/apps") or p.startswith("/apps-ui"):
            for m in r.methods:
                assert (m, p) in known, (m, p)

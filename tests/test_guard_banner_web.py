"""The deny-guard start-failure banner's server side: the flag gate, the route, the page identity with the flag OFF, read-only, and the older
/api/security-warnings entry. Hermetic: a tmp machine config, a tmp log, a fixed clock through the server's test seam; never the live server."""
import calendar
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon import flags as flags  # noqa: E402
from gnomon import guard_banner as gb  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402

FLAG = "guard_start_failure_banner"
BASE = "http://127.0.0.1:8770"
METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
ROUTES = ["/api/guard-start-failures", "/guard-banner-ui/guard_banner.js"]
NOW = calendar.timegm((2026, 10, 12, 12, 0, 0, 0, 0, 0))


@pytest.fixture
def world(tmp_path, monkeypatch):
    store_ = ConfigStore(os.path.join(str(tmp_path), "config.json"))

    def flag(on):
        store_.save(GnomonConfig(flags={FLAG: on}))
    flag(True)
    logp = str(tmp_path / "deny-guard" / "startup-failures.log")
    os.makedirs(os.path.dirname(logp))
    monkeypatch.setattr(srv, "_config_store", store_)
    monkeypatch.setattr(srv, "_guard_banner_override", {"path": logp, "now": NOW})
    return TestClient(srv.app, base_url=BASE), logp, flag


def test_the_flag_is_registered_default_off():
    assert flags.KNOWN_FLAGS[FLAG].default is False
    assert gb.FLAG_NAME == FLAG


def test_flag_on_serves_the_state_and_the_exact_text(world):
    c, logp, flag = world
    assert c.get("/api/guard-start-failures").json() == {"state": "none", "read_at": NOW}
    open(logp, "w").write("2026-10-12T09:41:07Z rc=127 class=start-up mode=allowed count=1\n2026-10-12T10:00:00Z rc=1 class=start-up mode=allowed count=1\n")
    r = c.get("/api/guard-start-failures")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    j = r.json()
    assert j["state"] == "failures" and j["count"] == 2
    assert j["text"] == ("A deny guard could not start: commands in at least one session went through unchecked. "
                         "2 time(s) in the last 24 hours; most recent 2026-10-12 10:00Z.")
    assert j["detail"] == "Which session is not recorded yet. See BREAK-GLASS: the deny guard."
    open(logp, "w").write("<script>alert(1)</script>\n")
    j = c.get("/api/guard-start-failures").json()
    assert j["state"] == "unreadable" and j["text"].startswith("The deny guard's failure log could not be read: ") and "<" not in json.dumps(j)


def test_the_route_uses_the_guards_real_log_path_when_there_is_no_seam(tmp_path, monkeypatch):
    from gnomon.harness import deny_guard as dg
    store_ = ConfigStore(os.path.join(str(tmp_path), "config.json"))
    store_.save(GnomonConfig(flags={FLAG: True}))
    monkeypatch.setattr(srv, "_config_store", store_)
    monkeypatch.setattr(srv, "_guard_banner_override", None)
    seen = {}
    real = gb.read_state
    monkeypatch.setattr(gb, "read_state", lambda path, now=None: seen.update(path=path, now=now) or real(path, now))
    monkeypatch.setattr(dg, "startup_failures_path", lambda: str(tmp_path / "nowhere" / "startup-failures.log"))
    r = TestClient(srv.app, base_url=BASE).get("/api/guard-start-failures")
    assert r.json()["state"] == "none" and seen == {"path": str(tmp_path / "nowhere" / "startup-failures.log"), "now": None}
    assert not (tmp_path / "nowhere").exists()                       # g106: the read route creates no directory


def test_flag_off_every_method_of_every_route_is_the_404_an_unknown_path_gets_and_the_page_is_byte_identical(world, monkeypatch):
    c, logp, flag = world
    open(logp, "w").write("2026-10-12T09:41:07Z rc=127 class=start-up mode=allowed count=1\n")           # even with a failure on disk
    flag(False)
    unknown = c.get("/api/definitely-not-a-route")
    for path in ROUTES:
        for m in METHODS:
            r = c.request(m, path, content=b"{}" if m in ("POST", "PUT", "PATCH") else None)
            assert r.status_code == 404, (m, path, r.status_code)
            if m == "GET":
                assert r.json() == unknown.json()
    shell = c.get("/").content
    assert shell == open(os.path.join(srv.STATIC_DIR, "index.html"), "rb").read()
    assert b"guard-banner" not in shell and b"guard_banner" not in shell
    assert "guard-start-failures" not in json.dumps(c.get("/openapi.json").json())
    # nothing about the banner is in any file the flag-OFF page can load
    for name in ("index.html", "app.js", "styles.css"):
        assert b"guard-start-failures" not in open(os.path.join(srv.STATIC_DIR, name), "rb").read(), name
    # a route the flag does not own is unchanged: with the flag OFF security-warnings still serves the older entry for the same log
    from gnomon.harness import deny_guard as dg
    monkeypatch.setattr(dg, "startup_failures_path", lambda: logp)
    kinds = [w.get("kind") for w in c.get("/api/security-warnings").json()["warnings"]]
    assert "guard-could-not-start" in kinds


def test_flag_on_injects_the_script_once_and_serves_only_it(world):
    c, logp, flag = world
    shell = c.get("/").text
    assert srv.GUARD_BANNER_UI_SCRIPT_TAG in shell and shell.count("guard_banner.js") == 1
    r = c.get("/guard-banner-ui/guard_banner.js")
    assert r.status_code == 200 and "GmGuardBanner" in r.text and r.headers["content-type"].startswith("application/javascript")
    assert c.get("/guard-banner-ui/other.js").status_code == 404
    assert c.get("/guard-banner-ui/..%2Fserver.py").status_code == 404


def test_flag_on_the_page_is_index_html_plus_exactly_one_script_tag(world):
    c, logp, flag = world
    on = c.get("/").content
    base = open(os.path.join(srv.STATIC_DIR, "index.html"), "rb").read()
    assert on != base and on.replace(("  " + srv.GUARD_BANNER_UI_SCRIPT_TAG + "\n").encode(), b"") == base


def test_the_other_flags_alone_do_not_load_the_banner(world):
    c, logp, flag = world
    srv._config_store.save(GnomonConfig(flags={"apps_tab": True}))
    assert b"guard_banner" not in c.get("/").content


def test_read_only_every_write_is_405_and_changes_nothing_on_disk(world, tmp_path):
    c, logp, flag = world
    open(logp, "w").write("2026-10-12T09:41:07Z rc=127 class=start-up mode=allowed count=1\n")

    def snap():
        return sorted((os.path.join(dp, f), os.path.getsize(os.path.join(dp, f)), os.path.getmtime(os.path.join(dp, f)))
                      for dp, dn, fn in os.walk(str(tmp_path)) for f in fn)
    s0 = snap()
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        r = c.request(m, "/api/guard-start-failures", headers={"Content-Type": "application/json", "Origin": BASE}, content=b'{"dismiss":true}')
        assert r.status_code == 405, m
    for _ in range(3):
        c.get("/api/guard-start-failures")
    assert snap() == s0 and sorted(os.listdir(os.path.dirname(logp))) == ["startup-failures.log"]


def test_the_route_answers_only_this_servers_own_loopback_host(world):
    c, logp, flag = world
    assert c.get("/api/guard-start-failures", headers={"Host": "evil.example:8770"}).status_code == 403
    assert c.get("/api/guard-start-failures", headers={"Host": "localhost:8770"}).status_code == 200


def test_the_older_never_clearing_entry_is_replaced_only_while_the_flag_is_on(world, monkeypatch):
    c, logp, flag = world
    from gnomon.harness import deny_guard as dg
    open(logp, "w").write("2026-09-01T09:41:07Z rc=127 class=start-up mode=allowed count=1\n")           # an OLD failure: the new banner is silent, the old entry would still show
    monkeypatch.setattr(dg, "startup_failures_path", lambda: logp)
    monkeypatch.setattr(dg, "warnings_path", lambda: logp + ".warnings.json")
    flag(False)
    off = c.get("/api/security-warnings").json()["warnings"]
    assert [w["kind"] for w in off if w.get("kind")] == ["guard-could-not-start"]
    flag(True)
    on = c.get("/api/security-warnings").json()["warnings"]
    assert not [w for w in on if w.get("kind") == "guard-could-not-start"]
    # the security-flag entries and the guard-degraded entry are untouched by the flag
    open(logp + ".warnings.json", "w").write(json.dumps({"count": 2, "last-ts": "x", "text": "SECURITY: degraded"}))
    assert [w["kind"] for w in c.get("/api/security-warnings").json()["warnings"] if w.get("kind")] == ["guard-degraded"]


def test_the_route_is_listed_for_the_feature_events_table_as_no_event():
    from gnomon import feature_sites as fs
    for m in METHODS:
        assert (m, "/api/guard-start-failures") in fs.WEB_NO_EVENT and (m, "/guard-banner-ui/{name}") in fs.WEB_NO_EVENT
        assert (m, "/api/guard-start-failures") not in fs.WEB_ROUTES

"""confirm-proof (g76): the Apps tab confirm / settings routes make the server run the presence
check AFTER the click and BEFORE committing; failure refuses; evidence goes to the Apps audit log; flag OFF =
previous behaviour. The Apps flag is ON here (the tab itself stays dark by default)."""

import os
import sys

import pytest

from appshost_support import Env, ROOT

_WEB = os.path.join(ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon import human_confirm as hc  # noqa: E402
from gnomon.appshost import verbs as V  # noqa: E402
from gnomon.appshost.verb_tools import FLAG  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402

def _raw_save(store, cfg):
    """Write the config file as a HUMAN-less test setup (bypasses ConfigStore.save's presence rule on purpose: these
    tests stage the on-disk state; the rule itself is tested through ``ConfigStore.save``)."""
    import json as _j
    with open(store.path, "w", encoding="utf-8") as fh:
        _j.dump(store._persisted(cfg), fh)
        fh.write("\n")


J = {"Content-Type": "application/json"}
BASE = "http://127.0.0.1:8770"


@pytest.fixture
def world(tmp_path, monkeypatch):
    e = Env(tmp_path)
    store = ConfigStore(os.path.join(e.tmp, "config.json"))
    state = {FLAG: True}

    def setflags(**kw):
        state.update({k: v for k, v in kw.items()})
        _raw_save(store, GnomonConfig(flags=dict(state)))
    setflags()
    monkeypatch.setattr(srv, "_config_store", store)
    monkeypatch.setattr(srv, "_apps_host_override", e.host)
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._APPS_CSRF})
    calls = []
    mode = {"result": "authenticated"}

    def runner(reason, timeout_s):
        calls.append((reason, timeout_s))
        r = mode["result"]
        return hc.ProofResult(r == "authenticated", r, hc.METHOD, 1790000000.0, "stub")
    hc.set_runner(runner)
    yield c, e, setflags, calls, mode
    e.cleanup()


def _propose(e, name):
    mp = e.manifest(name)
    assert V.dispatch(e.host, "app_propose_install", {"manifest_path": mp}, "beta-analyst")["ok"]
    return name


def _confirm(c, name):
    d = c.get(f"/api/apps/{name}").json()
    return c.post(f"/api/apps/{name}/confirm", headers=J, json={"values": {"run_at_boot": False}, "seen": d["seen"]})


def test_install_confirm_runs_the_check_after_the_click_and_records_evidence(world):
    c, e, _f, calls, _m = world
    _propose(e, "cp1")
    r = _confirm(c, "cp1")
    assert r.status_code == 200 and r.json()["installed"] is True
    assert len(calls) == 1 and "install the app cp1" in calls[0][0] and calls[0][1] == 120.0
    row = [a for a in e.audit("cp1") if a["verb"] == "confirm-install"][-1]
    assert row["outcome"] == "installed"
    p = row["detail"]["proof"]
    assert p["method"] == hc.METHOD and p["result"] == "authenticated" and p["ok"] is True and p["ts"] == 1790000000.0


@pytest.mark.parametrize("result", ["refused", "timeout", "unavailable", "error", "busy"])
def test_a_failed_check_refuses_commits_nothing_and_says_so(world, result):
    c, e, _f, calls, mode = world
    mode["result"] = result
    _propose(e, "cp2")
    r = _confirm(c, "cp2")
    body = r.json()
    assert r.status_code == 409 and body["ok"] is False and body["presence_refused"] is True
    assert "NOT confirmed" in body["error"] and "Nothing was changed" in body["error"]
    assert os.path.exists(e.host.paths.proposal("cp2"))                      # still pending
    assert "cp2" not in e.host._installed()                                  # nothing installed
    assert not os.path.exists(e.host.paths.pin("cp2")) and not os.path.exists(e.host.paths.settings("cp2"))
    row = e.audit("cp2")[-1]
    assert row["outcome"] == "refused-no-proof" and row["detail"]["proof"]["result"] == result


def test_the_check_runs_before_any_commit_step(world):
    c, e, _f, calls, mode = world
    _propose(e, "cp3")
    seen = {}

    def runner(reason, t):
        seen["installed_at_prompt"] = "cp3" in e.host._installed()
        seen["pin_at_prompt"] = os.path.exists(e.host.paths.pin("cp3"))
        return hc.ProofResult(True, "authenticated", hc.METHOD, 1.0)
    hc.set_runner(runner)
    assert _confirm(c, "cp3").status_code == 200
    assert seen == {"installed_at_prompt": False, "pin_at_prompt": False}


def test_a_retry_after_a_refusal_works(world):
    c, e, _f, calls, mode = world
    _propose(e, "cp4")
    mode["result"] = "timeout"
    assert _confirm(c, "cp4").status_code == 409
    mode["result"] = "authenticated"
    assert _confirm(c, "cp4").status_code == 200 and "cp4" in e.host._installed()


def test_settings_change_needs_the_check_too(world):
    c, e, _f, calls, mode = world
    name = e.install(e.manifest("cp5"))
    mode["result"] = "refused"
    r = c.post(f"/api/apps/{name}/settings", headers=J, json={"settings": {"run_at_boot": True}})
    assert r.status_code == 409 and r.json()["presence_refused"] is True
    assert (e.host.detail(name)["settings"]["run_at_boot"]) is False
    mode["result"] = "authenticated"
    r = c.post(f"/api/apps/{name}/settings", headers=J, json={"settings": {"run_at_boot": True}})
    assert r.status_code == 200
    row = [a for a in e.audit(name) if a["verb"] == "settings"][-1]
    assert row["detail"]["proof"]["result"] == "authenticated"
    assert "settings" in calls[-1][0] or "change the settings of the app" in calls[-1][0]


def test_uninstall_confirm_is_gated(world):
    c, e, _f, calls, mode = world
    name = e.install(e.manifest("cp6"))
    assert V.dispatch(e.host, "app_propose_uninstall", {"app_id": name}, "beta-analyst")["ok"]
    mode["result"] = "refused"
    assert c.post(f"/api/apps/{name}/confirm", headers=J, json={}).status_code == 409
    assert name in e.host._installed()
    assert "uninstall the app" in calls[-1][0]


def test_flag_off_is_the_previous_behaviour_no_prompt_at_all(world):
    c, e, setflags, calls, mode = world
    setflags(confirm_proof=False)
    mode["result"] = "refused"                        # would refuse if it were asked
    _propose(e, "cp7")
    assert _confirm(c, "cp7").status_code == 200
    assert calls == []
    row = [a for a in e.audit("cp7") if a["verb"] == "confirm-install"][-1]
    assert "proof" not in (row.get("detail") or {})


def test_default_flag_state_is_on(world):
    c, e, setflags, calls, mode = world
    # the machine config here sets only the Apps flag: the proof flag resolves to its registry default
    assert srv._flag(hc.FLAG) is True


def test_a_confirm_with_nothing_to_confirm_never_prompts_the_operator(world):
    c, e, _f, calls, mode = world
    r = c.post("/api/apps/ghost/confirm", headers=J, json={})
    assert r.status_code == 409 and calls == []
    r = c.post("/api/apps/ghost/settings", headers=J, json={"settings": {}})
    assert r.status_code == 409 and calls == []


def test_non_confirm_ops_do_not_prompt(world):
    c, e, _f, calls, mode = world
    name = e.install(e.manifest("cp8"))
    assert c.post(f"/api/apps/{name}/stop", headers=J, json={}).status_code == 200
    assert c.post(f"/api/apps/{name}/decline", headers=J, json={}).status_code in (200, 409)
    assert calls == []


def test_csrf_and_host_guards_still_come_first_no_prompt_for_a_forged_request(world):
    c, e, _f, calls, mode = world
    _propose(e, "cp9")
    r = c.post("/api/apps/cp9/confirm", headers={**J, "X-Gnomon-CSRF": "wrong"}, json={})
    assert r.status_code == 403 and calls == []


def test_apps_tab_stays_dark_flag_off_404_even_with_proof_on(world):
    c, e, setflags, calls, mode = world
    setflags(**{FLAG: False})
    assert c.post("/api/apps/x/confirm", headers=J, json={}).status_code == 404
    assert calls == []


def test_the_page_tells_the_operator_a_prompt_is_waiting():
    js = open(os.path.join(ROOT, "web", "apps_ui", "apps_tab.js")).read()
    assert "Mac password / Touch ID prompt is waiting" in js

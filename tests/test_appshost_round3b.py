"""Round 3b: g73-amendment-4b-2026-10-02 (app_open restored as the 7th agent verb, at the operator's word).
Hermetic: tmp_path, the launchd double, an injected opener (g31: tests never launch a browser)."""

import os

import pytest

from appshost_support import Env
from gnomon.appshost import verbs as V
from gnomon.appshost.host import Actor
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


def _kicks(env):
    return [c for c in env.launchd.calls if c[0] == "kickstart"]


def test_app_open_starts_a_stopped_app_then_opens_the_exact_loopback_url(env):
    a = env.install(env.manifest("viewer", ui_path="/ui/"))
    r = V.dispatch(env.host, "app_open", {"app_id": a}, "beta-analyst")
    assert r["ok"] is True, r
    port = env.host._port(a)
    assert env.opened == [f"http://127.0.0.1:{port}/ui/"]
    assert r["url"] == f"http://127.0.0.1:{port}/ui/" and r["opened"] is True
    assert len(_kicks(env)) == 1 and env.evidence(a)               # it WAS started, through the preflight wrapper
    assert any(e["verb"] == "app_open" and e["actor"] == "beta-analyst" for e in env.audit(a)), env.audit(a)


def test_app_open_on_a_running_app_does_not_start_it_again(env):
    a = env.install(env.manifest("viewer2", ui_path="/ui"))
    env.host.start(a, operator)
    env.wait_status(a, "running")
    n = len(_kicks(env))
    r = V.dispatch(env.host, "app_open", {"app_id": a}, "agent-x")
    assert r["ok"] and len(_kicks(env)) == n and len(env.opened) == 1


def test_app_open_response_never_carries_a_ui_token(env):
    a = env.install(env.manifest("tokapp", ui_path="/ui", ui_token={"mode": "open-only"}))
    r = V.dispatch(env.host, "app_open", {"app_id": a}, "agent-x")
    assert r["ok"], r
    tok = env.evidence(a)[-1]["token"]
    assert tok and tok not in repr(r) and tok in env.opened[0]    # in the browser URL only
    assert tok not in repr(env.audit(a))


def test_app_open_of_a_headless_app_refuses_and_starts_nothing(env):
    a = env.install(env.manifest("daemon", mode="sleep", with_port=False))
    r = V.dispatch(env.host, "app_open", {"app_id": a}, "agent-x")
    assert r["ok"] is False and "headless" in r["error"]
    assert not _kicks(env) and not [c for c in env.launchd.calls if c[0] == "bootstrap"] and env.opened == []


@pytest.mark.parametrize("extra", [{"url": "http://evil.example/"}, {"path": "/etc/passwd"}, {"ui_path": "/x"},
                                   {"port": 1}, {"args": ["x"]}])
def test_app_open_rejects_any_caller_supplied_url_or_path_before_doing_anything(env, extra):
    a = env.install(env.manifest("strict", ui_path="/ui"))
    r = V.dispatch(env.host, "app_open", {"app_id": a, **extra}, "agent-x")
    assert r["ok"] is False and "only app_id" in r["error"]
    assert env.opened == [] and not _kicks(env)


def test_app_open_with_a_changed_manifest_is_refused_before_any_open(env):
    mp = env.manifest("changed", ui_path="/ui")
    a = env.install(mp)
    raw = open(mp, "rb").read()
    open(mp, "wb").write(raw.replace(b'"1.0.0"', b'"1.0.1"'))
    r = V.dispatch(env.host, "app_open", {"app_id": a}, "agent-x")
    assert r["ok"] is False and r.get("reason") == "manifest-changed", r
    assert env.opened == [] and env.evidence(a) == []


def test_app_open_of_an_unknown_or_non_string_id_is_refused(env):
    for bad in ("nope", None, 7, ["x"]):
        r = V.dispatch(env.host, "app_open", {"app_id": bad}, "agent-x")
        assert r["ok"] is False
    assert env.opened == []


def test_app_open_of_a_merely_proposed_app_is_refused_and_starts_nothing(env):
    mp = env.manifest("proposed", ui_path="/ui")
    r = env.host.propose_install(mp, Actor("agent-x", False))
    assert r["ok"]
    out = V.dispatch(env.host, "app_open", {"app_id": r["app_id"]}, "agent-x")
    assert out["ok"] is False and env.opened == [] and not _kicks(env)


def test_the_pages_open_still_refuses_a_stopped_app(env):
    a = env.install(env.manifest("pageonly", ui_path="/ui"))
    r = env.host.open(a, operator)
    assert r["ok"] is False and "not running" in r["error"] and not _kicks(env)


def test_the_real_default_opener_refuses_under_pytest(monkeypatch):
    from gnomon.appshost import host as H

    got = []
    monkeypatch.setattr(H.webbrowser, "open_new", got.append)
    assert os.environ.get("PYTEST_CURRENT_TEST")
    with pytest.raises(RuntimeError, match="refuses to run under pytest"):
        H._default_opener("http://127.0.0.1:1/")
    assert got == []


def test_app_open_through_the_hub_service_dispatches_and_opens(tmp_path):
    from gnomon.hub import mcp
    from gnomon.hub.registry import SessionRegistry

    e = Env(tmp_path)
    try:
        a = e.install(e.manifest("hubopen", ui_path="/ui"))
        svc = mcp.GmMCPService(SessionRegistry(), apps_host=lambda: e.host, apps_enabled=lambda: True)
        r = svc.dispatch("app_open", {"app_id": a}, caller="beta-analyst")
        assert (r or {}).get("ok") is True, r
        assert len(e.opened) == 1
    finally:
        e.cleanup()

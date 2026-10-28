"""confirm-proof ROUND 2 (delta Diamond audit-confirm-proof-delta-2026-10-03: D1, D4, D5, D6, D7).

D6 the prompt text and the act digest come from ONE locked read; the commit re-reads and refuses a difference.
D1 ``ConfigStore.save`` (the library writer) refuses to turn a SECURITY flag OFF without a presence GRANT.
D4 ``..`` segments (and percent-escapes) are resolved before the route rule matches.
D5 write-then-run in one command is blocked again when the written text is a client call to a route.
D7 the act digest covers the action name, the pin and the run_action path (mutation-pinned).

STATED LIMITS (spec 13 requirement-security-flags): symlink / hard-link aliases of config.json, the write shapes the
deny rule does not read (directory destinations, tool options, find -exec, xargs ...), and a same-uid process in
general, can still write the file."""

import json
import os
import sys
import time

import pytest

from appshost_support import Env, ROOT, PYTHON

_WEB = os.path.join(ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon import human_confirm as hc  # noqa: E402
from gnomon.appshost import verbs as V  # noqa: E402
from gnomon.appshost.host import Actor, ActChanged  # noqa: E402
from gnomon.appshost.verb_tools import FLAG  # noqa: E402
from gnomon.app.config_controller import ConfigController  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402
try:
    from gnomon.config import PresenceRequired  # noqa: E402
except ImportError:      # a tree without the fix: collect, then fail per test
    class PresenceRequired(Exception):
        pass
from gnomon.harness import deny_guard as G  # noqa: E402
from gnomon.feature_events import VIEWER_ACTOR

J = {"Content-Type": "application/json"}
BASE = "http://127.0.0.1:8770"
SECURITY = ("confirm_proof", "dsd_submit_proof")


def _raw_save(store, cfg):
    with open(store.path, "w", encoding="utf-8") as fh:
        json.dump(store._persisted(cfg), fh)
        fh.write("\n")


@pytest.fixture
def world(tmp_path, monkeypatch, submit_proof_on):
    e = Env(tmp_path)
    store = ConfigStore(os.path.join(e.tmp, "config.json"))
    _raw_save(store, GnomonConfig(flags={FLAG: True}))
    monkeypatch.setattr(srv, "_config_store", store)
    monkeypatch.setattr(srv, "_apps_host_override", e.host)
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._APPS_CSRF})
    calls, mode = [], {"hook": None}

    def runner(reason, timeout_s):
        calls.append(reason)
        if mode["hook"]:
            mode["hook"]()
        return hc.ProofResult(True, "authenticated", hc.METHOD, 1790000000.0, "stub")
    hc.set_runner(runner)
    yield c, e, calls, mode, store
    e.cleanup()


# ------------------------------------------------------------------------------------------------ D6
def _swap_after_snapshot(monkeypatch, host, swap):
    """Run ``swap`` right AFTER the host has produced the prompt's state read (whichever name the build uses for it):
    the window the delta audit reproduced between 'what the prompt says' and 'what is bound'."""
    hit = []
    for name in ("proof_snapshot", "proof_kind"):
        if hasattr(host, name):
            orig = getattr(host, name)

            def wrap(*a, __o=orig, **k):
                out = __o(*a, **k)
                if not hit:
                    hit.append(1)
                    swap()
                return out
            monkeypatch.setattr(host, name, wrap)
            break
    return hit


def test_the_diamonds_swap_between_prompt_text_and_digest_is_refused(world, monkeypatch):
    """prompt says 're-confirm'; an agent proposes an uninstall right after that read; the commit must NOT uninstall."""
    c, e, calls, mode, store = world
    name = e.install(e.manifest("d6a"))
    hit = _swap_after_snapshot(monkeypatch, e.host, lambda: V.dispatch(
        e.host, "app_propose_uninstall", {"app_id": name}, "beta-analyst"))
    r = c.post(f"/api/apps/{name}/confirm", headers=J, json={"values": {}})
    assert hit, "the swap hook never ran"
    assert "re-confirm the app d6a" in calls[0]
    assert r.status_code == 409 and "changed while you were confirming" in r.json()["detail"]
    assert name in e.host._installed()                                  # NOT uninstalled


def test_the_prompt_text_and_the_digest_come_from_one_read(world):
    c, e, calls, mode, store = world
    e.install(e.manifest("d6b"))
    n = []
    real = e.host._proof_state
    e.host._proof_state = lambda *a, **k: (n.append(1), real(*a, **k))[1]
    what, digest = e.host.proof_snapshot("d6b", "confirm", values={})
    assert what == "re-confirm" and digest and len(n) == 1               # one read, both outputs
    assert e.host.act_digest("d6b", "confirm", values={}) == digest


def test_the_kind_is_part_of_what_is_bound(world):
    c, e, calls, mode, store = world
    name = e.install(e.manifest("d6c"))
    _w1, d1 = e.host.proof_snapshot(name, "confirm", values={})
    V.dispatch(e.host, "app_propose_uninstall", {"app_id": name}, "beta-analyst")
    w2, d2 = e.host.proof_snapshot(name, "confirm", values={})
    assert w2 == "uninstall" and d1 != d2


# ------------------------------------------------------------------------------------------------ D1
def _store(tmp_path, flags=None):
    st = ConfigStore(str(tmp_path / "config.json"))
    _raw_save(st, GnomonConfig(flags=dict(flags or {})))
    return st


def _flag_on_disk(st, name):
    from gnomon import flags as F
    return F.flag(name, config=st.load())


@pytest.mark.parametrize("flag", SECURITY)
def test_the_library_writer_refuses_to_turn_a_security_flag_off(tmp_path, flag, submit_proof_on):
    """The Diamond's one-liner: ConfigStore().save(cfg-with-the-flag-OFF) -> refused; the flag stays ON on disk."""
    st = _store(tmp_path)
    cfg = st.load()
    cfg.flags = {flag: False}
    with pytest.raises(PresenceRequired):
        st.save(cfg)
    assert _flag_on_disk(st, flag) is True


def test_the_controller_route_to_the_same_write_is_refused(tmp_path, submit_proof_on):
    st = _store(tmp_path)
    cc = ConfigController(store=st)
    cc.set("flags.confirm_proof", "false")
    with pytest.raises(PresenceRequired):
        cc.save()
    assert _flag_on_disk(st, "confirm_proof") is True


def test_turning_on_and_every_other_change_stay_free(tmp_path, submit_proof_on):
    st = _store(tmp_path, {"confirm_proof": False})              # staged OFF on disk (a human's earlier choice)
    cfg = st.load()
    cfg.flags = {"confirm_proof": True}                       # turning ON
    st.save(cfg)
    assert _flag_on_disk(st, "confirm_proof") is True
    cfg = st.load()
    cfg.user_name = "someone"
    st.save(cfg)                                                         # unrelated change
    assert st.load().user_name == "someone"
    cfg = st.load()
    cfg.flags = dict(cfg.flags, hook_noop_events=True)     # another flag
    st.save(cfg)


def test_a_grant_from_a_passed_check_allows_exactly_that_change_once(tmp_path, submit_proof_on):
    hc.set_runner(lambda r, t: hc.ProofResult(True, "authenticated", hc.METHOD, 1.0))
    st = _store(tmp_path)
    cfg = st.load()
    cfg.flags = {"confirm_proof": False}
    res, grant = hc.prove_presence_grant("turn off", hc.change_digest(["confirm_proof"]))
    assert res.ok and grant is not None
    st.save(cfg, presence_grant=grant)
    assert _flag_on_disk(st, "confirm_proof") is False
    # one-shot: the same grant cannot turn the flag off a second time
    _raw_save(st, GnomonConfig(flags={}))
    with pytest.raises(PresenceRequired):
        st.save(cfg, presence_grant=grant)
    assert _flag_on_disk(st, "confirm_proof") is True


def test_a_grant_is_bound_to_the_change_and_cannot_be_forged_or_expired(tmp_path, submit_proof_on, monkeypatch):
    hc.set_runner(lambda r, t: hc.ProofResult(True, "authenticated", hc.METHOD, 1.0))
    st = _store(tmp_path)
    both = st.load()
    both.flags = {n: False for n in SECURITY}
    _res, g_one = hc.prove_presence_grant("x", hc.change_digest(["confirm_proof"]))
    with pytest.raises(PresenceRequired):                                # a grant for ONE flag does not cover TWO
        st.save(both, presence_grant=g_one)
    with pytest.raises(PresenceRequired):                                # a hand-built grant is not in the registry
        st.save(both, presence_grant=hc.PresenceGrant("0" * 32, hc.change_digest(SECURITY)))
    with pytest.raises(PresenceRequired):
        st.save(both, presence_grant=object())
    _res, g_both = hc.prove_presence_grant("x", hc.change_digest(SECURITY))
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + hc.GRANT_TTL_S + 5)
    with pytest.raises(PresenceRequired):                                # expired
        st.save(both, presence_grant=g_both)
    assert _flag_on_disk(st, "confirm_proof") is True


def test_a_failed_check_mints_no_grant():
    hc.set_runner(lambda r, t: hc.ProofResult(False, "refused", hc.METHOD, 1.0))
    res, grant = hc.prove_presence_grant("x", "d")
    assert not res.ok and grant is None


def test_the_web_route_still_turns_a_flag_off_after_a_passed_check(world):
    c, e, calls, mode, store = world
    r = c.post("/api/config", headers=J, json={"fields": {"flags.confirm_proof": "false"},
                                               "custom_terminal_templates": {}})
    assert r.status_code == 200 and calls and _flag_on_disk(store, "confirm_proof") is False


# ------------------------------------------------------------------------------------------------ D4
U = "http://127.0.0.1:8770"
DOTDOT_BLOCK = [
    f"curl -X POST {U}/api/apps/../apps/x/confirm",                      # the Diamond's two examples
    f"curl -X POST {U}/api/apps/x/../x/confirm",
    f"curl -X POST {U}/api/dsd/../dsd/submit/x",
    f"curl -X POST {U}/api/apps/%2e%2e/apps/x/confirm",                   # encoded variants
    f"curl -X POST {U}/api/apps/%2E%2E/apps/x/settings",
    f"curl -X POST {U}/api/apps/x/..%2fx/confirm",
    f"curl -X POST {U}/api/apps%2f..%2fapps/x/confirm",
    f"curl -X POST {U}/api/apps/x/%63onfirm",
    f"curl -X POST {U}/api/dsd/%73ubmit/x",
    f"curl -X POST {U}/api/apps/a/../b/../x/confirm",
    f"curl -X POST {U}/api/apps/x/./../x/actions/touch",
    "python3 -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8770/api/apps/../apps/x/confirm', b'')\"",
]


@pytest.mark.parametrize("cmd", DOTDOT_BLOCK)
def test_dot_dot_and_percent_escaped_routes_are_still_the_route(cmd):
    r = G.decide(cmd)
    assert r and r.startswith(G.ROUTE_TAG), (cmd, r)


@pytest.mark.parametrize("cmd", [
    f"curl -s {U}/api/apps/../health",
    f"curl -s {U}/api/dsd/%6cist",
    "grep -n 'api/apps/../apps/x/confirm' notes.md",
    "git commit -m 'curl /api/apps/../apps/x/confirm is blocked'",
])
def test_dot_dot_normalisation_does_not_block_other_routes_or_reads(cmd):
    assert G.decide(cmd) is None, cmd


# ------------------------------------------------------------------------------------------------ D5
WTR_BLOCK = [
    "echo \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8770/api/dsd/submit/x',b'')\" > /tmp/a.py && python3 /tmp/a.py",
    "cat > /tmp/go.py <<'EOF'\nimport requests\nrequests.post('http://127.0.0.1:8770/api/dsd/submit/x')\nEOF\npython3 /tmp/go.py",
    "printf '%s' \"curl -X POST http://127.0.0.1:8770/api/apps/x/confirm\" > go.sh; bash go.sh",
    "cat > go.sh <<EOF\ncurl -X POST 127.0.0.1:8770/api/dsd/submit/x\nEOF\nsh go.sh",
    "cat > x.js <<EOF\nfetch('http://127.0.0.1:8770/api/apps/x/settings',{method:'POST'})\nEOF\nnode x.js",
    "node -e \"require('http').request({host:'127.0.0.1',port:8770,path:'/api/dsd/submit/x',method:'POST'}).end()\"",
    "php -r \"file_get_contents('http://127.0.0.1:8770/api/dsd/submit/x');\"",
]
WTR_ALLOW = [
    "cat > notes.md <<EOF\nthe /api/dsd/submit route: agents never call it\nEOF\npython3 check_notes.py",
    "echo 'POST /api/dsd/submit is the operator only' > notes.txt && python3 tools/lint.py notes.txt",
    "cat > go.py <<EOF\nprint('hello')\nEOF\npython3 go.py",
    "cat > doc.md <<EOF\nurllib /api/dsd/submit\nEOF\ncat doc.md",
    "python3 - <<'EOF'\nopen('s.txt','w').write('route /api/dsd/submit, agents never use curl')\nEOF",
    "echo 'see /api/apps/x/confirm' > a.txt; bash tools/run_tests.sh",
]


@pytest.mark.parametrize("cmd", WTR_BLOCK)
def test_write_then_run_of_a_client_call_to_a_route_is_blocked(cmd):
    r = G.decide(cmd)
    assert r and r.startswith(G.ROUTE_TAG), (cmd, r)


@pytest.mark.parametrize("cmd", WTR_ALLOW)
def test_writing_a_note_or_running_an_unrelated_script_still_passes(cmd):
    assert G.decide(cmd) is None, cmd


def test_accepted_trade_prose_that_spells_a_client_api_call_then_a_script_run_is_blocked():
    """The one residual false block of the D5 rule, stated: a note that literally spells ``requests.post`` AND the route,
    followed by running any script in the same command. Split it into two commands (write, then run)."""
    cmd = "cat > notes.md <<EOF\nnever requests.post('/api/dsd/submit/x')\nEOF\npython3 check_notes.py"
    assert (G.decide(cmd) or "").startswith(G.ROUTE_TAG)


# ------------------------------------------------------------------------------------------------ D7
def _two_actions(e, name="d7"):
    out = os.path.join(e.tmp, "d7.out")
    mp = e.manifest(name, actions=[{"name": "touch", "argv": [PYTHON, "act.py", out]},
                                   {"name": "other", "argv": [PYTHON, "act.py", out + "2"]}])
    with open(os.path.join(os.path.dirname(mp), "act.py"), "w") as fh:
        fh.write("import sys\nopen(sys.argv[1], 'w').write('x\\n')\n")
    e.install(mp)
    return name, out


def test_run_action_refuses_a_changed_act_and_runs_nothing(world):
    """Kills: run_action's own digest check removed (the proof for 'touch' used for 'other')."""
    c, e, calls, mode, store = world
    name, out = _two_actions(e)
    d_touch = e.host.act_digest(name, "action", action="touch")
    with pytest.raises(ActChanged):
        e.host.run_action(name, "other", Actor(VIEWER_ACTOR, False), confirmed=True, expect_digest=d_touch)
    assert not os.path.exists(out + "2") and not os.path.exists(out)
    assert [a for a in e.audit(name) if a["outcome"] == "refused-act-changed"]
    # the same digest for the same action runs it
    e.host.run_action(name, "touch", Actor(VIEWER_ACTOR, False), confirmed=True, expect_digest=d_touch)
    for t in e.host.action_threads:
        t.join(10)
    assert os.path.exists(out)


def test_the_digest_covers_the_action_name(world):
    """Kills: digest ignores the action name."""
    c, e, calls, mode, store = world
    name, _out = _two_actions(e)
    assert (e.host.act_digest(name, "action", action="touch") != e.host.act_digest(name, "action", action="other"))


def test_the_digest_covers_the_pin(world):
    """Kills: digest ignores the pin."""
    c, e, calls, mode, store = world
    name = e.install(e.manifest("d7pin"))
    before = e.host.act_digest(name, "confirm", values={})
    pin_path = e.host.paths.pin(name)
    pin = json.load(open(pin_path))
    pin["pinned_by"] = "someone-else"
    json.dump(pin, open(pin_path, "w"))
    assert e.host.act_digest(name, "confirm", values={}) != before


def test_a_pin_change_during_the_prompt_is_refused_through_the_route(world):
    c, e, calls, mode, store = world
    name = e.install(e.manifest("d7route"))
    pin_path = e.host.paths.pin(name)

    def rewrite_pin():
        pin = json.load(open(pin_path))
        pin["pinned_by"] = "swapped"
        json.dump(pin, open(pin_path, "w"))
    mode["hook"] = rewrite_pin
    r = c.post(f"/api/apps/{name}/confirm", headers=J, json={"values": {}})
    assert r.status_code == 409 and "changed while you were confirming" in r.json()["detail"]
    assert json.load(open(pin_path))["pinned_by"] == "swapped"          # nothing re-pinned over the swap

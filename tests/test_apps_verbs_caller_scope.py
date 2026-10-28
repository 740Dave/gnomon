"""Caller-binding scope of the ported Apps host verbs (owner ruling 2026-10-04, batch P1/P6 fix round).

Every ``app_*`` verb has an EXPLICIT registry entry: read-class takes the bound-agent scope. operator ruling 2026-10-08
: app_start, app_stop, app_open and app_propose_install are bound-agent too (an identified agent may
start, stop and open apps and PROPOSE installs; the operator still confirms an install); app_propose_uninstall stays REFUSED. Each assertion can fail: delete an entry and the
"unscoped" path answers differently; flip one to ``bound-agent`` and the refusal pins fail.
"""
from gnomon.appshost.verb_tools import APP_VERB_NAMES
from gnomon.hub import caller_binding as cb
from gnomon.hub.mcp import GmMCPService
from gnomon.hub.registry import SessionRegistry

READ = {"app_list", "app_status"}
AGENT_ACT = {"app_start", "app_stop", "app_open", "app_propose_install"}   # operator 2026-10-08 D1
REFUSED = {"app_propose_uninstall"}
ACT = AGENT_ACT | REFUSED


def test_every_app_verb_is_in_the_registry_and_classified():
    assert READ | ACT == set(APP_VERB_NAMES), "a new app verb must be classified here and in CALLER_SCOPES"
    for n in APP_VERB_NAMES:
        assert n in cb.CALLER_SCOPES, n


def test_read_class_is_bound_agent_scope():
    bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
    for n in READ:
        assert cb.CALLER_SCOPES[n].caller_scope == cb.BOUND_AGENT
        assert cb.authorize(n, {}, bound).allowed is True
        assert cb.authorize(n, {}, cb.UNBOUND).allowed is False          # control: an unbound caller is refused
        assert cb.visible(n, bound) is True


def test_agent_act_class_is_bound_agent_scope():
    bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
    for n in AGENT_ACT:
        assert cb.CALLER_SCOPES[n].caller_scope == cb.BOUND_AGENT, n
        assert cb.authorize(n, {"app_id": "x"}, bound).allowed is True, n
        assert cb.visible(n, bound) is True, n
        d = cb.authorize(n, {"app_id": "x"}, cb.UNBOUND)                      # control: an unbound caller is refused
        assert d.allowed is False, n
        assert cb.visible(n, cb.UNBOUND) is False, n


def test_propose_uninstall_is_still_refused_for_every_caller_and_never_advertised():
    bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
    op = cb.Principal.in_process_operator()
    for n in REFUSED:
        assert cb.CALLER_SCOPES[n].caller_scope == cb.REFUSED
        for p in (bound, op, cb.UNBOUND):
            d = cb.authorize(n, {"app_id": "x"}, p)
            assert d.allowed is False and "refused" in d.reason and "unscoped" not in d.reason, (n, d)
            assert cb.visible(n, p) is False


def test_service_tools_list_under_binding_shows_read_and_agent_act_verbs_but_not_uninstall():
    svc = GmMCPService(SessionRegistry(), caller_binding=True, apps_host=lambda: None, apps_enabled=lambda: True)
    bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
    names = {t["name"] for t in svc.tools(bound)}
    assert (READ | AGENT_ACT) <= names and not (REFUSED & names)
    out = svc.dispatch("app_propose_uninstall", {"app_id": "x"}, principal=bound)
    assert out["ok"] is False and out.get("refused") == "caller-binding"


def _svc(e):
    return GmMCPService(SessionRegistry(), caller_binding=True, apps_host=lambda: e.host, apps_enabled=lambda: True)


def test_bound_agent_can_propose_install_but_it_is_not_installed_until_the_confirm_route(tmp_path):
    from appshost_support import Env
    from gnomon.appshost.host import Actor
    from gnomon.feature_events import VIEWER_ACTOR

    e = Env(tmp_path)
    try:
        svc = _svc(e)
        bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
        mp = e.manifest("proposed")
        r = svc.dispatch("app_propose_install", {"manifest_path": mp}, principal=bound)
        assert r["ok"] is True, r
        lst = e.host.list()
        assert [p["id"] for p in lst["pending"]] == ["proposed"] and lst["apps"] == []        # pending, NOT installed
        assert e.host.detail("proposed")["installed"] is False
        s = svc.dispatch("app_start", {"app_id": "proposed"}, principal=bound)                # the host refuses an uninstalled app
        assert s["ok"] is False and "refused by caller binding" not in s.get("error", "")
        assert [x["id"] for x in e.host.list()["apps"]] == []
        c = e.host.confirm("proposed", Actor(VIEWER_ACTOR, False), {})                         # the operator's confirmation
        assert c["ok"], c
        assert [x["id"] for x in e.host.list()["apps"]] == ["proposed"]
    finally:
        e.cleanup()


def test_bound_agent_can_start_open_and_stop_a_registered_fixture_app_and_unbound_cannot(tmp_path):
    from appshost_support import Env

    e = Env(tmp_path)
    try:
        a = e.install(e.manifest("fixture", ui_path="/ui"))
        svc = _svc(e)
        bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
        for verb in ("app_start", "app_open", "app_stop"):                                    # control first: unbound is refused
            d = svc.dispatch(verb, {"app_id": a}, principal=cb.UNBOUND)
            assert d["ok"] is False and d.get("refused") == "caller-binding", verb
        assert e.host.status(a)["status"] != "running" and e.opened == []
        r = svc.dispatch("app_start", {"app_id": a}, principal=bound)
        assert r["ok"] is True, r
        e.wait_status(a, "running")
        r = svc.dispatch("app_open", {"app_id": a}, principal=bound)
        assert r["ok"] is True, r
        assert len(e.opened) == 1
        r = svc.dispatch("app_stop", {"app_id": a}, principal=bound)
        assert r["ok"] is True and r["was_running"] is True, r
        r = svc.dispatch("app_start", {"app_id": "not-registered"}, principal=bound)          # host's own check stays
        assert r["ok"] is False and r.get("refused") != "caller-binding"
    finally:
        e.cleanup()


def test_a_bound_agents_app_calls_are_audited_under_its_binding_instance_id_verified(tmp_path):
    """operator ruling 2026-10-08: WHO acted. The recorded actor is the binding's instance id (verified), never a tool argument; the
    operator page's actor is unchanged."""
    from appshost_support import Env

    e = Env(tmp_path)
    try:
        a = e.install(e.manifest("whoacted"))
        svc = _svc(e)
        bound = cb.Principal(bound_instance="agent-a", caller_header="bound")
        assert svc.dispatch("app_start", {"app_id": a}, principal=bound)["ok"] is True
        e.wait_status(a, "running")
        # an argument naming ANOTHER instance / caller does not change the recorded actor
        svc.dispatch("app_stop", {"app_id": a, "instance_id": "agent-evil", "caller": "agent-evil", "actor": "agent-evil"}, principal=bound)
        mp = e.manifest("whoproposed")
        assert svc.dispatch("app_propose_install", {"manifest_path": mp}, principal=bound)["ok"] is True
        rows = [r for r in e.audit(a) if r["verb"] in ("app_start", "app_stop")]
        rows += [r for r in e.audit("whoproposed") if r["verb"] == "app_propose_install"]
        assert {r["verb"] for r in rows} >= {"app_start", "app_propose_install"}
        for r in rows:
            assert r["actor"] == "agent-a" and r["actor_verified"] is True, r
        assert not [r for r in rows if "evil" in str(r["actor"])]
    finally:
        e.cleanup()

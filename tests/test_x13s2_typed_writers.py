"""X13 S2 slice bullet: typed-writer enforcement. An item needs a project in its backlog's workspace; a topic has exactly one;
an archived project is refused only for NEW assignments; re-files append project-history; current-project rules; the
registration writers. Tmp roots only (g31)."""

import json
import os

import pytest

from x13s2_world import build_world, tree_digest
from gnomon.workspace import events as ev
from gnomon.workspace.ltsread import read_file
from gnomon.workspace.scope import ScopeSession, load_session_scope
from gnomon.workspace.scopebase import ScopeRefusal


@pytest.fixture
def w(tmp_path):
    return build_world(tmp_path)


def _sess(cp=None, ws="wsa", roles=("wsa",)):
    return ScopeSession(ws, cp, roles)


# ---- items
def test_an_item_needs_a_project_and_a_missing_one_is_a_refusal_naming_the_missing_project(w):
    wr = w.writer()
    digest = tree_digest(w.root)
    with pytest.raises(ScopeRefusal) as e:
        wr.create_item("wsa/roadmap", "RM-11", "no project", session=_sess(None), by="r")
    assert e.value.code == "project-required" and "project" in e.value.message
    with pytest.raises(ScopeRefusal) as e:
        wr.create_item("wsa/roadmap", "RM-11", "no session", by="r")
    assert e.value.code == "no-scope-session"
    assert tree_digest(w.root) == digest


def test_an_item_takes_the_sessions_current_project_at_tier_2_and_stores_only_the_project(w):
    wr = w.writer()
    out = wr.create_item("wsa/roadmap", "RM-12", "via session", session=_sess("wsa/beta"), props={"status": "ready"}, by="r")
    assert (out["tier"], out["project"]) == (2, "wsa/beta")
    n = {x.id: x for x in read_file(w.roadmap_file())}["RM-12"]
    assert n.get("project") == "wsa/beta" and n.get("status") == "ready" and set(n.props) == {"status", "project"}, "no workspace is stored (I9)"


def test_an_item_whose_project_is_not_in_the_backlogs_workspace_is_refused(w):
    wr = w.writer()
    for session, project in ((None, "wsb/delta"), (_sess("wsb/delta", "wsb", ("wsa", "wsb")), None)):
        with pytest.raises(ScopeRefusal) as e:
            wr.create_item("wsa/roadmap", "RM-13", "wrong ws", project=project, session=session, by="r")
        assert e.value.code == "project-not-in-backlog-workspace"


def test_items_are_refused_into_the_reserved_project(w):
    with pytest.raises(ScopeRefusal) as e:
        w.writer().create_item("wsa/roadmap", "RM-14", "x", project="wsa/unassigned", by="r")
    assert e.value.code == "reserved-project"
    with pytest.raises(ScopeRefusal) as e:
        w.writer().refile_item("wsa/roadmap", "RM-1", "wsa/unassigned", by="r")
    assert e.value.code == "reserved-project"


def test_an_item_in_a_backlog_whose_corpus_is_machine_owned_is_refused(w):
    p = os.path.join(w.deploy, "workspaces", "wsa", "workspace.json")
    obj = json.load(open(p))
    obj["backlogs"][0]["corpus"] = "corp-shared"
    obj["backlogs"][0]["root-node"] = "SH-ROOT"
    json.dump(obj, open(p, "w"))
    with pytest.raises(ScopeRefusal) as e:
        w.writer().create_item("wsa/roadmap", "RM-15", "shared", project="wsa/alpha", by="r")
    assert e.value.code == "backlog-corpus-not-owned"


def test_item_creation_refuses_bad_ids_scope_key_props_duplicates_and_unknown_backlogs(w):
    wr = w.writer()
    for kw, code in (({"node_id": "RM-1"}, "item-exists"), ({"node_id": "RM-16", "props": {"project": "wsb/delta"}}, "scope-key-not-settable"),
                     ({"node_id": "RM-17", "props": {"workspace": "wsb"}}, "scope-key-not-settable"),
                     ({"node_id": "RM-18", "props": {"project-history": "x"}}, "scope-key-not-settable")):
        args = {"node_id": "RM-x", "props": None}
        args.update(kw)
        with pytest.raises(ScopeRefusal) as e:
            wr.create_item("wsa/roadmap", args["node_id"], "t", props=args["props"], project="wsa/alpha", by="r")
        assert e.value.code == code, kw
    with pytest.raises(ScopeRefusal) as e:
        wr.create_item("wsa/nosuch", "RM-19", "t", project="wsa/alpha", by="r")
    assert e.value.code == "backlog-unknown"
    with pytest.raises(ScopeRefusal):
        wr.create_item("roadmap", "RM-19", "t", project="wsa/alpha", by="r")      # an unqualified backlog ref


def test_refile_refuses_a_noop_a_missing_item_and_keeps_history_append_only(w):
    wr = w.writer()
    with pytest.raises(ScopeRefusal) as e:
        wr.refile_item("wsa/roadmap", "RM-1", "wsa/alpha", by="r")
    assert e.value.code == "already-in-project"
    with pytest.raises(ScopeRefusal) as e:
        wr.refile_item("wsa/roadmap", "RM-404", "wsa/beta", by="r")
    assert e.value.code == "item-unknown"
    wr.refile_item("wsa/roadmap", "RM-1", "wsa/beta", by="a")
    wr.refile_item("wsa/roadmap", "RM-1", "wsa/alpha", by="b")
    h = {x.id: x for x in read_file(w.roadmap_file())}["RM-1"].props["project-history"]
    assert [x.split(" ", 1)[1] for x in h] == ["wsa/alpha -> wsa/beta by a", "wsa/beta -> wsa/alpha by b"]


# ---- topics
def test_a_topic_has_exactly_one_project_and_a_list_or_a_missing_one_is_refused(w):
    wr = w.writer()
    with pytest.raises(ScopeRefusal) as e:
        wr.create_topic("two", project=["wsa/alpha", "wsa/beta"], by="r")            # type: ignore[arg-type]
    assert e.value.code == "project-not-qualified"
    with pytest.raises(ScopeRefusal) as e:
        wr.create_topic("none", session=_sess(None), by="r")
    assert e.value.code == "project-required"
    t = wr.create_topic("ok", project="wsa/alpha", by="r", body="b")
    rec = json.load(open(t["path"]))
    assert rec["project"] == "wsa/alpha" and "projects" not in rec and "workspace" not in rec and rec["state"] == "active"


def test_topic_ids_are_machine_wide_and_live_under_their_projects_workspace(w):
    wr = w.writer()
    a = wr.create_topic("a", project="wsa/alpha", by="r")
    b = wr.create_topic("b", project="wsb/delta", by="r")
    assert (a["id"], b["id"]) == ("TOP-001", "TOP-002")
    assert a["path"].endswith(os.path.join("workspaces", "wsa", "topics", "TOP-001.json"))
    assert b["path"].endswith(os.path.join("workspaces", "wsb", "topics", "TOP-002.json"))


def test_a_topic_takes_the_current_project_in_another_workspace_than_the_launch_workspace(w):
    t = w.writer().create_topic("x", session=_sess("wsb/delta", "wsa", ("wsa", "wsb")), by="r")
    assert (t["workspace"], t["tier"]) == ("wsb", 2) and "wsb" in t["path"]


# ---- current-project
def test_set_current_project_rules_and_logging(w):
    wr = w.writer()
    d = w.session("r5", current=None, role_workspaces=("wsa",))
    s = wr.set_current_project(d, "wsa/beta", by="r")
    assert s.current_project == "wsa/beta" and load_session_scope(d).current_project == "wsa/beta"
    with pytest.raises(ScopeRefusal) as e:                                   # not one of the role's workspaces
        wr.set_current_project(d, "wsb/delta", by="r")
    assert e.value.code == "project-not-in-role-workspaces"
    with pytest.raises(ScopeRefusal) as e:
        wr.set_current_project(d, "wsa/nope", by="r")
    assert e.value.code == "project-unknown"
    cleared = wr.set_current_project(d, None, by="r")
    assert cleared.current_project is None
    hist = load_session_scope(d).history
    assert [h["project"] for h in hist] == ["wsa/beta", None]
    kinds = [x["kind"] for x in w.events()]
    assert kinds == ["current-project-changed", "current-project-changed"]


# ---- registration writers (besides the prefix refusals in test_x13s2_invariants_i1_i5)
def test_add_backlog_needs_a_corpus_owned_by_the_same_workspace_and_registers_the_prefix(w):
    wr = w.writer()
    good = {"id": "extra", "label": "Extra", "corpus": "corp-a", "prefix": "EXTRA", "root-node": "RM-ROOT", "operator-gate-value": "op"}
    for corpus in ("corp-shared", "corp-b", "corp-missing"):
        with pytest.raises(ScopeRefusal) as e:
            wr.add_backlog("wsa", dict(good, corpus=corpus))
        assert e.value.code == "backlog-corpus-not-owned", corpus
    entry = wr.add_backlog("wsa", good)
    assert entry["prefix"] == "EXTRA"
    from gnomon.workspace.model import load_model

    assert [b.id for b in load_model(w.deploy).workspace("wsa").backlogs] == ["roadmap", "extra"]
    with pytest.raises(ScopeRefusal) as e:
        wr.add_backlog("wsa", dict(good, prefix="EXTRA2"))
    assert e.value.code == "backlog-exists"
    with pytest.raises(ScopeRefusal) as e:
        wr.add_backlog("wsa", {k: v for k, v in good.items() if k != "operator-gate-value"} | {"id": "e2", "prefix": "E2"})
    assert e.value.code == "backlog-fields"


def test_add_backlog_is_refused_in_an_archived_workspace(w):
    wr = w.writer()
    wr.archive_workspace("wsb", by="r")
    with pytest.raises(ScopeRefusal) as e:
        wr.add_backlog("wsb", {"id": "x", "label": "x", "corpus": "corp-b", "prefix": "XB", "root-node": "RQ-ROOT", "operator-gate-value": "op"})
    assert e.value.code == "workspace-archived"


def test_add_corpus_validations(w):
    wr = w.writer()
    roots = [os.path.join(w.root, "corpora", "new")]
    cases = (
        (dict(prefix="P1", owner="workspace:nope"), "corpus-owner"),
        (dict(prefix="P2", owner="both"), "corpus-owner"),
        (dict(prefix="P3", owner="machine", visible_in=["wsa"]), "visible-in-machine-owned"),
        (dict(prefix="P4", owner="workspace:wsa", visible_in=["nope"]), "visible-in-unknown"),
        (dict(prefix="P5", owner="workspace:wsa", projects=["wsb/delta"]), "corpus-project-workspace"),
        (dict(prefix="bad prefix", owner="machine"), "prefix-malformed"),
    )
    digest = tree_digest(w.root)
    for kw, code in cases:
        with pytest.raises(ScopeRefusal) as e:
            wr.add_corpus("corp-n", roots=roots, **kw)
        assert e.value.code == code, kw
    assert tree_digest(w.root) == digest
    wr.add_corpus("corp-n", roots=roots, prefix="P6", owner="workspace:wsa", visible_in=["wsb"], projects=["wsa/alpha"])
    assert json.load(open(w.corpus_config))["corp-n"]["visible-in"] == ["wsb"]
    with pytest.raises(ScopeRefusal) as e:
        wr.add_corpus("corp-n", roots=roots, prefix="P7", owner="machine")
    assert e.value.code == "corpus-exists"


def test_add_workspace_with_a_backlog_registers_it_and_its_corpus_must_belong_to_the_new_workspace(w):
    wr = w.writer()
    cfg = json.load(open(w.corpus_config))
    cfg["corp-c"] = {"roots": [{"path": os.path.join(w.root, "corpora", "c"), "area": "specs"}], "owner": "workspace:wsc"}
    cfg["corp-e"] = {"roots": [{"path": os.path.join(w.root, "corpora", "e"), "area": "specs"}], "owner": "workspace:wse"}
    json.dump(cfg, open(w.corpus_config, "w"))
    bl = {"id": "main", "label": "Main", "corpus": "corp-c", "prefix": "CC", "root-node": "CC-ROOT", "operator-gate-value": "op"}
    with pytest.raises(ScopeRefusal) as e:
        wr.add_workspace("wsd", "WSD", backlogs=[dict(bl, corpus="corp-a")])
    assert e.value.code == "backlog-corpus-not-owned"
    wr.add_workspace("wsc", "WSC", backlogs=[bl])
    reg = json.load(open(os.path.join(w.data, "prefix-registry.json")))
    assert reg["prefixes"]["CC"]["owner"] == "wsc/main"
    with pytest.raises(ScopeRefusal) as e:
        wr.add_workspace("wse", "WSE", backlogs=[dict(bl, corpus="corp-e", id="a", prefix="DUP"), dict(bl, corpus="corp-e", id="b", prefix="DUP")])
    assert e.value.code == "prefix-registered", "two new backlogs may not share a prefix either"


# ---- the event log
def test_the_event_log_is_append_only_with_a_loud_corrupt_line_marker(w):
    wr = w.writer()
    a = wr.write_alert("one", targets=["role-one"])
    b = wr.write_alert("two")
    text = open(w.event_log).read()
    assert text.count("\n") == 2
    open(w.event_log, "a").write("{not json\n")
    evs = list(ev.read_events(w.event_log))
    assert [e["kind"] for e in evs] == ["alert", "alert", "corrupt-line"] and evs[2]["line"] == 3
    assert a["targets"] == ["role-one"] and a["id"] != b["id"]
    with pytest.raises(ScopeRefusal):
        ev.retag_event(w.event_log, "EV-nope", project_tag="wsa/alpha", enabled=True)
    with pytest.raises(ScopeRefusal):
        ev.append_event(w.event_log, "alert", project_tag="alpha", enabled=True)

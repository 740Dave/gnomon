"""X13 S2, invariant I9 and the stamping library: the tier-0..3 precedence per record kind, with the scope
session defined per kind, the snapshot rule and the stored-workspace rule. Each test states the spec sentence it checks.

Acceptance (slice plan S2): 'each invariant's `test` property as a persisted test, shown failing on the pre-slice tree'.
"""

import os

import pytest

from x13s2_world import Clock, build_world
from gnomon.workspace import audit, scope
from gnomon.workspace.model import load_model
from gnomon.workspace.scope import ScopeSession, Subject, stamp, stamp_timeline
from gnomon.workspace.scopebase import ScopeRefusal


@pytest.fixture
def w(tmp_path):
    return build_world(tmp_path)


@pytest.fixture
def model(w):
    return load_model(w.deploy)


def _sess(cp=None, ws="wsa", roles=("wsa", "wsb")):
    return ScopeSession(ws, cp, roles)


# ---- I9 test property, sentence 1: workspace read == the workspace of the project in projects.json, nothing stored
def test_i9_workspace_is_derived_from_the_project_for_every_kind_that_carries_one(w, model):
    wr = w.writer()
    b = model.backlog("wsa/roadmap")
    item = wr.create_item("wsa/roadmap", "RM-10", "New item", project="wsa/beta", by="t")
    topic = wr.create_topic("A topic", project="wsb/delta", by="t")
    dec = wr.stamp_decision(w.answers_file(), "DEC-1", "Decision one", project="wsa/alpha")
    # each record's workspace, read back from what is STORED, equals the workspace of its project
    from gnomon.workspace.ltsread import read_file

    m = {n.id: n for n in read_file(w.roadmap_file())}
    assert audit.record_workspace({"project": m["RM-10"].get("project")}, model) == "wsa" == item["workspace"]
    assert topic["workspace"] == "wsb"
    import json

    rec = json.load(open(topic["path"]))
    assert "workspace" not in rec and audit.record_workspace(rec, model) == "wsb"
    assert os.path.dirname(os.path.dirname(topic["path"])).endswith(os.path.join("workspaces", "wsb"))
    dn = {n.id: n for n in read_file(w.answers_file())}["DEC-1"]
    assert "workspace" not in dn.props and dn.get("project") == "wsa/alpha" and dec["workspace"] == "wsa"
    for kind, record in (("item", {"project": m["RM-10"].get("project")}), ("topic", rec), ("dsd-decision", {"project": dn.get("project")})):
        assert audit.check_record_scope(kind, record, model) == [], kind


def test_i9_no_separate_workspace_is_stored_beside_a_project_and_the_audit_catches_a_planted_one(model):
    """Positive control: a record that stores a workspace beside its project is reported."""
    bad = {"project": "wsa/alpha", "workspace": "wsb"}
    v = audit.check_record_scope("item", bad, model)
    assert any("derived" in x for x in v), v
    assert audit.check_record_scope("item", {"project": "wsa/alpha"}, model) == []
    assert audit.check_record_scope("item", {}, model) == ["item has no project (required)"]


def test_i9_a_workspace_may_be_stored_only_at_tier_3_and_then_it_is_the_launch_workspace(w, model):
    st = stamp(model, "dsd-decision", session=_sess(None, "wsb"))
    assert (st.tier, st.project, st.workspace, st.stored_workspace) == (3, None, "wsb", "wsb")
    assert st.record_fields() == {"workspace": "wsb"}
    assert audit.check_record_scope("dsd-decision", st.record_fields(), model) == []
    # at any other tier nothing is stored
    st2 = stamp(model, "dsd-decision", explicit="wsa/alpha")
    assert st2.stored_workspace is None and "workspace" not in st2.record_fields()


def test_i9_stored_workspace_type_b_the_timeline_key_equals_the_derived_workspace(model):
    """stored-workspace-rule (b): the timeline key's workspace component is computed from the projects; a test asserts equality."""
    out = stamp_timeline(model, launch_workspace="wsa", change_events=[{"ts": "2026-09-30T00:00:00Z", "project": "wsb/delta"}],
                         segment_start="2026-09-30T01:00:00Z", segment_end="2026-09-30T02:00:00Z")
    assert [(t.workspace, t.projects) for t in out] == [("wsb", ("wsb/delta",))]
    entry = {"workspace": out[0].workspace, "projects": list(out[0].projects)}
    assert audit.check_timeline_key(entry, model) == []
    assert audit.check_timeline_key({"workspace": "wsa", "projects": ["wsb/delta"]}, model), "a mismatched key must be reported"


# ---- I9 test property, sentence 2: a DSD decision with a subject takes project + workspace from the SUBJECT, not the session
def test_i9_a_decision_with_a_subject_takes_scope_from_the_subject_not_the_session(model):
    st = stamp(model, "dsd-decision", subjects=[Subject("RQ-1", ("wsb/delta",))], session=_sess("wsa/alpha", "wsa"))
    assert (st.tier, st.project, st.workspace) == (1, "wsb/delta", "wsb")
    assert st.subject_ids == ("RQ-1",)


# ---- I9 test property, sentence 3: snapshot. Re-file a subject; the decision is unchanged; the subject's project-history shows the move
def test_i9_snapshot_refiling_a_subject_leaves_the_stamped_decision_unchanged(w):
    wr = w.writer()
    wr.stamp_decision(w.answers_file(), "DEC-S", "Snapshot decision", subjects=[Subject("RM-1", ("wsa/alpha",))], answered_by="op")
    before = open(w.answers_file()).read()
    wr.refile_item("wsa/roadmap", "RM-1", "wsa/beta", by="role-one")
    assert open(w.answers_file()).read() == before, "re-filing a subject must NOT cascade to records stamped from it"
    from gnomon.workspace.ltsread import read_file

    dec = {n.id: n for n in read_file(w.answers_file())}["DEC-S"]
    assert dec.get("project") == "wsa/alpha" and dec.props["subject-items"] == ["RM-1"]
    item = {n.id: n for n in read_file(w.roadmap_file())}["RM-1"]
    assert item.get("project") == "wsa/beta"
    hist = item.props["project-history"]
    assert len(hist) == 1 and hist[0].endswith("wsa/alpha -> wsa/beta by role-one"), hist


# ---- precedence, tier by tier
def test_i9_tier0_an_explicit_project_wins_over_subject_and_session(model):
    st = stamp(model, "dsd-decision", explicit="wsa/gamma", subjects=[Subject("RQ-1", ("wsb/delta",))], session=_sess("wsa/alpha"))
    assert (st.tier, st.project) == (0, "wsa/gamma")


def test_i9_tier0_must_be_qualified_known_and_for_new_assignments_active(w, model):
    for bad in ("alpha", "wsa/", "WSA/alpha"):
        with pytest.raises(ScopeRefusal) as e:
            stamp(model, "topic", explicit=bad, session=_sess())
        assert e.value.code == "project-not-qualified"
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "topic", explicit="wsa/nope", session=_sess())
    assert e.value.code == "project-unknown"
    w.set_project_state("wsa", "gamma", "archived")
    m2 = load_model(w.deploy)
    with pytest.raises(ScopeRefusal) as e:
        stamp(m2, "topic", explicit="wsa/gamma", session=_sess())
    assert e.value.code == "project-archived" and "re-pick" in e.value.prompt
    # ... but an evidence kind may name an archived project
    assert stamp(m2, "dsd-decision", explicit="wsa/gamma").project == "wsa/gamma"


def test_i9_item_tier0_must_be_in_the_backlogs_workspace_and_not_the_reserved_project(model):
    b = model.backlog("wsa/roadmap")
    assert stamp(model, "item", explicit="wsa/alpha", backlog=b).workspace == "wsa"
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "item", explicit="wsb/delta", backlog=b)
    assert e.value.code == "project-not-in-backlog-workspace"
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "item", explicit="wsa/unassigned", backlog=b)
    assert e.value.code == "reserved-project"


def test_i9_tier1_exactly_one_subject_with_one_project(model):
    st = stamp(model, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",))])
    assert (st.tier, st.project) == (1, "wsa/alpha")


def test_i9_tier1_subjects_spanning_projects_in_one_workspace_stamp_that_workspaces_unassigned(model):
    st = stamp(model, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",)), Subject("RM-9", ("wsa/beta",))])
    assert (st.tier, st.project, st.workspace) == (1, "wsa/unassigned", "wsa")
    assert len(st.subject_ids) == 2, "subject-items stays complete for linkage"


def test_i9_tier1_subjects_spanning_workspaces_are_refused_with_a_split_instruction(model):
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",)), Subject("RQ-1", ("wsb/delta",))], session=_sess("wsa/alpha"))
    assert e.value.code == "subjects-span-workspaces" and "split" in e.value.prompt
    # tier 0 resolves it
    assert stamp(model, "dsd-decision", explicit="wsb/delta", subjects=[Subject("RM-1", ("wsa/alpha",)), Subject("RQ-1", ("wsb/delta",))]).tier == 0


def test_i9_tier1_several_subjects_that_all_carry_the_SAME_project_stamp_tier_1(model):
    """Architect ruling (1), 2026-09-30: if they all carry the SAME project, tier 1 applies; refuse only when they differ."""
    st = stamp(model, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",)), Subject("RM-2", ("wsa/alpha",))], session=_sess("wsa/beta"))
    assert (st.tier, st.project, st.workspace) == (1, "wsa/alpha", "wsa")
    assert st.subject_ids == ("RM-1", "RM-2"), "subject-items stays complete for linkage"
    st3 = stamp(model, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",)), Subject("RM-2", ("wsa/alpha",)), Subject("X", ())])
    assert (st3.tier, st3.project) == (1, "wsa/alpha"), "a subject that carries no project does not count"
    # they differ: same workspace -> <ws>/unassigned; across workspaces -> refused (covered above)
    assert stamp(model, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",)), Subject("RM-2", ("wsa/beta",))]).project == "wsa/unassigned"


def test_i9_tier1_may_name_an_archived_subject_project_evidence(w):
    w.set_project_state("wsa", "alpha", "archived")
    m = load_model(w.deploy)
    st = stamp(m, "dsd-decision", subjects=[Subject("RM-1", ("wsa/alpha",))])
    assert (st.tier, st.project) == (1, "wsa/alpha")


def test_i9_tier1_a_subject_with_no_project_falls_through_to_the_session(model):
    st = stamp(model, "dsd-decision", subjects=[Subject("X-1", ())], session=_sess("wsb/epsilon", "wsb"))
    assert (st.tier, st.project) == (2, "wsb/epsilon")


def test_i9_tier2_the_scope_sessions_current_project(model):
    st = stamp(model, "topic", session=_sess("wsb/delta", "wsa"))
    assert (st.tier, st.project, st.workspace) == (2, "wsb/delta", "wsb"), "the launch workspace (wsa) is NOT the scope when a project exists"


def test_i9_tier2_an_archived_current_project_logs_an_event_and_falls_to_tier_3_or_refuses(w):
    w.set_project_state("wsa", "alpha", "archived")
    m = load_model(w.deploy)
    st = stamp(m, "dsd-decision", session=_sess("wsa/alpha", "wsa"))            # optional kind: falls to tier 3
    assert (st.tier, st.project, st.stored_workspace) == (3, None, "wsa")
    assert [e["kind"] for e in st.events] == ["current-project-archived"]
    with pytest.raises(ScopeRefusal) as e:                                        # required kind: refuses
        stamp(m, "topic", session=_sess("wsa/alpha", "wsa"))
    assert e.value.code == "project-required"
    assert [x["kind"] for x in e.value.events] == ["current-project-archived"], "the refusal still carries the event to log"


def test_i9_tier3_is_legal_only_for_optional_project_kinds(model):
    assert stamp(model, "dsd-decision", session=_sess(None, "wsa")).tier == 3
    assert stamp(model, "built-live-stamp", session=_sess(None, "wsa")).tier == 3
    for kind, kw in (("topic", {}), ("item", {"backlog": model.backlog("wsa/roadmap")})):
        with pytest.raises(ScopeRefusal) as e:
            stamp(model, kind, session=_sess(None, "wsa"), **kw)
        assert e.value.code == "project-required" and "name a project" in e.value.prompt


def test_i9_item_tier2_current_project_must_be_in_the_backlogs_workspace(model):
    b = model.backlog("wsa/roadmap")
    assert stamp(model, "item", session=_sess("wsa/beta"), backlog=b).tier == 2
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "item", session=_sess("wsb/delta", "wsb"), backlog=b)
    assert e.value.code == "project-not-in-backlog-workspace"


def test_i9_unknown_kind_and_missing_session_are_refusals(model):
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "spaceship", session=_sess())
    assert e.value.code == "kind-unknown"
    with pytest.raises(ScopeRefusal) as e:
        stamp(model, "topic")
    assert e.value.code == "no-scope-session"


# ---- scope-session per kind: timeline = the REVIEWED run; session-run exempt
def test_i9_timeline_scope_is_the_reviewed_runs_change_events_split_per_workspace(model):
    evs = [{"ts": "2026-09-30T00:00:00Z", "project": "wsa/alpha"}, {"ts": "2026-09-30T10:30:00Z", "project": "wsb/delta"},
           {"ts": "2026-09-30T10:45:00Z", "project": "wsa/beta"}]
    out = stamp_timeline(model, launch_workspace="wsa", change_events=evs, segment_start="2026-09-30T10:00:00Z", segment_end="2026-09-30T11:00:00Z")
    assert [(t.workspace, t.projects) for t in out] == [("wsa", ("wsa/alpha", "wsa/beta")), ("wsb", ("wsb/delta",))], \
        "one entry per workspace, each carrying ONLY that workspace's projects; the project in force at the start counts"
    none = stamp_timeline(model, launch_workspace="wsb", change_events=[], segment_start="2026-09-30T10:00:00Z", segment_end="2026-09-30T11:00:00Z")
    assert [(t.workspace, t.projects, t.tier) for t in none] == [("wsb", (), 3)], "no project in force: tier 3, the reviewed run's launch workspace"
    cleared = stamp_timeline(model, launch_workspace="wsb", change_events=[{"ts": "2026-09-30T09:00:00Z", "project": "wsa/alpha"}, {"ts": "2026-09-30T09:30:00Z", "project": None}],
                             segment_start="2026-09-30T10:00:00Z", segment_end="2026-09-30T11:00:00Z")
    assert cleared[0].projects == ()
    with pytest.raises(ScopeRefusal):
        stamp_timeline(model, launch_workspace="wsa", change_events=[], segment_start="2026-09-30T11:00:00Z", segment_end="2026-09-30T10:00:00Z")


def test_i9_a_built_live_stamp_takes_the_subjects_scope_even_if_archived(w):
    wr = w.writer()
    w.set_project_state("wsa", "alpha", "archived")            # RM-1 is in wsa/alpha
    out = wr.record_built_live("wsa/roadmap", "RM-1", "built", "sess-1")
    assert (out["project"], out["workspace"], out["tier"]) == ("wsa/alpha", "wsa", 1)
    from gnomon.workspace.ltsread import read_file

    n = {x.id: x for x in read_file(w.roadmap_file())}["RM-1"]
    assert n.get("built-in-session").startswith("sess-1@2026-09-30T")


def test_i9_session_run_is_exempt_its_scope_is_its_launch_workspace_not_its_current_project(w):
    """ENT-SESSION-RUN: the session-run's own scope is its launch workspace; current-project may be in another workspace."""
    wr = w.writer()
    d = w.session("run-1", current=None, launch_ws="wsa", role_workspaces=("wsa", "wsb"))
    s = wr.set_current_project(d, "wsb/delta", by="role-one")
    assert s.launch_workspace == "wsa" and s.current_project == "wsb/delta"
    assert scope.load_session_scope(d).launch_workspace == "wsa"


def test_i9_initial_current_project_previous_value_then_default_then_none(w, model):
    f = scope.initial_current_project
    assert f(model, previous_value="wsa/beta", default_project="wsa/alpha", allowed_workspaces=("wsa",)) == "wsa/beta"
    assert f(model, previous_value=None, default_project="wsa/alpha", allowed_workspaces=("wsa",)) == "wsa/alpha"
    assert f(model, previous_value="wsb/delta", default_project="wsa/alpha", allowed_workspaces=("wsa",)) == "wsa/alpha", "previous is in a workspace the role may not use"
    assert f(model, previous_value="wsa/nope", default_project=None, allowed_workspaces=("wsa",)) is None
    w.set_project_state("wsa", "beta", "archived")
    assert f(load_model(w.deploy), previous_value="wsa/beta", default_project=None, allowed_workspaces=("wsa",)) is None, "an archived previous value is not reused"

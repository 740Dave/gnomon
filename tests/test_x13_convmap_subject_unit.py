"""Unit tests of ``workspace/subject.py`` (DSD-decision SUBJECT resolution, X13 pre-S3 deliverable 2): the precedence
and the ruling-1 reduction on synthetic ids and words only (the engine is workspace-neutral; no deployment literal)."""

import json

import pytest

from gnomon.workspace import subject as S

PROJECTS = {"alpha/one", "alpha/two", "alpha/unassigned", "beta/main", "beta/unassigned"}
ITEMS = {"AA-1": "alpha/one", "AA-2": "alpha/two", "AA-U": "alpha/unassigned", "BB-1": "beta/main"}
CORPUS = {"NODE-A": "alpha", "NODE-B": "beta", "NODE-AMBIG": None}
TERMS = [S.Term("one", "alpha/one", __import__("re").compile(r"\bwidget\b", 34)),
         S.Term("two", "alpha/two", __import__("re").compile(r"\bgadget\b", 34)),
         S.Term("main", "beta/main", __import__("re").compile(r"\bsprocket\b", 34))]


def res(subject="", answer="", dsd="", exclude=frozenset()):
    return S.resolve([("own-subject", subject), ("own-answer", answer), ("dsd", dsd)], ITEMS, CORPUS, TERMS, exclude)


def test_none_when_no_evidence_so_the_engine_falls_back_to_the_author():
    assert res("nothing here", "nor here", "nor here").status == S.NONE


def test_item_in_subject_names_its_project():
    r = res("What about AA-1?")
    assert (r.status, r.rule, r.project) == (S.RESOLVED, "own-subject:item", "alpha/one")


def test_precedence_scope_then_tier_item_over_corpus_over_term():
    assert res("AA-1 NODE-A sprocket").rule == "own-subject:item"      # F-1: was NODE-B (another workspace), which now refuses; see the F1 tests
    assert res("NODE-B widget").rule == "own-subject:corpus"
    assert res("widget", "AA-2").rule == "own-subject:term", "a narrower scope wins over a stronger tier in a wider scope"
    assert res("", "AA-2 widget").rule == "own-answer:item"


def test_scope_order_subject_answer_dsd():
    assert res("widget", "sprocket", "gadget").project == "alpha/one"
    assert res("", "sprocket", "gadget").rule == "own-answer:term"
    assert res("", "", "gadget").rule == "dsd:term"


def test_several_projects_one_workspace_is_that_workspaces_unassigned():
    r = res("AA-1 AA-2")
    assert (r.status, r.project, r.span) == (S.RESOLVED, "alpha/unassigned", True)
    r = res("widget gadget")
    assert (r.project, r.span) == ("alpha/unassigned", True)


def test_unassigned_is_itself_a_project():
    assert res("AA-U").project == "alpha/unassigned" and res("AA-U").span is False
    assert res("AA-U AA-1").span is True


def test_cross_workspace_hard_evidence_in_own_scope_is_refused():
    r = res("AA-1 BB-1")
    assert r.status == S.REFUSED and r.workspaces == ("alpha", "beta") and r.rule == "own-subject:item"
    assert res("", "NODE-A NODE-B").status == S.REFUSED
    # F-1 (audit-x13-convmap, 2026-10-01): this line used to assert RESOLVED ("different tiers are never mixed: the item tier
    # alone decides"); that was the defect. Hard evidence is pooled across its tiers, so a cross-workspace mix refuses.
    r = res("", "AA-1 NODE-B")
    assert r.status == S.REFUSED and r.rule == "own-answer:item+corpus" and r.workspaces == ("alpha", "beta")


def test_F1_item_in_one_workspace_and_corpus_node_in_another_refuse_in_either_order_and_scope():
    for subj in ("AA-1 NODE-B", "NODE-B AA-1", "BB-1 NODE-A", "AA-U NODE-B"):
        r = res(subj)
        assert (r.status, r.rule) == (S.REFUSED, "own-subject:item+corpus"), subj
        assert set(r.evidence) == set(subj.split())
    assert res("", "AA-1 NODE-B").status == S.REFUSED


def test_F1_same_workspace_item_and_corpus_node_do_not_refuse_and_the_items_project_wins():
    r = res("AA-1 NODE-A")
    assert (r.status, r.rule, r.project) == (S.RESOLVED, "own-subject:item", "alpha/one")
    assert res("NODE-A").rule == "own-subject:corpus"


def test_F1_the_dsd_scope_and_an_ambiguous_corpus_node_never_refuse():
    assert res("", "", "AA-1 NODE-B").status == S.RESOLVED          # dsd scope: workspace-only evidence, never refuses
    assert res("AA-1 NODE-AMBIG").project == "alpha/one"             # an ambiguous node (owner None) is no evidence at all


def test_F1_across_scopes_the_narrowest_still_wins():
    r = res("AA-1", "BB-1 NODE-B")                                    # the answer's conflict is never reached
    assert (r.status, r.rule) == (S.RESOLVED, "own-subject:item")


def test_R6_sibling_workspace_unanimous_or_nothing():
    assert S.sibling_workspace({"D2": "alpha", "D3": "alpha"}) == "alpha"
    assert S.sibling_workspace({"D2": "alpha"}) == "alpha"
    assert S.sibling_workspace({}) is None
    assert S.sibling_workspace({"D2": "alpha", "D3": "beta"}) is None
    assert S.sibling_workspace({"D2": "alpha", "D3": "alpha", "D4": "beta"}) is None, "unanimous, not majority"
    assert S.SIBLINGS_RULE == "dsd-siblings"


def test_cross_workspace_heuristic_or_dsd_evidence_is_ambiguous_not_refused():
    assert res("widget sprocket").status == S.NONE
    assert res("widget sprocket", "", "").rule == "" and res("widget sprocket", "", "AA-1 BB-1").status == S.NONE
    r = res("widget sprocket", "gadget")                      # ambiguous subject line, the answer decides
    assert (r.rule, r.project) == ("own-answer:term", "alpha/unassigned")


def test_a_heuristic_word_outside_the_subject_line_names_only_the_workspace():
    r = res("", "widget")
    assert (r.project, r.workspace_only) == ("alpha/unassigned", True)
    r = res("", "", "sprocket")
    assert (r.project, r.workspace_only) == ("beta/unassigned", True)
    r = res("widget")
    assert (r.project, r.workspace_only) == ("alpha/one", False)
    assert res("", "AA-1").project == "alpha/one", "hard evidence in an own-* scope names its project"
    r = res("", "", "AA-1")
    assert (r.rule, r.project, r.workspace_only) == ("dsd:item", "alpha/unassigned", True), "a sibling's item names the workspace only"
    r = res("", "", "NODE-B")
    assert (r.rule, r.project) == ("dsd:corpus", "beta/unassigned")


def test_ambiguous_corpus_node_gives_no_evidence_and_self_ids_are_excluded():
    assert res("NODE-AMBIG").status == S.NONE
    assert res("see AA-1", exclude={"AA-1"}).status == S.NONE


def test_load_terms_absent_file_is_an_empty_table(tmp_path):
    assert S.load_terms(str(tmp_path), PROJECTS) == ([], [])


def _write(tmp_path, obj):
    (tmp_path / S.SUBJECT_RULES_FILE).write_text(obj if isinstance(obj, str) else json.dumps(obj))


def test_load_terms_ok(tmp_path):
    _write(tmp_path, {"_doc_what": "x", "terms": [{"name": "w", "project": "alpha/one", "pattern": "widget"}]})
    terms, errs = S.load_terms(str(tmp_path), PROJECTS)
    assert errs == [] and [t.name for t in terms] == ["w"] and terms[0].pattern.search("A WIDGET")


@pytest.mark.parametrize("obj,needle", [
    ('{not json', "unreadable"),
    ([], "top level"),
    ({"terms": [{"name": "w", "project": "alpha/zzz", "pattern": "x"}]}, "not a seeded project"),
    ({"terms": [{"name": "w", "project": "alpha", "pattern": "x"}]}, "not <workspace>/<slug>"),
    ({"terms": [{"name": "w", "project": "alpha/one", "pattern": "("}]}, "does not compile"),
    ({"terms": [{"name": "w", "project": "alpha/one"}]}, "needs string keys"),
    ({"terms": [{"name": "w", "project": "alpha/one", "pattern": "a"}, {"name": "w", "project": "alpha/one", "pattern": "b"}]}, "duplicate name"),
    ({"terms": [], "extra": 1}, "unknown top-level keys"),
])
def test_load_terms_malformed_is_an_error_never_a_silent_empty_table(tmp_path, obj, needle):
    _write(tmp_path, obj)
    terms, errs = S.load_terms(str(tmp_path), PROJECTS)
    assert any(needle in e for e in errs), errs


@pytest.mark.skip(reason="tests the one-time converter, which is not part of the public cut")
def test_corpus_map_origin_classifier_ratified_rulings_are_not_proposals():
    from gnomon.workspace import convert as C
    assert C.is_proposal_origin("builder-proposal-S1") and C.is_proposal_origin("")
    assert not C.is_proposal_origin("spec") and not C.is_proposal_origin("ruling:pre-s3-2026-10-01")
    assert C.is_proposal_origin("rulingx") and C.is_proposal_origin("Ruling:x")

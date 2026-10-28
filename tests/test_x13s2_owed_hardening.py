"""X13 S2 owed list (Diamond audit-X13-S2-r2): F-R2-2 (duplicate decision id), F-R2-3 / F-R2-10 (trailing newline in slugs, tags
and qualified projects), F-R2-5 (symlinked topics dir, non-ASCII digits in topic ids), F-R2-6 (dynamic imports in the I6 closure
check) and F-R2-9 (``stamp_decision(file=)``). F-R2-1 (the widened persistence-gate detector) is in test_x13s2_flag_gate.py.

Every test here fails on the S2 r2 tree (the fixes did not exist) and passes on the fixed one. Tmp roots only (g31)."""

import json
import os
import shutil

import pytest

from x13s2_world import build_world, tree_digest
from gnomon.workspace import audit, entities, events as ev, safety as S
from gnomon.workspace.model import load_model
from gnomon.workspace.scopebase import QUALIFIED_RE, SLUG_RE, ModelError, ScopeRefusal, split_qualified
from gnomon.workspace.store import TOPIC_ID_RE, TopicStore
from gnomon.workspace.writers import retag_record

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "workspace")


@pytest.fixture
def w(tmp_path):
    return build_world(tmp_path)


# ------------------------------------------------------------------ F-R2-2: stamp_decision refuses a duplicate node id (as create_item does)
def test_f_r2_2_a_second_decision_with_the_same_id_is_refused_and_the_first_stays_authoritative(w):
    wr = w.writer()
    wr.stamp_decision(w.answers_file(), "DEC-6", "first", project="wsa/alpha")
    before = tree_digest(w.root)
    ev_before = w.events()
    with pytest.raises(ScopeRefusal) as e:
        wr.stamp_decision(w.answers_file(), "DEC-6", "second", project="wsb/delta")
    assert e.value.code == "decision-exists"
    assert tree_digest(w.root) == before, "the refused write changed a byte"
    assert w.events() == ev_before, "a refused duplicate must not emit scope events either"
    from gnomon.workspace.ltsread import read_file

    nodes = [n for n in read_file(w.answers_file()) if n.id == "DEC-6"]
    assert len(nodes) == 1 and nodes[0].props["project"] == ["wsa/alpha"]


def test_f_r2_2_the_duplicate_check_covers_every_file_of_the_corpus_not_only_the_one_passed(w):
    wr = w.writer()
    second = os.path.join(os.path.dirname(w.answers_file()), "more.lts")
    open(second, "w").write("# More [MORE-ROOT]\n\n- status: ready\n")
    wr.stamp_decision(second, "DEC-7", "in the other file", project="wsa/alpha")
    with pytest.raises(ScopeRefusal) as e:
        wr.stamp_decision(w.answers_file(), "DEC-7", "same id, other file", project="wsb/delta")
    assert e.value.code == "decision-exists"


def test_f_r2_2_distinct_ids_still_stamp(w):
    wr = w.writer()
    wr.stamp_decision(w.answers_file(), "DEC-1", "a", project="wsa/alpha")
    wr.stamp_decision(w.answers_file(), "DEC-2", "b", project="wsb/delta")


# ------------------------------------------------------------------ F-R2-9: stamp_decision(file=) only appends to a registered machine-owned corpus file
def test_f_r2_9_a_file_outside_every_registered_corpus_is_refused_and_untouched(w, tmp_path):
    outside = tmp_path / "elsewhere.lts"
    outside.write_text("# X [X-ROOT]\n")
    before = outside.read_bytes()
    tree = tree_digest(w.root)
    with pytest.raises(ScopeRefusal) as e:
        w.writer().stamp_decision(str(outside), "DEC-9", "t", project="wsa/alpha")
    assert e.value.code == "decision-file-not-in-corpus"
    assert outside.read_bytes() == before and tree_digest(w.root) == tree


def test_f_r2_9_a_workspace_owned_backlog_file_is_refused(w):
    before = open(w.roadmap_file()).read()
    with pytest.raises(ScopeRefusal) as e:
        w.writer().stamp_decision(w.roadmap_file(), "DEC-9", "t", project="wsa/alpha")
    assert e.value.code == "decision-file-not-machine-corpus"
    assert open(w.roadmap_file()).read() == before


def test_f_r2_9_a_symlink_inside_the_corpus_pointing_out_is_not_followed(w, tmp_path):
    victim = tmp_path / "victim.lts"
    victim.write_text("# V [V-ROOT]\n")
    link = os.path.join(os.path.dirname(w.answers_file()), "link.lts")
    os.symlink(victim, link)
    with pytest.raises(ScopeRefusal) as e:
        w.writer().stamp_decision(link, "DEC-9", "t", project="wsa/alpha")
    assert e.value.code == "decision-file-invalid"
    assert victim.read_text() == "# V [V-ROOT]\n"


@pytest.mark.parametrize("bad", ["/does/not/exist.lts", "", None, 7])
def test_f_r2_9_a_missing_or_non_path_file_argument_is_refused(w, bad):
    with pytest.raises(ScopeRefusal) as e:
        w.writer().stamp_decision(bad, "DEC-9", "t", project="wsa/alpha")
    assert e.value.code == "decision-file-invalid"


def test_f_r2_9_a_non_lts_file_in_the_corpus_dir_is_refused(w):
    p = os.path.join(os.path.dirname(w.answers_file()), "notes.txt")
    open(p, "w").write("x")
    with pytest.raises(ScopeRefusal) as e:
        w.writer().stamp_decision(p, "DEC-9", "t", project="wsa/alpha")
    assert e.value.code == "decision-file-invalid"


# ------------------------------------------------------------------ F-R2-3 / F-R2-10: slugs, tags, qualified projects, prefixes use a full match
def test_f_r2_3_the_patterns_reject_a_trailing_newline_even_through_match():
    for ok in ("ws", "a-b1"):
        assert SLUG_RE.fullmatch(ok) and SLUG_RE.match(ok)
    assert SLUG_RE.fullmatch("newws\n") is None and SLUG_RE.match("newws\n") is None
    assert QUALIFIED_RE.fullmatch("wsa/alpha\n") is None and QUALIFIED_RE.match("wsa/alpha\n") is None
    assert QUALIFIED_RE.fullmatch("wsa/alpha")


def test_f_r2_3_add_workspace_with_a_trailing_newline_is_refused_and_creates_nothing(w):
    before = tree_digest(w.root)
    with pytest.raises(ScopeRefusal) as e:
        w.writer().add_workspace("newws\n", "X")
    assert e.value.code == "workspace-slug"
    assert tree_digest(w.root) == before
    assert not [d for d in os.listdir(os.path.join(w.deploy, "workspaces")) if d.startswith("newws")]


def test_f_r2_3_split_qualified_and_set_current_project_refuse_a_trailing_newline(w):
    with pytest.raises(ScopeRefusal) as e:
        split_qualified("wsa/alpha\n")
    assert e.value.code == "project-not-qualified"
    sd = w.session("nl", current="wsa/alpha")
    before = tree_digest(w.root)
    with pytest.raises(ScopeRefusal):
        w.writer().set_current_project(sd, "wsa/beta\n", by="r")
    assert tree_digest(w.root) == before


@pytest.mark.parametrize("kw", [{"workspace_tag": "wsa\n"}, {"project_tag": "wsa/alpha\n"}])
def test_f_r2_10_event_and_alert_tags_with_a_trailing_newline_are_refused(w, kw):
    wr = w.writer()
    with pytest.raises(ScopeRefusal) as e:
        wr.write_alert("x", **kw)
    assert e.value.code == "tag-malformed"
    with pytest.raises(ScopeRefusal):
        ev.append_event(w.event_log, "alert", enabled=True, **kw)
    with pytest.raises(ScopeRefusal):
        retag_record({"id": "x"}, **kw)
    ok = wr.write_alert("x")
    with pytest.raises(ScopeRefusal):
        wr.retag_alert(ok["id"], **kw)
    assert len(w.events()) == 1


def test_f_r2_3_backlog_id_corpus_id_and_prefix_with_a_trailing_newline_are_refused(w):
    wr = w.writer()
    good = {"id": "bk", "label": "x", "corpus": "corp-a", "prefix": "NEWPFX", "root-node": "RM-ROOT", "operator-gate-value": "op"}
    before = tree_digest(w.root)
    for patch, code in (({"id": "bk\n"}, "backlog-id"), ({"prefix": "NEWPFX\n"}, "prefix-malformed")):
        with pytest.raises(ScopeRefusal) as e:
            wr.add_backlog("wsa", {**good, **patch})
        assert e.value.code == code, patch
    with pytest.raises(ScopeRefusal) as e:
        wr.add_corpus("corp-z\n", roots=[os.path.join(w.root, "corpora", "new")], prefix="GATEZ", owner="machine")
    assert e.value.code == "corpus-id"
    assert tree_digest(w.root) == before


def test_f_r2_3_the_model_loader_refuses_a_slug_or_workspace_id_with_a_trailing_newline(w):
    p = os.path.join(w.deploy, "workspaces", "wsa", "projects.json")
    obj = json.load(open(p))
    obj["projects"].append({"slug": "bad\n", "name": "Bad", "state": "active", "source": "test"})
    json.dump(obj, open(p, "w"))
    with pytest.raises(ModelError):
        load_model(w.deploy)


def test_f_r2_3_topic_shape_and_the_project_ref_audit_refuse_a_trailing_newline(w):
    base = {"id": "TOP-001", "project": "wsa/alpha", "state": "active"}
    assert entities.topic_shape_errors(base, ws_dir="wsa", workspaces={"wsa": ["alpha"]}) == []
    assert entities.topic_shape_errors({**base, "id": "TOP-001\n"}, ws_dir="wsa", workspaces={"wsa": ["alpha"]})
    assert entities.topic_shape_errors({**base, "project": "wsa/alpha\n"}, ws_dir="wsa", workspaces={"wsa": ["alpha"]})
    d = os.path.join(w.root, "scan")
    os.makedirs(d)
    json.dump({"project": "wsa/alpha\n"}, open(os.path.join(d, "r.json"), "w"))
    assert audit.scan_project_refs([d]) == [(os.path.join(d, "r.json"), "project", "wsa/alpha\n")]


# ------------------------------------------------------------------ F-R2-5: ASCII digits only in topic ids
@pytest.mark.parametrize("bad", ["TOP-٠٠١", "TOP-００１", "TOP-00١", "TOP-০০১"])
def test_f_r2_5_a_non_ascii_digit_topic_id_is_malformed_everywhere(w, bad):
    assert TOPIC_ID_RE.fullmatch(bad) is None
    assert entities.topic_shape_errors({"id": bad, "project": "wsa/alpha", "state": "active"}, ws_dir="wsa", workspaces={"wsa": ["alpha"]})
    wr = w.writer()
    for call in (lambda: wr.delete_topic(bad, acting="workspace:wsa"), lambda: wr.refile_topic(bad, "wsa/beta", by="r"),
                 lambda: wr.topics.find(bad), lambda: wr.topics.path("wsa", bad)):
        with pytest.raises(ScopeRefusal) as e:
            call()
        assert e.value.code == "topic-id-malformed", "must be refused as malformed, not looked up as an unknown topic"


def test_f_r2_5_a_topic_file_named_with_non_ascii_digits_is_not_listed(w):
    wr = w.writer()
    real = wr.create_topic("real", project="wsa/alpha", by="r")
    open(os.path.join(w.data, "workspaces", "wsa", "topics", "TOP-٠٠١.json"), "w").write(json.dumps({"id": "TOP-٠٠١", "project": "wsa/alpha", "state": "active"}))
    assert [r["id"] for _w, _p, r in wr.topics.iter_all()] == [real["id"]]


# ------------------------------------------------------------------ F-R2-5: a symlinked topics dir is REFUSED (not followed): same code as a symlinked topic file
def _symlink_topics_dir(w, ws="wsa"):
    outside = os.path.join(w.root, "outside-topics")
    os.makedirs(outside)
    victim = os.path.join(outside, "TOP-888.json")
    json.dump({"id": "TOP-888", "project": f"{ws}/alpha", "state": "active"}, open(victim, "w"))
    d = os.path.join(w.data, "workspaces", ws, "topics")
    shutil.rmtree(d)
    os.symlink(outside, d)
    return victim


def test_f_r2_5_delete_topic_through_a_symlinked_topics_dir_is_refused_and_the_target_survives(w):
    victim = _symlink_topics_dir(w)
    before = open(victim).read()
    with pytest.raises(ScopeRefusal) as e:
        w.writer().delete_topic("TOP-888", acting="workspace:wsa")
    assert e.value.code == "topic-path-escape"
    assert open(victim).read() == before


def test_f_r2_5_create_refile_and_listing_through_a_symlinked_topics_dir_are_refused_too(w):
    victim = _symlink_topics_dir(w)
    wr = w.writer()
    with pytest.raises(ScopeRefusal) as e:
        wr.create_topic("t", project="wsa/alpha", by="r")
    assert e.value.code == "topic-path-escape"
    assert os.listdir(os.path.dirname(victim)) == ["TOP-888.json"], "nothing was written through the link"
    with pytest.raises(ScopeRefusal) as e:
        list(wr.topics.iter_all())
    assert e.value.code == "topic-path-escape", "a listing must fail loudly, not skip a dir that may hold topics"


def test_f_r2_5_a_symlinked_workspace_dir_under_the_data_root_is_refused(w, tmp_path):
    real = tmp_path / "moved-wsa"
    shutil.move(os.path.join(w.data, "workspaces", "wsa"), real)
    os.symlink(real, os.path.join(w.data, "workspaces", "wsa"))
    with pytest.raises(ScopeRefusal) as e:
        w.writer().topics.path("wsa", "TOP-001")
    assert e.value.code == "topic-path-escape"


def test_f_r2_5_positive_control_a_data_root_that_is_itself_behind_a_symlink_still_works(w, tmp_path):
    link = tmp_path / "data-link"
    os.symlink(w.data, link)
    ts = TopicStore(str(link))
    assert ts.path("wsa", "TOP-001").endswith(os.path.join("wsa", "topics", "TOP-001.json"))
    t = w.writer().create_topic("t", project="wsa/alpha", by="r")
    assert [r["id"] for _w, _p, r in ts.iter_all()] == [t["id"]]


# ------------------------------------------------------------------ F-R2-6: the I6 closure check sees dynamic imports
def _pkg(tmp_path, files):
    d = tmp_path / ("p" + str(len(list(tmp_path.iterdir()))))
    d.mkdir()
    for n, src in files.items():
        (d / (n + ".py")).write_text(src)
    return str(d)


@pytest.mark.parametrize("src, expected", [
    ("import importlib\ndef f():\n    importlib.import_module('gnomon.workspace.writers')\n", {"writers"}),
    ("from importlib import import_module\ndef f():\n    import_module('gnomon.workspace.model')\n", {"model"}),
    ("def f():\n    __import__('gnomon.workspace.store')\n", {"store"}),
    ("import importlib\ndef f():\n    importlib.import_module('.store', package='gnomon.workspace')\n", {"store"}),
    ("import importlib\ndef f(n):\n    importlib.import_module(n)\n", {"<dynamic-import>"}),
    ("import importlib\ndef f(n):\n    importlib.import_module('gnomon.workspace.' + n)\n", {"<dynamic-import>"}),
    ("import importlib.util\ndef f(p):\n    importlib.util.spec_from_file_location('m', p)\n", {"<dynamic-import>"}),
    ("def f(n):\n    return __import__(n)\n", {"<dynamic-import>"}),
])
def test_f_r2_6_a_dynamic_import_in_a_module_is_in_its_closure_and_is_forbidden(tmp_path, src, expected):
    d = _pkg(tmp_path, {"a": src, "writers": "", "model": "", "store": ""})
    assert S.forbidden_reach("a", d) == expected


def test_f_r2_6_a_dynamic_import_reached_transitively_is_found_and_a_non_workspace_constant_is_not_flagged(tmp_path):
    d = _pkg(tmp_path, {"a": "from . import b\n", "b": "import importlib\ndef f():\n    importlib.import_module('gnomon.workspace.writers')\n", "writers": ""})
    assert S.forbidden_reach("a", d) == {"b", "writers"}
    ok = _pkg(tmp_path, {"a": "import importlib\ndef f():\n    importlib.import_module('json')\n    importlib.import_module('gnomon.config')\n"})
    assert S.forbidden_reach("a", ok) == set()


def test_f_r2_6_the_real_package_copy_with_a_planted_lazy_importlib_in_events_fails_the_real_check(tmp_path):
    """Diamond mutant G8: ``events.py`` doing ``importlib.import_module('...workspace.model')`` survived the old static closure."""
    d = tmp_path / "pkgcopy"
    shutil.copytree(PKG, d, ignore=shutil.ignore_patterns("__pycache__", "*.bak*"))
    ev_src = (d / "events.py").read_text()
    (d / "events.py").write_text(ev_src + "\n\ndef _planted():\n    import importlib\n    return importlib.import_module('gnomon.workspace.model')\n")
    assert S.forbidden_reach("events", str(d)) == {"model"}
    assert S.forbidden_reach("events") == set(), "the real events module is clean"


def test_f_r2_6_every_real_safety_module_has_no_dynamic_import():
    for mod in S.SAFETY_MODULES:
        assert "<dynamic-import>" not in S.import_closure(mod), mod


def test_f_r2_6_the_sentinel_name_the_tests_use_is_the_one_the_module_exports():
    assert S.DYNAMIC_IMPORT == "<dynamic-import>"

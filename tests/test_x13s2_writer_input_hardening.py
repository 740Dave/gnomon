"""X13 S2 Diamond F4 and F5: the typed writers validate property VALUES, TITLES, ids and keys (not only keys), so a value can
never forge a scope line or a node; and topic ids are validated so a delete or re-file can never leave the topics dir.

F4 reproductions: a value holding a newline that injects ``- project: wsb/delta`` (and a stored ``workspace``), and a title
holding a newline that injects a whole second node. F5 reproduction: ``delete_topic("../../../topic-counter")``.
Each is shown REFUSED with the file bytes unchanged; benign look-alike text that cannot change the parse is still accepted."""

import os

import pytest

from x13s2_world import build_world, tree_digest
from gnomon.workspace.ltsread import read_file
from gnomon.workspace.scope import Subject
from gnomon.workspace.scopebase import ScopeRefusal
from gnomon.workspace.store import LtsFiles, TopicStore


@pytest.fixture
def w(tmp_path):
    return build_world(tmp_path)


FORGED_VALUE = "ready\n- project: wsb/delta\n- workspace: wsb"
FORGED_TITLE = "innocent\n\n## Forged [RM-79]\n\n- project: wsb/delta"
LINE_BREAKERS = ["a\nb", "a\rb", "a\r\nb", "a b", "a b", "a\x85b", "a\x0bb", "a\x0cb", "a\x00b", "a\x1cb"]


# ------------------------------------------------------------------ F4: the two forgeries, reproduced and refused
def test_f4_a_forged_project_line_in_a_property_value_is_refused_and_nothing_is_written(w):
    wr = w.writer()
    before = tree_digest(w.root)
    with pytest.raises(ScopeRefusal) as e:
        wr.create_item("wsa/roadmap", "RM-78", "Item", props={"status": FORGED_VALUE}, project="wsa/alpha", by="r")
    assert e.value.code == "unsafe-text"
    assert tree_digest(w.root) == before
    assert "RM-78" not in open(w.roadmap_file()).read()


def test_f4_a_forged_second_node_in_a_title_is_refused_and_nothing_is_written(w):
    wr = w.writer()
    before = tree_digest(w.root)
    with pytest.raises(ScopeRefusal) as e:
        wr.create_item("wsa/roadmap", "RM-78", FORGED_TITLE, project="wsa/alpha", by="r")
    assert e.value.code == "unsafe-text"
    assert tree_digest(w.root) == before
    assert "RM-79" not in {n.id for n in read_file(w.roadmap_file())}


@pytest.mark.parametrize("bad", LINE_BREAKERS)
def test_f4_every_line_breaking_character_is_refused_in_every_writer_text_field(w, bad):
    wr = w.writer()
    before = tree_digest(w.root)
    calls = {
        "value": lambda: wr.create_item("wsa/roadmap", "RM-78", "t", props={"status": bad}, project="wsa/alpha", by="r"),
        "title": lambda: wr.create_item("wsa/roadmap", "RM-78", bad, project="wsa/alpha", by="r"),
        "node id": lambda: wr.create_item("wsa/roadmap", "RM-78" + bad, "t", project="wsa/alpha", by="r"),
        "prop key": lambda: wr.create_item("wsa/roadmap", "RM-78", "t", props={"status" + bad: "x"}, project="wsa/alpha", by="r"),
        "edit value": lambda: wr.edit_item_property("wsa/roadmap", "RM-1", "status", bad),
        "edit key": lambda: wr.edit_item_property("wsa/roadmap", "RM-1", "status" + bad, "x"),
        "refile by": lambda: wr.refile_item("wsa/roadmap", "RM-1", "wsa/beta", by=bad),
        "built/live session": lambda: wr.record_built_live("wsa/roadmap", "RM-1", "built", bad),
        "decision title": lambda: wr.stamp_decision(w.answers_file(), "DEC-X", bad, project="wsa/alpha"),
        "decision id": lambda: wr.stamp_decision(w.answers_file(), "DEC-X" + bad, "t", project="wsa/alpha"),
        "decision answered-by": lambda: wr.stamp_decision(w.answers_file(), "DEC-X", "t", project="wsa/alpha", answered_by=bad),
        "decision subject id": lambda: wr.stamp_decision(w.answers_file(), "DEC-X", "t", subjects=[Subject("RM-1" + bad, ("wsa/alpha",))]),
    }
    for what, call in calls.items():
        with pytest.raises(ScopeRefusal) as e:
            call()
        assert e.value.code in ("unsafe-text", "item-id-prefix"), (what, e.value.code)
        assert tree_digest(w.root) == before, f"{what}: a refused write changed bytes"


def test_f4_text_that_would_parse_as_a_property_or_heading_line_is_refused_even_on_one_line(w):
    wr = w.writer()
    before = tree_digest(w.root)
    for v in ("- project: wsb/delta", "## Forged [RM-79]", "  - workspace: wsb", "# x"):
        with pytest.raises(ScopeRefusal):
            wr.edit_item_property("wsa/roadmap", "RM-1", "status", v)
    for t in ("[RM-1] sneaky", "a [RM-1] b", "[DOC:RM-1] x"):
        with pytest.raises(ScopeRefusal):
            wr.create_item("wsa/roadmap", "RM-78", t, project="wsa/alpha", by="r")
    for k in ("Bad Key", "1st", "a:b", "", "a b"):
        with pytest.raises(ScopeRefusal):
            wr.edit_item_property("wsa/roadmap", "RM-1", k, "x")
    for i in ("rm-78", "RM 78", "RM-78]", "[RM-78"):
        with pytest.raises(ScopeRefusal):
            wr.create_item("wsa/roadmap", i, "t", project="wsa/alpha", by="r")
    assert tree_digest(w.root) == before


def test_f4_ordinary_text_is_still_accepted_and_parses_back_exactly(w):
    wr = w.writer()
    wr.create_item("wsa/roadmap", "RM-60", "A title: with colons, dashes - and (parens) é", props={"status": "in-flight: S1 built - ok", "note": "a: b - c"},
                   project="wsa/alpha", by="r")
    n = {x.id: x for x in read_file(w.roadmap_file())}["RM-60"]
    assert n.title.startswith("A title: with colons") and n.props["project"] == ["wsa/alpha"]
    assert n.props["status"] == ["in-flight: S1 built - ok"] and n.props["note"] == ["a: b - c"] and "workspace" not in n.props


def test_f4_the_lts_editor_itself_refuses_unsafe_text_defence_in_depth():
    text = "# R [RM-ROOT]\n\n- a: b\n"
    for call in (lambda: LtsFiles.add_property(text, "RM-ROOT", "k", "v\n- project: x/y"), lambda: LtsFiles.set_property(text, "RM-ROOT", "a", "v\r- project: x/y"),
                 lambda: LtsFiles.append_node(text, "## t\n\n## Forged [X-1]", [("k", "v")]), lambda: LtsFiles.append_node(text, "## t [X-1]", [("k\n- project", "v")]),
                 lambda: LtsFiles.append_node(text, "## t [X-1]", [("k", "v\n## Forged [X-2]")])):
        with pytest.raises(ScopeRefusal):
            call()


def test_f4_a_forged_scope_never_reaches_the_reader_so_the_audit_sees_only_the_writers_stamp(w):
    """End state: after every refused forgery the item the reader sees has exactly the stamped project."""
    wr = w.writer()
    for kw in ({"props": {"status": FORGED_VALUE}}, {"title": FORGED_TITLE}):
        with pytest.raises(ScopeRefusal):
            wr.create_item("wsa/roadmap", "RM-90", kw.get("title", "t"), props=kw.get("props"), project="wsa/alpha", by="r")
    wr.create_item("wsa/roadmap", "RM-90", "clean", project="wsa/alpha", by="r")
    n = {x.id: x for x in read_file(w.roadmap_file())}["RM-90"]
    assert n.props["project"] == ["wsa/alpha"] and "workspace" not in n.props


# ------------------------------------------------------------------ F5: topic ids
def test_f5_delete_topic_with_a_traversal_id_is_refused_and_the_target_survives(w):
    wr = w.writer()
    t = wr.create_topic("real", project="wsa/alpha", by="r")
    counter = os.path.join(w.data, "topic-counter.json")
    assert os.path.isfile(counter)
    before = tree_digest(w.root)
    for bad in ("../../../topic-counter", "../../topic-counter", "TOP-001/../../../topic-counter", "/etc/hosts", os.path.join(w.data, "topic-counter"),
                "top-001", "TOP-1", "TOP-001\n", "TOP-001 ", "TOP-001.json", "TOP-00a", "", "TOP-001\x00"):
        with pytest.raises(ScopeRefusal) as e:
            wr.delete_topic(bad, acting="workspace:wsa")
        assert e.value.code == "topic-id-malformed", bad
    assert os.path.isfile(counter) and os.path.isfile(t["path"])
    assert tree_digest(w.root) == before


def test_f5_refile_and_the_store_reject_a_malformed_id_too(w):
    wr = w.writer()
    for bad in ("../x", "TOP-001/../../x"):
        with pytest.raises(ScopeRefusal) as e:
            wr.refile_topic(bad, "wsa/beta", by="r")
        assert e.value.code == "topic-id-malformed"
        with pytest.raises(ScopeRefusal):
            wr.topics.path("wsa", bad)
        with pytest.raises(ScopeRefusal):
            wr.topics.find(bad)
    with pytest.raises(ScopeRefusal):
        TopicStore(w.data).path("../wsa", "TOP-001")


def test_f5_a_valid_id_still_deletes_only_inside_the_owners_topics_dir(w):
    wr = w.writer()
    t = wr.create_topic("x", project="wsa/alpha", by="r")
    other = wr.create_topic("y", project="wsb/delta", by="r")
    with pytest.raises(ScopeRefusal) as e:
        wr.delete_topic(t["id"], acting="workspace:wsb")
    assert e.value.code == "not-the-owner"
    wr.delete_topic(t["id"], acting="workspace:wsa")
    assert not os.path.exists(t["path"]) and os.path.exists(other["path"])


def test_f5_a_topic_entry_that_is_a_symlink_out_of_the_dir_is_not_followed(w):
    wr = w.writer()
    victim = os.path.join(w.data, "victim.json")
    open(victim, "w").write("{}")
    link = os.path.join(w.data, "workspaces", "wsa", "topics", "TOP-777.json")
    os.symlink(victim, link)
    with pytest.raises(ScopeRefusal):
        wr.delete_topic("TOP-777", acting="workspace:wsa")
    assert os.path.exists(victim)

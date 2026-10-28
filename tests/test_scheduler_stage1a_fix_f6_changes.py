"""Fix round 2, F-6: a CHANGE proposal's Pending row shows what changed, field by field (old -> new)."""

from __future__ import annotations

from scheduler_support import confirm, make, propose_inject


def by_field(rows):
    return {r["field"]: r for r in rows}


def test_a_change_proposal_lists_each_changed_field_old_to_new(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, trigger={"times": ["06:10"]}, text="old words", title="Old title"); confirm(sch)
    propose_inject(sch, trigger={"every_minutes": 45}, text="new words", title="New title")
    row = sch.pending()[0]
    assert row["kind"] == "change"
    chg = by_field(row["changes"])
    assert chg["title"]["old"] == "Old title" and chg["title"]["new"] == "New title"
    assert chg["schedule"]["old"] == "Every day at 06:10 (local time)" and chg["schedule"]["new"] == "Every 45 minutes"
    assert chg["text"]["old"] == "old words" and chg["text"]["new"] == "new words"
    assert "target" not in chg and "owner" not in chg                 # unchanged fields are not listed
    assert sch.detail("remind-me")["proposal"]["changes"] == row["changes"]


def test_an_unchanged_field_is_not_listed_and_a_new_job_has_no_changes(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30}, text="same"); confirm(sch)
    propose_inject(sch, trigger={"every_minutes": 30}, text="same but longer")
    assert [r["field"] for r in sch.pending()[0]["changes"]] == ["text"]
    propose_inject(sch, job_id="brand-new", trigger={"every_minutes": 30})
    new = {r["id"]: r for r in sch.pending()}["brand-new"]
    assert new["kind"] == "new" and new["changes"] == []


def test_a_removal_proposal_says_what_would_be_removed(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30}, text="going away"); confirm(sch)
    sch.propose_remove("remind-me", proposer="agent-a", verified=True)
    row = sch.pending()[0]
    assert row["kind"] == "removal" and row["changes"] == [] and row["text"] == "going away" and row["schedule"]
    assert row["removes"] is True


def test_the_tab_renders_the_changes():
    import json, os, shutil, subprocess
    node = shutil.which("node")
    if node is None:
        import pytest; pytest.skip("node not installed")
    js = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "scheduler_ui", "scheduler_tab.js")
    prog = ("const T=require(process.argv[1]);const v=T.buildDetail({ok:true,id:'p',proposal:{title:'P',kind:'change',proposed_by:'a',"
            "proposed_by_verified:true,schedule:'s',target:'a',text:'t',digest:'d',changes:[{field:'text',old:'a',new:'b'}]}});"
            "console.log(JSON.stringify(v.changes))")
    out = json.loads(subprocess.run([node, "-e", prog, js], capture_output=True, text=True, timeout=60).stdout)
    assert out == [{"field": "text", "old": "a", "new": "b", "line": "text: a -> b"}]

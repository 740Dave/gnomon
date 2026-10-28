"""B1 core (pre-review batch, spec section 3): ``gnomon.dsd.dsd_ops``, the one module behind the DSD self-verbs.

Hermetic: a tmp store root, a fixed clock where a collision is the point. Real instances behind the refusals:
a table headed `| Option | What happens |` parsed to ZERO options; one DSD silently overwrote another's id (same millisecond);
a DSD filed with a wrong count of decisions; a recommendation line that matches no option.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from gnomon.dsd import dsd_ops as OPS
from gnomon.dsd.inbox import DsdInbox, STATUS_PENDING, status_lock
from gnomon.dsd.persist import LtsFileDecisionWriter, mint_dsd_nodes
from gnomon.dsd.model import DecisionAnswer, parse_dsd
from gnomon.hub.mcp import DSDStore

GOOD = """\
### D1 · Ship it?
**What this is:** the release.
**Why it matters:** the demo.
| Option | Tradeoffs |
|---|---|
| **Yes** | fast |
| No | slow |
**My recommendation:** Yes
**Your call:** ____
"""
TWO = GOOD + """\
### D2 · Announce it?
**What this is:** the blog post.
**Why it matters:** the launch.
| Option | Tradeoffs |
|---|---|
| **Now** | loud |
| Later | quiet |
**My recommendation:** Now
**Your call:** ____
"""
WHAT_HAPPENS = GOOD.replace("| Option | Tradeoffs |", "| Option | What happens |")     # the real instance: parses to zero options
NO_SLOT = GOOD.replace("**Your call:** ____\n", "")
BAD_RECO = GOOD.replace("**My recommendation:** Yes", "**My recommendation:** Maybe")
ME = "alpha-agent"


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "agents"
    r.mkdir()
    return str(r)


def stored(root, dsd_id, who=ME):
    with open(os.path.join(root, who, "dsd", dsd_id + ".json")) as fh:
        return json.load(fh)


# --------------------------------------------------------------------------- registry + send

def test_core_functions_are_registered_by_name_and_take_the_instance_first():
    assert set(OPS.CORE_FUNCTIONS) == {"send", "list_mine", "withdraw", "replace"}
    import inspect
    for name in OPS.CORE_FUNCTIONS:
        assert next(iter(inspect.signature(OPS.get(name)).parameters)) == "instance_id"
    with pytest.raises(KeyError):
        OPS.get("nope")


def test_send_files_the_record_and_returns_id_path_and_count(root):
    out = OPS.send(ME, TWO, root=root)
    assert out["ok"] is True and out["decisions"] == 2 and out["dsd_id"].startswith("dsd-")
    assert out["record_path"] == os.path.join(root, ME, "dsd", out["dsd_id"] + ".json")
    rec = stored(root, out["dsd_id"])
    assert rec["status"] == STATUS_PENDING == "pending-human-review" and rec["instance_id"] == ME
    assert rec["dsd"]["markdown"] == TWO


@pytest.mark.parametrize("md,needle", [
    (WHAT_HAPPENS, "D1"), (NO_SLOT, "no `Your call:` answer slot"), (BAD_RECO, "matches no option"),
    ("just some prose, no decisions", "zero decisions"), ("", "empty")])
def test_send_refuses_and_names_the_decision(root, md, needle):
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, md, root=root)
    assert needle in str(e.value) and "nothing was filed" in str(e.value)
    assert not os.path.exists(os.path.join(root, ME, "dsd")) or not os.listdir(os.path.join(root, ME, "dsd"))


def test_zero_options_refusal_names_the_decision_and_the_accepted_header(root):
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, WHAT_HAPPENS, root=root)
    assert "D1 · Ship it?" in str(e.value) and "| Option | Tradeoffs |" in str(e.value)


def test_over_200_kb_is_refused(root):
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, GOOD + "x" * (200 * 1024), root=root)
    assert "200 KB" in str(e.value)


def test_a_refusal_names_every_bad_decision_not_just_the_first(root):
    md = WHAT_HAPPENS + NO_SLOT.replace("D1", "D2")
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, md, root=root)
    assert "D1" in str(e.value) and "D2" in str(e.value)


# --------------------------------------------------------------------------- no new power

@pytest.mark.parametrize("bad", ["../escape", "a/b", ".hidden", "", "x\0y", "..", "a\\b"])
def test_a_crafted_instance_id_is_refused_and_nothing_is_written(root, tmp_path, bad):
    with pytest.raises(OPS.DsdRefused):
        OPS.send(bad, GOOD, root=root)
    assert not [p for p in os.listdir(str(tmp_path)) if p not in ("agents",)]
    assert os.listdir(root) == []


def test_a_symlinked_store_folder_pointing_outside_is_refused(root, tmp_path):
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    os.symlink(str(outside), os.path.join(root, ME))
    with pytest.raises(OPS.DsdRefused):
        OPS.send(ME, GOOD, root=root)
    assert os.listdir(str(outside)) == []
    with pytest.raises(OPS.DsdRefused):
        OPS.list_mine(ME, root=root)


def test_a_path_inside_the_markdown_cannot_make_the_write_go_elsewhere(root, tmp_path):
    md = GOOD.replace("**What this is:**", "**Documents:** ../../elsewhere/secret.md\n**What this is:**")
    out = OPS.send(ME, md, root=root)
    assert os.path.dirname(out["record_path"]) == os.path.join(root, ME, "dsd")
    assert sorted(os.listdir(str(tmp_path))) == ["agents"]


def test_no_core_function_has_a_way_to_name_another_instance(root):
    OPS.send("beta-agent", GOOD, root=root)
    mine = OPS.list_mine(ME, root=root)
    assert mine["count"] == 0                                         # beta's DSD is invisible to alpha
    beta_id = OPS.list_mine("beta-agent", root=root)["dsds"][0]["dsd_id"]
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, beta_id, "not mine", root=root, presence=lambda *a: False)
    with pytest.raises(OPS.DsdRefused):
        OPS.replace(ME, beta_id, GOOD, root=root, presence=lambda *a: False)
    assert stored(root, beta_id, "beta-agent")["status"] == "pending-human-review"


# --------------------------------------------------------------------------- list_mine

def test_list_mine_maps_statuses_in_the_reply_and_never_renames_the_stored_value(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    rows = OPS.list_mine(ME, root=root)["dsds"]
    assert [r["status"] for r in rows] == ["pending"] and rows[0]["dsd_id"] == a and rows[0]["title"]
    assert stored(root, a)["status"] == "pending-human-review"                 # C6: the stored value is untouched
    assert OPS.list_mine(ME, "answered", root=root)["count"] == 0
    assert OPS.list_mine(ME, "pending", root=root)["count"] == 1
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.list_mine(ME, "pending-human-review", root=root)
    assert "pending, answered, withdrawn, replaced" in str(e.value)


def test_list_mine_shows_per_decision_answers_for_an_answered_dsd(root, tmp_path):
    out = OPS.send(ME, TWO, root=root)
    dsd_id = out["dsd_id"]
    doc = parse_dsd(TWO)
    answers = {"D1": DecisionAnswer(labels=["Yes"], comment="go"), "D2": DecisionAnswer(labels=["Later"], comment="after")}
    corpus = str(tmp_path / "dsd-answers" / "dsd-answers.lts")
    nodes = mint_dsd_nodes(dsd_id, ME, doc.decisions, answers, decided_on="2026-10-05")
    LtsFileDecisionWriter(corpus).mint(nodes)
    DsdInbox(root).mark_answered(dsd_id, "DEC-DSD-" + dsd_id[4:].upper())
    row = OPS.list_mine(ME, root=root, answers_path=corpus)["dsds"][0]
    assert row["status"] == "answered" and row["answered"] and row["answer_node"].startswith("DEC-DSD-")
    assert [(a["decision"], a["chosen"], a["comment"], a["agreed_with_recommendation"]) for a in row["answers"]] == [
        ("D1", "Yes", "go", "true"), ("D2", "Later", "after", "false")]


# --------------------------------------------------------------------------- withdraw / replace

NEVER = lambda *a: False          # noqa: E731 - the operator does not have it open


def test_withdraw_keeps_the_record_marks_it_and_lists_it_as_withdrawn(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    out = OPS.withdraw(ME, a, "I filed it with the wrong count", root=root, presence=NEVER, now=1000.0)
    assert out == {"ok": True, "dsd_id": a, "status": "withdrawn", "reason": "I filed it with the wrong count"}
    rec = stored(root, a)
    assert rec["status"] == "withdrawn" and rec["withdrawn"] == {"reason": "I filed it with the wrong count",
                                                                  "at_epoch": 1000.0, "by": ME}
    assert rec["dsd"]["markdown"] == GOOD                                       # kept, not deleted
    row = OPS.list_mine(ME, "withdrawn", root=root)["dsds"][0]
    assert row["status"] == "withdrawn" and row["reason"] == "I filed it with the wrong count"
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, a, "again", root=root, presence=NEVER)               # already withdrawn
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, "dsd-1a2b3c4d5e6", "x", root=root, presence=NEVER)   # not one of mine
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, a, "  ", root=root, presence=NEVER)                  # a reason is required


def test_an_answered_dsd_can_never_be_withdrawn_or_replaced(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    DsdInbox(root).mark_answered(a, "DEC-DSD-" + a[4:].upper())
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.withdraw(ME, a, "too late", root=root, presence=NEVER)
    assert "already answered" in str(e.value)
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.replace(ME, a, TWO, root=root, presence=NEVER)
    assert "already answered" in str(e.value)
    assert stored(root, a)["status"] == "answered"
    assert len(os.listdir(os.path.join(root, ME, "dsd"))) == 1               # replace filed nothing


def test_replace_files_the_new_one_naming_the_old_and_keeps_the_old_marked(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    out = OPS.replace(ME, a, TWO, root=root, presence=NEVER, now=2000.0)
    new = out["dsd_id"]
    assert out["ok"] and out["replaced"] == a and new != a and out["decisions"] == 2
    assert stored(root, new)["replaces"] == a and stored(root, new)["status"] == "pending-human-review"
    old = stored(root, a)
    assert old["status"] == "replaced" and old["replaced"]["by_dsd_id"] == new and old["dsd"]["markdown"] == GOOD
    rows = {r["dsd_id"]: r for r in OPS.list_mine(ME, root=root)["dsds"]}
    assert rows[a]["status"] == "replaced" and rows[a]["replaced_by"] == new and rows[new]["replaces"] == a


def test_replace_runs_the_submit_checks_and_a_refusal_leaves_the_old_one_alone(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    with pytest.raises(OPS.DsdRefused):
        OPS.replace(ME, a, WHAT_HAPPENS, root=root, presence=NEVER)
    assert stored(root, a)["status"] == "pending-human-review"
    assert len(os.listdir(os.path.join(root, ME, "dsd"))) == 1


def test_an_open_dsd_needs_a_second_call_with_confirm_inside_ten_minutes(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    OPEN = lambda *a: True        # noqa: E731
    OPS._ARMED.clear()
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.withdraw(ME, a, "x", root=root, presence=OPEN, now=100.0)
    assert "has" in str(e.value) and "open right now" in str(e.value) and "confirm=true" in str(e.value)
    assert stored(root, a)["status"] == "pending-human-review"
    with pytest.raises(OPS.DsdRefused):                               # confirm without a first refusal in the window: no
        OPS.withdraw(ME, a, "x", root=root, presence=OPEN, confirm=True, now=100.0 + 601.0)
    OPS._ARMED.clear()
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, a, "x", root=root, presence=OPEN, confirm=True, now=100.0)       # never armed
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, a, "x", root=root, presence=OPEN, now=200.0)                      # arms
    out = OPS.withdraw(ME, a, "x", root=root, presence=OPEN, confirm=True, now=200.0 + 599.0)
    assert out["status"] == "withdrawn"


def test_the_presence_signal_is_the_dashboards_review_beacon(root, tmp_path, monkeypatch):
    from gnomon.harness import operator_activity as oa
    d = str(tmp_path / "act")
    os.makedirs(d, mode=0o700)
    monkeypatch.setattr(oa, "activity_dir", lambda: d)
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    import time
    assert OPS._operator_has_open(a, time.time()) is False
    oa.write_dsd_review_beacon(a, ME, now=time.time(), directory=d)
    assert OPS._operator_has_open(a, time.time()) is True
    assert OPS._operator_has_open(a, time.time() + 3600) is False         # a stale beacon is not "open now"


# --------------------------------------------------------------------------- C7: an answer wins a race

@pytest.mark.parametrize("round_", range(30))
def test_an_answer_wins_a_race_with_withdraw(root, round_):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    start = threading.Barrier(2)
    res = {}

    def answer():
        start.wait()
        DsdInbox(root).mark_answered(a, "DEC-DSD-" + a[4:].upper(), answered_epoch=5.0)
        res["answered"] = True

    def wd():
        start.wait()
        try:
            res["withdraw"] = OPS.withdraw(ME, a, "racing", root=root, presence=NEVER)["status"]
        except OPS.DsdRefused as e:
            res["withdraw"] = "refused: " + str(e)
    ts = [threading.Thread(target=answer), threading.Thread(target=wd)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    rec = stored(root, a)
    assert res["answered"] and rec["status"] == "answered" and rec["root_node_id"]      # the answer is never lost
    assert res["withdraw"] == "withdrawn" or "already answered" in res["withdraw"]       # and withdraw never reports a false win


def test_an_answer_that_arrives_after_a_withdraw_still_wins(root):
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    OPS.withdraw(ME, a, "oops", root=root, presence=NEVER)
    DsdInbox(root).mark_answered(a, "DEC-DSD-" + a[4:].upper())
    assert stored(root, a)["status"] == "answered"


def test_the_status_change_is_made_under_the_one_shared_lock(root, monkeypatch):
    seen = []
    import contextlib
    real = OPS.status_lock

    @contextlib.contextmanager
    def spy(r):
        seen.append("in")
        with real(r):
            yield
        seen.append("out")
    monkeypatch.setattr(OPS, "status_lock", spy)
    a = OPS.send(ME, GOOD, root=root)["dsd_id"]
    OPS.withdraw(ME, a, "x", root=root, presence=NEVER)
    assert seen == ["in", "out"]
    from gnomon.dsd import inbox
    assert OPS.status_lock is inbox.status_lock or real is inbox.status_lock      # the SAME lock the answer flip takes


# --------------------------------------------------------------------------- O-F3: the folder must be the instance's OWN folder

def _filed(root, who, dsd_id, **over):
    d = os.path.join(root, who, "dsd")
    os.makedirs(d, exist_ok=True)
    rec = {"dsd_id": dsd_id, "created_epoch": 1.0, "status": "pending-human-review", "dsd": {"markdown": GOOD}}
    if who is not None:
        rec["instance_id"] = who
    rec.update(over)
    rec = {k: v for k, v in rec.items() if v is not None}
    with open(os.path.join(d, dsd_id + ".json"), "w") as fh:
        json.dump(rec, fh)
    return os.path.join(d, dsd_id + ".json")


def test_a_store_folder_linked_to_another_instances_folder_is_refused_by_send_list_and_withdraw(root):
    frank = _filed(root, "frank-agent", "dsd-1a0e00000a")
    os.symlink(os.path.join(root, "frank-agent"), os.path.join(root, ME))           # inside the store, but someone else's folder
    before = open(frank).read()
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, GOOD, root=root)
    assert "link" in str(e.value)
    with pytest.raises(OPS.DsdRefused):
        OPS.list_mine(ME, root=root)
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, "dsd-1a0e00000a", "x", root=root, presence=NEVER)
    assert open(frank).read() == before and os.listdir(os.path.join(root, "frank-agent", "dsd")) == ["dsd-1a0e00000a.json"]


def test_only_the_dsd_subfolder_linked_to_another_instances_dsd_folder_is_refused_too(root):
    _filed(root, "frank-agent", "dsd-1a0e00000a")
    os.makedirs(os.path.join(root, ME))
    os.symlink(os.path.join(root, "frank-agent", "dsd"), os.path.join(root, ME, "dsd"))
    with pytest.raises(OPS.DsdRefused):
        OPS.send(ME, GOOD, root=root)
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, "dsd-1a0e00000a", "x", root=root, presence=NEVER)


def test_a_record_with_no_instance_id_is_not_the_callers_not_listed_and_not_marked(root):
    p = _filed(root, ME, "dsd-1a0e00000a", instance_id=None)
    assert "instance_id" not in json.load(open(p))
    assert OPS.list_mine(ME, root=root)["count"] == 0
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, "dsd-1a0e00000a", "x", root=root, presence=NEVER)
    assert json.load(open(p))["status"] == "pending-human-review"


def test_a_record_naming_a_different_instance_inside_my_folder_is_not_mine(root):
    p = _filed(root, ME, "dsd-1a0e00000a", instance_id="frank-agent")
    assert OPS.list_mine(ME, root=root)["count"] == 0
    with pytest.raises(OPS.DsdRefused):
        OPS.withdraw(ME, "dsd-1a0e00000a", "x", root=root, presence=NEVER)
    assert json.load(open(p))["status"] == "pending-human-review"


# --------------------------------------------------------------------------- O-F4: an explicit "no recommendation" is accepted

@pytest.mark.parametrize("line", ["None: this is your judgment", "None", "No recommendation: your call", "n/a", "No preference."])
def test_a_recommendation_that_says_there_is_none_is_accepted(root, line):
    md = GOOD.replace("**My recommendation:** Yes", f"**My recommendation:** {line}")
    out = OPS.send(ME, md, root=root)
    assert out["ok"]
    dec = parse_dsd(md).decisions[0]
    assert dec.recommended_id is None or dec.recommendation_source != "reco-line"


def test_a_recommendation_naming_a_non_option_is_still_refused(root):
    for line in ("Maybe", "Nonexistent option", "Noodles"):
        with pytest.raises(OPS.DsdRefused) as e:
            OPS.send(ME, GOOD.replace("**My recommendation:** Yes", f"**My recommendation:** {line}"), root=root)
        assert "matches no option" in str(e.value)


# --------------------------------------------------------------------------- D3: the none rule is narrow

def _with_options(options, reco):
    rows = "\n".join(f"| {o} | t |" for o in options)
    return ("### D1 · Which?\n**What this is:** a.\n**Why it matters:** b.\n| Option | Tradeoffs |\n|---|---|\n" + rows +
            f"\n**My recommendation:** {reco}\n**Your call:** ____\n")


@pytest.mark.parametrize("options,reco", [
    (["None (keep as is)", "Change it"], "None"),                  # which one is meant? ambiguous
    (["None", "Change it"], "None: keep"),                         # an option IS called None; the line is not exactly that label
    (["Yes", "No"], "None of the above; do Maybe"),                # not a none-line: a recommendation that matches no option
    (["Yes", "No"], "Nothing fancy: just do No"),
    (["Yes", "No"], "None, really"),                               # the word is not alone and not followed by a colon
])
def test_the_none_rule_does_not_swallow_an_ambiguous_or_real_recommendation(root, options, reco):
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, _with_options(options, reco), root=root)
    assert "D1 · Which?" in str(e.value) and ("ambiguous" in str(e.value) or "matches no option" in str(e.value)
                                                 or "not an accepted form" in str(e.value))


def test_the_ambiguous_case_names_itself_as_ambiguous(root):
    with pytest.raises(OPS.DsdRefused) as e:
        OPS.send(ME, _with_options(["None (keep as is)", "Change it"], "None"), root=root)
    assert "ambiguous" in str(e.value)


@pytest.mark.parametrize("options,reco", [
    (["Yes", "No"], "None"), (["Yes", "No"], "None: this is your judgment"), (["Alpha", "Beta"], "No recommendation: your call"),
    (["None", "Change it"], "None"),                               # an option literally labelled None, recommended by that label
    (["Neither / None", "Change it"], "None: your judgment"),      # no label STARTS with the word, so the none rule applies
])
def test_a_clean_none_line_is_still_accepted(root, options, reco):
    assert OPS.send(ME, _with_options(options, reco), root=root)["ok"]

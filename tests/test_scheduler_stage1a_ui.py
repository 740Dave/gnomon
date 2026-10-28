"""Scheduler / Run Log tab (web/scheduler_ui/scheduler_tab.js) under node: the pure view builders (
). The glass checks (ac-s1-own-session-listed-and-cancellable, ac-s1-real-pty) are LIVE-ONLY and not
claimed here. The pure builders only; the DOM wiring was not exercised by a browser in this build."""

import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
JS = os.path.join(ROOT, "web", "scheduler_ui", "scheduler_tab.js")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def run(prog):
    r = subprocess.run([NODE, "-e", "const T=require(process.argv[1]);" + prog, JS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_chips_unverified_has_its_own_colour_and_failure_class_covers_the_bad_states():
    out = run("console.log(JSON.stringify(['OK','OK-UNVERIFIED','FAIL','MISSED','SKIPPED','NEVER-RUN','refused','WAITING','waiting','paused']"
              ".map(r=>T.resultChip(r))))")
    cls = {c["text"]: c["cls"] for c in out}
    assert len({cls["OK"], cls["OK-UNVERIFIED"], cls["FAIL"]}) == 3          # three distinct looks
    for bad in ("FAIL", "MISSED", "SKIPPED", "NEVER-RUN", "refused"):
        assert "is-failure" in cls[bad], bad
    assert "is-failure" not in cls["OK"] and "is-failure" not in cls["OK-UNVERIFIED"] and "is-failure" not in cls["WAITING"]
    assert [c["failure"] for c in out][:3] == [False, False, True]


def test_list_has_pending_first_an_explicit_empty_state_and_flags_in_the_failure_class():
    out = run("""console.log(JSON.stringify([
      T.buildList({ok:true,jobs:[],pending:[],banner:[]}),
      T.buildList({ok:true,jobs:[{id:'j',title:'Rem',owner:'a',schedule:'Every 30 minutes',next_run:'2026-10-10T12:30:00Z',last_result:'NEVER-RUN',last_shown:'NEVER-RUN',enabled:true}],
        pending:[{id:'p',title:'New one',kind:'new',proposed_by:'agent-a',proposed_by_verified:true,schedule:'Once'}],
        banner:[{job_id:'j',result:'SKIPPED',reason:'target-not-live',ts:'2026-10-10T12:00:00Z'}]}),
      T.buildList(null), T.buildList({ok:false,error:'boom'})]))""")
    empty, full, none, bad = out
    assert empty["empty"] is True and "No jobs" in empty["emptyText"] and empty["pendingCount"] == 0
    assert full["pendingCount"] == 1 and full["pending"][0]["title"] == "New: New one" and full["rows"][0]["failure"] is True
    assert full["banner"][0]["text"].startswith("j: SKIPPED (target-not-live)")
    assert "error" in none and bad["error"] == "boom"


def test_detail_shows_text_pin_state_and_the_refused_state():
    out = run("""console.log(JSON.stringify([
      T.buildDetail({ok:true,id:'j',job:{title:'Rem',owner:'a',schedule:'Every day at 06:10 (local time)',next_runs:['x','y'],enabled:true,done:false,
        action:{type:'inject',target_instance_id:'a',text:'check it'},pin:{ok:true,failed:''},descriptor:'d',steps:['s1'],confirmed_at:'2026-10-10T12:00:00Z'},runs:[]}),
      T.buildDetail({ok:true,id:'j',job:{title:'Rem',owner:'a',schedule:'s',next_runs:[],enabled:true,done:false,action:{type:'inject',target_instance_id:'a',text:'t'},pin:{ok:false,failed:'job-changed'}},runs:[]}),
      T.buildDetail({ok:true,id:'p',proposal:{title:'P',kind:'new',proposed_by:'a',proposed_by_verified:false,schedule:'s',target:'a',text:'full text',digest:'dd'}})]))""")
    good, refused, prop = out
    assert good["text"] == "check it" and good["pinOk"] is True and any(r["key"] == "Next runs (UTC)" and r["value"] == "x, y" for r in good["rows"])
    assert refused["pinOk"] is False and any("DOES NOT MATCH" in r["value"] for r in refused["rows"])
    assert prop["hasPending"] is True and prop["text"] == "full text" and any("(unverified)" in r["value"] for r in prop["rows"])


def test_run_log_rows_filters_and_tamper_warnings():
    out = run("""console.log(JSON.stringify(T.buildRunLog({ok:true,runs:[{run_id:'r1',job_id:'j',result:'OK',outcome:'DELIVERED',trigger:'one-shot',slot_utc:'s',ts:'2026-10-10T12:00:00Z',flagged:false},
      {run_id:'r2',job_id:'j',result:'MISSED',reason:'one-shot-too-late',flagged:true,ts:'2026-10-10T12:01:00Z'}],
      chains:{j:{ok:false,breaks:[{line:3}]}},audit_chain:{ok:false,breaks:[{line:2}]},filters:{results:['OK','MISSED']}})))""")
    assert [r["chip"]["text"] for r in out["rows"]] == ["DELIVERED · OK", "MISSED"]
    assert out["rows"][1]["failure"] is True and "FLAGGED" in out["rows"][1]["sub"]
    assert any("run record of j: line 3" in w for w in out["warnings"]) and any("audit log: line 2" in w for w in out["warnings"])


def test_new_arrivals_and_no_html_strings_in_the_script():
    out = run("console.log(JSON.stringify(T.newArrivals(['a'],[{id:'a'},{id:'b'}])))")
    assert out == ["b"]
    src = open(JS, encoding="utf-8").read()
    assert "innerHTML" not in src and "insertAdjacentHTML" not in src and "document.write" not in src
    assert "monospace" not in src.lower() and "courier" not in src.lower()
    for forbidden in ("<form", "createjob", "newJob", "create-job"):
        assert forbidden.lower() not in src.lower()                      # no job builder

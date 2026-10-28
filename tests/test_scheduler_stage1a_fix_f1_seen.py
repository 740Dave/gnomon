"""Fix round 2, F-1 (P1): the Confirm click is bound to what the operator SAW. The tab echoes the digest of the exact
Pending row it rendered ("seen", the Apps tab's round-2 F11 mechanism); the server refuses a confirm whose seen digest is
missing or differs from the proposal as it is NOW, BEFORE any presence prompt, and returns the new version."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from scheduler_support import propose_inject
from test_scheduler_stage1a_web import J, world  # noqa: F401  (the fixture)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
JS = os.path.join(ROOT, "web", "scheduler_ui", "scheduler_tab.js")


def seen_of(w, job="remind-me"):
    return w.c.get(f"/api/scheduler/{job}").json()["proposal"]["digest"]


def post(w, op, body, job="remind-me"):
    return w.c.post(f"/api/scheduler/{job}/{op}", headers=J, content=json.dumps(body).encode())


def test_the_reviewers_probe_a_re_proposal_between_view_and_click_is_not_confirmed(world):
    w = world
    propose_inject(w.sch, trigger={"once_at": "2026-10-10T12:05:00Z"}, text="text A")
    seen = seen_of(w)                                              # the operator opens the Pending row
    propose_inject(w.sch, trigger={"every_minutes": 15}, text="text B")   # the agent re-proposes the same job id
    r = post(w, "confirm", {"seen": seen})                         # the operator clicks Confirm on what he saw
    assert r.status_code == 409 and r.json()["stale"] is True
    assert r.json()["proposal"]["text"] == "text B"                # the new version is shown for a fresh look
    assert w.asked == [] and not os.path.exists(w.sch.paths.job("remind-me"))   # refused BEFORE the presence prompt
    assert os.path.exists(w.sch.paths.proposal("remind-me"))       # the (new) proposal is still pending
    r2 = post(w, "confirm", {"seen": r.json()["proposal"]["digest"]})   # after a fresh look: confirms exactly B
    assert r2.status_code == 200
    job = json.load(open(w.sch.paths.job("remind-me")))
    assert job["action"]["text"] == "text B" and job["trigger"] == {"every_minutes": 15}


def test_a_confirm_without_a_seen_digest_is_refused(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30})
    for body in ({}, {"seen": None}, {"seen": 12}, {"seen": ""}):
        r = post(w, "confirm", body)
        assert r.status_code == 409, body
    assert w.asked == [] and not os.path.exists(w.sch.paths.job("remind-me"))
    assert post(w, "confirm", {"seen": seen_of(w)}).status_code == 200      # control


def test_a_seen_digest_of_an_unchanged_proposal_commits(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="keep me")
    assert post(w, "confirm", {"seen": seen_of(w)}).json()["status"] == "confirmed"
    assert len(w.asked) == 1


def test_the_presence_prompt_names_the_schedule_and_the_first_words_of_the_text(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="restart the harness at noon please")
    post(w, "confirm", {"seen": seen_of(w)})
    assert "remind-me" in w.asked[0] and "Every 30 minutes" in w.asked[0] and "restart the harness" in w.asked[0]


def test_decline_and_cancel_with_a_stale_seen_are_refused(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="one")
    old = seen_of(w)
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="two")
    r = post(w, "decline", {"seen": old})
    assert r.status_code == 409 and os.path.exists(w.sch.paths.proposal("remind-me"))
    assert post(w, "decline", {"seen": seen_of(w)}).status_code == 200
    propose_inject(w.sch, job_id="live-one", trigger={"every_minutes": 30})
    post(w, "confirm", {"seen": seen_of(w, "live-one")}, job="live-one")
    assert post(w, "cancel", {"seen": "0" * 64}, job="live-one").status_code == 409
    assert os.path.exists(w.sch.paths.job("live-one"))
    pin = json.load(open(w.sch.paths.pin("live-one")))["definition_sha256"]
    assert post(w, "cancel", {"seen": pin}, job="live-one").status_code == 200


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_the_tab_sends_the_digest_it_rendered():
    prog = ("const T=require(process.argv[1]);"
            "const v=T.buildDetail({ok:true,id:'p',proposal:{title:'P',kind:'new',proposed_by:'a',proposed_by_verified:true,"
            "schedule:'s',target:'a',text:'t',digest:'abc'}});"
            "const j=T.buildDetail({ok:true,id:'j',job:{title:'J',owner:'a',schedule:'s',next_runs:[],enabled:true,done:false,"
            "action:{type:'inject',target_instance_id:'a',text:'t'},pin:{ok:true,failed:'',sha256:'pinsha'}},runs:[]});"
            "console.log(JSON.stringify([T.actionBody(v,'confirm'),T.actionBody(v,'decline'),T.actionBody(j,'cancel'),T.actionBody(j,'pause')]))")
    out = json.loads(subprocess.run([NODE, "-e", prog, JS], capture_output=True, text=True, timeout=60).stdout)
    assert out == [{"seen": "abc"}, {"seen": "abc"}, {"seen": "pinsha"}, {}]

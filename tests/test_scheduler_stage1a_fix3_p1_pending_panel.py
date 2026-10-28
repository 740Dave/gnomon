"""Fix round 3, P1-1: for a CHANGE proposal the Pending panel shows the PROPOSED job (its rows, kind, proposed-by and the
new cleaned text); the old values appear only in the "What changed" lines. Tested with the SERVER'S REAL detail response
passed through the tab's buildDetail under node (the earlier F-6 test fed a hand-made response without `job`)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

from scheduler_support import confirm, propose_inject
from test_scheduler_stage1a_web import J, confirm_post, world  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
JS = os.path.join(ROOT, "web", "scheduler_ui", "scheduler_tab.js")
pytestmark = pytest.mark.skipif(NODE is None, reason="node not installed")


def build_detail(resp: dict) -> dict:
    prog = "const T=require(process.argv[1]);console.log(JSON.stringify(T.buildDetail(JSON.parse(process.argv[2]))))"
    r = subprocess.run([NODE, "-e", prog, JS, json.dumps(resp)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def values(rows):
    return {r["key"]: r["value"] for r in rows}


def test_a_change_proposals_pending_panel_shows_the_proposed_job_not_the_old_one(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="OLD TEXT", title="Old title")
    assert confirm_post(w).status_code == 200
    propose_inject(w.sch, trigger={"every_minutes": 15}, text="NEW TEXT", title="New title")
    real = w.c.get("/api/scheduler/remind-me").json()                      # the server's own response for a change proposal
    assert real["proposal"]["kind"] == "change" and real["job"]["action"]["text"] == "OLD TEXT"   # both are present
    v = build_detail(real)
    p = values(v["pendingRows"])
    assert p["Kind"] == "change" and p["Proposed by"].startswith("agent-a")
    assert p["Schedule"] == "Every 15 minutes" and p["Target session"] == "agent-a"
    assert v["pendingText"] == "NEW TEXT" and v["pendingTitle"] == "New title"
    assert "Every 30 minutes" not in json.dumps(v["pendingRows"]) and "OLD TEXT" not in json.dumps(v["pendingRows"])
    assert "OLD TEXT" not in v["pendingText"]
    # the old values are in the "What changed" lines, and the Job panel still shows the confirmed job
    lines = [c["line"] for c in v["changes"]]
    assert "schedule: Every 30 minutes -> Every 15 minutes" in lines and "text: OLD TEXT -> NEW TEXT" in lines
    j = values(v["jobRows"])
    assert j["Schedule"] == "Every 30 minutes" and v["jobText"] == "OLD TEXT"
    assert v["hasPending"] is True and v["isJob"] is True and v["digest"] == real["proposal"]["digest"]


def test_a_new_job_removal_and_plain_job_panels(world):
    w = world
    propose_inject(w.sch, job_id="fresh", trigger={"every_minutes": 30}, text="fresh text")
    v = build_detail(w.c.get("/api/scheduler/fresh").json())
    assert v["hasPending"] and not v["isJob"] and v["pendingText"] == "fresh text"
    assert values(v["pendingRows"])["Kind"] == "new" and v["changes"] == []
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="keep");  confirm(w.sch)
    w.sch.propose_remove("remind-me", proposer="agent-a", verified=True)
    v = build_detail(w.c.get("/api/scheduler/remind-me").json())
    assert v["removes"] is True and values(v["pendingRows"])["Kind"] == "removal" and v["jobText"] == "keep"
    plain = build_detail(w.c.get("/api/scheduler/fresh").json())
    w.sch.decline("fresh", actor="x", seen=w.sch.proposal_digest("fresh"))
    plain2 = propose_inject(w.sch, job_id="solo", trigger={"every_minutes": 30}); confirm(w.sch, "solo")
    v = build_detail(w.c.get("/api/scheduler/solo").json())
    assert v["hasPending"] is False and v["isJob"] is True and v["pendingRows"] == [] and v["jobText"] == "check the inbox"

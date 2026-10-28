"""Fix round 2, the cheap P3s: P3a plan_usage_band takes the job; P3e bounded compaction read; P3f action-type refusal
names the stage for any unknown or agent-launch type."""

from __future__ import annotations

import json
import os

from scheduler_support import T0, confirm, make, propose_inject
from gnomon.scheduler import inject, model, plan_usage


def test_p3a_the_plan_usage_seam_is_handed_the_job(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(plan_usage, "plan_usage_band", lambda job=None: (seen.append(job), "run")[1])
    sch, clock, reg, tgt = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30}); confirm(sch)
    clock.t = T0 + 30 * 60 + 5; sch.tick()
    assert len(seen) == 1 and seen[0]["id"] == "remind-me" and seen[0]["owner"] == "agent-a" and len(reg.routed) == 1
    assert plan_usage.plan_usage_band() == "run" and plan_usage.plan_usage_band({"id": "x"}) == "run"


def test_p3e_the_compaction_check_never_reads_more_than_four_megabytes(tmp_path, monkeypatch):
    p = tmp_path / "log.jsonl"
    p.write_text(json.dumps({"kind": "injection", "pad": "x" * 2000}) + "\n" * 1)
    with open(p, "a") as fh:
        for _ in range(3000):                              # ~6 MB of non-lifecycle records
            fh.write(json.dumps({"kind": "subagent-summary", "pad": "x" * 2000}) + "\n")
    reads = []
    real = inject._parse
    monkeypatch.setattr(inject, "_parse", lambda chunk, drop_first: (reads.append(len(chunk)), real(chunk, drop_first))[1])
    state, detail = inject.compaction_state(str(p), 1.0)
    assert state == "clear" and max(reads) <= 4 * 1024 * 1024 + 1


def test_p3f_every_unknown_or_launch_action_type_is_refused_naming_the_stage(tmp_path):
    sch, *_ = make(tmp_path)
    for t in ("script", "agent", "launch_agent", "launch", "bogus"):
        r = propose_inject(sch, action={"type": t, "text": "x"})
        assert r["ok"] is False and r["field"] == "action.type"
        assert "stage 1b" in r["error"] or "stage 2" in r["error"], (t, r["error"])
    assert len(model._AGENT_TYPES) == len(set(model._AGENT_TYPES))

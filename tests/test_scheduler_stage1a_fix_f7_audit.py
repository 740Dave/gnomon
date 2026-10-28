"""Fix round 2, F-7: every verb call, refusals included, leaves an audit line."""

from __future__ import annotations

import json
import os

from scheduler_support import confirm, make, propose_inject
from gnomon.scheduler import verbs

PROPOSE = {"job_id": "remind-me", "title": "t", "trigger": {"every_minutes": 30}, "action": {"type": "inject", "text": "hi"}}


def audit(sch):
    return [json.loads(l) for l in open(sch.paths.audit)] if os.path.exists(sch.paths.audit) else []


def test_a_verb_policy_refusal_writes_an_audit_line(tmp_path, monkeypatch):
    sch, *_ = make(tmp_path)
    monkeypatch.setattr(verbs, "_policy_check", lambda verb, actor: "denied by the test policy")
    out = verbs.dispatch(sch, "sched_list", {}, "agent-a", verified_caller=True)
    assert out["ok"] is False
    a = audit(sch)
    assert a and a[-1]["verb"] == "sched_list" and a[-1]["outcome"] == "refused" and a[-1]["actor"] == "agent-a"
    assert "policy" in json.dumps(a[-1]["detail"])


def test_remove_with_an_invalid_id_writes_an_audit_line(tmp_path):
    sch, *_ = make(tmp_path)
    for bad in ("Bad Id", None, 5, "../x"):
        r = sch.propose_remove(bad, proposer="agent-a", verified=True)
        assert r["ok"] is False
    a = audit(sch)
    assert len(a) == 4 and all(l["verb"] == "sched_propose_remove" and l["outcome"] == "refused" for l in a)


def test_every_verb_call_leaves_exactly_one_audit_line_including_refusals(tmp_path, monkeypatch):
    sch, *_ = make(tmp_path)
    calls = [("sched_list", {}), ("sched_status", {"job_id": "nope-nope"}), ("sched_status", {}), ("sched_log", {"limit": "x"}),
             ("sched_propose", {"job_id": "BAD"}), ("sched_propose_remove", {"job_id": "nope-nope"}), ("sched_run_now", {}),
             ("sched_propose", dict(PROPOSE))]
    for verb, args in calls:
        before = len(audit(sch))
        verbs.dispatch(sch, verb, args, "agent-a", verified_caller=True)
        assert len(audit(sch)) == before + 1, (verb, args)
    for verb in ("sched_list", "sched_propose"):             # unverified, and policy-denied, are audited too
        before = len(audit(sch))
        verbs.dispatch(sch, verb, dict(PROPOSE), "agent-a", verified_caller=False)
        assert len(audit(sch)) == before + 1

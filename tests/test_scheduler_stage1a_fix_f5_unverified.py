"""Fix round 2, F-5: the surviving mutant (verified = bool(caller)): a non-empty but UNVERIFIED caller is refused."""

from __future__ import annotations

import os

from scheduler_support import make
from gnomon.scheduler import verbs

PROPOSE = {"job_id": "remind-me", "title": "t", "trigger": {"every_minutes": 30}, "action": {"type": "inject", "text": "hi"}}


def test_a_non_empty_caller_that_is_not_verified_is_refused_for_every_verb(tmp_path):
    sch, *_ = make(tmp_path)
    for verb in ("sched_list", "sched_status", "sched_propose", "sched_propose_remove", "sched_log"):
        out = verbs.dispatch(sch, verb, dict(PROPOSE), "agent-a", verified_caller=False)   # a transport-claimed id
        assert out["ok"] is False and "not verified" in out["error"], verb
    assert not os.path.exists(sch.paths.proposal("remind-me"))
    ok = verbs.dispatch(sch, "sched_propose", dict(PROPOSE), "agent-a", verified_caller=True)   # control
    assert ok["ok"] is True
    assert verbs.dispatch(sch, "sched_propose", dict(PROPOSE), None, verified_caller=True)["ok"] is False   # verified but nobody

"""Fix round 3, P3-2 / P3-4 / P3-1: decline and cancel REQUIRE the seen digest (refused without it, audited); stale-page
refusals write an audit line; the dedicated script refusal keeps its exact message (pinned)."""

from __future__ import annotations

import json
import os

from scheduler_support import confirm, make, propose_inject
from test_scheduler_stage1a_web import J, confirm_post, world  # noqa: F401
from gnomon.scheduler import vocab


def audit(sch):
    return [json.loads(l) for l in open(sch.paths.audit)] if os.path.exists(sch.paths.audit) else []


def post(w, op, body, job="remind-me"):
    return w.c.post(f"/api/scheduler/{job}/{op}", headers=J, content=json.dumps(body).encode())


def test_decline_without_a_seen_digest_is_refused_and_audited(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30})
    for body in ({}, {"seen": None}, {"seen": 5}, {"seen": ""}):
        r = post(w, "decline", body)
        assert r.status_code == 409, body
    assert os.path.exists(w.sch.paths.proposal("remind-me"))
    a = [l for l in audit(w.sch) if l["verb"] == "decline"]
    assert len(a) == 4 and all(l["outcome"] == "refused" for l in a)
    seen = w.c.get("/api/scheduler/remind-me").json()["proposal"]["digest"]
    assert post(w, "decline", {"seen": seen}).status_code == 200                       # control


def test_cancel_without_a_seen_digest_is_refused_and_audited(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30})
    assert confirm_post(w).status_code == 200
    for body in ({}, {"seen": None}, {"seen": 0}):
        assert post(w, "cancel", body).status_code == 409
    assert os.path.exists(w.sch.paths.job("remind-me"))
    a = [l for l in audit(w.sch) if l["verb"] == "cancel"]
    assert len(a) == 3 and all(l["outcome"] == "refused" for l in a)
    pin = w.c.get("/api/scheduler/remind-me").json()["job"]["pin"]["sha256"]
    assert post(w, "cancel", {"seen": pin}).status_code == 200


def test_stale_decline_and_cancel_refusals_write_an_audit_line(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="one")
    old = w.c.get("/api/scheduler/remind-me").json()["proposal"]["digest"]
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="two")
    n = len(audit(w.sch))
    assert post(w, "decline", {"seen": old}).status_code == 409
    assert len(audit(w.sch)) == n + 1 and audit(w.sch)[-1]["verb"] == "decline" and audit(w.sch)[-1]["outcome"] == "refused"
    seen = w.c.get("/api/scheduler/remind-me").json()["proposal"]["digest"]
    post(w, "confirm", {"seen": seen})
    n = len(audit(w.sch))
    assert post(w, "cancel", {"seen": "0" * 64}).status_code == 409
    assert len(audit(w.sch)) == n + 1 and audit(w.sch)[-1]["verb"] == "cancel" and audit(w.sch)[-1]["outcome"] == "refused"


def test_the_engine_itself_requires_seen_for_decline_and_cancel(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30})
    assert sch.decline("remind-me", actor="x", seen=None)["ok"] is False
    assert os.path.exists(sch.paths.proposal("remind-me"))
    confirm(sch)
    assert sch.cancel("remind-me", actor="x", seen=None)["ok"] is False and os.path.exists(sch.paths.job("remind-me"))


def test_the_dedicated_script_refusal_keeps_its_exact_message(tmp_path):
    sch, *_ = make(tmp_path)
    for t in ("script", "shell", "python", "exec", "command"):
        r = propose_inject(sch, action={"type": t, "text": "x"})
        assert r["field"] == "action.type" and r["error"] == "action.type: " + vocab.STAGE_1B, t

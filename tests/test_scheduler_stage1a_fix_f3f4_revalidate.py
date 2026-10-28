"""Fix round 2, F-3 / F-4: defence in depth. confirm() re-validates the whole proposal (own session, limits, text
cleaning, normal form); the engine checks target == owner at EVERY send. The reviewer's planted-proposal-file probe."""

from __future__ import annotations

import json
import os

from scheduler_support import PRESENCE_OK, T0, confirm, make, propose_inject
from gnomon.scheduler import model, store


def plant_proposal(sch, *, job_id="planted", owner="agent-a", target="other-agent", text="hi", trigger=None, kind="general",
                   proposer=None, verified=True):
    defn = {"id": job_id, "title": "Planted", "owner": owner, "kind": kind, "trigger": trigger or {"every_minutes": 30},
            "action": {"type": "inject", "target_instance_id": target, "text": text}, "schema_version": 1}
    prop = {"schema_version": 1, "id": job_id, "proposer": proposer or owner, "owner": owner, "proposer_verified": verified,
            "proposed_at": "2026-10-10T12:00:00Z", "change_of_confirmed": False, "definition": defn,
            "definition_sha256": model.definition_sha256(defn)}
    os.makedirs(sch.paths.proposals, exist_ok=True)
    json.dump(prop, open(sch.paths.proposal(job_id), "w"))
    return prop


def plant_job(sch, *, job_id="planted", owner="agent-a", target="other-agent", text="hi", trigger=None):
    defn = {"id": job_id, "title": "Planted", "owner": owner, "kind": "general", "trigger": trigger or {"every_minutes": 30},
            "action": {"type": "inject", "target_instance_id": target, "text": text}, "schema_version": 1}
    for d in (sch.paths.jobs, sch.paths.pins, sch.paths.state):
        os.makedirs(d, exist_ok=True)
    json.dump({**defn, "enabled": True, "confirmed_at": "2026-10-10T12:00:00Z"}, open(sch.paths.job(job_id), "w"))
    json.dump({"schema_version": 1, "id": job_id, "definition_sha256": model.definition_sha256(defn)}, open(sch.paths.pin(job_id), "w"))
    json.dump({"anchor": T0, "next_due": T0 + 60, "done": False, "active": None, "tracking": []}, open(sch.paths.job_state(job_id), "w"))


def test_a_planted_cross_session_proposal_is_refused_at_confirm(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    reg.register("other-agent", tgt.dir)
    plant_proposal(sch)                                   # target other-agent, a RECOMPUTED definition sha
    r = confirm(sch, "planted")
    assert r["ok"] is False and "another agent" in r["error"]
    assert not os.path.exists(sch.paths.job("planted")) and reg.routed == []
    clock.advance(600); sch.tick()
    assert reg.routed == []


def test_a_planted_cross_session_job_is_refused_at_send_and_the_refusal_is_recorded(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    reg.register("other-agent", tgt.dir)
    plant_job(sch)                                        # job + matching pin written straight into the store
    clock.advance(120); sch.tick()
    assert [r for r in reg.routed if r[0] == "other-agent"] == [] and reg.routed == []
    runs = sch.run_log(job_id="planted")["runs"]
    assert runs and runs[0]["result"] == "FAIL" and runs[0]["reason"] == "refused(target-not-owner)" and runs[0]["flagged"]


def test_the_send_check_runs_on_every_send_even_if_the_job_changes_between_ticks(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30}); confirm(sch)
    clock.t = T0 + 30 * 60 + 5; sch.tick()
    assert len(reg.routed) == 1                           # control: its own session sends
    job = json.load(open(sch.paths.job("remind-me")))
    job["action"]["target_instance_id"] = "other-agent"
    json.dump(job, open(sch.paths.job("remind-me"), "w"))
    pin = json.load(open(sch.paths.pin("remind-me")))
    pin["definition_sha256"] = model.definition_sha256(job)       # even with a re-computed pin
    json.dump(pin, open(sch.paths.pin("remind-me"), "w"))
    reg.register("other-agent", tgt.dir)
    clock.t += 30 * 60; sch.tick()
    assert len(reg.routed) == 1 and not [x for x in reg.routed if x[0] == "other-agent"]


def test_confirm_revalidates_limits_text_and_normal_form(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    for i in range(20):                                   # twenty legitimate pending proposals
        assert propose_inject(sch, job_id=f"job-{i:02d}", trigger={"every_minutes": 60 + i})["ok"]
    plant_proposal(sch, job_id="one-too-many", target="agent-a")                      # a 21st, planted
    r = confirm(sch, "one-too-many")
    assert r["ok"] is False and "too many active jobs" in r["error"]
    plant_proposal(sch, job_id="dirty-text", target="agent-a", text="x\x1b[31m\x07y")  # control bytes in the stored text
    assert confirm(sch, "dirty-text")["ok"] is False
    plant_proposal(sch, job_id="bang", target="agent-a", text="!rm -rf")
    assert confirm(sch, "bang")["ok"] is False
    plant_proposal(sch, job_id="fast", target="agent-a", trigger={"every_minutes": 5})
    assert confirm(sch, "fast")["ok"] is False
    plant_proposal(sch, job_id="not-owner", owner="agent-a", proposer="agent-b", target="agent-a")
    assert confirm(sch, "not-owner")["ok"] is False
    plant_proposal(sch, job_id="unverified", target="agent-a", verified=False)
    assert confirm(sch, "unverified")["ok"] is False
    plant_proposal(sch, job_id="script", target="agent-a")
    p = json.load(open(sch.paths.proposal("script"))); p["definition"]["action"]["type"] = "script"
    p["definition_sha256"] = model.definition_sha256(p["definition"]); json.dump(p, open(sch.paths.proposal("script"), "w"))
    assert confirm(sch, "script")["ok"] is False
    assert sch.list_jobs()["jobs"] == []
    for bad in ("one-too-many", "dirty-text", "bang", "fast", "not-owner", "unverified", "script"):
        os.unlink(sch.paths.proposal(bad))                 # the refused plants stay on disk and still count: clear them
    sch.propose_remove("job-19", proposer="agent-a", verified=True)
    plant_proposal(sch, job_id="fine", target="agent-a")
    assert confirm(sch, "fine")["ok"] is True             # control: a well-formed planted proposal passes the re-validation


def test_a_planted_removal_by_a_non_owner_is_refused(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30}); confirm(sch)
    prop = {"schema_version": 1, "id": "remind-me", "proposer": "agent-b", "owner": "agent-b", "proposer_verified": True,
            "proposed_at": "x", "removal": True, "change_of_confirmed": True,
            "definition": model.definition_of(json.load(open(sch.paths.job("remind-me")))), "definition_sha256": "x"}
    json.dump(prop, open(sch.paths.proposal("remind-me"), "w"))
    assert confirm(sch)["ok"] is False and os.path.exists(sch.paths.job("remind-me"))

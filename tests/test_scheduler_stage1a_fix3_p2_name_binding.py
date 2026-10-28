"""Fix round 3, P2-1: re-validation binds the proposal FILE NAME to the job id inside it. The reviewer's planted
proposals/victim.json (carrying another definition id) must be refused and must not overwrite the victim's job."""

from __future__ import annotations

import json
import os

from scheduler_support import confirm, make, propose_inject
from test_scheduler_stage1a_fix_f3f4_revalidate import plant_proposal
from gnomon.scheduler import model


def test_a_planted_victim_json_with_another_definition_id_is_refused_and_overwrites_nothing(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    # agent-q owns a confirmed job "victim"
    sch.propose({"job_id": "victim", "title": "Q", "trigger": {"every_minutes": 30}, "action": {"type": "inject", "text": "q's note"}},
                proposer="agent-q", verified=True)
    assert confirm(sch, "victim")["ok"]
    before = open(sch.paths.job("victim")).read()
    plant_proposal(sch, job_id="mine", owner="agent-a", target="agent-a")          # a valid agent-a proposal ...
    os.replace(sch.paths.proposal("mine"), sch.paths.proposal("victim"))           # ... planted under the victim's FILE NAME
    r = confirm(sch, "victim")
    assert r["ok"] is False and "file name" in r["error"]
    assert open(sch.paths.job("victim")).read() == before                           # agent-q's job is untouched
    assert not os.path.exists(sch.paths.job("mine"))


def test_the_outer_id_and_the_inner_id_must_agree_too(tmp_path):
    sch, *_ = make(tmp_path)
    prop = plant_proposal(sch, job_id="alpha", target="agent-a")
    prop["id"] = "beta"                                                             # outer id differs from file and definition
    json.dump(prop, open(sch.paths.proposal("alpha"), "w"))
    assert confirm(sch, "alpha")["ok"] is False
    prop = plant_proposal(sch, job_id="gamma", target="agent-a")
    prop["definition"]["id"] = "delta"
    prop["definition_sha256"] = model.definition_sha256(prop["definition"])
    json.dump(prop, open(sch.paths.proposal("gamma"), "w"))
    assert confirm(sch, "gamma")["ok"] is False
    assert not os.path.exists(sch.paths.job("delta")) and not os.path.exists(sch.paths.job("gamma"))


def test_a_removal_planted_under_another_name_is_refused(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, job_id="keep-me", trigger={"every_minutes": 30}); confirm(sch, "keep-me")
    sch.propose_remove("keep-me", proposer="agent-a", verified=True)
    os.replace(sch.paths.proposal("keep-me"), sch.paths.proposal("other-name"))
    assert confirm(sch, "other-name")["ok"] is False and os.path.exists(sch.paths.job("keep-me"))


def test_the_honest_path_still_confirms(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30})
    assert confirm(sch)["ok"] is True

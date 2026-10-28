"""Scheduler stage 1a: proposal validation, text cleaning, triggers, limits (/ -LIMITS /
-ONE-SHOT). Pure functions and the engine's propose; nothing is sent."""

from __future__ import annotations

import datetime as dt
import os

import pytest

from scheduler_support import T0, confirm, make, propose_inject
from gnomon.scheduler import schedule, text as stext
from gnomon.scheduler.limits import LIMITS


def listing(root):
    return sorted(os.listdir(root)) if os.path.isdir(root) else []


# --- ac-s1-text-cleaned-or-refused / ac-s1-bang-refused ----------------------------------------------------------
def test_control_characters_are_stripped_and_the_text_is_one_plain_line_at_most_one_break():
    dirty = "hello\x1b[31m red\x1b[0m\x07\x00 world\x7f‮​\r\n\r\nsecond\n\nthird\x9b"
    assert stext.clean_text(dirty) == "hello red world\nsecond third"
    assert stext.clean_text("\x1b]0;title\x07abc") == "abc"
    assert "\x1b" not in stext.clean_text("a\x1bb") and stext.clean_text("tab\there") == "tab here"


def test_unicode_line_separators_and_format_characters_are_handled():
    assert stext.clean_text("a\u2028b\u2029c\x85d") == "a\nb c d"           # one break kept, the rest become spaces
    assert stext.clean_text("x\u200b\u200d\ufeff\u202ay") == "xy"           # zero-width, joiner, BOM, bidi override
    assert stext.clean_text("\x9b31m red") == "31m red"                          # a C1 CSI byte is dropped


def test_text_over_limit_after_stripping_or_empty_after_stripping_is_refused(tmp_path):
    sch, *_ = make(tmp_path)
    r = propose_inject(sch, text="x" * 1001)
    assert r["ok"] is False and r["field"] == "action.text" and "1000" in r["error"]
    assert propose_inject(sch, text="\x1b[0m\x07\x00 \n")["ok"] is False                  # empty after cleaning
    assert propose_inject(sch, text=("x" * 1000 + "\x00" * 50))["ok"] is True            # control bytes do not count
    assert propose_inject(sch, job_id="plain", text="x" * 1000)["ok"] is True             # control: exactly the limit
    assert propose_inject(sch, job_id="plain2", text=12345)["ok"] is False


def test_a_line_the_harness_would_refuse_is_refused_at_proposal_not_left_to_stay_SENT(tmp_path):
    sch, *_ = make(tmp_path)
    for t in ("fine first line\n\\hooks off", "ok\n\\approve"):
        r = propose_inject(sch, text=t)
        assert r["ok"] is False and r["field"] == "action.text" and "harness would refuse" in r["error"], t
    assert propose_inject(sch, text="fine first line\n\\\\escaped")["ok"] is True      # the harness's own escape passes
    assert propose_inject(sch, job_id="x2", text="no backslash command here \\ at all")["ok"] is True
    from gnomon.scheduler import inject
    assert inject.harness_screen("[scheduled: a by b at c] ok") is None
    assert inject.harness_screen("[scheduled: a by b at c] ok\n\\hooks off")


def test_a_text_starting_with_bang_is_refused_loudly_and_stored_nowhere(tmp_path):
    sch, *_ = make(tmp_path)
    for t in ("!rm -rf /", "  \x1b[0m! sneaky", "\n!x"):
        r = propose_inject(sch, text=t)
        assert r["ok"] is False and r["field"] == "action.text" and "'!'" in r["error"]
    assert propose_inject(sch, text="hello !")["ok"] is True                            # control: only a LEADING bang
    assert listing(sch.paths.jobs) == []


# --- ac-s1-other-session-refused / unverified ---------------------------------------------------------------------
def test_another_sessions_target_is_refused_with_the_stated_reason_and_nothing_is_stored(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    r = propose_inject(sch, action={"type": "inject", "text": "x", "target_instance_id": "agent-b"})
    assert r["ok"] is False and r["field"] == "action.target_instance_id"
    assert "an inject to another agent's session needs a confirmation rule that is not part of stage 1" in r["error"]
    r2 = propose_inject(sch, instance_id="agent-b")
    assert r2["ok"] is False and "another agent's session" in r2["error"]
    assert listing(sch.paths.proposals) == [] and listing(sch.paths.jobs) == [] and reg.routed == []
    own = propose_inject(sch, action={"type": "inject", "text": "x", "target_instance_id": "agent-a"})
    assert own["ok"] is True                                                           # control: its own session


def test_an_unverified_caller_is_refused(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    r = sch.propose({"job_id": "x1", "title": "t", "trigger": {"every_minutes": 30}, "action": {"type": "inject", "text": "hi"}},
                    proposer="agent-a", verified=False)
    assert r["ok"] is False and "not verified" in r["error"]
    assert listing(sch.paths.proposals) == []
    assert sch.propose({"job_id": "x1"}, proposer=None, verified=True)["ok"] is False


# --- ac-s1-1a-no-script-action ------------------------------------------------------------------------------------
@pytest.mark.parametrize("action,needle", [
    ({"type": "script", "argv": ["/bin/echo"]}, "stage 1b"), ({"type": "shell"}, "stage 1b"), ({"type": "python"}, "stage 1b"),
    ({"type": "agent", "prompt": "run PROC-X"}, "stage 2"), ({"type": "agent"}, "stage 2"),
    ({"type": "launch_agent"}, "stage 2"), ({"type": "nonsense", "text": "x"}, "'inject'"), ({"text": "x"}, "'inject'")])
def test_script_and_agent_actions_are_refused_naming_their_stage(tmp_path, action, needle):
    sch, *_ = make(tmp_path)
    r = propose_inject(sch, action=action)
    assert r["ok"] is False and r["field"] == "action.type" and needle in r["error"]
    assert listing(sch.paths.proposals) == []


@pytest.mark.parametrize("key", ["boot", "login", "on_gnomon_start", "on_start", "on-gnomon-start"])
def test_boot_login_and_on_gnomon_start_triggers_are_refused_naming_stage_one(tmp_path, key):
    sch, *_ = make(tmp_path)
    r = propose_inject(sch, trigger={key: True})
    assert r["ok"] is False and r["field"] == "trigger." + key and "not part of stage 1" in r["error"]


def test_strict_fields_kind_and_trigger_shape(tmp_path):
    sch, *_ = make(tmp_path)
    assert propose_inject(sch, bogus=1)["field"] == "bogus"
    assert propose_inject(sch, action={"type": "inject", "text": "x", "extra": 1})["field"] == "action.extra"
    assert propose_inject(sch, kind="backup")["field"] == "kind"
    assert propose_inject(sch, job_id="Bad Id")["field"] == "job_id"
    assert propose_inject(sch, trigger={"once_at": "2026-10-10T12:05:00Z", "every_minutes": 30})["field"] == "trigger"
    assert propose_inject(sch, trigger={"once_at": "2026-10-10T12:05:00"})["field"] == "trigger.once_at"   # naive time
    assert propose_inject(sch, trigger={"once_at": "tomorrow"})["field"] == "trigger.once_at"
    assert propose_inject(sch, trigger={"times": ["25:00"]})["field"] == "trigger.times"
    assert propose_inject(sch, trigger={"cron": "* * *"})["field"] == "trigger.cron"
    assert propose_inject(sch, trigger={"every_minutes": 0})["field"] == "trigger.every_minutes"
    assert propose_inject(sch, trigger={"once_at": "2026-10-10T05:05:00-07:00"})["ok"] is True       # an explicit offset
    assert sch.detail("remind-me")["proposal"]["schedule"] == "Once at 2026-10-10T12:05:00Z (UTC)"   # stored as UTC


def test_a_one_shot_already_past_the_late_window_is_refused_at_proposal(tmp_path):
    sch, *_ = make(tmp_path)
    assert propose_inject(sch, trigger={"once_at": "2026-10-10T10:00:00Z"})["field"] == "trigger.once_at"
    assert propose_inject(sch, trigger={"once_at": "2026-10-10T11:30:00Z"})["ok"] is True            # 30 min late: allowed


# --- ac-s1-over-limit-refused --------------------------------------------------------------------------------------
def test_the_limits_table_is_one_table_with_the_three_numbers():
    assert LIMITS["max_active_jobs_per_owner"] == 20 and LIMITS["min_fire_interval_s"] == 300
    assert LIMITS["min_inject_interval_s"] == 900 and LIMITS["one_shot_late_window_s"] == 3600
    from gnomon.scheduler import model
    import inspect
    src = inspect.getsource(model)
    assert "LIMITS[" in src and "900" not in src and "300" not in src and "20" not in src.replace("2026", "")


def test_a_twenty_first_active_job_is_refused_naming_the_limit_and_nothing_is_stored(tmp_path):
    sch, *_ = make(tmp_path)
    for i in range(20):
        assert propose_inject(sch, job_id=f"job-{i:02d}", trigger={"every_minutes": 60 + i})["ok"] is True
    r = propose_inject(sch, job_id="job-20", trigger={"every_minutes": 90})
    assert r["ok"] is False and r["limit"] == "max_active_jobs_per_owner" and r["asked"] == 21 and r["allowed"] == 20
    assert not os.path.exists(sch.paths.proposal("job-20"))
    assert propose_inject(sch, job_id="job-03", trigger={"every_minutes": 61}, text="changed")["ok"] is True   # a change is not a 21st
    assert sch.propose_remove("job-04", proposer="agent-a", verified=True)["ok"]                              # withdraw frees a slot
    assert propose_inject(sch, job_id="job-20", trigger={"every_minutes": 90})["ok"] is True
    other, *_ = make(tmp_path / "o", owner="agent-b")                                                      # the limit is per agent
    assert propose_inject(other, "agent-b")["ok"] is True


def test_a_confirmed_unfinished_one_shot_counts_and_a_finished_one_does_not(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    for i in range(20):
        propose_inject(sch, job_id=f"shot-{i:02d}", trigger={"once_at": "2026-10-10T12:30:00Z"})
        confirm(sch, f"shot-{i:02d}")
    assert propose_inject(sch, job_id="one-more")["limit"] == "max_active_jobs_per_owner"
    clock.t = T0 + 1805
    sch.tick()                                            # all twenty fire and are done
    assert propose_inject(sch, job_id="one-more", trigger={"once_at": "2026-10-10T14:00:00Z"})["ok"] is True


@pytest.mark.parametrize("trig", [{"every_minutes": 1}, {"every_minutes": 4}, {"cron": "* * * * *"}, {"cron": "*/2 * * * *"},
                                  {"times": ["06:00", "06:04"]}])
def test_a_job_firing_more_often_than_every_five_minutes_is_refused(tmp_path, trig):
    sch, *_ = make(tmp_path)
    r = propose_inject(sch, trigger=trig)
    assert r["ok"] is False and r["limit"] == "min_fire_interval_s" and r["allowed"] == 300 and r["asked"] < 300
    assert listing(sch.paths.proposals) == []


@pytest.mark.parametrize("trig", [{"every_minutes": 5}, {"every_minutes": 14}, {"cron": "*/10 * * * *"},
                                  {"times": ["06:00", "06:14"]}])
def test_a_recurring_inject_under_fifteen_minutes_is_refused(tmp_path, trig):
    sch, *_ = make(tmp_path)
    r = propose_inject(sch, trigger=trig)
    assert r["ok"] is False and r["limit"] == "min_inject_interval_s" and r["allowed"] == 900
    assert listing(sch.paths.proposals) == []


@pytest.mark.parametrize("trig", [{"every_minutes": 15}, {"cron": "*/15 * * * *"}, {"times": ["06:00", "06:15"]}, {"times": ["06:10"]}])
def test_the_boundary_is_allowed(tmp_path, trig):
    sch, *_ = make(tmp_path)
    assert propose_inject(sch, trigger=trig)["ok"] is True


# --- triggers (ac-trigger-recurring, ac-trigger-dst) ----------------------------------------------------------------
def _utc(y, mo, d, h=0, mi=0):
    return dt.datetime(y, mo, d, h, mi, tzinfo=dt.timezone.utc).timestamp()


def test_times_weekdays_interval_and_cron_next_fires():
    t = schedule.normalize_trigger({"times": ["06:10", "18:30"], "weekdays": ["mon", "fri"]})
    f = schedule.next_fires(t, _utc(2026, 10, 9, 7), 3, tz=dt.timezone.utc)      # Fri 2026-10-09 07:00
    assert f == [_utc(2026, 10, 9, 18, 30), _utc(2026, 10, 12, 6, 10), _utc(2026, 10, 12, 18, 30)]
    i = schedule.next_fires({"every_minutes": 30}, _utc(2026, 10, 9, 7, 10), 2, anchor=_utc(2026, 10, 9, 6, 0))
    assert i == [_utc(2026, 10, 9, 7, 30), _utc(2026, 10, 9, 8, 0)]
    c = schedule.normalize_trigger({"cron": "0 9 * * 1-5"})
    assert schedule.next_fires(c, _utc(2026, 10, 9, 10), 2, tz=dt.timezone.utc) == [_utc(2026, 10, 12, 9), _utc(2026, 10, 13, 9)]
    assert schedule.next_fires(schedule.normalize_trigger({"cron": "30 2 1 * *"}), _utc(2026, 10, 9), 2, tz=dt.timezone.utc) \
        == [_utc(2026, 11, 1, 2, 30), _utc(2026, 12, 1, 2, 30)]


def test_dst_a_nonexistent_time_runs_at_the_next_valid_minute_and_a_repeated_time_runs_once():
    zi = pytest.importorskip("zoneinfo")
    la = zi.ZoneInfo("America/Los_Angeles")
    t = schedule.normalize_trigger({"times": ["02:30"]})
    # 2026-03-08 spring forward: 02:30 does not exist; fires at 03:00 PDT (= 10:00Z), once
    f = schedule.next_fires(t, _utc(2026, 3, 7, 12), 3, tz=la)
    assert f[0] == _utc(2026, 3, 8, 10, 0) and len(set(f)) == 3 and f[1] == _utc(2026, 3, 9, 9, 30)
    # 2026-11-01 fall back: 01:30 happens twice; first occurrence (PDT, 08:30Z) only
    t2 = schedule.normalize_trigger({"times": ["01:30"]})
    g = schedule.next_fires(t2, _utc(2026, 10, 31, 12), 2, tz=la)
    assert g[0] == _utc(2026, 11, 1, 8, 30) and g[1] == _utc(2026, 11, 2, 9, 30)   # once on the repeated day


def test_describe_in_plain_language():
    assert schedule.describe({"times": ["06:10"]}) == "Every day at 06:10 (local time)"
    assert schedule.describe({"every_minutes": 30}) == "Every 30 minutes"
    assert schedule.describe({"once_at": "2026-10-10T14:00:00Z"}) == "Once at 2026-10-10T14:00:00Z (UTC)"


# --- ownership -----------------------------------------------------------------------------------------------------
def test_an_agent_cannot_touch_another_agents_job_or_pending_proposal(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch, "agent-a", "mine")
    # another agent re-using the id is refused, a removal by a non-owner is refused
    r = sch.propose({"job_id": "mine", "title": "t", "trigger": {"every_minutes": 30}, "action": {"type": "inject", "text": "x"}},
                    proposer="agent-b", verified=True)
    assert r["ok"] is False and "another agent" in r["error"]
    assert sch.propose_remove("mine", proposer="agent-b", verified=True)["ok"] is False
    assert sch.proposal_digest("mine") is not None


def test_removing_a_confirmed_job_is_a_proposal_that_needs_the_chs_confirm(tmp_path):
    sch, clock, reg, tgt = make(tmp_path)
    propose_inject(sch, trigger={"every_minutes": 30}); confirm(sch)
    r = sch.propose_remove("remind-me", proposer="agent-a", verified=True)
    assert r["status"] == "pending-confirm"
    assert os.path.exists(sch.paths.job("remind-me")) and os.path.exists(sch.paths.pin("remind-me"))   # still installed
    assert sch.propose_remove("remind-me", proposer="agent-b", verified=True)["ok"] is False
    assert confirm(sch)["status"] == "removed" and not os.path.exists(sch.paths.job("remind-me"))


def test_withdrawing_your_own_pending_proposal_takes_effect_at_once(tmp_path):
    sch, *_ = make(tmp_path)
    propose_inject(sch)
    r = sch.propose_remove("remind-me", proposer="agent-a", verified=True)
    assert r["status"] == "withdrawn" and listing(sch.paths.proposals) == []

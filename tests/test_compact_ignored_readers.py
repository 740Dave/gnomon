"""g146: a test pins, for EACH reader of lifecycle records that decides whether a session was compacted, what
that reader does with the ``compact-ignored`` record (``kind: lifecycle``, ``point: compact-ignored``) the subagent_compaction_check
flag adds (Diamond f77094f finding F2).

Two kinds of test, named in each test's docstring:

* NOT-A-COMPACTION: the ignored record alone is not read as a compaction. Each has a positive control: the SAME records with
  ``point: compact`` in its place ARE read as a compaction, so the test can fail.
* KNOWN-LIMIT: behaviour the Diamond named (F5/F6) and the owner accepted for now, pinned so a change is noticed: a subagent's
  PRE record is ``point: compact`` and is still read as a compaction by the readers that read only ``compact`` records; the relaunch
  readers do not tell compactions apart and see ``compact-ignored`` as "some other lifecycle record that is not an agent-end".

No reader's behaviour is changed by this file. Live-only: a real Claude Code subagent compaction.
"""
from __future__ import annotations

import json
import os

from gnomon import relaunch
from gnomon.harness import compact_gate as cg
from gnomon.memory import last_compaction
from gnomon.scheduler.inject import compaction_state


def _life(point, ts, phase=None, **extra):
    rec = {"kind": "lifecycle", "point": point, "ts": float(ts), "payload": ({"phase": phase} if phase else {})}
    rec.update(extra)
    return rec


def _alert(ts):
    return {"kind": "injection", "trigger": cg.ALERT_TRIGGER, "ts": float(ts)}


def _write_log(path, recs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r) + "\n")


# ---- reader 1: scheduler compaction_state (the original pin, kept here beside the others so the set is in one place)

def test_compaction_state_ignored_record_is_not_a_compaction(tmp_path):
    """NOT-A-COMPACTION. After a subagent's pre (compact/pre), the ignored record makes the state 'clear'; control: without it, 'compacting'."""
    p = str(tmp_path / "injection_log.jsonl")
    _write_log(p, [_life("compact", 100, "pre")])
    assert compaction_state(p, 101.0)[0] == "compacting"                      # control
    _write_log(p, [_life("compact", 100, "pre"), _life("compact-ignored", 105, "post")])
    assert compaction_state(p, 106.0)[0] == "clear"


# ---- reader 2: the 90 percent alert reset

def test_latest_alert_since_last_compact_is_not_reset_by_the_ignored_record():
    """NOT-A-COMPACTION. An alert before the ignored record is still 'the alert since the last compaction'; control: a real compact record resets it."""
    recs = [_alert(10), _life("compact-ignored", 20, "post")]
    assert cg.latest_alert_since_last_compact(recs) == 10.0
    assert cg.latest_alert_since_last_compact([_alert(10), _life("compact", 20, "post")]) is None   # control


# ---- reader 3: confirm-twice window

def _gate(records, now):
    return cg.CompactGate(read_context=lambda: 50.0, records=lambda: records, record=lambda *a, **k: None, clock=lambda: now)


def _refusal(ts):
    return {"kind": "compact-refused", "ts": float(ts), "reasons": [cg.REASON_CONFIRM]}


def test_confirm_twice_first_call_stays_armed_across_the_ignored_record():
    """NOT-A-COMPACTION. The armed first call survives the ignored record; control: a real compact record disarms it."""
    first = _refusal(1000)
    assert _gate([first, _life("compact-ignored", 1005, "post")], 1010)._armed_first_call(
        [first, _life("compact-ignored", 1005, "post")]) is first
    assert _gate([], 1010)._armed_first_call([first, _life("compact", 1005, "post")]) is None   # control


# ---- reader 4: gauge freshness

def test_last_compact_ts_ignores_the_ignored_record():
    """NOT-A-COMPACTION. The gauge-freshness timestamp does not move to the ignored record; control: a compact record moves it."""
    assert cg.last_compact_ts([_life("compact", 5, "pre"), _life("compact-ignored", 9, "post")]) == 5.0
    assert cg.last_compact_ts([_life("compact", 5, "pre"), _life("compact", 9, "post")]) == 9.0   # control


# ---- reader 5: memory.last_compaction (the progress boundary)

def test_memory_last_compaction_does_not_count_the_ignored_record(tmp_path):
    """NOT-A-COMPACTION. One real pre/post pair plus an ignored record is ONE compaction; control: a second compact pair makes two."""
    p = str(tmp_path / ".claude" / "injection_log.jsonl")
    real = [_life("compact", 100, "pre"), _life("compact", 101, "post")]
    _write_log(p, real + [_life("compact-ignored", 200, "post", reason="subagent compaction, ignored")])
    got = last_compaction(p)
    assert got["count"] == 1 and got["phase"] == "post"
    _write_log(p, real + [_life("compact", 199, "pre"), _life("compact", 200, "post")])
    assert last_compaction(p)["count"] == 2                                   # control


# ---- reader 6: relaunch readers

def _session(tmp_path, recs):
    sd = str(tmp_path / "sess")
    _write_log(relaunch.log_path(sd), recs)
    return sd


def test_relaunch_compact_since_does_not_count_the_ignored_record(tmp_path):
    """NOT-A-COMPACTION. compact_since is True only for a compact/post record; control shown."""
    sd = _session(tmp_path, [_life("compact-ignored", 200, "post")])
    assert relaunch.compact_since(sd, 100.0) is False
    sd2 = str(tmp_path / "sess2")
    _write_log(relaunch.log_path(sd2), [_life("compact", 200, "post")])
    assert relaunch.compact_since(sd2, 100.0) is True                         # control


def test_relaunch_last_lifecycle_clean_exit_own_stop_see_the_ignored_record_as_any_other_non_agent_end(tmp_path):
    """KNOWN-LIMIT (documented, unchanged). These readers do not tell compactions apart: the last non-relaunch lifecycle record
    decides, and they need an agent-end. An ignored record after a clean agent-end makes the exit read as NOT clean, exactly as
    a real compact record would (a session still running is not a clean exit)."""
    end = _life("agent-end", 50)
    end["payload"] = {"exit_code": 0, "reason": "test"}
    sd = _session(tmp_path, [end, _life("compact-ignored", 60, "post")])
    last = relaunch.last_lifecycle(sd)
    assert last["point"] == "compact-ignored"
    clean, detail = relaunch.clean_exit(last)
    assert clean is False and "compact-ignored" in detail and "not an agent-end" in detail
    assert relaunch.own_stop_exit(sd) == (False, "last lifecycle record is not an agent-end")
    # same verdict as the real-compact record (so the ignored record introduces no new behaviour for these readers)
    sd2 = str(tmp_path / "sess2")
    _write_log(relaunch.log_path(sd2), [end, _life("compact", 60, "post")])
    assert relaunch.clean_exit(relaunch.last_lifecycle(sd2))[0] is False
    assert relaunch.own_stop_exit(sd2) == (False, "last lifecycle record is not an agent-end")
    # and with the agent-end last, the ignored record before it changes nothing
    sd3 = _session(tmp_path / "x", [_life("compact-ignored", 40, "post"), end])
    assert relaunch.clean_exit(relaunch.last_lifecycle(sd3))[0] is True


# ---- known limit F5: a subagent's PRE is still read as a compaction by the compact-only readers

def test_known_limit_a_subagents_pre_record_still_counts_for_the_compact_only_readers():
    """KNOWN-LIMIT (Diamond F5, pre-existing, accepted for now): the pre cannot know whose compaction it is, so it is logged as
    point compact; the 90% alert reset, confirm-twice window and gauge freshness move with it. The ignored record that follows does not undo that."""
    recs = [_alert(10), _life("compact", 20, "pre"), _life("compact-ignored", 25, "post")]
    assert cg.latest_alert_since_last_compact(recs) is None
    assert cg.last_compact_ts(recs) == 20.0
    first = _refusal(5)
    assert _gate(recs, 22)._armed_first_call([first] + recs[1:]) is None

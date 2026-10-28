"""PTY-hooks WS-C — the dual-idle stall trigger (extends ``maybe_heartbeat``).

Acceptance (spec): both-idle past thresholds fires exactly ONE inject; any
operator OR agent activity re-arms; a post-compact stall is resolved without
external help; one-shot-then-escalate; global default + per-instance override
(incl. ``enabled: false``).
"""

from __future__ import annotations

import pytest

from hooks_support import Rig

from gnomon.harness.constants import DEFAULT_STALL_INJECT, merge_stall_inject
from gnomon.harness.monitor import ContextMonitor, NoticeKind


def _mon(**kw):
    fired, esc = [], []
    base = dict(read_context=lambda: None, heartbeat_interval=10.0, operator_idle_interval=5.0,
                heartbeat_text="RESUME", stall_escalate_after=30.0, rearm_grace=2.0)
    base.update(kw)
    m = ContextMonitor(on_notice=fired.append, on_escalate=esc.append, **base)
    return m, fired, esc


def test_both_idle_fires_exactly_once():
    m, fired, _ = _mon()
    m.note_activity(0.0)
    m.note_operator_activity(0.0)
    for t in range(1, 30):
        m.maybe_heartbeat(float(t))
    assert len(fired) == 1 and fired[0].kind is NoticeKind.HEARTBEAT and fired[0].text == "RESUME"


def test_operator_mid_thought_blocks_the_nudge():
    """The dual-idle GATE: a quiet agent is NOT nudged while the operator is active."""
    m, fired, _ = _mon()
    m.note_activity(0.0)
    for t in range(0, 40):
        m.note_operator_activity(float(t))  # operator keeps typing
        m.maybe_heartbeat(float(t) + 0.5)
    assert fired == []


def test_busy_agent_is_not_nudged():
    m, fired, _ = _mon()
    m.note_operator_activity(0.0)
    for t in range(0, 40):
        m.note_activity(float(t))
        m.maybe_heartbeat(float(t) + 0.5)
    assert fired == []


def test_one_shot_then_escalate_once_and_never_a_second_inject():
    m, fired, esc = _mon()
    m.note_activity(0.0)
    m.note_operator_activity(0.0)
    m.maybe_heartbeat(10.0)            # fire
    m.note_activity(10.1)              # the echo of our own inject (inside grace)
    for t in range(11, 200):
        m.maybe_heartbeat(float(t))
    assert len(fired) == 1
    assert len(esc) == 1 and esc[0]["kind"] == "stall-escalation"


def test_echo_inside_grace_does_not_rearm_but_real_work_after_it_does():
    m, fired, _ = _mon(stall_escalate_after=None)
    m.note_activity(0.0)
    m.note_operator_activity(0.0)
    m.maybe_heartbeat(10.0)
    m.note_activity(10.5)              # echo: no re-arm
    m.maybe_heartbeat(40.0)
    assert len(fired) == 1
    m.note_activity(15.0)              # agent actually worked (after grace) -> re-armed
    m.maybe_heartbeat(15.0 + 20.0 - 0.1)  # backoff: threshold is now 2x = 20s
    assert len(fired) == 1
    m.maybe_heartbeat(15.0 + 20.0)
    assert len(fired) == 2


def test_operator_activity_rearms_and_resets_backoff():
    m, fired, _ = _mon(stall_escalate_after=None)
    m.note_activity(0.0)
    m.note_operator_activity(0.0)
    m.maybe_heartbeat(10.0)
    m.note_operator_activity(12.0)
    m.note_activity(12.0)
    m.maybe_heartbeat(22.0)            # 10s agent-idle, 10s operator-idle, backoff reset -> fires
    assert len(fired) == 2


def test_backoff_is_capped():
    m, fired, _ = _mon(stall_escalate_after=None, stall_backoff_max=4, rearm_grace=0.0)
    t = 0.0
    m.note_operator_activity(0.0)
    gaps = []
    last = 0.0
    m.note_activity(0.0)
    while len(fired) < 5:
        t += 1.0
        if m.maybe_heartbeat(t):
            gaps.append(t - last)
            last = t
            m.note_activity(t)  # agent answers immediately (grace 0) -> new episode
    assert gaps == [10.0, 20.0, 40.0, 40.0, 40.0]


def test_post_compact_uses_the_shorter_threshold_once():
    m, fired, _ = _mon(post_compact_interval=3.0, stall_escalate_after=None, rearm_grace=0.0)
    m.note_activity(0.0)
    m.note_operator_activity(-100.0)
    m.note_compact()
    m.maybe_heartbeat(2.9)
    assert fired == []
    m.maybe_heartbeat(3.0)
    assert len(fired) == 1


def test_legacy_heartbeat_without_operator_interval_is_unchanged():
    m = ContextMonitor(read_context=lambda: None, heartbeat_interval=5.0)
    out = []
    m.set_on_notice(out.append)
    m.note_activity(0.0)
    assert m.stall_mode is False
    assert m.maybe_heartbeat(5.0) is not None
    assert m.maybe_heartbeat(100.0) is None  # once per quiet stretch, no escalation machinery


# -- config: global default + per-instance override --------------------------


def test_per_instance_override_wins_and_can_disable():
    merged = merge_stall_inject({"agent_idle_seconds": 60}, {"agent_idle_seconds": 30, "enabled": False})
    assert merged["agent_idle_seconds"] == 30 and merged["enabled"] is False
    assert merge_stall_inject(None, None) == DEFAULT_STALL_INJECT


@pytest.mark.parametrize("bad", [{"agent_idle_seconds": 0}, {"enabled": "yes"}, {"content": ""},
                                 {"nope": 1}, {"backoff_max": -1}, "x"])
def test_bad_stall_config_fails_loud(bad):
    with pytest.raises(ValueError):
        merge_stall_inject(bad, None)


def test_disabled_instance_never_fires_through_the_proxy(tmp_path):
    rig = Rig(tmp_path, harness={"stall": merge_stall_inject(None, {"enabled": False})})
    try:
        mon = rig.proxy._monitor
        mon.note_activity(0.0)
        mon.note_operator_activity(0.0)
        assert mon.maybe_heartbeat(10_000.0) is None
        assert rig.child_bytes() == b""
    finally:
        rig.close()


def test_proxy_resume_inject_is_non_disruptive_and_attributed(tmp_path):
    stall = merge_stall_inject(None, {"agent_idle_seconds": 1, "operator_idle_seconds": 1,
                                      "content": "PLEASE RESUME"})
    rig = Rig(tmp_path, harness={"stall": stall}, read_context=lambda: 10.0)
    try:
        mon = rig.proxy._monitor
        mon.poll()  # primed: the REPL is up
        mon.note_activity(0.0)
        mon.note_operator_activity(0.0)
        mon.maybe_heartbeat(5.0)
        assert rig.child_bytes() == b"PLEASE RESUME\r"  # no ESC ESC (WS-D)
        rec = [r for r in rig.records("injection") if r["source"] == "resume"]
        assert len(rec) == 1 and rec[0]["clear"] is False and rec[0]["trigger"] == "heartbeat"
    finally:
        rig.close()


def test_escalation_is_recorded_and_shown_to_the_operator(tmp_path):
    stall = merge_stall_inject(None, {"agent_idle_seconds": 1, "operator_idle_seconds": 1,
                                      "escalate_after_seconds": 5})
    rig = Rig(tmp_path, harness={"stall": stall}, read_context=lambda: 10.0)
    try:
        mon = rig.proxy._monitor
        mon.poll()
        mon.note_activity(0.0)
        mon.note_operator_activity(0.0)
        mon.maybe_heartbeat(2.0)
        mon.maybe_heartbeat(7.5)
        esc = rig.records("stall")
        assert len(esc) == 1 and esc[0]["decision"] == "escalate"
        assert b"STALL" in rig.operator_screen()
    finally:
        rig.close()


def test_post_compact_lifecycle_from_the_native_hook_arms_the_short_threshold(tmp_path):
    stall = merge_stall_inject(None, {"agent_idle_seconds": 100, "operator_idle_seconds": 1,
                                      "post_compact_idle_seconds": 2, "content": "PC-RESUME"})
    rig = Rig(tmp_path, harness={"stall": stall}, read_context=lambda: 10.0)
    try:
        mon = rig.proxy._monitor
        mon.poll()
        mon.note_activity(0.0)
        mon.note_operator_activity(0.0)
        rig.proxy.actuator.dispatch({"cmd": "lifecycle", "event": "post-compact", "trigger": "manual"},
                                    origin="self")
        mon.maybe_heartbeat(2.5)
        assert rig.child_bytes() == b"PC-RESUME\r"
    finally:
        rig.close()


# -- measured live 2026-09-27: never nudge before the REPL is up ----------------


def test_stall_never_fires_before_the_repl_is_up():
    """An un-gated resume CR answered Claude Code's startup trust dialog with its
    default ("No, exit") on a live launch. require_ready holds the trigger until
    the monitor has primed on a real context reading."""
    reading = {"v": None}
    m, fired, _ = _mon(require_ready=True, stall_escalate_after=None)
    m._read_context = lambda: reading["v"]
    m.note_activity(0.0)
    m.note_operator_activity(0.0)
    m.poll()
    for t in range(1, 60):
        m.maybe_heartbeat(float(t))
    assert fired == []                      # dialog on screen: no reading, no nudge
    reading["v"] = 3.0
    m.poll()                                # REPL up: statusline wrote a reading -> primed
    m.maybe_heartbeat(100.0)
    assert len(fired) == 1


def test_the_assembled_harness_requires_ready(tmp_path):
    stall = merge_stall_inject(None, {"agent_idle_seconds": 1, "operator_idle_seconds": 1})
    rig = Rig(tmp_path, harness={"stall": stall})  # read_context -> None: never primed
    try:
        mon = rig.proxy._monitor
        mon.note_activity(0.0)
        mon.note_operator_activity(0.0)
        assert mon.maybe_heartbeat(1000.0) is None and rig.child_bytes() == b""
    finally:
        rig.close()

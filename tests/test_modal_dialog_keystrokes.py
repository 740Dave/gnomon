"""Dialog-keystroke rule (architect ruling 2026-10-03, F1 side effect): keys the operator types WHILE a
dialog is open belong to the dialog. On dialog-open the gate snapshots the input tap's line state; on
dialog-close it restores it, so exactly the dialog-time keys are discarded and a half-typed line from
before the dialog survives. Each behavioural test fails on d0408f8 (the answer's digit counted as a busy line).
"""

from __future__ import annotations

import time

import pytest

from hooks_support import Rig

from test_modal_hold_choke_point import _bus, child, close_dialog, open_dialog, pump


@pytest.fixture
def rig(tmp_path):
    # the replay margin is small here so the mechanics tests run fast: _settle() (0.08 s) puts the answer
    # key OUTSIDE it. The PRODUCTION margin (2 s) is exercised by tests/test_modal_close_margin.py.
    r = Rig(tmp_path, harness={"dsd_notice_boot_grace_seconds": 0.0, "dsd_notice_operator_quiet_seconds": 0.0,
                               "modal_close_margin_seconds": 0.05})
    yield r
    r.close()


def _recs(rig, what):
    return [r for r in rig.records("modal-guard") if r.get("what") == what]


def _tap(rig):
    return rig.proxy.operator_tap


def test_a_half_typed_line_from_before_the_dialog_survives_and_the_answer_does_not_count(rig):
    rig.type(b"abc")
    pump(rig)                                  # observed closed, line partial
    open_dialog(rig)
    pump(rig)                                  # dialog seen: snapshot taken
    _bus(rig, "HELD-ITEM")
    rig.type(b"2")                             # the answer, no Enter
    child(rig)
    _settle()
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).line_state == "partial"
    assert _tap(rig).unsent_text() == "abc"    # exactly the pre-dialog text
    assert b"HELD-ITEM" not in child(rig)      # busy line: the item waits
    assert len(rig.gate.held()) == 1
    assert _recs(rig, "line-restored-after-dialog")


def test_b_empty_line_dialog_answer_without_enter_releases_the_held_item(rig):
    pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-ITEM")
    rig.type(b"2")
    child(rig)
    _settle()
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).line_state == "empty"
    pump(rig)
    assert child(rig).count(b"HELD-ITEM") == 1
    assert not rig.gate.held()


def test_c_answer_with_arrows_and_enter(rig):
    pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-ITEM")
    rig.type(b"\x1b[B\x1b[B\r")                # down, down, Enter: the dialog's own keys
    child(rig)
    _settle()
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).line_state == "empty"
    pump(rig)
    assert child(rig).count(b"HELD-ITEM") == 1


def test_c2_typed_text_during_the_dialog_with_enter_then_pre_dialog_line_still_survives(rig):
    rig.type(b"abc")
    pump(rig)
    open_dialog(rig)
    pump(rig)
    rig.type(b"zzz\r")                         # typed into the dialog (e.g. a free-text option)
    child(rig)
    _settle()
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).unsent_text() == "abc"


def test_first_look_at_an_open_dialog_with_a_pristine_tap_still_snapshots(rig):
    open_dialog(rig)                           # open at the very first observation, operator has typed nothing
    pump(rig)
    _bus(rig, "HELD-ITEM")
    rig.type(b"2")
    child(rig)
    _settle()
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).line_state == "empty"
    pump(rig)
    assert child(rig).count(b"HELD-ITEM") == 1


def test_snapshot_unavailable_when_first_seen_open_after_the_operator_typed_keeps_the_conservative_behaviour(rig):
    rig.type(b"abc")                           # operator input before the guard's first observation
    open_dialog(rig)
    pump(rig)
    assert _recs(rig, "line-snapshot-unavailable-started-mid-dialog")
    _bus(rig, "HELD-ITEM")
    rig.type(b"2")
    child(rig)
    _settle()
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).line_state == "partial"   # today's behaviour: the digit still counts
    assert b"HELD-ITEM" not in child(rig)
    assert not _recs(rig, "line-restored-after-dialog")


def test_a_screen_only_hold_never_discards_keys(rig):
    """A screen-marker-only hold may be a quoted marker: his keys there are real and must still count."""
    from test_modal_hold_choke_point import ANSI_PERM, _append, _row, paint

    pump(rig)
    paint(rig, ANSI_PERM)                      # screen leg only, no transcript tool_use
    pump(rig)
    rig.type(b"real typing")
    child(rig)
    g = rig.gate
    # force the hint to lapse without any transcript confirmation
    guard_owner = g.modal_check.__self__
    guard_owner._hint = None
    pump(rig)
    assert _tap(rig).unsent_text() == "real typing"
    assert not _recs(rig, "line-restored-after-dialog")


def test_d_flag_off_is_unchanged(tmp_path):
    r = Rig(tmp_path, harness={"modal_hold": False, "dsd_notice_boot_grace_seconds": 0.0,
                               "dsd_notice_operator_quiet_seconds": 0.0})
    try:
        assert r.gate.modal_snapshot is None and r.gate.modal_restore is None
        open_dialog(r)
        pump(r)
        r.type(b"2")
        close_dialog(r)
        pump(r)
        assert _tap(r).line_state == "partial"   # nothing snapshots or restores
    finally:
        r.close()


def test_tap_snapshot_roundtrip_is_a_copy():
    from gnomon.harness.optap import OperatorInputTap

    t = OperatorInputTap(writer=None, gate=None, stdout_fd=None, command_plane=False)
    snap = t.snapshot_line()
    t._line.extend(b"xyz")
    assert t.line_state == "partial"
    t.restore_line_snapshot(snap)
    assert t.line_state == "empty"
    t._line.extend(b"q")
    t.restore_line_snapshot(snap)               # restoring twice from the same snapshot still works
    assert t.line_state == "empty"


# ---- D1 (Diamond delta 2026-10-03): keys typed AFTER the dialog closed must survive the restore ----------
# The restore discards only keys typed while the dialog was open. The close boundary is the closing
# tool_result row's own transcript timestamp (mapped onto the monotonic clock), never the moment the guard
# noticed the close. Without a timestamp the line stays BUSY.

def _settle():
    time.sleep(0.08)    # answer -> row stamp gap > the 0.05 s test margin; and typed-after-row strictly later


def _held_msg_typed_onto_the_line(rig):
    """Every byte the child received while 'HELD-MSG' was typed: it must never follow other typed text."""
    return child(rig)


def test_d1_probe_a_key_typed_after_close_before_the_tick_is_not_typed_over(rig):
    pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"2")                             # the dialog's answer
    child(rig)
    _settle()
    close_dialog(rig)
    _settle()
    rig.type(b"h")                             # a new thought, after the real close, before any tick
    pump(rig)
    got = child(rig)
    assert b"HELD-MSG" not in got, got          # never 'hHELD-MSG\r'
    assert _tap(rig).line_state == "partial" and _tap(rig).unsent_text() is None  # round 4 (F2): replay within the ambiguity window: text not claimed
    assert len(rig.gate.held()) == 1
    rig.type(b"\r")                            # he submits his own line
    child(rig)
    pump(rig)
    assert child(rig).count(b"HELD-MSG") == 1   # now the held item goes, once
    assert not rig.gate.held()


def test_d1_probe_b_real_pump_order_hello_survives(rig):
    import os

    pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"2")
    child(rig)
    _settle()
    close_dialog(rig)
    _settle()
    os.write(rig.stdin_w, b"hello")             # readable stdin AND an unobserved close in ONE step
    rig.proxy.step(timeout=0.05)                # tap.feed runs before the tickers in the same step
    got = child(rig)
    assert b"HELD-MSG" not in got, got
    assert _tap(rig).unsent_text() is None  # round 4 (F2): replay within the ambiguity window: text not claimed
    assert len(rig.gate.held()) == 1


def test_d1_fast_answer_within_two_seconds_of_the_dialog_opening(rig):
    """The screen hint lingers screen_window (2 s) after the real close: a key typed in that linger is
    still after the close, and survives the eventual close edge."""
    from test_modal_hold_choke_point import ANSI_ASK, paint

    pump(rig)
    open_dialog(rig)
    paint(rig, ANSI_ASK)                        # painted: the screen hint now keeps the guard open
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"1")                              # answered at once
    child(rig)
    _settle()
    close_dialog(rig)
    _settle()
    rig.type(b"so")                             # inside the 2 s linger
    pump(rig)
    assert b"HELD-MSG" not in child(rig)
    guard = rig.gate.modal_check.__self__
    guard._hint = None                          # the linger ends: the close edge is finally observed
    pump(rig)
    assert _tap(rig).unsent_text() is None  # round 4 (F2): replay within the ambiguity window: text not claimed
    assert b"HELD-MSG" not in child(rig)
    assert len(rig.gate.held()) == 1


def test_d1_keys_before_during_and_after_one_dialog_in_one_sequence(rig):
    rig.type(b"abc")                           # before
    pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"2")                             # during (the answer)
    child(rig)
    _settle()
    close_dialog(rig)
    _settle()
    rig.type(b"xyz")                           # after
    pump(rig)
    assert _tap(rig).unsent_text() is None  # round 4 (F2): replay within the ambiguity window: text not claimed
    assert b"HELD-MSG" not in child(rig)
    rig.type(b"\x15")                          # Ctrl-U: he clears his line
    child(rig)
    pump(rig)
    assert child(rig).count(b"HELD-MSG") == 1


def test_d1_replay_has_no_side_effects_a_submitted_line_after_close_is_forwarded_once(rig):
    pump(rig)
    open_dialog(rig)
    pump(rig)
    rig.type(b"2")
    child(rig)
    _settle()
    close_dialog(rig)
    _settle()
    before = len([r for r in rig.records("injection") if r.get("trigger") == "operator-submit"])
    rig.type(b"hello\r")                       # typed AFTER the close, submitted live (once)
    got = child(rig)
    pump(rig)
    got += child(rig)
    assert got.count(b"hello\r") == 1
    after = len([r for r in rig.records("injection") if r.get("trigger") == "operator-submit"])
    assert after == before + 1                  # the replay recorded nothing
    assert _tap(rig).line_state == "empty"


def test_d1_boundary_unknown_keeps_the_line_busy(rig):
    pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"2")
    child(rig)
    _settle()
    close_dialog(rig, ts=False)                # the closing row carries no timestamp
    pump(rig)
    assert _tap(rig).line_state == "partial"   # conservative: BUSY, never restored over
    assert b"HELD-MSG" not in child(rig)
    assert len(rig.gate.held()) == 1
    assert _recs(rig, "line-restore-skipped-close-time-unknown")
    assert not _recs(rig, "line-restored-after-dialog")


def test_d1_snapshot_replay_unit_on_a_bare_tap():
    from gnomon.harness.optap import OperatorInputTap

    t = OperatorInputTap(writer=None, gate=None, stdout_fd=None, command_plane=False)
    t._forward = lambda d: None
    t.feed(b"abc")
    snap = t.snapshot_line()
    time.sleep(0.01)
    t.feed(b"2")                               # during
    time.sleep(0.01)
    boundary = time.monotonic()
    time.sleep(0.01)
    t.feed(b"xy")                              # after
    n = t.restore_line_snapshot(snap, boundary, boundary)
    # "xy" was typed within AMBIGUOUS_AFTER_STAMP of the stamp: it may be the dialog's, so the text is
    # not claimed (round 4, F2); the unambiguous case (stamp well before the keys) still reads "abcxy"
    assert n == 1 and t.unsent_text() is None
    assert t.restore_line_snapshot(snap, boundary, boundary - 5.0) == 1 and t.unsent_text() == "abcxy"
    assert t.restore_line_snapshot(snap) == 0 and t.unsent_text() == "abc"   # no `since`: no replay


def test_d6_approve_into_dialog_is_not_parsed_when_the_flag_is_off(tmp_path):
    r = Rig(tmp_path, harness={"modal_hold": False, "dsd_notice_boot_grace_seconds": 0.0,
                               "dsd_notice_operator_quiet_seconds": 0.0})
    try:
        r.proxy.operator_tap.execute_command("\\pause")
        _bus(r, "HELD-ITEM")
        assert len(r.gate.held()) == 1
        res = r.proxy.operator_tap.execute_command("\\approve --into-dialog")
        assert "approved nothing" in res["reply"], res     # not parsed: an unknown selector, as before the hold
        assert len(r.gate.held()) == 1
        assert b"HELD-ITEM" not in child(r)
    finally:
        r.close()

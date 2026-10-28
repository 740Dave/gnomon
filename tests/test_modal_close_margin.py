"""Modal-hold round 3 (architect ruling 2026-10-03): EVERY error direction of the dialog-close restore is
conservative. A held item may WAIT; it must NEVER be typed over the operator's text.

These run at the PRODUCTION replay margin (``modal_prompt.CLOSE_MARGIN_SECONDS``, 2 s), on the real
``assemble_proxy`` object graph. The scenarios are the Diamond-delta-2 audit's own reproductions
(``audit-modal-hold-delta2-2026-10-03.md``: E1 unknown boundary, E2 late stamp 5 ms / 20 ms / 100 ms / 1 s,
E4 early stamp with an Enter-terminated answer, E6 the g79 cases), each of which FAILS on a7a055a.

``HELD-MSG`` reaching the child while operator text is in its prompt is a TYPE-OVER (the test failure).
The cases that are NOT conservative are listed as ``test_limit_*`` and assert what really happens.
"""

from __future__ import annotations

import datetime
import time

import pytest

from hooks_support import Rig

from test_modal_hold_choke_point import (ANSI_PERM, _append, _bus, _row, child, close_dialog, open_dialog, paint,
                                         pump)

ENTER_ANSWER = b"\x1b[B\r"          # down, Enter: the dialog's own keys, ending in Enter


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat(timespec="milliseconds")


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, harness={"dsd_notice_boot_grace_seconds": 0.0, "dsd_notice_operator_quiet_seconds": 0.0})
    yield r
    r.close()


def _tap(rig):
    return rig.proxy.operator_tap


def _close_with(rig, stamp):
    """Append the closing row with a chosen timestamp: epoch seconds, a raw string, or None (no stamp)."""
    row = _row("result", "q1", ts=False)
    if stamp is not None:
        row["timestamp"] = stamp if isinstance(stamp, str) else iso(stamp)
    _append(rig, row)


def _after(stamp):
    """Let the clock pass the stamp: a real closing row is stamped BEFORE the guard reads it (a stamp ahead of
    the guard's clock is the 'future' case, which is unknown -> BUSY, tested separately)."""
    time.sleep(max(0.0, stamp - time.time()) + 0.01)


def _typed_over(got):
    return b"HELD-MSG" in got


def _ready(rig, pre=b""):
    pump(rig)
    if pre:
        rig.type(pre)
        pump(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")


def _settle_and_check_held(rig):
    pump(rig)
    got = child(rig)
    pump(rig)
    got += child(rig)
    return got


def _then_ctrl_u_delivers_once(rig):
    rig.type(b"\x15")
    child(rig)
    pump(rig)
    assert child(rig).count(b"HELD-MSG") == 1
    assert not rig.gate.held()
    assert _tap(rig).forced_busy is None


# ---- E2: the closing row's stamp LATE (it trails the real close by Claude Code's row-write latency) -------

@pytest.mark.parametrize("pre", [b"", b"abc"], ids=["empty-line", "pre-dialog-abc"])
@pytest.mark.parametrize("lag", [0.005, 0.02, 0.1, 1.0], ids=["5ms", "20ms", "100ms", "1s"])
def test_e2_a_key_typed_after_the_real_close_but_before_a_late_stamp_is_never_typed_over(rig, pre, lag):
    _ready(rig, pre)
    rig.type(b"2")                               # the dialog's answer (a digit)
    child(rig)
    time.sleep(0.2)
    t = time.time()
    rig.type(b"h")                               # a new thought, after the real close
    _close_with(rig, t + lag)                    # ... but the row is stamped `lag` later
    _after(t + lag)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1
    assert _tap(rig).line_state == "partial"
    rig.type(b"\x15")                            # he clears his line (Ctrl-U): the item goes, once
    child(rig)
    pump(rig)
    assert child(rig).count(b"HELD-MSG") == 1


@pytest.mark.parametrize("lag", [0.005, 0.02, 0.1, 1.0], ids=["5ms", "20ms", "100ms", "1s"])
def test_e2_enter_terminated_answer_pre_dialog_text_late_stamp(rig, lag):
    _ready(rig, b"abc")
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.2)
    t = time.time()
    rig.type(b"h")
    _close_with(rig, t + lag)
    _after(t + lag)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1


# ---- E4: the stamp EARLY, the answer ending in Enter, pre-dialog text -------------------------------------

@pytest.mark.parametrize("early", [0.5, 0.05, 0.002, 0.0], ids=["500ms", "50ms", "2ms", "equal"])
def test_e4_early_stamp_with_an_enter_answer_and_pre_dialog_text_is_busy_not_typed_over(rig, early):
    _ready(rig, b"abc")
    ta = time.time()
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.15)
    _close_with(rig, ta - early)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1
    assert _tap(rig).line_state == "partial"
    _then_ctrl_u_delivers_once(rig)


@pytest.mark.parametrize("early", [1.0, 0.15, 0.05, 0.001], ids=["1s", "150ms", "50ms", "1ms"])
def test_early_stamp_digit_answer_is_busy_not_typed_over(rig, early):
    _ready(rig, b"")
    rig.type(b"2")
    child(rig)
    time.sleep(0.2)
    t = time.time()
    rig.type(b"h")
    _close_with(rig, t - early)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1


# ---- E1: an unknown close boundary marks the line explicitly BUSY ---------------------------------------

UNKNOWN = {"absent": None, "garbage": "not-a-date", "empty-string": "", "future-1h": "FUTURE"}


@pytest.mark.parametrize("answer", [b"2", ENTER_ANSWER], ids=["digit-answer", "enter-answer"])
@pytest.mark.parametrize("pre", [b"", b"abc"], ids=["empty-line", "pre-dialog-abc"])
@pytest.mark.parametrize("which", sorted(UNKNOWN))
def test_e1_unknown_boundary_is_busy_and_never_typed_over(rig, which, pre, answer):
    stamp = UNKNOWN[which]
    if stamp == "FUTURE":
        stamp = iso(time.time() + 3600)
    _ready(rig, pre)
    rig.type(answer)
    child(rig)
    _close_with(rig, stamp)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1
    assert _tap(rig).line_state == "partial"                 # explicitly BUSY, whatever the tap's buffer says
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    assert [r for r in rig.records("modal-guard") if r.get("what") == "line-restore-skipped-close-time-unknown"]
    _then_ctrl_u_delivers_once(rig)


def test_e1_forced_busy_is_cleared_by_his_next_enter(rig):
    _ready(rig, b"abc")
    rig.type(ENTER_ANSWER)
    child(rig)
    _close_with(rig, None)
    pump(rig)
    assert _tap(rig).forced_busy
    rig.type(b"\r")                                           # his own Enter
    child(rig)
    pump(rig)
    assert _tap(rig).forced_busy is None
    assert child(rig).count(b"HELD-MSG") == 1


def test_e1_forced_busy_is_cleared_by_approve_into_dialog(rig):
    _ready(rig, b"abc")
    rig.type(ENTER_ANSWER)
    child(rig)
    _close_with(rig, None)
    pump(rig)
    assert _tap(rig).forced_busy and len(rig.gate.held()) == 1
    res = _tap(rig).execute_command("\\approve --into-dialog")
    assert "approved" in res["reply"]
    assert _tap(rig).forced_busy is None
    pump(rig)
    assert child(rig).count(b"HELD-MSG") == 1


def test_forced_busy_is_not_cleared_by_ordinary_typing_or_a_replay(rig):
    _ready(rig, b"abc")
    rig.type(ENTER_ANSWER)
    child(rig)
    _close_with(rig, None)
    pump(rig)
    rig.type(b"more")                                         # typing is not an Enter
    child(rig)
    pump(rig)
    assert _tap(rig).forced_busy and b"HELD-MSG" not in child(rig)


def test_a_future_stamp_within_the_clock_tolerance_is_taken_as_now_not_unknown(rig):
    _ready(rig, b"")
    rig.type(b"2")
    child(rig)
    time.sleep(0.1)
    _close_with(rig, time.time() + 0.1)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got)
    assert _tap(rig).forced_busy is None                      # a usable (clamped) stamp, not unknown


# ---- E6: the g79 cases, Enter-terminated answer with pre-dialog text ------------------------------------

def test_e6_first_look_at_an_already_open_dialog_after_the_operator_typed(rig):
    rig.type(b"abc")                                          # typed before the guard's first observation
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.1)
    close_dialog(rig)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1
    _then_ctrl_u_delivers_once(rig)


def test_e6_keys_typed_before_the_open_edge_was_observed(rig):
    pump(rig)
    rig.type(b"abc")
    pump(rig)
    open_dialog(rig)                                          # the dialog's row is written ...
    rig.type(ENTER_ANSWER)                                    # ... he answers before the guard polls
    child(rig)
    pump(rig)                                                 # first sight of an already-answered-keys dialog
    _bus(rig, "HELD-MSG")
    time.sleep(0.1)
    close_dialog(rig)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1
    _then_ctrl_u_delivers_once(rig)


def test_e6_open_row_without_a_stamp_treats_recent_keys_as_ambiguous(rig):
    pump(rig)
    rig.type(b"abc")
    pump(rig)
    open_dialog(rig, ts=False)
    rig.type(ENTER_ANSWER)
    child(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    time.sleep(0.1)
    close_dialog(rig)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1


def test_e6_screen_only_hold_with_pre_dialog_text(rig):
    pump(rig)
    rig.type(b"abc")
    pump(rig)
    paint(rig, ANSI_PERM)                                     # screen leg only, no transcript tool_use
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"\r")                                           # he answers the permission prompt with Enter
    child(rig)
    rig.gate.modal_check.__self__._hint = None                # the hold lapses (no transcript confirmation)
    pump(rig)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1


def test_e6_open_and_close_between_two_polls_with_pre_dialog_text(rig):
    """The guard never saw the dialog open (both rows landed before one poll): nothing was held, but a
    later injection that honours the operator's line (a DSD-answer notice) must not be typed onto the
    pre-dialog text the dialog's Enter hid from the tap. (A hub ``bus`` inject does not consult the line at
    all, with or without a dialog: pre-existing, outside this item.)"""
    from test_modal_hold_choke_point import _dsd

    pump(rig)
    rig.type(b"abc")
    pump(rig)
    open_dialog(rig)
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.05)
    close_dialog(rig)
    pump(rig)
    _dsd(rig, "")
    got = _settle_and_check_held(rig)
    assert b"DEC-DSD-1A2B3C4D5E1" not in got, got
    assert _tap(rig).forced_busy == "dialog-opened-and-closed-unseen"
    rig.type(b"\x15")                                         # Ctrl-U clears his line; the notice then arrives
    child(rig)
    pump(rig)
    assert child(rig).count(b"DEC-DSD-1A2B3C4D5E1") == 1


# ---- the limits that are NOT conservative (assert what really happens; g79 lists them) -------------------

def test_limit_a_key_typed_more_than_the_margin_before_a_late_stamp_is_lost(rig):
    """The replay starts at stamp - margin (2 s). A key typed after the real close but MORE than the margin
    before the row's stamp is outside the window: it is not replayed and the held item can be typed over it.
    The measured answer-to-row latency is ~0.12 s at most, so this needs a stamp ~17x slower than measured."""
    _ready(rig, b"")
    rig.type(b"2")
    child(rig)
    time.sleep(0.2)
    t = time.time()
    rig.type(b"h")
    _close_with(rig, t + 2.1)                                  # 0.1 s beyond the 2 s margin
    _after(t + 2.1)
    got = _settle_and_check_held(rig)
    assert _typed_over(got)                                    # NOT conservative: documented limit L1


# ---- bounds the replay cannot honour are BUSY, never silent ----------------------------------------------

def test_a_full_key_journal_that_lost_the_replay_window_marks_the_line_busy():
    from gnomon.harness.optap import OperatorInputTap

    t = OperatorInputTap(writer=None, gate=None, stdout_fd=None, command_plane=False)
    t._forward = lambda d: None
    snap = t.snapshot_line()
    for _ in range(t._key_journal.maxlen + 5):          # more chunks than the journal holds
        t.feed(b"x")
    t.restore_line_snapshot(snap, snap["_snap_at"], time.monotonic())
    assert t.forced_busy == "journal-overflow" and t.line_state == "partial"


def test_e5_a_command_typed_in_the_observation_lag_after_a_digit_answer_replays_as_the_child_saw_it(rig):
    """Audit E5: the live tap held the answer digit, so a ``\\appr`` typed next was TEXT for the child. The
    replay now includes the digit (it is inside the margin), so it rebuilds the same state, not COMMAND mode."""
    _ready(rig, b"")
    rig.type(b"2")
    child(rig)
    time.sleep(0.1)
    rig.type(b"\\appr")
    seen = child(rig)
    assert b"\\appr" in seen                            # the child's prompt holds it
    close_dialog(rig)
    pump(rig)
    assert _tap(rig).line_state == "partial"            # consistent with the child, never COMMAND mode
    assert b"HELD-MSG" not in child(rig)


def test_limit_a_stamp_more_than_1s_before_the_answer_with_an_enter_answer_and_pre_dialog_text_is_typed_over(rig):
    """A closing row stamped >1 s BEFORE the answer's Enter (the row cannot be written before the dialog
    resolves; this needs a clock step or a wrong wall/monotonic mapping) puts the answer outside both the
    ambiguity windows: the replay re-processes the dialog's own Enter as the operator's and the held item is
    typed over the pre-dialog text. NOT conservative: documented limit L2 (inferred hazard: macOS monotonic
    time stops during sleep)."""
    _ready(rig, b"abc")
    ta = time.time()
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.15)
    _close_with(rig, ta - 1.5)
    got = _settle_and_check_held(rig)
    assert _typed_over(got)                                    # NOT conservative: limit L2


def test_enter_answer_with_an_empty_pre_dialog_line_releases_at_the_production_margin(rig):
    """No pre-dialog text and an Enter-terminated answer: the replay leaves the line empty and the held item is
    released with no delay (the digit-answer delay is the price of the margin; this case does not pay it)."""
    _ready(rig, b"")
    ta = time.time()
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.15)
    _close_with(rig, ta + 0.1)
    pump(rig)
    pump(rig)
    assert _tap(rig).line_state == "empty" and _tap(rig).forced_busy is None
    assert child(rig).count(b"HELD-MSG") == 1


# ==== Round 4 (architect ruling 2026-10-03, Diamond delta-3 F1/F2/F3/F8) ==================================

def _forced_busy_setup(rig):
    """The delta-3 E1 case: pre-dialog ``abc``, held item, Enter-terminated answer, closing row with no stamp
    -> forced busy (the child's prompt may still hold ``abc``)."""
    _ready(rig, b"abc")
    rig.type(ENTER_ANSWER)
    child(rig)
    _close_with(rig, None)
    pump(rig)
    got = child(rig)
    pump(rig)
    got += child(rig)
    assert not _typed_over(got), got
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    assert len(rig.gate.held()) == 1


@pytest.mark.parametrize("cmd", ["\\approve", "\\resume", "\\status", "\\help"])
def test_f1_a_swallowed_operator_command_never_clears_forced_busy(rig, cmd):
    _forced_busy_setup(rig)
    rig.type(cmd.encode() + b"\r")
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got                          # the child's prompt still holds abc
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    assert len(rig.gate.held()) == 1
    assert _tap(rig).line_state == "partial" and _tap(rig).unsent_text() is None
    _then_ctrl_u_delivers_once(rig)                           # a keystroke that reaches the child still releases


def test_f1_command_mode_ctrl_c_and_sigil_backspace_do_not_clear_forced_busy(rig):
    _forced_busy_setup(rig)
    rig.type(b"\\appr\x03")                                   # Ctrl-C inside a \command: swallowed
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    rig.type(b"\\\x7f")                                       # sigil then Backspace over it: swallowed
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1


@pytest.mark.parametrize("key", [b"\r", b"\x15", b"\x03", b"\x1b"], ids=["enter", "ctrl-u", "ctrl-c", "esc"])
def test_f1_keystrokes_that_reach_the_childs_prompt_do_clear_it(rig, key):
    _forced_busy_setup(rig)
    rig.type(key)
    child(rig)
    pump(rig)
    assert _tap(rig).forced_busy is None
    assert child(rig).count(b"HELD-MSG") == 1 and not rig.gate.held()


def test_f1_approve_into_dialog_clears_it(rig):
    _forced_busy_setup(rig)
    _tap(rig).execute_command("\\approve --into-dialog")
    assert _tap(rig).forced_busy is None


# ---- F2: unsent_text() claims nothing for an ambiguous replay or a forced-busy line --------------------------

def test_f2_unsent_text_is_unknown_after_an_ambiguous_replay(rig):
    _ready(rig, b"abc")
    rig.type(b"2")                                            # the dialog's digit answer, inside the margin
    child(rig)
    time.sleep(0.2)
    t = time.time()
    rig.type(b"h")
    _close_with(rig, t + 0.02)
    _after(t + 0.02)
    _settle_and_check_held(rig)
    tap = _tap(rig)
    assert tap.line_state == "partial"
    assert tap.unsent_text() is None, tap.unsent_text()       # was 'abc2h' on 6e7aa21
    rig.type(b"\x15")                                         # a live reset ends the doubt
    assert tap.unsent_text() == ""


def test_f2_compact_takeover_types_nothing_over_an_unknown_line(rig, tmp_path):
    """The D50 takeover with unsent_text() == None: refuses (operator-line-busy), saves nothing, clears and
    types nothing."""
    from gnomon.harness import compact_gate as cg

    _forced_busy_setup(rig)
    tap = _tap(rig)
    calls = []

    class Ctl:
        def line_state(self):
            return tap.line_state

        def idle_seconds(self):
            return 9999.0

        def unsent_text(self):
            return tap.unsent_text()

        def clear_unsent(self):
            calls.append("clear")

        def restore_unsent(self, text):
            calls.append(("restore", text))

    gate = cg.CompactGate(read_context=lambda: 95.0, records=lambda: [], record=lambda *a, **k: None,
                          line_hold=lambda: "partial", line_control=Ctl(), session_dir=str(tmp_path))
    v = gate.check()
    assert v.allowed is False and v.reasons == ["operator-line-busy"]
    assert calls == [] and not (tmp_path / cg.SAVED_LINE_FILENAME).exists()


# ---- F3: the three mutation survivors ------------------------------------------------------------------------

def test_f3_guard_iv_screen_only_hold_with_text_typed_long_before_the_open(rig):
    """pre-dialog text typed MORE than the 2 s margin before the hold, so guard (iii) cannot cover it: only
    guard (iv) ``line-marked-busy-no-snapshot`` keeps the held item from being typed over it."""
    pump(rig)
    rig.type(b"abc")
    pump(rig)
    time.sleep(2.3)
    paint(rig, ANSI_PERM)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(b"\r")
    child(rig)
    rig.gate.modal_check.__self__._hint = None
    pump(rig)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert (_tap(rig).forced_busy or "").startswith("dialog-closed-without-snapshot-line-was-")
    assert len(rig.gate.held()) == 1


def test_f3_guard_iv_first_look_with_text_typed_long_before_the_open(rig):
    rig.type(b"abc")
    time.sleep(2.3)
    open_dialog(rig)
    pump(rig)
    _bus(rig, "HELD-MSG")
    rig.type(ENTER_ANSWER)
    child(rig)
    time.sleep(0.1)
    close_dialog(rig)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert len(rig.gate.held()) == 1


def test_f3_a_replay_never_clears_forced_busy(rig):
    tap = _tap(rig)
    rig.type(b"abc")
    tap.force_busy("x")
    snap = tap.snapshot_line()
    time.sleep(0.01)
    rig.type(b"\r")                                           # live Enter: reaches the child, clears the live mark
    assert tap.forced_busy is None
    assert tap.restore_line_snapshot(snap, snap["_snap_at"]) == 1    # the replay re-processes that Enter (dry)
    assert tap.forced_busy == "x"                             # ... and must not clear the carried mark


def test_f3_unsent_text_is_none_while_forced_busy(rig):
    tap = _tap(rig)
    rig.type(b"abc")
    assert tap.unsent_text() == "abc"
    tap.force_busy("x")
    assert tap.unsent_text() is None


# ---- F8: a naive ISO stamp has no zone: unknown -> BUSY --------------------------------------------------------

def test_f8_naive_iso_stamp_is_unknown_and_forces_busy(rig):
    from gnomon.harness.modal_prompt import _row_wall_time

    assert _row_wall_time({"timestamp": "2026-10-03T01:02:03.500"}) is None
    assert _row_wall_time({"timestamp": "2026-10-03T01:02:03.500Z"}) is not None
    assert _row_wall_time({"timestamp": "2026-10-03T01:02:03.500+00:00"}) is not None
    _ready(rig, b"abc")
    rig.type(ENTER_ANSWER)
    child(rig)
    naive = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None).isoformat(timespec="milliseconds")
    _close_with(rig, naive)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"


def test_f7_margin_comment_matches_value():
    import inspect
    from gnomon.harness import modal_prompt as mp

    src = inspect.getsource(mp)
    assert mp.CLOSE_MARGIN_SECONDS == 2.0
    assert "M = max(0.5 s, 4 x measured max)" not in src


# ==== Round 5 (architect, Diamond delta-4 F1b / F6) ==========================================================

@pytest.fixture
def deny_rig(tmp_path):
    r = Rig(tmp_path, harness={"dsd_notice_boot_grace_seconds": 0.0, "dsd_notice_operator_quiet_seconds": 0.0,
                               "hook_manifest": {"inbound-injection": [
                                   {"kind": "deny_all", "sources": ["operator"], "commands": ["/compact"],
                                    "name": "no-compact"}]}})
    yield r
    r.close()


def test_f1b_a_hook_denied_slash_submit_never_clears_forced_busy_or_lets_a_held_item_type_over(deny_rig):
    rig = deny_rig
    _forced_busy_setup(rig)
    rig.type(b"/compact\r")                                   # the gate DENIES: the Enter is never forwarded
    got = _settle_and_check_held(rig)
    assert b"/compact" in got and b"/compact\r" not in got    # text reached the child, the submit did not
    assert not _typed_over(got), got                          # the child's prompt still holds abc/compact
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    assert len(rig.gate.held()) == 1
    assert _tap(rig).unsent_text() is None
    _then_ctrl_u_delivers_once(rig)                           # a keystroke that reaches the child still releases


def test_f1b_an_allowed_slash_submit_that_is_written_does_clear_forced_busy(deny_rig):
    rig = deny_rig
    _forced_busy_setup(rig)
    rig.type(b"/clear\r")                                     # not denied: written to the child with its Enter
    child(rig)
    pump(rig)
    assert _tap(rig).forced_busy is None
    assert child(rig).count(b"HELD-MSG") == 1 and not rig.gate.held()


def test_f1b_a_plain_submitted_line_clears_forced_busy(rig):
    _forced_busy_setup(rig)
    rig.type(b"hello\r")
    child(rig)
    pump(rig)
    assert _tap(rig).forced_busy is None
    assert child(rig).count(b"HELD-MSG") == 1 and not rig.gate.held()


def test_f6_a_fall_through_command_is_forwarded_and_clears_forced_busy(rig):
    """Kills the mutant that drops the fall-through clear in _dispatch_command: the child got the text AND its
    Enter, so the prompt really is empty and the held item may be delivered."""
    _forced_busy_setup(rig)
    rig.type(b"\\zzzunknown\r")
    got = _settle_and_check_held(rig)
    assert b"\\zzzunknown\r" in got
    assert _tap(rig).forced_busy is None
    assert got.count(b"HELD-MSG") == 1 and not rig.gate.held()


# ==== Round 6 (architect, Diamond delta-5 F5-1 / F5-10) =====================================================

def test_f5_1_a_fall_through_whose_write_raises_never_clears_forced_busy_or_types_over(rig):
    """The auditor's reproduction: the child write RAISES on the fall-through line's Enter. Nothing reached the
    prompt, so the marks stay and the held item must not be typed over abc. FAILS on 8c2d642 (marks cleared
    before the forward, HELD-MSG typed)."""
    _forced_busy_setup(rig)
    tap = _tap(rig)
    real = tap._writer.write_keys

    def boom(data):
        if data.endswith(b"\r"):
            raise OSError(5, "EIO")
        return real(data)

    tap._writer.write_keys = boom
    try:
        with pytest.raises(OSError):
            rig.type(b"\\zzzunknown\r")
    finally:
        tap._writer.write_keys = real
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert tap.forced_busy == "dialog-close-time-unknown"
    assert len(rig.gate.held()) == 1


@pytest.mark.parametrize("line", [b"foo\\\r", b"\\zzzunknown\\\r"], ids=["plain", "fall-through"])
def test_f5_10_a_trailing_backslash_line_does_not_clear_forced_busy(rig, line):
    """Backslash-Enter is Claude Code's newline, not a submit: the prompt still holds text."""
    _forced_busy_setup(rig)
    rig.type(line)
    got = _settle_and_check_held(rig)
    assert not _typed_over(got), got
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"
    assert len(rig.gate.held()) == 1

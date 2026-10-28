"""Operator ``\\pause`` must intercept AND suppress a subsequent STALL/resume
injection — and the exact live symptom (``\\pause`` logged as a plain
``operator-submit`` passthrough) is reproduced ONLY when the command plane is
disabled.

Context (2026-09-28 dispatch): a live harnessed session logged a ``\\pause``
line as ``kind=injection source=operator trigger=operator-submit parsed=None``
— i.e. it fell through as plain text rather than being parsed as an operator
command. Reading the code end-to-end shows:

* ``\\pause`` IS an implemented verb (``opcommands._pause`` -> ``gate.set_paused``);
* the stall/resume inject path (``proxy.inject_notice`` -> ``injector.inject_line``
  -> ``writer.admit`` -> ``gate.decide`` -> ``gate.evaluate``) DEFERs every
  non-operator event while ``paused`` — so an intercepted ``\\pause`` suppresses
  stall injections;
* the ONLY way a ``\\pause`` reaches ``_submit_line`` (which emits exactly the
  logged ``injection / operator-submit / parsed_command=None`` record) is with the
  command plane DISABLED (``operator_command_plane: false``), so the sigil branch
  in ``OperatorInputTap.feed`` is skipped.

These tests lock that contract against regression. They use the REAL production
object graph (``tests/hooks_support.Rig`` over ``assemble_proxy``).
"""

from __future__ import annotations

import pytest

from hooks_support import Rig

from gnomon.harness.monitor import Notice, NoticeKind


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _stall_notice(text="[Gnomon] resume check: still idle — continue or say idle."):
    """A resume/stall heartbeat — the kind the dual-idle stall trigger emits."""
    return Notice(kind=NoticeKind.HEARTBEAT, text=text)


def test_pause_intercepts_and_suppresses_a_stall_injection_then_resume_releases(rig):
    # \pause is intercepted: nothing reaches the child, the gate is paused.
    rig.type(b"\\pause\r")
    assert rig.child_bytes() == b"", "\\pause must not reach the child"
    assert rig.gate.paused is True
    # It must NOT have been recorded as a plain operator-submit passthrough
    # (that is the live-bug shape); it is an operator-command dispatch instead.
    ops_submits = [r for r in rig.records("injection")
                   if r.get("source") == "operator" and r.get("trigger") == "operator-submit"]
    assert ops_submits == [], "\\pause was passed through as plain text (command plane not active)"

    # A stall/resume notice fired while paused is HELD, not typed into the child.
    rig.proxy.inject_notice(_stall_notice("STALL RESUME NUDGE"))
    assert rig.child_bytes() == b"", "a stall injection must be suppressed while paused"

    # \resume releases the held injection: now it reaches the child (submitted).
    rig.type(b"\\resume\r")
    out = rig.child_bytes()
    assert b"STALL RESUME NUDGE" in out and out.endswith(b"\r"), \
        "resume must release the held stall injection to the child"
    assert rig.gate.paused is False


def test_a_stall_injection_is_typed_when_not_paused(rig):
    """Control: with no pause, the same stall notice IS typed into the child —
    so the suppression above is the pause doing work, not a dead inject path."""
    rig.proxy.inject_notice(_stall_notice("LIVE NUDGE"))
    out = rig.child_bytes()
    assert b"LIVE NUDGE" in out and out.endswith(b"\r")


def test_disabled_command_plane_falls_through_as_route_plain(tmp_path):
    """With the command plane OFF, ``\\pause`` falls through byte-for-byte AND is
    recorded as exactly ``injection / operator-submit / route=plain /
    parsed_command=None`` — and it does NOT pause the gate, so a following stall
    injection is NOT suppressed.

    NOTE (2026-09-29, corrected label): this is the DISABLED-PLANE fall-through
    (``route=plain``). It is NOT the live passthrough the 2026-09-28 dispatch
    observed — that live case has the plane ON and is a report-prefix PUNT
    (``route=punted``), reproduced in
    ``test_report_prefix_before_a_command_used_to_punt_now_dispatches`` /
    ``test_report_prefix_plane_on_is_the_real_live_punt_case`` below. The prior
    name ("reproduces_the_live_passthrough") asserted a false equivalence: a
    disabled plane logs ``route:plain`` whereas the live bug logs ``route:punted``."""
    rig = Rig(tmp_path, harness={"operator_plane": False})
    try:
        rig.type(b"\\pause\r")
        assert rig.child_bytes() == b"\\pause\r", "disabled plane -> byte-identical passthrough"
        assert rig.gate.paused is False, "a passed-through \\pause never pauses the gate"

        rec = [r for r in rig.records("injection")
               if r.get("source") == "operator" and r.get("trigger") == "operator-submit"]
        assert len(rec) == 1, "the passthrough must produce one operator-submit record"
        assert rec[0]["route"] == "plain"
        assert rec[0]["parsed_command"] is None  # disabled-plane evidence: parsed=None

        # And because the gate never paused, a stall injection is NOT suppressed.
        rig.proxy.inject_notice(_stall_notice("NOT SUPPRESSED"))
        assert b"NOT SUPPRESSED" in rig.child_bytes()
    finally:
        rig.close()


# -- the REAL live bug: a terminal report on stdin before the operator's \command
#    (iTerm2 focus events ESC[I/ESC[O, DA replies, kitty keys, ...). Before the
#    fix the leading ESC punted the whole line, so \pause reached the child as
#    plain text (route: punted). After the fix the report is transparent to line
# assembly and \pause dispatches.

# (prefix bytes, label) — each is an UNSOLICITED, keystroke-unambiguous terminal
# report/query response, not operator input. NOTE (2026-09-29): the kitty key
# ESC[97u was REMOVED from this transparent set by a re-Diamond scope fix — a
# kitty-mode keypress is genuine operator input and must PUNT; it now lives in the
# excluded negative-control list below.
_REPORT_PREFIXES = [
    (b"\x1b[I", "focus-in ESC[I"),
    (b"\x1b[O", "focus-out ESC[O"),
    (b"\x1b[?62;22c", "DA1 reply ESC[?62;22c"),
]


@pytest.mark.parametrize("prefix,label", _REPORT_PREFIXES, ids=[p[1] for p in _REPORT_PREFIXES])
def test_report_prefix_before_a_command_used_to_punt_now_dispatches(rig, prefix, label):
    """A terminal report arriving on stdin immediately BEFORE ``\\pause`` must be
    transparent to line assembly: the report is forwarded to the child, ``\\pause``
    is still recognized at line start and DISPATCHES (gate paused; NOT punted, NOT
    passed to the child as text)."""
    rig.type(prefix + b"\\pause\r")

    # \pause dispatched: the gate is paused ...
    assert rig.gate.paused is True, f"{label}: \\pause must dispatch, not punt"
    # ... it was recorded as an operator-command dispatch, NOT an operator-submit
    # passthrough (the punt shape) ...
    dispatched = [r for r in rig.records("operator-command")
                  if r.get("command") == "pause" and r.get("decision") == "dispatched"]
    assert dispatched, f"{label}: expected an operator-command 'pause' dispatch record"
    ops_submits = [r for r in rig.records("injection")
                   if r.get("source") == "operator" and r.get("trigger") == "operator-submit"]
    assert ops_submits == [], f"{label}: \\pause must NOT be logged as an operator-submit (punt/plain)"
    # ... and only the report bytes reached the child; \pause did not.
    assert rig.child_bytes() == prefix, f"{label}: only the report is forwarded to the child"


def test_report_prefix_plane_on_is_the_real_live_punt_case(rig):
    """The exact live symptom (plane ON + a focus report), stated as the contract
    the fix establishes: the report is transparent and \\pause dispatches AND
    suppresses a following stall injection. Before the fix the leading ESC[I set
    _mode=_PUNT for the rest of the line, \\pause fell through as route=punted and
    the gate never paused — the opposite of the disabled-plane route=plain case."""
    rig.type(b"\x1b[I\\pause\r")
    assert rig.gate.paused is True

    # a stall/resume notice fired while paused is HELD, not typed into the child.
    rig.proxy.inject_notice(_stall_notice("REPORT-PREFIX SUPPRESSED"))
    # child has only the transparent report bytes; no stall text, no \pause text.
    assert rig.child_bytes() == b"\x1b[I", "paused: the stall injection must be suppressed"


# -- Fix 1 (regression guard, 2026-09-29): the OSC/DCS report scanner must be
#    BOUNDED — it must stop at an embedded C0 control (a CR) and NOT consume across
#    it to a later BEL. Before the bound, an OSC arriving in the same read as a
#    following \command swallowed the CR and the command as report TEXT: the command
# was lost and NO operator-submit record was written.

@pytest.mark.parametrize("osc_intro,label", [
    (b"\x1b]", "OSC ESC]"),
    (b"\x1bP", "DCS ESC P"),
], ids=["OSC", "DCS"])
def test_osc_dcs_with_embedded_cr_does_not_swallow_the_following_command(rig, osc_intro, label):
    """``ESC] x \\r \\pause \\r ... \\x07`` (or the DCS ``ESC P`` form) in ONE read:
    the string scan must STOP at the embedded CR (a C0 control), returning no match,
    so the intro punts its own line, the CR submits it, and the following \\pause is
    recognized at line start and DISPATCHES. Pre-fix the scan ran to the trailing
    BEL and consumed \\r\\pause\\r as text -> \\pause lost, gate never paused."""
    rig.type(osc_intro + b"x\r\\pause\r\x07")
    # \pause dispatched (gate paused) rather than swallowed into the report body.
    assert rig.gate.paused is True, f"{label}: bounded scan must stop at CR so \\pause dispatches"
    dispatched = [r for r in rig.records("operator-command")
                  if r.get("command") == "pause" and r.get("decision") == "dispatched"]
    assert dispatched, f"{label}: expected a \\pause dispatch after the CR-terminated string intro"


# -- Fix 3 (negative controls, 2026-09-29): line-start-then-\pause for each
#    EXCLUDED sequence must PUNT — \pause does NOT dispatch and the gate is not
#    paused. This pins the transparent set to exactly the safe reports so a future
#    widening (re-admitting a keystroke-ambiguous final) is caught here.
_EXCLUDED_PREFIXES = [
    (b"\x1b[3~", "Delete ESC[3~"),
    (b"\x1b[H", "Home ESC[H"),
    (b"\x1b[F", "End ESC[F"),
    (b"\x1b[m", "bare SGR ESC[m"),
    (b"\x1b[0m", "SGR reset ESC[0m"),
    (b"\x1bOA", "SS3 up ESC O A"),
    # 2026-09-29: a kitty ESC[97u is now DECODED as
    # the text "a" (a plain printable key), so "a\pause" is ordinary text — still no
    # dispatch, for the right reason. The SGR mouse ESC[<0;10;5m was REMOVED from this
    # list: the '<' private prefix is never a keystroke (kitty/modifyOtherKeys never use
    # it); a click on an EMPTY line is transparent, a press mid-line punts — see
    # tests/test_optap_line_assembly.py.
    (b"\x1b[97u", "kitty key ESC[97u"),
    (b"\x1b[1;2R", "Shift+F3 / CPR-lookalike ESC[1;2R"),
]


@pytest.mark.parametrize("prefix,label", _EXCLUDED_PREFIXES, ids=[p[1] for p in _EXCLUDED_PREFIXES])
def test_excluded_sequence_before_a_command_punts_not_dispatches(rig, prefix, label):
    """An EXCLUDED sequence (genuine editing/keystroke) at line start punts the
    line: the following \\pause must NOT dispatch (the safe pre-fix behavior)."""
    rig.type(prefix + b"\\pause\r")
    assert rig.gate.paused is False, f"{label}: excluded seq must punt -> \\pause must NOT dispatch"
    dispatched = [r for r in rig.records("operator-command")
                  if r.get("command") == "pause" and r.get("decision") == "dispatched"]
    assert dispatched == [], f"{label}: \\pause must NOT dispatch after an excluded sequence"


def test_ctrl_u_clears_a_stale_punt_so_a_following_pause_dispatches(rig):
    """Ctrl-U (line-kill) resets line state, so a \\command typed after a line that
    already punted (here a left-arrow, genuine editing) is recognized again."""
    rig.type(b"\x1b[D")   # left-arrow: genuine cursor movement -> the line punts
    rig.type(b"\x15")      # Ctrl-U: clear the stale punt
    rig.type(b"\\pause\r")
    assert rig.gate.paused is True, "Ctrl-U must clear the punt so \\pause dispatches"
    ops_submits = [r for r in rig.records("injection")
                   if r.get("source") == "operator" and r.get("trigger") == "operator-submit"]
    assert ops_submits == [], "after Ctrl-U, \\pause must dispatch, not fall through as a submit"

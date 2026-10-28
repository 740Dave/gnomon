"""PTY-hooks WS-B — operator input tap: ``/`` routing + the ``\\`` command plane.

Acceptance (spec): a non-``/``/``\\`` line is byte-identical passthrough;
``\\pause`` never reaches the child; ``\\hooks off`` works even when a deny-all
hook is configured. Plus the bounded assembly (CR + backspace only; punt on
cursor movement), fall-through, and ``note_operator_activity``.
"""

from __future__ import annotations

import pytest

from hooks_support import DENY_ALL_MANIFEST, Rig

from gnomon.harness import optap
from gnomon.harness.hooks import validate_manifest


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


def _ops(rig):
    return [r for r in rig.records() if r.get("source") == "operator"]


def test_plain_line_is_byte_identical_passthrough_and_recorded_once(rig):
    typed = b"hello there, claude\r"
    rig.type(typed[:5])
    rig.type(typed[5:])
    assert rig.child_bytes() == typed
    recs = [r for r in rig.records("injection") if r["source"] == "operator"]
    assert len(recs) == 1 and recs[0]["route"] == "plain" and recs[0]["content_preview"] == "hello there, claude"


def test_a_whole_session_of_ordinary_typing_is_byte_identical(rig):
    stream = b"ab\x7fc\rsecond line\r  indented\rthird \xc3\xa9\x7f\r"
    for i in range(0, len(stream), 3):
        rig.type(stream[i:i + 3])
    assert rig.child_bytes() == stream
    assert len([r for r in rig.records("injection") if r["source"] == "operator"]) == 4


def test_pause_never_reaches_the_child_and_pauses_injections(rig):
    rig.type(b"\\pause\r")
    assert rig.child_bytes() == b""
    assert rig.gate.paused is True
    rig.hub_down({"cmd": "inject", "text": "queued while paused"})
    assert rig.child_bytes() == b""
    rig.type(b"\\resume\r")
    assert rig.child_bytes() == b"queued while paused\r"
    assert b"[gm]" in rig.operator_screen()


def test_hooks_off_works_even_with_a_deny_all_hook(tmp_path):
    rig = Rig(tmp_path, harness={"hook_manifest": validate_manifest(DENY_ALL_MANIFEST)})
    try:
        rig.type(b"\\pause\r")  # a non-reserved command IS blocked by the deny-all command hook
        assert rig.gate.paused is False
        rig.hub_down({"cmd": "inject", "text": "blocked"})
        assert rig.child_bytes() == b""
        rig.type(b"\\hooks off\r")  # RESERVED: dispatched before any hook
        assert rig.gate.hooks_enabled is False
        rig.hub_down({"cmd": "inject", "text": "now allowed"})
        assert rig.child_bytes() == b"now allowed\r"
        rig.type(b"\\hooks on\r")
        rig.hub_down({"cmd": "inject", "text": "blocked again"})
        assert rig.child_bytes() == b""
        ctl = [r for r in rig.records("control") if r["control"] == "hooks"]
        assert [c["value"] for c in ctl] == ["off", "on"]
    finally:
        rig.close()


def test_hooks_is_reserved():
    assert "hooks" in optap.RESERVED_COMMANDS


def test_unregistered_backslash_falls_through_unchanged(rig):
    rig.type(b"\\frobnicate\r")
    assert rig.child_bytes() == b"\\frobnicate\r"
    ft = [r for r in rig.records("operator-command") if r["decision"] == "fall-through"]
    assert len(ft) == 1


def test_backslash_enter_is_claude_codes_newline_and_falls_through(rig):
    rig.type(b"\\\r")
    assert rig.child_bytes() == b"\\\r"


def test_leading_whitespace_then_sigil_is_a_command(rig):
    rig.type(b"  \\status\r")
    assert rig.child_bytes() == b""
    assert "status" in rig.proxy.operator_tap.dispatched


def test_leading_whitespace_then_text_is_passthrough(rig):
    rig.type(b"  hi\r")
    assert rig.child_bytes() == b"  hi\r"


def test_backslash_mid_line_is_ordinary_text(rig):
    rig.type(b"path C:\\pause\r")
    assert rig.child_bytes() == b"path C:\\pause\r"
    assert rig.gate.paused is False


def test_backspace_edits_the_command_and_erasing_the_sigil_returns_to_text(rig):
    rig.type(b"\\pausx\x7fe\r")
    assert rig.gate.paused is True and rig.child_bytes() == b""
    rig.type(b"\\\x7fhello\r")
    assert rig.child_bytes() == b"hello\r"


def test_ctrl_c_in_command_mode_cancels(rig):
    rig.type(b"\\pau\x03")
    rig.type(b"ok\r")
    assert rig.gate.paused is False and rig.child_bytes() == b"ok\r"


def test_cursor_keys_punt_the_line_to_raw_passthrough(rig):
    stream = b"/comp\x1b[Dx\r"
    rig.type(stream)
    assert rig.child_bytes() == stream
    rec = [r for r in rig.records("injection") if r["source"] == "operator"][-1]
    assert rec["route"] == "punted" and rec["content_preview"] is None


def test_escape_in_command_mode_punts_and_forwards_what_was_held(rig):
    rig.type(b"\\pa")
    rig.type(b"\x1b[A")
    rig.type(b"\r")
    assert rig.child_bytes() == b"\\pa\x1b[A\r"
    assert rig.gate.paused is False


def test_lone_escape_resets_to_line_start(rig):
    rig.type(b"half typed")
    rig.type(b"\x1b\x1b")
    rig.type(b"\\pause\r")
    assert rig.gate.paused is True
    assert rig.child_bytes() == b"half typed\x1b\x1b"


def test_bracketed_paste_with_newlines_is_not_a_submit(rig):
    paste = b"\x1b[200~line1\rline2\x1b[201~"
    rig.type(paste)
    assert [r for r in rig.records("injection") if r["source"] == "operator"] == []
    rig.type(b"\r")
    assert rig.child_bytes() == paste + b"\r"
    assert len([r for r in rig.records("injection") if r["source"] == "operator"]) == 1


def test_slash_line_runs_the_pipeline_and_a_deny_withholds_the_submit(tmp_path):
    rig = Rig(tmp_path, harness={"hook_manifest": {"inbound-injection": [
        {"kind": "deny_all", "sources": ["operator"], "commands": ["/compact"], "name": "no-compact"}]}})
    try:
        rig.type(b"/compact\r")
        assert rig.child_bytes() == b"/compact"  # typed text went through; the SUBMIT was withheld
        rec = [r for r in rig.records("injection") if r["source"] == "operator"][-1]
        assert rec["decision"] == "deny" and rec["parsed_command"] == "/compact"
        assert b"not submitted" in rig.operator_screen()
        rig.type(b"\x03/clear\r")  # a different command is unaffected
        assert rig.child_bytes().endswith(b"/clear\r")
    finally:
        rig.close()


def test_operator_compact_is_attributed(rig):
    rig.type(b"/compact keep DEC ids\r")
    assert rig.child_bytes() == b"/compact keep DEC ids\r"
    rec = [r for r in rig.records("injection") if r["source"] == "operator"][-1]
    assert rec["parsed_command"] == "/compact" and rec["route"] == "slash" and rec["decision"] == "allow"


def test_operator_activity_feeds_the_monitor(rig):
    mon = rig.proxy._monitor
    assert mon._last_operator_activity is None
    rig.type(b"x")
    assert mon._last_operator_activity is not None


def test_command_plane_can_be_disabled(tmp_path):
    rig = Rig(tmp_path, harness={"operator_plane": False})
    try:
        rig.type(b"\\pause\r")
        assert rig.child_bytes() == b"\\pause\r" and rig.gate.paused is False
    finally:
        rig.close()


def test_utf8_backspace_removes_the_whole_character():
    buf = bytearray("aé".encode())
    optap._pop_char(buf)
    assert bytes(buf) == b"a"


def test_status_reports_state(rig):
    rig.type(b"\\status\r")
    screen = rig.operator_screen()
    assert b"hooks_enabled" in screen and b"agent_launch" in screen

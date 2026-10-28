"""Condition C4 measurement (pre-review batch, spec section 4): can a held DSD notice be typed into an OPEN dialog?

EVERY TEST HERE PINS TODAY'S BEHAVIOUR, A KNOWN LIMIT. None of them states a behaviour we endorse. The bounded release of a held
notice is NOT built (coordinator ruling 2026-10-05: a permission prompt's close cannot be read, and today's 300 s "unknown" release
already has the exposure). They stay as the record; a test marked EXPOSURE passes because the limit exists, and must be changed on
purpose the day a positive read of "closed" exists.

These tests MEASURE; they do not assert a wish. Each one states what the harness's own view is (the real ModalGuard fed what
a real terminal would paint, the real DsdNoticeInbox on a fake clock) and what happens. Where a test shows a notice being
released while a dialog is, by the test's construction, still open, that is the exposure, recorded as a passing assertion
about TODAY's behaviour so the day it is fixed the test must be changed on purpose. The write-up is
~/gnomon-port-work/MEASURE-held-notice-release-2026-10-05.md.

Doubles: no terminal. The "dialog is open" fact is the test's own premise (it paints the bytes); the harness only ever sees the
bytes and the transcript file, exactly as in production.
"""
from __future__ import annotations

import json

import pytest

from gnomon.harness.modal_prompt import ModalGuard
from gnomon.harness.notice_inbox import DsdNoticeInbox

DSD = {"dsd_id": "dsd-1a2b3c4d5e1", "root_id": "DEC-DSD-1A2B3C4D5E1", "text": "t"}
PERMISSION_SCREEN = (b"\x1b[2J Bash command\r\n  rm -r build\r\n Do you want to proceed?\r\n"
                     b" 1. Yes\r\n 2. Yes, and don't ask again for rm commands\r\n 3. No\r\n")
UNRECOGNISED_SCREEN = b"\x1b[2J Select a model\r\n  1. Opus\r\n  2. Sonnet\r\n Enter to confirm . Esc to exit\r\n"
UNKNOWN_AFTER = 300.0


def _row(kind, **content):
    return {"type": kind, "message": {"role": kind, "content": [content]}}


def _append(path, rows):
    with open(path, "a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


class Rig:
    """A real ModalGuard + a real DsdNoticeInbox sharing one fake clock, with the line state fixed to ``unknown`` (the
    cursor/history/paste punt) and the operator silent: the exact condition of today's 300 s release."""

    def __init__(self, tmp_path, line_state="unknown", modal=True):
        self.t = {"now": 1000.0}
        self.transcript = tmp_path / "transcript.jsonl"
        self.transcript.write_text("")
        self.guard = ModalGuard(transcript_path=lambda: str(self.transcript), clock=lambda: self.t["now"])
        self.typed = []
        self.inbox = DsdNoticeInbox(
            inject=lambda text, **kw: self.typed.append(text) or None, ack=lambda _f: None, quiet_seconds=0.0,
            ready=lambda: True, line_state=lambda: line_state, unknown_after_seconds=UNKNOWN_AFTER,
            modal_open=self.guard.check if modal else None, clock=lambda: self.t["now"])

    def advance(self, seconds, step=10.0):
        """Advance in steps, ticking the inbox as the pump would."""
        end = self.t["now"] + seconds
        while self.t["now"] < end:
            self.t["now"] = min(end, self.t["now"] + step)
            self.inbox.tick()


# --------------------------------------------------------------------------- (b) today's release of an "unknown" line

def test_known_limit_pins_today_unknown_line_released_at_300_seconds(tmp_path):
    rig = Rig(tmp_path)
    rig.inbox.offer(dict(DSD))
    rig.advance(UNKNOWN_AFTER - 10)
    assert rig.typed == []                                    # held while the operator has been silent < 300 s
    rig.advance(20)
    assert len(rig.typed) == 1                                # released at 300 s (the shared line-hold rule)


def test_known_limit_pins_today_unrecognised_dialog_gets_the_notice(tmp_path):
    """EXPOSURE 1: a dialog whose text is not in SCREEN_MARKERS (a model picker, a trust prompt, a rewind menu ...) is invisible
    to the harness. At 300 s the notice and its newline are written."""
    rig = Rig(tmp_path)
    rig.guard.feed(UNRECOGNISED_SCREEN)                       # the premise: this dialog is painted and stays open
    assert rig.guard.check() is None                          # the harness's answer: "no dialog"
    rig.inbox.offer(dict(DSD))
    rig.advance(UNKNOWN_AFTER + 10)
    assert len(rig.typed) == 1                                # typed (the inbox's inject adds the newline) into that dialog


def test_pins_today_recognised_permission_prompt_holds_the_notice(tmp_path):
    """The guard that works: marker painted, transcript silent (a blocked turn writes nothing) -> held, with no time limit."""
    rig = Rig(tmp_path)
    rig.guard.feed(PERMISSION_SCREEN)
    rig.inbox.offer(dict(DSD))
    rig.advance(3 * 3600, step=60.0)
    assert rig.typed == [] and rig.inbox.held_ids() == [DSD["dsd_id"]]
    assert rig.guard.check() == "permission-prompt (screen)"


def test_known_limit_pins_today_permission_prompt_forgotten_when_transcript_grows(tmp_path):
    """EXPOSURE 2: for a permission prompt "closed" is INFERRED (the transcript size changed), never read. Any row appended
    while the prompt is still up (a background task's completion row, a subagent summary) makes the guard drop its hint."""
    rig = Rig(tmp_path)
    rig.guard.feed(PERMISSION_SCREEN)
    rig.t["now"] += 5.0                                       # past the 2 s screen window
    assert rig.guard.check() == "permission-prompt (screen)"  # held by the size rule
    _append(rig.transcript, [_row("user", type="text", text="<task-notification>a background task finished</task-notification>")])
    assert rig.guard.check() is None                          # dialog never closed (nothing read it closing), but forgotten
    rig.inbox.offer(dict(DSD))
    rig.advance(UNKNOWN_AFTER + 10)
    assert len(rig.typed) == 1


def test_known_limit_pins_today_unreadable_transcript_fails_open(tmp_path):
    rig = Rig(tmp_path)
    rig.transcript.unlink()                                   # the reader cannot see the transcript at all
    rig.guard.feed(UNRECOGNISED_SCREEN)
    rig.inbox.offer(dict(DSD))
    rig.advance(UNKNOWN_AFTER + 10)
    assert len(rig.typed) == 1


def test_known_limit_pins_today_modal_hold_off_means_no_check(tmp_path):
    """launcher.py passes ``modal_open=modal_guard.check if modal_on else None``: with modal_hold OFF nothing guards."""
    rig = Rig(tmp_path, modal=False)
    rig.guard.feed(PERMISSION_SCREEN)
    rig.inbox.offer(dict(DSD))
    rig.advance(UNKNOWN_AFTER + 10)
    assert len(rig.typed) == 1


# --------------------------------------------------------------------------- (a) what CAN be read positively

def test_pins_today_interactive_tool_dialog_has_a_positive_close(tmp_path):
    """The one dialog class with a positive close: AskUserQuestion / ExitPlanMode (an unanswered tool_use, then its tool_result)."""
    rig = Rig(tmp_path)
    _append(rig.transcript, [_row("assistant", type="tool_use", id="q1", name="AskUserQuestion", input={})])
    assert rig.guard.check() == "AskUserQuestion"
    rig.inbox.offer(dict(DSD))
    rig.advance(3600, step=60.0)
    assert rig.typed == []                                    # open: held however long
    _append(rig.transcript, [_row("user", type="tool_result", tool_use_id="q1", content="ok")])
    assert rig.guard.check() is None                          # closed, read from a row naming this very call
    boundary = rig.guard.close_boundary()
    assert boundary is not None and boundary.from_transcript is True


def test_known_limit_pins_today_permission_prompt_has_no_closing_row(tmp_path):
    """The gated tool's tool_use is unanswered while the prompt is up, but the guard only tracks MODAL_TOOLS; a long-running Bash is
    indistinguishable from a Bash waiting on permission, so it cannot be counted as a dialog by that signal alone."""
    rig = Rig(tmp_path)
    _append(rig.transcript, [_row("assistant", type="tool_use", id="b1", name="Bash", input={"command": "sleep 600"})])
    assert rig.guard.check() is None

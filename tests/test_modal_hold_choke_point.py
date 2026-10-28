"""ONE hold, at the ONE place harness text reaches the PTY.

Incident 2026-10-03T02:15:31Z: a harness-typed notice + CR landed on an open
AskUserQuestion; the CR selected the dialog's first option 83 ms later. The exposure is every
harness-originated injection, not one sender, so the hold is ``InjectionGate.decide`` (the funnel
``SingleWriteSite.admit`` -> gate). These tests drive the REAL production object graph
(``launcher.assemble_proxy``) over a socketpair/real pty and read what reaches the child.

Acceptance (the defect node): (1) a dialog stays open and every injection kind queues, delivered
exactly once after, in order; (2) a trailing-newline text is covered; (3) operator keystrokes are
never held; (4) fail-open on an unreadable transcript is LOGGED, and the first-instant race and
tool-permission prompts are tested as far as they can be and stated as limits in RUN_SUMMARY.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import textwrap
import time

import pytest

from hooks_support import FakeHub, Rig

from gnomon.harness.monitor import Notice, NoticeKind


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, harness={"dsd_notice_boot_grace_seconds": 0.0, "dsd_notice_operator_quiet_seconds": 0.0})
    yield r
    r.close()


def _tpath(rig):
    return os.path.join(rig.session_dir, ".claude", "transcript.jsonl")


def _now_iso():
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _row(kind, tid, name=None, ts=True):
    """Real Claude Code transcript rows carry their own ``timestamp`` (1123 of 1123 measured); the dialog-close
    boundary is read from the closing row's and the open edge's ambiguity window from the opening row's
    (``ts=False`` = a row without one)."""
    if kind == "use":
        row = {"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": {}}]}}
        if ts:
            row["timestamp"] = _now_iso()
        return row
    row = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tid, "content": "answered"}]}}
    if ts:
        row["timestamp"] = _now_iso()
    return row


def _append(rig, *rows):
    with open(_tpath(rig), "a", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def open_dialog(rig, tool="AskUserQuestion", tid="q1", ts=True):
    _append(rig, _row("use", tid, tool, ts=ts))


def close_dialog(rig, tid="q1", ts=True):
    _append(rig, _row("result", tid, ts=ts))


def pump(rig, n=3):
    for _ in range(n):
        rig.proxy.step(timeout=0.01)


def child(rig):
    return rig.child_bytes()


# every harness-originated injection kind, each through its REAL entry point -----------------------

def _bus(rig, t):
    rig.hub_down({"cmd": "inject", "text": t})

def _broadcast(rig, t):
    rig.hub_down({"cmd": "inject", "text": t, "source": "operator-broadcast"})

def _self(rig, t):
    rig.proxy.actuator.dispatch({"cmd": "inject", "text": t}, origin="self")

def _guidance(rig, t):
    rig.proxy.inject_notice(Notice(kind=NoticeKind.COMPACT_GUIDANCE, text=t, threshold=90))

def _fyi(rig, t):
    rig.proxy.inject_notice(Notice(kind=NoticeKind.FYI, text=t, threshold=95))

def _heartbeat(rig, t):  # the stall / resume / idle nudge path
    rig.proxy.inject_notice(Notice(kind=NoticeKind.HEARTBEAT, text=t))

def _raw(rig, t):
    rig.proxy._writer.inject(t.encode())

def _boot(rig, t):  # the X17 post-compact boot prompt's source
    rig.proxy._injector.inject_line(t, clear=False, source="bootstrap", trigger="post-compact-boot")

def _dsd(rig, t):
    rig.hub_down({"cmd": "inject", "source": "dsd-answer", "dsd_id": "dsd-1a2b3c4d5e1",
                  "root_id": "DEC-DSD-1A2B3C4D5E1", "text": "ignored"})

KINDS = {"bus": (_bus, "bus-msg"), "operator-broadcast": (_broadcast, "<d-to-all> hello"),
         "self-inject": (_self, "self note"), "compact-guidance": (_guidance, "pencils down"),
         "fyi": (_fyi, "fyi-text"), "stall-resume-heartbeat": (_heartbeat, "resume?"),
         "raw-writer-inject": (_raw, "rawbytes"), "boot-prompt": (_boot, "boot-prompt-text"),
         "dsd-answer": (_dsd, "DEC-DSD-1A2B3C4D5E1")}


@pytest.mark.parametrize("tool", ["AskUserQuestion", "ExitPlanMode"])
@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_injection_kind_is_held_while_a_dialog_is_open_then_delivered_exactly_once(rig, kind, tool):
    send, needle = KINDS[kind]
    open_dialog(rig, tool)
    send(rig, needle)
    pump(rig)
    assert needle.encode() not in child(rig), f"{kind} reached the PTY while {tool} was open"
    assert b"\r" not in child(rig)
    close_dialog(rig)                       # the human answered the dialog
    pump(rig)
    out = child(rig)
    assert out.count(needle.encode()) == 1, f"{kind}: not delivered exactly once: {out!r}"
    pump(rig)
    assert needle.encode() not in child(rig)  # and never again


def test_held_injections_keep_their_order_and_a_new_one_never_jumps_the_queue(rig):
    open_dialog(rig)
    _bus(rig, "first")
    _self(rig, "second")
    _guidance(rig, "third")
    pump(rig)
    assert child(rig) == b""
    close_dialog(rig)
    _fyi(rig, "fourth")           # arrives AFTER the close but BEFORE any release tick
    pump(rig)
    out = child(rig)
    idx = [out.index(w) for w in (b"first", b"second", b"third", b"fourth")]
    assert idx == sorted(idx), out
    assert all(out.count(w) == 1 for w in (b"first", b"second", b"third", b"fourth"))


def test_a_second_dialog_opening_mid_release_stops_the_release_and_keeps_the_rest(rig):
    open_dialog(rig, tid="q1")
    _bus(rig, "one")
    _bus(rig, "two")
    close_dialog(rig, "q1")
    open_dialog(rig, tid="q2")     # the agent asked again
    pump(rig)
    assert child(rig) == b""       # still nothing typed into dialog #2
    close_dialog(rig, "q2")
    pump(rig)
    out = child(rig)
    assert out.index(b"one") < out.index(b"two") and out.count(b"one") == out.count(b"two") == 1


def test_text_ending_in_a_newline_is_held_and_delivered_whole(rig):
    """Acceptance (2): the regression where the injected text itself ends in a newline."""
    open_dialog(rig)
    rig.proxy._injector.inject_line("answer text\n", clear=False, source="bus", trigger="t")
    pump(rig)
    assert child(rig) == b""
    close_dialog(rig)
    pump(rig)
    out = child(rig)
    assert out.count(b"answer text") == 1 and out.endswith(b"\r")


def test_operator_keystrokes_are_never_held(rig):
    open_dialog(rig)
    rig.type(b"2")                 # the operator answers the dialog with his own key
    rig.type(b"\r")
    assert child(rig) == b"2\r"    # straight through, no wait, no tick
    # and an operator-sourced injection (the tap's \command fall-through) is exempt too
    rig.proxy._injector.inject_line("/status", clear=False, source="operator", trigger="op")
    assert b"/status" in child(rig)


def test_dialog_closed_means_no_hold_at_all(rig):
    open_dialog(rig)
    close_dialog(rig)
    _bus(rig, "free")
    assert b"free" in child(rig)
    assert not rig.gate._modal_ids


def test_held_events_are_recorded_as_defer_then_release_in_the_provenance_log(rig):
    open_dialog(rig)
    _bus(rig, "recorded")
    held = [r for r in rig.records("injection") if r.get("decision") == "defer"]
    assert held and held[0]["handler"] == "modal-hold" and "AskUserQuestion" in held[0]["reason"]
    close_dialog(rig)
    pump(rig)
    assert any(r.get("decision") == "release" and r.get("reason") == "modal-closed"
               for r in rig.records("injection"))


def test_operator_pause_still_holds_modal_held_events_and_resume_rechecks_the_dialog(rig):
    open_dialog(rig)
    _bus(rig, "held-for-dialog")
    rig.gate.set_paused(True)
    close_dialog(rig)
    pump(rig)
    assert child(rig) == b""                       # paused: modal release does not override the pause
    open_dialog(rig, tid="q2")
    rig.gate.set_paused(False)                     # \resume while a NEW dialog is open
    assert child(rig) == b""                       # resume must not type into it
    close_dialog(rig, "q2")
    pump(rig)
    assert child(rig).count(b"held-for-dialog") == 1


def test_operator_explicit_approve_is_the_operators_call(rig):
    open_dialog(rig)
    _bus(rig, "forced")
    (held_id,) = [i for i, _e, _w in rig.gate.held()]
    rig.gate.approve(held_id, force=True)
    assert b"forced" in child(rig)


# fail-open is LOGGED -----------------------------------------------------------------------------

def _gaps(rig):
    return [r for r in rig.records("modal-guard") if r.get("what") == "transcript-gap-fail-open"]


def test_unreadable_transcript_fails_open_and_says_so_once(rig):
    p = _tpath(rig)
    open(p, "w").close()
    os.chmod(p, 0)
    try:
        _bus(rig, "through-the-gap")
        _bus(rig, "again")
        assert b"through-the-gap" in child(rig)     # fail OPEN: delivery is not wedged
        assert len(_gaps(rig)) == 1 and "Permission" in _gaps(rig)[0]["error"]   # logged, once per outage
    finally:
        os.chmod(p, stat.S_IRUSR | stat.S_IWUSR)


def test_missing_transcript_fails_open_and_says_so(rig):
    _bus(rig, "no-transcript-yet")
    assert b"no-transcript-yet" in child(rig)
    assert _gaps(rig) and "no transcript" in _gaps(rig)[0]["error"]


def test_outage_that_ends_rearms_the_log_line(rig):
    _bus(rig, "a")
    assert len(_gaps(rig)) == 1
    _append(rig, _row("use", "b1", "Bash"), _row("result", "b1"))
    _bus(rig, "b")
    _bus(rig, "c")
    assert len(_gaps(rig)) == 1
    os.chmod(_tpath(rig), 0)
    try:
        _bus(rig, "d")
        assert len(_gaps(rig)) == 2
    finally:
        os.chmod(_tpath(rig), stat.S_IRUSR | stat.S_IWUSR)


# the PTY screen bridge (first-instant race; tool-permission prompts) -------------------------------

ANSI_ASK = (b"\x1b[1m1.\x1b[0m Download a local model \x1b[2m(Recommended)\x1b[0m\r\n"
            b"\x1b[1m3.\x1b[0m Chat about this\r\n\x1b[2m\x1b[1mEnter\x1b[22m to select \xc2\xb7 "
            b"Tab/Arrow keys to navigate \xc2\xb7 \x1b[1mEsc\x1b[22m to cancel\x1b[0m")
ANSI_PERM = (b"\x1b[1mBash command\x1b[0m\r\n rm -rf x\r\n Do you want to \x1b[1mproceed\x1b[0m?\r\n"
             b" \xe2\x9d\xaf 1. Yes\r\n   2. Yes, and don't ask again for rm commands\r\n   3. No")


def _guard(rig):
    return rig.gate.modal_check.__self__


def paint(rig, data):
    rig.child.sendall(data)
    rig.proxy._on_master_readable()


@pytest.fixture
def clock(rig):
    g = _guard(rig)
    t = {"now": 1000.0}
    g._clock = lambda: t["now"]
    return t


def test_first_instant_race_dialog_painted_before_its_transcript_row_exists(rig, clock):
    _append(rig, _row("use", "b0", "Bash"), _row("result", "b0"))   # a readable, quiet transcript
    paint(rig, ANSI_ASK)                    # the dialog is on screen; the tool_use row is not flushed yet
    _bus(rig, "raced")
    pump(rig)
    assert b"raced" not in child(rig)       # held by the screen bridge alone
    open_dialog(rig)                        # the row lands: the transcript leg takes over
    clock["now"] += 10                      # ... long after the screen window
    pump(rig)
    assert child(rig) == b""
    close_dialog(rig)
    pump(rig)
    assert child(rig).count(b"raced") == 1


def test_tool_permission_prompt_on_screen_holds_until_the_turn_moves(rig, clock):
    _append(rig, _row("use", "t1", "Bash"))     # a Bash tool_use with no result: indistinguishable from a long run
    paint(rig, ANSI_PERM)
    _bus(rig, "behind-the-permission-prompt")
    clock["now"] += 60                           # the human is slow; transcript has not grown
    pump(rig)
    assert child(rig) == b""
    _append(rig, _row("result", "t1"))           # he answered; the tool ran; the turn moved on
    pump(rig)
    assert child(rig).count(b"behind-the-permission-prompt") == 1


def test_screen_hint_never_times_out_into_a_dialog_that_may_still_be_open(rig, clock):
    """F2a (fix round): the old 30-minute failsafe typed text + CR into a still-open permission dialog.
    Now the hold stays; only a human clears it (see test_modal_hold_fix_round for the visible state)."""
    _append(rig, _row("use", "t1", "Bash"))
    paint(rig, ANSI_PERM)
    _bus(rig, "stuck?")
    clock["now"] += 24 * 3600
    pump(rig)
    assert child(rig) == b""
    assert len(rig.gate.held()) == 1
    assert not any(r.get("what") == "screen-hint-failsafe-expired" for r in rig.records("modal-guard"))


def test_ordinary_output_and_old_markers_do_not_hold(rig, clock):
    _append(rig, _row("use", "b0", "Bash"), _row("result", "b0"))
    paint(rig, b"just some streamed assistant text, nothing modal\r\n")
    _bus(rig, "plain")
    assert b"plain" in child(rig)
    paint(rig, ANSI_ASK)                         # a dialog appears ...
    _append(rig, _row("use", "q", "AskUserQuestion"), _row("result", "q"))
    clock["now"] += 5                            # ... is answered; the transcript grew
    paint(rig, b"more output after the dialog\r\n")   # the old marker is still in the tail buffer
    pump(rig)
    _bus(rig, "after")
    assert b"after" in child(rig)                # a stale marker never re-arms the hold


def test_marker_split_across_pty_reads_is_still_seen(rig, clock):
    _append(rig, _row("use", "b0", "Bash"), _row("result", "b0"))
    paint(rig, b"2. Chat ab")
    paint(rig, b"out this\r\n")
    _bus(rig, "split")
    assert b"split" not in child(rig)


def test_dialog_painted_just_after_a_harness_write_is_recorded_as_a_suspected_collision(rig, clock):
    """The residual race (limit, stated): a write that wins the race cannot be undone, but it is LOUD."""
    _append(rig, _row("use", "b0", "Bash"), _row("result", "b0"))
    _bus(rig, "won-the-race")
    assert b"won-the-race" in child(rig)
    clock["now"] += 0.083                        # the dialog appears 83 ms later
    paint(rig, ANSI_ASK)
    sus = [r for r in rig.records("modal-guard") if r.get("what") == "dialog-after-injection-suspect"]
    assert sus and sus[0]["seconds_after_write"] <= 0.1


def test_folder_trust_accepter_is_not_held_by_the_dialog_it_exists_to_answer(rig):
    open_dialog(rig)
    from gnomon.harness.hooks import InjectionEvent

    from gnomon.harness.hooks import FOLDER_TRUST_CAP, FOLDER_TRUST_CAP_KEY

    ev = InjectionEvent(source="bootstrap", trigger="folder-trust-accept:down", content="<folder-trust down>",
                        meta={FOLDER_TRUST_CAP_KEY: FOLDER_TRUST_CAP})
    rig.proxy._writer.admit(ev, lambda _e: rig.proxy._writer.inject(b"\x1b[B"))
    assert b"\x1b[B" in child(rig)


# the original 83 ms scenario, end to end, in a fake PTY --------------------------------------------

FAKE_CLAUDE = textwrap.dedent('''\
    import json, os, sys, time, tty
    tty.setraw(0)
    tp, res, typed = os.environ["FAKE_TRANSCRIPT"], os.environ["FAKE_RESULT"], os.environ["FAKE_TYPED"]
    lag = float(os.environ.get("FAKE_ROW_LAG", "0"))
    def row(kind, tid):
        if kind == "use":
            return {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": tid, "name": "AskUserQuestion", "input": {}}]}}
        import datetime
        return {"type": "user", "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "message": {"content": [{"type": "tool_result", "tool_use_id": tid, "content": "x"}]}}
    def put(r):
        with open(tp, "a") as f: f.write(json.dumps(r) + "\\n")
    out = sys.stdout.buffer
    out.write(b"1. Download a local model (Recommended)\\r\\n2. Hosted\\r\\n3. Chat about this\\r\\n"
              b"Enter to select \\xc2\\xb7 Tab/Arrow keys to navigate \\xc2\\xb7 Esc to cancel\\r\\n"); out.flush()
    time.sleep(lag)
    put(row("use", "q1"))
    open(os.environ["FAKE_READY"], "w").close()
    in_dialog = True
    while True:
        b = os.read(0, 1)
        if not b:
            break
        if in_dialog:
            if b in (b"\\r", b"\\n"):
                open(res, "a").write("first-option-selected-by-CR\\n"); in_dialog = False; put(row("result", "q1"))
            elif b in (b"1", b"2", b"3"):
                open(res, "a").write("operator-chose-" + b.decode() + "\\n"); in_dialog = False; put(row("result", "q1"))
            # any other byte is IGNORED by a real select dialog
        else:
            open(typed, "ab").write(b)
''')


class _PtyRig:
    def __init__(self, tmp_path, lag=0.0):
        import pty
        from gnomon.harness import assemble_proxy
        from gnomon.harness.spawn import SpawnResult

        self.dir = tmp_path
        self.session_dir = str(tmp_path / "sess")
        os.makedirs(os.path.join(self.session_dir, ".claude"))
        self.transcript = os.path.join(self.session_dir, ".claude", "transcript.jsonl")
        self.res, self.typed, self.ready = (str(tmp_path / n) for n in ("res", "typed", "ready"))
        script = tmp_path / "fake_claude.py"
        script.write_text(FAKE_CLAUDE)
        self.master, slave = pty.openpty()
        env = dict(os.environ, FAKE_TRANSCRIPT=self.transcript, FAKE_RESULT=self.res, FAKE_TYPED=self.typed,
                   FAKE_READY=self.ready, FAKE_ROW_LAG=str(lag))
        self.proc = subprocess.Popen([sys.executable, str(script)], stdin=slave, stdout=slave, stderr=slave,
                                     env=env, close_fds=True)
        os.close(slave)
        self.stdin_r, self.stdin_w = os.pipe()
        self.devnull = os.open(os.devnull, os.O_WRONLY)
        self.hub = FakeHub()
        self.proxy = assemble_proxy(SpawnResult(pid=0, master_fd=self.master), read_context=lambda: 10.0,
                                    hub_connection=self.hub, hub_send=self.hub.send_frame,
                                    stdin_fd=self.stdin_r, stdout_fd=self.devnull, session_dir=self.session_dir,
                                    instance_id="gn-pty", harness={})
        self.proxy._injector._sleep = lambda _s: time.sleep(0.01)
        tap = getattr(self.proxy, "operator_tap", None)
        if tap is not None:
            tap._child_pid = None

    def run(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.proxy.step(timeout=0.02)

    def until(self, pred, t=5.0):
        end = time.time() + t
        while time.time() < end:
            self.proxy.step(timeout=0.02)
            if pred():
                return True
        return pred()

    def typed_text(self):
        return open(self.typed, "rb").read() if os.path.exists(self.typed) else b""

    def answers(self):
        return open(self.res).read().split() if os.path.exists(self.res) else []

    def close(self):
        self.proc.kill()
        self.proc.wait()
        self.hub.close()
        for fd in (self.master, self.stdin_r, self.stdin_w, self.devnull):
            try:
                os.close(fd)
            except OSError:
                pass


INJECTORS = {
    "dsd-answer": lambda r, t: r.proxy._injector.inject_line(t, clear=False, source="dsd-answer",
                                                             trigger="dsd-answer"),
    "self-inject": lambda r, t: r.proxy.actuator.dispatch({"cmd": "inject", "text": t}, origin="self"),
    "stall-nudge": lambda r, t: r.proxy.inject_notice(Notice(kind=NoticeKind.HEARTBEAT, text=t)),
}


@pytest.mark.parametrize("lag", [0.0, 0.6], ids=["row-flushed-first", "row-flushed-600ms-late"])
@pytest.mark.parametrize("kind", sorted(INJECTORS))
def test_fake_pty_injection_never_selects_the_dialogs_answer(tmp_path, kind, lag):
    """The 2026-10-03 scenario: a real pty, a child that behaves like Claude Code's select dialog (a CR
    selects the FIRST option; other bytes are ignored). The injection must wait; the operator's own key
    answers the dialog; the injection then arrives once, as a prompt line."""
    r = _PtyRig(tmp_path, lag=lag)
    try:
        assert r.until(lambda: os.path.exists(r.ready) or lag > 0 and r.proxy.gate.modal_check() is not None)
        text = "[DSD ANSWERED] dsd-1a2b3c4d5e1"
        INJECTORS[kind](r, text)
        r.run(0.5)
        assert r.answers() == [], f"the injection answered the dialog: {r.answers()}"
        assert r.typed_text() == b""
        assert r.until(lambda: os.path.exists(r.ready))
        r.run(0.3)
        assert r.answers() == [] and r.typed_text() == b""
        os.write(r.stdin_w, b"2")                      # the operator answers; his key is never held
        r.proxy._on_stdin_readable()
        assert r.until(lambda: r.answers() == ["operator-chose-2"])
        # Round 3 (E2): the closing row's stamp trails the real close, so the answer digit falls inside the
        # replay margin and is replayed as line text: the line looks BUSY and the injection WAITS (a delay,
        # never a type-over) until his next Enter / Ctrl-U.
        r.run(0.6)
        assert r.typed_text() == b"", "the injection was typed onto a line that may hold his answer digit"
        os.write(r.stdin_w, b"\x15")                   # Ctrl-U: he clears his line
        r.proxy._on_stdin_readable()
        assert r.until(lambda: r.typed_text().count(text.encode()) == 1)
        r.run(0.4)
        assert r.answers() == ["operator-chose-2"]     # still the human's choice, not the first option
        assert r.typed_text().count(text.encode()) == 1
    finally:
        r.close()

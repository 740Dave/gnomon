"""Round 2c of the leading-'!' rule (operator ruling g71-amendment-r2b-2026-10-02;
Diamond audit-refuse-leading-bang-r2b F1 P1, F2 P1, F3/F4 P2).

F1: the FINAL-bytes screen is the UNION (leading '!' + every byte class the upstream inject screens
    refuse), ONE shared function (``leading_bang.screen_text`` / ``screen_event``), applied at the
    gate (early + after the hooks pipeline), in approve(), and at the writer funnel.
F2: self-verb arguments are content: compact's direction and directive_path contents are screened
    with the same union before anything is typed.
P2: a screen refusal raised by the writer backstop is contained on every caller path and the pump
    keeps delivering later legitimate injections.
"""

from __future__ import annotations

import ast
import json
import os
import socket
import sys

import pytest

from hooks_support import Rig

from gnomon.harness import compact_gate
from gnomon.harness import leading_bang as lb
from gnomon.harness.hooks import Decision, InjectionEvent, Outcome
from gnomon.harness.monitor import Notice, NoticeKind
from gnomon.harness.writer import SingleWriteSite
from gnomon.harness.actuator import COMPACT_APPROVAL_FIELD

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "gnomon", "harness")

#: MODIFY prefixes the r2b Diamond reproduced (typed a bang line on 58e2394) plus siblings.
EDIT_PREFIXES = ["\x7f!", "\x15!", "\x08!", "ok\r!", "\x00!", "\x03!", "\x1b!", "\x1b[2K!",
                 "ab\x7f\x7f!", " \x7f!", "\r!", "x\x1b[201~\r!"]


def _rig(tmp_path, handlers=None, point="inbound-injection", context=50.0):
    harness = {"hook_manifest": {point: handlers}} if handlers else None
    return Rig(tmp_path, read_context=lambda: context, harness=harness)


def _refused(rig):
    return [r for r in rig.file_records() if r.get("kind") == "inject-refused"]


def _legit_typed(rig, text="legit-after"):
    """Type a clean message through the same rig and return the bytes the child received."""
    rig.child_bytes()
    out = rig.proxy._injector.inject_line(text, source="bus")
    return out, rig.child_bytes()


# -- the shared function ----------------------------------------------------------------------


@pytest.mark.parametrize("text,reasons", [
    ("\x7f!id", ["control-bytes"]), ("\x15!", ["control-bytes"]), ("ok\r!id", ["control-bytes"]),
    ("!id", ["leading-bang"]), ("！id", ["leading-bang"]), ("​!x", ["leading-bang"]),
    ("a\x1b[200~b", ["control-bytes"]), ("a\x1b[201~b", ["control-bytes"]),
    ("\x1b!", ["control-bytes"]), ("\x00", ["control-bytes"]),
])
def test_screen_text_is_the_union(text, reasons):
    assert lb.screen_text(text) == reasons


@pytest.mark.parametrize("text", ["", "id", "a\nb", "tab\there", "x!y", "multi\nline\n!inside paste",
                                  "café 中文 \U0001F600", "/compact foo"])
def test_screen_text_passes_ordinary_content(text):
    assert lb.screen_text(text) is None


def test_the_early_screens_and_the_final_screen_are_the_same_function():
    """No drift: compact_gate's byte predicate IS the leaf's, and the self/hub screens' control and
    bang verdicts equal screen_text's on every sample."""
    assert compact_gate.has_control_bytes is lb.has_control_bytes
    assert compact_gate.screen_text is lb.screen_text
    samples = ["\x7f!", "ok\r!", "!id", "a\x1b[201~", "plain", "a\nb", "\x00", "！x", "\x15"]
    for t in samples:
        shared = lb.screen_text(t) or []
        for early in (compact_gate.screen_self_inject(t), compact_gate.screen_hub_inject(t)):
            assert ("control-bytes" in (early or [])) == ("control-bytes" in shared), t
            if "control-bytes" not in shared:
                assert ("leading-bang" in (early or [])) == ("leading-bang" in shared), t


def test_screen_event_also_screens_the_compact_argument_typed_from_meta():
    assert lb.screen_event("/compact x", {"compact_argument": "x\x1b[201~\r!id"}) == ["control-bytes"]
    assert lb.screen_event("/compact x", {"compact_argument": "multi\nline"}) is None
    assert lb.screen_event("/compact x", None) is None


# -- F1: hook MODIFY handlers that edit the line --------------------------------------------


@pytest.mark.parametrize("prefix", EDIT_PREFIXES)
@pytest.mark.parametrize("path", ["self", "hub", "injector", "notice"])
def test_f1_a_modify_prefix_that_edits_the_line_into_a_bang_is_refused(tmp_path, prefix, path):
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": prefix}])
    try:
        if path == "self":
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "id"}, origin="self")
            assert out["ok"] is False
        elif path == "hub":
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "id"}, origin="hub")
            assert out["ok"] is False
        elif path == "injector":
            out = rig.proxy._injector.inject_line("id", source="bus")
            assert out.decision is Decision.DENY
        else:
            rig.proxy._type_notice(Notice(kind=NoticeKind.HEARTBEAT, text="id"))
        assert rig.child_bytes() == b"", f"{prefix!r}: bytes reached the PTY"
        refused = _refused(rig)
        assert refused, "no inject-refused record"
        assert refused[-1]["stage"] == "post-transform"
        assert set(refused[-1]["reasons"]) <= {"control-bytes", "leading-bang"} and refused[-1]["reasons"]
    finally:
        rig.close()


def test_f1_the_command_handler_kind_is_refused_too(tmp_path):
    script = tmp_path / "mod.py"
    script.write_text("import json, sys\nev = json.load(sys.stdin)\n"
                      "print(json.dumps({'decision': 'modify', 'content': '\\x7f!' + ev['content']}))\n")
    rig = _rig(tmp_path, [{"kind": "command", "command": [sys.executable, str(script)]}])
    try:
        out = rig.proxy._injector.inject_line("id", source="bus")
        assert out.decision is Decision.DENY
        assert rig.child_bytes() == b""
        assert _refused(rig)[-1]["stage"] == "post-transform"
    finally:
        rig.close()


def test_f1_a_session_with_an_editing_hook_keeps_delivering_clean_content(tmp_path):
    """A prefix that is NOT an edit/bang still rewrites and types (the screen is not a blanket deny)."""
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": "[bus] ", "sources": ["bus"]}])
    try:
        rig.hub_down({"cmd": "inject", "text": "hello"})
        assert b"[bus] hello\r" in rig.child_bytes()
        assert _refused(rig) == []
    finally:
        rig.close()


def test_f1_approve_never_releases_a_held_control_edited_bang(tmp_path):
    rig = _rig(tmp_path)
    try:
        wrote = []
        ev = InjectionEvent(source="bus", content="x\r!id", trigger="t")
        rig.gate._held[77] = (ev, wrote.append, "planted")
        assert rig.gate.approve(77) == []
        assert wrote == []
        assert _refused(rig)[-1]["stage"] == "post-transform"
        assert "control-bytes" in _refused(rig)[-1]["reasons"]
        # and a clean held event is still released
        ok = InjectionEvent(source="bus", content="clean", trigger="t")
        rig.gate._held[78] = (ok, wrote.append, "planted")
        assert rig.gate.approve(78) == [78]
        assert wrote == [ok]
    finally:
        rig.close()


class _EditGate:
    """A misbehaving gate: allows everything and emits a content-edited event."""

    def __init__(self, new_content, meta=None):
        self.new_content, self.meta = new_content, meta
        self.recorded = []

    def record(self, kind, **fields):
        self.recorded.append(dict(fields, kind=kind))
        return {}

    def decide(self, event, emit):
        emit(InjectionEvent(source=event.source, content=self.new_content, trigger=event.trigger,
                            meta=self.meta if self.meta is not None else event.meta))
        return Outcome(Decision.MODIFY, "x")


@pytest.mark.parametrize("content", ["\x7f!id", "\x15!id", "\x08!id", "ok\r!id", "\x1b[200~x", "!id"])
def test_f1_the_writer_funnel_refuses_the_union_from_a_misbehaving_gate(content):
    a, b = socket.socketpair()
    try:
        gate = _EditGate(content)
        w = SingleWriteSite(a.fileno(), gate=gate)
        wrote = []
        with pytest.raises(ValueError) as ei:
            w.admit(InjectionEvent(source="bus", content="id", trigger="t"), wrote.append)
        assert wrote == []
        assert isinstance(ei.value, lb.ScreenRefused) and ei.value.reasons
        assert gate.recorded and gate.recorded[-1]["kind"] == "inject-refused"
        assert gate.recorded[-1]["stage"] == "funnel"
        # a bang still raises the round-2b exception type
        if "!" in content and "\x1b" not in content and lb.REASON in ei.value.reasons:
            assert isinstance(ei.value, lb.LeadingBangRefused)
        # the operator source is exempt at the funnel (their own keystrokes)
        w.admit(InjectionEvent(source="operator", content="id", trigger="t"), wrote.append)
        assert wrote and wrote[0].content == content
    finally:
        a.close()
        b.close()


def test_f1_the_funnel_screens_the_compact_argument_in_meta():
    a, b = socket.socketpair()
    try:
        w = SingleWriteSite(a.fileno(), gate=_EditGate("/compact ok", meta={"compact_argument": "x\x1b[201~\r!id"}))
        with pytest.raises(ValueError):
            w.admit(InjectionEvent(source="self-verb", content="/compact ok", trigger="compact"), lambda ev: None)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("content", ["\x7f!id", "ok\r!id", "!id"])
def test_f1_no_gate_admit_refuses_the_union(content):
    a, b = socket.socketpair()
    try:
        w = SingleWriteSite(a.fileno())
        wrote = []
        out = w.admit(InjectionEvent(source="bus", content=content, trigger="t"), wrote.append)
        assert out.decision is Decision.DENY and wrote == []
        with pytest.raises(ValueError):
            w.inject(content.encode())
    finally:
        a.close()
        b.close()


# -- F2: compact direction / directive_path -------------------------------------------------

BAD_DIRECTIONS = ["x\x1b[201~\r!id", "x\x1b[201~\n!id", "x\r!id", "a\x1b[200~b", "x\x15!id", "x\x7f", "\x00"]


@pytest.mark.parametrize("direction", BAD_DIRECTIONS)
@pytest.mark.parametrize("origin", ["self", "hub"])
def test_f2_compact_direction_with_control_bytes_is_refused_and_types_nothing(tmp_path, direction, origin):
    rig = _rig(tmp_path, context=95.0)
    try:
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "direction": direction, COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin=origin)
        assert out["ok"] is False and out["decision"] == "refused", out
        assert "control-bytes" in out["reasons"]
        assert "REFUSED" in out["error"] and "nothing was typed" in out["error"]
        assert rig.child_bytes() == b"", "bytes were typed for a refused compact"
        assert any(r.get("kind") == "inject-refused" and r.get("stage") == "compact-argument"
                   for r in rig.file_records())
    finally:
        rig.close()


@pytest.mark.parametrize("direction", BAD_DIRECTIONS[:3])
def test_f2_the_directive_path_file_contents_are_screened_too(tmp_path, direction):
    # a file is read in universal-newline mode (CR/CRLF arrive as \n), so only ESC-style bytes matter
    # for it; write the bytes that survive as control bytes.
    path = tmp_path / "directive.md"
    path.write_bytes(("\x1b[201~\x15!id " + direction.replace("\r", "")).encode("utf-8"))
    rig = _rig(tmp_path, context=95.0)
    try:
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "directive_path": str(path), COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin="self")
        assert out["ok"] is False and out["decision"] == "refused", out
        assert "directive_path" in out["error"]
        assert rig.child_bytes() == b""
        assert any(r.get("stage") == "compact-argument" for r in rig.file_records()
                   if r.get("kind") == "inject-refused")
    finally:
        rig.close()


def test_f2_a_legitimate_multiline_directive_still_compacts(tmp_path):
    """What is allowed: tab and newline (typed inside ONE bracketed paste, where a newline never
    submits), unicode, and a '!' anywhere in the direction (it is typed after '/compact ', never as
    the first character)."""
    rig = _rig(tmp_path, context=95.0)
    try:
        direction = "Keep: the plan.\n\t- item one!\n!not a command\né中"
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "direction": direction, COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin="self")
        assert out["ok"] is True, out
        got = rig.child_bytes()
        assert b"/compact" in got and b"\x1b[200~" + direction.encode("utf-8") + b"\x1b[201~" in got
        assert _refused(rig) == []
    finally:
        rig.close()


def test_f2_a_directive_file_with_crlf_line_endings_still_compacts(tmp_path):
    path = tmp_path / "directive.md"
    path.write_bytes(b"line one\r\nline two\r\n")
    rig = _rig(tmp_path, context=95.0)
    try:
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "directive_path": str(path), COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin="self")
        assert out["ok"] is True, out
        assert b"line one\nline two" in rig.child_bytes()
    finally:
        rig.close()


def test_f2_a_modify_hook_cannot_smuggle_control_bytes_into_the_compact_via_meta_only(tmp_path):
    """The typed argument comes from meta, not content: the gate screens both, post-pipeline."""
    rig = _rig(tmp_path, context=95.0)
    try:
        ev = InjectionEvent(source="self-verb", content="/compact x", trigger="compact",
                            meta={"compact_argument": "x\x1b[201~\r!id"})
        out = rig.proxy._writer.admit(ev, rig.proxy._injector._emit_compact)
        assert out.decision is Decision.DENY
        assert rig.child_bytes() == b""
    finally:
        rig.close()


# -- P2: a refusal never kills the pump -----------------------------------------------------


class _OnceBadGate:
    """Wraps the real gate: the FIRST decide emits a bang event (a misbehaving gate; a plain '!' so the
    round-2b backstop raises on 58e2394 too: the failure there is the uncaught raise), later ones behave. This forces the writer backstop to fire mid-stream."""

    def __init__(self, real):
        self.real, self.n = real, 0

    def decide(self, event, emit):
        self.n += 1
        if self.n == 1:
            emit(InjectionEvent(source=event.source, content="!" + event.content, trigger=event.trigger))
            return Outcome(Decision.MODIFY, "bad")
        return self.real.decide(event, emit)

    def __getattr__(self, name):
        return getattr(self.real, name)


def _arm_bad_gate(rig):
    rig.proxy._writer.set_gate(_OnceBadGate(rig.gate))


def _wait_typed(rig, needle, tries=5):
    got = b""
    for _ in range(tries):
        got += rig.child_bytes()
        if needle in got:
            break
    return got


def test_p2_control_channel_contains_a_backstop_refusal_and_keeps_serving(tmp_path):
    rig = _rig(tmp_path)
    try:
        _arm_bad_gate(rig)
        rig.hub_down({"cmd": "inject", "text": "first"})  # backstop fires inside the pump read path
        assert b"first" not in rig.child_bytes() and b"\x7f" not in rig.child_bytes()
        rig.hub_down({"cmd": "inject", "text": "second-legit"})
        assert b"second-legit\r" in _wait_typed(rig, b"second-legit")
    finally:
        rig.close()


def test_p2_control_channel_handle_line_returns_a_loud_refusal_to_the_caller(tmp_path):
    rig = _rig(tmp_path)
    try:
        _arm_bad_gate(rig)
        chan = rig.proxy._control
        resp = chan._handle_line(json.dumps({"cmd": "inject", "text": "first"}).encode())
        assert resp["ok"] is False and resp["decision"] == "refused"
        assert "backstop" in resp["error"] and "nothing was typed" in resp["error"]
        assert resp["reasons"]
        assert any(r.get("stage") == "funnel" for r in _refused(rig))
        resp2 = chan._handle_line(json.dumps({"cmd": "inject", "text": "second"}).encode())
        assert resp2["ok"] is True
    finally:
        rig.close()


def test_p2_notice_path_contains_a_backstop_refusal_and_the_next_notice_types(tmp_path):
    rig = _rig(tmp_path)
    try:
        _arm_bad_gate(rig)
        rig.proxy._type_notice(Notice(kind=NoticeKind.HEARTBEAT, text="dropped"))  # must not raise
        assert rig.child_bytes() == b""
        assert rig.proxy.screen_refusals and rig.proxy.screen_refusals[-1][0] == "notice:" + NoticeKind.HEARTBEAT.value
        rig.proxy._type_notice(Notice(kind=NoticeKind.HEARTBEAT, text="delivered"))
        assert b"delivered\r" in _wait_typed(rig, b"delivered")
    finally:
        rig.close()


def test_p2_step_survives_a_backstop_refusal_from_monitor_poll_and_later_steps_deliver(tmp_path):
    rig = _rig(tmp_path)
    try:
        _arm_bad_gate(rig)
        calls = {"n": 0}

        def poll():
            calls["n"] += 1
            if calls["n"] == 1:
                rig.proxy.inject_notice(Notice(kind=NoticeKind.FYI, text="notice-one"))
            elif calls["n"] == 2:
                rig.proxy.inject_notice(Notice(kind=NoticeKind.FYI, text="notice-two"))

        class _Mon:
            def poll(self):
                poll()

            def maybe_heartbeat(self):
                pass

        rig.proxy._monitor = _Mon()
        rig.proxy._submitted_notice_kinds = frozenset({NoticeKind.FYI})
        rig.proxy._line_hold_reason = lambda: None
        rig.proxy._boot_blocks = lambda: False
        assert rig.proxy.step(0) is True  # first poll: refused, contained
        assert rig.proxy.step(0) is True  # second poll: delivered
        got = _wait_typed(rig, b"notice-two")
        assert b"notice-one" not in got and b"notice-two\r" in got
    finally:
        rig.close()


def test_p2_step_contains_a_refusal_raised_out_of_a_readable_handler(tmp_path):
    rig = _rig(tmp_path)
    try:
        def boom():
            raise lb.LeadingBangRefused("forced")

        rig.proxy._on_control_readable = boom
        rig.hub.send_down({"cmd": "inject", "text": "x"})
        assert rig.proxy.step(0.2) is True
        assert rig.proxy.screen_refusals and rig.proxy.screen_refusals[-1][0] == "control"
    finally:
        rig.close()


# -- byte-identity guard for allowed content ---------------------------------------------------


@pytest.mark.parametrize("text", ["hello", "multi\nline", "tab\tsep", "x!y", "/clear", "café 中"])
def test_allowed_content_is_typed_unchanged_through_every_gated_path(tmp_path, text):
    rig = _rig(tmp_path)
    try:
        out = rig.proxy._injector.inject_line(text, source="bus")
        assert out.decision is Decision.ALLOW
        assert text.encode("utf-8") in rig.child_bytes()
        assert _refused(rig) == []
    finally:
        rig.close()


# -- hook closure + leaf discipline ------------------------------------------------------------


def test_leading_bang_stays_a_stdlib_only_leaf():
    src = open(os.path.join(HARNESS, "leading_bang.py"), encoding="utf-8").read()
    mods = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "leading_bang must not import package modules"
            mods.add((node.module or "").split(".")[0])
    assert mods <= {"__future__", "typing", "unicodedata", "re"}, mods


def test_the_union_screen_does_not_load_gnomon_flags_in_a_fresh_interpreter():
    import subprocess

    code = ("import sys\n"
            "from gnomon.harness import leading_bang, hooks, writer, compact_gate, actuator\n"
            "print('gnomon.flags' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"

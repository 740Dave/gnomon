"""Round 2d of the leading-'!' rule (operator ruling g71-amendment-r2c-2026-10-02 (3);
Diamond audit-refuse-leading-bang-r2c F1, F2, F3, P3-1).

F1: C1 controls U+0080-U+009F are refused by the ONE union screen, as code points and wherever bytes are
    decoded into text before they are typed (writer depth-0 bytes, a directive file, a control frame).
F2: a lone surrogate (a str that cannot be encoded as UTF-8) is refused before any write, AND any
    exception out of dispatch is contained by the control channel and by step()/run(): recorded loudly,
    the next legitimate inject is typed.
F3: with ``inject_long_line_as_paste`` OFF, a single-line payload whose 1022-byte read-split
    leaves a chunk STARTING with '!' (or whitespace/Cf + '!') is bracketed; flag ON is byte-identical.
P3-1: every ``_contained`` wrapper in step() is pinned by a forced-failure test (mutation-killing).
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest

from hooks_support import Rig

from gnomon.harness import leading_bang as lb
from gnomon.harness import proxy as proxy_mod
from gnomon.harness.constants import BRACKETED_PASTE_END, BRACKETED_PASTE_START
from gnomon.harness.hooks import Decision, content_fields
from gnomon.harness.monitor import Notice, NoticeKind
from gnomon.harness.writer import SingleWriteSite
from gnomon.harness.actuator import COMPACT_APPROVAL_FIELD

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: The measured macOS PTY read size, written out here on purpose: the test models the
#: kernel, so it must not borrow the constant it is checking.
PTY_READ_CHUNK_BYTES = 1022


def _rig(tmp_path, handlers=None, point="inbound-injection", context=50.0):
    harness = {"hook_manifest": {point: handlers}} if handlers else None
    return Rig(tmp_path, read_context=lambda: context, harness=harness)


def _refused(rig):
    return [r for r in rig.file_records() if r.get("kind") == "inject-refused"]


def _typed(rig, needle, tries=5):
    got = b""
    for _ in range(tries):
        got += rig.child_bytes()
        if needle in got:
            break
    return got


C1_SAMPLES = ["\x80", "\x85", "\x90", "\x9b", "\x9d", "\x9f"]
SURROGATES = ["\ud800", "\udbff", "\udc00", "\udc80", "\udcff", "\udfff"]


# -- the shared function ------------------------------------------------------------------------


@pytest.mark.parametrize("char", C1_SAMPLES)
@pytest.mark.parametrize("shape", ["{c}!id", "{c}2K!", "ab{c}cd", "{c}"])
def test_f1_screen_text_refuses_every_c1_control_anywhere(char, shape):
    assert lb.screen_text(shape.format(c=char)) is not None
    assert "control-bytes" in lb.screen_text(shape.format(c=char))
    assert lb.has_control_bytes(shape.format(c=char))


@pytest.mark.parametrize("text", [" x", "¡", "ÿ", "x y", "Ā", "café 中"])
def test_f1_the_c1_range_is_exactly_0080_to_009f(text):
    assert lb.screen_text(text) is None  # U+00A0 and up are ordinary text


@pytest.mark.parametrize("char", SURROGATES)
@pytest.mark.parametrize("shape", ["{c}", "{c}x", "ok {c}", "!{c}"])
def test_f2_screen_text_refuses_every_lone_surrogate(char, shape):
    text = shape.format(c=char)
    assert "control-bytes" in lb.screen_text(text)
    with pytest.raises(UnicodeEncodeError):  # the reason it must be refused BEFORE a write
        text.encode("utf-8")


def test_f2_a_paired_surrogate_astral_character_is_not_a_lone_surrogate():
    assert lb.screen_text("\U0001F600 ok \U00010000") is None  # a real astral str has no Cs code points


def test_f2_screen_event_screens_a_surrogate_in_the_compact_argument():
    assert lb.screen_event("/compact x", {"compact_argument": "x\ud800"}) == ["control-bytes"]
    assert lb.screen_event("/compact x", {"compact_argument": "x\x9b"}) == ["control-bytes"]


def test_f2_content_fields_never_raises_on_a_lone_surrogate():
    f = content_fields("\ud800x")
    assert f["content_len"] == 2 and len(f["content_sha256"]) == 64 and "ud800" in f["content_preview"]
    assert content_fields("\udc80")["content_sha256"] == content_fields("\udc80")["content_sha256"]
    # unchanged for what already encoded (the surrogateescape arm)
    import hashlib

    assert content_fields("\udcff")["content_sha256"] == hashlib.sha256(b"\xff").hexdigest()
    assert content_fields("abc")["content_sha256"] == hashlib.sha256(b"abc").hexdigest()


# -- F1 / F2 through every real path ---------------------------------------------------------------

BAD_TEXTS = ["\u009b!id", "\u009b2K!id", "\u0090!id", "\u009d!id", "\u0080!id", "\u009fid", "ok \u0085 mid",
             "\ud800x", "\udcff", "x\ud800!id"]


@pytest.mark.parametrize("text", BAD_TEXTS)
@pytest.mark.parametrize("path", ["self", "hub", "injector", "notice", "hubframe"])
def test_f1_f2_c1_and_surrogate_text_is_refused_on_every_path_and_nothing_is_typed(tmp_path, text, path):
    rig = _rig(tmp_path)
    try:
        if path in ("self", "hub"):
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": text}, origin=path)
            assert out["ok"] is False
        elif path == "injector":
            out = rig.proxy._injector.inject_line(text, source="bus")
            assert out.decision is Decision.DENY
        elif path == "notice":
            rig.proxy._type_notice(Notice(kind=NoticeKind.HEARTBEAT, text=text))
        else:
            rig.hub_down({"cmd": "inject", "text": text})
        assert rig.child_bytes() == b""
        # the refusal is on the record (the record of a surrogate's refusal must itself not raise)
        if path in ("injector", "notice"):  # the gate denies and records (kind injection, decision deny)
            assert [r for r in rig.records() if r.get("decision") == "deny"], "no deny record"
        # and the pump delivers the next legitimate injection
        rig.hub_down({"cmd": "inject", "text": "legit-after"})
        assert b"legit-after\r" in _typed(rig, b"legit-after")
    finally:
        rig.close()


@pytest.mark.parametrize("prefix", ["\u009b", "\x90", "\ud800", "\u009b2K"])
def test_f1_f2_a_modify_prefix_that_adds_c1_or_a_surrogate_is_refused_after_the_pipeline(tmp_path, prefix):
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": prefix}])
    try:
        out = rig.proxy._injector.inject_line("id", source="bus")
        assert out.decision is Decision.DENY
        assert rig.child_bytes() == b""
        refused = _refused(rig)
        assert refused and "control-bytes" in refused[-1]["reasons"]
    finally:
        rig.close()


def test_f1_f2_the_gate_record_of_a_surrogate_event_is_written(tmp_path):
    rig = _rig(tmp_path)
    try:
        out = rig.proxy._injector.inject_line("\ud800x", source="bus")
        assert out.decision is Decision.DENY
        deny = [r for r in rig.records() if r.get("decision") == "deny"]
        assert deny and deny[-1]["content_len"] == 2 and "control-bytes" in deny[-1]["reason"]
    finally:
        rig.close()


def _pair():
    a, b = socket.socketpair()
    b.setblocking(False)
    return a, b


@pytest.mark.parametrize("data", [b"\x9b!x", b"\xc2\x9b!x", b"\xc2\x80", b"\xff\xfe", b"\x80", b"a\xe2\x28\xa1"])
def test_f1_f2_raw_depth0_bytes_are_refused_wherever_they_decode_to_c1_or_a_surrogate(data):
    a, b = _pair()
    try:
        w = SingleWriteSite(a.fileno())
        with pytest.raises(lb.ScreenRefused):
            w.inject(data)
        assert w.injected_bytes == b""
        with pytest.raises(BlockingIOError):
            b.recv(10)
    finally:
        a.close()
        b.close()


def test_f1_f2_raw_depth0_valid_utf8_text_is_still_written_byte_identically():
    a, b = _pair()
    try:
        w = SingleWriteSite(a.fileno())
        payloads = [b"abc", b"x!y", "café 中 \U0001F600".encode("utf-8"), b"\xc2\xa0nbsp"]
        for p in payloads:
            w.inject(p)
        assert b.recv(65536) == b"".join(payloads)
    finally:
        a.close()
        b.close()


@pytest.mark.parametrize("direction", ["x\u009b", "\u009d]0;t", "ok\ud800", "\u0080"])
@pytest.mark.parametrize("origin", ["self", "hub"])
def test_f1_f2_compact_direction_with_c1_or_surrogate_is_refused_and_types_nothing(tmp_path, direction, origin):
    rig = _rig(tmp_path, context=95.0)
    try:
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "direction": direction, COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin=origin)
        assert out["ok"] is False and out["decision"] == "refused", out
        assert "control-bytes" in out["reasons"]
        assert rig.child_bytes() == b""
    finally:
        rig.close()


def test_f1_a_directive_file_decoded_from_utf8_c1_is_refused(tmp_path):
    path = tmp_path / "d.md"
    path.write_bytes("keep this \u009b2K!id".encode("utf-8"))
    rig = _rig(tmp_path, context=95.0)
    try:
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "directive_path": str(path), COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin="self")
        assert out["ok"] is False and out["decision"] == "refused" and "directive_path" in out["error"]
        assert rig.child_bytes() == b""
    finally:
        rig.close()


def test_f1_an_unrelated_legit_compact_with_non_c1_unicode_still_compacts(tmp_path):
    rig = _rig(tmp_path, context=95.0)
    try:
        direction = "keep: café   中 \U0001F600"
        out = rig.proxy.actuator.dispatch({"cmd": "compact", "direction": direction, COMPACT_APPROVAL_FIELD: True,
                                           "approval_note": "test"}, origin="self")
        assert out["ok"] is True, out
        assert direction.encode("utf-8") in rig.child_bytes()
    finally:
        rig.close()


# -- F2: the pump survives ANY exception out of dispatch --------------------------------------------


class _BoomActuator:
    """An actuator whose dispatch raises a non-refusal exception on the first call."""

    def __init__(self, real, exc):
        self.real, self.exc, self.n = real, exc, 0

    def dispatch(self, command, origin=None):
        self.n += 1
        if self.n == 1:
            raise self.exc
        return self.real.dispatch(command, origin=origin) if origin is not None else self.real.dispatch(command)


@pytest.mark.parametrize("exc", [UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed"),
                                 RuntimeError("boom"), KeyError("k"), OSError(5, "io")])
def test_f2_control_channel_contains_any_exception_and_the_next_command_types(tmp_path, exc):
    rig = _rig(tmp_path)
    try:
        chan = rig.proxy._control
        chan._actuator = _BoomActuator(chan._actuator, exc)
        resp = chan._handle_line(json.dumps({"cmd": "inject", "text": "first"}).encode())
        assert resp["ok"] is False and "internal error" in resp["error"] and type(exc).__name__ in resp["error"]
        assert chan.dispatch_errors and type(exc).__name__ in chan.dispatch_errors[-1]
        resp2 = chan._handle_line(json.dumps({"cmd": "inject", "text": "second-legit"}).encode())
        assert resp2["ok"] is True
        assert b"second-legit\r" in _typed(rig, b"second-legit")
    finally:
        rig.close()


def test_f2_step_survives_an_unexpected_dispatch_error_and_the_next_inject_is_typed(tmp_path):
    """The measured r2c F2 kill: a lone surrogate over the hub control channel raised out of step()."""
    rig = _rig(tmp_path)
    try:
        rig.hub.send_down({"cmd": "inject", "text": "\ud800x"})
        assert rig.proxy.step(0.2) is True  # c50e667: raises UnicodeEncodeError out of step()
        assert rig.child_bytes() == b""
        rig.hub.send_down({"cmd": "inject", "text": "next-legit"})
        assert rig.proxy.step(0.2) is True
        assert b"next-legit\r" in _typed(rig, b"next-legit")
    finally:
        rig.close()


def test_f2_step_contains_a_non_refusal_exception_from_the_control_handler(tmp_path):
    rig = _rig(tmp_path)
    try:
        def boom():
            raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")

        rig.proxy._on_control_readable = boom
        rig.hub.send_down({"cmd": "inject", "text": "x"})
        assert rig.proxy.step(0.2) is True
        assert rig.proxy.pump_errors and rig.proxy.pump_errors[-1][:2] == ("control", "UnicodeEncodeError")
    finally:
        rig.close()


class _NullRaw:
    def __init__(self, fd):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_f2_run_survives_a_step_that_raises_and_keeps_pumping(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    try:
        monkeypatch.setattr(proxy_mod, "RawMode", _NullRaw)
        rig.proxy._reap = lambda: 0  # the Rig has pid=0: a real waitpid(0) would wait on ANY child of the suite
        monkeypatch.setattr(proxy_mod.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def step(timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise UnicodeEncodeError("utf-8", "\ud800", 0, 1, "surrogates not allowed")
            if calls["n"] == 3:
                rig.proxy._child_alive = False
            return rig.proxy._child_alive

        rig.proxy.step = step
        rig.proxy.run()
        assert calls["n"] == 3
        assert rig.proxy.pump_errors and rig.proxy.pump_errors[0][:2] == ("run", "UnicodeEncodeError")
    finally:
        rig.close()


def test_f2_run_does_not_spin_forever_on_a_persistent_fault(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    try:
        monkeypatch.setattr(proxy_mod, "RawMode", _NullRaw)
        rig.proxy._reap = lambda: 0  # the Rig has pid=0: a real waitpid(0) would wait on ANY child of the suite
        monkeypatch.setattr(proxy_mod.time, "sleep", lambda s: None)
        calls = {"n": 0}

        def step(timeout=None):
            calls["n"] += 1
            if calls["n"] > 5 * proxy_mod._MAX_CONSECUTIVE_STEP_FAILURES:  # a regression must fail, not hang
                rig.proxy._child_alive = False
            raise RuntimeError("persistent")

        rig.proxy.step = step
        with pytest.raises(RuntimeError, match="persistent"):
            rig.proxy.run()
        assert calls["n"] == proxy_mod._MAX_CONSECUTIVE_STEP_FAILURES
    finally:
        rig.close()


def test_f2_run_does_not_swallow_keyboard_interrupt(tmp_path, monkeypatch):
    rig = _rig(tmp_path)
    try:
        monkeypatch.setattr(proxy_mod, "RawMode", _NullRaw)
        rig.proxy._reap = lambda: 0  # the Rig has pid=0: a real waitpid(0) would wait on ANY child of the suite

        def step(timeout=None):
            raise KeyboardInterrupt()

        rig.proxy.step = step
        with pytest.raises(KeyboardInterrupt):
            rig.proxy.run()
    finally:
        rig.close()


# -- P3-1: every _contained wrapper in step() is pinned ---------------------------------------------


class _Mon:
    def __init__(self, poll_exc=None, hb_exc=None):
        self.poll_exc, self.hb_exc = poll_exc, hb_exc

    def poll(self):
        if self.poll_exc:
            raise self.poll_exc

    def maybe_heartbeat(self):
        if self.hb_exc:
            raise self.hb_exc


class _FdHandler:
    """A local-control / hub-link stand-in whose readable handler raises."""

    def __init__(self, fd, exc):
        self._fd, self._exc = fd, exc

    def fds(self):
        return [self._fd]

    def owns(self, fd):
        return fd == self._fd

    def on_readable(self, fd):
        raise self._exc

    def tick(self):
        pass


EXCS = [lb.LeadingBangRefused("forced"), RuntimeError("boom"), UnicodeEncodeError("utf-8", "\ud800", 0, 1, "x")]


@pytest.mark.parametrize("exc", EXCS, ids=["refusal", "runtime", "unicode"])
@pytest.mark.parametrize("site", ["monitor.poll", "monitor.heartbeat", "stdin", "control", "local-control",
                                  "hub-link"])
def test_p31_every_contained_site_in_step_survives_a_failing_handler(tmp_path, site, exc):
    rig = _rig(tmp_path)
    extra = []
    try:
        p = rig.proxy

        def boom(*a):
            raise exc

        if site == "monitor.poll":
            p._monitor = _Mon(poll_exc=exc)
        elif site == "monitor.heartbeat":
            p._monitor = _Mon(hb_exc=exc)
        elif site == "stdin":
            p._on_stdin_readable = boom
            os.write(rig.stdin_w, b"x")
        elif site == "control":
            p._on_control_readable = boom
            rig.hub.send_down({"cmd": "inject", "text": "x"})
        else:
            s1, s2 = socket.socketpair()
            extra += [s1, s2]
            handler = _FdHandler(s1.fileno(), exc)
            if site == "local-control":
                p._local_control = handler
            else:
                p._hub_link = handler
            s2.send(b"x")
        assert p.step(0.2) is True  # unwrapping the wrapper lets the exception escape here
        assert p.screen_refusals or p.pump_errors
        where = (p.screen_refusals or p.pump_errors)[-1][0]
        assert where == site
        # the pump is alive: the next step with a clean handler still works
        assert p.step(0) is True
    finally:
        for s in extra:
            s.close()
        rig.close()


# -- F3: the flag-OFF 1022-byte split ----------------------------------------------------------------

BANG = "!id"


def _writes_flag_off(rig, text, source="self-verb"):
    rig.proxy._injector._long_line_paste_bytes = None
    rig.child_bytes()
    out = rig.proxy._injector.inject_line(text, source=source)
    return out, rig.child_bytes()


def _unbracketed_chunk_starts(writes_bytes: bytes):
    """The X43 model of the PTY: the child's reads are <=1022-byte cuts of the bytes written; a
    bracketed paste is accumulated whole (measured), so only an UNBRACKETED payload's chunks matter."""
    body = writes_bytes[:-1] if writes_bytes.endswith(b"\r") else writes_bytes
    if body.startswith(BRACKETED_PASTE_START):
        return []
    return [body[i:i + PTY_READ_CHUNK_BYTES] for i in range(PTY_READ_CHUNK_BYTES, len(body), PTY_READ_CHUNK_BYTES)]


@pytest.mark.parametrize("pad", [1022, 2044, 3066])
@pytest.mark.parametrize("source", ["self-verb", "bus", "monitor-guidance"])
def test_f3_flag_off_a_bang_at_a_read_split_is_bracketed_not_left_to_open_bash_mode(tmp_path, pad, source):
    rig = _rig(tmp_path)
    try:
        out, got = _writes_flag_off(rig, "a" * pad + BANG, source=source)
        assert out.decision is Decision.ALLOW
        raw = ("a" * pad + BANG).encode()
        assert got == BRACKETED_PASTE_START + raw + BRACKETED_PASTE_END + b"\r"
        for chunk in _unbracketed_chunk_starts(got):
            assert not lb.has_leading_bang(chunk.decode("utf-8", "ignore"))
    finally:
        rig.close()


@pytest.mark.parametrize("lead", ["!id", "  !id", "​!id", "！id", "\t!x"])
def test_f3_every_refused_leading_class_at_a_chunk_start_is_covered(tmp_path, lead):
    rig = _rig(tmp_path)
    try:
        _, got = _writes_flag_off(rig, "a" * 1022 + lead)
        assert got.startswith(BRACKETED_PASTE_START), repr(got[:12])
    finally:
        rig.close()


def test_f3_a_chunk_cut_mid_character_before_a_bang_is_covered(tmp_path):
    rig = _rig(tmp_path)
    try:
        text = "a" * 1021 + "é" + BANG  # the 2-byte char straddles byte 1022: the next chunk opens with its tail
        assert text.encode().index(b"!") > 1022
        _, got = _writes_flag_off(rig, text)
        assert got.startswith(BRACKETED_PASTE_START)
    finally:
        rig.close()


def test_f3_a_hub_origin_payload_is_covered_through_the_actuator(tmp_path):
    rig = _rig(tmp_path)
    try:
        rig.proxy._injector._long_line_paste_bytes = None
        out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "a" * 1022 + BANG}, origin="hub")
        assert out["ok"] is True
        assert rig.child_bytes().startswith(BRACKETED_PASTE_START)
    finally:
        rig.close()


@pytest.mark.parametrize("text", ["a" * 1021 + BANG, "a" * 1500, "a" * 1022 + "b!id", "a" * 1023 + "!", "a" * 600,
                                  "x" * 1022 + "ok", "hello"])
def test_f3_flag_off_every_other_payload_is_written_exactly_as_before(tmp_path, text):
    """Byte-identity guard: no chunk of these starts with a bang class, so they stay one unbracketed write
    (the legacy flag-OFF behaviour, differentially identical to c50e667) + a separate CR."""
    rig = _rig(tmp_path)
    try:
        _, got = _writes_flag_off(rig, text)
        assert got == text.encode() + b"\r"
    finally:
        rig.close()


def test_f3_a_multiline_payload_is_untouched_even_ending_in_a_backslash(tmp_path):
    """Multi-line is ALREADY one bracketed paste: the F3 rewrite must not touch it (it would otherwise pad a
    trailing backslash with a space, since a long-line limit would be in force)."""
    rig = _rig(tmp_path)
    try:
        text = "a" * 1022 + "!x\nmore\\"
        _, got = _writes_flag_off(rig, text)
        assert got == BRACKETED_PASTE_START + text.encode() + BRACKETED_PASTE_END + b"\r"
    finally:
        rig.close()


def test_f3_the_operators_own_text_is_never_rewritten(tmp_path):
    rig = _rig(tmp_path)
    try:
        _, got = _writes_flag_off(rig, "a" * 1022 + BANG, source="operator")
        assert got == ("a" * 1022 + BANG).encode() + b"\r"
    finally:
        rig.close()


@pytest.mark.parametrize("pad", [1022, 2044])
def test_f3_flag_on_stays_byte_identical(tmp_path, pad):
    rig = _rig(tmp_path)
    try:
        rig.child_bytes()
        assert rig.proxy._injector._long_line_paste_bytes == 512
        rig.proxy._injector.inject_line("a" * pad + BANG, source="self-verb")
        assert rig.child_bytes() == BRACKETED_PASTE_START + ("a" * pad + BANG).encode() + BRACKETED_PASTE_END + b"\r"
    finally:
        rig.close()


def test_f3_split_chunk_leads_with_bang_unit():
    assert lb.split_chunk_leads_with_bang(b"a" * 1022 + b"!x", 1022)
    assert not lb.split_chunk_leads_with_bang(b"!" + b"a" * 1100, 1022)  # the FIRST piece is the screen's job
    assert not lb.split_chunk_leads_with_bang(b"a" * 1021 + b"!x", 1022)
    assert not lb.split_chunk_leads_with_bang(b"a" * 1022, 1022)
    assert not lb.split_chunk_leads_with_bang(b"", 1022)
    assert lb.split_chunk_leads_with_bang(b"abcdef!x", 3) is True
    assert lb.split_chunk_leads_with_bang(b"abc!xyz", 3) is True
    assert lb.split_chunk_leads_with_bang(b"abcd!xyz", 3) is False


# -- hook closure -----------------------------------------------------------------------------------


def test_the_hook_entry_modules_and_the_leaf_still_do_not_load_gnomon_flags():
    code = ("import sys\n"
            "import importlib\n"
            "mods = ['deny_guard','posttool_hook','lifecycle_hook','prompt_command_hook','statusline_hook',"
            "'subagent_stop_hook','compact_gate','hooks','writer','post_compact_boot','actuator','leading_bang',"
            "'control','proxy','injector']\n"
            "for m in mods:\n"
            "    importlib.import_module('gnomon.harness.' + m)\n"
            "print('gnomon.flags' in sys.modules)\n")
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=REPO))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False"

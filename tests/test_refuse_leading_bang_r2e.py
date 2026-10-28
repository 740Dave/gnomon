"""Leading-"!" refusal, round 2e (g71-amendment-r2d OPEN items (1)-(3); r2d Diamond
F1 P1, F2 P2, F3 P2).

(1) A long ``/token`` payload ("/" + "a"*1021 + "!id x") was typed as ONE raw write whose last 1022-byte
    chunk leads with "!" (g64 also forbids any raw single-line write over 512 bytes). The bare slash
    token is now bounded by LONG_LINE_PASTE_BYTES; a first word over the bound is typed as one bracketed
    paste. A legitimate long ``/command args`` still keeps its short token bare.
(2) run()'s failure cap also ends an INTERMITTENT persistent fault (a sliding window and a lifetime total);
    contained refusals never count.
(3) The last-byte chunk boundary, the long slash token, and the cap constants are pinned.
"""

from __future__ import annotations

import pytest

from hooks_support import Rig

from gnomon.harness import leading_bang as lb
from gnomon.harness import proxy as proxy_mod
from gnomon.harness.constants import BRACKETED_PASTE_END, BRACKETED_PASTE_START
from gnomon.harness.hooks import Decision
from gnomon.harness.injector import payload_phases
from gnomon.harness.leading_bang import ScreenRefused

#: The measured macOS PTY read size and the g64 raw-write bound, written out on purpose: the tests model
#: the kernel and the rule, so they must not borrow the constants they check.
PTY = 1022
RAW_MAX = 512


def _independent_leads_with_bang(chunk: bytes) -> bool:
    return chunk.decode("utf-8", "ignore").lstrip().startswith(("!", "！"))


def _rig(tmp_path):
    return Rig(tmp_path, read_context=lambda: 50.0)


def _record_writes(rig):
    writer = rig.proxy._injector._writer
    writes = []
    real = writer.inject

    def rec(data):
        writes.append(bytes(data))
        return real(data)

    writer.inject = rec
    return writes


def _unbracketed_chunks(writes):
    """Every PTY read chunk of every UNBRACKETED write (a bracketed paste is accumulated whole)."""
    out = []
    for w in writes:
        body = w[:-1] if w.endswith(b"\r") else w
        if body.startswith(BRACKETED_PASTE_START):
            continue
        out.append([body[i:i + PTY] for i in range(0, len(body), PTY)])
    return out


def _type(rig, text, flag_on, source="self-verb"):
    rig.proxy._injector._long_line_paste_bytes = 512 if flag_on else None
    rig.child_bytes()
    writes = _record_writes(rig)
    out = rig.proxy._injector.inject_line(text, source=source)
    return out, writes, rig.child_bytes()


# -- (1) the long slash token ----------------------------------------------------------------------


@pytest.mark.parametrize("flag_on", [True, False])
@pytest.mark.parametrize("pos", [1022, 2044, 3066])
@pytest.mark.parametrize("source", ["self-verb", "bus", "unattributed"])
def test_f1_a_long_slash_token_with_a_bang_at_a_read_split_never_leaves_a_bang_chunk(tmp_path, flag_on, pos, source):
    rig = _rig(tmp_path)
    try:
        text = "/" + "a" * (pos - 1) + "!id x"
        assert text.encode().index(b"!") == pos
        out, writes, got = _type(rig, text, flag_on, source=source)
        assert out.decision is Decision.ALLOW
        for chunks in _unbracketed_chunks(writes):
            for c in chunks[1:]:
                assert not _independent_leads_with_bang(c), repr(c[:8])
        assert got == BRACKETED_PASTE_START + text.encode() + BRACKETED_PASTE_END + b"\r"
    finally:
        rig.close()


@pytest.mark.parametrize("flag_on", [True, False])
def test_f1_multi_token_slash_commands_with_a_long_first_token(tmp_path, flag_on):
    rig = _rig(tmp_path)
    try:
        text = "/" + "a" * 1021 + "!id x y z"
        _, writes, got = _type(rig, text, flag_on)
        assert got.startswith(BRACKETED_PASTE_START)
        for chunks in _unbracketed_chunks(writes):
            assert all(not _independent_leads_with_bang(c) for c in chunks[1:])
    finally:
        rig.close()


@pytest.mark.parametrize("flag_on", [True, False])
def test_f1_g64_flag_on_no_raw_single_line_write_over_512_bytes_for_a_long_slash_line(tmp_path, flag_on):
    rig = _rig(tmp_path)
    try:
        for text in ("/" + "a" * 600 + " x", "/" + "a" * 1500 + " x y", "/" + "b" * 511 + " " + "c" * 3000):
            _, writes, _ = _type(rig, text, flag_on=True)
            for w in writes:
                body = w[:-1] if w.endswith(b"\r") else w
                if not body.startswith(BRACKETED_PASTE_START):
                    assert len(body) <= RAW_MAX, (len(body), text[:6])
    finally:
        rig.close()


@pytest.mark.parametrize("n", [1000, 1021, 1022, 2043, 2044, 3065, 3066, 5000])
def test_f1_payload_phases_with_an_overlong_token_is_one_bracketed_paste(n):
    for limit in (512, 1022):
        text = "/" + "a" * n + "!id x"
        if len(text.encode()) <= limit:
            continue  # a payload within the limit is one raw write (one PTY chunk), not this branch
        assert payload_phases(text, limit) == [BRACKETED_PASTE_START + text.encode() + BRACKETED_PASTE_END]


def test_f1_the_bare_token_bound_is_exactly_512_bytes_including_its_space():
    ok = "/" + "a" * 510  # token 511 bytes, + space = 512
    assert payload_phases(ok + " rest" + "z" * 600, 512) == [
        ok.encode() + b" ", BRACKETED_PASTE_START + b"rest" + b"z" * 600 + BRACKETED_PASTE_END]
    over = "/" + "a" * 511  # token 512 + space = 513
    assert payload_phases(over + " rest" + "z" * 600, 512) == [
        BRACKETED_PASTE_START + (over + " rest" + "z" * 600).encode() + BRACKETED_PASTE_END]


@pytest.mark.parametrize("cmd", ["/compact", "/clear", "/prepare-for-compact", "/model"])
@pytest.mark.parametrize("flag_on", [True, False])
def test_f1_a_legitimate_long_command_still_types_its_token_bare_then_the_args_as_a_paste(tmp_path, cmd, flag_on):
    """Claude Code recognises a slash command only when its token is typed raw, not pasted (flight 3)."""
    rig = _rig(tmp_path)
    try:
        args = "arg " * 700 + "!id"  # long, and the bang is inside the paste, never a raw chunk start
        _, writes, got = _type(rig, cmd + " " + args, flag_on)
        if flag_on:
            assert writes[0] == cmd.encode() + b" "
            assert writes[1] == BRACKETED_PASTE_START + args.encode() + BRACKETED_PASTE_END
            assert writes[-1] == b"\r"
        else:
            # flag OFF: the legacy raw write is kept unless a chunk would lead with '!'; either way nothing leads with '!'
            for chunks in _unbracketed_chunks(writes):
                assert all(not _independent_leads_with_bang(c) for c in chunks[1:])
    finally:
        rig.close()


def test_f1_a_short_slash_line_is_unchanged_byte_for_byte():
    assert payload_phases("/compact now", 512) == [b"/compact now"]
    assert payload_phases("/x " + "y" * 600, 512) == [b"/x ", BRACKETED_PASTE_START + b"y" * 600 + BRACKETED_PASTE_END]


def test_f1_hub_origin_long_slash_token_is_covered_through_the_actuator(tmp_path):
    rig = _rig(tmp_path)
    try:
        for flag_on in (True, False):
            rig.proxy._injector._long_line_paste_bytes = 512 if flag_on else None
            rig.child_bytes()
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "/" + "a" * 1021 + "!id x"}, origin="hub")
            assert out["ok"] is True
            assert rig.child_bytes().startswith(BRACKETED_PASTE_START)
    finally:
        rig.close()


# -- (3) the last-byte chunk boundary --------------------------------------------------------------


def test_f3_split_chunk_leads_with_bang_sees_a_bang_that_is_the_very_last_byte():
    assert lb.split_chunk_leads_with_bang(b"a" * 1022 + b"!", 1022) is True  # 1023 bytes, '!' last
    assert lb.split_chunk_leads_with_bang(b"a" * 2044 + b"!", 1022) is True
    assert lb.split_chunk_leads_with_bang(b"abc!", 3) is True
    assert lb.split_chunk_leads_with_bang(b"abcdef!", 3) is True
    assert lb.split_chunk_leads_with_bang(b"a" * 1021 + b"!", 1022) is False


@pytest.mark.parametrize("pad", [1022, 2044])
@pytest.mark.parametrize("source", ["self-verb", "bus"])
def test_f3_flag_off_a_payload_ending_in_the_bang_at_the_split_is_bracketed(tmp_path, pad, source):
    rig = _rig(tmp_path)
    try:
        text = "a" * pad + "!"
        _, writes, got = _type(rig, text, flag_on=False, source=source)
        assert len(text.encode()) == pad + 1
        assert got == BRACKETED_PASTE_START + text.encode() + BRACKETED_PASTE_END + b"\r"
    finally:
        rig.close()


# -- (2)/(3) run()'s failure caps -------------------------------------------------------------------


class _NullRaw:
    def __init__(self, fd):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_f2_the_cap_constants_are_pinned():
    assert proxy_mod._MAX_CONSECUTIVE_STEP_FAILURES == 100
    assert proxy_mod._MAX_STEP_FAILURES_IN_WINDOW == 100
    assert proxy_mod._STEP_FAILURE_WINDOW_SECONDS == 300.0
    assert proxy_mod._MAX_STEP_FAILURES_IN_DAY == 1000  # r2f: was a lifetime total, now a 24 h rate
    assert proxy_mod._STEP_FAILURE_DAY_SECONDS == 86400.0


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def _run_rig(tmp_path, monkeypatch, step, clock=None):
    rig = _rig(tmp_path)
    monkeypatch.setattr(proxy_mod, "RawMode", _NullRaw)
    rig.proxy._reap = lambda: 0  # the Rig has pid=0: a real waitpid(0) would wait on ANY child of the suite
    monkeypatch.setattr(proxy_mod.time, "sleep", lambda s: None)
    if clock is not None:
        monkeypatch.setattr(proxy_mod.time, "monotonic", clock)
    rig.proxy.step = lambda timeout=None: step(rig)
    return rig


def test_f2_an_intermittent_persistent_fault_ends_run_by_the_window(tmp_path, monkeypatch):
    """fail, idle, fail, idle ...: the consecutive counter is reset by every idle step, so only the
    windowed count can end it. One failure per fake second: 100 inside 300 s."""
    clock = _Clock()
    calls = {"n": 0, "fails": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] > 10000:  # a regression must fail, not hang
            rig.proxy._child_alive = False
        if calls["n"] % 2 == 1:
            calls["fails"] += 1
            clock.t += 1.0
            raise OSError(32, "Broken pipe")
        return True  # an idle, clean step

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    try:
        with pytest.raises(OSError):
            rig.proxy.run()
        assert calls["fails"] == proxy_mod._MAX_STEP_FAILURES_IN_WINDOW
        assert len(rig.proxy.pump_errors) == calls["fails"]
    finally:
        rig.close()


def test_f2_a_slow_persistent_fault_ends_run_by_the_daily_rate(tmp_path, monkeypatch):
    """One failure per 80 fake seconds never fills the 300 s window; only the 24 h rate can end it (r2f)."""
    clock = _Clock()
    calls = {"n": 0, "fails": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] > 20000:
            rig.proxy._child_alive = False
        if calls["n"] % 2 == 1:
            calls["fails"] += 1
            clock.t += 80.0
            raise OSError(5, "io")
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    try:
        with pytest.raises(OSError):
            rig.proxy.run()
        assert calls["fails"] == proxy_mod._MAX_STEP_FAILURES_IN_DAY
    finally:
        rig.close()


def test_f2_rare_one_off_faults_below_every_cap_do_not_end_run(tmp_path, monkeypatch):
    clock = _Clock()
    calls = {"n": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] >= 400:
            rig.proxy._child_alive = False
            return False
        if calls["n"] % 8 == 0:  # 49 failures, 400 fake seconds apart: under every cap
            clock.t += 400.0
            raise RuntimeError("one-off")
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    try:
        assert rig.proxy.run() == 0
        assert len(rig.proxy.pump_errors) == 49
    finally:
        rig.close()


def test_f2_failures_spread_so_that_no_window_holds_the_cap_do_not_end_run_early(tmp_path, monkeypatch):
    """The window SLIDES: 98 failures inside 300 s, a long gap, then 98 more is never 100 in a window."""
    clock = _Clock()
    calls = {"n": 0, "f": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] >= 500:
            rig.proxy._child_alive = False
            return False
        if calls["n"] % 2 == 1 and calls["f"] < 196:
            calls["f"] += 1
            clock.t += 1.0 if calls["f"] != 99 else 1000.0
            raise RuntimeError("x")
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    try:
        assert rig.proxy.run() == 0
        assert calls["f"] == 196
    finally:
        rig.close()


def test_f2_contained_refusals_never_count_toward_any_cap(tmp_path, monkeypatch):
    """1500 contained refusals (more than every cap) through the real _contained: run() ends only when the
    child does, with nothing recorded as a failure."""
    calls = {"n": 0}

    def raiser():
        raise ScreenRefused("refused", ["leading-bang"])

    def step(rig):
        calls["n"] += 1
        rig.proxy._contained("control", raiser)
        if calls["n"] >= 1500:
            rig.proxy._child_alive = False
            return False
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, _Clock())
    try:
        assert rig.proxy.run() == 0
        assert calls["n"] == 1500
        assert len(rig.proxy.screen_refusals) == 1500
        assert rig.proxy.pump_errors == []
    finally:
        rig.close()


def test_f1_the_bang_check_on_the_bare_token_is_a_live_belt_independent_of_the_length_bound(monkeypatch):
    """Were the 512 bound ever raised, the bare token still runs split_chunk_leads_with_bang on its final bytes."""
    from gnomon.harness import injector as inj

    monkeypatch.setattr(inj, "LONG_LINE_PASTE_BYTES", 5000)
    text = "/" + "a" * 1021 + "!id x"
    assert payload_phases(text, 1022) == [BRACKETED_PASTE_START + text.encode() + BRACKETED_PASTE_END]
    ok = "/" + "a" * 1500 + " x"  # a long token with no bang at any split: the bare shape is kept (bound raised)
    assert payload_phases(ok, 1022)[0] == ("/" + "a" * 1500).encode() + b" "

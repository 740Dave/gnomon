"""master_write_nonblocking, fix round 3 (Diamond P2-1 and the P3s of DIAMOND-DELTA-master-write-deadlock-10a45ef-2026-10-27).

P2-1: the 1 MiB harness cap is checked ONCE for a whole injection sequence (the clear prefix, the text writes, the CR), before any part is
queued: all of it is queued or none of it; no partial sequence reaches the wire. Flag ON only; flag OFF is untouched.
"""
import errno

import pytest

from gnomon.harness import SingleWriteSite
from gnomon.harness import writer as _writer_mod
from gnomon.harness.hooks import SOURCE_MONITOR_GUIDANCE, InjectionGate
from gnomon.harness.injector import Injector

ESC_ESC = b"\x1b\x1b"


class Pipe:
    def __init__(self):
        self.full = False
        self.wire = b""

    def __call__(self, _fd, data):
        if self.full:
            raise BlockingIOError(errno.EAGAIN, "full")
        self.wire += bytes(data)
        return len(data)


def _rig(monkeypatch, cap, gate=None):
    monkeypatch.setattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", cap, raising=False)
    p = Pipe()
    p.full = True                       # nothing leaves: whatever is queued stays visible as pending
    w = SingleWriteSite(-1, writer=p, gate=gate, nonblocking=True)
    inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
    return p, w, inj


def _drain(p, w):
    p.full = False
    while w.has_pending():
        w.flush_pending()


def test_cap_equal_to_the_text_refuses_a_text_plus_cr_injection_whole_with_no_text_on_the_wire(monkeypatch):
    text = "T" * 100
    p, w, inj = _rig(monkeypatch, cap=len(text))        # the text alone fits; text + CR does not
    with pytest.raises(ValueError):                     # PendingCapRefused is a ScreenRefused (a ValueError)
        inj.inject_line(text)
    _drain(p, w)
    assert p.wire == b"" and w._pending_harness == 0 and w._reserved_harness == 0


def test_clear_with_an_over_cap_text_sends_no_esc_esc(monkeypatch):
    p, w, inj = _rig(monkeypatch, cap=50)
    with pytest.raises(ValueError):
        inj.inject_line("T" * 200, clear=True)
    _drain(p, w)
    assert p.wire == b"" and ESC_ESC not in p.wire


def test_an_injection_of_exactly_cap_total_bytes_is_accepted_whole(monkeypatch):
    text = "T" * 100
    p, w, inj = _rig(monkeypatch, cap=len(text) + 1)    # text + CR == cap
    inj.inject_line(text)
    _drain(p, w)
    assert p.wire == text.encode() + b"\r"
    assert w._pending_harness == 0 and w._reserved_harness == 0


def test_an_injection_with_clear_of_exactly_cap_total_bytes_is_accepted_whole_and_one_more_is_not(monkeypatch):
    text = "T" * 100
    p, w, inj = _rig(monkeypatch, cap=len(ESC_ESC) + len(text) + 1)
    inj.inject_line(text, clear=True)
    _drain(p, w)
    assert p.wire == ESC_ESC + text.encode() + b"\r"
    p2, w2, inj2 = _rig(monkeypatch, cap=len(ESC_ESC) + len(text))
    with pytest.raises(ValueError):
        inj2.inject_line(text, clear=True)
    _drain(p2, w2)
    assert p2.wire == b""


def test_a_reserved_sequence_counts_against_another_threads_injection_and_is_released_when_done(monkeypatch):
    import threading
    p, w, inj = _rig(monkeypatch, cap=300)
    errs = []

    def other():
        try:
            w.inject(b"x" * 100)
        except ValueError as exc:
            errs.append(exc)
    with w.reserve_sequence(250):
        t = threading.Thread(target=other)
        t.start()
        t.join()
        assert len(errs) == 1                                                          # 250 reserved + 100 > 300
    assert w._reserved_harness == 0
    w.inject(b"x" * 100)                                                               # reservation gone: fits


def test_flag_off_sequence_has_no_cap(monkeypatch):
    monkeypatch.setattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", 10, raising=False)
    p = Pipe()
    w = SingleWriteSite(-1, writer=p)
    Injector(w, beat_seconds=0, sleep=lambda _s: None).inject_line("T" * 200, clear=True)
    assert p.wire == ESC_ESC + b"T" * 200 + b"\r"


def test_a_forced_approve_of_a_start_held_injection_says_forced_not_end_of_hold(monkeypatch):
    gate = InjectionGate()
    seen = []
    real = gate.record

    def rec(kind, **f):
        seen.append((kind, f))
        return real(kind, **f)
    gate.record = rec
    p = Pipe()
    w = SingleWriteSite(-1, writer=p, gate=gate, nonblocking=True)
    gate.line_hold_check = w.startup_hold_reason
    gate.start_hold_cause = w.start_hold_release_cause
    inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
    assert inj.inject_line("hello", source=SOURCE_MONITOR_GUIDANCE, trigger="t").decision.value == "defer"
    assert gate._start_held
    gate.approve("all", force=True)
    release = [f for k, f in seen if k == "injection" and f.get("decision") == "release"]
    assert release and "forced approve" in release[0]["reason"] and "released by the end of the start hold" not in release[0]["reason"], release
    assert gate._start_held == set() and b"hello" in p.wire


def test_a_release_refused_by_the_cap_is_followed_by_a_release_refused_record(monkeypatch):
    monkeypatch.setattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", 20, raising=False)
    gate = InjectionGate()
    seen = []
    real = gate.record

    def rec(kind, **f):
        seen.append((kind, f))
        return real(kind, **f)
    gate.record = rec
    p = Pipe()
    w = SingleWriteSite(-1, writer=p, gate=gate, nonblocking=True)
    gate.line_hold_check = w.startup_hold_reason
    gate.start_hold_cause = w.start_hold_release_cause
    inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
    inj.inject_line("Z" * 100, source=SOURCE_MONITOR_GUIDANCE, trigger="t")
    w.note_child_output()
    with pytest.raises(ValueError):
        gate.release_modal_holds()
    kinds = [f.get("what") for k, f in seen if k == "modal-guard"]
    assert "release-refused-by-pending-cap" in kinds and p.wire == b"" and gate._start_held == set()

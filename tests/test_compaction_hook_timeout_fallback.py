"""Harness-side fallback for a hook killed at its timeout (key
before-flag-on-hook-timeout, item 1, corrected ruling 2026-10-27; flag ``subagent_compaction_check``).

Flag ON: the hook sends a post-compact-STARTED frame first thing in post-compact, before its verdict wait. That frame (not the pre
frame: a real compaction takes 22 to 120 s, median 60.6 s, between pre and post) arms the bound; no verdict frame within
``compact_origin.FALLBACK_SECONDS`` is treated as a plain post-compact frame (one record, point ``compact-fallback``); a verdict
frame of ANY kind inside the bound cancels it; a post frame after it fired is a no-op (one boot prompt). Flag OFF: nothing is armed
and the hook sends no started frame. Frames are delivered to the real actuator and
the pump is stepped for real; only the clock is driven (``compact_origin._clock``).
"""
from __future__ import annotations

import pytest

from hooks_support import Rig

from gnomon import flags as F
from gnomon.config import GnomonConfig

FLAG = "subagent_compaction_check"
BOOT = "Please complete the bootstrap process (test text)."
PRE = {"cmd": "lifecycle", "event": "pre-compact", "trigger": "auto"}
STARTED = {"cmd": "lifecycle", "event": "post-compact-started"}
POST = {"cmd": "lifecycle", "event": "post-compact", "trigger": "auto"}
IGNORED_POST = dict(POST, ignored="subagent compaction, ignored", compaction_check_reason="no new main boundary")


class _Store:
    def __init__(self, cfg):
        self._cfg = cfg

    def load(self):
        return self._cfg


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(F, "_config_store", lambda: _Store(GnomonConfig(flags={FLAG: True})))


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(F, "_config_store", lambda: _Store(GnomonConfig()))


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    from gnomon.harness import compact_origin as co
    c = Clock()
    monkeypatch.setattr(co, "_clock", c, raising=False)   # raising=False: the unfixed tree has no clock, and the red run must fail on the ASSERTIONS
    return c


@pytest.fixture
def rig(tmp_path):
    harness = {"post_compact_boot": {"enabled": True, "text": BOOT, "role": "beta-test", "text_source": "test",
                                     "settle_seconds": 0.0, "max_hold_seconds": 1800.0}}
    r = Rig(tmp_path, harness=harness, read_context=lambda: 10.0)
    r.proxy._monitor.poll()
    r.note_calls = []
    orig = r.proxy._monitor.note_compact
    r.proxy._monitor.note_compact = lambda *a, **k: (r.note_calls.append(1), orig(*a, **k))[1]
    r.arms = []
    boot = r.proxy.post_compact_boot
    orig_arm = boot.arm
    boot.arm = lambda *a, **k: (r.arms.append(1), orig_arm(*a, **k))[1]
    yield r
    r.close()


def _send(rig, frame):
    cmd = {k: v for k, v in frame.items()}
    rig.proxy.actuator.dispatch(cmd, origin="self")


def _life(rig, point):
    return [r for r in rig.records("lifecycle") if r.get("point") == point]


def _points(rig):
    return [(r.get("point"), (r.get("payload") or {}).get("phase")) for r in rig.records("lifecycle")]


def _begin(rig, gap=0.0, clock=None):
    """A compaction: the pre frame, ``gap`` seconds of compaction, then the post hook's started frame."""
    _send(rig, PRE)
    if gap:
        _advance(rig, clock, gap)
    _send(rig, STARTED)


def _advance(rig, clock, seconds):
    clock.t += seconds
    rig.proxy.step(0.0)


# ---------------------------------------------------------------- flag ON

def test_on_started_without_a_verdict_applies_the_post_compact_behaviour_after_the_bound(rig, flag_on, clock):
    _begin(rig)
    assert getattr(getattr(rig.proxy, "compact_fallback", None), "state", None) == "armed", "a post-compact-started frame armed nothing"
    _advance(rig, clock, 14.9)
    assert rig.arms == [] and not _life(rig, "compact-fallback"), "fired before the bound"
    _advance(rig, clock, 0.2)                                   # 15.1 s with no post frame
    assert rig.arms == [1], "a post-compact-started frame with no verdict frame within 15 s armed no boot prompt"
    assert rig.proxy.post_compact_boot.pending
    assert rig.note_calls == [1]
    rec = _life(rig, "compact-fallback")
    assert len(rec) == 1 and rec[0]["reason"] == "no post-compact frame within 15 s"
    # same lifecycle shape as a plain post frame: pre, the fallback record, then the post-compact point
    assert _points(rig) == [("compact", "pre"), ("compact-fallback", "post"), ("compact", "post")]


def test_on_post_frame_at_14_s_cancels_the_fallback(rig, flag_on, clock):
    _begin(rig)
    _advance(rig, clock, 14.0)
    _send(rig, POST)
    assert rig.arms == [1]
    _advance(rig, clock, 30.0)
    assert rig.arms == [1], "the fallback fired after a post frame had arrived (a second boot prompt)"
    assert not _life(rig, "compact-fallback")
    assert rig.note_calls == [1]


def test_on_post_frame_at_16_s_after_the_fallback_fired_is_a_noop(rig, flag_on, clock):
    _begin(rig)
    _advance(rig, clock, 15.5)
    assert rig.arms == [1]
    _advance(rig, clock, 0.5)
    _send(rig, POST)                                            # the same episode's late post frame
    assert rig.arms == [1], "double boot prompt: the late post frame armed it again"
    assert rig.note_calls == [1]
    assert _points(rig).count(("compact", "post")) == 1
    assert len(_life(rig, "compact-fallback")) == 1
    _advance(rig, clock, 60.0)
    assert rig.arms == [1]


def test_on_ignored_subagent_frame_within_the_bound_cancels_the_fallback(rig, flag_on, clock):
    _begin(rig)
    _advance(rig, clock, 5.0)
    _send(rig, IGNORED_POST)
    _advance(rig, clock, 60.0)
    assert rig.arms == [] and rig.note_calls == [], "the fallback applied a session compaction after an ignored frame"
    assert not _life(rig, "compact-fallback")
    assert len(_life(rig, "compact-ignored")) == 1


def test_on_the_next_episode_is_not_swallowed_after_a_fired_one(rig, flag_on, clock):
    _begin(rig)
    _advance(rig, clock, 16.0)
    assert rig.arms == [1]
    _begin(rig)                                                 # a new compaction begins
    _advance(rig, clock, 3.0)
    _send(rig, POST)                                            # its post frame is its own, not the late one
    assert rig.arms == [1, 1]


def test_on_a_fired_fallback_stops_swallowing_post_frames_after_its_memory(rig, flag_on, clock):
    from gnomon.harness import compact_origin as co
    _begin(rig)
    _advance(rig, clock, 16.0)
    assert rig.arms == [1]
    _advance(rig, clock, getattr(co, "FIRED_MEMORY_SECONDS", 300.0) + 1)
    _send(rig, POST)                                            # no pre frame (lost): a real post must not be swallowed
    assert rig.arms == [1, 1]


def test_on_a_second_pre_frame_restarts_the_bound(rig, flag_on, clock):
    _begin(rig)
    _advance(rig, clock, 10.0)
    _begin(rig)
    _advance(rig, clock, 10.0)                                  # 20 s since the first pre, 10 s since the second
    assert rig.arms == []
    _advance(rig, clock, 6.0)
    assert rig.arms == [1]


def test_on_a_failing_apply_is_recorded_and_never_raises_into_the_pump(rig, flag_on, clock, monkeypatch):
    _begin(rig)
    fb = rig.proxy.compact_fallback

    def boom(info):
        raise RuntimeError("boom")
    monkeypatch.setattr(fb, "_apply", boom)
    _advance(rig, clock, 16.0)                                  # step() would surface a raise from the ticker? it must not
    failed = [r for r in _life(rig, "compact-fallback") if r.get("decision") == "failed"]
    assert len(failed) == 1 and "boom" in failed[0]["reason"]
    assert fb.state == "fired"


def test_on_the_fallback_is_a_pump_ticker_not_a_thread(rig, flag_on, clock):
    import threading
    before = threading.active_count()
    _begin(rig)
    assert threading.active_count() == before, "arming started a thread"
    assert rig.proxy.compact_fallback.tick in rig.proxy._tickers


# ---------------------------------------------------------------- the 300 s backstop (anchored on the pre frame)

def test_on_pre_then_nothing_for_300_s_gives_one_boot_prompt(rig, flag_on, clock):
    _send(rig, PRE)
    _advance(rig, clock, 299.0)
    assert rig.arms == [] and not _life(rig, "compact-fallback")
    _advance(rig, clock, 2.0)
    assert rig.arms == [1] and rig.note_calls == [1]
    rec = _life(rig, "compact-fallback")
    assert len(rec) == 1 and rec[0]["reason"] == "no post-compact frame within 300 s of pre"
    _advance(rig, clock, 600.0)
    assert rig.arms == [1] and len(_life(rig, "compact-fallback")) == 1


def test_on_started_at_60_s_then_a_verdict_at_61_s_adds_nothing(rig, flag_on, clock):
    _send(rig, PRE)
    _advance(rig, clock, 60.0)
    _send(rig, STARTED)
    assert rig.proxy.compact_fallback.state == "armed", "started must disarm the backstop and arm the 15 s bound"
    _advance(rig, clock, 1.0)
    _send(rig, POST)
    _advance(rig, clock, 900.0)
    assert rig.arms == [1] and not _life(rig, "compact-fallback")


def test_on_started_at_200_s_then_no_verdict_gives_exactly_one_boot_prompt(rig, flag_on, clock):
    _send(rig, PRE)
    _advance(rig, clock, 200.0)
    _send(rig, STARTED)
    _advance(rig, clock, 14.0)
    assert rig.arms == []
    _advance(rig, clock, 2.0)                                   # 15 s after started; 216 s after pre
    assert rig.arms == [1]
    _advance(rig, clock, 900.0)                                 # the 300 s backstop must not fire a second time
    assert rig.arms == [1]
    rec = _life(rig, "compact-fallback")
    assert len(rec) == 1 and rec[0]["reason"] == "no post-compact frame within 15 s"


def test_on_a_frame_after_the_backstop_fired_is_a_noop(rig, flag_on, clock):
    _send(rig, PRE)
    _advance(rig, clock, 301.0)
    assert rig.arms == [1]
    _send(rig, STARTED)                                         # late started frame: no re-arm
    _advance(rig, clock, 60.0)
    _send(rig, POST)                                            # late verdict: no-op
    assert rig.arms == [1] and rig.note_calls == [1]
    assert len(_life(rig, "compact-fallback")) == 1


def test_on_an_ignored_frame_disarms_the_backstop(rig, flag_on, clock):
    _send(rig, PRE)
    _advance(rig, clock, 5.0)
    _send(rig, IGNORED_POST)
    _advance(rig, clock, 900.0)
    assert rig.arms == [] and not _life(rig, "compact-fallback")


# ---------------------------------------------------------------- flag OFF

def test_off_nothing_is_armed_and_the_behaviour_is_todays(rig, flag_off, clock):
    _send(rig, PRE)
    _send(rig, STARTED)                                         # the hook never sends it with the flag OFF; a stray one arms nothing
    assert getattr(getattr(rig.proxy, "compact_fallback", None), "state", None) is None
    _advance(rig, clock, 1200.0)                                # past the 300 s backstop too
    assert rig.arms == [] and rig.note_calls == [] and not _life(rig, "compact-fallback")
    assert _points(rig) == [("compact", "pre")]
    _send(rig, POST)                                            # a late post frame is handled as ever
    assert rig.arms == [1] and rig.note_calls == [1]
    assert _points(rig) == [("compact", "pre"), ("compact", "post")]
    assert [k for k in rig.records() if k.get("kind") == "lifecycle" and k.get("point") not in ("compact",)] == []


def test_off_a_pre_frame_never_followed_by_any_post_frame_arms_no_backstop_and_applies_nothing(rig, flag_off, clock):
    """P2-1 of the Diamond on d685e44 (mutant: on_pre arms the backstop regardless of the flag)."""
    _send(rig, PRE)                                             # no started frame, no verdict frame, ever
    assert getattr(getattr(rig.proxy, "compact_fallback", None), "state", None) is None, "a flag-OFF pre frame armed something"
    for seconds in (299.0, 2.0, 600.0, 3600.0, 36000.0):       # through the 300 s backstop and well beyond
        _advance(rig, clock, seconds)
        assert rig.arms == [] and rig.note_calls == [], "the post-compact behaviour was applied with the flag OFF"
    assert not _life(rig, "compact-fallback")
    assert _points(rig) == [("compact", "pre")]
    assert not rig.proxy.post_compact_boot.pending


def test_off_a_second_post_frame_is_not_swallowed(rig, flag_off, clock):
    _send(rig, PRE)
    _send(rig, POST)
    _send(rig, POST)
    assert rig.arms == [1, 1]


# ---------------------------------------------------------------- the real hook's frames

def _run_hook(rig, event, srv, payload=None):
    """Run the real hook once; return the raw frames it sent (in order) and its stdout. Frames are NOT delivered here."""
    import io
    import json
    from gnomon.harness import lifecycle_hook
    before = len(srv.raw)
    out = io.StringIO()
    rc = lifecycle_hook.main(["--event", event, "--socket", srv.path, "--session-dir", rig.session_dir],
                             stdin=io.StringIO(json.dumps(payload if payload is not None else {"trigger": "auto"})), stdout=out)
    assert rc == 0
    return srv.raw[before:], out.getvalue()


@pytest.fixture
def hook_world(tmp_path, rig):
    from test_subagent_compaction_check import FrameServer, World
    from gnomon.harness import compact_origin as co
    srv = FrameServer()
    w = World(tmp_path, rig)
    yield rig, srv, w
    srv.close()


def test_on_the_hook_sends_started_first_and_before_the_verdict_wait(hook_world, flag_on, monkeypatch):
    import json
    from gnomon.harness import compact_origin as co
    rig, srv, w = hook_world
    _run_hook(rig, "pre-compact", srv)
    seen = {}
    real = co.verdict_post

    def spy(*a, **k):
        seen["frames_before_the_wait"] = [json.loads(x)["event"] for x in srv.raw]
        return real(*a, **k)
    monkeypatch.setattr(co, "verdict_post", spy)
    frames, _ = _run_hook(rig, "post-compact", srv)
    assert seen["frames_before_the_wait"] == ["pre-compact", "post-compact-started"]
    assert [json.loads(x)["event"] for x in frames] == ["post-compact-started", "post-compact"]
    assert json.loads(frames[0]) == {"cmd": "lifecycle", "event": "post-compact-started"}


def test_off_the_hooks_frames_are_byte_identical_to_todays(hook_world, flag_off):
    rig, srv, w = hook_world
    pre, _ = _run_hook(rig, "pre-compact", srv)
    post, _ = _run_hook(rig, "post-compact", srv)
    assert pre == [b'{"cmd": "lifecycle", "event": "pre-compact", "trigger": "auto"}\n']
    assert post == [b'{"cmd": "lifecycle", "event": "post-compact", "trigger": "auto"}\n']


def test_on_a_hook_that_dies_after_started_gives_todays_behaviour_after_the_bound(hook_world, flag_on, clock, monkeypatch):
    """The whole path: the real hook's frames delivered to the real actuator, the hook killed in its verdict wait."""
    import json
    from gnomon.harness import compact_origin as co
    rig, srv, w = hook_world
    for raw in _run_hook(rig, "pre-compact", srv)[0]:
        rig.proxy.actuator.dispatch(json.loads(raw), origin="self")

    def killed(*a, **k):
        raise SystemExit(0)                                     # Claude Code kills the hook at its timeout
    monkeypatch.setattr(co, "verdict_post", killed)
    with pytest.raises(SystemExit):
        _run_hook(rig, "post-compact", srv)
    # the started frame had been sent; the verdict frame never was
    frames = [json.loads(x)["event"] for x in srv.raw]
    assert frames == ["pre-compact", "post-compact-started"]
    rig.proxy.actuator.dispatch(json.loads(srv.raw[-1]), origin="self")
    _advance(rig, clock, 15.5)
    assert rig.arms == [1] and len(_life(rig, "compact-fallback")) == 1

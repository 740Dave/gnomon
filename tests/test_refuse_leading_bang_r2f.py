"""r2f (r2e Diamond F1): run()'s failure caps decay (no lifetime total) and every loop-level failure is recorded."""
import json

import pytest

from gnomon.harness import proxy as proxy_mod
from gnomon.harness.hooks import ProvenanceLog
from tests.test_refuse_leading_bang_r2e import _Clock, _run_rig


def test_no_lifetime_total_constant_remains():
    assert not hasattr(proxy_mod, "_MAX_TOTAL_STEP_FAILURES")


def test_a_multi_day_session_with_sparse_faults_never_dies(tmp_path, monkeypatch):
    """1500 genuine faults, one per 200 fake seconds (about 3.5 days): never 100 in 300 s, never 1000 in
    24 h (at most 432 per day). On 0c2572d the 1000 lifetime total killed this run."""
    clock = _Clock()
    calls = {"n": 0, "f": 0}

    def step(rig):
        calls["n"] += 1
        if calls["f"] >= 1500:
            rig.proxy._child_alive = False
            return False
        if calls["n"] % 2 == 1:
            calls["f"] += 1
            clock.t += 200.0
            raise OSError(5, "io")
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    try:
        assert rig.proxy.run() == 0
        assert calls["f"] == 1500 and len(rig.proxy.pump_errors) == 1000
        assert clock.t - 1000.0 > 3 * 86400
    finally:
        rig.close()


def test_the_daily_rate_still_ends_a_slow_persistent_fault(tmp_path, monkeypatch):
    clock = _Clock()
    calls = {"n": 0, "f": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] > 20000:
            rig.proxy._child_alive = False
        if calls["n"] % 2 == 1:
            calls["f"] += 1
            clock.t += 85.0  # 1000 within 85,000 s < 24 h; never 100 in 300 s
            raise OSError(5, "io")
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    try:
        with pytest.raises(OSError):
            rig.proxy.run()
        assert calls["f"] == proxy_mod._MAX_STEP_FAILURES_IN_DAY
    finally:
        rig.close()


def test_consecutive_and_window_caps_still_fire(tmp_path, monkeypatch):
    calls = {"n": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] > 5000:
            rig.proxy._child_alive = False
        raise RuntimeError("persistent")

    rig = _run_rig(tmp_path, monkeypatch, step, _Clock())
    try:
        with pytest.raises(RuntimeError):
            rig.proxy.run()
        assert calls["n"] == proxy_mod._MAX_CONSECUTIVE_STEP_FAILURES
    finally:
        rig.close()


def test_every_loop_failure_is_written_to_the_injection_log(tmp_path, monkeypatch):
    clock = _Clock()
    log = ProvenanceLog(str(tmp_path / "injection_log.jsonl"))
    calls = {"n": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] >= 6:
            rig.proxy._child_alive = False
            return False
        if calls["n"] % 2 == 1:
            clock.t += 10.0
            raise OSError(32, "Broken pipe")
        return True

    rig = _run_rig(tmp_path, monkeypatch, step, clock)
    rig.proxy._on_loop_failure = lambda f: log.append({"kind": "loop-failure", "source": "harness", **f})
    try:
        assert rig.proxy.run() == 0
    finally:
        rig.close()
    rows = [json.loads(x) for x in (tmp_path / "injection_log.jsonl").read_text().splitlines()]
    assert len(rows) == 3 == len(rig.proxy.pump_errors)
    r = rows[-1]
    assert r["kind"] == "loop-failure" and r["exc_type"] == "BrokenPipeError" and r["ts_iso"]
    assert (r["consecutive"], r["in_window"], r["in_day"], r["fatal"]) == (1, 3, 3, False)
    assert r["caps"] == [100, 100, 1000]


def test_the_fatal_failure_is_recorded_before_run_raises(tmp_path, monkeypatch):
    seen = []

    def step(rig):
        raise RuntimeError("persistent")

    rig = _run_rig(tmp_path, monkeypatch, step, _Clock())
    rig.proxy._on_loop_failure = seen.append
    try:
        with pytest.raises(RuntimeError):
            rig.proxy.run()
    finally:
        rig.close()
    assert len(seen) == proxy_mod._MAX_CONSECUTIVE_STEP_FAILURES
    assert seen[-1]["fatal"] is True and not any(f["fatal"] for f in seen[:-1])


def test_a_failing_log_write_is_contained(tmp_path, monkeypatch):
    calls = {"n": 0}

    def step(rig):
        calls["n"] += 1
        if calls["n"] >= 6:
            rig.proxy._child_alive = False
            return False
        if calls["n"] % 2 == 1:
            raise OSError(5, "io")
        return True

    attempts = []

    def boom(fields):
        attempts.append(fields)
        raise PermissionError("log unwritable")

    rig = _run_rig(tmp_path, monkeypatch, step, _Clock())
    rig.proxy._on_loop_failure = boom
    try:
        assert rig.proxy.run() == 0
        assert len(rig.proxy.pump_errors) == 3
        assert len(attempts) == 3  # the write WAS attempted each time, and each failure was swallowed
    finally:
        rig.close()


def test_launcher_wires_the_loop_failure_callback():
    import inspect
    from gnomon.harness import launcher
    assert "on_loop_failure=lambda fields: gate.record(\"loop-failure\"" in inspect.getsource(launcher)


def test_pump_errors_is_bounded_and_messages_truncated(tmp_path):
    """r2f Diamond F1: 5000 faults leave at most 1000 entries, each message at most 500 chars."""
    from gnomon.harness.proxy import HarnessProxy
    p = HarnessProxy.__new__(HarnessProxy)
    p.pump_errors = []
    for i in range(5000):
        p._note_failure("run", OSError("x" * 2000 + str(i)))
    assert len(p.pump_errors) <= 1000 and len(p.pump_errors) == 1000
    assert all(len(m) <= 500 for _, _, m in p.pump_errors)
    assert isinstance(p.pump_errors, list)

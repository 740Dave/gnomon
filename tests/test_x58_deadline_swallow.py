"""Delta Diamond D1 (X58 fix round 2): the stat helper's threads feed threading's internal WeakSets, and CPython runs weakref callbacks
with their exceptions SUPPRESSED. If the SIGALRM handler's ``_Deadline`` lands inside one, it is printed as "Exception ignored" and
lost, and the rest of the decision used to run with no deadline at all. Fixed two ways: the handler sets ``_deadline_fired`` BEFORE it
raises (a decision that would ALLOW while it is set is refused), and the timer repeats every REARM_S so a second raise lands outside
the callback and cuts the tail."""
import io
import json
import os
import signal
import sys
import time
import weakref

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
from gnomon.harness import deny_guard as G  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_alarm():
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)
    hook = sys.unraisablehook
    sys.unraisablehook = lambda u: None                 # "Exception ignored in ..." is the thing under test: keep it out of the output
    yield
    sys.unraisablehook = hook
    signal.setitimer(signal.ITIMER_REAL, 0)
    signal.signal(signal.SIGALRM, signal.SIG_DFL)


class _O:
    pass


@pytest.mark.parametrize("hook", [True, False])
def test_a_deadline_swallowed_in_a_weakref_callback_still_ends_as_a_refusal(monkeypatch, hook):
    monkeypatch.setattr(G, "DEADLINE_S", 0.3)
    swallowed = []
    reached = []

    def cb(_ref):
        end = time.time() + 0.4                         # the alarm (0.3 s) lands in here; its exception is discarded by the interpreter
        try:
            while time.time() < end:
                pass
        except BaseException:                           # never reached: CPython discards it before this frame sees it
            swallowed.append(1)

    def work():
        o = _O()
        ref = weakref.ref(o, cb)
        del o                                           # the callback runs here, synchronously
        time.sleep(0.8)                                 # the long tail that used to run with NO deadline
        reached.append(1)                               # the ALLOW path
        return None

    t0 = time.time()
    out = G._bounded_call(work, hook=hook)
    took = time.time() - t0
    assert out is not None and out.startswith(G.OVERRUN_TAG), out
    assert reached == [], "the allow path must never be reached once the deadline fired"
    assert took < 0.3 + 0.2, took


def test_a_fired_deadline_is_never_an_allow_even_when_the_work_returns_none(monkeypatch):
    """Belt and braces: the work swallows the exception itself and returns None; the flag alone must refuse."""
    monkeypatch.setattr(G, "DEADLINE_S", 0.2)

    def work():
        try:
            time.sleep(0.5)
        except G._Deadline:                              # a (hypothetical) inner handler that eats it
            pass
        return None
    out = G._bounded_call(work, hook=True)
    assert out is not None and out.startswith(G.OVERRUN_TAG) and "deadline fired" in out


def test_main_does_not_emit_an_allow_while_the_flag_is_set(monkeypatch):
    def fired_then_allow(data):                          # the deadline fires DURING the evaluation (main() resets the flag at its top)
        G._deadline_fired = True
        return "allow", None, []
    monkeypatch.setattr(G, "_evaluate", fired_then_allow)
    err = io.StringIO()
    rc = G.main(stdin=io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})), stderr=err)
    assert rc == 2 and "timed out" in err.getvalue()


def test_the_timer_repeats_after_the_first_expiry():
    run = G._HookAlarm()
    run.arm(0.05)
    try:
        assert signal.getitimer(signal.ITIMER_REAL)[1] == G.REARM_S == 0.05
    finally:
        run.disarm()
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)           # disarm clears the interval too


def test_many_alarm_landings_through_the_stat_helper_never_allow_a_must_deny(monkeypatch, tmp_path):
    """The reviewer's Q1 shape, small: the deadline is random so the alarm lands anywhere in a decision that starts and joins stat threads.
    Every fired-but-not-raised decision ends in a refusal; a must-deny is never an allow."""
    import random
    fired_not_raised = []
    monkeypatch.setattr(sys, "unraisablehook", lambda u: fired_not_raised.append(1))
    target = os.path.expanduser(G._protected_paths()[0])
    cmd = "tee /dev/null " + target
    payload = json.dumps({"session_id": "q1", "tool_name": "Bash", "tool_input": {"command": cmd}})
    random.seed(58)
    rc0 = 0
    for _ in range(400):
        monkeypatch.setattr(G, "DEADLINE_S", random.uniform(1e-5, 3e-3))
        rc = G.main(stdin=io.StringIO(payload), stderr=io.StringIO())
        rc0 += rc == 0
    assert rc0 == 0, "a must-deny was ALLOWED"


def test_a_handler_that_runs_at_the_entry_of_disarm_cannot_leave_the_timer_or_the_flag_behind(monkeypatch):
    """Measured under load 96 (high-load block, tip fa237ae): with the repeating timer, CPython's pending Python handler can run at the ENTRY
    of ``disarm()`` (before its body), raising while the flag is still True; the timer stayed armed (timer_left 10) and a later alarm
    raised in the caller (two race workers crashed). Emulated: the first six disarm() calls raise _Deadline at entry while armed."""
    real = G._HookAlarm.disarm
    calls = []

    def flaky(self):
        calls.append(1)
        if len(calls) <= 6 and G._HookAlarm.armed:        # the handler is inert unless armed, exactly like the real one
            raise G._Deadline()
        return real(self)
    monkeypatch.setattr(G._HookAlarm, "disarm", flaky)
    monkeypatch.setattr(G, "DEADLINE_S", 5.0)
    out = G._bounded_call(lambda: None, hook=True)
    assert out is None or out.startswith(G.OVERRUN_TAG)
    assert G._HookAlarm.armed is False, "the flag must be cleared whatever happens to disarm()"
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0), "the (repeating) timer must not be left armed"
    time.sleep(0.2)                                         # a stray repeat would raise _Deadline here

"""Context monitor: boundary-exact firing + negative controls (does NOT fire
early, does NOT fire silently).

Extends coverage in test_monitor.py. Those tests
already exercise prime-on-first-read, fire-once, re-arm-after-drop and the
90/95 kind split against the REAL ``ContextMonitor.poll()`` trigger logic (no
re-derived copy). This file adds the two properties the bringup brief calls
out explicitly:

* the threshold is exact (>= fires, a hair below does not) at BOTH 90 and 95,
  proven against ``monitor.py``'s own ``>=`` comparison in ``poll()``;
* a long climb that never reaches a threshold produces ZERO notices AND the
  sink is genuinely never invoked (silence is asserted on the real
  ``on_notice`` callable, not re-derived) — this is the "does not fire early
  or silently" half of item 1.
"""

from gnomon.harness import ContextMonitor, NoticeKind


class Feed:
    """Scripted read_context: successive values, then repeats the last."""

    def __init__(self, values):
        self._values = list(values)
        self._i = 0

    def __call__(self):
        if self._i < len(self._values):
            v = self._values[self._i]
            self._i += 1
            return v
        return self._values[-1] if self._values else None


def _monitor(values, **kw):
    fired = []
    reports = []
    m = ContextMonitor(
        read_context=Feed(values),
        on_notice=fired.append,
        on_report=reports.append,
        **kw,
    )
    return m, fired, reports


# -- exact boundary, both thresholds -----------------------------------------


def test_90_fires_at_exactly_90_not_at_89_point_9():
    # Prime low, then sit at 89.9 (must NOT fire), then climb to exactly 90.0
    # (MUST fire) — pins the real ``>=`` comparison in poll(), not a re-derived
    # copy of it.
    m, fired, _reports = _monitor([10, 89.9, 90.0])
    m.poll()  # prime at 10
    m.poll()  # 89.9 -> below threshold, must not fire
    assert fired == []
    m.poll()  # 90.0 -> exactly at threshold, must fire
    ninety = [n for n in fired if n.threshold == 90]
    assert len(ninety) == 1
    assert ninety[0].used_percentage == 90.0


def test_95_fires_at_exactly_95_not_at_94_point_9():
    m, fired, _reports = _monitor([10, 94.9, 95.0])
    m.poll()  # prime
    m.poll()  # 94.9 -> 90 fires (>=90), 95 must NOT
    ninety_five = [n for n in fired if n.threshold == 95]
    assert ninety_five == []
    m.poll()  # 95.0 -> now 95 fires too
    ninety_five = [n for n in fired if n.threshold == 95]
    assert len(ninety_five) == 1
    assert ninety_five[0].kind is NoticeKind.FYI
    assert ninety_five[0].used_percentage == 95.0


# -- does not fire EARLY: a long sub-threshold climb is silent ---------------


def test_long_climb_below_90_never_fires():
    # A slow climb from 10 to 89 in single-point steps: at NO point does the
    # monitor fire, and on_notice (the real sink, not a re-derived assertion)
    # is never called.
    climb = [10] + list(range(11, 90))
    m, fired, reports = _monitor(climb)
    for _ in climb:
        m.poll()
    assert fired == []
    # ... but it DID keep reporting up every poll (on_report is a separate,
    # unconditional sink) so "silent" is specifically about notices, not about
    # the harness going dark on the GUI-report side too.
    assert len(reports) == len(climb)


def test_notice_sink_is_never_invoked_below_threshold():
    # Same property, pinned directly on invocation-count of the callable
    # rather than on the returned/collected list, so a monitor that fired but
    # failed to route through on_notice would still be caught.
    calls = {"n": 0}

    def counting_sink(_notice):
        calls["n"] += 1

    m = ContextMonitor(read_context=Feed([10, 50, 89]), on_notice=counting_sink)
    m.poll()
    m.poll()
    m.poll()
    assert calls["n"] == 0


# -- does not fire SILENTLY: when it DOES cross, the sink IS invoked ---------


def test_notice_sink_is_invoked_exactly_once_on_crossing():
    calls = []
    m = ContextMonitor(read_context=Feed([10, 91]), on_notice=calls.append)
    m.poll()  # prime
    m.poll()  # 91 -> crosses 90
    assert len(calls) == 1
    assert calls[0].threshold == 90
    assert calls[0].kind is NoticeKind.COMPACT_GUIDANCE


def test_hysteresis_default_zero_margin_rearms_immediately_below_threshold():
    # Default rearm_margin=0: dropping to anything < threshold re-arms, even
    # a drop of a single point (89 after 91) — pinned against the real
    # rearm_margin=0.0 default in the constructor, not assumed.
    m, fired, _reports = _monitor([10, 91, 89, 91])
    m.poll()  # prime
    m.poll()  # 91 -> fire
    assert m.is_armed(90) is False
    m.poll()  # 89 -> below 90 (margin 0) -> re-arm
    assert m.is_armed(90) is True
    m.poll()  # 91 -> fires again
    ninety = [n for n in fired if n.threshold == 90]
    assert len(ninety) == 2

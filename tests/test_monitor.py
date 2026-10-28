"""Context monitor: hysteresis, re-arm-after-drop, prime-on-first-read.

Covers.
"""

from gnomon.harness import ContextMonitor, NoticeKind


class Feed:
    """A scripted read_context: returns successive values, then repeats last."""

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


def test_prime_on_first_read_does_not_fire_retroactively():
    # Resumed session already at 92% on the very first read: must NOT fire.
    m, fired, reports = _monitor([92])
    assert m.poll() == []
    assert fired == []
    assert m.primed is True
    # 90 was already exceeded at prime -> spent (disarmed); 95 still armed.
    assert m.is_armed(90) is False
    assert m.is_armed(95) is True
    # It still reported the reading up, even on the prime poll.
    assert reports == [92]


def test_fires_once_per_arming():
    # Prime low, climb across 90, stay high: fires exactly once.
    m, fired, _reports = _monitor([10, 91, 92, 93])
    m.poll()  # prime at 10 (both armed)
    m.poll()  # 91 -> fire 90
    m.poll()  # 92 -> no re-fire
    m.poll()  # 93 -> no re-fire
    ninety = [n for n in fired if n.threshold == 90]
    assert len(ninety) == 1
    assert ninety[0].kind is NoticeKind.COMPACT_GUIDANCE
    assert ninety[0].used_percentage == 91


def test_rearm_after_drop_then_refire():
    # Climb (fire), a /compact drop, climb again (fire again).
    m, fired, _reports = _monitor([10, 91, 40, 92])
    m.poll()  # prime at 10
    m.poll()  # 91 -> fire
    assert m.is_armed(90) is False
    m.poll()  # 40 -> drop re-arms
    assert m.is_armed(90) is True
    m.poll()  # 92 -> fire again
    ninety = [n for n in fired if n.threshold == 90]
    assert len(ninety) == 2
    assert [n.used_percentage for n in ninety] == [91, 92]


def test_backstop_threshold_is_fyi_and_independent():
    m, fired, _reports = _monitor([10, 96])
    m.poll()  # prime
    m.poll()  # 96 -> crosses BOTH 90 and 95
    kinds = {n.threshold: n.kind for n in fired}
    assert kinds[90] is NoticeKind.COMPACT_GUIDANCE
    assert kinds[95] is NoticeKind.FYI


def test_none_reading_is_ignored():
    m, fired, reports = _monitor([None, None, 91])
    assert m.poll() == []  # None -> nothing, not primed
    assert m.primed is False
    m.poll()  # None again
    m.poll()  # 91 -> this is the FIRST real read -> primes, no fire
    assert fired == []
    assert m.primed is True
    assert reports == [91]


def test_context_report_reads_now_and_emits_context_report_kind():
    emitted = []
    m = ContextMonitor(read_context=Feed([73]), on_notice=emitted.append)
    notice = m.context_report()
    assert notice.kind is NoticeKind.CONTEXT_REPORT
    assert notice.used_percentage == 73
    assert emitted == [notice]


def test_rearm_margin_suppresses_boundary_flap():
    # With a margin, dipping just below the threshold does NOT re-arm.
    m, fired, _reports = _monitor([10, 91, 89, 91], rearm_margin=5)
    m.poll()  # prime
    m.poll()  # 91 -> fire
    m.poll()  # 89 -> above (95-... ) ; 89 > 90-5=85 -> NOT re-armed
    assert m.is_armed(90) is False
    m.poll()  # 91 -> still disarmed -> no second fire
    assert len([n for n in fired if n.threshold == 90]) == 1


def test_heartbeat_fires_once_per_quiescence():
    emitted = []
    m = ContextMonitor(
        read_context=Feed([10]),
        on_notice=emitted.append,
        heartbeat_interval=5.0,
    )
    m.note_activity(now=0.0)
    assert m.maybe_heartbeat(now=3.0) is None  # not quiet long enough
    n = m.maybe_heartbeat(now=6.0)  # quiet past interval -> fire
    assert n is not None and n.kind is NoticeKind.HEARTBEAT
    assert m.maybe_heartbeat(now=9.0) is None  # already spent
    m.note_activity(now=10.0)  # activity re-arms
    assert m.maybe_heartbeat(now=16.0) is not None

"""Notice delivery: EVERY agent-facing notice is a SUBMITTED turn; only the
CONTEXT_REPORT (a control-channel answer) is not typed into the child.

Covers the slice-2a STEP-0 architect ruling (
delivery side).

All of COMPACT_GUIDANCE (90%), FYI (95% backstop) and HEARTBEAT reach the child
through the full inject_line sequence (ESC-ESC clear -> verbatim text -> CR
submit) so each lands as a processed user turn. The old raw, unsubmitted path is
gone — it dropped text into the live prompt buffer (pollution). CONTEXT_REPORT is
answered over the control channel and never typed. The real-pty proof that the
CR actually arrives at the child lives in test_pty_smoke.py.
"""

from gnomon.harness import (
    ESC_ESC,
    HarnessProxy,
    Injector,
    Notice,
    NoticeKind,
    SUBMIT,
    SingleWriteSite,
)
from gnomon.harness.proxy import SUBMITTED_NOTICE_KINDS


def _proxy():
    """A HarnessProxy over a capturing (fd-less) write site, no wall-clock beats."""
    captured = []

    def fake_write(_fd, data):
        captured.append(bytes(data))
        return len(data)

    writer = SingleWriteSite(master_fd=-1, writer=fake_write)
    injector = Injector(writer, sleep=lambda _s: None)  # no real beats
    proxy = HarnessProxy(
        master_fd=-1,
        pid=-1,
        writer=writer,
        injector=injector,
        stdin_fd=None,
    )
    return proxy, writer


def test_compact_guidance_is_delivered_as_submitted_turn():
    proxy, writer = _proxy()
    notice = Notice(
        kind=NoticeKind.COMPACT_GUIDANCE,
        text="PENCILS DOWN prepare-for-compact",
        used_percentage=91.0,
        threshold=90,
    )
    proxy.inject_notice(notice)

    # PTY-hooks WS-D (2026-09-27): notices are NON-disruptive now — no ESC-ESC; still a SUBMITTED turn: text, CR submit.
    assert writer.injected_bytes == b"PENCILS DOWN prepare-for-compact" + SUBMIT
    # The load-bearing pieces of a submitted turn.
    assert not writer.injected_bytes.startswith(ESC_ESC)  # WS-D: never interrupts
    assert b"PENCILS DOWN prepare-for-compact" in writer.injected_bytes
    assert writer.injected_bytes.endswith(SUBMIT)  # submitted with \r, not \n
    assert SUBMIT == b"\r"
    # Recorded on the notice log too.
    assert proxy.notices == [notice]


def test_fyi_is_delivered_as_submitted_turn():
    # STEP 0: the 95% FYI backstop is now a SUBMITTED turn, not a raw inject.
    proxy, writer = _proxy()
    notice = Notice(
        kind=NoticeKind.FYI,
        text="context at 96% backstop",
        used_percentage=96.0,
        threshold=95,
    )
    proxy.inject_notice(notice)

    # PTY-hooks WS-D (2026-09-27): notices are NON-disruptive now — no ESC-ESC.
    assert writer.injected_bytes == b"context at 96% backstop" + SUBMIT
    assert not writer.injected_bytes.startswith(ESC_ESC)
    assert writer.injected_bytes.endswith(SUBMIT)  # submitted with \r
    assert proxy.notices == [notice]


def test_heartbeat_is_delivered_as_submitted_turn():
    # STEP 0: the heartbeat quiescence nudge is a SUBMITTED turn too.
    proxy, writer = _proxy()
    notice = Notice(kind=NoticeKind.HEARTBEAT, text="heartbeat nudge")
    proxy.inject_notice(notice)
    assert writer.injected_bytes == b"heartbeat nudge" + SUBMIT  # PTY-hooks WS-D (2026-09-27): notices are NON-disruptive now — no ESC-ESC
    assert writer.injected_bytes.endswith(SUBMIT)


def test_context_report_is_not_typed_into_child():
    proxy, writer = _proxy()
    notice = Notice(
        kind=NoticeKind.CONTEXT_REPORT,
        text="context report: 73%",
        used_percentage=73.0,
    )
    proxy.inject_notice(notice)
    # Answered over the control channel; nothing typed into the child.
    assert writer.injected_bytes == b""
    # ... but still recorded.
    assert proxy.notices == [notice]


def test_submitted_kinds_default_is_all_agent_facing_notices():
    # STEP 0: the default is now all three agent-facing kinds; only the
    # CONTEXT_REPORT control-channel answer is excluded.
    assert SUBMITTED_NOTICE_KINDS == frozenset(
        {NoticeKind.COMPACT_GUIDANCE, NoticeKind.FYI, NoticeKind.HEARTBEAT}
    )
    assert NoticeKind.CONTEXT_REPORT not in SUBMITTED_NOTICE_KINDS


def test_submitted_kinds_override_can_exclude_a_kind():
    # The classification is still one-argument-configurable: narrow it and the
    # excluded kind is simply not typed (NO raw-inject pollution fallback).
    captured = []
    writer = SingleWriteSite(master_fd=-1, writer=lambda _f, d: (captured.append(bytes(d)), len(d))[1])
    injector = Injector(writer, sleep=lambda _s: None)
    proxy = HarnessProxy(
        master_fd=-1,
        pid=-1,
        writer=writer,
        injector=injector,
        submitted_notice_kinds=frozenset({NoticeKind.COMPACT_GUIDANCE}),
        stdin_fd=None,
    )
    proxy.inject_notice(Notice(kind=NoticeKind.FYI, text="excluded", threshold=95))
    # Not submitted AND not raw-injected — nothing typed at all.
    assert writer.injected_bytes == b""


def test_default_injector_is_constructed_when_none_given():
    # Even with no injector wired, a compact-guidance notice can be submitted:
    # the proxy defaults an Injector over the same single write site.
    captured = []
    writer = SingleWriteSite(master_fd=-1, writer=lambda _f, d: (captured.append(bytes(d)), len(d))[1])
    proxy = HarnessProxy(
        master_fd=-1,
        pid=-1,
        writer=writer,
        stdin_fd=None,
    )
    # Beats would sleep on the default injector; monkeypatch its sleep to no-op.
    proxy._injector._sleep = lambda _s: None
    proxy.inject_notice(Notice(kind=NoticeKind.COMPACT_GUIDANCE, text="x", threshold=90))
    assert writer.injected_bytes == b"x" + SUBMIT  # PTY-hooks WS-D (2026-09-27): notices are NON-disruptive now — no ESC-ESC

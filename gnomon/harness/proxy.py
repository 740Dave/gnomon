"""The pump — one ``select()`` over the three fds.

MUST run ONE ``select()`` cycle over ``[master_fd, own_stdin, control_fd]`` and
forward bytes both directions transparently — no per-fd poll loop, no terminal
emulation. The human's real terminal does all
ANSI/rendering/keys.

Wiring in this class:

* master_fd readable  -> copy child output to our stdout; note activity so the
  monitor's heartbeat re-arms.
* stdin readable      -> forward the human's keystrokes via ``write_keys``.
  PTY-hooks WS-A/WS-B: when an ``operator_tap`` is wired, the keystrokes go
  through it — one ``source: operator`` provenance record per submitted line,
  ``/`` lines through the hook pipeline, the backslash command plane intercepted —
  and ``note_operator_activity`` feeds the dual-idle stall trigger. Without a
  tap: legacy raw forward. Optional: pass ``stdin_fd=None`` to omit (e.g. in
  tests, or when a session is driven purely over the control channel).
* control_fd readable -> hand the bytes to the control channel, which decodes a
  command and drives the actuator (which types via the single write site).
* on the select timeout -> poll the context monitor and maybe heartbeat.

Keystrokes and injects converge on the ONE ``os.write(master_fd)`` inside
:class:`~gnomon.harness.writer.SingleWriteSite`.
"""

from __future__ import annotations

import errno
import os
import select
import signal
import sys
import time
from typing import AbstractSet, Any, Callable, List, Optional, Tuple

from .control import ControlChannel
from .hooks import SOURCE_MONITOR_GUIDANCE, SOURCE_RESUME
from .injector import Injector
from .leading_bang import ScreenRefused
from .monitor import ContextMonitor, Notice, NoticeKind
from .rawmode import RawMode
from .writer import SingleWriteSite

#: r2d: a pump step that fails this many times back to back ends ``run()`` (a persistent fault would
#: otherwise spin); one-off faults are recorded and survived.
_MAX_CONSECUTIVE_STEP_FAILURES = 100

#: r2e (r2d Diamond F2): the consecutive cap alone is reset by every clean (idle) step, so a persistent fault
#: that fires only intermittently (fail, idle, fail, ...) would never end ``run()`` (a zombie harness that
#: silently drops output). Two more bounds on the failures that reach ``run()``'s belt (a contained refusal
#: or contained dispatch error never reaches it, so refusals never count): this many inside a sliding window
#: of ``_STEP_FAILURE_WINDOW_SECONDS``, or (r2f) this many inside a sliding 24 h window
#: (``_MAX_STEP_FAILURES_IN_DAY``; the count decays, it is not a lifetime total).
_MAX_STEP_FAILURES_IN_WINDOW = 100
_STEP_FAILURE_WINDOW_SECONDS = 300.0
#: r2f (r2e Diamond F2/F1): the r2e LIFETIME total (1000, never decaying) would kill a multi-day session that
#: only ever takes rare genuine faults. It is now a RATE: this many failures inside a sliding
#: ``_STEP_FAILURE_DAY_SECONDS`` (24 h). The window cap above still catches a fast fault; this one still
#: ends a slow persistent fault (about one per 86 s sustained), but the count decays, so it can never
#: accumulate over days.
_MAX_STEP_FAILURES_IN_DAY = 1000
_STEP_FAILURE_DAY_SECONDS = 86400.0

#: r2f2 (r2f Diamond F1): ``pump_errors`` keeps only the most recent this many entries, each message cut to
#: ``_PUMP_ERROR_MESSAGE_CHARS`` (a long-lived run would otherwise grow it without bound). Stays a plain list
#: so existing readers (indexing, ``len``, ``== []``) are unchanged.
_PUMP_ERRORS_MAX = 1000
_PUMP_ERROR_MESSAGE_CHARS = 500

#: Notice kinds delivered to the child as a SUBMITTED user turn — the full
#: :meth:`Injector.inject_line` sequence (ESC-ESC clear -> verbatim text -> ``\r``
#: submit).
#:
#: Architect ruling, 2026-09-23 (slice-2a STEP 0): ALL agent-facing notices are
#: submitted ``inject_line`` turns — ``COMPACT_GUIDANCE``, ``FYI`` and
#: ``HEARTBEAT``. The earlier default (compact-guidance only, the other kinds on
#: a raw unsubmitted path) is retired: a raw ``writer.inject`` drops text into
#: the child's LIVE PROMPT BUFFER without submitting it, which is pollution — it
#: sits unsent in front of the human's next keystrokes, not a benign no-op. The
#: only kind that is NOT typed into the child is ``CONTEXT_REPORT``, which is a
#: control-channel answer to an on-demand query, not a notice.
#:
#: This remains a constructor argument so the classification lives in one place
#: and stays separately testable (e.g. an audit could narrow it again).
#:
#: ⚠ PTY-hooks WS-D (2026-09-27): these are still SUBMITTED turns, but they are
#: now NON-DISRUPTIVE — ``inject_line(clear=False)``: no ESC-ESC, Claude Code
#: queues the line behind a working turn instead of interrupting it. (Contract:
#: it concatenates with unsent operator text; the dual-idle gate (WS-C) means
#: the heartbeat/resume only fires when the operator is idle.) WS-A attribution:
#: COMPACT_GUIDANCE / FYI -> ``source: monitor-guidance``; HEARTBEAT (the stall
#: keep-alive) -> ``source: resume``.
SUBMITTED_NOTICE_KINDS: AbstractSet[NoticeKind] = frozenset(
    {NoticeKind.COMPACT_GUIDANCE, NoticeKind.FYI, NoticeKind.HEARTBEAT}
)

try:  # optional; only used to propagate window size on real terminals
    import fcntl
    import termios

    _HAVE_WINSZ = True
except Exception:  # pragma: no cover - non-POSIX
    _HAVE_WINSZ = False


def _set_winsize_from(dst_fd: int, src_fd: int) -> None:
    """Copy the terminal window size from ``src_fd`` to the pty ``dst_fd``."""
    if not _HAVE_WINSZ:
        return
    try:
        if not os.isatty(src_fd):
            return
        packed = fcntl.ioctl(src_fd, termios.TIOCGWINSZ, b"\x00" * 8)
        fcntl.ioctl(dst_fd, termios.TIOCSWINSZ, packed)
    except OSError:
        pass


class HarnessProxy:
    """Transparent PTY proxy tying spawn + single-write + control + monitor.

    The GUI/rendezvous is out of scope for this slice; ``on_notice`` is where a
    later slice reports up. By default a threshold/heartbeat/context notice is
    typed into the child locally (the monitor "injects notices locally") via the
    same single write site.
    """

    def __init__(
        self,
        master_fd: int,
        pid: int,
        writer: SingleWriteSite,
        control_channel: Optional[ControlChannel] = None,
        monitor: Optional[ContextMonitor] = None,
        injector: Optional[Injector] = None,
        submitted_notice_kinds: AbstractSet[NoticeKind] = SUBMITTED_NOTICE_KINDS,
        stdin_fd: Optional[int] = 0,
        stdout_fd: int = 1,
        poll_interval: float = 0.5,
        read_size: int = 65536,
        local_control: Optional[Any] = None,
        operator_tap: Optional[Any] = None,
        on_exit: Optional[Any] = None,
        output_watchers: Optional[List[Any]] = None,
        hub_link: Optional[Any] = None,
        tickers: Optional[List[Any]] = None,
        on_notice_hold: Optional[Any] = None,
        on_loop_failure: Optional[Any] = None,
        post_compact_boot: Optional[Any] = None,
    ) -> None:
        self._master_fd = master_fd
        # the post-compact boot prompt (harness.post_compact_boot.
        # PostCompactBoot), or None (flag off: the pump is byte-identical to before).
        # It is delivered BEFORE any queued/held notice is typed.
        self._post_compact_boot = post_compact_boot
        # 2026-09-29 stall-hold: agent-facing notices wait for the operator's
        # line (shared rule, harness.line_hold, via ``monitor.line_hold_reason``).
        # Held threshold notices queue here; ``on_notice_hold(decision, notice,
        # reason)`` records held / released / dropped-stale / dropped-held.
        self._held_notices: List[Notice] = []
        #: r2c: writer-backstop refusals contained by the pump: (where, reasons, message).
        self.screen_refusals: List[Tuple[str, Tuple[str, ...], str]] = []
        #: r2d: (where, exception type, message) for every non-refusal exception contained in the pump.
        self.pump_errors: List[Tuple[str, str, str]] = []
        self._on_notice_hold = on_notice_hold
        # r2f: ``on_loop_failure(fields)`` records every failure that reaches run()'s belt (the
        # injection_log, so the rate is measurable). Called inside a try: its own failure is contained.
        # Ruled (alpha-architect, r2f Diamond): failures already recovered by ``_contained`` are NOT pump-loop
        # failures, so they stay off the injection_log (they are in ``pump_errors`` / ``screen_refusals``).
        # Only ``Exception`` from the hook is swallowed: KeyboardInterrupt / SystemExit raised from a log
        # write must propagate.
        self._on_loop_failure = on_loop_failure
        # D15 amendment (2026-09-29): the self-healing hub link (harness.hublink.
        # HubLink). Its CURRENT socket joins the single select() and its tick()
        # reconnects/re-registers; ``tickers`` are further periodic callables (the
        # DSD-notice inbox). Both default to None: the pump is unchanged.
        self._hub_link = hub_link
        self._tickers = list(tickers or [])
        self._pid = pid
        self._writer = writer
        self._control = control_channel
        # fix-pass WS2a: the agent-side LOCAL control listener
        # (control.LocalControlListener). Its listen fd + live connection fds
        # join the SAME single select() — no second loop, no thread. ``None``
        # (the default) leaves the pump exactly as before.
        self._local_control = local_control
        self._monitor = monitor
        # Actionable (submitted) notices go through the full inject_line
        # sequence. Default to an Injector over the same single write site so a
        # compact-guidance notice can ALWAYS be submitted, even if the caller
        # wired no injector.
        self._injector = injector if injector is not None else Injector(writer)
        self._submitted_notice_kinds = frozenset(submitted_notice_kinds)
        self._stdin_fd = stdin_fd
        self._stdout_fd = stdout_fd
        self._poll_interval = poll_interval
        self._read_size = read_size
        self._child_alive = True
        self._notices: List[Notice] = []
        # PTY-hooks WS-B: the operator input tap (optap.OperatorInputTap) — None
        # = legacy raw keystroke forward.
        self._operator_tap = operator_tap
        # PTY-hooks WS-E: called ONCE with the exit status after the child is
        # reaped (the agent-end lifecycle point). Exceptions are swallowed so a
        # hook can never change the exit status.
        self._on_exit = on_exit
        self._exit_fired = False
        # PTY-hooks: read-only observers of child output (e.g. the opt-in
        # startup folder-trust accepter). Never alter the forwarded bytes.
        self._output_watchers = list(output_watchers or [])

    @property
    def child_alive(self) -> bool:
        return self._child_alive

    @property
    def notices(self) -> List[Notice]:
        return list(self._notices)

    # -- notice sink ---------------------------------------------------------

    def inject_notice(self, notice: Notice) -> None:
        """Type a monitor notice into the child (the "inject locally" side).

        Two explicit dispositions, keyed on :class:`NoticeKind` (STEP-0 ruling):

        * ``CONTEXT_REPORT`` — NOT typed back to the child at all; it answers an
          agent query over the control channel;
        * every other (agent-facing) kind — by default all of
          ``COMPACT_GUIDANCE``, ``FYI`` and ``HEARTBEAT`` — delivered as a
          SUBMITTED user turn via the full :meth:`Injector.inject_line` sequence
          (ESC-ESC clear -> verbatim text -> ``\r`` submit), so the child
          processes it as a turn.

        The old raw, unsubmitted ``writer.inject`` fallback is GONE: dropping
        notice text into the child's live prompt buffer without submitting it is
        pollution, not a benign informational nudge. A kind deliberately excluded
        from :attr:`_submitted_notice_kinds` is simply not typed into the child.
        """
        self._notices.append(notice)

        if notice.kind is NoticeKind.CONTEXT_REPORT:
            return
        if notice.kind in self._submitted_notice_kinds:
            # 2026-09-29 stall-hold: never type onto the operator's unsent line.
            reason = self._line_hold_reason()
            if reason is None and self._boot_blocks():
                reason = "post-compact boot prompt pending (it goes first)"
            if reason is not None:
                if notice.kind is NoticeKind.HEARTBEAT:
                    # A keep-alive is only meaningful NOW; the monitor already
                    # holds it at source. A late one would chase a fresh submit.
                    self._note_hold("dropped-held", notice, reason)
                    return
                # Audit note 1: only the NEWEST notice per threshold is kept (a
                # compaction between two 90% crossings must not type it twice).
                for old in [n for n in self._held_notices if n.threshold == notice.threshold]:
                    self._held_notices.remove(old)
                    self._note_hold("superseded", old, "a newer notice for the same threshold")
                self._held_notices.append(notice)
                self._note_hold("held", notice, reason)
                return
            self._type_notice(notice)
        # else: excluded kind — NOT typed. No raw-inject pollution path.

    def _boot_blocks(self) -> bool:
        """True while the post-compact boot prompt is still owed AFTER
        giving it its chance to type now. Never raises."""
        boot = self._post_compact_boot
        if boot is None:
            return False
        try:
            boot.tick()
            return bool(boot.pending)
        except Exception:  # noqa: BLE001 - the boot leg can never break the pump
            return False

    def _line_hold_reason(self) -> Optional[str]:
        if self._monitor is None:
            return None
        try:
            return self._monitor.line_hold_reason()
        except Exception:  # noqa: BLE001 - the monitor's own reader already fails toward 'unknown'
            return None

    def _note_refusal(self, where: str, exc: ScreenRefused) -> None:
        """Record (in memory, and on stderr) that a writer-backstop refusal was contained at
        ``where``. Never raises."""
        self.screen_refusals.append((where, tuple(exc.reasons), str(exc)))
        try:
            sys.stderr.write(f"gnomon: injection REFUSED at {where}: {exc}\n")
        except Exception:  # noqa: BLE001
            pass

    def _contained(self, where: str, fn: Callable[..., Any], *args: Any) -> None:
        """Run one pump step; ANY exception is contained and recorded loudly, never propagated
        (r2d, g71-amendment-r2c (2): a ScreenRefused or any other error out of dispatch must not kill
        the pump; BaseException such as KeyboardInterrupt / SystemExit still propagates)."""
        try:
            fn(*args)
        except ScreenRefused as exc:
            self._note_refusal(where, exc)
        except Exception as exc:  # noqa: BLE001
            self._note_failure(where, exc)

    def _note_failure(self, where: str, exc: BaseException) -> None:
        """Record (in memory, and on stderr) a non-refusal exception contained at ``where``. Never raises."""
        self.pump_errors.append((where, type(exc).__name__, str(exc)[:_PUMP_ERROR_MESSAGE_CHARS]))
        if len(self.pump_errors) > _PUMP_ERRORS_MAX:
            del self.pump_errors[:len(self.pump_errors) - _PUMP_ERRORS_MAX]
        try:
            sys.stderr.write(f"gnomon: pump step {where} FAILED ({type(exc).__name__}: {exc}); "
                             "continuing\n")
        except Exception:  # noqa: BLE001
            pass

    def _record_loop_failure(self, exc: BaseException, consecutive: int, in_window: int, in_day: int,
                             fatal: bool) -> None:
        """r2f: one injection_log record per failure that reached run()'s belt (the ``on_loop_failure`` hook).

        Rulings (alpha-architect, r2f Diamond): failures already recovered by ``_contained`` are NOT
        pump-loop failures, so they stay off the injection_log (they live in ``pump_errors`` /
        ``screen_refusals``). Only ``Exception`` from the hook is swallowed: a BaseException from a log
        write (KeyboardInterrupt, SystemExit) must propagate. Never raises otherwise."""
        if self._on_loop_failure is None:
            return
        try:
            self._on_loop_failure({
                "exc_type": type(exc).__name__, "error": str(exc)[:300],
                "consecutive": consecutive, "in_window": in_window, "in_day": in_day,
                "window_seconds": _STEP_FAILURE_WINDOW_SECONDS, "day_seconds": _STEP_FAILURE_DAY_SECONDS,
                "caps": [_MAX_CONSECUTIVE_STEP_FAILURES, _MAX_STEP_FAILURES_IN_WINDOW,
                         _MAX_STEP_FAILURES_IN_DAY],
                "fatal": fatal})
        except Exception:  # noqa: BLE001 - provenance never breaks the pump
            pass

    def _note_hold(self, decision: str, notice: Notice, reason: str) -> None:
        if self._on_notice_hold is not None:
            try:
                self._on_notice_hold(decision, notice, reason)
            except Exception:  # noqa: BLE001 - provenance never breaks the pump
                pass

    def _flush_held_notices(self) -> None:
        """Deliver queued threshold notices once the operator's line allows it.
        A notice whose threshold has RE-ARMED (context fell back below it, e.g. a
        compaction happened meanwhile) is stale and dropped, not typed."""
        if not self._held_notices:
            return
        reason = self._line_hold_reason()
        if reason is not None:
            return
        if self._boot_blocks():
            return  # the boot prompt was owed first; queued notices wait for it
        pending, self._held_notices = self._held_notices, []
        for notice in pending:
            if (notice.threshold is not None and self._monitor is not None
                    and self._monitor.is_armed(notice.threshold)):
                self._note_hold("dropped-stale", notice, "context fell below the threshold while held")
                continue
            self._note_hold("released", notice, "operator line empty")
            self._type_notice(notice)

    def claim_held_notices(self) -> List[str]:
        """Audit note 2: hand every HELD threshold notice (compact-guidance / FYI)
        to the agent via the PostToolUse hook — the operator's line is never
        touched. Each is recorded ``delivered-via-posttooluse`` and removed, so it
        is NOT typed later on Enter. A stale one (threshold re-armed) is dropped."""
        texts: List[str] = []
        pending, self._held_notices = self._held_notices, []
        for notice in pending:
            if (notice.threshold is not None and self._monitor is not None
                    and self._monitor.is_armed(notice.threshold)):
                self._note_hold("dropped-stale", notice, "context fell below the threshold while held")
                continue
            self._note_hold("delivered-via-posttooluse", notice,
                            "returned to the agent as PostToolUse additionalContext")
            texts.append(notice.text)
        return texts

    def _type_notice(self, notice: Notice) -> None:
        # Submitted user turn, NON-disruptive (WS-D): text -> CR submit.
        source = SOURCE_RESUME if notice.kind is NoticeKind.HEARTBEAT else SOURCE_MONITOR_GUIDANCE
        try:
            self._injector.inject_line(notice.text, clear=False, source=source,
                                       trigger=notice.kind.value)
        except ScreenRefused as exc:
            # r2c (g71-amendment-r2b (3)): a refused notice is dropped LOUDLY (the writer funnel
            # already wrote the inject-refused record; the hold sink records the drop) and the pump
            # goes on delivering later notices and injections. Fail loud, never fail dead.
            self._note_refusal("notice:" + notice.kind.value, exc)
            self._note_hold("refused", notice, f"screen refused: {exc}")

    # -- one select cycle ----------------------------------------------------

    def _fds(self) -> List[int]:
        fds = [self._master_fd]
        if self._stdin_fd is not None:
            fds.append(self._stdin_fd)
        if self._control is not None:
            fds.append(self._control.fd)
        if self._local_control is not None:
            fds.extend(self._local_control.fds())
        if self._hub_link is not None:
            fds.extend(self._hub_link.fds())
        return fds

    def _wfds(self) -> List[int]:
        """The write set: the master, while the writer holds bytes the master has not yet accepted (master_write_nonblocking ON)."""
        fn = getattr(self._writer, "has_pending", None)
        try:
            return [self._master_fd] if (fn is not None and fn() is True) else []
        except Exception:  # noqa: BLE001 - a writer that cannot say is "nothing pending"
            return []

    def step(self, timeout: Optional[float] = None) -> bool:
        """Run ONE select cycle. Returns whether the child is still alive.

        This is ``run``'s body, exposed so the pump is testable without an
        interactive controlling terminal.
        """
        if timeout is None:
            timeout = self._poll_interval
        try:
            # master_write_nonblocking: while the writer holds pending bytes the master is also selected for WRITABILITY, so the
            # relay keeps reading claude's output (and the operator's keystrokes) at all times. Nothing pending (always, with the
            # flag OFF) = the old call exactly: an empty write set.
            readable, writable, _ = select.select(self._fds(), self._wfds(), [], timeout)
        except InterruptedError:  # EINTR (e.g. SIGWINCH) — just loop again
            return self._child_alive
        except OSError as exc:  # pragma: no cover - defensive
            if exc.errno == errno.EINTR:
                return self._child_alive
            raise

        if writable:
            self._contained("master-write", self._writer.flush_pending)
        for fd in readable:
            if fd == self._master_fd:
                self._on_master_readable()
            elif self._stdin_fd is not None and fd == self._stdin_fd:
                self._contained("stdin", self._on_stdin_readable)
            elif self._control is not None and fd == self._control.fd:
                self._contained("control", self._on_control_readable)
            elif self._local_control is not None and self._local_control.owns(fd):
                self._contained("local-control", self._local_control.on_readable, fd)
            elif self._hub_link is not None and self._hub_link.owns(fd):
                self._contained("hub-link", self._hub_link.on_readable, fd)

        # Timeout / periodic work: monitor + heartbeat.
        if self._monitor is not None:
            self._contained("monitor.poll", self._monitor.poll)
            self._contained("monitor.heartbeat", self._monitor.maybe_heartbeat)
        if self._post_compact_boot is not None:
            try:
                self._post_compact_boot.tick()  # before queued notices
            except Exception:  # noqa: BLE001 - never break the pump
                pass
        try:
            self._flush_held_notices()
        except Exception:  # noqa: BLE001 - never break the pump
            pass
        if self._hub_link is not None:
            self._hub_link.tick()  # never raises (HubLink swallows + records)
        for ticker in self._tickers:
            try:
                ticker()
            except Exception:  # noqa: BLE001 - a ticker can never break the pump
                pass
        for watcher in self._output_watchers:  # time-driven observers (optional tick)
            tick = getattr(watcher, "tick", None)
            if tick is not None:
                try:
                    tick()
                except Exception:  # noqa: BLE001
                    pass
        return self._child_alive

    def _on_master_readable(self) -> None:
        try:
            data = os.read(self._master_fd, self._read_size)
        except BlockingIOError:
            return   # EAGAIN on a non-blocking master (master_write_nonblocking): nothing to read now, which is not end-of-file
        except OSError:
            # On Linux a closed pty master read raises EIO at EOF.
            data = b""
        if not data:
            self._child_alive = False
            return
        note = getattr(self._writer, "note_child_output", None)
        if note is not None:
            note()   # the child's first output opens the start hold (master_write_nonblocking); a no-op otherwise
        os.write(self._stdout_fd, data)
        if self._monitor is not None:
            self._monitor.note_activity()
        for watcher in self._output_watchers:
            try:
                watcher(data)
            except Exception:  # noqa: BLE001 - an observer can never break the pump
                pass

    def _on_stdin_readable(self) -> None:
        assert self._stdin_fd is not None
        try:
            data = os.read(self._stdin_fd, self._read_size)
        except OSError:
            data = b""
        if not data:
            # Human's stdin closed — stop forwarding it, keep the child running.
            self._stdin_fd = None
            return
        if self._operator_tap is not None:
            # WS-A/WS-B: source:operator provenance per submitted line, the "/"
            # hook route and the backslash command plane; the tap calls write_keys.
            self._operator_tap.feed(data)
            return
        self._writer.write_keys(data)  # legacy path (no tap wired): not logged

    def _on_control_readable(self) -> None:
        assert self._control is not None
        self._control.feed_readable(self._read_size)

    # -- blocking run --------------------------------------------------------

    def run(self) -> int:
        """Proxy until the child exits; returns the child's exit status.

        Puts our own controlling terminal into raw mode, propagates window size
        (initial + on SIGWINCH), then pumps. Restores the terminal on exit.
        """
        raw_fd = self._stdin_fd if self._stdin_fd is not None else self._stdout_fd

        def _on_winch(_signum, _frame):
            if self._stdin_fd is not None:
                _set_winsize_from(self._master_fd, self._stdin_fd)

        previous_winch = None
        if _HAVE_WINSZ and self._stdin_fd is not None and os.isatty(self._stdin_fd):
            _set_winsize_from(self._master_fd, self._stdin_fd)
            previous_winch = signal.signal(signal.SIGWINCH, _on_winch)

        try:
            with RawMode(raw_fd):
                failures = 0
                stamps: List[float] = []  # monotonic times of the failures inside the sliding 24 h window
                while self._child_alive:
                    try:
                        self.step()
                        failures = 0
                    except Exception as exc:  # noqa: BLE001
                        # r2d: step() contains every dispatch error itself (``_contained``); this is the
                        # belt for anything else (e.g. a select/os.write fault). Record loudly, keep
                        # pumping; a fault that repeats is NOT retried forever, it ends the run as before:
                        # back to back (consecutive), or (r2e, F2) intermittently, by a count inside a
                        # sliding window; r2f: the long-horizon bound is a 24 h RATE, never a lifetime total.
                        failures += 1
                        now = time.monotonic()
                        stamps = [t for t in stamps if now - t <= _STEP_FAILURE_DAY_SECONDS]
                        stamps.append(now)
                        in_window = sum(1 for t in stamps if now - t <= _STEP_FAILURE_WINDOW_SECONDS)
                        in_day = len(stamps)
                        fatal = (failures >= _MAX_CONSECUTIVE_STEP_FAILURES
                                 or in_window >= _MAX_STEP_FAILURES_IN_WINDOW
                                 or in_day >= _MAX_STEP_FAILURES_IN_DAY)
                        self._note_failure("run", exc)
                        self._record_loop_failure(exc, failures, in_window, in_day, fatal)
                        if fatal:
                            raise
                        time.sleep(0.05)
        finally:
            if previous_winch is not None:
                signal.signal(signal.SIGWINCH, previous_winch)

        return self._reap()

    def _reap(self) -> int:
        try:
            _, status = os.waitpid(self._pid, 0)
        except (ChildProcessError, OSError):
            return self._fire_exit(0, "unreapable")
        if os.WIFEXITED(status):
            return self._fire_exit(os.WEXITSTATUS(status), "exited")
        if os.WIFSIGNALED(status):
            return self._fire_exit(128 + os.WTERMSIG(status), f"signal {os.WTERMSIG(status)}")
        return self._fire_exit(status, "unknown")  # pragma: no cover

    def _fire_exit(self, code: int, reason: str) -> int:
        """WS-E agent-end: exactly once; can never alter the exit status."""
        if self._on_exit is not None and not self._exit_fired:
            self._exit_fired = True
            try:
                self._on_exit(code, reason)
            except Exception:  # noqa: BLE001
                pass
        return code

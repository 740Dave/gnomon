"""The single write-site.

Human keystrokes and harness injections MUST converge on exactly ONE
``os.write(master_fd)`` call site, so the child process cannot distinguish an
inject from a keystroke. Injections are logged; keystrokes are not.

This module owns that one call site. Everything that writes to the PTY master
goes through :class:`SingleWriteSite`:

* :meth:`SingleWriteSite.write_keys` — raw human keystrokes, NOT logged.
* :meth:`SingleWriteSite.inject` — harness-originated bytes, logged.

Both funnel into the private :meth:`SingleWriteSite._write`, which is the sole
place ``os.write`` touches the master fd, guarded by a lock so a keystroke and
an inject can never interleave mid-sequence.

PTY-hooks WS-A (2026-09-27) — the ENFORCEMENT CHOKE POINT
--------------------------------------------------------
An optional :class:`~gnomon.harness.hooks.InjectionGate` may be installed
(``gate=``). With a gate:

* :meth:`admit` is how the injector submits one logical injection: the gate runs
  the hook pipeline ONCE, appends ONE provenance record, and only on ALLOW/MODIFY
  calls ``emit`` — whose phase writes then go through :meth:`inject` as before.
* a bare :meth:`inject` call made OUTSIDE an admitted emit (any path that forgot
  to go through :meth:`admit`) is NOT a bypass: it is itself routed through the
  gate as a ``source: unattributed`` event, so it is decided and recorded too.

So a DENY cannot be bypassed by any actuation path — the bytes never reach
``os.write``. With NO gate (``gate=None``, the default) behaviour is byte-for-byte
the pre-hooks behaviour.

Flag ``master_write_nonblocking`` (default OFF)
-------------------------------------------------------------------------------------------------------------------------
``nonblocking=True`` is the ON path. The master fd is made non-blocking; :meth:`_write` appends to ONE ordered pending buffer
(under the same lock, so a keystroke burst and an injection sequence still never interleave) and writes only what the master
accepts; EAGAIN is flow control, not an error; EIO / EBADF still propagate and drop the buffer (the session is over).
The pump asks :meth:`has_pending`, selects the master for writability and calls :meth:`flush_pending`; the bytes stay in the
one write site. The same flag turns on the START HOLD: :meth:`startup_hold_reason` says "held" for a non-operator injection
until the pump has seen the child's first output (:meth:`note_child_output`) OR :data:`START_HOLD_TIMEOUT_SECONDS` after the
child's start, whichever is first (the end is recorded: ``start-hold-released``, ``by`` first output | timeout). The pending buffer is
capped for HARNESS injections only (:data:`PENDING_HARNESS_CAP_BYTES`): an injection that would push the waiting harness bytes over the
cap is refused whole (:class:`PendingCapRefused`, an ``inject-refused`` record naming the cap); operator keystrokes are always queued.
The ``inject`` provenance record is written at enqueue; the time the bytes actually left is the separate ``write-flushed`` record. With ``nonblocking=False`` (the default) none of
this exists: one blocking loop, the same bytes, the same exceptions as before.
"""

from __future__ import annotations

import collections
import contextlib
import fcntl
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .leading_bang import ScreenRefused as _ScreenRefused


#: master_write_nonblocking (ON only): the START HOLD ends at the child's first output OR this many seconds after the child's start,
#: whichever is first (Diamond P2-1: a hold with no bound could keep a harness injection for as long as the child stayed silent).
#: Read at call time, so a test may lower it on the module.
START_HOLD_TIMEOUT_SECONDS = 15.0
#: The reason text of the start hold begins with this; the gate (hooks.py) names the real cause of a hold by it.
START_HOLD_PREFIX = "start hold:"
#: master_write_nonblocking (ON only): the most bytes of HARNESS injections that may wait in the pending buffer (Diamond P2-2). An
#: injection that would push the waiting harness bytes over it is refused whole; operator keystrokes are never refused.
PENDING_HARNESS_CAP_BYTES = 1 << 20


class PendingCapRefused(_ScreenRefused):
    """A harness injection refused whole because the pending buffer already holds (or this injection alone is) more than
    :data:`PENDING_HARNESS_CAP_BYTES` of harness bytes. Nothing of it was queued; a refusal record names the cap."""


@dataclass(frozen=True)
class InjectionRecord:
    """One logged injection. Keystrokes never produce one of these."""

    data: bytes
    monotonic: float = field(default_factory=time.monotonic)
    wall: float = field(default_factory=time.time)

    @property
    def length(self) -> int:
        """Bytes in this injection (logged at enqueue; with ``master_write_nonblocking`` the bytes may leave later)."""
        return len(self.data)


class SingleWriteSite:
    """Owns the PTY master fd and the one write call site to it.

    Parameters
    ----------
    master_fd:
        The PTY master file descriptor (the parent side of ``pty.fork``).
    on_inject:
        Optional sink called with each :class:`InjectionRecord` as it is
        written. Lets a GUI / control channel observe injections without the
        writer knowing about them.
    writer:
        The low-level write function. Defaults to :func:`os.write`. Injected so
        tests can capture bytes without a real fd.
    nonblocking:
        The ``master_write_nonblocking`` ON path (see the module doc). Default ``False`` = the pre-fix writer, unchanged.
    """

    def __init__(
        self,
        master_fd: int,
        on_inject: Optional[Callable[[InjectionRecord], None]] = None,
        writer: Callable[[int, bytes], int] = os.write,
        gate: Optional[Any] = None,
        nonblocking: bool = False,
    ) -> None:
        self._master_fd = master_fd
        self._on_inject = on_inject
        self._writer = writer
        self._lock = threading.Lock()
        self._injection_log: List[InjectionRecord] = []
        # WS-A: the hook gate (hooks.InjectionGate) — None = legacy behaviour.
        self._gate = gate
        # Depth of admitted emits on THIS thread: an inject() inside one is part
        # of an already-decided event and must not be decided/recorded again.
        self._admitted = threading.local()
        # master_write_nonblocking (default OFF): the ordered pending buffer, the flush bookkeeping, the start hold.
        self._nonblocking = False
        self._pending = bytearray()
        self._queued_at = 0.0       # monotonic time of the oldest byte in the buffer
        self._burst_bytes = 0       # bytes queued since the buffer was last empty
        self._burst_deferred = False  # some of this burst could not be written when it was queued
        self._child_output_seen = False
        self._started = time.monotonic()            # the child's start, for the start hold's time bound
        self._start_release_cause: Optional[str] = None   # "first output" | "timeout", once the start hold has ended
        self._segments: "collections.deque[list]" = collections.deque()   # [is_harness, bytes] runs, in buffer order
        self._pending_harness = 0                   # harness bytes still waiting in the buffer
        self._reserved_harness = 0                  # harness bytes reserved by open injection sequences, not yet queued
        self._seq = threading.local()               # this thread's open sequence reservation: .depth, .remaining
        self.nonblock_error: Optional[str] = None
        if nonblocking:
            self._enable_nonblocking()

    def _enable_nonblocking(self) -> None:
        """Make the master fd non-blocking. A fd that cannot be (or a test's fake fd) leaves the writer in the blocking mode and says
        so on ``nonblock_error`` and stderr: a flag must never stop a session from starting."""
        fd = self._master_fd
        try:
            if fd >= 0:
                fcntl.fcntl(fd, fcntl.F_SETFL, fcntl.fcntl(fd, fcntl.F_GETFL) | os.O_NONBLOCK)
        except OSError as exc:
            self.nonblock_error = f"{type(exc).__name__}: {exc}"
            try:
                sys.stderr.write(f"gnomon harness: master_write_nonblocking could not set O_NONBLOCK ({self.nonblock_error}); "
                                 "the master write stays blocking\n")
            except Exception:  # noqa: BLE001
                pass
            return
        self._nonblocking = True

    @property
    def nonblocking(self) -> bool:
        return self._nonblocking

    # -- the ONE write call site --------------------------------------------

    def _write(self, data: bytes, *, harness: bool = False) -> None:
        """The single ``os.write(master_fd, ...)`` call site.

        Holds the lock across the whole (possibly multi-chunk) write so that an
        injection sequence and a keystroke burst cannot interleave on the wire.

        ``master_write_nonblocking`` ON: ``data`` is first appended to the ONE ordered pending buffer, then as much of the buffer as
        the master accepts is written; EAGAIN ends the attempt quietly (the pump flushes the rest when the master is writable);
        any other OSError (EIO / EBADF) drops the buffer and propagates. ``data`` may be empty in that mode: a pure flush
        (:meth:`flush_pending`). ``harness=True`` (injections only; ON path only) is checked against the pending cap under the lock,
        before anything is queued: :class:`PendingCapRefused`."""
        if not data and not (self._nonblocking and self._pending):
            return
        flushed: Optional[dict] = None
        with self._lock:
            nb = self._nonblocking
            if nb:
                if harness and data:
                    self._charge_harness(len(data))
                if data and not self._pending:
                    self._queued_at = time.monotonic()
                    self._burst_bytes = 0
                    self._burst_deferred = False
                if data:
                    if self._segments and self._segments[-1][0] == harness:
                        self._segments[-1][1] += len(data)
                    else:
                        self._segments.append([harness, len(data)])
                    if harness:
                        self._pending_harness += len(data)
                self._pending += data
                self._burst_bytes += len(data)
                remaining = memoryview(bytes(self._pending))   # a copy: the buffer itself is never exported, so it can be resized
            else:
                remaining = memoryview(data)
            sent = 0
            try:
                while remaining:
                    try:
                        n = self._writer(self._master_fd, remaining)  # <-- sole write site
                    except BlockingIOError:
                        if not nb:
                            raise
                        break    # EAGAIN: the master is full; the rest stays queued, in order
                    if n <= 0:  # pragma: no cover - defensive
                        break
                    remaining = remaining[n:]
                    sent += n
            except OSError:
                if nb:
                    self._pending.clear()    # EIO / EBADF: the session is over; a stale buffer must not re-raise forever
                    self._segments.clear()
                    self._pending_harness = 0
                    self._burst_deferred = False
                raise
            if nb:
                del self._pending[:sent]
                left = sent
                while left and self._segments:      # the sent bytes leave the front runs; harness bytes leave the cap's count
                    run = self._segments[0]
                    take = min(left, run[1])
                    run[1] -= take
                    left -= take
                    if run[0]:
                        self._pending_harness -= take
                    if run[1] == 0:
                        self._segments.popleft()
                if self._pending:
                    self._burst_deferred = True
                elif self._burst_deferred:
                    flushed = {"bytes": self._burst_bytes, "deferred": True,
                               "waited_seconds": round(time.monotonic() - self._queued_at, 4)}
                    self._burst_deferred = False
        if flushed is not None:
            self._record_flush(flushed)

    # -- public surface ------------------------------------------------------

    def write_keys(self, data: bytes) -> None:
        """Forward raw human keystrokes to the child. Not logged PER CHUNK.

        WS-A/WS-B: operator input is attributed by the operator input tap
        (``optap.OperatorInputTap``), which records ONE ``source: operator``
        provenance record per submitted line and then calls this."""
        self._write(data)

    @property
    def gate(self) -> Optional[Any]:
        return self._gate

    def set_gate(self, gate: Optional[Any]) -> None:
        self._gate = gate

    def _admitted_depth(self) -> int:
        return getattr(self._admitted, "depth", 0)

    def admit(self, event: Any, emit: Callable[[Any], None]) -> Any:
        """Submit ONE logical injection (WS-A). Returns the gate's ``Outcome``.

        Without a gate: ``emit(event)`` runs unconditionally and an ALLOW outcome
        is returned (legacy). With a gate: decided + recorded exactly once, and
        ``emit`` runs only on ALLOW/MODIFY (or later, on operator approval of a
        DEFER).
        """
        from .hooks import Decision, Outcome

        if self._gate is None:
            # operator ruling the leading-'!' refusal holds with no gate installed too.
            from . import leading_bang as _lb
            from .hooks import SOURCE_OPERATOR

            if getattr(event, "source", None) != SOURCE_OPERATOR:
                refused = _lb.screen_event(getattr(event, "content", None), getattr(event, "meta", None))
                if refused:  # r2c: the shared union screen (leading '!' + the upstream byte classes)
                    return Outcome(Decision.DENY, _lb.gate_reason(refused))
            self._admitted.depth = self._admitted_depth() + 1
            try:
                emit(event)
            finally:
                self._admitted.depth -= 1
            return Outcome(Decision.ALLOW, "no-gate")

        def _emit_admitted(ev: Any) -> None:
            # Round 2b (Diamond r2 F1): the LAST line, immediately before the write. Whatever a gate
            # (or a transform inside it) hands to emit is screened once more for non-operator sources,
            # so no gate implementation can type a leading '!' through this funnel.
            from . import leading_bang as _lb
            from .hooks import SOURCE_OPERATOR as _OP

            if getattr(ev, "source", None) != _OP:
                refused = _lb.screen_event(getattr(ev, "content", None), getattr(ev, "meta", None))
                if refused:
                    self._record_funnel_refusal(ev, refused)
                    raise _lb.refusal_for(refused)
            self._admitted.depth = self._admitted_depth() + 1
            try:
                emit(ev)
            finally:
                self._admitted.depth -= 1

        return self._gate.decide(event, _emit_admitted)

    def _record_funnel_refusal(self, ev: Any, reasons: List[str]) -> None:
        """The loud half of a backstop refusal (r2c): an ``inject-refused`` provenance record naming
        the stage, written BEFORE the exception is raised so the record exists whichever caller
        catches (or fails to catch) it. Never raises."""
        record = getattr(self._gate, "record", None)
        if record is None:
            return
        try:
            from . import leading_bang as _lb
            from .hooks import content_fields

            record("inject-refused", source=getattr(ev, "source", None), reason="; ".join(reasons),
                   reasons=list(reasons), stage="funnel", rule=_lb.RULE, detail=_lb.gate_reason(reasons),
                   **content_fields(getattr(ev, "content", None)))
        except Exception:  # noqa: BLE001
            pass

    def inject(self, data: bytes) -> None:
        """Write harness-originated bytes to the child. Logged.

        With a gate installed and no admitted emit in progress, the write is
        routed through the gate as an ``unattributed`` event first (never a
        bypass)."""
        if self._admitted_depth() == 0:
            from . import leading_bang as _lb
            from .hooks import SOURCE_UNATTRIBUTED, Decision, InjectionEvent

            raw = bytes(data)
            event = InjectionEvent(source=SOURCE_UNATTRIBUTED,
                                   content=raw.decode("utf-8", "surrogateescape"),
                                   trigger="raw-writer-inject")
            outcome = self.admit(event, lambda _ev: self._inject_now(raw))
            if self._gate is None and outcome.decision is Decision.DENY:
                # round 2b (Diamond r2 F4): no gate means no provenance record, so refuse loudly
                raise _lb.refusal_for(_lb.screen_text(event.content))
            return
        self._inject_now(data)

    def _inject_now(self, data: bytes) -> None:
        if self._nonblocking and not self._seq_remaining():
            self._refuse_over_cap(len(data), locked=False)   # before the record: a refused injection is not an injection
        record = InjectionRecord(data=bytes(data))
        # Log before the write so the record's ordering reflects intent even if
        # the write blocks; the lock in _write still serialises the bytes.
        self._injection_log.append(record)
        if self._on_inject is not None:
            self._on_inject(record)
        self._write(data, harness=self._nonblocking)

    # -- master_write_nonblocking: the pending buffer, the flush, the start hold -----

    def has_pending(self) -> bool:
        """True while bytes wait in the pending buffer (always False with the flag OFF)."""
        return bool(self._pending)

    def flush_pending(self) -> None:
        """Write what the master now accepts of the pending buffer (the pump calls this when the master is writable).
        A no-op with the flag OFF or nothing pending. EIO / EBADF propagate, as for any other write."""
        if self._nonblocking and self._pending:
            self._write(b"")

    def note_child_output(self) -> None:
        """The pump saw the child's first output: the start hold opens (idempotent)."""
        if self._child_output_seen:
            return
        self._child_output_seen = True
        self._end_start_hold("first output")

    def start_hold_release_cause(self) -> Optional[str]:
        """``"first output"`` or ``"timeout"`` once the start hold has ended, else ``None`` (always ``None`` with the flag OFF)."""
        return self._start_release_cause

    def _end_start_hold(self, cause: str) -> None:
        """Ends the start hold ONCE (the first cause wins) and says so in the provenance log. Never raises."""
        if self._start_release_cause is not None or not self._nonblocking:
            return
        self._start_release_cause = cause
        record = getattr(self._gate, "record", None)
        if record is None:
            return
        try:
            record("modal-guard", what="start-hold-released", by=cause,
                   waited_seconds=round(time.monotonic() - self._started, 3),
                   detail="held until the child's first output after start; released by " + cause)
        except Exception:  # noqa: BLE001
            pass

    def startup_hold_reason(self) -> Optional[str]:
        """Why a harness-originated injection must wait right now, or ``None``: with the flag ON, until the child's first output OR
        :data:`START_HOLD_TIMEOUT_SECONDS` after the child's start, whichever is first. Operator keystrokes never call this. Flag
        OFF: always ``None``."""
        if not self._nonblocking or self._start_release_cause is not None:
            return None
        if self._child_output_seen:
            self._end_start_hold("first output")
            return None
        limit = float(START_HOLD_TIMEOUT_SECONDS)
        if time.monotonic() - self._started >= limit:
            self._end_start_hold("timeout")
            return None
        return (f"{START_HOLD_PREFIX} held until the child's first output after start, or {limit:g} s after its start "
                "(master_write_nonblocking)")

    def _seq_remaining(self) -> int:
        return getattr(self._seq, "remaining", 0)

    def _charge_harness(self, size: int) -> None:
        """Under the lock, before ``size`` harness bytes are queued: bytes covered by this thread's open sequence reservation were
        checked when it was made and are not checked again; any excess is checked against the cap like a plain injection."""
        covered = min(size, self._seq_remaining())
        if covered:
            self._seq.remaining -= covered
            self._reserved_harness -= covered
        if size - covered:
            self._refuse_over_cap(size - covered, locked=True)

    @contextlib.contextmanager
    def reserve_sequence(self, total: int):
        """One whole injection SEQUENCE (every write the injector makes for one injection: a clear prefix, the text, the CR) is
        checked against the pending cap ONCE, before any part is queued: either all ``total`` bytes may be queued or the sequence is
        refused with :class:`PendingCapRefused` and nothing of it reaches the buffer. The length is reserved under the lock (other
        threads count it), the parts then queue through the one write site without a second check. Flag OFF: does nothing. The
        unused part of the reservation is released on exit. Re-entrant on one thread: the inner call is a no-op."""
        if not self._nonblocking or getattr(self._seq, "depth", 0):
            yield
            return
        with self._lock:
            self._refuse_over_cap(int(total), locked=True)
            self._reserved_harness += int(total)
        self._seq.depth = 1
        self._seq.remaining = int(total)
        try:
            yield
        finally:
            with self._lock:
                self._reserved_harness -= self._seq.remaining
            self._seq.remaining = 0
            self._seq.depth = 0

    def _refuse_over_cap(self, size: int, *, locked: bool) -> None:
        """Refuse a HARNESS injection whole when it would push the waiting harness bytes over the cap (an injection larger than the
        cap is refused even into an empty buffer). Logs the refusal, queues nothing, raises :class:`PendingCapRefused`."""
        cap = int(PENDING_HARNESS_CAP_BYTES)
        waiting = self._pending_harness if locked else self._pending_harness_unlocked()
        waiting += self._reserved_harness if locked else self._reserved_unlocked()   # sequences already cleared count as waiting
        if waiting + size <= cap:
            return
        why = (f"pending buffer cap: {waiting} harness bytes already wait and this injection is {size} bytes; "
               f"the cap is {cap} bytes (master_write_nonblocking); refused whole, nothing queued")
        record = getattr(self._gate, "record", None)
        if record is not None:
            try:
                record("inject-refused", source="harness", reason=why, reasons=["pending-cap"], stage="writer",
                       cap_bytes=cap, pending_harness_bytes=waiting, size=size)
            except Exception:  # noqa: BLE001
                pass
        raise PendingCapRefused(why, ["pending-cap"])

    def _pending_harness_unlocked(self) -> int:
        with self._lock:
            return self._pending_harness

    def _reserved_unlocked(self) -> int:
        with self._lock:
            return self._reserved_harness

    def _record_flush(self, info: dict) -> None:
        """One provenance record when a write that had to wait finally went out: the injection was logged at enqueue (with its length);
        this says when the bytes actually left. Never raises."""
        record = getattr(self._gate, "record", None)
        if record is None:
            return
        try:
            record("write-flushed", source="harness", **info)
        except Exception:  # noqa: BLE001
            pass

    # -- introspection -------------------------------------------------------

    @property
    def injection_log(self) -> List[InjectionRecord]:
        """The injections written so far, in order. Read-only view (a copy)."""
        return list(self._injection_log)

    @property
    def injected_bytes(self) -> bytes:
        """Concatenation of every injected payload, in order."""
        return b"".join(r.data for r in self._injection_log)

    @property
    def master_fd(self) -> int:
        return self._master_fd

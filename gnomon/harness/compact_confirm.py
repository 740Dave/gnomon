"""Fail-loud confirmation that a self-directed ``/compact`` actually STARTED.

Flight finding (fix-pass Part-3, 2026-09-27): ``Actuator.compact`` (and
therefore the ``request_compact`` self-verb) returned ``{"ok": True}`` on the
INJECTION allow-outcome — i.e. the moment the harness *typed* ``/compact`` into
the child, NOT the moment a compaction actually began. That false-positive hid
the crown paste-reclassification failure for three flight attempts: the verb said
"ok" while the ``/compact`` token had been swallowed into a bracketed paste and
no compaction ever ran.

This module closes that gap. After actuation, the actuator waits — BOUNDED — for
the EXISTING self-socket lifecycle stream to deliver a ``compact`` ``phase:
"pre"`` (Claude Code ``PreCompact``) signal. That signal is the child's own
report, over the same private control socket the self-verbs use, that a
compaction has begun (see :mod:`.lifecycle_hook` and ``launcher._on_lifecycle``).
The verb reports ``ok: True`` ONLY if that signal is observed inside the window;
otherwise it reports ``ok: False`` with a ``reason``, so an UNATTENDED caller
knows to retry / repair rather than believing it compacted.

Design notes
------------
* The wait is EPOCH-KEYED: :meth:`arm` opens a new epoch immediately before the
  actuation; :meth:`note_pre_compact` marks the CURRENT epoch seen; only a signal
  observed for the current epoch counts. A stale ``pre`` from a previous compact
  (seen before this epoch was armed) is therefore NOT mistaken for confirmation.
* :meth:`confirm` is BOUNDED by ``timeout`` and always terminates. It advances
  the world between polls through an injected ``poll`` callable so it never busy-
  spins and never hangs forever. In tests ``poll`` (or ``clock``) is injected to
  make the positive / negative-control cases deterministic and fast.
* ⚠ The genuine end-to-end path — a REAL claude emitting ``PreCompact`` over the
  socket while the pump is mid-``compact`` — is LIVE-ONLY and is proven in a
  re-flight, not by these unit tests. The unit tests prove the fail-loud LOGIC
  against a simulated/injected lifecycle signal. See the fix-pass SUMMARY.

stdlib only.
"""

from __future__ import annotations

import math
import time
from typing import Callable, Optional

#: The bounded window (seconds) the compact verb waits for a ``PreCompact``
#: signal before declaring the compaction unobserved. A few seconds: long enough
#: for the child to run its PreCompact hook, short enough to fail loud fast for an
#: unattended caller. Tunable per launch (launch.json ``confirm_compact_timeout``).
COMPACT_CONFIRM_TIMEOUT_SECONDS = 5.0


class CompactConfirmer:
    """Bounded, epoch-keyed observer of the ``compact`` ``phase: "pre"`` signal.

    Parameters
    ----------
    timeout:
        Default bound for :meth:`confirm` (seconds). Overridable per call.
    poll:
        ``poll(remaining: float) -> Optional[bool]`` — advances the world by
        servicing the transport that delivers the ``pre`` signal (in production:
        the local control socket AND the child's terminal output, so the child
        never blocks — audit finding F1), bounded by ``remaining``. It returns
        ``False`` to signal that the transport is DEAD (socket closed / a
        ``select`` error): :meth:`confirm` then stops waiting immediately instead
        of busy-spinning to the deadline (audit finding F3d — the auditor measured
        ~4.1M no-op iterations in 0.5s against the pre-fix loop). ``None`` / ``True``
        means "keep waiting". A ``None`` poll (unwired) falls back to a small
        ``sleep`` (which, with no external notifier, simply times out — the
        fail-loud outcome). Injected so the wait is deterministic in tests.
    clock:
        Monotonic clock. Injected for deterministic timeout tests.
    sleep:
        Fallback wait when no ``poll`` is wired. Injected for tests.
    """

    def __init__(
        self,
        *,
        timeout: float = COMPACT_CONFIRM_TIMEOUT_SECONDS,
        poll: Optional[Callable[[float], None]] = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._timeout = float(timeout)
        self._poll = poll
        self._clock = clock
        self._sleep = sleep
        self._epoch = 0
        self._seen_epoch = -1
        # Re-Diamond N1: set True when the wait was stopped because the CHILD
        # EXITED (as opposed to timing out unobserved). The launcher's confirm
        # poll flips it via :meth:`note_child_exited` when it detects the child is
        # gone; :meth:`confirm` resets it at the start of every wait. It lets the
        # actuator report a reason that distinguishes "child exited" from "not
        # observed in window".
        self._child_exited = False

    @property
    def timeout(self) -> float:
        return self._timeout

    def set_poll(self, poll: Callable[[float], None]) -> None:
        """Wire the world-advancing poll after construction (the launcher does
        this once the proxy + local control listener exist)."""
        self._poll = poll

    def arm(self) -> int:
        """Open a new confirmation epoch. Call IMMEDIATELY before actuating so a
        fast ``PreCompact`` cannot be attributed to a previous compact."""
        self._epoch += 1
        return self._epoch

    def note_pre_compact(self) -> None:
        """Record that a ``compact`` ``phase: "pre"`` signal arrived for the
        current epoch. Wired into ``launcher._on_lifecycle``."""
        self._seen_epoch = self._epoch

    def note_child_exited(self) -> None:
        """Record that the child EXITED during the wait (re-Diamond N1). The
        launcher's confirm poll calls this immediately before returning ``False``
        so a dead child stops the wait AND is reported distinctly."""
        self._child_exited = True

    @property
    def child_exited(self) -> bool:
        """True iff the most recent :meth:`confirm` stopped because the child
        exited (not merely because the window elapsed unobserved)."""
        return self._child_exited

    def _seen(self) -> bool:
        return self._epoch > 0 and self._seen_epoch == self._epoch

    def confirm(self, timeout: Optional[float] = None) -> bool:
        """Wait — BOUNDED — for the current epoch's ``pre`` signal.

        Returns ``True`` iff it was observed inside the window, else ``False``.
        Always terminates within ``timeout`` seconds.
        """
        # Re-Diamond N1: a fresh wait starts with no child-exit observed.
        self._child_exited = False
        bound = self._timeout if timeout is None else float(timeout)
        # F3d defence-in-depth: a non-finite / negative bound (NaN/Infinity slips
        # in from launch.json before the launcher's validate step, or a bad
        # direct caller) would make this loop never terminate. The authoritative
        # refusal is at the config boundary (launcher.validate_confirm_compact_config);
        # here we simply refuse to hang — clamp to the fail-loud default.
        if not math.isfinite(bound) or bound < 0:
            bound = COMPACT_CONFIRM_TIMEOUT_SECONDS
        deadline = self._clock() + bound
        while True:
            if self._seen():
                return True
            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._seen()
            if self._poll is not None:
                # A poll that returns False means the transport is dead: stop
                # waiting NOW rather than spin to the deadline (F3d).
                if self._poll(remaining) is False:
                    return self._seen()
            else:
                self._sleep(min(remaining, 0.05))

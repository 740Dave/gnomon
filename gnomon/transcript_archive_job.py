"""(revision 2026-10-27) — the web job's TIMER for the durable transcript archive.

The long-lived web backend (:8770) runs :class:`ArchiveJob`: one archive run when the web job starts and another
every :data:`gnomon.transcript_archive.INTERVAL_S` (10 minutes) while the ``transcript_archive`` flag is ON. That
covers sessions that end by sign-off, crash or relaunch without touching the every-session hook path.

* NEVER raises into the server: a run that raises is recorded (``last_error``, one log line) and the loop goes on.
* SINGLE-FLIGHT: a tick that finds the previous run still going is skipped.
* Flag OFF: a tick does NOTHING (no archive folder, no heartbeat, no pointer files, no alert).
* After each run the loud check (:func:`gnomon.transcript_archive.coverage_check`) is evaluated and any finding is
  raised as ONE operator alert through the existing alert path (``gnomon.alerts``): again only when the set of
  findings CHANGES, or after :data:`REALERT_S`, so a standing finding does not fire every 10 minutes.
* The free-space floor and its alert live in :func:`gnomon.transcript_archive.run` (a run below the floor refuses,
  writes nothing and alerts).

Neutral (g3): no deployment identifier.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import transcript_archive as ta

#: a standing, unchanged set of findings is re-announced at most this often
REALERT_S = 24 * 3600.0


class ArchiveJob:
    def __init__(self, *, interval: Optional[float] = None, flag_reader: Optional[Callable[[str], bool]] = None,
                 subagents: Optional[Callable[[], str]] = None, run_kwargs: Optional[Dict[str, Any]] = None,
                 alert: Optional[Callable[[str, str, str], None]] = None,
                 wait: Optional[Callable[[float], bool]] = None, log: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self._interval = interval
        self._flag = flag_reader
        self._subagents = subagents
        self._run_kwargs = dict(run_kwargs or {})
        self._alert = alert or ta._default_alert
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self._clock = clock
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.runs = 0
        self.skipped = 0
        self.last: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None
        self.last_findings: List[ta.Finding] = []
        self._alerted_sig: Optional[tuple] = None
        self._alerted_at: float = 0.0

    @property
    def interval(self) -> float:
        return float(self._interval if self._interval is not None else ta.INTERVAL_S)

    def _flag_on(self) -> bool:
        if self._flag is not None:
            return bool(self._flag(ta.FLAG_NAME))
        from . import flags

        return bool(flags.flag(ta.FLAG_NAME))

    def _alert_findings(self, findings: List[ta.Finding]) -> None:
        sig = tuple(sorted((f.code, f.subject) for f in findings))
        if not sig:
            self._alerted_sig = None
            return
        now = self._clock()
        if sig == self._alerted_sig and now - self._alerted_at < REALERT_S:
            return
        counts: Dict[str, int] = {}
        for f in findings:
            counts[f.code] = counts.get(f.code, 0) + 1
        title = "Transcript archive check FAILED: " + ", ".join(f"{n} {c}" for c, n in sorted(counts.items()))
        body = "; ".join(f"{f.code} {f.subject}" for f in findings[:6]) + (
            f" (+{len(findings) - 6} more)" if len(findings) > 6 else "") + ". Run: python -m gnomon.transcript_archive check"
        self._alert(title, body, "check:" + "|".join(f"{c}={n}" for c, n in sorted(counts.items())))
        self._alerted_sig, self._alerted_at = sig, now

    def tick(self) -> Dict[str, Any]:
        """One run. Returns ``{"ran": bool, ...}``. NEVER raises."""
        if not self._lock.acquire(blocking=False):
            self.skipped += 1
            return {"ran": False, "reason": "previous run still going (single-flight)"}
        try:
            try:
                if not self._flag_on():
                    return {"ran": False, "reason": f"{ta.FLAG_NAME} is off"}
                mode = self._subagents() if self._subagents is not None else ta.configured_subagents()
                kw = dict(self._run_kwargs)
                out = ta.run(subagents=mode, **kw)
                findings = ta.coverage_check(
                    roots=kw.get("roots"), archive_root=kw.get("archive_root"),
                    projects_root=kw.get("projects_root"), subagents=mode)
                self._alert_findings(findings)
                self.runs += 1
                self.last, self.last_error, self.last_findings = out, None, findings
                return {"ran": True, "summary": out, "findings": findings}
            except Exception as exc:  # noqa: BLE001 - a background loop never dies of one bad run
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._log(f"[gm-archive] transcript archive run FAILED: {self.last_error}")
                except Exception:  # noqa: BLE001
                    pass
                return {"ran": False, "reason": "error", "error": self.last_error}
        finally:
            self._lock.release()

    def run_loop(self, max_iterations: Optional[int] = None) -> int:
        """Tick now (the run at web-job start), then every :attr:`interval`, until :meth:`stop`."""
        n = 0
        while not self._stop.is_set():
            self.tick()
            n += 1
            if max_iterations is not None and n >= max_iterations:
                break
            if self._wait(self.interval):
                break
        return n

    def start(self) -> None:
        """Run the loop on a daemon thread (idempotent)."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        t = threading.Thread(target=self.run_loop, name="gm-archive", daemon=True)
        self._thread = t
        t.start()

    def stop(self) -> None:
        self._stop.set()

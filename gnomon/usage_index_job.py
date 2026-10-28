"""the BACKGROUND INDEXER (spec requirement-lag; g40, g44).

The design logs each event at once and has this job load it into the usage
DB every few minutes. This job is that loop: every :data:`FU_INDEX_INTERVAL_S` it loads the feature-event JSONL
(and, while ``usage_metrics`` is ON, the X7 injection logs) into the usage DB, so the DB is within
minutes of live without anyone calling the CLI.

* HOST: the long-lived web backend (:8770) runs it until the X23 Scheduler ships; X23 then runs the same
  :meth:`IndexJob.tick` at the same interval, and moving the host is ONE change for every stream (g40: a
  later usage-DB stream is indexed by THIS job, never by a second loop).
* It NEVER runs inside a hook, a self-verb or the emitter (the emitter never touches sqlite): the only
  caller in the package is the web backend's lifespan. A test asserts it by import scan.
* It runs while ``feature_events`` is ON, independent of ``usage_metrics``; indexing the X7
  stream in the same run stays gated by ``usage_metrics``.
* SINGLE-FLIGHT: a tick that finds the previous run still going is SKIPPED (the indexer, never the emitter,
  may hold a lock; it is non-blocking).
* Every run's evidence (``index_run_at``, the emitter-flag sample) lands in the DB ``meta``, so the report
  says how stale the data is and a smoke row can judge liveness on the LATEST run (g44, g35).

Neutral (g3): no deployment identifier.
"""

from __future__ import annotations

import sys
import threading
from typing import Any, Callable, Dict, Optional

from . import usage_metrics as um

#: every few minutes; an architect-choice placeholder (requirement-lag)
FU_INDEX_INTERVAL_S = 300


class IndexJob:
    """One background indexing loop. ``tick`` is the unit of work (also what X23 will run)."""

    def __init__(self, *, interval: Optional[float] = None, db_path: Optional[Callable[[], str]] = None,
                 events_roots: Optional[list] = None, session_roots: Optional[list] = None,
                 flag_reader: Optional[Callable[[str], bool]] = None,
                 wait: Optional[Callable[[float], bool]] = None, log: Optional[Callable[[str], None]] = None) -> None:
        # resolved at CALL time so a test that patches FU_INDEX_INTERVAL_S is honoured
        self._interval = interval
        self._db_path = db_path or um.job_db_path
        self._events_roots = events_roots
        self._session_roots = session_roots
        self._flag = flag_reader
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._log = log or (lambda m: print(m, file=sys.stderr, flush=True))
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.runs = 0
        self.skipped = 0
        self.last: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    # -- flags ---------------------------------------------------------------------------------------
    def _flag_on(self, name: str) -> bool:
        if self._flag is not None:
            return bool(self._flag(name))
        from . import flags

        return bool(flags.flag(name))

    @property
    def interval(self) -> float:
        return float(self._interval if self._interval is not None else FU_INDEX_INTERVAL_S)

    # -- the unit of work -------------------------------------------------------------------------------
    def tick(self) -> Dict[str, Any]:
        """One run. Returns ``{"ran": bool, ...}``. NEVER raises. A tick that finds a run in flight is
        skipped; with ``feature_events`` OFF nothing runs."""
        from . import feature_events as fe

        if not self._lock.acquire(blocking=False):
            self.skipped += 1
            return {"ran": False, "reason": "previous run still going (single-flight)"}
        try:
            try:
                if not self._flag_on(fe.FLAG_EVENTS):
                    return {"ran": False, "reason": f"{fe.FLAG_EVENTS} is off"}
                db = self._db_path()
                out: Dict[str, Any] = {"ran": True}
                if self._flag_on(um.FLAG_NAME):
                    try:
                        out["injection_log"] = um.index(db, roots=self._session_roots)
                    except FileNotFoundError as exc:   # no session root yet: recorded, never fatal
                        out["injection_log_error"] = str(exc)
                out["feature_events"] = um.index_feature_events(db, self._events_roots, emitter_flag=True)
                self.runs += 1
                self.last, self.last_error = out, None
                return out
            except Exception as exc:  # noqa: BLE001 - a background loop never dies of one bad run
                self.last_error = f"{type(exc).__name__}: {exc}"
                try:
                    self._log(f"[gm-index] feature-event index run FAILED: {self.last_error}")
                except Exception:  # noqa: BLE001
                    pass
                return {"ran": False, "reason": "error", "error": self.last_error}
        finally:
            self._lock.release()

    # -- the loop -------------------------------------------------------------------------------------------
    def run_loop(self, max_iterations: Optional[int] = None) -> int:
        """Tick now, then every :attr:`interval`, until :meth:`stop` (or ``max_iterations`` ticks)."""
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
        self._thread = threading.Thread(target=self.run_loop, name="gm-fu-index", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=5)


def run_once() -> Dict[str, Any]:
    """One run with the defaults (the CLI-equivalent entry for X23)."""
    return IndexJob().tick()

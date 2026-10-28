"""Single-flight short-TTL cache (poll-pileup fix, 2026-10-04).

``SingleFlightTTL.get(key, fn)``:

* a stored value is returned while ``now - flight_start < ttl`` (see the stamp rule below);
* otherwise exactly ONE caller runs ``fn``; every concurrent caller for the same key waits for that one
  computation and shares its result (N threads -> one computation);
* an exception is NOT cached: it is raised to the computing caller and to the callers that were waiting on
  that very flight (one call's worth), and the next call computes afresh;
* ``invalidate(key=None)`` drops cached values AND DETACHES any flight in progress (a mutating route calls it so
  the next read is fresh). A caller arriving after an invalidation never joins a flight that began before it:
  it starts (or joins) a fresh one. The detached flight still answers the callers already waiting on it, but
  its result is not stored.
* a stored value is stamped with the time its flight STARTED; the data was read after the start, so a REUSED value
  is never older than ``ttl``. A computation that itself takes longer than ``ttl`` lands already expired: it
  coalesces the concurrent callers (they share it) but is never reused afterwards.
* at most ``max_entries`` keys are kept (least-recently-used evicted), so a caller-controlled key space
  (the ledger query) cannot grow without bound.

Pure stdlib, no web imports. Thread-safe.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import Any, Callable, Hashable, Optional


class _Flight:
    __slots__ = ("event", "value", "error", "started")

    def __init__(self, started: float) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None
        self.started = started


class SingleFlightTTL:
    def __init__(self, ttl: float, clock: Callable[[], float] = time.monotonic, max_entries: int = 64) -> None:
        self.ttl = float(ttl)
        self.max_entries = int(max_entries)
        self._clock = clock
        self._lock = threading.Lock()
        self._values: "collections.OrderedDict[Hashable, Any]" = collections.OrderedDict()   # key -> (started, value)
        self._flights: dict = {}
        self.computations = 0     # observability (tests, /api/health-style probes)

    def __len__(self) -> int:
        with self._lock:
            return len(self._values)

    def get(self, key: Hashable, fn: Callable[[], Any]) -> Any:
        with self._lock:
            hit = self._values.get(key)
            if hit is not None and self._clock() - hit[0] < self.ttl:
                self._values.move_to_end(key)
                return hit[1]
            flight = self._flights.get(key)
            mine = flight is None
            if mine:
                flight = _Flight(self._clock())
                self._flights[key] = flight
        if not mine:
            flight.event.wait()
            if flight.error is not None:
                raise flight.error
            return flight.value
        try:
            value = fn()
        except BaseException as exc:
            flight.error = exc
            with self._lock:
                if self._flights.get(key) is flight:
                    del self._flights[key]
            flight.event.set()
            raise
        flight.value = value
        with self._lock:
            self.computations += 1
            if self._flights.get(key) is flight:          # still attached: no invalidate ran meanwhile
                del self._flights[key]
                self._values[key] = (flight.started, value)
                self._values.move_to_end(key)
                while len(self._values) > self.max_entries:
                    self._values.popitem(last=False)
        flight.event.set()
        return value

    def invalidate(self, key: Optional[Hashable] = None) -> None:
        with self._lock:
            if key is None:
                self._values.clear()
                self._flights.clear()          # detach every flight in progress
            else:
                self._values.pop(key, None)
                self._flights.pop(key, None)

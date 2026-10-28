"""App activity presence: the Apps host PULLS the operator's last-input time from a running app.

Spec: SPEC-X72 sections A, B and I (roadmap checklist g102 / g103). The web job runs ONE poller thread. Every
``INTERVAL_S`` it visits each installed app and, for an app that

* declares ``activity_path`` in its PINNED manifest (the manifest the operator confirmed, never the file on disk), and
* is RUNNING with its health answering, and
* has its port held ONLY by processes in the tree of the pid Gnomon launched (spec I2: a foreign listener, or no
  listener, means the app is skipped and its beacon removed),

fetches ``GET http://127.0.0.1:<port><activity_path>`` (1 s wall-clock deadline, 200 only, no redirect, size-capped),
validates the answer (spec B2) and writes ONE beacon through :mod:`gnomon.harness.operator_activity`:
``app-activity--<app id>.json`` beside the DSD-review beacons. Anything else removes that app's beacon. The beacon holds
the APP's own ``last_input_ts``, so the poll interval does not distort "<N>s ago" (spec B4).

Information only (spec I1): this module writes a timestamp and a label; it keys nothing, grants nothing and clears no
snooze. Gnomon never POSTs to an app (spec H3): the only request this module makes is a GET.

Flag ``app_activity_presence`` (default OFF) AND ``apps_tab`` both gate the poller; with either OFF a tick
makes no request and sweeps every app-activity beacon away.

A heartbeat file ``<DATA>/apps/activity-poller.json`` records each ENABLED tick (``ts``, counts; nothing is written
while the flags are off) so the go-live smoke
(tools/golive_smoke.py ``app-activity-poller``, checklist g44) can prove the loop is alive.
"""

from __future__ import annotations

import os
import socket
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, FrozenSet, List, Optional

from ..harness import operator_activity as _oa
from .manifest import ID_RE
from .ports import process_children, tree_in
from .state import atomic_write_json, manifest_entries, read_json

#: poll cadence and per-request wall-clock deadline (spec B1)
INTERVAL_S = 10.0
TIMEOUT_S = 1.0
HEARTBEAT_NAME = "activity-poller.json"
#: the flags that gate the poller
FLAG = "app_activity_presence"
APPS_FLAG = "apps_tab"
#: a beacon dated more than this far ahead is refused (spec B2)
FUTURE_TOLERANCE_S = 5.0


def heartbeat_path(paths: Any) -> str:
    return os.path.join(paths.apps, HEARTBEAT_NAME)


def validate_answer(body: Any, now: float, window: float = _oa.DEFAULT_ACTIVE_WINDOW) -> Optional[float]:
    """Spec B2: the app's ``last_input_ts`` if ``body`` is a valid answer, else ``None``. Valid = a JSON object
    ``{"schema": 1, "last_input_ts": <finite number>}`` whose time is not more than 5 s in the future and not older than
    ``window`` (the composer's 120 s active window). ``null`` (no input yet), a string, a bool, NaN/inf, a wrong schema
    or a missing key all give ``None``."""
    if not isinstance(body, dict):
        return None
    schema = body.get("schema")
    if isinstance(schema, bool) or schema != 1:
        return None
    ts = _oa._num(body.get("last_input_ts"))        # None for a bool, a non-number, NaN, inf or an int too large for a float
    if ts is None:
        return None
    if ts > now + FUTURE_TOLERANCE_S or now - ts > window:
        return None
    return ts


def fetch_activity(port: int, path: str, timeout: float = TIMEOUT_S) -> Any:
    """GET ``http://127.0.0.1:<port><path>`` and return the parsed JSON. Raises on anything but a 200 with a small JSON
    body inside the wall-clock ``timeout`` (a trickle server cannot hold it). Reuses the presence-sensor seam's hardened
    loopback GET (:meth:`PresenceSource._fetch_http`: no redirect, status 200 only, size cap); the worker thread of that
    class is NOT started (only ``read`` starts it)."""
    src = _oa.PresenceSource(None, fetch_deadline=timeout)
    return src._fetch_http(f"http://127.0.0.1:{int(port)}{path}")


@dataclass(frozen=True)
class Target:
    app_id: str
    label: str
    port: int
    path: str
    tree: FrozenSet[int]


class Scan:
    """ONE process-table snapshot shared by every app in a scan (checklist g91(6)): taken lazily, at most once, and only
    when some app is far enough along to need a process tree."""

    def __init__(self) -> None:
        self._children: Optional[Dict[int, List[int]]] = None

    def tree(self, pid: int) -> FrozenSet[int]:
        if self._children is None:
            self._children = process_children()
        return frozenset(tree_in(self._children, pid))


def healthy(port: int, path: str, deadline: float = TIMEOUT_S) -> bool:
    """True when ``GET http://127.0.0.1:<port><path>`` answers a 2xx status line within ``deadline`` seconds of WALL CLOCK
    for the whole exchange (not per socket operation: a server that trickles bytes cannot hold the poller thread). No
    redirect is followed (a 3xx is unhealthy); only the status line and headers are read; nothing is parsed from the body."""
    end = time.monotonic() + deadline
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=max(0.01, deadline)) as sock:
            sock.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n".encode("ascii"))
            buf = b""
            while b"\r\n" not in buf and len(buf) < 8192:
                rem = end - time.monotonic()
                if rem <= 0:
                    return False
                sock.settimeout(rem)
                chunk = sock.recv(1024)
                if not chunk:
                    break
                buf += chunk
    except (OSError, UnicodeEncodeError, ValueError):
        return False
    fields = buf.split(b"\r\n", 1)[0].decode("latin-1", "replace").split()
    return len(fields) >= 2 and fields[0].startswith("HTTP/") and fields[1].isdigit() and 200 <= int(fields[1]) < 300


def activity_target(host: Any, app_id: str, scan: Scan) -> Optional[Target]:
    """The app to poll, or ``None`` (skip it and remove its beacon). Spec B1 + I2 + I3, from the host's own records, in this
    ORDER (nothing is sent to the port until step 5 has passed, so a foreign holder is never contacted):
    1. the app id matches the manifest id grammar (a hand-edited registry key never reaches a path lookup);
    2. the PINNED manifest declares ``activity_path`` and a port;
    3. launchd reports the agent loaded with a pid (the pid Gnomon launched);
    4. EVERY listener on the port is in that pid's process tree AND is loopback-only (a foreign holder, no listener, or a
       listener on ``*``/``0.0.0.0``, which the Apps status refuses, skips the app);
    5. the app's health answers within a 1 s wall-clock deadline (:func:`healthy`).
    Cost per eligible app: one launchctl state read, one ``lsof``, one health GET (a second ``lsof`` runs after the activity
    request); the single ``ps`` is shared through ``scan``. An app that does not declare ``activity_path`` costs file reads only."""
    if not isinstance(app_id, str) or not ID_RE.match(app_id):
        return None
    pm, _pin = host._pinned(app_id)
    if pm is None or not pm.activity_path or not pm.has_port:
        return None
    port = host._port(app_id)
    if port is None:
        return None
    alive, st = host._alive(app_id)
    if not alive or not st.pid:
        return None
    tree = scan.tree(int(st.pid))
    if not _port_held_only_by(host, int(port), tree):
        return None
    if not healthy(int(port), pm.health_path or "/"):
        return None
    return Target(app_id, pm.name, int(port), pm.activity_path, tree)


def _port_held_only_by(host: Any, port: int, tree: FrozenSet[int]) -> bool:
    """I2/I3: at least one listener on ``port``; EVERY listener is in the tree of the pid Gnomon launched and is bound to
    loopback only."""
    ls = host.probe.listeners_on_port(port)
    return bool(ls) and all(l.pid in tree and l.loopback for l in ls)


class ActivityPoller:
    """One background loop. ``tick`` is the unit of work and NEVER raises. Single-flight: a tick that finds one running is
    skipped."""

    def __init__(self, host_getter: Callable[[], Any], flag_reader: Callable[[str], bool], *,
                 directory: Optional[str] = None, interval: float = INTERVAL_S, timeout: float = TIMEOUT_S,
                 clock: Callable[[], float] = time.time, fetch: Callable[[int, str, float], Any] = fetch_activity,
                 log: Optional[Callable[[str], None]] = None) -> None:
        self._host_getter = host_getter
        self._flag = flag_reader
        self._directory = directory
        self.interval = float(interval)
        self._timeout = float(timeout)
        self._clock = clock
        self._fetch = fetch
        self._log = log or (lambda m: None)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self.runs = 0
        self.last: Optional[Dict[str, Any]] = None
        self.last_error: Optional[str] = None

    # -- helpers ------------------------------------------------------------------------------------------
    def _dir(self) -> str:
        return self._directory if self._directory is not None else _oa.activity_dir()

    def _enabled(self) -> bool:
        return bool(self._flag(FLAG)) and bool(self._flag(APPS_FLAG))

    def _sweep(self, keep: FrozenSet[str] = frozenset()) -> int:
        """Remove every app-activity beacon whose app id is not in ``keep`` (apps that stopped, stopped answering, were
        uninstalled, or all of them when the flag is off). Never follows a symlink."""
        d = self._dir()
        try:
            names = os.listdir(d)
        except OSError:
            return 0
        n = 0
        for name in names:
            if not (name.startswith(_oa.APP_ACTIVITY_PREFIX) and name.endswith(".json")):
                continue
            app_id = name[len(_oa.APP_ACTIVITY_PREFIX):-len(".json")]
            if app_id in keep:
                continue
            if _oa._valid_app_id(app_id) and _oa.remove_app_activity_beacon(app_id, d):
                n += 1
        return n

    def _heartbeat(self, host: Any, now: float, out: Dict[str, Any]) -> None:
        try:
            atomic_write_json(heartbeat_path(host.paths), {"ts": now, "enabled": True, "interval": self.interval,
                                                           "apps": out.get("apps", 0), "wrote": out.get("wrote", 0)})
        except Exception as exc:  # noqa: BLE001 - a heartbeat fault is shown by its absence in the smoke row
            self.last_error = f"heartbeat: {type(exc).__name__}: {exc}"

    # -- the unit of work ---------------------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> Dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            return {"ran": False, "reason": "previous tick still going (single-flight)"}
        try:
            try:
                now = self._clock() if now is None else now
                if not self._enabled():
                    out = {"ran": False, "reason": "flag off", "swept": self._sweep()}
                    self.last = out
                    return out
                host = self._host_getter()
                out = self._poll(host, now)
                self.runs += 1
                self.last, self.last_error = out, None
                self._heartbeat(host, now, out)
                return out
            except Exception as exc:  # noqa: BLE001 - a background loop never dies of one bad run
                self.last_error = f"{type(exc).__name__}: {exc}"
                self._log(f"[gm-web] app-activity poll error: {self.last_error}")
                return {"ran": False, "error": self.last_error}
        finally:
            self._lock.release()

    def _poll(self, host: Any, now: float) -> Dict[str, Any]:
        directory = self._dir()
        wrote: List[str] = []
        apps = 0
        scan = Scan()
        for app_id in sorted(manifest_entries(host.paths)):
            try:
                tgt = activity_target(host, app_id, scan)
            except Exception as exc:  # noqa: BLE001 - one app's probe fault skips that app, never the loop
                self._log(f"[gm-web] app-activity: {app_id}: {type(exc).__name__}: {exc}")
                tgt = None
            if tgt is None:
                continue
            apps += 1
            try:
                ts = validate_answer(self._fetch(tgt.port, tgt.path, self._timeout), now)
            except Exception:  # noqa: BLE001 - no answer, timeout, malformed: nothing is written
                ts = None
            if ts is None:
                continue
            try:                                    # I2 again AFTER the request: the holder must still be ours
                if not _port_held_only_by(host, tgt.port, tgt.tree):
                    continue
                _oa.write_app_activity_beacon(app_id, tgt.label, ts, directory=directory)
                wrote.append(app_id)
            except Exception as exc:  # noqa: BLE001
                self._log(f"[gm-web] app-activity: {app_id}: beacon not written: {type(exc).__name__}: {exc}")
        try:        # housekeeping never stops the tick or the heartbeat
            removed = self._sweep(frozenset(wrote))
        except Exception as exc:  # noqa: BLE001
            removed = 0
            self._log(f"[gm-web] app-activity: sweep: {type(exc).__name__}: {exc}")
        try:
            pruned = _oa.prune_app_activity_beacons(now, directory)
        except Exception as exc:  # noqa: BLE001
            pruned = 0
            self._log(f"[gm-web] app-activity: prune: {type(exc).__name__}: {exc}")
        return {"ran": True, "apps": apps, "wrote": len(wrote), "removed": removed, "pruned": pruned}

    # -- the thread ---------------------------------------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="gm-app-activity", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def read_heartbeat(paths: Any) -> Optional[Dict[str, Any]]:
    """The poller's last heartbeat, or ``None`` (absent or unreadable). Read-only (used by the go-live smoke)."""
    try:
        obj = read_json(heartbeat_path(paths), default=None)   # through fsafe: a FIFO or symlink never blocks or follows
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None

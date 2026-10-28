"""Cross-harness OPERATOR ACTIVITY — shared presence for the stall nudge (2026-09-29).

The operator runs several agents at once (alpha-architect, beta-analyst,
gamma-researcher), each in its own harnessed terminal. Each harness's stall guard
used to know only whether the operator was typing in ITS OWN window, so "the
operator is away" and "the operator is talking to another agent right now" looked identical.
This module lets the harnesses share that one fact, WITHOUT the hub (so it works
for every launch type today).

1. **Activity beacons.** Each harness's operator tap writes a small per-session
   beacon into the machine-local run dir (the same base + hardening as
   :mod:`gnomon.hub.rendezvous`)::

       <run_base>/activity/<session_basename>.json   (dir 0700, file 0600, atomic)

       {"schema": 1, "instance_id": "beta-analyst", "session_dir": "...",
        "pid": 1234, "last_keystroke_ts": 1790.., "last_submit_ts": 1790..,
        "line_state": "empty"|"partial"|"unknown", "updated_ts": 1790..}

   Timestamps are wall-clock epoch seconds (the beacon is read by OTHER
   processes). Throttled: at most one write per ``throttle`` (2 s) while typing —
   a trailing flush on :meth:`ActivityBeacon.tick` lands the last keystroke of a
   burst — plus one IMMEDIATE write on submit (Enter). Never one per keystroke.
   Removed on clean harness exit (only if it still carries our pid).

2. **Reader.** :func:`operator_activity` returns, per LIVE session, seconds since
   the last keystroke / submit and the line state. A beacon whose ``pid`` is dead
   or whose ``updated_ts`` is older than 24 h is ignored. Reads are hardened like
   the rendezvous file: the dir must be a real 0700-ish dir owned by this uid;
   each file is opened O_NOFOLLOW|O_NONBLOCK and fstat-checked (regular, ours,
   not group/other writable, size-capped). Boundary (stated, same as rendezvous):
   a same-uid process can still write a beacon — the bar is "no foreign-uid or
   off-machine influence", not a hostile same-uid adversary.

3. **The stall text is composed at fire time** (:meth:`OperatorContext.compose`):
   operator active in ANOTHER session -> "talking to <agent>, move forward"; nobody
   typing anywhere past ``active_window`` -> "appears away, work autonomously";
   beacons unreadable -> the configured text, unchanged. The D44 line-hold rule is
   applied by the monitor BEFORE composition and is untouched.

5. **DSD review activity** (operator, 2026-09-29; flag ``dsd_review_presence``).
   The Gnomon web server writes one review beacon per DSD the operator is
   actively answering (radio / comment activity, throttled by the page and the
   server) into the SAME hardened activity dir::

       <run_base>/activity/dsd-review--<dsd_id>.json   (0600, atomic)
       {"schema": 1, "kind": "dsd-review", "dsd_id": "dsd-…",
        "owner_instance": "beta-analyst", "last_ts": 1790..}

   Never any answer content. The session reader skips these files; a fresh one
   (within ``active_window``) composes "<operator> is actively reviewing YOUR DSD …" for
   the owner and "<operator> is reviewing a DSD from <owner> …" for everyone else. Typing in
   a session still wins; review activity beats "appears away".

6. **App activity** (flag ``app_activity_presence``, default OFF). The web job's Apps host asks each
   running app that declares an ``activity_path`` when the operator last used it and writes one beacon per app into the
   SAME hardened activity dir::

       <run_base>/activity/app-activity--<app id>.json   (0600, atomic)
       {"schema": 1, "kind": "app-activity", "app_id": "...", "label": "<manifest display name>", "last_ts": 1790..}

   Never any content from the app. The session reader skips these files; the app reader ignores a beacon older than
   ``active_window`` (a beacon left behind by a stopped or crashed web job never speaks). A fresh one composes
   "<operator> is active in <label> (<N>s ago). Do what you can without him." for EVERY session (the same text for
   all). Typing in a session still wins; app activity and DSD review beat "appears away", and between those two the
   more recent is spoken. INFORMATION ONLY: it adds no state other code may key on (spec I1); a snooze is not cleared.

4. **Presence-sensor seam** (:class:`PresenceSource`): an optional file or
   loopback-HTTP endpoint delivering ``{"present": bool, "ts": epoch}`` (plus an
   optional ``"since"``). Stale (> 30 s) readings are ignored. ``present`` with
   nobody typing -> "at his desk but not typing (thinking?)"; absent for >= 60 s
   OVERRIDES the typing inference. No hardware code lives here.

stdlib only.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import socket
import stat
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..hub.rendezvous import _secure_dir, is_loopback_http_url, run_base_dir, safe_read_bytes
from .line_hold import LINE_STATES, LINE_UNKNOWN

SCHEMA = 1
ACTIVITY_DIRNAME = "activity"

#: "Active in session X" = a keystroke or submit within this many seconds.
DEFAULT_ACTIVE_WINDOW = 120.0
#: At most one beacon write per this many seconds while typing.
DEFAULT_BEACON_THROTTLE = 2.0
#: A beacon not updated for this long is ignored even if its pid is alive.
BEACON_MAX_AGE = 24 * 3600.0
#: A presence reading older than this is ignored.
PRESENCE_STALE_SECONDS = 30.0
#: Sensor-absent for at least this long overrides the typing inference.
PRESENCE_ABSENT_OVERRIDE_SECONDS = 60.0
#: A beacon timestamp more than this far in the FUTURE is invalid (clock skew /
#: forgery): the whole beacon is ignored.
FUTURE_TOLERANCE_SECONDS = 5.0
#: Idle-backoff ceiling (the stall interval never backs off past this).
DEFAULT_IDLE_BACKOFF_CAP = 3600.0

_MAX_BEACON_BYTES = 4096
_MAX_BEACONS = 256
_MAX_PRESENCE_BYTES = 4096

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

#: DSD review beacons (see module doc, item 5).
DSD_REVIEW_KIND = "dsd-review"
DSD_REVIEW_PREFIX = "dsd-review--"
#: The strict dsd_id grammar (the same as the answer-notice grammar).
DSD_REVIEW_ID_RE = re.compile(r"\Adsd-[0-9a-f]{6,32}\Z")
#: The writer deletes review beacons older than this (keeps the dir small; the
#: session reader caps at ``_MAX_BEACONS`` files).
DSD_REVIEW_MAX_AGE = 3600.0
_MAX_REVIEW_BEACONS = 64

#: App-activity beacons (see module doc, item 6). The id grammar is the Apps manifest's (appshost.manifest.ID_RE); it is
#: restated here because this every-session module must not import the Apps host.
APP_ACTIVITY_KIND = "app-activity"
APP_ACTIVITY_PREFIX = "app-activity--"
APP_ACTIVITY_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{1,40}\Z")
#: The writer's prune deletes app-activity beacons older than this (spec B4a); the READER ignores one older than the
#: composer's active window, so a stale beacon never speaks.
APP_ACTIVITY_MAX_AGE = 3600.0
_MAX_APP_BEACONS = 64
_MAX_APP_LABEL = 80


# -- names & paths ------------------------------------------------------------------


def display_name(instance_id: Optional[str]) -> str:
    """An instance id -> its short stall-text label from the deployment's role-label table
    (:mod:`gnomon.role_labels`; ``alpha-architect`` -> ``AA`` if the table says so); also a session-dated id
    (``alpha-architect-20260929-055439``) by longest known prefix; else the id itself (the neutral fallback)."""
    from .. import role_labels

    iid = (instance_id or "").strip()
    if not iid:
        return "another session"
    names = role_labels.stall_labels()
    if iid in names:
        return names[iid]
    for key in sorted(names, key=len, reverse=True):
        if iid.startswith(key + "-"):
            return names[key]
    return iid


def activity_dir(base: Optional[str] = None) -> str:
    """``<run_base>/activity`` — machine-local, per-uid, never synced."""
    return os.path.join(base if base is not None else run_base_dir(), ACTIVITY_DIRNAME)


def session_key(session_dir: str) -> str:
    """The beacon's file stem: ``<basename>-<sha256(realpath)[:12]>``.

    Fix pass (audit LOW): keyed by a hash of the FULL (real) session path, so a
    ``<other-root>/sessions/X`` and a ``~/gnomon-data/sessions/X`` never collide; the
    basename prefix keeps the file human-readable (``s`` when it is not a plain
    safe name)."""
    raw = session_dir or ""
    try:
        full = os.path.realpath(os.path.expanduser(raw)) if raw else ""
    except (OSError, ValueError):
        full = raw
    digest = hashlib.sha256(full.encode("utf-8", "surrogatepass")).hexdigest()[:12]
    base = os.path.basename(os.path.normpath(raw)) if raw else ""
    if not (_SAFE_NAME.match(base) and not base.startswith(".")) or len(base) > 100:
        base = "s"
    return f"{base}-{digest}"


def beacon_path(session_dir: str, directory: Optional[str] = None) -> str:
    return os.path.join(directory if directory is not None else activity_dir(),
                        session_key(session_dir) + ".json")


def pid_alive(pid: Any) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours to signal
    except OSError:
        return False
    return True


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    try:
        f = float(v)
    except OverflowError:       # an int too large for a float is "not a number" (X72 N3)
        return None
    return f if f == f and f not in (float("inf"), float("-inf")) else None


# -- the writer ---------------------------------------------------------------------


def _atomic_write(path: str, record: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    parent = os.path.dirname(directory)
    if parent == run_base_dir():
        _secure_dir(parent)
    _secure_dir(directory)
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class ActivityBeacon:
    """This harness's operator-activity beacon (one per session).

    ``note_keystroke`` / ``note_submit`` are called from the operator tap;
    ``tick`` from the proxy's periodic work (flushes a throttled write);
    ``close`` on clean exit. Every method is best-effort: a failed write is
    recorded in ``last_error`` and NEVER raised into the pump."""

    def __init__(
        self,
        instance_id: str,
        session_dir: str,
        *,
        line_state: Optional[Callable[[], str]] = None,
        pid: Optional[int] = None,
        clock: Callable[[], float] = time.time,
        throttle: float = DEFAULT_BEACON_THROTTLE,
        directory: Optional[str] = None,
    ) -> None:
        self.instance_id = str(instance_id or "")
        self.session_dir = str(session_dir or "")
        self.key = session_key(self.session_dir)
        self._line_state = line_state
        self.pid = int(pid if pid is not None else os.getpid())
        self._clock = clock
        self._throttle = max(0.0, float(throttle))
        self._directory = directory
        self.last_keystroke_ts: Optional[float] = None
        self.last_submit_ts: Optional[float] = None
        self._last_write: Optional[float] = None
        self._dirty = False
        self.writes = 0
        self.last_error: Optional[str] = None
        self.closed = False

    @property
    def path(self) -> str:
        return beacon_path(self.session_dir, self._directory)

    def _state(self) -> str:
        if self._line_state is None:
            return LINE_UNKNOWN
        try:
            st = self._line_state()
        except Exception:  # noqa: BLE001
            return LINE_UNKNOWN
        return st if st in LINE_STATES else LINE_UNKNOWN

    def record(self, now: Optional[float] = None) -> Dict[str, Any]:
        now = self._clock() if now is None else now
        return {
            "schema": SCHEMA, "instance_id": self.instance_id, "session_dir": self.session_dir,
            "pid": self.pid, "last_keystroke_ts": self.last_keystroke_ts,
            "last_submit_ts": self.last_submit_ts, "line_state": self._state(),
            "updated_ts": now,
        }

    def write(self, now: Optional[float] = None) -> bool:
        if self.closed:
            return False
        now = self._clock() if now is None else now
        try:
            _atomic_write(self.path, self.record(now))
        except OSError as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self._last_write = now
        self._dirty = False
        self.writes += 1
        self.last_error = None
        return True

    def start(self, now: Optional[float] = None) -> bool:
        """Publish the session (no keystroke yet) so peers see it is live."""
        return self.write(now)

    def note_keystroke(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        self.last_keystroke_ts = now
        if self._last_write is None or now - self._last_write >= self._throttle:
            self.write(now)
        else:
            self._dirty = True

    def note_submit(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        self.last_submit_ts = now
        self.last_keystroke_ts = now
        self.write(now)   # immediate — a submit is the strongest signal

    def tick(self, now: Optional[float] = None) -> None:
        if not self._dirty:
            return
        now = self._clock() if now is None else now
        if self._last_write is None or now - self._last_write >= self._throttle:
            self.write(now)

    def close(self) -> bool:
        """Remove the beacon iff it still names our pid. Returns whether removed."""
        if self.closed:
            return False
        self.closed = True
        path = self.path
        try:
            raw = safe_read_bytes(path, _MAX_BEACON_BYTES)
            obj = json.loads(raw.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return False
        if not isinstance(obj, dict) or obj.get("pid") != self.pid:
            return False
        try:
            os.unlink(path)
            return True
        except OSError:
            return False


# -- the reader ---------------------------------------------------------------------


def _check_dir(path: str) -> Optional[str]:
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"unreadable: {exc}"
    if not stat.S_ISDIR(st.st_mode):
        return "refused: activity dir is not a directory"
    if st.st_uid != os.getuid():
        return f"refused: activity dir owner uid {st.st_uid} is not {os.getuid()}"
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return "refused: activity dir is group/other writable"
    return None


def _parse_beacon(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    pid = obj.get("pid")
    updated = _num(obj.get("updated_ts"))
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or updated is None:
        return None
    iid = obj.get("instance_id")
    sdir = obj.get("session_dir")
    ls = obj.get("line_state")
    return {
        "instance_id": iid if isinstance(iid, str) and len(iid) <= 256 else "",
        "session_dir": sdir if isinstance(sdir, str) and len(sdir) <= 4096 else "",
        "pid": pid,
        "last_keystroke_ts": _num(obj.get("last_keystroke_ts")),
        "last_submit_ts": _num(obj.get("last_submit_ts")),
        "line_state": ls if ls in LINE_STATES else LINE_UNKNOWN,
        "updated_ts": updated,
    }


def read_beacons(now: Optional[float] = None, directory: Optional[str] = None
                 ) -> Tuple[Optional[Dict[str, Dict[str, Any]]], Dict[str, Any]]:
    """All LIVE beacons keyed by session key, plus a diagnostics dict.

    Returns ``(None, {"error": reason})`` when the directory itself cannot be
    trusted/read. Individual refused/dead/stale beacons are skipped and counted
    in ``diag`` (``refused`` / ``dead`` / ``stale`` / ``corrupt``)."""
    now = time.time() if now is None else now
    directory = directory if directory is not None else activity_dir()
    diag: Dict[str, Any] = {"refused": [], "dead": 0, "stale": 0, "corrupt": 0, "future": 0}
    err = _check_dir(directory)
    if err is not None:
        diag["error"] = err
        return None, diag
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        diag["error"] = f"unreadable: {exc}"
        return None, diag
    out: Dict[str, Dict[str, Any]] = {}
    names = [n for n in names if not n.startswith((DSD_REVIEW_PREFIX, APP_ACTIVITY_PREFIX))]   # not sessions
    for name in [n for n in names if n.endswith(".json") and not n.startswith(".")][:_MAX_BEACONS]:
        path = os.path.join(directory, name)
        try:
            raw = safe_read_bytes(path, _MAX_BEACON_BYTES)
        except FileNotFoundError:
            continue
        except OSError as exc:
            msg = str(exc)
            if exc.errno == errno.ELOOP or "symlink" in msg:
                msg = "refused: is a symlink"
            diag["refused"].append({"file": name, "reason": msg})
            continue
        rec = _parse_beacon(raw)
        if rec is None:
            diag["corrupt"] += 1
            continue
        if any(ts is not None and ts > now + FUTURE_TOLERANCE_SECONDS
               for ts in (rec["updated_ts"], rec["last_keystroke_ts"], rec["last_submit_ts"])):
            diag["future"] += 1
            continue
        if now - rec["updated_ts"] > BEACON_MAX_AGE:
            diag["stale"] += 1
            continue
        if not pid_alive(rec["pid"]):
            diag["dead"] += 1
            continue
        out[name[:-len(".json")]] = rec
    return out, diag


def _since(now: float, ts: Optional[float]) -> Optional[float]:
    return None if ts is None else max(0.0, now - ts)


def operator_activity(now: Optional[float] = None, directory: Optional[str] = None
                      ) -> Optional[Dict[str, Dict[str, Any]]]:
    """Per live session: ``{instance_id, session_dir, seconds_since_keystroke,
    seconds_since_submit, seconds_since_activity, line_state}``. ``None`` when
    the beacons cannot be read at all."""
    now = time.time() if now is None else now
    beacons, _diag = read_beacons(now, directory)
    if beacons is None:
        return None
    out: Dict[str, Dict[str, Any]] = {}
    for key, b in beacons.items():
        ks = _since(now, b["last_keystroke_ts"])
        sb = _since(now, b["last_submit_ts"])
        vals = [v for v in (ks, sb) if v is not None]
        out[key] = {
            "instance_id": b["instance_id"], "session_dir": b["session_dir"],
            "seconds_since_keystroke": ks, "seconds_since_submit": sb,
            "seconds_since_activity": min(vals) if vals else None,
            "line_state": b["line_state"],
        }
    return out


def is_active(entry: Optional[Dict[str, Any]], active_window: float) -> bool:
    s = None if entry is None else entry.get("seconds_since_activity")
    return s is not None and s <= active_window


# -- DSD review beacons (written by the web server, read by every harness) ----------


def _valid_dsd_id(dsd_id: Any) -> bool:
    return isinstance(dsd_id, str) and DSD_REVIEW_ID_RE.match(dsd_id) is not None


def dsd_review_beacon_path(dsd_id: str, directory: Optional[str] = None) -> str:
    if not _valid_dsd_id(dsd_id):
        raise ValueError(f"invalid dsd_id {dsd_id!r} (must match {DSD_REVIEW_ID_RE.pattern})")
    return os.path.join(directory if directory is not None else activity_dir(),
                        DSD_REVIEW_PREFIX + dsd_id + ".json")


def _parse_review(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("kind") != DSD_REVIEW_KIND:
        return None
    dsd_id, owner, ts = obj.get("dsd_id"), obj.get("owner_instance"), _num(obj.get("last_ts"))
    if not _valid_dsd_id(dsd_id) or ts is None:
        return None
    owner = owner if isinstance(owner, str) and len(owner) <= 256 else ""
    return {"dsd_id": dsd_id, "owner_instance": owner, "last_ts": ts}


def prune_dsd_review_beacons(now: Optional[float] = None, directory: Optional[str] = None,
                             max_age: float = DSD_REVIEW_MAX_AGE) -> int:
    """Delete review beacons whose ``last_ts`` is older than ``max_age`` (or that
    are unparseable). Best-effort; returns how many were removed."""
    now = time.time() if now is None else now
    directory = directory if directory is not None else activity_dir()
    if _check_dir(directory) is not None:
        return 0
    removed = 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    for name in names:
        if not (name.startswith(DSD_REVIEW_PREFIX) and name.endswith(".json")):
            continue
        path = os.path.join(directory, name)
        try:
            rec = _parse_review(safe_read_bytes(path, _MAX_BEACON_BYTES))
        except OSError:
            continue   # refused (symlink / foreign) — never follow it, never delete via it
        if rec is None or now - rec["last_ts"] > max_age:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    return removed


def write_dsd_review_beacon(dsd_id: str, owner_instance: str, *, now: Optional[float] = None,
                            directory: Optional[str] = None) -> str:
    """Write (atomically, 0600, in the 0700 activity dir) the review beacon for
    ``dsd_id``. Holds ONLY ``{schema, kind, dsd_id, owner_instance, last_ts}`` —
    never answer content. Raises ``ValueError`` for a bad dsd_id and ``OSError``
    for a refused/failed write; prunes hour-old review beacons first."""
    path = dsd_review_beacon_path(dsd_id, directory)
    now = time.time() if now is None else now
    try:
        prune_dsd_review_beacons(now, os.path.dirname(path))
    except Exception:  # noqa: BLE001 - pruning is housekeeping, never fatal
        pass
    owner = str(owner_instance or "")[:256]
    _atomic_write(path, {"schema": SCHEMA, "kind": DSD_REVIEW_KIND, "dsd_id": dsd_id,
                         "owner_instance": owner, "last_ts": now})
    return path


def remove_dsd_review_beacon(dsd_id: str, directory: Optional[str] = None) -> bool:
    """Remove the review beacon for ``dsd_id`` (e.g. once it is answered)."""
    try:
        path = dsd_review_beacon_path(dsd_id, directory)
        st = os.lstat(path)
    except (ValueError, OSError):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def read_dsd_reviews(now: Optional[float] = None, directory: Optional[str] = None
                     ) -> Optional[List[Dict[str, Any]]]:
    """Every readable review beacon as ``{dsd_id, owner_instance, last_ts,
    seconds_ago}``, most recent first. ``None`` when the dir cannot be trusted.
    Same hardening as :func:`read_beacons` (no-follow, owner/mode/size checked);
    future-dated (> ``FUTURE_TOLERANCE_SECONDS``) and > ``DSD_REVIEW_MAX_AGE``
    beacons are skipped. Freshness vs ``active_window`` is the caller's call."""
    now = time.time() if now is None else now
    directory = directory if directory is not None else activity_dir()
    if _check_dir(directory) is not None:
        return None
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    out: List[Dict[str, Any]] = []
    for name in [n for n in names if n.startswith(DSD_REVIEW_PREFIX)
                 and n.endswith(".json")][:_MAX_REVIEW_BEACONS]:
        try:
            rec = _parse_review(safe_read_bytes(os.path.join(directory, name), _MAX_BEACON_BYTES))
        except OSError:
            continue
        if rec is None or rec["last_ts"] > now + FUTURE_TOLERANCE_SECONDS:
            continue
        if now - rec["last_ts"] > DSD_REVIEW_MAX_AGE:
            continue
        rec["seconds_ago"] = max(0.0, now - rec["last_ts"])
        out.append(rec)
    out.sort(key=lambda r: r["seconds_ago"])
    return out


# -- app-activity beacons (written by the web job's Apps host, read by every harness)


def _valid_app_id(app_id: Any) -> bool:
    return isinstance(app_id, str) and APP_ACTIVITY_ID_RE.match(app_id) is not None


def clean_app_label(label: Any) -> str:
    """A manifest display name made safe to speak: printable characters only (no control characters, no newline),
    whitespace collapsed, at most ``_MAX_APP_LABEL`` characters. May be empty."""
    if not isinstance(label, str):
        return ""
    text = "".join(ch if ch.isprintable() else " " for ch in label)
    return " ".join(text.split())[:_MAX_APP_LABEL].strip()


def app_activity_beacon_path(app_id: str, directory: Optional[str] = None) -> str:
    if not _valid_app_id(app_id):
        raise ValueError(f"invalid app id {app_id!r} (must match {APP_ACTIVITY_ID_RE.pattern})")
    return os.path.join(directory if directory is not None else activity_dir(),
                        APP_ACTIVITY_PREFIX + app_id + ".json")


def _parse_app_activity(raw: bytes) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(obj, dict) or obj.get("kind") != APP_ACTIVITY_KIND:
        return None
    app_id, ts = obj.get("app_id"), _num(obj.get("last_ts"))
    label = clean_app_label(obj.get("label"))
    if not _valid_app_id(app_id) or ts is None or not label:
        return None
    return {"app_id": app_id, "label": label, "last_ts": ts}


def write_app_activity_beacon(app_id: str, label: str, last_ts: float, *, directory: Optional[str] = None) -> str:
    """Write (atomically, 0600, in the 0700 activity dir) the app-activity beacon for ``app_id``. Holds ONLY
    ``{schema, kind, app_id, label, last_ts}`` - ``last_ts`` is the APP's own last-input time. Raises ``ValueError`` for
    a bad app id / label / time and ``OSError`` for a refused or failed write."""
    path = app_activity_beacon_path(app_id, directory)
    text = clean_app_label(label)
    ts = _num(last_ts)
    if not text or ts is None:
        raise ValueError("an app-activity beacon needs a printable label and a numeric last_ts")
    _atomic_write(path, {"schema": SCHEMA, "kind": APP_ACTIVITY_KIND, "app_id": app_id, "label": text, "last_ts": ts})
    return path


def remove_app_activity_beacon(app_id: str, directory: Optional[str] = None) -> bool:
    """Remove the beacon for ``app_id`` (the app stopped, stopped answering, or is no longer eligible)."""
    try:
        path = app_activity_beacon_path(app_id, directory)
        st = os.lstat(path)
    except (ValueError, OSError):
        return False
    if not stat.S_ISREG(st.st_mode):
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def prune_app_activity_beacons(now: Optional[float] = None, directory: Optional[str] = None,
                               max_age: float = APP_ACTIVITY_MAX_AGE) -> int:
    """Delete app-activity beacons whose ``last_ts`` is older than ``max_age`` (or that are unparseable). Best-effort;
    returns how many were removed. Never follows a symlink."""
    now = time.time() if now is None else now
    directory = directory if directory is not None else activity_dir()
    if _check_dir(directory) is not None:
        return 0
    try:
        names = os.listdir(directory)
    except OSError:
        return 0
    removed = 0
    for name in names:
        if not (name.startswith(APP_ACTIVITY_PREFIX) and name.endswith(".json")):
            continue
        path = os.path.join(directory, name)
        try:
            rec = _parse_app_activity(safe_read_bytes(path, _MAX_BEACON_BYTES))
        except OSError:
            continue   # refused (symlink / foreign / FIFO): never followed, never deleted through
        if rec is None or now - rec["last_ts"] > max_age:
            try:
                os.unlink(path)
                removed += 1
            except OSError:
                pass
    return removed


def read_app_activities(now: Optional[float] = None, directory: Optional[str] = None,
                        max_age: float = DEFAULT_ACTIVE_WINDOW) -> Optional[List[Dict[str, Any]]]:
    """Every readable app-activity beacon as ``{app_id, label, last_ts, seconds_ago}``, most recent first. ``None`` when
    the dir cannot be trusted. The ONE reader of these files (checklist g103): same hardening as :func:`read_beacons`
    (no-follow, owner/mode/size checked). A beacon dated more than ``FUTURE_TOLERANCE_SECONDS`` ahead, or older than
    ``max_age`` (default: the 120 s active window; spec B4a), is skipped."""
    now = time.time() if now is None else now
    directory = directory if directory is not None else activity_dir()
    if _check_dir(directory) is not None:
        return None
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return None
    out: List[Dict[str, Any]] = []
    for name in [n for n in names if n.startswith(APP_ACTIVITY_PREFIX)
                 and n.endswith(".json")][:_MAX_APP_BEACONS]:
        try:
            rec = _parse_app_activity(safe_read_bytes(os.path.join(directory, name), _MAX_BEACON_BYTES))
        except OSError:
            continue
        if rec is None or rec["last_ts"] > now + FUTURE_TOLERANCE_SECONDS:
            continue
        if now - rec["last_ts"] > max_age:
            continue
        rec["seconds_ago"] = max(0.0, now - rec["last_ts"])
        out.append(rec)
    out.sort(key=lambda r: r["seconds_ago"])
    return out


# -- the presence-sensor seam -------------------------------------------------------


class PresenceSource:
    """An optional presence input (future mmWave sensor). ``source`` is a file
    path, a loopback ``http://127.0.0.1:<port>/...`` URL, or a callable returning
    the dict (tests). Delivers ``{"present": bool, "ts": epoch[, "since": epoch]}``.

    :meth:`read` returns ``{present, ts, absent_for}`` for a FRESH reading, else
    ``None`` (absent source, unreadable, malformed, or older than ``stale_after``).
    ``on_change`` fires on a fresh present<->absent flip.

    ⛔ Fix pass (audit MEDIUM): :meth:`read` runs ON THE PUMP (``tick`` /
    ``compose`` / the ``context`` verb) and must never block. An HTTP source is
    therefore fetched ONLY by a background daemon worker (started on first read,
    every ``poll_interval``) that caches the last parsed object; ``read`` only
    consults the cache. The worker speaks a minimal HTTP/1.0 GET over a raw
    socket under a WALL-CLOCK ``fetch_deadline`` for the whole exchange (a
    trickle server can no longer hold it), follows NO redirects (any non-200 is
    an error), and caps the response at ``_MAX_PRESENCE_BYTES``. A file source is
    read inline: ``safe_read_bytes`` is a non-blocking, no-follow, size-capped
    open of a regular local file (a FIFO/symlink is refused)."""

    def __init__(self, source: Any, *, stale_after: float = PRESENCE_STALE_SECONDS,
                 clock: Callable[[], float] = time.time,
                 on_change: Optional[Callable[[bool], None]] = None,
                 poll_interval: float = 2.0, fetch_deadline: float = 1.0) -> None:
        self._source = source
        self._stale_after = float(stale_after)
        self._clock = clock
        self._on_change = on_change
        self._poll_interval = max(0.01, float(poll_interval))
        self._fetch_deadline = max(0.05, float(fetch_deadline))
        self._absent_since: Optional[float] = None
        self._last_fresh: Optional[bool] = None
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._cache: Any = None
        self._worker: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._is_http = isinstance(source, str) and source.startswith("http://")

    def set_on_change(self, on_change: Optional[Callable[[bool], None]]) -> None:
        self._on_change = on_change

    # -- the HTTP worker (never on the pump) -----------------------------------

    @property
    def worker_alive(self) -> bool:
        return self._worker is not None and self._worker.is_alive()

    def _ensure_worker(self) -> None:
        if self._worker is None and not self._stop.is_set():
            self._worker = threading.Thread(target=self._run, name="gm-presence", daemon=True)
            self._worker.start()

    def close(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                obj = self._fetch_http(self._source)
                err = None
            except Exception as exc:  # noqa: BLE001 - a sensor fault is "no reading"
                obj, err = None, f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._cache = obj
                self.last_error = err
            self._stop.wait(self._poll_interval)

    def _fetch_http(self, url: str) -> Any:
        if not is_loopback_http_url(url):
            raise OSError("refused: presence endpoint is not a loopback http url")
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        deadline = time.monotonic() + self._fetch_deadline

        def _left() -> float:
            rem = deadline - time.monotonic()
            if rem <= 0:
                raise TimeoutError(f"presence fetch exceeded its {self._fetch_deadline:.2f}s deadline")
            return rem

        host = parts.hostname or "127.0.0.1"
        family = socket.AF_INET6 if ":" in host else socket.AF_INET
        with socket.socket(family, socket.SOCK_STREAM) as sock:
            sock.settimeout(_left())
            sock.connect((host, parts.port))
            sock.settimeout(_left())
            sock.sendall(f"GET {path} HTTP/1.0\r\nHost: {host}\r\nAccept: application/json\r\n"
                         f"Connection: close\r\n\r\n".encode("ascii"))
            buf = bytearray()
            cap = _MAX_PRESENCE_BYTES + 8192   # body cap + generous header room
            while True:
                sock.settimeout(_left())
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    raise TimeoutError(f"presence fetch exceeded its {self._fetch_deadline:.2f}s deadline") from None
                if not chunk:
                    break
                buf += chunk
                if len(buf) > cap:
                    raise OSError("refused: oversized presence response")
        head, sep, body = bytes(buf).partition(b"\r\n\r\n")
        if not sep:
            raise OSError("malformed presence response (no header terminator)")
        status_line = head.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        fields = status_line.split()
        if len(fields) < 2 or not fields[0].startswith("HTTP/") or fields[1] != "200":
            # ⛔ never follow a redirect (3xx) or accept any other status
            raise OSError(f"refused: presence endpoint status {' '.join(fields[1:2]) or '?'} "
                          f"(redirects are not followed)")
        if len(body) > _MAX_PRESENCE_BYTES:
            raise OSError("refused: oversized presence response")
        return json.loads(body.decode("utf-8"))

    # -- the pump side (never blocks) -------------------------------------------

    def _raw(self) -> Any:
        src = self._source
        if callable(src):
            return src()
        if not isinstance(src, str) or not src.strip():
            return None
        if self._is_http:
            if not is_loopback_http_url(src):   # a pure check, no I/O: refuse at once
                raise OSError("refused: presence endpoint is not a loopback http url")
            self._ensure_worker()
            with self._lock:
                return self._cache
        obj = json.loads(safe_read_bytes(os.path.expanduser(src), _MAX_PRESENCE_BYTES).decode("utf-8"))
        self.last_error = None
        return obj

    def read(self, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
        now = self._clock() if now is None else now
        try:
            obj = self._raw()
        except Exception as exc:  # noqa: BLE001 - a sensor fault is "no reading"
            self.last_error = f"{type(exc).__name__}: {exc}"
            obj = None
        present = obj.get("present") if isinstance(obj, dict) else None
        ts = _num(obj.get("ts")) if isinstance(obj, dict) else None
        if not isinstance(present, bool) or ts is None or abs(now - ts) > self._stale_after:
            self._absent_since = None   # lost track: the absence clock restarts
            return None
        if present:
            self._absent_since = None
        else:
            since = _num(obj.get("since"))
            if since is not None and since <= ts:
                self._absent_since = since
            elif self._absent_since is None:
                self._absent_since = ts
        if self._last_fresh is not None and self._last_fresh != present and self._on_change is not None:
            try:
                self._on_change(present)
            except Exception:  # noqa: BLE001
                pass
        self._last_fresh = present
        absent_for = None if present or self._absent_since is None else max(0.0, now - self._absent_since)
        return {"present": present, "ts": ts, "absent_for": absent_for}


# -- composition --------------------------------------------------------------------

_FRAMEWORK = ("using your decision framework (reversible, in your lane, value obvious → act, "
              "then flag it; queue only the decisions that are genuinely his)")
_DONE = "If you're genuinely done, reply 'idle' in one line."

#: g112: every notice that tells an agent what was or was not observed of the operator's typing names its instrument (console
#: keystrokes) and says what that instrument cannot see. The gloss is spelled out because an agent that has never met the term cannot tell what
#: "Remote Control" means. The open-work presence line (worklist/presence.py) carries the SHORT form of this clause, being bounded.
REMOTE_CONTROL_GLOSS = "messages sent through Claude Code's Remote Control, from another device, are not counted"

BRANCH_ELSEWHERE = "active-elsewhere"
BRANCH_AWAY = "away"
BRANCH_AT_DESK = "presence-at-desk"
BRANCH_SENSOR_ABSENT = "presence-absent"
BRANCH_FALLBACK = "fallback"
BRANCH_DSD_REVIEW_OWNER = "dsd-review-owner"
BRANCH_DSD_REVIEW_OTHER = "dsd-review-other"
BRANCH_APP_ACTIVITY = "app-activity"


def text_active_elsewhere(user: str, instance_id: str, seconds: float) -> str:
    return (f"[Gnomon] {user} is active, talking to {display_name(instance_id)} "
            f"(last console keystroke {int(seconds)}s ago; {REMOTE_CONTROL_GLOSS}), not in this session. Don't wait on him: "
            f"move forward on anything you can, {_FRAMEWORK}. {_DONE}")


def text_dsd_review_owner(user: str, dsd_id: str, seconds: float) -> str:
    return (f"[Gnomon] {user} is actively reviewing your DSD {dsd_id} (last DSD-review activity in the dashboard "
            f"{int(seconds)}s ago). Proceed on anything not gated on that DSD or his approval; "
            f"queue what is. {_DONE}")


def text_dsd_review_other(user: str, owner_instance: str, seconds: float) -> str:
    return (f"[Gnomon] {user} is reviewing a DSD from {display_name(owner_instance)} "
            f"(last DSD-review activity in the dashboard {int(seconds)}s ago), not in this session. Don't wait on him: "
            f"move forward on anything you can, {_FRAMEWORK}. {_DONE}")


def text_app_activity(user: str, label: str, seconds: float) -> str:
    """(spec B3): the SAME text for every session. Information only (spec I1)."""
    return f"[Gnomon] {user} is active in {label} ({int(seconds)}s ago). Do what you can without him."


def text_away(user: str, seconds: float) -> str:
    return (f"[Gnomon] {user} appears away (no console typing in any session for "
            f"{int(seconds // 60)} min; {REMOTE_CONTROL_GLOSS}). "
            f"If {user} has messaged this session recently, {user} is present for it. Work autonomously on anything you can, using your "
            f"decision framework; route his decisions to a DSD or a note. {_DONE}")


def text_sensor_absent(user: str, seconds: float) -> str:
    return (f"[Gnomon] {user} is away from his desk (presence sensor: absent for "
            f"{int(seconds // 60)} min). Work autonomously on anything you can, using your "
            f"decision framework; route his decisions to a DSD or a note. {_DONE}")


def text_at_desk(user: str) -> str:
    return (f"[Gnomon] {user} is at his desk but not typing at a console (thinking?), in no session "
            f"({REMOTE_CONTROL_GLOSS}). "
            f"Don't wait on him: move forward on anything you can, {_FRAMEWORK}. {_DONE}")


class OperatorContext:
    """This harness's view of the operator across ALL harnessed sessions.

    Owns the local :class:`ActivityBeacon` (may be ``None`` for a stdin-less
    session: it still READS peers), the optional :class:`PresenceSource`, and the
    ``active_window``. ``compose`` builds the stall text at fire time;
    ``context_field`` answers the ``context`` self-verb."""

    def __init__(
        self,
        *,
        session_dir: str,
        user_name: str = "the operator",
        beacon: Optional[ActivityBeacon] = None,
        presence: Optional[PresenceSource] = None,
        active_window: float = DEFAULT_ACTIVE_WINDOW,
        directory: Optional[str] = None,
        clock: Callable[[], float] = time.time,
        presence_poll_interval: float = 5.0,
        instance_id: Optional[str] = None,
        dsd_review: bool = False,
        app_activity: bool = False,
    ) -> None:
        self.key = session_key(session_dir) if session_dir else None
        # fix pass (audit LOW): a peer beacon carrying THIS session's instance_id
        # (the same agent in another dir) is never "elsewhere".
        self.instance_id = instance_id if instance_id is not None else (
            beacon.instance_id if beacon is not None else None)
        self.user = (user_name or "").strip() or "the operator"
        self.beacon = beacon
        self.presence = presence
        self.active_window = float(active_window)
        self._directory = directory
        self._clock = clock
        self._presence_poll = float(presence_poll_interval)
        self._last_presence_poll: Optional[float] = None
        self.last_branch: Optional[str] = None
        #: flag dsd_review_presence (resolved by the launcher). OFF =
        #: review beacons are never read: text and context field unchanged.
        self.dsd_review = bool(dsd_review)
        #: flag app_activity_presence (resolved by the launcher). OFF = app-activity beacons are never
        #: read: text, branch and context field are exactly as before.
        self.app_activity = bool(app_activity)

    # -- periodic --------------------------------------------------------------

    def tick(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        if self.beacon is not None:
            self.beacon.tick(now)
        if self.presence is not None and (self._last_presence_poll is None
                                          or now - self._last_presence_poll >= self._presence_poll):
            self._last_presence_poll = now
            self.presence.read(now)   # drives on_change (idle-backoff reset)

    def close(self) -> None:
        if self.beacon is not None:
            self.beacon.close()
        if self.presence is not None:
            self.presence.close()

    # -- views -----------------------------------------------------------------

    def _here(self, now: float) -> Optional[float]:
        if self.beacon is None:
            return None
        vals = [now - t for t in (self.beacon.last_keystroke_ts, self.beacon.last_submit_ts) if t is not None]
        return max(0.0, min(vals)) if vals else None

    def typed_here_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """seconds since the operator last typed (a key or a submit) in THIS session's console, from this
        harness's own in-memory beacon; ``None`` when he has not since this harness started (or it has no operator stdin).
        No file is read."""
        return self._here(self._clock() if now is None else now)

    def snapshot(self, now: Optional[float] = None) -> Dict[str, Any]:
        """``{readable, here, others: [(instance_id, secs)...], any, presence}``."""
        now = self._clock() if now is None else now
        acts = operator_activity(now, self._directory)
        here = self._here(now)
        others: List[Tuple[str, float]] = []
        if acts is not None:
            for key, a in acts.items():
                if key == self.key:
                    if here is None:
                        here = a["seconds_since_activity"]
                    continue
                if self.instance_id and a["instance_id"] == self.instance_id:
                    continue
                if a["seconds_since_activity"] is not None:
                    others.append((a["instance_id"] or key, a["seconds_since_activity"]))
        others.sort(key=lambda p: p[1])
        known = [s for s in [here] + [s for _i, s in others] if s is not None]
        presence = self.presence.read(now) if self.presence is not None else None
        snap = {"readable": acts is not None and (len(acts) > 0 or self.beacon is not None),
                "here": here, "others": others, "any": min(known) if known else None,
                "presence": presence}
        if self.dsd_review:
            fresh = self._fresh_reviews(now)         # ONE read of the review beacons: the picks below cannot disagree (X72 N5)
            snap["review"] = self._pick_review(fresh)
            if self.app_activity:
                # B3: the app is ranked against the MOST RECENT fresh DSD review (which may be another instance's), while
                # the review LINE that is spoken stays the one _pick_review picks (this instance's own review first), as before.
                snap["review_latest_ago"] = min((r["seconds_ago"] for r in fresh), default=None)
        if self.app_activity:
            snap["app"] = self._fresh_app(now)
        return snap

    def _fresh_app(self, now: float) -> Optional[Dict[str, Any]]:
        """The most recent app-activity beacon fresh within ``active_window`` (spec B3/B4a), or ``None`` (none, stale
        or unreadable)."""
        try:
            apps = read_app_activities(now, self._directory, self.active_window)
        except Exception:  # noqa: BLE001 - a reader fault is "no app activity"
            return None
        return (apps or [None])[0]

    def _fresh_reviews(self, now: float) -> List[Dict[str, Any]]:
        """Review beacons fresh within ``active_window`` (most recent first); ``[]`` if none / unreadable."""
        try:
            reviews = read_dsd_reviews(now, self._directory)
        except Exception:  # noqa: BLE001 - a reader fault is "no review"
            return []
        return [r for r in (reviews or []) if r["seconds_ago"] <= self.active_window]

    def _pick_review(self, fresh: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The DSD review to report: one OWNED by this instance if any (the owner needs to know it is theirs), else the
        most recent. ``None`` if there is none."""
        if not fresh:
            return None
        mine = [r for r in fresh if self.instance_id and r["owner_instance"] == self.instance_id]
        return (mine or fresh)[0]

    def _fresh_review(self, now: float) -> Optional[Dict[str, Any]]:
        return self._pick_review(self._fresh_reviews(now))

    def compose(self, fallback: str, local_idle: Optional[float] = None,
                now: Optional[float] = None) -> Tuple[str, str]:
        """``(text, branch)`` for a stall nudge firing NOW."""
        snap = self.snapshot(now)
        pres = snap["presence"]
        w = self.active_window
        if pres is not None and pres["present"] is False and (pres["absent_for"] or 0.0) >= PRESENCE_ABSENT_OVERRIDE_SECONDS:
            return self._done(text_sensor_absent(self.user, pres["absent_for"]), BRANCH_SENSOR_ABSENT)
        review = snap.get("review")
        app = snap.get("app")
        if not snap["readable"] and review is None and app is None:
            return self._done(fallback, BRANCH_FALLBACK)
        active_others = [(i, s) for i, s in snap["others"] if s <= w]
        if active_others:
            iid, secs = active_others[0]
            return self._done(text_active_elsewhere(self.user, iid, secs), BRANCH_ELSEWHERE)
        if snap["here"] is not None and snap["here"] <= w:
            return self._done(fallback, BRANCH_FALLBACK)
        # App activity: below typing, above at-desk / away. Between a DSD review and an app, the more recent
        # is spoken (a tie goes to the review, as before).
        if app is not None and (review is None or app["seconds_ago"] < (snap.get("review_latest_ago") if snap.get("review_latest_ago") is not None else review["seconds_ago"])):
            return self._done(text_app_activity(self.user, app["label"], app["seconds_ago"]), BRANCH_APP_ACTIVITY)
        # DSD review activity: below typing, above at-desk / away.
        if review is not None:
            if self.instance_id and review["owner_instance"] == self.instance_id:
                return self._done(text_dsd_review_owner(self.user, review["dsd_id"],
                                                        review["seconds_ago"]),
                                  BRANCH_DSD_REVIEW_OWNER)
            return self._done(text_dsd_review_other(self.user, review["owner_instance"],
                                                    review["seconds_ago"]),
                              BRANCH_DSD_REVIEW_OTHER)
        if not snap["readable"]:
            return self._done(fallback, BRANCH_FALLBACK)
        if pres is not None and pres["present"] is True:
            return self._done(text_at_desk(self.user), BRANCH_AT_DESK)
        idle = snap["any"]
        if idle is None:
            idle = local_idle
        if idle is None or idle <= w:
            return self._done(fallback, BRANCH_FALLBACK)
        return self._done(text_away(self.user, idle), BRANCH_AWAY)

    def _done(self, text: str, branch: str) -> Tuple[str, str]:
        self.last_branch = branch
        return text, branch

    def context_field(self, now: Optional[float] = None) -> Dict[str, Any]:
        """The ``operator`` field of the ``context`` self-verb."""
        snap = self.snapshot(now)
        w = self.active_window
        active_others = [(i, s) for i, s in snap["others"] if s <= w]
        out: Dict[str, Any] = {
            "active_here": snap["here"] is not None and snap["here"] <= w,
            "active_elsewhere": active_others[0][0] if active_others else None,
            "seconds_since_any_activity": None if snap["any"] is None else int(snap["any"]),
            "active_window_seconds": w,
            "beacons_readable": bool(snap["readable"]),
        }
        if self.dsd_review:
            r = snap.get("review")
            out["reviewing_dsd"] = (None if r is None else
                                    {"dsd_id": r["dsd_id"], "owner_instance": r["owner_instance"],
                                     "seconds_ago": int(r["seconds_ago"])})
        pres = snap["presence"]
        if self.presence is not None:
            out["presence"] = (None if pres is None else
                               {"present": pres["present"],
                                "absent_for_seconds": None if pres["absent_for"] is None else int(pres["absent_for"])})
        return out


    def latest_operator_activity(self, now: Optional[float] = None) -> Tuple[bool, Optional[float]]:
        """``(readable, ts)`` - the newest operator keystroke/submit timestamp across
        EVERY live harnessed session's beacon (this one's, same-instance peers' and the others';
        this session's in-memory beacon too), or ``None`` if there is none. ``readable`` is False
        when the beacon directory cannot be trusted/read (the caller then cannot see the operator
        return). The same beacons the ``context`` verb's ``operator`` field reads."""
        now = self._clock() if now is None else now
        vals: List[float] = []
        if self.beacon is not None:
            vals += [t for t in (self.beacon.last_keystroke_ts, self.beacon.last_submit_ts) if t is not None]
        beacons, _diag = read_beacons(now, self._directory)
        if beacons is None:
            return False, (max(vals) if vals else None)
        for b in beacons.values():
            vals += [t for t in (b["last_keystroke_ts"], b["last_submit_ts"]) if t is not None]
        return True, (max(vals) if vals else None)


# -- idle-reply detection (for the idle backoff) --------------------------------------

#: ``idle`` followed by end-of-text or a separator (":", ".", "—", ...), so
#: "Idle connections are leaking" is NOT an idle declaration. Used with
#: ``.match`` (anchored at 0) — deliberately no ``^`` so a ``.search`` mutation
#: is observable ("Status: idle").
_IDLE_RE = re.compile(r"idle\b(?=\s*(?:$|[:.;!—–\-(]))", re.IGNORECASE)
_NOT_IDLE_WORDS = re.compile(r"\b(?:blocked|waiting|stuck|cannot|approval)\b", re.IGNORECASE)
IDLE_REPLY_MAX_CHARS = 80
_TAIL_BYTES = 512 * 1024


def _parse_iso(ts: Any) -> Optional[float]:
    if not isinstance(ts, str) or not ts:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def is_idle_reply(text: Any) -> bool:
    """The agent's reply is a bare 'idle' declaration (fix pass, audit MEDIUM):
    the WHOLE stripped reply is ONE line of <= 80 chars that starts with the word
    ``idle`` (then end / a separator), and names none of blocked / waiting /
    stuck / cannot / approval. "idle – blocked on operator approval" and "Idle
    connections are leaking…" are NOT idle. Stricter => fewer backoffs: a
    borderline reply keeps the normal nudge cadence."""
    if not isinstance(text, str):
        return False
    s = text.strip()
    if not s or "\n" in s or "\r" in s or len(s) > IDLE_REPLY_MAX_CHARS:
        return False
    if _NOT_IDLE_WORDS.search(s):
        return False
    return bool(_IDLE_RE.match(s))


def last_assistant_text_since(transcript_path: str, since_epoch: float) -> Optional[str]:
    """The LAST assistant text written to the Claude Code transcript at/after
    ``since_epoch`` (tail-bounded read), or ``None``."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read()
    except OSError:
        return None
    last: Optional[str] = None
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        ts = _parse_iso(row.get("timestamp"))
        if ts is None or ts < since_epoch:
            continue
        content = (row.get("message") or {}).get("content") if isinstance(row.get("message"), dict) else None
        if isinstance(content, list):
            texts = [c.get("text") for c in content if isinstance(c, dict) and c.get("type") == "text"
                     and isinstance(c.get("text"), str) and c.get("text").strip()]
            if texts:
                last = texts[-1]
        elif isinstance(content, str) and content.strip():
            last = content
    return last


def resolve_transcript_path(session_dir: str) -> Optional[str]:
    """The session's LIVE Claude Code transcript: the statusline hook's pointer
    (``context_state.json`` ``transcript_path``), else the ``.claude/transcript.jsonl``
    link, else ``None``. (Factored out of :func:`transcript_idle_reply_reader`
 for's agent-turn reader — same resolution, one implementation.)"""
    state = os.path.join(session_dir, ".claude", "context_state.json")
    try:
        obj = json.loads(safe_read_bytes(state, 64 * 1024).decode("utf-8"))
        p = obj.get("transcript_path") if isinstance(obj, dict) else None
        if isinstance(p, str) and os.path.isfile(p):
            return p
    except (OSError, ValueError, UnicodeDecodeError):
        pass
    link = os.path.join(session_dir, ".claude", "transcript.jsonl")
    return link if os.path.isfile(link) else None


def transcript_idle_reply_reader(session_dir: str) -> Callable[[float], Optional[bool]]:
    """``since_epoch -> True (the last reply since then declared idle) / False (a
    non-idle reply) / None (no reply seen / no transcript)``. Transcript located
    via the statusline hook's LIVE pointer (``context_state.json``
    ``transcript_path``), else the ``.claude/transcript.jsonl`` link."""

    def _path() -> Optional[str]:
        return resolve_transcript_path(session_dir)

    def _read(since_epoch: float) -> Optional[bool]:
        p = _path()
        if p is None:
            return None
        text = last_assistant_text_since(p, since_epoch)
        if text is None:
            return None
        return is_idle_reply(text)

    return _read

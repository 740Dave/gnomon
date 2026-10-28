"""(core) -- the ONE Gnomon alert service.

Every producer (the harness's stuck-hold watchdog, the web server's DSD-arrival poll, an agent's ``alert``
self-verb, later the Scheduler and Apps) raises an alert through :func:`notify`. The alerts are shown ONLY on
UI surfaces: the web page's stacked alert column and a macOS notification. They are NEVER typed into an agent's
PTY (g79 governs everything typed there; this module has no path to a PTY and imports nothing
from the harness).

Storage (the DATA root, ``<runtime_root>/alerts/``; ``$GNOMON_ALERTS_DIR`` overrides, for tests):

* ``alerts.jsonl``    -- append-only, one JSON record per ACCEPTED alert (id, ts, source, key, severity, title,
  body, link, macos).
* ``dismissed.jsonl`` -- append-only, one ``{"ts", "ids": [...]}`` line per dismissal (a dismiss-all lists the ids
  it dismissed). The dismissed SET is the union of those ids.
* ``alerts.lock``     -- an ``flock`` file. Several processes write (every harness, the web server); every
  read-decide-append runs under it, so de-duplication and the rate limit hold ACROSS processes, not per process.

Rules, all enforced here (a producer cannot opt out):

* **plain text only**: control characters, ANSI escapes and bidi overrides are stripped; length caps; the UI
  renders with ``textContent`` (never ``innerHTML``). ``<`` and ``&`` are kept as typed (they are only ever shown
  as text).
* **de-duplication**: one alert per ``(source, key)`` per ``DEDUPE_WINDOW_S`` (``key`` defaults to a hash of the
  title), whether or not the first was dismissed.
* **rate limit per source**: at most ``RATE_MAX`` accepted alerts per source per ``RATE_WINDOW_S``. The first
  alert dropped in a window is replaced by ONE rate-limit notice (so a flood is visible, never silent).
* **macOS notification** for severity >= warning, budgeted per class of source (agent vs system, see MACOS_MAX).
  The title and body reach ``/usr/bin/osascript`` as ARGV after ``--`` (read by an ``on run argv`` handler); they
  are never interpolated into AppleScript source.
* :func:`notify` NEVER raises into a caller: any failure is returned as ``{"ok": False, ...}``.

Pure stdlib; no import from the rest of the package except ``config`` (a constant) inside a function.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import stat
import subprocess
import sys
import threading
import time
import unicodedata
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

FLAG_NAME = "alerts"
ENV_DIR = "GNOMON_ALERTS_DIR"
SUBDIR = "alerts"
ALERTS_FILE = "alerts.jsonl"
DISMISSED_FILE = "dismissed.jsonl"
LOCK_FILE = "alerts.lock"

SEVERITIES = ("info", "warning", "error")
_SEV_RANK = {s: i for i, s in enumerate(SEVERITIES)}
#: severities that also raise a macOS notification
MACOS_MIN_SEVERITY = "warning"

TITLE_CAP = 120
BODY_CAP = 600
SOURCE_CAP = 80
KEY_CAP = 120
#: the macOS notification text is shorter still (Notification Center truncates anyway)
MACOS_TITLE_CAP = 80
MACOS_BODY_CAP = 240

DEDUPE_WINDOW_S = 600.0
RATE_MAX = 10
RATE_WINDOW_S = 600.0
#: the agent verb's own tighter limit (per calling agent = per source)
AGENT_RATE_MAX = 5
#: macOS notification budget (fix round F5), per CLASS of source, per MACOS_WINDOW_S:
#:   * class ``agent`` (a source starting ``agent:``, i.e. anything an agent raised through the verb) gets
#:     MACOS_MAX in total AND at most MACOS_AGENT_PER_SOURCE per agent, so one agent cannot use the class up;
#:   * class ``system`` (every other source: ``harness:*``, ``dsd``, ``gnomon-alerts``, later Scheduler/Apps)
#:     has its OWN MACOS_MAX that agents can never spend.
#: A budget miss never drops the alert: it is still in the column (macos: "rate-limited").
MACOS_MAX = 5
MACOS_AGENT_PER_SOURCE = 2
MACOS_WINDOW_S = 300.0
#: how long a store operation waits for the directory lock / the in-process lock before giving up (F4)
LOCK_WAIT_S = 3.0
#: the bounded hand-off for harness producers (F4): the pump thread only ever does a put_nowait
PRODUCER_QUEUE_MAX = 32
FAILURES_FILE = "producer-failures.jsonl"
#: the failures file rotates to ``.1`` at this size (so it holds the most recent records, never freezes); the
#: banner counts only records inside FAILURES_WINDOW_S (24 h), read from both files.
FAILURES_FILE_MAX = 4 * 1024 * 1024
FAILURES_WINDOW_S = 24 * 3600.0
FAILURE_ERROR_CAP = 160
FAILURES_LOCK_FILE = "producer-failures.lock"
FAILURES_LOCK_WAIT_S = 1.0
#: drops are written by a flusher thread (never the pump), batched at most once per this many seconds
DROP_FLUSH_S = 1.0
#: an undismissed alert stops being "active" after this long (the file is the durable record)
ACTIVE_MAX_AGE_S = 7 * 24 * 3600.0
ACTIVE_LIMIT = 200
#: at this size the log is rotated to ``alerts.jsonl.1`` (and the dismissed file with it)
ROTATE_BYTES = 4 * 1024 * 1024
OSASCRIPT = "/usr/bin/osascript"
OSASCRIPT_TIMEOUT_S = 5.0

RATE_NOTICE_SOURCE = "gnomon-alerts"

#: a link is a dashboard TAB id (for example ``dsd``) and nothing else: no URL, no path, no scheme
LINK_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
_SOURCE_BAD_RE = re.compile(r"[^A-Za-z0-9._:@/-]")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
#: bidi controls (Trojan-Source style display spoofing) and the BOM / line separators
_STRIP_CHARS = frozenset("‪‫‬‭‮⁦⁧⁨⁩‎‏؜﻿")


# --- text ------------------------------------------------------------------------------------------------------


def clean_text(value: Any, cap: int, *, multiline: bool = False) -> str:
    """Plain text: ANSI escapes and control characters removed (a tab becomes a space; newlines survive only when
    ``multiline``, otherwise they become spaces), bidi overrides removed, whitespace trimmed, capped at ``cap``
    characters (with a trailing ellipsis). Never raises; a non-string is ``str()``-ed, None is empty."""
    try:
        s = "" if value is None else (value if isinstance(value, str) else str(value))
        s = _ANSI_RE.sub("", s)
        out: List[str] = []
        for cp in s:
            if cp in _STRIP_CHARS:
                continue
            if cp == "\n" or cp == " " or cp == " " or cp == "\x85":
                out.append("\n" if multiline else " ")
            elif cp == "\t":
                out.append(" ")
            elif unicodedata.category(cp) in ("Cc", "Cf", "Cs"):
                continue   # controls, format chars (zero-width, soft hyphen, tag block: F7), lone surrogates (F8)
            else:
                out.append(cp)
        s = "".join(out).strip()
        if multiline:
            s = re.sub(r"\n{3,}", "\n\n", s)
        if len(s) > cap:
            s = s[: max(0, cap - 1)].rstrip() + "…"
        return s
    except Exception:  # noqa: BLE001
        return ""


def clean_source(value: Any) -> str:
    s = _SOURCE_BAD_RE.sub("_", clean_text(value, SOURCE_CAP))
    return s[:SOURCE_CAP] or "unknown"


def clean_link(value: Any) -> Optional[str]:
    """A dashboard tab id, or None. Anything else (a URL, a path, ``javascript:``) is dropped."""
    if isinstance(value, str) and LINK_RE.match(value):
        return value
    return None


# --- location + store -------------------------------------------------------------------------------------------


def default_dir(runtime_root: Optional[str] = None) -> str:
    """``$GNOMON_ALERTS_DIR``, else ``<DATA root>/alerts``. The DATA root is the config's ``runtime_root`` (its default
    is set in the machine config)."""
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if not runtime_root:
        from .config import DEFAULT_RUNTIME_ROOT

        runtime_root = DEFAULT_RUNTIME_ROOT
    return os.path.join(os.path.abspath(os.path.expanduser(runtime_root)), SUBDIR)


class AlertStoreError(Exception):
    """The store cannot be used (a non-regular file at a store path, a lock held too long, an I/O error). Always
    carries a plain-text reason; :func:`notify` turns it into ``status: error``."""


def _open_regular(path: str, flags: int, mode: int = 0o600) -> int:
    """Open a store file ONLY if it is a regular file, never in a blocking mode (F4): lstat first (a FIFO,
    device, socket, directory or symlink is refused with a clear error), then open with O_NONBLOCK|O_NOFOLLOW
    and fstat the descriptor (closes the swap-after-lstat window). Returns the fd; raises AlertStoreError."""
    name = os.path.basename(path)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise AlertStoreError(f"alerts store file {name} cannot be examined: {exc}") from exc
    if st is not None and not stat.S_ISREG(st.st_mode):
        kind = "a symlink" if stat.S_ISLNK(st.st_mode) else f"type {stat.S_IFMT(st.st_mode):o}"
        raise AlertStoreError(f"alerts store file {name} is not a regular file ({kind}); refusing to open it")
    try:
        fd = os.open(path, flags | os.O_NONBLOCK | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), mode)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise AlertStoreError(f"alerts store file {name} cannot be opened: {exc}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AlertStoreError(f"alerts store file {name} is not a regular file; refusing to use it")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    """The records of one jsonl file. A missing file is empty; an unparseable line (a torn tail, garbage) is
    skipped, never fatal; a non-regular file raises AlertStoreError (never read, never blocks)."""
    out: List[Dict[str, Any]] = []
    try:
        fd = _open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return out
    try:
        with os.fdopen(fd, encoding="utf-8", errors="replace") as fh:
            fd = -1
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue  # a torn line is skipped, never fatal
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        pass
    finally:
        if fd >= 0:
            os.close(fd)
    return out


def _append_jsonl(path: str, rec: Dict[str, Any]) -> None:
    """Append one record on a line of its own (F1): if the file's last byte is not a newline (a crash or a full
    disk tore the previous write), a newline goes first, in the SAME write, so the new record is never glued to
    the torn one. Caller holds the store lock."""
    data = (json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    fd = _open_regular(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        size = os.fstat(fd).st_size
        if size > 0 and os.pread(fd, 1, size - 1) != b"\n":
            data = b"\n" + data
        os.write(fd, data)
    finally:
        os.close(fd)


def _try_flock(path: str, wait_s: float) -> Optional[int]:
    """An exclusive flock on a regular lock file, waiting at most ``wait_s``; the fd, or None (never raises)."""
    fd = None
    try:
        fd = _open_regular(path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait_s
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.002)
    except Exception:  # noqa: BLE001
        pass
    if fd is not None:
        try:
            os.close(fd)
        except OSError:
            pass
    return None


class AlertStore:
    """The durable alert log under one directory. Every mutation takes the directory's flock, so concurrent
    processes serialize; reads of a consistent snapshot take it too."""

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self._tlock = threading.Lock()

    @property
    def alerts_path(self) -> str:
        return os.path.join(self.dir, ALERTS_FILE)

    @property
    def dismissed_path(self) -> str:
        return os.path.join(self.dir, DISMISSED_FILE)

    class _Locked:
        def __init__(self, store: "AlertStore") -> None:
            self.store = store
            self.fd: Optional[int] = None

        def __enter__(self) -> "AlertStore._Locked":
            if not self.store._tlock.acquire(timeout=LOCK_WAIT_S):
                raise AlertStoreError("alerts store is busy (in-process lock held too long)")
            try:
                try:
                    os.makedirs(self.store.dir, mode=0o700, exist_ok=True)
                except OSError as exc:
                    raise AlertStoreError(f"alerts directory {self.store.dir} is not usable: {exc}") from exc
                self.fd = _open_regular(os.path.join(self.store.dir, LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
                deadline = time.monotonic() + LOCK_WAIT_S
                while True:
                    try:
                        fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise AlertStoreError("alerts store is busy (directory lock held too long)")
                        time.sleep(0.01)
            except BaseException:
                if self.fd is not None:
                    os.close(self.fd)
                    self.fd = None
                self.store._tlock.release()
                raise
            return self

        def __exit__(self, *exc: Any) -> None:
            try:
                if self.fd is not None:
                    try:
                        fcntl.flock(self.fd, fcntl.LOCK_UN)
                    finally:
                        os.close(self.fd)
            finally:
                self.store._tlock.release()

    def locked(self) -> "AlertStore._Locked":
        return AlertStore._Locked(self)

    # -- reads (caller holds the lock, or accepts a racy snapshot) --

    def _records(self) -> List[Dict[str, Any]]:
        return _read_jsonl(self.alerts_path)

    def _dismissed_ids(self) -> set:
        ids: set = set()
        for rec in _read_jsonl(self.dismissed_path):
            for i in rec.get("ids") or []:
                if isinstance(i, str):
                    ids.add(i)
        return ids

    def active(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        """Undismissed alerts younger than ACTIVE_MAX_AGE_S, newest first (capped at ACTIVE_LIMIT)."""
        now = time.time() if now is None else now
        with self.locked():
            recs, gone = self._records(), self._dismissed_ids()
        live = [r for r in recs if isinstance(r.get("id"), str) and r["id"] not in gone
                and isinstance(r.get("ts"), (int, float)) and now - r["ts"] <= ACTIVE_MAX_AGE_S]
        live.sort(key=lambda r: (r["ts"], r["id"]), reverse=True)
        return [public_view(r) for r in live[:ACTIVE_LIMIT]]

    # -- writes --

    def find_body(self, source: str, key: str) -> Optional[str]:
        """The body of the newest stored alert with this ``source`` and ``key`` that has not been resolved, else None
 (the dashboard badge takes its question text from the stored alert, never from the session log)."""
        with self.locked():
            recs = self._records()
        for r in reversed(recs):
            if r.get("source") == source and r.get("key") == key:
                return None if r.get("resolved") else (r.get("body") if isinstance(r.get("body"), str) else None)
        return None

    def dismiss(self, alert_id: str, now: Optional[float] = None) -> bool:
        """True when ``alert_id`` names a known alert (idempotent: dismissing twice is still True)."""
        now = time.time() if now is None else now
        with self.locked():
            if not any(r.get("id") == alert_id for r in self._records()):
                return False
            if alert_id not in self._dismissed_ids():
                _append_jsonl(self.dismissed_path, {"ts": now, "ids": [alert_id]})
        return True

    def dismiss_all(self, ids: Sequence[str], now: Optional[float] = None) -> int:
        """Dismiss exactly the alerts in ``ids`` that are known and still active (F6: the ids the client DISPLAYED,
        so an alert that arrived after its last poll, or sits behind "+k more", stays active). Returns how many
        were dismissed. Unknown and already-dismissed ids are ignored."""
        now = time.time() if now is None else now
        want = {i for i in ids if isinstance(i, str)}
        with self.locked():
            recs, gone = self._records(), self._dismissed_ids()
            hit = [r["id"] for r in recs if isinstance(r.get("id"), str) and r["id"] in want and r["id"] not in gone
                   and isinstance(r.get("ts"), (int, float)) and now - r["ts"] <= ACTIVE_MAX_AGE_S]
            if hit:
                _append_jsonl(self.dismissed_path, {"ts": now, "ids": hit, "all": True})
        return len(hit)

    # -- producer failure record (F3, F-D1..D3): best effort, never raises, no store lock. Written by BOTH the
    #    harness and the web process; the SAME `_append_jsonl` helper as the other two files (fresh-line repair). --

    @property
    def failures_path(self) -> str:
        return os.path.join(self.dir, FAILURES_FILE)

    def record_failure(self, producer: str, error: str, now: Optional[float] = None, *, kind: str = "failed",
                       n: int = 1) -> None:
        try:
            try:
                os.makedirs(self.dir, mode=0o700, exist_ok=True)
            except OSError:
                return
            rec = {"ts": time.time() if now is None else now, "producer": producer, "kind": kind, "n": int(n),
                   "error": clean_text(error, FAILURE_ERROR_CAP)}
            # F-R1: the size check, the rotation AND the append happen under ONE flock (own lock file, short wait),
            # so two writers at the cap cannot both rotate and overwrite the rotated generation. If the lock cannot
            # be had in time the record is still appended (no rotation): losing the rotation is better than the record.
            lock_fd = _try_flock(os.path.join(self.dir, FAILURES_LOCK_FILE), FAILURES_LOCK_WAIT_S)
            try:
                if lock_fd is not None:
                    try:
                        if os.lstat(self.failures_path).st_size > FAILURES_FILE_MAX:
                            os.replace(self.failures_path, self.failures_path + ".1")   # keep the NEWEST records
                    except OSError:
                        pass
                _append_jsonl(self.failures_path, rec)
            finally:
                if lock_fd is not None:
                    os.close(lock_fd)     # closing releases the flock
        except Exception:  # noqa: BLE001 - recording a failure must never itself fail a producer
            pass

    def failure_counts(self, now: Optional[float] = None, window_s: float = FAILURES_WINDOW_S) -> Dict[str, int]:
        """Failures + drops by producer inside the last ``window_s`` (default 24 h), from the records' own
        timestamps, across the current and rotated file and ANY writing process."""
        t = time.time() if now is None else now
        counts: Dict[str, int] = {}
        try:
            for path in (self.failures_path + ".1", self.failures_path):
                for rec in _read_jsonl(path):
                    p, ts = rec.get("producer"), rec.get("ts")
                    if not isinstance(p, str) or not isinstance(ts, (int, float)) or isinstance(ts, bool):
                        continue
                    if not (t - window_s <= ts <= t + 300):
                        continue
                    n = rec.get("n")
                    counts[p] = counts.get(p, 0) + (n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 1)
        except AlertStoreError:
            raise       # F-R3: a non-regular failures file is a store error for the caller to show, not a silent 0
        except Exception:  # noqa: BLE001
            pass
        return counts

    def _rotate_if_big(self) -> None:
        try:
            if os.path.getsize(self.alerts_path) < ROTATE_BYTES:
                return
        except OSError:
            return
        os.replace(self.alerts_path, self.alerts_path + ".1")
        if os.path.exists(self.dismissed_path):
            os.replace(self.dismissed_path, self.dismissed_path + ".1")


def public_view(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The fields the API returns (never the internal key/macos bookkeeping)."""
    return {"id": rec.get("id"), "ts": rec.get("ts"), "source": rec.get("source"),
            "severity": rec.get("severity"), "title": rec.get("title"), "body": rec.get("body"),
            "link": rec.get("link")}


# --- macOS channel ----------------------------------------------------------------------------------------------

#: The AppleScript is CONSTANT. The text arrives as argv: item 1 = title, item 2 = body.
_OSA_SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")


def osascript_argv(title: str, body: str, script: Sequence[str] = _OSA_SCRIPT) -> List[str]:
    """argv for ``osascript``: constant ``-e`` script lines, then ``--``, then the text as plain arguments. The
    ``--`` keeps a title that starts with ``-`` from being read as an option."""
    argv = [OSASCRIPT]
    for line in script:
        argv += ["-e", line]
    return argv + ["--", title, body]


#: Test seam: ``runner(argv) -> None`` replaces the real spawn. None = the real, guarded spawn below.
_macos_runner: Optional[Callable[[List[str]], None]] = None


def _spawn_osascript(argv: List[str]) -> None:
    """Real spawn on a daemon thread (never blocks the caller; a hung osascript is killed after the timeout).
    Under pytest the real spawn is REFUSED unless a test opted in, so no test run pops a notification."""
    if os.environ.get("PYTEST_CURRENT_TEST") and not os.environ.get("GNOMON_ALERTS_ALLOW_OSASCRIPT_IN_TESTS"):
        return
    if not os.path.exists(OSASCRIPT):
        return

    def _run() -> None:
        try:
            p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, shell=False)
            try:
                p.wait(timeout=OSASCRIPT_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        except Exception:  # noqa: BLE001 - a notification failure never matters to the alert
            pass

    threading.Thread(target=_run, name="gm-alert-osascript", daemon=True).start()


def _post_macos(title: str, source: str, body: str) -> str:
    try:
        t = clean_text("Gnomon: " + title, MACOS_TITLE_CAP)
        b = clean_text(body or source, MACOS_BODY_CAP)
        argv = osascript_argv(t, b)
        (_macos_runner or _spawn_osascript)(argv)
        return "posted"
    except Exception as exc:  # noqa: BLE001
        return f"failed: {type(exc).__name__}"


# --- notify -----------------------------------------------------------------------------------------------------


def source_class(source: Any) -> str:
    """``agent`` for what an agent raised through the verb (``agent:<id>``), ``system`` for every other source."""
    return "agent" if isinstance(source, str) and source.startswith("agent:") else "system"


def _new_id(now: float) -> str:
    return f"a{int(now * 1000):013d}-{secrets.token_hex(4)}"


def _rewrite_jsonl(path: str, recs: List[Dict[str, Any]]) -> None:
    """Replace the file with ``recs`` (one per line): a sibling temp file, then an atomic rename. Caller holds the store lock."""
    tmp = f"{path}.tmp-{os.getpid()}-{secrets.token_hex(3)}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fd = -1
            for r in recs:
                fh.write((json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        os.replace(tmp, path)
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def notify(source: Any, severity: Any, title: Any, body: Any = "", link: Any = None, *,
           key: Any = None, store: Optional[AlertStore] = None, runtime_root: Optional[str] = None,
           macos: bool = True, rate_max: int = RATE_MAX, now: Optional[float] = None,
           macos_share: Optional[Tuple[str, int]] = None, replace: bool = False) -> Dict[str, Any]:
    """Raise one alert. Returns ``{"ok": bool, "status": ..., "id"?: ..., "macos"?: ...}``; NEVER raises.

 ``macos_share=(key_prefix, n)``: this alert may post a macOS notification only while fewer than ``n`` alerts whose
    key starts with ``key_prefix`` posted one in the window, so one producer cannot spend the whole shared budget.
 ``replace=True`` (the 'mark the first alert resolved' the spec asks for): instead of raising, REWRITE the stored
    alert with this ``source`` and ``key`` in place (severity, title and body replaced, its macOS bookkeeping untouched); status
    ``replaced``, or ``no-original`` when there is none (never a new alert, never a notification).

    status: ``raised`` | ``deduped`` (same source+key inside the window) | ``rate-limited`` (this source is over
    its limit) | ``invalid`` (bad severity / empty title) | ``error`` (the store failed). ``ok`` is True for
    ``raised`` and ``deduped`` (the producer's event is already on screen); False otherwise.
    """
    try:
        sev = severity if severity in _SEV_RANK else None
        ttl = clean_text(title, TITLE_CAP)
        if sev is None:
            return {"ok": False, "status": "invalid", "error": f"severity must be one of {list(SEVERITIES)}"}
        if not ttl:
            return {"ok": False, "status": "invalid", "error": "title is required (plain text, non-empty)"}
        src = clean_source(source)
        bod = clean_text(body, BODY_CAP, multiline=True)
        lnk = clean_link(link)
        k = clean_text(key, KEY_CAP) if key not in (None, "") else hashlib.sha256(ttl.encode("utf-8")).hexdigest()[:16]
        t = time.time() if now is None else now
        st = store or AlertStore(default_dir(runtime_root))
        post_macos = False
        with st.locked():
            recs = st._records()
            if replace:
                idx = [i for i, r in enumerate(recs) if r.get("source") == src and r.get("key") == k]
                if not idx:
                    return {"ok": True, "status": "no-original"}
                r = dict(recs[idx[-1]])
                r.update({"severity": sev, "title": ttl, "body": bod, "link": lnk, "resolved": True})
                recs[idx[-1]] = r
                _rewrite_jsonl(st.alerts_path, recs)
                return {"ok": True, "status": "replaced", "id": r.get("id")}
            window_dupe = any(r.get("source") == src and r.get("key") == k and isinstance(r.get("ts"), (int, float))
                              and t - r["ts"] < DEDUPE_WINDOW_S for r in recs)
            if window_dupe:
                return {"ok": True, "status": "deduped", "link_dropped": bool(link) and lnk is None}
            mine = [r for r in recs if r.get("source") == src and isinstance(r.get("ts"), (int, float))
                    and t - r["ts"] < RATE_WINDOW_S]
            if len(mine) >= max(0, int(rate_max)):
                # one visible notice per window per source, itself exempt from the limit but de-duplicated
                nkey = "ratelimit:" + src
                if not any(r.get("source") == RATE_NOTICE_SOURCE and r.get("key") == nkey
                           and isinstance(r.get("ts"), (int, float)) and t - r["ts"] < RATE_WINDOW_S for r in recs):
                    st._rotate_if_big()
                    _append_jsonl(st.alerts_path, {
                        "id": _new_id(t), "ts": t, "source": RATE_NOTICE_SOURCE, "key": nkey,
                        "severity": "warning", "macos": False, "link": None,
                        "title": clean_text(f"Alerts from {src} are being rate-limited", TITLE_CAP),
                        "body": clean_text(f"More than {int(rate_max)} alerts in {int(RATE_WINDOW_S // 60)} minutes; "
                                           "further ones are dropped until the window passes.", BODY_CAP)})
                return {"ok": False, "status": "rate-limited"}
            if macos and _SEV_RANK[sev] >= _SEV_RANK[MACOS_MIN_SEVERITY]:
                cls = source_class(src)
                recent = [r for r in recs if r.get("macos") and isinstance(r.get("ts"), (int, float))
                          and t - r["ts"] < MACOS_WINDOW_S and source_class(r.get("source")) == cls]
                post_macos = len(recent) < MACOS_MAX
                if post_macos and cls == "agent":
                    post_macos = len([r for r in recent if r.get("source") == src]) < MACOS_AGENT_PER_SOURCE
                if post_macos and macos_share is not None:
                    pref, cap = macos_share
                    post_macos = len([r for r in recent if str(r.get("key") or "").startswith(pref)]) < int(cap)
            aid = _new_id(t)
            st._rotate_if_big()
            _append_jsonl(st.alerts_path, {"id": aid, "ts": t, "source": src, "key": k, "severity": sev,
                                           "title": ttl, "body": bod, "link": lnk, "macos": post_macos})
        out: Dict[str, Any] = {"ok": True, "status": "raised", "id": aid, "link_dropped": bool(link) and lnk is None}
        if post_macos:
            out["macos"] = _post_macos(ttl, src, bod)
        else:
            out["macos"] = "not-requested" if not macos or _SEV_RANK[sev] < _SEV_RANK[MACOS_MIN_SEVERITY] \
                else "rate-limited"
        return out
    except Exception as exc:  # noqa: BLE001 - notify never raises into a caller
        return {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"}


# --- producers (F3 loud failures, F4 off-pump hand-off) ----------------------------------------------------------

class _StderrHandler(logging.StreamHandler):
    """Emits to the CURRENT ``sys.stderr`` of the process that hit the failure (resolved at emit time)."""

    def __init__(self) -> None:
        super().__init__()

    @property  # type: ignore[override]
    def stream(self):  # noqa: D401
        return sys.stderr

    @stream.setter
    def stream(self, _value) -> None:  # StreamHandler.__init__ assigns it; ignored
        pass


_log = logging.getLogger("gnomon.alerts")
if not any(isinstance(h, _StderrHandler) for h in _log.handlers):   # F-D1(a): a handler that actually emits
    _log.addHandler(_StderrHandler())
    _log.setLevel(logging.WARNING)
    _log.propagate = False   # F-R5: this handler IS the emitter; a root handler must not print the line a second time

_counter_lock = threading.Lock()
_failures: Dict[str, int] = {}
_dropped: Dict[str, int] = {}
_queue: "queue.Queue[Any]" = queue.Queue()
_worker: Optional[threading.Thread] = None
_flusher: Optional[threading.Thread] = None
_pending_drops: Dict[str, List[Any]] = {}      # producer -> [count, store]; written by the flusher, never the pump
_drop_event = threading.Event()


def producer_counters() -> Dict[str, Dict[str, int]]:
    """This process's counts: ``failures`` (a producer's notify returned error/invalid or raised) and ``dropped``
    (the hand-off queue was full). The web API also reports the cross-process failure file."""
    with _counter_lock:
        return {"failures": dict(_failures), "dropped": dict(_dropped)}


def reset_producer_state() -> None:
    """Test seam: zero the counters and empty the queue."""
    with _counter_lock:
        _failures.clear()
        _dropped.clear()
        _pending_drops.clear()
    try:
        while True:
            _queue.get_nowait()
            _queue.task_done()
    except queue.Empty:
        pass


def _note_failure(producer: str, error: str, store: Optional[AlertStore]) -> None:
    with _counter_lock:
        _failures[producer] = _failures.get(producer, 0) + 1
    _log.warning("alert producer %s failed: %s", producer, error)
    if store is not None:
        store.record_failure(producer, error)


def raise_from_producer(producer: str, *args: Any, **kwargs: Any) -> None:
    """For a PRODUCER (stuck-hold, DSD arrival, later Scheduler/Apps): raise the alert; NEVER raise into the caller,
    but never fail silently either (g5): an ``error``/``invalid`` result, or an exception, writes one log line
    naming the producer and the error, bumps this process's counter, and is recorded in ``producer-failures.jsonl``
    beside the store (so the alerts column can show it). ``rate-limited`` and ``deduped`` are not failures."""
    store = kwargs.get("store")
    try:
        build = kwargs.pop("_build", None)
        if build is not None:
            # the work of making the alert (reading settings, the operator's presence) runs HERE, on the worker
            # thread, never on the terminal-reading thread that handed it over. ``None`` = nothing to raise.
            built = build()
            if built is None:
                return
            args, extra = built
            kwargs = {**kwargs, **extra}
        res = notify(*args, **kwargs)
        if res.get("status") in ("error", "invalid"):
            _note_failure(producer, f"{res.get('status')}: {res.get('error')}", store)
    except Exception as exc:  # noqa: BLE001 - belt and braces: notify itself never raises
        try:
            _note_failure(producer, f"{type(exc).__name__}: {exc}", store)
        except Exception:  # noqa: BLE001
            pass


def _flush_drops_once() -> None:
    with _counter_lock:
        batch = {k: (v[0], v[1]) for k, v in _pending_drops.items()}
    for producer, (count, store) in batch.items():
        _log.warning("alert producer %s dropped %d alert(s): hand-off queue full (%d)", producer, count,
                     PRODUCER_QUEUE_MAX)      # the flusher thread, never the pump (a full stderr pipe may block it)
        if store is not None:
            store.record_failure(producer, f"{count} alert(s) dropped: hand-off queue full ({PRODUCER_QUEUE_MAX})",
                                 kind="dropped", n=count)
        with _counter_lock:      # removed only AFTER the write, so drain() sees "pending" until it is on disk
            ent = _pending_drops.get(producer)
            if ent is not None:
                ent[0] -= count
                if ent[0] <= 0:
                    del _pending_drops[producer]


def _flusher_loop() -> None:
    while True:
        _drop_event.wait()
        _drop_event.clear()
        try:
            _flush_drops_once()
        except Exception:  # noqa: BLE001
            pass
        time.sleep(DROP_FLUSH_S)


def _worker_loop() -> None:
    while True:
        producer, args, kwargs = _queue.get()
        try:
            raise_from_producer(producer, *args, **kwargs)
        except Exception:  # noqa: BLE001
            pass
        finally:
            _queue.task_done()


def submit_producer(producer: str, *args: Any, **kwargs: Any) -> bool:
    """Hand an alert to the bounded worker queue and return IMMEDIATELY (F4: the harness pump must never wait on
    alert I/O). True = queued; False = the queue was full, the alert was DROPPED and counted (and logged). Never
    raises, never blocks."""
    global _worker, _flusher
    try:
        with _counter_lock:
            if _worker is None or not _worker.is_alive():
                _worker = threading.Thread(target=_worker_loop, name="gm-alert-producer", daemon=True)
                _worker.start()
            if _flusher is None or not _flusher.is_alive():
                _flusher = threading.Thread(target=_flusher_loop, name="gm-alert-drops", daemon=True)
                _flusher.start()
        if _queue.qsize() >= PRODUCER_QUEUE_MAX:
            raise queue.Full
        _queue.put_nowait((producer, args, kwargs))
        return True
    except queue.Full:
        with _counter_lock:
            _dropped[producer] = _dropped.get(producer, 0) + 1
            ent = _pending_drops.setdefault(producer, [0, kwargs.get("store")])
            ent[0] += 1
        _drop_event.set()          # the flusher thread logs AND persists the drop; no I/O of any kind here (F-R2)
        return False
    except Exception as exc:  # noqa: BLE001
        try:
            _note_failure(producer, f"{type(exc).__name__}: {exc}", None)
        except Exception:  # noqa: BLE001
            pass
        return False


def drain(timeout: float = 5.0) -> bool:
    """Wait (bounded) until every queued producer alert has been handled. For tests and orderly shutdown."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _counter_lock:
            pending = bool(_pending_drops)
        if _queue.unfinished_tasks == 0 and not pending and not _drop_event.is_set():
            return True
        time.sleep(0.01)
    return False


def notify_quietly(*args: Any, **kwargs: Any) -> None:
    """Deprecated alias kept for callers outside this repo: now loud (producer ``unknown``), never raises."""
    raise_from_producer("unknown", *args, **kwargs)

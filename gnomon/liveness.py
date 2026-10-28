"""Per-session liveness (liveness-method).

NO PID registry. A session's live/ended status is derived
from three cheap, restart-surviving signals:

* the tracking **canary** JSON is present in the session dir (written at launch);
* a matching **process** is running (``pgrep`` for a claude process whose command
  line names the session dir);
* the **transcript** file's mtime is within a freshness window (an earlier launcher uses
  45 min).

Rule: a session is LIVE iff its canary is present AND (a process is found OR the
transcript was touched within the window). The session list is rebuilt after a
restart by scanning canaries under the sessions root — no in-memory registry is
needed.

Every environmental probe (``pgrep``, ``mtime``, ``now``) is injected, so the
whole thing is deterministic in tests and never shells out there.

⛔ IMPORT CLOSURE OF THE STATUSLINE HOOK: ``instance_id_for`` / ``_read_json`` here are
imported by ``gnomon.harness.statusline_hook``, which Claude Code runs on every
status refresh of every harnessed session. This module is therefore on an every-session
path (g48e): an edit goes live in every session on save, so it needs a
Diamond-reviewed scratch copy before it is swapped into the live tree.
"""

from __future__ import annotations

import glob
import json
import os
import stat
import subprocess
import time
from dataclasses import dataclass
from typing import Callable, List, Optional


DEFAULT_WINDOW_SECONDS: float = 45 * 60  # an earlier launcher session.py window


@dataclass(frozen=True)
class SessionStatus:
    instance_id: str
    session_dir: str
    live: bool
    reason: str
    canary_present: bool
    process_found: bool
    transcript_mtime: Optional[float]
    transcript_path: Optional[str]


def _default_pgrep(pattern: str) -> bool:
    """True iff a process's argv matches ``pattern`` (``pgrep -f``)."""
    try:  # pragma: no cover - real subprocess, not exercised in tests
        return subprocess.run(
            ["pgrep", "-f", pattern],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _default_process_snapshot() -> Optional[str]:
    """ONE ``ps`` listing of every process's full command line (None on failure).

    The poll-pileup fix: ``scan`` used to fork one ``pgrep -f`` per session on every
    ``/api/sessions`` call (28 forks, ~97% of the request). Each fork/read/wait releases the GIL, and in
    the busy web process every release is a long wait to get it back. One listing, matched in-process,
    is one fork per scan."""
    try:  # pragma: no cover - real subprocess, not exercised in tests
        r = subprocess.run(["/bin/ps", "-axww", "-o", "command="], stdout=subprocess.PIPE,
                           stderr=subprocess.DEVNULL, timeout=15)
        if r.returncode != 0:
            return None
        return r.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return None


def _default_mtime(path: str) -> Optional[float]:
    try:
        return os.path.getmtime(path)
    except OSError:
        return None


class LivenessProbe:
    """Derives :class:`SessionStatus` from canary + process + transcript mtime.

    Parameters (all injectable for tests):

    * ``window_seconds`` — transcript-freshness window (default 45 min).
    * ``now`` — current unix time.
    * ``pgrep`` — ``pattern -> bool`` process presence check.
    * ``mtime`` — ``path -> Optional[float]`` file mtime.
    * ``transcript_resolver`` — ``canary_dict -> Optional[path]``; where the
      session's transcript lives. Default reads a ``transcript_path`` key from
      the canary if the launcher recorded one (a later slice wires the D6
      statusline hook to record it), else ``None``.
    """

    def __init__(
        self,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        now: Callable[[], float] = time.time,
        pgrep: Callable[[str], bool] = _default_pgrep,
        mtime: Callable[[str], Optional[float]] = _default_mtime,
        transcript_resolver: Optional[Callable[[dict], Optional[str]]] = None,
        process_snapshot: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        # ``process_snapshot`` is used by ``scan`` only, and only when ``pgrep`` is the default (an
        # injected ``pgrep`` always wins, so tests stay deterministic).
        self._snapshot = process_snapshot if process_snapshot is not None else (
            _default_process_snapshot if pgrep is _default_pgrep else None)
        self._window = window_seconds
        self._now = now
        self._pgrep = pgrep
        self._mtime = mtime
        self._resolve_transcript = transcript_resolver or _canary_transcript_path

    def probe(self, session_dir: str, snapshot: Optional[str] = None) -> SessionStatus:
        canary_path = _canary_path(session_dir)
        canary = _read_json(canary_path)
        canary_present = canary is not None and bool(canary.get("gnomon_canary"))
        instance_id = instance_id_for(session_dir, canary)

        if not canary_present:
            return SessionStatus(
                instance_id=instance_id,
                session_dir=session_dir,
                live=False,
                reason="no canary",
                canary_present=False,
                process_found=False,
                transcript_mtime=None,
                transcript_path=None,
            )

        # snapshot (one ps listing, taken once per scan) -> substring match; else the per-session pgrep.
        process_found = (session_dir in snapshot) if snapshot is not None else self._pgrep(session_dir)

        transcript_path = self._resolve_transcript(canary) if isinstance(canary, dict) else None
        transcript_mtime = self._mtime(transcript_path) if transcript_path else None
        fresh = (
            transcript_mtime is not None
            and (self._now() - transcript_mtime) <= self._window
        )

        live = process_found or fresh
        if process_found and fresh:
            reason = "process running; transcript fresh"
        elif process_found:
            reason = "process running"
        elif fresh:
            reason = "transcript fresh (no process match)"
        else:
            reason = "no process; transcript stale or absent"

        return SessionStatus(
            instance_id=instance_id,
            session_dir=session_dir,
            live=live,
            reason=reason,
            canary_present=True,
            process_found=process_found,
            transcript_mtime=transcript_mtime,
            transcript_path=transcript_path,
        )

    def scan(self, sessions_root: str) -> List[SessionStatus]:
        """Rebuild the session list by scanning canaries under the root."""
        out: List[SessionStatus] = []
        pattern = os.path.join(sessions_root, "*", ".claude", "canary.json")
        canaries = sorted(glob.glob(pattern))
        snapshot: Optional[str] = None
        if canaries and self._snapshot is not None:
            try:
                snapshot = self._snapshot()   # None (ps failed) -> fall back to the per-session pgrep
            except Exception:  # noqa: BLE001 - liveness must not fail the scan
                snapshot = None
        for canary_path in canaries:
            session_dir = os.path.dirname(os.path.dirname(canary_path))
            out.append(self.probe(session_dir, snapshot))
        return out


# -- helpers -----------------------------------------------------------------

def _canary_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", "canary.json")


def _canary_transcript_path(canary: dict) -> Optional[str]:
    path = canary.get("transcript_path")
    return path if isinstance(path, str) and path else None


_READ = object()


def instance_id_for(session_dir: str, canary: object = _READ) -> str:
    """The instance id of a harness session dir: the canary's ``instance_id``,
    else the dir name minus its timestamp. ONE implementation, shared by the
 liveness scan and the status line label."""
    if canary is _READ:
        canary = _read_json(_canary_path(session_dir))
    if isinstance(canary, dict):
        return str(canary.get("instance_id", "")) or _infer_instance_id(session_dir)
    return _infer_instance_id(session_dir)


def _infer_instance_id(session_dir: str) -> str:
    # session dir is "<instance_id>-<UTC-YYYYMMDD-HHMMSS>"; strip the timestamp.
    base = os.path.basename(session_dir.rstrip("/"))
    parts = base.rsplit("-", 2)
    if len(parts) == 3:
        return parts[0]
    return base


#: A canary is a few hundred bytes; anything past this is not one.
_READ_JSON_MAX_BYTES = 1 << 20


def _read_json(path: str) -> Optional[dict]:
    """Read a JSON object from a REGULAR file of bounded size, else None.

    A FIFO (or device) at the path must not hang the caller: the file is opened
    O_NONBLOCK (a FIFO then opens at once) and fstat'ed before any read."""
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > _READ_JSON_MAX_BYTES:
            return None
        with os.fdopen(fd, "r", encoding="utf-8") as fh:
            fd = -1  # fdopen owns it now
            obj = json.loads(fh.read(_READ_JSON_MAX_BYTES + 1))
    except (FileNotFoundError, ValueError, OSError):  # ValueError: JSONDecodeError, bad UTF-8
        return None
    finally:
        if fd >= 0:
            os.close(fd)
    return obj if isinstance(obj, dict) else None

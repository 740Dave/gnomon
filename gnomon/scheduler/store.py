"""The Scheduler store under <DATA>/scheduler (requirement-store) and the hash-chained logs
(requirement-log-chain).

Layout: jobs/<id>.json, pins/<id>.json, proposals/<id>.json, state/<id>.json, runs/<id>/<run-id>.json (the detail
view), runs/<id>/runs.jsonl (the CHAINED record), locks/, audit.jsonl (CHAINED).

Writes reuse the Apps host's atomic writer and descriptor-safe opens (``appshost.state`` / ``appshost.fsafe``): one
writer, no copy (g40/g16).

MACHINE-READ (g25): every record carries ``schema_version``. Chain format: each JSONL line is a JSON object carrying
``prev_sha256`` = the sha256 (hex) of the PREVIOUS line's bytes, excluding its trailing newline; the first line
carries the empty string. A reader verifies the chain with :func:`verify_chain` and a break is reported with its line
number, never skipped. KNOWN LIMIT (stated, not solved): an edit of the LAST line, or truncation of the tail, leaves
no later line to disagree, so the chain alone cannot see it.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterator, List, Optional, Tuple

from ..appshost.fsafe import NotRegularFile, open_append_fd, open_lock_fd, open_regular
from ..appshost.state import atomic_write_json, utc_now_iso
from .vocab import SCHEMA_VERSION

ID_RE = re.compile(r"\A[a-z][a-z0-9-]{1,40}\Z")
RUN_ID_RE = re.compile(r"\A[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}\Z")
MAX_LINE_BYTES = 64 * 1024


class StoreError(OSError):
    pass


class SchedulerPaths:
    """Pure path arithmetic for one scheduler root (``<DATA>/scheduler``); creates nothing."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))

    def _id(self, job_id: str) -> str:
        if not isinstance(job_id, str) or not ID_RE.match(job_id):
            raise ValueError(f"not a job id: {job_id!r}")
        return job_id

    @property
    def jobs(self) -> str: return os.path.join(self.root, "jobs")
    @property
    def pins(self) -> str: return os.path.join(self.root, "pins")
    @property
    def proposals(self) -> str: return os.path.join(self.root, "proposals")
    @property
    def state(self) -> str: return os.path.join(self.root, "state")
    @property
    def runs(self) -> str: return os.path.join(self.root, "runs")
    @property
    def locks(self) -> str: return os.path.join(self.root, "locks")
    @property
    def audit(self) -> str: return os.path.join(self.root, "audit.jsonl")
    @property
    def lock(self) -> str: return os.path.join(self.locks, ".scheduler.lock")

    def job(self, i: str) -> str: return os.path.join(self.jobs, self._id(i) + ".json")
    def pin(self, i: str) -> str: return os.path.join(self.pins, self._id(i) + ".json")
    def proposal(self, i: str) -> str: return os.path.join(self.proposals, self._id(i) + ".json")
    def job_state(self, i: str) -> str: return os.path.join(self.state, self._id(i) + ".json")
    def run_dir(self, i: str) -> str: return os.path.join(self.runs, self._id(i))
    def run_chain(self, i: str) -> str: return os.path.join(self.run_dir(i), "runs.jsonl")

    def run_file(self, i: str, run_id: str) -> str:
        if not RUN_ID_RE.match(run_id):
            raise ValueError(f"not a run id: {run_id!r}")
        return os.path.join(self.run_dir(i), run_id + ".json")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def read_json(path: str, default: Any = None) -> Any:
    """A JSON file, read without ever blocking on a FIFO. Missing -> default. Unreadable/corrupt -> ValueError."""
    try:
        with open_regular(path) as fh:
            return json.loads(fh.read(4 << 20).decode("utf-8"))
    except FileNotFoundError:
        return default
    except (NotRegularFile, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"{path}: unreadable ({exc})") from exc


def write_json(path: str, obj: Any) -> None:
    atomic_write_json(path, obj)


def remove_quiet(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


def list_ids(directory: str) -> List[str]:
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(n[:-5] for n in names if n.endswith(".json") and ID_RE.match(n[:-5]))


@contextlib.contextmanager
def store_lock(paths: SchedulerPaths) -> Iterator[None]:
    """One cross-process lock for every mutation of the store (proposals, jobs, pins, state)."""
    os.makedirs(paths.locks, mode=0o700, exist_ok=True)
    fd = open_lock_fd(paths.lock)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


# --- the hash-chained JSONL logs -----------------------------------------------------------------------------------

def _last_line(path: str) -> Optional[bytes]:
    try:
        with open_regular(path) as fh:
            size = fh.seek(0, os.SEEK_END)
            fh.seek(max(0, size - 2 * (MAX_LINE_BYTES + 1)))
            data = fh.read()
    except FileNotFoundError:
        return None
    lines = [ln for ln in data.split(b"\n") if ln.strip()]
    return lines[-1] if lines else None


def append_chained(path: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """Append ONE record to a chained JSONL file: ``schema_version`` and ``prev_sha256`` are set here (a caller's
    values are overwritten), under an exclusive lock so concurrent writers keep the chain whole."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = open_append_fd(path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        last = _last_line(path)
        rec = dict(record)
        rec["schema_version"] = SCHEMA_VERSION
        rec["prev_sha256"] = sha256_hex(last) if last is not None else ""
        line = json.dumps(rec, sort_keys=True, ensure_ascii=True).encode("utf-8")
        if len(line) > MAX_LINE_BYTES:
            raise StoreError(f"{path}: record of {len(line)} bytes exceeds the {MAX_LINE_BYTES}-byte line cap")
        os.write(fd, line + b"\n")
        return rec
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def verify_chain(path: str) -> Tuple[bool, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """``(ok, records, breaks)``. ``breaks`` is a list of ``{"line": N, "reason": ...}`` (1-based). The first line must
    carry the empty ``prev_sha256``; every later line must carry the sha256 of the previous line's bytes. A line that
    is not a JSON object is a break. NEVER skips a break; a missing file is ``(True, [], [])``."""
    try:
        with open_regular(path) as fh:
            data = fh.read()
    except FileNotFoundError:
        return True, [], []
    except OSError as exc:
        return False, [], [{"line": 0, "reason": f"unreadable: {exc}"}]
    records: List[Dict[str, Any]] = []
    breaks: List[Dict[str, Any]] = []
    prev: Optional[bytes] = None
    n = 0
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        n += 1
        try:
            rec = json.loads(raw.decode("utf-8"))
            if not isinstance(rec, dict):
                raise ValueError("not an object")
        except (ValueError, UnicodeDecodeError):
            breaks.append({"line": n, "reason": "line is not a JSON object"})
            prev = raw
            continue
        want = "" if prev is None else sha256_hex(prev)
        if rec.get("prev_sha256") != want:
            breaks.append({"line": n, "reason": ("first line must carry the empty prev_sha256" if prev is None else
                                                 "prev_sha256 does not match the previous line (an edited, removed "
                                                 "or inserted line)")})
        records.append(rec)
        prev = raw
    return not breaks, records, breaks


def read_chain_tolerant(path: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ok, recs, br = verify_chain(path)
    return recs, br


def new_run_id(now: float, nonce: str) -> str:
    t = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    return f"{t}-{nonce[:6]}"


__all__ = ["SchedulerPaths", "StoreError", "append_chained", "verify_chain", "read_json", "write_json", "store_lock",
           "list_ids", "remove_quiet", "canonical", "sha256_hex", "new_run_id", "utc_now_iso", "ID_RE", "RUN_ID_RE"]

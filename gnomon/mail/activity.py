"""The mail activity log (``<DATA>/mail/activity.jsonl``, 0600): who, when, which message id/subject, draft
recipients. NEVER a body. The writer keeps ONLY the keys in ``ALLOWED_KEYS`` (a ``body`` / ``text`` / ``raw``
key passed by mistake is dropped, and tests prove it), caps subjects, and rotates at 2 MiB."""

from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import time
from typing import Any, Callable, Dict, List, Optional

from .tokenstore import ensure_private_dir

ALLOWED_KEYS = frozenset({
    "ts", "instance", "op", "decision", "reason", "message_id", "message_ids", "thread_id", "draft_id",
    "subject", "recipients", "count", "error_kind", "from_header_allowlisted",
})
SUBJECT_CAP = 140
MAX_BYTES = 2 * 1024 * 1024
MAX_TAIL = 500


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sanitize(rec: Dict[str, Any], now: float) -> Dict[str, Any]:
    out: Dict[str, Any] = {k: v for k, v in rec.items() if k in ALLOWED_KEYS}
    out.setdefault("ts", _iso(now))
    s = out.get("subject")
    if isinstance(s, str):
        out["subject"] = " ".join(s.split())[:SUBJECT_CAP]
    ids = out.get("message_ids")
    if isinstance(ids, list):
        out["message_ids"] = [str(i)[:64] for i in ids[:50]]
    rc = out.get("recipients")
    if isinstance(rc, list):
        out["recipients"] = [str(r)[:120] for r in rc[:20]]
    return out


class ActivityLog:
    def __init__(self, path: str, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.clock = clock

    def record(self, rec: Dict[str, Any]) -> Dict[str, Any]:
        row = sanitize(rec, self.clock())
        ensure_private_dir(os.path.dirname(self.path))
        line = (json.dumps(row, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if os.fstat(fd).st_size > MAX_BYTES:
                os.replace(self.path, self.path + ".1")
                os.close(fd)
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX)
            os.write(fd, line)
        finally:
            os.close(fd)
        return row

    def tail(self, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), MAX_TAIL))
        try:
            with open(self.path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 512 * 1024))
                data = fh.read().decode("utf-8", "replace")
        except FileNotFoundError:
            return []
        rows: List[Dict[str, Any]] = []
        for ln in data.splitlines():
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if isinstance(obj, dict):
                rows.append({k: v for k, v in obj.items() if k in ALLOWED_KEYS})
        return rows[-limit:][::-1]  # newest first

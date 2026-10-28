"""The per-agent open-work log: append-only, hash-chained, derived state (rule-storage, g25 MACHINE-READ).

MACHINE-READ FILE (g25). One JSON object per line. Line 0 is a header (schema_version, reader_floor, and, after a restart, the
generation and the next-id floor). Every later line is ``{"seq": n, "ts": <epoch seconds from the harness clock>, "op": ..., "d": {...},
"prev": <hex>, "hash": <hex>}`` with ``hash = sha256(prev + canonical-json({seq, ts, op, d}))``. The reader VERIFIES the whole chain and
returns an error instead of a list when a line was altered, removed (middle or tail, via the ``.head`` sidecar), reordered, or when the
whole log is MISSING or SHORTER than the sidecar says. It never shrinks the list silently. A change to this format changes its readers
in the same change and bumps SCHEMA_VERSION (proposal-list-log-is-machine-read).

The ``.head`` sidecar: ``{"seq", "hash", "next_id", "generation", "lost_notice", "announced"}``. ``next_id`` is the id counter: ids are
never reused, not even after the log is lost (round 3, F3). All reading-then-writing (id assignment, the open-item limit, id checks) is
ONE critical section under an exclusive file lock (:func:`transact`; round 3, F2).

Scope hygiene, not a security boundary: the files are agent-writable (rule-owner-only-writes). A Store is bound to ONE instance id.
Pure stdlib. No clock is read here: every time is passed in.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from . import constants as C
from .safeio import open_lock, read_regular

#: a log larger than this is not read on a session thread (the Work tab warns at 5 MB long before).
MAX_LOG_BYTES = 64 * 1024 * 1024
#: how long a session thread waits for the list lock before the call is refused as "busy".
LOCK_WAIT_SECONDS = 3.0

ZERO_HASH = "0" * 64
_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
HEADER_NOTE = ("MACHINE-READ (g25): the open-work log of one agent. Append-only and hash-chained; do not edit by hand. "
               "A format change bumps schema_version.")


class LogError(Exception):
    """The log does not verify, or cannot be written. The message is shown to the agent / on the Work tab."""


class Refusal(Exception):
    """Raised by a transaction builder: the call is refused with this reason and NOTHING is written."""


def valid_instance_id(instance_id: Any) -> bool:
    return isinstance(instance_id, str) and bool(_ID_RE.match(instance_id))


def store_dir(data_root: str) -> str:
    return os.path.join(data_root, C.DIRNAME)


def log_path(data_root: str, instance_id: str) -> str:
    if not valid_instance_id(instance_id):
        raise LogError(f"invalid instance id {instance_id!r}")
    return os.path.join(store_dir(data_root), instance_id + ".jsonl")


def _canon(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(prev: str, body: Dict[str, Any]) -> str:
    return hashlib.sha256((prev + _canon(body)).encode("utf-8")).hexdigest()


def _line(seq: int, ts: float, op: str, d: Dict[str, Any], prev: str) -> Tuple[str, str]:
    body = {"seq": seq, "ts": ts, "op": op, "d": d}
    h = _hash(prev, body)
    rec = dict(body)
    rec["prev"] = prev
    rec["hash"] = h
    return json.dumps(rec, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n", h


@dataclass
class LogRead:
    ok: bool
    error: str = ""
    events: List[Dict[str, Any]] = field(default_factory=list)
    head_seq: int = -1
    head_hash: str = ZERO_HASH
    size: int = 0
    exists: bool = False
    #: the log (or its tail) is gone but the sidecar proves it existed: ``next_id`` still holds, new items may be added
    lost: bool = False
    next_id: int = 1
    generation: int = 0


def read_events(path: str) -> LogRead:
    """Read and VERIFY the chain of one file. Never raises. A missing file is ``ok`` with no events (see :func:`verified_read`)."""
    raw, why = read_regular(path, MAX_LOG_BYTES)        # non-blocking; a FIFO / device / directory / foreign symlink is "unreadable" (R3-1)
    if raw is None:
        if why == "absent":
            return LogRead(ok=True, exists=False)
        return LogRead(ok=False, error="the open-work log is unreadable (not a regular file, or too large): move it aside", exists=True)
    out = LogRead(ok=True, size=len(raw), exists=True)
    prev = ZERO_HASH
    expect = 0
    if not raw:
        return out
    partial = not raw.endswith(b"\n")          # R3-2: a log cut MID-LINE (a partial write, a full disk) is a LOSS of its tail, like a short log
    lines = raw.split(b"\n")[:-1]
    for n, ln in enumerate(lines):
        try:
            rec = json.loads(ln.decode("utf-8"))
            body = {"seq": rec["seq"], "ts": rec["ts"], "op": rec["op"], "d": rec["d"]}
            if rec["prev"] != prev or rec["seq"] != expect or _hash(prev, body) != rec["hash"]:
                raise ValueError("chain")
        except Exception:  # noqa: BLE001 - any parse or chain failure is the same loud answer
            return LogRead(ok=False, size=len(raw), exists=True,
                           error=f"open-work log is NOT intact at line {n + 1} (a line was altered, removed or reordered); "
                                 "the list is not shown as if it were clean")
        if expect == 0:
            if rec["op"] != "header" or int(rec["d"].get("reader_floor", 1)) > C.SCHEMA_VERSION:
                return LogRead(ok=False, size=len(raw), exists=True,
                               error="open-work log has an unknown header / a newer reader floor than this build")
        out.events.append(rec)
        prev = rec["hash"]
        expect += 1
    out.head_seq, out.head_hash = expect - 1, prev
    if partial:
        out.ok, out.lost = False, True
        out.error = ("the open-work log ends in a partial line (it was cut short): your earlier list could not be read")
    return out


def _head_path(path: str) -> str:
    return path + ".head"


def head_state(path: str) -> str:
    """``"absent"`` | ``"unreadable"`` (there but not a regular file / not valid) | ``""`` (usable). Never blocks."""
    raw, why = read_regular(_head_path(path), 64 * 1024)
    if raw is None:
        return why
    return "" if read_head(path) is not None else "unreadable"


def read_head(path: str) -> Optional[Dict[str, Any]]:
    """The sidecar, validated; None if absent or unusable."""
    raw, why = read_regular(_head_path(path), 64 * 1024)
    if raw is None:
        return None
    try:
        h = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(h, dict) or isinstance(h.get("seq"), bool) or not isinstance(h.get("seq"), int) or h["seq"] < 0:
        return None
    if not isinstance(h.get("hash"), str):
        return None
    ni = h.get("next_id", 1)
    if isinstance(ni, bool) or not isinstance(ni, int) or ni < 1 or ni > 10 ** 9:
        h["next_id"] = 1
    return h


def _next_id_of(events: List[Dict[str, Any]], head: Optional[Dict[str, Any]]) -> int:
    n = int(head.get("next_id", 1)) if head else 1
    if events:
        n = max(n, int(events[0].get("d", {}).get("next_id_floor", 1)))
    for ev in events:
        if ev["op"] == "add":
            m = re.fullmatch(r"w-(\d+)", str(ev["d"].get("id", "")))
            if m:
                n = max(n, int(m.group(1)) + 1)
    return n


def verified_read(path: str) -> LogRead:
    """``read_events`` plus the sidecar checks: a removed tail, an altered last line, a MISSING log, a log SHORTER than the head."""
    r = read_events(path)
    hs = head_state(path)
    head = read_head(path) if hs == "" else None
    if hs == "unreadable":
        return LogRead(ok=False, exists=r.exists, size=r.size,
                       error="the open-work sidecar (.head) is unreadable (not a regular file, or not valid): move it aside (keep it)")
    if not r.ok and r.lost:      # a cut-short log: the counter still comes from the sidecar and the events that are intact
        r.next_id = _next_id_of(r.events, head)
        r.generation = int((head or {}).get("generation", 0) or 0)
        r.error += f". Ids continue at w-{r.next_id}; work_add starts a new list"
        return r
    if not r.ok:
        return r
    if not r.exists or r.head_seq < 0:
        if head is not None and head["seq"] >= 0:
            return LogRead(ok=False, lost=True, exists=r.exists, size=r.size, next_id=_next_id_of([], head),
                           generation=int(head.get("generation", 0) or 0),
                           error=("the open-work log is MISSING (its sidecar says it had "
                                  f"{head['seq']} entries): your earlier list could not be read. Ids continue at "
                                  f"w-{_next_id_of([], head)}; work_add starts a new list"))
        return r
    r.next_id = _next_id_of(r.events, head)
    r.generation = int((head or {}).get("generation", 0) or 0)
    if head is not None:
        if r.head_seq < head["seq"]:
            return LogRead(ok=False, lost=True, size=r.size, exists=True, next_id=r.next_id, generation=r.generation,
                           error=("the open-work log is SHORTER than its recorded head (the last line(s) were removed): your "
                                  f"earlier list could not be read. Ids continue at w-{r.next_id}; work_add starts a new list"))
        if r.head_seq == head["seq"] and r.head_hash != head.get("hash"):
            return LogRead(ok=False, size=r.size, exists=True,
                           error="open-work log's last line does not match its recorded head (altered)")
    return r


def _write_head(path: str, seq: int, h: str, next_id: int, generation: int, lost_notice: Optional[str], announced: bool) -> None:
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".head-")      # a fresh name: a planted FIFO at a fixed temp name cannot block us
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"seq": seq, "hash": h, "next_id": next_id, "generation": generation,
                       "lost_notice": lost_notice, "announced": announced}, fh)
        os.chmod(tmp, 0o600)
        os.replace(tmp, _head_path(path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@contextmanager
def _locked(path: str, shared: bool = False, create: bool = True) -> Iterator[None]:
    if create:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = open_lock(path + ".lock", create)          # a FIFO / directory / device at the lock path is refused, never waited on
    try:
        # A bounded wait (R3-6): this runs on the PTY pump thread, so a stuck or stopped process holding the lock must not freeze it.
        mode = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | fcntl.LOCK_NB
        deadline = time.monotonic() + LOCK_WAIT_SECONDS
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK) or time.monotonic() >= deadline:
                    raise LogError("the open-work list is busy (another process holds its lock for too long); try again") from exc
                time.sleep(0.005)
        try:
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
    finally:
        os.close(fd)


# Builder protocol: build(items, next_id) -> (op, d) or raises Refusal. ``items`` are derived under the lock.
Builder = Callable[[Dict[str, Dict[str, Any]], int], Tuple[str, Dict[str, Any]]]


def transact(path: str, ts: float, build: Builder, *, allow_restart: bool = False) -> Dict[str, Any]:
    """The ONE write path. Under an exclusive lock: verify the chain, derive the items, let ``build`` check and decide (it assigns ids
    from ``next_id`` and enforces limits), append the event, update the sidecar (head, next id). Nothing is written if ``build`` raises
    :class:`Refusal`. If the log is LOST (missing / shorter than the sidecar) and ``allow_restart`` (work_add only), a NEW generation
    starts: the broken file is moved aside, ids continue from the counter, and the earlier loss is recorded in the sidecar so the
    agent is told once. Returns ``{"seq", "ts", "op", "d", "hash", "restarted"}``."""
    with _locked(path):
        r = verified_read(path)
        restarted = False
        head = read_head(path)
        notice = (head or {}).get("lost_notice")
        announced = bool((head or {}).get("announced", True))
        gen = int((head or {}).get("generation", 0) or 0)
        if not r.ok and not (r.lost and allow_restart):
            raise LogError(r.error)
        if not r.ok:
            if os.path.lexists(path):
                aside, n = f"{path}.broken-{int(ts)}", 1
                while os.path.lexists(aside):          # never overwrite earlier evidence (two losses in one second)
                    n += 1
                    aside = f"{path}.broken-{int(ts)}-{n}"
                os.replace(path, aside)
            restarted, gen, notice, announced = True, gen + 1, (r.error.split(":")[0] + ": your earlier list could not be read"), False
            events: List[Dict[str, Any]] = []
            items: Dict[str, Dict[str, Any]] = {}
            next_id = r.next_id
            prev, seq = ZERO_HASH, 0
            floor = next_id
        else:
            events = r.events
            items, _ = derive(events)
            next_id = r.next_id
            prev, seq = r.head_hash, r.head_seq + 1
            floor = None
        op, d = build(items, next_id)
        out = ""
        if not events:
            hdr = {"schema_version": C.SCHEMA_VERSION, "reader_floor": C.READER_FLOOR, "note": HEADER_NOTE, "generation": gen}
            if floor is not None and floor > 1:
                hdr["next_id_floor"] = floor
            hl, prev = _line(0, ts, "header", hdr, ZERO_HASH)
            out += hl
            seq = 1
        line, h = _line(seq, ts, op, d, prev)
        out += line
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
        try:
            data = out.encode("utf-8")
            if os.write(fd, data) != len(data):
                raise LogError("short write to the open-work log")
        finally:
            os.close(fd)
        new_next = next_id
        if op == "add":
            m = re.fullmatch(r"w-(\d+)", str(d.get("id", "")))
            if m:
                new_next = max(next_id, int(m.group(1)) + 1)
        _write_head(path, seq, h, new_next, gen, notice, announced)
        return {"seq": seq, "ts": ts, "op": op, "d": d, "hash": h, "restarted": restarted}


def take_notice(path: str) -> Optional[str]:
    """The one-time 'your earlier list could not be read' notice, if there is an unannounced one; marks it announced. Never raises."""
    try:
        with _locked(path):
            head = read_head(path)
            if head is None or not head.get("lost_notice") or head.get("announced", True):
                return None
            _write_head(path, head["seq"], head["hash"], head.get("next_id", 1), int(head.get("generation", 0) or 0),
                        head["lost_notice"], True)
            return str(head["lost_notice"])
    except Exception:  # noqa: BLE001
        return None


def pending_notice(path: str) -> Optional[str]:
    """The unannounced notice without consuming it (for a read-only caller)."""
    head = read_head(path)
    if head is None or not head.get("lost_notice") or head.get("announced", True):
        return None
    return str(head["lost_notice"])


# --- derived state ---------------------------------------------------------------------------------------------------------

def _new_item(item_id: str, ts: float, d: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": item_id, "title": d.get("title", ""), "state": "open", "waiting_on": d.get("waiting_on", "self"),
        "remind_every_min": d.get("remind_every_min"), "standing": bool(d.get("standing", False)),
        "hold": bool(d.get("hold_until_return", False)), "hold_ts": ts if d.get("hold_until_return") else None,
        "blocked_by": d.get("blocked_by"), "requested_by": d.get("requested_by"), "note": d.get("note"),
        "opened": ts, "closed": None, "close_note": None, "last_update": ts, "last_remind": None,
        "reminds_since_update": 0, "remind_ts": [],
        # denials in a row (a reservation the gate refused); reset by a delivered reminder and by an update
        "deny_streak": 0, "last_deny": None, "deny_reason": None, "_streak_before": 0,
    }


def derive(events: List[Dict[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], int]:
    """``(items by id in opening order, number of adds in this generation)`` from verified events. Pure."""
    items: Dict[str, Dict[str, Any]] = {}
    adds = 0
    for ev in events:
        op, d, ts = ev["op"], ev["d"], ev["ts"]
        if op == "add":
            adds += 1
            items[d["id"]] = _new_item(d["id"], ts, d)
        elif op == "update":
            for i in d.get("ids", []):
                it = items.get(i)
                if it is None or it["state"] != "open":
                    continue
                chg = d.get("changes", {})
                for k in ("title", "waiting_on", "remind_every_min", "standing", "note", "blocked_by", "requested_by"):
                    if k in chg:
                        it[k] = chg[k]
                if "hold_until_return" in chg:
                    it["hold"] = bool(chg["hold_until_return"])
                    it["hold_ts"] = ts if it["hold"] else None
                it["last_update"] = ts
                it["reminds_since_update"] = 0
                it["deny_streak"], it["last_deny"], it["deny_reason"] = 0, None, None
        elif op in ("done", "drop"):
            it = items.get(d.get("id"))
            if it is not None and it["state"] == "open":
                it["state"] = "done" if op == "done" else "dropped"
                it["closed"] = ts
                it["close_note"] = d.get("note") or d.get("reason")
        elif op == "remind":
            for i in d.get("ids", []):
                it = items.get(i)
                if it is not None and it["state"] == "open":
                    it["last_remind"] = ts
                    it["reminds_since_update"] += 1
                    it["remind_ts"].append(ts)
                    it["_streak_before"], it["deny_streak"], it["deny_reason"], it["last_deny"] = it["deny_streak"], 0, None, None   # delivered unless un-marked
        elif op == "unremind":
            # A reservation that was NOT delivered (gate deny / defer, a write refusal): it no longer counts toward the pause, toward the
            # 24 h count, or as the last reminder. ``d["at"]`` is the reservation's own ts.
            at = d.get("at")
            for i in d.get("ids", []):
                it = items.get(i)
                if it is None or it["state"] != "open" or at not in it["remind_ts"]:
                    continue
                k = len(it["remind_ts"]) - 1 - it["remind_ts"][::-1].index(at)
                del it["remind_ts"][k]
                if at >= it["last_update"]:
                    it["reminds_since_update"] = max(0, it["reminds_since_update"] - 1)
                    it["deny_streak"] = it["_streak_before"] + 1
                    it["last_deny"] = ts
                    it["deny_reason"] = d.get("why") if isinstance(d.get("why"), str) else None
                it["last_remind"] = it["remind_ts"][-1] if it["remind_ts"] else None
    return items, adds


class Store:
    """One agent's list. Bound to ONE instance id; it can only ever touch that agent's file."""

    def __init__(self, data_root: str, instance_id: str) -> None:
        self.instance_id = instance_id
        self.path = log_path(data_root, instance_id)

    def read(self, readonly: bool = False) -> Tuple[LogRead, Dict[str, Dict[str, Any]], int]:
        """``(LogRead, items, next_id)``; items are empty when the read is not ok. Taken under a SHARED lock, so a read never sees a
        write in flight (a torn last line would otherwise read as 'not intact'). ``readonly=True`` (the web reads) creates NOTHING: it takes
        the shared lock only if the lock file already exists; with none, no writer has ever run here, so the log is read as it is, and a
        read that is not intact is retried once under the lock if one has appeared meanwhile (a first write racing the read)."""
        if readonly:
            try:
                with _locked(self.path, shared=True, create=False):
                    r = verified_read(self.path)
            except FileNotFoundError:
                r = verified_read(self.path)
                if not r.ok and not r.lost and os.path.lexists(self.path + ".lock"):
                    try:
                        with _locked(self.path, shared=True, create=False):
                            r = verified_read(self.path)
                    except (LogError, OSError):
                        pass
            except LogError as exc:
                r = LogRead(ok=False, error=str(exc), exists=True)
            except OSError:
                r = verified_read(self.path)
        elif os.path.isdir(os.path.dirname(self.path)):
            try:
                with _locked(self.path, shared=True):
                    r = verified_read(self.path)
            except LogError as exc:
                r = LogRead(ok=False, error=str(exc), exists=True)
            except OSError:
                r = verified_read(self.path)
        else:
            r = verified_read(self.path)
        if not r.ok:
            return r, {}, r.next_id
        items, _adds = derive(r.events)
        return r, items, r.next_id

    def transact(self, ts: float, build: Builder, *, allow_restart: bool = False) -> Dict[str, Any]:
        return transact(self.path, ts, build, allow_restart=allow_restart)

    def append(self, op: str, d: Dict[str, Any], ts: float) -> Dict[str, Any]:
        """Append one event whose body needs no checking (a reminder record)."""
        return transact(self.path, ts, lambda items, nid: (op, d))

    def take_notice(self) -> Optional[str]:
        return take_notice(self.path)

    def pending_notice(self) -> Optional[str]:
        return pending_notice(self.path)

"""the deliverable ledger: what has each agent written or changed, and has the operator looked?

For every harnessed session this module tails the session's OWN transcript chain
(D40, via :func:`gnomon.harness.transcript_search.resolve_chain`, reused, not
re-implemented) for ``Write`` / ``Edit`` / ``MultiEdit`` / ``NotebookEdit`` ``tool_use``
records whose ``tool_result`` succeeded, and keeps one ledger ENTRY per (session, path).
A per-viewer "seen" store marks which entries the operator has looked at.

DESIGN CHOICES (each labelled; the roadmap node is silent on all of them)
----------------------------------------------------------------------------------
CHOICE-1  WHERE IT RUNS. On-request, incremental, in the web-server process, under a
          wall-clock DEADLINE (``DEFAULT_DEADLINE_SECONDS``, clamped to
          ``[DEADLINE_FLOOR, DEADLINE_CEILING]``). No daemon, no thread, no new process:
          a per-file byte-offset checkpoint makes every pass resume where the last one
          stopped, so the steady-state cost of a poll is one ``stat`` per transcript and
          a deadline cut only delays the OLDEST sessions' tails (newest sessions first).
CHOICE-2  NEW vs MODIFIED. An entry's ``action`` is decided ONCE, when the entry is
          first created: ``new`` iff the tool is ``Write`` AND the path has no earlier
          SUCCESSFUL ``Read`` in this session's chain AND the path has no earlier entry;
          everything else (``Edit`` / ``MultiEdit`` / ``NotebookEdit``, or a ``Write``
          after a ``Read``) is ``modified``. Later touches of the same path only bump
          ``count`` / ``last_ts`` / ``last_tool``; a ``new`` entry stays ``new``
          (the session produced this file). The ``File created successfully`` result
          text is deliberately NOT used: the rule is a property of the transcript's
          tool_use sequence, not of a message string a Claude Code upgrade may reword.
CHOICE-3  SEEN MODEL. Per viewer, per entry: the entry's ``last_ts`` (epoch) AT THE
          MOMENT OF MARKING, taken server-side from the ledger (never from the client).
          An entry is UNSEEN for a viewer iff it was never marked OR it was written
          again after the mark (``last_ts > marked``). Viewer ids are names, not
          credentials (this is a local single-operator dashboard): default ``operator``.
CHOICE-4  ENTRY ID. ``sha1(session '\\0' path)[:16]``: stable across rescans/resets.
CHOICE-5  READ-ONLY ON THE DELIVERABLES. Paths are shown; the files themselves are
          NEVER opened, rendered, hashed or copied here (a test pins this). Paths are
          not even ``realpath``-ed: they are strings from the transcript.
CHOICE-7  SUBAGENT CHAINS (round 2). Every session's transcript file(s) are tailed together with
          their subagent chains: ``<transcript stem>/subagents/agent-<id>.jsonl`` and
          ``.../subagents/workflows/wf_*/agent-<id>.jsonl`` (layout measured on this machine,
          claude 2.1.x; ``agent-<id>.meta.json`` beside each gives agentType / description /
          spawnDepth, read once per chain). A subagent file is its OWN chain for the Read history
          behind new-vs-modified. Entries stay ONE PER (session, path); each carries a ``writers``
          list (chain, agent_id, agent_type, description, count, first/last ts). ``action`` is the
          determination of the EARLIEST commit across chains (see ``_commit``): created by a
          subagent then edited by the parent stays ``new``; edited by the parent then overwritten
          by a subagent stays ``modified``.
CHOICE-8  PERSISTENCE (round 2, P2-3). The state file is rewritten at once only for a MATERIAL
          change (entry / pending / reset / cap); a pure offset or fingerprint advance is written
          at most every ``PERSIST_MIN_INTERVAL`` seconds. A crash costs only a rescan from the
          older checkpoint, idempotent through the applied-id dedupe.
CHOICE-6  ORDER. A session's chain files are scanned OLDEST FIRST (new-vs-modified
          needs earlier Reads first); sessions are scanned NEWEST FIRST (by
          ``launch.json`` mtime).

ROBUSTNESS
----------
* g29(d2): each transcript file's byte size is PINNED once per pass; bytes appended
  while the pass runs are invisible until the next pass. Only COMPLETE lines advance the
  checkpoint (a torn trailing line waits; an unterminated tail that already parses as a
  JSON object is complete).
* Rotation / truncation: the checkpoint records (dev, inode, head sha of the first
  ``HEAD_BYTES``). A smaller file, a different inode or a different head resets the
  offset to 0 and counts a reset; tool_use ids already applied are remembered
  (``applied``) so a rescan never double-counts.
* A ``tool_use`` whose ``tool_result`` has not arrived yet stays ``pending`` in the
  checkpointed state (bounded) and is resolved when the result is read later. A result
  with ``is_error`` true discards the pending write: a FAILED WRITE IS NOT A DELIVERABLE.
* g27b: the only duration (the scan deadline) has a stated floor and ceiling; every
  size that could grow without bound (entries, reads, applied ids, pending, line bytes)
  has a stated cap, and hitting a cap is reported, never silent.

EXTENSION POINTS (the standing order; proposed checklist text in the build report)
---------------------------------------------------------------------------------
* Subagent transcript readers (rule proposed for the checklist): the path is DERIVED from the D40
  ``transcript_path`` (``discover_subagent_files``), never taken from an argument; Read state is kept
  PER AGENT CHAIN; the layout (``subagents/agent-*.jsonl``, ``workflows/wf_*``) is a Claude Code
  internal, re-verified against ``tests/test_rm4a_deliverable_ledger.py`` on an upgrade.
* ``WRITE_TOOLS`` / ``READ_TOOLS`` — the closed tool-name -> input-key maps. A new
  Claude Code file-writing tool must be added here (and to a test) or its output is
  invisible to the ledger.
* The store layout under ``<runtime root>/deliverables`` (``ledger/<session>.json``,
  ``seen.json``, ``.lock``) is a MACHINE-READ interface (g25): 4B (markdown viewer) and
  3A (morning report) read entries through :func:`list_entries`, not the files.
  ``STATE_VERSION`` / ``SEEN_VERSION`` change only together with the readers.
* The ledger is durable operator state under ``~/gnomon-data`` (g10): it is covered by
  the data-root backup only as far as that backup covers ``~/gnomon-data``; there is
  no X23 job for it yet.
* feature-id ``deliverable_ledger`` is the X26 feature_event id this feature WILL emit
  (g1); ``feature_event`` does not exist yet, so nothing is emitted.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat as _stat
import tempfile
import threading
import time
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .harness.context_readers import claude_config_dir
from .harness.operator_activity import _parse_iso
from .harness.transcript_search import resolve_chain

FLAG_NAME = "deliverable_ledger"
#: The Deliverables tab "Open" button (operator 2026-10-03). Default ON in the registry; only meaningful while
#: the ledger flag is ON. See :func:`open_entry`.
OPEN_FLAG_NAME = "deliverable_open"
#: The X26 feature-id this feature WILL emit once feature_event exists (G1).
FEATURE_ID = "deliverable_ledger"

STATE_VERSION = 2
SEEN_VERSION = 1

#: tool name -> the tool_use ``input`` key holding the path. CLOSED: see EXTENSION POINTS.
WRITE_TOOLS: Dict[str, str] = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
READ_TOOLS: Dict[str, str] = {
    "Read": "file_path",
    "NotebookRead": "notebook_path",
}
#: Byte needles for the cheap raw-line prefilter (tool names appear quoted in the JSON).
_TOOL_NEEDLES: Tuple[bytes, ...] = tuple(('"%s"' % n).encode() for n in list(WRITE_TOOLS) + list(READ_TOOLS))

DEFAULT_VIEWER = "operator"
VIEWER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
FILTERS = ("unseen", "all")
#: The ``author`` filter's value for entries whose ledger record has no author (``instance`` missing or empty).
UNKNOWN_AUTHOR = "__unknown__"
AUTHOR_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@ -]{0,254}$")

# --- bounds (g27b) -------------------------------------------------------------
DEFAULT_DEADLINE_SECONDS = 4.0
DEADLINE_FLOOR = 0.05
DEADLINE_CEILING = 60.0
#: Lines between clock reads inside one file (a deadline cut costs at most this many lines).
CHECK_EVERY_LINES = 128
HEAD_BYTES = 4096
CHUNK_BYTES = 1 << 20
MAX_LINE_BYTES = 32 << 20
MAX_PATH_CHARS = 4096
ENTRY_CAP = 10000           # per session; the oldest-by-last_ts are dropped and counted
READS_CAP = 50000           # per session (12-hex path hashes)
APPLIED_CAP = 20000         # per session (12-hex tool_use id hashes)
PENDING_CAP = 2000          # per session
SEEN_CAP = 50000            # per viewer
LIST_LIMIT_DEFAULT = 200
LIST_LIMIT_CEILING = 1000


def clamp_deadline(seconds: Any) -> float:
    """The scan deadline, clamped to ``[DEADLINE_FLOOR, DEADLINE_CEILING]`` (never disabled)."""
    try:
        v = float(seconds)
    except (TypeError, ValueError):
        return DEFAULT_DEADLINE_SECONDS
    if v != v:  # NaN
        return DEFAULT_DEADLINE_SECONDS
    return max(DEADLINE_FLOOR, min(DEADLINE_CEILING, v))


def default_runtime_root() -> str:
    """The DATA root (X13 S5-4: ``config.runtime_root_default``: the per-user roots file's ``data_root`` when one is in
    play, else the legacy default)."""
    from .config import runtime_root_default

    return runtime_root_default()


def default_store_root() -> str:
    """``<data_root>/deliverables`` (durable operator state under the runtime root, g10)."""
    return os.path.join(default_runtime_root(), "deliverables")


def default_session_roots() -> List[str]:
    """Where harnessed session dirs live: ``<data_root>/sessions`` (each has ``.claude/launch.json``)."""
    from .config import sessions_root_default

    return [sessions_root_default()]


class LedgerError(Exception):
    """A fail-loud refusal (bad viewer / session / parameter)."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --- small helpers ---------------------------------------------------------------


def _h12(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8", errors="replace")).hexdigest()[:12]


def entry_id(session: str, path: str) -> str:
    return hashlib.sha1((session + "\0" + path).encode("utf-8", errors="replace")).hexdigest()[:16]


def _norm_path(raw: Any, cwd: Any) -> Optional[str]:
    if not isinstance(raw, str) or not raw or "\0" in raw or len(raw) > MAX_PATH_CHARS:
        return None
    p = raw
    if not os.path.isabs(p):
        if isinstance(cwd, str) and os.path.isabs(cwd):
            p = os.path.join(cwd, p)
        else:
            return os.path.normpath(p)
    return os.path.normpath(p)


def _atomic_write_json(path: str, obj: Any) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=os.path.basename(path) + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


_THREAD_LOCK = threading.RLock()


@contextlib.contextmanager
def _locked(store_root: str) -> Iterator[None]:
    """Serialise ledger writers: a process-wide RLock plus an ``flock`` for other processes."""
    with _THREAD_LOCK:
        os.makedirs(store_root, mode=0o700, exist_ok=True)
        fh = open(os.path.join(store_root, ".lock"), "a+")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            with contextlib.suppress(OSError):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            fh.close()


# --- per-session state ------------------------------------------------------------
#
# State v2 (4A round 2): entries are still ONE PER (session, path), but every transcript file
# belongs to a CHAIN: ``main`` (the D40 chain) or one subagent (``agent-<id>.jsonl``). The Read
# history behind new-vs-modified is PER CHAIN (a subagent's Reads never leak into the parent's or a
# sibling's). tool_use ids are globally unique, so ``applied`` and ``pending`` are per session.

MAIN_CHAIN = "main"
#: Subagent chain files are found under ``<transcript stem>/subagents`` (and ``workflows/wf_*``
#: beneath it), at most this many directory levels below it, at most this many files per session.
SUBAGENT_MAX_DEPTH = 8       # a practical 'any depth'; hitting it is REPORTED (caps_hit + warning)
SUBAGENT_FILES_CAP = 5000    # per SESSION (all chain files combined); hitting it is REPORTED
META_MAX_BYTES = 64 * 1024   # a larger meta.json is skipped with a warning
META_TEXT_CHARS = 200        # agentType and description are each cut to this
DISCOVERY_CHECK_EVERY = 256  # files between deadline checks during discovery
WARNINGS_CAP = 50            # API warnings list bound
WRITERS_CAP = 20            # per entry; evicted writers' counts live on in writers_overflow_count
#: Pure offset advances are persisted at most this often (seconds); material changes at once (P2-3).
PERSIST_MIN_INTERVAL = 60.0
_AGENT_FILE_RE = re.compile(r"^agent-([A-Za-z0-9][A-Za-z0-9_-]{0,127})\.jsonl$")
#: Test seam: the wall clock used ONLY for the persist throttle.
_wall = time.time


def _new_state(session: str, session_dir: str, instance: Optional[str]) -> Dict[str, Any]:
    return {"v": STATE_VERSION, "session": session, "session_dir": session_dir,
            "instance": instance, "files": {}, "chains": {}, "entries": {}, "pending": {},
            "applied": [], "resets": 0, "entries_dropped": 0, "caps_hit": [],
            "fingerprint": None, "last_error": None, "updated": None}


def _state_path(store_root: str, session: str) -> str:
    return os.path.join(store_root, "ledger", session + ".json")


class _Live:
    """A state dict plus the set views the scanner needs for O(1) membership.

    ``material`` = an entry, a pending tool_use, a reset or a cap changed (persist NOW);
    ``dirty`` = anything at all changed, including pure offset advances (persist within
    ``PERSIST_MIN_INTERVAL``). Everything not persisted is re-derivable by rescanning from the
    older checkpoint: ``applied`` makes that idempotent."""

    def __init__(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.applied = set(state["applied"])
        self._reads: Dict[str, set] = {}
        self.dirty = False
        self.material = False

    def chain(self, cid: str) -> Dict[str, Any]:
        return self.state["chains"].setdefault(cid, {"reads": []})

    def reads(self, cid: str) -> set:
        if cid not in self._reads:
            self._reads[cid] = set(self.chain(cid)["reads"])
        return self._reads[cid]

    def add_read(self, cid: str, path: str) -> None:
        h = _h12(path)
        rs = self.reads(cid)
        if h not in rs:
            rs.add(h)
            lst = self.chain(cid)["reads"]
            lst.append(h)
            self.dirty = True
            if len(lst) > READS_CAP:
                drop = lst[: len(lst) - READS_CAP]
                del lst[: len(drop)]
                rs.difference_update(drop)
                _cap(self.state, "reads")
                self.material = True

    def mark_applied(self, tool_use_id: str) -> None:
        h = _h12(tool_use_id)
        if h not in self.applied:
            self.applied.add(h)
            self.state["applied"].append(h)
            self.dirty = True
            if len(self.state["applied"]) > APPLIED_CAP:
                drop = self.state["applied"][: len(self.state["applied"]) - APPLIED_CAP]
                del self.state["applied"][: len(drop)]
                self.applied.difference_update(drop)
                _cap(self.state, "applied")
                self.material = True

    def is_applied(self, tool_use_id: str) -> bool:
        return _h12(tool_use_id) in self.applied


def _cap(state: Dict[str, Any], name: str) -> None:
    if name not in state["caps_hit"]:
        state["caps_hit"].append(name)


def load_state(store_root: str, session: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(state, warning)``. A corrupt state file is moved aside (never silently reused) and
    the caller rebuilds from offset 0; entries already applied are simply re-derived."""
    p = _state_path(store_root, session)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError, RecursionError) as exc:
        aside = p + ".corrupt-%d" % int(time.time())
        with contextlib.suppress(OSError):
            os.replace(p, aside)
        return None, f"ledger state for {session} was unreadable ({type(exc).__name__}); moved aside, rebuilding"
    if not isinstance(obj, dict) or obj.get("v") != STATE_VERSION or not isinstance(obj.get("entries"), dict):
        aside = p + ".badversion-%d" % int(time.time())
        with contextlib.suppress(OSError):
            os.replace(p, aside)
        return None, f"ledger state for {session} had an unknown schema; moved aside, rebuilding"
    for k, default in (("files", {}), ("chains", {}), ("pending", {}), ("applied", []), ("caps_hit", [])):
        if not isinstance(obj.get(k), type(default)):
            obj[k] = type(default)()
    obj.setdefault("resets", 0)
    obj.setdefault("entries_dropped", 0)
    return obj, None


def save_state(store_root: str, state: Dict[str, Any]) -> None:
    state["updated"] = _wall()
    _atomic_write_json(_state_path(store_root, state["session"]), state)


# --- record application -----------------------------------------------------------


def _content_blocks(rec: Dict[str, Any]) -> List[Dict[str, Any]]:
    msg = rec.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _apply_record(live: _Live, rec: Dict[str, Any], chain_id: str = MAIN_CHAIN) -> None:
    state = live.state
    t = rec.get("type")
    if t == "assistant":
        for b in _content_blocks(rec):
            if b.get("type") != "tool_use":
                continue
            name, tid = b.get("name"), b.get("id")
            if not isinstance(tid, str) or not isinstance(name, str):
                continue
            key = WRITE_TOOLS.get(name) or READ_TOOLS.get(name)
            if key is None or live.is_applied(tid):
                continue
            inp = b.get("input")
            raw = inp.get(key) if isinstance(inp, dict) else None
            path = _norm_path(raw, rec.get("cwd"))
            if path is None:
                if isinstance(raw, str) and raw:
                    _cap(state, "path-length-or-nul")    # g27b: a dropped path is REPORTED, never silent
                    live.material = True
                continue
            ts = rec.get("timestamp") if isinstance(rec.get("timestamp"), str) else None
            state["pending"][tid] = {"tool": name, "path": path, "ts": ts, "chain": chain_id,
                                     "ts_epoch": _parse_iso(ts),
                                     "uuid": rec.get("uuid") if isinstance(rec.get("uuid"), str) else None}
            live.dirty = live.material = True
            if len(state["pending"]) > PENDING_CAP:
                for k in list(state["pending"])[: len(state["pending"]) - PENDING_CAP]:
                    del state["pending"][k]
                _cap(state, "pending")
    elif t == "user":
        if not state["pending"]:
            return
        for b in _content_blocks(rec):
            if b.get("type") != "tool_result":
                continue
            tid = b.get("tool_use_id")
            p = state["pending"].pop(tid, None) if isinstance(tid, str) else None
            if p is None:
                continue
            live.dirty = live.material = True
            live.mark_applied(tid)
            if b.get("is_error") is True:
                continue                       # a FAILED write/read is nothing
            if p["tool"] in READ_TOOLS:
                live.add_read(p.get("chain") or MAIN_CHAIN, p["path"])
            else:
                _commit(live, p)


def _writer_for(state: Dict[str, Any], cid: str) -> Dict[str, Any]:
    meta = state["chains"].get(cid) or {}
    return {"chain": cid, "agent_id": None if cid == MAIN_CHAIN else cid,
            "agent_type": meta.get("agent_type"), "description": meta.get("description"),
            "depth": meta.get("depth", 0 if cid == MAIN_CHAIN else None)}


def _commit(live: _Live, p: Dict[str, Any]) -> None:
    """Fold one SUCCESSFUL write into the (session, path) entry.

    ``action`` (CHOICE-2, merged across chains): it is the determination of the EARLIEST commit,
    where a commit is ``new`` iff it is a ``Write`` with no earlier successful Read of the path IN
    ITS OWN CHAIN. So a file a subagent created and the parent later edited stays ``new``; a file
    the parent edited before a subagent overwrote it stays ``modified``.

    "Earliest" is decided on TIMESTAMPS only when BOTH commits carry one, by the key
    ``(timestamp, main-before-subagent, agent_id)``: equal timestamps are broken deterministically,
    main chain first, then agent_id. A commit WITHOUT a timestamp never overrides, and is never
    overridden by, a timed one on time grounds: SCAN ORDER decides (main first, then subagents
    newest file first), i.e. the first-scanned commit stays the entry's first. Clock skew between
    chains is not detected. ``count`` always equals the sum of the writers' counts plus
    ``writers_overflow_count`` (writes by writers evicted at WRITERS_CAP)."""
    state = live.state
    session = state["session"]
    cid = p.get("chain") or MAIN_CHAIN
    eid = entry_id(session, p["path"])
    ts, ep = p.get("ts"), p.get("ts_epoch")
    is_new = p["tool"] == "Write" and _h12(p["path"]) not in live.reads(cid)
    key = (ep, 0 if cid == MAIN_CHAIN else 1, cid)
    ent = state["entries"].get(eid)
    if ent is None:
        ent = {"id": eid, "session": session, "instance": state.get("instance"), "path": p["path"],
               "action": "new" if is_new else "modified", "first_chain": cid,
               "first_seen_ts": ts, "first_ts_epoch": ep, "last_ts": ts, "last_ts_epoch": ep,
               "count": 0, "uuid": p.get("uuid"), "first_uuid": p.get("uuid"), "last_tool": p["tool"],
               "writers": [], "writers_dropped": 0, "writers_overflow_count": 0}
        state["entries"][eid] = ent
        if len(state["entries"]) > ENTRY_CAP:
            order = sorted(state["entries"].values(), key=lambda e: (e.get("last_ts_epoch") or 0.0, e["id"]))
            for e in order[: len(state["entries"]) - ENTRY_CAP]:
                del state["entries"][e["id"]]
                state["entries_dropped"] += 1
            _cap(state, "entries")
    else:
        fe = ent.get("first_ts_epoch")
        fkey = (fe, 0 if ent.get("first_chain", MAIN_CHAIN) == MAIN_CHAIN else 1, ent.get("first_chain", MAIN_CHAIN))
        if ep is not None and fe is not None and key < fkey:      # strictly earlier, both timed
            ent["action"] = "new" if is_new else "modified"
            ent["first_chain"] = cid
            ent["first_seen_ts"], ent["first_ts_epoch"], ent["first_uuid"] = ts, ep, p.get("uuid")
        if ep is not None and (ent.get("last_ts_epoch") is None or ep >= ent["last_ts_epoch"]):
            ent["last_ts"], ent["last_ts_epoch"] = ts, ep
            ent["uuid"], ent["last_tool"] = p.get("uuid") or ent.get("uuid"), p["tool"]
    ent["count"] += 1
    w = next((x for x in ent["writers"] if x["chain"] == cid), None)
    if w is None:
        w = dict(_writer_for(state, cid), count=0, first_ts=ts, last_ts=ts)
        ent["writers"].append(w)
        if len(ent["writers"]) > WRITERS_CAP:
            lo = min((i for i, x in enumerate(ent["writers"]) if x is not w),
                     key=lambda i: (ent["writers"][i]["count"], ent["writers"][i]["chain"]))
            ent["writers_overflow_count"] += ent["writers"][lo]["count"]
            del ent["writers"][lo]
            ent["writers_dropped"] += 1
            _cap(state, "writers")
    w["count"] += 1
    w["last_ts"], w["last_tool"] = ts, p["tool"]
    live.dirty = live.material = True


# --- the incremental file tail ----------------------------------------------------


def _head(fh, n: int) -> Tuple[bytes, str]:
    fh.seek(0)
    b = fh.read(n) if n > 0 else b""
    return b, hashlib.sha256(b).hexdigest()


def _iter_lines(fh, start: int, end: int) -> Iterator[Tuple[Optional[bytes], int, bool]]:
    """Yield ``(line_or_None, offset_after_line, terminated)`` over EXACTLY ``[start, end)``.
    ``None`` = an oversized line that was skipped (never buffered beyond MAX_LINE_BYTES).
    The final unterminated fragment (if any) comes last with ``terminated=False``."""
    fh.seek(start)
    pos = start
    base = start
    buf = b""
    skipping = False
    while pos < end:
        data = fh.read(min(CHUNK_BYTES, end - pos))
        if not data:
            break
        pos += len(data)
        buf += data
        i0 = 0
        while True:
            j = buf.find(b"\n", i0)
            if j < 0:
                break
            line = buf[i0:j]
            base += (j - i0) + 1
            i0 = j + 1
            if skipping:
                skipping = False
                yield None, base, True
            else:
                yield line, base, True
        buf = buf[i0:]
        if len(buf) > MAX_LINE_BYTES and not skipping:
            skipping = True
            base += len(buf)
            buf = b""
    if buf and not skipping:
        yield buf, base + len(buf), False


def scan_transcript_file(live: _Live, fpath: str, *, expired: Callable[[], bool],
                         after_pin: Optional[Callable[[], None]] = None,
                         chain_id: str = MAIN_CHAIN) -> Dict[str, Any]:
    """Advance ``fpath``'s checkpoint as far as the deadline allows. Never raises on I/O:
    returns ``{status, ...}`` (``ok`` | ``deadline`` | ``unreadable: <Err>``)."""
    state = live.state
    out: Dict[str, Any] = {"file": fpath, "chain": chain_id, "status": "ok", "lines": 0, "reset": None}
    try:
        st = os.lstat(fpath)
        if not _stat.S_ISREG(st.st_mode):         # only REGULAR files are ever opened (a FIFO would block forever)
            out.update(status="ok", skipped=True, warning=f"{os.path.basename(fpath)}: not a regular file; skipped")
            return out
        pinned = st.st_size                       # g29(d2): pinned ONCE, before any read
    except OSError as exc:
        out["status"] = f"unreadable: {type(exc).__name__}"
        return out
    cp = state["files"].get(fpath)
    if (cp is not None and cp.get("offset") == pinned == cp.get("size_seen")
            and cp.get("mtime_ns") == st.st_mtime_ns and cp.get("inode") == st.st_ino
            and cp.get("dev") == st.st_dev and after_pin is None):
        out.update(offset=pinned, pinned=pinned, unchanged=True)      # fully consumed, untouched: no open
        return out
    if after_pin is not None:
        after_pin()                               # test seam: growth AFTER the pin
    try:
        with _open_regular(fpath) as fh:
            if cp is not None:
                reason = None
                if cp.get("inode") != st.st_ino or cp.get("dev") != st.st_dev:
                    reason = "replaced"
                elif pinned < cp.get("offset", 0):
                    reason = "truncated"
                elif cp.get("offset", 0) > 0:
                    _b, sha = _head(fh, int(cp.get("head_len", 0)))
                    if sha != cp.get("head_sha"):
                        reason = "replaced"
                if reason:
                    out["reset"] = reason
                    state["resets"] += 1
                    for k in [k for k, v in state["pending"].items() if v.get("chain") == chain_id]:
                        del state["pending"][k]
                    cp = None
                    live.dirty = live.material = True
            offset = cp["offset"] if cp else 0
            consumed = offset
            n = 0
            if offset < pinned:
                for line, nxt, terminated in _iter_lines(fh, offset, pinned):
                    if line is None:
                        consumed = nxt
                        continue
                    if not terminated:
                        try:
                            rec = json.loads(line)
                        except (ValueError, RecursionError):
                            break                  # a torn tail: wait for the rest
                        if not isinstance(rec, dict):
                            break
                        _apply_record(live, rec, chain_id)
                        consumed = nxt
                        n += 1
                        break
                    consumed = nxt
                    n += 1
                    if _maybe_relevant(line, state):
                        try:
                            rec = json.loads(line)
                        except (ValueError, RecursionError):
                            rec = None
                        if isinstance(rec, dict):
                            _apply_record(live, rec, chain_id)
                    if n % CHECK_EVERY_LINES == 0 and expired():
                        out["status"] = "deadline"
                        break
            head_len = min(HEAD_BYTES, consumed)
            _b, sha = _head(fh, head_len)
            new_cp = {"offset": consumed, "head_len": head_len, "head_sha": sha, "chain": chain_id,
                      "inode": st.st_ino, "dev": st.st_dev, "size_seen": pinned, "mtime_ns": st.st_mtime_ns}
            if cp is None or any(cp.get(k) != v for k, v in new_cp.items()):
                state["files"][fpath] = new_cp
                live.dirty = True
            out["lines"] = n
            out["offset"] = consumed
            out["pinned"] = pinned
    except OSError as exc:
        out["status"] = f"unreadable: {type(exc).__name__}"
    return out


def _open_regular(path: str):
    """Open ``path`` read-only WITHOUT blocking and without following a final symlink; refuse (OSError)
    anything that is not a regular file at the moment of opening (closes the lstat->open race)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not _stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _maybe_relevant(raw: bytes, state: Dict[str, Any]) -> bool:
    """Cheap prefilter: only parse a line that could carry a tool_use for a tracked tool, or
    (when something is pending) any line at all that mentions a tool_result."""
    if b'"tool_use"' in raw:
        return any(n in raw for n in _TOOL_NEEDLES)
    return bool(state["pending"]) and b'"tool_result"' in raw


# --- session + fleet scanning ----------------------------------------------------


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError, RecursionError):
        return None
    return obj if isinstance(obj, dict) else None


def discover_subagent_files(main_path: str, projects_root: str, *, budget: int = SUBAGENT_FILES_CAP,
                            expired: Optional[Callable[[], bool]] = None) -> Dict[str, Any]:
    """``{files: [{path, agent_id, meta_path}], caps: [...], cut: bool}``: subagent chain files under
    ``<main_path minus .jsonl>/subagents`` (measured layout on claude 2.1.x: ``agent-<id>.jsonl`` +
    ``agent-<id>.meta.json``, and ``workflows/wf_*/agent-<id>.jsonl`` beneath it). Nested (depth>1)
    subagents live in the same tree and are included the same way, down to SUBAGENT_MAX_DEPTH
    directory levels. Every file must resolve under ``projects_root`` (scope hygiene); symlinked
    directories are not descended. ``budget`` is how many files this call may still return (the
    per-SESSION cap minus what earlier main files found). ``caps`` names every limit that was hit
    (``subagent-depth``, ``subagent-files``) and ``cut`` says the deadline stopped the walk: neither
    is ever silent."""
    base = main_path[:-6] if main_path.endswith(".jsonl") else main_path
    root = os.path.join(base, "subagents")
    res: Dict[str, Any] = {"files": [], "caps": [], "cut": False, "notes": []}
    if not os.path.isdir(root):
        return res
    root_depth = root.rstrip(os.sep).count(os.sep)
    verified = 0
    for dirpath, dirnames, filenames in os.walk(root):
        if dirpath.rstrip(os.sep).count(os.sep) - root_depth >= SUBAGENT_MAX_DEPTH:
            if dirnames and "subagent-depth" not in res["caps"]:
                res["caps"].append("subagent-depth")
            dirnames[:] = []
        dirnames.sort()
        for fn in sorted(filenames):
            m = _AGENT_FILE_RE.match(fn)
            if not m:
                continue
            p = os.path.join(dirpath, fn)
            if (projects_root, p) not in _VERIFIED:
                # The expensive step (two realpaths). Progress guarantee for a tiny deadline: the
                # clock is read only every DISCOVERY_CHECK_EVERY *new* verifications and verified
                # paths are remembered, so every cut pass extends the verified set and the walk
                # converges instead of restarting from zero.
                verified += 1
                if expired is not None and verified % DISCOVERY_CHECK_EVERY == 0 and expired():
                    res["cut"] = True
                    return res
                try:
                    regular = _stat.S_ISREG(os.lstat(p).st_mode)      # never a FIFO / socket / device / symlink
                except OSError:
                    continue
                if not regular:
                    res["notes"].append(f"{fn}: not a regular file; skipped")
                    continue
                if not _under_root(p, projects_root):
                    continue
                if len(_VERIFIED) >= VERIFIED_CAP:
                    _VERIFIED.clear()
                _VERIFIED.add((projects_root, p))
            if len(res["files"]) >= budget:
                if "subagent-files" not in res["caps"]:
                    res["caps"].append("subagent-files")
                return res
            res["files"].append({"path": p, "agent_id": m.group(1), "meta_path": p[:-6] + ".meta.json"})
    return res


#: In-memory memo of subagent files already realpath-checked under their projects root (process
#: lifetime only; a symlink swapped in AFTER verification is not re-detected until restart).
_VERIFIED: set = set()
VERIFIED_CAP = 50000


def _under_root(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def _read_meta(meta_path: str, projects_root: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(meta_or_None, warning_or_None)``: the sidecar is attacker-influenced text, so it is read
    only when it is a REGULAR file (never a symlink) that resolves under the projects root and is
    at most META_MAX_BYTES; a parse error (including RecursionError) means 'unknown'."""
    name = os.path.basename(meta_path)
    try:
        st = os.lstat(meta_path)
    except FileNotFoundError:
        return None, None                     # no sidecar: attribution unknown, nothing to report
    except OSError as exc:
        return None, f"{name}: unreadable ({type(exc).__name__}); attribution unknown"
    if _stat.S_ISLNK(st.st_mode):
        return None, f"{name}: is a symlink; not followed, attribution unknown"
    if not _stat.S_ISREG(st.st_mode) or not _under_root(meta_path, projects_root):
        return None, f"{name}: not a regular file under the projects dir; skipped"
    if st.st_size > META_MAX_BYTES:
        return None, f"{name}: {st.st_size} bytes exceeds {META_MAX_BYTES}; skipped, attribution unknown"
    try:
        with _open_regular(meta_path) as fh:
            obj = json.loads(fh.read(META_MAX_BYTES + 1).decode("utf-8", errors="strict"))
    except (OSError, ValueError, RecursionError):   # UnicodeDecodeError is a ValueError
        return None, f"{name}: not valid JSON; attribution unknown"
    if not isinstance(obj, dict):
        return None, f"{name}: not a JSON object; attribution unknown"
    return obj, None


def _record_chain_meta(live: _Live, agent_id: str, meta_path: str, projects_root: str,
                       warnings: List[str]) -> None:
    """Attribution (type/description/depth) from the sidecar ``*.meta.json``, once per chain; each
    text field is cut to META_TEXT_CHARS."""
    ch = live.chain(agent_id)
    if ch.get("meta_read"):
        return
    m, warn = _read_meta(meta_path, projects_root)
    ch["meta_warning"] = warn                 # sticky: re-surfaced on every pass (see _meta_warnings)
    m = m or {}
    at, d = m.get("agentType"), m.get("description")
    ch["agent_type"] = at[:META_TEXT_CHARS] if isinstance(at, str) else None
    ch["description"] = d[:META_TEXT_CHARS] if isinstance(d, str) else None
    ch["depth"] = m.get("spawnDepth") if isinstance(m.get("spawnDepth"), int) else None
    ch["meta_read"] = bool(m)
    live.dirty = True


def _meta_warnings(state: Dict[str, Any]) -> List[str]:
    """The stored sidecar problems of this session's chains: reported on EVERY pass (also the
    fast 'unchanged' one), so a bad meta.json never goes quiet after its first read."""
    return [str(c["meta_warning"]) for c in state["chains"].values() if c.get("meta_warning")]


def _fingerprint(session_dir: str, files: Sequence[str]) -> List[Any]:
    fp: List[Any] = []
    try:
        fp.append(os.stat(os.path.join(session_dir, ".claude", "launch.json")).st_mtime_ns)
    except OSError:
        fp.append(None)
    for f in files:
        try:
            s = os.stat(f)
            fp.append([f, s.st_size, s.st_mtime_ns, s.st_ino])
        except OSError:
            fp.append([f, None])
    return fp


def scan_session(store_root: str, session_dir: str, *, projects_root: Optional[str] = None,
                 expired: Callable[[], bool], after_pin: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """Incrementally tail one session's chains (main + every subagent) into its ledger. Never
    raises for a bad session: the outcome is in the returned summary (``status``)."""
    session_dir = os.path.abspath(session_dir)
    session = os.path.basename(session_dir.rstrip(os.sep))
    summ: Dict[str, Any] = {"session": session, "status": "ok", "files": [], "warnings": []}
    if not SESSION_RE.match(session):
        summ["status"] = "skipped: unusable session directory name"
        return summ
    launch = _read_json(os.path.join(session_dir, ".claude", "launch.json"))
    instance = launch.get("instance_id") if isinstance(launch, dict) and isinstance(launch.get("instance_id"), str) else None
    summ["instance"] = instance
    with _locked(store_root):
        state, warn = load_state(store_root, session)
        if warn:
            summ["warnings"].append(warn)
        if state is None:
            state = _new_state(session, session_dir, instance)
        state["session_dir"], state["instance"] = session_dir, instance or state.get("instance")
        caps_before = list(state["caps_hit"])
        chain = resolve_chain(session_dir, projects_root=projects_root)
        if not chain.get("ok"):
            summ["status"] = "chain-error: " + str(chain.get("error_kind"))
            summ["error"] = chain.get("error")
            state["last_error"] = {"kind": chain.get("error_kind"), "error": chain.get("error")}
            if warn or not os.path.exists(_state_path(store_root, session)):
                _persist(store_root, state, summ, force=True)
            return summ
        state["last_error"] = None
        main_files = [f for f in chain["files"] if f.get("exists")]
        summ["warnings"].extend(chain.get("warnings") or [])       # g30b(g): EVERY scan-level warning reaches the API
        proot = projects_root if projects_root is not None else os.path.join(claude_config_dir(), "projects")
        subs: List[Dict[str, Any]] = []
        disc_cut = False
        for f in main_files:
            found = discover_subagent_files(f["path"], proot, budget=max(0, SUBAGENT_FILES_CAP - len(subs)),
                                            expired=expired)
            subs.extend(found["files"])
            summ["warnings"].extend(found["notes"])
            for c in found["caps"]:
                _cap(state, c)
                summ["warnings"].append(
                    "subagent directory depth cap (%d) reached; deeper chains are not tailed" % SUBAGENT_MAX_DEPTH
                    if c == "subagent-depth" else
                    "subagent file cap (%d per session) reached; some subagent chains are not tailed" % SUBAGENT_FILES_CAP)
            if found["cut"]:
                disc_cut = True
                summ["warnings"].append("deadline reached while discovering subagent chains; the ledger is incomplete this pass")
                break
        # newest subagent files first: a deadline cut leaves the oldest, quietest chains for later
        def _mt(x):
            try:
                return -os.stat(x["path"]).st_mtime
            except OSError:
                return 0.0
        subs.sort(key=lambda x: (_mt(x), x["path"]))
        plan: List[Tuple[str, str, Optional[Dict[str, Any]]]] = (
            [(f["path"], MAIN_CHAIN, None) for f in main_files] + [(s["path"], s["agent_id"], s) for s in subs])
        summ["subagent_files"] = len(subs)
        fp = _fingerprint(session_dir, [p for p, _c, _s in plan])
        if state.get("fingerprint") == fp and not warn:
            summ["status"] = "unchanged"
            summ["warnings"].extend(_meta_warnings(state))
            if state["caps_hit"] != caps_before:
                _persist(store_root, state, summ, force=True)
            return summ
        live = _Live(state)
        complete = not disc_cut
        opened = False
        for path, cid, sub in plan:                    # main oldest-first, then subagents (CHOICE-6)
            if opened and expired():                   # F4: also BETWEEN files (progress: >=1 file opened per pass)
                complete = False
                summ["warnings"].append("deadline reached between chain files; the ledger is incomplete this pass")
                break
            if sub is not None:
                _record_chain_meta(live, cid, sub["meta_path"], proot, summ["warnings"])
            r = scan_transcript_file(live, path, expired=expired, after_pin=after_pin, chain_id=cid)
            opened = opened or not r.get("unchanged")
            summ["files"].append(r)
            if r.get("skipped"):
                summ["warnings"].append(r["warning"])
                continue
            if r["status"] != "ok":
                complete = False
                if r["status"] == "deadline":
                    break
            if r.get("reset"):
                summ["warnings"].append(f"{os.path.basename(path)}: checkpoint reset ({r['reset']})")
        # The PRE-scan fingerprint is stored only when EVERY file was consumed up to its pinned
        # size. Any later change (growth included) differs from it, so the next pass rescans.
        fully = complete and len(summ["files"]) == len(plan) and all(
            r.get("skipped") or r.get("offset") == r.get("pinned") for r in summ["files"])
        new_fp = fp if fully else None
        if state.get("fingerprint") != new_fp:
            state["fingerprint"] = new_fp
            live.dirty = True
        if not fully:
            summ["status"] = "partial"
        summ["warnings"].extend(_meta_warnings(state))
        if state["caps_hit"] != caps_before:
            live.material = True                       # a newly hit cap is persisted at once, never throttled
        if live.dirty or live.material:
            _persist(store_root, live.state, summ, force=live.material)
    return summ


def _persist(store_root: str, state: Dict[str, Any], summ: Dict[str, Any], *, force: bool) -> None:
    """P2-3: write the state file when something MATERIAL changed (an entry, a pending tool_use, a
    reset or a cap), or when a pure offset/fingerprint advance is older than PERSIST_MIN_INTERVAL.
    An unpersisted advance costs only a rescan from the older checkpoint, which is idempotent
    (``applied``). A failed write is a LOUD warning on the summary, never an exception."""
    last = state.get("updated")
    if not force and last is not None and (_wall() - last) < PERSIST_MIN_INTERVAL:
        summ["persisted"] = False
        return
    try:
        save_state(store_root, state)
        summ["persisted"] = True
    except OSError as exc:
        summ["persisted"] = False
        summ["persist_failed"] = True
        summ["_state"] = state                  # in-memory result of THIS pass, for the caller's overlay
        summ["warnings"].append(f"could not persist ledger state ({type(exc).__name__}); will retry next pass")


def discover_sessions(roots: Sequence[str]) -> List[str]:
    """Harnessed session dirs (those with ``.claude/launch.json``) under ``roots``, NEWEST first."""
    found: List[Tuple[float, str]] = []
    for root in roots:
        root = os.path.abspath(os.path.expanduser(root))
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for name in names:
            sd = os.path.join(root, name)
            lj = os.path.join(sd, ".claude", "launch.json")
            try:
                found.append((os.stat(lj).st_mtime, sd))
            except OSError:
                continue
    found.sort(key=lambda t: (-t[0], t[1]))
    return [sd for _m, sd in found]


def scan_all(store_root: Optional[str] = None, roots: Optional[Sequence[str]] = None, *,
             projects_root: Optional[str] = None, deadline_seconds: Any = DEFAULT_DEADLINE_SECONDS,
             clock: Callable[[], float] = time.monotonic, only_session: Optional[str] = None,
             after_pin: Optional[Callable[[], None]] = None) -> Dict[str, Any]:
    """One bounded incremental pass over every harnessed session (or just ``only_session``).
    ``scan_complete`` is False when the deadline cut the pass: the ledger is then a LOWER BOUND."""
    store_root = store_root or default_store_root()
    roots = list(roots) if roots else default_session_roots()
    budget = clamp_deadline(deadline_seconds)
    started = clock()
    scanned_any = [False]

    def expired() -> bool:
        return clock() - started > budget

    dirs = discover_sessions(roots)
    if only_session is not None:
        dirs = [d for d in dirs if os.path.basename(d) == only_session]
    sessions: List[Dict[str, Any]] = []
    complete = True
    stop: List[str] = []
    for sd in dirs:
        # progress guarantee: the first session is always attempted; later ones only in budget
        if scanned_any[0] and expired():
            complete = False
            stop.append("deadline")
            break
        s = scan_session(store_root, sd, projects_root=projects_root, expired=expired, after_pin=after_pin)
        sessions.append(s)
        if s["status"] not in ("unchanged",):
            scanned_any[0] = True
        if s["status"] == "partial":
            complete = False
            if "deadline" not in stop and any(f.get("status") == "deadline" for f in s["files"]):
                stop.append("deadline")
    warnings: List[str] = []
    overlay: Dict[str, Dict[str, Any]] = {}
    for sm in sessions:
        for w in sm.get("warnings") or []:
            warnings.append(f"{sm['session']}: {w}")
        st = sm.pop("_state", None)
        if sm.get("persist_failed") and st is not None:
            overlay[sm["session"]] = st
    more = max(0, len(warnings) - WARNINGS_CAP)
    warnings = warnings[:WARNINGS_CAP] + ([f"... and {more} more scan warnings"] if more else [])
    return {"scan_complete": complete, "stop_reasons": stop, "deadline_seconds": budget,
            "persisted": not overlay, "warnings": warnings, "overlay": overlay,
            "sessions_seen": len(dirs), "sessions_scanned": len(sessions),
            "sessions": sessions, "seconds": round(clock() - started, 3)}


# --- seen store + listing ----------------------------------------------------------


def _seen_path(store_root: str) -> str:
    return os.path.join(store_root, "seen.json")


def _load_seen(store_root: str) -> Dict[str, Any]:
    p = _seen_path(store_root)
    try:
        with open(p, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        return {"v": SEEN_VERSION, "viewers": {}}
    except (OSError, ValueError) as exc:
        # FAIL LOUD: silently treating a corrupt seen store as empty would re-flag everything
        # as unseen with no explanation, or worse hide the loss. Move aside and say so.
        aside = p + ".corrupt-%d" % int(time.time())
        with contextlib.suppress(OSError):
            os.replace(p, aside)
        return {"v": SEEN_VERSION, "viewers": {}, "warning": f"seen store unreadable ({type(exc).__name__}); moved aside"}
    if not isinstance(obj, dict) or obj.get("v") != SEEN_VERSION or not isinstance(obj.get("viewers"), dict):
        return {"v": SEEN_VERSION, "viewers": {}, "warning": "seen store had an unknown schema; ignored"}
    return obj


def check_viewer(viewer: Any) -> str:
    v = DEFAULT_VIEWER if viewer is None or viewer == "" else viewer
    if not isinstance(v, str) or not VIEWER_RE.match(v):
        raise LedgerError("bad-viewer", "viewer must match " + VIEWER_RE.pattern)
    return v


def _all_entries(store_root: str, overlay: Optional[Dict[str, Dict[str, Any]]] = None
                 ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """``(entries, sessions_meta, warnings)`` from every ledger state file. ``overlay`` maps a session
    to the IN-MEMORY state of the pass that just ran, used INSTEAD of the on-disk file for a session
    whose persist failed (so the request still shows what the scan saw; nothing is written)."""
    entries: List[Dict[str, Any]] = []
    metas: List[Dict[str, Any]] = []
    warns: List[str] = []
    d = os.path.join(store_root, "ledger")
    overlay = overlay or {}
    try:
        names = [n[:-5] for n in os.listdir(d) if n.endswith(".json")]
    except OSError:
        names = []
    for sess in sorted(set(names) | set(overlay)):
        if sess in overlay:
            st, w = overlay[sess], None
        else:
            st, w = load_state(store_root, sess)
        if w:
            warns.append(w)
        if st is None:
            continue
        metas.append({"session": st["session"], "instance": st.get("instance"),
                      "entries": len(st["entries"]), "chains": len(st.get("chains") or {}), "resets": st.get("resets", 0),
                      "entries_dropped": st.get("entries_dropped", 0), "caps_hit": st.get("caps_hit", []),
                      "last_error": st.get("last_error"), "complete": st.get("fingerprint") is not None})
        entries.extend(st["entries"].values())
    return entries, metas, warns


def _author_of(ent: Dict[str, Any]) -> str:
    """An entry's author: its ledger ``instance`` (stripped), else ``UNKNOWN_AUTHOR``."""
    inst = ent.get("instance")
    return inst.strip() if isinstance(inst, str) and inst.strip() else UNKNOWN_AUTHOR


def _is_unseen(ent: Dict[str, Any], marks: Dict[str, Any]) -> bool:
    m = marks.get(ent["id"])
    le = ent.get("last_ts_epoch")
    if m is None:
        return True
    if le is None:
        return False
    return le > m


def list_entries(store_root: Optional[str] = None, *, viewer: Any = None, filter: str = "all",  # noqa: A002
                 session: Optional[str] = None, since: Any = None, limit: int = LIST_LIMIT_DEFAULT,
                 offset: int = 0, overlay: Optional[Dict[str, Dict[str, Any]]] = None,
                 author: Optional[str] = None) -> Dict[str, Any]:
    """The read model (4B / 3A consume this). ``since`` is an epoch float (or None) compared with
    an entry's ``last_ts``. Newest ``last_ts`` first. ``unseen_count`` counts the WHOLE filtered-by-
    session-and-since set, independent of paging.

    ``author`` (operator 2026-10-03, the Deliverables author filter): ``None`` = every author; otherwise only the
    entries whose AUTHOR equals it, where an entry's author is its ledger ``instance`` (the harnessed
    session's instance id, e.g. ``agent-a``); ``UNKNOWN_AUTHOR`` selects the entries that have no
    author recorded (shown as "Unknown author", never hidden). ``authors`` in the result lists every author
    present in the session/since set (BEFORE the author and unseen filters, so the choices do not collapse
    to the one chosen): ``[{"author": <name or UNKNOWN_AUTHOR>, "count": n, "unseen": u}]``, named authors
    sorted, the unknown one last."""
    store_root = store_root or default_store_root()
    viewer = check_viewer(viewer)
    if filter not in FILTERS:
        raise LedgerError("bad-filter", f"filter must be one of {list(FILTERS)}")
    if session is not None and not SESSION_RE.match(session):
        raise LedgerError("bad-session", "session is not a valid session directory name")
    if author is not None and author != UNKNOWN_AUTHOR and not (isinstance(author, str) and AUTHOR_RE.match(author)):
        raise LedgerError("bad-author", "author must be an instance id or " + UNKNOWN_AUTHOR)
    limit = max(1, min(LIST_LIMIT_CEILING, int(limit)))
    offset = max(0, int(offset))
    with _locked(store_root):
        ents, metas, warns = _all_entries(store_root, overlay)
        seen = _load_seen(store_root)
    if seen.get("warning"):
        warns.append(seen["warning"])
    marks = seen["viewers"].get(viewer) or {}
    if session is not None:
        ents = [e for e in ents if e["session"] == session]
    if since is not None:
        ents = [e for e in ents if (e.get("last_ts_epoch") or 0.0) >= since]
    tally: Dict[str, List[int]] = {}
    for e in ents:
        a = _author_of(e)
        t = tally.setdefault(a, [0, 0])
        t[0] += 1
        t[1] += 1 if _is_unseen(e, marks) else 0
    authors = [{"author": a, "count": tally[a][0], "unseen": tally[a][1]}
               for a in sorted(k for k in tally if k != UNKNOWN_AUTHOR)]
    if UNKNOWN_AUTHOR in tally:
        authors.append({"author": UNKNOWN_AUTHOR, "count": tally[UNKNOWN_AUTHOR][0],
                        "unseen": tally[UNKNOWN_AUTHOR][1]})
    if author is not None:
        ents = [e for e in ents if _author_of(e) == author]
    unseen = [e for e in ents if _is_unseen(e, marks)]
    chosen = unseen if filter == "unseen" else ents
    chosen = sorted(chosen, key=lambda e: (-(e.get("last_ts_epoch") or 0.0), e["id"]))
    page = chosen[offset: offset + limit]
    rows = [dict(e, seen=not _is_unseen(e, marks)) for e in page]
    return {"viewer": viewer, "filter": filter, "session": session, "author": author, "authors": authors,
            "total": len(chosen),
            "returned": len(rows), "truncated": offset + len(rows) < len(chosen),
            "unseen_count": len(unseen), "entries": rows, "sessions": metas, "warnings": warns}


def mark_seen(store_root: Optional[str] = None, *, viewer: Any = None, ids: Optional[Iterable[str]] = None,
              session: Optional[str] = None, mark_all: bool = False) -> Dict[str, Any]:
    """Mark entries seen for ``viewer``: by ``ids``, or every entry of ``session``, or ``mark_all``.
    The mark records the ledger's own ``last_ts`` (CHOICE-3). Unknown ids are reported, not errors."""
    store_root = store_root or default_store_root()
    viewer = check_viewer(viewer)
    if session is not None and not SESSION_RE.match(session):
        raise LedgerError("bad-session", "session is not a valid session directory name")
    id_list = list(ids) if ids is not None else []
    if not all(isinstance(i, str) and re.fullmatch(r"[0-9a-f]{16}", i) for i in id_list):
        raise LedgerError("bad-id", "ids must be 16-hex entry ids")
    if not (id_list or session is not None or mark_all):
        raise LedgerError("bad-request", "give ids, a session, or all=true")
    with _locked(store_root):
        ents, _m, _w = _all_entries(store_root)
        by_id = {e["id"]: e for e in ents}
        targets: Dict[str, Dict[str, Any]] = {}
        for i in id_list:
            if i in by_id:
                targets[i] = by_id[i]
        if session is not None:
            targets.update({e["id"]: e for e in ents if e["session"] == session})
        if mark_all:
            targets.update(by_id)
        seen = _load_seen(store_root)
        seen.pop("warning", None)
        marks = seen["viewers"].setdefault(viewer, {})
        changed = 0
        for eid, e in targets.items():
            v = e.get("last_ts_epoch")
            v = 0.0 if v is None else v
            if marks.get(eid) != v:
                marks[eid] = v
                changed += 1
        if len(marks) > SEEN_CAP:
            for k in sorted(marks, key=lambda k: marks[k])[: len(marks) - SEEN_CAP]:
                del marks[k]
        if changed:
            _atomic_write_json(_seen_path(store_root), seen)
    return {"viewer": viewer, "marked": changed, "matched": len(targets),
            "unknown": [i for i in id_list if i not in by_id]}


# --- the Open action (operator 2026-10-03) --------------------------------------------------------------------
#
# ``open_entry`` opens ONE ledger entry, named BY ID (the client never sends a path), with the macOS
# ``open`` command through the SAME guarded path the DSD documents use (``dsd.documents``: the
# operator-ratified allow-listed roots, the named refusals, the injected opener, the real opener that refuses to
# run under pytest). What this adds on top:
#   * FILE IDENTITY for containment: the resolved path must have, as an ancestor (or itself), the very
#     directory a root names, compared with ``os.path.samestat`` (st_dev + st_ino), NOT a string prefix;
#   * a final-component SYMLINK is refused outright (a path "replaced by a symlink to outside" is the
#     classic swap; a symlink to somewhere inside the roots is refused too, the safe reading);
#   * a regular file must pass ``documents.classify`` with the wider ``DELIVERABLE_EXTENSIONS`` list
#     (documents + common source/text types; NO scripts, bundles, installers) and carry no executable bit;
#   * a directory is opened in the file manager unless its name is an active bundle type (.app etc.);
#   * the entry is marked seen (for the viewer) only AFTER the opener succeeded.
# KNOWN LIMIT: a check-then-open window remains (the opener is handed the resolved path after the checks and
# an inode re-check just before it; a same-uid process can still race inside that window). Every same-uid
# agent already has a shell and can run ``open`` itself, so this guards a UI click, not a security boundary.

#: Opened by the ledger's Open button: the DSD document types plus common source / text / config types.
#: Deliberately absent: .sh .command .app .pkg .dmg .terminal .scpt .workflow ... (launching or installing).
DELIVERABLE_EXTENSIONS = frozenset(
    "." + e for e in ("md", "txt", "lts", "smd", "json", "jsonl", "csv", "tsv", "pdf", "html", "htm", "png",
                      "jpg", "jpeg", "svg", "log", "py", "js", "mjs", "ts", "tsx", "css", "yaml", "yml",
                      "toml", "ini", "cfg", "rst", "xml", "sql", "ipynb", "gif", "webp", "diff", "patch"))
#: A DIRECTORY with one of these suffixes is an application / installer / plug-in bundle: never opened.
BUNDLE_SUFFIXES = frozenset(
    "." + e for e in ("app", "bundle", "workflow", "action", "pkg", "mpkg", "framework", "plugin", "xpc",
                      "kext", "prefpane", "saver", "wdgt", "scptd", "dmg", "command", "terminal"))
ID_RE = re.compile(r"^[0-9a-f]{16}$")


def get_entry(store_root: Optional[str], eid: str) -> Optional[Dict[str, Any]]:
    """The ledger entry with id ``eid`` (a copy), or None. ``eid`` must be a 16-hex entry id."""
    store_root = store_root or default_store_root()
    if not isinstance(eid, str) or not ID_RE.match(eid):
        raise LedgerError("bad-id", "id must be a 16-hex entry id")
    with _locked(store_root):
        ents, _m, _w = _all_entries(store_root)
    for e in ents:
        if e["id"] == eid:
            return dict(e)
    return None


def contained_by_identity(real_path: str, real_roots: Sequence[str]) -> bool:
    """True iff ``real_path`` IS, or lies under, a directory that is the same filesystem object as one of
    ``real_roots`` (``os.path.samestat`` on stat results, walking the ancestors): file identity, not a
    string-prefix comparison. Any stat failure on the way is "not contained"."""
    root_stats = []
    for r in real_roots:
        try:
            root_stats.append(os.stat(r))
        except OSError:
            continue
    if not root_stats:
        return False
    cur = real_path
    while True:
        try:
            cst = os.stat(cur)
        except OSError:
            return False
        if any(os.path.samestat(cst, rs) for rs in root_stats):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def check_openable(path: str, roots: Optional[Sequence[str]] = None, *, home: Optional[str] = None
                   ) -> Dict[str, Any]:
    """The Open policy for one ledger path. Returns ``{ok, code, reason, resolved, is_dir, ident}`` where
    ``code`` is a NAMED refusal when not ok. Never raises."""
    from .dsd import documents as docs

    res: Dict[str, Any] = {"ok": False, "code": "", "reason": "", "resolved": "", "is_dir": False, "ident": None}

    def refuse(code: str, reason: str) -> Dict[str, Any]:
        res.update(code=code, reason=reason)
        return res

    try:
        raw = str(path or "")
        if not raw.strip() or "\x00" in raw:
            return refuse(docs.REFUSAL_MISSING, "empty or NUL-containing path")
        if not os.path.isabs(raw):
            return refuse(docs.REFUSAL_RELATIVE, f"{raw!r} is not absolute")
        try:
            lst = os.lstat(raw)
        except FileNotFoundError:
            return refuse(docs.REFUSAL_MISSING, f"no such file or directory: {raw}")
        except OSError as exc:
            return refuse(docs.REFUSAL_UNREADABLE, f"cannot stat {raw}: {type(exc).__name__}")
        if _stat.S_ISLNK(lst.st_mode):
            return refuse("symlink-refused", f"{raw} is a symbolic link; links are never followed by Open")
        real = os.path.realpath(raw)
        res["resolved"] = real
        use_roots = list(roots) if roots is not None else docs.allowed_roots(home)
        real_roots = [os.path.realpath(r) for r in use_roots]
        if not contained_by_identity(real, real_roots):
            return refuse(docs.REFUSAL_OUTSIDE_ROOTS,
                          f"{real} is outside the allow-listed roots (checked by file identity)")
        if _stat.S_ISDIR(lst.st_mode):
            base = os.path.basename(real.rstrip(os.sep)).lower()
            if os.path.splitext(base)[1] in BUNDLE_SUFFIXES:
                return refuse("bundle-refused", f"{os.path.basename(real)} is an application/installer bundle")
            res.update(ok=True, code=docs.STATUS_OK, is_dir=True, ident=(lst.st_dev, lst.st_ino))
            return res
        c = docs.classify(real, real_roots, extensions=DELIVERABLE_EXTENSIONS)
        if not c["ok"]:
            return refuse(c["code"], c["reason"])
        res.update(ok=True, code=docs.STATUS_OK, ident=(lst.st_dev, lst.st_ino))
        return res
    except Exception as exc:  # noqa: BLE001 - the policy never raises
        return refuse("unreadable", f"{type(exc).__name__}: {exc}")


class OpenRefused(LedgerError):
    """Open refused (``kind`` = a NAMED code: unknown-id, missing, outside-allowed-roots, symlink-refused, ...)."""


def open_entry(store_root: Optional[str] = None, *, entry_id: Any, viewer: Any = None,
               opener: Callable[[str, bool], None], roots: Optional[Sequence[str]] = None,
               home: Optional[str] = None) -> Dict[str, Any]:
    """Open ledger entry ``entry_id`` for ``viewer`` and mark it seen. NEVER takes a path. ``opener`` is
    ``opener(resolved_path, as_text)`` (injected; the real one is ``dsd.documents.system_opener``, which
    runs ``['/usr/bin/open', path]`` as an argv list: no shell, no interpolation). Raises
    :class:`OpenRefused` (named) on every refusal and on an opener failure; the entry is marked seen only
    after the opener returned."""
    from .dsd import documents as docs

    store_root = store_root or default_store_root()
    viewer = check_viewer(viewer)
    ent = get_entry(store_root, entry_id)          # LedgerError(bad-id) for a malformed id
    if ent is None:
        raise OpenRefused("unknown-id", f"no deliverable with id {entry_id}")
    chk = check_openable(ent.get("path"), roots, home=home)
    if not chk["ok"]:
        raise OpenRefused(chk["code"], chk["reason"])
    try:                                           # narrow the check-to-open window: same inode as checked
        now = os.lstat(chk["resolved"])
    except OSError as exc:
        raise OpenRefused("changed-before-open", f"{type(exc).__name__}: {exc}")
    if (now.st_dev, now.st_ino) != chk["ident"]:
        raise OpenRefused("changed-before-open", "the path now names a different file; refused")
    as_text = (not chk["is_dir"]) and docs.opens_as_text(chk["resolved"])
    try:
        opener(chk["resolved"], as_text)
    except docs.DocumentRefused as exc:
        raise OpenRefused(exc.code, exc.message)
    except Exception as exc:  # noqa: BLE001 - loud, never a silent no-op
        raise OpenRefused("opener-failed", f"{type(exc).__name__}: {exc}")
    marked = mark_seen(store_root, viewer=viewer, ids=[ent["id"]])
    return {"ok": True, "id": ent["id"], "path": ent["path"], "is_dir": chk["is_dir"], "viewer": viewer,
            "marked_seen": marked["marked"], "matched": marked["matched"]}

"""durable transcript archive.

Why: an agent's reasoning (``thinking`` blocks) and the operator's exact words live
ONLY in Claude Code's transcript JSONL, and Claude Code deletes dormant
transcripts (``cleanupPeriodDays``, default 30). (behavioural
diagnosis) and (an agent-def A/B review) need that record retained and
retrievable, so this archive is the durable copy.

⚠ PRIVACY / STORAGE TIER. Transcripts contain the operator's words and the agents'
reasoning verbatim. The default archive root, ``~/gnomon-data/transcript-archive``,
is on the data-root tier — the existing tier for session
data, accepted for this content by the operator (2026-09-29) because it gives
durability. A sync service on that tier may copy it off this machine; nothing in
this module transmits content anywhere else (no network, no telemetry). Files
are written 0600 and directories 0700. Point ``--archive-root`` (or
``GNOMON_TRANSCRIPT_ARCHIVE_ROOT``) elsewhere to change the tier.

Discovery (never guess): for every session dir under the roots
(``~/gnomon-data/sessions/*``), sources are taken ONLY
from what the harness RECORDED —

* ``launch.json`` ``transcript_path`` (D40)                       kind ``launch``
* ``launch.json`` ``previous_session_ids`` under
  ``transcript_project_dir`` (the resume/relaunch chain)          kind ``previous``
* pre-D40 resume records: ``resume_session_id`` under the
  project dir of ``resume_cwd`` (deterministic, same mangle the
  harness uses), only when no ``transcript_path`` was recorded   kind ``resume_derived``
* ``context_state.json`` ``transcript_path`` (the statusline
  hook's live pointer — a ``/clear`` successor shows up here)     kind ``live_pointer``
* the ``.claude/transcript.jsonl`` symlink target (fallback)      kind ``link``

``~/.claude/projects`` is NEVER globbed to guess. A session dir with none of
``launch.json`` / ``context_state.json`` / the link is "not harnessed" (counted,
never archived); a harnessed dir with no recorded pointer is "no_pointer".

Copy: ``<archive_root>/<session_basename>/<transcript_filename>``. Per
(session, source) the state file keeps, per archived version, ``(inode, byte
offset, head hash, tail hash)``. A re-run copies only the appended bytes. A
source that shrank, or whose already-archived bytes changed (head or tail hash
mismatch), or an archive copy that no longer matches its recorded offset, starts
a NEW versioned file (``<stem>.v2.jsonl`` …) — an existing archive file is never
overwritten or appended to out of sequence. An inode change whose content is
still a pure extension continues the same version. The source is only ever
opened read-only. Several session dirs can record the SAME source (a resume
records the original transcript); each session dir gets its own copy (the
layout is per session), and the report dedupes by source.

Subagent transcripts (revision 2026-10-27): the ``<transcript_stem>/subagents/*.jsonl``
sidechain transcripts beside each recorded main transcript are archived with the SAME append-aware versioned
copy, under the session's archive folder at ``subagents/<transcript_stem>/<file>``. The setting
``transcript_archive_subagents`` (machine config) is ``all`` (default) | ``none``; ``flagged-only`` is UNDEFINED
in the spec, so it is not implemented: it archives ALL (the data-keeping direction) and every run says so in
``warnings``.

Each session folder gets a small pointer file, ``<session>/.claude/transcript-archive.json``, naming where its
transcripts are kept (atomic write; it is rewritten when its content changes and recreated if missing; nothing
else in the session folder is touched). Every non-dry run writes a heartbeat record, ``<archive_root>/heartbeat.json``
(time, sessions, files copied, bytes, archive total size, free space). A run REFUSES, writing nothing and
raising one operator alert, while free space on the archive's volume is below :data:`MIN_FREE_BYTES` (15 GiB; the product owner's choice 2026-10-27).
:func:`coverage_check` is the read-only loud check (go-live smoke and the web job's alert).

CLI::

    python -m gnomon.transcript_archive run [--dry-run]
    python -m gnomon.transcript_archive report [--json]
    python -m gnomon.transcript_archive check [--json]      # the loud check; exit 1 on any NEW finding
    python -m gnomon.transcript_archive baseline [--add SESSION --reason TEXT [--who NAME]]   # list / add a known gap

Historical gaps (ruling 2026-10-27): the first non-dry run on a machine records every session gap it then finds
(``no_pointer``, ``main_missing``, ``main_empty``) in ``baseline.json`` under the archive root, with the reason per
session. The loud check reports a baselined gap as KNOWN (a count, never a FAIL, never an alert) and fails only on a
gap not in the baseline. A baselined session that is later healthy is dropped. The file gains entries after the first
run only through the ``baseline --add`` verb, which logs who and when.

Real runs happen when ``run`` is invoked explicitly, via :func:`run_if_enabled`, or from the web job's timer
(:mod:`gnomon.transcript_archive_job`: once at web-job start and every 10 minutes, only while the
``transcript_archive`` flag is ON; default ON since the 2026-10-27 revision). ``report`` and ``check`` are
strictly read-only (they never create the archive). Sources are only ever opened read-only; nothing here writes
under ``~/.claude/projects``.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as _dt
import errno
import fcntl
import getpass
import hashlib
import json
import os
import shutil
import stat
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .harness.context_readers import claude_config_dir, claude_project_dir_name

FLAG_NAME = "transcript_archive"
STATE_SCHEMA = 1
STATE_FILENAME = ".archive_state.json"
LOCK_FILENAME = ".lock"

ARCHIVE_ROOT_ENV = "GNOMON_TRANSCRIPT_ARCHIVE_ROOT"


# Import-time path defaults are a hazard (g31, 2026-09-30): resolved at CALL time; the old
# public names DEFAULT_ARCHIVE_ROOT / DEFAULT_ROOTS keep working via module ``__getattr__``.
def default_archive_root() -> str:
    from .config import transcript_archive_root_default

    return transcript_archive_root_default()


def default_roots() -> Tuple[str, ...]:
    """The session roots scanned by default: ``<data_root>/sessions`` plus the deployment's LEGACY session sources
    (``defaults/legacy-session-roots.json``, ``{"roots": [...]}``; X13 S5-4: neutral default = none)."""
    from .config import sessions_root_default
    from .deploy_data import legacy_session_roots

    return (sessions_root_default(),) + legacy_session_roots()


def __getattr__(name: str):  # PEP 562
    if name == "DEFAULT_ARCHIVE_ROOT":
        return default_archive_root()
    if name == "DEFAULT_ROOTS":
        return default_roots()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


#: revision 2026-10-27
SUBAGENT_MODES = ("all", "flagged-only", "none")
DEFAULT_SUBAGENTS = "all"
#: a run refuses (writes nothing, one alert) below this much free space on the archive's volume
FREE_SPACE_FLOOR_GIB = 15
MIN_FREE_BYTES = FREE_SPACE_FLOOR_GIB * 1024 ** 3
#: the web job's timer
INTERVAL_S = 600
HEARTBEAT_FILENAME = "heartbeat.json"
#: the known-gaps baseline (written once by the first non-dry run; see the module doc)
BASELINE_FILENAME = "baseline.json"
#: the finding codes that are per-session gaps (the only ones a baseline can cover)
BASELINE_CODES = ("no_pointer", "main_missing", "main_empty")
#: the loud check fails when the newest heartbeat is older than this while the flag is ON
HEARTBEAT_MAX_AGE_S = 30 * 60
HEARTBEAT_FUTURE_SKEW_S = 60
#: a discovered subagent transcript still not archived after this many runs is a finding
SUBAGENT_STUCK_RUNS = 2
#: a source (or session) younger than this is not yet judged "missing" (two timer runs)
GRACE_S = 2 * INTERVAL_S
POINTER_FILENAME = "transcript-archive.json"
ALERT_SOURCE = "gnomon-transcript-archive"

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 4 * 1024
_CHUNK = 1024 * 1024

DIR_MODE = 0o700
FILE_MODE = 0o600

NO_MODEL = "<no-model>"

#: Reused, not reimplemented: the same cwd -> project-dir mangle the harness uses.
project_dir_name = claude_project_dir_name


def _utcnow() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_projects_root() -> str:
    return os.path.join(claude_config_dir(), "projects")


# --- discovery ---------------------------------------------------------------


@dataclass
class Source:
    kind: str
    path: str
    #: for a ``subagent`` source: the main transcript whose ``<stem>/subagents`` folder holds it
    parent: str = ""


@dataclass
class SessionRecord:
    session_dir: str
    basename: str
    harnessed: bool
    sources: List[Source] = field(default_factory=list)
    #: the subagent transcripts beside the recorded main transcripts (kept apart from ``sources``)
    subagents: List[Source] = field(default_factory=list)


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _session_sources(session_dir: str, projects_root: str) -> Tuple[bool, List[Source]]:
    cdir = os.path.join(session_dir, ".claude")
    launch_p = os.path.join(cdir, "launch.json")
    state_p = os.path.join(cdir, "context_state.json")
    link_p = os.path.join(cdir, "transcript.jsonl")
    has_launch = os.path.isfile(launch_p)
    has_state = os.path.isfile(state_p)
    has_link = os.path.islink(link_p)
    harnessed = has_launch or has_state or has_link
    out: List[Source] = []
    seen: set = set()

    def add(kind: str, p: Any) -> None:
        if not isinstance(p, str) or not p:
            return
        p = os.path.normpath(p)
        if p in seen:
            return
        seen.add(p)
        out.append(Source(kind, p))

    cfg = _read_json(launch_p) if has_launch else None
    if cfg:
        tp = cfg.get("transcript_path")
        add("launch", tp)
        pdir = cfg.get("transcript_project_dir")
        if isinstance(pdir, str) and pdir:
            for sid in cfg.get("previous_session_ids") or []:
                if isinstance(sid, str) and sid:
                    add("previous", os.path.join(pdir, f"{sid}.jsonl"))
        rsid, rcwd = cfg.get("resume_session_id"), cfg.get("resume_cwd")
        if not tp and isinstance(rsid, str) and rsid and isinstance(rcwd, str) and rcwd:
            add("resume_derived",
                os.path.join(projects_root, claude_project_dir_name(rcwd), f"{rsid}.jsonl"))
    live = _read_json(state_p) if has_state else None
    if live:
        add("live_pointer", live.get("transcript_path"))
    if has_link:
        try:
            tgt = os.readlink(link_p)
            if not os.path.isabs(tgt):
                tgt = os.path.join(cdir, tgt)
            add("link", tgt)
        except OSError:
            pass
    return harnessed, out


def normalize_subagents(value: Any) -> Tuple[str, Optional[str]]:
    """``(mode, warning)``. ``all`` and ``none`` are implemented. ``flagged-only`` is UNDEFINED in the spec and
    anything else is unknown: both archive ALL (the direction that keeps data) and say so."""
    if value in ("all", "none"):
        return value, None
    if value == "flagged-only":
        return "all", ("transcript_archive_subagents=flagged-only is UNDEFINED in the spec (nothing says what "
                       "'flagged' means), so it is not implemented: archiving ALL subagent transcripts")
    return "all", f"transcript_archive_subagents={value!r} is not one of {list(SUBAGENT_MODES)}: archiving ALL"


def _subagent_sources(main_sources: Sequence[Source]) -> List[Source]:
    out: List[Source] = []
    seen: set = set()
    for m in main_sources:
        sdir = _subagent_dir(m.path)
        try:
            names = sorted(os.listdir(sdir))
        except OSError:
            continue
        for n in names:
            p = os.path.join(sdir, n)
            if not n.endswith(".jsonl") or p in seen:
                continue
            try:
                if not stat.S_ISREG(os.lstat(p).st_mode):
                    continue
            except OSError:
                continue
            seen.add(p)
            out.append(Source("subagent", p, parent=m.path))
    return out


def discover(roots: Sequence[str], *, projects_root: Optional[str] = None,
             subagents: str = DEFAULT_SUBAGENTS) -> List[SessionRecord]:
    """Every session dir directly under each root, with its RECORDED sources (and, unless ``subagents`` is
    ``none``, the subagent transcripts beside them)."""
    proot = projects_root or default_projects_root()
    recs: List[SessionRecord] = []
    for root in roots:
        try:
            names = sorted(os.listdir(root))
        except OSError:
            continue
        for name in names:
            sd = os.path.join(root, name)
            if not os.path.isdir(sd):
                continue
            harnessed, sources = _session_sources(sd, proot)
            subs = [] if subagents == "none" else _subagent_sources(sources)
            recs.append(SessionRecord(sd, name, harnessed, sources, subs))
    return recs


def missing_roots(roots: Sequence[str]) -> List[str]:
    return [r for r in roots if not os.path.isdir(r)]


# --- state ---------------------------------------------------------------------


def _state_path(archive_root: str) -> str:
    return os.path.join(archive_root, STATE_FILENAME)


def load_state(archive_root: str) -> Dict[str, Any]:
    st = _read_json(_state_path(archive_root))
    if not st or not isinstance(st.get("entries"), dict):
        return {"schema": STATE_SCHEMA, "entries": {}}
    return st


def _save_state(archive_root: str, st: Dict[str, Any]) -> None:
    path = _state_path(archive_root)
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(st, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _entry_key(session: str, source: str) -> str:
    return f"{session}|{source}"


def _mkdir_private(path: str) -> None:
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    os.chmod(path, DIR_MODE)


# --- hashing -----------------------------------------------------------------


def _hash_range(path: str, start: int, length: int) -> Optional[str]:
    """sha256 of ``length`` bytes at ``start`` (read-only), or None if short/unreadable."""
    try:
        with open(path, "rb") as fh:
            fh.seek(start)
            data = fh.read(length)
    except OSError:
        return None
    if len(data) != length:
        return None
    return hashlib.sha256(data).hexdigest()


def _fingerprint(path: str, offset: int) -> Dict[str, Any]:
    head_len = min(offset, HEAD_BYTES)
    tail_len = min(offset, TAIL_BYTES)
    return {
        "head_len": head_len, "head_hash": _hash_range(path, 0, head_len),
        "tail_len": tail_len, "tail_hash": _hash_range(path, offset - tail_len, tail_len),
    }


def _continuous(src: str, ver: Dict[str, Any]) -> bool:
    off = ver["offset"]
    return (_hash_range(src, 0, ver["head_len"]) == ver["head_hash"]
            and _hash_range(src, off - ver["tail_len"], ver["tail_len"]) == ver["tail_hash"])


# --- copy ----------------------------------------------------------------------


def _version_name(filename: str, n: int) -> str:
    if n <= 1:
        return filename
    stem, ext = os.path.splitext(filename)
    return f"{stem}.v{n}{ext}"


def _alloc_filename(session_dir: str, base: str, taken: set, start_n: int) -> Tuple[str, int]:
    n = start_n
    while True:
        name = _version_name(base, n)
        if name not in taken and not os.path.lexists(os.path.join(session_dir, name)):
            return name, n
        n += 1


def _append_bytes(src: str, dst: str, start: int, end: int) -> int:
    """Append src[start:end] to dst (created 0600). Source opened read-only."""
    copied = 0
    sfd = os.open(src, os.O_RDONLY)
    try:
        dfd = os.open(dst, os.O_WRONLY | os.O_APPEND | os.O_CREAT, FILE_MODE)
        try:
            os.fchmod(dfd, FILE_MODE)
            os.lseek(sfd, start, os.SEEK_SET)
            remaining = end - start
            while remaining > 0:
                buf = os.read(sfd, min(_CHUNK, remaining))
                if not buf:
                    break
                mv = memoryview(buf)
                while mv:
                    w = os.write(dfd, mv)
                    mv = mv[w:]
                copied += len(buf)
                remaining -= len(buf)
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.close(sfd)
    return copied


def _plan_source(entry: Optional[Dict[str, Any]], src: str, st: os.stat_result,
                 archive_session_dir: str) -> Tuple[str, str]:
    """``(action, reason)``: action in new / append / new_version / unchanged."""
    if entry is None or not entry.get("versions"):
        return "new", "initial"
    ver = entry["versions"][-1]
    dst = os.path.join(archive_session_dir, ver["file"])
    try:
        dsize = os.path.getsize(dst)
    except OSError:
        return "new_version", "archive copy missing"
    if dsize != ver["offset"]:
        return "new_version", f"archive copy size {dsize} != recorded offset {ver['offset']}"
    if st.st_size < ver["offset"]:
        return "new_version", f"source truncated ({st.st_size} < {ver['offset']})"
    if not _continuous(src, ver):
        return "new_version", "source rewritten (archived bytes changed)"
    if st.st_size == ver["offset"]:
        return "unchanged", ("inode changed, content continuous"
                             if st.st_ino != ver.get("inode") else "")
    return "append", ("inode changed, content continuous"
                      if st.st_ino != ver.get("inode") else "")


def _mkdir_private_tree(base: str, target: str) -> None:
    """Create ``target`` (under ``base``), every level 0700 (``os.makedirs`` applies its mode to the leaf only)."""
    rel = os.path.relpath(target, base)
    cur = base
    _mkdir_private(cur)
    if rel in (".", ""):
        return
    for part in rel.split(os.sep):
        cur = os.path.join(cur, part)
        _mkdir_private(cur)


def _iso_epoch(iso: str) -> Optional[float]:
    try:
        return float(calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ")))
    except (TypeError, ValueError):
        return None


def free_bytes_of(path: str) -> int:
    """Free bytes on the volume that holds ``path`` (or its nearest existing ancestor)."""
    p = os.path.abspath(path)
    while not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return shutil.disk_usage(p).free


def archive_total_bytes(archive_root: str) -> int:
    total = 0
    for dp, _dn, fns in os.walk(archive_root):
        for fn in fns:
            try:
                st = os.lstat(os.path.join(dp, fn))
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                total += st.st_size
    return total


def _atomic_write_json(path: str, obj: Any) -> None:
    """tmp file beside the target (same directory, 0600), fsync, rename. Touches no other file."""
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        os.fchmod(fd, FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=1, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def pointer_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", POINTER_FILENAME)


def _write_pointer(rec: SessionRecord, archive_root: str, entries: Dict[str, Any], stamp: str) -> bool:
    """Write (or refresh) the session folder's pointer file. Returns True when it wrote. A session with nothing
    archived gets none; a session folder without a ``.claude`` folder is never given one."""
    asd = os.path.join(archive_root, rec.basename)
    trs = []
    for s in list(rec.sources) + list(rec.subagents):
        e = entries.get(_entry_key(rec.basename, s.path))
        if not e or not e.get("versions"):
            continue
        trs.append({"kind": s.kind, "source": s.path, "files": [v["file"] for v in e["versions"]]})
    if not trs:
        return False
    cdir = os.path.join(rec.session_dir, ".claude")
    if not os.path.isdir(cdir):
        return False
    body = {"schema": 1, "archive_dir": asd, "archive_root": archive_root, "transcripts": trs}
    path = pointer_path(rec.session_dir)
    cur = _read_json(path)
    if cur is not None and {k: v for k, v in cur.items() if k != "updated_at"} == body:
        return False
    _atomic_write_json(path, dict(body, updated_at=stamp))
    return True


def baseline_path(archive_root: str) -> str:
    return os.path.join(archive_root, BASELINE_FILENAME)


def load_baseline(archive_root: str) -> Optional[Dict[str, Any]]:
    """The baseline, or ``None`` when no baseline file exists yet (the first non-dry run creates it). A file that exists
    but cannot be parsed reads as an EMPTY baseline (never as "absent": it must not be silently re-taken)."""
    path = baseline_path(archive_root)
    if not os.path.lexists(path):
        return None
    b = _read_json(path)
    if not isinstance(b, dict) or not isinstance(b.get("entries"), dict):
        return {"schema": 1, "entries": {}, "log": [], "unreadable": True}
    return b


def _entry_matches(entry: Any, code: str) -> bool:
    codes = entry.get("codes") if isinstance(entry, dict) else None
    return isinstance(codes, list) and ("*" in codes or code in codes)


def split_findings(findings: Sequence["Finding"], baseline: Optional[Dict[str, Any]]) -> Tuple[List["Finding"], List["Finding"]]:
    """(failing, known): a session-gap finding is KNOWN when its session is baselined for that code."""
    entries = (baseline or {}).get("entries") or {}
    fails: List[Finding] = []
    known: List[Finding] = []
    for f in findings:
        if f.code in BASELINE_CODES and _entry_matches(entries.get(f.subject), f.code):
            known.append(f)
        else:
            fails.append(f)
    return fails, known


def _baseline_log(who: str, action: str, **kw: Any) -> Dict[str, Any]:
    return dict({"at": _utcnow(), "who": who, "action": action}, **kw)


def _preexisting_gap(f: "Finding", errored: set) -> bool:
    """A gap that existed BEFORE the run: a session with no recorded pointer, or whose recorded original is already
    gone (no archived copy, or only zero-length ones). Never a gap the run itself caused (a copy error, a write failure,
    a partial copy): a session with any error in this run, or whose original still exists but has no usable copy, is NOT
    pre-existing and so fails as a new gap."""
    if f.subject in errored:
        return False
    if f.code == "no_pointer":
        return True
    if f.code in ("main_missing", "main_empty"):
        return "source also gone" in f.detail
    return False


def _maintain_baseline(archive_root: str, roots: Sequence[str], projects_root: Optional[str], mode: str,
                       summary: Dict[str, Any]) -> None:
    """Called by a non-dry run, under the archive lock, after the run's own work. No baseline file yet: record every
    session gap found now. A baseline exists: drop every entry no current gap matches (a repaired session). It never
    adds an entry to an existing baseline."""
    base = load_baseline(archive_root)
    gaps = [f for f in _coverage_findings(roots=roots, archive_root=archive_root, projects_root=projects_root,
                                          subagents=mode) if f.code in BASELINE_CODES]
    if base is None:
        entries: Dict[str, Any] = {}
        errored = {str(e.get("session")) for e in summary.get("errors", []) if e.get("session")}
        for f in gaps:
            if not _preexisting_gap(f, errored):
                continue
            e = entries.setdefault(f.subject, {"codes": [], "reason": [], "added_by": "first-run", "added_at": _utcnow()})
            if f.code not in e["codes"]:
                e["codes"].append(f.code)
            e["reason"].append(f"{f.code}: {f.detail}")
        for e in entries.values():
            e["reason"] = "; ".join(e["reason"])
        base = {"schema": 1, "created_at": _utcnow(), "entries": entries,
                "log": [_baseline_log("first-run", "create", sessions=len(entries))]}
        _atomic_write_json(baseline_path(archive_root), base)
        summary["baseline"] = {"created": True, "sessions": len(entries), "dropped": []}
        return
    if base.get("unreadable"):
        summary["baseline"] = {"created": False, "sessions": 0, "dropped": [], "unreadable": True}
        return
    live = {f.subject for f in split_findings(gaps, base)[1]}
    dropped = sorted(k for k in base["entries"] if k not in live)
    if dropped:
        for k in dropped:
            base["entries"].pop(k, None)
        base.setdefault("log", []).append(_baseline_log("run", "drop-healed", sessions=dropped))
        _atomic_write_json(baseline_path(archive_root), base)
    summary["baseline"] = {"created": False, "sessions": len(base["entries"]), "dropped": dropped}


def baseline_add(archive_root: str, session: str, reason: str, who: Optional[str] = None) -> Dict[str, Any]:
    """The ONLY way an entry joins an existing baseline. Requires the baseline to exist (the first run makes it); logs
    who and when. Raises ``ValueError`` (no baseline yet / empty arguments)."""
    if not session or not reason or not reason.strip():
        raise ValueError("baseline --add needs a session and a non-empty --reason")
    who = who or getpass.getuser()
    _mkdir_private(archive_root)
    lock_fd = os.open(os.path.join(archive_root, LOCK_FILENAME), os.O_WRONLY | os.O_CREAT, FILE_MODE)
    try:
        os.fchmod(lock_fd, FILE_MODE)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        base = load_baseline(archive_root)
        if base is None:
            raise ValueError("no baseline yet: the first archive run creates it")
        if base.get("unreadable"):
            raise ValueError(f"{baseline_path(archive_root)} exists but is unreadable; not overwriting it")
        at = _utcnow()
        base["entries"][session] = {"codes": ["*"], "reason": reason.strip(), "added_by": who, "added_at": at}
        base.setdefault("log", []).append(dict(_baseline_log(who, "add", session=session, reason=reason.strip()), at=at))
        _atomic_write_json(baseline_path(archive_root), base)
        return base["log"][-1]
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def heartbeat_path(archive_root: str) -> str:
    return os.path.join(archive_root, HEARTBEAT_FILENAME)


def read_heartbeat(archive_root: str) -> Optional[dict]:
    return _read_json(heartbeat_path(archive_root))


def _default_alert(title: str, body: str, key: str) -> None:
    """Raise one operator alert through the existing alert service. That service has its own flag (``alerts``): while it
    is OFF no producer raises, so this honours it and says so on stderr instead (the go-live smoke and the heartbeat
    still FAIL loudly)."""
    from . import alerts, flags

    try:
        on = bool(flags.flag(alerts.FLAG_NAME))
    except Exception:  # noqa: BLE001 - an unreadable flag is not "off"
        on = True
    if not on:
        print(f"[gm-archive] ALERT NOT RAISED ({alerts.FLAG_NAME} is off): {title} -- {body}", file=sys.stderr, flush=True)
        return
    alerts.notify(ALERT_SOURCE, "error", title, body, key=key)


def run(*, roots: Optional[Sequence[str]] = None, archive_root: Optional[str] = None,
        projects_root: Optional[str] = None, dry_run: bool = False,
        now: Callable[[], str] = _utcnow, subagents: str = DEFAULT_SUBAGENTS,
        free_bytes: Optional[Callable[[str], int]] = None, min_free_bytes: int = MIN_FREE_BYTES,
        alert: Optional[Callable[[str, str, str], None]] = None) -> Dict[str, Any]:
    """Archive every discovered source. Returns a summary dict (never raises on a
    single bad source — that source becomes an ``errors`` item).

    ``subagents``: ``all`` | ``none`` (``flagged-only`` is undefined: see :func:`normalize_subagents`).
    A non-dry run first checks free space on the archive's volume: below ``min_free_bytes`` it REFUSES
    (``summary["refused"]``), writes nothing at all (no folder, no state, no heartbeat) and calls
    ``alert(title, body, key)`` once (default: the operator alert path, ``gnomon.alerts``)."""
    roots = default_roots() if roots is None else roots
    archive_root = archive_root or os.environ.get(ARCHIVE_ROOT_ENV) or default_archive_root()
    mode, warn = normalize_subagents(subagents)
    recs = discover(roots, projects_root=projects_root, subagents=mode)
    summary: Dict[str, Any] = {
        "dry_run": dry_run, "archive_root": archive_root, "missing_roots": missing_roots(roots),
        "bytes_copied": 0, "would_copy_bytes": 0, "new_versions": 0, "new_sources": 0, "files_copied": 0,
        "unchanged": 0, "source_missing": [], "anomalies": [], "errors": [], "actions": [],
        "warnings": [warn] if warn else [], "refused": None, "pointers_written": 0,
    }
    if not dry_run:
        free = (free_bytes or free_bytes_of)(archive_root)
        if free < min_free_bytes:
            summary["refused"] = {"reason": "free space below the floor", "free_bytes": free,
                                  "min_free_bytes": min_free_bytes}
            title = "Transcript archive refused to run: disk nearly full"
            body = (f"Free space on the archive's volume is {free / 1024 ** 3:.1f} GiB, below the "
                    f"{min_free_bytes / 1024 ** 3:.0f} GiB floor. Nothing was archived or written. Free space, "
                    f"then the next 10-minute run will go ahead. Archive: {archive_root}")
            try:
                (alert or _default_alert)(title, body, "low-disk")
            except Exception as exc:  # noqa: BLE001 - an alert failure is reported, never raised
                summary["errors"].append({"session": "", "path": archive_root, "error": f"alert failed: {exc}"})
            return summary
    st_all = load_state(archive_root)
    entries = st_all["entries"]
    lock_fd = None
    if not dry_run:
        _mkdir_private(archive_root)
        lock_fd = os.open(os.path.join(archive_root, LOCK_FILENAME),
                          os.O_WRONLY | os.O_CREAT, FILE_MODE)
        os.fchmod(lock_fd, FILE_MODE)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        st_all = load_state(archive_root)  # re-read under the lock
        entries = st_all["entries"]
    try:
        failed_keys: set = set()
        for rec in recs:
            for s in list(rec.sources) + list(rec.subagents):
                key = _entry_key(rec.basename, s.path)
                entry = entries.get(key)
                try:
                    sst = os.stat(s.path)
                except OSError:
                    summary["source_missing"].append({"session": rec.basename, "kind": s.kind,
                                                      "path": s.path, "archived": bool(entry)})
                    continue
                asd = os.path.join(archive_root, rec.basename)
                action, reason = _plan_source(entry, s.path, sst, asd)
                size = sst.st_size
                if action == "unchanged":
                    summary["unchanged"] += 1
                    if reason and not dry_run:
                        entry["versions"][-1]["inode"] = sst.st_ino
                        entry["source_last_seen_at"] = now()
                        _save_state(archive_root, st_all)
                    continue
                if action == "append":
                    start = entry["versions"][-1]["offset"]
                elif action == "new_version" and reason.startswith("archive copy"):
                    start = 0
                    summary["anomalies"].append({"session": rec.basename, "path": s.path,
                                                 "reason": reason})
                else:
                    start = 0
                summary["actions"].append({"session": rec.basename, "kind": s.kind,
                                           "path": s.path, "action": action, "reason": reason,
                                           "bytes": size - start})
                if dry_run:
                    summary["would_copy_bytes"] += size - start
                    continue
                try:
                    _mkdir_private(asd)
                    if entry is None:
                        taken = {v["file"] for e in entries.values()
                                 if e.get("session") == rec.basename for v in e["versions"]}
                        base = os.path.basename(s.path)
                        if s.kind == "subagent":
                            base = os.path.join("subagents", os.path.splitext(os.path.basename(s.parent))[0], base)
                        elif base in {e.get("filename") for e in entries.values()
                                      if e.get("session") == rec.basename}:
                            stem, ext = os.path.splitext(base)
                            base = f"{stem}.{hashlib.sha256(s.path.encode()).hexdigest()[:8]}{ext}"
                        fname, _ = _alloc_filename(asd, base, taken, 1)
                        entry = {"session": rec.basename, "source": s.path, "kind": s.kind,
                                 "filename": base, "versions": []}
                        entries[key] = entry
                        summary["new_sources"] += 1
                    elif action == "new_version":
                        taken = {v["file"] for v in entry["versions"]}
                        fname, _ = _alloc_filename(asd, entry["filename"], taken,
                                                   len(entry["versions"]) + 1)
                        summary["new_versions"] += 1
                    else:
                        fname = entry["versions"][-1]["file"]
                    dst = os.path.join(asd, fname)
                    _mkdir_private_tree(asd, os.path.dirname(dst))
                    if action != "append" and os.path.lexists(dst):  # belt: never overwrite
                        raise OSError(errno.EEXIST, "refusing to overwrite archive file", dst)
                    copied = _append_bytes(s.path, dst, start, size)
                    summary["bytes_copied"] += copied
                    summary["files_copied"] += 1
                    offset = start + copied
                    fp = _fingerprint(dst, offset)  # hash what WE hold, not the moving source
                    stamp = now()
                    if action == "append":
                        ver = entry["versions"][-1]
                        ver.update(fp)
                        ver.update({"offset": offset, "inode": sst.st_ino, "dev": sst.st_dev,
                                    "last_archived_at": stamp})
                    else:
                        entry["versions"].append(dict(fp, file=fname, offset=offset,
                                                      inode=sst.st_ino, dev=sst.st_dev,
                                                      created_at=stamp, last_archived_at=stamp,
                                                      reason=reason))
                    entry["kind"] = s.kind
                    entry["source_last_seen_at"] = stamp
                    st_all["last_run_at"] = stamp
                    _save_state(archive_root, st_all)
                except OSError as exc:
                    failed_keys.add(key)
                    if entry is not None and not entry.get("versions"):
                        entries.pop(key, None)   # a source that never got a copy is not an entry
                    summary["errors"].append({"session": rec.basename, "path": s.path,
                                              "error": str(exc)})
        if not dry_run:
            stamp = now()
            # subagent transcripts discovered but not archived, counted in RUNS (the check fires at SUBAGENT_STUCK_RUNS)
            pend = st_all.setdefault("subagent_pending", {})
            for rec in recs:
                for s in rec.subagents:
                    k = _entry_key(rec.basename, s.path)
                    if k in failed_keys:
                        pend[k] = int(pend.get(k, 0)) + 1
                    else:
                        pend.pop(k, None)
            st_all["runs"] = int(st_all.get("runs", 0)) + 1
            try:
                _save_state(archive_root, st_all)
            except OSError as exc:
                summary["errors"].append({"session": "", "path": _state_path(archive_root), "error": str(exc)})
            for rec in recs:
                if not rec.harnessed:
                    continue
                try:
                    if _write_pointer(rec, archive_root, entries, stamp):
                        summary["pointers_written"] += 1
                except OSError as exc:
                    summary["errors"].append({"session": rec.basename, "path": pointer_path(rec.session_dir),
                                              "error": f"pointer file: {exc}"})
            try:
                hb = {"ts": _iso_epoch(stamp), "ts_iso": stamp,
                      "sessions": sum(1 for r in recs if r.harnessed),
                      "files_copied": summary["files_copied"], "bytes_copied": summary["bytes_copied"],
                      "archive_bytes": archive_total_bytes(archive_root),
                      "free_bytes": (free_bytes or free_bytes_of)(archive_root),
                      "errors": len(summary["errors"]), "subagents": mode,
                      "subagents_stuck": sorted(k for k, n in pend.items() if n >= SUBAGENT_STUCK_RUNS)}
                _atomic_write_json(heartbeat_path(archive_root), hb)
                summary["heartbeat"] = hb
            except OSError as exc:
                summary["errors"].append({"session": "", "path": heartbeat_path(archive_root),
                                          "error": f"heartbeat: {exc}"})
            try:
                _maintain_baseline(archive_root, roots, projects_root, mode, summary)
            except OSError as exc:
                summary["errors"].append({"session": "", "path": baseline_path(archive_root),
                                          "error": f"baseline: {exc}"})
    finally:
        if lock_fd is not None:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)
    return summary


def run_if_enabled(*, config: Any = None, profile: Any = None, **kw: Any) -> Optional[Dict[str, Any]]:
    """The automatic path: runs only when ``transcript_archive`` is ON
    (default OFF). Returns None when off. Not wired to any caller yet."""
    from . import flags

    if not flags.flag(FLAG_NAME, config=config, profile=profile):
        return None
    return run(**kw)


# --- thinking parser ---------------------------------------------------------


def count_thinking(path: str) -> Dict[str, Any]:
    """Thinking blocks per ``message.model``: ``with_text`` (non-blank ``thinking``),
    ``empty`` (blank/absent text — Claude Code kept only the signature) and
    ``redacted`` (``redacted_thinking``). Only lines containing ``thinking`` are
    parsed, so ``malformed_lines`` counts malformed THINKING-BEARING lines (a live
    file's partial last line lands here). A row repeated with the same ``uuid`` is
    counted once (``duplicate_rows``)."""
    by: Dict[str, Dict[str, int]] = {}
    malformed = dup = 0
    seen: set = set()
    with open(path, "rb") as fh:
        for raw in fh:
            if b"thinking" not in raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                malformed += 1
                continue
            if not isinstance(row, dict) or row.get("type") != "assistant":
                continue
            msg = row.get("message")
            if not isinstance(msg, dict) or not isinstance(msg.get("content"), list):
                continue
            uid = row.get("uuid")
            if isinstance(uid, str):
                if uid in seen:
                    dup += 1
                    continue
                seen.add(uid)
            model = msg.get("model") if isinstance(msg.get("model"), str) and msg.get("model") else NO_MODEL
            for b in msg["content"]:
                if not isinstance(b, dict):
                    continue
                t = b.get("type")
                if t not in ("thinking", "redacted_thinking"):
                    continue
                c = by.setdefault(model, {"with_text": 0, "empty": 0, "redacted": 0})
                if t == "redacted_thinking":
                    c["redacted"] += 1
                elif isinstance(b.get("thinking"), str) and b["thinking"].strip():
                    c["with_text"] += 1
                else:
                    c["empty"] += 1
    return {"by_model": by, "malformed_lines": malformed, "duplicate_rows": dup}


# --- report --------------------------------------------------------------------


def _size(p: str) -> Optional[int]:
    try:
        return os.path.getsize(p)
    except OSError:
        return None


def _subagent_dir(src: str) -> str:
    return os.path.join(os.path.dirname(src), os.path.splitext(os.path.basename(src))[0], "subagents")


def report(*, roots: Optional[Sequence[str]] = None, archive_root: Optional[str] = None,
           projects_root: Optional[str] = None, thinking: bool = True,
           subagents: str = DEFAULT_SUBAGENTS) -> Dict[str, Any]:
    """Coverage report. STRICTLY read-only: opens sources and archive files for
    reading only and never creates the archive root."""
    roots = default_roots() if roots is None else roots
    archive_root = archive_root or os.environ.get(ARCHIVE_ROOT_ENV) or default_archive_root()
    mode, _warn = normalize_subagents(subagents)
    recs = discover(roots, projects_root=projects_root, subagents=mode)
    entries = load_state(archive_root)["entries"]

    harnessed = [r for r in recs if r.harnessed]
    # source path -> {kinds, sessions}
    src_info: Dict[str, Dict[str, Any]] = {}
    for r in harnessed:
        for s in r.sources:
            i = src_info.setdefault(s.path, {"kinds": set(), "sessions": set(), "archive_files": []})
            i["kinds"].add(s.kind)
            i["sessions"].add(r.basename)
    archived_sessions: set = set()
    last_archived = None
    archived_bytes = 0
    sa_arch_files = sa_arch_bytes = 0
    for e in entries.values():
        if e.get("kind") == "subagent":   # reported on their own below, never mixed into the main counts
            for v in e.get("versions", []):
                sz = _size(os.path.join(archive_root, e["session"], v["file"]))
                if sz is not None:
                    sa_arch_files += 1
                    sa_arch_bytes += sz
            continue
        files = []
        for v in e.get("versions", []):
            p = os.path.join(archive_root, e["session"], v["file"])
            sz = _size(p)
            if sz is not None:
                files.append((p, sz))
                archived_bytes += sz
            la = v.get("last_archived_at")
            if la and (last_archived is None or la > last_archived):
                last_archived = la
        if files:
            archived_sessions.add(e["session"])
        i = src_info.setdefault(e["source"], {"kinds": set(), "sessions": set(), "archive_files": []})
        i["kinds"].add(e.get("kind", "?"))
        i["sessions"].add(e["session"])
        i["archive_files"].extend(files)

    wins, losses = [], []
    live_bytes = 0
    live_count = 0
    thinking_by_model: Dict[str, Dict[str, int]] = {}
    basis = {"from_live_source": 0, "from_archive": 0, "unreadable": 0}
    malformed = dup = 0
    for path, i in sorted(src_info.items()):
        sz = _size(path)
        has_arch = bool(i["archive_files"])
        if sz is not None:
            live_bytes += sz
            live_count += 1
        elif has_arch:
            wins.append({"source": path, "sessions": sorted(i["sessions"])})
        else:
            losses.append({"source": path, "kinds": sorted(i["kinds"]),
                           "sessions": sorted(i["sessions"])})
        if not thinking:
            continue
        target = path if sz is not None else (
            max(i["archive_files"], key=lambda t: t[1])[0] if has_arch else None)
        if target is None:
            continue
        try:
            got = count_thinking(target)
        except OSError:
            basis["unreadable"] += 1
            continue
        basis["from_live_source" if sz is not None else "from_archive"] += 1
        malformed += got["malformed_lines"]
        dup += got["duplicate_rows"]
        for m, c in got["by_model"].items():
            t = thinking_by_model.setdefault(m, {"with_text": 0, "empty": 0, "redacted": 0})
            for k in t:
                t[k] += c[k]

    sa_live = {s.path for r in recs for s in r.subagents}
    sa_live_bytes = sum(_size(p) or 0 for p in sa_live)
    return {
        "archive_root": archive_root,
        "archive_root_exists": os.path.isdir(archive_root),
        "roots": list(roots),
        "missing_roots": missing_roots(roots),
        "sessions": {
            "dirs_scanned": len(recs),
            "harnessed_discovered": len(harnessed),
            "not_harnessed": len(recs) - len(harnessed),
            "no_pointer": sum(1 for r in harnessed if not r.sources),
            "archived": len(archived_sessions),
            "discovered_and_archived": len({r.basename for r in harnessed} & archived_sessions),
        },
        "sources": {"distinct": len(src_info), "live": live_count,
                    "archived": sum(1 for i in src_info.values() if i["archive_files"])},
        "bytes": {"live_sources": live_bytes, "archived": archived_bytes},
        "last_archived_at": last_archived,
        "thinking_by_model": thinking_by_model,
        "thinking_basis": dict(basis, malformed_lines=malformed, duplicate_rows=dup,
                               note="one parse per DISTINCT source: the live file if it exists, "
                                    "else its largest archived version; whole-file, unwindowed"),
        "counters": {"deleted_at_source_archived": len(wins), "lost_missing_both": len(losses)},
        "wins": wins,
        "losses": losses,
        "subagent_transcripts": {"setting": mode, "live_files": len(sa_live), "live_bytes": sa_live_bytes,
                                 "archived_files": sa_arch_files, "archived_bytes": sa_arch_bytes,
                                 "note": "<stem>/subagents/*.jsonl beside live recorded transcripts "
                                         "(archived copies are counted apart from the main transcripts)"},
        "heartbeat": read_heartbeat(archive_root),
    }


# --- the loud check (go-live smoke + the web job's alert) ---------------------


@dataclass
class Finding:
    code: str        # main_missing | main_empty | no_pointer | subagent_unarchived | heartbeat_missing | heartbeat_stale | heartbeat_future
    subject: str     # the session (or subagent key) it is about
    detail: str


CHECK_CODES = ("main_missing", "main_empty", "no_pointer", "subagent_unarchived",
               "heartbeat_missing", "heartbeat_stale", "heartbeat_future")


def _mtime(path: str) -> Optional[float]:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def coverage_check(**kw: Any) -> List[Finding]:
    """The loud check's FAILING findings: everything :func:`_coverage_findings` reports except session gaps that are
    in the baseline (those are KNOWN, see :func:`coverage_check_split`). Strictly read-only."""
    return coverage_check_split(**kw)[0]


def coverage_check_split(*, archive_root: Optional[str] = None, **kw: Any) -> Tuple[List[Finding], List[Finding]]:
    """``(failing, known)``: the loud check with the known-gaps baseline applied. No baseline file yet: every finding
    fails (the first run takes the baseline)."""
    archive_root = archive_root or os.environ.get(ARCHIVE_ROOT_ENV) or default_archive_root()
    return split_findings(_coverage_findings(archive_root=archive_root, **kw), load_baseline(archive_root))


def _coverage_findings(*, roots: Optional[Sequence[str]] = None, archive_root: Optional[str] = None,
                       projects_root: Optional[str] = None, subagents: str = DEFAULT_SUBAGENTS,
                       now_epoch: Optional[float] = None, grace_s: float = GRACE_S,
                       heartbeat_max_age_s: float = HEARTBEAT_MAX_AGE_S) -> List[Finding]:
    """Every finding of the loud check, baseline NOT applied; STRICTLY read-only (it never creates the archive). Findings:

    * ``no_pointer``: a harnessed session folder with no recorded transcript pointer;
    * ``main_missing`` / ``main_empty``: a recorded main transcript whose archived copy is missing / zero-length
      (a source or session never yet archived and younger than ``grace_s`` is not yet judged: the next timer run takes it;
      an archived copy that has since gone missing is judged at once);
    * ``subagent_unarchived``: a subagent transcript still not archived after SUBAGENT_STUCK_RUNS runs
      (not judged while the setting is ``none``);
    * ``heartbeat_missing`` / ``heartbeat_stale`` / ``heartbeat_future``: the last run's heartbeat is absent, older
      than ``heartbeat_max_age_s``, or dated in the future (invalid evidence).
    The caller decides whether the check applies at all (the flag is ON)."""
    roots = default_roots() if roots is None else roots
    archive_root = archive_root or os.environ.get(ARCHIVE_ROOT_ENV) or default_archive_root()
    mode, _w = normalize_subagents(subagents)
    now_e = time.time() if now_epoch is None else now_epoch
    st = load_state(archive_root)
    entries = st["entries"]
    out: List[Finding] = []
    for rec in discover(roots, projects_root=projects_root, subagents="none"):
        if not rec.harnessed:
            continue
        if not rec.sources:
            out.append(Finding("no_pointer", rec.basename, "harnessed session with no recorded transcript pointer"))
            continue
        launch_m = _mtime(os.path.join(rec.session_dir, ".claude", "launch.json"))
        for s in rec.sources:
            e = entries.get(_entry_key(rec.basename, s.path))
            src_size = None
            try:
                src_size = os.stat(s.path).st_size
            except OSError:
                pass
            files = []
            for v in (e or {}).get("versions", []):
                try:
                    files.append((v["file"], os.path.getsize(os.path.join(archive_root, rec.basename, v["file"]))))
                except OSError:
                    pass
            if any(sz > 0 for _f, sz in files) or (files and src_size == 0):
                continue
            if files:   # copies exist but every one is zero-length while the source has bytes (or is gone)
                out.append(Finding("main_empty", rec.basename, f"archived copy of {s.path} is zero-length"
                                   + (" (source also gone)" if src_size is None else f" (source has {src_size} bytes)")))
                continue
            if not (e or {}).get("versions"):   # never archived: a brand-new session / transcript gets the grace
                young_ref = _mtime(s.path) if src_size is not None else launch_m
                if young_ref is not None and now_e - young_ref < grace_s:
                    continue   # the next timer run takes it
            out.append(Finding("main_missing", rec.basename,
                               f"no archived copy of {s.path}" + ("" if src_size is not None else " (source also gone)")))
    if mode != "none":
        for k, n in sorted((st.get("subagent_pending") or {}).items()):
            if int(n) >= SUBAGENT_STUCK_RUNS:
                out.append(Finding("subagent_unarchived", k, f"subagent transcript not archived after {int(n)} runs"))
    hb = read_heartbeat(archive_root)
    ts = hb.get("ts") if isinstance(hb, dict) else None
    if isinstance(ts, bool) or not isinstance(ts, (int, float)):
        out.append(Finding("heartbeat_missing", archive_root, f"no usable heartbeat at {heartbeat_path(archive_root)}"
                           + ": the archive has not completed a run"))
    else:
        age = now_e - float(ts)
        if age < -HEARTBEAT_FUTURE_SKEW_S:
            out.append(Finding("heartbeat_future", archive_root, f"heartbeat is dated {-age:.0f}s in the future"))
        elif age > heartbeat_max_age_s:
            out.append(Finding("heartbeat_stale", archive_root,
                               f"last heartbeat {hb.get('ts_iso')} is {age:.0f}s old (limit {heartbeat_max_age_s:.0f}s)"))
    return out


def configured_subagents(config: Any = None) -> str:
    """The machine config's ``transcript_archive_subagents`` (default ``all``); never raises."""
    try:
        if config is None:
            from .config import ConfigStore

            config = ConfigStore().load()
        v = config.transcript_archive_subagents
        return v if isinstance(v, str) else DEFAULT_SUBAGENTS
    except Exception:  # noqa: BLE001
        return DEFAULT_SUBAGENTS


def _human(rep: Dict[str, Any]) -> str:
    s, b, c = rep["sessions"], rep["bytes"], rep["counters"]
    lines = [
        f"transcript archive: {rep['archive_root']}"
        + ("" if rep["archive_root_exists"] else "  (does not exist yet — nothing archived)"),
        f"sessions: {s['dirs_scanned']} dirs scanned, {s['harnessed_discovered']} harnessed "
        f"({s['not_harnessed']} not harnessed, {s['no_pointer']} harnessed with no recorded pointer); "
        f"archived {s['archived']} (of discovered: {s['discovered_and_archived']}/{s['harnessed_discovered']})",
        f"sources: {rep['sources']['distinct']} distinct, {rep['sources']['live']} live, "
        f"{rep['sources']['archived']} archived",
        f"bytes: live {b['live_sources']:,}  archived {b['archived']:,}",
        f"last archived: {rep['last_archived_at'] or 'never'}",
        f"deleted-at-source but archived (wins): {c['deleted_at_source_archived']}",
        f"missing in both (losses): {c['lost_missing_both']}",
    ]
    for l in rep["losses"]:
        lines.append(f"  LOST {','.join(l['kinds'])}: {l['source']}  <- {', '.join(l['sessions'])}")
    lines.append("thinking blocks per model (with text / empty / redacted):")
    tot = {"with_text": 0, "empty": 0, "redacted": 0}
    for m, t in sorted(rep["thinking_by_model"].items()):
        n = t["with_text"] + t["empty"] + t["redacted"]
        pct = f"{100.0 * t['with_text'] / n:.1f}%" if n else "-"
        lines.append(f"  {m}: {t['with_text']} / {t['empty']} / {t['redacted']}  (text {pct})")
        for k in tot:
            tot[k] += t[k]
    tb = rep["thinking_basis"]
    lines.append(f"  total: {tot['with_text']} / {tot['empty']} / {tot['redacted']}; basis: "
                 f"{tb['from_live_source']} live, {tb['from_archive']} archive-only, "
                 f"{tb['unreadable']} unreadable; malformed {tb['malformed_lines']}, "
                 f"dup rows {tb['duplicate_rows']}")
    sa = rep["subagent_transcripts"]
    lines.append(f"subagent transcripts (setting {sa['setting']}): {sa['live_files']} live "
                 f"({sa['live_bytes']:,} bytes), {sa['archived_files']} archived ({sa['archived_bytes']:,} bytes)")
    hb = rep.get("heartbeat")
    lines.append("last run heartbeat: " + (f"{hb.get('ts_iso')}, {hb.get('files_copied')} files, "
                 f"{hb.get('bytes_copied')} bytes, archive {hb.get('archive_bytes')} bytes, free "
                 f"{hb.get('free_bytes')} bytes" if hb else "none"))
    for m in rep["missing_roots"]:
        lines.append(f"WARNING: root missing: {m}")
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m gnomon.transcript_archive",
                                 description="durable transcript archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "report", "check"):
        p = sub.add_parser(name)
        p.add_argument("--roots", nargs="+", default=list(default_roots()))
        p.add_argument("--archive-root", default=None,
                       help=f"default ${ARCHIVE_ROOT_ENV} or ~/gnomon-data/transcript-archive")
        p.add_argument("--projects-root", default=None)
        p.add_argument("--json", action="store_true")
        p.add_argument("--subagents", choices=list(SUBAGENT_MODES), default=None,
                       help="default: the machine config's transcript_archive_subagents (else all)")
        if name == "run":
            p.add_argument("--dry-run", action="store_true")
            p.add_argument("--min-free-bytes", type=int, default=MIN_FREE_BYTES,
                           help="refuse to run below this much free space on the archive's volume "
                                f"(default {MIN_FREE_BYTES}, {FREE_SPACE_FLOOR_GIB} GiB)")
    bp = sub.add_parser("baseline", help="list the known-gaps baseline, or add a session to it")
    bp.add_argument("--archive-root", default=None)
    bp.add_argument("--add", metavar="SESSION", default=None)
    bp.add_argument("--reason", default=None)
    bp.add_argument("--who", default=None, help="default: the OS user")
    bp.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "baseline":
        root = a.archive_root or os.environ.get(ARCHIVE_ROOT_ENV) or default_archive_root()
        if a.add is not None:
            try:
                ent = baseline_add(root, a.add, a.reason or "", a.who)
            except ValueError as exc:
                print(f"transcript_archive: baseline: {exc}", file=sys.stderr)
                return 2
            print(f"baseline: {ent['at']} {ent['who']} added {a.add}: {ent['reason']}")
            return 0
        b = load_baseline(root)
        if b is None:
            print("baseline: none yet (the first archive run creates it)")
        elif a.json:
            print(json.dumps(b, indent=1, sort_keys=True))
        else:
            print(f"baseline: {len(b['entries'])} known session gap(s)")
            for k, e in sorted(b["entries"].items()):
                print(f"  {k}: {e.get('reason')}")
        return 0
    missing = missing_roots(a.roots)
    for m in missing:
        print(f"transcript_archive: root missing: {m}", file=sys.stderr)
    if a.cmd == "run":
        out = run(roots=a.roots, archive_root=a.archive_root, projects_root=a.projects_root,
                  dry_run=a.dry_run, subagents=a.subagents or configured_subagents(),
                  min_free_bytes=a.min_free_bytes)
        if a.json:
            print(json.dumps(out, indent=1))
        else:
            verb = "would copy" if a.dry_run else "copied"
            n = out["would_copy_bytes"] if a.dry_run else out["bytes_copied"]
            print(f"{verb} {n:,} bytes; new sources {out['new_sources']}, new versions "
                  f"{out['new_versions']}, unchanged {out['unchanged']}, source missing "
                  f"{len(out['source_missing'])}, anomalies {len(out['anomalies'])}, "
                  f"errors {len(out['errors'])} -> {out['archive_root']}")
            for x in out["anomalies"] + out["errors"] + out["warnings"]:
                print(f"  ! {x}", file=sys.stderr)
            if out["refused"]:
                print(f"  ! REFUSED: {out['refused']}", file=sys.stderr)
        rc = 1 if (out["errors"] or out["refused"]) else 0
    elif a.cmd == "check":
        fs, kn = coverage_check_split(roots=a.roots, archive_root=a.archive_root, projects_root=a.projects_root,
                                      subagents=a.subagents or configured_subagents())
        if a.json:
            print(json.dumps([f.__dict__ for f in fs], indent=1))
        else:
            for f in fs:
                print(f"FAIL {f.code}: {f.subject}: {f.detail}")
            print(f"---- {len(fs)} finding(s), {len(kn)} known (baselined)")
        rc = 1 if fs else 0
    else:
        rep = report(roots=a.roots, archive_root=a.archive_root, projects_root=a.projects_root,
                     subagents=a.subagents or configured_subagents())
        print(json.dumps(rep, indent=1) if a.json else _human(rep))
        rc = 0
    return 2 if missing and rc == 0 else rc


if __name__ == "__main__":
    sys.exit(main())

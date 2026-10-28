"""Resume-key derivation for gnomon-relaunch, the neutral/one-shot resume prompt, and the pre-spawn transcript check.

Flag: ``relaunch_derives_resume_keys`` (default OFF). Spec: ``gnomon:-
FRESH-LAUNCHES`` (``fix`` parts 2 and 3 and the ``review-2026-10-04`` conditions 2 and 3), plus the Diamond rulings of
2026-10-05 (``audits/DIAMOND-relaunch-2026-10-05.md``).

Why: a session launched FRESH (an agent provisioned fresh rather than adopted) has ``session_id`` and ``transcript_cwd`` in launch.json
(written by ``transcript.record_transcript_location``) but no ``resume_session_id`` / ``resume_cwd``, so the resume rail
refused it. The pair can be DERIVED, but only when it is provably the right transcript.

ONE TRANSCRIPT DEFINITION (:func:`inspect_transcript`), used by the derivation, by the resume RAIL (before anything is
stopped, also when the keys are already present) and by the harness's pre-spawn check: a REGULAR file (not a symlink),
readable, non-empty, with at least one parseable entry. The recency rule uses the newest ENTRY's timestamp, never the
file's mtime; a transcript with no timestamped entry is refused for derivation.

THE DERIVATION RULE (every clause must hold, else REFUSE with a message that says which clause and what the operator can do):

1. launch.json has a UUID-shaped ``session_id``, an absolute ``transcript_cwd`` and ``transcript_mode`` in
   {fresh, resume, resume-fork} (the modes whose session_id IS the conversation Claude Code is writing).
2. The project dir is the ENCODED REALPATH of ``transcript_cwd`` (``harness.context_readers.claude_project_dir_name``;
   the data root is often a symlink into a synced folder, so the string spelling is never used). A name over 200 characters is
   hash-truncated by Claude Code and cannot be verified here: refuse. ``transcript_cwd`` must be stat-able.
3. EXACTLY ONE candidate: a file named exactly ``<session_id>.jsonl`` (never a prefix match, never a directory) exists
   across ALL project dirs under ``<claude config>/projects``, and that one lives in the dir from clause 2, and it passes
   :func:`inspect_transcript` with a timestamped entry. Zero, several, wrong dir, symlink, empty: refuse.
4. LIVE POINTER AGREES: when the statusline's ``context_state.json`` names an existing transcript, it is the same file
   (realpath). A different file means the session moved on (for example after ``/clear``): refuse.
5. RECENT, TWO-SIDED: ``-FORWARD_SECONDS <= last_activity - newest_entry <= RECENT_SECONDS`` (600 s behind, 3600 s
   ahead). ``newest_entry`` is the timestamp of the last transcript entry that has a zone-aware one. ``last_activity``
   is the mtime of ``.claude/context_state.json`` (the statusline's last render, written by the live claude each turn),
   else of ``.claude/injection_log.jsonl``; neither present: refuse (fail closed). BEHIND: the transcript stopped
   growing long before the session did, so it is not the live conversation. AHEAD (looser, because a long tool run
   legitimately writes entries between statusline renders): a transcript far newer than the activity file means that
   file is no longer being written and cannot corroborate anything, so refuse rather than pass vacuously.

Derived values: ``resume_session_id = session_id``; ``resume_cwd = realpath(transcript_cwd)``. They are written, with a
``resume_keys_derived`` provenance record, by :func:`apply_launch_updates` (atomic, backup first, rolled back by
:func:`rollback_launch_updates` if the relaunch then fails).

ONE-SHOT PROMPT (``resume_prompt_once``): AT-MOST-ONCE. The harness clears it from launch.json (atomically) BEFORE the
spawn that carries it (:func:`consume_prompt_once`); a crash after the clear loses the prompt, the safer failure for text
that may be an instruction. It is honoured ONLY when resume keys are present and the text is non-blank. Arming and
delivery are both logged (who, when, sha256, first 80 chars). KNOWN LIMIT: anything able to write launch.json can arm
one (no guard rule names that file); the log is how it would be seen.

Stdlib only, no app-layer imports at module import time (the launcher imports it lazily and tolerates its absence).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

FLAG_NAME = "relaunch_derives_resume_keys"

#: launch.json key holding a deliberate ONE-SHOT resume prompt: removed by the harness BEFORE the spawn that delivers it.
ONCE_KEY = "resume_prompt_once"
#: who armed the pending one-shot (set with it, removed with it).
ONCE_META_KEY = "resume_prompt_once_meta"
#: bounded audit trail of arm/deliver events kept in launch.json (newest last).
ONCE_LOG_KEY = "resume_prompt_once_log"
ONCE_LOG_KEEP = 10
#: an armed one-shot older than this is ignored and cleared by the next harness start (relaunch fix round 3).
ONCE_MAX_AGE_SECONDS = 24 * 3600.0
#: how long a harness start waits for the launch.json lock before it declines to deliver the one-shot.
ONCE_LOCK_TIMEOUT = 5.0
PREVIEW_CHARS = 80

#: clause 5: the transcript's newest entry may trail the session's last activity by at most this many seconds ...
RECENT_SECONDS = 600.0
#: ... and may lead it by at most this many (see the module docstring for why it is looser).
FORWARD_SECONDS = 3600.0

_UUID_RE = re.compile(r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")
_ALLOWED_MODES = ("fresh", "resume", "resume-fork")
_TAIL_BYTES = 262144
#: the backward read for the last line grows to this many bytes and no further (then: refuse, saying so).
_MAX_TAIL_BYTES = 8 * 1024 * 1024
#: Claude Code hash-truncates a mangled project-dir name longer than this (transcript.py documents it).
_NAME_LIMIT = 200


def flag_on(cfg: Optional[dict] = None, config: Any = None) -> bool:
    """Is ``relaunch_derives_resume_keys`` ON? launch.json ``flags`` override > machine config > OFF.
    Never raises: any failure to read the flag is OFF (the previous behaviour)."""
    try:
        from . import flags

        return bool(flags.flag(FLAG_NAME, profile=cfg, config=config))
    except Exception:  # noqa: BLE001
        return False


def utc_stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def neutral_prompt(epoch: float) -> str:
    """The relaunch worker's resume prompt: states the fact of the relaunch, repeats no instruction."""
    return f"[Gnomon] relaunched at {utc_stamp(epoch)}; continue from your handover."


def text_digest(text: str) -> Dict[str, str]:
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(), "preview": text[:PREVIEW_CHARS]}


# ------------------------------------------------------------------ the one transcript definition

@dataclass
class Inspection:
    ok: bool
    reason: str = ""
    newest_epoch: Optional[float] = None     # newest zone-aware entry timestamp (None: no entry carried one)
    entries: int = 0                          # parseable JSON-object entries seen in the tail


def inspect_transcript(path: str) -> Inspection:
    """THE transcript definition: a regular file (not a symlink), readable, non-empty, with >= 1 parseable entry.
    Never raises. ``newest_epoch`` comes from entry timestamps only, never from the file's mtime."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return Inspection(False, f"{path} does not exist or cannot be examined ({exc})")
    if stat.S_ISLNK(st.st_mode):
        return Inspection(False, f"{path} is a symlink, not a regular transcript file")
    if not stat.S_ISREG(st.st_mode):
        return Inspection(False, f"{path} is not a regular file")
    if st.st_size == 0:
        return Inspection(False, f"{path} is empty (zero bytes)")
    # Read BACKWARDS in growing windows (256 KB, doubling, bounded at _MAX_TAIL_BYTES) until the LAST line is whole,
    # and only then decide: a valid transcript whose last line is longer than the first window is not refused for it.
    window = _TAIL_BYTES
    while True:
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, st.st_size - window))
                data = fh.read()
        except OSError as exc:
            return Inspection(False, f"{path} is not readable ({exc})")
        at_start = st.st_size <= window
        body = data.rstrip()
        if at_start or b"\n" in body:      # a newline before the last line, or the file's own start: the line is whole
            break
        if window >= _MAX_TAIL_BYTES:
            return Inspection(False, f"{path}: the last line is longer than {_MAX_TAIL_BYTES} bytes "
                                     f"({_MAX_TAIL_BYTES // (1024 * 1024)} MB), the most this check reads; the line exceeded "
                                     "the bound, so the transcript cannot be judged")
        window = min(window * 2, _MAX_TAIL_BYTES)
    lines = data.split(b"\n")
    if not at_start:
        lines = lines[1:]            # the first line of a mid-file read is probably partial
    entries, newest = 0, None
    for ln in reversed(lines):
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        entries += 1
        if newest is None:
            ts = obj.get("timestamp")
            if isinstance(ts, str):
                try:
                    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if dt.tzinfo is not None:      # a zone-less stamp is ambiguous: never read as local time
                    newest = dt.timestamp()
    if entries == 0:
        return Inspection(False, f"{path} has no parseable entry in its last {_TAIL_BYTES} bytes")
    return Inspection(True, "", newest, entries)


# ------------------------------------------------------------------ paths and candidates

def projects_root() -> str:
    from .harness.context_readers import claude_config_dir

    return os.path.join(claude_config_dir(), "projects")


def project_dir_name(cwd: str) -> str:
    from .harness.context_readers import claude_project_dir_name

    return claude_project_dir_name(cwd)


def find_candidates(session_id: str, root: str) -> List[str]:
    """Every file named EXACTLY ``<session_id>.jsonl`` directly inside any project dir under ``root`` (a directory
    of that name is not a candidate; a symlink is, and is then refused by :func:`inspect_transcript`)."""
    out: List[str] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    target = session_id + ".jsonl"
    for d in names:
        p = os.path.join(root, d, target)
        if os.path.isfile(p):
            out.append(p)
    return out


def last_activity(session_dir: str) -> Tuple[Optional[float], str]:
    cdir = os.path.join(session_dir, ".claude")
    for name in ("context_state.json", "injection_log.jsonl"):
        p = os.path.join(cdir, name)
        try:
            return os.path.getmtime(p), name
        except OSError:
            continue
    return None, "none"


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


# ------------------------------------------------------------------ derivation

@dataclass
class Derivation:
    ok: bool
    rid: str = ""
    rcwd: str = ""
    transcript_path: str = ""
    newest_epoch: Optional[float] = None
    activity_epoch: Optional[float] = None
    activity_source: str = ""
    refusal: str = ""
    notes: List[str] = field(default_factory=list)

    def summary(self) -> str:
        lag = ""
        if self.newest_epoch is not None and self.activity_epoch is not None:
            lag = f"; newest entry {self.activity_epoch - self.newest_epoch:+.0f}s from last activity ({self.activity_source})"
        return f"resume_session_id={self.rid} resume_cwd={self.rcwd} transcript={self.transcript_path}{lag}"


def _refuse(msg: str) -> Derivation:
    return Derivation(ok=False, refusal=msg)


def derive_resume_keys(session_dir: str, cfg: Dict[str, Any], *, root: Optional[str] = None) -> Derivation:
    """Evaluate the derivation rule (module docstring). READ-ONLY; never raises."""
    try:
        return _derive(session_dir, cfg, root)
    except Exception as exc:  # noqa: BLE001 - fail closed, loudly
        return _refuse(f"derivation raised {type(exc).__name__}: {exc}. Resume keys NOT derived; nothing was changed. "
                       "Operator: launch the session fresh, or report this error.")


def _derive(session_dir: str, cfg: Dict[str, Any], root: Optional[str]) -> Derivation:
    from .agent_def import SameDirUnknown, dir_identity

    sid = cfg.get("session_id")
    if not isinstance(sid, str) or not _UUID_RE.match(sid):
        return _refuse(f"clause 1: launch.json session_id is missing or not a UUID ({sid!r}). Operator: this launch "
                       "recorded no conversation id, so there is nothing to resume; launch the session fresh.")
    tcwd = cfg.get("transcript_cwd")
    if not isinstance(tcwd, str) or not os.path.isabs(os.path.expanduser(tcwd)):
        return _refuse(f"clause 1: launch.json transcript_cwd is missing or not absolute ({tcwd!r}). Operator: "
                       "launch the session fresh.")
    mode = cfg.get("transcript_mode")
    if mode not in _ALLOWED_MODES:
        return _refuse(f"clause 1: launch.json transcript_mode is {mode!r} (need one of {list(_ALLOWED_MODES)}): the "
                       "recorded session_id may not be the conversation claude wrote. Operator: launch the session "
                       "fresh (do not hand-set the resume keys until the live trial of this fix has passed).")
    rcwd = os.path.realpath(os.path.expanduser(tcwd))
    name = project_dir_name(rcwd)
    if len(name) > _NAME_LIMIT:
        return _refuse(f"clause 2: the encoded project dir name for {rcwd} is {len(name)} chars (> {_NAME_LIMIT}); "
                       "Claude Code hash-truncates such names and this tool cannot verify the transcript. Operator: "
                       "launch the session fresh.")
    try:
        dir_identity(rcwd)
    except SameDirUnknown as exc:
        return _refuse(f"clause 2: transcript_cwd cannot be identified as a directory: {exc}. Operator: launch fresh.")
    root = root if root is not None else projects_root()
    expected = os.path.join(root, name, sid + ".jsonl")
    cands = find_candidates(sid, root)
    if not cands:
        return _refuse(f"clause 3: ZERO transcript candidates: no file {sid}.jsonl in any project dir under {root} "
                       f"(expected {expected}). Operator: check CLAUDE_CONFIG_DIR and that the transcript was not "
                       "deleted; if it is gone the conversation cannot be resumed, launch the session fresh.")
    if len(cands) > 1:
        return _refuse(f"clause 3: {len(cands)} transcript candidates for session_id {sid}: {cands}. Ambiguous; "
                       "nothing was changed. Operator: remove or move the copies that are not the live conversation, "
                       "then retry.")
    if cands[0] != expected:
        return _refuse(f"clause 3: the single candidate {cands[0]} is NOT under the encoded realpath of "
                       f"transcript_cwd ({expected}). Resuming from {rcwd} would not find it. Operator: the session "
                       "was recorded with a different cwd than it ran in; launch fresh.")
    path = cands[0]
    ins = inspect_transcript(path)
    if not ins.ok:
        return _refuse(f"clause 3: the single candidate is unusable: {ins.reason}. Operator: launch the session fresh.")
    if ins.newest_epoch is None:
        return _refuse(f"clause 5: the candidate {path} has {ins.entries} parseable entries but none with a zone-aware "
                       "timestamp, so its recency cannot be judged (the file's mtime is never used). Operator: launch fresh.")
    d = Derivation(ok=True, rid=sid, rcwd=rcwd, transcript_path=path)
    # clause 4: the live pointer
    st = _read_json(os.path.join(session_dir, ".claude", "context_state.json")) or {}
    lp = st.get("transcript_path")
    if isinstance(lp, str) and os.path.isfile(lp):
        if os.path.realpath(lp) != os.path.realpath(path):
            return _refuse(f"clause 4: the statusline's live pointer names {os.path.realpath(lp)}, not the candidate "
                           f"{path}: the session has moved on to another transcript (for example after /clear). "
                           "Operator: launch fresh, or relaunch after the pointer agrees.")
        d.notes.append("live pointer agrees")
    else:
        d.notes.append("live pointer absent or names no existing file (not checked)")
    # clause 5: recency, two-sided
    act, asrc = last_activity(session_dir)
    if act is None:
        return _refuse("clause 5: no .claude/context_state.json or injection_log.jsonl in the session dir, so the "
                       "session's last activity is unknown and the transcript's recency cannot be judged. Operator: "
                       "launch the session fresh.")
    newest = ins.newest_epoch
    d.newest_epoch, d.activity_epoch, d.activity_source = newest, act, asrc
    if act - newest > RECENT_SECONDS:
        return _refuse(f"clause 5: STALE candidate: its newest entry ({utc_stamp(newest)}) is "
                       f"{act - newest:.0f}s older than the session's last activity ({utc_stamp(act)}, {asrc}); the "
                       f"limit is {RECENT_SECONDS:.0f}s. The live conversation is probably elsewhere. Operator: find "
                       "the transcript the session is writing (the statusline's context_state.json) or launch fresh.")
    if newest - act > FORWARD_SECONDS:
        return _refuse(f"clause 5: the candidate's newest entry ({utc_stamp(newest)}) is {newest - act:.0f}s NEWER than "
                       f"the session's last activity file ({utc_stamp(act)}, {asrc}); the limit is {FORWARD_SECONDS:.0f}s. "
                       "That file is no longer being written, so it cannot corroborate the transcript. Operator: "
                       "launch fresh.")
    d.notes.append("newest entry by entry timestamp")
    return d


def present_keys_check(session_dir: str, rid: str, rcwd: str, *, root: Optional[str] = None) -> Tuple[bool, str]:
    """The resume RAIL's check when the keys are ALREADY present (flag ON): read-only, run before anything is stopped.

    * ``resume_cwd`` and the session dir must both be identifiable by stat (:func:`dir_identity`); if either cannot
      be, refuse (the same-dir decision at spawn would otherwise be a guess);
    * the resumed transcript passes :func:`inspect_transcript` (the harness's pre-spawn check would refuse it AFTER
      the stop). A project dir name over 200 characters cannot be verified and is passed with a note."""
    from .agent_def import SameDirUnknown, dir_identity

    try:
        dir_identity(rcwd)
        dir_identity(session_dir)
    except SameDirUnknown as exc:
        return False, f"cannot identify resume_cwd / the session dir by stat, so refusing rather than guess: {exc}"
    name = project_dir_name(os.path.expanduser(rcwd))
    if len(name) > _NAME_LIMIT:
        return True, f"resume transcript not verified (project dir name over {_NAME_LIMIT} chars)"
    root = root if root is not None else projects_root()
    path = os.path.join(root, name, rid + ".jsonl")
    ins = inspect_transcript(path)
    if not ins.ok:
        return False, (f"the resume transcript is unusable: {ins.reason}; `claude --resume {rid}` would not find a "
                       "conversation. Operator: restore the transcript, or launch the session fresh (no resume keys).")
    return True, f"resume transcript ok ({path}, {ins.entries} entries)"


# ------------------------------------------------------------------ the worker's write, and its undo

def _updates_for(derived: Optional[Derivation], now: float, prompt_once: str, armed_by: str
                 ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], List[str]]:
    updates: Dict[str, Any] = {"resume_prompt": neutral_prompt(now)}
    if derived is not None:
        updates["resume_session_id"] = derived.rid
        updates["resume_cwd"] = derived.rcwd
        updates["resume_keys_derived"] = {
            "at": utc_stamp(now), "transcript_path": derived.transcript_path,
            "rule": "exactly one <session_id>.jsonl under the encoded realpath of transcript_cwd; live pointer agrees; "
                    f"newest entry within -{FORWARD_SECONDS:.0f}/+{RECENT_SECONDS:.0f}s of last activity",
            "last_activity": derived.activity_source, "notes": list(derived.notes)}
    log_entry: Optional[Dict[str, Any]] = None
    if prompt_once:
        meta = {"armed_by": armed_by or "gnomon-relaunch --prompt-once", "armed_at": utc_stamp(now), **text_digest(prompt_once)}
        updates[ONCE_KEY] = prompt_once
        updates[ONCE_META_KEY] = meta
        log_entry = {"event": "armed", "at": utc_stamp(now), "by": meta["armed_by"], "sha256": meta["sha256"],
                     "preview": meta["preview"]}
    keys = list(updates) + ([ONCE_LOG_KEY] if log_entry else [])
    return updates, log_entry, keys


def apply_launch_updates(session_dir: str, *, derived: Optional[Derivation], now: float, prompt_once: str = "",
                         armed_by: str = "") -> Tuple[bool, str, Optional[str], List[str]]:
    """A single write to launch.json (atomic, ALWAYS preceded by a backup of the previous file) of the values the
    relaunch worker decided on. The worker itself uses :func:`prepare_launch_updates`, which also makes the decision
    INSIDE the same lock; this form takes a decision already made.

    Sets ``resume_prompt`` to the neutral line; adds the derived pair + ``resume_keys_derived`` when ``derived``; arms the
    one-shot (key + who/when/sha256/preview + an audit-log entry) when given. Re-reads the file and verifies every value
    landed. Returns ``(ok, detail, backup_path, keys_written)``; never raises."""
    from .agent_def import edit_launch_json

    updates, log_entry, keys = _updates_for(derived, now, prompt_once, armed_by)

    def _mutate(cfg: Dict[str, Any]) -> None:
        if log_entry is not None:
            cfg[ONCE_LOG_KEY] = (list(cfg.get(ONCE_LOG_KEY) or []) + [log_entry])[-ONCE_LOG_KEEP:]

    bak: Optional[str] = None
    try:
        bak = edit_launch_json(session_dir, set_keys=updates, backup=True, mutate=_mutate)
        back = _read_json(os.path.join(session_dir, ".claude", "launch.json")) or {}
        missing = [k for k, v in updates.items() if back.get(k) != v]
        if missing:
            return False, f"launch.json re-read does not carry {missing}", bak, keys
    except Exception as exc:  # noqa: BLE001
        return False, f"launch.json write failed ({type(exc).__name__}: {exc}); the previous file is intact", bak, keys
    return True, "launch.json updated: " + ", ".join(sorted(updates)), bak, keys


# ------------------------------------------------------------------ the worker's decision + write under ONE lock, and its journal

JOURNAL_NAME = "launch.json.relaunch-journal"
#: how long a relaunch worker waits for the launch.json lock while preparing (a refusal, not a hang, beyond this).
PREPARE_LOCK_TIMEOUT = 30.0


def journal_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", JOURNAL_NAME)


def journal_write(session_dir: str, record: Dict[str, Any]) -> None:
    """Atomically (tmp + replace, 0600) write the worker's journal beside launch.json."""
    path = journal_path(session_dir)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def journal_read(session_dir: str) -> Optional[Dict[str, Any]]:
    """The leftover journal of a worker that never settled, or None. A present but unreadable journal comes back as
    ``{"unreadable": True}`` (it is still evidence that a worker did not finish)."""
    path = journal_path(session_dir)
    if not os.path.lexists(path):
        return None
    obj = _read_json(path)
    return obj if obj is not None else {"unreadable": True}


def journal_clear(session_dir: str) -> None:
    try:
        os.remove(journal_path(session_dir))
    except FileNotFoundError:
        pass


def describe_journal(j: Dict[str, Any], session_dir: str) -> str:
    if j.get("unreadable"):
        return (f"a relaunch journal {journal_path(session_dir)} exists but cannot be read: a relaunch worker did not "
                "finish. Operator: inspect launch.json by hand before relaunching, then delete the journal.")
    return (f"a relaunch worker (pid {j.get('pid')}, started {j.get('at')}) wrote launch.json keys {j.get('keys')} and "
            f"never settled (it died or was killed); the next relaunch rolls them back first "
            f"(journal {journal_path(session_dir)})")


def rollback_journal(session_dir: str) -> Tuple[bool, str]:
    """Undo, under the launch.json lock, what a worker that never settled wrote: each key it names goes back to the
    value the journal recorded (or is removed if it was absent). The journal is deleted only when that succeeded."""
    from .agent_def import edit_launch_json

    j = journal_read(session_dir)
    if j is None:
        return True, "no journal"
    if j.get("unreadable") or not isinstance(j.get("keys"), list) or not isinstance(j.get("before"), dict):
        return False, (f"the journal {journal_path(session_dir)} cannot be used to roll back; launch.json was NOT "
                       "touched. Operator: inspect launch.json by hand, then delete the journal.")
    keys, before = list(j["keys"]), dict(j["before"])

    def _undo(cfg: Dict[str, Any]) -> None:
        for k in keys:
            if k in before:
                cfg[k] = before[k]
            else:
                cfg.pop(k, None)
    try:
        edit_launch_json(session_dir, mutate=_undo, lock_timeout=PREPARE_LOCK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        return False, f"journal rollback failed ({type(exc).__name__}: {exc}); launch.json may carry keys {keys}"
    journal_clear(session_dir)
    return True, f"rolled back keys {sorted(keys)} written by worker pid {j.get('pid')}"


@dataclass
class Prepared:
    ok: bool
    refusal: str = ""            # ok False because the decision refused (nothing written)
    detail: str = ""
    backup: Optional[str] = None
    keys: List[str] = field(default_factory=list)
    derived: Optional[Derivation] = None
    resume_session_id: Any = None


class _Refused(Exception):
    pass


def prepare_launch_updates(session_dir: str, *, now: float, prompt_once: str = "", armed_by: str = "",
                           pid: int = 0) -> Prepared:
    """The relaunch worker's ONE launch.json write, with the "derive or present" decision made INSIDE the launch.json
    lock on the file read inside that lock (so what is decided on is what is written over). The journal naming the keys
    (with their previous values) is written under the same lock BEFORE the file is replaced.

    Refusals (nothing written, no backup left): derivation or the present-keys check fails. Never raises."""
    from .agent_def import edit_launch_json

    st: Dict[str, Any] = {}

    def _mutate(cfg: Dict[str, Any]) -> None:
        derived = None
        if not cfg.get("resume_session_id") and not cfg.get("resume_cwd"):
            derived = derive_resume_keys(session_dir, cfg)
            if not derived.ok:
                raise _Refused("resume keys cannot be derived safely; nothing stopped: " + derived.refusal)
        else:
            ok, note = present_keys_check(session_dir, str(cfg.get("resume_session_id") or ""),
                                          str(cfg.get("resume_cwd") or ""))
            if not ok:
                raise _Refused("the resume transcript or directory is no longer usable; nothing stopped: " + note)
        updates, log_entry, keys = _updates_for(derived, now, prompt_once, armed_by)
        before = {k: cfg[k] for k in keys if k in cfg}
        journal_write(session_dir, {"pid": pid or os.getpid(), "at": utc_stamp(now), "keys": keys, "before": before})
        st.update(derived=derived, keys=keys, updates=updates, rid=cfg.get("resume_session_id"))
        cfg.update(updates)
        if log_entry is not None:
            cfg[ONCE_LOG_KEY] = (list(cfg.get(ONCE_LOG_KEY) or []) + [log_entry])[-ONCE_LOG_KEEP:]

    bak: Optional[str] = None
    try:
        bak = edit_launch_json(session_dir, backup=True, mutate=_mutate, lock_timeout=PREPARE_LOCK_TIMEOUT)
    except _Refused as exc:
        return Prepared(False, refusal=str(exc))      # raised before the journal write: a leftover journal is not ours to clear
    except Exception as exc:  # noqa: BLE001
        if "keys" in st:                   # the journal was written but the replace did not land: nothing to undo
            journal_clear(session_dir)
        return Prepared(False, detail=f"launch.json write failed ({type(exc).__name__}: {exc}); the previous file is intact",
                        backup=bak, keys=list(st.get("keys") or []))
    back = _read_json(os.path.join(session_dir, ".claude", "launch.json")) or {}
    missing = [k for k, v in st["updates"].items() if back.get(k) != v]
    if missing:
        return Prepared(False, detail=f"launch.json re-read does not carry {missing}", backup=bak, keys=st["keys"],
                        derived=st["derived"], resume_session_id=st["rid"])
    return Prepared(True, detail="launch.json updated: " + ", ".join(sorted(st["updates"])), backup=bak, keys=st["keys"],
                    derived=st["derived"], resume_session_id=st["rid"])


def rollback_launch_updates(session_dir: str, backup: Optional[str], keys: List[str]) -> Tuple[bool, str]:
    """Undo :func:`apply_launch_updates` after a relaunch that did NOT happen (the stop or the launch failed).

    Restores, atomically and under the launch.json lock, each key the worker wrote to its value in ``backup`` (or
    removes it if the backup lacks it). Key-level on purpose: a session that is still running may have written other
    keys since; those are kept. When nothing else changed, the result is byte-identical to the backup. Never raises."""
    from .agent_def import edit_launch_json

    if not backup or not os.path.isfile(backup):
        return False, f"no backup to restore from ({backup!r}); launch.json keys {keys} were NOT rolled back"
    orig = _read_json(backup)
    if orig is None:
        return False, f"backup {backup} is unreadable; launch.json keys {keys} were NOT rolled back"

    def _undo(cfg: Dict[str, Any]) -> None:
        for k in keys:
            if k in orig:
                cfg[k] = orig[k]
            else:
                cfg.pop(k, None)
    try:
        edit_launch_json(session_dir, mutate=_undo)
    except Exception as exc:  # noqa: BLE001
        return False, f"rollback failed ({type(exc).__name__}: {exc}); launch.json may carry keys {keys}"
    return True, f"launch.json restored from {os.path.basename(backup)}: {sorted(keys)}"


# ------------------------------------------------------------------ harness side (launcher imports these lazily)

def parse_armed_at(stamp: Any) -> Optional[float]:
    """Epoch of an ``armed_at`` stamp written by :func:`utc_stamp`; None when absent or not that format."""
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def once_age(cfg: Dict[str, Any], now: float) -> Tuple[str, Optional[float]]:
    """``(state, age_seconds)`` of the one-shot in ``cfg``: state is ``none`` (no armed text), ``unknown`` (armed with no
    usable arming record for THIS text, so its age cannot be told: honoured as before, shown as unknown),
    ``ok`` (age known, within :data:`ONCE_MAX_AGE_SECONDS`) or ``expired`` (older than that, or an arming time that is
    unreadable or more than an hour in the future: refused rather than trusted)."""
    text = cfg.get(ONCE_KEY)
    if not isinstance(text, str) or not text:
        return "none", None
    meta = cfg.get(ONCE_META_KEY) if isinstance(cfg.get(ONCE_META_KEY), dict) else {}
    if meta.get("sha256") != text_digest(text)["sha256"] or "armed_at" not in meta:
        return "unknown", None
    at = parse_armed_at(meta.get("armed_at"))
    if at is None:
        return "expired", None
    age = now - at
    if age < -3600.0 or age > ONCE_MAX_AGE_SECONDS:
        return "expired", age
    return "ok", age


def describe_once(cfg: Dict[str, Any], now: float) -> str:
    """One plain line about an armed one-shot for the dry run (text LENGTH and age, never the text); '' when none."""
    state, age = once_age(cfg, now)
    if state == "none":
        return ""
    text = cfg.get(ONCE_KEY)
    n = len(text) if isinstance(text, str) else 0
    if age is None:
        age_s = "armed time unknown" if state == "unknown" else "armed time unreadable"
    else:
        age_s = f"armed {age / 3600.0:.1f} h ago"
    verdict = ("will be IGNORED and cleared by the next start (older than 24 h)" if state == "expired"
               else f"will be delivered by whichever harness start comes first, if resume keys are present (expires after "
                    f"{ONCE_MAX_AGE_SECONDS / 3600.0:.0f} h)")
    return f"an armed one-shot resume prompt is pending in launch.json ({n} characters, {age_s}): {verdict}"


def _log_decision(session_dir: str, entry: Dict[str, Any], decision: str) -> None:
    try:
        from .harness.hooks import append_record, injection_log_path

        append_record(injection_log_path(session_dir), {
            "kind": "resume-prompt-once", "source": "harness", "trigger": "resume-prompt-once", "decision": decision,
            "armed_by": entry.get("armed_by"), "armed_at": entry.get("armed_at"), "sha256": entry.get("sha256"),
            "preview": entry.get("preview"), "reason": entry.get("reason")})
    except Exception as exc:  # noqa: BLE001 - the launch.json trail is the durable record
        sys.stderr.write(f"gnomon harness: ⚠ one-shot {decision} not written to the injection log ({exc})\n")


def apply_prompt_once(cfg: Dict[str, Any]) -> bool:
    """Harness start, flag ON: arm the one-shot in memory (``resume_prompt`` := its text). It is honoured ONLY in resume
    mode (a non-empty ``resume_session_id``), when the text is non-blank and when it is not expired. Returns True when
    armed. In-memory only; the caller must then call :func:`consume_prompt_once` before building the argv."""
    once = cfg.get(ONCE_KEY)
    rid = cfg.get("resume_session_id")
    if isinstance(once, str) and once.strip() and isinstance(rid, str) and rid and once_age(cfg, _now())[0] != "expired":
        cfg["resume_prompt"] = once
        return True
    return False


def _now() -> float:
    import time as _time

    return _time.time()


def expire_prompt_once(session_dir: str, cfg: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """Harness start, flag ON, BEFORE :func:`apply_prompt_once`: a one-shot armed more than 24 hours ago (or with an
    unreadable arming time) is IGNORED and CLEARED, with a log entry (launch.json trail + injection log). The check is
    repeated inside the launch.json lock on a fresh read. Returns True when an expired one-shot was cleared (and is then
    absent from ``cfg``). Never raises; a failure to clear leaves it armed but it is still not delivered (the in-memory
    ``cfg`` loses it for this start)."""
    from .agent_def import edit_launch_json

    now = _now() if now is None else now
    if once_age(cfg, now)[0] != "expired":
        return False
    cleared: Dict[str, Any] = {}

    def _mutate(c: Dict[str, Any]) -> None:
        state, age = once_age(c, now)
        if state != "expired":
            return                                   # another start already dealt with it, or it was re-armed fresh
        text = c.get(ONCE_KEY) if isinstance(c.get(ONCE_KEY), str) else ""
        meta = c.get(ONCE_META_KEY) if isinstance(c.get(ONCE_META_KEY), dict) else {}
        dig = text_digest(text)
        entry = {"event": "expired", "at": utc_stamp(now), "armed_by": meta.get("armed_by"),
                 "armed_at": meta.get("armed_at"), "age_seconds": None if age is None else round(age),
                 "reason": f"older than {ONCE_MAX_AGE_SECONDS / 3600.0:.0f} h (or armed time unreadable): ignored and cleared",
                 **dig}
        for k in (ONCE_KEY, ONCE_META_KEY):
            c.pop(k, None)
        c[ONCE_LOG_KEY] = (list(c.get(ONCE_LOG_KEY) or []) + [entry])[-ONCE_LOG_KEEP:]
        cleared.update(entry)
    try:
        edit_launch_json(session_dir, mutate=_mutate, lock_timeout=ONCE_LOCK_TIMEOUT)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"gnomon harness: ⚠ an expired one-shot resume prompt could not be cleared ({exc}); "
                         "it is NOT delivered by this start\n")
        cfg.pop(ONCE_KEY, None)
        cfg.pop(ONCE_META_KEY, None)
        return False
    cfg.pop(ONCE_KEY, None)
    cfg.pop(ONCE_META_KEY, None)
    if cleared:
        sys.stderr.write("gnomon harness: an armed one-shot resume prompt older than 24 h was ignored and "
                         f"cleared (armed {cleared.get('armed_at')}; sha256 {str(cleared.get('sha256'))[:12]})\n")
        _log_decision(session_dir, cleared, "expired")
    return bool(cleared)


def consume_prompt_once(session_dir: str, cfg: Dict[str, Any], *, now: Optional[float] = None) -> bool:
    """AT-MOST-ONCE across overlapping starts: the read-check-clear happens under ONE exclusive lock on launch.json's
    lock file (the one the writers use), and the file is READ INSIDE that lock. The text in ``cfg`` (read earlier,
    outside any lock) is delivered only if the locked read still holds the SAME text; the key is removed in the same
    locked write and the delivery logged. Whoever finds the key gone or changed, or cannot take the lock within
    :data:`ONCE_LOCK_TIMEOUT` seconds, does NOT deliver, and logs why.

    Returns True = deliver ``cfg['resume_prompt']`` (the text); False = do not (the caller restores the prompt it had).
    Raises ``ValueError`` if the locked clear itself fails: the launcher then refuses the launch (a crash after the clear
    loses the prompt; that is the safer failure)."""
    from .agent_def import LockTimeout, edit_launch_json

    now = _now() if now is None else now
    text = cfg.get(ONCE_KEY) if isinstance(cfg.get(ONCE_KEY), str) else ""
    dig = text_digest(text)
    out: Dict[str, Any] = {"delivered": False}

    def _mutate(c: Dict[str, Any]) -> None:
        cur = c.get(ONCE_KEY)
        meta = c.get(ONCE_META_KEY) if isinstance(c.get(ONCE_META_KEY), dict) else {}
        if cur != text or not text:
            reason = ("the one-shot was already consumed by another start" if cur is None
                      else "the one-shot in launch.json is no longer the text this start read")
            entry = {"event": "not-delivered", "at": utc_stamp(now), "reason": reason, **dig}
            c[ONCE_LOG_KEY] = (list(c.get(ONCE_LOG_KEY) or []) + [entry])[-ONCE_LOG_KEEP:]
            out["entry"] = entry
            return
        armed_by = meta.get("armed_by") if meta.get("sha256") == dig["sha256"] else None
        entry = {"event": "delivered", "at": utc_stamp(now),
                 "armed_by": armed_by or "UNKNOWN (no arming record for this text)",
                 "armed_at": meta.get("armed_at") if armed_by else None, **dig}
        for k in (ONCE_KEY, ONCE_META_KEY):
            c.pop(k, None)
        c[ONCE_LOG_KEY] = (list(c.get(ONCE_LOG_KEY) or []) + [entry])[-ONCE_LOG_KEEP:]
        out["delivered"] = True
        out["entry"] = entry
    try:
        edit_launch_json(session_dir, mutate=_mutate, lock_timeout=ONCE_LOCK_TIMEOUT)
    except LockTimeout as exc:
        reason = (f"could not take the launch.json lock within {ONCE_LOCK_TIMEOUT:g}s ({exc}); another writer or start "
                  "holds it, so this start does NOT deliver the one-shot (it stays armed for the holder or a later start)")
        sys.stderr.write(f"gnomon harness: ⚠ one-shot resume prompt NOT delivered: {reason}\n")
        _log_decision(session_dir, {"armed_by": None, "armed_at": None, "reason": reason, **dig}, "not-delivered")
        return False
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"cannot clear {ONCE_KEY} from launch.json ({type(exc).__name__}: {exc}); refusing to deliver "
                         "a one-shot prompt that could not be consumed (it stays armed and undelivered)") from exc
    entry = out.get("entry") or {}
    if not out["delivered"]:
        sys.stderr.write(f"gnomon harness: ⚠ one-shot resume prompt NOT delivered by this start: {entry.get('reason')}\n")
        _log_decision(session_dir, entry, "not-delivered")
        return False
    _log_decision(session_dir, entry, "delivered")
    return True


def check_resume_transcript(cfg: Dict[str, Any], transcript_plan: Dict[str, Any]) -> Optional[str]:
    """Pre-spawn check for a resume launch, with the SAME definition as the rail (:func:`inspect_transcript`).

    Raises ``ValueError`` (the launcher turns it into 'launch refused', exit 2, before any spawn) when the resumed
    conversation's file is missing, a symlink, unreadable, empty or entry-less. Returns the path checked, or None when
    not applicable / not verifiable (a >200-char project dir name)."""
    rid = cfg.get("resume_session_id")
    if not (isinstance(rid, str) and rid):
        return None
    pdir = transcript_plan.get("transcript_project_dir")
    if not isinstance(pdir, str) or not pdir:
        return None
    if len(os.path.basename(pdir)) > _NAME_LIMIT:
        sys.stderr.write("gnomon harness: ⚠ resume transcript NOT verified: the project dir name is over "
                         f"{_NAME_LIMIT} chars (Claude Code hash-truncates it)\n")
        return None
    path = os.path.join(pdir, rid + ".jsonl")
    ins = inspect_transcript(path)
    if not ins.ok:
        raise ValueError(
            f"resume transcript unusable: {ins.reason}, so `claude --resume {rid}` run in "
            f"{transcript_plan.get('transcript_cwd')} would not find the conversation. Fix resume_session_id / "
            "resume_cwd in launch.json, or remove both keys to launch a fresh conversation")
    return path

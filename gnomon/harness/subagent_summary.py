"""-- the harness writes a subagent's SUMMARY file from its RETURNED TEXT.

Driven by the SubagentStop hook (:mod:`.subagent_stop_hook`), whose payload carries ``last_assistant_message``
(the subagent's final text; Claude Code 2.1.286 hook-input schema) plus ``agent_id``, ``agent_type`` and
``agent_transcript_path``. A top-level Agent call's PostToolUse carries only an ``async_launched`` stub, which is
why this is NOT a PostToolUse feature. Only when ``subagent_summary_md`` is ON (registry default OFF).

Where the file goes:

* the brief's ``SUMMARY-PATH: <path>`` when the subagent's brief declares one (read from the first user turn of
  the agent transcript) AND the path passes confinement: relative paths resolve against the session dir; the
  result must sit under the session dir or an explicitly allowed root, with no ``..``, no symlinked component, a
  plain filename (``[A-Za-z0-9._-]``, no leading dot). A declared path that fails any check is REJECTED (recorded,
  never followed) and the DEFAULT is used;
* otherwise ``<session_dir>/subagent-summaries/SUMMARY-<utc>-<agent-type>-<agent-id>.md``.

Properties (checklist g48), each tested: runs after the hook's work, on a daemon thread with a hard join bound
(the flag read, the JSON parse and every file read are INSIDE it); ``O_NONBLOCK`` opens that refuse a non-regular
file; confined to its declared dirs by FILE DESCRIPTOR, not by path (each directory component is opened with
``O_DIRECTORY|O_NOFOLLOW`` relative to its parent's fd, and the file is created with ``dir_fd``, so a component swapped
to a symlink after the check cannot redirect the write; the path recorded is the one the fd denotes); the bytes go to a
TEMP name in that directory and are hard-linked to the final name only when COMPLETE (``link`` never overwrites: a
numeric suffix on collision), so a write cut short by the bound, a fault or a kill never leaves a partial file under a
final name; old orphan temps (``.gm-summary-tmp-*.tmp``, older than :data:`TMP_MAX_AGE_S`) are swept on the next run;
filename parts from a closed character set (the description and anything free-form go in the file BODY, one-lined and
capped); size-capped with a visible truncation notice; never raises, never prints.
No session dir (``--session-dir`` / ``GNOMON_SESSION_DIR``, absolute) => no file: it never guesses a location.

Neutral (g3); stdlib only apart from one lazy, guarded ``flags`` import and the lazy provenance/feature imports.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

FLAG = "subagent_summary_md"
FEATURE_ID = "subagent-summary-md"            # the feature-registry node
PROVENANCE_KIND = "subagent-summary"          # the injection_log record kind (g9)
ENV_SESSION_DIR = "GNOMON_SESSION_DIR"
SUBDIR = "subagent-summaries"
#: hard wall-clock bound (s) on EVERYTHING the hook does for X18 (flag read, parse, reads, write, log)
WORK_BOUND_S = 1.5
#: cap on the saved text, in BYTES (UTF-8); beyond it the text is cut and the file SAYS SO
MAX_TEXT_BYTES = 256 * 1024
MAX_DESC_CHARS = 200
MAX_PART_CHARS = 40
MAX_BRIEF_BYTES = 1 << 20                     # the transcript's first line is read up to this
MAX_TAIL_BYTES = 1 << 20                      # the fallback last-assistant-turn read
#: temp-file naming for the atomic write (F2); the sweep removes ONLY regular files that match this pattern and are
#: older than TMP_MAX_AGE_S (an in-flight write from another run is young), looking at no more than SWEEP_MAX_ENTRIES
TMP_PREFIX = ".gm-summary-tmp-"
TMP_SUFFIX = ".tmp"
TMP_MAX_AGE_S = 600.0
SWEEP_MAX_ENTRIES = 500
_NO_HARDLINK_ERRNOS = {errno.EPERM, errno.ENOTSUP, errno.ENOSYS, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}
_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | os.O_NONBLOCK
              | getattr(os, "O_CLOEXEC", 0))
_SAFE = re.compile(r"[^a-z0-9-]+")
_PATH_RE = re.compile(r"SUMMARY-PATH[ \t]*[:=][ \t]*[`'\"<]?([^\s`'\">]+)")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")


def flag_on() -> bool:
    """The flag via the registry (profile > machine config > registry default OFF); a failed read is OFF."""
    try:
        from .. import flags as _f

        return bool(_f.flag(FLAG))
    except Exception:  # noqa: BLE001
        return False


def _slug(value: Any, fallback: str) -> str:
    try:
        s = _SAFE.sub("-", str(value).lower()).strip("-")[:MAX_PART_CHARS].strip("-")
        return s or fallback
    except Exception:  # noqa: BLE001
        return fallback


def _oneline(value: Any, limit: int) -> str:
    s = "".join(ch if (ch.isprintable() and ch not in "\r\n\t") else " " for ch in str(value or ""))
    return " ".join(s.split())[:limit]


# --- reading the agent transcript (bounded, regular files only) ---------------------------------------


def _read_regular(path: str, *, tail: bool = False, limit: int = MAX_BRIEF_BYTES) -> Optional[bytes]:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        if tail and st.st_size > limit:
            os.lseek(fd, st.st_size - limit, os.SEEK_SET)
        return os.read(fd, limit)
    finally:
        os.close(fd)


def _blocks_text(content: Any) -> Optional[str]:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [b.get("text") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)]
        return "\n".join(parts) if parts else None
    return None


def read_brief(transcript_path: Any) -> Optional[str]:
    """The subagent's brief: the first user turn of its transcript JSONL. None when unreachable."""
    try:
        if not isinstance(transcript_path, str) or not os.path.isabs(transcript_path):
            return None
        data = _read_regular(transcript_path)
        if not data:
            return None
        for line in data.split(b"\n")[:5]:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if isinstance(msg, dict) and msg.get("role") == "user":
                return _blocks_text(msg.get("content"))
        return None
    except Exception:  # noqa: BLE001
        return None


def last_assistant_text(transcript_path: Any) -> Optional[str]:
    """Fallback when the payload lacks ``last_assistant_message``: the last assistant turn's text, from the
    transcript's tail. None when unreachable."""
    try:
        if not isinstance(transcript_path, str) or not os.path.isabs(transcript_path):
            return None
        data = _read_regular(transcript_path, tail=True, limit=MAX_TAIL_BYTES)
        if not data:
            return None
        for line in reversed(data.split(b"\n")):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            msg = rec.get("message") if isinstance(rec, dict) else None
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                t = _blocks_text(msg.get("content"))
                if t and t.strip():
                    return t
        return None
    except Exception:  # noqa: BLE001
        return None


def read_meta(transcript_path: Any) -> Dict[str, Any]:
    """``agent-<id>.meta.json`` beside the transcript: ``description`` and ``agentType``. {} if absent."""
    try:
        if not isinstance(transcript_path, str) or not transcript_path.endswith(".jsonl"):
            return {}
        data = _read_regular(transcript_path[:-len(".jsonl")] + ".meta.json", limit=65536)
        meta = json.loads(data) if data else {}
        return meta if isinstance(meta, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def declared_summary_path(brief: Optional[str]) -> Optional[str]:
    """The brief's ``SUMMARY-PATH:`` value, unvalidated (untrusted); None when it declares none."""
    m = _PATH_RE.search(brief or "")
    return m.group(1) if m else None


# --- extraction ---------------------------------------------------------------------------------------


def extract(payload: Any) -> Optional[Dict[str, Any]]:
    """The facts to write from a SubagentStop payload; None when it is not one or carries no text."""
    if not isinstance(payload, dict) or payload.get("hook_event_name") not in (None, "SubagentStop"):
        return None
    tpath = payload.get("agent_transcript_path")
    text = payload.get("last_assistant_message")
    if not (isinstance(text, str) and text.strip()):
        text = last_assistant_text(tpath)
    if not (isinstance(text, str) and text.strip()):
        return None
    meta = read_meta(tpath)
    agent_id = payload.get("agent_id") or ""
    return {"type": payload.get("agent_type") or meta.get("agentType") or "agent",
            "description": meta.get("description") or "", "agent_id": agent_id if isinstance(agent_id, str) else "",
            "text": text, "transcript_path": tpath}


def render(info: Dict[str, Any], now: float, *, path_source: str, rejected: Optional[str] = None
           ) -> Tuple[bytes, str, int, bool]:
    """(file bytes, sha256 of the FULL text, full byte length, truncated)."""
    raw = info["text"].encode("utf-8", "replace")
    sha = hashlib.sha256(raw).hexdigest()
    body, notice, truncated = raw, "", False
    if len(raw) > MAX_TEXT_BYTES:
        truncated = True
        body = raw[:MAX_TEXT_BYTES].decode("utf-8", "ignore").encode("utf-8")
        notice = (f"> TRUNCATED by the harness: the returned text was {len(raw)} bytes; only the first "
                  f"{len(body)} bytes are kept (cap {MAX_TEXT_BYTES}). The sha256 above is of the FULL text.\n\n")
    head = ("---\n"
            f"agent_type: {_oneline(info['type'], MAX_PART_CHARS)}\n"
            f"agent_id: {_oneline(info['agent_id'], 80)}\n"
            f"description: {_oneline(info['description'], MAX_DESC_CHARS)}\n"
            f"time: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now))}\n"
            f"path_source: {path_source}\n"
            + (f"summary_path_rejected: {_oneline(rejected, 160)}\n" if rejected else "")
            + f"returned_text_bytes: {len(raw)}\n"
            f"returned_text_sha256: {sha}\n"
            "written_by: gnomon harness; the body below is the subagent's returned text\n"
            "---\n\n")
    out = head.encode("utf-8") + notice.encode("utf-8") + body + (b"" if body.endswith(b"\n") else b"\n")
    return out, sha, len(raw), truncated


# --- confinement --------------------------------------------------------------------------------------


def _norm_session(session_dir: Any) -> Optional[str]:
    if isinstance(session_dir, str) and os.path.isabs(session_dir) and os.path.isdir(session_dir):
        return os.path.realpath(session_dir)
    return None


def _under(root: str, path: str) -> bool:
    try:
        return os.path.commonpath([root, path]) == root
    except ValueError:
        return False


def _open_dir_chain(path: str, root: str) -> int:
    """Open ``path`` (creating missing components, mode 0700) and return an fd to it, proving a real directory chain under
    ``root``: ``root`` is opened, then EVERY component is opened ``O_DIRECTORY|O_NOFOLLOW`` relative to its parent's fd
    (a symlink, FIFO or file at any level is refused by the kernel at the open itself, with no check-then-use gap). The
    caller creates files relative to the returned fd and closes it. Raises OSError."""
    if not _under(root, path):
        raise OSError(errno.EPERM, "directory escapes the allowed root")
    rel = os.path.relpath(path, root)
    parts = [] if rel == "." else rel.split(os.sep)
    if any(p in ("", ".", "..") for p in parts):
        raise OSError(errno.EPERM, "directory path is not normalised")
    fd = os.open(root, _DIR_FLAGS)
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _fd_path(fd: int) -> str:
    """The absolute path the directory fd denotes NOW (macOS ``F_GETPATH``; Linux ``/proc/self/fd``). Raises OSError
    when the platform offers neither: the caller then publishes nothing rather than record a path it cannot vouch for."""
    try:
        import fcntl

        raw = fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024))
        return os.fsdecode(raw.split(b"\0", 1)[0])
    except (ImportError, AttributeError, OSError):
        pass
    try:
        return os.readlink("/proc/self/fd/%d" % fd)
    except OSError:
        raise OSError(errno.ENOTSUP, "cannot name the directory behind a file descriptor on this platform")


def resolve_declared(declared: str, session_dir: str, allowed_roots: List[str]) -> Tuple[str, str]:
    """(directory, filename) for a brief-declared SUMMARY-PATH, or raises ValueError(reason). The path is
    untrusted input: ``~`` and env are NOT expanded; relative => under the session dir; no ``..``."""
    if "\x00" in declared or declared.startswith("~") or "$" in declared or declared.endswith("/"):
        raise ValueError("unexpanded or unsafe characters")
    p = declared if os.path.isabs(declared) else os.path.join(session_dir, declared)
    if ".." in p.split(os.sep):
        raise ValueError("parent reference")
    p = os.path.normpath(p)
    d, name = os.path.split(p)
    if not _NAME_RE.match(name):
        raise ValueError("filename outside the closed set")
    for root in [session_dir] + allowed_roots:
        if _under(root, d if d else p):
            return d, name
        # the path may name a not-yet-real dir under a symlinked spelling of the root (e.g. /var -> /private/var)
        if _under(root, os.path.realpath(d)):
            return os.path.realpath(d), name
    raise ValueError("outside the session dir and every allowed root")


def _unlink_quiet(dfd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dfd)
    except OSError:
        pass


def _publish(dfd: int, tmp: str, stem: str, ext: str) -> str:
    """Give the COMPLETE temp file its final name ``stem[-n]ext`` without ever overwriting: ``link`` fails EEXIST if the
    name is taken (a FIFO or anything else there included), so the suffix loop moves on. Filesystems without hard links
    fall back to a not-exists check + ``rename``. Returns the final NAME."""
    for i in range(1000):
        name = "%s%s%s" % (stem, "" if i == 0 else "-%d" % (i + 1), ext)
        try:
            os.link(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd, follow_symlinks=False)
        except FileExistsError:
            continue
        except NotImplementedError:
            pass
        except OSError as exc:
            if exc.errno not in _NO_HARDLINK_ERRNOS:
                raise
        else:
            _unlink_quiet(dfd, tmp)
            return name
        # no hard links on this filesystem: not-exists check + rename (a small, documented race)
        try:
            os.lstat(name, dir_fd=dfd)
            continue
        except FileNotFoundError:
            pass
        os.rename(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        return name
    raise OSError(errno.EEXIST, "no free name")


def _create_exclusive(d: Any, stem: str, ext: str, data: bytes) -> str:
    """Create ``stem[-n]ext`` in directory ``d`` (an open directory fd, or a path opened here) holding ``data``, and
    return the ABSOLUTE path actually written (the directory fd's own path + the name). The bytes are written to a
    temp name in the same directory first (``O_EXCL``, ``O_NONBLOCK``, ``O_NOFOLLOW``, a regular file only) and
    published only when complete (:func:`_publish`), so nothing partial ever carries a final name; on any error the temp
    is removed and the error raised; if the bound kills the thread instead, the orphan temp is swept by a later run."""
    own = isinstance(d, (str, os.PathLike))
    dfd = os.open(d, _DIR_FLAGS) if own else d
    try:
        tmp = None
        for _ in range(100):
            cand = "%s%d-%s%s" % (TMP_PREFIX, os.getpid(), os.urandom(6).hex(), TMP_SUFFIX)
            try:
                fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0),
                             0o600, dir_fd=dfd)
            except FileExistsError:
                continue
            tmp = cand
            break
        if tmp is None:
            raise OSError(errno.EEXIST, "no free temp name")
        published = None
        try:
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):    # defence in depth: our own fresh file is regular
                    raise OSError(errno.ENOTSUP, "not a regular file")
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
            finally:
                os.close(fd)
            published = _publish(dfd, tmp, stem, ext)
            try:
                return os.path.join(_fd_path(dfd), published)
            except OSError:
                _unlink_quiet(dfd, published)                 # a file we cannot name is not left behind
                raise
        except BaseException:
            if published is None:
                _unlink_quiet(dfd, tmp)
            raise
    finally:
        if own:
            os.close(dfd)


def _sweep_orphans(dfd: int) -> None:
    """Remove OLD orphan temps (a run killed mid-write by the bound) from the directory behind ``dfd``. Only regular
    files named ``TMP_PREFIX*TMP_SUFFIX`` and older than :data:`TMP_MAX_AGE_S`; ``lstat`` so a symlink or directory is
    never touched; bounded by :data:`SWEEP_MAX_ENTRIES`; every error swallowed."""
    try:
        now, seen = time.time(), 0
        with os.scandir(dfd) as it:
            for entry in it:
                seen += 1
                if seen > SWEEP_MAX_ENTRIES:
                    break
                name = entry.name
                if not (name.startswith(TMP_PREFIX) and name.endswith(TMP_SUFFIX)):
                    continue
                try:
                    st = os.lstat(name, dir_fd=dfd)
                    if stat.S_ISREG(st.st_mode) and now - st.st_mtime > TMP_MAX_AGE_S:
                        os.unlink(name, dir_fd=dfd)
                except OSError:
                    pass
    except OSError:
        pass


def write_summary(payload: Any, session_dir: Any, allowed_roots: Optional[List[str]] = None,
                  now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Write the file. Returns the facts (never content) of what was written, or None. Raises on I/O faults
    (the bounded caller swallows them)."""
    root = _norm_session(session_dir)
    if root is None:
        return None                                           # never guess a location
    info = extract(payload)
    if info is None:
        return None
    now = time.time() if now is None else now
    roots = [r for r in (os.path.realpath(x) for x in (allowed_roots or []) if isinstance(x, str)
                         and os.path.isabs(x) and os.path.isdir(x))]
    declared = declared_summary_path(read_brief(info["transcript_path"]))
    if not declared:
        # provenance filter (ruling-undispatched-agents-2026-10-02): no declared SUMMARY-PATH => write only for a
        # NAMED agent_type (not the generic "agent") WITH a non-empty description; else no file, and a visible skip.
        atype, desc = str(info["type"] or "").strip(), str(info["description"] or "").strip()
        if not atype or atype == "agent":
            reason = "no declared SUMMARY-PATH and agent_type is generic"
        elif not desc:
            reason = "no declared SUMMARY-PATH and empty description"
        else:
            reason = None
        if reason:
            return {"skipped": reason, "agent_id": info["agent_id"], "agent_type": info["type"],
                    "bytes": len(info["text"].encode("utf-8", "replace"))}
    target: Optional[Tuple[str, str]] = None
    rejected: Optional[str] = None
    dfd: Optional[int] = None
    if declared:
        try:
            target = resolve_declared(declared, root, roots)
            allowed = [root] + roots
            base = next(r for r in allowed if _under(r, target[0]))
            dfd = _open_dir_chain(target[0], base)
        except (ValueError, OSError) as exc:
            target, rejected = None, "%s: %s" % (declared[:100], exc)
    if target is not None:
        name = target[1]
        stem, ext = os.path.splitext(name)
        source = "declared"
    else:
        dfd = _open_dir_chain(os.path.join(root, SUBDIR), root)
        stem = "SUMMARY-%s-%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now)),
                                      _slug(info["type"], "agent"), _slug(info["agent_id"], "noid"))
        ext, source = ".md", "default"
    data, sha, n, truncated = render(info, now, path_source=source, rejected=rejected)
    try:
        path = _create_exclusive(dfd, stem, ext, data)
        _sweep_orphans(dfd)
    finally:
        os.close(dfd)
    return {"path": path, "path_source": source, "rejected": rejected, "agent_id": info["agent_id"],
            "agent_type": info["type"], "bytes": n, "sha256": sha, "truncated": truncated}


def _log_provenance(session_dir: str, facts: Dict[str, Any]) -> None:
    """g9: one injection_log record, kind ``subagent-summary`` (metadata only, never content)."""
    from .hooks import append_record, injection_log_path

    append_record(injection_log_path(session_dir), {
        "kind": PROVENANCE_KIND, "event": "written", "path": facts["path"], "path_source": facts["path_source"],
        "agent_id": facts["agent_id"], "agent_type": _oneline(facts["agent_type"], MAX_PART_CHARS),
        "bytes": facts["bytes"], "sha256": facts["sha256"], "truncated": facts["truncated"],
        **({"summary_path_rejected": facts["rejected"]} if facts["rejected"] else {})})


def _log_skipped(session_dir: str, facts: Dict[str, Any]) -> None:
    """One injection_log record, kind ``subagent-summary``, event ``subagent-summary-skipped`` (metadata only)."""
    from .hooks import append_record, injection_log_path

    append_record(injection_log_path(session_dir), {
        "kind": PROVENANCE_KIND, "event": "subagent-summary-skipped", "agent_id": facts["agent_id"],
        "agent_type": _oneline(facts["agent_type"], MAX_PART_CHARS), "bytes": facts["bytes"],
        "reason": facts["skipped"]})


def _emit_feature(session_dir: str, outcome: str) -> None:
    from .. import feature_events as _fe

    _fe.set_process_kind("hooks")
    _fe.set_session_dir(session_dir)
    _fe.feature_event(FEATURE_ID, None, "hook", outcome=outcome)


def handle(raw_stdin: str, session_dir: Optional[str], allowed_roots: Optional[List[str]] = None) -> bool:
    """The hook's whole X18 job, BOUNDED: flag read, parse, reads, write, log, all on one daemon thread the
    caller waits for at most :data:`WORK_BOUND_S`. Returns True iff a file was written. Never raises/prints."""
    try:
        res = [False]

        def run() -> None:
            sd = session_dir or os.environ.get(ENV_SESSION_DIR) or None
            try:
                if not raw_stdin or not flag_on():
                    return
                payload = json.loads(raw_stdin)
                facts = write_summary(payload, sd, allowed_roots)
                if facts is None:
                    return
                if facts.get("skipped"):
                    try:
                        _log_skipped(os.path.realpath(sd), facts)
                    except Exception:  # noqa: BLE001
                        pass
                    return
                res[0] = True
                try:
                    _log_provenance(os.path.realpath(sd), facts)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    _emit_feature(sd, "ok")
                except Exception:  # noqa: BLE001
                    pass
            except BaseException:  # noqa: BLE001
                try:
                    if sd and flag_on():
                        _emit_feature(sd, "error")
                except BaseException:  # noqa: BLE001
                    pass

        t = threading.Thread(target=run, name="gm-summary-md", daemon=True)
        t.start()
        t.join(WORK_BOUND_S)
        return res[0] if not t.is_alive() else False
    except BaseException:  # noqa: BLE001
        return False

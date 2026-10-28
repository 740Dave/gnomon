"""the ``search_transcript`` self-verb: search YOUR OWN transcript chain.

An agent that has compacted (or been relaunched) loses the detail from its live
context, but Claude Code's jsonl transcript is APPEND-ONLY: a compaction writes a
``system/compact_boundary`` line into the SAME file and the pre-compaction records
stay on disk above it (measured 2026-09-30: one real 81 MB session file holds 12
compact boundaries and one ``sessionId`` throughout). So "across a compaction
boundary" needs no second file. A CHAIN of more than one file arises from
relaunches / resume-forks / ``/clear`` and is recorded by D40 in the
session's ``launch.json`` (``transcript_path``, ``session_id``,
``previous_session_ids``, ``resume_session_id``, ``transcript_project_dir``) and the
statusline hook's ``context_state.json`` live pointer.

ARCHITECT BOUNDARY (binding in-flight-2026-09-30)
-----------------------------------------------------------
1. SCOPE. The chain is resolved from harness identity (``launch_context`` over
   the session's private control socket gives ``session_dir``) plus the session's OWN
   ``<session_dir>/.claude/launch.json`` and ``context_state.json``. Those two files
   are agent-writable, so the guarantee is ONLY that no MCP ARGUMENT can widen scope:
   the verb has NO path / session-id / instance-name parameter, the tool schema is
   closed, and ``self_mcp._command_for`` refuses any unknown argument. As defence in
   depth every file must also resolve (realpath) to a ``*.jsonl`` under
   ``<claude config>/projects``. This is scope hygiene, not a security boundary
   (architect ruling on Diamond F1, 2026-09-30): an agent with a shell can read the
   same files, and one that edits its own launch.json can point the chain at another
   projects-dir transcript. Do not describe it as an isolation guarantee.
2. OUTPUT BOUNDS. Every parameter has a floor and a ceiling; a result carries
   locality and a bounded snippet, never a whole record; ``total_matches`` and
   ``truncated`` are always reported.
3. FAIL LOUD. A missing/unreadable transcript is ``ok: False`` with a reason, never
   an empty result that reads as "no matches". A missing PREDECESSOR does not fail
   the search but is reported (``chain.complete: False`` + a warning).
4. READ-ONLY. Transcripts are opened ``"r"`` only. The only write is one count-only
   provenance row in the session's own injection log (query LENGTH, never text).

The query is a LITERAL substring (``re.escape``d, so no backtracking hazard); there
is deliberately no regex option.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .context_readers import claude_config_dir, claude_project_dir_name
from .operator_activity import _parse_iso
from .self_mcp import SERVER_NAME as _SELF_SERVER_NAME

FLAG_NAME = "search_transcript_verb"
#: The X26 feature-id this verb WILL emit once feature_event exists (G1).
FEATURE_ID = "search_transcript"
#: The provenance ``kind`` written to the session's injection log (counts only).
PROVENANCE_KIND = "transcript-search"

ROLES = ("user", "assistant", "tool")

DEFAULT_MAX_RESULTS = 10
MAX_RESULTS_FLOOR = 1
MAX_RESULTS_CEILING = 50
DEFAULT_SNIPPET_CHARS = 300
SNIPPET_CHARS_FLOOR = 50
SNIPPET_CHARS_CEILING = 1000
MAX_QUERY_CHARS = 200
#: Wall-clock cap on one scan. Hit => ok:True but ``scan_complete: False`` (loud).
SCAN_DEADLINE_SECONDS = 20.0
#: Lines between clock reads in the pre-pass (the deadline covers TOTAL wall time).
PRESCAN_CHECK_LINES = 2000
#: g29(c) — the transcript-related ``launch.json`` fields the launcher writes
#: (``transcript.plan_transcript`` / ``record_transcript_location`` / ``record_exit_check``)
#: that :func:`resolve_chain` deliberately does NOT read, each with its reason. Every OTHER
#: transcript field the writer emits must be read by resolve_chain; a guard test writes
#: launch.json through the real writer and fails when a new field is neither.
LAUNCH_FIELDS_IGNORED: Dict[str, str] = {
    "transcript_mode": "informational (fresh/resume/fork); the chain is built from ids and paths",
    "transcript_cwd": "the recorded project dir already encodes it; resume_cwd is read for the parent's dir",
    "transcript_link": "resolve_chain builds <session_dir>/.claude/transcript.jsonl itself from the session_dir",
    "transcript_live_pointer": "resolve_chain builds <session_dir>/.claude/context_state.json itself",
    "transcript_recorded_at": "a timestamp, not a location",
    "transcript_exit_check": "a post-run witness written by a different writer; the chain never needs it",
}
#: Only queries made of these characters use the raw-line prefilter (JSON string
#: escaping cannot alter them); anything else is matched on every parsed record.
_PREFILTER_SAFE_RE = re.compile(r"^[A-Za-z0-9 _.:/@#-]+$")
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
#: Arguments the verb accepts. Anything else (path, session_id, file, ...) is refused
#: by ``self_mcp._command_for`` before it gets here, and re-checked in
#: :func:`validate_params`.
PARAM_NAMES = ("query", "max_results", "snippet_chars", "role", "since", "until", "case_sensitive",
               "include_own_calls")


class SearchError(Exception):
    """A fail-loud refusal. The message never contains the query text."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


# --- parameters ---------------------------------------------------------------


def _int_param(args: Dict[str, Any], key: str, default: int, floor: int, ceiling: int,
               clamped: List[str]) -> int:
    if key not in args or args[key] is None:
        return default
    v = args[key]
    if isinstance(v, bool) or not isinstance(v, int):
        raise SearchError("bad-param", f"'{key}' must be an integer")
    if v < floor:
        raise SearchError("bad-param", f"'{key}' must be >= {floor}")
    if v > ceiling:
        clamped.append(f"{key} {v} -> {ceiling} (ceiling)")
        return ceiling
    return v


def _time_param(args: Dict[str, Any], key: str) -> Optional[float]:
    if key not in args or args[key] is None:
        return None
    v = args[key]
    if not isinstance(v, str) or not v.strip():
        raise SearchError("bad-param", f"'{key}' must be an ISO-8601 timestamp string")
    s = v.strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        s += "T00:00:00Z"
    if not re.search(r"(Z|[+-]\d{2}:?\d{2})$", s):
        s += "Z"  # naive == UTC
    ts = _parse_iso(s)
    if ts is None:
        raise SearchError("bad-param", f"'{key}' is not a valid ISO-8601 timestamp")
    return ts


def validate_params(args: Any) -> Dict[str, Any]:
    """Validate + normalise the caller's arguments. Raises :class:`SearchError`."""
    if not isinstance(args, dict):
        raise SearchError("bad-param", "arguments must be an object")
    extra = sorted(set(args) - set(PARAM_NAMES))
    if extra:
        # No path / session id / instance name can widen the scope: refused loudly.
        raise SearchError("bad-param", f"unknown argument(s): {extra}; this verb searches only "
                                       f"YOUR OWN transcript chain and accepts {list(PARAM_NAMES)}")
    q = args.get("query")
    if not isinstance(q, str) or not q.strip():
        raise SearchError("bad-param", "'query' is required and must be a non-empty string")
    if len(q) > MAX_QUERY_CHARS:
        raise SearchError("bad-param", f"'query' is too long ({len(q)} > {MAX_QUERY_CHARS} chars)")
    clamped: List[str] = []
    max_results = _int_param(args, "max_results", DEFAULT_MAX_RESULTS, MAX_RESULTS_FLOOR,
                             MAX_RESULTS_CEILING, clamped)
    snippet = _int_param(args, "snippet_chars", DEFAULT_SNIPPET_CHARS, SNIPPET_CHARS_FLOOR,
                         SNIPPET_CHARS_CEILING, clamped)
    role = args.get("role")
    if role is not None and role not in ROLES:
        raise SearchError("bad-param", f"'role' must be one of {list(ROLES)}")
    cs = args.get("case_sensitive", False)
    if cs is None:
        cs = False
    if not isinstance(cs, bool):
        raise SearchError("bad-param", "'case_sensitive' must be a boolean")
    ioc = args.get("include_own_calls", False)
    if ioc is None:
        ioc = False
    if not isinstance(ioc, bool):
        raise SearchError("bad-param", "'include_own_calls' must be a boolean")
    since, until = _time_param(args, "since"), _time_param(args, "until")
    if since is not None and until is not None and since > until:
        raise SearchError("bad-param", "'since' is after 'until'")
    return {"query": q, "max_results": max_results, "snippet_chars": snippet, "role": role,
            "since": since, "until": until, "case_sensitive": cs, "include_own_calls": ioc,
            "since_raw": args.get("since"), "until_raw": args.get("until"), "clamped": clamped}


# --- the chain -----------------------------------------------------------------


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def _under(path: str, root: str) -> bool:
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def resolve_chain(session_dir: str, *, projects_root: Optional[str] = None) -> Dict[str, Any]:
    """The calling session's transcript chain, OLDEST FIRST, from its own state.

    Sources, in order: ``previous_session_ids`` (older runs of this session dir),
    ``resume_session_id`` when it differs from the current id (a resume-fork's
    parent), the recorded ``transcript_path``, then the statusline hook's LIVE
    pointer (a ``/clear`` successor). Deduplicated by real path. Returns
    ``{ok, files: [{path, session_id, source, exists}], warnings, complete}`` or
    ``{ok: False, error, error_kind}``. Never takes a path from the caller.
    """
    if not isinstance(session_dir, str) or not session_dir:
        return {"ok": False, "error_kind": "no-session-dir",
                "error": "the harness reported no session_dir for this session"}
    claude_dir = os.path.join(session_dir, ".claude")
    cfg = _read_json(os.path.join(claude_dir, "launch.json"))
    if cfg is None:
        return {"ok": False, "error_kind": "no-launch-json",
                "error": "this session's launch.json is missing or unreadable, so its "
                         "transcript location is unknown"}
    root = projects_root if projects_root is not None else os.path.join(claude_config_dir(), "projects")
    cur_id = cfg.get("session_id") if isinstance(cfg.get("session_id"), str) else None
    project_dirs: List[str] = []
    pd = cfg.get("transcript_project_dir")
    if isinstance(pd, str) and pd:
        project_dirs.append(pd)
    rc = cfg.get("resume_cwd")
    if isinstance(rc, str) and rc:
        project_dirs.append(os.path.join(root, claude_project_dir_name(rc)))

    entries: List[Tuple[str, Optional[str], str, bool]] = []  # (path, sid, source, required)

    def _predecessor(sid: str, source: str) -> None:
        if not _ID_RE.match(sid):
            entries.append(("", sid[:40], source + ":invalid-id", False))
            return
        for d in project_dirs:
            cand = os.path.join(d, sid + ".jsonl")
            if os.path.isfile(cand):
                entries.append((cand, sid, source, False))
                return
        entries.append((os.path.join(project_dirs[0], sid + ".jsonl") if project_dirs else "",
                        sid, source, False))

    for sid in cfg.get("previous_session_ids") or []:
        if isinstance(sid, str) and sid != cur_id:
            _predecessor(sid, "previous_session_ids")
    rsid = cfg.get("resume_session_id")
    if isinstance(rsid, str) and rsid and rsid != cur_id:
        _predecessor(rsid, "resume_session_id")
    tp = cfg.get("transcript_path")
    if isinstance(tp, str) and tp:
        entries.append((tp, cur_id, "transcript_path", True))
    live = _read_json(os.path.join(claude_dir, "context_state.json"))
    lp = live.get("transcript_path") if live else None
    if isinstance(lp, str) and lp:
        lsid = live.get("session_id") if isinstance(live.get("session_id"), str) else None
        entries.append((lp, lsid, "live_pointer", False))
    if not any(e[3] for e in entries) and not (live and lp):
        link = os.path.join(claude_dir, "transcript.jsonl")
        if os.path.islink(link):
            entries.append((os.path.realpath(link), cur_id, "transcript_link", True))
    if not any(e[0] for e in entries if e[2] in ("transcript_path", "live_pointer", "transcript_link")):
        return {"ok": False, "error_kind": "no-transcript-recorded",
                "error": "no transcript is recorded for this session"
                         + (f" ({cfg['transcript_unknown_reason']})" if cfg.get("transcript_unknown_reason") else "")}

    files: List[Dict[str, Any]] = []
    warnings: List[str] = []
    seen: set = set()
    current_ok = False
    current_missing: List[str] = []
    for path, sid, source, required in entries:
        if not path:
            warnings.append(f"{source}: predecessor {sid!r} could not be located")
            continue
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        if not real.endswith(".jsonl") or not _under(real, root):
            warnings.append(f"{source}: {os.path.basename(path)} is not a *.jsonl under the claude projects dir; skipped")
            if required:
                current_missing.append(path)
            continue
        exists = os.path.isfile(real) and os.access(real, os.R_OK)
        files.append({"path": real, "session_id": sid or os.path.basename(real)[:-6],
                      "source": source, "exists": exists})
        if not exists:
            warnings.append(f"{source}: {real} is missing or unreadable")
            if required:
                current_missing.append(real)
        elif source != "previous_session_ids" and source != "resume_session_id":
            current_ok = True
    if not current_ok:
        return {"ok": False, "error_kind": "transcript-unreadable",
                "error": "this session's own transcript is missing or unreadable: " + "; ".join(warnings or ["no readable file"]),
                "files": files}
    return {"ok": True, "files": [f for f in files], "warnings": warnings,
            "complete": not warnings, "current_missing": current_missing}


# --- record extraction ----------------------------------------------------------


def is_own_tool_name(name: Any) -> bool:
    """True ONLY for this server's own tool: the bare name or ``mcp__<self server>__search_transcript``.
    Another MCP server's identically-named tool is a different tool and is NOT excluded."""
    return isinstance(name, str) and name in (FEATURE_ID, f"mcp__{_SELF_SERVER_NAME}__{FEATURE_ID}")


def _block_texts(content: Any, own_ids: Optional[set] = None) -> Tuple[str, bool]:
    """(text, has_tool_result) for a message ``content``. With ``own_ids`` (F4) the
    verb's OWN earlier invocations are left out: ``tool_use`` blocks naming this verb
    and ``tool_result`` blocks answering one of ``own_ids``."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts: List[str] = []
    tool = False
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str):
            parts.append(b["text"])
        elif t == "thinking" and isinstance(b.get("thinking"), str):
            parts.append(b["thinking"])
        elif t == "tool_use":
            if own_ids is not None and is_own_tool_name(b.get("name")):
                continue
            try:
                inp = json.dumps(b.get("input"), ensure_ascii=False)
            except (TypeError, ValueError):
                inp = ""
            parts.append(f"[tool_use {b.get('name')}] {inp}")
        elif t == "tool_result":
            tool = True
            if own_ids is not None and b.get("tool_use_id") in own_ids:
                continue
            c = b.get("content")
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                parts.extend(x["text"] for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
    return "\n".join(p for p in parts if p), tool


def classify(rec: Dict[str, Any], own_ids: Optional[set] = None) -> Optional[Tuple[str, str]]:
    """``(role, text)`` for a searchable record, else ``None`` (bookkeeping lines).
    ``own_ids`` not None => exclude this verb's own calls/results (see ``_block_texts``)."""
    t = rec.get("type")
    msg = rec.get("message")
    if t not in ("user", "assistant") or not isinstance(msg, dict):
        return None
    text, tool = _block_texts(msg.get("content"), own_ids)
    if t == "assistant":
        return "assistant", text
    return ("tool" if tool else "user"), text


def _snippet(text: str, start: int, qlen: int, width: int) -> str:
    """At most ``width`` chars around the match (ellipses count against ``width``)."""
    if len(text) <= width:
        return text
    pad = max(0, (width - qlen) // 2)
    lo = max(0, start - pad)
    hi = min(len(text), lo + width)
    lo = max(0, hi - width)
    s = text[lo:hi]
    if lo > 0:
        s = "\u2026" + s[1:]
    if hi < len(text):
        s = s[:-1] + "\u2026"
    return s


# --- the scan ---------------------------------------------------------------------


class _PrepassDeadline(Exception):
    """The time budget ran out during the pre-pass."""


def _prescan(path: str, size: int, expired: Optional[Callable[[], bool]] = None) -> Tuple[int, List[int], set]:
    """One cheap forward pass over EXACTLY the bytes ``[0, size)`` (``size`` was pinned once,
    before any pass, so appends made while the search runs are invisible):
    ``(total_lines, boundary_line_numbers, own_tool_use_ids)``. Boundaries give
    ``compactions_before`` for a backwards scan; the ids let the scan recognise the results
    of this verb's own earlier calls (which come AFTER the call). Raises
    :class:`_PrepassDeadline` when ``expired()`` turns true."""
    if expired is not None and expired():
        raise _PrepassDeadline()
    total = 0
    consumed = 0
    boundaries: List[int] = []
    own_ids: set = set()
    with open(path, "rb") as fh:
        for raw in fh:
            if consumed >= size:
                break
            if consumed + len(raw) > size:
                raw = raw[:size - consumed]   # the pinned region ends inside this line
            consumed += len(raw)
            total += 1
            if expired is not None and total % PRESCAN_CHECK_LINES == 0 and expired():
                raise _PrepassDeadline()
            if b'"compact_boundary"' in raw:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    rec = None
                if isinstance(rec, dict) and rec.get("type") == "system" \
                        and rec.get("subtype") == "compact_boundary":
                    boundaries.append(total)
                    continue
            if FEATURE_ID.encode() in raw and b'"tool_use"' in raw:
                try:
                    rec = json.loads(raw)
                except ValueError:
                    continue
                msg = rec.get("message") if isinstance(rec, dict) else None
                content = msg.get("content") if isinstance(msg, dict) else None
                for b in content if isinstance(content, list) else []:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and is_own_tool_name(b.get("name")) \
                            and isinstance(b.get("id"), str):
                        own_ids.add(b["id"])
    return total, boundaries, own_ids


def _iter_lines_reverse(path: str, total_lines: int, *, size: Optional[int] = None,
                        block: int = 1 << 20):
    """Yield ``(lineno, bytes_without_newline)`` from the LAST line to the first, so a
    time-boxed scan reaches the newest records first. Covers exactly ``[0, size)`` (the
    pinned size; the whole file when ``None``), and ``total_lines`` must be the forward
    line count of that same region (:func:`_prescan`) so numbering matches."""
    with open(path, "rb") as fh:
        if size is None:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
        pos = size
        tail = b""
        lineno = total_lines
        first = True
        while pos > 0:
            n = min(block, pos)
            pos -= n
            fh.seek(pos)
            parts = (fh.read(n) + tail).split(b"\n")
            tail = parts[0]
            body = parts[1:]
            if first and body and body[-1] == b"":
                body = body[:-1]  # the file's trailing newline is not a line
            first = False
            for line in reversed(body):
                yield lineno, line
                lineno -= 1
        if total_lines > 0:
            yield lineno, tail


def search_chain(files: List[Dict[str, Any]], params: Dict[str, Any], *,
                 deadline_seconds: float = SCAN_DEADLINE_SECONDS,
                 clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Scan ``files`` (given oldest first) NEWEST FIRST: the last file first (the
    current transcript / live pointer), each file from its last line backwards, then
    predecessors newest to oldest. A deadline cut therefore leaves the OLDEST records
    unsearched. Results are newest-first (the first ``max_results`` met);
    ``total_matches`` counts every matching RECORD (not occurrences) that was scanned."""
    q = params["query"]
    flags = 0 if params["case_sensitive"] else re.IGNORECASE
    pat = re.compile(re.escape(q), flags)
    prefilter = _PREFILTER_SAFE_RE.match(q) is not None
    q_raw = q.encode("utf-8") if params["case_sensitive"] else q.lower().encode("utf-8")
    exclude_own = not params.get("include_own_calls", False)
    max_results = params["max_results"]
    results: List[Dict[str, Any]] = []
    total = 0
    started = clock()   # BEFORE the pre-pass: the deadline bounds total wall time
    scan_complete = True
    stop_reasons: List[str] = []
    per_file: List[Dict[str, Any]] = []
    for f in files:
        per_file.append({"file": f["path"], "session_id": f["session_id"], "source": f["source"],
                         "status": "ok" if f["exists"] else "missing", "lines_scanned": 0,
                         "compactions": 0})
    # PIN each file's size ONCE, before any pass: the pre-pass and the reverse read both cover
    # exactly [0, pinned), so bytes appended while we search (the live transcript grows) cannot
    # shift line numbers or compaction offsets.
    sizes: Dict[int, int] = {}
    for i, f in enumerate(files):
        if f["exists"]:
            try:
                sizes[i] = os.path.getsize(f["path"])
            except OSError as exc:
                per_file[i]["status"] = f"unreadable: {type(exc).__name__}"
                scan_complete = False
                if "unreadable" not in stop_reasons:
                    stop_reasons.append("unreadable")

    def _expired() -> bool:
        return clock() - started > deadline_seconds

    # pre-pass over every readable file (oldest first) -> cumulative compaction offsets
    pre: Dict[int, Tuple[int, List[int], set]] = {}
    offsets: Dict[int, int] = {}
    running = 0
    own_ids: set = set()
    for i, f in enumerate(files):
        if i not in sizes:
            continue
        try:
            pre[i] = _prescan(f["path"], sizes[i], _expired)
        except _PrepassDeadline:
            for j in sizes:
                per_file[j]["status"] = "not-scanned"
            return {"total_matches": 0, "returned": 0, "truncated": False, "results": [],
                    "scan_complete": False, "stop_reasons": ["deadline-prepass"],
                    "compactions_seen": running, "files": per_file,
                    "scan_seconds": round(clock() - started, 3)}
        except OSError as exc:
            per_file[i]["status"] = f"unreadable: {type(exc).__name__}"
            scan_complete = False
            if "unreadable" not in stop_reasons:
                stop_reasons.append("unreadable")
            continue
        offsets[i] = running
        running += len(pre[i][1])
        per_file[i]["compactions"] = len(pre[i][1])
        own_ids |= pre[i][2]
    scanning_own = own_ids if exclude_own else None
    first_scanned = True
    cut = False
    for i in range(len(files) - 1, -1, -1):
        if i not in pre:
            continue
        info = per_file[i]
        if cut:
            info["status"] = "not-scanned"
            continue
        # The newest file always starts (progress guarantee); older ones only in budget.
        if not first_scanned and clock() - started > deadline_seconds:
            cut = True
            scan_complete = False
            stop_reasons.append("deadline")
            info["status"] = "not-scanned"
            continue
        first_scanned = False
        total_lines, boundaries, _ = pre[i]
        seen = 0
        try:
            for lineno, rawb in _iter_lines_reverse(files[i]["path"], total_lines, size=sizes[i]):
                seen += 1
                if seen % 500 == 0 and clock() - started > deadline_seconds:
                    cut = True
                    scan_complete = False
                    stop_reasons.append("deadline")
                    info["status"] = "scan-deadline"
                    seen -= 1  # this line was not searched
                    break
                if prefilter and q_raw not in (rawb if params["case_sensitive"] else rawb.lower()):
                    continue
                try:
                    rec = json.loads(rawb.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if not isinstance(rec, dict):
                    continue
                cl = classify(rec, scanning_own)
                if cl is None:
                    continue
                role, text = cl
                if params["role"] and role != params["role"]:
                    continue
                ts_raw = rec.get("timestamp")
                if params["since"] is not None or params["until"] is not None:
                    ts = _parse_iso(ts_raw)
                    if ts is None:
                        continue
                    if params["since"] is not None and ts < params["since"]:
                        continue
                    if params["until"] is not None and ts > params["until"]:
                        continue
                m = pat.search(text)
                if m is None:
                    continue
                total += 1
                if len(results) < max_results:
                    results.append({
                        "file": files[i]["path"], "session_id": files[i]["session_id"], "line": lineno,
                        "timestamp": ts_raw if isinstance(ts_raw, str) else None, "role": role,
                        "uuid": rec.get("uuid") if isinstance(rec.get("uuid"), str) else None,
                        "compactions_before": offsets[i] + bisect.bisect_left(boundaries, lineno),
                        "snippet": _snippet(text, m.start(), len(q), params["snippet_chars"]),
                    })
        except OSError as exc:
            info["status"] = f"unreadable: {type(exc).__name__}"
            scan_complete = False
            if "unreadable" not in stop_reasons:
                stop_reasons.append("unreadable")
            continue
        info["lines_scanned"] = seen
    return {"total_matches": total, "returned": len(results),
            "truncated": total > len(results), "results": results,
            "scan_complete": scan_complete, "stop_reasons": stop_reasons,
            "compactions_seen": running, "files": per_file,
            "scan_seconds": round(clock() - started, 3)}


def search_own_transcript(session_dir: str, args: Any, *, projects_root: Optional[str] = None,
                          deadline_seconds: float = SCAN_DEADLINE_SECONDS,
                          clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """Validate ``args``, resolve THIS session's chain from ``session_dir``, search.
    Always returns a dict with ``ok``; never raises; the query is never echoed."""
    try:
        params = validate_params(args)
    except SearchError as exc:
        return {"ok": False, "error_kind": exc.kind, "error": str(exc)}
    chain = resolve_chain(session_dir, projects_root=projects_root)
    if not chain.get("ok"):
        return {"ok": False, "error_kind": chain["error_kind"], "error": chain["error"]}
    try:
        found = search_chain(chain["files"], params, deadline_seconds=deadline_seconds, clock=clock)
    except Exception as exc:  # noqa: BLE001 - surface, never crash the MCP server
        return {"ok": False, "error_kind": "scan-failed",
                "error": f"transcript scan failed: {type(exc).__name__}"}
    out: Dict[str, Any] = {
        "ok": True, "feature": FEATURE_ID, "query_chars": len(params["query"]),
        "total_matches": found["total_matches"], "returned": found["returned"],
        "truncated": found["truncated"], "order": "newest-first",
        "results": found["results"],
        "effective": {"max_results": params["max_results"], "snippet_chars": params["snippet_chars"],
                      "role": params["role"], "since": params["since_raw"], "until": params["until_raw"],
                      "case_sensitive": params["case_sensitive"],
                      "include_own_calls": params["include_own_calls"]},
        "scan_complete": found["scan_complete"], "compactions_seen": found["compactions_seen"],
        "scan_seconds": found["scan_seconds"],
        "chain": {"files": found["files"], "complete": chain["complete"] and found["scan_complete"],
                  "warnings": chain["warnings"], "scan_order": "newest-first"},
    }
    if params["clamped"]:
        out["clamped"] = params["clamped"]
    notes: List[str] = []
    if chain.get("current_missing"):
        notes.append("the RECORDED current transcript is missing or unreadable ("
                     + ", ".join(chain["current_missing"])
                     + "); only the files listed under chain.files were searched, so recent history may be absent")
    elif chain["warnings"]:
        notes.append("the chain is INCOMPLETE (" + "; ".join(chain["warnings"]) + "); older history may be absent")
    if "deadline-prepass" in found["stop_reasons"]:
        notes.append("the time limit was reached during the pre-pass (the line-numbering read of the chain), "
                     "before any record was searched: no records were searched, and total_matches (0) is a "
                     "LOWER BOUND; retry, or narrow the chain")
    if "deadline" in found["stop_reasons"]:
        notes.append("scan stopped at its time limit: total_matches is a LOWER BOUND. The scan runs "
                     "newest-first, so the OLDEST records were left unsearched (see chain.files status "
                     "scan-deadline / not-scanned)")
    if "unreadable" in found["stop_reasons"]:
        notes.append("a chain file became unreadable during the scan and was skipped: total_matches is a LOWER BOUND")
    if notes:
        out["warning"] = " | ".join(notes)
    return out

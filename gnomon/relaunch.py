"""gnomon-relaunch — the relaunch verb, as an EXTERNAL tool.

    gnomon-relaunch <session_dir> [--compact-first] [--dry-run] [--force-launch]
                [--terminal iterm|print] [--wait-idle SECONDS]

Stops ONE harnessed session gracefully and relaunches it through the same
``python -m gnomon.harness <session_dir>`` command, which RESUMES the
transcript from ``launch.json`` ``resume_session_id`` (operator ruling
 resume the transcript, optional compact-first). It is
self-fireable by the agent under rails (autonomous operation is meant to stay enabled).

It is deliberately EXTERNAL (it needs nothing from the running harness beyond
files it already writes), so it works on sessions still running OLD harness code
— the whole point: re-homing live sessions onto new harness code.

RAILS — all checked BEFORE anything is stopped, and re-checked by the detached
worker immediately before the stop. Any failure refuses (exit 2), nothing sent:

* ``harness-pid``    — exactly ONE process whose argv is
  ``<python> -m gnomon.harness <dir>`` with ``realpath(<dir>) ==
  realpath(session_dir)``. Parsed, never pattern-matched: ``harness.self_mcp``,
  trailing args, a sub-dir, a ``bash -c`` wrapper, a relative dir all REJECT.
* ``claude-child``   — exactly ONE ``claude`` process whose PARENT is that pid
  (the stop target — see STOP below).
* ``resume``         — ``launch.json`` has ``resume_session_id`` + ``resume_cwd``;
  otherwise re-running the command would NOT preserve the transcript (D1).
  With ``relaunch_derives_resume_keys`` ON (default OFF) a launch.json
  that has NEITHER key (a session launched fresh) passes when the keys can be
  DERIVED safely (exactly one recent transcript for its session_id; the rule is
  in :mod:`gnomon.resume_keys`), is written atomically with a backup just
  before the stop, and otherwise refuses naming the clause. The same flag makes
  the worker replace a sticky ``resume_prompt`` with a neutral "relaunched at
  <UTC>" line and enables ``--prompt-once TEXT`` (delivered once, then cleared).
* ``turn-boundary``  — the transcript tail's last user/assistant entry is an
  assistant message with ``stop_reason`` end_turn/stop_sequence and no tool_use,
  and the file has been quiet for ``SETTLE_SECONDS``. Source: the statusline
  hook's ``context_state.json`` ``transcript_path`` (else the
  ``.claude/transcript.jsonl`` link) — both written by old and new harnesses.
  Not idle yet -> the detached worker waits up to ``--wait-idle`` seconds (this
  is how a self-fire works: the agent's own tool call is mid-turn).
* ``subagents``      — as far as detectable from Claude Code's own files under
  ``<transcript-stem>/``: a workflow run dir under ``subagents/workflows/``
  with no completion record ``workflows/<run>.json`` and activity within
  ``WORKFLOW_ACTIVE_WINDOW``; or a background ``subagents/agent-*.jsonl``
  whose last entry is not an end_turn and which was written within
  ``AGENT_ACTIVE_WINDOW``. A foreground Agent/Task call shows up as
  turn-boundary=busy. NOT detectable: a subagent that has been silent for
  longer than the windows (reported as stale, not blocking).
* ``operator-line``  — from the Gnomon presence beacon's ``line_state`` (harnesses
  from 2026-09-29 ~16:37 PT on). ``partial`` refuses; ``unknown`` refuses until
  300 s of operator silence (the D44 line-hold rule). OLD harnesses write NO
  beacon: the line is UNDETECTABLE — reported, not blocking, unless
  ``--require-line-empty``.
* ``compact-gate``   — with ``--compact-first`` only: the D42 gate
  (:class:`~gnomon.harness.compact_gate.CompactGate`, current code) is
  evaluated read-only against the session's own log + statusline reading; a
  refusal refuses the relaunch. The tool NEVER types ``/compact`` itself (that
  would bypass the gate): the compact is delivered by the agent's own
  ``request_compact`` (gated in the harness) or by the operator, and the worker
  waits for its ``lifecycle/compact`` ``post`` record before stopping.

STOP (measured from the harness code, 2026-09-29): the harness installs NO
SIGTERM handler (only SIGWINCH, in ``HarnessProxy.run``). SIGTERM to the harness
python is Python's default action — immediate death: no ``finally`` (control
socket dir left in /tmp, ``record_exit_check`` skipped), no ``agent-end``
lifecycle record, the terminal left in raw mode, and claude killed indirectly by
the PTY hangup. There is no external route to type ``/exit`` into an old
harness's PTY without the hub operator token. The clean path is therefore
SIGTERM to the exact claude CHILD pid: claude exits, the proxy's pump sees EOF
and leaves its loop, ``_reap`` fires ``agent-end`` once, and every ``finally``
in ``launcher.main`` runs. Escalation, each on the SAME pid it follows:
claude SIGTERM -> ``CHILD_TERM_TIMEOUT`` -> claude SIGKILL ->
``HARNESS_EXIT_TIMEOUT`` -> harness SIGTERM -> ``HARNESS_TERM_TIMEOUT`` ->
harness SIGKILL. Every signal is preceded by a pid-reuse guard (same pid, same
start time, same argv). Nothing is ever signalled by pattern.

DETACH: the real (non-dry-run) run double-forks + ``setsid`` into a worker
whose output goes to ``<session_dir>/.claude/relaunch/<id>.log``; the caller
returns at once. The worker sleeps ``--grace`` seconds (default 3) so the
caller's tool call can return, then waits for the rails, stops, relaunches via a
terminal ADAPTER (``iterm``: osascript opens a new iTerm tab running
``cd <dir> && exec <python> -m gnomon.harness <dir>`` in a fresh login
shell — the same env essentials as the original tab, which was launched the same
way; ``print``: prints the command for the operator), then polls until a NEW
exact harness pid exists for the dir. Every step is appended to
``<session_dir>/.claude/injection_log.jsonl`` as ``kind: lifecycle, point:
relaunch``. ``--dry-run`` writes NOTHING anywhere.

LAUNCH-ONLY (live-finding-launch-only, 2026-09-30): the natural
operator flow is ``/exit`` then relaunch, and a boot-time relaunch after a power
outage finds every session already gone. When NO harness is running for the
exact session dir, the tool skips the stop phase and goes straight to the
terminal adapter (logged as step ``launch-only``), provided ALL of:

* ``harness-ambiguity`` — no process that LOOKS like a harness for this dir but
  fails the exact match (a ``bash -c`` wrapper, a relative dir, trailing args,
  a sub-dir, ...: any argv naming ``gnomon.harness`` plus the session
  dir's name). Such a process refuses: a launch must never create a duplicate
  session, and a near-miss is exactly the case the exact match cannot see.
* ``live-resume``   — no ``claude`` process anywhere carries ``--resume
  <resume_session_id>`` (an orphaned or hand-started claude on the same
  transcript would also be a duplicate).
* ``resume``        — as above.
* ``last-exit``     — the LAST lifecycle record in ``injection_log.jsonl``
  (ignoring gnomon-relaunch's own ``point: relaunch`` records) is a clean
  ``agent-end`` (``exit_code`` 0), OR that agent-end is provably THIS tool's own
  clean stop (``own_stop_exit``: exit 143, the tool's SIGTERM-to-claude record
  immediately before it, and its ``stop clean`` record for the same harness pid
  and relaunch id after it), OR ``--force-launch`` is given. A session that
  crashed, was killed by anything else, or never wrote an agent-end refuses,
  naming the flag.

The terminal launch is tried ``LAUNCH_ATTEMPTS`` times; if every try fails the log
says the session is DOWN and gives the command that reopens it.

``--compact-first`` refuses in launch-only mode (there is no session to compact).
The detached worker re-evaluates immediately before the launch and aborts if a
harness appeared meanwhile.

Exit: 0 = dispatched (or dry-run: all rails would pass); 2 = refused (or
dry-run: would refuse); 1 = error.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

VERSION = "1.3.0"
EXIT_OK, EXIT_ERR, EXIT_REFUSED = 0, 1, 2

HARNESS_MODULE = "gnomon.harness"
LOG_POINT = "relaunch"

#: Timeouts (seconds) — the documented escalation ladder.
CHILD_TERM_TIMEOUT = 30.0      # claude SIGTERM -> wait for the harness to exit
CHILD_KILL_TIMEOUT = 10.0      # claude SIGKILL -> wait for the harness to exit
HARNESS_TERM_TIMEOUT = 5.0     # harness SIGTERM -> wait
HARNESS_KILL_TIMEOUT = 5.0     # harness SIGKILL -> wait
VERIFY_TIMEOUT = 90.0          # relaunch -> new harness pid must appear
DEFAULT_WAIT_IDLE = 180.0
DEFAULT_GRACE = 3.0
DEFAULT_COMPACT_WAIT = 900.0
SETTLE_SECONDS = 3.0
POLL_INTERVAL = 1.0
LAUNCH_ATTEMPTS = 3            # terminal-adapter launch tries before the session is declared DOWN
LAUNCH_RETRY_WAIT = 5.0        # seconds between tries (3 tries span about 10 s)
SIGTERM_EXIT_CODE = 128 + int(signal.SIGTERM)   # 143: what an agent-end records when this tool's SIGTERM ended claude
WORKFLOW_ACTIVE_WINDOW = 1800.0
AGENT_ACTIVE_WINDOW = 600.0
AGENT_SCAN_WINDOW = 6 * 3600.0
LINE_UNKNOWN_AFTER = 300.0
TAIL_BYTES = 512 * 1024

_PYTHON_RE = re.compile(r"^[Pp]ython(\d+(\.\d+)*)?$")
ITERM_EXE_SUFFIX = "/iTerm.app/Contents/MacOS/iTerm2"


class Refused(Exception):
    pass


# ---------------------------------------------------------------- system seam

@dataclass
class ProcRow:
    pid: int
    ppid: int
    lstart: str
    command: str


class System:
    """Everything that touches the machine. Tests substitute a fake."""

    def ps(self) -> List[ProcRow]:
        out = subprocess.run(["/bin/ps", "-axww", "-o", "pid=,ppid=,lstart=,command="],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True
                             ).stdout.decode("utf-8", "replace")
        rows = []
        for line in out.splitlines():
            parts = line.strip().split(None, 7)
            if len(parts) < 7:
                continue
            try:
                pid, ppid = int(parts[0]), int(parts[1])
            except ValueError:
                continue
            rows.append(ProcRow(pid, ppid, " ".join(parts[2:7]), parts[7] if len(parts) > 7 else ""))
        return rows

    def kill(self, pid: int, sig: int) -> None:
        if not isinstance(pid, int) or pid <= 1:
            raise ValueError(f"refusing to signal pid {pid!r}")
        os.kill(pid, sig)

    def osascript(self, lines: Sequence[str]) -> Tuple[int, str]:
        argv = ["/usr/bin/osascript"]
        for ln in lines:
            argv += ["-e", ln]
        p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        return p.returncode, p.stdout.decode("utf-8", "replace")

    def osascript_focus(self, lines: Sequence[str]) -> Tuple[int, str]:
        """The focus step's own seam (separate from ``osascript`` so a test double can record it apart)."""
        return self.osascript(lines)

    def now(self) -> float:
        return time.time()

    def sleep(self, s: float) -> None:
        time.sleep(s)

    def getpid(self) -> int:
        return os.getpid()


# ---------------------------------------------------------------- exact matching

def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def parse_harness_command(command: str) -> Optional[Tuple[str, str]]:
    """``(python_argv0, session_dir_arg)`` iff ``command`` is EXACTLY
    ``<python> -m gnomon.harness <abs dir>`` — else ``None``.

    The separator must occur exactly once; argv0's basename must be a python;
    the dir must be absolute. Everything after the module is the dir (so a
    trailing extra arg makes the dir not match, rather than being ignored)."""
    sep = " -m " + HARNESS_MODULE + " "
    if command.count(sep) != 1:
        return None
    argv0, rest = command.split(sep, 1)
    if not argv0 or not rest or any(ch.isspace() for ch in argv0):
        # argv0 must be ONE token: '/bin/bash -c <python>' must not pass as a python.
        # (A python path containing whitespace therefore never matches: fail closed.)
        return None
    if not _PYTHON_RE.match(os.path.basename(argv0)):
        return None
    if not os.path.isabs(rest) or rest != rest.strip():
        return None
    return argv0, rest


def find_harness(rows: List[ProcRow], session_dir: str) -> List[ProcRow]:
    target = _real(session_dir)
    hits = []
    for r in rows:
        parsed = parse_harness_command(r.command)
        if parsed and _real(parsed[1]) == target:
            hits.append(r)
    return hits


def near_miss_harness(rows: List[ProcRow], session_dir: str) -> List[ProcRow]:
    """Processes that name ``gnomon.harness`` AND this session dir's name
    but are NOT an exact match. Deliberately over-inclusive (fail closed): in
    launch-only mode any hit refuses, because it may be a live harness for this
    dir that the exact parser cannot see, and a launch would duplicate it."""
    exact = {r.pid for r in find_harness(rows, session_dir)}
    names = {os.path.basename(_real(session_dir).rstrip("/")),
             os.path.basename(os.path.abspath(os.path.expanduser(session_dir)).rstrip("/"))}
    names.discard("")
    hits = []
    for r in rows:
        if r.pid in exact or HARNESS_MODULE not in r.command:
            continue
        if any(n in r.command for n in names):
            hits.append(r)
    return hits


def live_resume_claudes(rows: List[ProcRow], resume_id: str) -> List[ProcRow]:
    """Any ``claude`` process already running ``--resume <resume_id>``."""
    return [r for r in rows if is_claude_command(r.command)
            and f"--resume {resume_id}" in r.command]


def is_claude_command(command: str) -> bool:
    argv = command.split()
    if not argv:
        return False
    if os.path.basename(argv[0]) in ("claude", "claude.exe"):
        return True
    return any(a.endswith("/@anthropic-ai/claude-code/cli.js") for a in argv[:3])


def claude_children(rows: List[ProcRow], harness_pid: int) -> List[ProcRow]:
    return [r for r in rows if r.ppid == harness_pid and is_claude_command(r.command)]


def ancestors(rows: List[ProcRow], pid: int) -> List[int]:
    by = {r.pid: r for r in rows}
    chain, seen = [], set()
    cur = by.get(pid)
    while cur is not None and cur.pid not in seen:
        seen.add(cur.pid)
        chain.append(cur.pid)
        if cur.ppid <= 1:
            break
        cur = by.get(cur.ppid)
    return chain


def same_process(sysm: System, row: ProcRow) -> bool:
    """pid-reuse guard: the pid still carries the same start time and argv."""
    for r in sysm.ps():
        if r.pid == row.pid:
            return r.lstart == row.lstart and r.command == row.command
    return False


def alive(sysm: System, row: ProcRow) -> bool:
    return same_process(sysm, row)


# ---------------------------------------------------------------- session files

def claude_dir(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude")


def log_path(session_dir: str) -> str:
    return os.path.join(claude_dir(session_dir), "injection_log.jsonl")


def read_json(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def read_launch(session_dir: str) -> Optional[Dict[str, Any]]:
    return read_json(os.path.join(claude_dir(session_dir), "launch.json"))


def transcript_path(session_dir: str) -> Optional[str]:
    st = read_json(os.path.join(claude_dir(session_dir), "context_state.json")) or {}
    p = st.get("transcript_path")
    if isinstance(p, str) and os.path.isfile(p):
        return os.path.realpath(p)
    link = os.path.join(claude_dir(session_dir), "transcript.jsonl")
    return os.path.realpath(link) if os.path.isfile(link) else None


def tail_entries(path: str, nbytes: int = TAIL_BYTES) -> List[Dict[str, Any]]:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - nbytes))
            data = fh.read()
    except OSError:
        return []
    lines = data.split(b"\n")
    if size > nbytes:
        lines = lines[1:]  # first line is probably partial
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            obj = json.loads(ln.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _is_end_turn(entry: Dict[str, Any]) -> bool:
    if entry.get("type") != "assistant":
        return False
    msg = entry.get("message") or {}
    if msg.get("stop_reason") not in ("end_turn", "stop_sequence"):
        return False
    content = msg.get("content")
    if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_use"
                                         for c in content):
        return False
    return True


def turn_state(path: Optional[str], now: float) -> Tuple[str, str]:
    """``("idle"|"busy"|"unknown", detail)`` from the transcript tail."""
    if not path:
        return "unknown", "no transcript path (context_state.json / .claude/transcript.jsonl)"
    entries = [e for e in tail_entries(path)
               if e.get("type") in ("user", "assistant") and not e.get("isSidechain")]
    if not entries:
        return "unknown", f"no user/assistant entry in the tail of {path}"
    last = entries[-1]
    try:
        quiet = now - os.path.getmtime(path)
    except OSError:
        quiet = 0.0
    if not _is_end_turn(last):
        msg = last.get("message") or {}
        return "busy", (f"last entry is {last.get('type')} (stop_reason="
                        f"{msg.get('stop_reason')!r}) at {last.get('timestamp')}")
    if quiet < SETTLE_SECONDS:
        return "busy", f"end_turn seen but transcript written {quiet:.1f}s ago (settling)"
    return "idle", f"end_turn at {last.get('timestamp')}; transcript quiet {quiet:.0f}s"


def subagent_state(path: Optional[str], now: float) -> Tuple[str, List[str], List[str]]:
    """``(verdict, inflight, notes)``; verdict ``clear|inflight|unknown``."""
    if not path:
        return "unknown", [], ["no transcript path: subagents undetectable"]
    stem = path[:-6] if path.endswith(".jsonl") else path
    sub = os.path.join(stem, "subagents")
    notes: List[str] = []
    inflight: List[str] = []
    if not os.path.isdir(sub):
        return "clear", [], [f"no subagents dir ({sub})"]
    wf_root = os.path.join(sub, "workflows")
    if os.path.isdir(wf_root):
        for run in sorted(os.listdir(wf_root)):
            rdir = os.path.join(wf_root, run)
            if not os.path.isdir(rdir):
                continue
            if os.path.isfile(os.path.join(stem, "workflows", run + ".json")):
                continue
            newest = _newest_mtime(rdir)
            age = now - newest if newest else float("inf")
            if age <= WORKFLOW_ACTIVE_WINDOW:
                inflight.append(f"workflow {run} (no completion record; active {age:.0f}s ago)")
            else:
                notes.append(f"workflow {run}: no completion record but silent {age:.0f}s — stale, ignored")
    for name in sorted(os.listdir(sub)):
        if not (name.startswith("agent-") and name.endswith(".jsonl")):
            continue
        p = os.path.join(sub, name)
        try:
            age = now - os.path.getmtime(p)
        except OSError:
            continue
        if age > AGENT_SCAN_WINDOW:
            continue
        ents = [e for e in tail_entries(p, 64 * 1024) if e.get("type") in ("user", "assistant")]
        if ents and _is_end_turn(ents[-1]):
            continue
        if age <= AGENT_ACTIVE_WINDOW:
            inflight.append(f"subagent {name[:-6]} (not at end_turn; written {age:.0f}s ago)")
        else:
            notes.append(f"subagent {name[:-6]}: not at end_turn but silent {age:.0f}s — stale, ignored")
    return ("inflight" if inflight else "clear"), inflight, notes


def _newest_mtime(d: str) -> float:
    newest = 0.0
    for root, _dirs, files in os.walk(d):
        for f in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, f)))
            except OSError:
                pass
    return newest


def line_state(session_dir: str, harness_pid: int, now: float) -> Tuple[str, str]:
    """``(empty|partial|unknown|held|undetectable, detail)`` from the presence beacon."""
    try:
        from .harness.operator_activity import beacon_path
        bp = beacon_path(session_dir)
    except Exception as exc:  # noqa: BLE001
        return "undetectable", f"beacon path unavailable: {exc}"
    b = read_json(bp)
    if b is None:
        return "undetectable", ("no presence beacon (harness predates the Gnomon presence beacon, "
                                "or it is not writing one): the operator's unsent line cannot be seen")
    if b.get("pid") != harness_pid:
        return "undetectable", f"beacon names pid {b.get('pid')}, not the harness {harness_pid}"
    st = b.get("line_state")
    last = b.get("last_keystroke_ts")
    silent = now - last if isinstance(last, (int, float)) else float("inf")
    if st == "empty":
        return "empty", "beacon line_state=empty"
    if st == "partial":
        return "partial", "operator has a partially typed line"
    if silent >= LINE_UNKNOWN_AFTER:
        return "empty", f"line_state={st!r} but operator silent {silent:.0f}s (>= {LINE_UNKNOWN_AFTER:.0f}s)"
    return "held", f"line_state={st!r} and operator active {silent:.0f}s ago (< {LINE_UNKNOWN_AFTER:.0f}s)"


def compact_gate_verdict(session_dir: str, directive: str = "",
                         approval_note: str = "") -> Tuple[bool, str]:
    """Evaluate the D42 self-compact gate READ-ONLY (its own logging disabled)."""
    from .harness.compact_gate import CompactGate, load_log_records

    state_path = os.path.join(claude_dir(session_dir), "context_state.json")

    def _stamped():
        st = read_json(state_path)
        if not st:
            return None
        return st.get("used_percentage"), st.get("written_epoch")

    def _pct():
        s = _stamped()
        return s[0] if s else None

    gate = CompactGate(read_context=_pct, read_stamped=_stamped,
                       records=lambda: load_log_records(log_path(session_dir)),
                       record=lambda *a, **k: None)
    v = gate.check(directive_path=directive, ch_approved=bool(approval_note),
                   approval_note=approval_note or None)
    if v.allowed:
        extra = f" (pilot override: {v.overridden})" if v.overridden else ""
        return True, f"gate allows (context {v.used_percentage}%){extra}"
    return False, f"gate REFUSES: {', '.join(v.reasons)} — {' '.join(v.messages)}"


def compact_since(session_dir: str, since: float) -> bool:
    try:
        from .harness.compact_gate import load_log_records
        recs = load_log_records(log_path(session_dir))
    except Exception:  # noqa: BLE001
        return False
    for r in recs:
        if (r.get("kind") == "lifecycle" and r.get("point") == "compact"
                and (r.get("payload") or {}).get("phase") == "post"
                and isinstance(r.get("ts"), (int, float)) and r["ts"] > since):
            return True
    return False


def last_lifecycle(session_dir: str) -> Optional[Dict[str, Any]]:
    """The LAST ``kind: lifecycle`` record in the injection log, skipping
    gnomon-relaunch's own ``point: relaunch`` records (a refused attempt appends one,
    and must not mask the agent-end before it). ``None`` if there is none."""
    last = None
    try:
        with open(log_path(session_dir), "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if (isinstance(rec, dict) and rec.get("kind") == "lifecycle"
                        and rec.get("point") != LOG_POINT):
                    last = rec
    except OSError:
        return None
    return last


def clean_exit(rec: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    """``(clean, detail)``: clean iff an ``agent-end`` with ``exit_code`` 0."""
    if rec is None:
        return False, "no lifecycle record in the injection log"
    point = rec.get("point")
    payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
    when = rec.get("ts_iso") or rec.get("ts")
    if point != "agent-end":
        extra = f" phase={payload.get('phase')}" if payload.get("phase") else ""
        return False, f"last lifecycle record is {point!r}{extra} at {when}, not an agent-end"
    code = payload.get("exit_code")
    if code != 0:
        return False, f"last lifecycle record is agent-end exit_code={code!r} at {when} (not clean)"
    return True, f"clean agent-end (exit_code 0, reason={payload.get('reason')!r}) at {when}"


def _int_not_bool(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_tool_record(r: Any) -> bool:
    return (isinstance(r, dict) and r.get("kind") == "lifecycle" and r.get("point") == LOG_POINT
            and r.get("source") == "gnomon-relaunch")


def own_stop_exit(session_dir: str) -> Tuple[bool, str]:
    """``(True, why)`` only when the LAST lifecycle record (gnomon-relaunch's own skipped,
    as in ``last_lifecycle``) is an ``agent-end`` this tool's OWN clean stop caused.
    Every one of these must hold, from the log alone, else ``(False, reason)``:

    * ``exit_code`` is ``SIGTERM_EXIT_CODE`` (143), a real int;
    * the record immediately before it is this tool's ``signal`` / ``sent`` /
      ``target=claude`` / ``signal=SIGTERM`` record, with a ``relaunch_id``;
    * after it, with only this tool's records of the SAME relaunch id between, comes
      this tool's ``stop`` / ``clean`` / ``via=claude SIGTERM`` record whose
      ``harness_pid`` equals the agent-end record's ``pid`` (the harness that wrote it).
    Anything missing, out of order, or from another relaunch id refuses."""
    recs: List[Any] = []
    last_ok, ends_nl = True, True
    try:
        with open(log_path(session_dir), "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                ends_nl = ln.endswith("\n")
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    recs.append(json.loads(ln))
                    last_ok = True
                except ValueError:
                    last_ok = False
    except OSError:
        return False, "injection log unreadable"
    if not last_ok or not ends_nl:
        # A torn or unparseable FINAL line may hide a newer agent-end (a crash); an earlier chain must not vouch for it.
        return False, "the last line of the injection log cannot be parsed or is cut off"
    idx = None
    for i, r in enumerate(recs):
        if isinstance(r, dict) and r.get("kind") == "lifecycle" and r.get("point") != LOG_POINT:
            idx = i
    if idx is None:
        return False, "no lifecycle record"
    ae = recs[idx]
    payload = ae.get("payload") if isinstance(ae.get("payload"), dict) else {}
    if ae.get("point") != "agent-end":
        return False, "last lifecycle record is not an agent-end"
    code = payload.get("exit_code")
    if not _int_not_bool(code) or code != SIGTERM_EXIT_CODE:
        return False, f"agent-end exit_code={code!r} is not the SIGTERM code {SIGTERM_EXIT_CODE}"
    hpid = ae.get("pid")
    if not _int_not_bool(hpid):
        return False, "agent-end record carries no integer pid"
    prev = recs[idx - 1] if idx >= 1 else None
    if not (_is_tool_record(prev) and prev.get("step") == "signal" and prev.get("decision") == "sent"
            and prev.get("target") == "claude" and prev.get("signal") == "SIGTERM"
            and isinstance(prev.get("relaunch_id"), str) and prev.get("relaunch_id")):
        return False, "the record before the agent-end is not this tool's SIGTERM-to-claude record"
    rid = prev["relaunch_id"]
    for r in recs[idx + 1:]:
        if not (_is_tool_record(r) and r.get("relaunch_id") == rid):
            return False, "no stop record of the same relaunch id follows the agent-end"
        if r.get("step") == "stop":
            if (r.get("decision") == "clean" and r.get("via") == "claude SIGTERM"
                    and _int_not_bool(r.get("harness_pid")) and r.get("harness_pid") == hpid):
                return True, (f"agent-end exit_code={code} at {ae.get('ts_iso') or ae.get('ts')} is this tool's own "
                              f"clean stop (relaunch {rid}, harness pid {hpid}, claude SIGTERM)")
            return False, "the stop record does not match the agent-end (decision, via or harness pid)"
    return False, "no stop record follows the agent-end"


# ---------------------------------------------------------------- logging

class Logger:
    """Appends ``lifecycle/relaunch`` records. Disabled (no-op) in dry-run."""

    def __init__(self, session_dir: str, instance_id: str, relaunch_id: str,
                 enabled: bool, sysm: System, echo: Callable[[str], None] = print) -> None:
        self.session_dir, self.instance_id, self.relaunch_id = session_dir, instance_id, relaunch_id
        self.enabled, self.sysm, self.echo = enabled, sysm, echo

    def __call__(self, step: str, decision: str = "info", **fields: Any) -> None:
        ts = self.sysm.now()
        self.echo(f"gnomon-relaunch [{step}] {decision}: " +
                  "; ".join(f"{k}={v}" for k, v in fields.items() if v is not None))
        if not self.enabled:
            return
        rec = {"ts": ts, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
               "pid": self.sysm.getpid(), "kind": "lifecycle", "instance_id": self.instance_id,
               "point": LOG_POINT, "source": "gnomon-relaunch", "relaunch_id": self.relaunch_id,
               "step": step, "decision": decision, **fields}
        line = (json.dumps(rec, default=str) + "\n").encode("utf-8")
        try:
            fd = os.open(log_path(self.session_dir), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                os.write(fd, line)  # one write: O_APPEND keeps it whole beside the harness's own
            finally:
                os.close(fd)
        except OSError as exc:
            self.echo(f"gnomon-relaunch: WARNING: could not append to the injection log: {exc}")


# ---------------------------------------------------------------- terminal adapters

def framework_python(argv0: str) -> str:
    """The harness shows the macOS framework stub ``.../Resources/Python.app/
    Contents/MacOS/Python``; relaunch through ``.../Versions/X/bin/python3``
    (what the launcher ran) when it exists, else argv0 verbatim."""
    marker = "/Resources/Python.app/Contents/MacOS/Python"
    if argv0.endswith(marker):
        cand = os.path.join(argv0[: -len(marker)], "bin", "python3")
        if os.path.exists(cand):
            return cand
    return argv0


def relaunch_shell_command(python: str, session_dir_arg: str) -> str:
    """Byte-for-byte the launcher's shape (launch.py ``_build_inner_command``)."""
    argv = [python, "-m", HARNESS_MODULE, session_dir_arg]
    return f"cd {shlex.quote(session_dir_arg)} && exec " + " ".join(shlex.quote(a) for a in argv)


def _as_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class TerminalAdapter:
    name = "abstract"

    def available(self, rows: List[ProcRow]) -> Tuple[bool, str]:
        return True, ""

    def launch(self, sysm: System, command: str, echo: Callable[[str], None]) -> Tuple[bool, str]:
        raise NotImplementedError

    def activate(self, sysm: System) -> Tuple[bool, str]:
        """Bring the terminal to the front after a VERIFIED launch. Cosmetic: default is nothing to do."""
        return True, "no focus step for this adapter"


class PrintAdapter(TerminalAdapter):
    name = "print"

    def launch(self, sysm, command, echo):
        echo("gnomon-relaunch: run this in a terminal to relaunch:\n  " + command)
        return True, "printed for the operator (not launched)"


class ItermAdapter(TerminalAdapter):
    name = "iterm"

    def available(self, rows):
        if any(r.command.split(" ")[0].endswith(ITERM_EXE_SUFFIX) for r in rows):
            return True, "iTerm2 is running"
        return False, "iTerm2 is not running"

    @staticmethod
    def script(command: str) -> List[str]:
        """Pick ONE target window explicitly, then address only that variable.

        The old script said ``tell current window`` twice and failed with -1728
        ("Can't get current window") at an instant when iTerm had windows but
        none current (2026-10-14, an earlier relaunch). Order of choice, no focus
        change (``activate`` is NOT in this script: since 1.3.0 it is a separate step, ``activate_script``, run only after the launch is verified): the current
        window if iTerm has one, else the first window, else (windows exist but
        neither answers) the operator's own form: ``select`` the first window so it
        becomes current, still without ``activate``. With no windows at all, a
        window is created and its session is written to. After the choice the
        script never names "current window" again."""
        body = _as_escape(command)
        return [
            'tell application "iTerm2"',
            "if (count of windows) = 0 then",
            "set targetWindow to (create window with default profile)",
            "else",
            "set targetWindow to missing value",
            "try",
            "set targetWindow to current window",
            "end try",
            "if targetWindow is missing value then",
            "try",
            "set targetWindow to first window",
            "set probeId to id of targetWindow",
            "on error",
            "set targetWindow to missing value",
            "end try",
            "end if",
            "if targetWindow is missing value then",
            "tell first window to select",
            "set targetWindow to current window",
            "end if",
            "tell targetWindow to create tab with default profile",
            "end if",
            f'tell current session of targetWindow to write text "{body}"',
            "end tell",
        ]

    def launch(self, sysm, command, echo):
        rc, out = sysm.osascript(self.script(command))
        return rc == 0, (f"osascript rc={rc}" + (f": {out.strip()}" if out.strip() else ""))

    @staticmethod
    def activate_script() -> List[str]:
        """The focus step: a script of its own, run only after the launch is verified (version 1.3.0)."""
        return ['tell application "iTerm2" to activate']

    def activate(self, sysm):
        rc, out = sysm.osascript_focus(self.activate_script())
        return rc == 0, (f"osascript rc={rc}" + (f": {out.strip()}" if out.strip() else ""))


ADAPTERS: Dict[str, Callable[[], TerminalAdapter]] = {"iterm": ItermAdapter, "print": PrintAdapter}


def register_adapter(name: str, factory: Callable[[], TerminalAdapter]) -> None:
    """Pluggable: a new terminal is one adapter class (terminal-agnostic)."""
    ADAPTERS[name] = factory


def choose_adapter(name: Optional[str], rows: List[ProcRow]) -> TerminalAdapter:
    if name:
        if name not in ADAPTERS:
            raise Refused(f"unknown terminal adapter {name!r}; known: {sorted(ADAPTERS)}")
        return ADAPTERS[name]()
    it = ItermAdapter()
    return it if it.available(rows)[0] else PrintAdapter()


# ---------------------------------------------------------------- rails

@dataclass
class Rail:
    name: str
    status: str   # PASS | FAIL | WAIT | UNDETECTABLE | SKIP
    detail: str


@dataclass
class Plan:
    session_dir: str
    instance_id: str = ""
    harness: Optional[ProcRow] = None
    claude: Optional[ProcRow] = None
    python: str = ""
    session_dir_arg: str = ""
    command: str = ""
    adapter: Optional[TerminalAdapter] = None
    self_fire: bool = False
    mode: str = "relaunch"          # relaunch (stop, then launch) | launch-only
    last_exit: str = ""
    rails: List[Rail] = field(default_factory=list)

    @property
    def refused(self) -> List[Rail]:
        return [r for r in self.rails if r.status == "FAIL"]

    @property
    def waiting(self) -> List[Rail]:
        return [r for r in self.rails if r.status == "WAIT"]


@dataclass
class Options:
    compact_first: bool = False
    directive: str = ""
    approval_note: str = ""
    require_line_empty: bool = False
    terminal: Optional[str] = None
    wait_idle: float = DEFAULT_WAIT_IDLE
    force_launch: bool = False
    #: X13 S2 (workspace_scope_core): when True a ``relaunch-bundle`` rail is added and the worker copies the
    #: bundle forward. Resolved ONCE by main() from the flag; default False = byte-identical to the pre-S2 tool.
    scope_core: bool = False
    #: relaunch_derives_resume_keys: resolved ONCE by main(). True = derive missing resume keys (rule in
    #: :mod:`gnomon.resume_keys`), write the neutral resume_prompt, honour ``prompt_once``. False = the previous
    #: tool exactly (the resume rail refuses a launch.json without the keys).
    derive_keys: bool = False
    #: A deliberate ONE-SHOT resume prompt (``--prompt-once``): written to launch.json by the worker, delivered by the
    #: next harness start, then cleared by the harness. Only with ``derive_keys``.
    prompt_once: str = ""


def evaluate(sysm: System, session_dir: str, opts: Options, *, idle_can_wait: bool = True,
             compact_pending_ok: bool = True) -> Plan:
    """Every rail, read-only. Never signals, never writes.

    X13 S2: with ``opts.scope_core`` the ``relaunch-bundle`` rail is appended (I6: the relaunch reads ONLY the session's
    relaunch bundle and the live machine.json). With it False this is exactly the pre-S2 evaluation."""
    plan = _evaluate_core(sysm, session_dir, opts, idle_can_wait=idle_can_wait, compact_pending_ok=compact_pending_ok)
    if opts.scope_core and os.path.isabs(session_dir) and os.path.isdir(claude_dir(session_dir)):
        from .workspace import bundle as _bundle

        ok, detail = _bundle.check_ready(session_dir)
        plan.rails.append(Rail("relaunch-bundle", "PASS" if ok else "FAIL", detail))
    return plan


def _prompt_note(opts: "Options", rp: str) -> str:
    """The resume rail's note about launch.json's stored ``resume_prompt``."""
    if opts.derive_keys:
        return (f"; NOTE: launch.json resume_prompt {rp[:60]!r}... will be REPLACED by a neutral 'relaunched at <UTC>' "
                "line (relaunch_derives_resume_keys)")
    return f"; NOTE: launch.json resume_prompt will be re-delivered: {rp[:90]!r}..."


def _derived_id(session_dir: str, cfg: Dict[str, Any]) -> str:
    from .resume_keys import derive_resume_keys

    d = derive_resume_keys(session_dir, cfg)
    return d.rid if d.ok else ""


def _resume_rail(session_dir: str, cfg: Dict[str, Any], opts: "Options", what: str) -> Tuple[str, str]:
    """``(status, detail)`` of the ``resume`` rail. Read-only. ``status == ""`` means 'keys present and usable: the caller
    builds the PASS detail' (``detail`` then holds an optional note to append).

    Flag OFF (``opts.derive_keys`` False): exactly the previous rail. Flag ON:
    * BOTH keys absent -> the derivation rule is evaluated READ-ONLY: PASS 'will derive' or FAIL with the refusal;
    * ONE key present -> FAIL (a half pair is never completed);
    * BOTH present -> the SAME transcript definition the harness uses at spawn (:func:`resume_keys.inspect_transcript`)
      and a stat-able resume_cwd / session dir are required HERE, before anything is stopped, so an unusable transcript
      never costs a stopped session."""
    rid, rcwd = cfg.get("resume_session_id"), cfg.get("resume_cwd")
    if opts.derive_keys and not rid and not rcwd:
        from .agent_def import SameDirUnknown, dir_identity
        from .resume_keys import derive_resume_keys

        d = derive_resume_keys(session_dir, cfg)
        if d.ok:
            try:
                dir_identity(session_dir)
            except SameDirUnknown as exc:
                return "FAIL", f"the session dir cannot be identified by stat, so refusing rather than guess: {exc}"
            return "PASS", (f"launch.json has no resume keys (a fresh launch): they WILL be derived at launch and "
                            f"written atomically with a backup: {d.summary()} [{'; '.join(d.notes)}]")
        return "FAIL", f"launch.json lacks resume keys and they cannot be derived safely: {d.refusal}"
    if opts.derive_keys and bool(rid) != bool(rcwd):
        return "FAIL", ("launch.json has only one of resume_session_id/resume_cwd: a half-set pair is never completed "
                        "automatically. Operator: remove the stray key (or restore both from launch.json.bak-*), or "
                        "launch fresh.")
    if not rid or not rcwd:
        return "FAIL", (f"launch.json lacks resume_session_id/resume_cwd: {what} would "
                        "NOT resume the transcript")
    if opts.derive_keys:
        from .resume_keys import present_keys_check

        ok, note = present_keys_check(session_dir, str(rid), str(rcwd))
        if not ok:
            return "FAIL", note
        return "", note
    return "", ""


def _evaluate_core(sysm: System, session_dir: str, opts: Options, *, idle_can_wait: bool = True,
                   compact_pending_ok: bool = True) -> Plan:
    plan = Plan(session_dir=session_dir)
    add = lambda n, s, d: plan.rails.append(Rail(n, s, d))  # noqa: E731
    now = sysm.now()
    if not os.path.isabs(session_dir):
        add("session-dir", "FAIL", "session_dir must be an absolute path")
        return plan
    if not os.path.isdir(claude_dir(session_dir)):
        add("session-dir", "FAIL", f"{claude_dir(session_dir)} does not exist")
        return plan
    rows = sysm.ps()

    hits = find_harness(rows, session_dir)
    if not hits:
        return _evaluate_launch_only(sysm, session_dir, opts, rows, plan)
    if len(hits) != 1:
        add("harness-pid", "FAIL",
            f"{len(hits)} harness processes match (pids {[h.pid for h in hits]}): ambiguous")
        return plan
    h = plan.harness = hits[0]
    argv0, dir_arg = parse_harness_command(h.command)  # type: ignore[misc]
    plan.session_dir_arg = dir_arg
    plan.python = framework_python(argv0)
    plan.command = relaunch_shell_command(plan.python, dir_arg)
    add("harness-pid", "PASS", f"pid {h.pid} (ppid {h.ppid}, started {h.lstart}): {h.command}")
    plan.self_fire = h.pid in ancestors(rows, sysm.getpid())

    kids = claude_children(rows, h.pid)
    if len(kids) != 1:
        add("claude-child", "FAIL", f"expected exactly 1 claude child of pid {h.pid}, found {len(kids)}")
        return plan
    plan.claude = kids[0]
    add("claude-child", "PASS", f"pid {kids[0].pid} (the graceful-stop target)")

    cfg = read_launch(session_dir) or {}
    plan.instance_id = str(cfg.get("instance_id") or os.path.basename(session_dir.rstrip("/")))
    rid, rcwd = cfg.get("resume_session_id"), cfg.get("resume_cwd")
    rstatus, rdetail = _resume_rail(session_dir, cfg, opts, "a relaunch")
    if rstatus:
        add("resume", rstatus, rdetail)
    else:
        detail = f"resume_session_id={rid}" + (f"; {rdetail}" if rdetail else "")
        if f"--resume {rid}" not in kids[0].command:
            detail += " (WARNING: the running claude was not started with this --resume id)"
        rp = cfg.get("resume_prompt")
        if rp:
            detail += _prompt_note(opts, rp)
        add("resume", "PASS", detail)

    tp = transcript_path(session_dir)
    ts, tdetail = turn_state(tp, now)
    if opts.compact_first and compact_pending_ok:
        add("turn-boundary", "WAIT", f"{ts}: {tdetail} — evaluated again after the compact lands")
    elif ts == "idle":
        add("turn-boundary", "PASS", tdetail)
    elif idle_can_wait and opts.wait_idle > 0:
        add("turn-boundary", "WAIT", f"{ts}: {tdetail} — the worker waits up to {opts.wait_idle:.0f}s")
    else:
        add("turn-boundary", "FAIL", f"{ts}: {tdetail}")

    sv, inflight, notes = subagent_state(tp, now)
    note = ("; " + "; ".join(notes)) if notes else ""
    if sv == "inflight":
        add("subagents", "FAIL", "in flight: " + "; ".join(inflight) + note)
    elif sv == "unknown":
        add("subagents", "UNDETECTABLE", "; ".join(notes))
    else:
        add("subagents", "PASS", "none in flight (workflow runs + background agent transcripts)" + note)

    ls, ldetail = line_state(session_dir, h.pid, now)
    if ls == "empty":
        add("operator-line", "PASS", ldetail)
    elif ls == "undetectable":
        add("operator-line", "FAIL" if opts.require_line_empty else "UNDETECTABLE", ldetail)
    elif ls == "held" and idle_can_wait and opts.wait_idle > 0:
        add("operator-line", "WAIT", ldetail)
    else:
        add("operator-line", "FAIL", ldetail)

    if opts.compact_first:
        ok, gdetail = compact_gate_verdict(session_dir, opts.directive, opts.approval_note)
        add("compact-gate", "PASS" if ok else "FAIL",
            gdetail + (" — the worker waits for the compact (request_compact or the operator's "
                       "/compact); gnomon-relaunch never types it" if ok else ""))
    else:
        add("compact-gate", "SKIP", "no --compact-first")

    try:
        plan.adapter = choose_adapter(opts.terminal, rows)
        ok, adetail = plan.adapter.available(rows)
        add("terminal", "PASS" if ok else "FAIL", f"{plan.adapter.name}: {adetail or 'ok'}")
    except Refused as exc:
        add("terminal", "FAIL", str(exc))
    return plan


def _evaluate_launch_only(sysm: System, session_dir: str, opts: Options, rows: List[ProcRow],
                          plan: Plan) -> Plan:
    """No exact harness is running: evaluate the LAUNCH-ONLY rails (read-only)."""
    add = lambda n, s, d: plan.rails.append(Rail(n, s, d))  # noqa: E731
    plan.mode = "launch-only"
    near = near_miss_harness(rows, session_dir)
    if near:
        add("harness-pid", "FAIL", "no EXACT harness for this session dir, but a near-miss exists "
            "(see harness-ambiguity): refusing so a launch cannot duplicate a live session")
        add("harness-ambiguity", "FAIL", "; ".join(f"pid {r.pid}: {r.command}" for r in near))
        return plan
    add("harness-pid", "PASS", "no harness running for this exact session dir: LAUNCH-ONLY "
        "(no stop phase)")
    add("harness-ambiguity", "PASS", "no near-miss harness process for this dir")
    cfg = read_launch(session_dir) or {}
    plan.instance_id = str(cfg.get("instance_id") or os.path.basename(session_dir.rstrip("/")))
    plan.session_dir_arg = session_dir
    plan.python = framework_python(sys.executable)
    plan.command = relaunch_shell_command(plan.python, session_dir)
    rid, rcwd = cfg.get("resume_session_id"), cfg.get("resume_cwd")
    rstatus, rdetail = _resume_rail(session_dir, cfg, opts, "a launch")
    if rstatus:
        add("resume", rstatus, rdetail)
        if rstatus == "PASS":     # flag ON, keys to be derived: the same duplicate-session guard on the derived id
            did = _derived_id(session_dir, cfg)
            live = live_resume_claudes(rows, did) if did else []
            add("live-resume", "FAIL" if live else "PASS",
                ("a claude is already running this transcript: " +
                 "; ".join(f"pid {r.pid} (ppid {r.ppid})" for r in live)) if live else
                f"no claude process carries --resume {did}")
    else:
        detail = f"resume_session_id={rid}" + (f"; {rdetail}" if rdetail else "")
        rp = cfg.get("resume_prompt")
        if rp:
            detail += _prompt_note(opts, rp)
        add("resume", "PASS", detail)
        live = live_resume_claudes(rows, str(rid))
        add("live-resume", "FAIL" if live else "PASS",
            ("a claude is already running this transcript: " +
             "; ".join(f"pid {r.pid} (ppid {r.ppid})" for r in live)) if live else
            f"no claude process carries --resume {rid}")
    ok, edetail = clean_exit(last_lifecycle(session_dir))
    plan.last_exit = edetail
    own, owhy = (False, "") if ok else own_stop_exit(session_dir)
    if ok:
        add("last-exit", "PASS", edetail)
    elif own:
        plan.last_exit = f"{edetail}; {owhy}"
        add("last-exit", "PASS", f"not a clean exit code, but {owhy}; no --force-launch needed")
    elif opts.force_launch:
        add("last-exit", "PASS", f"--force-launch given; overriding: {edetail}")
    else:
        add("last-exit", "FAIL", f"{edetail}; pass --force-launch to launch anyway")
    for name in ("turn-boundary", "subagents", "operator-line"):
        add(name, "SKIP", "no running session (launch-only)")
    if opts.compact_first:
        add("compact-gate", "FAIL", "--compact-first needs a running session; none is running "
            "(launch-only)")
    else:
        add("compact-gate", "SKIP", "no --compact-first")
    try:
        plan.adapter = choose_adapter(opts.terminal, rows)
        ok, adetail = plan.adapter.available(rows)
        add("terminal", "PASS" if ok else "FAIL", f"{plan.adapter.name}: {adetail or 'ok'}")
    except Refused as exc:
        add("terminal", "FAIL", str(exc))
    return plan


def render_plan(plan: Plan, opts: Options) -> str:
    out = [f"gnomon-relaunch {VERSION}: {plan.session_dir}"]
    for r in plan.rails:
        out.append(f"  RAIL {r.name:<17} {r.status:<12} {r.detail}")
    if plan.harness:
        out.append(f"  self-fire (caller inside the target's tree): {'yes' if plan.self_fire else 'no'}")
    if plan.mode == "launch-only":
        out.append("  mode: LAUNCH-ONLY (no harness running: no stop phase; straight to the "
                   f"{plan.adapter.name if plan.adapter else '?'} terminal adapter)")
        if plan.command:
            out.append(f"  launch command: {plan.command}")
    elif plan.command:
        out.append(f"  stop: SIGTERM claude pid {plan.claude.pid if plan.claude else '?'} -> "
                   f"{CHILD_TERM_TIMEOUT:.0f}s -> SIGKILL same pid -> {CHILD_KILL_TIMEOUT:.0f}s -> "
                   f"SIGTERM harness pid {plan.harness.pid if plan.harness else '?'} -> "
                   f"{HARNESS_TERM_TIMEOUT:.0f}s -> SIGKILL same pid")
        out.append(f"  relaunch command: {plan.command}")
    verdict = ("WOULD REFUSE: " + ", ".join(r.name for r in plan.refused)) if plan.refused else (
        "WOULD PROCEED" + (f" after waiting on: {', '.join(r.name for r in plan.waiting)}"
                           if plan.waiting else ""))
    if opts.derive_keys:         # flag ON only: flag OFF prints exactly what it always printed
        out.extend(_flag_on_notes(plan.session_dir))
    out.append(f"  verdict: {verdict}")
    return "\n".join(out)


def _flag_on_notes(session_dir: str) -> List[str]:
    """Dry-run / plan lines (flag ON): an armed one-shot (length and age, never the text) and a leftover journal of a
    worker that never settled. Read-only; never raises."""
    out: List[str] = []
    try:
        from . import resume_keys as rk

        cfg = read_launch(session_dir) or {}
        line = rk.describe_once(cfg, time.time())
        if line:
            out.append("  ONE-SHOT " + line)
        j = rk.journal_read(session_dir)
        if j is not None:
            out.append("  JOURNAL  " + rk.describe_journal(j, session_dir)
                       + ". NOTE: a worker that died AFTER launching leaves the same journal, so the launch may already have happened: "
                       "check `ps` for a harness for this session that started after the time above before relaunching "
                       "(docs/BREAK-GLASS-relaunch.md, RECOVERY)")
    except Exception as exc:  # noqa: BLE001
        out.append(f"  (could not read the one-shot / journal state: {type(exc).__name__}: {exc})")
    return out


# ---------------------------------------------------------------- rollback: strip the keys a flag-ON relaunch derived

#: every launch.json key a flag-ON relaunch can write (resume_keys._updates_for): put back to the pre-derivation backup, or removed
DERIVED_KEYS = ("resume_session_id", "resume_cwd", "resume_keys_derived", "resume_prompt",
                "resume_prompt_once", "resume_prompt_once_meta", "resume_prompt_once_log")


def _summary(v: Any) -> str:
    if v is None:
        return "(absent)"
    if isinstance(v, str):
        return f"text, {len(v)} chars"
    return type(v).__name__


def strip_derived_keys(sysm: System, session_dir: str, dry_run: bool) -> int:
    """``gnomon-relaunch <session_dir> --strip-derived-keys [--dry-run]``: undo what flag-ON relaunches derived BEFORE the session is started on code
    that does not know the flag. The pre-derivation state is the newest ``.claude/launch.json.bak-<UTC>`` (a backup the relaunch wrote) that
    carries no ``resume_keys_derived`` marker; each key in :data:`DERIVED_KEYS` goes back to its value there, or is removed if the backup
    lacks it (key-level, under the launch.json lock, other keys untouched; a ``flags`` entry is NOT touched: remove it by hand). Refuses on a
    running session, on a launch.json that carries no marker, and when no backup predates the derivation. Returns an exit code."""
    import fcntl

    cdir = claude_dir(session_dir)
    if not os.path.isdir(cdir):
        print(f"gnomon-relaunch: REFUSED: {cdir} does not exist")
        return EXIT_REFUSED
    # The per-session worker lock, taken NON-BLOCKING exactly as run_worker takes it: a worker that holds it is mid-relaunch (a
    # verify-wait worker keeps its keys on purpose for a harness that is still starting), and stripping under it would pull them out.
    lock_path = os.path.join(cdir, WORKER_LOCK_NAME)
    try:
        fd = os.open(lock_path, (os.O_RDWR if dry_run else os.O_CREAT | os.O_RDWR), 0o600)
    except FileNotFoundError:
        fd = -1                                  # dry run and no lock file yet: no worker has ever held it, and a dry run writes nothing
    except OSError as exc:
        print(f"gnomon-relaunch: REFUSED: cannot open the per-session worker lock {lock_path} ({exc}); nothing changed")
        return EXIT_REFUSED
    try:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                try:
                    holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown"
                except OSError:
                    holder = "unknown"
                print(f"gnomon-relaunch: REFUSED: another gnomon-relaunch worker for this session is running (pid {holder}, lock {lock_path}); "
                      "wait for it to finish (or stop it by that pid) and try again. Nothing changed")
                return EXIT_REFUSED
        from . import resume_keys as rk

        j = rk.journal_read(session_dir)
        if j is not None:
            print("gnomon-relaunch: REFUSED: " + rk.describe_journal(j, session_dir) + ". Resolve it first (a normal relaunch rolls it back; "
                  "docs/BREAK-GLASS-relaunch.md, RECOVERY, says how to tell whether the launch happened). Nothing changed")
            return EXIT_REFUSED
        return _strip_derived_keys_locked(sysm, session_dir, cdir, dry_run)
    finally:
        if fd >= 0:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


#: the backup names the writer produces: ``launch.json.bak-`` + ``%Y%m%dT%H%M%S`` + 3 ms digits + ``Z`` (agent_def._utc_stamp). A hand-made
#: ``launch.json.bak-pre-x`` or ``launch.json.bak-<date>-<item>`` is NOT one of them and is never a restore source.
def _relaunch_backup_re():
    from .agent_def import LAUNCH_JSON_BACKUP_PREFIX

    return re.compile("^" + re.escape(LAUNCH_JSON_BACKUP_PREFIX) + r"\d{8}T\d{9}Z$")


def _strip_derived_keys_locked(sysm: System, session_dir: str, cdir: str, dry_run: bool) -> int:
    cfg = read_launch(session_dir)
    if cfg is None:
        print(f"gnomon-relaunch: REFUSED: {os.path.join(cdir, 'launch.json')} is missing or unreadable; nothing changed")
        return EXIT_REFUSED
    if "resume_keys_derived" not in cfg:
        print("gnomon-relaunch: nothing to strip: launch.json carries no resume_keys_derived marker (only keys a flag-ON relaunch derived "
              "are stripped); nothing changed")
        return EXIT_OK
    hits = find_harness(sysm.ps(), session_dir)
    if hits:
        print(f"gnomon-relaunch: REFUSED: a harness is running for this session (pid {[h.pid for h in hits]}); stop it first. "
              "Stripping the keys under a running session helps nothing: they are read when a harness STARTS. Nothing changed")
        return EXIT_REFUSED
    pat = _relaunch_backup_re()
    backups = sorted((os.path.join(cdir, n) for n in os.listdir(cdir) if pat.match(n)), reverse=True)      # UTC stamps sort by time
    pick: Optional[str] = None
    for b in backups:
        old = read_json(b)
        if isinstance(old, dict) and "resume_keys_derived" not in old:
            pick = b
            break
    if pick is None:
        print(f"gnomon-relaunch: REFUSED: none of the {len(backups)} kept backup(s) in {cdir} predates the derivation "
              "(each carries resume_keys_derived, or none exists): nothing to restore from. Edit launch.json by hand: "
              "remove resume_session_id, resume_cwd, resume_keys_derived, resume_prompt_once* and set resume_prompt back. Nothing changed")
        return EXIT_REFUSED
    old = read_json(pick) or {}
    print(f"gnomon-relaunch: strip derived keys in {os.path.join(cdir, 'launch.json')}")
    print(f"  restoring from backup {os.path.basename(pick)} (the newest that predates the derivation)")
    for k in DERIVED_KEYS:
        if k in cfg or k in old:
            print(f"    {k:26s} now {_summary(cfg.get(k)):18s} -> {_summary(old.get(k)) if k in old else 'REMOVED'}")
    print("  the session's `flags` entry, if any, is NOT touched (remove it by hand to turn the flag off for this session)")
    if dry_run:
        print("gnomon-relaunch: --dry-run: nothing changed")
        return EXIT_OK
    from . import resume_keys as rk

    ok, detail = rk.rollback_launch_updates(session_dir, pick, list(DERIVED_KEYS))
    print("gnomon-relaunch: " + ("OK: " if ok else "FAILED: ") + detail)
    return EXIT_OK if ok else EXIT_ERR


# ---------------------------------------------------------------- stop + relaunch

def _wait_gone(sysm: System, row: ProcRow, timeout: float) -> bool:
    deadline = sysm.now() + timeout
    while True:
        if not alive(sysm, row):
            return True
        if sysm.now() >= deadline:
            return False
        sysm.sleep(min(POLL_INTERVAL, max(0.05, deadline - sysm.now())))


def _signal(sysm: System, row: ProcRow, sig: int, log: Logger, who: str) -> bool:
    if not same_process(sysm, row):
        log("signal", "skipped", target=who, pid=row.pid, reason="exited or pid reused")
        return False
    try:
        sysm.kill(row.pid, sig)
        log("signal", "sent", target=who, pid=row.pid, signal=signal.Signals(sig).name)
        return True
    except ProcessLookupError:
        log("signal", "skipped", target=who, pid=row.pid, reason="already gone")
        return False


def graceful_stop(sysm: System, plan: Plan, log: Logger) -> bool:
    h, c = plan.harness, plan.claude
    assert h is not None and c is not None
    _signal(sysm, c, signal.SIGTERM, log, "claude")
    if _wait_gone(sysm, h, CHILD_TERM_TIMEOUT):
        log("stop", "clean", via="claude SIGTERM", harness_pid=h.pid)
        return True
    if alive(sysm, c):
        log("stop", "escalate", reason=f"claude alive after {CHILD_TERM_TIMEOUT:.0f}s")
        _signal(sysm, c, signal.SIGKILL, log, "claude")
    if _wait_gone(sysm, h, CHILD_KILL_TIMEOUT):
        log("stop", "ok", via="claude SIGKILL", harness_pid=h.pid)
        return True
    log("stop", "escalate", reason=f"harness alive {CHILD_KILL_TIMEOUT:.0f}s after claude stopped")
    _signal(sysm, h, signal.SIGTERM, log, "harness")
    if _wait_gone(sysm, h, HARNESS_TERM_TIMEOUT):
        log("stop", "ok", via="harness SIGTERM", harness_pid=h.pid)
        return True
    _signal(sysm, h, signal.SIGKILL, log, "harness")
    if _wait_gone(sysm, h, HARNESS_KILL_TIMEOUT):
        log("stop", "ok", via="harness SIGKILL", harness_pid=h.pid)
        return True
    log("stop", "failed", reason="harness pid survived SIGKILL")
    return False


def verify_relaunch(sysm: System, session_dir: str, old_pid: int, timeout: float = VERIFY_TIMEOUT
                    ) -> Optional[ProcRow]:
    deadline = sysm.now() + timeout
    while True:
        hits = [h for h in find_harness(sysm.ps(), session_dir) if h.pid != old_pid]
        if len(hits) == 1:
            return hits[0]
        if sysm.now() >= deadline:
            return None
        sysm.sleep(POLL_INTERVAL)


def _copy_bundle_forward(session_dir: str, log: Logger) -> bool:
    """X13 S2: the new run's relaunch bundle is the old one COPIED FORWARD (generation + 1), written BEFORE anything is
    stopped so a failure leaves the running session untouched. Reads only the old bundle and the live machine.json."""
    from .workspace import bundle as _bundle

    try:
        nb = _bundle.copy_forward(session_dir, enabled=True)
    except Exception as exc:  # noqa: BLE001 - any failure refuses: never relaunch from partial state
        log("aborted", "refused", reason=f"relaunch bundle could not be copied forward ({type(exc).__name__}: {exc}); nothing stopped")
        return False
    log("bundle", "ok", generation=nb.generation, path=nb.path)
    return True


def _prepare_launch_json(sysm: System, session_dir: str, opts: Options, log: Logger
                         ) -> Tuple[bool, Optional[str], List[str]]:
    """Flag ON only (``opts.derive_keys``): the ONE launch.json write of a relaunch, done AFTER the rails passed and
    BEFORE anything is stopped, so a refusal or a failed write leaves the running session untouched.

    * resume keys absent: re-derive (the state may have moved since the rails were evaluated) and refuse if it no longer
      holds; keys present: re-run the rail's transcript / directory check for the same reason;
    * ``resume_prompt`` := the neutral 'relaunched at <UTC>' line (never the sticky text);
    * ``--prompt-once`` text -> ``resume_prompt_once`` (+ who/when/sha256 and an audit entry; logged as ``one-shot-armed``).
    Atomic, ALWAYS with a backup of the previous file. Returns ``(ok, backup_path, keys_written)``; ok False = abort,
    nothing stopped. The caller undoes the write with :func:`_rollback_launch_json` if the stop or the launch fails."""
    if not opts.derive_keys:
        return True, None, []
    from . import resume_keys as rk

    armed_by = f"gnomon-relaunch --prompt-once (relaunch {log.relaunch_id}, pid {sysm.getpid()})" if opts.prompt_once else ""
    # ONE lock hold: the launch.json read, the derive-or-present decision and the write (round 3, P2-1).
    prep = rk.prepare_launch_updates(session_dir, now=sysm.now(), prompt_once=opts.prompt_once, armed_by=armed_by,
                                     pid=sysm.getpid())
    if prep.refusal:
        log("aborted", "refused", reason=prep.refusal)
        return False, None, []
    try:
        log("launch-json", "ok" if prep.ok else "failed", detail=prep.detail, backup=prep.backup,
            derived=prep.derived is not None, resume_session_id=prep.derived.rid if prep.derived else prep.resume_session_id)
        if not prep.ok:
            if prep.backup:     # the write may have partly landed (verification failed): undo it
                _rollback_launch_json(session_dir, prep.backup, prep.keys, log)
            rk.journal_clear(session_dir)
            log("aborted", "refused", reason="launch.json could not be updated; nothing stopped: " + prep.detail)
            return False, None, []
        if opts.prompt_once:
            dig = rk.text_digest(opts.prompt_once)
            log("one-shot-armed", "ok", armed_by=armed_by, sha256=dig["sha256"], preview=dig["preview"])
    except BaseException:
        # Round 7 item 5 (P3-2): launch.json is ALREADY written here. An exception from the logging above (or a KeyboardInterrupt) escapes
        # before the caller's try/finally exists, so undo the write here (rollback, then journal only if the rollback worked) and re-raise.
        if prep.ok and prep.keys:
            _finish_launch_json(session_dir, prep.backup, prep.keys, log, False)
        raise
    return True, prep.backup, prep.keys


def _rollback_launch_json(session_dir: str, bak: Optional[str], keys: List[str], log: Logger) -> bool:
    """The relaunch did not happen (stop or launch failed): put back what :func:`_prepare_launch_json` wrote, so no
    derived keys, no 'relaunched at' line and no armed one-shot outlive a relaunch that never took place."""
    if not keys:
        return True
    from . import resume_keys as rk

    ok, detail = rk.rollback_launch_updates(session_dir, bak, keys)
    log("launch-json-rollback", "ok" if ok else "failed", detail=detail, backup=bak)
    return ok


def _finish_launch_json(session_dir: str, bak: Optional[str], wkeys: List[str], log: Logger, keep: bool) -> None:
    """The ``finally`` of a worker that wrote launch.json: roll back unless the relaunch is to be kept (it completed, or
    the verify timed out: a harness may be mid-start), then delete the journal (only if the rollback worked, so a failed
    one stays reported). Runs for ANY exit, an exception or KeyboardInterrupt included; never raises."""
    if not wkeys:
        return
    from . import resume_keys as rk

    ok = True
    if not keep:
        try:
            ok = _rollback_launch_json(session_dir, bak, wkeys, log)
        except Exception as exc:  # noqa: BLE001
            ok = False
            try:
                log("launch-json-rollback", "failed", detail=f"{type(exc).__name__}: {exc}", backup=bak)
            except Exception:  # noqa: BLE001
                pass
    if ok:
        try:
            rk.journal_clear(session_dir)
        except OSError:
            pass


WORKER_LOCK_NAME = "relaunch.worker.lock"


def run_worker(sysm: System, session_dir: str, opts: Options, log: Logger, grace: float,
               expect: Optional[ProcRow] = None, start: Optional[float] = None) -> int:
    """The detached half: wait for the rails, stop, relaunch, verify. ``expect`` is
    the harness the caller evaluated — a different pid/start time aborts.

    Flag ON (``opts.derive_keys``) only: ONE worker per session. A per-session exclusive lock is held for the whole run;
    a second worker refuses with a plain message naming the holder's pid and writes nothing. A journal left by a worker
    that died is rolled back before anything else. Flag OFF: straight to the previous body."""
    if not opts.derive_keys:
        return _run_worker(sysm, session_dir, opts, log, grace, expect, start)
    import fcntl

    lock_path = os.path.join(claude_dir(session_dir), WORKER_LOCK_NAME)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        log("aborted", "refused", reason=f"cannot open the per-session worker lock {lock_path} ({exc}); nothing written")
        return EXIT_REFUSED
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            try:
                holder = os.pread(fd, 32, 0).decode("ascii", "replace").strip() or "unknown"
            except OSError:
                holder = "unknown"
            log("aborted", "refused", reason=f"another gnomon-relaunch worker for this session is already running (pid "
                f"{holder}, lock {lock_path}); this one wrote nothing and stopped nothing")
            return EXIT_REFUSED
        try:
            os.ftruncate(fd, 0)
            os.pwrite(fd, str(sysm.getpid()).encode("ascii"), 0)
        except OSError:
            pass
        from . import resume_keys as rk

        j = rk.journal_read(session_dir)
        if j is not None:
            log("journal", "found", detail=rk.describe_journal(j, session_dir))
            rok, rdetail = rk.rollback_journal(session_dir)
            log("journal-rollback", "ok" if rok else "failed", detail=rdetail)
            if not rok:
                log("aborted", "refused", reason="a previous worker's journal could not be rolled back; nothing stopped: "
                    + rdetail)
                return EXIT_REFUSED
        return _run_worker(sysm, session_dir, opts, log, grace, expect, start)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _run_worker(sysm: System, session_dir: str, opts: Options, log: Logger, grace: float,
                expect: Optional[ProcRow] = None, start: Optional[float] = None) -> int:
    start = sysm.now() if start is None else start
    log("worker-start", "info", grace=grace, wait_idle=opts.wait_idle, compact_first=opts.compact_first)
    sysm.sleep(grace)
    if expect is None:
        return _launch_only_worker(sysm, session_dir, opts, log)
    if opts.compact_first:
        cdeadline = start + DEFAULT_COMPACT_WAIT
        while not compact_since(session_dir, start):
            if sysm.now() >= cdeadline:
                log("aborted", "refused", reason=f"no compact landed within {DEFAULT_COMPACT_WAIT:.0f}s; "
                    "nothing stopped")
                return EXIT_REFUSED
            sysm.sleep(POLL_INTERVAL * 2)
        log("compact-seen", "info")
    deadline = sysm.now() + opts.wait_idle
    while True:
        plan = evaluate(sysm, session_dir, Options(**{**opts.__dict__, "compact_first": False}),
                        idle_can_wait=False, compact_pending_ok=False)
        if plan.harness is not None and expect is not None and (
                plan.harness.pid != expect.pid or plan.harness.lstart != expect.lstart):
            log("aborted", "refused", reason=f"the harness changed under us (was pid {expect.pid}, "
                f"now pid {plan.harness.pid}); nothing stopped")
            return EXIT_REFUSED
        if not plan.refused:
            break
        if sysm.now() >= deadline:
            log("aborted", "refused", reason="rails still failing at the deadline; nothing stopped",
                rails={r.name: f"{r.status}: {r.detail}" for r in plan.refused})
            return EXIT_REFUSED
        sysm.sleep(POLL_INTERVAL)
    log("rails", "pass", rails={r.name: r.status for r in plan.rails},
        harness_pid=plan.harness.pid, claude_pid=plan.claude.pid)
    if opts.scope_core and not _copy_bundle_forward(session_dir, log):
        return EXIT_REFUSED
    wrote, bak, wkeys = _prepare_launch_json(sysm, session_dir, opts, log)
    if not wrote:
        return EXIT_REFUSED
    keep = False        # the finally rolls launch.json back unless the relaunch is to be kept
    try:
        if not graceful_stop(sysm, plan, log):
            return EXIT_ERR
        ok, detail, running = launch_with_retry(sysm, plan, session_dir, log, plan.harness.pid)
        if not ok:
            keep = running     # a harness may be mid-start and has to find its keys (as after a verify timeout)
            return EXIT_ERR
        keep = True     # launched: from here a harness may be mid-start and has to find its keys
        if plan.adapter.name == "print":
            log("verify", "skipped", reason="print adapter: the operator runs the command")
            return EXIT_OK
        new = verify_relaunch(sysm, session_dir, plan.harness.pid)
        if new is None:
            log("verify", "failed", reason=f"no new harness pid within {VERIFY_TIMEOUT:.0f}s")
            return EXIT_ERR
        log("verify", "ok", new_harness_pid=new.pid, old_harness_pid=plan.harness.pid)
        bring_to_front(sysm, plan, log)
        return EXIT_OK
    finally:
        _finish_launch_json(session_dir, bak, wkeys, log, keep)


def _already_up(sysm: System, session_dir: str, old_pid: int) -> Tuple[str, str]:
    """Before any retry: is the session (or something that looks like it) already running?
    ``("none", "")`` | ``("exact", detail)`` (exactly one harness for this dir, not ``old_pid``: go to verify) |
    ``("stop", detail)`` (more than one exact, a near-miss harness, or a claude carrying this resume id: never launch again) |
    ``("unknown", detail)`` (the look itself could not be completed: UNKNOWN IS NOT "NOTHING"; treated like "stop").
    Uses the tool's own probes (``find_harness``, ``near_miss_harness``, ``live_resume_claudes``)."""
    try:
        rows = sysm.ps()
    except Exception as exc:  # noqa: BLE001 - any failure to list processes means we cannot say nothing is running
        return "unknown", f"the process listing could not be read ({type(exc).__name__}: {exc})"
    cfg = read_launch(session_dir)
    if not isinstance(cfg, dict):
        return "unknown", ("launch.json could not be read, so the check for a claude carrying this session's "
                           "resume id could not be made")
    exact = [h for h in find_harness(rows, session_dir) if h.pid != old_pid]
    near = near_miss_harness(rows, session_dir)
    rid = cfg.get("resume_session_id")
    claudes = live_resume_claudes(rows, str(rid)) if rid else []
    if len(exact) == 1 and not near:
        return "exact", f"harness pid {exact[0].pid} for this session dir appeared although the launch reported a failure"
    if exact or near or claudes:
        found = ([f"harness pid {r.pid} for this dir" for r in exact] +
                 [f"near-miss harness pid {r.pid}" for r in near] +
                 [f"claude pid {r.pid} carrying --resume {rid}" for r in claudes])
        return "stop", "; ".join(found)
    return "none", ""


def bring_to_front(sysm: System, plan: Plan, log: Logger) -> None:
    """After a verified launch, raise the terminal. Failure (or an exception) is logged and changes nothing:
    the session is up, and focus is cosmetic."""
    try:
        ok, detail = plan.adapter.activate(sysm)  # type: ignore[union-attr]
    except Exception as e:  # noqa: BLE001 - never let a cosmetic step fail the relaunch
        ok, detail = False, f"{type(e).__name__}: {e}"
    log("activate", "ok" if ok else "failed", adapter=plan.adapter.name, detail=detail)


def launch_with_retry(sysm: System, plan: Plan, session_dir: str, log: Logger,
                      old_pid: int = -1) -> Tuple[bool, str, bool]:
    """Run the adapter's launch up to ``LAUNCH_ATTEMPTS`` times, one ``[launch]`` log line per attempt.
    After EVERY failed attempt, including the last, wait ``LAUNCH_RETRY_WAIT`` s (so a harness the
    failed attempt did start can show up) and then ``_already_up`` looks for a harness, a near-miss or
    a stray claude. A failure report from osascript does not prove nothing was typed, and a second
    launch could run two harnesses on one session dir. One exact harness -> ``(True, ..., False)`` (the
    caller verifies it); anything found, or anything the look could not read -> a ``[launch-recheck]
    stop`` line and ``(False, ..., True)`` (the caller must keep launch.json: something may be
    mid-start). Only when the last failure is followed by a clean look that finds nothing is the
    session declared DOWN, with the command that reopens it. Known limit: the gap between a look that
    found nothing and the next launch call is milliseconds wide and is not closed (it would need a
    lock). Returns ``(ok, detail, maybe_running)``."""
    ok, detail = False, ""
    for n in range(1, LAUNCH_ATTEMPTS + 1):
        ok, detail = plan.adapter.launch(sysm, plan.command, log.echo)  # type: ignore[union-attr]
        log("launch", "ok" if ok else "failed", adapter=plan.adapter.name, detail=detail,
            command=plan.command, attempt=n, attempts=LAUNCH_ATTEMPTS)
        if ok:
            return True, detail, False
        sysm.sleep(LAUNCH_RETRY_WAIT)
        state, found = _already_up(sysm, session_dir, old_pid)
        if state == "exact":
            log("launch-recheck", "found", detail=f"{found}; NOT launching again, going to verify", attempt=n)
            return True, found, False
        if state in ("stop", "unknown"):
            what = (f"found {found}" if state == "stop" else f"could not finish looking: {found}")
            log("launch-recheck", "stop", detail=f"{what}, after a failed launch; NOT launching again. "
                f"launch.json was KEPT, not rolled back (a harness may be mid-start and needs its keys); to undo "
                f"it later use ROLLBACK in docs/BREAK-GLASS-relaunch.md "
                f"(~/.claude/bin/gnomon-relaunch {shlex.quote(session_dir)} --strip-derived-keys). "
                f"First look at what is running: ps -axww | grep gnomon.harness | grep {shlex.quote(session_dir)}",
                attempt=n)
            return False, detail, True
    sd = shlex.quote(session_dir)
    log("session-down", "failed",
        detail=f"THE SESSION IS DOWN: no terminal tab could be opened after {LAUNCH_ATTEMPTS} tries "
               f"(last: {detail}). FIRST check nothing is running for it: "
               f"ps -axww | grep gnomon.harness | grep {sd}   Only if no harness shows, reopen it: "
               f"~/.claude/bin/gnomon-relaunch {sd}   "
               f"(if that refuses on last-exit, add --force-launch); or by hand in a terminal: {plan.command}")
    return False, detail, False


def _launch_only_worker(sysm: System, session_dir: str, opts: Options, log: Logger) -> int:
    """Launch-only: re-evaluate (a harness that appeared meanwhile aborts), then
    straight to the terminal adapter — nothing is ever signalled."""
    plan = evaluate(sysm, session_dir, opts, idle_can_wait=False, compact_pending_ok=False)
    if plan.mode != "launch-only":
        log("aborted", "refused", reason="a harness for this dir appeared before the launch "
            f"(pid {plan.harness.pid if plan.harness else '?'}); nothing launched")
        return EXIT_REFUSED
    if plan.refused:
        log("aborted", "refused", reason="launch-only rails failing at launch time; nothing launched",
            rails={r.name: f"{r.status}: {r.detail}" for r in plan.refused})
        return EXIT_REFUSED
    if opts.scope_core and not _copy_bundle_forward(session_dir, log):
        return EXIT_REFUSED
    wrote, bak, wkeys = _prepare_launch_json(sysm, session_dir, opts, log)
    if not wrote:
        return EXIT_REFUSED
    keep = False
    try:
        log("launch-only", "info", reason="no harness running for this exact session dir; stop phase "
            "skipped", last_exit=plan.last_exit, forced=opts.force_launch, adapter=plan.adapter.name,
            command=plan.command)
        ok, detail, running = launch_with_retry(sysm, plan, session_dir, log)
        if not ok:
            keep = running
            return EXIT_ERR
        keep = True
        if plan.adapter.name == "print":
            log("verify", "skipped", reason="print adapter: the operator runs the command")
            return EXIT_OK
        new = verify_relaunch(sysm, session_dir, -1)
        if new is None:
            log("verify", "failed", reason=f"no new harness pid within {VERIFY_TIMEOUT:.0f}s")
            return EXIT_ERR
        log("verify", "ok", new_harness_pid=new.pid, old_harness_pid=None)
        bring_to_front(sysm, plan, log)
        return EXIT_OK
    finally:
        _finish_launch_json(session_dir, bak, wkeys, log, keep)


# ---------------------------------------------------------------- detach

def detach(fn: Callable[[], int], log_file: str) -> int:
    """Double-fork + setsid; the grandchild runs ``fn`` with stdio on ``log_file``.
    Returns the grandchild pid to the caller (who returns at once)."""
    r, w = os.pipe()
    pid = os.fork()
    if pid == 0:  # first child
        try:
            os.close(r)
            os.setsid()
            pid2 = os.fork()
            if pid2 != 0:
                os.write(w, str(pid2).encode())
                os._exit(0)
            os.close(w)
            fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            nul = os.open(os.devnull, os.O_RDONLY)
            os.dup2(nul, 0)
            os.dup2(fd, 1)
            os.dup2(fd, 2)
            sys.stdout = os.fdopen(1, "w", buffering=1)
            sys.stderr = os.fdopen(2, "w", buffering=1)
            signal.signal(signal.SIGHUP, signal.SIG_IGN)
            rc = 1
            try:
                rc = int(fn() or 0)
            except BaseException as exc:  # noqa: BLE001
                print(f"gnomon-relaunch worker: ERROR: {type(exc).__name__}: {exc}", flush=True)
            finally:
                try:
                    sys.stdout.flush()
                except Exception:  # noqa: BLE001
                    pass
            os._exit(rc)
        except BaseException:  # noqa: BLE001
            os._exit(1)
    os.close(w)
    data = b""
    while True:
        chunk = os.read(r, 32)
        if not chunk:
            break
        data += chunk
    os.close(r)
    os.waitpid(pid, 0)
    return int(data or b"0")


# ---------------------------------------------------------------- main

def _scope_core_on() -> bool:
    """Is ``workspace_scope_core`` ON? (X13 S2; the registry default is OFF.) Never raises."""
    try:
        from . import flags
        from .workspace.scopebase import SCOPE_CORE_FLAG

        return bool(flags.flag(SCOPE_CORE_FLAG))
    except Exception:  # noqa: BLE001 - a broken flag read must not break a relaunch: it stays OFF
        return False


def _derive_keys_on(session_dir: str) -> bool:
    """Is ``relaunch_derives_resume_keys`` ON for this session (launch.json ``flags`` > machine config)?
    Never raises; any failure is OFF = the previous tool."""
    try:
        from .resume_keys import flag_on

        return flag_on(read_launch(session_dir))
    except Exception:  # noqa: BLE001
        return False


def _alert_bundle_refusal(plan: Plan, session_dir: str) -> None:
    """A refused relaunch whose ``relaunch-bundle`` rail failed raises an alert event (never silent; ENT-HANDOVER
    ``unclean-boot``). Best effort but LOUD: if the alert cannot be written that is printed."""
    bad = [r for r in plan.refused if r.name == "relaunch-bundle"]
    if not bad:
        return
    try:
        from .config import ConfigStore
        from .workspace import events as _events

        path = _events.resolve_event_log_path(ConfigStore().load().resolved_deploy_root())
        _events.append_event(path, "relaunch-refused", severity="error", enabled=True,
                             message=f"relaunch of {os.path.basename(session_dir.rstrip('/'))} refused: {bad[0].detail}",
                             detail={"session-dir": session_dir})
    except Exception as exc:  # noqa: BLE001
        print(f"gnomon-relaunch: WARNING: the refusal alert could NOT be written ({type(exc).__name__}: {exc})")


def main(argv: Optional[List[str]] = None, sysm: Optional[System] = None,
         detacher: Callable[[Callable[[], int], str], int] = detach) -> int:
    ap = argparse.ArgumentParser(prog="gnomon-relaunch", description=__doc__.split("\n\n")[1],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("session_dir")
    ap.add_argument("--compact-first", action="store_true",
                    help="wait for a D42-gated compact (request_compact / operator) before stopping")
    ap.add_argument("--directive", default="", help="with --compact-first: the COMPACT-DIRECTIVE.md "
                    "path the gate checks for staleness")
    ap.add_argument("--approval-note", default="", help="with --compact-first: a pilot approval note "
                    "(the gate's ch_approved override)")
    ap.add_argument("--dry-run", action="store_true", help="evaluate every rail; change NOTHING")
    ap.add_argument("--terminal", choices=None, default=None,
                    help=f"terminal adapter ({'|'.join(sorted(ADAPTERS))}); default iterm if running, else print")
    ap.add_argument("--wait-idle", type=float, default=DEFAULT_WAIT_IDLE,
                    help=f"seconds the worker waits for the rails (default {DEFAULT_WAIT_IDLE:.0f})")
    ap.add_argument("--grace", type=float, default=DEFAULT_GRACE,
                    help="seconds the worker sleeps first so the caller's tool call returns")
    ap.add_argument("--force-launch", action="store_true",
                    help="launch-only mode: launch even if the last lifecycle record is not a "
                    "clean agent-end (crash, kill, or no record)")
    ap.add_argument("--require-line-empty", action="store_true",
                    help="refuse when the operator line cannot be seen (old harness)")
    ap.add_argument("--prompt-once", default="", metavar="TEXT",
                    help="a deliberate ONE-SHOT resume prompt: delivered by the relaunched harness's first start, then "
                    "cleared from launch.json (needs relaunch_derives_resume_keys ON; replaces the neutral "
                    "'relaunched at' line for that one start)")
    ap.add_argument("--strip-derived-keys", action="store_true",
                    help="ROLLBACK: put back the launch.json keys a flag-ON relaunch derived (resume_session_id, resume_cwd, ...), from the "
                    "newest backup that predates them, so the session can be started on code without the flag; with --dry-run it prints "
                    "the change and writes nothing; refuses on a running session")
    ap.add_argument("--version", action="version", version="gnomon-relaunch " + VERSION)
    a = ap.parse_args(argv)
    sysm = sysm or System()
    if a.wait_idle < 0 or a.grace < 0:
        print("gnomon-relaunch: REFUSED: --wait-idle/--grace must be >= 0")
        return EXIT_REFUSED
    session_dir = os.path.abspath(os.path.expanduser(a.session_dir))
    if a.strip_derived_keys:
        return strip_derived_keys(sysm, session_dir, a.dry_run)
    derive_keys = _derive_keys_on(session_dir)
    if a.prompt_once and not derive_keys:
        print("gnomon-relaunch: REFUSED: --prompt-once needs relaunch_derives_resume_keys ON "
              "(it is OFF: this run changes nothing)")
        return EXIT_REFUSED
    opts = Options(compact_first=a.compact_first, directive=a.directive, approval_note=a.approval_note,
                   require_line_empty=a.require_line_empty, terminal=a.terminal, wait_idle=a.wait_idle,
                   force_launch=a.force_launch, scope_core=_scope_core_on(),
                   derive_keys=derive_keys, prompt_once=a.prompt_once)
    try:
        plan = evaluate(sysm, session_dir, opts)
    except Exception as exc:  # noqa: BLE001
        print(f"gnomon-relaunch: ERROR evaluating rails: {type(exc).__name__}: {exc}")
        return EXIT_ERR
    print(render_plan(plan, opts))
    if a.dry_run:
        print("gnomon-relaunch: --dry-run: nothing stopped, nothing launched, nothing logged")
        return EXIT_REFUSED if plan.refused else EXIT_OK
    rid = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(sysm.now())) + "-" + uuid.uuid4().hex[:6]
    log = Logger(session_dir, plan.instance_id, rid, enabled=True, sysm=sysm)
    if plan.refused:
        if os.path.isdir(claude_dir(session_dir)):
            log("refused", "refused", rails={r.name: f"{r.status}: {r.detail}" for r in plan.refused})
        if opts.scope_core:
            _alert_bundle_refusal(plan, session_dir)
        return EXIT_REFUSED
    if plan.mode == "launch-only":
        log("dispatch", "info", mode="launch-only", harness_pid=None, adapter=plan.adapter.name,
            last_exit=plan.last_exit, forced=opts.force_launch)
    else:
        log("dispatch", "info", mode="relaunch", harness_pid=plan.harness.pid,
            claude_pid=plan.claude.pid, self_fire=plan.self_fire, adapter=plan.adapter.name,
            waiting=[r.name for r in plan.waiting])
    logdir = os.path.join(claude_dir(session_dir), "relaunch")
    os.makedirs(logdir, exist_ok=True)
    log_file = os.path.join(logdir, rid + ".log")
    wlog = Logger(session_dir, plan.instance_id, rid, enabled=True, sysm=sysm,
                  echo=lambda s: print(s, flush=True))
    start = sysm.now()
    child = detacher(lambda: run_worker(sysm, session_dir, opts, wlog, a.grace,
                                        expect=plan.harness, start=start), log_file)
    print(f"gnomon-relaunch: detached worker pid {child}; log {log_file}; "
          f"progress in {log_path(session_dir)} (kind=lifecycle point=relaunch relaunch_id={rid})")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

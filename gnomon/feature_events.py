"""the feature-usage EMITTER (spec,
).

One function, :func:`feature_event`, is called at every feature site. It appends ONE
categories-only JSON line to a per-process-KIND, per-local-day JSONL file under the event
root; a background indexer (:mod:`gnomon.usage_index_job`, via
:mod:`gnomon.usage_metrics`) loads those lines into the usage DB. The call site never
touches sqlite.

CONTRACT (never weakened by a call site):

* it NEVER raises into its caller (every fault, including a ``BaseException`` such as a
  ``KeyboardInterrupt`` during the wait, returns ``False``; the caller's verdict and exit are untouched);
* it is BOUNDED (Diamond F1/F-B): the I/O runs on ONE short-lived daemon thread per call and the caller waits
  at most :data:`EMIT_BOUND_S`; a thread still alive after that is abandoned, and while more than
  :data:`MAX_ABANDONED` abandoned threads are alive new emits are dropped (counted) without spawning. It takes no
  lock, opens no sqlite, no socket, never fsyncs, sleeps or retries: per call (on that thread) it does at most one
  ``mkdir``, one ``open(O_WRONLY|O_APPEND|O_CREAT|O_NONBLOCK)``, one ``fstat`` (a non-regular file is refused), one
  ``os.write`` of the whole line (<= :data:`MAX_LINE_BYTES`, so POSIX appends do not interleave; a short write is
  a counted FAILURE), one ``close`` (plus, only when earlier calls failed, ONE second ``write`` of the
  ``feature-events-dropped`` summary on the same descriptor);
* a hook-kind process (``hooks``) NEVER writes to stderr from here (Diamond F-A): a hook's stderr is its verdict
  channel (a deny block message), so a drop is only COUNTED; other kinds write ONE stderr line on the first fault;
* it NEVER opens a path under a workspace tree: its only sources of scope are its
  arguments, the session directory's own files and the environment, so it is safe to call from
  safety paths (deny-guard, PreCompact, SessionStart);
* the record is closed: a fixed key set, no content (no text, no arguments, no paths, no
  error strings).

Flags (registry defaults in :mod:`gnomon.flags`, read ONCE per process through
``flags.flag`` with no ``default=`` of their own; if the machine config is not PROVABLY absent and cannot be read or
parsed (a dangling symlink or an unreadable parent included, g48b(b)) the emitter fails OFF, see :func:`_read_flags`):
``feature_events`` (registry default ON: the operator) and ``hook_noop_events``
(registry default OFF).

This module is neutral (D1, g3): no deployment or tenant identifier appears in it.
Stdlib only; the ``flags`` import is lazy and failure-tolerant.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import stat
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

SCHEMA_VERSION = 1
MAX_LINE_BYTES = 512

FLAG_EVENTS = "feature_events"
FLAG_NOOP = "hook_noop_events"
ENV_EVENTS_DIR = "GNOMON_FEATURE_EVENTS_DIR"
ENV_PROBE = "GNOMON_PROBE"
ENV_SESSION_DIR = "GNOMON_SESSION_DIR"

#: closed enums (g24(d): a new value changes validation, the indexer
#: allowlist, the report and the registry in one change)
SURFACES = ("hook", "self-verb", "operator-command", "hub-mcp-tool", "web-api", "subagent", "skill")
OUTCOMES = ("ok", "denied", "refused", "error", "noop")
#: the closed process-KIND set (g24(a)); one daily JSONL per kind
KINDS = ("hooks", "harness", "web", "cli")
#: a process that never declares its kind is a command-line tool
DEFAULT_KIND = "cli"
#: emitter-internal (system) feature ids (g24(c)); never user-facing, no registry node
DROPPED_FEATURE_ID = "feature-events-dropped"
RESERVED_FEATURE_IDS = frozenset({DROPPED_FEATURE_ID})

FEATURE_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,47}$")
ACTOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
#: INTERIM workspace/project slug shape (field-workspace-project) until
#: the workspace model states its own grammar
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")

#: p99 budget (ms) for one call; an architect-choice placeholder (
#: requirement-latency) to be replaced by the measured figure with margin
FU_EMIT_BUDGET_MS = 10

#: cap (Diamond F-B) on abandoned (timed-out, still-alive) emit threads: past it a new emit is dropped at once and
#: counted, so a long stall in a long-lived process cannot leak threads or keep costing EMIT_BOUND_S per call
MAX_ABANDONED = 8

#: HARD wall-clock bound (s) on one emit (Diamond F1): the I/O runs on a daemon thread and the caller waits at most
#: this long, so a stalled filesystem (a synced tree, a FIFO) can never delay a hook's verdict. A call that does not
#: finish in time returns False and is counted as a FAILURE; process exit never waits for the thread.
EMIT_BOUND_S = 0.05

#: the viewer id for a human action; the operator-authenticated hub
#: callers and the web UI use it
VIEWER_ACTOR = "ch"

# --- hook points (requirement-noop-events) ---------------------------------
#
# The ONE closed table: hook-point name -> feature_id. A hook point is every harness dispatch
# location that can run consumers for an event. Claude Code hook entry points are named
# ``claude:<hook event name>`` (the key the launcher writes into the session's Claude Code
# settings); internal harness hook registries use their own point names
# (``harness.hooks.POINTS``). A NEW hook point is added here in the same change (g39), and the
# pre-release test (tests/test_fu_hook_noop.py) derives the registered set independently.
HOOK_POINTS: Dict[str, str] = {
    "claude:PreToolUse": "deny-guard",
    "claude:PostToolUse": "post-tool-use-hook",
    "claude:SubagentStop": "subagent-stop-hook",        #
    "claude:PreCompact": "pre-compact-hook",
    "claude:SessionStart": "session-start-hook",
    "claude:UserPromptSubmit": "prompt-command-hook",
    "inbound-injection": "hook-point-inbound-injection",
    "operator-command": "hook-point-operator-command",
    "operator-command-post": "hook-point-operator-command-post",
    "agent-start": "hook-point-agent-start",
    "agent-end": "hook-point-agent-end",
    "compact": "hook-point-compact",
}

#: hook points the pre-release coverage test ("every hook is functional", tests/test_fu_hook_noop.py part g) exempts,
#: BY NAME, each WITH ITS REASON. The exemption is explicit (a named table entry), never an omission. The statusline
#: hook is exempt (the operator's decision, 2026-10-01): it has its own tests. It fires constantly and carries no user intent, so it never emits (exclusion-statusline) and is not in
#: HOOK_POINTS. Adding a name here needs a reason that cites the operator's answer and own-test files that exist
#: (tests/test_x26_d3_statusline_exemption.py checks both). Every hook NOT named here stays checked.
HOOK_COVERAGE_EXEMPTIONS: Dict[str, str] = {
    "claude:statusLine": (
        "The pilot exempted it (decision of 2026-10-01): "
        "the statusline hook fires constantly, carries no user intent and never emits, and its own tests cover it."),
}
#: the test files that are the 'own tests' the reasons above cite (repo-relative), per exempt hook point
HOOK_EXEMPTION_OWN_TESTS: Dict[str, Tuple[str, ...]] = {
    "claude:statusLine": ("tests/test_fixpass_ws1_context_gauge.py", "tests/test_g48d_hook_entry_silence.py"),
}
#: legacy name for the exempt set (tests read it); DERIVED from the one table above so the two cannot disagree
EXEMPT_FROM_HOOK_COVERAGE: Tuple[str, ...] = tuple(HOOK_COVERAGE_EXEMPTIONS)
#: the statusline hook emits nothing. The statusline hook itself does NOT import this module (it fires constantly; its
#: import cost matters), so the constant is checked against the hook's source by the test rather than consulted at run time.
STATUSLINE_EMITS = False

#: the process kind assumed for the self-actuation MCP stdio server (a child of the agent
#: process, spawned by the harness). The closed kind table (g24(a)) names no such process:
#: ASSUMED ``harness`` (it lives in the harness package) pending an architect ruling; changing
#: the answer is this one constant.
SELF_MCP_PROCESS_KIND = "harness"

# --- process state ----------------------------------------------------------------------------

_kind: str = DEFAULT_KIND
_flags: Optional[Tuple[bool, bool]] = None
_drops: int = 0
_stderr_warned: bool = False
_actor_cache: Dict[str, Optional[str]] = {}
#: timed-out emit threads that may still be alive (F-B); pruned on every call
_abandoned: List[threading.Thread] = []
log = logging.getLogger(__name__)
#: optional ``(project) -> bool`` hook a process may register to refuse an ARCHIVED tier-0
#: project. Safety-path processes never register one (I6).
_project_validator: Optional[Callable[[str], bool]] = None
#: the session directory of THIS process when the environment does not carry it (the harness proxy
#: process: its child gets GNOMON_SESSION_DIR, the proxy itself does not)
_session_dir: Optional[str] = None


def _reset_for_tests() -> None:
    global _kind, _flags, _drops, _stderr_warned, _project_validator, _session_dir
    _kind, _flags, _drops, _stderr_warned, _project_validator = DEFAULT_KIND, None, 0, False, None
    _session_dir = None
    _actor_cache.clear()
    del _abandoned[:]


def set_session_dir(path: Optional[str]) -> None:
    """Name THIS process's session directory (the harness proxy, whose environment lacks it). The
    environment's ``GNOMON_SESSION_DIR`` is used when this is unset. Never raises."""
    global _session_dir
    try:
        _session_dir = path if isinstance(path, str) and path else None
    except Exception:  # noqa: BLE001
        pass


def set_process_kind(kind: str) -> bool:
    """Declare THIS process's kind (one of :data:`KINDS`). Returns False (and leaves the
    kind unchanged) for anything else. Never raises."""
    global _kind
    try:
        if kind in KINDS:
            _kind = kind
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def process_kind() -> str:
    return _kind


def set_project_validator(fn: Optional[Callable[[str], bool]]) -> None:
    global _project_validator
    _project_validator = fn


def dropped_count() -> int:
    """Calls in this process that returned False since the last summary (diagnostics)."""
    return _drops


# --- location -----------------------------------------------------------------------------------


def event_root() -> str:
    """The event root, resolved at CALL time (g31(e)). PRE-X13 INTERIM PATH: the sibling of the
    usage DB under the machine runtime tree; ``GNOMON_FEATURE_EVENTS_DIR`` overrides it (tests point
    it at a temp dir, never the real one). Post-X13 this constant is the ONLY thing that changes."""
    env = os.environ.get(ENV_EVENTS_DIR)
    if env:
        return env
    from .config import runtime_root_default

    return os.path.join(runtime_root_default(), "metrics", "feature-events")


# --- flags (read ONCE per process, through the registry) ------------------------------------------


def _read_flags() -> Tuple[bool, bool]:
    global _flags
    if _flags is None:
        try:
            from . import flags as _f

            if _machine_config_unreadable(_f):
                # FAIL OFF (Diamond F-L): flags.flag() would silently fall back to the registry default (events
                # ON), so a corrupt or unreadable config.json would defeat the OFF switch exactly when it is a
                # safety hold. Telemetry loss is cheap; writing after the operator said stop is not.
                log.warning("gnomon feature_events: machine config exists but is unreadable; emitter OFF for this process")
                _flags = (False, False)
            else:
                _flags = (bool(_f.flag(FLAG_EVENTS)), bool(_f.flag(FLAG_NOOP)))
        except Exception:  # noqa: BLE001 - the flag machinery itself failed: fail OFF too (never ON by accident)
            log.warning("gnomon feature_events: flag read failed; emitter OFF for this process")
            _flags = (False, False)
    return _flags


def _config_state(path: str) -> str:
    """``"absent"`` only when the path is PROVABLY absent: ``lstat`` raises FileNotFoundError and the nearest ancestor
    that exists could be stat'ed (every missing ancestor above it is ENOENT too). ``"present"`` when ``lstat`` succeeds,
    which includes a symlink, so a DANGLING symlink is present and the read below fails it. ``"unreadable"`` for any other
    error (EACCES on an unreadable parent, ENOTDIR, EIO, ESTALE, a stalled mount): not provable, so not absent (g48b(b)).
    g48d(a): the nearest existing ancestor must also RESOLVE to a readable directory (a dangling symlink passes ``lstat``
    but not ``stat``), else ``"unreadable"``; only a missing leaf under a resolvable, readable parent is ``"absent"``.
    ``os.path.exists`` is False for all of those and was the F-3 hole."""
    try:
        os.lstat(path)
        return "present"
    except FileNotFoundError:
        pass
    except OSError:
        return "unreadable"
    parent = os.path.dirname(path)
    while parent and parent != os.path.dirname(parent):
        try:
            os.lstat(parent)
            # g48d(a): lstat succeeds on a DANGLING symlink, so the nearest existing ancestor must also RESOLVE (stat
            # follows links) to a readable directory; otherwise the config may be hiding behind it: not provably absent.
            try:
                if not stat.S_ISDIR(os.stat(parent).st_mode):
                    return "unreadable"
                if not os.access(parent, os.R_OK | os.X_OK):
                    return "unreadable"
            except OSError:
                return "unreadable"
            return "absent"
        except FileNotFoundError:
            parent = os.path.dirname(parent)
        except OSError:
            return "unreadable"
    return "absent"


def _machine_config_unreadable(_f: Any) -> bool:
    """True iff the machine config is not PROVABLY absent and cannot be read as a JSON object (g48b(b)). A provably
    absent file is normal (the registry default applies); a dangling symlink, an unreadable parent or any other stat
    error means the operator's OFF may be hiding there, so the emitter fails OFF. A test seam without a path
    (``flags._config_store`` replaced) is not checked."""
    store = _f._config_store()
    path = getattr(store, "path", None)
    if not isinstance(path, (str, os.PathLike)):
        return False
    path = os.fspath(path)
    state = _config_state(path)
    if state == "absent":
        return False
    if state == "unreadable":
        return True
    try:
        return not isinstance(_read_json(path), dict)
    except (OSError, ValueError):
        return True


def events_enabled() -> bool:
    return _read_flags()[0]


def noop_events_enabled() -> bool:
    ev, noop = _read_flags()
    return ev and noop


# --- derivation ------------------------------------------------------------------------------------


def _read_json(path: str) -> Any:
    """Parse ``path`` as JSON WITHOUT ever blocking on a non-regular file (g48b(c), X26R F-5): the open is O_NONBLOCK (a FIFO
    with no writer opens at once instead of hanging) and an ``fstat`` regular-file check runs BEFORE any read; a FIFO, socket
    or device raises OSError (callers treat that as unreadable). ``O_NONBLOCK`` is a no-op for a regular file."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTSUP, "not a regular file")
        fh = os.fdopen(fd, "r", encoding="utf-8")
    except BaseException:
        os.close(fd)
        raise
    with fh:                      # owns fd from here
        return json.load(fh)


def _derive_actor(session_dir: str) -> Optional[str]:
    """instance_id from ``<session_dir>/.claude/launch.json``, else the dir's basename (the same
    rule as usage_metrics._instance_for). Cached per session dir for the life of the process."""
    if session_dir in _actor_cache:
        return _actor_cache[session_dir]
    actor: Optional[str] = None
    try:
        iid = _read_json(os.path.join(session_dir, ".claude", "launch.json")).get("instance_id")
        if isinstance(iid, str) and iid.strip():
            actor = iid.strip()
    except Exception:  # noqa: BLE001 - a missing/corrupt launch.json falls back to the basename
        actor = None
    if actor is None:
        actor = os.path.basename(os.path.normpath(session_dir)) or None
    _actor_cache[session_dir] = actor
    return actor


def _split_project(project: str) -> Optional[Tuple[str, str]]:
    if not isinstance(project, str) or project.count("/") != 1:
        return None
    ws, slug = project.split("/")
    if SLUG_RE.match(ws) and SLUG_RE.match(slug):
        return ws, slug
    return None


def _stamp_scope(session_dir: Optional[str], project: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """(workspace, project) by the precedence. tier 0: an explicit project; tier 1 does
    not apply (an event has no subject); tier 2: the session-run's current-project; tier 3: no
    project, workspace = the session-run's launch workspace; else null/null. The workspace is
    DERIVED from the project when one is present. Reads ONLY the session directory's own
    scope-state file (never a workspace tree: I6). Raises ValueError for a malformed or refused
    explicit project (the caller drops the event)."""
    if project is not None:
        parts = _split_project(project)
        if parts is None:
            raise ValueError("project shape")
        if _project_validator is not None and not _project_validator(project):
            raise ValueError("project refused")
        return parts[0], project
    if not session_dir:
        return None, None
    try:
        state = _read_json(os.path.join(session_dir, ".claude", "scope-state.json"))
        if not isinstance(state, dict):
            return None, None
    except Exception:  # noqa: BLE001 - no readable state => no scope; never an error
        return None, None
    cur = state.get("current-project")
    parts = _split_project(cur) if isinstance(cur, str) else None
    if parts is not None:
        return parts[0], cur
    ws = state.get("launch-workspace")
    if isinstance(ws, str) and SLUG_RE.match(ws):
        return ws, None
    return None, None


# --- the emitter ------------------------------------------------------------------------------------


def _warn_once(reason: str) -> None:
    global _stderr_warned
    if _stderr_warned:
        return
    _stderr_warned = True
    try:
        sys.stderr.write(f"gnomon feature_events: an event was dropped ({reason}); "
                         "further drops are counted and summarised at the next successful emit\n")
    except Exception:  # noqa: BLE001
        pass


def _note_drop(reason: str, *, failure: bool = False) -> bool:
    """Count one call that returned False. ``failure=True`` marks a FAULT (an I/O error, a crash, an unreadable
    registry), as opposed to a DROP by design (a field that fails its shape, no actor, a refused no-op): only the
 first FAILURE per process lifetime writes the one stderr line (requirement-dropped-summary
    separates 'a failure or a drop' for the count from 'the first failure' for stderr), EXCEPT in a ``hooks``-kind
    process, which never writes stderr from here (F-A) and only counts."""
    global _drops
    try:
        _drops += 1
        if failure and _kind != "hooks":   # a hook's stderr is its verdict channel: count only (F-A)
            _warn_once(reason)
    except Exception:  # noqa: BLE001
        pass
    return False


def _line(obj: Dict[str, Any]) -> Optional[bytes]:
    data = (json.dumps(obj, separators=(",", ":"), ensure_ascii=True) + "\n").encode("ascii")
    return data if len(data) <= MAX_LINE_BYTES else None


def feature_event(feature_id: str, actor: Optional[str], surface: str, *, project: Optional[str] = None,
                  outcome: Optional[str] = None) -> bool:
    """Record ONE use of a feature. Returns True iff one complete line was handed to the OS; False
    on ANY failure or drop (nothing raises). ``actor``: the viewer id or an instance id when the
    caller knows it, else derived from ``GNOMON_SESSION_DIR``; a call that has no actor is dropped,
    never written with a guess. ``outcome`` is REQUIRED for the hook surface. ``probe`` is not a
    parameter: ``GNOMON_PROBE=1`` in the environment stamps ``probe: true``."""
    try:
        _abandoned[:] = [t for t in _abandoned if t.is_alive()]
        if len(_abandoned) >= MAX_ABANDONED:   # F-B: stalled; do not spawn, do not wait
            return _note_drop("backlog", failure=True)
        res = [False]

        def run() -> None:
            try:
                res[0] = _feature_event(feature_id, actor, surface, project, outcome)
            except Exception as exc:  # noqa: BLE001 - the contract: never raise into the caller
                try:
                    _note_drop(type(exc).__name__, failure=True)
                except Exception:  # noqa: BLE001
                    pass

        t = threading.Thread(target=run, name="fu-emit", daemon=True)
        t.start()
        t.join(EMIT_BOUND_S)
        if t.is_alive():                      # stalled: give up NOW; the thread is abandoned (daemon)
            _abandoned.append(t)
            return _note_drop("timeout", failure=True)
        return res[0]
    except BaseException as exc:  # noqa: BLE001 - incl. KeyboardInterrupt/SystemExit during the wait: the verdict is
        # already written, so the hook's exit must stay what it computed; the emitter only counts the drop
        try:
            return _note_drop(type(exc).__name__, failure=True)
        except BaseException:  # noqa: BLE001 - even the drop accounting is best-effort
            return False


def _feature_event(feature_id: Any, actor: Any, surface: Any, project: Any, outcome: Any) -> bool:
    on, noop_on = _read_flags()
    if not on:
        return False  # switched off in machine config: nothing written, not a failure
    # -- schema (a field that fails its shape is NOT written) -------------------------------------
    if not (isinstance(feature_id, str) and FEATURE_ID_RE.match(feature_id)) or feature_id in RESERVED_FEATURE_IDS:
        return _note_drop("feature_id")
    if surface not in SURFACES or not isinstance(surface, str):
        return _note_drop("surface")
    if outcome is not None and (not isinstance(outcome, str) or outcome not in OUTCOMES):
        return _note_drop("outcome")
    if surface == "hook" and outcome is None:
        return _note_drop("hook outcome required")
    if outcome == "noop" and (surface != "hook" or not noop_on):
        return _note_drop("noop")
    if project is not None and not isinstance(project, str):
        return _note_drop("project")
    kind = _kind
    # the web process (which also hosts the hub) serves the viewer, not a session-run: session_run and
    # the session scope are null by contract, even if the process inherited a session env (field-session-run).
    session_dir = (_session_dir or os.environ.get(ENV_SESSION_DIR) or None) if kind != "web" else None
    if actor is None:
        actor = _derive_actor(session_dir) if session_dir else None
    if not (isinstance(actor, str) and ACTOR_RE.match(actor)):
        return _note_drop("actor")
    session_run: Optional[str] = None
    if session_dir:
        sr = os.path.basename(os.path.normpath(session_dir))
        session_run = sr if ACTOR_RE.match(sr) else None
    try:
        workspace, project = _stamp_scope(session_dir, project)
    except ValueError as exc:
        return _note_drop(str(exc))
    now = time.time()
    rec: Dict[str, Any] = {"v": SCHEMA_VERSION, "ts": round(now, 3), "feature_id": feature_id,
                           "actor": actor, "surface": surface, "session_run": session_run,
                           "workspace": workspace, "project": project}
    if outcome is not None:
        rec["outcome"] = outcome
    probe = os.environ.get(ENV_PROBE) == "1"
    if probe:
        rec["probe"] = True
    data = _line(rec)
    if data is None:
        return _note_drop("line too long")
    summary: Optional[bytes] = None
    pending = _drops
    if pending > 0:
        srec: Dict[str, Any] = {"v": SCHEMA_VERSION, "ts": round(now, 3), "feature_id": DROPPED_FEATURE_ID,
                                "actor": actor, "surface": kind, "session_run": session_run,
                                "workspace": workspace, "project": project}
        if probe:
            srec["probe"] = True
        srec["count"] = pending
        summary = _line(srec)
    # -- I/O: one mkdir, one open, one write, one close ----------------------------------------------
    root = event_root()
    day_dir = os.path.join(root, time.strftime("%Y%m%d", time.localtime(now)))
    try:
        os.mkdir(day_dir, 0o700)
    except FileExistsError:
        pass
    except FileNotFoundError:
        os.makedirs(root, 0o700, exist_ok=True)
        os.makedirs(day_dir, 0o700, exist_ok=True)
    path = os.path.join(day_dir, f"{kind}.jsonl")
    # O_NONBLOCK: opening a FIFO with no reader fails at once instead of blocking; a non-regular file at the path
    # (FIFO, socket, device) is refused and counted as a drop, never written to.
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.ENOTSUP, "events path is not a regular file")
        n = os.write(fd, data)
        if n != len(data):
            raise OSError(errno.EIO, "short write")   # a torn line: a FAILURE (the except in feature_event counts it)
        if summary is not None:
            try:
                if os.write(fd, summary) == len(summary):
                    _reset_drops(pending)
            except OSError:
                pass  # the count is kept: it will be summarised at the next successful emit
    finally:
        os.close(fd)
    return True


def _reset_drops(summarised: int) -> None:
    global _drops
    _drops = max(0, _drops - summarised)


# --- hook-point helpers (the hook sites call these, never feature_event directly) ----------------------


def hook_point_feature_id(name: Any) -> Optional[str]:
    """Closed-set lookup over :data:`HOOK_POINTS`; an unlisted name returns None."""
    try:
        return HOOK_POINTS.get(name) if isinstance(name, str) else None
    except Exception:  # noqa: BLE001
        return None


def emit_hook(name: str, outcome: str, *, actor: Optional[str] = None, kind: Optional[str] = None) -> bool:
    """One event for a hook point that ran at least one consumer (the normal hook event). ``kind``
    declares the process kind first (hook scripts pass ``hooks``). Never raises."""
    try:
        if kind is not None:
            set_process_kind(kind)
        fid = hook_point_feature_id(name)
        if fid is None:
            return False
        return feature_event(fid, actor, "hook", outcome=outcome)
    except BaseException:  # noqa: BLE001 - never raises (see feature_event)
        return False


def emit_hook_noop(name: str, *, actor: Optional[str] = None) -> bool:
    """One ``noop`` event for a hook point that resolved ZERO consumers. Written ONLY while
    ``hook_noop_events`` (and the emitter flag) are ON; otherwise nothing, and not a drop.
    Never raises."""
    try:
        if not noop_events_enabled():
            return False
        fid = hook_point_feature_id(name)
        if fid is None:
            return False
        return feature_event(fid, actor, "hook", outcome="noop")
    except BaseException:  # noqa: BLE001 - never raises (see feature_event)
        return False


def emit_hook_dispatch(name: str, consumers: int, outcome: str = "ok") -> bool:
    """The internal-registry form: ``consumers`` handlers ran => the normal hook event with
    ``outcome``; zero => the no-op event. Never raises."""
    try:
        if consumers and consumers > 0:
            return emit_hook(name, outcome)
        return emit_hook_noop(name)
    except BaseException:  # noqa: BLE001 - never raises (see feature_event)
        return False

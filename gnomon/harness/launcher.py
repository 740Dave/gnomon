"""The harness launcher — assembles + runs the PTY proxy for one session.

This is the INNER command the terminal template runs
(launch-command-shape): ``python -m gnomon.harness
<session_dir>``. It reads ``<session_dir>/.claude/launch.json`` (written by
:class:`~gnomon.launch.LaunchManager`) for the claude bin, bootstrap
prompt and hub port, spawns ``claude --dangerously-skip-permissions -- <prompt>``
under the PTY proxy, connects to the hub rendezvous, and pumps.

It BUILDS ON slice 1 — it imports the harness object graph and wires it; it does
not re-implement any of it. :func:`assemble_proxy` is separated from :func:`main`
so the object wiring is testable with a fake spawner and no real PTY / hub.
"""

from __future__ import annotations

import json
import math
import functools
import os
import signal
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .actuator import ORIGIN_HUB, Actuator
from .constants import (
    DEFAULT_BEAT_SECONDS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    HOSTED_CHILD_THRESHOLDS,
    merge_stall_inject,
)
from .context_readers import ProjectTranscriptContextReader, StatuslineHookContextReader
from .control import ControlChannel, LocalControlListener
from .hooks import (
    POINT_AGENT_END,
    POINT_AGENT_START,
    POINT_COMPACT,
    POINT_INBOUND,
    POINT_OPERATOR_COMMAND,
    POINT_OPERATOR_COMMAND_POST,
    HookPipeline,
    InjectionGate,
    LifecycleBus,
    ProvenanceLog,
    build_handlers,
    injection_log_path,
    validate_manifest,
)
from .injector import Injector
from . import plan_usage as _plan_usage_mod
from .monitor import ContextMonitor
from .proxy import HarnessProxy
from .spawn import (PtySpawner, SpawnResult, build_claude_args, sanitize_env, validate_env_passthrough,
                    validate_extra_args)
from .writer import SingleWriteSite

#: Re-Diamond N4 — the confirm-window ceiling margin below the self-MCP response
#: timeout. Before the confirmation wait even begins, ``Injector._emit_compact``
#: emits up to THREE ~0.2s beats (the ESC-ESC clear beat when clear=True, the beat
#: after the command token, and the pre-submit beat). If the confirm window were
#: allowed right up to the self-MCP response timeout (15s), those pre-wait pauses
#: could push the total past the socket timeout, so the agent would see a
#: "socket unreachable" transport error instead of the fail-loud ok:False+reason.
#: The window must therefore stay this far BELOW the response timeout:
#: 3 beats of pre-wait pause + a ~1s safety margin (~1.6s ⇒ ceiling ~13.4s).
CONFIRM_PRE_WAIT_MARGIN_SECONDS = 3 * DEFAULT_BEAT_SECONDS + 1.0

#: Files the harness writes into ``<session_dir>/.claude/`` at launch (fix-pass).
HARNESS_SETTINGS_FILENAME = "harness.settings.json"
HARNESS_MCP_FILENAME = "harness.mcp.json"
CONTEXT_STATE_FILENAME = "context_state.json"
SELF_MCP_SERVER_NAME = "gnomon-self"


#: the whole-delivery fix's flag (registry default ON; see flags.KNOWN_FLAGS).
_LONG_LINE_PASTE_FLAG = "inject_long_line_as_paste"
#: the modal-dialog hold's kill switch (registry default ON: a safety fix).
_MODAL_HOLD_FLAG = "modal_hold"
_DSD_SELF_VERBS_FLAG = "dsd_self_verbs"
#: the non-blocking master write + start hold (default OFF).
_MASTER_WRITE_NONBLOCKING_FLAG = "master_write_nonblocking"


def _x43_flag(h: Dict[str, Any], key: str, flag_name: str) -> bool:
    """A resolved X43 setting from the harness dict; when absent (a caller that did not
    run resolve_harness_settings, e.g. a test rig) the REGISTRY default, never a local one."""
    if key in h:
        return bool(h[key])
    from .. import flags as flags

    spec = flags.KNOWN_FLAGS.get(flag_name)
    return bool(spec.default) if spec is not None else False


def read_launch_config(session_dir: str) -> Dict[str, Any]:
    path = os.path.join(session_dir, ".claude", "launch.json")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError(f"launch.json is not an object: {path}")
    return cfg


def default_context_reader(
    session_dir: str,
    *,
    projects_root: Optional[str] = None,
    window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
    transcript_cwd: Optional[str] = None,
) -> Callable[[], Optional[float]]:
    """The D6 statusline-hook state file if present, else the transcript estimate.

    Both are plain callables sharing one interface (context_readers.py). The hook
    is authoritative; the transcript is an estimate against ``window_tokens``.

    fix-pass WS1 (2026-09-27): the fallback is REPOINTED from
    ``<session_dir>/.claude/transcript.jsonl`` (never written by Claude Code — the
    flight's null gauge) to the real transcript,
    ``<claude config>/projects/<mangled RESOLVED session_dir>/*.jsonl``. The
    child's cwd IS ``session_dir`` (``PtySpawner.spawn(cwd=session_dir)``).

    RESUME MODE (2026-09-28): the child is spawned in ``resume_cwd``, so its real
    transcript lives under ``<projects>/<mangled resume_cwd>/`` — pass that as
    ``transcript_cwd`` so the fallback estimate reads the RIGHT transcript. The
    statusline hook (written into the harness ``session_dir``) stays authoritative
    regardless; this only fixes the fallback. Defaults to ``session_dir`` when
    unset, so the fresh path is unchanged.
    """
    state_path = os.path.join(session_dir, ".claude", CONTEXT_STATE_FILENAME)
    hook = StatuslineHookContextReader(state_path)
    estimate = ProjectTranscriptContextReader(
        transcript_cwd or session_dir, window_tokens, projects_root=projects_root
    )

    def _read() -> Optional[float]:
        value = hook()
        return value if value is not None else estimate()

    return _read


# --- fix-pass: launch-time files + the child argv ---------------------------


def _write_json_file(path: str, obj: Dict[str, Any]) -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)
    return path


#: Interpreter flags every Gnomon hook / hook-like child command carries between the interpreter and ``-m``.
#: ``-P`` (Python 3.11+; this package requires 3.12+) is "do not prepend a potentially unsafe path to sys.path": with ``-m`` the
#: interpreter otherwise puts the WORKING DIRECTORY first on the import path, so a hook started in a directory that holds a
#: ``gnomon`` package (an old tree's root, a scratch clone's root, any folder an agent can write) would run THAT package
#: instead of the installed one (design-input-hook-import-follows-working-directory). A change here reaches a session
#: only when the launcher writes its settings: at its next launch or relaunch. It does not stop PYTHONPATH, .pth files or
#: sitecustomize; those are not the working directory.
HOOK_PYTHON_FLAGS = ("-P",)


#: PreCompact hook timeout (s) when it also re-renders the agent def (2026-09-29).
#: A single-corpus engine build + render measures in milliseconds; this is headroom
#: for a large corpus, and Claude Code awaits the hook before compacting.
AGENT_DEF_PRECOMPACT_TIMEOUT = 60


#: PostToolUse hook timeout (s): one bounded socket round trip (the hook's own
#: socket timeout is 1 s); fires after EVERY tool call, so it must stay cheap.
POSTTOOL_HOOK_TIMEOUT = 5


def posttool_hook_settings(python_bin: str, socket_path: str) -> Dict[str, Any]:
    """The PostToolUse hook (:mod:`.posttool_hook`): asks THIS proxy for any
    threshold notice currently HELD for the operator's line and returns it to
    the agent as ``hookSpecificOutput.additionalContext`` (contract verified live
    against Claude Code 2.1.284, 2026-09-29). FAIL OPEN at the shell too: even an
    interpreter/import failure prints nothing and exits 0."""
    from ..terminal import shell_join

    command = shell_join([python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.posttool_hook",
                          "--socket", socket_path]) + " 2>/dev/null; exit 0"
    return {"PostToolUse": [{"matcher": "", "hooks": [{"type": "command", "command": command,
                                                        "timeout": POSTTOOL_HOOK_TIMEOUT}]}]}


#: SubagentStop hook timeout (s): the hook bounds its own work at subagent_summary.WORK_BOUND_S (1.5 s).
SUBAGENT_STOP_HOOK_TIMEOUT = 10


def subagent_stop_hook_settings(python_bin: str, session_dir: str,
                                allow_roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """the SubagentStop hook (:mod:`.subagent_stop_hook`). Always installed with the harness hooks;
    what it DOES is gated at run time by ``subagent_summary_md`` (default OFF). Carries the session dir
    (so the write is confined to a verified session, not an inherited env) and any explicitly allowed
    SUMMARY-PATH roots. Takes effect only at a harness (re)launch: settings are read at claude start. FAIL OPEN
    at the shell too."""
    from ..terminal import shell_join

    argv = [python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.subagent_stop_hook", "--session-dir", session_dir]
    for r in allow_roots or []:
        argv += ["--allow-root", r]
    command = shell_join(argv) + " 2>/dev/null; exit 0"
    return {"SubagentStop": [{"matcher": "", "hooks": [{"type": "command", "command": command,
                                                         "timeout": SUBAGENT_STOP_HOOK_TIMEOUT}]}]}


def compact_hook_settings(python_bin: str, socket_path: Optional[str],
                          session_dir: Optional[str] = None) -> Dict[str, Any]:
    """PTY-hooks WS-E: the Claude-dialect compact signal as native hooks.

    ``PreCompact`` (any trigger) and ``SessionStart`` with matcher ``compact``
    run :mod:`.lifecycle_hook`, which reports to THIS proxy's self socket. Without
    a socket (``self_actuation: false``) the post-compact hook still adds its
    context line; the proxy just is not told (WS-C dual-idle remains the backstop).

    2026-09-29 (additive): with ``session_dir`` the hooks also get
    ``--session-dir``, so PreCompact RE-RENDERS the agent def (fresh ``{{lts: … }}``
    expansion) before Claude Code's post-compact CLAUDE.md reload, and post-compact
    reports the render status into the resumed context.
    """
    from ..terminal import shell_join

    def _cmd(event: str) -> Dict[str, Any]:
        argv = [python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.lifecycle_hook", "--event", event]
        if socket_path:
            argv += ["--socket", socket_path]
        timeout = 10
        if session_dir:
            argv += ["--session-dir", session_dir]
            if event == "pre-compact":
                timeout = AGENT_DEF_PRECOMPACT_TIMEOUT
        return {"type": "command", "command": shell_join(argv), "timeout": timeout}

    return {
        "PreCompact": [{"matcher": "", "hooks": [_cmd("pre-compact")]}],
        "SessionStart": [{"matcher": "compact", "hooks": [_cmd("post-compact")]}],
    }


#: UserPromptSubmit hook timeout (s). The hook round-trips ONE ``operator_command``
#: to the proxy (a ``\\memory`` write included); self_mcp.RESPONSE_TIMEOUT_SECONDS
#: (15 s) bounds the socket wait, this bounds the whole hook.
PROMPT_COMMAND_HOOK_TIMEOUT = 30


def _fallback_classifier_source() -> str:
    """A self-contained ``python -I -c`` program: exit 2 iff the hook-input JSON's
    ``prompt`` is an operator ``\\command`` by the hook's OWN rule (same regex +
    sigil, generated from :mod:`.prompt_command_hook`), else exit 0. Imports only
    ``json`` / ``re`` / ``sys`` — never gnomon (that import may be what broke)."""
    from .prompt_command_hook import SIGIL, _COMMAND_RE

    return ("import json,re,sys\n"
            "p=json.load(sys.stdin).get('prompt')\n"
            "l=p.lstrip() if isinstance(p,str) else ''\n"
            f"s={SIGIL!r}\n"
            f"c=l.startswith(s) and not l.startswith(s*2) and re.match({_COMMAND_RE.pattern!r},l)\n"
            "sys.exit(2 if c else 0)\n")


#: The fallback program (module constant so a test can prove it is gm-free).
PROMPT_HOOK_FALLBACK_CLASSIFIER = _fallback_classifier_source()

#: Shown to the operator when a \\command prompt is blocked by the fallback.
PROMPT_HOOK_FAIL_MESSAGE = (
    "[gm] operator command plane FAILED (the UserPromptSubmit hook crashed before it "
    "could run) - this command was BLOCKED and NOT sent to the agent. Fix the harness "
    "hook (see the error above), or relaunch. Ordinary prompts still pass through."
)


def _prompt_hook_command(python_bin: str, socket_path: str) -> str:
    """Audit F1, revised (coordinator 2026-09-29): fail closed ONLY for commands.

    The hook's own ``main()`` always exits 0, so a non-zero exit means it failed
    OUTSIDE ``main()`` (import error, bad interpreter, killed) — and Claude Code
    treats a non-zero, non-2 exit as non-blocking, which would LEAK a ``\\`` line
    to the model. But blocking EVERY prompt would turn one bad import into a
    fleet-wide outage (the harness's own notices arrive as prompts too). So:
    read stdin ONCE; feed it to the hook; on hook failure classify the prompt
    with a gm-free ``python -I -c`` (the hook's interpreter, then ``python3`` on
    PATH): a ``\\command`` -> exit 2 + message (block); anything else -> exit 0
    (pass through). If every classifier fails too -> exit 0: a double failure
    never blocks the fleet.
    """
    from ..terminal import shell_join
    import shlex

    hook = shell_join([python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.prompt_command_hook",
                       "--socket", socket_path])
    code = shlex.quote(PROMPT_HOOK_FALLBACK_CLASSIFIER)
    return (
        "gm_in=$(cat); "
        f"printf '%s' \"$gm_in\" | {hook} && exit 0; "
        f"for gm_py in {shlex.quote(python_bin)} python3; do "
        f"printf '%s' \"$gm_in\" | \"$gm_py\" -I -c {code} 2>/dev/null; gm_rc=$?; "
        f"if [ \"$gm_rc\" -eq 2 ]; then echo {shlex.quote(PROMPT_HOOK_FAIL_MESSAGE)} >&2; exit 2; fi; "
        "if [ \"$gm_rc\" -eq 0 ]; then exit 0; fi; "
        "done; exit 0"
    )


def prompt_command_hook_settings(python_bin: str, socket_path: str) -> Dict[str, Any]:
    """(2026-09-29): the ``\\command`` plane as
    a Claude Code ``UserPromptSubmit`` hook. Claude Code hands the hook the DECODED
    submitted prompt (after its own key handling), so kitty / modifyOtherKeys /
    mouse / focus byte encodings on stdin can no longer lose a command. The hook
    (:mod:`.prompt_command_hook`) sends a ``\\…`` line to THIS proxy's self socket
    and BLOCKS the prompt with the visible result; any other prompt passes untouched.
    """
    from ..terminal import shell_join

    # Audit F1 (revised): fail closed for \\command prompts ONLY — see
    # _prompt_hook_command.
    command = _prompt_hook_command(python_bin, socket_path)
    return {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": command,
                                             "timeout": PROMPT_COMMAND_HOOK_TIMEOUT}]}]}


#: Fallback permission mode when the operator's ~/.claude/settings.json cannot be
#: read (Group B, live demo 2026-09-28). "dontAsk" is the mode the operator's real
#: settings carry; it is what stops Claude Code's onboarding modal ("Make auto
#: mode your default permission mode?") from BLOCKING a --terminal none session.
DEFAULT_HARNESS_PERMISSIONS: Dict[str, Any] = {"defaultMode": "dontAsk"}


def resolve_harness_permissions(claude_settings_path: Optional[str] = None) -> Dict[str, Any]:
    """The ``permissions`` block to carry into the built harness.settings.json.

    Group B (live demo 2026-09-28): a pure-resume ``--terminal none`` session BLOCKS
    on Claude Code's auto-mode onboarding modal because the built harness.settings.json
    omitted the permission-mode config the operator's real ``~/.claude/settings.json``
    carries. This reads that file's ``permissions`` block (esp. ``defaultMode``) and
    carries it through the ``--settings`` file so the harnessed claude inherits the
    operator's mode and does not prompt. When the file is missing / unreadable / has no
    ``permissions`` dict, falls back to :data:`DEFAULT_HARNESS_PERMISSIONS`
    (``{"defaultMode": "dontAsk"}``). ``claude_settings_path`` is injectable for tests;
    default is ``<claude config dir>/settings.json``.
    """
    if claude_settings_path is None:
        from .context_readers import claude_config_dir

        claude_settings_path = os.path.join(claude_config_dir(), "settings.json")
    try:
        with open(claude_settings_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        perms = data.get("permissions")
        if isinstance(perms, dict) and perms:
            return dict(perms)
    except (OSError, ValueError, AttributeError):
        pass
    return dict(DEFAULT_HARNESS_PERMISSIONS)


#: Security posture D1/D2 (operator ruling 2026-09-29): the harness-owned
#: ``permissions.deny`` rules — CATASTROPHIC-ONLY. Deny beats allow across settings
#: sources and was probed to hold under --dangerously-skip-permissions. The shapes a
#: prefix rule cannot express (rm -rf / or ~, curl … | sh) are the deny_guard hook's.
HARNESS_DENY_RULES: Tuple[str, ...] = ("Bash(sudo:*)", "Bash(pkill:*)", "Bash(killall:*)")
DENY_GUARD_HOOK_TIMEOUT = 10


#: size cap of the launcher's read of the machine config (a regular file only; larger reads as the default, ON).
GUARD_CONFIG_MAX_BYTES = 1 << 20
#: the shell watchdog around the guard kills it after this many seconds. It MUST stay below DENY_GUARD_HOOK_TIMEOUT
#: (Claude Code is not relied on to block a timed-out hook) and above the guard's own in-process deadline (deny_guard.DEADLINE_S).
DENY_GUARD_WATCHDOG_SECONDS = 8
#: with ``guard_fail_closed`` ON a missing/non-executable interpreter (class interpreter) BLOCKS. By decision, an install whose interpreter is missing stops every command and says why, with the command to recover.
#: True is that decision; False would allow it after the log line (the old roadmap allowance). This is the ONLY place it is decided.
INTERPRETER_CLASS_BLOCKS_WHEN_ON = True


def read_guard_fail_closed(config_path: Optional[str] = None) -> bool:
    """the ``guard_fail_closed`` option as the LAUNCHER reads it, at session launch: a plain JSON read of the machine
    config's top-level key, with nothing from the guard or the rest of the harness. A missing key, a missing file, an unreadable
    file, a file that is not a JSON object, and ANY value that is not the boolean false read as True (the default). Never raises."""
    import errno
    import stat

    from .. import config as _config

    try:
        # The path is resolved by ConfigStore itself (never re-derived here), so the launcher reads the file the Config tab writes.
        # Agents are kept from writing that file by the deny guard's machine-config rule (gnomon/harness/deny_guard.py, the block
        # headed "the machine config file", tag CONFIG_TAG ``g71-config:``; checklist g71-amendment). Stated limit (g82 rule 5): that
        # rule stops an over-eager agent, not a deliberately adversarial one; the latter is on the hardening backlog, not covered here.
        path = config_path if config_path is not None else _config.ConfigStore().path
        # never block on a non-regular file at the config path (a FIFO with no writer): O_NONBLOCK open, then fstat before reading
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.ENOTSUP, "not a regular file")
            if st.st_size > GUARD_CONFIG_MAX_BYTES:
                raise OSError(errno.EFBIG, "config file over the size cap")
            fh = os.fdopen(fd, "r", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise
        with fh:
            data = json.loads(fh.read(GUARD_CONFIG_MAX_BYTES + 1))
        return not (isinstance(data, dict) and data.get("guard_fail_closed") is False)
    except Exception:  # noqa: BLE001 - anything that cannot be read as the boolean false is True: the safe default
        return True


#: (fix round 1, F4): identical consecutive failure lines of one session (the hook's parent process, ``$PPID``) are written at
#: most once per this many seconds; the line carries ``count=`` (this occurrence plus the ones suppressed since the previous written
#: line of the same kind). The state is one small file per session process in the guard's directory (``.startup-state-<ppid>``).
STARTUP_LOG_DEDUPE_SECONDS = 60

#: The shell text that appends ONE failure line (the failure's ``$rc`` and ``$cls`` are set). @G@ is the guard directory, @MODE@ the
#: literal mode. No Python, no config read; every variable is initialised and every number is checked before it is used in
#: arithmetic (a syntax error in ``$(( ))`` would abort a POSIX shell).
_LOG_SNIPPET = (
    'mkdir -p @G@ 2>/dev/null; now=$(date +%s); st=@G@/.startup-state-$PPID; lt=; lr=; lc=; lm=; sp=; '
    'if [ -r "$st" ]; then read lt lr lc lm sp < "$st"; fi; '
    'case "$lt" in \'\'|*[!0-9]*) lt=0;; esac; case "$sp" in \'\'|*[!0-9]*) sp=0;; esac; '
    'if [ "$lr $lc $lm" = "$rc $cls @MODE@" ] && [ $((now - lt)) -lt @WINDOW@ ]; then '
    'sp=$((sp + 1)); printf \'%s %s %s %s %s\\n\' "$lt" "$rc" "$cls" @MODE@ "$sp" > "$st" 2>/dev/null; '
    'else if [ "$lr $lc $lm" = "$rc $cls @MODE@" ]; then n=$((sp + 1)); else n=1; fi; '
    'printf \'%s rc=%s class=%s mode=@MODE@ count=%s\\n\' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$rc" "$cls" "$n" >> @G@/startup-failures.log 2>/dev/null; '
    'printf \'%s %s %s %s 0\\n\' "$now" "$rc" "$cls" @MODE@ > "$st" 2>/dev/null; fi; ')


def _log_snippet(gdir: str, mode: str) -> str:
    return _LOG_SNIPPET.replace("@G@", gdir).replace("@MODE@", mode).replace("@WINDOW@", str(STARTUP_LOG_DEDUPE_SECONDS))


def _fail_closed_tail(classify: str, gdir: str, log: str, py: str) -> str:
    """The ON tail: log the failure (mode=blocked), print the plain message for its class, exit 2. ``py`` is the shell-quoted
    interpreter path the hook names; the class-interpreter message carries the recovery command built from it."""
    return (classify + _log_snippet(gdir, "blocked") +
            "if [ \"$cls\" = interpreter ]; then printf 'SECURITY: the deny guard could not start, so this command is BLOCKED "
            "(class interpreter, status %%s: the Python interpreter named in the hook, %%s, is missing or cannot be run). Rescue: restore "
            "that interpreter, then check it from a terminal with the recovery command: %%s -m gnomon.harness.deny_guard --selftest "
            "(it must exit 0), then relaunch this session. Log: %%s. The setting guard_fail_closed exists; only the operator "
            "may change it.\\n' \"$rc\" %s %s %s >&2; " % (py, py, log) +
            "else printf 'SECURITY: the deny guard could not start, so this command is BLOCKED (class start-up, status %%s). "
            "Rescue: from a terminal run the guard self-test (python -m gnomon.harness.deny_guard --selftest) and restore the last good "
            "guard files. Log: %%s. The setting guard_fail_closed exists; only the operator may change it.\\n' \"$rc\" %s >&2; fi; exit 2"
            % log)


def deny_guard_hook_settings(python_bin: str, fail_closed: Optional[bool] = None,
                             watchdog_seconds: Optional[int] = None) -> Dict[str, Any]:
    """The PreToolUse catastrophic-command hook (:mod:`.deny_guard`). Its OWN block is exit 2. A status of 128 or more (the
    guard process was KILLED by a signal, e.g. a late SIGALRM, SIGKILL, SIGSEGV) also refuses, in BOTH settings, because a dead
    guard has checked nothing.

 every OTHER non-zero status is a START-UP failure (the guard could not run: an import or syntax error, a crash
    with an unexpected status, a missing or non-executable interpreter 126/127). What the line does with it is chosen HERE, at
    session LAUNCH, by the machine config's top-level ``guard_fail_closed`` (see :func:`read_guard_fail_closed`; ``fail_closed``
    overrides it for tests and tools), and written into the command as one of TWO FIXED tails, so deciding at hook time needs no
    working Python and no readable config:

    * ON (the default): the call is BLOCKED (exit 2) with a plain message on stderr naming the class, the status, the log and the
      rescue, and a ``class=`` / ``mode=blocked`` line is appended to ``startup-failures.log`` in the guard's runtime directory
      (``deny-guard/``, beside the machine config), which /api/security-warnings serves.
    * OFF: the call is ALLOWED after the same log line, with ``mode=allowed`` (the behaviour before X79).

    The guard runs under a SHELL WATCHDOG (``DENY_GUARD_WATCHDOG_SECONDS``, below the hook timeout; ``watchdog_seconds`` overrides it
    for tests): a guard still running at the bound is killed (SIGKILL, status 137), which blocks in BOTH settings. No Python decides it.

    Class ``interpreter`` is rc 126 or 127, together with a check made BEFORE the guard runs that the interpreter path can be
    executed (a bare name is looked up on PATH); every other start-up failure is class ``start-up``. A broken install is never
    inferred from a Python error. The line is written into harness.settings.json at LAUNCH, so a change here, or to the option,
    reaches a session only at its next launch/relaunch."""
    from ..terminal import shell_join

    import shlex
    from .. import config as _config

    if fail_closed is None:
        fail_closed = read_guard_fail_closed()
    # The directory the guard (deny_guard.guard_dir) and the dashboard read: <data root>/deny-guard, the data root from the
    # roots resolver AT LAUNCH. Under HOME it is written as "$HOME"/<relative> (so the line follows a different HOME at run
    # time, as before); elsewhere as the absolute path. A roots file that changes after launch moves the guard's own directory
    # but not this line's: the line's log is then found by the next launch's settings (stated limit).
    root = _config.runtime_root_default()
    home = os.path.expanduser("~")
    rel = os.path.relpath(root, home)
    gdir = ('"$HOME"/' + shlex.quote(rel + "/deny-guard")) if not (rel == ".." or rel.startswith("../") or os.path.isabs(rel)) \
        else shlex.quote(os.path.join(root, "deny-guard"))
    log = gdir + "/startup-failures.log"
    guard = shell_join([python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.deny_guard"])
    py = shlex.quote(python_bin)
    # The pre-run check: a path (it contains a separator) must be an executable non-directory; a bare name must be found on PATH.
    # Decided here from the interpreter string only, never from the environment. A failed check is rc 127 without running anything.
    exe_ok = ("command -v %s >/dev/null 2>&1" % py) if os.sep not in python_bin else ("[ -x %s ] && [ ! -d %s ]" % (py, py))
    wd = int(DENY_GUARD_WATCHDOG_SECONDS if watchdog_seconds is None else watchdog_seconds)
    # The guard runs in the background so a watchdog can kill it; an async command's stdin is /dev/null unless redirected, so the
    # hook's stdin (fd 0) is kept on fd 3 and handed to the guard explicitly. The watchdog's own descriptors are closed so a
    # lingering ``sleep`` can never hold the hook's pipes open.
    run = ("exec 3<&0; %s <&3 3<&- & gp=$!; ( sleep %d; kill -9 $gp ) >/dev/null 2>&1 <&- 3<&- & wp=$!; wait $gp; rc=$?; "
           "kill $wp 2>/dev/null; exec 3<&-" % (guard, wd))
    head = "if %s; then t0=$(date +%%s); %s; else rc=127; fi; " % (exe_ok, run) + \
        "[ \"$rc\" -eq 0 ] && exit 0; [ \"$rc\" -eq 2 ] && exit 2; "
    # A guard that was KILLED (status 128 or more) or outlived the watchdog checked nothing: blocked in BOTH settings, logged as
    # mode=blocked with class timeout (it ran for at least the watchdog bound) or killed (any other signal), with a message.
    killed = ("[ \"$rc\" -ge 128 ] && { if [ $(( $(date +%%s) - t0 )) -ge %d ]; then cls=timeout; else cls=killed; fi; " % wd +
              _log_snippet(gdir, "blocked") +
              "printf 'SECURITY: the deny guard did not finish (class %%s, status %%s: it was killed or ran past its time limit), so it "
              "checked nothing and this command is BLOCKED, whatever the setting guard_fail_closed says. Rescue: from a terminal run the "
              "guard self-test (python -m gnomon.harness.deny_guard --selftest) and restore the last good guard files. Log: %%s.\\n' "
              "\"$cls\" \"$rc\" %s >&2; exit 2; }; " % log)
    classify = "case \"$rc\" in 126|127) cls=interpreter;; *) cls=start-up;; esac; "
    if fail_closed and not INTERPRETER_CLASS_BLOCKS_WHEN_ON:
        # the switch point: class interpreter is allowed (logged, mode=allowed) even with the option ON
        tail = ("case \"$rc\" in 126|127) cls=interpreter; " + _log_snippet(gdir, "allowed") + "exit 0;; esac; ") + \
            _fail_closed_tail(classify, gdir, log, py)
    elif fail_closed:
        tail = _fail_closed_tail(classify, gdir, log, py)
    else:
        tail = classify + _log_snippet(gdir, "allowed") + "exit 0"
    # the ``>= 128`` test comes BEFORE the start-up handling in both tails (a killed guard blocks in both settings)
    command = head + killed + tail
    return {"PreToolUse": [{"matcher": "Bash|Monitor|Edit|Write|MultiEdit|NotebookEdit", "hooks": [{"type": "command", "command": command,
                                                                   "timeout": DENY_GUARD_HOOK_TIMEOUT}]}]}


def with_deny_rules(permissions: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """A COPY of ``permissions`` whose ``deny`` list also carries HARNESS_DENY_RULES
    (the operator's own deny/allow/defaultMode are preserved)."""
    out = dict(permissions or {})
    raw = out.get("deny") or []
    if isinstance(raw, str):  # a lone string rule — never split it into characters
        raw = [raw]
    deny = [r for r in raw if isinstance(r, str)] if isinstance(raw, (list, tuple)) else []
    out["deny"] = deny + [r for r in HARNESS_DENY_RULES if r not in deny]
    return out


HEADLESS_SETTINGS_FILENAME = "harness.headless.settings.json"


def write_deny_layer_settings(session_dir: str, python_bin: Optional[str] = None) -> str:
    """Audit fix pass 2: the deny layer ALONE (deny rules + deny_guard hook) for a
    child with no other harness settings — ``headless`` (``claude -p``). Raises on a
    write failure so the caller can refuse loud rather than run without it."""
    python_bin = python_bin or sys.executable or "python3"
    path = os.path.join(session_dir, ".claude", HEADLESS_SETTINGS_FILENAME)
    return _write_json_file(path, {"permissions": with_deny_rules(None),
                                   "hooks": deny_guard_hook_settings(python_bin)})


def deny_guard_selftest(python_bin: str, env: Optional[Dict[str, str]] = None,
                        cwd: Optional[str] = None, timeout: float = 15.0) -> Tuple[bool, str]:
    """Audit fix pass 2: the hook's shell tail maps an import failure to exit 0 (fail
    open), so a broken guard would be SILENT. Run ``--selftest`` with the child's
    python/env/cwd and report ``(ok, detail)``; never raises."""
    import subprocess

    try:
        p = subprocess.run([python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.deny_guard", "--selftest"],
                           env=env, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    if p.returncode == 0:
        return True, "ok"
    tail = (p.stderr or p.stdout or "").strip().splitlines()
    return False, f"exit {p.returncode}: {tail[-1] if tail else 'no output'}"[:300]


def write_harness_settings(
    session_dir: str,
    python_bin: Optional[str] = None,
    *,
    socket_path: Optional[str] = None,
    compact_hooks: bool = True,
    permissions: Optional[Dict[str, Any]] = None,
    claude_md_excludes: Optional[List[str]] = None,
    prompt_hook: bool = False,
    deny_layer: bool = False,
) -> str:
    """WS1a: install the D6 statusline hook for THIS child (``claude --settings``).

    Writes ``<session_dir>/.claude/harness.settings.json`` carrying a
    ``statusLine`` command that runs :mod:`.statusline_hook`, which writes
    ``used_percentage`` to ``<session_dir>/.claude/context_state.json`` and chains
    the user's own statusline for display. Passed via ``--settings`` so it
    applies to this child only and needs no change to ``settings.local.json``
    (the bypass pin file LaunchManager owns).

    PTY-hooks WS-E (2026-09-27, additive): also carries ``hooks`` —
    ``PreCompact`` + ``SessionStart(compact)`` (:func:`compact_hook_settings`).
    ``compact_hooks=False`` writes the statusLine-only file exactly as before.
    """
    from ..terminal import shell_join

    python_bin = python_bin or sys.executable or "python3"
    state_path = os.path.join(session_dir, ".claude", CONTEXT_STATE_FILENAME)
    command = shell_join([
        python_bin, *HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.statusline_hook",
        "--state", state_path, "--chain-user-statusline",
    ])
    path = os.path.join(session_dir, ".claude", HARNESS_SETTINGS_FILENAME)
    body: Dict[str, Any] = {"statusLine": {"type": "command", "command": command}}
    if compact_hooks:
        body["hooks"] = compact_hook_settings(python_bin, socket_path, session_dir=session_dir)
        # line-hold audit note 2 (2026-09-29): held compact-guidance/FYI reach the
        # agent as PostToolUse additionalContext — only with a self socket.
        if socket_path:
            body["hooks"].update(posttool_hook_settings(python_bin, socket_path))
        # the SubagentStop summary writer (flag-gated at run time; needs no socket).
        body["hooks"].update(subagent_stop_hook_settings(python_bin, session_dir))
    #: only with a self socket — the hook's
    # whole job is to reach THIS proxy; without one the byte-level tap keeps the
    # command plane (the fallback route).
    if prompt_hook and socket_path:
        body.setdefault("hooks", {}).update(prompt_command_hook_settings(python_bin, socket_path))
    # Group B (2026-09-28): merge the operator's permission mode into the
    # --settings file so a --terminal none session does not BLOCK on the auto-mode
    # onboarding modal. ADDITIVE: only written when a caller supplies it; None
    # (the default) leaves the file byte-identical to before, so the low-level
    # unit tests that call write_harness_settings directly are unchanged. The
    # launch path (prepare_child_launch) resolves + passes it.
    if permissions is not None:
        body["permissions"] = permissions
    # Agent-def RESUME (2026-09-29 remediation, Diamond #2): exclude the ORIGINAL
    # cwd's (externally provisioned, stale) CLAUDE.md so only the fresh --add-dir render
    # loads. Binary 2.1.284: `claudeMdExcludes` = absolute paths/globs, picomatch,
    # applied to User/Project/Local memory, realpath variants added by claude
    # itself; live `claude -p` probe: honored from a --settings file, incl. resume.
    # Only written when a caller supplies it -> fresh-mode file unchanged.
    if claude_md_excludes:
        body["claudeMdExcludes"] = list(claude_md_excludes)
    # Security posture D1/D2 (2026-09-29): the catastrophic deny layer — deny rules
    # merged into permissions + the deny_guard PreToolUse hook. Off by default so a
    # bare call is byte-identical; the launch path (prepare_child_launch) turns it on.
    if deny_layer:
        body["permissions"] = with_deny_rules(permissions)
        body.setdefault("hooks", {}).update(deny_guard_hook_settings(python_bin))
    return _write_json_file(path, body)


def write_self_mcp_config(
    session_dir: str,
    python_bin: Optional[str],
    socket_path: str,
    *,
    agent_launch: bool = False,
    quiesce: bool = False,
    search_transcript: bool = False,
    restart_service: bool = False,
    extra_servers: Optional[Dict[str, Dict[str, Any]]] = None,
    idle_snooze: bool = False,
    plan_usage: bool = False,
    alert: bool = False,
    sync_my_skills: bool = False,
    dsd_self_verbs: bool = False,
    work_verbs: bool = False,
    status_setter: bool = False,
    mail: bool = False,
) -> str:
    """WS2a: the child's self-actuation MCP server config (``claude --mcp-config``).

    ONE server, ``gnomon-self`` (stdio), pointed at this proxy's private
    control socket. No env, no token — authority is being in this proxy's tree.
    ``agent_launch`` (PTY-hooks WS-G, off by default) adds ``--agent-launch`` so
    the server advertises the ``launch_agent`` verb.
    """
    python_bin = python_bin or sys.executable or "python3"
    path = os.path.join(session_dir, ".claude", HARNESS_MCP_FILENAME)
    args = [*HOOK_PYTHON_FLAGS, "-m", "gnomon.harness.self_mcp", "--socket", socket_path]
    if agent_launch:
        args.append("--agent-launch")
    if quiesce:  # advertise the quiesce verb (quiescent_state on)
        args.append("--quiesce")
    if search_transcript:  # advertise search_transcript (search_transcript_verb on)
        args.append("--search-transcript")
    if restart_service:  # flag ON AND the verb policy does not deny the identity (resolve_harness_settings)
        args.append("--restart-service")
    if idle_snooze:  # advertise idle_snooze (idle_snooze on at launch)
        args.append("--idle-snooze")
    if plan_usage:  # s1: advertise plan_usage (plan_usage_verb on)
        args.append("--plan-usage")
    if alert:  # advertise alert (alerts on at launch)
        args.append("--alert")
    if sync_my_skills:  # advertise sync_my_skills (sync_my_skills on at launch)
        args.append("--sync-my-skills")
    if mail:  # advertise the four mail verbs (agent_mail on at launch)
        args.append("--mail")
    if dsd_self_verbs:  # pre-review B1: advertise send_dsd / my_dsds / withdraw_dsd / replace_dsd (dsd_self_verbs on)
        args.append("--dsd-self-verbs")
    if work_verbs:  # advertise the five work verbs (open_work_list on at launch)
        args.append("--work-verbs")
    if status_setter:  # advertise operator_status_set (this session is in the allowed-setter list)
        args.append("--status-setter")
    servers: Dict[str, Any] = {SELF_MCP_SERVER_NAME: {
        "type": "stdio",
        "command": python_bin,
        "args": args,
    }}
    # session_mcp_from_profile: the profile's resolved servers ride in the SAME --mcp-config file.
    # None/empty (flag off) -> the file is byte-identical to before. The self server can never be replaced.
    for _name, _entry in (extra_servers or {}).items():
        if _name != SELF_MCP_SERVER_NAME:
            servers[_name] = dict(_entry)
    return _write_json_file(path, {"mcpServers": servers})


# --- PTY-hooks: harness settings resolution ----------------------------------

#: The per-session hand-placed hook manifest (wins over launch.json / config).
HOOKS_MANIFEST_FILENAME = "hooks.json"


def resolve_harness_settings(
    cfg: Dict[str, Any],
    session_dir: str,
    *,
    machine_config: Optional[Any] = None,
    profile_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Resolve the PTY-hooks settings for one launch. Fails LOUD (ValueError).

    Precedence for each: ``launch.json`` key (a new LaunchManager writes these,
    already merged) > the machine config (``~/gnomon-data/config.json``, via
    ``machine_config``) + the profile's per-instance override > built-in
    defaults. The config/profile fallback is what makes these apply on the NEXT
    launch even when the running dashboard predates this build (its
    LaunchManager does not write the keys yet).

    Returns ``{stall, hook_manifest, user_name, operator_plane, agent_depth,
    agent_launch_enabled, agent_launch_caps, launched_by}``.
    """
    from .agent_launch import DEFAULT_MAX_CHILDREN, DEFAULT_MAX_DEPTH
    from .opcommands import resolve_user_name

    mc = machine_config
    if "stall_inject" in cfg and cfg["stall_inject"] is not None:
        stall = merge_stall_inject(None, cfg["stall_inject"])
    else:
        stall = merge_stall_inject(getattr(mc, "stall_inject", None), profile_override)

    manifest_path = os.path.join(session_dir, ".claude", HOOKS_MANIFEST_FILENAME)
    if os.path.isfile(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    elif cfg.get("hook_manifest") is not None:
        manifest = cfg["hook_manifest"]
    else:
        manifest = getattr(mc, "hook_manifest", None) or {}
    manifest = validate_manifest(manifest)

    user_name = resolve_user_name(cfg.get("user_name") or getattr(mc, "user_name", None))
    plane = cfg.get("operator_command_plane", getattr(mc, "operator_command_plane", True))
    if not isinstance(plane, bool):
        raise ValueError("operator_command_plane must be a boolean")
    depth = cfg.get("agent_depth", 0)
    if isinstance(depth, bool) or not isinstance(depth, int) or depth < 0:
        raise ValueError("launch.json 'agent_depth' must be a non-negative integer")
    enabled = cfg.get("agent_launch_enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("launch.json 'agent_launch_enabled' must be a boolean")
    caps_in = cfg.get("agent_launch") or {}
    caps = {"max_depth": DEFAULT_MAX_DEPTH, "max_children": DEFAULT_MAX_CHILDREN}
    for key in caps:
        if key in caps_in:
            v = caps_in[key]
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValueError(f"launch.json agent_launch.{key} must be a non-negative integer")
            # Caps may only be TIGHTENED from launch.json, never raised above
            # the interim rail (DELTA-3) — raising is the operator's decision.
            caps[key] = min(v, caps[key])
    accept_trust = cfg.get("accept_folder_trust", False)
    if not isinstance(accept_trust, bool):
        raise ValueError("launch.json 'accept_folder_trust' must be a boolean")
    # the quiescent stall state, via the flag helper — the
    # launch config's ``flags`` dict is the profile override, then the machine
    # config's ``flags``, then the registry default (OFF).
    from .. import flags as flags
    from .quiescence import FLAG_NAME as _QUIESCENT_FLAG

    quiescent_state = flags.flag(_QUIESCENT_FLAG, profile=cfg, config=mc)
    # the search_transcript self-verb (registry default OFF; read here ONCE).
    from .transcript_search import FLAG_NAME as _SEARCH_TRANSCRIPT_FLAG

    search_transcript = flags.flag(_SEARCH_TRANSCRIPT_FLAG, profile=cfg, config=mc)
    # Hub-MCP wiring fix: deliver the profile's mcp_servers via --mcp-config (registry default OFF).
    session_mcp_from_profile = flags.flag("session_mcp_from_profile", profile=cfg, config=mc)
    # slice 1: the plan_usage self-verb (registry default OFF; read here ONCE).
    from .plan_usage import FLAG_NAME as _PLAN_USAGE_FLAG

    plan_usage_verb = flags.flag(_PLAN_USAGE_FLAG, profile=cfg, config=mc)
    # (core): the alert service (registry default OFF; read here ONCE). ON: the ``alert`` self-verb is
    # advertised, and the stuck-hold watchdog raises an alert. The store lives under the machine's DATA root.
    from .. import alerts as _alerts

    alerts_on = flags.flag(_alerts.FLAG_NAME, profile=cfg, config=mc)
    alerts_dir = _alerts.default_dir(getattr(mc, "runtime_root", None)) if alerts_on else None
    # the sync_my_skills self-verb (registry default OFF; read here ONCE).
    from ..skills.verb import FLAG_NAME as _SYNC_MY_SKILLS_FLAG

    sync_my_skills = flags.flag(_SYNC_MY_SKILLS_FLAG, profile=cfg, config=mc)
    # the agent mail verbs (registry default OFF; read here ONCE).
    from ..mail import FLAG_NAME as _AGENT_MAIL_FLAG

    agent_mail = flags.flag(_AGENT_MAIL_FLAG, profile=cfg, config=mc)
    # the restart_service self-verb. OFFERED iff the flag is ON AND the central verb
    # policy (harness/verb_policy.toml, or the machine-config override) does not DENY this session's
    # identity (launch.json instance_id) every registered service: confirm-twice keeps it VISIBLE
    # to everyone, only `deny` hides it. A bad registry or policy disables the verb loudly (g27b f)
    # and never breaks the launch. Flag OFF: neither file is even read.
    from .. import restart_service as _rs
    from .. import service_registry as _sr
    from . import verb_policy as _vp

    restart_service = False
    restart_service_errors: List[str] = []
    policy_narrowing: List[str] = []
    if flags.flag(_rs.FLAG_NAME, profile=cfg, config=mc):
        _reg = _sr.load_registry(getattr(mc, "services", None))
        _pol = _vp.load_policy(getattr(mc, "runtime_root", None))
        restart_service_errors = list(_reg.errors) + list(_pol.errors)
        policy_narrowing = list(_pol.narrowing)
        restart_service = bool(not _reg.disabled and not _pol.invalid and _vp.visible_targets(
            _pol, _rs.TOOL_NAME, cfg.get("instance_id"), sorted(_reg.entries)))
    # the idle_snooze self-verb (registry default OFF; read here ONCE). The inbox
    # and dsd directories the question lists live under the machine's agents root.
    from .idle_snooze import FLAG_NAME as _IDLE_SNOOZE_FLAG

    idle_snooze = flags.flag(_IDLE_SNOOZE_FLAG, profile=cfg, config=mc)
    try:
        intelligences_root = mc.intelligences_root()
    except Exception:  # noqa: BLE001 - no machine config: the shipped default location
        from ..profile import default_intelligences_root

        intelligences_root = default_intelligences_root()
    # the effect ledger (registry default OFF; read here ONCE, no local default).
    from .effect_ledger import FLAG_NAME as _EFFECT_LEDGER_FLAG

    effect_ledger = flags.flag(_EFFECT_LEDGER_FLAG, profile=cfg, config=mc)
    # 2026-09-29: DSD review activity in the stall nudge (registry default ON).
    dsd_review_presence = flags.flag("dsd_review_presence", profile=cfg, config=mc)
    # app activity in the idle message (registry default OFF; read here ONCE; the key is added below only when ON).
    app_activity_presence = flags.flag("app_activity_presence", profile=cfg, config=mc)
    # the post-compact boot prompt (flag default OFF). ONE resolution of
    # flag + per-role text, shared by the SessionStart:compact context leg (via the
    # boot file) and the PTY leg (assemble_proxy). Flag off: nothing else is read.
    from . import post_compact_boot as _pcb

    post_compact_boot = _pcb.resolve_for_launch(
        cfg, mc, session_dir, flags.flag(_pcb.FLAG_NAME, profile=cfg, config=mc))
    # whole-delivery fix + full operator-message copy + delivery check (registry
    # defaults; read here ONCE, no local default).
    from . import operator_messages as _om

    long_line_paste = flags.flag(_LONG_LINE_PASTE_FLAG, profile=cfg, config=mc)
    operator_messages_log = flags.flag(_om.FLAG_LOG, profile=cfg, config=mc)
    operator_delivery_check = flags.flag(_om.FLAG_DELIVERY_CHECK, profile=cfg, config=mc)
    resolved = {
        "modal_hold": flags.flag(_MODAL_HOLD_FLAG, profile=cfg, config=mc),
        "stall": stall, "hook_manifest": manifest, "user_name": user_name, "operator_plane": plane,
        "agent_depth": depth, "agent_launch_enabled": enabled, "agent_launch_caps": caps,
        "launched_by": cfg.get("launched_by"), "accept_folder_trust": accept_trust,
        "quiescent_state": quiescent_state, "dsd_review_presence": dsd_review_presence,
        "effect_ledger": effect_ledger,
        "post_compact_boot": post_compact_boot,
        "search_transcript": search_transcript,
        "session_mcp_from_profile": session_mcp_from_profile,
        "plan_usage_verb": plan_usage_verb,
        "alerts": alerts_on, "alerts_dir": alerts_dir,
        "sync_my_skills": sync_my_skills,
        "agent_mail": agent_mail,
        "restart_service": restart_service,
        "idle_snooze": idle_snooze, "intelligences_root": intelligences_root,
        "restart_service_errors": restart_service_errors,
        "policy_narrowing": policy_narrowing,
        "inject_long_line_as_paste": long_line_paste,
        "operator_messages_log": operator_messages_log,
        "operator_messages_dir": _om.default_dir(getattr(mc, "runtime_root", None)) if operator_messages_log else None,
        "operator_delivery_check": operator_delivery_check,
    }
    # pre-review B1: the DSD self-verbs. Read from the MACHINE config only (never from launch.json, which the session can write);
    # the key is present ONLY when ON, so with the flag OFF this dict is exactly what it was.
    if flags.flag(_DSD_SELF_VERBS_FLAG, config=mc):
        resolved["dsd_self_verbs"] = True
    # master_write_nonblocking: machine config only (never launch.json, which the session can write); the key is present ONLY when ON, so
    # with the flag OFF this dict is exactly what it was (assemble_proxy reads it with a False default).
    if flags.flag(_MASTER_WRITE_NONBLOCKING_FLAG, config=mc):
        resolved[_MASTER_WRITE_NONBLOCKING_FLAG] = True
    # the waiting-on-operator alert. Machine config only (never launch.json, which the session can write); the key is
    # present ONLY when ON, so with the flag OFF this dict is exactly what it was.
    if flags.flag("waiting_on_operator_alert", config=mc):
        resolved["waiting_on_operator_alert"] = True
    # machine config only (never launch.json, which the session can write); the key is present ONLY when ON, so with the flag
    # OFF this dict is exactly what it was. ON also retires idle_snooze (rule-removed): the verb is not advertised.
    if flags.flag("open_work_list", config=mc):
        try:
            from ..worklist import launch as _wl

            resolved["open_work_list"] = _wl.resolve(cfg, mc, session_dir, user_name)
            resolved["idle_snooze"] = False
        except Exception as exc:  # noqa: BLE001 - a fault here must never stop a launch: this launch runs as with the flag OFF, loudly
            sys.stderr.write(f"gnomon harness: ⚠ open-work list NOT enabled for this launch: {type(exc).__name__}: {exc}\n")
    # same rule: the key is present ONLY when the flag is ON (assemble_proxy reads it with a False default), so with the
    # flag OFF the dict is exactly what it was before X72.
    if app_activity_presence:
        resolved["app_activity_presence"] = True
    return resolved


def _report_restart_service_config(session_dir: str, harness: Dict[str, Any]) -> None:
    """(g27b f): a registry entry rejected by validation (flag ON only) is LOUD at
    launch: one stderr line and one provenance record per fault naming the field. Never
    refuses the launch; a launch with a bad registry simply does not get the verb."""
    for note in harness.get("policy_narrowing") or []:   # F-R2-5: an override that takes access away is never silent
        sys.stderr.write(f"gnomon harness: ⚠ verb policy override narrows access: {note}\n")
        try:
            from .hooks import append_record, injection_log_path

            append_record(injection_log_path(session_dir), {
                "kind": "restart-service", "source": "harness", "trigger": "restart-service",
                "decision": "policy-narrowed", "loud": True, "note": str(note)[:400]})
        except Exception:  # noqa: BLE001
            pass
    errors = harness.get("restart_service_errors") or []
    for err in errors:
        sys.stderr.write(f"gnomon harness: ⚠ restart_service registry: {err}\n")
        try:
            from .hooks import append_record, injection_log_path

            append_record(injection_log_path(session_dir), {
                "kind": "restart-service", "source": "harness", "trigger": "restart-service",
                "decision": "config-invalid", "loud": True, "field": str(err).split(":")[0][:120],
                "error": str(err)[:400]})
        except Exception:  # noqa: BLE001
            pass


def _sync_post_compact_boot_file(session_dir: str, harness: Dict[str, Any]) -> None:
    """write (flag on) or REMOVE (flag off) the SessionStart:compact hook's
    boot-text file. A failure is LOUD (stderr + a provenance row) but never refuses
    the launch: the PTY leg still works from the same resolution."""
    from . import post_compact_boot as _pcb

    boot = harness.get("post_compact_boot") or {"enabled": False}
    if boot.get("config_error"):
        # F9 ruling: a malformed config with the flag ON disables the feature for this
        # session — one loud stderr line + a provenance record naming the bad field —
        # and the launch (and the resume nudge) proceed normally.
        err = str(boot["config_error"])
        sys.stderr.write(f"gnomon harness: ⚠ post-compact boot DISABLED for this session — "
                         f"bad config: {err}\n")
        try:
            from .hooks import append_record, injection_log_path

            append_record(injection_log_path(session_dir), {
                "kind": _pcb.RECORD_KIND, "decision": "config-invalid", "loud": True,
                "field": err.split(" must ")[0].replace("post_compact_boot ", "", 1), "error": err})
        except Exception:  # noqa: BLE001
            pass
    try:
        path = _pcb.sync_boot_file(session_dir, boot)
    except OSError as exc:
        sys.stderr.write(f"gnomon harness: ⚠ post-compact boot file NOT written ({exc}); the "
                         "SessionStart:compact context leg will not carry the boot text.\n")
        try:
            from .hooks import append_record, injection_log_path

            append_record(injection_log_path(session_dir), {
                "kind": _pcb.RECORD_KIND, "decision": "file-write-failed", "loud": True,
                "error": f"{type(exc).__name__}: {exc}"})
        except Exception:  # noqa: BLE001
            pass
        return
    if path:
        try:
            from .hooks import append_record, injection_log_path

            append_record(injection_log_path(session_dir), {
                "kind": _pcb.RECORD_KIND, "decision": "configured", "role": boot.get("role"),
                "text_source": boot.get("text_source"), "trigger": _pcb.TRIGGER})
        except Exception:  # noqa: BLE001
            pass


def resolve_wish_target(instance_id: Optional[str]) -> Tuple[Optional[str], Optional[str]]:
    """follow-up (WISHPASS, 2026-09-30): the near-finish nudge's wish
    corpus + id prefix for THIS instance — ``("<instance_id>-personality", "<P>")``
    with ``<P>`` resolved exactly as the ``memory`` writer resolves it (profile
    ``personality_prefix``, else the lifelog's ``<P>-LIFELOG`` root), so the nudge
    queries the same ``<P>-WISH-*`` ids the writer mints. Unknown instance, no
    lifelog, ambiguous prefix or ANY error -> ``(None, None)``, i.e. the monitor's
    generic placeholders. Never raises: a launch must not fail over a nudge."""
    if not instance_id:
        return None, None
    try:
        from .. import memory as _memory

        target = _memory.resolve_target(str(instance_id))
        prefix = getattr(target, "prefix", None)
        if not isinstance(prefix, str) or not prefix:
            return None, None
        return f"{instance_id}-personality", prefix
    except Exception:  # noqa: BLE001 — fall back to the generic text, never crash
        return None, None


def _load_machine_context(instance_id: str) -> "tuple[Optional[Any], Optional[Dict[str, Any]]]":
    """(machine config, per-instance stall override) — best effort, never raises."""
    try:
        from ..config import ConfigStore
        from ..profile import ProfileStore

        mc = ConfigStore().load()
    except Exception:  # noqa: BLE001
        return None, None
    override = None
    try:
        prof = ProfileStore(mc.intelligences_root()).read(instance_id)
        override = getattr(prof, "stall_inject", None)
    except Exception:  # noqa: BLE001
        override = None
    return mc, override


#: A claude session id is a UUID (``~/.claude/projects/<mangled-cwd>/<uuid>.jsonl``).
_RESUME_ID_RE = __import__("re").compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)


def validate_resume_session_id(resume_id: Any) -> str:
    """Validate a resume session id (L1, fix-pass 2026-09-28). Fails LOUD (ValueError).

    Rejects: a non-string; a value that starts with ``-`` (a ``--``-leading id
    would be spliced into the child argv as a flag, pushing ``--mcp-config`` into
    the prompt slot); a value that is not UUID-shaped (claude resolves the session
    by a UUID file name, so a non-UUID can never name a real transcript). Returns
    the id unchanged on success so callers can use it inline.
    """
    if not isinstance(resume_id, str):
        raise ValueError(
            f"resume_session_id must be a string, got {type(resume_id).__name__} {resume_id!r}"
        )
    if resume_id.startswith("-"):
        raise ValueError(
            f"resume_session_id must not start with '-' (got {resume_id!r}) — a dash-leading "
            "value would be read as a CLI flag and corrupt the child argv"
        )
    if not _RESUME_ID_RE.match(resume_id):
        raise ValueError(
            f"resume_session_id must be UUID-shaped (got {resume_id!r}) — claude resolves the "
            "session by a UUID transcript file name (~/.claude/projects/<mangled-cwd>/<uuid>.jsonl)"
        )
    return resume_id


def build_child_argv(
    cfg: Dict[str, Any],
    session_dir: str,
    *,
    settings_path: Optional[str],
    mcp_config_path: Optional[str],
    extra_mcp_config_paths: Optional[List[str]] = None,
) -> List[str]:
    """The child's argv: harness flags + the profile's ``extra_args`` (WS4).

 ``extra_mcp_config_paths``: further ``--mcp-config`` files appended after
    ``mcp_config_path`` in the same variadic flag (the 0600 caller-credential config). None = today's argv.

    ``<claude> --dangerously-skip-permissions [--settings S] <extra_args...>
    [--mcp-config M] -- <bootstrap prompt>``. ``extra_args`` is validated (list
    of non-empty strings, no bare ``--``) and fails LOUD.

    ORDER IS LOAD-BEARING: ``--mcp-config <configs...>`` is VARIADIC in Claude's
    CLI, so it goes LAST, where ``--`` terminates it — otherwise a bare value at
    the head of ``extra_args`` would be silently swallowed as another MCP config.
    ``extra_args`` is an ALLOWLIST (D15 fix pass 2, ``spawn.EXTRA_ARGS_ALLOWLIST``:
    --model, --effort, --name, --verbose, --debug, and the transcript planner's
    session-selection flags --continue/-c, --resume/-r, --fork-session,
    --session-id). Everything else — --settings / --setting-sources (the harness
    owns the child's settings) and every permission/tool/MCP/dir/plugin/prompt
    flag — is refused by :func:`validate_extra_args`, in both ``--flag`` and
    ``--flag=value`` forms, as is a stray positional value.

    ⚠ ``--strict-mcp-config`` is deliberately NOT added: the harness mcp-config
    is ADDITIVE (the child keeps the MCP surface it has today + gains the self
    verbs). Honoring the session ``.mcp.json`` strictly is deferred — see the
    fix-pass SUMMARY (WS4): its catalog entries are not runnable on this machine.
    A profile can NO LONGER opt in via ``extra_args`` (escalation-flag rule); that
    would now be a harness-config change.

    RESUME MODE (session adoption, 2026-09-28): when ``cfg['resume_session_id']``
    is a non-empty string, restore that EXISTING claude session instead of
    cold-starting::

        <claude> --dangerously-skip-permissions [--settings S] [extra...] \
                 --resume <id> [--mcp-config M] [-- <resume_prompt>]

    The cold-start ``-- <bootstrap_prompt>`` append is OMITTED (a resumed session
    already carries its context). ``--mcp-config`` stays LAST among the flag args
    (the variadic invariant above) so ``--resume``'s value can't be swallowed and
    a bare ``extra_args`` value can't be read as a config. If ``resume_prompt`` is
    non-empty it is appended after ``--`` as a gentle re-orient turn. The
    fresh-session path (no ``resume_session_id``) is UNCHANGED.
    """
    extra = validate_extra_args(cfg.get("extra_args"))
    resume_id = cfg.get("resume_session_id")
    args: List[str] = []
    if settings_path:
        args += ["--settings", settings_path]
    # --model passthrough (2026-09-29): LaunchManager writes cfg['model'] from the
    # profile but nothing read it. Passed in BOTH modes when set; an operator
    # --model in extra_args wins (never passed twice). Absent -> argv unchanged.
    model = cfg.get("model")
    if model is not None and model != "":
        if not isinstance(model, str) or model.startswith("-"):
            raise ValueError(f"launch.json 'model' must be a model-name string, got {model!r}")
        if not any(a == "--model" or a.startswith("--model=") for a in extra):
            args += ["--model", model]
    args += extra
    if resume_id is not None and resume_id != "":
        # -- RESUME MODE ------------------------------------------------------
        if not isinstance(resume_id, str):
            raise ValueError(
                f"resume_session_id must be a string, got {type(resume_id).__name__} {resume_id!r}"
            )
        # L1 (fix-pass 2026-09-28): reject a dash-leading id at the argv chokepoint
        # too (defense in depth alongside validate_resume_session_id) — a "--"-leading
        # value here would be spliced in as a flag and push --mcp-config off the tail.
        if resume_id.startswith("-"):
            raise ValueError(
                f"resume_session_id must not start with '-' (got {resume_id!r}) — it would "
                "be read as a CLI flag and corrupt the child argv"
            )
        # Transcript follow-up (2026-09-29): a resume + --fork-session gets a NEW
        # id, and claude accepts --session-id alongside --fork-session, so pass the
        # launcher's pre-decided id (cfg['session_id'], set by main() only when
        # plan_transcript says inject). A PLAIN resume (no fork) NEVER gets
        # --session-id — claude exits 1 on it. Operator --session-id wins.
        from .transcript import has_fork_session

        sid = cfg.get("session_id")
        if (sid and has_fork_session(extra)
                and not any(a == "--session-id" or a.startswith("--session-id=") for a in extra)):
            args += ["--session-id", validate_resume_session_id(sid)]
        # Agent-def delivery (2026-09-29): the resumed child runs in the ORIGINAL
        # cwd, whose .claude/CLAUDE.md is externally provisioned + stale. --add-dir
        # <session_dir> (+ CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1 in the env,
        # see agent_def_child_env) makes claude load the harness-rendered
        # <session_dir>/.claude/CLAUDE.md too — without touching that file.
        # Set by main() at spawn; absent -> argv byte-identical to before. Placed
        # immediately before --resume so the variadic --add-dir is terminated by it.
        add_dir = cfg.get("agent_def_add_dir")
        if add_dir:
            if not isinstance(add_dir, str) or add_dir.startswith("-"):
                raise ValueError(f"agent_def_add_dir must be a path string, got {add_dir!r}")
            args += ["--add-dir", os.path.abspath(os.path.expanduser(add_dir))]
        args += ["--resume", resume_id]
        if mcp_config_path:
            args += ["--mcp-config", mcp_config_path, *(extra_mcp_config_paths or [])]  # variadic: keep LAST
        argv: List[str] = [cfg.get("claude_bin", "claude"), "--dangerously-skip-permissions"]
        argv += args
        resume_prompt = cfg.get("resume_prompt") or ""
        if resume_prompt:
            # A gentle re-orient turn — delivered as argv after "--", never typed,
            # exactly like the cold-start bootstrap it replaces.
            argv += ["--", resume_prompt]
        return argv
    # -- FRESH cold-start path ------------------------------------------------
    # Transcript STOP-THE-LINE (2026-09-29, additive): when the launcher has
    # pre-decided the claude session id (cfg['session_id'], set by main() from
    # transcript.plan_transcript), pass it as --session-id so the transcript path
    # is KNOWN at launch. Absent key -> argv byte-identical to before. An operator
    # --session-id in extra_args wins (never passed twice).
    sid = cfg.get("session_id")
    if sid and not any(a == "--session-id" or a.startswith("--session-id=") for a in extra):
        args += ["--session-id", validate_resume_session_id(sid)]
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path, *(extra_mcp_config_paths or [])]  # variadic: keep LAST
    return build_claude_args(
        cfg.get("claude_bin", "claude"),
        cfg.get("bootstrap_prompt", ""),
        extra_args=args,
    )


def resolve_child_cwd(cfg: Dict[str, Any], session_dir: str) -> str:
    """The cwd the CHILD claude is spawned in.

    Fresh mode: the harness ``session_dir`` (unchanged — ``PtySpawner.spawn`` has
    always used it). RESUME MODE: the ORIGINAL session's ``resume_cwd``, because
    ``claude --resume <id>`` resolves the session id WITHIN the current project
    dir (``~/.claude/projects/<mangled-cwd>/<id>.jsonl``) — spawning in the
    harness ``session_dir`` would make the id unresolvable. The harness
    ``session_dir`` stays harness-owned (launch.json, injection_log); only the
    child's cwd moves. Fails LOUD when a resume id is set without a ``resume_cwd``
    rather than silently resolving nothing.
    """
    resume_id = cfg.get("resume_session_id")
    if resume_id is None or resume_id == "":
        return session_dir
    resume_cwd = cfg.get("resume_cwd")
    if not isinstance(resume_cwd, str) or not resume_cwd:
        raise ValueError(
            "resume mode (resume_session_id set) requires a non-empty string 'resume_cwd' — "
            "claude --resume resolves the session id within the project dir "
            "(~/.claude/projects/<mangled-cwd>/<id>.jsonl)"
        )
    return os.path.expanduser(resume_cwd)


def _is_resume(cfg: Dict[str, Any]) -> bool:
    rid = cfg.get("resume_session_id")
    return rid is not None and rid != ""


def agent_def_child_env(cfg: Dict[str, Any]) -> Dict[str, str]:
    """Env overrides that make the child load the --add-dir CLAUDE.md (2026-09-29).

    Binary-verified (claude 2.1.284): memory loading iterates the --add-dir list
    ONLY when ``CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD`` is truthy; without it
    the add-dir CLAUDE.md is ignored (live probe: control run did not see it).
    Emitted only when :func:`build_child_argv` will pass ``--add-dir`` (resume mode
    with ``agent_def_add_dir`` set), so the fresh-mode env is unchanged.
    """
    from ..agent_def import ADDITIONAL_DIRS_CLAUDE_MD_ENV

    if _is_resume(cfg) and cfg.get("agent_def_add_dir"):
        return {ADDITIONAL_DIRS_CLAUDE_MD_ENV: "1"}
    return {}


def resume_claude_md_excludes(cfg: Dict[str, Any]) -> List[str]:
    """The ``claudeMdExcludes`` for a RESUME launch: the ORIGINAL cwd's CLAUDE.md
    files (as given AND realpath-resolved — resume cwds are often symlinks).
    Empty in fresh mode or when no ``agent_def_add_dir`` delivery is armed (without
    the fresh render we must not hide the only agent def the child has)."""
    from ..agent_def import superseded_paths

    if not (_is_resume(cfg) and cfg.get("agent_def_add_dir")):
        return []
    out: List[str] = []
    for p in superseded_paths(cfg.get("resume_cwd")):
        for v in (p, os.path.realpath(p)):
            if v not in out:
                out.append(v)
    return out


def render_agent_def_at_spawn(cfg: Dict[str, Any], session_dir: str, *,
                              stderr: Any = None, same_dir_fix: bool = False) -> Dict[str, Any]:
    """Re-render the agent def FRESH immediately before spawning claude (2026-09-29).

    Reads ``agent_def_source`` from launch.json (LaunchManager records it). Writes a
    NEW ``agent-def/NNN-spawn-<UTC>.md`` artifact + the delivered
    ``.claude/CLAUDE.md`` and records it (launch.json + provenance). RESUME mode also
    sets ``cfg['agent_def_add_dir'] = session_dir`` so :func:`build_child_argv` /
    :func:`agent_def_child_env` deliver that file into the ORIGINAL cwd's session.

    Never blocks the launch: a missing source (a launch.json from a pre-2026-09-29
    dashboard) is recorded LOUDLY and the existing CLAUDE.md is left in place; an
    expansion failure is already a banner inside the file; an unexpected error is
    printed to the operator's terminal and recorded.
    """
    from ..agent_def import merge_launch_json, record_provenance, render_agent_def, superseded_paths

    err = stderr if stderr is not None else sys.stderr
    session_dir = os.path.abspath(os.path.expanduser(session_dir))
    resume = _is_resume(cfg)
    mode = "resume" if resume else "fresh"
    # relaunch_derives_resume_keys (fix part 1). HOW THE
    # DEFINITION REACHES THE AGENT, per case:
    #  * DIFFERENT dir (resume_cwd != session_dir; every adopted session): the child runs in resume_cwd, whose own
    #    .claude/CLAUDE.md is stale, so --add-dir <session_dir> (+ CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1)
    #    delivers the freshly rendered <session_dir>/.claude/CLAUDE.md and claudeMdExcludes hides the stale one.
    #  * SAME dir (a resumed fresh launch: realpath(resume_cwd) == realpath(session_dir)): the child's cwd IS the session
    #    dir, so <session_dir>/.claude/CLAUDE.md, the file rendered just above, IS the project instructions file Claude
    #    Code loads at start. Nothing else is needed, and the exclude would name that very file (whether claudeMdExcludes
    #    also filters --add-dir content is UNPROVEN), so NEITHER the exclude NOR the add-dir is emitted.
    #    Only the real resumed boot proves it: the agent reports its definition's render-header timestamp.
    same_dir = False
    # B4 (pre-review batch): a session whose launch.json ALREADY records ``agent_def_delivery.same_dir: true`` (written by an
    # earlier start with the flag ON) keeps that delivery mode at this start whatever the flag says now. Without this, a
    # flag-OFF start over the derived keys builds ``claudeMdExcludes`` naming the delivered file itself and the session
    # comes up with NO definition (rehearsal phase D5). No such record = ``recorded_same_dir`` False = every line as before.
    _deliv = cfg.get("agent_def_delivery")
    recorded_same_dir = isinstance(_deliv, dict) and _deliv.get("same_dir") is True
    if (same_dir_fix or recorded_same_dir) and resume:
        # By FILE IDENTITY (st_dev, st_ino), never by path spelling: a case variant of the session dir on a
        # case-insensitive volume is the SAME directory. If either side cannot be stat'ed this RAISES (an OSError:
        # main() reports "launch refused" and spawns nothing) rather than guess. This applies at any flag value to a session
        # that RECORDS same_dir; a session with no record is judged only when the flag is ON.
        from ..agent_def import is_same_dir

        same_dir = is_same_dir(cfg.get("resume_cwd"), session_dir)
    if resume:
        if same_dir:
            cfg.pop("agent_def_add_dir", None)   # never a stale add-dir: no excludes follow from its absence
        else:
            cfg["agent_def_add_dir"] = session_dir
    delivery = {
        "mode": mode,
        "delivered_file": os.path.join(session_dir, ".claude", "CLAUDE.md"),
        "mechanism": ("--add-dir <session_dir> + CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1 "
                      "+ claudeMdExcludes(original cwd CLAUDE.md) in harness.settings.json"
                      if resume and not same_dir else
                      "resume_cwd == session_dir: child cwd = session_dir, the delivered file IS the project "
                      "CLAUDE.md (no --add-dir, no claudeMdExcludes)" if same_dir else
                      "child cwd = session_dir (project CLAUDE.md)"),
        "claude_md_excludes": resume_claude_md_excludes(cfg),
    }
    if same_dir:
        delivery["same_dir"] = True    # read by the PreCompact re-render (lifecycle_hook.rerender_agent_def)
    source = cfg.get("agent_def_source")
    try:
        if not source:
            rec = {"event": "spawn", "mode": mode, "status": "no-source-recorded", "path": None,
                   "reason": "launch.json carries no agent_def_source (pre-2026-09-29 launch?) — "
                             "agent def NOT re-rendered; the existing .claude/CLAUDE.md is delivered as-is"}
            err.write(f"gnomon harness: ⚠ agent def NOT re-rendered at spawn: {rec['reason']}\n")
            record_provenance(session_dir, rec)
        else:
            rec = render_agent_def(session_dir, source, "spawn", mode=mode,
                                   superseded=(superseded_paths(cfg.get("resume_cwd"))
                                               if resume and not same_dir else None))
            if rec.get("status") not in ("ok", "none"):   # ANY non-ok status warns (a new one can never be silent)
                why = rec.get("reason") or "; ".join(
                    f"{f.get('query')}: {f.get('reason')}" for f in rec.get("failures") or [])
                err.write(f"gnomon harness: ⚠ agent-def render {rec['status']} at spawn — {why}\n")
    except Exception as exc:  # noqa: BLE001 — never block the launch, never silent
        rec = {"event": "spawn", "mode": mode, "status": "error",
               "reason": f"{type(exc).__name__}: {exc}"}
        err.write(f"gnomon harness: ⚠ agent-def render ERROR at spawn — {rec['reason']}\n")
        record_provenance(session_dir, rec)
    try:
        merge_launch_json(session_dir, {"agent_def_delivery": delivery})
    except (OSError, ValueError):
        pass
    if recorded_same_dir:
        # A record that points at a definition which is not there (missing, empty, a directory, a dangling link, unreadable) is a
        # LOUD STOP at EVERY start of that session, the relaunch flag ON, OFF or absent (main() reports "launch refused" and
        # spawns nothing), never a start with no definition. A session with no record is untouched.
        from ..agent_def import _has_usable_def

        delivered = delivery["delivered_file"]
        if not _has_usable_def(delivered):
            raise ValueError(
                f"launch.json records agent_def_delivery.same_dir but the agent definition file {delivered} is missing "
                f"or empty, so this session would start with NO definition. Not starting it. Re-render it (start the "
                f"session once with relaunch_derives_resume_keys ON) or restore that file, then start again")
    return rec


def build_agent_start_payload(
    cfg: Dict[str, Any],
    harness: Dict[str, Any],
    *,
    instance_id: str,
    session_dir: str,
    pid: int,
    hub_registered: bool,
) -> Dict[str, Any]:
    """The WS-E agent-start payload fired once post-spawn.

    RESUME MODE (2026-09-28): ``resumed`` is present in ALL modes (``False`` for a
    fresh cold-start) so a hook can rely on the key; ``resumed_session_id`` names
    the adopted session in resume mode (``None`` otherwise). A resumed launch fired
    NO cold-start bootstrap (the argv carried no ``-- <bootstrap>``), so the row is
    the provenance record that instrumentation attached to an already-warm session.
    """
    resume_id = cfg.get("resume_session_id") or None
    return {
        "instance_id": instance_id, "session_dir": session_dir, "pid": pid,
        "agent_depth": harness["agent_depth"], "agent_launch_enabled": harness["agent_launch_enabled"],
        "launched_by": harness["launched_by"], "hub_registered": hub_registered,
        "stall": {k: harness["stall"][k] for k in ("enabled", "agent_idle_seconds", "operator_idle_seconds")},
        "hook_points": sorted(harness["hook_manifest"]),
        "resumed": bool(resume_id),
        "resumed_session_id": resume_id,
    }


def validate_confirm_compact_config(cfg: Dict[str, Any]) -> Tuple[bool, Optional[float]]:
    """Validate the Part-3 confirm-compact launch.json keys. Fails LOUD (ValueError).

    Returns ``(confirm_compact_requested, confirm_compact_timeout)``. The returned
    ``requested`` flag is the RAW opt-in from ``confirm_compact`` — ⭐ DEFAULT
    ``False`` (Architect design call 2026-09-28: OFF unless launch.json opts in;
    the pre-fix code defaulted this ON — see the remediation SUMMARY, flagged
    behaviour change). The caller ANDs it with "a self socket exists" (the
    PreCompact signal has no route otherwise). This is called BEFORE the child is
    spawned / any socket is bound (audit F3b) so a bad value fails loud with a
    clean message instead of a post-spawn traceback.

    Audit findings covered:
    * F3a — ``confirm_compact_timeout`` must be FINITE, ``> 0`` and strictly below
      the self-MCP response timeout. Python's ``json`` parses bare ``NaN`` /
      ``Infinity``, and ``float()`` accepts them, so an un-validated value makes
      the poll never end. Refused here with a clear message.
    * F3c — ``confirm_compact`` must be a real boolean. The pre-fix
      ``bool(... and cfg.get("confirm_compact", True))`` treated the STRING
      ``"false"`` as truthy-on; a typed check refuses it.
    """
    from .self_mcp import RESPONSE_TIMEOUT_SECONDS as _SELF_MCP_RESPONSE_TIMEOUT

    raw_on = cfg.get("confirm_compact", False)
    if not isinstance(raw_on, bool):
        raise ValueError(
            "launch.json 'confirm_compact' must be a boolean (true/false), got "
            f"{type(raw_on).__name__} {raw_on!r} — note the JSON string \"false\" is NOT false"
        )

    timeout = cfg.get("confirm_compact_timeout")
    if timeout is not None:
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(
                "launch.json 'confirm_compact_timeout' must be a number, got "
                f"{type(timeout).__name__} {timeout!r}"
            )
        timeout = float(timeout)
        if not math.isfinite(timeout):
            raise ValueError(
                "launch.json 'confirm_compact_timeout' must be a finite number "
                "(NaN / Infinity are refused — they make the confirmation poll never end)"
            )
        if timeout <= 0:
            raise ValueError(
                f"launch.json 'confirm_compact_timeout' must be > 0 (got {timeout})"
            )
        # Re-Diamond N4: keep a margin below the self-MCP response timeout for the
        # pre-wait beats + a ~1s cushion (CONFIRM_PRE_WAIT_MARGIN_SECONDS), not just
        # strictly-below. Otherwise ~14.4s of window + ~0.6s of pre-wait pauses can
        # outlast the 15s socket timeout and the agent sees a transport error
        # instead of the fail-loud ok:False+reason.
        ceiling = _SELF_MCP_RESPONSE_TIMEOUT - CONFIRM_PRE_WAIT_MARGIN_SECONDS
        if timeout > ceiling:
            raise ValueError(
                f"launch.json 'confirm_compact_timeout' ({timeout}s) must be <= {ceiling}s — "
                f"the self-MCP response timeout ({_SELF_MCP_RESPONSE_TIMEOUT}s) minus the "
                f"pre-wait pause budget ({CONFIRM_PRE_WAIT_MARGIN_SECONDS}s: 3 injector beats "
                "+ ~1s margin) — or the tool call can time out before the confirmer does"
            )
    return bool(raw_on), timeout


def assemble_proxy(
    spawn_result: SpawnResult,
    *,
    read_context: Optional[Callable[[], Optional[float]]] = None,
    hub_connection: Optional[Any] = None,
    stdin_fd: Optional[int] = 0,
    thresholds: Optional[Sequence[int]] = None,
    local_control: Optional[LocalControlListener] = None,
    session_dir: Optional[str] = None,
    instance_id: Optional[str] = None,
    harness: Optional[Dict[str, Any]] = None,
    stdout_fd: int = 1,
    hub_send: Optional[Callable[[Dict[str, Any]], None]] = None,
    clock: Optional[Callable[[], float]] = None,
    confirm_compact: bool = False,
    confirm_compact_timeout: Optional[float] = None,
    hub_link: Optional[Any] = None,
) -> HarnessProxy:
    """Wire the slice-1 object graph into a runnable :class:`HarnessProxy`.

    D15 amendment (2026-09-29): ``hub_link`` (a :class:`~gnomon.harness.
    hublink.HubLink`) supersedes the one-shot ``hub_connection``: it reconnects +
    re-registers for the harness's whole life, carries status/fan-out UP, and a
    :class:`~gnomon.harness.notice_inbox.DsdNoticeInbox` (``proxy.
    dsd_notices``) holds + delivers + acks queued DSD-answer notices. The legacy
    ``hub_connection`` path is unchanged.

    * ``SingleWriteSite`` over the child's master fd (the ONE write site);
    * ``Injector`` over it (the 5-phase submit sequence);
    * ``ContextMonitor`` reading ``read_context`` (reports up + injects locally),
      armed at ``thresholds`` — the config-driven context thresholds carried in
      ``launch.json`` (final-audit MEDIUM). ``None`` / empty falls back to
      ``HOSTED_CHILD_THRESHOLDS`` (90/95), so an unconfigured launch is unchanged;
    * if a ``hub_connection`` is given, an ``Actuator`` + ``ControlChannel`` bound
      to that socket so the hub can drive injects DOWN and the monitor reports
      status UP.

    PTY-hooks (2026-09-27) — ALWAYS wired (defaults = no content hooks):
    * WS-A: an :class:`InjectionGate` on the writer (the choke point) with the
      manifest's ``inbound-injection`` pipeline and the provenance log at
      ``<session_dir>/.claude/injection_log.jsonl`` (in-memory only when no
      ``session_dir``);
    * WS-B: an operator input tap on stdin (when ``stdin_fd`` is set) with the
      standard backslash commands (the plane can be disabled by config);
    * WS-C: the dual-idle stall trigger on the monitor (``harness["stall"]``);
    * WS-E: a :class:`LifecycleBus` (``proxy.lifecycle``) — agent-end on exit,
      compact pre/post from the native hooks over the self socket;
    * WS-G: a :class:`LaunchLedger` answering the self socket's launch verbs.

    ``harness`` is :func:`resolve_harness_settings`'s dict; ``None`` = defaults.
    """
    from .agent_launch import LaunchLedger
    from .constants import DEFAULT_STALL_INJECT
    from .opcommands import install_standard_commands, resolve_user_name
    from .hooks import SOURCE_OPERATOR
    from .optap import SURFACE_PROMPT_HOOK, OperatorInputTap

    h = dict(harness or {})
    manifest = h.get("hook_manifest") or {}
    stall = h.get("stall") or dict(DEFAULT_STALL_INJECT)
    instance_id = instance_id or (os.path.basename(session_dir) if session_dir else None)

    log = ProvenanceLog(injection_log_path(session_dir) if session_dir else None)
    gate = InjectionGate(
        HookPipeline(build_handlers(manifest, POINT_INBOUND)), log,
        command_pipeline=HookPipeline(build_handlers(manifest, POINT_OPERATOR_COMMAND)),
        command_post_pipeline=HookPipeline(build_handlers(manifest, POINT_OPERATOR_COMMAND_POST)),
        session_dir=session_dir, instance_id=instance_id,
        effect_ledger=bool(h.get("effect_ledger", False)),
    )
    writer = SingleWriteSite(master_fd=spawn_result.master_fd, gate=gate,
                             nonblocking=bool(h.get(_MASTER_WRITE_NONBLOCKING_FLAG, False)))
    # the ONE hold. Every non-operator injection reaches the PTY through
    # gate.decide, which DEFERs it while this guard reports an open AskUserQuestion / ExitPlanMode
    # (transcript) or a freshly painted dialog (PTY screen bridge, incl. tool-permission prompts).
    from .modal_prompt import ModalGuard as _ModalGuard

    from .modal_prompt import CLOSE_MARGIN_SECONDS as _CLOSE_MARGIN

    modal_on = _x43_flag(h, "modal_hold", _MODAL_HOLD_FLAG)  # kill switch, default ON; OFF = no hold at all
    # (flag waiting_on_operator_alert, machine config only; the key is present in ``h`` ONLY when ON): the alert for a
    # permission prompt that waits on the operator. It needs modal_hold (that is what sees the prompt close) and alerts (that is
    # what delivers). With the key absent nothing is imported and the guard is built exactly as before.
    waiting_hooks = None
    if h.get("waiting_on_operator_alert") and modal_on:
        from . import waiting_alert as _wa

        waiting_hooks = _wa.make_hooks(
            instance_id=instance_id, session_dir=session_dir,
            alerts_dir=h.get("alerts_dir") if h.get("alerts") else None,
            record=lambda what, **f: gate.record("modal-guard", what=what, **f),
            typed_here=lambda: op_ctx.typed_here_seconds())     # op_ctx is built further down; read late
    modal_guard = _ModalGuard(
        session_dir, on_event=lambda what, **f: gate.record("modal-guard", what=what, **f),
        close_margin=float(h.get("modal_close_margin_seconds", _CLOSE_MARGIN)), waiting=waiting_hooks)
    if modal_on:
        gate.modal_check = modal_guard.check
        gate.modal_note_write = modal_guard.note_write
        gate.modal_dismiss = modal_guard.dismiss
        # a release re-checks the operator's line like a fresh injection (g6 / g27); `monitor` binds late.
        gate.line_hold_check = lambda: monitor.line_hold_reason()

        def _on_modal_stuck(info: Dict[str, Any]) -> None:
            t = tap_ref.get("tap") or tap_ref.get("registry")
            if t is not None:
                t.feedback("HOLD STUCK: " + str(info.get("label")))
            # (alerts): ALSO raise an alert for the human. UI only: the alert never reaches
            # a PTY, so it cannot itself become a held injection. Once per stuck episode (the caller raises
            # this once per episode), de-duplicated per instance by the alert key.
            if h.get("alerts") and h.get("alerts_dir"):
                from .. import alerts as _alerts

                # F4: handed to a bounded worker queue (drop-and-count on overflow) so the pump thread can never
                # block on alert I/O; F3: a failure there is a log line + a counter, never silent.
                _alerts.submit_producer(
                    "stuck-hold",
                    "harness:" + str(instance_id or "unknown"), "warning",
                    f"Held text is stuck in {instance_id or 'a session'}",
                    str(info.get("label") or "harness text is held behind a dialog"),
                    "intelligences", key="stuck-hold:" + str(instance_id or ""),
                    store=_alerts.AlertStore(h["alerts_dir"]))

        gate.on_modal_stuck = _on_modal_stuck

        def _on_broadcast_held(info: Dict[str, Any]) -> None:
            # g79: the operator's own broadcast / DM waits like any other harness write; tell him so
            t = tap_ref.get("tap") or tap_ref.get("registry")
            if t is not None:
                frm = f" from {info.get('from')}" if info.get("from") else ""
                t.feedback(f"HELD: an operator message{frm} is WAITING and was NOT typed over your line "
                           f"({info.get('why')}). It is delivered when your line is empty "
                           "(Enter submits your own draft first, then it is delivered; or Ctrl-U to clear the line); "
                           "\\approve --into-dialog to force, \\reject to drop.")
            # round 3 P2-2: tell the SENDER too (the hub's "delivered to N" only means "sent"): one
            # delivery_report(verdict=held) -> the hub routes "held at M (operator line busy)" to the sender.
            up = hub_send
            if up is not None and isinstance(info.get("msg_id"), str) and isinstance(info.get("from"), str):
                try:
                    up({"type": "delivery_report", "msg_id": info["msg_id"], "from": info["from"],
                        "verdict": "held", "why": "dialog" if info.get("kind") == "dialog" else "line"})
                except Exception:  # noqa: BLE001 - best-effort; the recipient's own screen line stands
                    pass

        gate.on_operator_broadcast_held = _on_broadcast_held
    # a long single-line injection is typed as a bracketed paste (flag, default ON).
    from . import operator_messages as _om
    from .constants import LONG_LINE_PASTE_BYTES

    injector = Injector(writer, session_dir=session_dir,
                        long_line_paste_bytes=(LONG_LINE_PASTE_BYTES if _x43_flag(
                            h, "inject_long_line_as_paste", _LONG_LINE_PASTE_FLAG) else None))
    # requirement 2: the full-copy operator-messages log (flag, default ON).
    op_msg_log: Optional[_om.OperatorMessageLog] = None
    if _x43_flag(h, "operator_messages_log", _om.FLAG_LOG):
        op_msg_log = _om.OperatorMessageLog(h.get("operator_messages_dir") or _om.default_dir(),
                                            lock_timeout=_om.TYPING_PATH_LOCK_TIMEOUT)
    lifecycle = LifecycleBus(manifest, gate)

    def _on_report(pct: float) -> None:
        up = hub_link if hub_link is not None else hub_connection
        if up is not None:
            try:
                up.send_status(used_percentage=pct, alive=True)
            except OSError:
                pass

    tap_ref: Dict[str, Any] = {}
    if op_msg_log is not None:
        # round 2 (Diamond F5, g65(c)): a full-copy write failure is NEVER
        # silent: a loud provenance record, one stderr line, and an operator-visible line.
        def _on_opmsg_error(err: str) -> None:
            gate.record("operator-messages-log", decision="write-failed", loud=True, error=err,
                        path=op_msg_log.path)
            try:
                sys.stderr.write(f"gnomon harness: operator-messages log write FAILED ({err}); "
                                 "the full text of that message was NOT saved\n")
            except Exception:  # noqa: BLE001
                pass
            t = tap_ref.get("tap")
            if t is not None:
                t.feedback(f"WARNING: the full copy of your message was NOT saved ({err}).")

        op_msg_log.on_error = _on_opmsg_error
    plane_enabled = bool(h.get("operator_plane", True))
    #: when the UserPromptSubmit hook owns the
    # \\command plane, the byte-level tap does NOT intercept \\ lines (they reach
    # Claude Code, whose hook dispatches them) — one line, one dispatcher.
    prompt_hook = bool(h.get("prompt_command_hook"))

    def _on_operator_command(line: str) -> Dict[str, Any]:
        plane = tap_ref.get("registry")
        if plane is None or not plane_enabled:
            gate.record("operator-command", source=SOURCE_OPERATOR, surface=SURFACE_PROMPT_HOOK,
                        decision="refused", reason="operator command plane disabled for this session")
            return {"ok": False, "decision": "disabled",
                    "reply": "the operator command plane is disabled for this session (config "
                             "operator_command_plane: false) — NOT sent to the agent"}
        return plane.execute_command(line.strip(), surface=SURFACE_PROMPT_HOOK)

    def _on_escalate(info: Dict[str, Any]) -> None:
        gate.record("stall", decision="escalate", reason="still idle after the one resume inject",
                    **{k: v for k, v in info.items() if k != "kind"})
        tap = tap_ref.get("tap")
        if tap is not None:
            tap.feedback("STALL: agent still idle after the resume nudge — needs a human look "
                         "(no further automatic inject).")

    def _on_stall_hold(info: Dict[str, Any]) -> None:
        gate.record("stall", decision="held", reason=info.get("reason"))

    # every quiescent transition is provenance (the dashboard's
    # /api/sessions ``quiescent`` field is read back from these records).
    def _on_quiescent(info: Dict[str, Any]) -> None:
        gate.record("quiescent", **info)

    # 2026-09-29 stall-hold: ONE unknown-line window for every harness-originated
    # injection (DSD notices, stall/resume nudge, compact-guidance, FYI).
    unknown_line_after = float(h.get("dsd_notice_unknown_line_after_seconds", 300.0))

    def _operator_line_state() -> str:
        # no tap (a stdin-less session) = no operator line = empty
        tap = tap_ref.get("tap")
        return tap.line_state if tap is not None else "empty"

    # 2026-09-29 cross-harness operator activity (harness/operator_activity.py):
    # this harness publishes an activity beacon (when it has an operator stdin)
    # and READS its peers', so the stall nudge can say "talking to <agent>" vs "away".
    # No hub dependency — works for every launch type.
    from . import operator_activity as _oa
    from . import quiescence as _q

    op_beacon: Optional[_oa.ActivityBeacon] = None
    if session_dir and stdin_fd is not None:
        op_beacon = _oa.ActivityBeacon(instance_id or "", session_dir, line_state=_operator_line_state)
        op_beacon.start()
        if op_beacon.last_error:
            gate.record("operator-activity", decision="beacon-unwritable", reason=op_beacon.last_error)
    op_presence = (_oa.PresenceSource(stall["presence_source"])
                   if stall.get("presence_source") else None)
    op_ctx = _oa.OperatorContext(
        session_dir=session_dir or "", user_name=h.get("user_name") or resolve_user_name(None),
        beacon=op_beacon, presence=op_presence, instance_id=instance_id or None,
        active_window=float(stall.get("active_window_seconds") or _oa.DEFAULT_ACTIVE_WINDOW),
        # dsd_review_presence, resolved in resolve_harness_settings; absent = OFF.
        dsd_review=bool(h.get("dsd_review_presence", False)),
        # app_activity_presence, resolved in resolve_harness_settings; absent = OFF.
        app_activity=bool(h.get("app_activity_presence", False)),
    )
    context_aware = (bool(stall.get("context_aware", True))
                     and stall.get("content") == DEFAULT_STALL_INJECT["content"])

    def _compose_stall_text(local_idle: Optional[float]) -> Optional[str]:
        text, branch = op_ctx.compose(stall["content"], local_idle=local_idle)
        gate.record("stall-text", decision="composed", branch=branch)
        return text

    stall_on = bool(stall.get("enabled", True))
    _wish_corpus, _wish_prefix = resolve_wish_target(instance_id)
    # (flag idle_snooze, resolved in resolve_harness_settings; absent = OFF = the
    # verb is refused 'disabled' and the monitor never consults it). Every tap / refusal / confirm /
    # clear is provenance (/api/sessions ``idle_snooze`` is read back from the records).
    from .idle_snooze import IdleSnooze

    _snooze_inbox = _snooze_dsd = None
    try:
        from ..profile import _validate_instance_id

        if instance_id:
            _validate_instance_id(instance_id)
            _root = h.get("intelligences_root")
            if _root:
                _snooze_inbox = os.path.join(_root, instance_id, "inbox")
                _snooze_dsd = os.path.join(_root, instance_id, "dsd")
    except Exception:  # noqa: BLE001 - an odd instance id: the question just cannot list open work
        _snooze_inbox = _snooze_dsd = None
    def _on_idle_snooze(**info: Any) -> None:
        gate.record("idle-snooze", **info)

    idle_snooze = IdleSnooze(
        enabled=bool(h.get("idle_snooze", False)), record=_on_idle_snooze,
        operator_latest=op_ctx.latest_operator_activity,
        inbox_dir=_snooze_inbox, dsd_dir=_snooze_dsd,
        wish_hint=(f"your wish list: lts_find(corpus=\"{_wish_corpus}\", prefix=\"{_wish_prefix}-WISH\")"
                   if _wish_corpus and _wish_prefix else None),
    )
    monitor = ContextMonitor(
        read_context=read_context if read_context is not None else (lambda: None),
        thresholds=thresholds if thresholds else HOSTED_CHILD_THRESHOLDS,
        on_report=_on_report,
        heartbeat_interval=float(stall["agent_idle_seconds"]) if stall_on else None,
        operator_idle_interval=float(stall["operator_idle_seconds"]) if stall_on else None,
        heartbeat_text=stall["content"],
        stall_escalate_after=stall.get("escalate_after_seconds"),
        on_escalate=_on_escalate,
        rearm_grace=float(stall.get("rearm_grace_seconds") or 0.0),
        stall_backoff_max=int(stall.get("backoff_max") or 8),
        post_compact_interval=stall.get("post_compact_idle_seconds"),
        # measured 2026-09-27: never fire before the REPL is up (a startup
        # dialog would be answered by the CR).
        require_ready=True,
        # 2026-09-29 stall-hold: never nudge onto the operator's unsent line.
        line_state=_operator_line_state,
        unknown_line_after=unknown_line_after,
        on_hold=_on_stall_hold,
        heartbeat_text_fn=_compose_stall_text if context_aware else None,
        idle_reply_since=(_oa.transcript_idle_reply_reader(session_dir) if session_dir else None),
        idle_backoff_cap=float(stall.get("idle_backoff_cap_seconds") or _oa.DEFAULT_IDLE_BACKOFF_CAP),
        # (flag quiescent_state, resolved in resolve_harness_settings;
        # absent = OFF = the monitor behaves exactly as before).
        quiescence_enabled=bool(h.get("quiescent_state", False)),
        agent_turn_since=(_q.transcript_agent_turn_reader(session_dir) if session_dir else None),
        on_quiescent=_on_quiescent,
        # follow-up (WISHPASS): the instance's real wish corpus + prefix
        # (None, None = the generic placeholders).
        wish_corpus=_wish_corpus,
        wish_prefix=_wish_prefix,
        idle_snooze=idle_snooze,
    )
    # master_write_nonblocking (ON only): no harness injection before the child's first output. The monitor's line-hold rule carries
    # it (so the notice path, the stall nudge and the gate's hold all see it); when the modal hold is switched off the gate has no line
    # check of its own, so it gets the start hold alone. Operator keystrokes never pass through either.
    if writer.nonblocking:
        monitor.startup_hold = writer.startup_hold_reason
        gate.start_hold_cause = writer.start_hold_release_cause
        if gate.line_hold_check is None:
            gate.line_hold_check = writer.startup_hold_reason
    if op_presence is not None:
        op_presence.set_on_change(lambda _present: monitor.note_presence_change())
    # (open_work_list; the key is present in the resolved settings ONLY when ON, so flag OFF runs none of this and
    # imports nothing from worklist/). ON: the recurring idle/stall nudge is retired (only the post-compact resume check stays,
    # with a fixed text), list reminders are typed by work.tick, the presence line rides on every harness wake-up.
    work = None
    _ow = h.get("open_work_list")
    if _ow and instance_id:
        from ..worklist.launch import launch_check as _owl_launch_check
        from ..worklist.runtime import PRESENCE_SOURCES as _owl_sources, WorkRuntime as _WorkRuntime

        try:
            work = _WorkRuntime(instance_id=instance_id, data_root=_ow["data_root"], clock=time.time, record=gate.record,
                                user_name=h.get("user_name") or resolve_user_name(None), deploy_root=_ow.get("deploy_root"))
        except Exception as exc:  # noqa: BLE001 - e.g. an instance id the list cannot key on: no list, the old behaviour, said loudly
            work = None
            gate.record("work-verbs", decision="not-available", loud=True,
                        reason=f"open-work list not started: {type(exc).__name__}: {exc}")
        if work is not None:
            monitor.work_list_mode = True
            monitor.set_heartbeat_text_fn(None)
            work.attach(inject=injector.inject_line, monitor=monitor, observed=op_ctx.snapshot,
                        boot_blocks=lambda: (proxy_ref["proxy"]._boot_blocks() if proxy_ref.get("proxy") is not None else False),
                        gate_paused=lambda: bool(getattr(gate, "paused", False)))
            injector.set_presence(work.presence_line, _owl_sources, work.presence_fault)
            try:
                _owl_launch_check(_ow, instance_id, gate.record, time.time())
            except Exception as exc:  # noqa: BLE001
                gate.record("work-verbs", decision="check-failed", loud=True, error=f"{type(exc).__name__}: {exc}")

    ledger = LaunchLedger(
        instance_id=instance_id or "", session_dir=session_dir or "",
        depth=int(h.get("agent_depth", 0)), enabled=bool(h.get("agent_launch_enabled", False)),
        max_depth=int((h.get("agent_launch_caps") or {}).get("max_depth", 1)),
        max_children=int((h.get("agent_launch_caps") or {}).get("max_children", 2)),
        gate=gate,
    )
    compact_counter = {"n": 0}
    compact_gate_ref: Dict[str, Any] = {}  # set once the gate is built

    # fix-pass Part-3: fail-loud compaction-start confirmation. OFF by default
    # (byte-identical assembly to before); real launches opt in via main(). When
    # on, the compact verb reports ok:True only after a PreCompact signal is
    # observed on the self socket. Its world-advancing poll is wired below, once
    # the local control listener is in hand.
    compact_confirmer = None
    if confirm_compact:
        from .compact_confirm import CompactConfirmer

        compact_confirmer = (
            CompactConfirmer(timeout=confirm_compact_timeout)
            if confirm_compact_timeout is not None
            else CompactConfirmer()
        )

    # (flag post_compact_boot, resolved in resolve_harness_settings;
    # absent/off = NO object = the lifecycle path below is unchanged). The PTY leg of
    # the post-compact boot prompt: typed only on an empty operator line (the shared
    # D44 line-hold, via the monitor), before queued notices, source: harness.
    post_boot = None
    boot_cfg = h.get("post_compact_boot") or {}
    if boot_cfg.get("enabled") and boot_cfg.get("text"):
        from .post_compact_boot import PostCompactBoot

        post_boot = PostCompactBoot(
            text=boot_cfg["text"], inject=injector.inject_line,
            line_hold_reason=monitor.line_hold_reason, record=gate.record,
            paused=lambda: bool(getattr(gate, "paused", False)),
            # no REPL-ready gate: a compaction just completed, so the REPL is up by definition
            on_pending=monitor.set_boot_pending, on_written=monitor.consume_post_compact,
            settle_seconds=boot_cfg.get("settle_seconds", 2.0),
            max_hold_seconds=boot_cfg.get("max_hold_seconds", 1800.0),
            text_source=boot_cfg.get("text_source"),
        )

    from .compact_origin import CompactFallback

    def _on_lifecycle(event: str, info: Dict[str, Any]) -> None:
        # Flag subagent_compaction_check ON: the hook's post-compact-started frame (sent first thing, before its verdict wait) arms a
        # bounded fallback (compact_origin.FALLBACK_SECONDS, a pump ticker); any post verdict frame cancels it; a post frame after it
        # fired is a no-op (one boot prompt). The pre frame arms nothing. OFF: the hook sends no started frame, nothing changes.
        if event == "post-compact-started":
            compact_fallback.on_started()
            return
        if event == "pre-compact":
            compact_fallback.on_pre(info)
        elif not compact_fallback.on_post():
            return
        _apply_lifecycle(event, info)

    def _apply_lifecycle(event: str, info: Dict[str, Any]) -> None:
        phase = "pre" if event == "pre-compact" else "post"
        # flag subagent_compaction_check (the hook sets these keys only when it is ON): a subagent's compaction is recorded
        # once and applies NO session effect (no lifecycle point, note_compact, boot prompt or operator-line restore).
        if phase == "post" and info.get("ignored") == "subagent compaction, ignored":
            # kind "lifecycle" with a point other than "compact" on purpose: the scheduler's compaction_state() reads the LAST
            # lifecycle record, and the subagent's pre record (point compact) would otherwise hold injects "compacting" for its
            # 30-minute cap, since no boot prompt is written to clear it.
            gate.record("lifecycle", point="compact-ignored", decision="ignored", reason="subagent compaction, ignored",
                        payload={"phase": "post", "check": info.get("compaction_check_reason"),
                                 "agent_id": info.get("agent_id"), "agent_type": info.get("agent_type")})
            return
        if phase == "post" and info.get("compaction_check") == "unknown":
            gate.record("compact-origin", decision="unknown-treated-as-session",
                        reason="main transcript or pre-compact record unreadable; treated as the session's own compaction",
                        check=info.get("compaction_check_reason"))
        if phase == "pre":
            compact_counter["n"] += 1
            if compact_confirmer is not None:
                compact_confirmer.note_pre_compact()
        lifecycle.fire(POINT_COMPACT, {"phase": phase, **info},
                       once_key=f"compact-{phase}-{compact_counter['n']}-{len(log.records)}")
        if phase == "post":
            monitor.note_compact()
            if post_boot is not None:
                post_boot.arm()  # owe the boot prompt (after note_compact, which re-arms the nudge)
            # D3: put back the operator's saved unsent line (NOT sent).
            try:
                compact_gate_ref["gate"].on_post_compact()
            except Exception as exc:  # noqa: BLE001 - never break the lifecycle path
                gate.record("operator-line-restore", decision="failed", loud=True,
                            error=f"{type(exc).__name__}: {exc}")

    compact_fallback = CompactFallback(lambda info: _apply_lifecycle("post-compact", info), gate.record)

    def _on_feedback(text: str) -> None:
        tap = tap_ref.get("tap")
        gate.record("operator-feedback", text_preview=text[:200])
        if tap is not None:
            tap.feedback(text)

    def _on_compact_confirmation(result: Dict[str, Any]) -> None:
        # AUDIT F5: the fail-loud result must reach SOMEONE.
        # (a) provenance — ALWAYS record observed/not-observed + reason (NOT
        #     live-only; a re-flight reads this back to see what the harness saw).
        # (b) operator — on a NOT-observed result, surface it on the operator's
        #     terminal via the tap so a watching human sees the loud failure.
        observed = bool(result.get("compaction_observed"))
        gate.record(
            "compact-confirm",
            compaction_observed=observed,
            confirmation_ok=bool(result.get("ok")),
            reason=result.get("reason"),
            timeout=(compact_confirmer.timeout if compact_confirmer is not None else None),
        )
        if result.get("ok") is False:
            tap = tap_ref.get("tap")
            if tap is not None:
                window = compact_confirmer.timeout if compact_confirmer is not None else "?"
                tap.feedback(
                    f"COMPACT: /compact was sent but NO compaction was observed within {window}s "
                    "— it may have been paste-reclassified or the session was busy. NOT compacted; "
                    "check your handover and retry."
                )

    # ONE local Actuator serves both control paths (same injector, same single
    # write site): the hub rendezvous (operator, hub->proxy) and — fix-pass WS2a —
    # the agent-side local listener (self). Built only when a path needs it.
    # operator ruling 2026-09-29: the self-compact gate (90% threshold + directive
    # freshness), enforced at the verb. Reads the SAME gauge the monitor reads
    # (fresh read, falling back to the last good reading) and the session's own
    # provenance log (the file, so it spans resumes; in-memory when no file).
    # Audit fix: NO ``last_reading`` fallback for the gate (a dark gauge is no
    # reading), and after a compaction only a statusline reading whose
    # written_epoch is newer than it counts (``read_stamped``).
    from .compact_gate import CompactGate, load_log_records

    stamped_reader = (StatuslineHookContextReader(
        os.path.join(session_dir, ".claude", CONTEXT_STATE_FILENAME)).read_with_epoch
        if session_dir else None)
    from .compact_gate import TapLineControl

    compact_gate = CompactGate(
        read_context=monitor.read_now,
        read_stamped=stamped_reader,
        records=lambda: load_log_records(log.path, log.records),
        record=gate.record,
        # D3: the half-typed-line takeover (save / compact / restore).
        # The tap is built later: looked up lazily. No session dir = no takeover.
        line_control=TapLineControl(lambda: tap_ref.get("tap")),
        session_dir=session_dir,
    )
    compact_gate_ref["gate"] = compact_gate
    compact_gate.modal_check = lambda: gate._modal_reason()  # round 4 P2: never restore a line into a dialog

    # D15 amendment: the DSD-answer notice inbox (built with the actuator).
    from .notice_inbox import DsdNoticeInbox

    def _inline_notice_text(dsd_id: str, root_id: str) -> Optional[str]:
        from ..dsd.notice import inline_text_for

        return inline_text_for(dsd_id, root_id)

    def _notice_ack(frame: Dict[str, Any]) -> None:
        up = hub_link if hub_link is not None else hub_connection
        if up is None:
            raise OSError("no hub link")
        up.send_frame(frame)

    _boot = time.monotonic()
    _boot_grace = float(h.get("dsd_notice_boot_grace_seconds", 60.0))
    notice_inbox: Optional[DsdNoticeInbox] = None
    if hub_connection is not None or hub_link is not None:
        notice_inbox = DsdNoticeInbox(
            inject=injector.inject_line, ack=_notice_ack, gate=gate,
            # D15 fix pass: tri-state (empty / partial / unknown); no tap (a
            # stdin-less session) = no operator line = empty.
            line_state=_operator_line_state,
            unknown_after_seconds=unknown_line_after,
            # the stall-guard's require_ready rule: the monitor has primed (the
            # REPL is up); bounded by a boot grace for a gauge that never reads.
            ready=lambda: bool(monitor.primed) or (time.monotonic() - _boot) >= _boot_grace,
            quiet_seconds=float(h.get("dsd_notice_operator_quiet_seconds", 3.0)),
            # never type into an open AskUserQuestion / ExitPlanMode dialog
            modal_open=modal_guard.check if modal_on else None,
            inline_text=_inline_notice_text,        # pre-review B2: None unless dsd_notice_inline is ON (machine config)
        )
        notice_inbox.seed_from_log_file(log.path)

    from .hublink import RateLimiter

    # FIX PASS 3: superseded/vacated provenance records and operator-terminal
    # feedback are rate-limited like every other hub-link event (1 per 30s per
    # kind, the record carrying a suppressed count) — a flapping twin/rogue can
    # never flood the log or the operator's screen. The LINK state change itself
    # (park / reclaim) is never rate-limited.
    hub_frame_limiter = RateLimiter(float(h.get("hub_event_interval_seconds", 30.0)))

    def _on_hub_frame(kind: str, frame: Dict[str, Any]) -> None:
        # D15 fix pass: hub_hello (a VALID greeting — one leg of the reconnect
        # backoff reset), superseded (another harness took this instance_id) and
        # vacated (fix pass 2: the holder left — re-register).
        if kind == "hub_hello":
            if hub_link is not None:
                hub_link.note_hub_hello(frame.get("hub_instance_id"))
            return
        if kind not in ("superseded", "vacated"):
            return
        if hub_link is not None:
            if kind == "superseded":
                hub_link.note_superseded(frame)
            else:
                hub_link.note_vacated(frame)
        allowed, suppressed = hub_frame_limiter.allow(kind)
        if not allowed:
            return
        extra = {"suppressed": suppressed} if suppressed else {}
        if kind == "superseded" and frame.get("by") == "caller-binding":
            # round 3 (R2-P2-1): a caller-binding refusal is not "another harness": say what it is, loudly.
            state = hub_link.status() if hub_link is not None else {}
            gate.record("hub-link", event="registration-refused", decision="unregistered", by="caller-binding",
                        hub_instance_id=frame.get("hub_instance_id"),
                        reason=state.get("binding_refused_reason") or "refused by the hub's caller binding",
                        **extra)
            text = (f"HUB: registration of {instance_id!r} REFUSED by the hub's caller binding "
                    f"({state.get('binding_refused_reason') or 'no admissible credential'}): this session gets "
                    "no hub injects or DSD notices until it registers with an admitted credential.")
        elif kind == "superseded":
            gate.record("hub-link", event="superseded", decision="unregistered",
                        by=frame.get("by"), hub_instance_id=frame.get("hub_instance_id"),
                        reason="another harness registered this instance_id on the hub", **extra)
            text = (f"HUB: another harness registered as {instance_id!r} — THIS session is "
                    "no longer reachable from the dashboard hub (DSD notices, operator "
                    "fan-out and hub injects go to the other one).")
        else:
            gate.record("hub-link", event="vacated", decision="re-registering",
                        reason="the harness that superseded this one left the hub", **extra)
            text = (f"HUB: the other harness registered as {instance_id!r} left — this "
                    "session re-registered and is reachable from the hub again.")
        tap = tap_ref.get("tap")
        if tap is not None:
            tap.feedback(text + (f" ({suppressed} similar notice(s) suppressed)" if suppressed else ""))

    # requirement 2: the hub's answer to OUR \\all/DM (msg_id + recipients) is
    # appended to the operator-messages log, and a delivery-mismatch warning too.
    def _on_feedback_frame(frame: Dict[str, Any]) -> None:
        if op_msg_log is None or not isinstance(frame.get("msg_id"), str):
            return
        if isinstance(frame.get("delivery_held"), list):
            op_msg_log.append({"record": "delivery-held", "msg_id": frame["msg_id"],
                               "from_instance": instance_id, "sessions": frame["delivery_held"]})
            return
        if isinstance(frame.get("delivery_mismatch"), list):
            op_msg_log.append({"record": "delivery-mismatch", "msg_id": frame["msg_id"],
                               "from_instance": instance_id, "sessions": frame["delivery_mismatch"]})
            return
        op_msg_log.append({"record": "delivery", "msg_id": frame["msg_id"], "sha256": frame.get("sha256"),
                           "from_instance": instance_id, "target": frame.get("target"),
                           "recipients": frame.get("delivered"), "failed": frame.get("failed")})

    # requirement 3: after an operator broadcast/DM is typed into THIS session,
    # check what the TUI actually submitted (transcript, read-only) and report it.
    delivery_checker: Optional[Any] = None
    if session_dir and _x43_flag(h, "operator_delivery_check", _om.FLAG_DELIVERY_CHECK):
        from .operator_activity import resolve_transcript_path as _rtp

        def _on_delivery_result(res: Dict[str, Any]) -> None:
            verdict = res.get("verdict")
            gate.record("operator-delivery-check", decision=verdict, loud=(verdict == _om.MISMATCH),
                        **{k: v for k, v in res.items() if k != "verdict"})
            if verdict == _om.MISMATCH:
                t = tap_ref.get("tap")
                if t is not None:
                    t.feedback(f"WARNING: operator message {res.get('msg_id')} from {res.get('from_instance')} "
                               f"arrived ALTERED here ({res.get('delivered_len')} of {res.get('sent_len')} chars).")
            up = hub_send
            if up is not None and isinstance(res.get("msg_id"), str):
                try:
                    up({"type": "delivery_report", "msg_id": res["msg_id"], "from": res.get("from_instance"),
                        "verdict": verdict, "sent_sha256": res.get("sent_sha256"), "sent_len": res.get("sent_len"),
                        "delivered_sha256": res.get("delivered_sha256"), "delivered_len": res.get("delivered_len")})
                except Exception:  # noqa: BLE001 - the report is best-effort; the record above stands
                    pass

        delivery_checker = _om.DeliveryChecker(
            lambda: _rtp(session_dir), _on_delivery_result,
            timeout_seconds=float(h.get("delivery_check_timeout_seconds", 180.0)),
            poll_seconds=float(h.get("delivery_check_poll_seconds", 0.5)))

    def _on_broadcast_written(command: Dict[str, Any], since: float) -> None:
        if delivery_checker is None:
            return
        delivery_checker.check(sent_text=command.get("text") or "", since_epoch=since,
                               msg_id=command.get("msg_id") if isinstance(command.get("msg_id"), str) else None,
                               from_instance=command.get("from") if isinstance(command.get("from"), str) else None,
                               trigger=command.get("trigger") if isinstance(command.get("trigger"), str) else None)

    # (core): the ``alert`` self-verb's handler, bound ONLY when alerts is on (None = the verb
    # is refused 'disabled'). The SOURCE is this session's own instance id, never an argument; the agent's own
    # tighter rate limit applies; one provenance record per call (counts and the alert id, never the text).
    _alert_handler = None
    if h.get("alerts") and h.get("alerts_dir"):
        from .. import alerts as _alerts

        _alert_store = _alerts.AlertStore(h["alerts_dir"])

        def _alert_handler(frame: Dict[str, Any]) -> Dict[str, Any]:
            sev = frame.get("severity") or "info"
            res = _alerts.notify("agent:" + str(instance_id or "unknown"), sev, frame.get("title"),
                                 frame.get("body", ""), frame.get("link"), store=_alert_store,
                                 rate_max=_alerts.AGENT_RATE_MAX)
            status = res.get("status")
            decision = status if status in ("raised", "deduped", "rate-limited") else "refused"
            gate.record("alert", source="self-verb", decision=decision, status=status, severity=sev,
                        title_len=len(frame.get("title") or ""), body_len=len(frame.get("body") or ""),
                        alert_id=res.get("id"), macos=res.get("macos"))
            out = {"ok": bool(res.get("ok")), "action": "alert", "status": status,
                   "decision": decision if res.get("ok") else "refused"}
            for k in ("id", "macos", "link_dropped", "error"):
                if res.get(k) is not None and res.get(k) is not False:
                    out[k] = res[k]
            return out

    def _on_broadcast_released(ev: Any, since: float) -> None:
        """Round 3 P2-2: a HELD operator broadcast was typed on release: run the X43 delivery check the
        actuator skipped (it only checks a plain ALLOW). Skipped, WITH a record, when a hook modified the text
        (the comparison would report a false mismatch) or the hub gave no sent_sha256."""
        if delivery_checker is None:
            return
        import hashlib as _hl

        meta = ev.meta if isinstance(ev.meta, dict) else {}
        sha = meta.get("sent_sha256")
        if not isinstance(sha, str) or _hl.sha256(str(ev.content).encode("utf-8", "surrogateescape")).hexdigest() != sha:
            gate.record("operator-delivery-check", decision="not-run", msg_id=meta.get("msg_id"),
                        reason="released broadcast: text differs from what was sent (hook-modified) or no "
                               "sent_sha256; no delivery check")
            return
        _on_broadcast_written({"text": ev.content, "msg_id": meta.get("msg_id"), "from": meta.get("from_instance"),
                               "trigger": ev.trigger}, since)

    gate.on_operator_broadcast_released = _on_broadcast_released

    proxy_ref: Dict[str, Any] = {}
    actuator: Optional[Actuator] = None
    if hub_connection is not None or local_control is not None or hub_link is not None:
        actuator = Actuator(injector, monitor, on_lifecycle=_on_lifecycle, on_feedback=_on_feedback,
                            on_launch=ledger.handle, compact_confirmer=compact_confirmer,
                            on_operator_command=_on_operator_command,
                            compact_gate=compact_gate,
                            on_dsd_notice=(notice_inbox.offer if notice_inbox is not None else None),
                            on_hub_frame=_on_hub_frame,
                            # line-hold audit note 2: PostToolUse claims held notices
                            on_claim_held_notices=lambda: (proxy_ref["proxy"].claim_held_notices()
                                                           if proxy_ref.get("proxy") is not None else []),
                            operator_context=(work.wrap_context(op_ctx.context_field) if work is not None
                                              else op_ctx.context_field),
                            on_work=(work.handle if work is not None else None),
                            claim_presence=(work.presence_line if work is not None else None),
                            held_notice_count=(notice_inbox.held_count if notice_inbox is not None else None),
                            mail_identity=((instance_id or None) if (harness or {}).get("agent_mail") else None),
                            plan_usage_reader=(functools.partial(
                                _plan_usage_mod.read,
                                os.path.join(session_dir, ".claude", _plan_usage_mod.PLAN_USAGE_STATE_FILENAME))
                                if (session_dir and (harness or {}).get("plan_usage_verb")) else None),
                            alert_handler=_alert_handler,
                            on_feedback_frame=_on_feedback_frame,
                            on_broadcast_written=(_on_broadcast_written if delivery_checker is not None else None),
                            on_compact_confirmation=(
                                _on_compact_confirmation if compact_confirmer is not None else None))

    control_channel: Optional[ControlChannel] = None
    if hub_connection is not None:
        assert actuator is not None
        control_channel = ControlChannel(hub_connection.fileno(), actuator, origin=ORIGIN_HUB)

    if hub_link is not None:
        assert actuator is not None
        hub_link.attach(actuator)
        if hub_send is None:
            hub_send = hub_link.send_frame

    if local_control is not None:
        assert actuator is not None
        # Admit only this child's process tree (the claude pid is the root).
        local_control.attach(actuator, spawn_result.pid)

    tap: Optional[OperatorInputTap] = None
    if modal_on:
        # dialog-keystroke rule: snapshot the tap's line on dialog-open, restore on dialog-close
        def _modal_snapshot(first: bool) -> Any:
            t = tap_ref.get("tap")
            if t is None or (first and t.seconds_since_input() is not None):
                return None   # first look at an already-open dialog after the operator typed: cannot vouch
            return t.snapshot_line()

        gate.modal_snapshot = _modal_snapshot
        gate.modal_close_time = modal_guard.close_boundary
        gate.modal_restore = lambda snap, bnd: (
            tap_ref["tap"].restore_line_snapshot(snap, bnd.replay_from, bnd.stamp)
            if snap is not None and tap_ref.get("tap") is not None else None)
        gate.modal_force_busy = lambda cause: (tap_ref["tap"].force_busy(cause)
                                               if tap_ref.get("tap") is not None else None)
        gate.modal_open_time = modal_guard.open_boundary
        gate.modal_input_between = lambda t0, t1: (tap_ref["tap"].input_between(t0, t1)
                                                   if tap_ref.get("tap") is not None else False)
        gate.modal_line_state = lambda: (tap_ref["tap"].line_state if tap_ref.get("tap") is not None else None)
    if stdin_fd is not None:
        def _operator_activity() -> None:
            monitor.note_operator_activity()
            if notice_inbox is not None:
                notice_inbox.note_operator_activity()

        def _operator_input() -> None:
            # fix pass: only REAL input bytes (not focus/DA/DSR reports) move the beacon
            if op_beacon is not None:
                op_beacon.note_keystroke()   # throttled (<= 1 write / 2 s)
            # the same REAL-input predicate (optap.is_operator_input) ends
            # quiescence — a focus report is not the operator coming back.
            monitor.exit_quiescent(_q.TRIGGER_OPERATOR)
            # the operator typing in THIS session ends a dismissal of idle alerts.
            idle_snooze.on_operator_input()

        tap = OperatorInputTap(writer, gate, stdout_fd=stdout_fd, on_activity=_operator_activity,
                               command_plane=plane_enabled and not prompt_hook, child_pid=spawn_result.pid,
                               on_submit=(op_beacon.note_submit if op_beacon is not None else None),
                               on_input=_operator_input)
        tap_ref["tap"] = tap
        # round 2 (g64): the operator-line restore uses the injector's long-line rule.
        tap.long_line_paste_bytes = injector._long_line_paste_bytes
        # D3: a saved-but-never-restored operator line from a previous
        # run (a crash mid-compact) is reported LOUDLY, never silently dropped.
        if session_dir:
            _saved = os.path.join(session_dir, "operator-line-saved.json")
            try:
                from .compact_gate import saved_line_report
                _rep = saved_line_report(_saved, time.time())
                if (_rep["corrupt_asides"] or _rep["corrupt_file"] or _rep["pending"]
                        or _rep["expired_kept"] or _rep["poisoned_kept"]):
                    gate.record("operator-line-launch-report", loud=True, path=_saved,
                                corrupt_asides=_rep["corrupt_asides"], corrupt_file=_rep["corrupt_file"],
                                pending=_rep["pending"], pending_text_len=_rep["pending_text_len"],
                                expired_kept=_rep["expired_kept"], poisoned_kept=_rep["poisoned_kept"],
                                detail="saved operator-line file state at launch")
                if _rep["pending"]:
                    tap.feedback("An unsent line of yours was saved before a compact and NOT "
                                 "restored; it is pending in " + _saved)
            except Exception:  # noqa: BLE001 - the launch report never blocks a launch
                pass
    # The command REGISTRY exists whenever the plane can be reached: the tap's own
    # (fallback route), or — for a stdin-less session driven by the prompt hook —
    # a feed-less registry-only instance. Same handlers either way (no fork).
    registry = tap
    if registry is None and prompt_hook:
        registry = OperatorInputTap(writer, gate, stdout_fd=None, command_plane=False)
    if registry is not None:
        install_standard_commands(
            registry, gate, monitor=monitor, hub_send=hub_send,
            user_name=h.get("user_name") or resolve_user_name(None), instance_id=instance_id,
            status_extra=lambda: {"agent_launch": ledger.context(),
                                  "operator_command_route": "prompt-hook" if prompt_hook else "tap"},
            message_log=op_msg_log,
        )
        tap_ref["registry"] = registry
        if work is not None:  # the operator's own way to declare his status (no source needed)
            registry.register("presence", lambda ctx: work.operator_command(ctx.args),
                              "away <text> | in-office — declare where you are (agents see it; it never changes by itself)")

    watchers: List[Any] = [modal_guard] if modal_on else []  # the guard's screen leg reads child output
    if h.get("accept_folder_trust"):
        from .startup import FolderTrustAccepter

        watchers.append(FolderTrustAccepter(writer))

    def _on_exit(code: int, reason: str) -> None:
        op_ctx.close()   # clean exit: remove this session's activity beacon
        if delivery_checker is not None:
            delivery_checker.close()  #
        lifecycle.fire(POINT_AGENT_END, {"instance_id": instance_id, "exit_code": code, "reason": reason,
                                         "session_dir": session_dir})

    proxy = HarnessProxy(
        master_fd=spawn_result.master_fd,
        pid=spawn_result.pid,
        writer=writer,
        control_channel=control_channel,
        monitor=monitor,
        injector=injector,
        stdin_fd=stdin_fd,
        stdout_fd=stdout_fd,
        local_control=local_control,
        operator_tap=tap,
        on_exit=_on_exit,
        output_watchers=watchers,
        hub_link=hub_link,
        tickers=([notice_inbox.tick] if notice_inbox is not None else []) + [op_ctx.tick, gate.release_modal_holds, compact_gate.tick_restore, compact_fallback.tick]
        + ([work.tick] if work is not None else []),
        on_notice_hold=lambda decision, notice, reason: gate.record(
            "notice-hold", decision=decision, notice_kind=notice.kind.value,
            threshold=notice.threshold, reason=reason),
        on_loop_failure=lambda fields: gate.record("loop-failure", source="harness", **fields),
        post_compact_boot=post_boot,
    )
    proxy_ref["proxy"] = proxy
    # The monitor types notices locally into the child via the proxy's sink.
    monitor.set_on_notice(proxy.inject_notice)  # wire the local-inject side
    # Observable handles (tests, the status command, the launcher's agent-start).
    proxy.gate = gate  # type: ignore[attr-defined]
    proxy.lifecycle = lifecycle  # type: ignore[attr-defined]
    proxy.ledger = ledger  # type: ignore[attr-defined]
    proxy.operator_tap = tap  # type: ignore[attr-defined]
    proxy.actuator = actuator  # type: ignore[attr-defined]
    proxy.compact_confirmer = compact_confirmer  # type: ignore[attr-defined]
    proxy.compact_gate = compact_gate  # type: ignore[attr-defined]
    proxy.compact_fallback = compact_fallback  # type: ignore[attr-defined]
    proxy.dsd_notices = notice_inbox  # type: ignore[attr-defined]
    if notice_inbox is not None and post_boot is not None:
        # r2 (F3): the DSD-answer inbox types via the injector directly, so it
        # consults the SAME boot-pending signal (proxy._boot_blocks). Flag OFF: never wired.
        notice_inbox.set_boot_blocks(proxy._boot_blocks)
    proxy.hub_link = hub_link  # type: ignore[attr-defined]
    proxy.operator_context = op_ctx  # type: ignore[attr-defined]
    proxy.work_list = work  # type: ignore[attr-defined] # runtime (None unless open_work_list is ON)
    proxy.post_compact_boot = post_boot  # type: ignore[attr-defined]
    if compact_confirmer is not None and local_control is not None:
        # The PreCompact signal arrives on the LOCAL control socket while the
        # compact verb (itself dispatched off that socket, on the single pump
        # thread) is waiting. So the confirmation wait must SERVICE that socket.
        #
        # ⛔ AUDIT F1 (the deadlock): it must ALSO drain the child's terminal
        # output (master_fd) during the wait. The child's PreCompact hook can
        # only report AFTER the child has run — and a child that writes more than
        # its pty output buffer (~1 KB measured on macOS) BLOCKS on that write
        # until someone reads it. The pre-fix poll serviced ONLY the control
        # socket, so a chatty child blocked before its hook fired, the signal
        # never arrived in-window, and the verb returned a FALSE ok:False on a
        # compaction that actually happened. Reading master_fd here (read-only,
        # forwarded to the operator exactly as the normal pump does) keeps the
        # child unblocked so its hook can fire in-window. This is a narrow
        # re-entrant step on the single pump thread — never master_fd + stdin
        # WRITES, only the read-and-forward drain. Bounded per poll; the confirmer
        # bounds the whole wait. ⚠ Whether a real claude (Bun runtime) actually
        # emits >1 KB and blocks before its PreCompact hook is LIVE-ONLY (proven
        # in the re-flight); this wiring removes the pump-starvation deadlock a
        # naive block would cause, and the audit's real-pty starvation probe
        # (tests/test_confirm_poll_realpty.py) proves it for a blocking-write child.
        import select as _select

        def _confirm_poll(remaining: float) -> bool:
            fds = local_control.fds() + [spawn_result.master_fd]
            try:
                readable, _, _ = _select.select(fds, [], [], max(0.0, min(remaining, 0.1)))
            except (OSError, ValueError):
                # F3d: the transport is dead (socket/master closed, select error).
                # Signal the confirmer to STOP waiting rather than busy-spin to
                # the deadline (the auditor measured ~4.1M no-op iterations/0.5s).
                return False
            try:
                writer.flush_pending()  # master_write_nonblocking: bytes queued before this wait keep going out (no-op otherwise)
            except OSError:
                pass   # EIO / EBADF: the child is gone; the read below sees the end of file
            for fd in readable:
                if fd == spawn_result.master_fd:
                    proxy._on_master_readable()  # F1: drain + forward child output (read-only)
                elif local_control.owns(fd):
                    local_control.on_readable(fd)
            # ⛔ RE-DIAMOND N1 (the busy-spin F1 introduced): once master_fd is in
            # the select, a child that EXITS mid-wait leaves master_fd readable at
            # EOF FOREVER. `_on_master_readable()` above drains the b"" EOF and sets
            # `child_alive=False`, but if this poll keeps returning True the
            # confirmer spins at 100% CPU (~1.8M no-op iterations) to the deadline
            # and then returns a misleading "paste-reclassified or busy" reason on a
            # session that simply ended. Stop the wait the instant the child is gone:
            # record the child-exit (distinct reason) and return the child's
            # liveness — False breaks the confirmer's loop immediately (its dead-
            # transport contract), True keeps waiting while the child is still up.
            if not proxy.child_alive:
                compact_confirmer.note_child_exited()
                return False
            return True

        compact_confirmer.set_poll(_confirm_poll)
    return proxy


def default_rendezvous_file(machine_config: Optional[Any] = None) -> str:
    """``<runtime_root>/hub/rendezvous.json`` for this machine (D15 amendment).

    Mirrors where the Controller's hub publishes it (``dirname(intelligences_root)
    /hub``, i.e. the config's runtime root). Falls back to the config default
    runtime root when no machine config could be loaded."""
    from ..config import runtime_root_default
    from ..hub.rendezvous import rendezvous_path

    root = getattr(machine_config, "runtime_root", None) or runtime_root_default()
    return rendezvous_path(root)


def _registration_hash_if_hub_binds(rendezvous_file: str) -> Optional[str]:
    """Round 2: when the HUB publishes ``binding: on``, every harness needs a registration credential (even with
    profile MCP delivery OFF, so no MCP token is delivered), or the hub refuses its registration and the session
    gets no injects or notices. Returns sha256 of a fresh 256-bit token (the token itself is discarded), or None
    when the hub does not bind (OFF: nothing changes). Never raises."""
    try:
        from ..hub.rendezvous import read_rendezvous

        info, _why = read_rendezvous(rendezvous_file)
        if info is None or info.binding != "on":
            return None
        from ..hub.caller_binding import new_token, token_hash

        return token_hash(new_token())
    except Exception:  # noqa: BLE001
        return None


def build_hub_link(cfg: Dict[str, Any], instance_id: str, session_dir: str,
                   machine_config: Optional[Any] = None, mail: bool = False, **kw: Any) -> Any:
    """The harness's self-healing hub link for EVERY launch type (D15 amendment,
 proxy-attach-invariant).

    * discovery file: launch.json ``hub_rendezvous_file`` if set, else
      ``<runtime_root>/hub/rendezvous.json``;
    * ``hub_rendezvous_port`` (null for CLI/resume/adopted launches) is only a HINT.
    Nothing is connected here — the first :meth:`HubLink.tick` does that."""
    from .hublink import HubLink

    rv_file = cfg.get("hub_rendezvous_file")
    if not isinstance(rv_file, str) or not rv_file.strip():
        rv_file = default_rendezvous_file(machine_config)
    hint = cfg.get("hub_rendezvous_port")
    if isinstance(hint, str) and hint.isdigit():
        hint = int(hint)
    if isinstance(hint, bool) or not isinstance(hint, int):
        hint = None
    #. Round 3 (R2-P2-1): the registration hash is decided at REGISTER time, from the
    # hub's CURRENT rendezvous binding, by the provider below (stable once minted). A session started while the
    # hub was down or OFF therefore binds once the hub comes up ON. Before EVERY registration attempt the link
    # writes a fresh pending-launch record for (instance, hash) (round 2): the hub admits an unheld instance
    # only with one, and consumes it on bind. While the hub does not bind, no hash exists and nothing is written.
    on_mint = kw.pop("on_mint", None)
    rv_path = os.path.expanduser(rv_file)
    session_id = cfg.get("session_id") if isinstance(cfg.get("session_id"), str) else None

    def _caller_hash_provider() -> Optional[str]:
        h = cfg.get(CALLER_HASH_CFG_KEY)
        if not (isinstance(h, str) and h):
            h = _registration_hash_if_hub_binds(rv_path)
            if not h:
                return None
            cfg[CALLER_HASH_CFG_KEY] = h
        cfg[CALLER_PENDING_INSTANCE_CFG_KEY] = instance_id
        if on_mint is not None:
            try:
                on_mint()  # arm the every-exit cleanup now that a record will be written
            except Exception:  # noqa: BLE001
                pass
        return h

    def _write_record() -> None:
        h = cfg.get(CALLER_HASH_CFG_KEY)
        if isinstance(h, str) and h:
            from ..hub.caller_binding import write_pending_record

            write_pending_record(instance_id, session_id, h)

    kw = {**kw, "caller_hash_provider": _caller_hash_provider, "pending_writer": _write_record}
    return HubLink(instance_id, rendezvous_file=os.path.expanduser(rv_file), hint_port=hint,
                   meta=({"session_dir": session_dir, "agent_mail": True} if mail else {"session_dir": session_dir}), **kw)


def prepare_child_launch(
    cfg: Dict[str, Any],
    session_dir: str,
    *,
    python_bin: Optional[str] = None,
    listener_factory: Callable[[], LocalControlListener] = LocalControlListener,
    prompt_hook: bool = True,
    quiesce: bool = False,
    search_transcript: bool = False,
    restart_service: bool = False,
    session_mcp_from_profile: bool = False,
    rendezvous_file: Optional[str] = None,
    idle_snooze: bool = False,
    plan_usage: bool = False,
    alert: bool = False,
    sync_my_skills: bool = False,
    dsd_self_verbs: bool = False,
    work_verbs: bool = False,
    status_setter: bool = False,
    mail: bool = False,
) -> "tuple[List[str], Optional[LocalControlListener]]":
    """Everything the harness does BEFORE spawning claude (fix-pass WS1/WS2a/WS4).

    1. validate ``extra_args`` FIRST — a bad profile fails loud before any file
       or socket is created;
    2. WS1a: write ``harness.settings.json`` (the statusline hook);
    3. WS2a: unless ``launch.json`` says ``"self_actuation": false``, bind the
       private local control socket and write ``harness.mcp.json`` pointing the
       ``gnomon-self`` stdio server at it. A socket that cannot be bound
       RAISES — self-actuation is a 1.0 must-have and is never silently dropped;
    4. build the argv (harness flags + ``extra_args`` + ``--`` + prompt).

    Returns ``(argv, listener)``; the caller attaches the listener to the proxy
    (``assemble_proxy(local_control=...)``) and closes it on exit.
    """
    validate_extra_args(cfg.get("extra_args"))
    listener: Optional[LocalControlListener] = None
    mcp_config_path: Optional[str] = None
    self_actuation = cfg.get("self_actuation", True)
    if not isinstance(self_actuation, bool):
        raise ValueError("launch.json 'self_actuation' must be a boolean")
    agent_launch = cfg.get("agent_launch_enabled", False)
    if not isinstance(agent_launch, bool):
        raise ValueError("launch.json 'agent_launch_enabled' must be a boolean")
    # PTY-hooks WS-E: the listener is bound FIRST so the compact hooks in the
    # settings file can name its socket.
    if self_actuation:
        listener = listener_factory()
    try:
        # Group B (2026-09-28): carry the operator's permission mode into the
        # --settings file so a --terminal none session does not block on the
        # auto-mode onboarding modal (this is the ONE intended fresh-mode change to
        # harness.settings.json). Resolved from ~/.claude/settings.json, falling
        # back to {"defaultMode": "dontAsk"} when unreadable.
        settings_path = write_harness_settings(session_dir, python_bin,
                                               socket_path=listener.path if listener is not None else None,
                                               permissions=resolve_harness_permissions(),
                                               claude_md_excludes=resume_claude_md_excludes(cfg),
                                               prompt_hook=bool(prompt_hook and listener is not None),
                                               deny_layer=True)
        #: record WHICH route owns the
        # \\command plane for this launch (the proxy reads cfg; launch.json is the
        # durable witness). Exactly one route is live — never both.
        cfg["operator_command_route"] = (
            ROUTE_PROMPT_HOOK if (prompt_hook and listener is not None) else ROUTE_TAP)
        _record_operator_command_route(session_dir, cfg["operator_command_route"])
        extra_servers: Optional[Dict[str, Dict[str, Any]]] = None
        credential_config: Optional[str] = None
        if session_mcp_from_profile:
            # session_mcp_from_profile (default OFF). Lazy import: nothing is loaded when off.
            if listener is None:
                sys.stderr.write("gnomon harness: ⚠ session-mcp-from-profile is ON but self_actuation is "
                                 "off, so there is no --mcp-config file; profile MCP servers NOT delivered\n")
            else:
                extra_servers, credential_config = _resolve_session_mcp(cfg, session_dir, rendezvous_file)
        if listener is not None:
            mcp_config_path = write_self_mcp_config(session_dir, python_bin, listener.path,
                                                    agent_launch=agent_launch, quiesce=quiesce,
                                                    search_transcript=search_transcript,
                                                    restart_service=restart_service,
                                                    extra_servers=extra_servers,
                                                    idle_snooze=idle_snooze,
                                                    plan_usage=plan_usage,
                                                    alert=alert,
                                                    sync_my_skills=sync_my_skills,
                                                    dsd_self_verbs=dsd_self_verbs,
                                                    work_verbs=work_verbs, status_setter=status_setter,
                                                    mail=mail)
    except BaseException:
        if listener is not None:
            listener.close()
        cleanup_caller_credential(cfg)
        raise
    try:
        if credential_config and mcp_config_path:
            argv = build_child_argv(cfg, session_dir, settings_path=settings_path,
                                    mcp_config_path=mcp_config_path, extra_mcp_config_paths=[credential_config])
        else:
            argv = build_child_argv(cfg, session_dir, settings_path=settings_path,
                                    mcp_config_path=mcp_config_path)
    except BaseException:
        if listener is not None:
            listener.close()
        cleanup_caller_credential(cfg)
        raise
    return argv, listener


#: In-memory launch-cfg keys (never written to launch.json): the caller token's sha256 (for the hub link's
#: register frames) and the credential config's path (deleted on exit). The token itself is never kept.
CALLER_HASH_CFG_KEY = "caller_token_sha256"
CALLER_CREDENTIAL_CFG_KEY = "caller_credential_file"
#: The instance id the pending-launch records were written for (set with the hash by build_hub_link).
CALLER_PENDING_INSTANCE_CFG_KEY = "caller_pending_instance"


def cleanup_caller_credential(cfg: Dict[str, Any]) -> bool:
    """Delete this launch's caller-credential config AND its pending-launch record, if either exists (every exit
    path; see :class:`CredentialCleanup`). Idempotent. Imports session_mcp only when a credential file exists
    (hook reachability: nothing loads when the flag is OFF). Returns whether anything was removed."""
    if not isinstance(cfg, dict):
        return False
    removed = False
    path = cfg.get(CALLER_CREDENTIAL_CFG_KEY)
    if path:
        try:
            from . import session_mcp as _sm

            removed = _sm.remove_credential_config(path) or removed
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"gnomon harness: ⚠ caller credential {path} NOT removed: {exc}\n")
    h = cfg.get(CALLER_HASH_CFG_KEY)
    iid = cfg.get(CALLER_PENDING_INSTANCE_CFG_KEY)
    if h and iid:
        try:
            from ..hub.caller_binding import remove_pending_record

            removed = remove_pending_record(iid, h) or removed
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"gnomon harness: ⚠ pending-launch record NOT removed: {exc}\n")
    return removed


class CredentialCleanup:
    """F-P2-4: run :func:`cleanup_caller_credential` on every exit path of a launch.

    ``arm()`` (idempotent) registers an ``atexit`` hook and SIGTERM/SIGHUP/SIGINT handlers; a handler cleans up,
    RESTORES the prior handlers, then hands the signal on (calls a prior Python handler, or re-sends it under the
    restored default so the process ends exactly as it would have). ``disarm()`` restores the prior handlers and
    unregisters the atexit hook. Nothing is installed until ``arm()``, so a launch with no credential is unchanged."""

    SIGNALS = ("SIGTERM", "SIGHUP", "SIGINT")

    def __init__(self, cfg: Dict[str, Any]) -> None:
        self._cfg = cfg
        self._saved: Dict[int, Any] = {}
        self.armed = False

    def run(self) -> bool:
        return cleanup_caller_credential(self._cfg)

    def _handler(self, signum: int, frame: Any) -> None:
        self.run()
        prior = self._saved.get(signum, signal.SIG_DFL)
        self._restore()
        if callable(prior):
            prior(signum, frame)
        elif prior != signal.SIG_IGN:
            os.kill(os.getpid(), signum)  # the default action, now restored

    def arm(self) -> None:
        if self.armed:
            return
        import atexit

        atexit.register(self.run)
        for name in self.SIGNALS:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                prior = signal.getsignal(sig)
                signal.signal(sig, self._handler)
                self._saved[sig] = prior if prior is not None else signal.SIG_DFL
            except (ValueError, OSError):
                pass  # not the main thread: atexit + the finally paths still cover it
        self.armed = True

    def _restore(self) -> None:
        for sig, prior in list(self._saved.items()):
            try:
                if signal.getsignal(sig) == self._handler:
                    signal.signal(sig, prior)
            except (ValueError, OSError):
                pass
        self._saved.clear()

    def disarm(self) -> None:
        if not self.armed:
            return
        import atexit

        self._restore()
        atexit.unregister(self.run)
        self.armed = False


def _session_mcp_record(session_dir: str, record: Dict[str, Any]) -> None:
    try:
        from .hooks import append_record, injection_log_path

        append_record(injection_log_path(session_dir), record)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"gnomon harness: session-mcp provenance not recorded: {exc}\n")


def _resolve_session_mcp(cfg: Dict[str, Any], session_dir: str,
                         rendezvous_file: Optional[str]) -> "tuple[Dict[str, Dict[str, Any]], Optional[str]]":
    """session_mcp_from_profile: resolve + REPORT (stderr warning per skip, one provenance record).
    Never raises into the launch: a fault delivers nothing extra and says so.

 delivery is REFUSED unless the HUB publishes ``binding: on`` (its state, not this
    launcher's flag view). When the hub is delivered, a per-launch caller token goes ONLY into a 0600 credential
    config (returned as the second value; passed as a second ``--mcp-config`` file); its sha256 goes into the
    in-memory cfg for the hub link. Returns ``(extra_servers_for_harness_mcp_json, credential_config_or_None)``."""
    try:
        from . import session_mcp as _sm

        rv = os.path.expanduser(rendezvous_file or cfg.get("hub_rendezvous_file") or default_rendezvous_file(None))
        state, how = _sm.hub_binding_state(rv)
        if state != "on":
            reason = (f"the hub does not publish binding: on ({how if state is None else 'binding: ' + state}); "
                      "profile MCP delivery is REFUSED until the hub binds callers")
            sys.stderr.write(f"gnomon harness: ⚠ session-mcp-from-profile: {reason}\n")
            _session_mcp_record(session_dir, {
                "kind": "launch", "event": "session-mcp-refused", "instance_id": cfg.get("instance_id"),
                "hub_binding": state, "reason": reason})
            return {}, None
        extra, events, names = _sm.resolve_session_servers(
            session_dir, cwd=resolve_child_cwd(cfg, session_dir), rendezvous_file=rv)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"gnomon harness: ⚠ session-mcp-from-profile failed ({type(exc).__name__}: {exc}); "
                         "profile MCP servers NOT delivered\n")
        return {}, None
    credential: Optional[str] = None
    if _sm.HUB_SERVER_NAME in extra:
        hub_entry = dict(extra.pop(_sm.HUB_SERVER_NAME))
        try:
            from ..hub.caller_binding import CALLER_HEADER, new_token, token_hash

            token = new_token()
            hub_entry["headers"] = {CALLER_HEADER: token}
            credential = _sm.write_credential_config(session_dir, {_sm.HUB_SERVER_NAME: hub_entry})
            cfg[CALLER_HASH_CFG_KEY] = token_hash(token)
            cfg[CALLER_CREDENTIAL_CFG_KEY] = credential
            del token, hub_entry
            events.append({"server": _sm.HUB_SERVER_NAME, "action": "include",
                           "reason": "with a per-launch caller credential (0600, outside the session dir, "
                                     "deleted on exit)"})
        except Exception as exc:  # noqa: BLE001
            credential = None
            cfg.pop(CALLER_HASH_CFG_KEY, None)
            cfg.pop(CALLER_CREDENTIAL_CFG_KEY, None)
            events.append({"server": _sm.HUB_SERVER_NAME, "action": "skip",
                           "reason": f"caller credential could not be written ({type(exc).__name__}); "
                                     "hub NOT delivered"})
    for ev in events:
        if ev["action"] in ("skip", "warn"):
            sys.stderr.write(f"gnomon harness: ⚠ session-mcp {ev.get('server') or '-'}: "
                             f"{ev['action']}: {ev['reason']}\n")
    _session_mcp_record(session_dir, {
        "kind": "launch", "event": "session-mcp-wiring", "instance_id": cfg.get("instance_id"),
        "profile_servers": list(names),
        "delivered": sorted(list(extra) + ([_sm.HUB_SERVER_NAME] if credential else [])),
        "caller_credential": bool(credential), "events": events,
    })
    return extra, credential


ROUTE_PROMPT_HOOK = "prompt-hook"
ROUTE_TAP = "tap"


def _record_operator_command_route(session_dir: str, route: str) -> None:
    """Merge the route into launch.json. A launch dir without launch.json (unit
    tests that call prepare_child_launch bare) is left alone — cfg carries it."""
    if not os.path.isfile(os.path.join(session_dir, ".claude", "launch.json")):
        return
    from ..agent_def import merge_launch_json

    merge_launch_json(session_dir, {"operator_command_route": route,
                                    "prompt_command_hook_installed": route == ROUTE_PROMPT_HOOK})


def _set_default_winsize(master_fd: int, rows: int = 50, cols: int = 160) -> None:
    """A detached (no-tty) harness still gives the child TUI a sane size."""
    try:
        import fcntl
        import struct
        import termios

        fcntl.ioctl(master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except (OSError, ImportError):
        pass


def record_env_scrub(session_dir: str, instance_id: Any, scrubbed: List[str], passthrough: List[str]) -> None:
    """Security posture D3: the provenance ``launch`` record of the credential scrub —
    the COUNT and the NAMES, never a value. Best effort (never blocks the launch)."""
    try:
        from .hooks import append_record, injection_log_path

        append_record(injection_log_path(session_dir), {
            "kind": "launch", "event": "env-scrub", "instance_id": instance_id,
            "scrubbed_count": len(scrubbed), "scrubbed_names": sorted(scrubbed),
            "passthrough": list(passthrough), "deny_layer": list(HARNESS_DENY_RULES),
        })
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"gnomon harness: env-scrub provenance not recorded: {exc}\n")


def check_deny_guard_live(session_dir: str, instance_id: Any, *, env: Optional[Dict[str, str]] = None,
                          cwd: Optional[str] = None) -> bool:
    """Audit fix pass 2: prove the deny_guard hook is importable by the CHILD'S python
    (``sys.executable``, the one written into harness.settings.json). Always records a
    provenance ``launch``/``deny-guard-selftest`` row; on failure ALSO warns loudly in
    the operator's terminal. Never refuses the launch (the deny RULES still hold)."""
    ok, detail = deny_guard_selftest(sys.executable or "python3", env=env, cwd=cwd)
    try:
        from .hooks import append_record, injection_log_path

        append_record(injection_log_path(session_dir), {
            "kind": "launch", "event": "deny-guard-selftest", "instance_id": instance_id, "ok": ok,
            "decision": "deny-guard-live" if ok else "deny-guard-INACTIVE", "detail": detail})
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"gnomon harness: deny-guard selftest not recorded: {exc}\n")
    if not ok:
        sys.stderr.write(
            "gnomon harness: ⚠⚠ SECURITY: the deny_guard hook is NOT active for this child "
            f"({detail}). Its shell wrapper fails open, so rm -rf ~ / curl|sh are NOT blocked; "
            "the permissions.deny rules (sudo/pkill/killall) still hold.\n")
    return ok


def _init_feature_events(session_dir: str) -> None:
    """X26 (process-kinds): declare this process the ``harness`` kind and name its session
    directory (the proxy's environment does not carry GNOMON_SESSION_DIR; its child's does), then warm the
    feature registry on a daemon thread so the first operator command never pays the engine build on the
    pump thread. Entirely guarded: telemetry never fails a launch."""
    try:
        import threading as _threading

        from .. import feature_events as _fe
        from .. import feature_sites as _fs

        _fe.set_process_kind("harness")
        _fe.set_session_dir(session_dir)
        if _fe.events_enabled():
            _threading.Thread(target=_fs.warm_registry, name="fu-registry-warm", daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[list] = None) -> int:  # pragma: no cover - real PTY/hub
    """Spawn claude under the proxy for the session dir named in argv[1]."""
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 2:
        sys.stderr.write("usage: python -m gnomon.harness <session_dir>\n")
        return 2
    session_dir = argv[1]
    cfg = read_launch_config(session_dir)
    # round 2 (F-P2-4): the caller credential and the pending-launch record are deleted on
    # EVERY exit path: atexit + SIGTERM/SIGHUP/SIGINT (prior handlers restored), a pre-spawn exception, a refused
    # or failed launch, and the clean exit. Armed only once a credential exists (OFF: nothing installed).
    cred_cleanup = CredentialCleanup(cfg)
    _init_feature_events(session_dir)   # X26: this process is the harness (never fatal)

    # (hub_rendezvous_port is now read by build_hub_link as a HINT — D15 amendment)
    instance_id = cfg.get("instance_id", os.path.basename(session_dir))
    thresholds = cfg.get("context_thresholds")  # config-driven; None -> 90/95

    try:
        machine_config, stall_override = _load_machine_context(instance_id)
        harness = resolve_harness_settings(cfg, session_dir, machine_config=machine_config,
                                           profile_override=stall_override)
        # relaunch_derives_resume_keys (default OFF = every line below is skipped, byte-for-byte the previous
        # launch). Read ONCE here; launch.json ``flags`` > machine config > registry default.
        # A missing or broken resume_keys module (a partial checkout, a land that copied launcher.py first) must leave
        # every harness start behaving EXACTLY as with the flag off: the whole import is guarded, never the start.
        try:
            from ..resume_keys import (apply_prompt_once, check_resume_transcript, consume_prompt_once,
                                       expire_prompt_once, flag_on as _relaunch_fix_flag)

            relaunch_fix = _relaunch_fix_flag(cfg, machine_config)
        except Exception as exc:  # noqa: BLE001
            relaunch_fix = False
            if isinstance(cfg.get("flags"), dict) and cfg["flags"].get("relaunch_derives_resume_keys") is True:
                sys.stderr.write(f"gnomon harness: ⚠ the relaunch fix is requested for this session but "
                                 f"resume_keys is unavailable ({type(exc).__name__}: {exc}); it is OFF for this start\n")
        # AUDIT F3b: validate the confirm-compact keys BEFORE binding a socket or
        # spawning, so a bad value (NaN/Infinity/out-of-range timeout, or a
        # non-boolean confirm_compact) fails loud here rather than tracebacking
        # after the child is live.
        confirm_compact_requested, confirm_compact_timeout = validate_confirm_compact_config(cfg)
        env_passthrough = validate_env_passthrough(cfg.get("env_passthrough"))  # security posture D3
        # RESUME MODE (2026-09-28): the child's cwd is the ORIGINAL session's cwd
        # (resume_cwd) so `claude --resume <id>` resolves the transcript; fresh
        # mode keeps session_dir. Fails LOUD here (before spawn) on a resume id
        # with no resume_cwd, alongside the other launch-config validation.
        # (Transcript STOP-THE-LINE: resolved BEFORE the argv now — it is pure —
        # because the transcript plan needs the child's cwd.)
        child_cwd = resolve_child_cwd(cfg, session_dir)
        # Transcript STOP-THE-LINE (2026-09-29): decide + RECORD where the real
        # transcript will land before spawning (launch.json + .claude/transcript.jsonl
        # symlink). Fresh: pre-generated --session-id. Resume: original project dir
        # + resumed id. Continue/resume in extra_args WITHOUT --fork-session: no
        # --session-id (claude refuses it), path recorded null + reason. Any fork:
        # pre-generated --session-id.
        from .transcript import plan_transcript, record_transcript_location

        transcript_plan = plan_transcript(cfg, child_cwd)
        prompt_once_armed = False
        if relaunch_fix:
            # The harness never spawns `claude --resume <id>` at a conversation that is not on disk (ValueError ->
            # "launch refused", exit 2, before any socket or spawn), and arms a deliberate one-shot resume prompt.
            check_resume_transcript(cfg, transcript_plan)
            # Fix round 3: a one-shot older than 24 h is ignored and cleared; a live one is consumed HERE, under the
            # launch.json lock with a fresh read (at most once even when two starts overlap), BEFORE the argv is built.
            # Not delivered (consumed by another start, changed, lock not taken in time) = the prompt this start had.
            expire_prompt_once(session_dir, cfg)
            _had_prompt = "resume_prompt" in cfg
            _prompt_before = cfg.get("resume_prompt")
            prompt_once_armed = apply_prompt_once(cfg)
            if prompt_once_armed and not consume_prompt_once(session_dir, cfg):
                prompt_once_armed = False
                if _had_prompt:
                    cfg["resume_prompt"] = _prompt_before
                else:
                    cfg.pop("resume_prompt", None)
        if transcript_plan["inject_session_id"]:
            cfg["session_id"] = transcript_plan["session_id"]
        else:
            cfg.pop("session_id", None)  # a stale on-disk id must not become --session-id
        # Agent-def (2026-09-29): re-render FRESH right before spawn, and in RESUME
        # mode arm the --add-dir delivery (cfg['agent_def_add_dir']) the argv reads.
        render_agent_def_at_spawn(cfg, session_dir, same_dir_fix=relaunch_fix)
        child_argv, listener = prepare_child_launch(cfg, session_dir,
                                                    prompt_hook=bool(harness.get("operator_plane", True)),
                                                    quiesce=bool(harness.get("quiescent_state", False)),
                                                    search_transcript=bool(harness.get("search_transcript")),
                                                    restart_service=bool(harness.get("restart_service")),
                                                    session_mcp_from_profile=bool(harness.get("session_mcp_from_profile")),
                                                    idle_snooze=bool(harness.get("idle_snooze")),
                                                    plan_usage=bool(harness.get("plan_usage_verb")),
                                                    alert=bool(harness.get("alerts")),
                                                    sync_my_skills=bool(harness.get("sync_my_skills")),
                                                    dsd_self_verbs=bool(harness.get("dsd_self_verbs")),
                                                    work_verbs=bool(harness.get("open_work_list")),
                                                    status_setter=bool((harness.get("open_work_list") or {}).get("status_setter")),
                                                    mail=bool(harness.get("agent_mail")),
                                                    rendezvous_file=(cfg.get("hub_rendezvous_file") or default_rendezvous_file(machine_config)))
        if cfg.get(CALLER_CREDENTIAL_CFG_KEY):
            cred_cleanup.arm()
        _report_restart_service_config(session_dir, harness)
        harness["prompt_command_hook"] = cfg.get("operator_command_route") == ROUTE_PROMPT_HOOK
        _sync_post_compact_boot_file(session_dir, harness)
        try:
            record_transcript_location(session_dir, transcript_plan)
        except BaseException:
            if listener is not None:
                listener.close()
            raise
    except (ValueError, OSError) as exc:
        # Fail LOUD in the terminal the operator is watching; never launch a
        # child with argv / wiring nobody asked for.
        cred_cleanup.run()
        cred_cleanup.disarm()
        sys.stderr.write(f"gnomon harness: launch refused: {exc}\n")
        return 2
    try:
        # WS-G: the child (and its tool subprocesses) carry their launch depth +
        # identity. These are a WITNESS only — agent_launch decides by ancestry.
        from .agent_launch import ENV_DEPTH, ENV_INSTANCE, ENV_SESSION

        from .transcript import ENV_FORCE_PERSISTENCE, record_exit_check

        # Transcript STOP-THE-LINE (2026-09-29): sanitize_env scrubs the inherited
        # CLAUDE_CODE_CHILD_SESSION marker (the measured cause of the empty pilot
        # project dirs); FORCE_SESSION_PERSISTENCE is Claude Code's own override for
        # the same suppression — belt and braces, so a future unscrubbed marker cannot
        # silently turn transcript saving off again.
        # Security posture D3 (2026-09-29): credential scrub (profile passthrough honoured);
        # the scrubbed NAMES + count (never values) go to the provenance log.
        scrubbed: List[str] = []
        env = sanitize_env(overrides={ENV_DEPTH: str(harness["agent_depth"]), ENV_INSTANCE: str(instance_id),
                                      ENV_SESSION: session_dir, ENV_FORCE_PERSISTENCE: "1",
                                      **agent_def_child_env(cfg)},
                           passthrough=env_passthrough, scrubbed=scrubbed)
        # g25b/env-leak (2026-09-30): fresh mode passes no --add-dir, so the parent's
        # CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD must NOT leak into the child
        # (resume/add-dir mode sets it via agent_def_child_env, above).
        if not agent_def_child_env(cfg):
            from ..agent_def import ADDITIONAL_DIRS_CLAUDE_MD_ENV

            env.pop(ADDITIONAL_DIRS_CLAUDE_MD_ENV, None)
        record_env_scrub(session_dir, instance_id, scrubbed, env_passthrough)
        check_deny_guard_live(session_dir, instance_id, env=env, cwd=child_cwd)
        # telemetry as-is|otlp. Flag off (default) -> env returned untouched
        # (byte-identical) and no spans; flag on + no endpoint -> loud warning, nothing
        # exported. Never raises, never refuses the launch.
        from .telemetry import (attach_telemetry, child_env_for_launch, resolve_settings as _otel_settings,
                                telemetry_for_launch)

        otel = _otel_settings(cfg, machine_config)
        env = child_env_for_launch(env, otel, instance_id=instance_id, session_dir=session_dir)
        telemetry = None
    except BaseException:
        # F-P2-4: a pre-spawn exception (env scrub, deny-guard selftest, telemetry) must not leak the credential.
        cred_cleanup.run()
        cred_cleanup.disarm()
        if listener is not None:
            listener.close()
        raise
    try:
        spawn_result = PtySpawner().spawn(child_argv, env=env, cwd=child_cwd)
        if not os.isatty(0):
            _set_default_winsize(spawn_result.master_fd)

        # D15 amendment (operator-approved 2026-09-29): NOT a one-shot any more. The
        # HubLink finds the hub via <runtime_root>/hub/rendezvous.json (the
        # launch-time hub_rendezvous_port — null for CLI/resume launches — is only
        # a hint) and re-registers whenever the hub restarts or the socket drops,
        # for the harness's whole life. No hub = keep running; it never blocks or
        # crashes the child. Hub-link state transitions go to the provenance log.
        # round 3: the hub link mints its registration hash lazily (at register time) and arms the every-exit
        # cleanup through on_mint the moment it does
        hub_link = build_hub_link(cfg, instance_id, session_dir, machine_config, mail=bool(harness.get("agent_mail")),
                                  on_mint=cred_cleanup.arm)

        # RESUME MODE: the child's real transcript lives under the mangled
        # resume_cwd, not session_dir — point the fallback estimate there. The
        # statusline hook (session_dir-owned) stays authoritative; this only
        # corrects the fallback. child_cwd == session_dir in fresh mode.
        read_context = default_context_reader(session_dir, transcript_cwd=child_cwd)
        # fix-pass Part-3: real launches opt IN to fail-loud compaction-start
        # confirmation, but only when the self socket exists (the PreCompact
        # signal has no route otherwise). ⭐ Architect design call (2026-09-28):
        # confirm_compact is OFF unless launch.json opts IN (confirm_compact:true)
        # — the re-flight sets it. Whether a real claude blocks on its terminal
        # before PreCompact is LIVE-ONLY until F1's fix is proven against a real
        # claude, so on-by-default would carry unproven risk. Values were already
        # validated (F3b) above. confirm_compact_requested defaults False here.
        confirm_compact = bool(listener is not None and confirm_compact_requested)
        proxy = assemble_proxy(
            spawn_result,
            read_context=read_context,
            thresholds=thresholds,
            local_control=listener,
            session_dir=session_dir,
            instance_id=instance_id,
            harness=harness,
            confirm_compact=confirm_compact,
            confirm_compact_timeout=confirm_compact_timeout,
            hub_link=hub_link,
        )
        _gate = proxy.gate  # type: ignore[attr-defined]
        # the provenance log is the ONE span hook (pointers only).
        _tap = getattr(proxy, "operator_tap", None)

        def _known_instances():  # audit F2: gm.target only for real instances
            root = machine_config.intelligences_root() if machine_config is not None else None
            return os.listdir(root) if root and os.path.isdir(root) else []

        telemetry = attach_telemetry(_gate.log, telemetry_for_launch(
            otel, instance_id=instance_id, session_dir=session_dir,
            transcript_path=transcript_plan.get("transcript_path"),
            command_names=(lambda: list(_tap.commands())) if _tap is not None else None,
            known_instances=_known_instances))
        if otel.reason != "flag-off":  # as-is writes nothing new
            _gate.record("telemetry", mode=otel.mode, decision=otel.reason, endpoint=otel.endpoint or None,
                         protocol=otel.protocol, log_prompts=otel.log_prompts,
                         harness_spans=telemetry is not None, reason=otel.message or None)
        hub_link.set_on_event(lambda ev: _gate.record("hub-link", **{k: v for k, v in ev.items()
                                                                     if k != "t"}))
        hub_link.tick()  # first attempt now, so agent-start records the truth
        # WS-E agent-start: fired ONCE, post-spawn (the cold-start prompt is argv,
        # so it has already been delivered). A handler DENY/DEFER is a VETO: the
        # child is torn down and the launch reports refused. RESUME MODE marks the
        # row a RESUME (no cold-start bootstrap injected — build_agent_start_payload).
        start = proxy.lifecycle.fire(POINT_AGENT_START, build_agent_start_payload(  # type: ignore[attr-defined]
            cfg, harness, instance_id=instance_id, session_dir=session_dir,
            pid=spawn_result.pid, hub_registered=hub_link.connected,
        ))
        if start.vetoed:
            sys.stderr.write(f"gnomon harness: agent-start VETOED by hook: {start.reason}\n")
            try:
                os.kill(spawn_result.pid, signal.SIGTERM)
            except OSError:
                pass
            proxy._reap()  # fires agent-end once
            return 3
        try:
            return proxy.run()
        finally:
            hub_link.close()
            if telemetry is not None:
                telemetry.close(timeout=2.0)
    finally:
        if listener is not None:
            listener.close()
        # the caller-credential config is deleted on exit (a SIGKILL leaves it; it is
        # 0600, machine-local, and its hash is revoked when the hub sees this session unregister).
        cred_cleanup.run()
        cred_cleanup.disarm()
        # Post-run witness (best effort — a SIGKILLed launcher never gets here).
        record_exit_check(session_dir)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

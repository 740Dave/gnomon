"""The D6 statusline hook — writes the AUTHORITATIVE context gauge (fix-pass WS1).

Flight finding #5/#6a: nothing ever wrote ``<session_dir>/.claude/
context_state.json``, so :class:`~gnomon.harness.context_readers.
StatuslineHookContextReader` read null and the 90% compact-guidance could never
fire. This module is that writer.

Claude Code runs a ``statusLine`` command on every status refresh, piping one
JSON object on stdin and displaying the command's stdout. The harness installs
this module as that command for its child (``launcher.write_harness_settings``,
passed via ``claude --settings``)::

    python -m gnomon.harness.statusline_hook --state <session>/.claude/context_state.json \
        --chain-user-statusline

Percentage rule (same as other status HUDs' context-percent function, which reads the same
stdin on this machine):

1. ``context_window.used_percentage`` when it is a number > 0 (Claude Code
   v2.1.6+; 0 is "not populated yet" on a fresh session);
2. else ``(input + cache_creation + cache_read) / context_window_size`` from
   ``context_window.current_usage`` (output tokens are not context);
3. else UNKNOWN — nothing is written (never a fabricated 0).

State file (atomic replace)::

    {"used_percentage": 57.0, "source": "...", "context_window_size": 200000,
     "session_id": "...", "transcript_path": "...", "written_epoch": ...}

Display: with ``--chain-user-statusline`` the user's own ``statusLine`` command
from ``<claude config>/settings.json`` (for example an external status HUD) is run
with the SAME stdin and its output is shown — installing the gauge must not
silently take the user's statusline away. Otherwise (or if chaining fails) the
hook's own ``<instance id>  ctx NN%`` line is printed.

Keyed by the SESSION DIR, never by cwd (
2026-10-08): a resumed session's child can run in a cwd that is not the harness
session dir (an agent that resumes a conversation started elsewhere),
so the payload's ``cwd`` / ``workspace.current_dir`` does not locate
``context_state.json``. The session dir is ``dirname(dirname(--state))``. The
chained command is told it in its environment: ``GNOMON_SESSION_DIR``,
``GNOMON_CONTEXT_STATE`` (the file just written), ``GNOMON_INSTANCE_ID`` and
``GNOMON_CONTEXT_PCT`` (this refresh's reading, empty when unknown).

⛔ A statusline must never crash or hang the TUI: every failure is swallowed,
the process always exits 0, and the chained command is bounded by a timeout.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, TextIO, Tuple

from .context_readers import _context_tokens, claude_config_dir

#: Marker used to refuse chaining to ourselves (infinite recursion guard).
_SELF_MODULE = "gnomon.harness.statusline_hook"
_CHAIN_TIMEOUT_SECONDS = 5.0


def percent_from_statusline(payload: Any) -> Tuple[Optional[float], Optional[str]]:
    """``(used_percentage, source)`` from a statusline stdin payload, or ``(None, None)``."""
    if not isinstance(payload, dict):
        return None, None
    cw = payload.get("context_window")
    if not isinstance(cw, dict):
        return None, None
    native = cw.get("used_percentage")
    if isinstance(native, (int, float)) and not isinstance(native, bool) and native > 0:
        return float(min(100.0, max(0.0, native))), "context_window.used_percentage"
    size = cw.get("context_window_size")
    usage = cw.get("current_usage")
    if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0 and isinstance(usage, dict):
        tokens = _context_tokens(usage)
        if tokens is not None:
            return float(min(100.0, max(0.0, 100.0 * tokens / float(size)))), "context_window.current_usage"
    return None, None


def write_state(state_path: str, pct: float, source: str, payload: Dict[str, Any]) -> None:
    cw = payload.get("context_window") if isinstance(payload.get("context_window"), dict) else {}
    body = {
        "used_percentage": pct,
        "source": source,
        "context_window_size": cw.get("context_window_size"),
        "session_id": payload.get("session_id"),
        "transcript_path": payload.get("transcript_path"),
        "written_epoch": time.time(),
    }
    directory = os.path.dirname(state_path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{state_path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
        fh.write("\n")
    os.replace(tmp, state_path)


def user_statusline_command(settings_path: Optional[str] = None) -> Optional[str]:
    """The user's own statusLine command, unless it is (or wraps) this hook."""
    path = settings_path or os.path.join(claude_config_dir(), "settings.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        return None
    sl = settings.get("statusLine") if isinstance(settings, dict) else None
    if not isinstance(sl, dict) or sl.get("type", "command") != "command":
        return None
    cmd = sl.get("command")
    if not isinstance(cmd, str) or not cmd.strip() or _SELF_MODULE in cmd:
        return None
    return cmd


def session_dir_of(state_path: str) -> str:
    """The harness session dir a ``--state`` path belongs to
    (``<session_dir>/.claude/context_state.json``)."""
    return os.path.dirname(os.path.dirname(os.path.abspath(state_path)))


def _sanitise_id(value: object) -> str:
    """Drop NUL / control characters and lone surrogates: the id goes into a child's
    environment and onto the terminal, and either would raise or garble there."""
    text = value if isinstance(value, str) else ""
    return "".join(c for c in text if c.isprintable() and not (0xD800 <= ord(c) <= 0xDFFF))


def _instance_id(session_dir: str) -> str:
    try:
        from ..liveness import instance_id_for

        return _sanitise_id(instance_id_for(session_dir)) or "?"
    except BaseException:  # noqa: BLE001 - a statusline must never crash the TUI
        return _sanitise_id(os.path.basename(session_dir)) or "?"


def _chain_env(state_path: str, session_dir: str, instance_id: str, pct: Optional[float]) -> Dict[str, str]:
    env = dict(os.environ)
    env.update({
        "GNOMON_SESSION_DIR": session_dir,
        "GNOMON_CONTEXT_STATE": os.path.abspath(state_path),
        "GNOMON_INSTANCE_ID": instance_id,
        "GNOMON_CONTEXT_PCT": f"{pct:.0f}" if pct is not None else "",
    })
    return env


def _run_chained(cmd: str, raw_stdin: str, env: Optional[Dict[str, str]] = None) -> Optional[str]:
    try:
        proc = subprocess.run(
            cmd, shell=True, input=raw_stdin, capture_output=True, text=True,
            timeout=_CHAIN_TIMEOUT_SECONDS, env=env,
        )
    except (OSError, ValueError, subprocess.SubprocessError):  # ValueError covers UnicodeEncodeError, embedded NUL
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.rstrip("\n")


def main(
    argv: Optional[List[str]] = None,
    stdin: Optional[TextIO] = None,
    stdout: Optional[TextIO] = None,
) -> int:
    try:  # g48d silence at the hook entry, first; g48e(c)/audit-fix2 F-1: a missing or broken helper must never touch the verdict
        from ..hook_silence import silence_logging
        silence_logging()
    except BaseException:
        pass
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    parser = argparse.ArgumentParser(prog="python -m " + _SELF_MODULE, add_help=True)
    parser.add_argument("--state", required=True, help="context_state.json path to write")
    parser.add_argument("--chain-user-statusline", action="store_true",
                        help="also run + display the user's own statusLine command")
    parser.add_argument("--user-settings", default=None, help=argparse.SUPPRESS)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0  # never break the TUI, even on a bad install

    try:
        raw = stdin.read()
    except (OSError, ValueError):
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else None
    except ValueError:
        payload = None

    pct, source = percent_from_statusline(payload)
    if pct is not None and source is not None:
        try:
            write_state(args.state, pct, source, payload)  # type: ignore[arg-type]
        except OSError:
            pass

    # slice 1: persist rate_limits (account plan usage) beside the context
    # gauge. Independent of the context reading; absent/garbage rate_limits writes
    # nothing (the old file simply ages into 'stale'), never a fabricated value.
    try:
        from . import plan_usage

        rl = plan_usage.extract_rate_limits(payload)
        if rl is not None:
            plan_usage.write_state(
                os.path.join(os.path.dirname(args.state) or ".", plan_usage.PLAN_USAGE_STATE_FILENAME), rl)
    except BaseException:  # noqa: BLE001 - a statusline must never crash the TUI
        pass

    session_dir = session_dir_of(args.state)
    instance_id = _instance_id(session_dir)
    shown: Optional[str] = None
    if args.chain_user_statusline:
        cmd = user_statusline_command(args.user_settings)
        if cmd is not None:
            shown = _run_chained(cmd, raw, _chain_env(args.state, session_dir, instance_id, pct))
    if shown is None:
        shown = instance_id + "  ctx " + (f"{pct:.0f}%" if pct is not None else "--")
    try:
        stdout.write(shown + "\n")
        stdout.flush()
    except (OSError, ValueError):
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

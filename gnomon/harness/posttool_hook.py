"""PostToolUse hook: deliver HELD threshold notices to the agent (2026-09-29).

Line-hold audit note 2. The harness never types a compact-guidance / FYI notice
onto the operator's half-written line (``harness.line_hold``); it queues it. But
a line held forever would keep the 90% guidance from EVER reaching the agent,
which could then hit a system auto-compact unwarned (operator rule: compact happens at
>= 90%, deliberately). An auto-compact only happens while the agent works, i.e.
makes tool calls — so this hook runs after every tool call, asks THIS proxy over
its private self socket (``{"cmd": "held_notices"}``) for any held notice, and
returns it as model-visible context. The operator's line is never touched; the
harness records ``notice-hold`` / ``delivered-via-posttooluse`` and does not type
it later on Enter.

Output contract (Claude Code 2.1.284; verified by the binary's hook-output schema
AND a live ``claude -p`` probe with a negative control, 2026-09-29)::

    {"hookSpecificOutput": {"hookEventName": "PostToolUse",
                            "additionalContext": "<text>"}}      exit 0

Claude Code attaches the text to the tool result as a ``hook_additional_context``
attachment, which the model reads.

⛔ FAIL OPEN: any error -> print NOTHING, exit 0 (the installed command also ends
``2>/dev/null; exit 0``, covering an interpreter/import failure). ⛔ CHEAP: it
fires after EVERY tool call — stdlib only, one socket round trip bounded at 1.5 s TOTAL.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
import time
from typing import List, Optional, TextIO

_TIMEOUT_SECONDS = 1.0
#: TOTAL wall-clock budget for the whole round trip (re-audit: the per-recv
#: timeout alone let a server trickling a byte every 0.5 s hold the hook ~20 s).
_TOTAL_DEADLINE_SECONDS = 1.5
_MAX_REPLY = 1 << 20
HOOK_EVENT = "PostToolUse"


def claim(socket_path: str) -> List[str]:
    """Ask the proxy for its held notices. Raises on any transport problem or
    when the TOTAL deadline (``_TOTAL_DEADLINE_SECONDS``) passes."""
    deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(_TIMEOUT_SECONDS)
        s.connect(socket_path)
        s.sendall(b'{"cmd": "held_notices"}\n')
        buf = b""
        while b"\n" not in buf and len(buf) < _MAX_REPLY:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("posttool_hook: total deadline exceeded")
            s.settimeout(min(_TIMEOUT_SECONDS, remaining))
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf.split(b"\n", 1)[0] or b"{}")
    if not (isinstance(reply, dict) and reply.get("ok") is True):
        return []
    notices = reply.get("notices")
    if not isinstance(notices, list):
        return []
    return [n for n in notices if isinstance(n, str) and n.strip()]


def _emit_feature_event(outcome: str) -> None:
    """X26: one feature event per invocation that ran the hook's logic, after the
    verdict and inside its own guard: it can never change the output or the exit status. A ``hooks``-kind
    process. Never raises."""
    try:
        from .. import feature_events as _fe

        _fe.emit_hook("claude:PostToolUse", outcome, kind="hooks")
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[List[str]] = None, stdin: Optional[TextIO] = None,
         stdout: Optional[TextIO] = None) -> int:
    try:  # g48d silence at the hook entry, first; g48e(c)/audit-fix2 F-1: a missing or broken helper must never touch the verdict
        from ..hook_silence import silence_logging
        silence_logging()
    except BaseException:
        pass
    try:
        stdin = sys.stdin if stdin is None else stdin
        stdout = sys.stdout if stdout is None else stdout
        parser = argparse.ArgumentParser(prog="python -m gnomon.harness.posttool_hook",
                                         add_help=False)
        parser.add_argument("--socket", default=None)
        try:
            args, _unknown = parser.parse_known_args(argv)
        except SystemExit:
            return 0
        try:
            stdin.read()        # drain Claude's hook JSON (content unused)
        except (OSError, ValueError):
            pass
        if not args.socket:
            return 0
        try:
            notices = claim(args.socket)
        except BaseException:  # noqa: BLE001 - X26: record the failed verdict, then fail OPEN as before
            _emit_feature_event("error")
            raise
        if not notices:
            _emit_feature_event("ok")
            return 0
        out = json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_EVENT,
                                                 "additionalContext": "\n\n".join(notices)}})
        stdout.write(out + "\n")
        stdout.flush()
        _emit_feature_event("ok")        # after the verdict output is written and flushed (Diamond F1)
    except BaseException:  # noqa: BLE001 - fail OPEN: nothing on stdout, exit 0
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)

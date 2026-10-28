"""The operator ``\\command`` plane as a Claude Code ``UserPromptSubmit`` hook.
 (2026-09-29, operator-approved redirect).

WHY: the byte-level :class:`~.optap.OperatorInputTap` lost to terminal key
encoding twice. Claude Code 2.1.284 in iTerm2 turns on kitty keyboard flags,
xterm modifyOtherKeys, focus events and SGR any-motion mouse tracking, so the
operator's stdin is a stream of escape sequences the tap had to guess about.
This hook runs AFTER Claude Code's own key handling and receives the DECODED
submitted prompt — the text the operator actually submitted.

Contract (verified against the installed claude 2.1.284 binary, 2026-09-29):

* input  — JSON on stdin: ``{session_id, transcript_path, cwd, hook_event_name:
  "UserPromptSubmit", prompt, session_title}`` (``hook_event_name:"UserPromptSubmit",
  prompt:r`` in the binary's hook-input builder);
* exit 0 with EMPTY stdout — the prompt proceeds untouched (a non-empty stdout at
  exit 0 is added to the model's context, so pass-through prints NOTHING);
* block — exit 0 with ``{"decision": "block", "reason": <shown to the operator>,
  "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
  "suppressOriginalPrompt": <bool>}}`` — the exact shape Claude Code's own internal
  UserPromptSubmit block emits; the prompt never reaches the model.

Routing, on the prompt after leading whitespace:

* ``\\\\…`` (a doubled sigil) — pass through untouched: the escape for a message
  that must start with a backslash;
* ``\\<word>…`` — a command. Sent as ONE ``{"cmd": "operator_command", "line": …}``
  to THIS proxy's self socket (the same socket + peer-ancestry admission the
  lifecycle hook and the self-MCP use), where the SAME registry the tap uses runs
  it. The prompt is ALWAYS blocked, with the result — success, refusal, unknown
  command, or an unreachable harness. A failed command never looks like a sent
  message, and never reaches the agent;
* anything else (including ``\\`` followed by a non-word character) — pass through.

Identity is the harness's (the proxy that owns the socket), never the caller's.
⛔ A hook must never break the TUI: every failure on a command line becomes a
visible block; every failure on any other line is a silent pass-through.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any, Dict, List, Optional, TextIO

SIGIL = "\\"
_COMMAND_RE = re.compile(r"\\[A-Za-z0-9][A-Za-z0-9._-]*(\s|$)")


def classify(prompt: Any) -> Optional[str]:
    """The command line to dispatch, or ``None`` to pass the prompt through."""
    if not isinstance(prompt, str):
        return None
    line = prompt.lstrip()
    if not line.startswith(SIGIL) or line.startswith(SIGIL * 2):
        return None
    if not _COMMAND_RE.match(line):
        return None
    return line.rstrip("\r\n")


def block(reason: str, *, suppress_original: bool) -> Dict[str, Any]:
    return {"decision": "block", "reason": reason,
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "suppressOriginalPrompt": bool(suppress_original)}}


def dispatch(socket_path: Optional[str], line: str) -> Dict[str, Any]:
    """One ``operator_command`` round-trip -> the block payload to emit."""
    name = line[1:].split(None, 1)[0] if len(line) > 1 else ""
    try:
        from .self_mcp import actuate

        reply = actuate(socket_path, {"cmd": "operator_command", "line": line})
    except Exception as exc:  # noqa: BLE001 - a hook never crashes
        reply = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    text = reply.get("reply") if isinstance(reply.get("reply"), str) else None
    if text is None:
        err = reply.get("error") or reply
        return block(f"\\{name} NOT processed — the harness did not answer ({err}). "
                     f"Nothing was sent to the agent.", suppress_original=False)
    ok = bool(reply.get("ok"))
    # Success: the receipt alone. Anything else: keep the original prompt visible
    # so the operator can see (and re-type) exactly what failed.
    return block(f"[gm] {text}", suppress_original=ok)


def _emit_feature_event(outcome: str) -> None:
    """X26: one feature event per prompt this hook handled, after its verdict and inside
    its own guard (it can never change the output or exit status). outcome ``ok`` for a pass-through or a
    successful command, ``refused`` for a command the harness did not accept. A ``hooks``-kind process.
    Never raises."""
    try:
        from .. import feature_events as _fe

        _fe.emit_hook("claude:UserPromptSubmit", outcome, kind="hooks")
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[List[str]] = None, stdin: Optional[TextIO] = None,
         stdout: Optional[TextIO] = None) -> int:
    try:  # g48d silence at the hook entry, first; g48e(c)/audit-fix2 F-1: a missing or broken helper must never touch the verdict
        from ..hook_silence import silence_logging
        silence_logging()
    except BaseException:
        pass
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    parser = argparse.ArgumentParser(prog="python -m gnomon.harness.prompt_command_hook")
    parser.add_argument("--socket", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        args = argparse.Namespace(socket=None)
    try:
        payload = json.loads(stdin.read() or "{}")
    except (OSError, ValueError):
        return 0  # unreadable input: never block an ordinary prompt
    line = classify(payload.get("prompt") if isinstance(payload, dict) else None)
    if line is None:
        _emit_feature_event("ok")
        return 0  # pass-through: print NOTHING (exit-0 stdout would reach the model)
    try:
        out = dispatch(args.socket, line)
    except Exception as exc:  # noqa: BLE001
        out = block(f"{line.split(None, 1)[0]} NOT processed — hook error {type(exc).__name__}: {exc}. "
                    f"Nothing was sent to the agent.", suppress_original=False)
    try:
        stdout.write(json.dumps(out) + "\n")
        stdout.flush()
    except (OSError, ValueError):
        pass
    # after the verdict output is written and flushed (Diamond F1)
    _emit_feature_event("ok" if out.get("hookSpecificOutput", {}).get("suppressOriginalPrompt") else "refused")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

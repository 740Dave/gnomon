"""Claude-dialect compact signal — the native hook command (PTY-hooks WS-E).

Installed by the harness into ``<session>/.claude/harness.settings.json`` (next
to the WS1 statusline hook) for two Claude Code hook events::

    PreCompact                  -> --event pre-compact
    SessionStart (matcher compact) -> --event post-compact

Each run reads Claude's hook JSON on stdin and reports one
``{"cmd": "lifecycle", "event": ..., "trigger": ...}`` line to THIS proxy's
private local control socket (the same socket the self-MCP verbs use; the hook
process is a descendant of claude, so the socket's peer-ancestry check admits
it). The proxy records it in the provenance log, fires the ``compact`` lifecycle
point, and — on post-compact — arms the dual-idle stall trigger's shorter
post-compact threshold (WS-C is the agent-agnostic backstop either way).

For ``post-compact`` it also prints one line to stdout, which Claude Code adds
to the resumed session's context: a NON-forcing reminder (a SessionStart hook
cannot submit a turn; that is what the harness's resume inject is for).

⛔ A hook must never break or stall the TUI: every failure is swallowed, the
socket round-trip is bounded, and the process always exits 0.

AGENT-DEF RE-RENDER (2026-09-29, additive — only with ``--session-dir``):

* ``pre-compact`` RE-RENDERS the agent def (fresh ``{{lts: … }}`` expansion) into
  a NEW ``<session>/.claude/agent-def/NNN-pre-compact-<UTC>.md`` artifact and the
  delivered ``.claude/CLAUDE.md``, recorded in launch.json + provenance. ⭐ WHY
  PRE: Claude Code (2.1.284) caches CLAUDE.md per process and re-reads it only in
  its post-compact cleanup (``Tet("compact")`` → ``load_reason: "compact"``); live
  probe: a PreCompact-hook write was in the post-compact context, a mid-session
  write with no compaction was NOT. PreCompact is the earliest point that lands.
  The render runs AFTER the socket report so the compact confirmer is not delayed.
* ``post-compact`` appends the CURRENT render's status line (path + ok / ⚠
  FAILED) to its stdout context line, so the resumed session is told what it
  booted with — and told LOUDLY if its memory expansion failed.

POST-COMPACT BOOT TEXT (flag ``post_compact_boot``, default OFF):
``post-compact`` also appends the per-role boot line, read from
``<session>/.claude/post-compact-boot.json`` (written by the harness at launch,
removed when the flag is off; see :mod:`.post_compact_boot`). This is the ORDERING
leg: the text shares the compaction summary's context, so it precedes any later
turn. No file = the stdout is exactly what it was before.

SUBAGENT COMPACTIONS (flag ``subagent_compaction_check``, default OFF):
Claude Code runs these hooks for a subagent's compaction too. With the flag ON, pre-compact records the session's main-transcript
``compact_boundary`` count and post-compact checks for a new one (:mod:`.compact_origin`); none means a subagent's, and the hook
sends the proxy an ``ignored`` frame (one record, no compaction effect) and prints nothing. An unreadable transcript keeps the
behaviour above and records why. OFF: not one byte differs.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
from typing import List, Optional, TextIO

POST_COMPACT_CONTEXT = (
    "[Gnomon] Compaction complete. Resume your in-flight task from your "
    "handover notes. If you stay idle with no operator input, the harness will "
    "send one resume nudge."
)
_TIMEOUT_SECONDS = 2.0
EVENTS = ("pre-compact", "post-compact")
#: Flag ``subagent_compaction_check`` ON only: sent at the very start of post-compact, BEFORE the verdict wait, so the harness can bound a hook
#: that is killed at its timeout (compact_origin.FALLBACK_SECONDS). Never sent with the flag OFF.
STARTED_EVENT = "post-compact-started"


def report(socket_path: Optional[str], event: str, trigger: Optional[str],
           extra: Optional[dict] = None) -> bool:
    if not socket_path:
        return False
    frame = {"cmd": "lifecycle", "event": event, "trigger": trigger}
    if event == STARTED_EVENT:
        frame = {"cmd": "lifecycle", "event": event}
    if extra:   # only with flag subagent_compaction_check ON; absent = the frame is exactly what it was
        frame.update(extra)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(_TIMEOUT_SECONDS)
            s.connect(socket_path)
            s.sendall((json.dumps(frame) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0] or b"{}")
        return bool(isinstance(reply, dict) and reply.get("ok"))
    except (OSError, ValueError):
        return False


def main(argv: Optional[List[str]] = None, stdin: Optional[TextIO] = None,
         stdout: Optional[TextIO] = None) -> int:
    try:  # g48d silence at the hook entry, first; g48e(c)/audit-fix2 F-1: a missing or broken helper must never touch the verdict
        from ..hook_silence import silence_logging
        silence_logging()
    except BaseException:
        pass
    stdin = sys.stdin if stdin is None else stdin
    stdout = sys.stdout if stdout is None else stdout
    parser = argparse.ArgumentParser(prog="python -m gnomon.harness.lifecycle_hook")
    parser.add_argument("--event", choices=EVENTS, required=True)
    parser.add_argument("--socket", default=None)
    parser.add_argument("--session-dir", default=None)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 0
    try:
        payload = json.loads(stdin.read() or "{}")
    except (OSError, ValueError):
        payload = {}
    trigger = None
    if isinstance(payload, dict):
        trigger = payload.get("trigger") or payload.get("source")
    # (flag subagent_compaction_check, default OFF): at post-compact,
    # ask the session's own main transcript whether THIS session was compacted. OFF = ``extra`` stays None and nothing below
    # differs from before.
    _report_started(args)
    extra, ignored = _post_compact_origin(args, payload)
    reported = report(args.socket, args.event, trigger if isinstance(trigger, str) else None, extra)
    if args.event == "pre-compact" and args.session_dir:
        _note_pre_compact(args.session_dir)
        # ⛔ Diamond #3: a PreCompact hook's STDOUT becomes compaction custom
        # instructions. The render must emit NOTHING there — silence stdout at the
        # fd AND sys.stdout level (a library print, a C-level write) for its duration.
        with _silenced_stdout():
            rerender_agent_def(args.session_dir)
    if args.event == "pre-compact":
        _emit_feature_event("pre-compact", reported, args.socket)
    if args.event == "post-compact" and ignored:
        _emit_feature_event("post-compact", reported, args.socket)   # no context line, no boot text: no compaction effect
    elif args.event == "post-compact":
        text = POST_COMPACT_CONTEXT
        if args.session_dir:
            try:
                from ..agent_def import status_line

                line = status_line(args.session_dir)
                if line:
                    text += "\n" + line
            except Exception:  # noqa: BLE001 — a hook never fails
                pass
        # (flag post_compact_boot): the ORDERING leg. The per-role boot
        # text goes into THIS context, so it is present before any following turn
        # (incl. a background subagent's task-notification). No file (flag off) = the
        # line is exactly what it was before.
        try:
            from .post_compact_boot import CONTEXT_LABEL, read_boot_text

            boot = read_boot_text(args.session_dir)
            if boot:
                text += "\n" + CONTEXT_LABEL + "\n" + boot
        except Exception:  # noqa: BLE001 — a hook never fails
            pass
        # B3 (flag compact_plain_messages, default OFF): one line saying the compact is done. OFF = the
        # text is exactly what it was before. Read from the machine config only; a failure here means OFF.
        try:
            from . import compact_messages as _cm

            if _cm.enabled():
                text += "\n" + _cm.completed_line()
        except Exception:  # noqa: BLE001 — a hook never fails
            pass
        try:
            stdout.write(text + "\n")
            stdout.flush()
        except (OSError, ValueError):
            pass
        _emit_feature_event("post-compact", reported, args.socket)
    return 0


def _report_started(args) -> None:
    """Flag ``subagent_compaction_check`` ON, post-compact only: tell the harness the post hook has begun, before the verdict wait.
    Flag OFF or any error: nothing is sent. Never raises."""
    if args.event != "post-compact" or not args.socket:
        return
    try:
        from . import compact_origin as _co

        if _co.enabled():
            report(args.socket, STARTED_EVENT, None)
    except Exception:  # noqa: BLE001 - a hook never fails
        pass


def _note_pre_compact(session_dir: str) -> None:
    """Flag ``subagent_compaction_check``: remember the main transcript's boundary count for the post-compact check."""
    try:
        from . import compact_origin as _co

        if _co.enabled():
            _co.note_pre(session_dir)
    except Exception:  # noqa: BLE001 - a hook never fails
        pass


def _post_compact_origin(args, payload):
    """``(extra frame fields or None, ignored)``. ``ignored`` is True only for a verified subagent compaction. ``unknown`` (the
    main transcript or the pre record unreadable) keeps today's behaviour and records why. Flag OFF or any error: (None, False)."""
    if args.event != "post-compact" or not args.session_dir:
        return None, False
    try:
        from . import compact_origin as _co

        if not _co.enabled():
            return None, False
        v = _co.verdict_post(args.session_dir)
        if v["verdict"] == _co.SUBAGENT:
            extra = {"ignored": _co.IGNORED_TEXT, "compaction_check_reason": v["reason"]}
            for key in ("agent_id", "agent_type"):    # evidence only, never relied on
                val = payload.get(key) if isinstance(payload, dict) else None
                if isinstance(val, str) and val:
                    extra[key] = val[:200]
            return extra, True
        if v["verdict"] == _co.UNKNOWN:
            return {"compaction_check": "unknown", "compaction_check_reason": v["reason"]}, False
        return None, False
    except Exception:  # noqa: BLE001 - a hook never fails
        return None, False


def _emit_feature_event(event: str, reported: bool, socket_path: Optional[str]) -> None:
    """X26: one feature event per invocation, after the verdict, inside its own guard
    (a safety path: it reads no workspace tree and can never change the output or exit status). outcome
    ``ok`` when the harness acknowledged the report (or no socket was configured), else ``error``. A
    PreCompact hook's STDOUT becomes compaction instructions: the emitter writes nothing there. Never
    raises."""
    try:
        from .. import feature_events as _fe

        outcome = "ok" if (reported or not socket_path) else "error"
        if event == "pre-compact":
            _fe.emit_hook("claude:PreCompact", outcome, kind="hooks")
        else:
            _fe.emit_hook("claude:SessionStart", outcome, kind="hooks")
    except Exception:  # noqa: BLE001
        pass


@contextlib.contextmanager
def _silenced_stdout():
    """Route fd 1 and ``sys.stdout`` to /dev/null for the block (restored after).
    Diagnostics belong on stderr / the provenance log, never on a PreCompact
    hook's stdout. Best effort: if fd juggling fails, still swap sys.stdout."""
    saved_fd = None
    devnull_fd = None
    saved_py = sys.stdout
    try:
        try:
            sys.stdout.flush()
        except (OSError, ValueError, AttributeError):
            pass
        try:
            saved_fd = os.dup(1)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull_fd, 1)
        except OSError:
            saved_fd = None
        sys.stdout = open(os.devnull, "w")
        yield
    finally:
        try:
            sys.stdout.close()
        except Exception:  # noqa: BLE001
            pass
        sys.stdout = saved_py
        if saved_fd is not None:
            try:
                os.dup2(saved_fd, 1)
            finally:
                os.close(saved_fd)
        if devnull_fd is not None:
            os.close(devnull_fd)


def rerender_agent_def(session_dir: str) -> Optional[dict]:
    """PreCompact: render a fresh agent def from launch.json's ``agent_def_source``.

    Never raises. With no recorded source (a pre-2026-09-29 launch) nothing is
    re-rendered and the skip is RECORDED in the provenance log (never silent);
    the existing CLAUDE.md stays in place.
    """
    try:
        from ..agent_def import record_provenance, render_agent_def, superseded_paths

        try:
            with open(os.path.join(session_dir, ".claude", "launch.json"), "r", encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (OSError, ValueError) as exc:
            rec = {"event": "pre-compact", "status": "error", "reason": f"launch.json unreadable: {exc}"}
            record_provenance(session_dir, rec)
            return rec
        source = cfg.get("agent_def_source") if isinstance(cfg, dict) else None
        if not source:
            rec = {"event": "pre-compact", "status": "no-source-recorded",
                   "reason": "launch.json carries no agent_def_source — agent def NOT re-rendered"}
            record_provenance(session_dir, rec)
            return rec
        rid = cfg.get("resume_session_id")
        # A same-dir resume (spawn recorded agent_def_delivery.same_dir; only with
        # relaunch_derives_resume_keys ON): the delivered file IS the project CLAUDE.md, nothing is superseded.
        deliv = cfg.get("agent_def_delivery")
        same_dir = isinstance(deliv, dict) and deliv.get("same_dir") is True
        return render_agent_def(session_dir, source, "pre-compact",
                                mode="resume" if rid else "fresh",
                                superseded=superseded_paths(cfg.get("resume_cwd")) if rid and not same_dir else None)
    except Exception as exc:  # noqa: BLE001 — a hook never fails
        try:
            from ..agent_def import record_provenance

            rec = {"event": "pre-compact", "status": "error", "reason": f"{type(exc).__name__}: {exc}"}
            record_provenance(session_dir, rec)
            return rec
        except Exception:  # noqa: BLE001
            return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

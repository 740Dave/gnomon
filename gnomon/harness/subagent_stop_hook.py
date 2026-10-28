"""SubagentStop hook: the harness writes the subagent's SUMMARY file from its returned text.

Installed by :func:`gnomon.harness.launcher.subagent_stop_hook_settings` into
``<session>/.claude/harness.settings.json``. Reads Claude Code's hook JSON on stdin (``agent_id``,
``agent_type``, ``agent_transcript_path``, ``last_assistant_message``, ...), and, ONLY while
``subagent_summary_md`` is ON (registry default OFF), writes the file (:mod:`.subagent_summary`).

HOT (g47): a fresh ``python -m`` per subagent stop, so an edit here is live on save; everything is flag-gated.
Output contract: it prints NOTHING and exits 0, always (a SubagentStop hook's stdout/exit can block the stop;
this one never does). The one X26 hook-point event is emitted AFTER the work, inside its own guard.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional, TextIO


def _emit_feature_event(outcome: str, session_dir: Optional[str] = None) -> None:
    try:
        from .. import feature_events as _fe

        if session_dir and os.path.isabs(session_dir):
            _fe.set_session_dir(session_dir)     # the actor: the hook was handed its session dir explicitly
        _fe.emit_hook("claude:SubagentStop", outcome, kind="hooks")
    except Exception:  # noqa: BLE001
        pass


def main(argv: Optional[List[str]] = None, stdin: Optional[TextIO] = None, stdout: Optional[TextIO] = None) -> int:
    try:  # g48d silence at the hook entry, first; g48e(c)/audit-fix2 F-1: a missing or broken helper must never touch the verdict
        from ..hook_silence import silence_logging
        silence_logging()
    except BaseException:
        pass
    outcome = "ok"
    session_dir: Optional[str] = None
    try:
        stdin = sys.stdin if stdin is None else stdin
        parser = argparse.ArgumentParser(prog="python -m gnomon.harness.subagent_stop_hook", add_help=False)
        parser.add_argument("--session-dir", default=None)
        parser.add_argument("--allow-root", action="append", default=[])
        try:
            args, _unknown = parser.parse_known_args(argv)
        except SystemExit:
            return 0
        session_dir = args.session_dir
        try:
            raw = stdin.read()
        except (OSError, ValueError):
            raw = ""
        try:
            from . import subagent_summary as _ss

            _ss.handle(raw, args.session_dir, args.allow_root)
        except BaseException:  # noqa: BLE001
            outcome = "error"
    except BaseException:  # noqa: BLE001
        return 0
    finally:
        _emit_feature_event(outcome, session_dir)
    return 0


if __name__ == "__main__":  # pragma: no cover
    try:
        main()
    except BaseException:  # noqa: BLE001
        pass
    sys.exit(0)

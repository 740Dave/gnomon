"""Headless child wrapper — ``claude -p`` under a process-tree MARKER (PTY-hooks WS-G).

    python -m gnomon.harness.headless <session_dir> --claude-bin B \\
        --output P --timeout T --depth D -- <prompt>

Why a wrapper and not a bare ``claude -p``: the wrapper STAYS the parent of the
headless claude, so anything that claude runs (its Bash tool, a nested
``agent_launch`` CLI) finds THIS process as its nearest gnomon ancestor
— and ``agent_launch`` refuses any launch from under a headless agent (interim
rail). A bare ``claude -p`` would inherit the LAUNCHER's ancestry and could
launch as if it were its parent.

Runs ``<claude> -p <prompt> --output-format text`` in ``session_dir`` (so the
profile's rendered ``.claude/CLAUDE.md`` + settings apply), with a hard timeout;
writes stdout to ``--output`` and echoes it; exits with claude's exit code
(124 on timeout).
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import List, Optional


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--" in argv:
        idx = argv.index("--")
        head, prompt_parts = argv[:idx], argv[idx + 1:]
    else:
        head, prompt_parts = argv, []
    parser = argparse.ArgumentParser(prog="python -m gnomon.harness.headless")
    parser.add_argument("session_dir")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--output", required=True)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--depth", type=int, default=0)  # informational; visible in argv
    # Transcript STOP-THE-LINE (2026-09-29): a pre-decided claude session id so
    # the headless transcript path is KNOWN (recorded in launch.json by agent_launch).
    parser.add_argument("--session-id", default=None)
    args = parser.parse_args(head)
    prompt = " ".join(prompt_parts)
    if not prompt.strip():
        sys.stderr.write("headless: empty prompt refused\n")
        return 2
    cmd = [args.claude_bin, "-p", prompt, "--output-format", "text"]
    if args.session_id:
        cmd += ["--session-id", args.session_id]
    # Security posture (D46 audit fix pass 2): the SAME catastrophic deny layer as a
    # harnessed child — deny rules + deny_guard hook via --settings. Nothing else in
    # the headless run changes. No deny layer -> refuse LOUD, never run without it.
    try:
        from .launcher import check_deny_guard_live, write_deny_layer_settings

        cmd += ["--settings", write_deny_layer_settings(args.session_dir)]
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"headless: refused — cannot write the deny layer settings: {exc}\n")
        return 2
    check_deny_guard_live(args.session_dir, os.path.basename(os.path.normpath(args.session_dir)),
                          cwd=args.session_dir)
    try:
        proc = subprocess.run(cmd, cwd=args.session_dir, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=args.timeout)
        out, err, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        err, code = f"headless: timed out after {args.timeout}s\n", 124
    except OSError as exc:
        out, err, code = "", f"headless: cannot run {args.claude_bin!r}: {exc}\n", 127
    try:
        os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(out or "")
    except OSError as exc:
        err = (err or "") + f"headless: cannot write output file: {exc}\n"
    sys.stdout.write(out or "")
    sys.stdout.flush()
    if err:
        sys.stderr.write(err)
    return code


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

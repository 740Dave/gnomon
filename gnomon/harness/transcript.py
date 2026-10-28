"""Transcript persistence + findability for every harnessed claude (STOP-THE-LINE 2026-09-29).

Why this exists (measured, not inferred):

* 3 of 8 harness-FRESH pilot sessions left an EMPTY Claude Code project dir
  (``~/.claude/projects/<mangled cwd>/`` held only ``memory/``). Their TUI logs
  (``<session_dir>/.claude/harness_stdout.log``) each carry Claude Code's own
  banner: *"Transcript saving is off — inherited CLAUDE_CODE_CHILD_SESSION
  marker"*. They were ``--terminal none`` launches, spawned straight out of the
  :8770 dashboard's env, which carries ``CLAUDE_CODE_CHILD_SESSION=1`` (it was
  started from inside a Claude Code tool call). An INTERACTIVE claude that sees
  that marker suppresses persistence (``claude -p`` does not — which is why the
  earlier ``-p`` A/B wrote a jsonl in both arms). The 5 sessions that DID persist
  were Terminal.app launches (fresh login env) or headless ``-p``.
* Nothing in the session dir said where the transcript lives, so the reviewer and
  the design-session tools could not find it deterministically.

The fix is two-sided and lives here + in :mod:`.spawn` / :mod:`.launcher`:

1. PERSISTENCE — ``sanitize_env`` now scrubs the marker (and its siblings), and the
   harnessed child additionally gets ``CLAUDE_CODE_FORCE_SESSION_PERSISTENCE=1``
   (Claude Code's own documented override for exactly this banner) as a belt to
   the scrub's braces.
2. FINDABILITY — the launcher decides the transcript location BEFORE spawning:
   a fresh child gets a pre-generated ``--session-id <uuid>`` so the jsonl path is
   KNOWN, not guessed from mtimes; a resumed child's path is its resume id under
   its original cwd's project dir. The result is written into ``launch.json`` and
   a ``<session_dir>/.claude/transcript.jsonl`` symlink points at the real file.

The project-dir name reuses :func:`.context_readers.claude_project_dir_name` (the
mangle of the child's RESOLVED cwd — a data root may be a symlink, so
the real dir is the mangled resolved path).

⚠ Claude Code hash-truncates project-dir names over 200 chars; this module does
NOT reproduce that tail, so for such names the recorded path is unreliable and
consumers should fall back to ``context_state.json``'s live pointer.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence

from .context_readers import _PROJECT_DIR_NAME_SOFT_LIMIT, claude_config_dir, claude_project_dir_name

#: Claude Code's documented override for the "Transcript saving is off —
#: inherited CLAUDE_CODE_CHILD_SESSION marker" banner (string verified in the
#: installed claude 2.1.284 binary: persistence-suppression returns false when set).
ENV_FORCE_PERSISTENCE = "CLAUDE_CODE_FORCE_SESSION_PERSISTENCE"

#: The stable in-session-dir pointer to the real transcript.
TRANSCRIPT_LINK_NAME = "transcript.jsonl"

#: The statusline hook's state file — it records the LIVE session_id /
#: transcript_path each render (so a ``/clear``-started successor is visible there).
LIVE_POINTER_FILENAME = "context_state.json"


def _extra_flag_value(extra: Sequence[str], flag: str) -> Optional[str]:
    """The value of ``flag`` in ``extra`` (``--flag v`` or ``--flag=v``), else None."""
    for i, item in enumerate(extra):
        if item == flag and i + 1 < len(extra):
            return extra[i + 1]
        if item.startswith(flag + "="):
            return item[len(flag) + 1:]
    return None


#: claude's continue/resume flags. A FRESH-mode launch whose ``extra_args`` carry
#: any of these is really a continuation, and claude REFUSES ``--session-id`` with
#: them unless ``--fork-session`` is also given (measured: exit 1, "Error:
#: --session-id can only be used with --continue or --resume if --fork-session is
#: also specified"). Matched as a bare token or the ``=`` form.
CONTINUE_RESUME_FLAGS = ("--continue", "-c", "--resume", "-r")
FORK_SESSION_FLAG = "--fork-session"


def _has_flag(extra: Sequence[str], flags: Sequence[str]) -> bool:
    return any(item == f or item.startswith(f + "=") for item in extra for f in flags)


def has_fork_session(extra: Sequence[str]) -> bool:
    """True when ``extra`` carries ``--fork-session`` (bare or ``=`` form)."""
    return _has_flag(extra, (FORK_SESSION_FLAG,))


def has_continue_or_resume(extra: Sequence[str]) -> bool:
    """True when ``extra`` carries ``--continue``/``-c``/``--resume``/``-r``."""
    return _has_flag(extra, CONTINUE_RESUME_FLAGS)


def plan_transcript(
    cfg: Dict[str, Any],
    child_cwd: str,
    *,
    projects_root: Optional[str] = None,
    new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> Dict[str, Any]:
    """Decide where THIS launch's transcript will land. Pure (no I/O beyond realpath).

    Returns the keys the launcher merges into ``launch.json``:

    * ``transcript_mode`` — ``"fresh"`` | ``"resume"`` | ``"resume-fork"`` |
      ``"continue"`` (fresh-mode launch whose extra_args continue/resume, no fork) |
      ``"continue-fork"`` (same, with ``--fork-session``);
    * ``transcript_cwd`` — the child's cwd, RESOLVED (what Claude Code mangles);
    * ``transcript_project_dir`` — ``<claude config>/projects/<mangled cwd>``;
    * ``session_id`` — the claude session id (fresh / any fork: pre-generated and
      passed as ``--session-id``, or the operator's own ``--session-id`` from
      extra_args; resume: the resumed id; continue WITHOUT fork: ``None`` — claude
      refuses ``--session-id`` there and picks the id itself);
    * ``transcript_path`` — ``<project dir>/<session_id>.jsonl`` or ``None``;
    * ``transcript_unknown_reason`` — why ``transcript_path`` is ``None`` (else ``None``);
    * ``inject_session_id`` — True when the launcher must add ``--session-id``.

    ⚠ LONG PROJECT-DIR NAMES: Claude Code truncates a mangled project-dir name
    longer than 200 chars and appends a hash tail. This function does NOT reproduce
    that tail, so for such names the recorded ``transcript_project_dir`` /
    ``transcript_path`` are unreliable; consumers must fall back to
    ``context_state.json``'s live pointer (``transcript_live_pointer``).

    NEVER injects ``--session-id`` on a non-fork resume/continue (claude exits 1).
    A stale ``cfg['session_id']`` (from a prior run's launch.json) is IGNORED here.
    """
    root = projects_root if projects_root is not None else os.path.join(claude_config_dir(), "projects")
    resolved = os.path.realpath(os.path.expanduser(child_cwd))
    project_dir = os.path.join(root, claude_project_dir_name(resolved))
    # NOTE: claude_project_dir_name() is the plain mangle. Past
    # _PROJECT_DIR_NAME_SOFT_LIMIT (200) chars Claude Code hash-truncates the name;
    # we deliberately do NOT reimplement that hash (unverifiable locally). The
    # recorded path is then a best guess — see transcript_exit_check's
    # 'long_project_dir_name' flag and the context_state.json live-pointer fallback.
    extra = [a for a in (cfg.get("extra_args") or []) if isinstance(a, str)]
    resume_id = cfg.get("resume_session_id") or None
    operator_sid = _extra_flag_value(extra, "--session-id")
    fork = has_fork_session(extra)

    inject = False
    reason: Optional[str] = None
    if resume_id:
        if fork:
            # A fork gets a NEW id; claude accepts --session-id alongside
            # --fork-session, so pre-decide it (operator's own wins).
            mode = "resume-fork"
            if operator_sid:
                sid = operator_sid
            else:
                sid, inject = new_id(), True
        else:
            # Plain resume: the transcript IS the resumed id. NEVER inject.
            mode, sid = "resume", resume_id
    elif has_continue_or_resume(extra):
        if fork:
            mode = "continue-fork"
            if operator_sid:
                sid = operator_sid
            else:
                sid, inject = new_id(), True
        else:
            # claude refuses --session-id with --continue/--resume sans fork, and
            # the id it continues is its own choice — the path is NOT knowable here.
            mode, sid = "continue", None
            reason = ("extra_args continue/resume without --fork-session: claude refuses "
                      "--session-id and chooses the session itself; use the live pointer "
                      "(context_state.json) for the transcript path")
    else:
        mode = "fresh"
        if operator_sid:
            sid = operator_sid
        else:
            sid, inject = new_id(), True
    return {
        "transcript_mode": mode,
        "transcript_cwd": resolved,
        "transcript_project_dir": project_dir,
        "session_id": sid,
        "transcript_path": os.path.join(project_dir, f"{sid}.jsonl") if sid else None,
        "transcript_unknown_reason": reason,
        "inject_session_id": inject,
    }


def record_transcript_location(session_dir: str, plan: Dict[str, Any]) -> Dict[str, Any]:
    """Merge the plan into ``launch.json`` (atomic) and point the symlink at it.

    A prior run's ``session_id`` (a re-run of the same session dir) is kept in
    ``previous_session_ids`` so re-launching never erases the older transcript's
    pointer. Returns the dict that was merged.
    """
    claude_dir = os.path.join(session_dir, ".claude")
    path = os.path.join(claude_dir, "launch.json")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    prev: List[str] = [p for p in (cfg.get("previous_session_ids") or []) if isinstance(p, str)]
    old = cfg.get("session_id")
    if isinstance(old, str) and old and old != plan.get("session_id") and old not in prev:
        prev.append(old)
    updates: Dict[str, Any] = {
        k: plan[k] for k in ("transcript_mode", "transcript_cwd", "transcript_project_dir",
                             "session_id", "transcript_path")
    }
    updates["transcript_unknown_reason"] = plan.get("transcript_unknown_reason")
    updates["transcript_link"] = os.path.join(claude_dir, TRANSCRIPT_LINK_NAME)
    updates["transcript_live_pointer"] = os.path.join(claude_dir, LIVE_POINTER_FILENAME)
    updates["transcript_recorded_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    if prev:
        updates["previous_session_ids"] = prev
    cfg.update(updates)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)

    link = updates["transcript_link"]
    try:
        if os.path.islink(link) or os.path.exists(link):
            os.remove(link)
        if plan.get("transcript_path"):
            # Dangling until claude writes its first event — by design: the target
            # is DECIDED, not discovered.
            os.symlink(plan["transcript_path"], link)
    except OSError:
        pass  # the launch.json record is the contract; the link is a convenience
    return updates


def transcript_exit_check(session_dir: str) -> Dict[str, Any]:
    """Post-run witness: does the recorded transcript exist, and how big is it?

    Also reports the statusline hook's LIVE pointer (a ``/clear`` successor).
    Written to ``launch.json`` as ``transcript_exit_check`` by the launcher. Never
    raises.
    """
    out: Dict[str, Any] = {"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    try:
        with open(os.path.join(session_dir, ".claude", "launch.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        out["error"] = "launch.json unreadable"
        return out
    tp = cfg.get("transcript_path")
    out["transcript_path"] = tp
    out["exists"] = bool(tp) and os.path.isfile(tp)
    out["bytes"] = os.path.getsize(tp) if out["exists"] else 0
    # Fail-loud status: "ok" | "missing" (a path was DECIDED and nothing is there)
    # | "unknown" (no path was knowable at launch — see transcript_unknown_reason).
    out["status"] = "ok" if out["exists"] else ("missing" if tp else "unknown")
    pd = cfg.get("transcript_project_dir")
    out["project_dir_exists"] = bool(pd) and os.path.isdir(pd)
    if pd and len(os.path.basename(pd)) > _PROJECT_DIR_NAME_SOFT_LIMIT:
        # Claude Code hash-truncates names this long; the recorded path is a guess.
        out["long_project_dir_name"] = True
    if out["status"] != "ok":
        out["fallback"] = ("recorded transcript not found — use live pointer "
                           "(context_state.json: live_transcript_path)")
    link = cfg.get("transcript_link") or os.path.join(session_dir, ".claude", TRANSCRIPT_LINK_NAME)
    out["link_present"] = os.path.islink(link)
    out["link_dangling"] = out["link_present"] and not os.path.exists(link)
    try:
        with open(os.path.join(session_dir, ".claude", LIVE_POINTER_FILENAME), "r", encoding="utf-8") as fh:
            live = json.load(fh)
        lp = live.get("transcript_path")
        out["live_session_id"] = live.get("session_id")
        out["live_transcript_path"] = lp
        out["live_exists"] = bool(lp) and os.path.isfile(lp)
    except (OSError, ValueError, AttributeError):
        pass
    return out


def record_exit_check(session_dir: str) -> Optional[Dict[str, Any]]:
    """Best-effort: write :func:`transcript_exit_check` into launch.json."""
    try:
        check = transcript_exit_check(session_dir)
        path = os.path.join(session_dir, ".claude", "launch.json")
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        cfg["transcript_exit_check"] = check
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        return check
    except (OSError, ValueError):
        return None

"""the Home tab "read" button's backend -- a read-only view of an agent def and a
ONE-OFF dry render of it ("Run Macro Expanders") into a throwaway temp dir.

WHAT A PREVIEW IS
    The SAME call ``LaunchManager.launch`` makes for an agent def
    (``agent_def.render_agent_def``, renderer/config/clock taken from the LaunchManager, event
    ``launch`` / ``resume`` exactly as a launch would pick), but with ``session_dir`` = a fresh
    ``tempfile.mkdtemp`` dir (``/var/folders/...`` on macOS) and ``record=False``. Every write
    ``render_agent_def`` makes is relative to ``session_dir`` (the numbered artifact, ``current.md``,
    the delivered ``.claude/CLAUDE.md``), so the redirect is complete by construction:
    nothing reaches the real rendered defs, the render manifest, ``~/.claude/agents`` or any
    real session dir. ``launch.json`` / provenance are NOT written (``record=False``; the launch
    path's own ``record_provenance`` step is simply not called). The render READS the lts corpora
    and ``~/.claude`` fragments / symbols like any launch; it never touches the lts write path.

IDS
    A preview is addressed by a preview id (the mkdtemp suffix, validated by ``_ID_RE``); an
    instance id is resolved ONLY through the ProfileStore (which validates it). No path ever
    comes from a request.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

PREFIX = "gm-defpreview-"
#: abandoned previews (tab closed, browser crash) older than this are swept.
MAX_AGE_S = 3600.0
#: mkdtemp suffixes are 8 chars of [a-z0-9_]; nothing else is ever accepted as an id.
_ID_RE = re.compile(r"[A-Za-z0-9_]{1,32}")


def preview_base(base: Optional[str] = None) -> str:
    return os.path.realpath(base or tempfile.gettempdir())


def _dir_for(preview_id: str, base: Optional[str]) -> Optional[str]:
    """The preview dir for an id, or None when the id is malformed / not a real dir under base."""
    if not isinstance(preview_id, str) or not _ID_RE.fullmatch(preview_id):
        return None
    b = preview_base(base)
    d = os.path.join(b, PREFIX + preview_id)
    if os.path.islink(d) or not os.path.isdir(d):
        return None
    if os.path.dirname(os.path.realpath(d)) != b:
        return None
    return d


def sweep(base: Optional[str] = None, *, max_age_s: float = MAX_AGE_S,
          now: Optional[float] = None) -> List[str]:
    """Delete preview dirs (prefix match, real dirs directly under base) older than ``max_age_s``.
    Never raises; returns the removed names."""
    removed: List[str] = []
    b = preview_base(base)
    t = time.time() if now is None else now
    try:
        names = os.listdir(b)
    except OSError:
        return removed
    for n in names:
        if not n.startswith(PREFIX):
            continue
        p = os.path.join(b, n)
        try:
            if os.path.islink(p) or not os.path.isdir(p):
                continue
            if t - os.path.getmtime(p) > max_age_s:
                shutil.rmtree(p, ignore_errors=True)
                removed.append(n)
        except OSError:
            continue
    return removed


def close(preview_id: str, base: Optional[str] = None) -> bool:
    """Delete one preview's temp dir. False when the id is malformed or names no preview."""
    d = _dir_for(preview_id, base)
    if d is None:
        return False
    shutil.rmtree(d, ignore_errors=True)
    return True


def macro_rows(expansions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-macro status rows. status: ok / empty-ok / failed (the expander's own vocabulary)."""
    rows = []
    for e in expansions or []:
        rows.append({"query": e.get("query"), "corpus": e.get("corpus"), "status": e.get("status"),
                     "matched": e.get("matched"), "shown": e.get("shown"), "withheld": e.get("withheld"),
                     "chars": e.get("chars"), "reason": e.get("reason")})
    return rows


def run_preview(profile: Any, manager: Any, *, base: Optional[str] = None,
                max_age_s: float = MAX_AGE_S) -> Dict[str, Any]:
    """Render ``profile``'s def through the launch path into a fresh temp dir and return the text +
    status. ``manager`` is the LaunchManager whose renderer / config / clock a launch would use."""
    from .agent_def import render_agent_def, superseded_paths
    from .profile import _validate_instance_id

    _validate_instance_id(profile.instance_id)
    sweep(base, max_age_s=max_age_s)
    root = tempfile.mkdtemp(prefix=PREFIX, dir=preview_base(base))
    preview_id = os.path.basename(root)[len(PREFIX):]
    try:
        resume_session_id = getattr(profile, "resume_session_id", None)
        resume_cwd = getattr(profile, "resume_cwd", None)
        # session dir named like the launcher's (<id>-<UTC stamp>) so ${ROLE} resolves identically
        stamp = manager._clock().strftime("%Y%m%d-%H%M%S")
        session_dir = os.path.join(root, f"{profile.instance_id}-{stamp}")
        os.makedirs(os.path.join(session_dir, ".claude"), exist_ok=True)
        rec = render_agent_def(
            session_dir, profile.claude_md_source,
            "resume" if resume_session_id else "launch",
            renderer=manager._renderer, clock=manager._clock, record=False, config=manager._config,
            mode="resume" if resume_session_id else "fresh",
            superseded=superseded_paths(resume_cwd) if resume_session_id else None,
        )
        delivered = os.path.join(session_dir, ".claude", "CLAUDE.md")
        try:
            with open(delivered, "r", encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            text = f"(preview render produced no readable def: {type(exc).__name__}: {exc})"
        expansions = list(rec.get("expansions") or [])
        rows = macro_rows(expansions)
        counts = {"ok": 0, "empty-ok": 0, "failed": 0}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        return {
            "preview_id": preview_id, "instance_id": profile.instance_id,
            "render_status": rec.get("status"), "reason": rec.get("reason"),
            "macros": rows, "counts": counts, "warnings": list(rec.get("warnings") or []),
            "pending": list(rec.get("pending") or []), "fragments": dict(rec.get("fragments") or {}),
            "symbols_used": list(rec.get("symbols_used") or []), "chars": len(text),
            "temp_dir": root, "text": text,
            "note": "dry render: written only under temp_dir; ${SESSION_ROOT} shows that temp session dir",
        }
    except BaseException:
        shutil.rmtree(root, ignore_errors=True)
        raise

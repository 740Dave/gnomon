"""Whose compaction was it? (flag ``subagent_compaction_check``, default OFF).

Claude Code runs the session's PreCompact and SessionStart(compact) hooks when a SUBAGENT's context is compacted too, so the
lifecycle hook alone cannot tell the two apart. The test is the session's own MAIN transcript (ruling ``fix-ruling-2026-10-27``):

* at ``pre-compact`` the hook records how many ``compact_boundary`` rows (``type`` system, ``subtype`` compact_boundary, not a
  sidechain) the main transcript holds, and the time (:func:`note_pre`);
* at ``post-compact`` it re-reads that file, retrying for up to :data:`WAIT_SECONDS`, and the event is the session's only if a
  NEW main-thread boundary has appeared, stamped at or after the pre event (:func:`verdict_post`).

Three answers: ``session`` (a new boundary: today's behaviour), ``subagent`` (none appeared: the caller applies no effect) and
``unknown`` (the main transcript, or the pre record, could not be read: the caller keeps today's behaviour and says why, because
missing a real compaction leaves an agent booted without its definition, which is worse than a needless boot prompt).

The main transcript is found the way every other reader of it finds it, ``operator_activity.resolve_transcript_path``: the
statusline hook's live pointer (``context_state.json``, written only by the session's own status line) else the launcher's
``transcript.jsonl`` link. Neither names a subagent file. The hook payload's ``agent_id``/``agent_type`` are logged as evidence
and never relied on.

The flag is read from the MACHINE config and the registry default only (never from the session's ``launch.json``). Nothing here
raises: every failure is an ``unknown`` answer or a no-op.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

FLAG_NAME = "subagent_compaction_check"
#: The one-line record text for the ignored case.
IGNORED_TEXT = "subagent compaction, ignored"
STATE_FILENAME = "compact-main-boundaries.json"
WAIT_SECONDS = 5.0
POLL_SECONDS = 0.25

#: Harness-side fallback (key before-flag-on-hook-timeout, item 1, corrected
#: ruling fallback-ruling-corrected-2026-10-27): with the flag ON, a post-compact-STARTED frame with no post-compact verdict frame
#: (session, unknown or ignored) within this many seconds is treated as if a plain post-compact frame had arrived. It runs from the
#: post-started frame, not from the pre frame: the pre-to-post gap of a real compaction is 21.9 s at the least (median 60.6 s, 140
#: recorded pairs), while the hook's verdict wait (WAIT_SECONDS) runs BEFORE it reports, so a hook killed at its timeout sends nothing.
FALLBACK_SECONDS = 15.0
#: Backstop, anchored on the PRE frame (a later addition): with the flag ON, if neither a post-compact-started frame nor any
#: post verdict frame arrives within this many seconds of a pre frame, the post-compact behaviour is applied once. 300 s is 2.5 times
#: the longest of the 140 recorded pre-to-post gaps (120.5 s). A started frame or a verdict frame disarms it (the 15 s bound then
#: governs). Beyond today's behaviour in one case: today a hook that never sends a post frame produces no boot prompt at all.
BACKSTOP_SECONDS = 300.0
#: How long a fired fallback remembers itself, so the same episode's late post frame is a no-op. After this a post frame is a
#: new episode's and is never swallowed (swallowing a real compaction's frame would lose its boot prompt).
FIRED_MEMORY_SECONDS = 300.0

SESSION = "session"
SUBAGENT = "subagent"
UNKNOWN = "unknown"


def enabled() -> bool:
    """Is the flag on? Never raises: any error reading it is OFF (today's behaviour)."""
    try:
        from .. import flags as flags

        return bool(flags.flag(FLAG_NAME))
    except Exception:  # noqa: BLE001
        return False


def _parse_ts(value: Any) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def main_boundaries(path: str) -> Optional[List[Optional[datetime]]]:
    """The timestamps (None where unparseable) of the main-thread ``compact_boundary`` rows in ``path``, in file order;
    ``None`` when the file cannot be read. A row counts when ``type`` is system, ``subtype`` is compact_boundary and
    ``isSidechain`` is not true."""
    out: List[Optional[datetime]] = []
    try:
        with open(path, "rb") as fh:
            for raw in fh:
                if b"compact_boundary" not in raw:        # cheap prefilter: transcripts run to tens of MB
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if (isinstance(obj, dict) and obj.get("type") == "system" and obj.get("subtype") == "compact_boundary"
                        and not obj.get("isSidechain")):
                    out.append(_parse_ts(obj.get("timestamp")))
    except OSError:
        return None
    return out


def _main_transcript(session_dir: str) -> Optional[str]:
    from .operator_activity import resolve_transcript_path

    return resolve_transcript_path(session_dir)


def _real(path: Optional[str]) -> Optional[str]:
    """``os.path.realpath`` of ``path``, or None when there is no path or it cannot be resolved. The launcher link
    (``.claude/transcript.jsonl``) keeps its own name when its TARGET changes, so the path alone cannot show a change."""
    if not path:
        return None
    try:
        return os.path.realpath(path)
    except Exception:  # noqa: BLE001
        return None


def _state_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", STATE_FILENAME)


def note_pre(session_dir: Optional[str], now: Optional[datetime] = None) -> bool:
    """PreCompact: record the main transcript's boundary count and the time. Never raises; False when nothing was recorded
    (the post step then answers ``unknown``)."""
    if not session_dir:
        return False
    try:
        path = _main_transcript(session_dir)
        found = main_boundaries(path) if path else None
        state: Dict[str, Any] = {"transcript": path, "real": _real(path), "count": (len(found) if found is not None else None),
                                 "pre_ts": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}  # floored to the ms transcript rows carry
        target = _state_path(session_dir)
        tmp = f"{target}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, target)
        return True
    except Exception:  # noqa: BLE001 - a hook never fails
        return False


def verdict_post(session_dir: Optional[str], *, wait: Optional[float] = None, poll: Optional[float] = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """PostCompact: ``{"verdict": session|subagent|unknown, "reason": str}``. Never raises."""
    try:
        wait = WAIT_SECONDS if wait is None else wait
        poll = POLL_SECONDS if poll is None else poll
        if not session_dir:
            return {"verdict": UNKNOWN, "reason": "no session dir"}
        try:
            with open(_state_path(session_dir), "r", encoding="utf-8") as fh:
                state = json.load(fh)
        except (OSError, ValueError):
            return {"verdict": UNKNOWN, "reason": "no pre-compact record"}
        pre_count = state.get("count") if isinstance(state, dict) else None
        pre_ts = _parse_ts(state.get("pre_ts")) if isinstance(state, dict) else None
        if not isinstance(pre_count, int) or pre_ts is None or not state.get("transcript") or not state.get("real"):
            return {"verdict": UNKNOWN, "reason": "pre-compact record incomplete (main transcript unread at pre)"}
        deadline = clock() + max(0.0, wait)
        while True:
            path = _main_transcript(session_dir)
            real = _real(path)
            if path != state.get("transcript") or real is None or real != state.get("real"):
                return {"verdict": UNKNOWN, "reason": "main transcript path changed since the pre-compact record"}
            found = main_boundaries(path)
            if found is None:
                return {"verdict": UNKNOWN, "reason": "main transcript unreadable"}
            if len(found) > pre_count and any(ts is None or ts >= pre_ts for ts in found[pre_count:]):
                return {"verdict": SESSION, "reason": f"new main-thread boundary ({pre_count} -> {len(found)})"}
            if clock() >= deadline:
                return {"verdict": SUBAGENT, "reason": f"no new main-thread boundary within {wait:g}s ({len(found)} rows, was {pre_count})"}
            sleep(poll)
    except Exception as exc:  # noqa: BLE001 - a hook never fails
        return {"verdict": UNKNOWN, "reason": f"{type(exc).__name__}: {exc}"}


def _clock() -> float:
    """Seconds on a monotonic clock. A module attribute so tests can drive it."""
    return time.monotonic()


class CompactFallback:
    """The harness-side fallback for a hook that never reports its post-compact frame (flag ``subagent_compaction_check``).

    Single-threaded by design: ``on_pre``/``on_post`` run in the actuator's lifecycle dispatch and ``tick`` is a pump ticker
    (the same thread, like the other tickers), so there is no timer thread and no lock, and it can neither block the relay nor
    raise into it. Flag OFF at the pre frame: nothing is armed and every method is a no-op returning today's answer.
    """

    BACKSTOP = "backstop"   # armed by the pre frame, BACKSTOP_SECONDS
    ARMED = "armed"         # armed by the post-compact-started frame, FALLBACK_SECONDS
    FIRED = "fired"

    def __init__(self, apply: Callable[[Dict[str, Any]], None], record: Callable[..., Any]) -> None:
        self._apply = apply        # applies today's post-compact behaviour for a plain post frame carrying this info
        self._record = record      # gate.record
        self._state: Optional[str] = None
        self._deadline = 0.0
        self._info: Dict[str, Any] = {}

    @property
    def state(self) -> Optional[str]:
        return self._state

    def on_pre(self, info: Dict[str, Any]) -> None:
        """A pre-compact frame arrived: a new episode. Arms only the long backstop (a compaction takes 22 to 120 s before its post
        hook starts, so the 15 s bound must not run from here), and only when the flag is ON. Never raises."""
        try:
            self._state = None
            self._info = {"trigger": info.get("trigger")}
            if enabled():
                self._deadline = _clock() + BACKSTOP_SECONDS
                self._state = self.BACKSTOP
        except Exception:  # noqa: BLE001
            self._state = None

    def on_started(self) -> None:
        """A post-compact-started frame arrived (the hook sends it first thing, flag ON only): disarm the backstop and arm the 15 s
        bound. After a fired fallback it is a no-op. Flag OFF in the harness: nothing is armed. Never raises."""
        try:
            if self._state == self.FIRED and _clock() - self._deadline < FIRED_MEMORY_SECONDS:
                return
            self._state = None
            if enabled():
                self._deadline = _clock() + FALLBACK_SECONDS
                self._state = self.ARMED
        except Exception:  # noqa: BLE001
            self._state = None

    def on_post(self) -> bool:
        """A post-compact frame arrived (any verdict). True = handle it as usual; False = the fallback already applied this
        episode's post-compact behaviour, so the frame is a no-op. Never raises."""
        try:
            state, self._state = self._state, None
            if state == self.FIRED and _clock() - self._deadline < FIRED_MEMORY_SECONDS:
                return False
        except Exception:  # noqa: BLE001
            self._state = None
        return True

    def tick(self) -> None:
        """Pump ticker: when the armed deadline has passed, apply the post-compact behaviour once. Never raises."""
        try:
            if self._state not in (self.ARMED, self.BACKSTOP) or _clock() < self._deadline:
                return
            reason = (f"no post-compact frame within {FALLBACK_SECONDS:g} s" if self._state == self.ARMED
                      else f"no post-compact frame within {BACKSTOP_SECONDS:g} s of pre")
            self._state = self.FIRED
            try:
                self._record("lifecycle", point="compact-fallback", decision="fallback", reason=reason,
                             payload={"phase": "post", "seconds": FALLBACK_SECONDS if reason.endswith(" s") else BACKSTOP_SECONDS})
            except Exception:  # noqa: BLE001
                pass
            try:
                self._apply(dict(self._info))
            except Exception as exc:  # noqa: BLE001 - never into the pump; say so in the log
                try:
                    self._record("lifecycle", point="compact-fallback", decision="failed", loud=True,
                                 reason=f"{type(exc).__name__}: {exc}"[:300], payload={"phase": "post"})
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

"""plan_usage — the account plan-usage reading (slice 1, read-only).

Claude Code's statusLine stdin carries ``rate_limits.five_hour`` and
``rate_limits.seven_day``, each ``{used_percentage, resets_at}`` (``resets_at`` is
epoch SECONDS; measured on Claude Code 2.1.x, see RUN_SUMMARY.md). The harness's
statusline hook (:mod:`.statusline_hook`) already receives that JSON for the
context gauge; it now ALSO persists the ``rate_limits`` block to
``<session_dir>/.claude/plan_usage_state.json`` (atomic replace). This module reads
that file back. It is the single contract shared by the ``plan_usage`` self-verb and
the Scheduler (spec 14)::

    read(state_path) -> {
      "five_hour": {"used_percentage": float|None, "resets_at": float|None, "reason": None|"unreadable"|"stale"|"clock-skew"},
      "seven_day": {... same ...},
      "reading_age_seconds": float|None,   # now - the hook's written_epoch
      "source": "statusline:rate_limits" | None,
    }

NEVER a guessed or zero value: a missing file, malformed JSON, an absent / non-numeric
field, or a reading older than ``STALE_AFTER_SECONDS`` (15 min) gives
``used_percentage: None`` plus a ``reason``. ``read`` never raises.

A ``written_epoch`` more than ``MAX_FUTURE_SKEW_SECONDS`` (120 s) in the future gives
``clock-skew`` (not fresh); a missing / non-numeric ``written_epoch`` gives ``unreadable``.
The file is opened non-blocking and read only if it is a REGULAR file of at most
``MAX_STATE_BYTES`` (a FIFO, device, or oversized file gives ``unreadable``, never a hang).

SCOPE: ``read()`` is THIS SESSION's reading: the state file is per session
(``<session_dir>/.claude``), so the reading is the last one THIS session's statusline
saw; ``reading_age_seconds`` shows how fresh it is. It is NOT an account-level
aggregate across sessions. The Scheduler needs an account-level reading (the freshest
across sessions): slice 2 / the scheduler spec item (see RUN_SUMMARY.md).
"""

from __future__ import annotations

import json
import math
import os
import stat
import time
from typing import Any, Dict, Optional

FLAG_NAME = "plan_usage_verb"  # gates the VERB only; the hook write and read() are unflagged
PLAN_USAGE_STATE_FILENAME = "plan_usage_state.json"
STALE_AFTER_SECONDS = 15 * 60
#: A written_epoch further than this into the future is a clock fault, not a fresh reading.
MAX_FUTURE_SKEW_SECONDS = 120
#: The state file is a few hundred bytes; anything larger is not ours and is never read whole.
MAX_STATE_BYTES = 64 * 1024
SOURCE = "statusline:rate_limits"
WINDOWS = ("five_hour", "seven_day")


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if math.isfinite(f) else None


def extract_rate_limits(payload: Any) -> Optional[Dict[str, Dict[str, Optional[float]]]]:
    """The ``{five_hour, seven_day}`` percentage/reset block from a statusline payload,
    or ``None`` when the payload carries no usable ``used_percentage`` for either window.
    Used by the statusline hook (writer side) so the writer and reader share one parse."""
    if not isinstance(payload, dict):
        return None
    rl = payload.get("rate_limits")
    if not isinstance(rl, dict):
        return None
    out: Dict[str, Dict[str, Optional[float]]] = {}
    any_pct = False
    for win in WINDOWS:
        w = rl.get(win)
        w = w if isinstance(w, dict) else {}
        pct = _num(w.get("used_percentage"))
        if pct is not None and not 0.0 <= pct <= 100.0:
            pct = None
        reset = _num(w.get("resets_at"))
        if reset is not None and reset <= 0:
            reset = None
        any_pct = any_pct or pct is not None
        out[win] = {"used_percentage": pct, "resets_at": reset}
    return out if any_pct else None


def write_state(state_path: str, rate_limits: Dict[str, Any], now: Optional[float] = None) -> None:
    body = {"rate_limits": rate_limits, "source": SOURCE,
            "written_epoch": time.time() if now is None else now}
    directory = os.path.dirname(state_path) or "."
    os.makedirs(directory, exist_ok=True)
    tmp = f"{state_path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(body, fh)
        fh.write("\n")
    os.replace(tmp, state_path)


def _null_result(reason: str, age: Optional[float] = None) -> Dict[str, Any]:
    return {
        "five_hour": {"used_percentage": None, "resets_at": None, "reason": reason},
        "seven_day": {"used_percentage": None, "resets_at": None, "reason": reason},
        "reading_age_seconds": age,
        "source": None,
    }


def default_state_path() -> Optional[str]:
    """``$GNOMON_SESSION_DIR/.claude/plan_usage_state.json`` when the env var is absolute, else None."""
    sd = os.environ.get("GNOMON_SESSION_DIR")
    if sd and os.path.isabs(sd):
        return os.path.join(sd, ".claude", PLAN_USAGE_STATE_FILENAME)
    return None


def _read_regular_capped(path: str) -> Optional[str]:
    """The file's text, or None when it is not a regular file or exceeds MAX_STATE_BYTES.
    O_NONBLOCK so opening a FIFO with no writer cannot hang; fstat on the OPENED fd (so a
    symlink to a FIFO/device is judged by its target, with no lstat/open race)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOCTTY", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_STATE_BYTES:
            return None
        data = b""
        while len(data) <= MAX_STATE_BYTES:
            chunk = os.read(fd, MAX_STATE_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
        if len(data) > MAX_STATE_BYTES:  # grew while we read
            return None
        return data.decode("utf-8")
    finally:
        os.close(fd)


def read(state_path: Optional[str] = None, *, now: Optional[float] = None,
         max_age_seconds: float = STALE_AFTER_SECONDS) -> Dict[str, Any]:
    """The plan-usage reading (module docstring has the contract). Never raises."""
    try:
        path = state_path or default_state_path()
        if not path:
            return _null_result("unreadable")
        try:
            raw = _read_regular_capped(path)
            obj = json.loads(raw) if raw is not None else None
        except (OSError, ValueError):
            return _null_result("unreadable")
        if raw is None:
            return _null_result("unreadable")
        if not isinstance(obj, dict):
            return _null_result("unreadable")
        written = _num(obj.get("written_epoch"))
        if written is None:
            return _null_result("unreadable")
        t = time.time() if now is None else now
        if written - t > MAX_FUTURE_SKEW_SECONDS:
            return _null_result("clock-skew", None)
        age = max(0.0, t - written)  # skew within tolerance clamps to 0
        rl = extract_rate_limits({"rate_limits": obj.get("rate_limits")})
        if rl is None:
            return _null_result("unreadable", age)
        stale = age > max_age_seconds
        out: Dict[str, Any] = {"reading_age_seconds": round(age, 3), "source": SOURCE}
        for win in WINDOWS:
            pct = rl[win]["used_percentage"]
            if pct is None:
                out[win] = {"used_percentage": None, "resets_at": rl[win]["resets_at"], "reason": "unreadable"}
            elif stale:
                out[win] = {"used_percentage": None, "resets_at": rl[win]["resets_at"], "reason": "stale"}
            else:
                out[win] = {"used_percentage": pct, "resets_at": rl[win]["resets_at"], "reason": None}
        return out
    except Exception:  # noqa: BLE001 - a read surface must never raise into a handler
        return _null_result("unreadable")

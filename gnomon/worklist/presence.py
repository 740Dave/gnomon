"""The presence line and the declared-status view (rule-presence-line, rule-presence-line-text, rule-context-verb).

The line is fixed text written by the harness: ``[Gnomon] <name>'s status: <in office | away[: text] | unreadable> (as told to <setter> at
<time>[; source: <source>])[; seen typing at a console <n> min ago <here | with <agent>> (Remote Control messages are not counted)]``. With no
status ever set it shows only the observed part (``[Gnomon] <name>: seen typing at a console ... (Remote Control messages are not counted)`` or
``[Gnomon] <name>: no console typing seen (Remote Control messages are not counted)``).

WORDING (round 4, step 1): the observed part is what the harness measures: keystrokes at a
console. Messages the operator sends through Remote Control never cross the console, so they are not seen. Wherever the observed part is shown it
says "at a console" and the line ends with the fixed clause :data:`REMOTE_CONTROL_CLAUSE`. A DECLARED status is unaffected.

ROUND 5 (g112): a reading that could not be taken is never worded as a reading of nothing: with the typing beacons unreadable the
line says ``console typing could not be read`` (never-set: ``[Gnomon] <name>: console typing could not be read (Remote Control messages are not
counted)``; after a declared status: ``...; console typing could not be read (Remote Control messages are not counted)``). The cut order when the
line would pass its bound is in :func:`presence_line`.

ROUND 3 (F1): the line is built ONLY from VALIDATED values (:func:`status.read_status` validates types, the enum, finite times, lengths and
control characters), is passed through :func:`text.clean_text` once more at the end, is exactly ONE line and is never longer than
``PRESENCE_LINE_MAX`` characters. OBSERVED activity is a fact with an age; it never changes the declared status and is never worded as
"back". Nothing in this module raises on a bad stored value.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from .status import Status
from .text import clean_text


def _when(ts: float) -> str:
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    except (ValueError, OverflowError, OSError):
        return "an unreadable time"


#: shown at the end of every line that reports (or denies, or could not read) observed typing: the harness does not see the operator's
#: Remote Control messages. SHORT on purpose: the line is bounded and rides on every wake-up (the notices carry the longer gloss).
REMOTE_CONTROL_CLAUSE = "(Remote Control messages are not counted)"
NO_CONSOLE_TYPING = "no console typing seen"
UNREADABLE_CONSOLE_TYPING = "console typing could not be read"
_ELLIPSIS = "…"
_MIN_KEPT = 8                       # the shortest a field is cut to (before the ellipsis) in step 3


def _observe(observed: Optional[Dict[str, Any]]) -> Tuple[str, int, Optional[str]]:
    """``(kind, minutes, agent)`` from an OperatorContext snapshot. ``kind``: ``"seen"`` (typing was seen; ``agent`` is None for THIS session),
    ``"none"`` (the beacons were read and nobody was typing) or ``"unreadable"`` (no snapshot, or one that says it could not read the beacons:
    a reading that could not be taken is never worded as a reading of nothing, g112)."""
    if observed is None or not isinstance(observed, dict) or observed.get("readable") is False:
        return "unreadable", 0, None
    best: Optional[tuple] = None
    here = observed.get("here")
    if isinstance(here, (int, float)) and not isinstance(here, bool) and math.isfinite(here):
        best = (float(here), None)
    for item in observed.get("others") or []:
        try:
            iid, secs = item
        except (TypeError, ValueError):
            continue
        if isinstance(secs, (int, float)) and not isinstance(secs, bool) and math.isfinite(secs) and (best is None or float(secs) < best[0]):
            best = (float(secs), clean_text(str(iid))[:64])
    if best is None:
        return "none", 0, None
    return "seen", int(max(0.0, best[0]) // 60), best[1]


def observed_part(observed: Optional[Dict[str, Any]]) -> str:
    """The observed part as the line shows it after a declared status: ``seen typing at a console <n> min ago <here | with <agent>>``, or
    ``console typing could not be read``, or ``""`` when the beacons were read and nothing was seen (a declared status then stands alone)."""
    kind, mins, who = _observe(observed)
    if kind == "seen":
        return f"seen typing at a console {mins} min ago " + ("here" if who is None else f"with {who}")
    return UNREADABLE_CONSOLE_TYPING if kind == "unreadable" else ""


def _shorten(s: str, over: int) -> str:
    """``s`` made at least ``over`` characters shorter (never below _MIN_KEPT kept characters), the cut marked with an ellipsis."""
    if len(s) <= _MIN_KEPT:
        return s
    keep = max(_MIN_KEPT, len(s) - over - 1)
    return s if keep >= len(s) else s[:keep].rstrip() + _ELLIPSIS


def presence_line(status: Status, observed: Optional[Dict[str, Any]], user: str) -> str:
    """One line, at most PRESENCE_LINE_MAX characters. When it would be longer it is cut in this order and NO other (g112, round 5): (1) the
    optional ``; source: ...`` part is dropped whole (left as ``; source: …``); (2) the declared status TEXT is shortened, marked with an ellipsis;
    (3) an over-long agent id, then the setter name, then the display name are shortened, marked. The words that carry the state ("in office",
    "away", "unreadable", "as told to"), the observed part and the closing clause are NEVER cut."""
    kind, mins, who = _observe(observed)
    f: Dict[str, Any] = {"user": clean_text(user or "")[:60] or "the operator", "who": who,
                         "text": clean_text(status.text or "") if status.declared and not (status.unknown or status.error) else "",
                         "setter": clean_text(str(status.setter_name or status.setter_id or "")),
                         "source": clean_text(status.source or ""), "src_cut": False}
    show_obs = (not status.declared) or kind in ("seen", "unreadable")
    tail = (" " + REMOTE_CONTROL_CLAUSE) if show_obs else ""

    def obs() -> str:
        if kind == "seen":
            return f"seen typing at a console {mins} min ago " + ("here" if f["who"] is None else f"with {f['who']}")
        return UNREADABLE_CONSOLE_TYPING if kind == "unreadable" else NO_CONSOLE_TYPING

    def build() -> str:
        if not status.declared:
            return clean_text(f"[Gnomon] {f['user']}: {obs()}") + tail
        if status.unknown or status.error:
            head = f"[Gnomon] {f['user']}'s status: unreadable (status unreadable; treated as not in office)"
        else:
            state = "in office" if status.state == "in-office" else ("away" + (f": {f['text']}" if f["text"] else ""))
            told = f" (as told to {f['setter']} at {_when(status.set_at)}"
            if f["source"]:
                told += "; source: …)" if f["src_cut"] else f"; source: {f['source']})"
            else:
                told += ")"
            head = f"[Gnomon] {f['user']}'s status: {state}{told}"
        return clean_text(head + (f"; {obs()}" if show_obs else "")) + tail

    line = build()
    for step in (1, 2, 3, 4, 5):
        over = len(line) - C.PRESENCE_LINE_MAX
        if over <= 0:
            break
        if step == 1 and f["source"] and not f["src_cut"]:
            f["src_cut"] = True
        elif step == 2 and f["text"]:
            f["text"] = _shorten(f["text"], over) if len(f["text"]) > _MIN_KEPT else _ELLIPSIS
        elif step == 3 and f["who"]:
            f["who"] = _shorten(f["who"], over)
        elif step == 4 and f["setter"]:
            f["setter"] = _shorten(f["setter"], over)
        elif step == 5:
            f["user"] = _shorten(f["user"], over)
        line = build()
    return line


def declared_view(status: Status, now: float, user: str) -> Dict[str, Any]:
    """The ``context`` verb's declared-status block (rule-context-verb). Never raises."""
    if not status.declared:
        return {"declared": False, "state": "in-office", "note": "no status has ever been set"}
    if status.unknown or status.error:
        return {"declared": True, "state": "unknown", "lost": bool(status.lost), "error": status.error,
                "note": "status unreadable: it is NOT treated as in office (holds stay silent); a new declaration recovers it"}
    age = max(0, int(now - status.set_at)) if (math.isfinite(now) and math.isfinite(status.set_at)) else 0
    out: Dict[str, Any] = {
        "declared": True, "state": status.state, "text": status.text, "set_by": status.setter_name or status.setter_id,
        "as_told_to": status.setter_name or status.setter_id, "set_at": _when(status.set_at), "source": status.source or None,
        "age_seconds": age,
    }
    if status.state == "away" and age > C.AWAY_NOT_RENEWED_SECONDS:
        out["not_renewed"] = True
        out["not_renewed_text"] = f"away status is {age // 3600} h old and not renewed; it has NOT expired"
    return out

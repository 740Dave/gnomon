"""Bin analysis over the DSD-answer corpus (pre-review B2): group the per-decision answer nodes by their plain scalar properties.

The acceptance query: ``recommended-position`` x ``option-position-chosen`` (in the aggregate), and the same cross-binned by day
(``decided-in-session``). Answer nodes minted BEFORE the fields existed (or with ``dsd_notice_inline`` OFF) do not carry them;
they are COUNTED and reported (``without_fields``), never rewritten (the corpus is machine-written). Reads the corpus FILE, the source of
record, the same file the live engine reads; the engine's own aggregate tool gives the same grouping over the same properties.

CONVENTIONS (also in PORT-LEDGER.md for the checklist rule): positions are 1-based; ``0`` means none (comment-only, the "Neither / None"
escape hatch, or no recommendation); ``seconds-from-sent-to-answered`` is a whole number or the string ``unknown``; ``answered-at`` is a UTC
instant (ISO 8601, ``Z``). Binning by day uses a STATED ``zone`` (an IANA name, default ``UTC``) applied to ``answered-at``; a node with no
``answered-at`` falls back to its ``decided-in-session`` date (the web job's local calendar date at the mint) and is COUNTED under
``day_from_decided_in_session``. The corpus is a DIRECTORY (one file per DSD plus the legacy file): the ONE shared reader
(:mod:`gnomon.dsd.answers_reader`) reads all of it and an unreadable path RAISES.

Headless, stdlib only.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

from .persist import ANSWER_FIELD_KEYS

def read_decision_nodes(path: str) -> Dict[str, Any]:
    """``{"nodes": [...], "files_read": [...]}`` for the whole corpus (the directory of per-DSD files plus the legacy file), through
    the ONE shared reader. Raises :class:`~gnomon.dsd.answers_reader.AnswersReadError` on an unreadable path."""
    from .answers_reader import read_answers

    return read_answers(path)


def _day_in_zone(props: Dict[str, str], zone: str) -> Tuple[str, bool]:
    """``(day, from_answered_at)``: the node's day in ``zone``."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    at = props.get("answered-at", "")
    try:
        dt = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(ZoneInfo(zone))
        return dt.date().isoformat(), True
    except (ValueError, TypeError):
        return props.get("decided-in-session", "?"), False


def group_counts(path: str, keys: Iterable[str] = ("recommended-position", "option-position-chosen"),
                 by_day: bool = False, zone: str = "UTC") -> Dict[str, Any]:
    """``{"total", "with_fields", "without_fields", "groups": {(k1, k2[, day]): n}}``. A node lacking any of
    :data:`~gnomon.dsd.persist.ANSWER_FIELD_KEYS` is counted under ``without_fields`` and left out of ``groups``."""
    keys = tuple(keys)
    read = read_decision_nodes(path)
    nodes = read["nodes"]
    if by_day:
        from zoneinfo import ZoneInfo

        ZoneInfo(zone)                       # a bad zone name raises here, before anything is counted
    groups: Dict[Tuple[str, ...], int] = {}
    without = 0
    fallback_days = 0
    for n in nodes:
        p = n["props"]
        if not all(k in p for k in ANSWER_FIELD_KEYS):
            without += 1
            continue
        day: Tuple[str, ...] = ()
        if by_day:
            d, from_at = _day_in_zone(p, zone)
            fallback_days += 0 if from_at else 1
            day = (d,)
        g = tuple(p[k] for k in keys) + day
        groups[g] = groups.get(g, 0) + 1
    return {"total": len(nodes), "with_fields": len(nodes) - without, "without_fields": without, "groups": groups,
            "files_read": read["files_read"], "zone": zone if by_day else None, "day_from_decided_in_session": fallback_days}

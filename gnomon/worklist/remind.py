"""When does an item remind? PURE functions of (item, declared status, now, workspace default). No clock, no I/O.

Rules (each cited in specs/15): every OPEN item reminds at its own interval or the workspace default (rule-who-reminds,
rule-default-applies-to-all) unless (a) its interval is ``none``, (b) a hold is EFFECTIVE, (c) it is paused. The next
reminder is due one interval after the latest of: the item's opening, its last update, its last reminder (rule-clock-anchor),
and, for an item whose hold was cleared by an in-office declaration, that declaration (rule-resume-one-interval-delay).
A hold is EFFECTIVE only when BOTH (a) the declared status is away AND (b) the item's waiting_on is the operator word (operator change
2026-10-11); an in-office declaration clears every hold (rule-hold-cleared-on-in-office).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import constants as C
from .status import Status

MISSING_AWAY = "no away status is declared"


def missing_operator(label: str = C.DEFAULT_OPERATOR_LABEL) -> str:
    return f"this item is not marked as waiting on {label}"


def hold_valid(item: Dict[str, Any], status: Status) -> bool:
    """The hold flag is set AND no in-office declaration came after it was set (that declaration clears it)."""
    return bool(item.get("hold")) and float(item.get("hold_ts") or 0.0) > status.last_in_office_at


def hold_missing(item: Dict[str, Any], status: Status, label: str = C.DEFAULT_OPERATOR_LABEL) -> List[str]:
    out = []
    if not status.silences_holds:
        out.append(MISSING_AWAY)
    if item.get("waiting_on") != C.WAIT_OPERATOR:
        out.append(missing_operator(label))
    return out


def hold_effective(item: Dict[str, Any], status: Status) -> bool:
    return hold_valid(item, status) and not hold_missing(item, status)


def anchor(item: Dict[str, Any], status: Status, now: Optional[float] = None) -> Optional[float]:
    """The latest of opening, last update, last reminder (and, for a cleared hold, the in-office time). With ``now`` given, a time that is
    in the FUTURE of the clock (the wall clock was stepped back) is ignored; if nothing is left the answer is None, meaning "the clock
    jumped: due now" (F-12). One reminder then re-anchors the item on the new clock."""
    cands = [float(item["opened"]), float(item["last_update"]), float(item["last_remind"] or 0.0)]
    if item.get("hold") and not hold_valid(item, status):
        cands.append(status.last_in_office_at)
    if now is not None:
        cands = [t for t in cands if t <= now]
        if not cands:
            return None
    return max(cands)


def backoff_seconds(streak: int, interval_min: Optional[int]) -> float:
    """The wait after ``streak`` denials in a row: 60 s, then doubling, never more than the item's own reminder interval."""
    if streak <= 0:
        return 0.0
    wait = C.VOID_RETRY_SECONDS * (2.0 ** min(int(streak) - 1, 40))
    return min(wait, float(interval_min) * 60.0) if interval_min is not None else wait


def evaluate(item: Dict[str, Any], status: Status, now: float, default_minutes: int,
             label: str = C.DEFAULT_OPERATOR_LABEL) -> Dict[str, Any]:
    """The derived view of one item: interval, source, next due, paused / held / silent flags, whether it is due now."""
    own = item.get("remind_every_min")
    none = own == "none"
    interval = default_minutes if own is None else (None if none else int(own))
    paused = (not item.get("standing")) and int(item["reminds_since_update"]) >= C.PAUSE_AFTER
    held = hold_effective(item, status)
    is_open = item["state"] == "open"
    reminding = is_open and not none and not paused and not held
    nxt = None
    if is_open and interval is not None:
        a = anchor(item, status, now)
        nxt = (a + interval * 60.0) if a is not None else now          # a clock stepped back: due now, once (F-12)
    streak = int(item.get("deny_streak", 0) or 0)
    denied_until = (float(item["last_deny"]) + backoff_seconds(streak, interval)) if (streak > 0 and item.get("last_deny") is not None) else None
    in_backoff = denied_until is not None and now < denied_until
    return {
        "deny_streak": streak, "denied_until": denied_until, "refused": is_open and streak >= C.DENY_FAULT_AFTER,
        "refused_reason": item.get("deny_reason"),
        "interval_min": interval, "interval_source": ("none" if none else ("default" if own is None else "own")),
        "next_due": nxt, "paused": is_open and paused, "held": is_open and held,
        "hold_stored_not_effective": is_open and hold_valid(item, status) and not held,
        "hold_missing": hold_missing(item, status, label) if (is_open and hold_valid(item, status)) else [],
        "no_reminder": is_open and none, "reminding": reminding,
        "due": bool(reminding and nxt is not None and now >= nxt and not in_backoff),
        "reminders_24h": sum(1 for t in item["remind_ts"] if t > now - 86400.0),
        "short_interval": bool(is_open and interval is not None and interval < 60),
    }


def due_items(items: Dict[str, Dict[str, Any]], status: Status, now: float, default_minutes: int) -> List[Dict[str, Any]]:
    """Open items due NOW, in opening order (several due at once are ONE message: rule-one-message)."""
    return [it for it in items.values() if it["state"] == "open"
            and evaluate(it, status, now, default_minutes)["due"]]


def waiting_label(waiting_on: str, label: str = C.DEFAULT_OPERATOR_LABEL) -> str:
    if waiting_on == "self":
        return "self"
    if waiting_on == C.WAIT_OPERATOR:
        return label
    kind, _, rest = waiting_on.partition(":")
    return f"agent {rest}" if kind == "agent" else (f"process {rest}" if kind == "process" else waiting_on)


def reminder_line(item: Dict[str, Any], interval_min: int, label: str = C.DEFAULT_OPERATOR_LABEL) -> str:
    """rule-fixed-text. The only agent-written part is the title."""
    return (f"[Gnomon] reminder: {item['title']} (item {item['id']}, waiting on {waiting_label(item['waiting_on'], label)}, "
            f"every {interval_min} min). Update it, raise its interval, or mark it done.")


def reminder_message(due: List[Dict[str, Any]], status: Status, now: float, default_minutes: int,
                     label: str = C.DEFAULT_OPERATOR_LABEL) -> str:
    """ONE message for all items due: one fixed line per item, in order."""
    return "\n".join(reminder_line(it, evaluate(it, status, now, default_minutes)["interval_min"], label) for it in due)

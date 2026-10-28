"""READ functions for the Work tab (slice 2 is UI only) and for the DSD system (rule-dsd-read). Pure: no routes, no writes.

Cross-agent read (rule-cross-agent-read) lives ONLY here. Each function takes the data root and a ``now`` so a test can fake both.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from typing import Any, Dict, Iterable, List, Optional

from . import constants as C
from . import remind as R
from .safeio import read_regular
from .settings import resolve as resolve_settings
from .status import read_status
from .store import Store, store_dir, valid_instance_id

NO_ITEMS = "no open items"
VERBS_UNAVAILABLE = "verbs not available"


def _marker_path(data_root: str, instance_id: str) -> str:
    return os.path.join(store_dir(data_root), instance_id + ".verbs-unavailable.json")


def mark_verbs_unavailable(data_root: str, instance_id: str, reason: str, now: float) -> None:
    """Written at launch when the session cannot call the work verbs (rule-verbs-unavailable-is-loud). Cleared by
    :func:`clear_verbs_unavailable` at the next launch that can."""
    os.makedirs(store_dir(data_root), mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=store_dir(data_root), prefix=".vu-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"reason": reason, "at": now}, fh)
    os.replace(tmp, _marker_path(data_root, instance_id))


def clear_verbs_unavailable(data_root: str, instance_id: str) -> None:
    try:
        os.unlink(_marker_path(data_root, instance_id))
    except OSError:
        pass


def tools_line_lacks_verbs(tools_line: Optional[str], verbs: Iterable[str]) -> List[str]:
    """The verbs NOT named in a definition's ``tools:`` line (a line of None = no fixed line = nothing lacking)."""
    if not tools_line:
        return []
    have = {t.strip() for t in tools_line.replace("mcp__gnomon-self__", "").split(",")}
    return [v for v in verbs if v not in have]


def known_agents(data_root: str) -> List[str]:
    try:
        names = os.listdir(store_dir(data_root))
    except OSError:
        return []
    out = set()
    for n in names:
        if n.endswith(".jsonl") and valid_instance_id(n[:-6]):
            out.add(n[:-6])
        elif n.endswith(".verbs-unavailable.json") and valid_instance_id(n[:-len(".verbs-unavailable.json")]):
            out.add(n[:-len(".verbs-unavailable.json")])
    return sorted(out)


_DSD_ID = re.compile(C.DSD_ID_PATTERN)


def _is_dsd_id(v: Any) -> bool:
    return v is None or (isinstance(v, str) and _DSD_ID.match(v) is not None)


def _item_row(it: Dict[str, Any], ev: Dict[str, Any], now: float, label: str = C.DEFAULT_OPERATOR_LABEL) -> Dict[str, Any]:
    return {
        "id": it["id"], "title": it["title"], "waiting_on": it["waiting_on"], "waiting_text": R.waiting_label(it["waiting_on"], label),
        "age_seconds": max(0, int(now - it["opened"])),
        "last_reminder_age_seconds": (None if it["last_remind"] is None else max(0, int(now - it["last_remind"]))),
        "interval_min": ev["interval_min"], "interval_source": ev["interval_source"],
        "last_reminder_epoch": it["last_remind"], "standing": it["standing"], "paused": ev["paused"],
        "paused_text": "paused: no update after 8 reminders" if ev["paused"] else None,
        "held": ev["held"], "held_text": "held until return" if ev["held"] else None,
        "hold_stored_not_effective": ev["hold_stored_not_effective"],
        "hold_stored_text": (("hold stored, not in effect yet: " + "; ".join(ev["hold_missing"]))
                             if ev["hold_stored_not_effective"] else None),
        "refused": ev["refused"], "denied_in_a_row": ev["deny_streak"],
        "refused_text": ((f"reminders for this item are being refused ({ev['deny_streak']} denials in a row)"
                          + (f": {ev['refused_reason']}" if ev["refused_reason"] else "")) if ev["refused"] else None),
        "no_reminder": ev["no_reminder"], "no_reminder_text": "no reminder" if ev["no_reminder"] else None,
        "reminders_24h": ev["reminders_24h"], "short_interval_mark": ev["short_interval"],
        "blocked_by": it["blocked_by"], "blocked_by_is_dsd_id": _is_dsd_id(it["blocked_by"]), "says_requested_by": it["requested_by"], "note": it["note"],
        "dsd_label": (it["blocked_by"] or "no DSD filed") if it["waiting_on"] == C.WAIT_OPERATOR else None,
    }


def agent_detail(data_root: str, instance_id: str, now: float, deploy_root: Optional[str] = None,
                 operator_label: str = C.DEFAULT_OPERATOR_LABEL) -> Dict[str, Any]:
    """One agent's row for the Work tab. ``state`` is one of ok | 'no open items' | 'verbs not available' | 'list not intact'."""
    row: Dict[str, Any] = {"instance_id": instance_id, "read_at_epoch": now, "items": [], "open": 0, "waiting_on_operator": 0,
                           "paused": 0, "held": 0, "reminders_24h": 0}
    marker = _marker_path(data_root, instance_id)
    if os.path.lexists(marker):
        raw, _why = read_regular(marker, 16 * 1024)
        try:
            row["verbs_unavailable_reason"] = str(json.loads(raw.decode("utf-8")).get("reason")) if raw is not None else "unreadable marker"
        except (ValueError, UnicodeDecodeError, AttributeError):
            row["verbs_unavailable_reason"] = "unreadable marker"
        row["state"] = VERBS_UNAVAILABLE
        return row
    st = Store(data_root, instance_id)
    r, items, _ = st.read(readonly=True)          # a web read creates nothing (no lock file)
    if not r.ok:
        row["state"] = "list not intact"
        row["error"] = r.error
        return row
    status, default = read_status(data_root), resolve_settings(instance_id, deploy_root).default_minutes
    rows = []
    for it in items.values():
        if it["state"] != "open":
            continue
        ev = R.evaluate(it, status, now, default, operator_label)
        rows.append(_item_row(it, ev, now, operator_label))
    row.update(items=rows, open=len(rows), waiting_on_operator=sum(1 for x in rows if x["waiting_on"] == C.WAIT_OPERATOR),
               paused=sum(1 for x in rows if x["paused"]), held=sum(1 for x in rows if x["held"]),
               reminders_24h=sum(x["reminders_24h"] for x in rows), log_bytes=r.size,
               log_warning=("the list's log is over 5 MB" if r.size > C.LOG_WARN_BYTES else None))
    row["state"] = "ok" if rows else NO_ITEMS
    return row


def agents_overview(data_root: str, now: float, deploy_root: Optional[str] = None,
                    extra_agents: Iterable[str] = (), operator_label: str = C.DEFAULT_OPERATOR_LABEL) -> Dict[str, Any]:
    """Every agent with a list (plus ``extra_agents``, which show as 'no open items' when they have none), and the cross-agent
    'Waiting on you' list, oldest first. A broken log or a missing-verbs agent is reported as such, never as empty."""
    ids = sorted(set(known_agents(data_root)) | {a for a in extra_agents if valid_instance_id(a)})
    agents = [agent_detail(data_root, i, now, deploy_root, operator_label) for i in ids]
    waiting = []
    for a in agents:
        for it in a["items"]:
            if it["waiting_on"] == C.WAIT_OPERATOR:
                waiting.append({"agent": a["instance_id"], **it})
    waiting.sort(key=lambda x: -x["age_seconds"])
    return {"read_at_epoch": now, "agents": agents, "waiting_on_operator": waiting}


def blocked_by_dsd(data_root: str, dsd_id: str, now: float, deploy_root: Optional[str] = None,
                   agents: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """The 'blocks work' read for the DSD system: EXACTLY the open items that name ``dsd_id`` as blocked-by (urgency is
    derived: the count and the oldest item's age; there is no stored urgency)."""
    out_items: List[Dict[str, Any]] = []
    unreadable: List[str] = []
    for aid in (list(agents) if agents is not None else known_agents(data_root)):
        r, items, _ = Store(data_root, aid).read(readonly=True)
        if not r.ok:
            unreadable.append(aid)
            continue
        for it in items.values():
            if it["state"] == "open" and it["blocked_by"] == dsd_id:
                out_items.append({"agent": aid, "id": it["id"], "title": it["title"], "waiting_on": it["waiting_on"],
                                  "age_seconds": int(now - it["opened"])})
    out_items.sort(key=lambda x: -x["age_seconds"])
    return {"dsd_id": dsd_id, "count": len(out_items), "oldest_age_seconds": (out_items[0]["age_seconds"] if out_items else None),
            "items": out_items, "unreadable_agents": unreadable}


def operator_status(data_root: str, now: float, user: str = C.DEFAULT_OPERATOR_LABEL) -> Dict[str, Any]:
    """The declared status for the Work tab / any reader: validated, never raises. ``state`` is ``in-office``, ``away`` or ``unknown``
    (lost / unreadable: NOT in office; the fault is stated; only a new declaration recovers it)."""
    from .presence import declared_view
    from .status import read_status

    try:
        return declared_view(read_status(data_root), now, user)
    except Exception as exc:  # noqa: BLE001
        return {"declared": True, "state": "unknown", "error": f"status view failed ({type(exc).__name__})"}


# --- the Work tab (slice 2): two pure read functions; the routes only call these -------------------------------------------------

def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    try:
        return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, OverflowError, OSError):
        return ""


def _agent_summary(a: Dict[str, Any], retired: bool) -> Dict[str, Any]:
    """The left-hand row of the tab: counts and badges, no item rows."""
    out = {k: a.get(k) for k in ("instance_id", "state", "open", "waiting_on_operator", "paused", "held", "reminders_24h", "log_bytes",
                                 "log_warning", "error", "verbs_unavailable_reason")}
    out["retired"] = bool(retired)
    out["short_interval_items"] = sum(1 for i in a.get("items", []) if i.get("short_interval_mark"))
    return out


def work_overview(data_root: str, now: float, deploy_root: Optional[str] = None, known: Optional[Iterable[str]] = None,
                  operator_label: str = C.DEFAULT_OPERATOR_LABEL) -> Dict[str, Any]:
    """Everything the Work tab's left side and its top list need, in one read. ``known`` = the instance ids Gnomon still knows as agents
    (profiles): an agent with a list that is not in it is ``retired``; an agent in it with no list shows ``no open items``. ``known=None``
    means "unknown": nothing is marked retired. Never raises."""
    known_set = {k for k in (known or []) if valid_instance_id(k)}
    ov = agents_overview(data_root, now, deploy_root, extra_agents=known_set, operator_label=operator_label)
    agents = []
    for a in ov["agents"]:
        retired = known is not None and a["instance_id"] not in known_set
        agents.append(_agent_summary(a, retired))
    waiting = [{"agent": w["agent"], "id": w["id"], "title": w["title"], "age_seconds": w["age_seconds"], "blocked_by": w["blocked_by"],
                "blocked_by_is_dsd_id": w["blocked_by_is_dsd_id"], "dsd_label": w["dsd_label"], "waiting_text": w["waiting_text"]} for w in ov["waiting_on_operator"]]
    return {"ok": True, "read_at_epoch": now, "read_at": _iso(now), "operator_label": operator_label,
            "status": operator_status(data_root, now, operator_label), "agents": agents, "waiting_on_you": waiting,
            "total_open": sum(int(a["open"] or 0) for a in agents)}


def work_agent(data_root: str, instance_id: str, now: float, deploy_root: Optional[str] = None,
               operator_label: str = C.DEFAULT_OPERATOR_LABEL, known: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """One agent's full right-hand pane: its row plus every open item. ``ok`` False + ``error`` for an unusable instance id."""
    if not valid_instance_id(instance_id):
        return {"ok": False, "error": "not a valid agent id"}
    row = agent_detail(data_root, instance_id, now, deploy_root, operator_label)
    known_set = {k for k in (known or []) if valid_instance_id(k)}
    row["retired"] = known is not None and instance_id not in known_set
    row.update(ok=True, read_at_epoch=now, read_at=_iso(now), operator_label=operator_label)
    return row

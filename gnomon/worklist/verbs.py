"""The five work verbs (rule-verb-set): work_add, work_update, work_done, work_drop, work_list.

A WorkVerbs object is bound to ONE instance id (the calling session's own, supplied by the harness, never by the caller): it
can only read and write that agent's log (rule-owner-only-writes). Each verb returns the item as stored; a refused call says
why and changes nothing (rule-verb-returns). Times come from the injected clock (rule-times-from-clock).
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import constants as C
from . import remind as R
from .settings import WorkSettings, resolve as resolve_settings
from .status import Status, read_status
from .store import LogError, Refusal, Store, valid_instance_id
from .text import clean_text

_LINK_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_DSD_ID = re.compile(C.DSD_ID_PATTERN)
_FIELDS = ("title", "waiting_on", "remind_every_min", "standing", "hold_until_return", "links", "note")


def iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def refused(why: str, **extra: Any) -> Dict[str, Any]:
    return {"ok": False, "decision": "refused", "changed": False, "error": why, **extra}


def check_waiting_on(v: Any, aliases: Optional[Dict[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """``(normalised value, error)``. An alias from the workspace / deployment data is normalised to the neutral operator form."""
    aliases = aliases or {}
    allowed = C.WAITING_FORMS + (f" (also accepted as the operator: {', '.join(sorted(aliases))})" if aliases else "")
    if not isinstance(v, str):
        return None, f"waiting_on must be a string: {allowed}"
    if v in ("self", C.WAIT_OPERATOR):
        return v, None
    if v in aliases:
        return aliases[v], None
    kind, sep, rest = v.partition(":")
    if kind == "agent" and sep and valid_instance_id(rest):
        return v, None
    if kind == "process" and sep:
        if not rest.strip() or len(rest) > C.PROCESS_TEXT_MAX or clean_text(rest) != rest:
            return None, f"waiting_on process text must be 1 to {C.PROCESS_TEXT_MAX} characters with no control characters"
        return v, None
    return None, f"waiting_on {v!r} is not one of: {allowed}"


def check_interval(v: Any) -> Tuple[Optional[Any], Optional[str]]:
    """``(stored value, error)``: an int minutes, ``"none"``, or ``None`` (use the workspace default)."""
    if v is None or v == "default":
        return None, None
    if v == "none":
        return "none", None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not v.is_integer()):
        return None, (f"remind_every_min must be a whole number of minutes from {C.MIN_INTERVAL_MIN} to "
                      f"{C.MAX_INTERVAL_MIN}, or 'none' (got {v!r})")
    n = int(v)
    if n < C.MIN_INTERVAL_MIN or n > C.MAX_INTERVAL_MIN:
        return None, (f"remind_every_min {n} is outside {C.MIN_INTERVAL_MIN} to {C.MAX_INTERVAL_MIN} minutes; nothing changed")
    return n, None


class WorkVerbs:
    def __init__(self, instance_id: str, data_root: str, clock: Callable[[], float], *, deploy_root: Optional[str] = None,
                 record: Optional[Callable[..., None]] = None, operator_label: str = C.DEFAULT_OPERATOR_LABEL) -> None:
        self.instance_id = instance_id
        self.label = operator_label
        self.data_root = data_root
        self._clock = clock
        self._deploy_root = deploy_root
        self._record = record or (lambda *a, **k: None)
        self.store = Store(data_root, instance_id)

    # -- shared -----------------------------------------------------------------------------------------------------------
    def _settings(self) -> WorkSettings:
        return resolve_settings(self.instance_id, self._deploy_root)

    def _default(self) -> int:
        return self._settings().default_minutes

    def view(self, it: Dict[str, Any], status: Status, now: float, default: int) -> Dict[str, Any]:
        ev = R.evaluate(it, status, now, default, self.label)
        out = {
            "id": it["id"], "title": it["title"], "state": it["state"], "waiting_on": it["waiting_on"],
            "remind_every_min": ("none" if ev["no_reminder"] else (None if ev["interval_source"] == "default" else ev["interval_min"])),
            "interval_min": ev["interval_min"], "interval_source": ev["interval_source"],
            "standing": it["standing"], "hold_until_return": R.hold_valid(it, status), "hold_effective": ev["held"],
            "paused": ev["paused"], "reminders_since_update": it["reminds_since_update"],
            "next_reminder": iso(ev["next_due"] if ev["reminding"] else None),
            "blocked_by": it["blocked_by"], "says_requested_by": it["requested_by"], "note": it["note"],
            "opened": iso(it["opened"]), "last_update": iso(it["last_update"]), "last_reminder": iso(it["last_remind"]),
            "closed": iso(it["closed"]),
        }
        if ev["hold_stored_not_effective"]:
            out["hold_missing"] = ev["hold_missing"]
        if ev["paused"]:
            out["paused_text"] = "paused: no update after 8 reminders"
        return out

    def _hold_note(self, views: List[Dict[str, Any]]) -> Optional[str]:
        parts = []
        for v in views:
            if v.get("hold_missing"):
                parts.append(f"{v['id']}: " + "; ".join(v["hold_missing"]))
        if not parts:
            return None
        return ("hold stored but NOT effective yet (" + " | ".join(parts) + "). The item keeps reminding; the hold takes "
                "effect as soon as an away status is declared AND the item is waiting on the operator.")

    def _load(self) -> Tuple[Optional[Dict[str, Any]], Dict[str, Dict[str, Any]], int]:
        r, items, adds = self.store.read()
        if not r.ok:
            return refused(r.error), {}, 0
        return None, items, adds

    def _fields(self, args: Dict[str, Any], *, adding: bool) -> Tuple[Dict[str, Any], Optional[str]]:
        chg: Dict[str, Any] = {}
        if "title" in args and args["title"] is not None:
            t = args["title"]
            if not isinstance(t, str):
                return {}, "title must be a string"
            c = clean_text(t)
            if not c:
                return {}, "title is empty"
            if len(c) > C.TITLE_MAX:
                return {}, f"title is {len(c)} characters; the limit is {C.TITLE_MAX} (it is never cut: shorten it)"
            chg["title"] = c
        if "waiting_on" in args and args["waiting_on"] is not None:
            norm, err = check_waiting_on(args["waiting_on"], self._settings().aliases)
            if err:
                return {}, err
            chg["waiting_on"] = norm
        if "remind_every_min" in args:
            val, err = check_interval(args["remind_every_min"])
            if err:
                return {}, err
            chg["remind_every_min"] = val
        for k in ("standing", "hold_until_return"):
            if k in args and args[k] is not None:
                if not isinstance(args[k], bool):
                    return {}, f"{k} must be a boolean"
                chg[k] = args[k]
        if "note" in args and args["note"] is not None:
            if not isinstance(args["note"], str):
                return {}, "note must be a string"
            n = clean_text(args["note"])
            if len(n) > C.NOTE_MAX:
                return {}, f"note is over {C.NOTE_MAX} characters"
            chg["note"] = n
        links = args.get("links")
        if links is not None:
            if not isinstance(links, dict) or set(links) - {"blocked_by", "requested_by"}:
                return {}, "links must be an object with only blocked_by (a DSD id) and/or requested_by"
            for k, name in (("blocked_by", "blocked_by"), ("requested_by", "requested_by")):
                if k in links and links[k] is not None:
                    if k == "blocked_by":
                        if not isinstance(links[k], str) or not _DSD_ID.match(links[k]):
                            return {}, "links.blocked_by must be a DSD id (dsd- followed by 6 to 32 lowercase hex digits)"
                    elif not isinstance(links[k], str) or not _LINK_ID.match(links[k]):
                        return {}, f"links.{k} must be a short id (letters, digits, . _ -)"
                    chg[name] = links[k]
        return chg, None

    # -- the verbs --------------------------------------------------------------------------------------------------------
    def work_add(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(args.get("title"), str) or args.get("waiting_on") is None:
            return refused("title and waiting_on are required (waiting_on: " + C.WAITING_FORMS + ")")
        chg, err = self._fields(args, adding=True)
        if err:
            return refused(err)
        now = float(self._clock())
        made: List[str] = []

        def build(items: Dict[str, Dict[str, Any]], next_id: int) -> Tuple[str, Dict[str, Any]]:
            # ONE critical section (round 3, F2): the id and the open-item limit are decided under the same lock as the append.
            if sum(1 for i in items.values() if i["state"] == "open") >= C.MAX_OPEN_ITEMS:
                raise Refusal(f"you already have {C.MAX_OPEN_ITEMS} open items; mark some done or drop them first")
            item_id = f"w-{next_id}"
            made.append(item_id)
            return "add", {"id": item_id, "title": chg["title"], "waiting_on": chg["waiting_on"],
                           "remind_every_min": chg.get("remind_every_min"), "standing": chg.get("standing", False),
                           "hold_until_return": chg.get("hold_until_return", False), "blocked_by": chg.get("blocked_by"),
                           "requested_by": chg.get("requested_by"), "note": chg.get("note")}
        try:
            self.store.transact(now, build, allow_restart=True)
        except Refusal as exc:
            return refused(str(exc))
        except (LogError, OSError) as exc:
            return refused(f"could not write your list: {exc}")
        return self._reply_for(made, now)

    def _norm_ids(self, args: Dict[str, Any], items: Dict[str, Dict[str, Any]]) -> List[str]:
        raw = args.get("ids", args.get("id"))
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw or not all(isinstance(x, str) for x in raw):
            raise Refusal("id (one item id) or ids (a list of item ids) is required")
        ids = list(dict.fromkeys(raw))
        for i in ids:
            if i not in items:
                raise Refusal(f"no item {i!r} on your list")
            if items[i]["state"] != "open":
                raise Refusal(f"item {i} is already {items[i]['state']}")
        return ids

    def _reply_for(self, ids: List[str], now: float) -> Dict[str, Any]:
        _, items, _ = self.store.read()
        status, default = read_status(self.data_root), self._default()
        views = [self.view(items[i], status, now, default) for i in ids if i in items]
        out: Dict[str, Any] = {"ok": True, "changed": True}
        if len(views) == 1:
            out["item"] = views[0]
        else:
            out["items"] = views
        note = self._hold_note(views)
        if note:
            out["hold_note"] = note
        lost = self.store.take_notice()
        if lost:
            out["notice"] = lost
        return out

    def work_update(self, args: Dict[str, Any]) -> Dict[str, Any]:
        chg, err = self._fields(args, adding=False)
        if err:
            return refused(err)
        if not chg:
            return refused("give at least one field to update (a note is enough): title, waiting_on, remind_every_min, "
                           "standing, hold_until_return, links, note")
        now = float(self._clock())
        done: List[str] = []

        def build(items: Dict[str, Dict[str, Any]], next_id: int) -> Tuple[str, Dict[str, Any]]:
            ids = self._norm_ids(args, items)
            if len(ids) > 1 and "title" in chg:
                raise Refusal("title can be changed on one item at a time")
            done.extend(ids)
            return "update", {"ids": ids, "changes": dict(chg)}
        try:
            self.store.transact(now, build)
        except Refusal as exc:
            return refused(str(exc))
        except (LogError, OSError) as exc:
            return refused(f"could not write your list: {exc}")
        return self._reply_for(done, now)

    def _close(self, op: str, args: Dict[str, Any], why_key: str, required: bool) -> Dict[str, Any]:
        why = args.get(why_key)
        if why is not None and not isinstance(why, str):
            return refused(f"{why_key} must be a string")
        why = clean_text(why or "")
        if required and not why:
            return refused(f"{why_key} is required")
        if len(why) > C.NOTE_MAX:
            return refused(f"{why_key} is over {C.NOTE_MAX} characters")
        now = float(self._clock())
        i = args.get("id")

        def build(items: Dict[str, Dict[str, Any]], next_id: int) -> Tuple[str, Dict[str, Any]]:
            if not isinstance(i, str) or i not in items:
                raise Refusal(f"no item {i!r} on your list")
            if items[i]["state"] != "open":
                raise Refusal(f"item {i} is already {items[i]['state']}")
            return op, {"id": i, why_key: why or None}
        try:
            self.store.transact(now, build)
        except Refusal as exc:
            return refused(str(exc))
        except (LogError, OSError) as exc:
            return refused(f"could not write your list: {exc}")
        return self._reply_for([i], now)

    def work_done(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._close("done", args, "note", False)

    def work_drop(self, args: Dict[str, Any]) -> Dict[str, Any]:
        return self._close("drop", args, "reason", True)

    def work_list(self, args: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        bad, items, _ = self._load()
        if bad:
            return bad
        now = float(self._clock())
        status, cfg = read_status(self.data_root), self._settings()
        default, fault = cfg.default_minutes, cfg.fault
        open_views = [self.view(i, status, now, default) for i in items.values() if i["state"] == "open"]
        out = {"ok": True, "items": open_views, "open": len(open_views),
               "closed": sum(1 for i in items.values() if i["state"] != "open"),
               "paused": sum(1 for v in open_views if v["paused"]), "held": sum(1 for v in open_views if v["hold_effective"]),
               "default_interval_min": default}
        out["default_interval_source"] = cfg.default_source
        if fault:
            out["settings_fault"] = fault
        lost = self.store.take_notice()
        if lost:
            out["notice"] = lost
        return out

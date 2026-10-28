"""The per-session runtime of the open-work list: verbs, the declared status, reminders, the presence line, the context view.

Built by the launcher ONLY when ``open_work_list`` is ON (the key ``open_work_list`` is present in the resolved harness
settings only then). Everything it needs from the harness is injected, so a test drives it with a fake clock and a fake
injector and never touches a PTY, the hub or a live data root.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from . import api
from . import constants as C
from . import remind as R
from .presence import declared_view, presence_line
from .settings import resolve as resolve_settings
from .status import OPERATOR_SETTER, read_status, validate_set, write_status
from .store import Refusal, valid_instance_id
from .verbs import WorkVerbs, refused

WORK_VERB_NAMES = ("work_add", "work_update", "work_done", "work_drop", "work_list")
STATUS_VERB = "operator_status_set"
ALL_VERB_NAMES = WORK_VERB_NAMES + (STATUS_VERB,)
#: sources whose messages WAKE an agent and so carry the presence line (rule-presence-line-covers). NOT the operator's own typed or
#: broadcast text, NOT a peer's bus message, NOT a self-verb inject.
PRESENCE_SOURCES = frozenset({"harness", "resume", "monitor-guidance", "dsd-answer"})
REMINDER_SOURCE = "harness"
#: the gate decisions under which the reminder IS delivered: written now (allow, modify), or HELD by the gate and released exactly once later
#: (defer: the modal-dialog / half-typed-line / pause hold queue), plus "typed" for an injector that reports none. A DEFER must count: the
#: gate types it on release, and un-counting it would let the next tick send a second one for the same due time. Only a DENY (nothing
#: written, nothing held) and a refusal are NOT delivered.
DELIVERED_DECISIONS = frozenset({"allow", "modify", "defer", "typed"})
REMINDER_TRIGGER = "work-reminder"


class WorkRuntime:
    def __init__(self, *, instance_id: str, data_root: str, clock: Callable[[], float],
                 record: Optional[Callable[..., Any]] = None, user_name: str = "the operator",
                 deploy_root: Optional[str] = None) -> None:
        if not valid_instance_id(instance_id):
            raise ValueError(f"open-work list: invalid instance id {instance_id!r}")
        self.instance_id = instance_id
        self.data_root = data_root
        self._clock = clock
        self._record = record or (lambda *a, **k: None)
        self.user = user_name
        self._deploy_root = deploy_root
        self.verbs = WorkVerbs(instance_id, data_root, clock, deploy_root=deploy_root, record=self._record, operator_label=user_name)
        # wired by attach()
        self._inject: Optional[Callable[..., Any]] = None
        self._monitor: Any = None
        self._observed: Optional[Callable[[], Dict[str, Any]]] = None
        self._boot_blocks: Optional[Callable[[], bool]] = None
        self._gate_paused: Optional[Callable[[], bool]] = None
        self._last_tick: Optional[float] = None
        self._last_error: Optional[str] = None
        self._held_reported = False
        self._remind_fault: Optional[str] = None
        self._status_fault: Optional[str] = None

    def attach(self, *, inject: Callable[..., Any], monitor: Any, observed: Optional[Callable[[], Dict[str, Any]]] = None,
               boot_blocks: Optional[Callable[[], bool]] = None, gate_paused: Optional[Callable[[], bool]] = None) -> None:
        self._inject, self._monitor, self._observed = inject, monitor, observed
        self._boot_blocks, self._gate_paused = boot_blocks, gate_paused

    # -- verbs ------------------------------------------------------------------------------------------------------------
    def handle(self, cmd: str, frame: Dict[str, Any]) -> Dict[str, Any]:
        """One self-verb frame -> reply. The agent is ALWAYS the calling session's own instance id: a frame that names an
        agent is refused (rule-owner-only-writes)."""
        if cmd not in ALL_VERB_NAMES:
            return refused(f"unknown work verb {cmd!r}")
        for k in ("instance_id", "agent", "owner", "setter_id"):
            if k in frame:
                return refused(f"{k} is not accepted: these verbs act on YOUR OWN list/identity only")
        args = {k: v for k, v in frame.items() if k != "cmd"}
        if cmd == STATUS_VERB:
            return self.set_status_verb(args)
        out = getattr(self.verbs, cmd)(args)
        self._record("work-verb", verb=cmd, decision=("ok" if out.get("ok") else "refused"),
                     item=(out.get("item") or {}).get("id"), error=out.get("error"))
        return out

    # -- the declared status ----------------------------------------------------------------------------------------------
    def _apply_status(self, args: Dict[str, Any], *, role_setter: bool, setter_id: str, setter_name: str) -> Dict[str, Any]:
        v = validate_set(args.get("state"), args.get("text"), args.get("source"), role_setter=role_setter)
        if not v["ok"]:
            self._record("operator-status", decision="refused", setter=setter_id, error=v["error"])
            return refused(v["error"])
        now = float(self._clock())
        st = write_status(self.data_root, state=v["state"], text=v["text"], source=v["source"], setter_id=setter_id,
                          setter_name=setter_name, now=now)
        # audited in the setter's injection log (the log text is bounded); sends NO message to any agent
        self._record("operator-status", decision="set", state=v["state"], setter=setter_id, text=v["text"][:200],
                     source=v["source"][:200], role_setter=role_setter)
        return {"ok": True, "changed": True, "status": declared_view(st, now, self.user)}

    def set_status_verb(self, args: Dict[str, Any]) -> Dict[str, Any]:
        setters = resolve_settings(self.instance_id, self._deploy_root).setters
        if self.instance_id not in setters:
            self._record("operator-status", decision="refused", setter=self.instance_id,
                         error="not in the allowed-setter list")
            return refused("operator_status_set is not available to this session (only the operator and the sessions in the "
                           "workspace's allowed-setter list may declare his status)")
        return self._apply_status(args, role_setter=True, setter_id=self.instance_id, setter_name=setters[self.instance_id])

    def operator_command(self, args: str) -> str:
        """The operator's own command (``\\presence away <text>`` / ``\\presence in-office`` / ``\\presence back``)."""
        toks = (args or "").strip().split(None, 1)
        if not toks or toks[0] not in ("away", "in-office", "back"):
            st = read_status(self.data_root)
            return (f"usage: \\presence away <text> | in-office (or back). Now: {declared_view(st, float(self._clock()), self.user)}")
        state = "away" if toks[0] == "away" else "in-office"
        out = self._apply_status({"state": state, "text": toks[1] if len(toks) > 1 else ""}, role_setter=False,
                                 setter_id=OPERATOR_SETTER, setter_name=self.user)
        return "declared: " + state if out.get("ok") else "refused: " + str(out.get("error"))

    def status(self):
        st = read_status(self.data_root)
        self._note_status_fault(st)
        return st

    def _note_status_fault(self, st) -> None:
        """A lost / unreadable status is recorded visibly, once per distinct reason (it stays 'unknown', never in-office)."""
        reason = st.error if (st.unknown or st.error) else None
        if reason != self._status_fault:
            self._status_fault = reason
            if reason:
                self._record("operator-status", decision="unreadable", reason=reason, state="unknown", loud=True)

    def presence_fault(self, info: Dict[str, Any]) -> None:
        """Called by the Injector when the gate refused a wake-up BECAUSE of the presence line and it was re-sent without it."""
        self._record("presence-line", decision="refused-resent-without", loud=True, **{k: str(v)[:200] for k, v in info.items()})
    def settings_view(self) -> Dict[str, Any]:
        """The effective settings for this agent (default interval, source, aliases, workspace, faults); read-only."""
        s = resolve_settings(self.instance_id, self._deploy_root)
        return {"default_interval_min": s.default_minutes, "default_source": s.default_source, "aliases": dict(s.aliases),
                "workspace": s.workspace, "faults": list(s.faults)}

    # -- presence ---------------------------------------------------------------------------------------------------------
    def presence_line(self) -> Optional[str]:
        obs = None
        if self._observed is not None:
            try:
                obs = self._observed()
            except Exception:  # noqa: BLE001 - an unreadable beacon directory is "nothing observed"
                obs = None
        st = self.status()
        return presence_line(st, obs, self.user)

    def wrap_context(self, base: Callable[[], Dict[str, Any]]) -> Callable[[], Dict[str, Any]]:
        """The ``context`` verb's ``operator`` field: unchanged, plus the declared status."""
        def _field() -> Dict[str, Any]:
            out = dict(base())
            try:
                out["declared_status"] = declared_view(self.status(), float(self._clock()), self.user)
            except Exception as exc:  # noqa: BLE001 - the operator block of the context verb must still return
                out["declared_status"] = {"declared": True, "state": "unknown", "error": f"status view failed ({type(exc).__name__})"}
            return out
        return _field

    # -- reminders --------------------------------------------------------------------------------------------------------
    def tick(self, now: Optional[float] = None) -> Optional[str]:
        """Called by the pump. Returns the message typed (or None). Never raises into the pump."""
        try:
            return self._tick(now)
        except Exception as exc:  # noqa: BLE001 - the list can never break the pump
            self._record("work-reminder", decision="error", error=f"{type(exc).__name__}: {exc}"[:300], loud=True)
            return None

    def _tick(self, now: Optional[float]) -> Optional[str]:
        now = float(self._clock()) if now is None else float(now)
        if self._last_tick is not None and 0 <= now - self._last_tick < C.TICK_SECONDS:
            return None
        self._last_tick = now
        r, items, _ = self.verbs.store.read()
        if not r.ok:
            if r.error != self._last_error:
                self._last_error = r.error
                self._record("work-reminder", decision="log-not-intact", error=r.error, loud=True)
            return None
        self._last_error = None
        if not items:
            return None
        status = self.status()
        default = resolve_settings(self.instance_id, self._deploy_root).default_minutes
        due = R.due_items(items, status, now, default)
        if not due or self._inject is None:
            return None
        mon = self._monitor
        if mon is not None and not getattr(mon, "primed", True):
            return None
        if self._gate_paused is not None and self._gate_paused():
            return None
        if self._boot_blocks is not None and self._boot_blocks():
            return None
        if mon is not None:
            q = mon.seconds_quiet()
            if q is not None and q < C.QUIET_SECONDS:
                return None
            hold = mon.line_hold_reason()
            if hold is not None:
                if not self._held_reported:
                    self._held_reported = True
                    self._record("work-reminder", decision="held", reason=hold)
                return None
        self._held_reported = False
        # THE RESERVATION IS THE CLAIM (R4-1, R6-1). Under the agent's list lock, the log is re-read AFTER the lock is held and the due items are
        # decided again from that fresh read; the claim is a `remind` record written in the same critical section. A process (another session of
        # this agent) that finds the item already claimed finds it no longer due (the record moved its anchor) and writes and types NOTHING.
        # The lock is released before anything is typed. If the lock cannot be taken within the bounded wait, or the record cannot be written,
        # nothing is typed on this tick and one fault is recorded; the item is still due and the next tick tries again. A reminder is therefore
        # typed at most once for one (item, due time) across all processes (at worst a crash between the record and the typing loses one
        # reminder, which is counted).
        claimed: List[Dict[str, Any]] = []

        def claim(fresh_items: Dict[str, Dict[str, Any]], _next_id: int):
            mine = R.due_items(fresh_items, status, now, default)
            if not mine:
                raise Refusal("nothing is due (another session of this agent already claimed it)")
            claimed.extend(dict(i) for i in mine)
            return "remind", {"ids": [i["id"] for i in mine],
                              "due": {i["id"]: R.evaluate(i, status, now, default)["next_due"] for i in mine}}
        try:
            self.verbs.store.transact(now, claim)
        except Refusal:
            return None                                            # someone else holds the claim: type nothing, record nothing
        except Exception as exc:  # noqa: BLE001 - LogError, OSError, anything: no record, no reminder
            msg = f"{type(exc).__name__}: {exc}"[:300]
            if msg != self._remind_fault:
                self._remind_fault = msg
                self._record("work-reminder", decision="not-recorded-not-typed", ids=[i["id"] for i in due], error=msg, loud=True)
            return None
        self._remind_fault = None
        ids = [i["id"] for i in claimed]
        text = R.reminder_message(claimed, status, now, default, self.user)
        lost = self.verbs.store.take_notice()
        if lost:
            text = "[Gnomon] " + lost + "\n" + text
        decision, why = "error", ""
        try:
            out = self._inject(text, clear=False, source=REMINDER_SOURCE, trigger=REMINDER_TRIGGER)
            decision = str(getattr(getattr(out, "decision", None), "value", "typed"))
            why = str(getattr(out, "reason", "") or "")
        except Exception as exc:  # noqa: BLE001 - a refused / failed reminder is recorded, not retried every tick
            decision = f"refused: {type(exc).__name__}"
            why = str(exc)
            self._record("work-reminder", decision="refused", ids=ids, error=str(exc)[:300], loud=True)
        self._record("work-reminder", decision=decision, ids=ids)
        if decision in DELIVERED_DECISIONS:
            return text
        # NOT delivered (the gate denied it, or the write was refused): the reservation is marked so, and it counts for nothing (the pause
        # counts DELIVERED reminders). The item is still due; the next try waits 60 s, then twice as long after each further denial in a row,
        # never more than the item's own interval (remind.backoff_seconds; derived from the log, so every session agrees).
        why = " ".join(why.split())[:200]
        try:
            self.verbs.store.append("unremind", {"ids": ids, "at": now, "why": why}, now)
        except Exception as exc:  # noqa: BLE001 - the reservation then stays counted; say so, loudly
            self._record("work-reminder", decision="not-delivered-not-unmarked", ids=ids, error=f"{type(exc).__name__}: {exc}"[:300], loud=True)
            return None
        refused_now = [i["id"] for i in claimed if int(i.get("deny_streak", 0) or 0) + 1 == C.DENY_FAULT_AFTER]
        if refused_now:                                              # ONE fault record per run of denials: the 8th in a row
            self._record("work-reminder", decision="reminders-refused", ids=refused_now, loud=True,
                         error=("reminders for this item are being refused" + (f": {why}" if why else ""))[:300])
        return None

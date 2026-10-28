"""The scheduler engine for sub-stage 1a: propose / confirm-once / pin, the 30 s tick, the inject run state machine,
and the hash-chained receipts (spec 14 +-*). Pure of web and hub: the registry, clock, time zone,
turn probe and effect-ledger state are injected, so every behaviour is testable with fakes. No thread starts here.

RUN STATE MACHINE (one inject fire), persisted in ``state/<id>.json`` so a restart resumes it:

  slot due -> [classify lateness] -> pre-send checks, IN THIS ORDER (owner ruling 2026-10-09):
      0. target live?            no  -> SKIPPED target-not-live (nothing queued, no session started)
      1. compaction ordering     compacting (a compact event under 30 min old, no boot 'written' after it) / log
                                 unreadable -> WAIT (re-check each minute, at most 30 min, then SKIPPED
                                 target-log-unreadable; a stale compact stops holding at 30 min and the inject sends)
      2. one-shot late window    measured from the FIRE time; a compaction wait does NOT extend it
                                 (>= 1 h late -> MISSED one-shot-too-late)
      3. plan-usage band         plan_usage.plan_usage_band(): "run" today (injects exempt until the reader exists)
  -> SEND (SessionRegistry.route, the ONE inject module) -> poll the target's injection_log for the run's unique
     trigger -> DELIVERED | DELIVERED-QUEUED | HELD (until released; there is NO expiry and NO drop) | stays SENT.

Nothing is queued durably for a dead target. Confirming never fires. Everything the scheduler writes is atomic or
chained; a receipt is written for every run, skip, miss and refusal.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import inject as _inject
from . import model as _model
from . import plan_usage as _plan_usage
from . import schedule as _schedule
from . import store as _store
from . import text as _text
from . import vocab as V
from .limits import LIMITS
from .model import Refusal
from .store import SchedulerPaths, store_lock


def _iso(epoch: float) -> str:
    return _dt.datetime.fromtimestamp(float(epoch), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_of(iso: str) -> float:
    return _dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()


def _default_turn_state(session_dir: Any, now: float) -> str:
    """idle | busy | unknown. ``unknown`` is NEVER read as idle (relaunch.turn_state returns it when it cannot tell)."""
    try:
        from .. import relaunch

        if not isinstance(session_dir, str):
            return "unknown"
        return relaunch.turn_state(relaunch.transcript_path(session_dir), now)[0]
    except Exception:  # noqa: BLE001
        return "unknown"


class Scheduler:
    def __init__(self, root: str, *, registry: Any, clock: Callable[[], float] = time.time,
                 tz: Optional[_dt.tzinfo] = None, turn_state: Optional[Callable[[Any, float], str]] = None,
                 effect_ledger_on: Optional[Callable[[], Optional[bool]]] = None,
                 nonce: Optional[Callable[[], str]] = None) -> None:
        self.paths = SchedulerPaths(root)
        self._registry = registry          # a registry object, or a zero-arg callable returning one
        self._clock = clock
        self._tz = tz
        self._turn_state = turn_state or _default_turn_state
        self._ledger_on = effect_ledger_on or (lambda: None)
        self._nonce = nonce or (lambda: secrets.token_hex(3))
        self._tick_lock = threading.Lock()
        self.ticks = 0

    # ------------------------------------------------------------------ helpers
    def _reg(self) -> Any:
        r = self._registry
        return r() if callable(r) and not hasattr(r, "route") else r

    def now(self) -> float:
        return float(self._clock())

    def tz(self) -> _dt.tzinfo:
        return self._tz or _schedule.local_tz()

    def _audit(self, verb: str, job_id: Optional[str], actor: str, verified: bool, outcome: str,
               detail: Optional[Dict[str, Any]] = None) -> None:
        rec = {"ts": _iso(self.now()), "verb": verb, "job_id": job_id, "actor": actor,
               "actor_verified": bool(verified), "outcome": outcome}
        if detail:
            rec["detail"] = detail
        try:
            _store.append_chained(self.paths.audit, rec)
        except OSError as exc:
            sys.stderr.write(f"[scheduler] audit write FAILED ({exc}); the {verb} call is not on the audit log\n")

    def _job(self, job_id: str) -> Optional[Dict[str, Any]]:
        return _store.read_json(self.paths.job(job_id))

    def _proposal(self, job_id: str) -> Optional[Dict[str, Any]]:
        return _store.read_json(self.paths.proposal(job_id))

    def _state(self, job_id: str) -> Dict[str, Any]:
        st = _store.read_json(self.paths.job_state(job_id))
        return st if isinstance(st, dict) else {"anchor": None, "next_due": None, "done": False, "active": None}

    def _save_state(self, job_id: str, st: Dict[str, Any]) -> None:
        st["schema_version"] = V.SCHEMA_VERSION
        _store.write_json(self.paths.job_state(job_id), st)

    def _active_counts(self, exclude_id: Optional[str]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        ids = set(_store.list_ids(self.paths.jobs)) | set(_store.list_ids(self.paths.proposals))
        for i in ids:
            if i == exclude_id:
                continue
            owner = None
            prop = None
            try:
                prop = self._proposal(i)
            except ValueError:
                prop = None
            if prop and not prop.get("removal"):
                owner = prop.get("proposer")
            else:
                try:
                    job = self._job(i)
                    st = self._state(i)
                except ValueError:
                    continue
                if job:
                    unfinished_one_shot = _schedule.is_one_shot(job["trigger"]) and not st.get("done")
                    if (job.get("enabled") and not st.get("done")) or unfinished_one_shot:
                        owner = job.get("owner")
            if owner:
                counts[owner] = counts.get(owner, 0) + 1
        return counts

    # ------------------------------------------------------------------ propose / remove (the agent verbs)
    def propose(self, args: Any, *, proposer: Optional[str], verified: bool) -> Dict[str, Any]:
        now = self.now()
        job_id = args.get("job_id") if isinstance(args, dict) else None
        with store_lock(self.paths):
            existing_owner = None
            if isinstance(job_id, str) and _store.ID_RE.match(job_id):
                for loader in (self._job, self._proposal):
                    try:
                        rec = loader(job_id)
                    except ValueError:
                        rec = None
                    if rec:
                        existing_owner = rec.get("owner") or rec.get("proposer")
            try:
                defn = _model.validate_proposal(args, proposer=proposer, verified=verified, now=now,
                                                active_counts=self._active_counts(job_id if isinstance(job_id, str) else None),
                                                existing_owner=existing_owner, tz=self.tz())
            except Refusal as r:
                self._audit("sched_propose", job_id if isinstance(job_id, str) else None, proposer or "unidentified",
                            verified, "refused", {"field": r.field, "reason": r.reason[:300], **r.detail})
                return r.as_result()
            n_props = sum(1 for i in _store.list_ids(self.paths.proposals)
                          if (self._safe(self._proposal, i) or {}).get("proposer") == proposer)
            if n_props >= LIMITS["max_proposals_per_owner"] and not os.path.exists(self.paths.proposal(defn["id"])):
                r = Refusal("limit.max_proposals_per_owner", "too many pending proposals", asked=n_props + 1,
                            allowed=LIMITS["max_proposals_per_owner"])
                self._audit("sched_propose", defn["id"], proposer, verified, "refused", {"reason": r.reason})
                return r.as_result()
            prop = {"schema_version": V.SCHEMA_VERSION, "id": defn["id"], "proposer": proposer,
                    "proposer_verified": True, "proposed_at": _iso(now), "owner": proposer,
                    "change_of_confirmed": os.path.exists(self.paths.job(defn["id"])),
                    "definition": defn, "definition_sha256": _model.definition_sha256(defn)}
            _store.write_json(self.paths.proposal(defn["id"]), prop)
            self._audit("sched_propose", defn["id"], proposer, verified, "ok", {"change": prop["change_of_confirmed"]})
        first = _schedule.next_fires(defn["trigger"], now, 1, tz=self.tz(), anchor=now)
        return {"ok": True, "job_id": defn["id"], "status": "pending-confirm",
                "note": "Nothing is scheduled and nothing will fire until the operator confirms this in the Scheduler tab "
                        "(Mac password or Touch ID). An agent cannot confirm.",
                "schedule": _schedule.describe(defn["trigger"]),
                "first_fire_utc": _iso(first[0]) if first else None}

    @staticmethod
    def _safe(fn: Callable[..., Any], *a: Any) -> Any:
        try:
            return fn(*a)
        except (ValueError, OSError):
            return None

    def propose_remove(self, job_id: Any, *, proposer: Optional[str], verified: bool) -> Dict[str, Any]:
        if not verified or not proposer:
            self._audit("sched_propose_remove", job_id if isinstance(job_id, str) else None, "unidentified", False,
                        "refused", {"reason": "caller not verified"})
            return {"ok": False, "refused": True, "error": "the caller is not verified (the hub caller binding is off "
                                                          "or did not identify this session)"}
        if not isinstance(job_id, str) or not _store.ID_RE.match(job_id):
            self._audit("sched_propose_remove", None, proposer, True, "refused", {"reason": "invalid job id",
                                                                                  "given": repr(job_id)[:60]})
            return {"ok": False, "refused": True, "field": "job_id", "error": "job_id: not a valid job id"}
        with store_lock(self.paths):
            job, prop = self._safe(self._job, job_id), self._safe(self._proposal, job_id)
            if not job and not prop:
                self._audit("sched_propose_remove", job_id, proposer, True, "refused", {"reason": "unknown id"})
                return {"ok": False, "refused": True, "error": f"no job or pending proposal {job_id!r}"}
            owner = (job or prop).get("owner") or (prop or {}).get("proposer")
            if owner != proposer:
                self._audit("sched_propose_remove", job_id, proposer, True, "refused", {"reason": "not the owner"})
                return {"ok": False, "refused": True, "error": f"{job_id!r} is not your job"}
            if prop and not job:
                _store.remove_quiet(self.paths.proposal(job_id))
                self._audit("sched_propose_remove", job_id, proposer, True, "ok", {"effect": "pending proposal withdrawn"})
                return {"ok": True, "job_id": job_id, "status": "withdrawn",
                        "note": "Your pending proposal was withdrawn at once; nothing had been installed."}
            rm = {"schema_version": V.SCHEMA_VERSION, "id": job_id, "proposer": proposer, "owner": proposer,
                  "proposer_verified": True, "proposed_at": _iso(self.now()), "removal": True,
                  "change_of_confirmed": True, "definition": _model.definition_of(job),
                  "definition_sha256": _model.definition_sha256(job)}
            _store.write_json(self.paths.proposal(job_id), rm)
            self._audit("sched_propose_remove", job_id, proposer, True, "ok", {"effect": "removal proposed"})
            return {"ok": True, "job_id": job_id, "status": "pending-confirm",
                    "note": "Removing a CONFIRMED job needs the operator's confirmation in the Scheduler tab; the job keeps "
                            "running until then."}

    # ------------------------------------------------------------------ the operator's actions (web routes only)
    def proposal_digest(self, job_id: str) -> Optional[str]:
        prop = self._safe(self._proposal, job_id)
        return None if prop is None else _store.sha256_hex(_store.canonical(prop))

    def confirm(self, job_id: str, *, actor: str, presence: Any, expect_digest: Optional[str]) -> Dict[str, Any]:
        """Commit a proposal. ``presence`` must be the evidence of a PASSED device-owner check (a dict with ok True and
        result 'authenticated'); there is no waiver. ``expect_digest`` binds the commit to the proposal the operator saw.
        NEVER fires or runs anything."""
        now = self.now()
        with store_lock(self.paths):
            prop = self._safe(self._proposal, job_id)
            if prop is None:
                return {"ok": False, "error": f"no pending proposal {job_id!r}"}
            if not (isinstance(presence, dict) and presence.get("ok") is True and presence.get("result") == "authenticated"):
                self._audit("confirm", job_id, actor, False, "refused", {"reason": "no presence proof"})
                return {"ok": False, "error": "NOT confirmed: the device-owner presence check did not pass"}
            if expect_digest is None or _store.sha256_hex(_store.canonical(prop)) != expect_digest:
                self._audit("confirm", job_id, actor, False, "refused", {"reason": "proposal changed during the prompt"})
                return {"ok": False, "error": "NOT confirmed: the proposal changed after you clicked; review it again"}
            ev = {k: presence.get(k) for k in ("result", "method", "ts")}
            bad = self._revalidate(prop, now, job_id)
            if bad:
                self._audit("confirm", job_id, actor, False, "refused", {"reason": "re-validation: " + bad[:200]})
                return {"ok": False, "error": "NOT confirmed: the stored proposal is not valid (" + bad + ")"}
            if prop.get("removal"):
                for p in (self.paths.job(job_id), self.paths.pin(job_id), self.paths.job_state(job_id),
                          self.paths.proposal(job_id)):
                    _store.remove_quiet(p)
                self._audit("confirm", job_id, actor, False, "ok", {"effect": "removed", "presence": ev})
                return {"ok": True, "job_id": job_id, "status": "removed"}
            defn = prop["definition"]
            if _model.definition_sha256(defn) != prop.get("definition_sha256"):
                return {"ok": False, "error": "the proposal's definition does not match its recorded digest"}
            trig = defn["trigger"]
            first: Optional[float]
            if _schedule.is_one_shot(trig):
                first = _schedule.one_shot_epoch(trig)
                if now - first >= LIMITS["one_shot_late_window_s"]:
                    return {"ok": False, "error": "that one-shot instant is already past its late window; propose a new one"}
            else:
                nf = _schedule.next_fires(trig, now, 1, tz=self.tz(), anchor=now)
                first = nf[0] if nf else None
            job = dict(defn)
            job.update({"enabled": True, "confirmed_at": _iso(now), "confirmed_by": actor})
            _store.write_json(self.paths.job(job_id), job)
            _store.write_json(self.paths.pin(job_id), {"schema_version": V.SCHEMA_VERSION, "id": job_id,
                                                       "definition_sha256": _model.definition_sha256(defn),
                                                       "confirmed_at": _iso(now), "presence": ev})
            self._save_state(job_id, {"anchor": now, "next_due": first, "done": False, "active": None})
            _store.remove_quiet(self.paths.proposal(job_id))
            self._audit("confirm", job_id, actor, False, "ok", {"effect": "confirmed", "presence": ev,
                                                                 "first_fire_utc": _iso(first) if first else None})
        return {"ok": True, "job_id": job_id, "status": "confirmed", "first_fire_utc": _iso(first) if first else None,
                "note": "Confirmed. Confirming never fires the job."}

    def prompt_text(self, job_id: str) -> str:
        """What the OS presence dialog says: the job, its schedule and the first words of the text (the dialog itself is
        then a second check on what is being confirmed)."""
        row = self._safe(self._proposal, job_id)
        if not row:
            return f"confirm the scheduled job {job_id}"
        d = row["definition"]
        first = " ".join(d["action"]["text"].split())[:40]
        verb = "remove" if row.get("removal") else "confirm"
        return f"{verb} the scheduled job {job_id}: {_schedule.describe(d['trigger'])}; \"{first}\""

    def _revalidate(self, prop: Dict[str, Any], now: float, file_id: str) -> Optional[str]:
        """Defence in depth (F-3/F-4): a proposal file is data on disk, so it is checked AGAIN at confirm with the same
        validator the verb used: own session (target == the verified proposer == owner), the limits, the text rules,
        the trigger rules, and that the stored definition is already in normal form. Returns a reason, or None."""
        d, proposer = prop.get("definition"), prop.get("proposer")
        if not isinstance(d, dict) or not (file_id == prop.get("id") == d.get("id")):
            return "the proposal's file name, its id and its definition id are not the same job id"
        if not isinstance(d, dict) or not isinstance(proposer, str) or not proposer:
            return "malformed proposal"
        if prop.get("proposer_verified") is not True:
            return "the proposer was not verified"
        if d.get("owner") != proposer or prop.get("owner", proposer) != proposer:
            return "the owner is not the verified proposer"
        existing = self._safe(self._job, d.get("id")) if isinstance(d.get("id"), str) and _store.ID_RE.match(d["id"]) else None
        if prop.get("removal"):
            if not existing or existing.get("owner") != proposer:
                return "a removal must name a job owned by the proposer"
            return None
        act = d.get("action")
        if not isinstance(act, dict):
            return "malformed action"
        args = {"job_id": d.get("id"), "title": d.get("title"), "trigger": d.get("trigger"), "kind": d.get("kind", "general"),
                "action": dict(act)}
        try:
            norm = _model.validate_proposal(args, proposer=proposer, verified=True, now=now,
                                            active_counts=self._active_counts(d.get("id")),
                                            existing_owner=existing.get("owner") if existing else None, tz=self.tz())
        except Refusal as r:
            return f"{r.field}: {r.reason}"
        if norm != d:
            return "the stored definition is not in normal form (it differs from what validation produces)"
        return None

    def _own_session_ok(self, job: Dict[str, Any]) -> bool:
        """A job may target ONLY its owner's own session (the owner recorded at the verified proposal)."""
        t = (job.get("action") or {}).get("target_instance_id")
        return isinstance(t, str) and bool(t) and t == job.get("owner")

    def decline(self, job_id: str, *, actor: str, seen: Optional[str]) -> Dict[str, Any]:
        with store_lock(self.paths):
            prop = self._safe(self._proposal, job_id)
            if prop is None:
                return {"ok": False, "error": f"no pending proposal {job_id!r}"}
            if not isinstance(seen, str) or seen != _store.sha256_hex(_store.canonical(prop)):
                self._audit("decline", job_id, actor, False, "refused",
                            {"reason": "stale page" if isinstance(seen, str) else "no seen digest"})
                return {"ok": False, "stale": True, "error": "NOT declined: that is not the proposal you were looking at "
                        "(it changed, or the page did not say which version you saw); reload and look again"}
            _store.remove_quiet(self.paths.proposal(job_id))
            self._audit("decline", job_id, actor, False, "ok")
        return {"ok": True, "job_id": job_id, "status": "declined"}

    def cancel(self, job_id: str, *, actor: str, seen: Optional[str]) -> Dict[str, Any]:
        """The operator cancels a confirmed job from the tab (it only removes capability). History stays in the Run Log.
        ``seen`` is REQUIRED: the pinned definition sha256 the page showed; a missing or different one is refused and
        audited."""
        with store_lock(self.paths):
            job = self._safe(self._job, job_id)
            if job is None:
                return {"ok": False, "error": f"no confirmed job {job_id!r}"}
            if not isinstance(seen, str) or seen != _model.definition_sha256(job):
                self._audit("cancel", job_id, actor, False, "refused",
                            {"reason": "stale page" if isinstance(seen, str) else "no seen digest"})
                return {"ok": False, "stale": True, "error": "NOT cancelled: that is not the job you were looking at "
                        "(it changed, or the page did not say which version you saw); reload and look again"}
            for p in (self.paths.job(job_id), self.paths.pin(job_id), self.paths.job_state(job_id)):
                _store.remove_quiet(p)
            self._audit("cancel", job_id, actor, False, "ok")
        return {"ok": True, "job_id": job_id, "status": "cancelled"}

    def set_enabled(self, job_id: str, enabled: bool, *, actor: str) -> Dict[str, Any]:
        """Pause / enable. Not part of the pinned definition. Slots passed while paused are not misses: enabling
        recomputes the next slot from now."""
        now = self.now()
        with store_lock(self.paths):
            job = self._safe(self._job, job_id)
            if job is None:
                return {"ok": False, "error": f"no confirmed job {job_id!r}"}
            job["enabled"] = bool(enabled)
            _store.write_json(self.paths.job(job_id), job)
            st = self._state(job_id)
            if enabled and not st.get("done") and not _schedule.is_one_shot(job["trigger"]):
                nf = _schedule.next_fires(job["trigger"], now, 1, tz=self.tz(), anchor=st.get("anchor") or now)
                st["next_due"] = nf[0] if nf else None
                self._save_state(job_id, st)
            self._audit("enable" if enabled else "pause", job_id, actor, False, "ok")
        return {"ok": True, "job_id": job_id, "enabled": bool(enabled)}

    # ------------------------------------------------------------------ reads
    def pending(self) -> List[Dict[str, Any]]:
        out = []
        for i in _store.list_ids(self.paths.proposals):
            p = self._safe(self._proposal, i)
            if p:
                out.append(self._pending_row(p))
        return out

    def _changes(self, p: Dict[str, Any]) -> List[Dict[str, Any]]:
        """For a CHANGE proposal, each field that differs from the confirmed job, old -> new (
        requirement-proposal-view). Empty for a new job or a removal."""
        if p.get("removal") or not p.get("change_of_confirmed"):
            return []
        old = self._safe(self._job, p["id"])
        if not old:
            return []
        new = p["definition"]
        pairs = [("title", old.get("title"), new.get("title")),
                 ("schedule", _schedule.describe(old["trigger"]), _schedule.describe(new["trigger"])),
                 ("text", old["action"].get("text"), new["action"].get("text")),
                 ("target", old["action"].get("target_instance_id"), new["action"].get("target_instance_id")),
                 ("kind", old.get("kind"), new.get("kind")),
                 ("owner", old.get("owner"), new.get("owner"))]
        out = [{"field": f, "old": o, "new": n} for f, o, n in pairs if o != n]
        if old["trigger"] != new["trigger"] and not any(c["field"] == "schedule" for c in out):
            out.append({"field": "schedule", "old": json.dumps(old["trigger"], sort_keys=True),
                        "new": json.dumps(new["trigger"], sort_keys=True)})
        return out

    def _pending_row(self, p: Dict[str, Any]) -> Dict[str, Any]:
        d = p["definition"]
        return {"changes": self._changes(p), "removes": bool(p.get("removal")), "id": p["id"], "title": d["title"], "owner": d["owner"], "kind": "removal" if p.get("removal") else
                ("change" if p.get("change_of_confirmed") else "new"), "proposed_by": p.get("proposer"),
                "proposed_by_verified": bool(p.get("proposer_verified")), "proposed_at": p.get("proposed_at"),
                "schedule": _schedule.describe(d["trigger"]), "target": d["action"].get("target_instance_id"),
                "text": d["action"]["text"], "digest": _store.sha256_hex(_store.canonical(p))}

    def _pin_ok(self, job: Dict[str, Any]) -> Tuple[bool, str]:
        try:
            pin = _store.read_json(self.paths.pin(job["id"]))
        except ValueError:
            return False, "pin unreadable"
        if not pin:
            return False, "no pin"
        if pin.get("definition_sha256") != _model.definition_sha256(job):
            return False, V.JOB_CHANGED
        return True, ""

    def _last_run(self, job_id: str) -> Optional[Dict[str, Any]]:
        recs, _ = _store.read_chain_tolerant(self.paths.run_chain(job_id)) if os.path.isdir(self.paths.run_dir(job_id)) else ([], [])
        latest: Dict[str, Dict[str, Any]] = {}
        for r in recs:
            if r.get("run_id"):
                latest[r["run_id"]] = r
        return max(latest.values(), key=lambda r: r.get("ts", ""), default=None) if latest else None

    def list_jobs(self, owner: Optional[str] = None) -> Dict[str, Any]:
        now = self.now()
        rows = []
        for i in _store.list_ids(self.paths.jobs):
            try:
                job, st = self._job(i), self._state(i)
            except ValueError as exc:
                rows.append({"id": i, "error": str(exc), "result": V.FAIL, "failure_class": True})
                continue
            if not job or (owner is not None and job.get("owner") != owner):
                continue
            ok, why = self._pin_ok(job)
            last = self._last_run(i)
            if not ok:
                result, shown = "refused", f"refused ({why})"
            elif last:
                result, shown = last.get("result"), last.get("outcome") or last.get("result")
            elif not job.get("enabled"):
                result, shown = "paused", "paused"
            else:
                nd = st.get("next_due")
                result, shown = ("NEVER-RUN", "NEVER-RUN") if nd is not None and now > nd + LIMITS["miss_grace_s"] \
                    else ("waiting", "waiting for first run")
            if st.get("done"):
                nxt = "done"
            elif not job.get("enabled") or not ok:
                nxt = "not scheduled" + ("" if ok else f" ({why})")
            else:
                nxt = _iso(st["next_due"]) if st.get("next_due") else "none"
            rows.append({"id": i, "title": job["title"], "owner": job["owner"], "enabled": bool(job.get("enabled")),
                         "schedule": _schedule.describe(job["trigger"]), "next_run": nxt, "last_result": result,
                         "last_shown": shown, "done": bool(st.get("done")), "pin_ok": ok,
                         "failure_class": result in ("refused", "NEVER-RUN", "FAIL", "MISSED", "SKIPPED") or not ok})
        pend = [r for r in self.pending() if owner is None or r["owner"] == owner]
        return {"ok": True, "jobs": rows, "pending": pend}

    def detail(self, job_id: str) -> Dict[str, Any]:
        prop = self._safe(self._proposal, job_id)
        job = self._safe(self._job, job_id)
        if prop is None and job is None:
            return {"ok": False, "error": f"no job or proposal {job_id!r}"}
        out: Dict[str, Any] = {"ok": True, "id": job_id}
        if prop is not None:
            out["proposal"] = self._pending_row(prop)
        if job is not None:
            now = self.now()
            st = self._state(job_id)
            ok, why = self._pin_ok(job)
            fires = [] if st.get("done") else _schedule.next_fires(job["trigger"], now, 5, tz=self.tz(),
                                                                 anchor=st.get("anchor") or now)
            out["job"] = {"id": job_id, "title": job["title"], "owner": job["owner"], "enabled": bool(job.get("enabled")),
                          "kind": job["kind"], "schedule": _schedule.describe(job["trigger"]), "trigger": job["trigger"],
                          "action": job["action"], "confirmed_at": job.get("confirmed_at"),
                          "pin": {"ok": ok, "failed": why, "sha256": _model.definition_sha256(job)}, "next_runs": [_iso(f) for f in fires], "done": bool(st.get("done")),
                          "descriptor": "Types this note into the owner's own running session at the set time. It "
                                        "never launches anything and never runs a command.",
                          "steps": ["At the scheduled time the scheduler sends one labelled line to the session "
                                    f"{job['action']['target_instance_id']!r} through the harness injection gate.",
                                    "If the operator's line is partly typed or a dialog is open, the harness holds it "
                                    "and releases it when the line is clear.",
                                    "The receipt records SENT, then DELIVERED only when the session's injection log shows it."]}
            out["runs"] = self.run_log(job_id=job_id, limit=20)["runs"]
        return out

    def run_log(self, *, job_id: Optional[str] = None, result: Optional[str] = None, outcome: Optional[str] = None,
                owner: Optional[str] = None, limit: int = 200) -> Dict[str, Any]:
        """Runs across all jobs, newest first, one row per run (its latest event), with chain verification."""
        ids = [job_id] if job_id else self._run_dirs()
        runs: List[Dict[str, Any]] = []
        chains: Dict[str, Any] = {}
        for i in ids:
            if not isinstance(i, str) or not _store.ID_RE.match(i) or not os.path.isdir(self.paths.run_dir(i)):
                continue
            ok, recs, breaks = _store.verify_chain(self.paths.run_chain(i))
            chains[i] = {"ok": ok, "breaks": breaks}
            latest: Dict[str, Dict[str, Any]] = {}
            for r in recs:
                if r.get("run_id"):
                    latest[r["run_id"]] = r
            for r in latest.values():
                if owner is not None and r.get("owner") != owner:
                    continue
                if result and r.get("result") != result:
                    continue
                if outcome and r.get("outcome") != outcome:
                    continue
                runs.append(r)
        runs.sort(key=lambda r: (r.get("ts", ""), r.get("run_id", "")), reverse=True)
        audit_ok, _, audit_breaks = _store.verify_chain(self.paths.audit)
        return {"ok": True, "runs": runs[: max(1, min(int(limit), 1000))], "chains": chains,
                "audit_chain": {"ok": audit_ok, "breaks": audit_breaks},
                "filters": {"results": list(V.RUN_LOG_RESULT_FILTER), "outcomes": list(V.RUN_LOG_OUTCOME_FILTER)}}

    def _run_dirs(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.paths.runs) if _store.ID_RE.match(n))
        except FileNotFoundError:
            return []

    def run_receipt(self, job_id: str, run_id: str) -> Dict[str, Any]:
        try:
            rec = _store.read_json(self.paths.run_file(job_id, run_id))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "receipt": rec} if rec else {"ok": False, "error": "no such run"}

    def banner(self, within_s: float = 86400.0, limit: int = 20) -> List[Dict[str, Any]]:
        """Flagged runs of the last day: in stage 1a the 'flag' is a Run Log entry plus this banner (the alert service
 is not built; known limit)."""
        cutoff = _iso(self.now() - within_s)
        rows = [r for r in self.run_log(limit=1000)["runs"] if r.get("flagged") and r.get("ts", "") >= cutoff]
        return rows[:limit]

    # ------------------------------------------------------------------ the tick
    def tick(self, now: Optional[float] = None) -> int:
        """One scheduler tick. Single-flight, never raises. Returns the number of jobs examined."""
        if not self._tick_lock.acquire(blocking=False):
            return 0
        try:
            t = self.now() if now is None else float(now)
            n = 0
            for jid in _store.list_ids(self.paths.jobs):
                n += 1
                try:
                    with store_lock(self.paths):
                        self._tick_job(jid, t)
                except Exception as exc:  # noqa: BLE001 - one broken job never stops the others
                    self._audit("tick", jid, "scheduler", True, "error", {"error": f"{type(exc).__name__}: {exc}"[:300]})
            self.ticks += 1
            return n
        finally:
            self._tick_lock.release()

    def _tick_job(self, jid: str, now: float) -> None:
        job = self._job(jid)
        if not job:
            return
        st = self._state(jid)
        st.setdefault("tracking", [])
        if st["tracking"]:                       # runs already SENT keep being tracked whatever the next slot does
            self._poll_tracking(job, st, now)
            self._save_state(jid, st)
        if st.get("done") and not st.get("active"):
            return
        if not job.get("enabled") and not st.get("active"):
            return
        ok, why = self._pin_ok(job)
        if not ok:
            nd = st.get("next_due")
            if st.get("active") or (nd is not None and now >= nd and not st.get("done")):
                self._refuse(job, st, now, why)
                self._save_state(jid, st)
            return
        if st.get("active"):
            self._advance(job, st, now)
            self._save_state(jid, st)
            return
        nd = st.get("next_due")
        if nd is not None and now >= nd:
            self._begin_slot(job, st, now)
            self._save_state(jid, st)

    # -- slot classification
    def _begin_slot(self, job: Dict[str, Any], st: Dict[str, Any], now: float) -> None:
        trig, slot = job["trigger"], float(st["next_due"])
        late = now - slot
        one_shot = _schedule.is_one_shot(trig)
        if one_shot:
            st["done"], st["next_due"] = True, None
            if late >= LIMITS["one_shot_late_window_s"]:
                self._finish_nofire(job, now, slot, V.T_ONE_SHOT_LATE, V.MISSED, V.ONE_SHOT_TOO_LATE, flagged=True,
                                    extra={"late_s": int(late)})
                return
            kind = V.T_ONE_SHOT if late <= LIMITS["on_time_s"] else V.T_ONE_SHOT_LATE
        else:
            nxt = _schedule.next_fires(trig, now, 1, tz=self.tz(), anchor=st.get("anchor") or slot)
            st["next_due"] = nxt[0] if nxt else None
            if late > LIMITS["miss_grace_s"]:
                missed = 1 + len([f for f in _schedule.next_fires(trig, slot, 200, tz=self.tz(), anchor=st.get("anchor") or slot) if f < now])
                self._finish_nofire(job, now, slot, V.T_SCHEDULED, V.MISSED, V.MISSED_SLOT, flagged=True,
                                    extra={"missed_slots": missed, "late_s": int(late),
                                           "note": "a missed recurring inject is flagged, never caught up"})
                return
            kind = V.T_SCHEDULED
        run_id = _store.new_run_id(now, self._nonce())
        st["active"] = {"run_id": run_id, "slot": slot, "trigger": kind, "phase": "pre", "started": now,
                        "wait_since": None, "wait_reason": None, "next_check": now, "one_shot": one_shot}
        self._advance(job, st, now)

    # -- receipts
    def _record(self, job: Dict[str, Any], run_id: str, event: str, now: float, **fields: Any) -> Dict[str, Any]:
        jid = job["id"]
        os.makedirs(self.paths.run_dir(jid), mode=0o700, exist_ok=True)
        path = self.paths.run_file(jid, run_id)
        try:
            rec = _store.read_json(path) or {}
        except ValueError:
            rec = {}
        rec.update({"schema_version": V.SCHEMA_VERSION, "run_id": run_id, "job_id": jid, "owner": job["owner"],
                    "target": job["action"].get("target_instance_id"), "definition_sha256": _model.definition_sha256(job)})
        rec.update(fields)
        rec["last_event"], rec["ts"] = event, _iso(now)
        _store.write_json(path, rec)
        line = {k: rec.get(k) for k in ("run_id", "job_id", "owner", "target", "trigger", "slot_utc", "result", "outcome",
                                        "reason", "flagged", "definition_sha256", "frame_trigger", "content_sha256",
                                        "mid_turn", "late_s", "missed_slots", "note", "effect", "delivery") if k in rec}
        line.update({"event": event, "ts": _iso(now)})
        _store.append_chained(self.paths.run_chain(jid), line)
        return rec

    def _finish_nofire(self, job: Dict[str, Any], now: float, slot: float, trigger: str, result: str, reason: str, *,
                       flagged: bool, extra: Optional[Dict[str, Any]] = None) -> None:
        run_id = _store.new_run_id(now, self._nonce())
        self._record(job, run_id, result.lower(), now, trigger=trigger, slot_utc=_iso(slot), result=result,
                     outcome=None, reason=reason, flagged=flagged, **(extra or {}))

    def _refuse(self, job: Dict[str, Any], st: Dict[str, Any], now: float, why: str) -> None:
        a = st.get("active") or {}
        run_id = a.get("run_id") or _store.new_run_id(now, self._nonce())
        slot = a.get("slot") or st.get("next_due") or now
        self._record(job, run_id, "refused", now, trigger=a.get("trigger", V.T_SCHEDULED), slot_utc=_iso(slot),
                     result=V.FAIL, outcome=V.ERROR, reason=f"refused({why})", flagged=True,
                     note="the pinned definition no longer matches the job; re-confirm in the Scheduler tab")
        st["active"] = None
        if _schedule.is_one_shot(job["trigger"]):
            st["done"], st["next_due"] = True, None
        elif st.get("next_due") is not None and now >= st["next_due"]:
            nxt = _schedule.next_fires(job["trigger"], now, 1, tz=self.tz(), anchor=st.get("anchor") or now)
            st["next_due"] = nxt[0] if nxt else None

    def _end(self, job: Dict[str, Any], st: Dict[str, Any], now: float, a: Dict[str, Any], event: str,
             **fields: Any) -> None:
        """Close run ``a`` (the pre-send active run, or a sent run being tracked) with a final receipt."""
        self._record(job, a["run_id"], event, now, trigger=a["trigger"], slot_utc=_iso(a["slot"]), **fields)
        if st.get("active") is a:
            st["active"] = None
        st["tracking"] = [t for t in st.get("tracking", []) if t is not a and t.get("run_id") != a["run_id"]]

    # -- the run
    def _poll_tracking(self, job: Dict[str, Any], st: Dict[str, Any], now: float) -> None:
        for t in list(st.get("tracking", [])):
            self._poll_sent(job, st, now, t)

    def _advance(self, job: Dict[str, Any], st: Dict[str, Any], now: float) -> None:
        a = st["active"]
        if now < a.get("next_check", 0):
            return
        target = job["action"]["target_instance_id"]
        if not self._own_session_ok(job):
            self._end(job, st, now, a, "refused", result=V.FAIL, outcome=V.ERROR, reason="refused(target-not-owner)",
                      flagged=True, note="a job may only target its owner's own session; nothing was sent")
            return
        if not job.get("enabled"):
            self._end(job, st, now, a, "skipped", result=V.SKIPPED, outcome=None, reason="paused", flagged=True)
            return
        reg = self._reg()
        try:
            live = reg.is_registered(target)
        except Exception:  # noqa: BLE001
            live = False
        if not live:
            self._end(job, st, now, a, "skipped", result=V.SKIPPED, outcome=None, reason=V.TARGET_NOT_LIVE, flagged=True,
                      note="nothing is queued for later and no session is started")
            return
        sd = None
        try:
            sd = reg.get(target).meta.get("session_dir")
        except Exception:  # noqa: BLE001
            pass
        # 1. compaction ordering
        try:
            logpath = _inject.session_log_path(sd)
            cstate, cdetail = _inject.compaction_state(logpath, now)
        except _inject.LogUnreadable as exc:
            logpath, cstate, cdetail = None, "unreadable", str(exc)
        if cstate != "clear":
            reason = V.TARGET_COMPACTING if cstate == "compacting" else V.TARGET_LOG_UNREADABLE
            # 2. (a compaction wait does NOT extend the one-shot late window)
            if a.get("one_shot") and now - a["slot"] >= LIMITS["one_shot_late_window_s"]:
                self._end(job, st, now, a, "missed", result=V.MISSED, outcome=None, reason=V.ONE_SHOT_TOO_LATE, flagged=True,
                          late_s=int(now - a["slot"]), note=f"still waiting ({reason}) when the late window ran out")
                return
            ws = a.get("wait_since")
            if ws is not None and now - ws >= LIMITS["compaction_wait_max_s"]:
                self._end(job, st, now, a, "skipped", result=V.SKIPPED, outcome=None, reason=reason, flagged=True,
                          note=cdetail[:300])
                return
            if a.get("wait_reason") != reason:
                a["wait_since"] = ws if ws is not None else now
                a["wait_reason"] = reason
                self._record(job, a["run_id"], "waiting", now, trigger=a["trigger"], slot_utc=_iso(a["slot"]),
                             result=V.WAITING, outcome=None, reason=reason, flagged=cstate == "unreadable",
                             note=cdetail[:300])
            a["next_check"] = now + LIMITS["compaction_recheck_s"]
            return
        a["wait_since"], a["wait_reason"] = None, None
        # 2. the one-shot late window, measured from the fire time
        if a.get("one_shot") and now - a["slot"] >= LIMITS["one_shot_late_window_s"]:
            self._end(job, st, now, a, "missed", result=V.MISSED, outcome=None, reason=V.ONE_SHOT_TOO_LATE, flagged=True,
                      late_s=int(now - a["slot"]))
            return
        # 3. the plan-usage band; "run" today, see plan_usage.py
        band = _plan_usage.plan_usage_band(job)
        if band == _plan_usage.SKIP:
            self._end(job, st, now, a, "skipped", result=V.SKIPPED, outcome=None, reason=V.PLAN_USAGE_HIGH, flagged=True)
            return
        if band in (_plan_usage.WAIT, _plan_usage.UNKNOWN):
            unknown = band == _plan_usage.UNKNOWN
            nd = st.get("next_due")
            if nd is not None and now >= nd:
                self._end(job, st, now, a, "skipped", result=V.SKIPPED, outcome=None,
                          reason=V.PLAN_USAGE_UNKNOWN if unknown else V.PLAN_USAGE_HIGH, flagged=True)
                return
            if a.get("band_wait") != band:
                a["band_wait"] = band
                self._record(job, a["run_id"], "waiting", now, trigger=a["trigger"], slot_utc=_iso(a["slot"]),
                             result=V.WAITING, outcome=None, reason=V.PLAN_USAGE_UNKNOWN if unknown else "plan-usage-wait",
                             flagged=False, note="usage unknown" if unknown else "plan usage in the wait band")
            a["next_check"] = now + LIMITS["plan_usage_retry_s"]
            return
        self._send(job, st, now, reg, target, sd, logpath)

    def _send(self, job: Dict[str, Any], st: Dict[str, Any], now: float, reg: Any, target: str, sd: Any,
              logpath: str) -> None:
        a = st["active"]
        if not self._own_session_ok(job):       # checked again at the send itself
            self._end(job, st, now, a, "refused", result=V.FAIL, outcome=V.ERROR, reason="refused(target-not-owner)",
                      flagged=True, note="a job may only target its owner's own session; nothing was sent")
            return
        cleaned = job["action"]["text"]
        if _text.first_visible(cleaned) == "!" or cleaned != _text.clean_text(cleaned):
            self._end(job, st, now, a, "refused", result=V.FAIL, outcome=V.ERROR, reason="refused(text)", flagged=True,
                      note="the stored text no longer passes the text rules")
            return
        body = _text.labelled(job["id"], job["owner"], a["slot"], cleaned)
        if _inject.harness_screen(body):
            self._end(job, st, now, a, "refused", result=V.FAIL, outcome=V.ERROR, reason="refused(harness-screen)",
                      flagged=True, note="the harness screen would refuse this text; nothing was sent")
            return
        trig = _inject.frame_trigger(job["id"], a["run_id"])
        hashes = _inject.expected_hashes(body)
        try:
            offset = _inject.log_size(logpath)
        except _inject.LogUnreadable as exc:
            a["next_check"] = now + LIMITS["compaction_recheck_s"]
            a["wait_since"] = a.get("wait_since") or now
            a["wait_reason"] = V.TARGET_LOG_UNREADABLE
            self._record(job, a["run_id"], "waiting", now, trigger=a["trigger"], slot_utc=_iso(a["slot"]), result=V.WAITING,
                         outcome=V.ERROR, reason=V.TARGET_LOG_UNREADABLE, flagged=True, note=str(exc)[:300])
            return
        mid = self._turn_state(sd, now)
        try:
            _inject.send(reg, target, _inject.make_frame(body, trig))
        except (KeyError, OSError) as exc:        # UnknownSession is a KeyError; a dead socket is an OSError
            self._end(job, st, now, a, "skipped", result=V.SKIPPED, outcome=None, reason=V.TARGET_NOT_LIVE, flagged=True,
                      note=f"{type(exc).__name__}: {exc}"[:200])
            return
        st["active"] = None
        st.setdefault("tracking", []).append(a)
        if len(st["tracking"]) > 50:
            st["tracking"] = st["tracking"][-50:]
        a.update({"phase": "sent", "sent_at": now, "offset": offset, "frame_trigger": trig, "hashes": list(hashes),
                  "mid_turn": mid, "log": logpath, "seen": "none", "next_check": now})
        self._record(job, a["run_id"], "sent", now, trigger=a["trigger"], slot_utc=_iso(a["slot"]), result=V.OK_UNVERIFIED,
                     outcome=V.SENT, reason="the hub accepted the frame; delivery is not yet confirmed by the target's "
                     "injection log", flagged=False, frame_trigger=trig, content_sha256=hashes[0], mid_turn=mid,
                     effect_ledger_flag=self._ledger_on())
        self._poll_sent(job, st, now, a)

    def _poll_sent(self, job: Dict[str, Any], st: Dict[str, Any], now: float, a: Dict[str, Any]) -> None:
        try:
            recs, a["offset"] = _inject.read_new(a["log"], int(a["offset"]))
        except _inject.LogUnreadable as exc:
            if now - a["sent_at"] >= LIMITS["sent_poll_horizon_s"]:
                self._end(job, st, now, a, "error", result=V.FAIL, outcome=V.ERROR, reason="target-log-unreadable",
                          flagged=True, note=str(exc)[:300])
            return
        if a.get("pending_effect_since") is None:
            res = _inject.classify_delivery(recs, a["frame_trigger"], tuple(a["hashes"]))
        else:   # delivered already; only waiting for the effect-ledger record
            res = {"state": "delivered", "evidence": a.get("evidence", {})}
            eff = _inject.classify_delivery(recs, a["frame_trigger"], tuple(a["hashes"]))
            if eff["state"] in ("effect-failed",):
                res = eff
            elif eff["evidence"].get("effect") == "completed":
                res["evidence"] = dict(res["evidence"], effect="completed")
        state, ev = res["state"], res["evidence"]
        if state == "refused":
            self._end(job, st, now, a, "refused", result=V.FAIL, outcome=V.ERROR, reason="harness-refused", flagged=True,
                      delivery=ev)
            return
        if state == "effect-failed":
            self._end(job, st, now, a, "error", result=V.FAIL, outcome=V.ERROR, reason="effect-failed", flagged=True,
                      delivery=ev)
            return
        if state == "held" and a.get("seen") != "held":
            a["seen"] = "held"
            self._record(job, a["run_id"], "held", now, trigger=a["trigger"], slot_utc=_iso(a["slot"]), result=V.WAITING,
                         outcome=V.HELD, reason="the harness gate is holding the frame until the operator's line is "
                         "clear and no dialog is open; there is no expiry and the scheduler never drops it",
                         flagged=False, delivery=ev)
            return
        if state == "delivered":
            ledger = self._ledger_on()
            if ev.get("effect") != "completed" and ledger and now - a.get("pending_effect_since", now) < 120:
                a["pending_effect_since"] = a.get("pending_effect_since") or now
                a["evidence"] = ev
                return
            outcome = V.DELIVERED_QUEUED if a.get("mid_turn") == "busy" else V.DELIVERED
            note = ("delivered while the session was mid-turn: it arrives after the current turn (Claude Code queues "
                    "the line)" if outcome == V.DELIVERED_QUEUED else
                    ("session turn state was unknown at send time" if a.get("mid_turn") == "unknown" else None))
            ev = dict(ev, effect_ledger_flag=ledger)
            self._end(job, st, now, a, "delivered", result=V.OK, outcome=outcome, reason="the target's injection_log "
                      "shows the delivery for this run's trigger and content hash", flagged=False, delivery=ev,
                      **({"note": note} if note else {}))
            return
        horizon = LIMITS["held_poll_horizon_s"] if a.get("seen") == "held" else LIMITS["sent_poll_horizon_s"]
        if now - a["sent_at"] >= horizon:
            self._end(job, st, now, a, "closed", result=V.OK_UNVERIFIED if a.get("seen") != "held" else V.WAITING,
                      outcome=V.HELD if a.get("seen") == "held" else V.SENT,
                      reason="polling stopped: the injection log never showed a delivery for this run",
                      flagged=a.get("seen") != "held")

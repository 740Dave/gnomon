"""The job definition and proposal validation.

Strict: an unknown field is refused. A refusal names the field, the reason and, for a limit, the value asked for and
the value allowed (requirement-refusal-shown). A refusal never partly applies: validation is
pure and returns either a complete definition or raises :class:`Refusal`.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from . import schedule, text as _text
from .limits import LIMITS
from .store import ID_RE, canonical, sha256_hex
from .vocab import OTHER_SESSION, STAGE_1B, STAGE_2_LAUNCH, STAGE_2_TRIGGER, SCHEMA_VERSION

PROPOSE_FIELDS = ("instance_id", "job_id", "title", "trigger", "action", "kind")
ACTION_FIELDS = ("type", "text", "target_instance_id")
#: the closed set of action types stage 1a registers (a set of ONE; checklist proposal action-type)
ACTION_TYPES_1A = ("inject",)
_SCRIPT_TYPES = ("script", "shell", "python", "exec", "command")
_AGENT_TYPES = ("agent", "launch", "launch_agent")


class Refusal(Exception):
    def __init__(self, field: str, reason: str, **detail: Any) -> None:
        super().__init__(f"{field}: {reason}")
        self.field, self.reason, self.detail = field, reason, detail

    def as_result(self) -> Dict[str, Any]:
        return {"ok": False, "refused": True, "field": self.field, "error": f"{self.field}: {self.reason}",
                "reason": self.reason, **self.detail}


def definition_of(job: Dict[str, Any]) -> Dict[str, Any]:
    """The pinned definition: everything but the mutable bookkeeping (enabled, confirmation fields)."""
    return {"id": job["id"], "title": job["title"], "owner": job["owner"], "kind": job["kind"],
            "trigger": job["trigger"], "action": job["action"], "schema_version": SCHEMA_VERSION}


def definition_sha256(job: Dict[str, Any]) -> str:
    return sha256_hex(canonical(definition_of(job)))


def validate_proposal(args: Any, *, proposer: Optional[str], verified: bool, now: float,
                      active_counts: Dict[str, int], existing_owner: Optional[str] = None,
                      tz: Any = None) -> Dict[str, Any]:
    """The validated, normalised job definition for a proposal, or :class:`Refusal`.

    ``proposer`` is the VERIFIED instance id (from the hub caller binding) when ``verified``; with ``verified`` False
    every inject is refused (requirement-target-rule). ``active_counts`` maps owner -> active jobs excluding this id.
    ``existing_owner`` is the owner of a job/proposal that already uses this id."""
    if not isinstance(args, dict):
        raise Refusal("arguments", "must be an object")
    for k in args:
        if k not in PROPOSE_FIELDS:
            raise Refusal(str(k), "unknown field (refused strictly)")
    if not verified or not isinstance(proposer, str) or not proposer:
        raise Refusal("instance_id", OTHER_SESSION + " (the caller is not verified: the hub caller binding is off "
                      "or did not identify this session)")
    given = args.get("instance_id")
    if given is not None and given != proposer:
        raise Refusal("instance_id", OTHER_SESSION)
    job_id = args.get("job_id")
    if not isinstance(job_id, str) or not ID_RE.match(job_id):
        raise Refusal("job_id", r"required; must match ^[a-z][a-z0-9-]{1,40}$")
    title = args.get("title")
    if not isinstance(title, str) or not title.strip():
        raise Refusal("title", "required")
    if len(title.strip()) > LIMITS["title_max_chars"]:
        raise Refusal("title", f"at most {LIMITS['title_max_chars']} characters")
    title = _text.clean_text(title).replace("\n", " ")[: LIMITS["title_max_chars"]] or "(untitled)"
    kind = args.get("kind", "general")
    if kind != "general":
        raise Refusal("kind", "an inject is kind 'general'; it is never kind backup")
    action = args.get("action")
    if not isinstance(action, dict):
        raise Refusal("action", "required: {type: 'inject', text: ...}")
    atype = action.get("type")
    if isinstance(atype, str) and atype.lower() in _SCRIPT_TYPES:
        raise Refusal("action.type", STAGE_1B)
    if isinstance(atype, str) and atype.lower() in _AGENT_TYPES:
        raise Refusal("action.type", STAGE_2_LAUNCH)
    if atype not in ACTION_TYPES_1A:
        raise Refusal("action.type", "must be 'inject' (the only action type in this stage; script actions are stage 1b and "
                      "launching an agent is stage 2, which needs the operator's approval)")
    for k in action:
        if k not in ACTION_FIELDS:
            raise Refusal("action." + str(k), "unknown field (refused strictly)")
    target = action.get("target_instance_id", proposer)
    if target != proposer:
        raise Refusal("action.target_instance_id", OTHER_SESSION)
    cleaned, why = _text.validate_text(action.get("text"))
    if cleaned is None:
        raise Refusal("action.text", why or "invalid")
    from . import inject as _inject

    screened = _inject.harness_screen(_text.make_label(job_id, proposer, 0) + " " + cleaned)
    if screened:
        raise Refusal("action.text", "the harness would refuse this text (" + ", ".join(screened) + "); a line may "
                      "not start with a backslash command")
    try:
        trig = schedule.normalize_trigger(args.get("trigger"))
    except schedule.TriggerError as exc:
        raise Refusal(exc.field, exc.reason)
    # --- limits (one table) ---
    if existing_owner is not None and existing_owner != proposer:
        raise Refusal("job_id", "this job id belongs to another agent")
    n = active_counts.get(proposer, 0)
    if n + 1 > LIMITS["max_active_jobs_per_owner"]:
        raise Refusal("limit.max_active_jobs_per_owner", "too many active jobs for this agent",
                      limit="max_active_jobs_per_owner", asked=n + 1, allowed=LIMITS["max_active_jobs_per_owner"])
    if schedule.is_one_shot(trig):
        due = schedule.one_shot_epoch(trig)
        if now - due >= LIMITS["one_shot_late_window_s"]:
            raise Refusal("trigger.once_at", "that instant is already more than the late window "
                          f"({LIMITS['one_shot_late_window_s']} s) in the past")
    else:
        gap = schedule.min_gap_s(trig, now, tz=tz)
        if gap is not None:
            if gap < LIMITS["min_fire_interval_s"]:
                raise Refusal("limit.min_fire_interval_s", "fires more often than once in 5 minutes",
                              limit="min_fire_interval_s", asked=int(gap), allowed=LIMITS["min_fire_interval_s"])
            if gap < LIMITS["min_inject_interval_s"]:
                raise Refusal("limit.min_inject_interval_s", "a recurring inject must be at least 15 minutes apart",
                              limit="min_inject_interval_s", asked=int(gap), allowed=LIMITS["min_inject_interval_s"])
    return {"id": job_id, "title": title, "owner": proposer, "kind": "general", "trigger": trig,
            "action": {"type": "inject", "target_instance_id": proposer, "text": cleaned},
            "schema_version": SCHEMA_VERSION}

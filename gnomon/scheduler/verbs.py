"""The six scheduler hub verbs: dispatch, the own-session rule, the audit.

OWN-SESSION IDENTITY (requirement-own-session-identity): under the hub caller binding the
verified instance id arrives as the FILLED ``args['instance_id']`` (the verbs' scope is BOUND_AGENT with
identity_args=('instance_id',)), and ``verified_caller`` is True. With the binding OFF no id is verified and every
verb refuses (a proposal names it: requirement-target-rule). A handler never receives a principal.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .verb_tools import FLAG, OPERATOR_ONLY_OPERATIONS, SCHED_VERB_NAMES, SCHED_VERB_TOOLS  # noqa: F401
from .vocab import STAGE_1B

__all__ = ["SCHED_VERB_NAMES", "SCHED_VERB_TOOLS", "FLAG", "dispatch"]

_UNVERIFIED = ("the caller is not verified: the hub caller binding (hub_caller_binding) is off or did not "
               "identify this session, and the scheduler acts only for a verified session")


def _policy_check(verb: str, actor: str) -> Optional[str]:
    """g32: the central verb policy. Returns a refusal message, or None to proceed. Fails closed."""
    try:
        from ..harness import verb_policy as vp

        pol = vp.load_policy(None)
        d = vp.check(verb, actor, ctx=vp.default_ctx(pol))
        if d.outcome != vp.ALLOW:
            return d.message or f"{verb} is not allowed by the verb policy"
    except Exception as exc:  # noqa: BLE001 - a policy that cannot be read denies
        return f"{verb}: the verb policy could not be read ({type(exc).__name__}); denied"
    return None


def _redact_obj(o: Any) -> Any:
    from ..appshost.redact import redact_line

    if isinstance(o, str):
        return redact_line(o)
    if isinstance(o, list):
        return [_redact_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: _redact_obj(v) for k, v in o.items()}
    return o


def dispatch(sched: Any, name: str, args: Dict[str, Any], caller: Optional[str], *, verified_caller: bool = False) -> Dict[str, Any]:
    """Run one verb. Never raises for an expected refusal. ``caller`` is the verified instance id (or None)."""
    args = args if isinstance(args, dict) else {}
    verified = bool(verified_caller and caller)
    actor = caller if verified else "unidentified"
    refusal = _policy_check(name, actor)
    if refusal:
        sched._audit(name, args.get("job_id") if isinstance(args.get("job_id"), str) else None, actor, verified, "refused",
                     {"reason": "verb policy: " + refusal[:200]})
        return {"ok": False, "error": refusal}
    try:
        if name == "sched_run_now":
            sched._audit(name, args.get("job_id") if isinstance(args.get("job_id"), str) else None, actor, verified,
                         "refused", {"reason": "stage 1b"})
            return {"ok": False, "refused": True, "error": f"sched_run_now: {STAGE_1B}"}
        if not verified:
            sched._audit(name, args.get("job_id") if isinstance(args.get("job_id"), str) else None, actor, False,
                         "refused", {"reason": "caller not verified"})
            return {"ok": False, "refused": True, "error": f"{name}: {_UNVERIFIED}"}
        if name == "sched_propose":
            return sched.propose({**args, "instance_id": caller}, proposer=caller, verified=True)
        if name == "sched_propose_remove":
            return sched.propose_remove(args.get("job_id"), proposer=caller, verified=True)
        if name == "sched_list":
            out = sched.list_jobs(owner=caller)
            sched._audit(name, None, actor, True, "ok")
            return _redact_obj(out)
        if name == "sched_status":
            jid = args.get("job_id")
            out = sched.detail(jid) if isinstance(jid, str) else {"ok": False, "error": "job_id is required"}
            if out.get("ok"):
                own = (out.get("job") or {}).get("owner") or (out.get("proposal") or {}).get("owner")
                if own != caller:
                    out = {"ok": False, "refused": True, "error": f"{jid!r} is not your job"}
            sched._audit(name, jid if isinstance(jid, str) else None, actor, True, "ok" if out.get("ok") else "refused")
            return _redact_obj(out)
        if name == "sched_log":
            lim = args.get("limit", 50)
            lim = lim if isinstance(lim, int) and not isinstance(lim, bool) and lim > 0 else 50
            jid = args.get("job_id") if isinstance(args.get("job_id"), str) else None
            out = sched.run_log(job_id=jid, owner=caller, limit=min(lim, 200))
            sched._audit(name, jid, actor, True, "ok")
            return _redact_obj({"ok": True, "runs": out["runs"], "chain_ok": all(c["ok"] for c in out["chains"].values())})
    except Exception as exc:  # noqa: BLE001 - a verb never raises into the hub
        try:
            sched._audit(name, None, actor, verified, "error", {"error": f"{type(exc).__name__}: {exc}"[:200]})
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": f"{name} failed: {type(exc).__name__}: {exc}"}
    sched._audit(str(name)[:40], None, actor, verified, "refused", {"reason": "unknown scheduler verb"})
    return {"ok": False, "error": f"unknown scheduler verb {name!r}"}

"""The seven Apps hub verbs, their caller scopes, and the legacy aliases (g32, g41, g60c, g73
amendment items 1 and 7).

Verbs (agents and the operator both): ``app_list``, ``app_status``, ``app_start``,
``app_stop``, ``app_open``, ``app_propose_install``, ``app_propose_uninstall``. There is NO verb that confirms
or declines a proposal, and none that runs an action: those are operator-only web routes (the operator's Apps tab).

Each verb call: asks ``verb_policy.check`` (g32; every entry ships ``others = "allow"``), runs through
:class:`~.host.AppsHost`, and appends an audit entry (the host does). The actor is the caller the hub transport
established, marked unverified (this tree has no hub caller binding, g60b).

Caller scopes (g60c): this tree has no caller-scope registry (it lives in ~/gnomon's
``hub/caller_binding.CALLER_SCOPES``). :data:`CALLER_SCOPES` declares the seven verbs' scopes here, in one table a
test enumerates against the tool surface, ready to be moved into that registry when the gnomon tree takes this
code. All seven are ``any`` here, because without a binding no agent identity exists to bind to.

Aliases (architect ruling R13): ``launch_app`` / ``terminate_app`` / ``list_running_apps`` converge onto
``app_start`` / ``app_stop`` / ``app_list`` under the same apps.json whitelist, for one release
(:data:`ALIASES`: removal date, owner; a test FAILS after the date, g73-amendment item 7). For an apps.json
entry that is a LEGACY entry (e.g. cluster_viewer, which stays as-is, ruling R1) the alias keeps the legacy
behaviour, because there is no manifest to start it through.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from .host import Actor, AppsError, AppsHost

from .verb_tools import (ALIASES, APP_VERB_NAMES, APP_VERB_TOOLS, CALLER_SCOPES, FLAG,  # noqa: F401
                         OPERATOR_ONLY_OPERATIONS)

#: re-exported descriptors (the verb surface's constants live in .verb_tools so the hub can read them lazily)
__all__ = ["ALIASES", "APP_VERB_NAMES", "APP_VERB_TOOLS", "CALLER_SCOPES", "FLAG", "OPERATOR_ONLY_OPERATIONS",
           "alias_dispatch", "dispatch"]


def _policy_check(verb: str, actor: Actor, runtime_root: Optional[str]) -> Optional[str]:
    """g32: ask the verb policy. Returns a refusal message, or None to proceed."""
    from ..harness import verb_policy as vp

    pol = vp.load_policy(runtime_root)
    d = vp.check(verb, actor.id, ctx=vp.default_ctx(pol))
    if d.outcome != vp.ALLOW:
        return d.message or f"{verb} is not allowed by the verb policy"
    return None


def _verb_audit(host: AppsHost, name: str, args: Dict[str, Any], actor: Actor, out: Dict[str, Any]) -> None:
    """ONE verb-call entry per call (ac-actor-and-audit): to the app's audit.jsonl when the call names a known app
    (installed, proposed, or with host state), else to the host-level <DATA>/apps/audit.jsonl."""
    import os

    from .manifest import ID_RE
    from .state import append_audit, append_host_audit

    app_id = args.get("app_id") if isinstance(args, dict) else None
    if not isinstance(app_id, str):
        app_id = out.get("app_id") if isinstance(out, dict) else None
    outcome = "ok" if isinstance(out, dict) and out.get("ok") else "refused"
    detail = {"error": str(out.get("error"))[:300]} if isinstance(out, dict) and out.get("error") else None
    try:
        p = host.paths
        if isinstance(app_id, str) and ID_RE.match(app_id) and os.path.isdir(p.app(app_id)):
            append_audit(p, app_id, verb=name, actor=actor.id, actor_verified=actor.verified, outcome=outcome,
                         detail=detail)
        else:
            append_host_audit(p, verb=name, actor=actor.id, actor_verified=actor.verified, outcome=outcome,
                              detail={**(detail or {}), **({"app_id_arg": str(app_id)[:80]} if app_id else {})} or None)
    except OSError:
        pass


def dispatch(host: AppsHost, name: str, args: Dict[str, Any], caller: Optional[str], *,
             verified_caller: bool = False) -> Dict[str, Any]:
    """Run one Apps verb, then append its verb-call audit entry. Never raises for an expected refusal.
    ``verified_caller`` is True ONLY when the hub's caller binding established ``caller`` (never from a tool argument)."""
    actor = host.actor(caller)
    if verified_caller and caller:
        actor = type(actor)(caller, True)
    args = args if isinstance(args, dict) else {}
    out = _dispatch(host, name, args, actor)
    _verb_audit(host, name, args, actor, out)
    return out


def _dispatch(host: AppsHost, name: str, args: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
    try:
        refusal = _policy_check(name, actor, host.paths.root)
    except Exception as exc:  # noqa: BLE001 - a policy that cannot be read denies (fail closed)
        refusal = f"{name}: the verb policy could not be read ({type(exc).__name__}); denied"
    if refusal:
        return {"ok": False, "error": refusal}
    try:
        if name == "app_list":
            return host.list()
        if name == "app_status":
            return host.status(args.get("app_id"))
        if name == "app_start":
            return host.start(args.get("app_id"), actor)
        if name == "app_stop":
            return host.stop(args.get("app_id"), actor)
        if name == "app_open":
            extra = sorted(k for k in args if k != "app_id")
            if extra:                    # g73-amendment-4b: app id ONLY; a caller never names a URL or a path
                return {"ok": False, "error": f"app_open takes only app_id; refused argument(s): {', '.join(extra)[:80]}"}
            return host.open(args.get("app_id"), actor, start_if_needed=True)
        if name == "app_propose_install":
            return host.propose_install(args.get("manifest_path"), actor)
        if name == "app_propose_uninstall":
            return host.propose_uninstall(args.get("app_id"), actor)
    except AppsError as exc:
        return {"ok": False, "error": str(exc), **{k: v for k, v in exc.detail.items() if k != "errors"}}
    return {"ok": False, "error": f"unknown Apps verb {name!r}"}


def alias_dispatch(host: AppsHost, legacy: Any, name: str, args: Dict[str, Any], caller: Optional[str]) -> Dict[str, Any]:
    """The converged legacy verbs (flag ON). ``legacy`` is the hub's :class:`~gnomon.hub.apps.AppLifecycle`."""
    meta = {"alias_of": ALIASES[name]["alias_of"], "alias_remove_after": ALIASES[name]["remove_after"]}
    installed = host._installed()
    if name == "launch_app":
        app_id = args.get("app_id")
        if isinstance(app_id, str) and app_id in installed:
            if args.get("args"):
                return {"ok": False, "error": "args are not accepted for a manifest app: its settings live on the "
                                              "Apps tab detail page", **meta}
            return {**dispatch(host, "app_start", {"app_id": app_id}, caller), **meta}
        return {**legacy.launch_app(app_id, args.get("args")), **meta}
    if name == "terminate_app":
        sel = args.get("handle_or_pid")
        if isinstance(sel, str) and sel in installed:
            return {**dispatch(host, "app_stop", {"app_id": sel}, caller), **meta}
        return {**legacy.terminate_app(sel), **meta}
    # list_running_apps: the legacy union PLUS the manifest apps
    out = legacy.list_running_apps()
    lst = dispatch(host, "app_list", {}, caller)
    out["manifest_apps"] = lst.get("apps", []) if lst.get("ok") else []
    out["manifest_pending"] = lst.get("pending", []) if lst.get("ok") else []
    if not lst.get("ok"):
        out["manifest_error"] = lst.get("error")
    return {**out, **meta}

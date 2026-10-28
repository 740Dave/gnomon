"""The Gnomon MCP service — tool surface + transport-agnostic dispatch.

One server owns BOTH the DSD surface AND the actuation/notice surface (D10). The
tool surface:

* ``inject(instance_id, text)``            — type text into the session
 (routed hub->proxy).
* ``request_compact(instance_id, direction)`` — relay the agent's self-directed
 ``/compact`` (the harness NEVER force-compacts, D23).
* ``context(instance_id)``                 — report the proxy's last-known
 ``used_percentage``.
* ``send_dsd_for_human_review(instance_id, dsd)`` — a NATIVE Gnomon tool, independent
  of ComposeBus/MQTT (D29). ⚠ Slice-2a STUBS the RENDER (returns "DSD rendering
  lands in slice 3") while wiring the full signature + a persistence seam
  (:class:`DSDStore`), so the DSD is durably captured now and the DSD-tab render
  is the only thing owed.
* ``launch_app`` / ``terminate_app`` / ``list_running_apps`` — the THIRD cluster,
 app lifecycle over a whitelist; implemented in
  ``apps.py`` and dispatched here (same server, D10).

The core is transport-agnostic: :meth:`GmMCPService.dispatch` maps
``(name, args) -> result dict`` (the usual stdio MCP server pattern,
). :meth:`GmMCPService.handle_jsonrpc` is the thin MCP JSON-RPC
shell (``initialize`` / ``tools/list`` / ``tools/call``) the localhost HTTP
transport drives; ``server.py`` supplies the streamable-HTTP wire.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ..profile import _validate_instance_id, ensure_contained
from .registry import SessionRegistry, UnknownSession


# --- the tool surface (MCP tool descriptors) --------------------------------

def _instance_arg() -> dict:
    return {"type": "string", "description": "The target session's instance_id."}


TOOLS: List[Dict[str, Any]] = [
    {
        "name": "inject",
        "description": (
            "OPERATOR-ONLY (requires 'Authorization: Bearer <operator token>' over "
            "HTTP). Type text into the target session as a submitted turn. "
            "Changed 2026-09-27 (PTY-hooks WS-D): by DEFAULT the inject is NON-"
            "disruptive — no ESC ESC; a working agent queues it — but the text "
            "CONCATENATES with any unsent typed input. clear=true opts IN to ESC "
            "ESC first (clears the prompt and INTERRUPTS a working agent). "
            "skip_clear is the legacy spelling of the default. (A proxy still "
            "running a pre-2026-09-27 harness keeps its old default: it clears.)"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_id": _instance_arg(),
                "text": {"type": "string", "description": "Text to inject."},
                "skip_clear": {
                    "type": "boolean",
                    "description": (
                        "Legacy: no leading ESC ESC (now the default on current "
                        "harnesses). Concatenates with unsent typed input."
                    ),
                },
                "clear": {
                    "type": "boolean",
                    "description": "Opt IN to a leading ESC ESC (interrupts a working agent). Default false.",
                },
            },
            "required": ["instance_id", "text"],
        },
    },
    {
        "name": "request_compact",
        "description": (
            "OPERATOR-ONLY (requires 'Authorization: Bearer <operator token>' over "
            "HTTP). Relay a /compact to a session. The harness never "
            "force-compacts; this only relays intent. A harnessed agent compacting "
            "ITSELF uses its local gnomon-self request_compact tool instead. "
            "⚠ FAIL-LOUD CAVEAT (F5c, deferred): the fail-loud compaction-start "
            "confirmation (ok:False when no compaction is observed) is NOT propagated "
            "back to THIS hub caller — this tool returns ok:True on successful ROUTING, "
            "not on confirmed compaction. A not-observed result is logged to the "
            "session's provenance and surfaced on the OPERATOR SCREEN only. The SELF "
            "path (the pilot's own gnomon-self request_compact) DOES propagate "
            "ok:False synchronously to the calling agent."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_id": _instance_arg(),
                "direction": {
                    "type": "string",
                    "description": "Optional /compact direction (summary steering).",
                },
                "clear": {
                    "type": "boolean",
                    "description": (
                        "Leading ESC ESC (clears the prompt; INTERRUPTS the target's "
                        "current turn) before /compact. Default TRUE (pilot ruling "
                        "2026-09-28: the ESC ESC is load-bearing for compaction). Set "
                        "false to opt OUT per call (non-disruptive, but NOT known to fire)."
                    ),
                },
            },
            "required": ["instance_id"],
        },
    },
    {
        "name": "context",
        "description": (
            "OPERATOR-ONLY (requires 'Authorization: Bearer <operator token>' over "
            "HTTP). Report the session's last-known context used_percentage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"instance_id": _instance_arg()},
            "required": ["instance_id"],
        },
    },
    {
        "name": "send_dsd_for_human_review",
        "description": (
            "Persist a Decision-Support-Document for human review. NOTE: slice-2a "
            "captures the DSD durably but the DSD-tab RENDER lands in slice 3."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "instance_id": _instance_arg(),
                "dsd": {
                    "type": "object",
                    "description": "The DSD payload (question, options, context).",
                },
            },
            "required": ["instance_id", "dsd"],
        },
    },
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)

#: The actuation cluster that DRIVES a session (fix-pass WS2b, 2026-09-27). Over
#: the HTTP transport these REQUIRE the operator credential — see
#: ``server.Hub`` — because the hub otherwise lets ANY localhost client drive ANY
#: session's REPL (flight finding #4). The gate lives at the transport, not in
#: :meth:`GmMCPService.dispatch`: in-process callers (the DSD round-trip, the
#: GUI) already hold the registry and are the operator by construction.
#: A harnessed child self-actuates through its proxy's LOCAL control socket
#: (``harness.self_mcp``), never through this surface, and holds no token.
OPERATOR_TOOLS = frozenset({"inject", "request_compact", "context", "launch_agent"})

#: PTY-hooks WS-G — the HUB launch verb. OFF by default: advertised in
#: ``tools/list`` and dispatchable ONLY when the service's
#: ``agent_launch_enabled()`` is true (machine config ``agent_launch_enabled``),
#: and operator-token-gated like the other actuation tools. It launches as the
#: OPERATOR (the hub process has no harness ancestor) — a depth-0 root.
LAUNCH_AGENT_TOOL: Dict[str, Any] = {
    "name": "launch_agent",
    "description": (
        "OPERATOR-ONLY, OFF BY DEFAULT (machine config agent_launch_enabled). Launch "
        "an agent from a profile: harnessed (terminal, or terminal='none' for a "
        "detached background harness) or headless (claude -p, background; output "
        "path returned). Every launch is logged."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "profile": {"type": "string"},
            "prompt": {"type": "string"},
            "headless": {"type": "boolean"},
            "terminal": {"type": "string"},
            "instance_id": {"type": "string"},
            "enable_agent_launch": {"type": "boolean"},
        },
        "required": ["profile"],
    },
}

# The THIRD cluster (rides-existing-hub / D10): app
# lifecycle on the SAME server. ``TOOLS`` above stays the DSD + actuation
# surface, unchanged; ``ALL_TOOLS`` is what ``tools/list`` advertises.
from .apps import (  # noqa: E402
    APP_LIFECYCLE_TOOL_NAMES,
    APP_LIFECYCLE_TOOLS,
    AppLifecycle,
    AppLifecycleError,
)

ALL_TOOLS: List[Dict[str, Any]] = TOOLS + APP_LIFECYCLE_TOOLS

#: what tools/list shows a bound caller for the DSD tool when the binding is ON. DERIVED
#: from the ``TOOLS`` entry (never hand-copied, Diamond F-P3-7): the same schema with instance_id optional, because
#: the hub files under the BOUND instance (an instance_id, if given, must equal it).
#: LAZY (round 3, R2-P3-3): this module is in every hook's import closure, so nothing here runs at import. The
#: derivation runs on first use (:func:`bound_dsd_tool`), is cached, and a failure REFUSES the bound DSD tool
#: (not listed, not dispatched) instead of raising: a broken edit can never break a hook's import.
def _derive_bound_dsd_tool() -> Dict[str, Any]:
    import copy

    base = next(t for t in TOOLS if t["name"] == "send_dsd_for_human_review")
    tool = copy.deepcopy(base)
    tool["description"] = (base["description"] + " With caller binding on, the hub files it under YOUR session "
                           "(from your launch credential); instance_id is optional and, if given, must be your own.")
    tool["inputSchema"]["properties"]["instance_id"] = {
        "type": "string", "description": "Optional: your own instance_id (the hub takes it from your credential)."}
    tool["inputSchema"]["required"] = [r for r in base["inputSchema"]["required"] if r != "instance_id"]
    return tool


_BOUND_DSD_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def bound_dsd_tool() -> Optional[Dict[str, Any]]:
    """The derived bound DSD tool, built on first use and cached; None when the derivation fails (logged once).
    Never raises."""
    if "tool" not in _BOUND_DSD_CACHE:
        try:
            tool: Optional[Dict[str, Any]] = _derive_bound_dsd_tool()
        except Exception as exc:  # noqa: BLE001 - fail closed for this tool, never at import or for the hub
            tool = None
            import sys as _sys

            _sys.stderr.write(f"[hub] bound DSD tool unavailable ({type(exc).__name__}: {exc}); "
                              "send_dsd_for_human_review is refused while the caller binding is on\n")
        _BOUND_DSD_CACHE["tool"] = tool
    return _BOUND_DSD_CACHE["tool"]

# (spec 13): the Apps host's seven verbs. Advertised and dispatchable ONLY while the service's
# ``apps_enabled()`` is true (apps_tab, default OFF); with the flag OFF the legacy three keep their
# exact earlier behaviour. There is NO confirm / decline verb: those are operator-only Apps-tab routes.
# Imported LAZILY (inside the functions below): this module is in every hook's import closure (g47b/g67).


#: every Apps verb name starts with this; the dispatcher routes on the PREFIX so no non-app tool ever imports the
#: Apps host (round 2, Diamond F5).
APP_VERB_PREFIX = "app_"


def _app_verbs():
    """The Apps verb descriptors, imported LAZILY. Raises on a broken appshost: callers contain it."""
    from ..appshost import verb_tools

    return verb_tools


def _is_app_verb(name: str) -> bool:
    try:
        return name in _app_verbs().APP_VERB_NAMES
    except Exception:  # noqa: BLE001
        return False


#: (spec 14, stage 1a): the six ``sched_*`` verbs. Advertised and dispatchable ONLY while the service's
#: ``sched_enabled()`` is true (scheduler, default OFF). Descriptors are read LAZILY (g47b/g67). There is NO
#: confirm verb: confirm / decline / cancel are operator-only Scheduler-tab routes.
SCHED_VERB_PREFIX = "sched_"


def _sched_verbs():
    """The scheduler verb descriptors, imported LAZILY. Raises on a broken scheduler package: callers contain it."""
    from ..scheduler import verb_tools

    return verb_tools


def _is_sched_verb(name: Any) -> bool:
    try:
        return isinstance(name, str) and name in _sched_verbs().SCHED_VERB_NAMES
    except Exception:  # noqa: BLE001
        return False


# --- the DSD persistence seam ----------------------------------------------

@dataclass
class DSDRecord:
    dsd_id: str
    instance_id: str
    path: str
    #: the send-time record of each listed document (sha256/size/mtime or a
    #: named status) + the loud warnings (a missing document still persists).
    documents: List[Dict[str, Any]] = field(default_factory=list)
    document_warnings: List[str] = field(default_factory=list)


class DSDStore:
    """Persists DSD payloads to disk (the seam; render is slice 3).

    Layout: ``<root>/<instance_id>/dsd/<dsd_id>.json``. ``root`` defaults to the
    agents home so a DSD lives beside the instance it came from. This is
    the persistence half of ``send_dsd_for_human_review``; the DSD tab that
    RENDERS these for the human is slice 3.
    """

    def __init__(
        self,
        root: Optional[str] = None,
        clock: Callable[[], float] = time.time,
        registry: Optional["SessionRegistry"] = None,
        doc_roots: Optional[List[str]] = None,
    ) -> None:
        from ..profile import default_intelligences_root

        self._root = os.path.abspath(os.path.expanduser(root or default_intelligences_root()))
        self._clock = clock
        # Optional registry so persist() can refuse an unknown session directly.
        # The service-layer _send_dsd also gates on the registry; this closes the
        # gap for any direct persist() call too (defense in depth).
        self._registry = registry
        # the allow-listed document roots (None = the operator's list, resolved at
        # call time from the real home). Injectable so tests never read the real home.
        self._doc_roots = doc_roots

    @contextlib.contextmanager
    def _mint_lock(self):
        """Exclusive flock on ``<root>/.dsd-mint.lock``: serialises the cross-dir id scan + publish across
        threads and processes (flock is per open file description, so separate opens exclude each other)."""
        import fcntl

        fd = os.open(os.path.join(self._root, ".dsd-mint.lock"), os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)             # closing releases the lock

    def persist(self, instance_id: str, dsd: Dict[str, Any], *, extra: Optional[Dict[str, Any]] = None) -> DSDRecord:
        # ⛔ SECURITY (slice-2a cold audit): instance_id here is MCP-TOOL-SUPPLIED
        # (an agent passes it to send_dsd_for_human_review). It names an on-disk
        # directory under _root; a traversal-shaped id ("../../etc/pwned") would
        # otherwise write a DSD record OUTSIDE the agents home. Reject
        # separators/traversal/null at this site, reusing profile.py's validator.
        _validate_instance_id(instance_id)
        # And it must name a REAL registered session, not an arbitrary/typo'd id
        # that would create a junk directory. When a registry is wired, refuse an
        # unregistered id. (The service layer enforces this too — see _send_dsd.)
        if self._registry is not None and not self._registry.is_registered(instance_id):
            raise ToolError(f"no registered session: {instance_id!r}")
        ts = self._clock()
        # a markdown decision that parses to ZERO options
        # is REFUSED here too (the direct path), persist is the ONE enforcement point (the hub send path calls it).
        _md = dsd.get("markdown") if isinstance(dsd, dict) else None
        if isinstance(_md, str) and _md.strip():
            from ..dsd.model import parse_dsd, zero_options_message

            _zmsg = zero_options_message(parse_dsd(_md))
            if _zmsg:
                raise ToolError(f"DSD context guard refused: {_zmsg}")
        dsd_dir = os.path.join(self._root, instance_id, "dsd")
        # ⛔ SECURITY (slice-2b cold audit MEDIUM): a SAFE-shaped id can still
        # escape _root if a symlink was pre-planted at <root>/<instance_id>;
        # makedirs(exist_ok=True) would follow it. Resolve-and-contain the target
        # dir BEFORE creating it, so a planted symlink is refused, not followed.
        ensure_contained(self._root, dsd_dir)
        os.makedirs(dsd_dir, exist_ok=True)
        ms = int(ts * 1000)
        # record each listed document's sha256/size/mtime AT SEND. A missing
        # document warns loudly (returned to the sender + kept in the record) but the DSD
        # still persists. Additive: a DSD with no **Documents:** line gets no new key, so
        # its record is byte-identical to before. Never lets a documents hiccup lose a DSD.
        doc_entries: List[Dict[str, Any]] = []
        doc_warnings: List[str] = []
        md = dsd.get("markdown") if isinstance(dsd, dict) else None
        if isinstance(md, str) and md.strip():
            try:
                from ..dsd.documents import describe_for_persist

                doc_entries, doc_warnings = describe_for_persist(md, self._doc_roots)
            except Exception as exc:  # noqa: BLE001
                doc_warnings = [f"DOCUMENTS NOT RECORDED: {type(exc).__name__}: {exc}"]
        # the id is minted from a millisecond clock, so two persists in
        # one ms used to share an id and the second os.replace() silently destroyed the first. Now the record
        # is written + fsynced to a unique temp file and PUBLISHED with os.link(), which fails (never
        # replaces) when the target exists; on a collision the ms value is bumped until a free id is found,
        # so the id stays ``dsd-<hex>`` for every reader. Other instances' dirs are checked too (readers
        # look an id up across every instance dir): the scan-then-link step runs under an flock on
        # <root>/.dsd-mint.lock (every writer, every process and thread, takes it), so an id is unique across
        # dirs too. Per-dir exclusivity (os.link) stays as the second belt. Only the scan + link are locked.
        while True:
            dsd_id = f"dsd-{ms:x}"
            path = os.path.join(dsd_dir, f"{dsd_id}.json")
            record = {
                "dsd_id": dsd_id,
                "instance_id": instance_id,
                "created_epoch": ts,
                "status": "pending-human-review",
                "dsd": dsd,
            }
            if extra:     # pre-review B1: additive record keys (replace_dsd's `replaces`); None = the record is exactly as before
                record.update(extra)
            if doc_entries:
                record["documents"] = doc_entries
            if doc_warnings:
                record["document_warnings"] = doc_warnings
            payload = (json.dumps(record, indent=2) + "\n").encode("utf-8")
            tmp = os.path.join(dsd_dir, f".{dsd_id}.{os.getpid()}.{secrets.token_hex(6)}.tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())
                try:
                    with self._mint_lock():
                        if any(os.path.lexists(os.path.join(d, f"{dsd_id}.json"))
                               for d in glob.glob(os.path.join(self._root, "*", "dsd")) if d != dsd_dir):
                            raise FileExistsError(dsd_id)   # taken in another instance dir
                        os.link(tmp, path)      # exclusive publish: raises FileExistsError, never overwrites
                except FileExistsError:
                    ms += 1
                    continue
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
            break
        try:
            dfd = os.open(dsd_dir, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:  # directory fsync is best-effort
            pass
        return DSDRecord(dsd_id=dsd_id, instance_id=instance_id, path=path,
                         documents=doc_entries, document_warnings=doc_warnings)


# --- the service ------------------------------------------------------------

class ToolError(Exception):
    """A tool call that failed with a message safe to return to the agent."""


class GmMCPService:
    """Maps tool calls onto the registry (hub->proxy) and the DSD store."""

    SERVER_NAME = "gnomon"
    SERVER_VERSION = "0.2.0"
    PROTOCOL_VERSION = "2025-06-18"

    def __init__(
        self,
        registry: SessionRegistry,
        dsd_store: Optional[DSDStore] = None,
        app_lifecycle: Optional[AppLifecycle] = None,
        agent_launcher: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        agent_launch_enabled: Optional[Callable[[], bool]] = None,
        caller_binding: bool = False,
        caller_audit: Optional[Any] = None,
        apps_host: Optional[Callable[[], Any]] = None,
        apps_enabled: Optional[Callable[[], bool]] = None,
    ) -> None:
        self._registry = registry
        # (hub_caller_binding, default OFF): when OFF every path below is today's,
        # byte-identical, and a ``principal`` argument is ignored.
        self._caller_binding = bool(caller_binding)
        self._caller_audit = caller_audit
        # the Apps host (a zero-arg factory, built lazily) and its flag (default OFF).
        self._apps_host_factory = apps_host
        self._apps_enabled = apps_enabled or (lambda: False)
        # the Scheduler (a zero-arg factory, built lazily) and its flag (default OFF); see set_scheduler().
        self._sched_factory: Optional[Callable[[], Any]] = None
        self._sched_enabled: Callable[[], bool] = lambda: False
        # PTY-hooks WS-G: the hub launch verb (None / disabled = not advertised).
        self._agent_launcher = agent_launcher
        self._agent_launch_enabled = agent_launch_enabled or (lambda: False)
        # App-lifecycle cluster. The default reads the whitelist from
        # ~/gnomon-data/apps.json LAZILY (per call), so constructing a service
        # never touches disk; the Controller injects a config-rooted one.
        self._apps = app_lifecycle if app_lifecycle is not None else AppLifecycle()
        # A default store is wired to the registry so persist() itself refuses an
        # unregistered/unsafe id. An INJECTED store is left as the caller built it
        # (tests supply one scoped to a tmp root); _send_dsd still gates on the
        # registry regardless, so the guarantee holds either way.
        self._dsd = dsd_store if dsd_store is not None else DSDStore(registry=registry)

    # -- caller binding ------------------------------

    @property
    def caller_binding(self) -> bool:
        return self._caller_binding

    def enable_caller_binding(self, audit: Optional[Any] = None) -> None:
        """Turn the binding ON for this service (the Hub does this when its flag is ON)."""
        self._caller_binding = True
        self._caller_audit = audit

    def _audit(self, event: str, **fields: Any) -> None:
        if self._caller_audit is None:
            return
        try:
            self._caller_audit.record(event, **fields)
        except Exception:  # noqa: BLE001 - auditing never breaks a call
            pass

    def _dispatch_bound(self, name: Any, args: Optional[Dict[str, Any]], principal: Any) -> Dict[str, Any]:
        """Binding ON: authorize against the ONE caller-scope registry, then dispatch with identity args taken
        from the binding. A refusal dispatches nothing, is audited, and is attributed from the binding only."""
        from .caller_binding import ACTOR_OPERATOR, CALLER_SCOPES, UNBOUND, Principal, _short, authorize

        p = principal if isinstance(principal, Principal) else UNBOUND
        decision = authorize(name, args, p)
        if decision.allowed and name == "send_dsd_for_human_review" and bound_dsd_tool() is None:
            decision = type(decision)(False, decision.args, decision.actor,
                                      "the bound DSD tool is unavailable (its schema derivation failed)")
        actor = decision.actor
        if not decision.allowed:
            scope = CALLER_SCOPES.get(name) if isinstance(name, str) else None
            given = {k: _short(args.get(k)) for k in (scope.identity_args if scope else ())
                     if isinstance(args, dict) and k in args}
            self._audit("call-refused", tool=name if isinstance(name, str) else repr(name),
                        reason=decision.reason, bound_instance=p.bound_instance, operator=p.operator,
                        caller_header=p.caller_header, via=p.via, identity_args_given=given)
            out: Dict[str, Any] = {"ok": False, "error": f"refused by caller binding: {decision.reason}",
                                   "refused": "caller-binding"}
        else:
            # WHO acted (operator ruling 2026-10-08): a BOUND agent's Apps verb is audited under the binding's instance id, verified
            # (it comes from the binding, never from a tool argument); the operator's actor is unchanged (no caller).
            bound_actor = actor if (actor and actor != ACTOR_OPERATOR and (_is_app_verb(name) or _is_sched_verb(name))) else None
            out = (self._dispatch(name, decision.args, bound_actor, verified_caller=True) if bound_actor
                   else self._dispatch(name, decision.args))
        self._note_feature_use(name, None, out, self._actor_id(actor, ACTOR_OPERATOR), from_binding=True)
        return out

    @staticmethod
    def _actor_id(actor: Optional[str], operator_marker: str) -> Optional[str]:
        if actor == operator_marker:
            from ..feature_events import VIEWER_ACTOR

            return VIEWER_ACTOR
        return actor

    # -- transport-agnostic dispatch core ------------------------------------

    def dispatch(self, name: str, args: Optional[Dict[str, Any]] = None, *,
                 caller: Optional[str] = None, principal: Optional[Any] = None) -> Dict[str, Any]:
        """``(tool name, args) -> result dict``. Never raises for a known tool's
        expected failure — it returns ``{"ok": False, "error": ...}``.

 X26: this is the SINGLE dispatch function, so ONE feature event per
        tools/call is emitted here (a new tool is covered by default). ``caller`` is the identity the
        TRANSPORT established (the operator for an operator-token call; None when unknown).

 with the binding ON, ``principal`` (a ``caller_binding.Principal``) is the caller,
        checked HERE so an in-process caller cannot bypass it; None means UNBOUND. ``caller`` is then ignored."""
        if self._caller_binding:
            return self._dispatch_bound(name, args, principal)
        out = self._dispatch(name, args, caller)
        self._note_feature_use(name, args, out, caller)
        return out

    @staticmethod
    def _note_feature_use(name: Any, args: Any, out: Any, caller: Optional[str], *,
                          from_binding: bool = False) -> None:
        """One event per tool call, after the verdict. The actor is the caller's identity when the hub
        KNOWS it (the transport's operator authentication; for ``send_dsd_for_human_review`` the registered
        sending instance, known only when the call succeeded); when the hub cannot identify the caller the
        event is DROPPED (counted), never written with a guessed actor. The feature id is the hub's OWN tool
        table, never the request body. Guarded.

        ``from_binding`` (caller binding ON): ``caller`` came from the binding and is the ONLY source; the
        ``args['instance_id']`` fallback is never used."""
        try:
            from .. import feature_events as _fe
            from . import feature_ids as _fs

            if _fs.hub_tool_feature_id(name) is None:
                return
            ok = isinstance(out, dict) and bool(out.get("ok"))
            who = caller
            if (not from_binding and who is None and name == "send_dsd_for_human_review" and ok
                    and isinstance(args, dict)):
                who = args.get("instance_id") if isinstance(args.get("instance_id"), str) else None
            _fe.set_process_kind("web")
            _fe.feature_event(_fs.hub_tool_feature_id(name), who, "hub-mcp-tool", outcome="ok" if ok else "error")
        except Exception:  # noqa: BLE001
            pass

    def _dispatch(self, name: str, args: Optional[Dict[str, Any]] = None,
                  caller: Optional[str] = None, *, verified_caller: bool = False) -> Dict[str, Any]:
        args = args or {}
        try:
            if name.startswith(APP_VERB_PREFIX):
                return self._apps_verb(name, args, caller, verified_caller)
            if name.startswith(SCHED_VERB_PREFIX):
                return self._sched_verb(name, args, caller, verified_caller)
            if name in APP_LIFECYCLE_TOOL_NAMES and self.apps_enabled():
                return self._apps_alias(name, args, caller)
            if name == "inject":
                return self._inject(args)
            if name == "request_compact":
                return self._request_compact(args)
            if name == "context":
                return self._context(args)
            if name == "send_dsd_for_human_review":
                return self._send_dsd(args)
            if name in APP_LIFECYCLE_TOOL_NAMES:
                return self._app_lifecycle(name, args)
            if name == "launch_agent":
                return self._launch_agent(args)
            return {"ok": False, "error": f"unknown tool: {name!r}"}
        except ToolError as exc:
            return {"ok": False, "error": str(exc)}
        except AppLifecycleError as exc:
            out: Dict[str, Any] = {"ok": False, "error": str(exc)}
            out.update(exc.detail)
            return out

    @property
    def app_lifecycle(self) -> AppLifecycle:
        return self._apps

    # -- ---------------------------------------------------------------
    def apps_enabled(self) -> bool:
        try:
            return bool(self._apps_host_factory is not None and self._apps_enabled())
        except Exception:  # noqa: BLE001 - a broken flag reads as OFF
            return False

    def apps_host(self) -> Any:
        assert self._apps_host_factory is not None
        return self._apps_host_factory()

    def _apps_verb(self, name: str, args: Dict[str, Any], caller: Optional[str],
                   verified_caller: bool = False) -> Dict[str, Any]:
        if not self.apps_enabled():
            raise ToolError(f"{name} is disabled (apps_tab is off)" if _is_app_verb(name)
                            else f"unknown tool: {name!r}")
        try:
            from ..appshost import verbs as _av
            known = name in _av.APP_VERB_NAMES
        except Exception as exc:  # noqa: BLE001 - F5: a broken Apps host fails ONLY the app verbs, loudly
            raise ToolError(f"{name}: the Apps host is unavailable ({type(exc).__name__}: {exc}); "
                            "every other hub verb still works")
        if not known:
            raise ToolError(f"unknown tool: {name!r}")
        return _av.dispatch(self.apps_host(), name, args, caller, verified_caller=verified_caller)

    # -- ---------------------------------------------------------------
    def set_scheduler(self, factory: Optional[Callable[[], Any]], enabled: Optional[Callable[[], bool]]) -> None:
        """Wire the Scheduler (the Controller calls this after building the Hub). Without it no sched verb exists."""
        self._sched_factory = factory
        self._sched_enabled = enabled or (lambda: False)

    def sched_enabled(self) -> bool:
        try:
            return bool(self._sched_factory is not None and self._sched_enabled())
        except Exception:  # noqa: BLE001 - a broken flag reads as OFF
            return False

    def _sched_verb(self, name: str, args: Dict[str, Any], caller: Optional[str],
                    verified_caller: bool = False) -> Dict[str, Any]:
        if not self.sched_enabled() or not _is_sched_verb(name):
            raise ToolError(f"{name} is disabled (scheduler is off)" if _is_sched_verb(name)
                            else f"unknown tool: {name!r}")
        try:
            from ..scheduler import verbs as _sv
        except Exception as exc:  # noqa: BLE001 - a broken scheduler fails ONLY the sched verbs, loudly
            raise ToolError(f"{name}: the scheduler is unavailable ({type(exc).__name__}: {exc}); "
                            "every other hub verb still works")
        assert self._sched_factory is not None
        return _sv.dispatch(self._sched_factory(), name, args, caller, verified_caller=verified_caller)

    def _apps_alias(self, name: str, args: Dict[str, Any], caller: Optional[str]) -> Dict[str, Any]:
        if name == "terminate_app" and "handle_or_pid" not in args:
            raise AppLifecycleError("'handle_or_pid' is required")
        try:
            from ..appshost import verbs as _av
        except Exception as exc:  # noqa: BLE001 - F5: the legacy verb keeps working, and says why it is legacy-only
            out = self._app_lifecycle(name, args)
            out["apps_host_error"] = (f"the Apps host is unavailable ({type(exc).__name__}: {exc}); "
                                      "answered by the legacy launcher only")
            return out
        return _av.alias_dispatch(self.apps_host(), self._apps, name, args, caller)

    def _app_lifecycle(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        if name == "launch_app":
            return self._apps.launch_app(args.get("app_id"), args.get("args"))
        if name == "terminate_app":
            if "handle_or_pid" not in args:
                raise AppLifecycleError("'handle_or_pid' is required")
            return self._apps.terminate_app(args.get("handle_or_pid"))
        return self._apps.list_running_apps()

    # -- verbs ---------------------------------------------------------------

    def launch_agent_enabled(self) -> bool:
        try:
            return bool(self._agent_launcher is not None and self._agent_launch_enabled())
        except Exception:  # noqa: BLE001 - a broken flag reads as OFF
            return False

    def tools(self, principal: Optional[Any] = None) -> List[Dict[str, Any]]:
        """What ``tools/list`` advertises: ALL_TOOLS (+ launch_agent iff enabled, + the Apps verbs iff
        apps_tab is on). With the caller binding ON, only the tools THIS caller could call
        (``principal``; None = unbound), and the DSD tool's instance_id is optional (the hub takes it from the
        binding)."""
        everything = ALL_TOOLS + [LAUNCH_AGENT_TOOL] if self.launch_agent_enabled() else list(ALL_TOOLS)
        if self.apps_enabled():
            try:
                everything = everything + list(_app_verbs().APP_VERB_TOOLS)
            except Exception:  # noqa: BLE001 - F5: a broken Apps host hides only the Apps verbs
                pass
        if self.sched_enabled():
            try:
                everything = everything + list(_sched_verbs().SCHED_VERB_TOOLS)
            except Exception:  # noqa: BLE001 - a broken scheduler hides only the sched verbs
                pass
        if not self._caller_binding:
            return everything
        from .caller_binding import visible

        out: List[Dict[str, Any]] = []
        for t in everything:
            if not visible(t["name"], principal):
                continue
            if t["name"] == "send_dsd_for_human_review":
                bt = bound_dsd_tool()
                if bt is None:
                    continue  # derivation failed: the bound DSD tool is refused, never advertised
                t = bt
            out.append(t)
        return out

    def _launch_agent(self, args: Dict[str, Any]) -> Dict[str, Any]:
        if not self.launch_agent_enabled():
            raise ToolError("launch_agent is disabled (machine config agent_launch_enabled=false; "
                            "the pilot has not ratified autonomous launching)")
        profile = _require_str(args, "profile")
        clean: Dict[str, Any] = {"profile": profile}
        for key, typ in (("prompt", str), ("terminal", str), ("instance_id", str), ("headless", bool),
                         ("enable_agent_launch", bool)):
            if key in args and args[key] is not None:
                if not isinstance(args[key], typ):
                    raise ToolError(f"'{key}' must be a {typ.__name__}")
                clean[key] = args[key]
        assert self._agent_launcher is not None
        return self._agent_launcher(clean)

    def _inject(self, args: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = _require_str(args, "instance_id")
        text = _require_str(args, "text")
        skip_clear = args.get("skip_clear", False)
        if not isinstance(skip_clear, bool):
            raise ToolError("'skip_clear' must be a boolean")
        clear = args.get("clear", False)
        if not isinstance(clear, bool):
            raise ToolError("'clear' must be a boolean")
        command: Dict[str, Any] = {"cmd": "inject", "text": text}
        if skip_clear:
            # Only carried when set, so the default frame is byte-identical to
            # the pre-fix one.
            command["skip_clear"] = True
        if clear:
            # PTY-hooks WS-D: the opt-in clear, only carried when set.
            command["clear"] = True
        self._route(instance_id, command)
        out: Dict[str, Any] = {"ok": True, "action": "inject", "instance_id": instance_id}
        if skip_clear:
            out["skip_clear"] = True
        if clear:
            out["clear"] = True
        return out

    def _request_compact(self, args: Dict[str, Any]) -> Dict[str, Any]:
        # ⚠ F5c (DEFERRED — see the request_compact tool description and
        # REMEDIATION-P3-SUMMARY.md): this returns ok:True on successful ROUTING of
        # the /compact intent, NOT on a CONFIRMED compaction. The fail-loud
        # compaction-start confirmation (ok:False + reason when no compaction is
        # observed) is NOT propagated back to this hub caller — hub/server.py's
        # _handle_frame drops the proxy's compact reply (it carries no `type`), and
        # correlating an async reply to a pending request across the rendezvous
        # duplex socket is out of scope for the additive fix pass. A not-observed
        # result IS recorded to the session's provenance log (F5a) and surfaced on
        # the OPERATOR SCREEN via tap.feedback (F5b). The SELF path — the pilot's own
        # gnomon-self request_compact tool over the local control socket —
        # reads the actuator's reply synchronously and DOES propagate ok:False.
        instance_id = _require_str(args, "instance_id")
        direction = args.get("direction", "")
        if direction is None:
            direction = ""
        if not isinstance(direction, str):
            raise ToolError("'direction' must be a string")
        # F2 (2026-09-28): the operator path is opt-in/out per call too. clear
        # DEFAULTS TRUE (operator ruling: ESC ESC is load-bearing for compaction) and the
        # resolved boolean is threaded EXPLICITLY into the frame so the actuator does
        # not re-default an opt-out. Guarded: a non-boolean is refused loudly.
        clear = args.get("clear", True)
        if not isinstance(clear, bool):
            raise ToolError("'clear' must be a boolean")
        self._route(instance_id, {"cmd": "compact", "direction": direction, "clear": clear})
        return {
            "ok": True,
            "action": "request_compact",
            "instance_id": instance_id,
            "direction": direction,
            "clear": clear,
        }

    def _context(self, args: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = _require_str(args, "instance_id")
        try:
            status = self._registry.status(instance_id)
        except UnknownSession:
            raise ToolError(f"no registered session: {instance_id!r}")
        return {
            "ok": True,
            "action": "context",
            "instance_id": instance_id,
            "used_percentage": status["used_percentage"],
            "alive": status["alive"],
        }

    def _send_dsd(self, args: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = _require_str(args, "instance_id")
        dsd = args.get("dsd")
        if not isinstance(dsd, dict):
            raise ToolError("'dsd' must be an object")
        # ⛔ SECURITY: gate on the registry at the service layer — the id is
        # agent-supplied and must name a real session, never write a junk/escaping
        # directory. persist() re-checks (defense in depth); a traversal-shaped id
        # surfaces as ValueError there, which we normalise to a soft ToolError.
        if not self._registry.is_registered(instance_id):
            raise ToolError(f"no registered session: {instance_id!r}")
        # ⛔ CONTEXT GUARD (slice 3a). When the payload carries a
        # DSD markdown document (``dsd["markdown"]``), it MUST pass the same
        # validate_dsd predicate the DSD tab (slice 3b) gates on: every decision
        # carries What-this-is + Why-it-matters. A validation failure PUBLISHES
        # NOTHING and reports loudly ([CXC-DSDV-MCP] no-silent-fallback). A payload
        # WITHOUT markdown is the slice-2a generic-object capture path (unchanged).
        markdown = dsd.get("markdown")
        if isinstance(markdown, str) and markdown.strip():
            from ..dsd import parse_dsd, validate_dsd

            verdict = validate_dsd(parse_dsd(markdown))
            if not verdict.ok:
                raise ToolError(f"DSD context guard refused: {verdict.message}")
        try:
            record = self._dsd.persist(instance_id, dsd)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        # ⚠ RENDER STUB (slice 3). The DSD is captured durably; the human-facing
        # DSD tab render is not built yet.
        out: Dict[str, Any] = {
            "ok": True,
            "action": "send_dsd_for_human_review",
            "instance_id": instance_id,
            "dsd_id": record.dsd_id,
            "persisted_path": record.path,
            "rendered": False,
            "note": "DSD rendering lands in slice 3",
        }
        # additive keys, ONLY when the DSD lists documents (otherwise the
        # result is byte-identical to before). A missing/refused document is a loud
        # warning; the DSD was still sent.
        if record.documents:
            out["documents_recorded"] = sum(1 for e in record.documents if e.get("status") == "ok")
            out["documents_listed"] = len(record.documents)
        if record.document_warnings:
            out["warnings"] = list(record.document_warnings)
        return out

    def _route(self, instance_id: str, command: Dict[str, Any]) -> None:
        try:
            self._registry.route(instance_id, command)
        except UnknownSession:
            raise ToolError(f"no registered session: {instance_id!r}")

    # -- thin MCP JSON-RPC shell ---------------------------------------------

    def handle_jsonrpc(self, request: Dict[str, Any], *, caller: Optional[str] = None,
                       principal: Optional[Any] = None) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC 2.0 request. Returns the response, or ``None`` for
        a notification (a request without an ``id``). ``principal`` is used only
        with the caller binding ON (tools/list filtering and the dispatch check)."""
        rid = request.get("id")
        method = request.get("method")
        params = request.get("params") or {}

        if method == "initialize":
            return _rpc_result(rid, {
                "protocolVersion": self.PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": self.SERVER_NAME, "version": self.SERVER_VERSION},
            })
        if method in ("notifications/initialized", "initialized"):
            return None  # notification, no response
        if method == "tools/list":
            if self._caller_binding:
                return _rpc_result(rid, {"tools": self.tools(principal)})
            return _rpc_result(rid, {"tools": self.tools()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str):
                return _rpc_error(rid, -32602, "tools/call requires a string 'name'")
            if self._caller_binding:
                result = self.dispatch(name, arguments, principal=principal)
            else:
                result = self.dispatch(name, arguments, caller=caller)
            is_error = not result.get("ok", False)
            return _rpc_result(rid, {
                "content": [{"type": "text", "text": json.dumps(result)}],
                "isError": is_error,
            })
        if rid is None:
            return None
        return _rpc_error(rid, -32601, f"method not found: {method!r}")


# -- helpers -----------------------------------------------------------------

def _require_str(args: Dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val:
        raise ToolError(f"'{key}' is required and must be a non-empty string")
    return val


def _rpc_result(rid: Any, result: Dict[str, Any]) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _rpc_error(rid: Any, code: int, message: str) -> Dict[str, Any]:
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

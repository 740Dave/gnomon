"""Hub caller binding: the hub knows which agent is calling from a credential it issued, never from a tool
argument (revision 2; checklist g60(b) and g60c). Flag ``hub_caller_binding``,
default OFF; OFF is today's hub, byte-identical.

How it works (ON):

* CREDENTIAL. Per launch, the harness mints a random 256-bit token (:func:`new_token`), writes it ONLY into that
  session's ``--mcp-config`` hub entry as the ``X-Gnomon-Caller`` header (a 0600 file, deleted on clean exit), and
  sends ONLY ``sha256(token)`` in its rendezvous ``register`` frame (key :data:`REGISTER_HASH_KEY`). There is no
  shared secret and no claim grammar: nothing on disk lets anyone forge an arbitrary instance.
* VERIFICATION. The hub keeps, per registered instance, the hash of the CURRENT holder. A call presenting a token
  whose sha256 matches (``hmac.compare_digest``) is the BOUND caller of that instance; anything else is UNBOUND.
  A re-register for a held instance without the holder's hash is PARKED and audited, never a silent supersede.
* REGISTRATION (round 2, Diamond F-P2-1/2/3). A register for an instance with NO current holder (first launch,
  after a hub restart, after the holder left, or a parked connection answering ``vacated``) is accepted ONLY if a
  PENDING-LAUNCH RECORD for exactly that instance and that hash exists (:func:`write_pending_record`): a 0600 file
  in the 0700 machine-local credential dir, written by the launching harness before every registration attempt,
  read by the hub through the hardened reader, rejected after :data:`PENDING_TTL_SECONDS`, and CONSUMED on bind.
  A parked connection is therefore never promoted on its own, and a hash in one instance's record cannot bind any
  other instance. A register with no hash, or with no matching record, is refused and audited.
* SCOPES. :data:`CALLER_SCOPES` is the ONE registry: every hub tool declares a ``caller_scope`` and the
  ``identity_args`` that name an identity. An unscoped tool is refused (fail closed). :func:`authorize` is called
  from ``GmMCPService.dispatch`` (which takes a :class:`Principal`), so in-process callers cannot bypass it; the
  HTTP transport only ESTABLISHES the principal.
* HEADER PRECEDENCE. The operator Bearer token wins only on operator-scope tools; on a bound-agent tool the
  identity comes from the binding alone. A duplicate or malformed caller header means UNBOUND.

Why ``X-Gnomon-Caller`` and not ``Authorization``: ``Authorization: Bearer`` already carries the operator token,
and keeping the two credentials in distinct headers keeps the precedence rule mechanical. Verified at the object
(2026-10-02, Claude Code 2.1.287, a header-logging stub server): the static ``headers`` map of an ``--mcp-config``
http entry is sent on every request (``server/discover``, ``initialize``, the initialized notification, the GET
stream, ``tools/list``), with ``Host: 127.0.0.1:<port>`` and no ``Origin``.

THREAT-MODEL BOUND (stated exactly): this stops one agent acting as another through tool arguments (a mistake or
a prompt injection), stops a rogue re-registration from silently taking over an instance, and stops a process
that cannot write the credential dir from registering ANY instance. It does NOT stop a same-uid process that reads
a live session's 0600 credential file (that yields that one instance), nor one that WRITES a pending-launch record
into the credential dir (that lets it register any instance that has no current holder). While agent tool limits
are unenforced, a Bash-capable agent can do both, so this is not a hostile-agent defence.

stdlib only; imported by the hub (``hub/server.py``, ``hub/mcp.py``) and, for the token helpers only, by the
harness's lazily loaded ``harness/session_mcp.py``.
"""

from __future__ import annotations

# Top-level imports are STDLIB ONLY (this module is in every hook's import closure via launcher -> hub.server;
# g47). Package modules are imported lazily inside the functions that need them.
import collections
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

FLAG_NAME = "hub_caller_binding"

#: The request header that carries the per-launch caller token.
CALLER_HEADER = "X-Gnomon-Caller"
#: The register-frame key that carries sha256(token) (hex). Never the token itself.
REGISTER_HASH_KEY = "caller_hash"
#: rendezvous.json key the hub publishes (only when ON; an absent key means off, so OFF stays byte-identical).
RENDEZVOUS_BINDING_KEY = "binding"
BINDING_ON = "on"
BINDING_OFF = "off"

TOKEN_BYTES = 32  # 256 bits
_HEX64 = re.compile(r"\A[0-9a-f]{64}\Z")

# --- the caller scopes -------------------------------------------------------------------------------------------

OPERATOR = "operator"
BOUND_AGENT = "bound-agent"
ANY = "any"
#: ``context``: the operator, or a bound agent asking about its OWN instance.
OPERATOR_OR_BOUND_SELF = "operator-or-bound-self"
#: ``refused``: nobody may call it through the hub while the binding is ON (not even the operator Bearer), and it is
#: never advertised. An EXPLICIT, reviewed "no" for act-class tools whose scope has not been designed yet (the Apps
#: host's act-class verbs, owner ruling 2026-10-04): it is a registry entry, never the absence of one.
REFUSED = "refused"
SCOPES = frozenset({OPERATOR, BOUND_AGENT, ANY, OPERATOR_OR_BOUND_SELF, REFUSED})


@dataclass(frozen=True)
class ToolScope:
    caller_scope: str
    #: Top-level argument names that carry an identity. Checked against (and filled from) the binding for a
    #: bound-agent caller; nested payloads (``dsd.*``) are never identity.
    identity_args: Tuple[str, ...] = ()


#: THE ONE registry (g60c). Every hub tool appears here; ``tests/test_hub_caller_binding.py`` enumerates it
#: against the hub's tool surface so a new tool cannot ship unscoped. Scopes per the design's table:
#: inject / request_compact = operator; context = operator or bound-self; launch_agent = operator (so it is hidden
#: from agents' tools/list by its scope alone); send_dsd_for_human_review = bound-agent, identity from the binding only; launch_app / terminate_app =
#: operator until a plug-in needs them (then bound-agent + a g59c role allowlist + pid ownership);
#: list_running_apps = bound-agent. A future ``open_plugin`` is bound-agent.
CALLER_SCOPES: Dict[str, ToolScope] = {
    "inject": ToolScope(OPERATOR),
    "request_compact": ToolScope(OPERATOR),
    "context": ToolScope(OPERATOR_OR_BOUND_SELF, identity_args=("instance_id",)),
    "launch_agent": ToolScope(OPERATOR),
    "send_dsd_for_human_review": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
    "launch_app": ToolScope(OPERATOR),
    "terminate_app": ToolScope(OPERATOR),
    "list_running_apps": ToolScope(BOUND_AGENT),
    # --- the Apps host verbs (ported from live 2e9264a / f8e4ce2; apps_tab). Owner ruling 2026-10-04:
    # read-class takes the bound-agent read scope. Operator ruling 2026-10-08: any identified (bound) agent may
    # start, stop and open apps and PROPOSE installs (installs still need the operator's confirmation on the Apps tab; the Apps
    # host's own checks are unchanged). app_propose_uninstall stays REFUSED: it was not in his selection.
    "app_list": ToolScope(BOUND_AGENT),
    "app_status": ToolScope(BOUND_AGENT),
    "app_start": ToolScope(BOUND_AGENT),
    "app_stop": ToolScope(BOUND_AGENT),
    "app_open": ToolScope(BOUND_AGENT),
    "app_propose_install": ToolScope(BOUND_AGENT),
    "app_propose_uninstall": ToolScope(REFUSED),
    # --- the Scheduler verbs (stage 1a; scheduler). All BOUND_AGENT: the scheduler acts only for a
    # VERIFIED session. identity_args=("instance_id",) makes authorize() FILL instance_id from the binding (and refuse a
    # mismatch), which is the ONLY way the handler learns the verified id: under binding a handler receives no
    # principal. The Apps verbs' arrangement (caller=None, "unidentified hub caller") would break the own-session rule.
    "sched_list": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
    "sched_status": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
    "sched_propose": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
    "sched_propose_remove": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
    "sched_run_now": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
    "sched_log": ToolScope(BOUND_AGENT, identity_args=("instance_id",)),
}

#: JSON-RPC methods that are not tool calls: any caller (tools/list is FILTERED by the caller's scope).
METHOD_SCOPES: Dict[str, str] = {
    "initialize": ANY,
    "tools/list": ANY,
    "notifications/initialized": ANY,
    "initialized": ANY,
}


# --- tokens ------------------------------------------------------------------------------------------------------

def new_token() -> str:
    """A fresh per-launch caller token: 256 random bits, 64 lowercase hex chars."""
    return secrets.token_hex(TOKEN_BYTES)


def token_hash(token: str) -> str:
    """sha256(token) as lowercase hex: what the register frame carries and the hub stores."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def is_wellformed_token(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


def is_wellformed_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX64.match(value))


# --- the principal -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Principal:
    """WHO is calling, as established by a transport (or an in-process caller). Never derived from arguments.

    ``caller_header`` records what the transport saw: ``absent`` | ``bound`` | ``unknown`` (well-formed, no
    holder matches) | ``malformed`` | ``duplicate``. Only ``bound`` yields a ``bound_instance``."""

    operator: bool = False
    bound_instance: Optional[str] = None
    caller_header: str = "absent"
    via: str = "none"

    @classmethod
    def in_process_operator(cls) -> "Principal":
        """For in-process holders of the hub (the GUI, the web backend): they are the operator by construction."""
        return cls(operator=True, caller_header="absent", via="in-process")


UNBOUND = Principal()


@dataclass(frozen=True)
class Decision:
    allowed: bool
    args: Dict[str, Any]
    #: ``"operator"`` | the bound instance id | None (no identity known).
    actor: Optional[str]
    reason: str = ""


ACTOR_OPERATOR = "operator"


def _short(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[:limit] + "...'"


def authorize(name: Any, args: Optional[Mapping[str, Any]], principal: Optional[Principal],
              scopes: Optional[Mapping[str, ToolScope]] = None) -> Decision:
    """The binding check. Returns the args to dispatch with (identity args FILLED from the binding for a
    bound-agent caller) or a refusal with its reason. Fail closed: an unscoped tool or an unknown scope refuses."""
    reg = CALLER_SCOPES if scopes is None else scopes
    p = principal if isinstance(principal, Principal) else UNBOUND
    clean: Dict[str, Any] = dict(args) if isinstance(args, Mapping) else {}
    scope = reg.get(name) if isinstance(name, str) else None
    if scope is None:
        return Decision(False, clean, None, f"unscoped tool {name!r}: every hub tool must declare a caller_scope "
                        "in the caller-scope registry (fail closed)")
    cs = scope.caller_scope
    if cs not in SCOPES:
        return Decision(False, clean, None, f"tool {name!r} declares an unknown caller_scope {cs!r} (fail closed)")
    if cs == ANY:
        return Decision(True, clean, ACTOR_OPERATOR if p.operator else p.bound_instance)
    if cs == REFUSED:
        return Decision(False, clean, ACTOR_OPERATOR if p.operator else p.bound_instance,
                        f"{name!r} is act-class and has no reviewed caller scope yet: refused while the caller "
                        "binding is on (fail closed)")
    if cs == OPERATOR:
        if p.operator:
            return Decision(True, clean, ACTOR_OPERATOR)
        return Decision(False, clean, p.bound_instance,
                        f"{name!r} is operator-scope: it needs 'Authorization: Bearer <operator token>'")
    if cs == OPERATOR_OR_BOUND_SELF and p.operator:
        return Decision(True, clean, ACTOR_OPERATOR)  # the operator Bearer wins only on operator-scope tools
    # bound-agent (or the bound-self half of operator-or-bound-self): identity from the binding ONLY.
    if not p.bound_instance:
        return Decision(False, clean, None, f"{name!r} needs a bound caller (caller header {p.caller_header}); "
                        f"a launched session presents its per-launch '{CALLER_HEADER}' credential")
    for key in scope.identity_args:
        given = clean.get(key)
        if given is not None and given != p.bound_instance:
            return Decision(False, clean, p.bound_instance,
                            f"identity argument {key}={_short(given)} does not match the bound caller "
                            f"{p.bound_instance!r}")
        clean[key] = p.bound_instance
    return Decision(True, clean, p.bound_instance)


def visible(name: str, principal: Optional[Principal],
            scopes: Optional[Mapping[str, ToolScope]] = None) -> bool:
    """Is ``name`` advertised in tools/list to this caller? Only a tool the caller could call; an unscoped tool is
    never advertised."""
    reg = CALLER_SCOPES if scopes is None else scopes
    p = principal if isinstance(principal, Principal) else UNBOUND
    scope = reg.get(name)
    if scope is None or scope.caller_scope not in SCOPES:
        return False
    cs = scope.caller_scope
    if cs == ANY:
        return True
    if cs == REFUSED:
        return False
    if cs == OPERATOR:
        return p.operator
    if cs == OPERATOR_OR_BOUND_SELF:
        return p.operator or bool(p.bound_instance)
    return bool(p.bound_instance)


# --- the HTTP transport's half: header parsing, Host / Origin -----------------------------------------------------

def principal_from_headers(caller_values: Optional[Sequence[str]], *, operator: bool,
                           resolve: Callable[[str], Optional[str]]) -> Principal:
    """Build the principal from the request's caller-header values (ALL of them; ``get_all``) and whether the
    operator Bearer matched. ``resolve(token) -> instance_id | None`` is the hub's holder lookup."""
    values = list(caller_values or [])
    if len(values) > 1:
        return Principal(operator=operator, caller_header="duplicate", via="http")
    if not values:
        return Principal(operator=operator, caller_header="absent", via="http")
    token = values[0].strip() if isinstance(values[0], str) else ""
    if not is_wellformed_token(token):
        return Principal(operator=operator, caller_header="malformed", via="http")
    iid = resolve(token)
    if iid is None:
        return Principal(operator=operator, caller_header="unknown", via="http")
    return Principal(operator=operator, bound_instance=iid, caller_header="bound", via="http")


_LOOPBACK_NAMES = frozenset({"127.0.0.1", "::1", "localhost"})


def _host_port(value: str) -> Tuple[Optional[str], Optional[int]]:
    try:
        parts = urlsplit("//" + value)
        return parts.hostname, parts.port
    except ValueError:
        return None, None


def host_origin_ok(host_values: Optional[Sequence[str]], origin_values: Optional[Sequence[str]],
                   server_port: Optional[int]) -> Tuple[bool, str]:
    """/mcp accepts only a loopback ``Host`` naming this server's port, and an ``Origin`` that is absent or the
    same loopback origin, so a browser page cannot POST to the hub (DNS rebinding / cross-site)."""
    hosts = list(host_values or [])
    if len(hosts) != 1:
        return False, f"Host header count {len(hosts)} (exactly one required)"
    host, port = _host_port(hosts[0].strip())
    if host not in _LOOPBACK_NAMES:
        return False, "Host is not loopback"
    if server_port is not None and port != server_port:
        return False, "Host port is not this hub's port"
    origins = list(origin_values or [])
    if not origins:
        return True, ""
    if len(origins) != 1:
        return False, "duplicate Origin header"
    try:
        o = urlsplit(origins[0].strip())
        o_port = o.port
    except ValueError:
        return False, "malformed Origin"
    if o.scheme != "http" or o.hostname not in _LOOPBACK_NAMES or o_port != port:
        return False, "Origin is not this hub's own loopback origin"
    return True, ""


# --- pending-launch records (round 2: the registration credential) -------------------------------------------------

#: The credential dir's name under the machine-local, per-uid run base (shared with the 0600 --mcp-config files).
CREDENTIAL_DIRNAME = "caller-credentials"
#: A record older than this (or dated in the future beyond the skew) is refused. The harness rewrites its record
#: before every registration attempt, so a live harness always presents a fresh one.
PENDING_TTL_SECONDS = 600.0
_PENDING_FUTURE_SKEW = 60.0
_PENDING_MAX_BYTES = 4096


def credential_dir() -> str:
    """``<run base>/caller-credentials`` (0700, never a synced path)."""
    from .rendezvous import run_base_dir  # lazy: keep top-level imports stdlib-only

    return os.path.join(run_base_dir(), CREDENTIAL_DIRNAME)


def pending_record_path(instance_id: str, caller_hash: str, directory: Optional[str] = None) -> str:
    """Keyed by the instance AND the hash, so the hub looks up exactly (instance, presented hash) and a relaunch's
    record never overwrites the running launch's."""
    iid = hashlib.sha256(instance_id.encode("utf-8")).hexdigest()[:24]
    return os.path.join(directory or credential_dir(), f"pending-{iid}-{caller_hash[:24]}.json")


def write_pending_record(instance_id: str, session_id: Optional[str], caller_hash: str, *,
                         issued_at: Optional[float] = None, directory: Optional[str] = None) -> str:
    """Write ``{instance_id, session_id, caller_hash, issued_at}`` 0600 (``os.open`` + ``O_EXCL`` tmp + atomic
    replace) into the 0700 credential dir. Returns the path. Raises OSError on failure."""
    from .rendezvous import _secure_dir, run_base_dir  # lazy

    if not is_wellformed_hash(caller_hash):
        raise ValueError("caller_hash must be 64 lowercase hex chars")
    d = directory or credential_dir()
    if directory is None:
        _secure_dir(run_base_dir())
    _secure_dir(d)
    path = pending_record_path(instance_id, caller_hash, d)
    rec = {"schema": 1, "instance_id": instance_id, "session_id": session_id, "caller_hash": caller_hash,
           "issued_at": float(time.time() if issued_at is None else issued_at)}
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            os.write(fd, (json.dumps(rec, sort_keys=True) + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def remove_pending_record(instance_id: str, caller_hash: Optional[str], directory: Optional[str] = None) -> bool:
    """Delete THIS launch's record (only the exact (instance, hash) path). Returns whether a file was removed."""
    if not isinstance(instance_id, str) or not is_wellformed_hash(caller_hash):
        return False
    try:
        os.unlink(pending_record_path(instance_id, caller_hash, directory))
        return True
    except (FileNotFoundError, ValueError):
        return False
    except OSError:
        return False


def check_pending_record(instance_id: str, caller_hash: Optional[str], *, directory: Optional[str] = None,
                         now: Optional[float] = None) -> Tuple[bool, str]:
    """The hub's half: is there a valid, unexpired record for exactly (``instance_id``, ``caller_hash``)? The dir
    must be ours and not group/other writable; the file is read without following a symlink, must be ours, not
    group/other writable, and size-capped. Never raises."""
    if not is_wellformed_hash(caller_hash):
        return False, "no caller hash presented"
    try:
        d = directory or credential_dir()
        st = os.lstat(d)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            return False, "credential dir is not a private directory owned by this uid"
        from .rendezvous import safe_read_bytes  # lazy

        raw = safe_read_bytes(pending_record_path(instance_id, caller_hash, d), _PENDING_MAX_BYTES)
        rec = json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return False, "no pending-launch record for this instance and hash"
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ENOENT:
            return False, "no pending-launch record for this instance and hash"
        return False, f"pending-launch record refused ({exc})"
    except (ValueError, UnicodeDecodeError):
        return False, "pending-launch record is corrupt"
    if not isinstance(rec, dict) or rec.get("instance_id") != instance_id:
        return False, "pending-launch record names another instance"
    stored = rec.get("caller_hash")
    if not (is_wellformed_hash(stored) and hmac_equal(stored, caller_hash)):
        return False, "pending-launch record carries another hash"
    issued = rec.get("issued_at")
    t = time.time() if now is None else now
    if isinstance(issued, bool) or not isinstance(issued, (int, float)):
        return False, "pending-launch record has no issued_at"
    if issued < t - PENDING_TTL_SECONDS or issued > t + _PENDING_FUTURE_SKEW:
        return False, "pending-launch record expired"
    return True, ""


def consume_pending_record(instance_id: str, caller_hash: str, directory: Optional[str] = None) -> None:
    remove_pending_record(instance_id, caller_hash, directory)


def hmac_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("ascii"), b.encode("ascii"))


# --- the audit sink ----------------------------------------------------------------------------------------------

#: caller-audit.jsonl rotates to ``.1`` at this size (one generation kept).
AUDIT_MAX_BYTES = 1 << 20

class CallerAudit:
    """Append-only audit of binding decisions (refused calls, parked registrations, refused requests).

    Records go to an in-memory ring (``records``), one stderr line each, and, when ``path`` is given, a JSONL
    file written 0600 in a 0700 directory. A record NEVER carries a token or a token hash; callers pass ids and
    reasons only. A write failure is loud on stderr and never stops the hub."""

    def __init__(self, path: Optional[str] = None, maxlen: int = 1000,
                 max_bytes: int = AUDIT_MAX_BYTES) -> None:
        self._path = path
        self._max_bytes = max(1024, int(max_bytes))
        self._lock = threading.Lock()
        self.records: Deque[Dict[str, Any]] = collections.deque(maxlen=maxlen)

    @property
    def path(self) -> Optional[str]:
        return self._path

    def record(self, event: str, **fields: Any) -> Dict[str, Any]:
        rec: Dict[str, Any] = {"ts": time.time(), "event": event, **fields}
        with self._lock:
            self.records.append(rec)
            if self._path:
                try:
                    directory = os.path.dirname(self._path) or "."
                    os.makedirs(directory, mode=0o700, exist_ok=True)
                    self._rotate_if_full()
                    fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
                                 0o600)
                    try:
                        os.write(fd, (json.dumps(rec, sort_keys=True, default=str) + "\n").encode("utf-8"))
                    finally:
                        os.close(fd)
                except OSError as exc:
                    sys.stderr.write(f"[hub] caller-binding audit write FAILED ({exc}); record kept in memory\n")
        detail = ", ".join(f"{k}={v}" for k, v in fields.items() if k in ("tool", "instance_id", "reason"))
        loud = "\u26a0 OPERATOR: a session registration was REFUSED; that session is dark (no injects, no DSD " \
               "notices) until it registers with an admitted credential. " if event == "register-parked" else ""
        sys.stderr.write(f"[hub] caller-binding {event}: {loud}{detail}\n")
        return rec

    def _rotate_if_full(self) -> None:
        """Bounded on disk: at :data:`AUDIT_MAX_BYTES` the file becomes ``<path>.1`` (one generation kept), so a
        caller spamming refused calls cannot grow it without bound."""
        try:
            if os.lstat(self._path).st_size >= self._max_bytes:
                os.replace(self._path, self._path + ".1")
        except FileNotFoundError:
            pass

    def events(self, event: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self.records if event is None or r.get("event") == event]

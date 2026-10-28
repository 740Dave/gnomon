"""The restart_service SERVICE REGISTRY (operator amendment 2026-09-30).

Which services the ``restart_service`` self-verb may restart, on which host and by
which mechanism. WHO may restart them is NOT here: callers live only in the verb policy
file (``harness/verb_policy.toml`` / the machine-config override, see
``harness.verb_policy``; redirect 2, operator 2026-09-30). The registry lives in MACHINE CONFIG
(``GnomonConfig.services``, ``~/gnomon-data/config.json``), never in
agent-writable session state (launch.json). Editing it (a caller, a host, a
service) is a SECURITY-POSTURE change and therefore the operator's call: agents only
consume it. Pure stdlib, no app-layer imports (like ``flags.py`` / ``config.py``).

Shape (one entry per ``service_id``)::

    "gnomon-web": {
        "host": "local",                          # 'local' | a hostname
        "mechanism": {"launchd": "com.example.web"},   # or {"systemd": "<unit>"}
        "owner": "alpha-architect",
        "hosts_hub": true,                        # optional (default false)
        "health_url": "http://127.0.0.1:8770/api/health",   # optional
        "notes": "one line"                       # optional
    }

Validation is STRICT and per entry: an unknown key, a bad id, a host that is not
hostname-shaped, a malformed mechanism name, a non-loopback / off-host health URL
all REJECT the entry, loudly (an error string naming the service and the field).
A rejected entry is REMOVED, never replaced by the shipped default of the same id
(a hand-edit's intent is not silently ignored). A registry that is not an object,
or that leaves no valid entry after errors, disables the verb (g27b f).

MERGE RULE (labelled choice): the shipped default registry is the base (EMPTY: it
names no service, owner or launchd label; X13 S5-3), the DEPLOYMENT table
``<deploy_root>/defaults/services.json`` (``{"services": {...}}``) adds entries and
overrides by service_id, and the machine-config ``services`` object adds and overrides
last; there is no "delete" spelling (to take a service away from a caller, use the verb policy's
per-target ``others = "deny"``).

Nothing from a CALLER ever reaches this module: the only caller-supplied value the
verb accepts is a service_id, which is looked up here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Shipped default registry: EMPTY by design (X13 S5-3). A service, its host, its launchd label and its owner role
#: are deployment data (``<deploy_root>/defaults/services.json``) or machine config; adding ANYTHING is the
#: operator's call. An empty registry restarts nothing (a fresh install has nothing to restart).
DEFAULT_SERVICES: Dict[str, Dict[str, Any]] = {}

LOCAL_HOST = "local"
MECH_LAUNCHD = "launchd"
MECH_SYSTEMD = "systemd"

SERVICE_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{0,62}\Z")
INSTANCE_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
#: hostname-shaped: dot-separated labels of [A-Za-z0-9-], no leading/trailing
#: hyphen per label, never starts with '-' (an ssh option), <= 253 chars.
_LABEL_RE = re.compile(r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
LAUNCHD_LABEL_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
SYSTEMD_UNIT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}\Z")

ENTRY_KEYS = frozenset({"host", "mechanism", "owner", "hosts_hub", "health_url", "notes"})
REQUIRED_KEYS = ("host", "mechanism", "owner")
MAX_NOTES = 500


def is_hostname(value: Any) -> bool:
    if not isinstance(value, str) or not value or len(value) > 253:
        return False
    return all(_LABEL_RE.match(lbl) is not None for lbl in value.split("."))


@dataclass(frozen=True)
class ServiceEntry:
    service_id: str
    host: str
    mechanism_kind: str          # launchd | systemd
    mechanism_name: str          # the label / unit (regex-validated)
    owner: str
    hosts_hub: bool = False
    health_url: Optional[str] = None
    notes: str = ""

    @property
    def is_local(self) -> bool:
        return self.host == LOCAL_HOST


@dataclass
class Registry:
    entries: Dict[str, ServiceEntry] = field(default_factory=dict)
    #: one string per rejected entry / structural fault, each naming the service + field
    errors: List[str] = field(default_factory=list)
    #: set when the registry as a whole is unusable (verb disabled)
    disabled_reason: Optional[str] = None

    @property
    def disabled(self) -> bool:
        return self.disabled_reason is not None


def _valid_health_url(url: Any, entry_host: str) -> Optional[str]:
    """None when acceptable, else a reason. http(s), no userinfo, no whitespace or
    control bytes, a real port; the host is loopback (local entries) or EXACTLY the
    entry's own host (remote entries), so a health probe can never be pointed
    somewhere else via the registry."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        return "must be a non-empty string <= 2048 chars"
    if any(ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in url):
        return "contains whitespace or a control character"
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "is not a parseable URL"
    if parts.scheme not in ("http", "https"):
        return "scheme must be http or https"
    if parts.username is not None or parts.password is not None:
        return "must not carry userinfo"
    if port is None or not 1 <= port <= 65535:
        return "must name an explicit port"
    host = parts.hostname
    if entry_host == LOCAL_HOST:
        if host not in ("127.0.0.1", "::1"):
            return "a local service's health_url must be a literal loopback address"
    elif host is None or host.lower() != entry_host.lower():
        return f"a remote service's health_url host must equal its host {entry_host!r}"
    return None


def validate_entry(service_id: Any, raw: Any) -> Tuple[Optional[ServiceEntry], List[str]]:
    """``(entry, [])`` or ``(None, [reasons])``. Never raises."""
    sid = service_id if isinstance(service_id, str) else repr(service_id)
    errs: List[str] = []

    def bad(field_: str, why: str) -> None:
        errs.append(f"services[{sid!r}].{field_}: {why}")

    if not isinstance(service_id, str) or SERVICE_ID_RE.match(service_id) is None:
        errs.append(f"services[{sid!r}]: service id must match {SERVICE_ID_RE.pattern}")
    if not isinstance(raw, dict):
        errs.append(f"services[{sid!r}]: entry must be an object")
        return None, errs
    unknown = sorted(str(k) for k in raw if k not in ENTRY_KEYS)
    if unknown:
        hint = (" (callers live ONLY in the verb policy file, not in the registry)"
                if "allowed_callers" in unknown else "")
        bad("*", f"unknown key(s) {unknown}{hint}")
    for k in REQUIRED_KEYS:
        if k not in raw:
            bad(k, "required")

    host = raw.get("host")
    if "host" in raw and not (host == LOCAL_HOST or is_hostname(host)):
        bad("host", f"must be 'local' or a hostname (got {host!r})")

    kind = name = None
    mech = raw.get("mechanism")
    if "mechanism" in raw:
        if not isinstance(mech, dict) or len(mech) != 1:
            bad("mechanism", "must be an object with exactly one key: launchd or systemd")
        else:
            (kind, name), = mech.items()
            if kind == MECH_LAUNCHD:
                if not isinstance(name, str) or LAUNCHD_LABEL_RE.match(name) is None:
                    bad("mechanism.launchd", f"label must match {LAUNCHD_LABEL_RE.pattern}")
            elif kind == MECH_SYSTEMD:
                if not isinstance(name, str) or SYSTEMD_UNIT_RE.match(name) is None:
                    bad("mechanism.systemd", f"unit must match {SYSTEMD_UNIT_RE.pattern}")
            else:
                bad("mechanism", f"unknown mechanism {kind!r} (launchd | systemd)")
    if host == LOCAL_HOST and kind == MECH_SYSTEMD:
        bad("mechanism", "systemd is only supported on a remote host (a local restart is launchd)")

    owner = raw.get("owner")
    if "owner" in raw and not (isinstance(owner, str) and INSTANCE_ID_RE.match(owner)):
        bad("owner", f"must be an instance id matching {INSTANCE_ID_RE.pattern}")

    hosts_hub = raw.get("hosts_hub", False)
    if not isinstance(hosts_hub, bool):
        bad("hosts_hub", "must be a boolean")
    elif hosts_hub and host != LOCAL_HOST:
        bad("hosts_hub", "the hub is co-hosted on THIS machine; only a local service may host it")

    health = raw.get("health_url")
    if health is not None and isinstance(host, str):
        why = _valid_health_url(health, host)
        if why:
            bad("health_url", why)
    notes = raw.get("notes", "")
    if not isinstance(notes, str) or len(notes) > MAX_NOTES or "\n" in notes or "\r" in notes:
        bad("notes", f"must be a single-line string <= {MAX_NOTES} chars")

    if errs:
        return None, errs
    return ServiceEntry(
        service_id=service_id, host=host, mechanism_kind=kind, mechanism_name=name,
        owner=owner, hosts_hub=bool(hosts_hub),
        health_url=health, notes=notes), []


def _deploy_services(reg: "Registry", deploy: Any) -> Dict[str, Any]:
    """The deployment layer: ``deploy`` is the injected ``services`` value, or (``None``) the lazily-read
    ``<deploy_root>/defaults/services.json``. A value that is not an object is reported and contributes nothing."""
    if deploy is None:
        from . import deploy_data

        deploy = deploy_data.section("services", "services")
        for path, why in deploy_data.errors().items():
            if os.path.basename(path) == "services.json":
                reg.errors.append(f"deployment services table {path}: {why}")
    if deploy is None:
        return {}
    if not isinstance(deploy, dict):
        reg.errors.append(f"deployment services table: must be an object mapping service_id -> entry "
                          f"(got {type(deploy).__name__}); ignored")
        return {}
    return dict(deploy)


def load_registry(configured: Any = None, deploy: Any = None) -> Registry:
    """Build the effective registry: shipped defaults (empty), then the DEPLOYMENT table (``deploy``; ``None`` =
    read ``<deploy_root>/defaults/services.json`` lazily), then ``configured`` (the raw
    ``GnomonConfig.services`` value; ``None`` = nothing configured).

    Never raises. See the module doc for the merge and disable rules."""
    reg = Registry()
    raw_all: Dict[str, Any] = dict(DEFAULT_SERVICES)
    raw_all.update(_deploy_services(reg, deploy))
    rejected_ids: set = set()
    if configured is not None:
        if not isinstance(configured, dict):
            reg.errors.append("services: must be an object mapping service_id -> entry "
                              f"(got {type(configured).__name__})")
            reg.disabled_reason = "machine config 'services' is not an object"
            return reg
        raw_all.update({k: v for k, v in configured.items()})
    for sid, raw in raw_all.items():
        entry, errs = validate_entry(sid, raw)
        if entry is None:
            reg.errors.extend(errs)
            rejected_ids.add(sid)
            continue
        reg.entries[sid] = entry
    hubs = [e.service_id for e in reg.entries.values() if e.hosts_hub]
    if len(hubs) > 1:
        reg.errors.append(f"services: more than one entry hosts the hub ({sorted(hubs)}); "
                          "the hub checks are ambiguous, all of them are removed")
        for h in hubs:
            reg.entries.pop(h, None)
    if reg.errors and not reg.entries:
        reg.disabled_reason = "no valid service entry remains after validation errors"
    return reg

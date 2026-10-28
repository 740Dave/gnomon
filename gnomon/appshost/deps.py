"""Resolving a ``requires`` dependency: an installed manifest app, or a registered machine-config service.

A dependency id resolves (in this order) to:

1. an INSTALLED manifest app (``<DATA>/apps.json`` entry with a ``manifest``): healthy = its health URL answers
   (port apps), else its launchd agent has a live pid. NOTHING is executed to decide it (g73-amendment-4 rule 1);
2. a SERVICE in the machine config's ``services`` registry (``gnomon.service_registry``) that declares a
   ``health_url`` (loopback): healthy = that URL answers 2xx. This is how an EXTERNAL dependency with its own
   supervisor, such as fl-image, is declared (``requires`` mode check): Gnomon only health-checks it and never
   creates a launchd agent or a process for it (amendment 2026-10-02).

Adding a service to the machine config is the operator's call (g33); the Apps host only reads it.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Optional, Tuple

from . import preflight as _pf
from .launchd import LaunchdAdapter, label_for
from .manifest import ManifestError, parse_manifest
from .state import HostPaths, manifest_entries, read_ports


def service_health_urls(services_config: Any) -> Dict[str, Optional[str]]:
    """``{service_id: health_url or None}`` from the machine config's services registry (strictly validated)."""
    from ..service_registry import load_registry

    reg = load_registry(services_config)
    out: Dict[str, Optional[str]] = {}
    for sid, entry in (getattr(reg, "entries", None) or {}).items():
        out[sid] = getattr(entry, "health_url", None)
    return out


def resolve(paths: HostPaths, dep_id: str, services: Dict[str, Optional[str]]) -> Optional[str]:
    """``"app"`` | ``"service"`` | ``"service-no-health"`` | None (unregistered)."""
    if dep_id in manifest_entries(paths):
        return "app"
    if dep_id in services:
        return "service" if services[dep_id] else "service-no-health"
    return None


def make_health(paths: HostPaths, services: Dict[str, Optional[str]], launchd: LaunchdAdapter,
                http: Callable[[int, str], bool] = _pf.http_healthy,
                url: Callable[[str], bool] = _pf.url_healthy) -> Callable[[str], Tuple[bool, str]]:
    def health(dep_id: str) -> Tuple[bool, str]:
        kind = resolve(paths, dep_id, services)
        if kind is None:
            return False, "not registered (neither an installed app nor a machine-config service)"
        if kind == "service-no-health":
            return False, "the service declares no health_url, so its health cannot be checked"
        if kind == "service":
            u = services[dep_id] or ""
            return (True, "health URL answers") if url(u) else (False, "health URL does not answer")
        # a dependency's health is judged from its PINNED manifest (the sha-checked copy taken at confirm): an HTTP
        # GET on 127.0.0.1, or the launchd agent's pid. No process is run.
        from . import pins as _pins
        from .state import read_json

        pin = read_json(paths.pin(dep_id), default=None)
        raw = _pins.pinned_manifest_bytes(paths.pin_snapshot(dep_id), pin) if isinstance(pin, dict) else None
        if raw is None:
            return False, "no intact pin for it (re-confirm the dependency)"
        try:
            m = parse_manifest(json.loads(raw.decode("utf-8")))
        except (ManifestError, ValueError, UnicodeDecodeError) as exc:
            return False, f"its pinned manifest does not load: {exc}"
        if m.has_port:
            row = read_ports(paths).get(dep_id)
            if row is None:
                return False, "no port assigned"
            ok = http(int(row["port"]), m.health_path or "/")
            return (True, "health URL answers") if ok else (False, "health URL does not answer")
        try:
            st = launchd.state(label_for(dep_id))
        except Exception as exc:  # noqa: BLE001 - e.g. the real adapter refusing: unknown = unhealthy
            return False, f"launchd state unavailable: {exc}"
        if not (st.loaded and st.pid):
            return False, "not running"
        return True, "running (health unverified)"
    return health

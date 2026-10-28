"""The RELAUNCH BUNDLE (``relaunch-bundle``, I6 the relaunch path; steel-man 2 R6).

At launch a bundle is written into the session dir: ``<session>/.claude/relaunch-bundle/``
* ``bundle.json``: the resolved settings for the workspace, launch-config and session layers (the layers as held, keyed
  by canonical name, plus the resolved view with each value's source), the home-workspace slug and its archive state as of
  launch, where the live ``machine.json`` is (``deploy-root``), and the content hash of the role definition;
* ``role-def.md``: a COPY of the rendered role definition.

A relaunch reads ONLY this bundle and the LIVE ``machine.json`` (the machine layer is always read live: it is outside
``workspaces/``). It never opens DEPLOY or DATA ``workspaces/**``. A relaunch writes the new run's bundle by COPYING the
layers forward (:func:`copy_forward`: generation + 1, ``copied-from`` the previous bundle's hash).

I6: this module is a MACHINE SAFETY PATH. It imports no workspace-model code (:mod:`.model`) and opens nothing under
``workspaces/``; ``tests/test_x13s2_safety_paths.py`` checks both (an import check and a file-access trace with a planted
violation as positive control). A missing or corrupt bundle is a loud refusal (:class:`BundleError`): a relaunch is never
silently done from partial state (ENT-HANDOVER ``unclean-boot``).

Writing is gated by ``workspace_scope_core`` (:func:`write_bundle` / :func:`copy_forward` refuse when OFF).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from . import settings_registry as SR
from .machine import Machine, load_machine
from .scopebase import ScopeError, atomic_write_json, atomic_write_text, now_iso, require_enabled
from .settings_resolve import Layer, SettingsResolver, layer_from_canonical

BUNDLE_DIR = "relaunch-bundle"
BUNDLE_JSON = "bundle.json"
ROLE_DEF = "role-def.md"
SCHEMA = 1


class BundleError(ScopeError):
    """The relaunch bundle is missing, corrupt, or inconsistent with its role-definition copy."""


def bundle_dir(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", BUNDLE_DIR)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(path: str) -> str:
    with open(path, "rb") as fh:
        return sha256_bytes(fh.read())


@dataclass(frozen=True)
class Bundle:
    data: Mapping[str, Any]
    path: str

    @property
    def generation(self) -> int:
        return int(self.data["generation"])

    @property
    def deploy_root(self) -> str:
        return str(self.data["deploy-root"])

    @property
    def home_workspace(self) -> Mapping[str, Any]:
        return self.data["home-workspace"]

    @property
    def role_def_path(self) -> str:
        return os.path.join(os.path.dirname(self.path), ROLE_DEF)

    def layer(self, name: str, reg: Optional[dict] = None) -> Layer:
        """The snapshotted layer ``name`` as a :class:`Layer` (the workspace layer keeps its launch-time availability)."""
        reg = reg if reg is not None else SR.load()
        vals = self.data["layers"][name]
        lay = layer_from_canonical(reg, name, vals)
        if name == "workspace":
            wl = self.data.get("workspace-layer") or {}
            if not wl.get("available", True):
                return Layer("workspace", {}, available=False, reason=str(wl.get("reason") or "unavailable at launch"))
        return lay


_REQUIRED = ("schema", "generation", "written-at", "instance-id", "deploy-root", "layers", "resolved", "role-def",
             "home-workspace", "workspace-layer")


def read_bundle(session_dir: str) -> Bundle:
    """Read and VALIDATE the bundle of ``session_dir`` (structure, layer names, role-def copy hash). Raises
    :class:`BundleError` (``bundle-missing`` / ``bundle-corrupt`` / ``role-def-mismatch``)."""
    d = bundle_dir(session_dir)
    path = os.path.join(d, BUNDLE_JSON)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise BundleError("bundle-missing", f"no relaunch bundle at {path!r}: this run was launched without one; never relaunch from partial state") from None
    except (OSError, ValueError) as exc:
        raise BundleError("bundle-corrupt", f"{path!r} unreadable: {exc}") from exc
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise BundleError("bundle-corrupt", f"{path!r}: not a schema-{SCHEMA} bundle")
    missing = [k for k in _REQUIRED if k not in data]
    if missing:
        raise BundleError("bundle-corrupt", f"{path!r}: missing {missing}")
    layers = data["layers"]
    if not isinstance(layers, dict) or set(layers) != {"workspace", "launch-config", "session"} or not all(isinstance(v, dict) for v in layers.values()):
        raise BundleError("bundle-corrupt", f"{path!r}: layers must be exactly workspace, launch-config and session objects")
    if not isinstance(data["home-workspace"], dict) or not data["home-workspace"].get("slug") or "state" not in data["home-workspace"]:
        raise BundleError("bundle-corrupt", f"{path!r}: home-workspace must carry slug and state")
    rd = data["role-def"]
    if not isinstance(rd, dict) or not rd.get("sha256"):
        raise BundleError("bundle-corrupt", f"{path!r}: role-def must carry its sha256")
    rd_path = os.path.join(d, ROLE_DEF)
    try:
        actual = sha256_file(rd_path)
    except OSError as exc:
        raise BundleError("role-def-mismatch", f"role-definition copy unreadable at {rd_path!r}: {exc}") from exc
    if actual != rd["sha256"]:
        raise BundleError("role-def-mismatch", f"role-definition copy {rd_path!r} hashes to {actual[:12]}, the bundle says {str(rd['sha256'])[:12]}")
    return Bundle(data, path)


def write_bundle(session_dir: str, *, instance_id: str, deploy_root: str, layers: Mapping[str, Layer], resolved: Mapping[str, Any],
                 role_def_text: str, home_workspace: Mapping[str, Any],
                 generation: int = 1, copied_from: Optional[Mapping[str, Any]] = None,
                 clock: Optional[Callable[[], datetime]] = None, enabled: Optional[bool] = None, config: Any = None) -> Bundle:
    """Write a bundle (files first, ``bundle.json`` last, so a torn write leaves no bundle that validates)."""
    require_enabled(enabled, config, "write_bundle")
    d = bundle_dir(session_dir)
    os.makedirs(d, exist_ok=True)
    rd_bytes = role_def_text.encode("utf-8")
    atomic_write_text(os.path.join(d, ROLE_DEF), role_def_text, enabled=enabled, config=config)
    ws_layer = layers["workspace"]
    data: Dict[str, Any] = {
        "schema": SCHEMA, "generation": generation, "written-at": now_iso(clock), "instance-id": instance_id,
        "session-dir-id": os.path.basename(session_dir.rstrip("/")), "deploy-root": os.path.abspath(deploy_root),
        "layers": {n: dict(layers[n].values) for n in ("workspace", "launch-config", "session")},
        "workspace-layer": {"available": ws_layer.available, "reason": ws_layer.reason},
        "resolved": dict(resolved), "role-def": {"file": ROLE_DEF, "sha256": sha256_bytes(rd_bytes), "bytes": len(rd_bytes)},
        "home-workspace": dict(home_workspace),
    }
    if copied_from is not None:
        data["copied-from"] = dict(copied_from)
    path = os.path.join(d, BUNDLE_JSON)
    atomic_write_json(path, data, enabled=enabled, config=config)
    return Bundle(data, path)


@dataclass(frozen=True)
class RelaunchInputs:
    """Everything a relaunch may use: the bundle and the LIVE machine.json, and the settings resolved from them."""

    bundle: Bundle
    machine: Machine
    resolver: SettingsResolver


def read_relaunch_inputs(session_dir: str, *, reg: Optional[dict] = None,
                         alert: Optional[Callable[[Dict[str, Any]], None]] = None) -> RelaunchInputs:
    """The relaunch reads ONLY the bundle and the live ``machine.json`` (located by the bundle). Raises
    :class:`BundleError` / :class:`~.scopebase.ModelError` (a missing/corrupt machine.json) as loud refusals."""
    reg = reg if reg is not None else SR.load()
    b = read_bundle(session_dir)
    m = load_machine(b.deploy_root)
    resolver = SettingsResolver(reg, machine=layer_from_canonical(reg, "machine", m.settings), workspace=b.layer("workspace", reg),
                                launch_config=b.layer("launch-config", reg), session=b.layer("session", reg), alert=alert)
    return RelaunchInputs(b, m, resolver)


def copy_forward(session_dir: str, *, reg: Optional[dict] = None, clock: Optional[Callable[[], datetime]] = None,
                 enabled: Optional[bool] = None, config: Any = None,
                 alert: Optional[Callable[[Dict[str, Any]], None]] = None) -> Bundle:
    """A relaunch writes the new run's bundle by COPYING the previous layers forward (generation + 1; the role
    definition copy is carried byte-for-byte). Reads only the old bundle and live machine.json."""
    require_enabled(enabled, config, "copy_forward")
    reg = reg if reg is not None else SR.load()
    inp = read_relaunch_inputs(session_dir, reg=reg, alert=alert)
    old = inp.bundle
    with open(old.role_def_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    layers = {n: old.layer(n, reg) for n in ("workspace", "launch-config", "session")}
    resolved = {n: {"value": r.value, "source": r.source} for n, r in inp.resolver.effective().items()}
    return write_bundle(session_dir, instance_id=str(old.data["instance-id"]), deploy_root=old.deploy_root, layers=layers, resolved=resolved,
                        role_def_text=text, home_workspace=dict(old.home_workspace),
                        generation=old.generation + 1,
                        copied_from={"generation": old.generation, "bundle-sha256": sha256_file(old.path)}, clock=clock, enabled=enabled, config=config)


def check_ready(session_dir: str) -> tuple:
    """``(ok, detail)`` for the gnomon-relaunch rail: the bundle validates and the live machine.json is readable. Never
    raises (a failure is data for the rail to refuse on)."""
    try:
        inp = read_relaunch_inputs(session_dir)
    except ScopeError as exc:
        return False, f"{exc.code}: {exc.message}"
    except Exception as exc:  # noqa: BLE001 - a rail must report, not crash
        return False, f"{type(exc).__name__}: {exc}"
    b = inp.bundle
    return True, (f"bundle generation {b.generation} (home workspace {b.home_workspace.get('slug')!r}, "
                  f"role-def {b.data['role-def']['sha256'][:12]}) + live {inp.machine.path}")

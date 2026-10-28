"""Settings of the open-work list: the default interval, the waiting_on aliases and the allowed status setters (g3, g68).

PRECEDENCE (the setting is workspace-configurable): the agent's HOME WORKSPACE value, else the DEPLOYMENT value, else the neutral built-in.
* The workspace layer is the top-level key ``"open-work"`` of ``<deploy_root>/workspaces/<ws>/workspace.json`` (NOT its registry-keyed
  ``settings`` object, which the settings registry polices), with keys ``default_interval_minutes``, ``waiting_on_aliases``,
  ``status_setters``. The agent's home workspace is the one holding ``workspaces/<ws>/roles/<instance id>.md``
  (:func:`gnomon.workspace.model.home_workspace_of`). The harness does not otherwise resolve a workspace today
  (no ``workspace`` read anywhere under ``harness/``; the scope core runs in launch.py only behind ``workspace_scope_core``),
  so this module asks that same function, lazily, and never fails a call because of it: no deploy root, no workspaces folder, no role
  file, several holders (an I1 violation) or an unreadable workspace.json all fall back to the deployment value, and the fault is
  RECORDED and visible (``deploy_data.errors()``).
* The deployment layer is ``<deploy_root>/defaults/open-work.json`` ({default_interval_minutes, waiting_on_aliases}) and
  ``defaults/status-setters.json`` ({setters: {id: name}}).

Neutral defaults: default interval 60 minutes, NO aliases, NO role setters (only the operator can declare his status).
``default_interval_minutes`` and ``waiting_on_aliases`` are PRESENTATION data (a bad value falls back, visibly). The setter list decides
WHO MAY ACT, so it fails CLOSED at both layers (any fault means nobody is a role setter). Read lazily; NEVER written.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

from . import constants as C

_ALIAS_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,31}\Z")
_RESERVED = {"self", C.WAIT_OPERATOR}


@dataclass
class WorkSettings:
    default_minutes: int = C.DEFAULT_INTERVAL_MIN
    default_source: str = "built-in"          # "workspace" | "deployment" | "built-in"
    aliases: Dict[str, str] = field(default_factory=dict)
    setters: Dict[str, str] = field(default_factory=dict)
    workspace: Optional[str] = None
    faults: Tuple[str, ...] = ()

    @property
    def fault(self) -> Optional[str]:
        return "; ".join(self.faults) if self.faults else None


def _bad_interval(v: Any) -> bool:
    return isinstance(v, bool) or not isinstance(v, int) or not (C.MIN_INTERVAL_MIN <= v <= C.MAX_INTERVAL_MIN)


def _root(deploy_root: Optional[str]) -> str:
    if deploy_root:
        return deploy_root
    from .. import deploy_data as dd

    return dd.deploy_root()


def _workspace_block(instance_id: Optional[str], deploy_root: str, faults: list) -> Tuple[Optional[str], Dict[str, Any]]:
    """``(workspace id, its open-work block)``; ``(None, {})`` when there is none. Never raises; faults are appended."""
    if not instance_id or not deploy_root:
        return None, {}
    try:
        from ..workspace import model as wm

        ws = wm.home_workspace_of(deploy_root, instance_id)
    except Exception as exc:  # noqa: BLE001 - several holders (I1) or an unreadable folder: fall back to the deployment, visibly
        faults.append(f"home workspace of {instance_id!r} not resolved ({type(exc).__name__}: {exc}); using the deployment value")
        return None, {}
    if ws is None:
        return None, {}
    # ONE reader and ONE set of rules for workspace.json: the model's own lenient reader (non-blocking, regular files only, `settings` must be an
    # object, top level an object). A file the rest of Gnomon rejects is rejected here too, and the other way round (round 4 / P3).
    rec, why = wm.read_workspace_record(deploy_root, ws)
    if rec is None:
        faults.append(f"workspace {ws!r}: workspace.json is unusable ({why.split(': ', 1)[-1][:120]}); using the deployment value")
        return ws, {}
    block = rec.get(C.WORKSPACE_KEY)
    if block is None:
        return ws, {}
    if not isinstance(block, dict):
        faults.append(f"workspace {ws!r}: {C.WORKSPACE_KEY!r} must be an object; using the deployment value")
        return ws, {}
    return ws, block


def _aliases(raw: Any, where: str, faults: list) -> Dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        faults.append(f"{where}: waiting_on_aliases must be an object {{word: '{C.WAIT_OPERATOR}'}}; ignored")
        return {}
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and _ALIAS_RE.match(k) and k not in _RESERVED and v == C.WAIT_OPERATOR:
            out[k] = C.WAIT_OPERATOR
        else:
            faults.append(f"{where}: waiting_on_aliases entry {k!r}: {v!r} is not usable (a lowercase word mapped to "
                          f"'{C.WAIT_OPERATOR}'); ignored")
    return out


def _setters(raw: Any, where: str, faults: list) -> Optional[Dict[str, str]]:
    """The setter map, ``{}`` if absent-but-declared-empty, or ``None`` to mean FAILED (fail closed -> nobody)."""
    from .store import valid_instance_id

    if not isinstance(raw, dict):
        faults.append(f"{where}: setters must be an object {{instance id: display name}}; NOBODY is a role setter")
        return None
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if valid_instance_id(k) and isinstance(v, str) and v.strip() and len(v) <= 40 and v.isprintable():
            out[k] = v.strip()
        else:
            faults.append(f"{where}: setter entry {k!r} is not usable; NOBODY is a role setter until it is fixed")
            return None
    return out


def resolve(instance_id: Optional[str] = None, deploy_root: Optional[str] = None) -> WorkSettings:
    """The effective settings for one agent. Never raises."""
    from .. import deploy_data as dd

    faults: list = []
    s = WorkSettings()
    try:
        root = _root(deploy_root)
    except Exception as exc:  # noqa: BLE001
        s.faults = (f"deploy root unresolved: {exc}",)
        return s
    ws, block = _workspace_block(instance_id, root, faults)
    s.workspace = ws
    # deployment layer
    dep: Dict[str, Any] = {}
    try:
        t = dd.load_json("open-work", root)       # (a literal: the g68 table scanner reads literals; == C.TABLE_DEFAULTS)
    except Exception as exc:  # noqa: BLE001
        t = None
        faults.append(f"{C.TABLE_DEFAULTS}.json unreadable: {exc}")
    if t is None:
        err = dd.errors().get(dd.data_path(C.TABLE_DEFAULTS + ".json", root) or "", "")
        if err:
            faults.append(err)
    else:
        dep = t
    # default interval: workspace, else deployment, else 60
    for layer, src, where in ((block, "workspace", f"workspace {ws!r}"), (dep, "deployment", f"{C.TABLE_DEFAULTS}.json")):
        if "default_interval_minutes" in layer:
            v = layer["default_interval_minutes"]
            if _bad_interval(v):
                msg = (f"{where}: default_interval_minutes {v!r} must be a whole number from {C.MIN_INTERVAL_MIN} to "
                       f"{C.MAX_INTERVAL_MIN}; using the next layer, else {C.DEFAULT_INTERVAL_MIN}")
                faults.append(msg)
                dd.record_fault(C.TABLE_DEFAULTS, msg, root)
                continue
            s.default_minutes, s.default_source = v, src
            break
    # aliases: workspace replaces deployment when it declares the key
    if "waiting_on_aliases" in block:
        s.aliases = _aliases(block["waiting_on_aliases"], f"workspace {ws!r}", faults)
    else:
        s.aliases = _aliases(dep.get("waiting_on_aliases"), f"{C.TABLE_DEFAULTS}.json", faults)
    # setters (fail closed at both layers)
    if "status_setters" in block:
        got = _setters(block["status_setters"], f"workspace {ws!r}", faults)
        s.setters = got if got is not None else {}
    else:
        try:
            raw = dd.section("status-setters", "setters", root)       # (a literal for the g68 scanner; == C.TABLE_SETTERS)
        except Exception:  # noqa: BLE001
            raw = {}
            faults.append(f"{C.TABLE_SETTERS}.json unreadable; NOBODY is a role setter")
        if raw is None:
            tbl_err = dd.errors().get(dd.data_path(C.TABLE_SETTERS + ".json", root) or "", "")
            if tbl_err:
                faults.append(f"{C.TABLE_SETTERS}.json: {tbl_err}; NOBODY is a role setter")
            s.setters = {}
        else:
            got = _setters(raw, f"{C.TABLE_SETTERS}.json", faults)
            s.setters = got if got is not None else {}
            if got is None:
                dd.record_fault(C.TABLE_SETTERS, faults[-1], root)
    s.faults = tuple(faults)
    return s


# thin accessors kept for callers that need one value
def default_interval_minutes(deploy_root: Optional[str] = None, instance_id: Optional[str] = None) -> Tuple[int, Optional[str]]:
    s = resolve(instance_id, deploy_root)
    return s.default_minutes, s.fault


def status_setters(deploy_root: Optional[str] = None, instance_id: Optional[str] = None) -> Dict[str, str]:
    return resolve(instance_id, deploy_root).setters

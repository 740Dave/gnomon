"""The layered SETTINGS RESOLVER (I7 and I6): machine < workspace < launch-config < session.

Reuses S1's packaged registry (:mod:`.settings_registry`); it is NOT a second registry. Every key has ONE default (the
registry's), a scope-floor (the layers it may be set at) and, optionally, ``restrictive-on-skip`` with its
``restrictive-value``. :meth:`SettingsResolver.get` returns the effective value AND its source layer; a read site never
carries its own default (there is no ``default=`` parameter here, by design).

I6: this module is PURE (no file or workspace access). It is used by machine safety paths, so it must never import the
workspace model loader (:mod:`.model`); a caller on an ordinary path builds the layers (reading workspace.json) and
passes them in; a safety path passes the layers it is allowed to read (machine.json live, the relaunch bundle's
snapshot of the rest).

An unreadable workspace layer is SKIPPED: ``Layer(available=False)``. Then
* a visible :attr:`SettingsResolver.banner` names it, and every resolved value carries ``skipped`` naming
  ``'workspace layer UNAVAILABLE'`` (the source string the spec requires);
* every key marked ``restrictive-on-skip`` takes its ``restrictive-value`` (overriding every layer above, because the
  skipped layer may have carried the restriction) and records source ``'workspace layer UNAVAILABLE'``;
* an alert is emitted once per skip through the ``alert`` callback (the caller appends it to the event log).

A read of an UNREGISTERED key: :meth:`get` raises :class:`UnregisteredSetting` (I7: a test fails on any such read). On a
safety path (``safety=True``) it instead alerts LOUDLY and returns the restrictive branch (value ``None``, source
``'UNREGISTERED'``, ``restrictive=True``): the registry is the only place a packaged default lives, so an unregistered
key has none (ENT-SETTINGS unregistered-key-on-safety-path).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from . import settings_registry as SR
from .scopebase import ScopeRefusal

WORKSPACE_UNAVAILABLE = "workspace layer UNAVAILABLE"
UNREGISTERED = "UNREGISTERED"


class UnregisteredSetting(ScopeRefusal):
    def __init__(self, name: str) -> None:
        super().__init__("unregistered-setting", f"settings key {name!r} is not in the settings registry (I7)",
                         "register it in settings-registry.json with a default, a type and a scope-floor")


@dataclass(frozen=True)
class Layer:
    """One settings layer. ``values`` are keyed by CANONICAL setting name (use :func:`layer_from_raw` to translate a
    raw per-layer file, whose key names differ: the registry's alias map)."""

    name: str
    values: Mapping[str, Any]
    available: bool = True
    reason: str = ""
    unknown: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Resolved:
    name: str
    value: Any
    source: str
    skipped: Tuple[str, ...] = ()
    restrictive: bool = False
    ignored: Tuple[str, ...] = ()


def unavailable(name: str, reason: str) -> Layer:
    return Layer(name, {}, available=False, reason=reason)


def layer_from_raw(reg: dict, layer: str, raw: Mapping[str, Any]) -> Layer:
    """Translate a raw per-layer mapping (this layer's own key spellings) to a canonical :class:`Layer`. A key that is
    neither a registered alias at this layer, nor run state, nor the flag family is returned in ``unknown`` (never
    silently dropped: the caller decides)."""
    if layer not in SR.LAYERS:
        raise SR.RegistryError(f"unknown layer {layer!r}")
    idx = SR.alias_index(reg)
    values: Dict[str, Any] = {}
    unknown: List[str] = []
    for key, val in raw.items():
        name = idx.get((layer, key))
        if name is not None:
            values[name] = val
        elif SR.classify_key(reg, key, layer) == "UNREGISTERED":
            unknown.append(key)
    return Layer(layer, values, unknown=tuple(unknown))


def layer_from_canonical(reg: dict, layer: str, values: Mapping[str, Any]) -> Layer:
    """A layer whose keys are already canonical setting names (machine.json ``settings``, workspace.json ``settings``)."""
    if layer not in SR.LAYERS:
        raise SR.RegistryError(f"unknown layer {layer!r}")
    known = reg["settings"]
    return Layer(layer, {k: v for k, v in values.items() if k in known}, unknown=tuple(k for k in values if k not in known))


class SettingsResolver:
    def __init__(self, reg: Optional[dict] = None, *, machine: Optional[Layer] = None, workspace: Optional[Layer] = None,
                 launch_config: Optional[Layer] = None, session: Optional[Layer] = None,
                 alert: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        self.reg = reg if reg is not None else SR.load()
        empty = lambda n: Layer(n, {})  # noqa: E731
        self._layers: Dict[str, Layer] = {
            "machine": machine or empty("machine"), "workspace": workspace or empty("workspace"),
            "launch-config": launch_config or empty("launch-config"), "session": session or empty("session")}
        for lname, lay in self._layers.items():
            if lay.name != lname:
                raise SR.RegistryError(f"layer passed as {lname!r} is named {lay.name!r}")
        self._alert = alert
        self._announced = False
        self._announce_skip()

    # -- the skip (I6) ------------------------------------------------------------------------------------------
    @property
    def workspace_skipped(self) -> bool:
        return not self._layers["workspace"].available

    @property
    def banner(self) -> Optional[str]:
        """The visible banner for a skipped workspace layer (None when nothing was skipped)."""
        ws = self._layers["workspace"]
        if ws.available:
            return None
        return (f"{WORKSPACE_UNAVAILABLE}: {ws.reason or 'unreadable'}. Restrictive-on-skip settings use their most "
                "restrictive value until it is readable.")

    def _announce_skip(self) -> None:
        if self._announced or not self.workspace_skipped:
            return
        self._announced = True
        if self._alert is not None:
            self._alert({"kind": "settings-layer-skipped", "severity": "warn", "message": self.banner,
                         "detail": {"layer": "workspace", "source": WORKSPACE_UNAVAILABLE,
                                    "restrictive-on-skip-keys": sorted(n for n, r in self.reg["settings"].items() if r.get("restrictive-on-skip"))}})

    # -- reads --------------------------------------------------------------------------------------------------
    def get(self, name: str, *, safety: bool = False) -> Resolved:
        row = self.reg["settings"].get(name)
        if row is None:
            if not safety:
                raise UnregisteredSetting(name)
            if self._alert is not None:
                self._alert({"kind": "unregistered-setting-read", "severity": "error",
                             "message": f"a safety path read the unregistered settings key {name!r}; taking the restrictive branch",
                             "detail": {"key": name}})
            return Resolved(name, None, UNREGISTERED, restrictive=True)
        floor = tuple(row["scope-floor"])
        value, source = row["default"], "default"
        ignored: List[str] = []
        skipped: Tuple[str, ...] = (WORKSPACE_UNAVAILABLE,) if self.workspace_skipped else ()
        for lname in SR.LAYERS:
            lay = self._layers[lname]
            if not lay.available or name not in lay.values:
                continue
            if lname not in floor:
                ignored.append(lname)           # a value outside its scope-floor is never applied
                continue
            value, source = lay.values[name], lname
        if self.workspace_skipped and row.get("restrictive-on-skip"):
            return Resolved(name, row["restrictive-value"], WORKSPACE_UNAVAILABLE, skipped, True, tuple(ignored))
        return Resolved(name, value, source, skipped, False, tuple(ignored))

    def value(self, name: str, *, safety: bool = False) -> Any:
        return self.get(name, safety=safety).value

    def effective(self) -> Dict[str, Resolved]:
        """Every registered setting with its effective value and source layer (inspectable: I7)."""
        return {n: self.get(n) for n in self.reg["settings"]}

    def layer_values(self, layer: str) -> Dict[str, Any]:
        """The canonical values of one layer as held (what the relaunch bundle snapshots)."""
        return dict(self._layers[layer].values)

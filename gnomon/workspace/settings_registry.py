"""Loader + validator for the packaged settings registry (`registry`, I7).

``settings-registry.json`` ships READ-ONLY with the package. MACHINE-READ under g25: this module is
its reader and FAILS (raises :class:`RegistryError`) rather than silently shrinking: a registry with
fewer than :data:`MIN_SETTINGS` settings, a row missing its one default / type / scope-floor, a
scope-floor naming a layer that does not exist, or a duplicate key alias in one layer is an error.
X13 S2: a row marked ``restrictive-on-skip`` must also carry a ``restrictive-value`` of its own type.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional

LAYERS = ("machine", "workspace", "launch-config", "session")
#: floor (g25 b): the registry S1 ships holds 37 settings; a parse that yields fewer is a bug.
MIN_SETTINGS = 30
_TYPES = {"string", "boolean", "integer", "number", "array", "object",
          "string|null", "integer|null", "number|null", "array|null", "object|null"}
PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings-registry.json")


class RegistryError(ValueError):
    pass


_PY_TYPES = {"string": (str,), "boolean": (bool,), "integer": (int,), "number": (int, float), "array": (list,), "object": (dict,)}


def value_matches_type(type_name, value) -> bool:
    """Does ``value`` fit a registry ``type`` (``'string'``, ``'integer|null'``, ...)? bool is not an integer/number."""
    if not isinstance(type_name, str):
        return False
    parts = type_name.split("|")
    if value is None:
        return "null" in parts
    for part in parts:
        pys = _PY_TYPES.get(part)
        if pys and isinstance(value, pys) and not (isinstance(value, bool) and part != "boolean"):
            return True
    return False


def load(path: str = PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        reg = json.load(fh)
    validate(reg)
    return reg


def validate(reg: dict) -> None:
    errs: List[str] = []
    if tuple(reg.get("layers", ())) != LAYERS:
        errs.append(f"layers must be exactly {LAYERS}")
    settings = reg.get("settings")
    if not isinstance(settings, dict) or len(settings) < MIN_SETTINGS:
        raise RegistryError(f"registry holds {len(settings or {})} settings, below the floor {MIN_SETTINGS}: "
                            "the reader refuses to shrink silently (g25 b)")
    seen_alias: Dict[tuple, str] = {}
    for name, row in settings.items():
        for req in ("type", "default", "scope-floor", "keys"):
            if req not in row:
                errs.append(f"{name}: missing {req!r} (exactly one default, a type and a scope-floor are required)")
        if row.get("type") not in _TYPES:
            errs.append(f"{name}: unknown type {row.get('type')!r}")
        if row.get("restrictive-on-skip"):
            # X13 S2 (I6, ENT-SETTINGS restrictive-on-skip): the row must NAME its most restrictive value, or the
            # resolver could not apply it when the workspace layer is unreadable.
            if "restrictive-value" not in row:
                errs.append(f"{name}: restrictive-on-skip requires a restrictive-value (the value taken when the workspace layer is skipped)")
            elif not value_matches_type(row.get("type"), row["restrictive-value"]):
                errs.append(f"{name}: restrictive-value {row['restrictive-value']!r} does not match type {row.get('type')!r}")
        floor = row.get("scope-floor") or []
        if not floor or any(l not in LAYERS for l in floor):
            errs.append(f"{name}: scope-floor {floor!r} must be a non-empty subset of {LAYERS}")
        for layer, key in (row.get("keys") or {}).items():
            if layer not in floor:
                errs.append(f"{name}: key for layer {layer!r} is outside its scope-floor {floor!r}")
            prev = seen_alias.setdefault((layer, key), name)
            if prev != name:
                errs.append(f"{name}: layer {layer!r} key {key!r} is already the alias of {prev!r}")
    if errs:
        raise RegistryError("; ".join(errs))


def alias_index(reg: dict) -> Dict[tuple, str]:
    """``(layer, key) -> canonical setting name``."""
    out: Dict[tuple, str] = {}
    for name, row in reg["settings"].items():
        for layer, key in row["keys"].items():
            out[(layer, key)] = name
    return out


def classify_key(reg: dict, key: str, layer: Optional[str] = None) -> str:
    """``'setting:<name>'`` | ``'run-state'`` | ``'flag-family'`` | ``'UNREGISTERED'`` for a raw key name.

    With ``layer`` given, the key must be an alias AT THAT LAYER (a key that is an alias only at another
    layer is ``UNREGISTERED`` there: S1 Diamond F10). Without it, any layer matches (the legacy behaviour)."""
    if layer is not None and layer not in LAYERS:
        raise RegistryError(f"unknown layer {layer!r}")
    for (lyr, k), name in alias_index(reg).items():
        if k == key and (layer is None or lyr == layer):
            return f"setting:{name}"
    fam = reg.get("flag-family", {})
    fam_keys = {"machine": fam.get("machine-key"), "launch-config": fam.get("launch-config-key")}
    if (key in fam_keys.values() if layer is None else key == fam_keys.get(layer)):
        return "flag-family"
    if key in reg.get("run-state-keys", {}).get("keys", {}):
        return "run-state"
    return "UNREGISTERED"

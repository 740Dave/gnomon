"""The ONE role-label table: short display labels for agent instance ids (X13 S5-3, survey rows 5 and 6).

A role id (an instance id such as ``alpha-architect``) is a DEPLOYMENT fact; Gnomon ships no role. The neutral
fallback is the instance id itself, which every reader already does for an unknown id. A deployment supplies its
labels in ``<deploy_root>/defaults/role-labels.json``::

    {"labels":       {"<instance id>": "<short label>", ...},   # the DSD sender accordions AND the stall text
     "order":        ["<label>", ...],                          # display order of the DSD sender accordions
     "stall-labels": {"<instance id>": "<label>" | null, ...}}  # per-surface overrides for the stall text only
                                                                # (null = "no short label: show the id")

One table, two surfaces (:mod:`gnomon.dsd.sections`, :mod:`gnomon.harness.operator_activity`), so a new
role is labelled in ONE place. Lazy: every function reads the deployment table when CALLED (mtime-cached by
``deploy_data``), never at import. Both consumers are hook-closure modules, so this module imports only the stdlib.
"""

from __future__ import annotations

from typing import Dict, Tuple


_SHAPES = (("labels", dict), ("order", list), ("stall-labels", dict))


def _table():
    from . import deploy_data

    t = deploy_data.load_json("role-labels") or {}
    for key, typ in _SHAPES:          # g68-amendment: a wrong-shaped key degrades to the neutral built-in VISIBLY, never silently
        if key in t and not isinstance(t[key], typ):
            deploy_data.record_fault("role-labels", f"key {key!r} must be a {typ.__name__}; ignored")
    return t


def labels() -> Dict[str, str]:
    """``{instance id: short label}`` for the DSD sender accordions (``{}`` = show the instance id)."""
    from . import deploy_data

    return deploy_data.str_map(_table().get("labels"))


def order() -> Tuple[str, ...]:
    """The display order of the sender LABELS (unknown senders follow, alphabetically)."""
    from . import deploy_data

    return tuple(deploy_data.str_list(_table().get("order")))


def stall_labels() -> Dict[str, str]:
    """``labels()`` with the table's ``stall-labels`` overrides applied (a ``null`` override removes that id)."""
    out = dict(labels())
    raw = _table().get("stall-labels")
    if isinstance(raw, dict):
        for k, v in raw.items():
            if not isinstance(k, str) or not k:
                continue
            if v is None:
                out.pop(k, None)
            elif isinstance(v, str):
                out[k] = v
    return out

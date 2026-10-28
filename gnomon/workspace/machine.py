"""``machine.json`` (the MACHINE settings layer), in a module of its own on purpose.

``machine.json`` lives at the DEPLOY root, OUTSIDE ``workspaces/``, so machine safety paths (I6: relaunch, alerts, ...) may
read it. The workspace model loader (:mod:`.model`) may not be imported by a safety path; this module holds only what a
safety path needs of the deployment, and imports nothing from the model. Read-only; a missing or malformed file raises
:class:`~.scopebase.ModelError` (a loud stop, never a guessed default).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping

from .scopebase import ModelError, read_json


@dataclass(frozen=True)
class Machine:
    raw: Mapping[str, object]
    path: str

    @property
    def settings(self) -> Mapping[str, object]:
        s = self.raw.get("settings")
        return s if isinstance(s, dict) else {}


def machine_path(deploy_root: str) -> str:
    return os.path.join(deploy_root, "machine.json")


def load_machine(deploy_root: str) -> Machine:
    """``<deploy>/machine.json`` (always read live). Loud stop if absent or malformed."""
    path = machine_path(deploy_root)
    try:
        raw = read_json(path)
    except (OSError, ValueError) as exc:
        raise ModelError(f"machine.json unreadable at {path!r}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema") != 1:
        raise ModelError(f"{path!r}: not a schema-1 object")
    if "settings" in raw and not isinstance(raw["settings"], dict):
        raise ModelError(f"{path!r}: settings must be an object")
    return Machine(raw=raw, path=path)

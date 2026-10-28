"""Mail configuration: operator-editable only (the dashboard's Mail panel -> ``POST /api/mail/config``).

Lives at ``<DATA>/mail/config.json`` (0600). NO agent verb reads or writes it through an argument; the
service re-reads it on every call (a operator edit takes effect immediately). Keys:

* ``account``: the operator's Gmail/Workspace address (default ``operator@example.test``; the mail tool also checks
  the connected mailbox's real address against it at connect time).
* ``plus_map``: instance -> explicit plus-address override. Default per instance: ``<local>+<instance>@<domain>``
  derived from ``account`` (``operator+alpha-architect@example.test``).
* ``label_prefix``: default ``agents/``; an agent's label is ``<prefix><instance>``.
* ``allowlist``: addresses a DRAFT may go to. DEFAULT EMPTY (every draft is refused until the operator adds
  Pat / Sam / himself; no address is hard-coded).
* ``signature_template`` (default ``{display_name}\\n(A member of the Agent Team)``) and ``signatures``
  (instance -> full override); ``display_names`` (instance -> name; default derived: ``alpha-architect`` ->
  ``Alpha-Architect``).
* ``max_writes_per_hour``: per-agent cap on drafts + deliveries to the operator (default 20).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .errors import MailError
from .tokenstore import read_private_json, write_private_json

DEFAULT_ACCOUNT = "operator@example.test"
DEFAULT_LABEL_PREFIX = "agents/"
DEFAULT_SIGNATURE_TEMPLATE = "{display_name}\n(A member of the Agent Team)"
DEFAULT_MAX_WRITES_PER_HOUR = 20

INSTANCE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
_ADDR_RE = re.compile(r"\A[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\Z")
_LABEL_PREFIX_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 _/-]{0,30}/\Z")
CONFIG_KEYS = ("account", "plus_map", "label_prefix", "allowlist", "signature_template", "signatures",
               "display_names", "max_writes_per_hour")


def normalize_address(value: Any) -> str:
    """A bare lowercase ``local@domain``; refuses display names, lists, CR/LF and angle brackets."""
    if not isinstance(value, str):
        raise MailError("an address must be a string")
    v = value.strip()
    if not _ADDR_RE.match(v):
        raise MailError(f"not a plain email address: {value[:60]!r}")
    return v.lower()


def _clean_text(value: Any, what: str, limit: int) -> str:
    if not isinstance(value, str):
        raise MailError(f"{what} must be a string")
    if len(value) > limit:
        raise MailError(f"{what} is longer than {limit} characters")
    if re.search(r"[\x00-\x09\x0b-\x1f\x7f]", value):  # \n (0x0a) allowed, nothing else
        raise MailError(f"{what} contains control characters")
    return value


def default_display_name(instance: str) -> str:
    parts = [p for p in re.split(r"[-_]", instance) if p]
    return "-".join(p.upper() if len(p) <= 2 else p[:1].upper() + p[1:] for p in parts) or instance


@dataclass
class MailConfig:
    account: str = DEFAULT_ACCOUNT
    plus_map: Dict[str, str] = field(default_factory=dict)
    label_prefix: str = DEFAULT_LABEL_PREFIX
    allowlist: List[str] = field(default_factory=list)
    signature_template: str = DEFAULT_SIGNATURE_TEMPLATE
    signatures: Dict[str, str] = field(default_factory=dict)
    display_names: Dict[str, str] = field(default_factory=dict)
    max_writes_per_hour: int = DEFAULT_MAX_WRITES_PER_HOUR

    # --- derived ---
    def _split_account(self) -> Tuple[str, str]:
        local, _, domain = normalize_address(self.account).partition("@")
        return local.split("+", 1)[0], domain

    def plus_address(self, instance: str) -> str:
        _check_instance(instance)
        if instance in self.plus_map:
            return normalize_address(self.plus_map[instance])
        local, domain = self._split_account()
        return f"{local}+{instance.lower()}@{domain}"

    def label_name(self, instance: str) -> str:
        _check_instance(instance)
        return f"{self.label_prefix}{instance}"

    def display_name(self, instance: str) -> str:
        _check_instance(instance)
        return self.display_names.get(instance) or default_display_name(instance)

    def signature(self, instance: str) -> str:
        _check_instance(instance)
        if instance in self.signatures:
            return self.signatures[instance]
        return self.signature_template.replace("{display_name}", self.display_name(instance))

    def to_dict(self) -> Dict[str, Any]:
        return {"account": self.account, "plus_map": dict(self.plus_map), "label_prefix": self.label_prefix,
                "allowlist": list(self.allowlist), "signature_template": self.signature_template,
                "signatures": dict(self.signatures), "display_names": dict(self.display_names),
                "max_writes_per_hour": self.max_writes_per_hour}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MailConfig":
        """Validate strictly; unknown keys are refused (a typo must not silently fall back to a default)."""
        if not isinstance(data, dict):
            raise MailError("mail config must be an object")
        unknown = sorted(set(data) - set(CONFIG_KEYS))
        if unknown:
            raise MailError(f"unknown mail config key(s): {unknown}")
        cfg = cls()
        if "account" in data:
            cfg.account = normalize_address(data["account"])
        if "label_prefix" in data:
            lp = data["label_prefix"]
            if not isinstance(lp, str) or not _LABEL_PREFIX_RE.match(lp):
                raise MailError("label_prefix must look like 'agents/' (letters, digits, space, _ - /, ending in /)")
            cfg.label_prefix = lp
        for key, val_check in (("plus_map", normalize_address), ("display_names", None), ("signatures", None)):
            if key in data:
                m = data[key]
                if not isinstance(m, dict):
                    raise MailError(f"{key} must be an object (instance -> value)")
                out: Dict[str, str] = {}
                for inst, v in m.items():
                    _check_instance(inst)
                    out[inst] = (val_check(v) if val_check else
                                 _clean_text(v, f"{key}[{inst}]", 500 if key == "signatures" else 80))
                    if key == "display_names" and not out[inst].strip():
                        raise MailError("a display name must not be empty")
                setattr(cfg, key, out)
        if "allowlist" in data:
            al = data["allowlist"]
            if not isinstance(al, list):
                raise MailError("allowlist must be a list of addresses")
            seen: List[str] = []
            for a in al:
                n = normalize_address(a)
                if n not in seen:
                    seen.append(n)
            if len(seen) > 50:
                raise MailError("allowlist is limited to 50 addresses")
            cfg.allowlist = seen
        if "signature_template" in data:
            cfg.signature_template = _clean_text(data["signature_template"], "signature_template", 500)
        if "max_writes_per_hour" in data:
            n = data["max_writes_per_hour"]
            if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= 200:
                raise MailError("max_writes_per_hour must be an integer 1..200")
            cfg.max_writes_per_hour = n
        return cfg


def _check_instance(instance: Any) -> None:
    if not isinstance(instance, str) or not INSTANCE_RE.match(instance):
        raise MailError(f"invalid agent instance id: {str(instance)[:40]!r}")


class MailConfigStore:
    FILENAME = "config.json"

    def __init__(self, directory: str) -> None:
        self.dir = directory
        self.path = os.path.join(directory, self.FILENAME)

    def load(self) -> MailConfig:
        raw = read_private_json(self.path)
        return MailConfig() if raw is None else MailConfig.from_dict(raw)

    def save(self, data: Dict[str, Any]) -> MailConfig:
        cfg = MailConfig.from_dict(data)  # validate BEFORE touching the file
        write_private_json(self.path, cfg.to_dict())
        return cfg

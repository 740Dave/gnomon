"""The app manifest: ONE schema registry, strict validation (-RESOURCES, -GUARDS).

A manifest is a JSON file shipped in the app's own directory and registered by PATH in ``<DATA>/apps.json``.
This module validates its CONTENT only; the checks that need the host (is a ``requires`` id registered, is the
file outside Gnomon's source tree, is a port free) live in :mod:`.host`.

Extension point (g73-amendment-2026-10-02 item 6): a new manifest field is added HERE, to :data:`SCHEMA`
(one registry), with a ``schema_version`` bump. Unknown fields at the declared version are REFUSED, naming the
field (architect ruling R13), at the top level and inside every nested object.

Every refusal raises :class:`ManifestError` whose ``field`` names the offending field (dotted path, e.g.
``paths[2].flag``), so the registration error always names it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

#: The schema versions this host understands. A new field means a new version here (and in SCHEMA).
SUPPORTED_SCHEMA_VERSIONS: FrozenSet[int] = frozenset({1, 2})

#: The manifest id grammar (architect ruling R13). Immutable across versions: host state keys off it.
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
#: path-parameter / setting / action names (a placeholder name; underscores allowed, no hyphens).
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
#: a named workspace (scope other than "all").
WORKSPACE_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
#: g73-amendment-4 rule 4: the ONLY env names a manifest may set are APP_<NAME> (an ALLOW-list, never a deny-list).
#: Matched with fullmatch: a trailing newline does not slip through (round-2 P3).
ENV_KEY_RE = re.compile(r"APP_[A-Z0-9_]{1,59}")
#: an interpreter the host runs isolated (-I -B): ANY file name starting ``python`` (python, python3, python3.13,
#: python3-intel64, python3.13-intel64; g73-amendment-4 rule 3, round 3c R3-F3: a prefix, not a version grammar)
PYTHON_RE = re.compile(r"python", re.I)
#: every {...} in an argv token / env value. Anything inside braces that is not a known placeholder is refused.
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
_PH_PARSE = re.compile(r"^(port|host|path|setting)(?::([a-z][a-z0-9_]{0,40}))?$")

#: ids reserved by the host's own layout under <DATA>/apps/ (the legacy launch logs live in <DATA>/apps/logs).
RESERVED_IDS: FrozenSet[str] = frozenset({"logs"})

LOOPBACK_HOST = "127.0.0.1"

#: The v1 user_editable types (fixed by requirement-user-editable).
SETTING_TYPES: Tuple[str, ...] = ("text", "number", "yes/no", "choice", "path")
PATH_KINDS: Tuple[str, ...] = ("dir", "file")
ACCESS_MODES: Tuple[str, ...] = ("read_only", "writes")
REQUIRES_MODES: Tuple[str, ...] = ("check", "start")
STOP_SIGNALS: Tuple[str, ...] = ("SIGTERM", "SIGINT", "SIGHUP", "SIGQUIT", "SIGUSR1", "SIGUSR2")
DEFAULT_STOP_SIGNAL = "SIGTERM"
DEFAULT_STOP_GRACE_S = 10.0          # architect ruling R13: SIGTERM, 10 s grace, then SIGKILL
MAX_STOP_GRACE_S = 300.0

#: ONE schema registry: version -> {top-level field: required?}
SCHEMA: Dict[int, Dict[str, bool]] = {
    1: {
        "schema_version": True, "id": True, "name": True, "version": True, "purpose": True, "owner": True,
        "start": True,
        # health_check is KEPT in the registry only so its refusal can name the rule (g73-amendment-4 rule 1)
        "health_check": False, "port_arg": False, "preferred_port": False, "health_path": False,
        "ui_path": False, "scope": False, "run_at_boot_default": False,
        "paths": False, "backup_before_first_start": False, "requires": False, "requires_files": False,
        "licences": False, "hardware": False, "user_editable": False, "env": False, "stop": False,
        "actions": False, "ui_token": False,
        # round 2 (Diamond F1): the directory the code pin walks; relative to the manifest's directory. Added to
        # v1 without a version bump because v1 has never shipped (no manifest exists in the field) [A, builder].
        "app_root": False,
    },
}
#: X72 (spec B6, g73 amendment item 6): schema_version 2 = version 1 plus ``activity_path``. Version 1 manifests
#: stay valid unchanged; ``activity_path`` in a version 1 manifest is an unknown field and is refused by name.
SCHEMA[2] = {**SCHEMA[1], "activity_path": False}

#: nested object keys -> required?  (strict: an unknown nested key is refused too)
_PATH_KEYS = {"name": True, "description": True, "kind": True, "access": True, "optional": False, "flag": False}
_REQUIRES_KEYS = {"id": True, "mode": True}
_LICENCE_KEYS = {"name": True, "path_param": True}
_HARDWARE_KEYS = {"device_class": True, "presence_check": True}
_SETTING_KEYS = {"name": True, "type": True, "default": True, "description": False, "options": False, "kind": False}
_STOP_KEYS = {"signal": False, "grace": False}
_ACTION_KEYS = {"name": True, "argv": True, "requires_stopped": False, "description": False}
_UI_TOKEN_KEYS = {"mode": True}
_UI_TOKEN_MODES = ("open-only",)

#: env names the host itself sets for the app (GNOMON_APP_*) are not the manifest's to declare.
RESERVED_ENV_PREFIX = "GNOMON_APP_"


class ManifestError(ValueError):
    """A manifest refused at registration. ``field`` names the offending field."""

    def __init__(self, field_: str, message: str) -> None:
        super().__init__(f"{field_}: {message}")
        self.field = field_
        self.message = message


@dataclass(frozen=True)
class PathParam:
    name: str
    description: str
    kind: str
    access: str
    optional: bool = False
    flag: Optional[str] = None


@dataclass(frozen=True)
class Requirement:
    id: str
    mode: str


@dataclass(frozen=True)
class Licence:
    name: str
    path_param: str


@dataclass(frozen=True)
class Hardware:
    device_class: str
    presence_check: Tuple[str, ...]


@dataclass(frozen=True)
class Setting:
    name: str
    type: str
    default: Any
    description: str = ""
    options: Tuple[str, ...] = ()
    kind: Optional[str] = None


@dataclass(frozen=True)
class Action:
    name: str
    argv: Tuple[str, ...]
    requires_stopped: bool = False
    description: str = ""


@dataclass(frozen=True)
class Manifest:
    schema_version: int
    id: str
    name: str
    version: str
    purpose: str
    owner: str
    start: Tuple[str, ...]
    port_arg: Optional[Tuple[str, ...]] = None
    preferred_port: Optional[int] = None
    health_path: Optional[str] = None
    activity_path: Optional[str] = None
    ui_path: Optional[str] = None
    scope: str = "all"
    run_at_boot_default: bool = False
    paths: Tuple[PathParam, ...] = ()
    backup_before_first_start: Tuple[str, ...] = ()
    requires: Tuple[Requirement, ...] = ()
    requires_files: Tuple[str, ...] = ()
    licences: Tuple[Licence, ...] = ()
    hardware: Optional[Hardware] = None
    user_editable: Tuple[Setting, ...] = ()
    env: Tuple[Tuple[str, str], ...] = ()
    stop_signal: str = DEFAULT_STOP_SIGNAL
    stop_grace_s: float = DEFAULT_STOP_GRACE_S
    actions: Tuple[Action, ...] = ()
    ui_token: bool = False
    app_root: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def has_port(self) -> bool:
        return self.port_arg is not None

    @property
    def headless(self) -> bool:
        return self.ui_path is None

    def path(self, name: str) -> Optional[PathParam]:
        for p in self.paths:
            if p.name == name:
                return p
        return None

    def setting(self, name: str) -> Optional[Setting]:
        for s in self.user_editable:
            if s.name == name:
                return s
        return None

    def action(self, name: str) -> Optional[Action]:
        for a in self.actions:
            if a.name == name:
                return a
        return None


# --------------------------------------------------------------------------------------------------------
# placeholder parsing (shared with the preflight's substitution)
# --------------------------------------------------------------------------------------------------------

def placeholders(token: str) -> List[Tuple[str, Optional[str]]]:
    """Every placeholder in ``token`` as ``(kind, name)``; raises ValueError on an unknown one."""
    out: List[Tuple[str, Optional[str]]] = []
    for inner in PLACEHOLDER_RE.findall(token):
        m = _PH_PARSE.match(inner)
        if not m:
            raise ValueError(f"unknown placeholder {{{inner}}} (known: {{port}}, {{host}}, {{path:<name>}}, "
                             f"{{setting:<name>}})")
        kind, name = m.group(1), m.group(2)
        if kind in ("port", "host") and name is not None:
            raise ValueError(f"{{{inner}}}: {{{kind}}} takes no name")
        if kind in ("path", "setting") and name is None:
            raise ValueError(f"{{{inner}}}: {{{kind}:<name>}} needs a name")
        out.append((kind, name))
    # a stray brace that is not part of a {...} pair
    stripped = PLACEHOLDER_RE.sub("", token)
    if "{" in stripped or "}" in stripped:
        raise ValueError(f"unbalanced brace in {token!r}")
    return out


# --------------------------------------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------------------------------------

def _strict_keys(where: str, obj: Any, keys: Dict[str, bool]) -> Dict[str, Any]:
    if not isinstance(obj, dict):
        raise ManifestError(where, "must be a JSON object")
    for k in obj:
        if k not in keys:
            raise ManifestError(f"{where}.{k}" if where else str(k), "unknown field (refused: strict schema)")
    for k, req in keys.items():
        if req and k not in obj:
            raise ManifestError(f"{where}.{k}" if where else k, "required field is missing")
    return obj


def _str(where: str, v: Any, *, allow_empty: bool = False, limit: int = 4000) -> str:
    if not isinstance(v, str) or (not allow_empty and not v.strip()):
        raise ManifestError(where, "must be a non-empty string")
    if len(v) > limit:
        raise ManifestError(where, f"longer than {limit} characters")
    if any(ord(c) < 32 and c not in "\t" for c in v):
        raise ManifestError(where, "contains a control character")
    return v


def _argv(where: str, v: Any) -> Tuple[str, ...]:
    if isinstance(v, str):
        raise ManifestError(where, "must be an argv LIST of strings, never a shell string")
    if not isinstance(v, list) or not v:
        raise ManifestError(where, "must be a non-empty argv list of strings")
    out = []
    for i, t in enumerate(v):
        if not isinstance(t, str) or t == "":
            raise ManifestError(f"{where}[{i}]", "argv items must be non-empty strings")
        if "\x00" in t or "\n" in t:
            raise ManifestError(f"{where}[{i}]", "argv items must not contain NUL or newline")
        out.append(t)
    return tuple(out)


def is_python_interpreter(argv0: str) -> bool:
    """True when ``argv0`` names a Python interpreter by its file name (the host then runs it isolated: -I)."""
    import os

    return bool(PYTHON_RE.match(os.path.basename(argv0)))


def check_argv_shape(where: str, argv: Tuple[str, ...]) -> None:
    """g73-amendment-4 rule 2, the part that needs no filesystem. The executed program is never chosen by a host
    value: NO placeholder of any kind ({path:}, {setting:}, {port}, {host}) in argv[0] or argv[1]; argv[0] is an
    absolute path or a root-relative file (a path with a separator that is not ``~``-prefixed), never a bare command
    (resolved on a PATH an attacker may shadow) and never ``~``; a Python interpreter is followed by its script, not
    by a flag (the host inserts -I itself) and not by -m/-c."""
    for idx in (0, 1):
        if idx < len(argv) and PLACEHOLDER_RE.search(argv[idx]):
            raise ManifestError(f"{where}[{idx}]", "a placeholder ({path:}, {setting:}, {port}, {host}) may not appear "
                                "in argv[0] or argv[1]: the program and its script are never chosen by a host value "
                                "(g73-amendment-4 rule 2)")
    a0 = argv[0]
    if a0.startswith("~"):
        raise ManifestError(f"{where}[0]", "must be an absolute path to a system or framework interpreter (outside "
                            "the user's home and the app root); ~ is refused")
    if "/" not in a0:
        raise ManifestError(f"{where}[0]", f"{a0!r} is a bare command, resolved on a PATH that can be shadowed; name an "
                            "absolute interpreter path (outside the user's home and the app root; a file under the "
                            "app root is never the program, g73-amendment-6)")
    if len(argv) < 2:
        raise ManifestError(f"{where}[1]", "every program is an interpreter and needs a script under the app root as argv[1]")
    if argv[1].startswith(("-", "+")):
        raise ManifestError(f"{where}[1]", f"{argv[1]!r}: the manifest supplies NO flag or option token between the "
                            "interpreter and its script (the host inserts its own hardening flags); argv[1] must be the "
                            "script file under the app root (g73-amendment-6)")


def _name(where: str, v: Any) -> str:
    if not isinstance(v, str) or not NAME_RE.match(v):
        raise ManifestError(where, f"must match {NAME_RE.pattern}")
    return v


def _bool(where: str, v: Any) -> bool:
    if not isinstance(v, bool):
        raise ManifestError(where, "must be true or false")
    return v


def _names_list(where: str, v: Any) -> Tuple[str, ...]:
    if not isinstance(v, list):
        raise ManifestError(where, "must be a list of path parameter names")
    return tuple(_name(f"{where}[{i}]", x) for i, x in enumerate(v))


def validate_setting_value(s: Setting, value: Any, check_fs: bool = True) -> Any:
    """Validate (and normalise) one user_editable value against its declared type. Raises ValueError.
    ``check_fs=False`` (a manifest DEFAULT at registration) checks the path's shape only, never this host's disk."""
    t = s.type
    if t == "text":
        if not isinstance(value, str) or any(ord(c) < 32 for c in value) or len(value) > 4000:
            raise ValueError(f"{s.name}: must be text (no control characters)")
        if value.startswith("-"):
            # Diamond F11 [A, builder]: a text value would reach the app as a flag-looking argv element
            raise ValueError(f"{s.name}: a text value may not start with '-' (it would read as a command-line flag)")
        return value
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{s.name}: must be a number")
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError(f"{s.name}: must be a finite number")
        return value
    if t == "yes/no":
        if not isinstance(value, bool):
            raise ValueError(f"{s.name}: must be yes or no (true/false)")
        return value
    if t == "choice":
        if not isinstance(value, str) or value not in s.options:
            raise ValueError(f"{s.name}: must be one of {list(s.options)}")
        return value
    if t == "path":
        return validate_path_value(s.name, s.kind or "file", value, check_fs=check_fs)
    raise ValueError(f"{s.name}: unknown type {t!r}")


def validate_path_value(name: str, kind: str, value: Any, check_fs: bool = True) -> str:
    """A host path value: absolute, no control characters, and (when it exists) of the declared kind."""
    import os

    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name}: must be a path")
    expanded = os.path.expanduser(value)
    if not os.path.isabs(expanded):
        raise ValueError(f"{name}: must be an absolute path")
    if check_fs and os.path.exists(expanded):
        if kind == "dir" and not os.path.isdir(expanded):
            raise ValueError(f"{name}: is not a directory (declared kind dir)")
        if kind == "file" and not os.path.isfile(expanded):
            raise ValueError(f"{name}: is not a file (declared kind file)")
    return expanded


def parse_manifest(data: Any) -> Manifest:
    """Validate a decoded manifest object. Raises :class:`ManifestError` naming the field."""
    if not isinstance(data, dict):
        raise ManifestError("<manifest>", "must be a JSON object")
    if "schema_version" not in data:
        raise ManifestError("schema_version", "required field is missing")
    sv = data["schema_version"]
    if isinstance(sv, bool) or not isinstance(sv, int) or sv not in SUPPORTED_SCHEMA_VERSIONS:
        raise ManifestError("schema_version", f"unsupported (this host understands {sorted(SUPPORTED_SCHEMA_VERSIONS)})")
    _strict_keys("", data, SCHEMA[sv])

    app_id = data["id"]
    if not isinstance(app_id, str) or not ID_RE.match(app_id):
        raise ManifestError("id", f"must match {ID_RE.pattern}")
    if app_id in RESERVED_IDS:
        raise ManifestError("id", f"{app_id!r} is reserved by the host's own state layout")
    name = _str("name", data["name"], limit=200)
    version = _str("version", data["version"], limit=100)
    purpose = _str("purpose", data["purpose"])
    owner = _str("owner", data["owner"], limit=200)
    start = _argv("start", data["start"])
    check_argv_shape("start", start)

    if "health_check" in data:
        raise ManifestError("health_check", "refused: a status poll executes nothing (g73-amendment-4 rule 1). Health "
                            "is an HTTP GET to health_path on 127.0.0.1; an app with no port shows 'running (health "
                            "unverified)'. Remove health_check.")

    port_arg: Optional[Tuple[str, ...]] = None
    if "port_arg" in data:
        pa = data["port_arg"]
        if isinstance(pa, str):
            toks = tuple(t for t in pa.split() if t)
            if not toks:
                raise ManifestError("port_arg", "must not be empty")
            port_arg = _argv("port_arg", list(toks))
        else:
            port_arg = _argv("port_arg", pa)
        if not any("{port}" in t for t in port_arg):
            raise ManifestError("port_arg", "must contain the {port} placeholder")
    preferred_port = None
    if "preferred_port" in data:
        pp = data["preferred_port"]
        if isinstance(pp, bool) or not isinstance(pp, int) or not (1024 <= pp <= 65535):
            raise ManifestError("preferred_port", "must be an integer port 1024..65535")
        if port_arg is None:
            raise ManifestError("preferred_port", "needs port_arg (an app without port_arg has no port)")
        preferred_port = pp
    health_path = None
    if "health_path" in data:
        health_path = _str("health_path", data["health_path"], limit=500)
        if not health_path.startswith("/"):
            raise ManifestError("health_path", "must start with /")
    if port_arg is not None and health_path is None:
        raise ManifestError("health_path", "is required when port_arg is given (the app has a port)")
    if health_path is not None and port_arg is None:
        raise ManifestError("health_path", "needs port_arg (health is checked on the app's port)")
    activity_path = None
    if "activity_path" in data:
        # X72 (I3): same rules as health_path (starts with /, on the app's own loopback port), and nothing that could name
        # another host: no scheme, no authority ("//"), no userinfo, no query/fragment, no whitespace or control character.
        # (There is no manifest field that could declare a non-local host: the host fetches http://127.0.0.1:<port><path>.)
        activity_path = _str("activity_path", data["activity_path"], limit=500)
        if not activity_path.startswith("/"):
            raise ManifestError("activity_path", "must start with /")
        if (activity_path.startswith("//") or "://" in activity_path or "@" in activity_path or "?" in activity_path
                or "#" in activity_path or "\\" in activity_path
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in activity_path)):
            raise ManifestError("activity_path", "must be a plain path on the app's own loopback port (no scheme, host, "
                                "query, fragment or whitespace): activity is only read from a local app")
        if port_arg is None or health_path is None:
            raise ManifestError("activity_path", "needs port_arg and health_path (activity is read from the app's own "
                                "loopback port, like health)")
    ui_path = None
    if "ui_path" in data:
        ui_path = _str("ui_path", data["ui_path"], limit=500)
        if not ui_path.startswith("/"):
            raise ManifestError("ui_path", "must start with /")
        if port_arg is None:
            raise ManifestError("ui_path", "needs port_arg (the UI is opened on the app's port)")

    scope = data.get("scope", "all")
    if not isinstance(scope, str) or not (scope == "all" or WORKSPACE_RE.match(scope)):
        raise ManifestError("scope", 'must be "all" or one named workspace')
    run_at_boot_default = _bool("run_at_boot_default", data.get("run_at_boot_default", False))

    # -- paths
    paths: List[PathParam] = []
    raw_paths = data.get("paths", [])
    if not isinstance(raw_paths, list):
        raise ManifestError("paths", "must be a list")
    for i, rp in enumerate(raw_paths):
        w = f"paths[{i}]"
        _strict_keys(w, rp, _PATH_KEYS)
        pname = _name(f"{w}.name", rp["name"])
        if any(p.name == pname for p in paths):
            raise ManifestError(f"{w}.name", f"duplicate path parameter {pname!r}")
        kind = rp["kind"]
        if kind not in PATH_KINDS:
            raise ManifestError(f"{w}.kind", f"must be one of {list(PATH_KINDS)}")
        access = rp["access"]
        if access not in ACCESS_MODES:
            raise ManifestError(f"{w}.access", f"must be one of {list(ACCESS_MODES)}")
        optional = _bool(f"{w}.optional", rp.get("optional", False))
        flag = rp.get("flag")
        if optional:
            if flag is None:
                raise ManifestError(f"{w}.flag", "an optional path parameter MUST declare flag (ruling R18)")
            flag = _str(f"{w}.flag", flag, limit=100)
            if not flag.startswith("-") or any(c.isspace() for c in flag) or "{" in flag:
                raise ManifestError(f"{w}.flag", "must be one option token such as --events")
        elif flag is not None:
            raise ManifestError(f"{w}.flag", "flag is only for an optional path parameter")
        if pname == "venv" and (kind != "dir" or access != "read_only"):
            raise ManifestError(w, "the venv path parameter must be kind dir, access read_only (ruling R11b)")
        paths.append(PathParam(pname, _str(f"{w}.description", rp["description"]), kind, access, optional, flag))
    by_path = {p.name: p for p in paths}

    def _declared_paths(where: str, names: Tuple[str, ...]) -> Tuple[str, ...]:
        for j, n in enumerate(names):
            if n not in by_path:
                raise ManifestError(f"{where}[{j}]", f"{n!r} is not declared in paths")
        return names

    backups = _declared_paths("backup_before_first_start",
                              _names_list("backup_before_first_start", data.get("backup_before_first_start", [])))
    requires_files = _declared_paths("requires_files", _names_list("requires_files", data.get("requires_files", [])))

    requires: List[Requirement] = []
    raw_req = data.get("requires", [])
    if not isinstance(raw_req, list):
        raise ManifestError("requires", "must be a list of {id, mode}")
    for i, rr in enumerate(raw_req):
        w = f"requires[{i}]"
        _strict_keys(w, rr, _REQUIRES_KEYS)
        rid = rr["id"]
        if not isinstance(rid, str) or not ID_RE.match(rid):
            raise ManifestError(f"{w}.id", f"must match {ID_RE.pattern}")
        if rid == app_id:
            raise ManifestError(f"{w}.id", "an app cannot require itself")
        mode = rr["mode"]
        if mode not in REQUIRES_MODES:
            raise ManifestError(f"{w}.mode", f"must be one of {list(REQUIRES_MODES)} (got {mode!r})")
        if any(r.id == rid for r in requires):
            raise ManifestError(f"{w}.id", f"duplicate requirement {rid!r}")
        requires.append(Requirement(rid, mode))

    licences: List[Licence] = []
    raw_lic = data.get("licences", [])
    if not isinstance(raw_lic, list):
        raise ManifestError("licences", "must be a list of {name, path_param}")
    for i, rl in enumerate(raw_lic):
        w = f"licences[{i}]"
        _strict_keys(w, rl, _LICENCE_KEYS)
        pp_name = _name(f"{w}.path_param", rl["path_param"])
        if pp_name not in by_path:
            raise ManifestError(f"{w}.path_param", f"{pp_name!r} is not declared in paths")
        if by_path[pp_name].kind != "file":
            raise ManifestError(f"{w}.path_param", "a licence path parameter must be kind file")
        licences.append(Licence(_str(f"{w}.name", rl["name"], limit=200), pp_name))

    hardware = None
    if "hardware" in data:
        _strict_keys("hardware", data["hardware"], _HARDWARE_KEYS)
        hardware = Hardware(_str("hardware.device_class", data["hardware"]["device_class"], limit=200),
                            _argv("hardware.presence_check", data["hardware"]["presence_check"]))
        check_argv_shape("hardware.presence_check", hardware.presence_check)

    settings: List[Setting] = []
    raw_ue = data.get("user_editable", [])
    if not isinstance(raw_ue, list):
        raise ManifestError("user_editable", "must be a list of {name, type, default}")
    for i, rs in enumerate(raw_ue):
        w = f"user_editable[{i}]"
        _strict_keys(w, rs, _SETTING_KEYS)
        sname = _name(f"{w}.name", rs["name"])
        if any(s.name == sname for s in settings):
            raise ManifestError(f"{w}.name", f"duplicate setting {sname!r}")
        stype = rs["type"]
        if stype not in SETTING_TYPES:
            raise ManifestError(f"{w}.type", f"must be one of {list(SETTING_TYPES)} (got {stype!r})")
        options: Tuple[str, ...] = ()
        if stype == "choice":
            opts = rs.get("options")
            if not isinstance(opts, list) or not opts or not all(isinstance(o, str) and o for o in opts):
                raise ManifestError(f"{w}.options", "a choice setting needs a non-empty list of strings")
            if len(set(opts)) != len(opts):
                raise ManifestError(f"{w}.options", "duplicate option")
            options = tuple(opts)
        elif "options" in rs:
            raise ManifestError(f"{w}.options", "options are only for a choice setting")
        kind = None
        if stype == "path":
            kind = rs.get("kind")
            if kind not in PATH_KINDS:
                raise ManifestError(f"{w}.kind", f"a path setting must declare kind, one of {list(PATH_KINDS)}")
        elif "kind" in rs:
            raise ManifestError(f"{w}.kind", "kind is only for a path setting")
        desc = rs.get("description", "")
        if desc != "":
            _str(f"{w}.description", desc)
        s = Setting(sname, stype, rs["default"], desc, options, kind)
        try:
            validate_setting_value(s, rs["default"], check_fs=False)
        except ValueError as exc:
            raise ManifestError(f"{w}.default", f"does not match its type: {exc}")
        settings.append(s)
    by_setting = {s.name for s in settings}

    env: List[Tuple[str, str]] = []
    if "env" in data:
        raw_env = data["env"]
        if not isinstance(raw_env, dict):
            raise ManifestError("env", "must be an object of NAME: value strings (non-secret only)")
        for k, v in raw_env.items():
            if not isinstance(k, str) or not ENV_KEY_RE.fullmatch(k):
                why = (f"names starting {RESERVED_ENV_PREFIX} are set by the host" if isinstance(k, str)
                       and k.startswith(RESERVED_ENV_PREFIX) else
                       "the host builds a CLOSED environment: a manifest may set only APP_<NAME> variables "
                       "(A-Z, 0-9, _; non-secret), never PATH, HOME, PYTHON*, DYLD_*, LD_*, or any other name "
                       "(g73-amendment-4 rule 4)")
                raise ManifestError(f"env.{k}", why)
            env.append((k, _str(f"env.{k}", v, allow_empty=True)))

    stop_signal, stop_grace = DEFAULT_STOP_SIGNAL, DEFAULT_STOP_GRACE_S
    if "stop" in data:
        _strict_keys("stop", data["stop"], _STOP_KEYS)
        sig = data["stop"].get("signal", DEFAULT_STOP_SIGNAL)
        if sig not in STOP_SIGNALS:
            raise ManifestError("stop.signal", f"must be one of {list(STOP_SIGNALS)}")
        g = data["stop"].get("grace", DEFAULT_STOP_GRACE_S)
        if isinstance(g, bool) or not isinstance(g, (int, float)) or not (0 <= g <= MAX_STOP_GRACE_S):
            raise ManifestError("stop.grace", f"must be a number of seconds 0..{MAX_STOP_GRACE_S:.0f}")
        stop_signal, stop_grace = sig, float(g)

    actions: List[Action] = []
    raw_act = data.get("actions", [])
    if not isinstance(raw_act, list):
        raise ManifestError("actions", "must be a list of {name, argv, requires_stopped}")
    for i, ra in enumerate(raw_act):
        w = f"actions[{i}]"
        _strict_keys(w, ra, _ACTION_KEYS)
        aname = _name(f"{w}.name", ra["name"])
        if any(a.name == aname for a in actions):
            raise ManifestError(f"{w}.name", f"duplicate action {aname!r}")
        desc = ra.get("description", "")
        if desc != "":
            _str(f"{w}.description", desc)
        aargv = _argv(f"{w}.argv", ra["argv"])
        check_argv_shape(f"{w}.argv", aargv)
        actions.append(Action(aname, aargv, _bool(f"{w}.requires_stopped", ra.get("requires_stopped", False)), desc))

    ui_token = False
    if "ui_token" in data:
        _strict_keys("ui_token", data["ui_token"], _UI_TOKEN_KEYS)
        if data["ui_token"]["mode"] not in _UI_TOKEN_MODES:
            raise ManifestError("ui_token.mode", f"must be one of {list(_UI_TOKEN_MODES)}")
        if ui_path is None:
            raise ManifestError("ui_token", "needs ui_path (the token rides on the Open URL)")
        ui_token = True

    app_root = None
    if "app_root" in data:
        app_root = _str("app_root", data["app_root"], limit=1000)
        if "{" in app_root or "\x00" in app_root:
            raise ManifestError("app_root", "must be a plain path (no placeholders)")

    # -- placeholders: every {path:x} / {setting:x} is declared; optional paths never appear as a placeholder
    def _check_tokens(where: str, tokens: Tuple[str, ...]) -> None:
        for j, tok in enumerate(tokens):
            try:
                phs = placeholders(tok)
            except ValueError as exc:
                raise ManifestError(f"{where}[{j}]", str(exc))
            for kind, pname in phs:
                if kind == "path":
                    if pname not in by_path:
                        raise ManifestError(f"{where}[{j}]", f"{{path:{pname}}} names an undeclared path parameter")
                    if by_path[pname].optional:
                        raise ManifestError(f"{where}[{j}]", f"{{path:{pname}}}: an optional path parameter may "
                                            "not be a placeholder; the host appends [flag, value] when set (R18)")
                elif kind == "setting" and pname not in by_setting:
                    raise ManifestError(f"{where}[{j}]", f"{{setting:{pname}}} names an undeclared user_editable setting")
                elif kind == "port" and port_arg is None:
                    raise ManifestError(f"{where}[{j}]", "{port} is used but the app declares no port_arg")

    _check_tokens("start", start)
    if port_arg is not None:
        _check_tokens("port_arg", port_arg)
    if hardware is not None:
        _check_tokens("hardware.presence_check", hardware.presence_check)
    for i, a in enumerate(actions):
        _check_tokens(f"actions[{i}].argv", a.argv)
    for k, v in env:
        _check_tokens(f"env.{k}", (v,) if v else ())

    return Manifest(
        schema_version=sv, id=app_id, name=name, version=version, purpose=purpose, owner=owner, start=start,
        port_arg=port_arg, preferred_port=preferred_port, health_path=health_path,
        activity_path=activity_path, ui_path=ui_path, scope=scope, run_at_boot_default=run_at_boot_default, paths=tuple(paths),
        backup_before_first_start=backups, requires=tuple(requires), requires_files=requires_files,
        licences=tuple(licences), hardware=hardware, user_editable=tuple(settings), env=tuple(env),
        stop_signal=stop_signal, stop_grace_s=stop_grace, actions=tuple(actions), ui_token=ui_token,
        app_root=app_root, raw=dict(data),
    )


#: a manifest larger than this is refused (it is a small description, never data)
MAX_MANIFEST_BYTES = 256 * 1024


def read_manifest_bytes(path: str) -> bytes:
    """The manifest file's bytes (regular file only, size-capped). Raises ManifestError."""
    import os
    import stat as _stat

    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        raise ManifestError("<manifest>", f"cannot open {path}: {exc.strerror or exc}")
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode):
            raise ManifestError("<manifest>", f"{path} is not a regular file")
        if st.st_size > MAX_MANIFEST_BYTES:
            raise ManifestError("<manifest>", f"{path} is larger than {MAX_MANIFEST_BYTES} bytes")
        chunks = []
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
        return b"".join(chunks)
    finally:
        os.close(fd)


def load_manifest(path: str) -> Tuple[Manifest, bytes]:
    """Read + parse + validate the manifest at ``path``. Returns ``(manifest, raw_bytes)``."""
    raw = read_manifest_bytes(path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ManifestError("<manifest>", f"not valid JSON: {exc}")
    return parse_manifest(data), raw

"""The preflight: EVERY precondition, checked at EVERY start, hub up or not (
requirement-preflight-wrapper; architect rulings R6, R18; g73: "a new precondition kind is added to the preflight
wrapper (the single enforcement point), never to one caller").

Order (ruling R6): manifest pin and code pin; required path values (R18 path-unset); licences; requires
(mode check: health; mode start: start-or-wait with bounded backoff, then refuse); requires_files;
backup_before_first_start; hardware. The first failure raises :class:`Refusal` with a NAMED reason, which the
wrapper records in ``runtime/preflight.json`` and the status reads.

The same function serves the wrapper (``launch.py``, the real gate, run by launchd), the host's synchronous
pre-check before it kickstarts (so a refusal is reported to the caller at once), and the actions (which go
through the same preflight as a start).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import re as _re

from . import licence as _licence
from . import pins as _pins
from . import rules as _rules
from .launchd import SAFE_APP_PATH
from .manifest import (LOOPBACK_HOST, PLACEHOLDER_RE, Manifest, ManifestError, parse_manifest, placeholders,
                       read_manifest_bytes)
from .state import HostPaths, atomic_write_json, manifest_entries, read_json, read_ports, utc_now_iso

#: the named refusal reasons the status understands (requirement-refused, R6, R8, R18)
REASONS = ("licence", "backup", "requires", "requires_files", "hardware", "manifest-changed", "manifest-moved",
           "code-changed", "non-loopback", "path-unset", "not-installed", "port-unassigned")
#: the wrapper's exit status for a refusal (EX_CONFIG); the status reads the record, not just this code
REFUSAL_EXIT = 78
HEALTH_TIMEOUT_S = 1.0
#: requires mode start: total wait for the dependency to become healthy, and the backoff steps [A, builder]
REQUIRES_START_BUDGET_S = 30.0
REQUIRES_START_BACKOFF_S = (0.25, 0.5, 1.0, 2.0, 4.0)
PRESENCE_TIMEOUT_S = 15.0
BACKUP_ABSENT_NOTE = "nothing to back up (path absent)"


class Refusal(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        assert reason in REASONS, reason
        super().__init__(f"refused({reason}): {detail}")
        self.reason = reason
        self.detail = detail


# --------------------------------------------------------------------------------------------------------
# health helpers (also used by the status)
# --------------------------------------------------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """R3-F4: the health GET never follows a redirect: an app that answers 3xx must not make the host request any other
    URL (ruling: 3xx = unhealthy / unverified; loopback only)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


#: an opener with ONLY the http handler (no ftp/file/https handlers) and no redirect handler that follows
_OPENER = urllib.request.OpenerDirector()
_OPENER.add_handler(urllib.request.HTTPHandler())
_OPENER.add_handler(urllib.request.HTTPDefaultErrorHandler())
_OPENER.add_handler(_NoRedirect())
_OPENER.add_handler(urllib.request.HTTPErrorProcessor())


def _get_2xx(url: str, timeout: float) -> bool:
    try:
        with _OPENER.open(url, timeout=timeout) as r:       # a 3xx/4xx/5xx raises HTTPError (a URLError): unhealthy
            return 200 <= r.status < 300
    except (urllib.error.URLError, OSError, ValueError):
        return False


def http_healthy(port: int, path: str, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    """[A, builder] "the health URL answers" = an HTTP 2xx from http://127.0.0.1:<port><path> (loopback only; a redirect
    is NOT followed and reads as unhealthy: round-3c R3-F4)."""
    return _get_2xx(f"http://{LOOPBACK_HOST}:{int(port)}{path}", timeout)


def url_healthy(url: str, timeout: float = HEALTH_TIMEOUT_S) -> bool:
    # R3C-F9: parsed, not prefix-matched (``http://127.0.0.1:80@evil/`` has the host ``evil``)
    try:
        u = urllib.parse.urlsplit(url)
        host, _port = u.hostname, u.port
    except ValueError:
        return False
    if u.scheme != "http" or host not in ("127.0.0.1", "::1", "localhost") or u.username or u.password or _port is None:
        return False
    return _get_2xx(url, timeout)


def run_argv_ok(argv: List[str], cwd: str, timeout: float, env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """Run a host-built, JUDGED argv (a presence check) with the CLOSED env it was given (g73-amendment-4 rules 3 and 4);
    never with the host's own environment. R3G-F1: this is an exec site like the wrapper's and the action runner's, so it
    takes the sealed :class:`JudgedArgv` from :func:`exec_argv` (exact type; R3H-F2), calls ``verify()`` and execs ``judged.exec_target()``
    (``executable=``); a plain list (no ``.judged``) is refused, never run (g73-amendment-10 rule (i))."""
    if type(argv) is not JudgedArgv:                # exact type: a list, a list subclass or a duck-typed object is refused
        raise Refusal("code-changed", "the presence check was not judged by exec_argv; refusing to run it (g73-amendment-10)")
    try:
        argv.verify()                                           # the SAME tokens, and the judged files unchanged (R3H-F2)
        judged = argv.judged
        target = judged.exec_target()
    except _rules.RuleViolation as exc:
        raise Refusal("code-changed", exc.message)
    try:
        r = subprocess.run(list(argv), executable=target, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, env=env if env is not None else {"PATH": SAFE_APP_PATH})
    except subprocess.TimeoutExpired:
        return -1, "timed out"
    except OSError as exc:
        return -1, f"could not run: {exc.strerror or exc}"
    return r.returncode, (r.stdout or "").strip()


# --------------------------------------------------------------------------------------------------------
# dependencies the preflight needs (injected; the wrapper and the host each build theirs)
# --------------------------------------------------------------------------------------------------------

@dataclass
class Deps:
    #: dep id -> (healthy?, safe detail)
    dependency_healthy: Callable[[str], Tuple[bool, str]]
    #: start a mode-start dependency through the launchd path
    start_dependency: Callable[[str], None]
    today: Callable[[], _dt.date] = field(default=lambda: _dt.datetime.now(_dt.timezone.utc).date())
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    requires_start_budget_s: float = REQUIRES_START_BUDGET_S
    #: run an argv (presence_check) with the given closed env -> (exit code, stdout)
    run: Callable[[List[str], str, float, Dict[str, str]], Tuple[int, str]] = run_argv_ok


@dataclass
class Prepared:
    manifest: Manifest
    manifest_path: str
    raw: bytes
    settings: Dict[str, Any]
    pin: Dict[str, Any]
    port: Optional[int]
    cwd: str
    warnings: List[str] = field(default_factory=list)


# --------------------------------------------------------------------------------------------------------
# loading an INSTALLED app's state
# --------------------------------------------------------------------------------------------------------

def load_installed(paths: HostPaths, app_id: str) -> Prepared:
    entries = manifest_entries(paths)
    if app_id not in entries:
        raise Refusal("not-installed", f"{app_id!r} is not an installed app (no confirmed install in apps.json)")
    registry_path = entries[app_id]
    settings = read_json(paths.settings(app_id), default=None)
    pin = read_json(paths.pin(app_id), default=None)
    if not isinstance(settings, dict) or not isinstance(pin, dict) or not isinstance(pin.get("manifest_path"), str):
        raise Refusal("not-installed", f"{app_id!r} has no settings.json / pin.json under the DATA root")
    # F2: ONE manifest path. apps.json is an index; the app is verified and launched from the PINNED path.
    mpath = pin["manifest_path"]
    if os.path.realpath(registry_path) != mpath:
        raise Refusal("manifest-moved", f"apps.json names {os.path.realpath(registry_path)}, but the confirmed "
                                        f"manifest is {mpath}; re-confirm")
    try:
        raw = read_manifest_bytes(mpath)
    except ManifestError as exc:
        raise Refusal("manifest-changed", f"the manifest cannot be read: {exc.message}")
    manifest: Optional[Manifest]
    try:
        manifest = parse_manifest(json.loads(raw.decode("utf-8")))
    except (ManifestError, UnicodeDecodeError, ValueError):
        manifest = None
    bad = _pins.verify(pin, manifest, raw, settings, manifest_path=registry_path)
    if bad is not None:
        raise Refusal(bad[0], bad[1])
    assert manifest is not None
    if _pins.pinned_manifest_bytes(paths.pin_snapshot(app_id), pin) is None:
        # the host's behaviour source (stop signal, ui_path, health) is the sha-checked copy taken at confirm
        raise Refusal("manifest-changed", "the pinned manifest copy under the DATA root is missing or altered; "
                                          "re-confirm the app")
    if manifest.id != app_id:
        raise Refusal("manifest-changed", f"the manifest's id is {manifest.id!r}, not {app_id!r}")
    port = None
    if manifest.has_port:
        row = read_ports(paths).get(app_id)
        if row is None:
            raise Refusal("port-unassigned", f"no port is assigned to {app_id!r} in ports.json")
        port = int(row["port"])
    return Prepared(manifest, mpath, raw, settings, pin, port, os.path.dirname(mpath))


# --------------------------------------------------------------------------------------------------------
# substitution
# --------------------------------------------------------------------------------------------------------

def setting_value(manifest: Manifest, settings: Dict[str, Any], name: str) -> Any:
    s = manifest.setting(name)
    vals = settings.get("user") or {}
    return vals.get(name, s.default if s is not None else None)


def _stringify(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def substitute(tokens: Tuple[str, ...], manifest: Manifest, settings: Dict[str, Any], port: Optional[int]) -> List[str]:
    """ONE pass per token (Diamond F11): a substituted value is never re-scanned, so a value containing
    ``{setting:x}`` or ``{port}`` stays literal."""
    import re as _re

    path_vals = settings.get("paths") or {}

    def repl(m: "_re.Match") -> str:
        inner = m.group(1)
        kind, _, name = inner.partition(":")
        if kind == "port":
            if port is None:
                raise Refusal("port-unassigned", "the argv uses {port} but no port is assigned")
            return str(port)
        if kind == "host":
            return LOOPBACK_HOST
        if kind == "path":
            v = path_vals.get(name)
            if not v:
                raise Refusal("path-unset", f"the required path parameter {name!r} has no value")
            return v
        if kind == "setting":
            return _stringify(setting_value(manifest, settings, name))
        raise ValueError(f"unknown placeholder {{{inner}}}")

    out = []
    for tok in tokens:
        placeholders(tok)
        out.append(PLACEHOLDER_RE.sub(repl, tok))
    return out


def build_start_argv(p: Prepared) -> List[str]:
    """The UNGUARDED start argv (hardened start + port_arg + optional path flags). Production code never calls this
    directly: it goes through :func:`exec_argv`, the single choke point (round 3c)."""
    return _build_start(p)[0]


def _build_start(p: Prepared) -> Tuple[List[str], int]:
    m = p.manifest
    argv = _rules.harden_argv(substitute(m.start, m, p.settings, p.port), p.cwd)      # rule 3: Python gets -I -B
    n_fixed = len(argv)
    if m.port_arg is not None:
        argv += substitute(m.port_arg, m, p.settings, p.port)
    path_vals = p.settings.get("paths") or {}
    for pp in m.paths:
        if pp.optional and path_vals.get(pp.name):
            argv += [pp.flag, path_vals[pp.name]]          # type: ignore[list-item]
    return argv, n_fixed


def build_action_argv(p: Prepared, action_name: str) -> List[str]:
    """UNGUARDED (see :func:`exec_argv`)."""
    a = p.manifest.action(action_name)
    if a is None:
        raise KeyError(action_name)
    return _rules.harden_argv(substitute(a.argv, p.manifest, p.settings, p.port), p.cwd)


_SEAL = object()           # private factory token: only :func:`exec_argv` holds it (R3H-F2)


class JudgedArgv:
    """The argv :func:`exec_argv` returns, SEALED (R3H-F2): an immutable sequence of the judged tokens plus the
    :class:`rules.Judged` they were judged under. It is not a ``list`` (no subclass of it can be passed off as one, and no
    item can be assigned); it cannot be built without the module's private seal; and the digest of its tokens and of the
    judgement was bound at judgement time, so :meth:`verify` re-checks that the tokens now are the tokens then. Every exec
    site takes one of these, calls :meth:`verify`, and execs ``judged.exec_target()`` (R3E-F1, R3F-F1, g73-amendment-8/10).

    Limit (stated, same-uid and in-process): code that can call ``object.__setattr__`` on the slots AND recompute the
    sha-256 held inside ``Judged`` can still forge one; the seal stops accident, a stale object, a list subclass and an edit
    after judgement, not a hostile caller already inside the host process."""

    __slots__ = ("_items", "_judged")

    def __init__(self, items, judged: "_rules.Judged", seal=None) -> None:
        if seal is not _SEAL:
            raise TypeError("JudgedArgv is built only by preflight.exec_argv (R3H-F2)")
        object.__setattr__(self, "_items", tuple(items))
        object.__setattr__(self, "_judged", judged)

    def __setattr__(self, name, value):
        raise AttributeError("JudgedArgv is immutable (R3H-F2)")

    __delattr__ = __setattr__

    @property
    def judged(self) -> "_rules.Judged":
        return self._judged

    def verify(self) -> None:
        """judged files unchanged AND these tokens are the tokens that were judged; raises rules.RuleViolation."""
        self._judged.verify(self._items)

    def __iter__(self):
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, i):
        r = self._items[i]
        return list(r) if isinstance(i, slice) else r

    def __eq__(self, other) -> bool:
        if isinstance(other, JudgedArgv):
            return self._items == other._items
        return isinstance(other, (list, tuple)) and list(self._items) == list(other)

    __hash__ = None  # type: ignore[assignment]

    def __repr__(self) -> str:
        return f"JudgedArgv({list(self._items)!r})"


def exec_argv(p: Prepared, kind: str, name: Optional[str] = None) -> "JudgedArgv":
    """THE CHOKE POINT (round 3c, R3-F2/F3/F11): build the final argv for ``kind`` ("start" | "action" | "presence")
    and run :func:`rules.final_check` on it. EVERY argv the host or its wrapper executes for an app is obtained here,
    so a launch path that skips the check cannot exist without bypassing this function (the reachability test
    monkeypatches ``rules.final_check`` to raise and asserts every path refuses; a source test pins the exec sites).
    Raises :class:`Refusal` (``code-changed``: the pinned tree and manifest no longer satisfy the argv rule)."""
    m = p.manifest
    if kind == "start":
        argv, n_fixed = _build_start(p)
        raw = m.start
    elif kind == "action":
        a = m.action(name or "")
        if a is None:
            raise KeyError(name)
        argv = build_action_argv(p, a.name)
        n_fixed, raw = len(argv), a.argv
    elif kind == "presence":
        if m.hardware is None:
            raise KeyError("presence")
        raw = m.hardware.presence_check
        argv = _rules.harden_argv(substitute(raw, m, p.settings, p.port), p.cwd)
        n_fixed = len(argv)
    else:
        raise ValueError(kind)
    root = (p.pin.get("code") or {}).get("root")
    if not isinstance(root, str):
        raise Refusal("code-changed", "the pin carries no app root; re-confirm")
    try:
        judged = _rules.final_check(argv, raw=tuple(raw), n_fixed=n_fixed, cwd=p.cwd, root=root,
                                    where={"start": "start", "action": f"actions[{name}].argv",
                                           "presence": "hardware.presence_check"}[kind])
    except _rules.RuleViolation as exc:
        raise Refusal("code-changed", exc.message)
    return JudgedArgv(argv, judged, _SEAL)


def path_access_map(p: Prepared) -> Dict[str, Dict[str, str]]:
    """Label-and-withhold (ruling R9): every path the app is given, with its DECLARED access mark only."""
    vals = p.settings.get("paths") or {}
    return {pp.name: {"path": vals[pp.name], "access": pp.access} for pp in p.manifest.paths if vals.get(pp.name)}


_LANG_RE = _re.compile(r"[A-Za-z0-9_.@-]{1,40}")
_TZ_RE = _re.compile(r"[A-Za-z0-9_/+:-]{1,64}")
DEFAULT_LANG = "en_US.UTF-8"


def ensure_app_home(paths: HostPaths, app_id: str) -> str:
    """The host-owned HOME for this app (0700), created on demand."""
    home = paths.app_home(app_id)
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)
    return home


def build_env(paths: HostPaths, p: Prepared, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """g73-amendment-4 rule 4: the env is built from an ALLOW-list, never from the host's own environment minus a
    deny-list. PATH (host-fixed, system dirs only), LANG, TZ (the host's, if well-formed), HOME (a host-owned per-app
    dir), the manifest's APP_<NAME> variables, and the host's own: GNOMON_APP_ID, GNOMON_APP_PATH_ACCESS,
    PYTHONDONTWRITEBYTECODE=1 (rule 3) and PYTHONNOUSERSITE=1 (so a Python started INDIRECTLY, such as the python3 a
    run.sh execs, ignores the user site too: -I only covers the interpreter the host itself starts)."""
    base = base if base is not None else {}
    env: Dict[str, str] = {"PATH": SAFE_APP_PATH}
    lang = base.get("LANG", "")
    env["LANG"] = lang if _LANG_RE.fullmatch(lang or "") else DEFAULT_LANG
    tz = base.get("TZ", "")
    if _TZ_RE.fullmatch(tz or ""):
        env["TZ"] = tz
    env["HOME"] = ensure_app_home(paths, p.manifest.id)
    for k, v in p.manifest.env:
        env[k] = substitute((v,), p.manifest, p.settings, p.port)[0] if v else ""
    env["GNOMON_APP_ID"] = p.manifest.id
    env["GNOMON_APP_PATH_ACCESS"] = json.dumps(path_access_map(p), sort_keys=True)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONNOUSERSITE"] = "1"
    return env


# --------------------------------------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------------------------------------

def check_all(paths: HostPaths, app_id: str, deps: Deps) -> Prepared:
    """Run EVERY precondition for ``app_id``. Returns the prepared launch; raises :class:`Refusal`."""
    p = load_installed(paths, app_id)                               # manifest pin + code pin
    m = p.manifest
    vals = p.settings.get("paths") or {}
    for pp in m.paths:                                              # R18: a required path with no value
        if not pp.optional and not vals.get(pp.name):
            raise Refusal("path-unset", f"the required path parameter {pp.name!r} has no value")
    exec_argv(p, "start")                                           # round 3c: the final argv rule, at the start pre-check
    today = deps.today()
    for lic in m.licences:                                          # licences
        lp = vals.get(lic.path_param)
        try:
            info = _licence.read_licence(lp)
        except _licence.LicenceUnreadable as exc:
            raise Refusal("licence", f"licence {lic.name!r}: {exc}")
        verdict, msg = _licence.assess(info, today)
        if verdict in ("expired", "unknown"):
            raise Refusal("licence", f"licence {lic.name!r}: {msg}")
        if verdict == "warn":
            p.warnings.append(f"licence {lic.name!r}: {msg}")
    for req in m.requires:                                          # requires
        ok, why = deps.dependency_healthy(req.id)
        if ok:
            continue
        if req.mode == "check":
            raise Refusal("requires", f"dependency {req.id!r} (mode check) is not healthy: {why}")
        deps.start_dependency(req.id)
        deadline = deps.monotonic() + deps.requires_start_budget_s
        i = 0
        while True:
            ok, why = deps.dependency_healthy(req.id)
            if ok:
                break
            if deps.monotonic() >= deadline:
                raise Refusal("requires", f"dependency {req.id!r} (mode start) never became healthy within "
                                          f"{deps.requires_start_budget_s:.0f} s: {why}")
            deps.sleep(REQUIRES_START_BACKOFF_S[min(i, len(REQUIRES_START_BACKOFF_S) - 1)])
            i += 1
    for name in m.requires_files:                                   # requires_files
        v = vals.get(name)
        if not v or not os.path.exists(v):
            raise Refusal("requires_files", f"the required file {name!r} does not exist")
    if m.backup_before_first_start:                                 # backup_before_first_start
        rec = read_json(paths.backups(app_id), default={}) or {}
        changed = False
        for name in m.backup_before_first_start:
            if isinstance(rec.get(name), dict):
                continue
            v = vals.get(name)
            if v and not os.path.exists(v):
                rec[name] = {"recorded_at": utc_now_iso(), "note": BACKUP_ABSENT_NOTE, "path": v}
                changed = True
                continue
            raise Refusal("backup", f"no backup of {name!r} is recorded (first start needs one)")
        if changed:
            atomic_write_json(paths.backups(app_id), rec)
    if m.hardware is not None:                                      # hardware
        argv = exec_argv(p, "presence")
        code, out = deps.run(argv, p.cwd, PRESENCE_TIMEOUT_S, build_env(paths, p))
        if not (code == 0 and out.strip().lower() == "ok"):
            raise Refusal("hardware", f"device {m.hardware.device_class!r} is missing")
    return p


def record_preflight(paths: HostPaths, app_id: str, outcome: str, reason: Optional[str] = None,
                     detail: Optional[str] = None, warnings: Optional[List[str]] = None) -> None:
    rec: Dict[str, Any] = {"ts": utc_now_iso(), "outcome": outcome, "pid": os.getpid()}
    if reason:
        rec["reason"] = reason
    if detail:
        rec["detail"] = detail
    if warnings:
        rec["warnings"] = warnings
    try:
        atomic_write_json(paths.preflight_record(app_id), rec)
    except OSError:
        pass

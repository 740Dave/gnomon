"""The I6 SAFETY-PATH REGISTRY and the FILE-ACCESS TRACER (checklist g5 and g34 (h)).

I6: machine safety paths (alerts, BREAK-GLASS, the 98% wind-down, the usage-limit trigger, relaunch, bootstrap jobs,
MACHINE attention state) never open DEPLOY ``workspaces/**`` or DATA ``workspaces/**``. Two instruments enforce it:

* :class:`AccessTracer`: while active, every open / listdir / scandir / walk / glob / chdir / write-class call (audit
  events) and every ``os.stat`` / ``os.lstat`` (which ``os.path.exists`` / ``isdir`` / ``Path.exists`` use; wrapped, as
  CPython emits no audit event for stat) whose path is under a forbidden root is RECORDED as a violation. It is
  inert outside a context. NOT covered (stated limit): a native extension doing its own ``open``; a caller that bound
  ``os.stat`` before the context began (``from os import stat``); a child process.
* :func:`import_closure`: the workspace-package modules a module can reach by import. A safety module's closure must lie
  inside the ALLOW-LIST :data:`SAFE_WORKSPACE_MODULES`; anything not on it (the model loader, the writers, the stamping
  library, and any module added later) is forbidden to a safety path.

:data:`SAFETY_PATHS` is the registry of the safety paths the invariant names (seven named; eight rows: relaunch also has its settings read traced). Each is either ``traced`` (it has code
and a runner the trace test executes) or ``unbuilt`` (no code exists yet: it names the slice that builds it, and that
slice MUST flip it to ``traced`` with a runner in the same change, or the registry test fails: g34 (h)). A new safety path
added by any feature is added here, with a runner, in the same change.

This module is itself a safety module: it imports no workspace config code at module level.
"""

from __future__ import annotations

import ast
import os
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

#: ALLOW-LIST (F7): the ONLY workspace-package modules a safety module may reach by import. Everything else in the package
#: reads or writes WORKSPACE CONFIG (or the converter) and is forbidden to a safety path (I6). A new module is therefore
#: forbidden to safety code until it is deliberately added HERE, and ``tests/test_x13s2_safety_paths.py`` fails until every
#: ``workspace/*.py`` is classified in exactly one of the two sets below.
SAFE_WORKSPACE_MODULES = frozenset({"bundle", "events", "machine", "settings_resolve", "settings_registry", "scopebase", "safety"})
#: the modules that ARE safety code (their import closure is checked against the allow-list)
SAFETY_MODULES = ("bundle", "events", "machine", "settings_resolve", "settings_registry", "scopebase", "safety")
#: the workspace-config / converter / writer modules (classified, so a new module cannot be forgotten)
CONFIG_MODULES = frozenset({"model", "writers", "scope", "store", "launchhook", "audit", "ownership", "convert", "deploy", "entities",
                            "registry", "ltsread", "rules", "fingerprint", "writeguard", "subject", "apply"})

# audit event -> indexes of the args that are paths
_PATH_EVENTS: Dict[str, Tuple[int, ...]] = {
    "open": (0,), "os.listdir": (0,), "os.scandir": (0,), "os.walk": (0,), "glob.glob": (0,), "os.chdir": (0,),
    "os.mkdir": (0,), "os.remove": (0,), "os.rmdir": (0,), "os.truncate": (0,), "os.chmod": (0,), "os.utime": (0,),
    "os.rename": (0, 1), "os.link": (0, 1), "os.symlink": (1,), "shutil.copyfile": (0, 1), "shutil.copytree": (0, 1),
    "shutil.rmtree": (0,), "shutil.move": (0, 1), "sqlite3.connect": (0,),
}

_lock = threading.Lock()
_active: List["AccessTracer"] = []
_installed = False
#: re-entrancy guard: resolving a path (``os.path.realpath`` calls ``os.lstat``) must not be traced itself
_busy = threading.local()
_orig_stat = os.stat
_orig_lstat = os.lstat


def _real(p: Any) -> Optional[str]:
    if isinstance(p, int) or p is None:
        return None
    try:
        return os.path.realpath(os.fsdecode(p))
    except Exception:  # noqa: BLE001 - an undecodable path cannot be under a forbidden root
        return None


def _under(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _hook(event: str, args: tuple) -> None:
    idxs = _PATH_EVENTS.get(event)
    if not idxs or not _active or getattr(_busy, "on", False):
        return
    _busy.on = True
    try:
        for i in idxs:
            if i < len(args):
                rp = _real(args[i])
                if rp is None:
                    continue
                for t in list(_active):
                    t._check(event, rp)
    finally:
        _busy.on = False


def _stat_wrap(orig: Callable) -> Callable:
    def wrapper(path, *a, **kw):
        if _active and not getattr(_busy, "on", False):
            _busy.on = True
            try:
                rp = _real(path)
                if rp is not None:
                    for t in list(_active):
                        t._check("os.stat", rp)
            finally:
                _busy.on = False
        return orig(path, *a, **kw)
    wrapper.__wrapped__ = orig  # type: ignore[attr-defined]
    return wrapper


class AccessTracer:
    """Record every access under ``forbidden_roots`` while the context is active. Use ``with AccessTracer(roots) as t``
    then read ``t.violations`` (``[(event, realpath)]``)."""

    def __init__(self, forbidden_roots: Sequence[str]) -> None:
        self.forbidden = [os.path.realpath(r) for r in forbidden_roots]
        self.violations: List[Tuple[str, str]] = []
        self.observed = 0

    def _check(self, event: str, rp: str) -> None:
        self.observed += 1
        if any(_under(rp, r) for r in self.forbidden):
            self.violations.append((event, rp))

    def __enter__(self) -> "AccessTracer":
        global _installed
        with _lock:
            if not _installed:
                sys.addaudithook(_hook)
                _installed = True
            if not _active:
                os.stat = _stat_wrap(_orig_stat)      # type: ignore[assignment]
                os.lstat = _stat_wrap(_orig_lstat)    # type: ignore[assignment]
            _active.append(self)
        return self

    def __exit__(self, *exc: Any) -> None:
        with _lock:
            if self in _active:
                _active.remove(self)
            if not _active:
                os.stat = _orig_stat                  # type: ignore[assignment]
                os.lstat = _orig_lstat                # type: ignore[assignment]


def forbidden_roots(deploy_root: str, data_root: str) -> List[str]:
    """DEPLOY ``workspaces/`` and DATA ``workspaces/`` (I6 path-set)."""
    return [os.path.join(deploy_root, "workspaces"), os.path.join(data_root, "workspaces")]


# --- the import closure (static check) --------------------------------------------------------------------------

#: F-R2-6: a module that imports DYNAMICALLY (``importlib.import_module`` / ``__import__`` / ``spec_from_file_location`` /
#: ``import_module`` with a name the AST cannot read) cannot be proven to stay inside the allow-list, so the closure reports this
#: sentinel, which is on no allow-list: ``forbidden_reach`` then flags it. A constant dynamic import of a workspace module is
#: resolved to that module instead (and judged by the allow-list like a static import).
DYNAMIC_IMPORT = "<dynamic-import>"
_DYNAMIC_FUNCS = {"import_module", "__import__", "spec_from_file_location", "spec_from_loader", "module_from_spec", "exec_module", "find_spec", "load_module"}
_PKG_PREFIX = "gnomon.workspace"


def _dynamic_imports(tree: ast.AST) -> Set[str]:
    out: Set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
        if name not in _DYNAMIC_FUNCS:
            continue
        if name in ("import_module", "__import__") and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            target = node.args[0].value
            if target == _PKG_PREFIX or target.startswith(_PKG_PREFIX + "."):
                rest = target[len(_PKG_PREFIX):].lstrip(".")
                out.add(rest.split(".")[0] if rest else DYNAMIC_IMPORT)
            elif target.startswith("."):                           # relative: resolved against ``package=`` (the workspace package, or unreadable)
                out.add(target.lstrip(".").split(".")[0] or DYNAMIC_IMPORT)
            # any other constant name is a non-workspace module: not our closure
            continue
        out.add(DYNAMIC_IMPORT)
    return out


def _workspace_imports(source: str, module: str) -> Set[str]:
    """The workspace-package sibling modules a source imports (``from .x import y``, ``from . import x``,
    ``from gnomon.workspace.x import y``, ``import gnomon.workspace.x``), plus dynamic imports
    (:data:`DYNAMIC_IMPORT`, or the module a constant ``importlib.import_module`` names)."""
    out: Set[str] = set()
    tree = ast.parse(source)
    out |= _dynamic_imports(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module:
                out.add(node.module.split(".")[0])
            elif node.level == 1 and not node.module:
                out.update(a.name for a in node.names)
            elif node.level == 0 and node.module and node.module.startswith("gnomon.workspace"):
                rest = node.module[len("gnomon.workspace"):].lstrip(".")
                if rest:
                    out.add(rest.split(".")[0])
                else:
                    out.update(a.name for a in node.names)
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("gnomon.workspace."):
                    out.add(a.name[len("gnomon.workspace."):].split(".")[0])
    out.discard(module)
    return out


def import_closure(module: str, package_dir: Optional[str] = None) -> Set[str]:
    """Every workspace-package module reachable from ``module`` by import (module-level AND function-level imports:
    a lazy import is still an import; and DYNAMIC imports, F-R2-6: a constant ``importlib.import_module`` of a workspace module is
    followed, any other dynamic import yields :data:`DYNAMIC_IMPORT`). LIMIT: still static: a name built at run time and passed to
    ``exec`` / ``eval`` / ``getattr(__builtins__, ...)`` is not seen."""
    pdir = package_dir or os.path.dirname(os.path.abspath(__file__))
    seen: Set[str] = set()
    todo = [module]
    while todo:
        m = todo.pop()
        path = os.path.join(pdir, m + ".py")
        if m == DYNAMIC_IMPORT:
            seen.add(m)
            continue
        if m in seen or not os.path.isfile(path):
            continue
        seen.add(m)
        with open(path, "r", encoding="utf-8") as fh:
            todo.extend(_workspace_imports(fh.read(), m))
    seen.discard(module)
    return seen


def forbidden_reach(module: str, package_dir: Optional[str] = None) -> Set[str]:
    """Modules reachable from ``module`` that are NOT on the allow-list (allow-list, F7: unknown modules are forbidden)."""
    return import_closure(module, package_dir) - SAFE_WORKSPACE_MODULES


# --- the registry ----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class SafetyPath:
    name: str
    #: 'traced' (code + a runner) | 'unbuilt' (no code yet; ``owner`` names the slice that builds it)
    status: str
    owner: str
    note: str
    runner: Optional[Callable[[Mapping[str, Any]], None]] = None


def _run_relaunch(world: Mapping[str, Any]) -> None:
    """The relaunch path as gnomon-relaunch runs it, INCLUDING its glue (F6): the bundle rail (``evaluate``), the new run's bundle
    (``copy_forward``), and ``relaunch.main`` end to end (``_scope_core_on``, ``run_worker`` and ``_copy_bundle_forward`` on a
    session with a bundle; ``_alert_bundle_refusal`` on one without). ``world`` carries ``session_dir`` (a session dir with a
    bundle), ``bad_session_dir`` (one without), ``sysm`` (a fake System whose process table holds ``session_dir``'s harness),
    ``config_path`` (a machine config file with the flag ON and ``deploy_root``). Raises if the glue did not behave, so a runner
    that silently did nothing cannot pass as 'clean'."""
    import contextlib
    import io

    from .. import config as cfgmod
    from .. import flags, relaunch
    from . import bundle

    opts = relaunch.Options(scope_core=True)
    relaunch.evaluate(world["sysm"], world["session_dir"], opts, idle_can_wait=False, compact_pending_ok=False)
    bundle.read_relaunch_inputs(world["session_dir"])
    bundle.copy_forward(world["session_dir"], enabled=True)
    old_path, old_store = cfgmod.DEFAULT_CONFIG_PATH, flags._config_store
    cfgmod.DEFAULT_CONFIG_PATH = world["config_path"]
    flags._config_store = lambda: cfgmod.ConfigStore(world["config_path"])
    try:
        if not relaunch._scope_core_on():
            raise RuntimeError("the relaunch runner's machine config did not turn the flag ON")
        inline = lambda fn, _lf: (fn(), 12345)[1]          # noqa: E731 - run the detached worker in-process
        with contextlib.redirect_stdout(io.StringIO()):
            rc = relaunch.main([world["session_dir"], "--grace", "0"], sysm=world["sysm"], detacher=inline)
            rc_bad = relaunch.main([world["bad_session_dir"], "--grace", "0"], sysm=world["sysm"], detacher=inline)
        if rc != relaunch.EXIT_OK or rc_bad != relaunch.EXIT_REFUSED:
            raise RuntimeError(f"relaunch.main did not run the glue as expected (rc={rc}, rc_bad={rc_bad})")
    finally:
        cfgmod.DEFAULT_CONFIG_PATH, flags._config_store = old_path, old_store


def _run_alerts(world: Mapping[str, Any]) -> None:
    from . import events

    events.append_event(world["event_log"], "alert", severity="warn", message="probe", enabled=True)
    list(events.read_events(world["event_log"]))


def _run_settings(world: Mapping[str, Any]) -> None:
    """A safety path resolving settings: machine layer live + the bundle's snapshot; an unregistered key takes the
    restrictive branch with an alert."""
    from . import bundle, events

    inp = bundle.read_relaunch_inputs(world["session_dir"], alert=lambda e: events.append_event(
        world["event_log"], e["kind"], severity=e.get("severity", "info"), message=e.get("message", ""), enabled=True))
    inp.resolver.get("agent_launch_enabled", safety=True)
    inp.resolver.get("a_key_nobody_registered", safety=True)


SAFETY_PATHS: Dict[str, SafetyPath] = {
    "alerts": SafetyPath("alerts", "traced", "X13-S2", "the machine event log append/read (workspace.events)", _run_alerts),
    "relaunch": SafetyPath("relaunch", "traced", "X13-S2", "gnomon-relaunch's bundle rail + the new run's bundle (workspace.bundle)", _run_relaunch),
    "settings-on-a-safety-path": SafetyPath("settings-on-a-safety-path", "traced", "X13-S2", "the settings resolver with the bundle snapshot (restrictive-on-skip)", _run_settings),
    "BREAK-GLASS": SafetyPath("BREAK-GLASS", "unbuilt", "GM-RM-1C", "the recovery launch verb is unbuilt (flag break_glass_launch); BREAK-GLASS.md is a document"),
    "wind-down": SafetyPath("wind-down", "unbuilt", "GM-RM-X19", "the 98% wind-down is a skill today, with no code in the package"),
    "usage-limit-trigger": SafetyPath("usage-limit-trigger", "unbuilt", "GM-RM-X19", "threshold -> alert -> notice to the machine-duty role: unbuilt"),
    "bootstrap-jobs": SafetyPath("bootstrap-jobs", "unbuilt", "GM-RM-X23", "UPS detector / watchdog / launcher plist: unbuilt (the Scheduler)"),
    "attention-state": SafetyPath("attention-state", "unbuilt", "X13-S4", "the MACHINE attention read model is built with the views slice"),
}

#: the names the I6 invariant lists (alerts, BREAK-GLASS, wind-down, usage-limit trigger, relaunch, bootstrap jobs,
#: attention state); a test cross-checks this against the spec text so a spec change forces a review here
I6_NAMED = ("alerts", "BREAK-GLASS", "wind-down", "usage-limit", "relaunch", "bootstrap jobs", "attention state")

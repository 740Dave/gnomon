"""the STATIC CALL-SITE SCAN (spec requirement-known-categories,
).

Finds the feature ids at ``feature_event(`` / ``emit_hook*(`` call sites by AST, resolved through the
closed-set functions in :mod:`gnomon.feature_sites` (and the hub's in
:mod:`gnomon.hub.feature_ids`), plus every call whose first argument is NOT a literal, a module
constant or a closed-set call (``dynamic``: a violation). Used by the report ('built' = a call site exists),
``usage_metrics.known_categories()['feature']`` and the coverage tests; never by the harness runtime (it
reaches into the hub package).

Neutral (g3).
"""

from __future__ import annotations

import ast
import os
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from . import feature_events as _fe
from . import feature_sites as fs
from .feature_events import HOOK_POINTS


def _hub_ids() -> Set[str]:
    from .hub import feature_ids

    return {v.replace("_", "-") for v in feature_ids.hub_tool_names()}


#: a call whose first argument is one of these is resolved through the closed set it names
CLOSED_SETS: Dict[str, Callable[[], Set[str]]] = {
    "command_feature_id": lambda: set(fs.command_alias_map().values()),
    "dispatch_feature_id": lambda: {rn.feature_id for rn in fs._registry()
                                    if {"subagent", "skill"} & set(rn.surfaces)},
    "self_verb_feature_id": lambda: {fs._kebab(v) for v in fs.self_verb_names()},
    "hub_tool_feature_id": _hub_ids,
    "route_feature_id": lambda: set(fs.all_web_routes().values()),
    "hook_point_feature_id": lambda: set(HOOK_POINTS.values()),
}
#: the emitting calls the scan looks for
EMIT_CALLS = ("feature_event", "emit_hook", "emit_hook_noop", "emit_hook_dispatch")
INTERNAL_HOOK_IDS = lambda: {fid for name, fid in HOOK_POINTS.items() if not name.startswith("claude:")}  # noqa: E731


def _call_name(func: ast.AST) -> Optional[str]:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_str_consts(tree: ast.AST) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for node in getattr(tree, "body", []):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            val = node.value
            if isinstance(val, ast.Constant) and isinstance(val.value, str):
                for t in targets:
                    if isinstance(t, ast.Name):
                        out[t.id] = val.value
    return out


def scan_tree(tree: ast.AST, path: str = "") -> Tuple[Set[str], List[Dict[str, Any]]]:
    """(ids, dynamic) for one parsed module. ``dynamic`` lists emitting calls whose first argument is
    not a literal, a module constant or a closed-set call."""
    consts = _module_str_consts(tree)
    ids: Set[str] = set()
    dynamic: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        cname = _call_name(node.func)
        if cname not in EMIT_CALLS or not node.args:
            continue
        a0 = node.args[0]
        lit: Optional[str] = None
        if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
            lit = a0.value
        elif isinstance(a0, ast.Name) and a0.id in consts:
            lit = consts[a0.id]
        if lit is not None:
            fid = lit if cname == "feature_event" else HOOK_POINTS.get(lit)
            if fid is None:
                dynamic.append({"path": path, "line": node.lineno, "call": cname, "reason": f"unlisted name {lit!r}"})
            else:
                ids.add(fid)
            continue
        if isinstance(a0, ast.Call) and _call_name(a0.func) in CLOSED_SETS:
            try:
                ids |= CLOSED_SETS[_call_name(a0.func)]()  # type: ignore[index]
            except Exception as exc:  # noqa: BLE001 - an unresolvable closed set is reported, not hidden
                dynamic.append({"path": path, "line": node.lineno, "call": cname,
                                "reason": f"closed set {_call_name(a0.func)} unresolvable: {type(exc).__name__}: {exc}"})
            continue
        if cname == "emit_hook_dispatch" and isinstance(a0, (ast.Name, ast.Attribute)):
            ids |= INTERNAL_HOOK_IDS()   # the internal-registry dispatcher: its closed set is the internal HOOK_POINTS
            continue
        dynamic.append({"path": path, "line": node.lineno, "call": cname, "reason": "first argument is not a "
                        "literal, a module constant or a closed-set call"})
    return ids, dynamic


def scan_sources(sources: Iterable[Tuple[str, ast.AST]]) -> Tuple[Set[str], List[Dict[str, Any]]]:
    ids: Set[str] = set()
    dynamic: List[Dict[str, Any]] = []
    own = os.path.abspath(_fe.__file__)
    for path, tree in sources:
        if os.path.abspath(path) in (own, own[:-1] if own.endswith(".pyc") else own):
            continue   # the emitter's own machinery is not a call site
        i, d = scan_tree(tree, path)
        ids |= i
        dynamic += d
    return ids, dynamic


def _web_backend_sources() -> Iterable[Tuple[str, ast.AST]]:
    """The web backend (``<project>/web/backend/*.py``) sits OUTSIDE the package but is the ``web`` process's
    call site (the route middleware): scanned too when the project tree is present (a pure install has none)."""
    d = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web", "backend")
    if not os.path.isdir(d):
        return
    for fn in sorted(os.listdir(d)):
        if fn.endswith(".py"):
            p = os.path.join(d, fn)
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    yield p, ast.parse(fh.read(), filename=p)
            except (OSError, SyntaxError, ValueError):
                continue


def scan_call_sites(package_sources: Optional[Iterable[Tuple[str, ast.AST]]] = None) -> Tuple[Set[str], List[Dict[str, Any]]]:
    """The AST scan over the whole package plus the web backend (ids found at call sites, dynamic
    violations). ``package_sources`` = already-parsed package modules (shared with the X7 scan); None parses."""
    from . import usage_metrics as um

    def _all() -> Iterable[Tuple[str, ast.AST]]:
        yield from (package_sources if package_sources is not None else um._iter_sources())
        yield from _web_backend_sources()

    return scan_sources(_all())

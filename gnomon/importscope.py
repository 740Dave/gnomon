"""Which source files a process actually loads — the shared import-scope helper.

Factored out of ``tools/golive_smoke.py`` (audit-SMOKESCOPE 2026-09-30) so that
``gnomon.buildstamp`` derives the SESSION build scope the SAME way the smoke
check derives its harness-code scope (build BSTAMP 2026-09-30), instead of hashing
every ``.py`` in the package (which made a web-server-only ``dsd/*`` edit mark every
session stale).

Pure stdlib. Two derivations:

* :func:`import_closure` — static ``ast`` walk; nothing is imported or executed.
* :func:`runtime_import_set` / :func:`harness_scope` — import the harness entry
  module in a CHILD ``python -B`` and read ``sys.modules`` (module import only;
  ``main()`` is never called; no bytecode written).
"""

from __future__ import annotations

import ast
import glob
import json
import os
import subprocess
import sys
from typing import Callable, Iterable, List, Optional, Set, Tuple

PKG = "gnomon"


def _module_file(src_root: str, modname: str) -> Optional[str]:
    base = os.path.join(src_root, *modname.split("."))
    for cand in (base + ".py", os.path.join(base, "__init__.py")):
        if os.path.isfile(cand):
            return cand
    return None


def _modname_of(src_root: str, path: str) -> Optional[str]:
    rel = os.path.relpath(path, src_root)
    if rel.startswith(".."):
        return None
    parts = rel[:-3].split(os.sep)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _import_nodes(tree: ast.AST, nested: bool) -> List[ast.AST]:
    """``Import``/``ImportFrom`` nodes of ``tree``. ``nested=False`` keeps only the
    ones that run at MODULE IMPORT (top level, class bodies, top-level if/try) and
    drops those inside a function/lambda body, which run only when called."""
    if nested:
        return [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    out: List[ast.AST] = []
    todo: List[ast.AST] = [tree]
    while todo:
        node = todo.pop()
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                out.append(child)
            todo.append(child)
    return out


def import_closure(src_root: str, seeds: Iterable[str], package: str = PKG,
                   nested: Optional[Callable[[str], bool]] = None,
                   ancestor_leaves: bool = False) -> Set[str]:
    """Every in-tree ``package`` source file reachable from ``seeds`` by import
    (absolute, relative, and function-level imports; package ``__init__`` files
    included). Pure ``ast`` — nothing is imported or executed.

    ``nested(path)`` decides, per file, whether imports inside function bodies are
    followed (default: always — the backend closure, unchanged).

    ``ancestor_leaves`` (build BSEED 2026-09-30; default False = unchanged): a
    package ``__init__`` reached ONLY as the parent of an imported submodule
    (``from ..dsd.notice import x`` runs ``dsd/__init__``) is INCLUDED as a file but
    its own imports are not followed — its eager re-exports of siblings
    (``dsd.writer``/``dsd.persist``) are not code the importer uses. A package that
    is itself the import target (``from ..hub import procinfo`` where ``procinfo``
    is not a submodule, ``import gnomon.dsd``) is followed as usual."""
    seen: Set[str] = set()       # files in the closure
    followed: Set[str] = set()   # files whose imports were walked
    stack: List[Tuple[str, bool]] = [(os.path.abspath(s), True) for s in seeds if os.path.isfile(s)]
    while stack:
        path, follow = stack.pop()
        seen.add(path)
        if not follow or path in followed:
            continue
        followed.add(path)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
        except (OSError, SyntaxError, ValueError):
            continue
        me = _modname_of(src_root, path) or ""
        is_pkg = path.endswith("__init__.py")
        my_pkg = me if is_pkg else me.rpartition(".")[0]
        targets: List[str] = []
        for node in _import_nodes(tree, True if nested is None else nested(path)):
            if isinstance(node, ast.Import):
                targets += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    parts = my_pkg.split(".") if my_pkg else []
                    if node.level - 1 > len(parts):
                        continue
                    base = ".".join(parts[:len(parts) - (node.level - 1)])
                    mod = ".".join(p for p in (base, node.module or "") if p)
                else:
                    mod = node.module or ""
                if not mod:
                    continue
                subs = [f"{mod}.{a.name}" for a in node.names]
                if not ancestor_leaves:
                    targets += [mod] + subs
                    continue
                real_subs = [m for m in subs if _module_file(src_root, m)]
                targets += real_subs
                if len(real_subs) < len(subs):      # a name that is an attribute of mod itself
                    targets.append(mod)
        for t in targets:
            if not (t == package or t.startswith(package + ".")):
                continue
            parts = t.split(".")
            for k in range(1, len(parts) + 1):   # the module + every parent package
                f = _module_file(src_root, ".".join(parts[:k]))
                if not f:
                    continue
                f = os.path.abspath(f)
                follow_f = k == len(parts) or not ancestor_leaves
                if f not in followed and (follow_f or f not in seen):
                    stack.append((f, follow_f))
    return {p for p in seen if _modname_of(src_root, p) is not None}


#: what ``python -m gnomon.harness <dir>`` runs: ``__main__`` -> ``launcher.main``.
HARNESS_ENTRY = f"{PKG}.harness.launcher"
HARNESS_MAIN = f"{PKG}.harness.__main__"

#: Run in a CHILD interpreter (``-B``: no ``__pycache__`` written): import the
#: harness entry module and report which in-package modules that actually loaded.
_IMPORT_PROBE = (
    "import importlib, json, sys\n"
    "root, entry, pkg = sys.argv[1:4]\n"
    "sys.path.insert(0, root)\n"
    "importlib.import_module(entry)\n"
    "print(json.dumps(sorted(f for f in (getattr(m, '__file__', None) for n, m in list(sys.modules.items())\n"
    "                                    if n == pkg or n.startswith(pkg + '.')) if f)))\n"
)


def runtime_import_set(src_root: str, entry: str = HARNESS_ENTRY,
                       package: str = PKG) -> Tuple[Optional[Set[str]], str]:
    """(in-tree source files ``sys.modules`` holds after ``import entry``, error).
    Derived by IMPORTING, not from a hand-written list, so it cannot drift. The
    import runs in a child ``python -B`` (no bytecode written; nothing executed
    beyond module import — the harness's ``main()`` is never called)."""
    root = os.path.abspath(src_root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.pop("PYTHONPATH", None)
    try:
        cp = subprocess.run([sys.executable, "-B", "-c", _IMPORT_PROBE, root, entry, package],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            timeout=60, cwd=root, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"{type(exc).__name__}: {exc}"
    if cp.returncode != 0:
        tail = (cp.stderr.strip().splitlines() or ["?"])[-1]
        return None, f"import {entry} exited {cp.returncode}: {tail[:160]}"
    try:
        files = json.loads(cp.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None, f"import probe printed no module list ({cp.stdout[:80]!r})"
    out = {os.path.abspath(f) for f in files if isinstance(f, str) and f.endswith(".py")}
    return {f for f in out if _modname_of(root, f) is not None and os.path.isfile(f)}, ""


def lazy_import_targets(src_root: str, path: str, package: str = PKG) -> Set[str]:
    """The in-tree module files named by the FUNCTION-LEVEL (lazy) imports of ``path``
    — imports inside a function/lambda body, which load at first call. Parent
    packages are not returned (see ``import_closure(ancestor_leaves=True)``)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
    except (OSError, SyntaxError, ValueError):
        return set()
    eager_ids = {id(n) for n in _import_nodes(tree, False)}
    lazy_nodes = [n for n in _import_nodes(tree, True) if id(n) not in eager_ids]
    if not lazy_nodes:
        return set()
    me = _modname_of(src_root, path) or ""
    my_pkg = me if path.endswith("__init__.py") else me.rpartition(".")[0]
    names: List[str] = []
    for node in lazy_nodes:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
            continue
        if node.level:
            parts = my_pkg.split(".") if my_pkg else []
            if node.level - 1 > len(parts):
                continue
            base = ".".join(parts[:len(parts) - (node.level - 1)])
            mod = ".".join(p for p in (base, node.module or "") if p)
        else:
            mod = node.module or ""
        if not mod:
            continue
        subs = [f"{mod}.{a.name}" for a in node.names if _module_file(src_root, f"{mod}.{a.name}")]
        names += subs
        if len(subs) < len(node.names):
            names.append(mod)
    out: Set[str] = set()
    for n in names:
        if n == package or n.startswith(package + "."):
            f = _module_file(src_root, n)
            if f:
                out.add(os.path.abspath(f))
    return out


#: build BSEED (2026-09-30): harness-package modules that run as a SEPARATE process
#: living as long as the session (not per call), so the code they loaded at start
#: can go stale exactly like the launcher's. Seeded explicitly: nothing in the
#: launcher's import graph names them (they are ``python -m`` entry points).
#: * ``self_mcp`` — the session's stdio MCP server (``launcher.write_self_mcp_config``:
#:   ``python -m gnomon.harness.self_mcp --socket …``), spawned once by claude
#:   and alive for the whole session.
#: * ``headless`` — the ``claude -p`` wrapper (``agent_launch.HEADLESS_MODULE``); it
#:   is the harness process of a headless session and lives for that run.
#: NOT seeded (per-invocation ``python -m`` hook processes, always fresh from disk):
#: ``deny_guard``, ``lifecycle_hook``, ``posttool_hook``, ``statusline_hook``,
#: ``prompt_command_hook`` — see audits/build-BSEED-2026-09-30.md.
SESSION_PROCESS_ENTRIES = (f"{PKG}.harness.self_mcp", f"{PKG}.harness.headless")


def harness_scope(src_root: str, *, strict: bool = False) -> Tuple[Set[str], str]:
    """(the source files the HARNESS PROCESS can run during a session, how it was
    derived).

    = what ``sys.modules`` holds after importing the harness entry module (+ its
    ``__main__`` and the :data:`SESSION_PROCESS_ENTRIES`), closed over EVERY import
    those modules can execute — module-level AND function-level/lazy (build BSEED
    2026-09-30, audit-BSTAMP N1: ``notice_inbox.offer`` -> ``dsd.notice`` loads at
    first use inside a long-lived harness, so an edit to it after launch is exactly
    the staleness this scope exists to see). A package ``__init__`` reached only as
    the parent of a lazily imported submodule is included as a file but not
    followed (``ancestor_leaves``), so ``dsd.writer``/``dsd.persist`` (re-exported
    by ``dsd/__init__``, never used by the harness) stay out and a web-server-only
    DSD edit still does not flag every session.

    If the import probe fails, falls back to the old static closure of every
    ``harness/*.py`` (over-inclusive: it can false-alarm, never miss) and says so —
    unless ``strict``, when it raises ``RuntimeError`` instead (a build ID must be
    derived one way only, or two stampers could disagree and read as "stale")."""
    root = os.path.abspath(src_root)
    eager, err = runtime_import_set(root)
    if eager is None and strict:
        raise RuntimeError(f"runtime import of {HARNESS_ENTRY} failed: {err}")
    if eager is None:
        seeds = glob.glob(os.path.join(root, PKG, "harness", "*.py"))
        return (import_closure(root, seeds),
                f"runtime import of {HARNESS_ENTRY} FAILED ({err}); fell back to the static "
                f"closure of every harness module (over-inclusive)")
    extra = {HARNESS_ENTRY, HARNESS_MAIN, *SESSION_PROCESS_ENTRIES}
    startup = {os.path.abspath(f) for f in (_module_file(root, m) for m in extra) if f}
    harness_dir = os.path.join(root, PKG, "harness") + os.sep
    # Function-level imports are followed in harness-package modules and, transitively,
    # in every module reached THROUGH such an import (the code the harness calls into:
    # memory.remember -> app.graph_viewer). A module loaded only as a side effect of a
    # package __init__ (hub/__init__ -> hub.server, hub.mcp) has its module-level
    # imports followed but not its function bodies: those run in the hub process.
    lazy: Set[str] = set()
    while True:
        files = import_closure(root, eager | startup | lazy, ancestor_leaves=True,
                               nested=lambda p: p.startswith(harness_dir) or p in lazy)
        new = set()
        for f in files:
            if f.startswith(harness_dir) or f in lazy:
                new |= lazy_import_targets(root, f) - lazy
        if not new:
            break
        lazy |= new
    return files, (f"{len(files)} module(s) derived from import {HARNESS_ENTRY} "
                   f"({len(eager)} loaded at import, + session-process entries, + "
                   f"{len(lazy)} function-level import target(s) and their closures)")

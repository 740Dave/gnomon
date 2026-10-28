"""Static scanner for every process-starting reference in the ``gnomon/appshost`` package (R3H-F1, round 3i).

Used by tests/test_appshost_round3i.py (and the round-3h structural test). It is a test instrument, not product code.

WHAT IT GUARANTEES (g73-amendment-11): over every ``.py`` file under ``appshost/`` found at run time (``os.walk``, so new
files AND subpackages are scanned; there is no file list), a process-starting call is accepted only when
  (a) it is handed ``<judged>.exec_target()`` (receiver spelled ``judged`` or ``<x>.judged``, directly or through a local
      assigned from it) AND a ``.verify(...)`` call precedes it in the same function; or
  (b) it sits in a (file, function) pinned in FIXED_ARGV_SITES, up to the pinned CALL COUNT (a second call in a pinned
      function is a violation, R3H-F4).
It also rejects, whatever the call: aliasing a watched module (``import subprocess as sp``, ``sp = subprocess``,
``import os as _o``), from-imports of exec functions, ``getattr``/``vars``/``__dict__`` on a watched module, any bare
reference to a watched module or exec function (it is not called, so nothing can judge it), asyncio subprocess functions,
``pty``, ``multiprocessing``, ``ctypes``/``cffi``/native extension files, ``eval``/``exec``/``__import__``/``compile``,
``importlib``, ``sys.modules`` and module ``__dict__``/``__globals__`` access.

WHAT IT CANNOT SEE (stated limits; the spec sentence says exactly this): a process started by code OUTSIDE ``appshost/``
that appshost calls; an injected callable (a ``Deps`` field, ``exec_fn``) that a caller replaces; ``getattr(obj, name)``
on an object that is not statically a watched module (``host.py`` uses a dynamic ``getattr(self.paths, name)``); a
stdlib or third-party module that spawns internally and is not on the banned-import list; a hostile caller already inside
the host process. An allowed (judged) site is a guarantee about WHICH PATH is exec'd, never about what that program does.
"""

from __future__ import annotations

import ast
import os
from typing import Dict, List, Tuple

APPSHOST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "appshost")

#: (relative file, function scope) -> (pinned number of exec calls, why). A new entry is a conscious edit with a reason.
FIXED_ARGV_SITES: Dict[Tuple[str, str], Tuple[int, str]] = {
    ("launchd.py", "RealLaunchd._run"): (1, "launchctl <verb> <label> (host constants)"),
    ("ports.py", "ListenerProbe._run"): (1, "lsof -nP -F pcn (host constants)"),
    ("ports.py", "process_children"): (1, "/bin/ps -axo pid=,ppid= (X72: the one snapshot; process_tree and the poller's Scan reuse it)"),
    # R3I (found by the scanner, missed by the R3H guard and the R3H exhaustive grep): the browser launcher. The URL is
    # host-built (http, loopback, the validated port and ui_path); no app-supplied program or argv reaches it.
    ("host.py", "_default_opener"): (1, "webbrowser.open_new(<host-built loopback URL>)"),
}

_SUBPROCESS = {"Popen", "run", "call", "check_call", "check_output", "getoutput", "getstatusoutput"}
_OS_SIMPLE = {"system", "popen", "forkpty", "startfile"}
#: attribute names that start a process whatever the receiver is (an object that holds a module under another name)
_GENERIC_ATTRS = {"Popen", "posix_spawn", "posix_spawnp", "subprocess_exec", "subprocess_shell",
                  "create_subprocess_exec", "create_subprocess_shell", "fork_exec", "_execute_child", "forkpty",
                  "ProcessPoolExecutor"}
_BARE_NAMES = {"exec_fn", "Popen", "posix_spawn", "posix_spawnp", "ProcessPoolExecutor"}
#: a Name that is a watched module must be used only as the receiver of an attribute, never passed around or aliased
_WATCHED_MODULES = {"subprocess", "os", "pty", "asyncio", "sys", "webbrowser"}
_BANNED_IMPORTS = {"pty", "ctypes", "_ctypes", "cffi", "_cffi_backend", "multiprocessing", "posix", "_posixsubprocess",
                   "pexpect", "ptyprocess", "sh", "plumbum", "distutils", "importlib", "imp", "builtins",
                   "runpy", "code", "codeop", "commands", "popen2", "concurrent"}
_BANNED_NAMES = {"eval", "exec", "__import__", "compile", "globals", "locals", "vars", "__builtins__"}
_BANNED_ATTRS = {"__dict__", "__globals__", "__builtins__", "__loader__", "__getattribute__", "__subclasses__"}
_NATIVE_EXT = (".so", ".dylib", ".pyd", ".dll")

Site = Tuple[str, str, str, bool, str]       # (file, scope, kind, ok, why)


def _is_exec_dotted(head: str, rest: str) -> bool:
    first = rest.split(".", 1)[0] if rest else ""
    if head == "subprocess":
        return first in _SUBPROCESS
    if head == "os":
        return first.startswith(("exec", "spawn", "posix_spawn")) or first in _OS_SIMPLE
    if head == "pty":
        return True
    if head == "webbrowser":
        return first.startswith("open") or first in ("get", "register")
    if head == "asyncio":
        return first in ("create_subprocess_exec", "create_subprocess_shell") or first == "subprocess"
    return False


def _style(name: str) -> str:
    """How the executable is handed: 'kw' (executable=), 'pos' (first positional), 'none' (cannot be judged)."""
    if name in ("exec_fn", "posix_spawn", "posix_spawnp") or name.startswith("exec"):
        return "pos"
    if name in _SUBPROCESS - {"getoutput", "getstatusoutput"} or name in ("Popen", "create_subprocess_exec",
                                                                          "subprocess_exec", "_execute_child"):
        return "kw"
    return "none"


def _judged_receiver(expr) -> bool:
    return (isinstance(expr, ast.Name) and expr.id == "judged") or (isinstance(expr, ast.Attribute) and expr.attr == "judged")


def _is_exec_target_call(expr, assigned) -> bool:
    if isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute) and expr.func.attr == "exec_target" \
            and not expr.args and not expr.keywords and _judged_receiver(expr.func.value):
        return True
    return isinstance(expr, ast.Name) and expr.id in assigned


def scan_source(source: str, fname: str) -> List[Site]:
    tree = ast.parse(source)
    for n in ast.walk(tree):
        for c in ast.iter_child_nodes(n):
            c._p = n                                                    # parent link

    def scope_of(n) -> str:
        names, p = [], getattr(n, "_p", None)
        while p is not None:
            if isinstance(p, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(p.name)
            p = getattr(p, "_p", None)
        return ".".join(reversed(names)) or "<module>"

    def func_of(n):
        p = getattr(n, "_p", None)
        while p is not None and not isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef)):
            p = getattr(p, "_p", None)
        return p

    out: List[Site] = []
    bind: Dict[str, str] = {}                                           # local name -> dotted module path (imports only)

    def bad(n, kind, why):
        out.append((fname, scope_of(n), kind, False, why))

    # ---- imports: aliases, banned modules, exec from-imports
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                top = al.name.split(".")[0]
                if top in _BANNED_IMPORTS or al.name.startswith("asyncio.subprocess"):
                    bad(n, f"import {al.name}", "a process-starting / foreign-call / dynamic-import module is banned in appshost")
                    continue
                if al.asname and al.name in _WATCHED_MODULES and al.asname != al.name:
                    bad(n, f"import {al.name} as {al.asname}", "an aliased import of a watched module")
                bind[al.asname or top] = al.name if al.asname else top
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            top = mod.split(".")[0]
            if n.level == 0 and (top in _BANNED_IMPORTS or mod.startswith("asyncio.subprocess")):
                bad(n, f"from {mod} import ...", "a process-starting / foreign-call / dynamic-import module is banned in appshost")
                continue
            for al in n.names:
                if al.name == "*" and mod in _WATCHED_MODULES:
                    bad(n, f"from {mod} import *", "a star import of a watched module")
                elif n.level == 0 and _is_exec_dotted(top, al.name) or (n.level == 0 and al.name in _GENERIC_ATTRS):
                    bad(n, f"from {mod} import {al.name}", "an exec primitive imported by name (it can be aliased and called unjudged)")
                elif n.level == 0 and mod in _WATCHED_MODULES:
                    bind[al.asname or al.name] = f"{mod}.{al.name}"

    def resolve(expr):
        if isinstance(expr, ast.Name):
            return bind.get(expr.id, expr.id)
        if isinstance(expr, ast.Attribute):
            base = resolve(expr.value)
            return f"{base}.{expr.attr}" if base else None
        return None

    def exec_info(func):
        """(is_exec, simple name) for the callee/reference expression."""
        if isinstance(func, ast.Name):
            full = bind.get(func.id, func.id)
            if func.id in _BARE_NAMES:
                return True, func.id
            head, _, rest = full.partition(".")
            return (bool(rest) and _is_exec_dotted(head, rest)), (rest.split(".")[-1] if rest else func.id)
        if isinstance(func, ast.Attribute):
            full = resolve(func)
            head, _, rest = (full or "").partition(".")
            if full and _is_exec_dotted(head, rest):
                return True, func.attr
            if func.attr in _GENERIC_ATTRS:
                return True, func.attr
        return False, ""

    # ---- locals assigned from judged.exec_target(), per function
    assigned_by_func: Dict[int, set] = {}
    for fn in [t for t in ast.walk(tree) if isinstance(t, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        asg = set()
        for sub in ast.walk(fn):
            if isinstance(sub, ast.Assign) and _is_exec_target_call(sub.value, set()):
                asg |= {t.id for t in sub.targets if isinstance(t, ast.Name)}
        assigned_by_func[id(fn)] = asg

    def verify_before(call) -> bool:
        fn = func_of(call)
        if fn is None:
            return False
        return any(isinstance(s, ast.Call) and isinstance(s.func, ast.Attribute) and s.func.attr == "verify"
                   and s.lineno <= call.lineno for s in ast.walk(fn))

    unjudged: List[Tuple[int, str, str, str, str]] = []                 # (lineno, scope, kind, style-why, ...) pending pin check

    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            is_exec, name = exec_info(n.func)
            if is_exec:
                kind = ast.unparse(n.func)
                st = _style(name)
                fn = func_of(n)
                asg = assigned_by_func.get(id(fn), set()) if fn is not None else set()
                target = None
                if st == "kw":
                    target = {k.arg: k.value for k in n.keywords}.get("executable")
                elif st == "pos":
                    target = n.args[0] if n.args else None
                if target is not None and _is_exec_target_call(target, asg) and verify_before(n):
                    out.append((fname, scope_of(n), kind, True, ""))
                else:
                    why = ("not handed <judged>.exec_target() after a verify()" if st != "none"
                           else "this primitive takes no executable= and cannot be judged")
                    unjudged.append((n.lineno, scope_of(n), kind, why, ""))
            elif isinstance(n.func, ast.Name) and n.func.id in ("getattr", "setattr", "delattr", "hasattr") and n.args:
                recv = n.args[0]
                full = resolve(recv)
                if full and full.split(".")[0] in _WATCHED_MODULES:
                    bad(n, f"{n.func.id}({ast.unparse(recv)}, ...)", f"{n.func.id} on a watched module")
        elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            par = getattr(n, "_p", None)
            if n.id in _BANNED_NAMES:
                bad(n, n.id, f"{n.id} is banned in appshost (dynamic code / namespace access)")
            elif bind.get(n.id, n.id) in _WATCHED_MODULES and not (isinstance(par, ast.Attribute) and par.value is n):
                if not (isinstance(par, ast.Call) and par.func is n):
                    bad(n, n.id, f"a bare reference to the {bind.get(n.id, n.id)} module (aliased, passed or stored)")
        elif isinstance(n, ast.Attribute):
            par = getattr(n, "_p", None)
            if n.attr in _BANNED_ATTRS:
                bad(n, ast.unparse(n), f".{n.attr} is banned in appshost (namespace access)")
                continue
            if isinstance(par, ast.Call) and par.func is n:
                continue                                                # a call: handled above
            full = resolve(n)
            head, _, rest = (full or "").partition(".")
            if full == "sys.modules":
                bad(n, "sys.modules", "sys.modules is banned in appshost")
            elif (full and _is_exec_dotted(head, rest)) or n.attr in _GENERIC_ATTRS:
                # a parameter default named exec_fn is the seam; the call through exec_fn is judged at its own site
                is_default = False
                if isinstance(par, ast.arguments):
                    pos = par.posonlyargs + par.args
                    pairs = list(zip(pos[len(pos) - len(par.defaults):], par.defaults)) + \
                        [(a, d) for a, d in zip(par.kwonlyargs, par.kw_defaults) if d is not None]
                    is_default = any(d is n and a.arg == "exec_fn" for a, d in pairs)
                if not is_default:
                    bad(n, ast.unparse(n), "a bare reference to an exec primitive (it can be aliased and called unjudged)")

    # ---- pinned fixed-argv scopes: up to the pinned count; the rest are violations
    seen: Dict[Tuple[str, str], int] = {}
    for lineno, sc, kind, why, _x in sorted(unjudged):
        key = (fname, sc)
        if key in FIXED_ARGV_SITES:
            seen[key] = seen.get(key, 0) + 1
            if seen[key] <= FIXED_ARGV_SITES[key][0]:
                out.append((fname, sc, kind, True, ""))
                continue
            out.append((fname, sc, kind, False, f"more exec calls than the {FIXED_ARGV_SITES[key][0]} pinned for this function"))
        else:
            out.append((fname, sc, kind, False, why))
    return out


def package_files(root: str = APPSHOST):
    """Every file under the package, recursive: (relative path, absolute path)."""
    found = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d != "__pycache__")
        for f in sorted(fns):
            found.append((os.path.relpath(os.path.join(dp, f), root), os.path.join(dp, f)))
    return found


def scan_package(root: str = APPSHOST) -> List[Site]:
    sites: List[Site] = []
    for rel, ab in package_files(root):
        if rel.endswith(_NATIVE_EXT):
            sites.append((rel, "<file>", rel, False, "a native extension / shared library inside appshost"))
        elif rel.endswith(".py"):
            sites += scan_source(open(ab).read(), rel)
    return sites

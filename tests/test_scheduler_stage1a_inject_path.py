"""Scheduler stage 1a: ONE inject path (ac-s1-one-inject-path) and the label is not authority
(ac-s1-label-not-authority). Static scans with positive controls: each scan is shown to FAIL on a planted violation."""

from __future__ import annotations

import ast
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "gnomon", "scheduler")
FORBIDDEN_IMPORTS = {"pty", "termios", "socket", "subprocess", "ctypes", "tty", "fcntl_ioctl"}
FORBIDDEN_NAMES = {"route", "inject_line", "Injector", "Actuator", "SingleWriteSite", "SocketDownlink", "InjectionGate",
                   "admit", "Downlink", "send_frame", "writer"}
FORBIDDEN_CALLS = {("os", "system"), ("os", "execv"), ("os", "execvp"), ("os", "fork"), ("os", "posix_spawn"),
                   ("os", "openpty"), ("os", "forkpty")}


def violations(src: str, *, allow_route: bool) -> list:
    tree = ast.parse(src)
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            out += [f"import {a.name}" for a in n.names if a.name.split(".")[0] in FORBIDDEN_IMPORTS]
        elif isinstance(n, ast.ImportFrom) and (n.module or "").split(".")[0] in FORBIDDEN_IMPORTS:
            out.append(f"from {n.module}")
        elif isinstance(n, ast.Attribute) and n.attr in FORBIDDEN_NAMES and not (allow_route and n.attr == "route"):
            out.append(f".{n.attr}")
        elif isinstance(n, ast.Name) and n.id in FORBIDDEN_NAMES - {"route", "writer"}:
            out.append(n.id)
        elif (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
              and (n.func.value.id, n.func.attr) in FORBIDDEN_CALLS):
            out.append(f"{n.func.value.id}.{n.func.attr}")
        elif (isinstance(n, ast.ImportFrom) and allow_route and n.module == "harness.compact_gate"
              and [a.name for a in n.names] == ["screen_hub_inject"]):
            continue                      # inject.py may import the harness's own screen (a check, not an input path)
        elif isinstance(n, ast.ImportFrom) and any(a.name in ("hub", "harness") for a in n.names):
            out.append("imports hub/harness")
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            mod = (n.module or "") if isinstance(n, ast.ImportFrom) else ""
            names = [a.name for a in getattr(n, "names", [])]
            if (re.search(r"(^|\.)(harness|hub)(\.|$)", mod) and (getattr(n, "level", 0) or 0) >= 1
                    and not (mod == "harness" and names == ["verb_policy"])
                    and not (mod == "harness.compact_gate" and names == ["screen_hub_inject"] and allow_route)):
                out.append(f"from {'.' * n.level}{mod}")
    return out


def _mods():
    return {f: open(os.path.join(PKG, f), encoding="utf-8").read() for f in sorted(os.listdir(PKG)) if f.endswith(".py")}


def test_only_inject_py_touches_registry_route_and_no_scheduler_module_reaches_input_another_way():
    mods = _mods()
    assert "inject.py" in mods and "engine.py" in mods
    bad = {}
    for f, src in mods.items():
        v = violations(src, allow_route=(f == "inject.py"))
        if v:
            bad[f] = v
    assert bad == {}, bad
    assert ".route(" in mods["inject.py"] and ".route(" not in "".join(s for f, s in mods.items() if f != "inject.py")
    # the engine reaches a session ONLY through inject.send (which is the one place that calls registry.route)
    assert "_inject.send(" in mods["engine.py"]


def test_the_scan_itself_can_fail_positive_controls():
    assert violations("def f(reg):\n    reg.route('x', {})\n", allow_route=False) == [".route"]
    assert violations("import pty\n", allow_route=False)
    assert violations("import subprocess\n", allow_route=False)
    assert violations("from socket import socket\n", allow_route=False)
    assert violations("import os\nos.system('x')\n", allow_route=False)
    assert violations("def f(w):\n    w.writer.inject('x')\n", allow_route=False)
    assert violations("from ..harness import hooks\n", allow_route=False)
    assert violations("def f(reg):\n    reg.route('x', {})\n", allow_route=True) == []


def test_the_scheduler_package_is_not_imported_at_top_level_by_the_hook_closure_modules():
    for rel in ("hub/mcp.py", "hub/caller_binding.py", "hub/server.py", "flags.py", "hub/feature_ids.py"):
        tree = ast.parse(open(os.path.join(ROOT, "gnomon", rel), encoding="utf-8").read())
        for n in tree.body:                                     # module level only
            if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scheduler"):
                raise AssertionError(f"{rel} imports the scheduler at module level")
            if isinstance(n, ast.ImportFrom) and n.level and any(a.name == "scheduler" for a in n.names):
                raise AssertionError(f"{rel} imports the scheduler at module level")
            if isinstance(n, ast.ImportFrom) and n.level and (n.module or "").startswith("scheduler"):
                raise AssertionError(f"{rel} imports the scheduler at module level")


def test_importing_the_hub_and_flags_does_not_import_the_scheduler():
    import subprocess, sys
    code = ("import sys; import gnomon.hub.mcp, gnomon.hub.caller_binding, gnomon.flags; "
            "print(sorted(m for m in sys.modules if m.startswith('gnomon.scheduler')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=ROOT,
                         env={**os.environ, "PYTHONPATH": ROOT}).stdout.strip()
    assert out == "[]", out


# --- ac-s1-label-not-authority --------------------------------------------------------------------------------------
def _py_files():
    for base, _d, files in os.walk(os.path.join(ROOT, "gnomon")):
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(base, f)


def label_decisions(src: str) -> list:
    """Code that COMPARES or PARSES the label text: a call to startswith/match/search/find/index/split on, or a
    comparison/`in` involving, a string constant containing '[scheduled'."""
    out = []
    for n in ast.walk(ast.parse(src)):
        consts = [c for c in ast.walk(n) if isinstance(c, ast.Constant) and isinstance(c.value, str) and "[scheduled" in c.value]
        if not consts:
            continue
        if isinstance(n, ast.Compare):
            out.append("compare")
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in (
                "startswith", "endswith", "match", "search", "find", "index", "split", "fullmatch", "findall", "sub", "compile"):
            out.append(n.func.attr)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "re":
            out.append("re." + n.func.attr)
    return out


def test_nothing_decides_trust_origin_or_permission_from_the_label_text():
    holders = {}
    for p in _py_files():
        src = open(p, encoding="utf-8").read()
        if "[scheduled" in src:
            holders[os.path.relpath(p, ROOT)] = label_decisions(src)
    assert set(holders) <= {"gnomon/scheduler/text.py", "gnomon/scheduler/verb_tools.py",
                            "gnomon/scheduler/engine.py", "gnomon/scheduler/vocab.py"}, holders
    assert all(v == [] for v in holders.values()), holders
    # positive control: the scan does catch a label comparison
    assert label_decisions("def f(t):\n    return t.startswith('[scheduled: ')\n") == ["startswith"]
    assert label_decisions("def f(t):\n    return t == '[scheduled: x]'\n") == ["compare"]
    # the proof of origin the code reads is the injection_log trigger
    eng = open(os.path.join(PKG, "engine.py"), encoding="utf-8").read()
    inj = open(os.path.join(PKG, "inject.py"), encoding="utf-8").read()
    assert 'r.get("trigger") == trigger' in inj and "frame_trigger" in eng


def test_the_web_tab_script_and_the_agent_visible_text_never_branch_on_the_label():
    js = open(os.path.join(ROOT, "web", "scheduler_ui", "scheduler_tab.js"), encoding="utf-8").read()
    assert "[scheduled" not in js

"""round 3g: the fixes for the round-3f delta Diamond (audits/audit-apps-tab-r3f-2026-10-03.md, on 3e97a13).

R3F-F1 (P1): a venv symlinked into the app root ran as its target interpreter WITHOUT the venv, because exec of the
realpath hides the venv symlink and macOS Python takes sys.prefix / pyvenv.cfg from the execve path (not argv[0]). The
architect's ruling: exec the LITERAL manifest path when it is absolute, has no '.'/'..' component and no '//', and
``os.stat(literal)`` has the same (st_dev, st_ino) as the judged realpath (re-checked at exec); else the judged realpath.
R3F-F2: nothing tested that the action runner execs the judged path. R3F-F4: the AST guard had evasions (from-import,
getattr, pathlib). Spec: g73-amendment-9.

Hermetic: tmp_path, the launchd double. The venv test is end to end (a real ``python -m venv --symlinks`` and a real
os.execve in a forked child)."""

import ast
import glob
import json
import os
import shutil
import subprocess
import time

import pytest

from appshost_support import PYTHON, Env
from test_appshost_round3d import _manifest_with, _write
from test_appshost_round3f import _LEXICAL_OK
from gnomon.appshost import launch as L
from gnomon.appshost import preflight as PF
from gnomon.appshost import rules as R
from gnomon.appshost.host import Actor
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)
TRUE = "/usr/bin/true"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


# ---------------------------------------------------------------- R3F-F1: exec_target, unit level
def _judged(tmp_path, spelling):
    exe = os.path.realpath(PYTHON)
    return R.Judged(exe, None, spelling), exe


def test_r3f_f1_a_plain_absolute_symlink_literal_is_the_exec_target(tmp_path):
    link = tmp_path / "pyl"
    os.symlink(PYTHON, link)
    j, exe = _judged(tmp_path, str(link))
    assert j.exec_target() == str(link) != exe


@pytest.mark.parametrize("spelling", [
    "{d}/./pyl", "{d}/sub/../pyl", "{d}//pyl", "{d}/pyl/", "pyl", "./pyl", "", "{d}/../{b}/pyl",
])
def test_r3f_f1_a_literal_that_is_not_plain_absolute_execs_the_judged_realpath(tmp_path, spelling):
    os.makedirs(tmp_path / "sub")
    os.symlink(PYTHON, tmp_path / "pyl")
    lit = spelling.format(d=str(tmp_path), b=tmp_path.name)
    j, exe = _judged(tmp_path, lit)
    assert j.exec_target() == exe


def test_r3f_f1_a_literal_swapped_to_another_file_after_judgement_execs_the_realpath(tmp_path):
    link = tmp_path / "pyl"
    os.symlink(PYTHON, link)
    j, exe = _judged(tmp_path, str(link))
    assert j.exec_target() == str(link)
    other = tmp_path / "other"
    shutil.copy(TRUE, other)
    tmp = tmp_path / "pyl.new"
    os.symlink(other, tmp)
    os.replace(tmp, link)                                             # the symlink now names a DIFFERENT file
    assert j.exec_target() == exe                                    # (c) fails: never the swapped file
    os.unlink(link)
    assert j.exec_target() == exe                                    # a vanished literal too


def test_r3f_f1_no_literal_means_the_realpath():
    j = R.Judged(PYTHON, None)
    assert j.exec_target() == PYTHON


# ---------------------------------------------------------------- R3F-F1: the wrapper, with an injected exec
def _wrapper_exec(env, name, spelling_of):
    mp = env.manifest(name, with_port=False, mode="sleep")
    d = os.path.realpath(os.path.dirname(mp))
    os.symlink(PYTHON, os.path.join(d, "pyl"))
    m = json.load(open(mp))
    m["start"] = [spelling_of(d), "app.py", "--mode", "sleep"]
    json.dump(m, open(mp, "w"))
    a = env.install(mp)
    got = []
    assert L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=lambda *x: got.append(x[0]), watch=False) == 0
    return d, got


def test_r3f_f1_wrapper_execs_the_plain_literal(env):
    d, got = _wrapper_exec(env, "lit1", lambda d: os.path.join(d, "pyl"))
    assert got == [os.path.join(d, "pyl")]


def test_r3f_f1_wrapper_execs_the_realpath_for_a_dot_component(env):
    d, got = _wrapper_exec(env, "lit2", lambda d: os.path.join(d, ".", "pyl"))
    assert got == [PYTHON]


def test_r3f_f1_a_dotdot_after_a_symlink_still_refuses_at_propose(env, tmp_path):
    mp = env.manifest("dd", with_port=False, mode="sleep")
    d = os.path.realpath(os.path.dirname(mp))
    outside = tmp_path / "outside"
    outside.mkdir()
    shutil.copy(TRUE, outside / "python3")                           # a Mach-O, but NOT the one a lexical collapse names
    os.symlink(outside, os.path.join(d, "S"))
    open(os.path.join(d, "python3"), "w").write("#!/bin/sh\n")       # what a lexical collapse of S/.. would name
    m = json.load(open(mp))
    m["start"] = [os.path.join(d, "S", "..", "python3"), "app.py"]
    json.dump(m, open(mp, "w"))
    r = env.host.propose_install(mp, Actor("agent-x", False))
    assert r["ok"] is False and r.get("field") == "start[0]", r


# ---------------------------------------------------------------- R3F-F1: end to end, a real venv
def test_r3f_f1_a_root_venv_imports_its_package_when_started_through_the_host(env, tmp_path):
    mp = env.manifest("vv", with_port=False, mode="sleep")
    d = os.path.realpath(os.path.dirname(mp))
    subprocess.check_call([PYTHON, "-m", "venv", "--symlinks", os.path.join(d, ".venv")])
    sp = glob.glob(os.path.join(d, ".venv", "lib", "python*", "site-packages"))[0]
    open(os.path.join(sp, "venvpkg_r3g.py"), "w").write("X = 1\n")
    out = str(tmp_path / "result.txt")
    open(os.path.join(d, "app.py"), "w").write(
        "import sys\ntry:\n    import venvpkg_r3g\n    r = 'VENV-OK'\nexcept ImportError:\n    r = 'NO-VENV'\n"
        f"open({out!r}, 'w').write(r + ' ' + sys.prefix)\n")
    m = json.load(open(mp))
    m["start"] = [os.path.join(d, ".venv", "bin", "python"), "app.py"]
    json.dump(m, open(mp, "w"))
    a = env.install(mp)
    pid = os.fork()
    if pid == 0:                                                     # the REAL wrapper, the REAL os.execve
        try:
            L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=os.execve, watch=False)
        finally:
            os._exit(9)
    os.waitpid(pid, 0)
    got = open(out).read() if os.path.exists(out) else "no output"
    assert got.startswith("VENV-OK"), got
    assert os.path.realpath(got.split(" ", 1)[1]) == os.path.realpath(os.path.join(d, ".venv")), got


# ---------------------------------------------------------------- R3F-F2: the action runner's exec path
def _capture_popen(monkeypatch):
    seen = []
    real = subprocess.Popen

    def spy(*a, **kw):
        seen.append(kw.get("executable"))
        return real(*a, **kw)

    monkeypatch.setattr(subprocess, "Popen", spy)
    return seen


def _action_env(env, name, spelling_of):
    mp = _manifest_with(env, name, actions=[{"name": "go", "argv": [PYTHON, "act.py"]}])
    d = os.path.realpath(os.path.dirname(mp))
    os.symlink(PYTHON, os.path.join(d, "pyl"))
    _write(os.path.join(d, "act.py"), "pass\n")
    m = json.load(open(mp))
    m["actions"][0]["argv"] = [spelling_of(d), "act.py"]
    json.dump(m, open(mp, "w"))
    return env.install(mp), d


def test_r3f_f2_the_action_runner_execs_the_judged_realpath_for_a_dot_spelling(env, monkeypatch):
    """KILLS the mutant 'the action runner execs the literal path instead of the judged one': the literal has a '.'
    component, so it must NOT be exec'd; its realpath must."""
    a, d = _action_env(env, "act1", lambda d: os.path.join(d, ".", "pyl"))
    seen = _capture_popen(monkeypatch)
    r = env.host.run_action(a, "go", operator, confirmed=True)
    assert r["ok"] is True, r
    assert seen == [PYTHON], seen


def test_r3f_f2_the_action_runner_execs_a_plain_literal_as_is(env, monkeypatch):
    """The other direction (a mutant that always execs the realpath is killed too)."""
    a, d = _action_env(env, "act2", lambda d: os.path.join(d, "pyl"))
    seen = _capture_popen(monkeypatch)
    assert env.host.run_action(a, "go", operator, confirmed=True)["ok"] is True
    assert seen == [os.path.join(d, "pyl")], seen


def test_r3f_f2_an_action_whose_literal_symlink_is_swapped_after_judgement_runs_the_judged_file(env, monkeypatch):
    a, d = _action_env(env, "act3", lambda d: os.path.join(d, "pyl"))
    link = os.path.join(d, "pyl")
    other = os.path.join(os.path.dirname(d), "other-interp")
    shutil.copy(TRUE, other)
    real_build_env = PF.build_env

    def swap(*args, **kw):                                           # between exec_argv (judged) and the Popen
        tmp = link + ".new"
        os.symlink(other, tmp)
        os.replace(tmp, link)
        return real_build_env(*args, **kw)

    monkeypatch.setattr(PF, "build_env", swap)
    seen = _capture_popen(monkeypatch)
    env.host.run_action(a, "go", operator, confirmed=True)
    assert seen in ([PYTHON], []), seen                              # never the swapped target, via the literal
    assert link not in seen


# ---------------------------------------------------------------- R3F-F4: the extended AST guard
_BANNED = {"normpath", "abspath"}
_PATHLIB_BANNED = {"resolve", "absolute"}
_PATH_MODULES = {"os.path", "posixpath", "ntpath", "genericpath", "os"}


def _lexical_calls_v2(source: str, fname: str):
    """Every (file, enclosing scope, what) that normalises a path lexically or via pathlib, under any spelling:
    ``os.path.normpath``/``abspath`` attribute, a from-import of them (and a call of the imported name), ``getattr`` on
    the path modules or with a banned constant name, pathlib imports, and zero-positional-arg ``.resolve()`` /
    ``.absolute()`` calls (``Path.resolve(strict=False)``; a call with positional arguments is some other API)."""
    tree = ast.parse(source)
    out = []
    imported = set()

    def visit(node, scope):
        for ch in ast.iter_child_nodes(node):
            sc = scope
            if isinstance(ch, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                sc = scope + [ch.name]
            where = ".".join(scope) or "<module>"
            if isinstance(ch, ast.Attribute) and ch.attr in _BANNED:
                out.append((fname, where, ch.attr))
            elif isinstance(ch, ast.ImportFrom):
                mod = ch.module or ""
                if mod in _PATH_MODULES or mod.split(".")[0] == "pathlib":
                    for al in ch.names:
                        if al.name in _BANNED or mod.split(".")[0] == "pathlib":
                            out.append((fname, where, f"from {mod} import {al.name}"))
                            imported.add(al.asname or al.name)
            elif isinstance(ch, ast.Import):
                for al in ch.names:
                    if al.name.split(".")[0] == "pathlib":
                        out.append((fname, where, f"import {al.name}"))
            elif isinstance(ch, ast.Call):
                f = ch.func
                if isinstance(f, ast.Name) and f.id == "getattr" and len(ch.args) >= 2:
                    base = ast.unparse(ch.args[0])
                    name = ch.args[1]
                    if base in _PATH_MODULES or (isinstance(name, ast.Constant) and name.value in _BANNED | _PATHLIB_BANNED):
                        out.append((fname, where, "getattr"))
                elif isinstance(f, ast.Name) and f.id in imported and f.id in _BANNED:
                    pass                                              # already reported at the import
                elif isinstance(f, ast.Attribute) and f.attr in _PATHLIB_BANNED and not ch.args:
                    out.append((fname, where, f".{f.attr}()"))
            visit(ch, sc)

    visit(tree, [])
    return out


def test_r3f_f4_guard_no_security_path_normalises_lexically_or_through_pathlib():
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "appshost")
    found = []
    files = sorted(f for f in os.listdir(base) if f.endswith(".py"))
    assert len(files) >= 10, files                                   # the scan reached the package
    for fn in files:
        found += _lexical_calls_v2(open(os.path.join(base, fn)).read(), fn)
    bad = [(f, s, n) for f, s, n in found if (f, s) not in _LEXICAL_OK]
    assert bad == [], f"a security path must use rules.physical (realpath of the original string): {bad}"


@pytest.mark.parametrize("src,expect", [
    ("from os.path import normpath\ndef f(x):\n    return normpath(x)\n", "from os.path import normpath"),
    ("from os.path import abspath as ap\nap('x')\n", "from os.path import abspath"),
    ("from posixpath import normpath\n", "from posixpath import normpath"),
    ("import os\ndef f(x):\n    return getattr(os.path, 'norm' + 'path')(x)\n", "getattr"),
    ("def f(o):\n    return getattr(o, 'abspath')('x')\n", "getattr"),
    ("import pathlib\n", "import pathlib"),
    ("from pathlib import Path\n", "from pathlib import Path"),
    ("def f(p):\n    return p.resolve()\n", ".resolve()"),
    ("def f(p):\n    return p.resolve(strict=False)\n", ".resolve()"),
    ("def f(p):\n    return p.absolute()\n", ".absolute()"),
    ("import os\ndef f(x):\n    return os.path.normpath(x)\n", "normpath"),
])
def test_r3f_f4_the_guard_can_fail(src, expect):
    assert [n for _f, _s, n in _lexical_calls_v2(src, "rules.py")] != [], src
    assert expect in [n for _f, _s, n in _lexical_calls_v2(src, "rules.py")], src


@pytest.mark.parametrize("src", [
    "import os\nos.path.realpath('x')\nos.path.join('a', 'b')\nos.path.dirname('x')\n",
    "def f(d, a):\n    return d.resolve(a, 'x')\n",                    # a positional-arg API (deps.resolve), not Path.resolve
    "def f(o, name):\n    return getattr(o, name)\n",                  # a dynamic attribute on a non-path object
])
def test_r3f_f4_the_guard_does_not_flag_innocent_code(src):
    assert _lexical_calls_v2(src, "rules.py") == []

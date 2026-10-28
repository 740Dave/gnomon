"""round 3i: the fixes for the round-3h delta Diamond (audits/audit-apps-tab-r3h-2026-10-03.md, on f43db60).

R3H-F1 (P2): the structural exec-site guard was weaker than g73-amendment-10 (3)(i) said (8 of 12 planted shapes were
caught by nothing). Now tests/appshost_execscan.py scans the WHOLE package at run time (os.walk, no file list), resolves
aliases, bans the modules and builtins a call could hide behind, pins fixed-argv sites by CALL COUNT, and requires the
exec_target() receiver to be the judged object and a verify() before the call. The auditor's 12 planted shapes are the
parametrized cases below, each run both as a snippet and (where the shape is about a real file) planted in a COPY of
the real package.
R3H-F2 (P2): run_argv_ok (and every exec site) takes a SEALED JudgedArgv: an immutable non-list, buildable only through
a private token, whose tokens and judgement are bound into a digest that verify() re-checks.

Hermetic: tmp_path, the launchd double. Never touches the real package (plants go in a copy)."""

import json
import os
import shutil
import subprocess

import pytest

import appshost_execscan as X
from appshost_support import PYTHON, Env
from test_appshost_round3d import _manifest_with, _write
from gnomon.appshost import launch as L
from gnomon.appshost import preflight as PF
from gnomon.appshost import rules as R
from gnomon.appshost.host import Actor
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


# ================================================================== R3H-F1: the scanner over the real package
def test_r3h_f1_the_real_package_every_exec_site_is_judged_or_pinned():
    files = X.package_files()
    assert len([f for f, _a in files if f.endswith(".py")]) >= 10, files
    sites = X.scan_package()
    bad = [s for s in sites if not s[3]]
    assert bad == [], f"g73-amendment-11 (i): every process start is judged or pinned, nothing else is in appshost: {bad}"
    judged = {(f, sc) for f, sc, _k, ok, _w in sites if (f, sc) not in X.FIXED_ARGV_SITES}
    assert judged == {("launch.py", "run"), ("host.py", "AppsHost.run_action"), ("preflight.py", "run_argv_ok")}, judged
    counts = {}
    for f, sc, _k, ok, _w in sites:
        if (f, sc) in X.FIXED_ARGV_SITES:
            counts[(f, sc)] = counts.get((f, sc), 0) + 1
    assert counts == {k: v[0] for k, v in X.FIXED_ARGV_SITES.items()}, counts     # no stale pin, none under- or over-counted


# the auditor's 12 planted shapes (audit-apps-tab-r3h, 3.2), as sources; each must be reported by the scanner
_A = "import subprocess\n"
PLANTS = {
    "P1_module_alias": "import subprocess as sp\ndef f(a):\n    return sp.run(a)\n",
    "P2_alias_by_assignment": "import subprocess\nsp = subprocess\ndef f(a):\n    return sp.run(a)\n",
    "P3_asyncio": "import asyncio\nasync def f(a):\n    return await asyncio.create_subprocess_exec(*a)\n",
    "P4_getattr_concat": "import os\ndef f(a, e):\n    getattr(os, 'exe' + 'cve')(a[0], a, e)\n",
    "P6_os_alias": "import os as _o\ndef f(a, e):\n    _o.execv(a[0], a)\n",
    "P7_pty_spawn": "import pty\ndef f(a):\n    pty.spawn(a)\n",
    "P8_ctypes": "import ctypes\ndef f(c):\n    ctypes.CDLL(None).system(c)\n",
    "P10_new_file_plain": "import subprocess\ndef f(a):\n    subprocess.run(a)\n",
    "P11_executable_but_not_judged": "import subprocess\ndef f(a, j):\n    subprocess.run(a, executable=j.exec_target())\n",
    "P12_alias_in_an_existing_file": "import subprocess as _sp\n",
}


@pytest.mark.parametrize("name", sorted(PLANTS))
def test_r3h_f1_each_planted_shape_is_caught_as_a_snippet(name):
    assert [s for s in X.scan_source(PLANTS[name], "newfile.py") if not s[3]] != [], name


@pytest.fixture
def pkg_copy(tmp_path):
    dst = tmp_path / "appshost_copy"
    shutil.copytree(X.APPSHOST, dst, ignore=shutil.ignore_patterns("__pycache__"))
    return str(dst)


def _append(path, text):
    with open(path, "a") as fh:
        fh.write(text)


def test_r3h_f1_control_the_unmodified_copy_is_clean(pkg_copy):
    assert [s for s in X.scan_package(pkg_copy) if not s[3]] == []


def test_r3h_f1_p9_a_new_subpackage_is_scanned(pkg_copy):
    """KILLS 'the scanner lists only top-level files' (the R3H P9 plant: appshost/sub/newsite.py)."""
    os.makedirs(os.path.join(pkg_copy, "sub", "deeper"))
    _write(os.path.join(pkg_copy, "sub", "deeper", "newsite.py"), "import subprocess\ndef f(a):\n    subprocess.run(a)\n")
    bad = [s for s in X.scan_package(pkg_copy) if not s[3]]
    assert any(s[0] == os.path.join("sub", "deeper", "newsite.py") for s in bad), bad


def test_r3h_f1_p5_a_second_call_inside_a_pinned_function_is_caught(pkg_copy):
    """KILLS 'fixed-argv sites are pinned by (file, function)': the pin counts CALLS (R3H-F4)."""
    p = os.path.join(pkg_copy, "launchd.py")
    src = open(p).read()
    marker = "subprocess.run("
    assert src.count(marker) == 1
    # a second exec call in the same pinned function: add it right after the existing statement's line
    lines = src.split("\n")
    i = next(k for k, l in enumerate(lines) if marker in l)
    indent = len(lines[i]) - len(lines[i].lstrip())
    j = i
    while lines[j].count("(") > lines[j].count(")") or lines[j].rstrip().endswith(","):
        j += 1
        if sum(l.count("(") - l.count(")") for l in lines[i:j + 1]) <= 0:
            break
    lines.insert(j + 1, " " * indent + "subprocess.run(['/bin/sh', '-c', args[0]])")
    open(p, "w").write("\n".join(lines))
    bad = [s for s in X.scan_package(pkg_copy) if not s[3]]
    assert any(s[0] == "launchd.py" and "pinned" in s[4] for s in bad), bad


def test_r3h_f1_p12_an_alias_added_to_an_existing_file_is_caught(pkg_copy):
    _append(os.path.join(pkg_copy, "host.py"), "\nimport subprocess as _sp\n")
    bad = [s for s in X.scan_package(pkg_copy) if not s[3]]
    assert any(s[0] == "host.py" and "aliased" in s[4] for s in bad), bad


def test_r3h_f1_a_native_extension_in_the_package_is_caught(pkg_copy):
    open(os.path.join(pkg_copy, "fast.so"), "wb").write(b"\x00")
    assert any(s[0] == "fast.so" and not s[3] for s in X.scan_package(pkg_copy))


@pytest.mark.parametrize("src", [
    "from subprocess import run\n",
    "from subprocess import Popen as P\n",
    "from os import execv\n",
    "from os import *\n",
    "from asyncio import create_subprocess_shell\n",
    "from asyncio.subprocess import PIPE\n",
    "import subprocess\nR = subprocess.run\n",
    "import subprocess\ndef f(run=subprocess.run):\n    pass\n",
    "import subprocess\ndef f():\n    return [subprocess]\n",
    "import os\nfor m in (os,):\n    pass\n",
    "import os\ndef f(a, e):\n    os.posix_spawn(a[0], a, e)\n",
    "import os\ndef f(a, j):\n    os.system(a)\n",
    "import os\ndef f(a, j, e):\n    os.spawnv(os.P_NOWAIT, j.exec_target(), a)\n",
    "import os\ndef f(a, e):\n    os.execve(a[0], a, e)\n",
    "import subprocess\ndef f(a, j):\n    subprocess.Popen(a, executable=j.exe)\n",
    "import subprocess\ndef f(a, j, other):\n    subprocess.run(a, executable=other.exec_target())\n",
    "import subprocess\ndef f(a, judged):\n    subprocess.run(a, executable=judged.exec_target())\n",            # no verify()
    "import subprocess\ndef f(a, j, other):\n    j.verify()\n    subprocess.run(a, executable=other.exec_target())\n",   # R3H-F9: wrong receiver
    "import subprocess\ndef f(a):\n    subprocess.getoutput(a)\n",
    "import multiprocessing\n",
    "import cffi\n",
    "import importlib\n",
    "from importlib import import_module\n",
    "def f():\n    return __import__('subprocess')\n",
    "def f(s):\n    return eval(s)\n",
    "def f(s):\n    exec(s)\n",
    "import sys\ndef f():\n    return sys.modules['subprocess']\n",
    "import os\ndef f():\n    return os.__dict__['execv']\n",
    "import os\ndef f():\n    return vars(os)\n",
    "import subprocess\ndef f():\n    return getattr(subprocess, 'run')\n",
    "import webbrowser\ndef f(u):\n    webbrowser.open(u)\n",
    "def f(loop, a):\n    return loop.subprocess_exec(None, *a)\n",
    "def f(sp, a):\n    return sp.Popen(a)\n",
    "import pty\n",
    "from concurrent.futures import ProcessPoolExecutor\n",
])
def test_r3h_f1_the_scanner_can_fail(src):
    assert [s for s in X.scan_source(src, "newfile.py") if not s[3]] != [], src


@pytest.mark.parametrize("src", [
    "import subprocess\ndef f(a, judged):\n    judged.verify()\n    return subprocess.run(a, executable=judged.exec_target())\n",
    "import subprocess\ndef f(a, argv):\n    argv.verify()\n    return subprocess.run(a, executable=argv.judged.exec_target())\n",
    "import subprocess\ndef f(a, judged):\n    judged.verify(a)\n    t = judged.exec_target()\n    return subprocess.Popen(a, executable=t)\n",
    "import os\ndef f(a, judged, e):\n    judged.verify()\n    os.execve(judged.exec_target(), a, e)\n",
    "import os\ndef run(a, exec_fn=os.execve):\n    a.verify()\n    exec_fn(a.judged.exec_target(), [], {})\n",
    "import os\nos.path.join('a', 'b')\nos.environ.get('x')\nos.fork\n",
    "import subprocess\ntry:\n    pass\nexcept subprocess.TimeoutExpired:\n    pass\nx = subprocess.DEVNULL\n",
    "import re\nX = re.compile('a')\n",
])
def test_r3h_f1_the_scanner_accepts_judged_sites_and_innocent_code(src):
    assert [s for s in X.scan_source(src, "newfile.py") if not s[3]] == [], src


# ================================================================== R3H-F2: the sealed JudgedArgv
TRUE_BIN = "/usr/bin/true"


def _presence(env, name):
    mp = env.manifest(name, with_port=False, mode="sleep",
                      hardware={"device_class": "d", "presence_check": [PYTHON, "presence.py", "ok"]})
    d = os.path.realpath(os.path.dirname(mp))
    _write(os.path.join(d, "presence.py"), "print('ok')\n")
    a = env.install(mp)
    prep = PF.load_installed(env.host.paths, a)
    return prep, PF.exec_argv(prep, "presence"), a


def _spy(monkeypatch, kind="run"):
    seen = []
    real = getattr(subprocess, "run" if kind == "run" else "Popen")

    def spy(*a, **kw):
        seen.append((list(a[0]) if a else None, kw.get("executable")))
        return real(*a, **kw)

    monkeypatch.setattr(subprocess, "run" if kind == "run" else "Popen", spy)
    return seen


def test_r3h_f2_a_list_subclass_carrying_a_judged_from_another_argv_is_refused(env, monkeypatch):
    """The auditor's reproduction 1: a list subclass with .judged taken from a DIFFERENT argv ran ['/bin/echo','x']."""
    prep, real_argv, _a = _presence(env, "seal1")

    class Fake(list):
        pass

    fake = Fake(["/bin/echo", "x"])
    fake.judged = real_argv.judged
    seen = _spy(monkeypatch)
    with pytest.raises(PF.Refusal) as ei:
        PF.run_argv_ok(fake, prep.cwd, 5, {})
    assert ei.value.reason == "code-changed" and seen == []


def test_r3h_f2_a_judged_argv_edited_after_judgement_is_refused_and_cannot_be_edited_by_normal_means(env, tmp_path, monkeypatch):
    """The auditor's reproduction 2: argv[:] = [argv[0], '-c', "open(marker,'w')..."] ran and wrote the marker."""
    prep, argv, _a = _presence(env, "seal2")
    marker = str(tmp_path / "marker")
    edit = [argv[0], "-c", f"open({marker!r}, 'w').write('x')"]
    with pytest.raises(TypeError):
        argv[:] = edit                                                  # no item assignment on a sealed argv
    with pytest.raises(TypeError):
        argv[1] = "-c"
    with pytest.raises(AttributeError):
        argv.append("x")
    with pytest.raises(AttributeError):
        argv.judged = argv.judged
    with pytest.raises(AttributeError):
        argv._items = tuple(edit)
    # the forced edit (object.__setattr__ is what a hostile in-process caller would need): the digest refuses it
    object.__setattr__(argv, "_items", tuple(edit))
    seen = _spy(monkeypatch)
    with pytest.raises(PF.Refusal) as ei:
        PF.run_argv_ok(argv, prep.cwd, 5, PF.build_env(env.host.paths, prep))
    assert ei.value.reason == "code-changed" and seen == [] and not os.path.exists(marker)


def test_r3h_f2_a_judged_argv_given_another_argvs_judgement_is_refused(env, monkeypatch):
    prep, argv, _a = _presence(env, "seal3")
    _p2, other, _a2 = _presence(env, "seal3b")
    object.__setattr__(argv, "_judged", other.judged)                  # same shape of tokens, other judgement object
    # the tokens still equal what 'other' judged only if the two argvs are token-identical; make them differ
    object.__setattr__(argv, "_items", tuple(argv) + ("extra",))
    seen = _spy(monkeypatch)
    with pytest.raises(PF.Refusal):
        PF.run_argv_ok(argv, prep.cwd, 5, {})
    assert seen == []


def test_r3h_f2_a_judged_argv_cannot_be_built_without_the_seal():
    with pytest.raises(TypeError):
        PF.JudgedArgv(["/bin/echo"], None)
    with pytest.raises(TypeError):
        PF.JudgedArgv(["/bin/echo"], None, seal=object())


def test_r3h_f2_judged_is_bound_once_and_verify_needs_the_argv(env):
    prep, argv, _a = _presence(env, "seal4")
    j = argv.judged
    with pytest.raises(R.RuleViolation):
        j.verify()                                                       # a bound judgement needs the argv
    with pytest.raises(R.RuleViolation):
        j.verify(list(argv) + ["x"])
    j.verify(list(argv))                                                 # the judged argv: fine
    argv.verify()
    with pytest.raises(R.RuleViolation):
        j.bind(list(argv))                                               # never re-pointed at another argv
    j.literal = "/tmp/evil"                                              # a changed judgement field
    with pytest.raises(R.RuleViolation):
        argv.verify()


def test_r3h_f2_a_real_judged_argv_still_runs_and_equals_its_list(env, monkeypatch):
    prep, argv, _a = _presence(env, "seal5")
    assert type(argv) is PF.JudgedArgv and not isinstance(argv, list)
    assert argv == list(argv) and argv[0] == PYTHON and len(argv) == len(list(argv))
    assert PF.run_argv_ok(argv, prep.cwd, 20, PF.build_env(env.host.paths, prep))[0] == 0


def test_r3h_f2_the_wrapper_refuses_an_argv_edited_after_judgement(env, monkeypatch):
    """launch.run is an exec site: an argv changed between exec_argv and the exec is refused, exec_fn never called."""
    mp = env.manifest("seal6", with_port=False, mode="sleep")
    a = env.install(mp)
    real = PF.exec_argv

    def tamper(p, kind, name=None):
        out = real(p, kind, name)
        object.__setattr__(out, "_items", tuple(out) + ("--evil",))
        return out

    monkeypatch.setattr(PF, "exec_argv", tamper)
    called = []
    rc = L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=lambda *x: called.append(x), watch=False)
    assert rc == PF.REFUSAL_EXIT and called == []


def test_r3h_f2_the_action_runner_refuses_an_argv_edited_after_judgement(env, monkeypatch):
    mp = _manifest_with(env, "seal7", actions=[{"name": "go", "argv": [PYTHON, "act.py"]}])
    d = os.path.realpath(os.path.dirname(mp))
    _write(os.path.join(d, "act.py"), "pass\n")
    a = env.install(mp)
    real = PF.exec_argv

    def tamper(p, kind, name=None):
        out = real(p, kind, name)
        object.__setattr__(out, "_items", tuple(out) + ("--evil",))
        return out

    monkeypatch.setattr(PF, "exec_argv", tamper)
    seen = _spy(monkeypatch, "Popen")
    r = env.host.run_action(a, "go", operator, confirmed=True)
    assert r["ok"] is False and r.get("reason") == "code-changed" and seen == [], (r, seen)

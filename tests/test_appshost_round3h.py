"""round 3h: the fixes for the round-3g delta Diamond (audits/audit-apps-tab-r3g-2026-10-03.md, on e391b1e).

R3G-F1 (P1): the hardware presence check (preflight.run_argv_ok) was a third exec site that ran the literal argv[0]
without ``Judged.verify()`` or ``exec_target()``. Fixed: it takes the JudgedArgv, verifies, execs ``exec_target()``; a
plain list is refused. A structural test enumerates every process-starting call in appshost and fails when a site
receives anything but a value produced by ``exec_target()`` (or is not on the pinned fixed-argv host allow-list).
R3G-F4: the st_dev-dropped identity mutant is killed with a faked identity. Spec: g73-amendment-10.

Hermetic: tmp_path, the launchd double."""

import json
import os
import subprocess
import time

import pytest

from appshost_support import PYTHON, Env
from test_appshost_round3d import _manifest_with, _write
from gnomon.appshost import launch as L
from gnomon.appshost import preflight as PF
from gnomon.appshost import rules as R
from gnomon.appshost.host import Actor
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)
APPSHOST = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "appshost")


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


# ---------------------------------------------------------------- R3G-F1: the presence check execs the judged path
def _presence_app(env, name, spelling_of):
    mp = env.manifest(name, with_port=False, mode="sleep",
                      hardware={"device_class": "d", "presence_check": [PYTHON, "presence.py", "ok"]})
    d = os.path.realpath(os.path.dirname(mp))
    _write(os.path.join(d, "presence.py"), "print('ok')\n")
    os.symlink(PYTHON, os.path.join(d, "pyl"))
    m = json.load(open(mp))
    m["hardware"]["presence_check"] = [spelling_of(d), "presence.py", "ok"]
    json.dump(m, open(mp, "w"))
    return env.install(mp), d


def _spy_run(monkeypatch):
    seen = []
    real = subprocess.run

    def spy(*a, **kw):
        seen.append(kw.get("executable"))
        return real(*a, **kw)

    monkeypatch.setattr(subprocess, "run", spy)
    return seen


def _presence_argv(env, a):
    prep = PF.load_installed(env.host.paths, a)
    return prep, PF.exec_argv(prep, "presence")


def test_r3g_f1_presence_check_execs_the_judged_realpath_for_a_dot_spelling(env, monkeypatch):
    """KILLS 'the presence check runs the literal argv[0] (executable=None)': a '.' spelling must not be exec'd."""
    a, d = _presence_app(env, "pres1", lambda d: os.path.join(d, ".", "pyl"))
    prep, argv = _presence_argv(env, a)
    seen = _spy_run(monkeypatch)
    code, out = PF.run_argv_ok(argv, prep.cwd, 20, PF.build_env(env.host.paths, prep))
    assert (code, out) == (0, "ok"), (code, out)
    assert seen == [PYTHON], seen


def test_r3g_f1_presence_check_execs_a_plain_literal_as_is(env, monkeypatch):
    """The other direction: a mutant that always execs the realpath is killed too (the venv case)."""
    a, d = _presence_app(env, "pres2", lambda d: os.path.join(d, "pyl"))
    prep, argv = _presence_argv(env, a)
    seen = _spy_run(monkeypatch)
    assert PF.run_argv_ok(argv, prep.cwd, 20, PF.build_env(env.host.paths, prep))[0] == 0
    assert seen == [os.path.join(d, "pyl")], seen


def test_r3g_f1_presence_check_is_verified_before_it_runs(env, monkeypatch):
    """KILLS 'run_argv_ok drops judged.verify()': a changed identity refuses and nothing is run."""
    a, d = _presence_app(env, "pres3", lambda d: os.path.join(d, "pyl"))
    prep, argv = _presence_argv(env, a)
    argv.judged.exe_id = (0, 0)                                      # the judged interpreter is no longer that file
    seen = _spy_run(monkeypatch)
    with pytest.raises(PF.Refusal) as ei:
        PF.run_argv_ok(argv, prep.cwd, 20, PF.build_env(env.host.paths, prep))
    assert ei.value.reason == "code-changed" and seen == []


def test_r3g_f1_presence_check_refuses_an_argv_that_was_not_judged(env, monkeypatch):
    """KILLS 'run_argv_ok runs any list': a plain list (no .judged) is refused, never executed."""
    seen = _spy_run(monkeypatch)
    with pytest.raises(PF.Refusal):
        PF.run_argv_ok([TRUE_BIN, "-c", "x"], "/", 5, {})
    assert seen == []


TRUE_BIN = "/usr/bin/true"


def test_r3g_f1_end_to_end_start_preflight_runs_the_presence_check_on_the_judged_path(env, monkeypatch):
    """Through the wrapper's own preflight (check_all with the REAL deps.run), not a direct call."""
    a, d = _presence_app(env, "pres4", lambda d: os.path.join(d, ".", "pyl"))
    seen = _spy_run(monkeypatch)
    got = []
    assert L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=lambda *x: got.append(x[0]), watch=False) == 0
    assert PYTHON in seen and os.path.join(d, ".", "pyl") not in seen, seen


# (the R3G-F1 structural exec-site guard moved to tests/appshost_execscan.py + test_appshost_round3i.py in round 3i, R3H-F1)


# ---------------------------------------------------------------- R3G-F4: the st_dev-dropped mutant
def test_r3g_f4_exec_target_compares_st_dev_as_well_as_st_ino(tmp_path, monkeypatch):
    """KILLS 'identity compares only st_ino': a literal with the same inode on ANOTHER device is a different file."""
    link = tmp_path / "pyl"
    os.symlink(PYTHON, link)
    exe = os.path.realpath(PYTHON)
    j = R.Judged(exe, None, str(link))
    assert j.exec_target() == str(link)
    dev, ino = j.exe_id
    monkeypatch.setattr(R, "identity", lambda p: (dev + 1, ino) if p == str(link) else (dev, ino))
    assert j.exec_target() == exe


# ---------------------------------------------------------------- (coordinator, from a review) no .pyc into a pinned root
def test_r3h_the_closed_env_sets_pythondontwritebytecode_for_children_that_are_not_isolated(env):
    """KILLS 'the closed env drops PYTHONDONTWRITEBYTECODE=1': -I/-B cover only the interpreter the host starts; a child
    python the app spawns reads this variable. The pin is NOT told to ignore __pycache__ (round 3 ruling)."""
    mp = env.manifest("envb", with_port=False, mode="sleep")
    a = env.install(mp)
    prep = PF.load_installed(env.host.paths, a)
    e = PF.build_env(env.host.paths, prep, {"PYTHONDONTWRITEBYTECODE": "", "PYTHONPYCACHEPREFIX": "/x"})
    assert e["PYTHONDONTWRITEBYTECODE"] == "1" and "PYTHONPYCACHEPREFIX" not in e


_E2E_APP = '''import os, subprocess, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)                             # -I drops the script dir from sys.path
import helper_mod                                    # an in-root module: -B (host flag) must keep it from writing a .pyc
r = subprocess.run([sys.executable, "-c", "import child_mod; print(child_mod.X)"], capture_output=True, text=True, cwd=HERE)
open(os.environ["APP_MARK"], "a").write("child:" + r.stdout.strip() + ":" + str(r.returncode) + "\\n")
open(os.environ["APP_MARK"], "a").write("up\\n")
time.sleep(600)
'''


def _pycaches(root):
    return [os.path.join(dp, d) for dp, dns, _ in os.walk(root) for d in dns if d == "__pycache__"]


def test_r3h_start_stop_start_with_in_root_imports_leaves_no_pycache_and_the_pin_unchanged(env, tmp_path):
    """The acceptance case: confirm -> start (the app imports a root module AND spawns a child python, NOT -I/-B, that
    imports another) -> stop -> start again is not refused as code-changed, and no __pycache__ appears under the root."""
    mark = str(tmp_path / "mark.txt")
    mp = env.manifest("pycacc", with_port=False, mode="sleep", env={"APP_MARK": mark})
    d = os.path.realpath(os.path.dirname(mp))
    _write(os.path.join(d, "app.py"), _E2E_APP)
    _write(os.path.join(d, "helper_mod.py"), "X = 1\n")
    _write(os.path.join(d, "child_mod.py"), "X = 2\n")
    a = env.install(mp)
    assert _pycaches(d) == []

    def started(n):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if os.path.exists(mark) and open(mark).read().count("up\n") >= n:
                return open(mark).read()
            time.sleep(0.1)
        raise AssertionError((open(mark).read() if os.path.exists(mark) else "no marker", env.log(a)))

    r = env.host.start(a, operator)
    assert r["ok"] is True, r
    assert "child:2:0" in started(1)                                 # the child python imported its root module
    assert env.host.stop(a, operator)["ok"] is True
    r2 = env.host.start(a, operator)                                       # the pin is unchanged: not refused as code-changed
    assert r2["ok"] is True, r2
    started(2)
    assert _pycaches(d) == [], _pycaches(d)
    assert not [w for w in os.listdir(d) if w.endswith(".pyc")]


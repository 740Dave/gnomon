"""round 3f: the fixes for the round-3e delta Diamond FAIL (audits/audit-apps-tab-r3e-2026-10-03.md, on 8c8fcef).

R3E-F1 (P1): JUDGE WHAT YOU RUN. ``..`` after a symlink in argv[0] or the script operand was collapsed lexically
(``normpath``) before ``realpath``, while the kernel resolves the symlink first. Now every judged path is
``os.path.realpath`` of the ORIGINAL string, the exec'd path is that realpath, and the identities of the interpreter and
script are re-verified immediately before exec (g73-amendment-8, spec 13 requirement-exec-path-is-the-judged-path).
R3E-F2: the plist writer and the action-log open are non-blocking, regular-file-only. R3E-F3: a lock file that is a FIFO
is a clean refusal. R3E-F4: an escaped quote inside a JSON secret value. R3E-F7: tests for ``..`` after a symlink end to
end, and a guard that fails if a security path normalises lexically. Mach-O: header plausibility.

Each test was written to FAIL on 8c8fcef (fail-before in RUN_SUMMARY.md). Hermetic: tmp_path, the launchd double."""

import ast
import json
import os
import shutil
import stat
import struct
import threading

import pytest

from appshost_support import PYTHON, Env
from test_appshost_round3d import _call_with_deadline, _manifest_with, _write
from gnomon.appshost import fsafe, rules as R
from gnomon.appshost import launch as L
from gnomon.appshost import preflight as PF
from gnomon.appshost.host import Actor, AppsError
from gnomon.appshost.redact import redact_line
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)
AG = Actor("agent-x", False)
TRUE = "/usr/bin/true"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


# ---------------------------------------------------------------- helpers: plausible Mach-O bytes
def macho_bytes(magic_hex: str) -> bytes:
    """A small but PLAUSIBLE Mach-O (or universal) file for the given magic: arm64 MH_EXECUTE, one 8-byte load command."""
    magic = bytes.fromhex(magic_hex)

    def thin(end, is64):
        hdr = struct.pack(end + "IiiIIII", 0xFEEDFACF if is64 else 0xFEEDFACE, 0x0100000C if is64 else 12, 0, 2, 1, 8, 0)
        hdr = hdr[:4] + hdr[4:]
        body = hdr + (b"\0" * 4 if is64 else b"") + struct.pack(end + "II", 0x19, 8)
        return body + b"\0" * 16

    thin_magics = {"feedface": (">", False), "feedfacf": (">", True), "cefaedfe": ("<", False), "cffaedfe": ("<", True)}
    if magic_hex in thin_magics:
        end, is64 = thin_magics[magic_hex]
        b = thin(end, is64)
        # the magic bytes on disk are the given ones; the header fields use the matching byte order
        return magic + b[4:]
    fat = {"cafebabe": (">", False), "bebafeca": ("<", False), "cafebabf": (">", True), "bfbafeca": ("<", True)}
    end, is64 = fat[magic_hex]
    sl = thin(end, True)
    ent = 32 if is64 else 20
    off = 8 + ent
    if is64:
        e = struct.pack(end + "iiQQII", 0x0100000C, 0, off, len(sl), 0, 0)
    else:
        e = struct.pack(end + "iiIII", 0x0100000C, 0, off, len(sl), 0)
    hdr = magic + struct.pack(end + "I", 1)
    return hdr + e + sl


def _mkroot_deep(env, name):
    """An app root (via the Env manifest) holding: ``S`` -> a directory ``depth+2`` levels below the root, so that
    ``S/<depth+1 x '..'>/usr/bin/python3`` COLLAPSES LEXICALLY to /usr/bin/python3 (a real Mach-O) while the kernel
    lands inside the root. Returns (manifest_path, root_dir, spelling_of_argv0, physical_landing_dir)."""
    mp = env.manifest(name, with_port=False, mode="sleep")
    d = os.path.realpath(os.path.dirname(mp))
    depth = len([p for p in d.split("/") if p])
    deep = os.path.join(d, *(["x"] * (depth + 2)))
    os.makedirs(deep)
    S = os.path.join(d, "S")
    os.symlink(deep, S)
    ups = depth + 1
    prog = S + "/" + "/".join([".."] * ups) + "/usr/bin/python3"
    comps = deep.split("/")[1:]
    land = "/" + "/".join(comps[:len(comps) - ups])
    return mp, d, prog, land


# ====================================================================== R3E-F1: judge what you run
@pytest.mark.parametrize("planted", ["script", "macho-copy"])
def test_r3e_f1_dotdot_after_a_dir_symlink_in_argv0_is_refused_end_to_end(env, tmp_path, planted):
    """The auditor's reproduction: propose -> confirm -> start. The lexical collapse names /usr/bin/python3; the kernel
    lands on the ROOT's own python3 (a #! script, or a Mach-O copy inside the root). Must refuse at propose, and the
    script must never run."""
    mp, d, prog, land = _mkroot_deep(env, "dd")
    target = os.path.join(land, "usr", "bin")
    os.makedirs(target, exist_ok=True)
    evil = str(tmp_path / "EVIL")
    exe = os.path.join(target, "python3")
    if planted == "script":
        _write(exe, f'#!/bin/sh\necho RAN > {evil}\nsleep 3\n', 0o755)
    else:
        shutil.copy(TRUE, exe)
        os.chmod(exe, 0o755)
    assert os.path.realpath(prog) == os.path.realpath(exe)         # the physical file is the planted one
    m = json.load(open(mp))
    m["start"] = [prog, "app.py"]
    json.dump(m, open(mp, "w"))
    r = env.host.propose_install(mp, AG)
    assert r["ok"] is False, r
    assert r.get("field") == "start[0]", r
    assert os.path.realpath(exe) in r["error"], r["error"]        # the refusal names the file that would RUN
    assert not os.path.exists(evil)


def test_r3e_f1_the_exec_string_is_a_fixed_point_of_realpath(tmp_path):
    """Whatever spelling the manifest uses (``..`` after a dir symlink, after a FILE symlink where the kernel would
    fail ENOTDIR), the judged exe is a realpath of itself: no ``..``, no symlink component left for the kernel to
    resolve differently from what was judged."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("pass\n")
    os.symlink(root / "app.py", root / "F")
    prog = str(root) + "/F/" + "/".join([".."] * 12) + "/usr/bin/python3"
    if not os.path.exists("/usr/bin/python3"):
        pytest.skip("no /usr/bin/python3")
    flags = list(R.host_flags(prog, str(root)))
    argv = [prog] + flags + [str(root / "app.py")]
    j = R.operand_rule(argv, raw=(prog, "app.py"), n_fixed=len(argv), cwd=str(root), root=str(root), where="start",
                       homes=["/home/nobody-x"])
    assert j.exe == os.path.realpath(j.exe) and ".." not in j.exe.split("/")


def test_r3e_f1_rule_level_dotdot_is_judged_physically(tmp_path):
    """operand_rule on D/S/../python3 where D/python3 is a benign Mach-O outside and S -> root/sub: the kernel runs
    root/python3 (a script). Judged on the realpath: refused, naming the root file."""
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    d = tmp_path / "D"
    d.mkdir()
    (root / "app.py").write_text("pass\n")
    shutil.copy(TRUE, d / "python3")
    os.chmod(d / "python3", 0o755)
    (root / "python3").write_text("#!/bin/sh\nexit 0\n")
    os.chmod(root / "python3", 0o755)
    os.symlink(root / "sub", d / "S")
    prog = f"{d}/S/../python3"
    assert os.path.realpath(prog) == str(root / "python3")
    flags = list(R.host_flags(prog, str(root)))
    argv = [prog] + flags + [str(root / "app.py")]
    with pytest.raises(R.RuleViolation) as ei:
        R.operand_rule(argv, raw=(prog, "app.py"), n_fixed=len(argv), cwd=str(root), root=str(root), where="start",
                       homes=["/home/nobody-x"])
    assert str(root / "python3") in ei.value.message


def test_r3e_f1_dotdot_after_a_symlink_in_the_script_operand(env, tmp_path):
    """The script operand: ``S/../x.py`` where S -> a directory OUTSIDE the root. Old code judged the lexical
    root/x.py (inside) and passed that; the judged and passed path must be the PHYSICAL one (outside the root: refused)."""
    mp = env.manifest("sc", with_port=False, mode="sleep")
    d = os.path.realpath(os.path.dirname(mp))
    outside = tmp_path / "outside" / "deep"
    outside.mkdir(parents=True)
    (tmp_path / "outside" / "x.py").write_text("pass\n")
    (open(os.path.join(d, "x.py"), "w")).write("pass\n")
    os.symlink(outside, os.path.join(d, "S"))
    m = json.load(open(mp))
    m["start"] = [PYTHON, "S/../x.py"]
    json.dump(m, open(mp, "w"))
    r = env.host.propose_install(mp, AG)
    assert r["ok"] is False and r.get("field") == "start[1]", r


def test_r3e_f1_script_operand_is_emitted_as_its_realpath_and_that_is_what_runs(env):
    """A script reached through a symlinked directory inside the root: the argv carries the REALPATH (judged ==
    passed), the wrapper execs the plain literal of an interpreter spelled through a symlink (round 3g), and argv[0] stays the
    manifest's literal (data only: on macOS Python takes sys.prefix and pyvenv.cfg from the execve path, not argv[0]; the
    interpreter spelled with a '..' component is exec'd as its realpath, see round 3g for the plain-literal case)."""
    mp = env.manifest("jp", with_port=False, mode="sleep")
    d = os.path.realpath(os.path.dirname(mp))
    os.makedirs(os.path.join(d, "a", "b"))
    shutil.copy(os.path.join(d, "app.py"), os.path.join(d, "a", "app.py"))
    os.symlink(os.path.join(d, "a", "b"), os.path.join(d, "S"))
    os.symlink(PYTHON, os.path.join(d, "pyl"))                      # a venv-style symlink to a system interpreter
    m = json.load(open(mp))
    m["start"] = [os.path.join(d, "pyl"), "S/../app.py", "--mode", "sleep"]
    json.dump(m, open(mp, "w"))
    a = env.install(mp)
    execd = []
    code = L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=lambda *x: execd.append(x), watch=False)
    assert code == 0 and len(execd) == 1, code
    exe, argv, _env = execd[0]
    # round 3g (R3F-F1): the literal is plain absolute and is the same file as the judged realpath, so IT is exec'd
    assert exe == os.path.join(d, "pyl") and os.path.realpath(exe) == PYTHON
    assert argv[0] == os.path.join(d, "pyl")                       # the manifest's literal spelling: data, not exec'd
    script = argv[argv.index("-B") + 1]
    assert script == os.path.join(d, "a", "app.py")                # realpath of S/../app.py: judged and passed


def test_r3e_f1_positive_control_a_dotdot_that_resolves_to_a_system_interpreter_is_accepted(tmp_path):
    """Control: a ``..`` is not refused as such; the realpath is judged. /usr/bin/../bin/python3 is /usr/bin/python3."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("pass\n")
    prog = "/usr/bin/../bin/python3"
    if not os.path.exists(prog):
        pytest.skip("no /usr/bin/python3 on this machine")
    flags = list(R.host_flags(prog, str(root)))
    argv = [prog] + flags + [str(root / "app.py")]
    j = R.operand_rule(argv, raw=(prog, "app.py"), n_fixed=len(argv), cwd=str(root), root=str(root), where="start",
                       homes=["/home/nobody-x"])
    assert j.exe == os.path.realpath(prog) == R.exec_path(prog, str(root)) and j.script == str(root / "app.py")


def test_r3e_f1_exec_path_is_never_the_lexical_collapse(tmp_path):
    root = tmp_path / "r"
    (root / "a" / "b").mkdir(parents=True)
    os.symlink(root / "a" / "b", root / "S")
    assert R.exec_path(f"{root}/S/../python3", str(root)) == str(root / "a" / "python3")     # not root/python3
    assert R.physical(str(root), "S/../app.py") == str(root / "a" / "app.py")


# ---- identity re-verification immediately before exec
def test_r3e_f1_judged_identity_is_reverified_and_a_swap_is_refused(tmp_path):
    exe = tmp_path / "py"
    scr = tmp_path / "app.py"
    shutil.copy(TRUE, exe)
    scr.write_text("pass\n")
    j = R.Judged(str(exe), str(scr))
    j.verify()                                                      # unchanged: fine
    new = tmp_path / "py2"
    shutil.copy(TRUE, new)
    os.replace(new, exe)                                            # another inode at the judged name
    with pytest.raises(R.RuleViolation, match="interpreter"):
        j.verify()
    j2 = R.Judged(str(exe), str(scr))
    new = tmp_path / "app2.py"
    new.write_text("evil\n")
    os.replace(new, scr)
    with pytest.raises(R.RuleViolation, match="script"):
        j2.verify()
    j3 = R.Judged(str(exe), str(scr))
    os.unlink(scr)
    with pytest.raises(R.RuleViolation):
        j3.verify()


def test_r3e_f1_the_wrapper_refuses_when_the_script_is_swapped_between_judgement_and_exec(env, monkeypatch):
    a = env.install(env.manifest("sw", with_port=False, mode="sleep"))
    script = os.path.join(env.apps_dir, "sw", "app.py")

    def swap_then_nothing(*_a, **_k):                               # runs after the checks, before the final verify
        tmp = script + ".new"
        open(tmp, "w").write("print('swapped')\n")
        os.replace(tmp, script)

    monkeypatch.setattr(L, "_spawn_watcher", swap_then_nothing)
    execd = []
    code = L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=lambda *x: execd.append(x), watch=True)
    assert code == PF.REFUSAL_EXIT and execd == []


def test_r3e_f1_an_action_is_not_run_when_its_script_is_swapped_after_the_judgement(env, monkeypatch):
    mp = _manifest_with(env, "actsw", actions=[{"name": "go", "argv": [PYTHON, "act.py"]}])
    _write(os.path.join(os.path.dirname(mp), "act.py"), "pass\n")
    a = env.install(mp)
    script = os.path.join(os.path.dirname(mp), "act.py")
    real_build_env = PF.build_env

    def swap(*args, **kw):                                           # between exec_argv (judged) and the Popen
        tmp = script + ".new"
        open(tmp, "w").write("open('RAN', 'w')\n")
        os.replace(tmp, script)
        return real_build_env(*args, **kw)

    # the swap changes the code pin too: the action's own check_all would refuse first, so swap AFTER check_all
    monkeypatch.setattr(PF, "build_env", swap)
    r = env.host.run_action(a, "go", operator, confirmed=True)
    assert r["ok"] is False, r
    assert not os.path.exists(os.path.join(os.path.dirname(mp), "RAN"))


# ---- the guard: no lexical normalisation in a security path
_LEXICAL = {"normpath", "abspath"}
#: (file, enclosing function) pairs that are NOT a judged path: each is a trusted-config or installation path.
_LEXICAL_OK = {
    ("state.py", "data_root"),             # the machine config's DATA root (the operator's own setting)
    ("state.py", "HostPaths.__init__"),    # the same root, held
    ("launchd.py", "package_root"),        # dirname(abspath(__file__)): where Gnomon's own code is
}


def _lexical_calls(source: str, fname: str):
    """Every (file, enclosing function, name) that calls os.path.normpath / abspath / posixpath.normpath."""
    tree = ast.parse(source)
    out = []

    def visit(node, scope):
        for ch in ast.iter_child_nodes(node):
            sc = scope
            if isinstance(ch, ast.ClassDef):
                sc = scope + [ch.name]
            elif isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sc = scope + [ch.name]
            if isinstance(ch, ast.Attribute) and ch.attr in _LEXICAL:
                out.append((fname, ".".join(scope) or "<module>", ch.attr))
            visit(ch, sc)

    visit(tree, [])
    return out


def test_r3e_f7_guard_no_security_path_normalises_lexically():
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "appshost")
    found = []
    for fn in sorted(os.listdir(base)):
        if fn.endswith(".py"):
            found += _lexical_calls(open(os.path.join(base, fn)).read(), fn)
    bad = [(f, s, n) for f, s, n in found if (f, s) not in _LEXICAL_OK]
    assert bad == [], ("normpath/abspath collapses '..' lexically BEFORE the kernel resolves symlinks; a path that is "
                       f"judged or exec'd must use realpath on the original string (g73-amendment-8): {bad}")


def test_r3e_f7_the_guard_can_fail():
    """The scanner finds a normpath in a rules-shaped function, in a module-level call, and through an alias-free
    ``os.path.abspath`` (so the guard above is not decoration)."""
    src = "import os\ndef _resolve(cwd, tok):\n    return os.path.normpath(os.path.join(cwd, tok))\nX = os.path.abspath('.')\n"
    assert _lexical_calls(src, "rules.py") == [("rules.py", "_resolve", "normpath"), ("rules.py", "<module>", "abspath")]
    assert _lexical_calls("import os\nos.path.realpath('x')\n", "rules.py") == []


def test_r3e_f7_every_site_that_judges_a_path_uses_the_one_helper():
    """rules.py's argv/script judgement goes through ``physical`` (realpath on the original string)."""
    src = open(R.__file__).read()
    assert "def physical(" in src and "_resolve(" not in src
    body = src[src.index("def physical("):src.index("def exec_path(")]
    assert "os.path.realpath(tok if os.path.isabs(tok) else os.path.join(cwd, tok))" in body and "normpath" not in body.split('"""')[2]


def test_r3e_f1_launch_has_no_second_exe_resolver():
    """launch.py must exec ``argv.judged.exe``; the old ``_resolve_exe`` (a literal absolute argv[0], normpath for
    relative) is gone so judge and exec cannot differ."""
    assert not hasattr(L, "_resolve_exe")
    assert "judged.exe" in open(L.__file__).read()


def test_r3e_f1_propose_manifest_path_is_not_collapsed_lexically(env):
    """host.propose_install used os.path.abspath on the manifest path (collapsing ``..`` after a symlink), reading
    <app>/<name> while the physical path names another file. The spelling that is judged is the one that is read."""
    mp = env.manifest("mpath", with_port=False, mode="sleep")
    d = os.path.dirname(os.path.realpath(mp))
    os.makedirs(os.path.join(d, "a", "b"))
    os.symlink(os.path.join(d, "a", "b"), os.path.join(d, "S"))
    m = json.load(open(mp))
    m["id"] = "mpathb"
    json.dump(m, open(os.path.join(d, "a", os.path.basename(mp)), "w"))      # the PHYSICAL S/../<name>
    r = env.host.propose_install(os.path.join(d, "S", "..", os.path.basename(mp)), AG)
    assert r.get("app_id") != "mpath", r                                      # the lexical d/<name> was NOT what was read


# ====================================================================== R3E-F2: two host writes never block
def _label(app_id):
    from gnomon.appshost.launchd import label_for
    return label_for(app_id)


def _real_launchd(tmp_path):
    from gnomon.appshost.launchd import RealLaunchd
    return RealLaunchd(allow_under_pytest=True, plist_dir=str(tmp_path / "LaunchAgents"))   # file ops only: launchctl is never called


def test_r3e_f2_plist_writer_does_not_block_on_a_fifo_at_the_old_tmp_name(tmp_path):
    ld = _real_launchd(tmp_path)
    label = _label("fx")
    os.makedirs(ld.plist_dir())
    os.mkfifo(ld.plist_path(label) + ".tmp")                          # the predictable name the old writer opened
    box, hung = _call_with_deadline(lambda: ld.write_plist(label, {"Label": label}), 4.0)
    if hung:                                                          # release the old writer so the thread dies
        try:
            os.close(os.open(ld.plist_path(label) + ".tmp", os.O_RDONLY | os.O_NONBLOCK))
        except OSError:
            pass
    assert not hung, "write_plist blocked on a FIFO at <label>.plist.tmp"
    assert "e" not in box, box
    assert stat.S_ISREG(os.stat(ld.plist_path(label)).st_mode)
    import plistlib
    assert plistlib.load(open(ld.plist_path(label), "rb")) == {"Label": label}


def test_r3e_f2_plist_temp_name_is_unique_exclusive_and_cleaned_up(tmp_path, monkeypatch):
    ld = _real_launchd(tmp_path)
    label = _label("fy")
    names = []
    real = fsafe.open_new_fd

    def spy(path, mode=0o600):
        names.append(path)
        return real(path, mode)

    monkeypatch.setattr(fsafe, "open_new_fd", spy)
    ld.write_plist(label, {"Label": label})
    ld.write_plist(label, {"Label": label, "x": 1})
    assert len(names) == 2 and names[0] != names[1] and not any(n.endswith(label + ".plist.tmp") for n in names)
    assert sorted(os.listdir(ld.plist_dir())) == [label + ".plist"]            # no temp left behind
    # a failing write cleans its temp up and re-raises
    monkeypatch.setattr("plistlib.dumps", lambda *_a, **_k: (_ for _ in ()).throw(ValueError("boom")))
    with pytest.raises(ValueError):
        ld.write_plist(label, {"Label": label})
    assert sorted(os.listdir(ld.plist_dir())) == [label + ".plist"]


def test_r3e_f2_open_new_fd_refuses_an_existing_entry_of_any_kind(tmp_path):
    f = str(tmp_path / "p")
    os.mkfifo(f)
    box, hung = _call_with_deadline(lambda: fsafe.open_new_fd(f), 3.0)
    assert not hung and isinstance(box.get("e"), FileExistsError), box
    g = str(tmp_path / "ok")
    fd = fsafe.open_new_fd(g)
    os.close(fd)
    assert stat.S_ISREG(os.stat(g).st_mode) and stat.S_IMODE(os.stat(g).st_mode) == 0o600
    os.symlink(g, str(tmp_path / "ln"))
    with pytest.raises(OSError):
        fsafe.open_new_fd(str(tmp_path / "ln"))


def test_r3e_f2_action_log_on_a_fifo_is_a_clean_failure_not_a_hang(tmp_path):
    e = Env(tmp_path, clock=lambda: 1_700_000_000.0)
    try:
        mp = _manifest_with(e, "actlog", actions=[{"name": "go", "argv": [PYTHON, "act.py"]}])
        _write(os.path.join(os.path.dirname(mp), "act.py"), "pass\n")
        a = e.install(mp)
        logs = e.host.paths.logs(a)
        os.makedirs(logs, exist_ok=True)
        fifo = os.path.join(logs, "action-go-20231114T221320Z.log")
        os.mkfifo(fifo)
        box, hung = _call_with_deadline(lambda: e.host.run_action(a, "go", operator, confirmed=True), 5.0)
        if hung:
            try:
                os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
            except OSError:
                pass
        assert not hung, "run_action blocked on a FIFO log"
        assert "e" not in box, box
        r = box["v"]
        assert r["ok"] is False and "log" in r["error"], r
    finally:
        e.cleanup()


def test_r3e_f2_action_log_regular_file_still_works_and_is_0600(env):
    mp = _manifest_with(env, "actok", actions=[{"name": "go", "argv": [PYTHON, "act.py"]}])
    _write(os.path.join(os.path.dirname(mp), "act.py"), "print('hello-log')\n")
    a = env.install(mp)
    r = env.host.run_action(a, "go", operator, confirmed=True)
    assert r["ok"], r
    for t in env.host.action_threads:
        t.join(10)
    assert "hello-log" in open(r["log"]).read()
    assert stat.S_IMODE(os.stat(r["log"]).st_mode) == 0o600


# ====================================================================== R3E-F3: lock file a FIFO -> clean refusal
def test_r3e_f3_lock_file_fifo_is_a_clean_refusal(env):
    os.makedirs(env.host.paths.apps, exist_ok=True)
    os.mkfifo(env.host.paths.lock)
    mp = env.manifest("lk", with_port=False, mode="sleep")
    box, hung = _call_with_deadline(lambda: env.host.propose_install(mp, AG), 4.0)
    assert not hung
    assert isinstance(box.get("e"), AppsError), box                    # AppsError (what the verbs turn into a refusal), not OSError
    assert "lock" in str(box["e"]) and "regular file" in str(box["e"])


def test_r3e_f3_the_verb_layer_returns_a_refusal_not_an_exception(env):
    os.makedirs(env.host.paths.apps, exist_ok=True)
    os.mkfifo(env.host.paths.lock)
    mp = env.manifest("lk2", with_port=False, mode="sleep")
    # every mutating host verb goes through host_lock: a state_lock refusal is an AppsError subclass instance
    with pytest.raises(AppsError):
        env.host.propose_install(mp, AG)
    from gnomon.appshost import state
    assert state.AppsError is AppsError


def test_r3e_f3_lock_file_normal_path_still_locks(env):
    from gnomon.appshost.state import host_lock
    with host_lock(env.host.paths):
        pass
    assert stat.S_ISREG(os.stat(env.host.paths.lock).st_mode)


# ====================================================================== R3E-F4: escaped quote inside a JSON secret
@pytest.mark.parametrize("line,leak", [
    ('{"password": "hun\\"ter2 more words"}', "ter2"),
    ('{"password": "hun\\"ter2 more words"}', "more words"),
    ('{"token": "a\\": b and c"}', "and c"),
    ("{'secret': 'it\\'s a long secret'}", "long secret"),
    ('{"api_key": "x\\\\\\"yz tail-of-secret"}', "tail-of-secret"),
    ('password="p\\"w rest-of-it"', "rest-of-it"),
    ('{"password": "never closed \\"quote and the tail', "the tail"),
])
def test_r3e_f4_escaped_quote_in_a_secret_value_does_not_leak_the_tail(line, leak):
    out = redact_line(line)
    assert leak not in out, out
    assert "<redacted>" in out


def test_r3e_f4_plain_quoted_values_and_non_secrets_are_unchanged():
    assert redact_line('{"user": "bob smith"}') == '{"user": "bob smith"}'
    assert redact_line('{"password": "x"} and more') == '{"password": <redacted>} and more'
    assert redact_line('{"password": "a\\"b"} after') == '{"password": <redacted>} after'


def test_r3e_f4_still_linear_on_backslash_heavy_input():
    import time
    for line in ('"password": "' + '\\"' * 5000, 'token="' + '\\' * 6000, 'secret: "' + 'a\\' * 4000):
        t = time.perf_counter()
        redact_line(line)
        assert time.perf_counter() - t < 0.5


# ====================================================================== Mach-O header plausibility
THIN_AND_FAT = ["feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca"]


@pytest.mark.parametrize("magic", THIN_AND_FAT)
def test_machoplaus_a_plausible_header_behind_every_magic_is_accepted(tmp_path, magic):
    p = tmp_path / "ok"
    p.write_bytes(macho_bytes(magic))
    assert R.is_macho(str(p)), magic


@pytest.mark.parametrize("magic", THIN_AND_FAT)
@pytest.mark.parametrize("tail", [b"#!/bin/sh\nexit 0\n", b"\0" * 40, b"A" * 64, b""])
def test_machoplaus_a_text_file_that_starts_with_a_magic_is_refused(tmp_path, magic, tail):
    p = tmp_path / "fake"
    p.write_bytes(bytes.fromhex(magic) + tail)
    os.chmod(p, 0o755)
    assert not R.is_macho(str(p)), (magic, tail)


def test_machoplaus_java_class_and_truncated_and_bad_fields_are_refused(tmp_path):
    good = macho_bytes("cffaedfe")
    cases = {
        "java": bytes.fromhex("cafebabe00000041") + b"\0" * 64,
        "truncated-thin": good[:20],
        "truncated-fat": macho_bytes("cafebabe")[:30],
        "bad-cpu": good[:4] + struct.pack("<i", 99) + good[8:],
        "not-execute": good[:12] + struct.pack("<I", 6) + good[16:],           # MH_DYLIB
        "zero-ncmds": good[:16] + struct.pack("<I", 0) + good[20:],
        "ncmds-huge": good[:16] + struct.pack("<I", 10**6) + good[20:],
        "sizeofcmds-beyond-file": good[:20] + struct.pack("<I", 10**6) + good[24:],
    }
    fat = bytearray(macho_bytes("cafebabe"))
    fat[16:20] = struct.pack(">I", 10**6)                                     # slice offset beyond the file
    cases["fat-offset-beyond"] = bytes(fat)
    fat2 = bytearray(macho_bytes("cafebabe"))
    fat2[4:8] = struct.pack(">I", 99)                                         # 99 archs
    cases["fat-too-many"] = bytes(fat2)
    for name, data in cases.items():
        p = tmp_path / name
        p.write_bytes(data)
        assert not R.is_macho(str(p)), name


def test_machoplaus_real_system_binaries_are_still_accepted():
    n = 0
    for p in (TRUE, "/bin/sh", "/bin/zsh", "/usr/bin/perl", "/usr/bin/python3", PYTHON):
        if os.path.exists(p):
            assert R.is_macho(os.path.realpath(p)), p
            n += 1
    assert n >= 3


def test_machoplaus_operand_rule_refuses_a_magic_prefixed_text_file_as_the_interpreter(tmp_path):
    """The auditor's 3.5 case, at the rule: a ``cafebabe`` + '#!/bin/sh' file named python3, outside the root and
    scratch (tmp_path IS scratch, so use a location-neutral check: the Mach-O test comes first)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "app.py").write_text("pass\n")
    fake = tmp_path / "bin" / "python3"
    fake.parent.mkdir()
    fake.write_bytes(bytes.fromhex("cafebabe") + b"#!/bin/sh\nexit 0\n")
    os.chmod(fake, 0o755)
    prog = str(fake)
    flags = list(R.host_flags(prog, str(root)))
    argv = [prog] + flags + [str(root / "app.py")]
    with pytest.raises(R.RuleViolation, match="not a Mach-O"):
        R.operand_rule(argv, raw=(prog, "app.py"), n_fixed=len(argv), cwd=str(root), root=str(root), where="start",
                       homes=["/home/nobody-x"])

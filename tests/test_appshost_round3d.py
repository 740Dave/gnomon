"""round 3d: the fixes for the round-3c delta Diamond FAIL (audits/audit-apps-tab-r3c-2026-10-03.md, on 4a40788).

R3C-F1 (P1): the operand rule is ONE strict shape (g73-amendment-6): argv[0] an allow-listed interpreter by REALPATH
outside the home and the root; only the host's own flags before the script; a literal root file as the script, emitted
as an absolute path; host values only after it. R3C-F3 (a secret straddling the 2048 cut), R3C-F4 (read_json /
read_audit / start_sig never block on a FIFO), R3C-F5 (zsh and bash startup files), R3C-F6 (spec text), R3C-F7 (test
strength: literal numbers, not constants derived from the code under test), R3-F9 (the legacy hub launcher).

Every test was written to FAIL on 4a40788 and PASS after (the fail-before is in RUN_SUMMARY.md). Hermetic: tmp_path, the
launchd double, a patched user home. macOS-only paths (/bin/bash, /bin/zsh) skip when absent."""

import json
import os
import secrets
import shutil
import threading
import time

import pytest

from appshost_support import PYTHON, Env, ROOT
from gnomon.appshost import rules as R
from gnomon.appshost.host import Actor
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)
AG = Actor("agent-x", False)
BASH = "/bin/bash"
ZSH = "/bin/zsh"
SH = "/bin/sh"
NODE = shutil.which("node")


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


def _manifest_with(env, app_id, /, with_port=False, mode="sleep", **fields):
    mp = env.manifest(app_id, with_port=with_port, mode=mode)
    m = json.load(open(mp))
    for k, v in fields.items():
        if v is None:
            m.pop(k, None)
        else:
            m[k] = v
    json.dump(m, open(mp, "w"), indent=2)
    return mp


def _write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    if mode is not None:
        os.chmod(path, mode)


def _p(name, **kw):
    return {"name": name, "description": "d", "kind": "file", "access": "read_only", **kw}


def _propose(env, mp):
    return env.host.propose_install(mp, AG)


def _need(path):
    if not os.path.exists(path):
        pytest.skip(f"{path} not on this machine")


def _call_with_deadline(fn, seconds=4.0):
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["e"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(seconds)
    return box, t.is_alive()


def _release(fifo):
    try:
        os.close(os.open(fifo, os.O_WRONLY | os.O_NONBLOCK))
    except OSError:
        pass


# ====================================================================== R3C-F1: the strict shape, at propose
def _refused(env, app_id, start, files=(), **fields):
    mp = _manifest_with(env, app_id, start=start, **fields)
    d = os.path.dirname(mp)
    for f in files:
        _write(os.path.join(d, f), "#!/bin/sh\nsleep 5\n", 0o755)
    r = _propose(env, mp)
    assert r["ok"] is False and r["field"].startswith("start"), r
    return r


def test_f1_h1_bash_plus_option_token_with_a_host_path_after_it_is_refused(env):
    """The audit's H1: bash +O nullglob {path:s}, a root FILE named '+O'. f8e4ce2/4a40788: accepted; the host path ran."""
    _need(BASH)
    r = _refused(env, "h1", [BASH, "+O", "nullglob", "{path:s}"], files=["+O"],
                 paths=[_p("s")], )
    assert r["field"] == "start[1]", r


@pytest.mark.parametrize("tok", ["+O", "+o", "-O", "-c", "-e", "--init-file", "--rcfile", "+x"])
def test_f1_no_flag_or_plus_token_between_the_interpreter_and_the_script(env, tok):
    r = _refused(env, "tokx", [PYTHON, tok, "app.py"], files=[tok, "app.py"])
    assert r["field"] == "start[1]", r


@pytest.mark.parametrize("which", ["bash", "zsh", "sh"])
def test_f1_a_shell_with_a_plus_or_minus_option_then_script_is_refused(env, which):
    sh = {"bash": BASH, "zsh": ZSH, "sh": SH}[which]
    _need(sh)
    _refused(env, "shx" + which, [sh, "+O", "nullglob", "run.sh"], files=["+O", "run.sh"])
    _refused(env, "shy" + which, [sh, "-O", "nullglob", "run.sh"], files=["run.sh"])


def test_f1_node_require_with_a_host_path_is_refused_and_a_subcommand_word_cannot_be_the_script(env):
    if not NODE:
        pytest.skip("no node on this machine")
    _refused(env, "nodereq", [NODE, "--require", "{path:s}", "a.js"], files=["a.js"], paths=[_p("s")])
    _refused(env, "nodeeval", [NODE, "-e", "1", "a.js"], files=["a.js"])
    # H3 (unproven in the audit, closed anyway): a root file named 'inspect' is a legal script token, and the host emits it
    # as an ABSOLUTE path, which node can never read as the 'inspect' subcommand
    mp = _manifest_with(env, "nodeinspect", start=[NODE, "inspect", "{path:s}"], paths=[_p("s")])
    _write(os.path.join(os.path.dirname(mp), "inspect"), "console.log(1)\n", 0o644)
    r = _propose(env, mp)
    # the script operand 'inspect' is a literal root file, so the shape is legal; the safety is the absolute emission
    root = os.path.dirname(mp)
    hard = R.harden_argv([NODE, "inspect", "/host/x.js"], root)
    assert hard[1] == os.path.join(root, "inspect") and hard[1].startswith("/"), hard
    assert r["ok"] is True, r


def _copy_of_python(dest):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    shutil.copy2(PYTHON, dest)
    return dest


@pytest.mark.parametrize("name", ["nc", "python3", "pyc", "node", "sh"])
def test_f1_h2_a_copy_of_an_interpreter_in_the_root_is_refused_under_any_name(env, name):
    """The audit's H2/H2b: a copied node named nc took --require {path:s}; a copied python named pyc ran without -I."""
    mp = _manifest_with(env, "cp" + name, start=[f"./{name}", "app.py"])
    d = os.path.dirname(mp)
    _copy_of_python(os.path.join(d, name))
    r = _propose(env, mp)
    assert r["ok"] is False and r["field"] == "start[0]", r
    assert "inside the app root" in r["error"] or "allow-listed" in r["error"], r


@pytest.mark.skipif(not NODE, reason="no node")
def test_f1_h2_the_audit_repro_a_copied_node_named_nc_with_require_is_refused(env):
    mp = _manifest_with(env, "ncnode", start=["./nc", "--require", "{path:s}", "a.js"], paths=[_p("s")])
    d = os.path.dirname(mp)
    shutil.copy2(NODE, os.path.join(d, "nc"))
    _write(os.path.join(d, "a.js"), "1\n")
    r = _propose(env, mp)
    assert r["ok"] is False and r["field"].startswith("start"), r


def test_f1_a_root_script_or_binary_is_never_argv0(env):
    mp = _manifest_with(env, "rootprog", start=["./run.sh", "app.py"])
    _write(os.path.join(os.path.dirname(mp), "run.sh"), "#!/bin/sh\nsleep 5\n", 0o755)
    r = _propose(env, mp)
    assert r["ok"] is False and r["field"] == "start[0]", r


def test_f1_a_root_symlink_that_resolves_to_a_system_interpreter_is_allowed_a_venv_like_control(env):
    mp = _manifest_with(env, "venvlike", start=["./bin/python", "app.py"])
    d = os.path.dirname(mp)
    os.makedirs(os.path.join(d, "bin"))
    os.symlink(PYTHON, os.path.join(d, "bin", "python"))
    r = _propose(env, mp)
    assert r["ok"] is True, r
    root = os.path.realpath(d)
    hard = R.harden_argv(["./bin/python", "app.py"], root)
    assert hard[:3] == ["./bin/python", "-I", "-B"] and hard[3] == os.path.join(root, "app.py")


def test_f1_a_root_symlink_to_a_copy_under_the_users_home_is_refused(env, monkeypatch, tmp_path):
    fake_home = str(tmp_path / "fakehome")
    os.makedirs(fake_home)
    monkeypatch.setattr(R, "user_homes", lambda: [os.path.realpath(fake_home)])
    cp = _copy_of_python(os.path.join(fake_home, "py", "python3.13"))
    mp = _manifest_with(env, "homecopy", start=["./bin/python", "app.py"])
    d = os.path.dirname(mp)
    os.makedirs(os.path.join(d, "bin"))
    os.symlink(cp, os.path.join(d, "bin", "python"))
    r = _propose(env, mp)
    assert r["ok"] is False and r["field"] == "start[0]" and "home" in r["error"], r


def test_f1_a_symlink_to_a_copy_in_a_scratch_dir_is_refused_even_through_a_venv_style_link(env):
    scratch = os.path.join("/private/tmp", f"gm3d-{secrets.token_hex(4)}")
    os.makedirs(scratch)
    try:
        cp = _copy_of_python(os.path.join(scratch, "python3.13"))
        mp = _manifest_with(env, "scratchcopy", start=[cp, "app.py"])
        r = _propose(env, mp)
        assert r["ok"] is False and r["field"] == "start[0]", r
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_f1_positive_controls_python_and_bash_with_a_root_script_and_a_path_after_it(env):
    mp = _manifest_with(env, "okpy", start=[PYTHON, "app.py", "{path:s}"], paths=[_p("s")])
    assert _propose(env, mp)["ok"] is True
    _need(BASH)
    mp = _manifest_with(env, "okbash", start=[BASH, "run.sh", "{path:s}"], paths=[_p("s")])
    _write(os.path.join(os.path.dirname(mp), "run.sh"), "#!/bin/bash\nsleep 5\n", 0o755)
    assert _propose(env, mp)["ok"] is True


# ====================================================================== R3C-F1: the shape, on the final argv
def _final(raw, argv, cwd, root, n_fixed=None):
    R.final_check(list(argv), raw=tuple(raw), n_fixed=len(argv) if n_fixed is None else n_fixed, cwd=str(cwd),
                  root=str(root), where="start")


def test_f1_host_flags_are_per_kind_and_the_script_is_emitted_absolute(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.py").write_text("")
    assert R.host_flags(PYTHON, str(root)) == ("-I", "-B")
    for sh, flags in ((ZSH, ("-f",)), (BASH, ("--norc", "--noprofile")), (SH, ())):
        if os.path.exists(sh):
            assert R.host_flags(sh, str(root)) == flags, sh
    h = R.harden_argv([PYTHON, "a.py", "x"], str(root))
    assert h == [PYTHON, "-I", "-B", str(root / "a.py"), "x"]
    assert R.harden_argv(h, str(root)) == h                         # idempotent
    h2 = R.harden_argv([PYTHON, "+O", "x"], str(root))
    assert h2[3] == "+O"                                            # a '+' token is left alone: the rule refuses it


def test_f1_final_check_refuses_a_host_value_before_the_script_and_extra_tokens_between(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.py").write_text("")
    outside = tmp_path / "host.py"
    outside.write_text("")
    raw = (PYTHON, "a.py")
    ok = R.harden_argv(list(raw), str(root))
    _final(raw, ok, root, root)                                      # control: the legal shape
    with pytest.raises(R.RuleViolation):                             # a host path where the manifest's script should be
        _final(raw, [PYTHON, "-I", "-B", str(outside), str(root / "a.py")], root, root, n_fixed=4)
    with pytest.raises(R.RuleViolation):                             # a token between the host flags and the script
        _final(raw, [PYTHON, "-I", "-B", "-X", str(root / "a.py")], root, root)
    with pytest.raises(R.RuleViolation):                             # a relative script (an option/subcommand word is possible)
        _final(raw, [PYTHON, "-I", "-B", "a.py"], root, root)
    with pytest.raises(R.RuleViolation):                             # missing the host flags
        _final(raw, [PYTHON, str(root / "a.py")], root, root)
    with pytest.raises(R.RuleViolation):                             # host flags the manifest did not get to supply
        _final(raw, [PYTHON, "-I", "-B", "-S", str(root / "a.py")], root, root)


@pytest.mark.parametrize("raw1", ["+O", "-O", "-c", "~/x", "{path:s}", "{port}"])
def test_f1_final_check_refuses_an_option_home_or_placeholder_script_token(tmp_path, raw1):
    root = tmp_path / "root"
    root.mkdir()
    raw = (PYTHON, raw1, "nullglob", "{path:s}")
    argv = R.harden_argv([PYTHON, raw1, "nullglob", "/host/s"], str(root))
    with pytest.raises(R.RuleViolation):
        _final(raw, argv, root, root, n_fixed=len(argv) - 1)


def test_f1_final_check_refuses_a_program_that_is_not_the_manifests_own_token_and_a_root_binary(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.py").write_text("")
    cp = _copy_of_python(str(root / "python3"))
    raw = (cp, "a.py")
    argv = R.harden_argv(list(raw), str(root))
    with pytest.raises(R.RuleViolation, match="inside the app root"):
        _final(raw, argv, root, root)
    argv2 = [PYTHON, *argv[1:]]                                      # the program swapped for another one
    with pytest.raises(R.RuleViolation):
        _final(raw, argv2, root, root)


@pytest.mark.skipif(not os.path.exists(BASH), reason="no /bin/bash")
def test_f1_final_check_bash_must_carry_exactly_its_host_flags(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "run.sh").write_text("")
    raw = (BASH, "run.sh")
    ok = R.harden_argv(list(raw), str(root))
    assert ok[:3] == [BASH, "--norc", "--noprofile"]
    _final(raw, ok, root, root)
    with pytest.raises(R.RuleViolation):
        _final(raw, [BASH, str(root / "run.sh")], root, root)
    with pytest.raises(R.RuleViolation):
        _final(raw, [BASH, "--norc", str(root / "run.sh")], root, root)


# ====================================================================== R3C-F5: shell startup files
def _start_and_wait(env, a, marker, seconds=10):
    r = env.host.start(a, operator)
    assert r["ok"] is True, r
    deadline = time.time() + seconds
    while not os.path.exists(marker) and time.time() < deadline:
        time.sleep(0.05)
    time.sleep(0.3)


@pytest.mark.skipif(not os.path.exists(ZSH), reason="no zsh")
def test_f5_zsh_does_not_source_the_apps_home_zshenv(env, tmp_path):
    ran, evil = str(tmp_path / "ran"), str(tmp_path / "ZSHENV-RAN")
    mp = _manifest_with(env, "zshapp", start=[ZSH, "run.zsh"], env={"APP_MARK": ran})
    _write(os.path.join(os.path.dirname(mp), "run.zsh"), 'print ok > "$APP_MARK"\nsleep 4\n', 0o644)
    a = env.install(mp)
    home = env.host.paths.app_home(a)
    os.makedirs(home, exist_ok=True)
    _write(os.path.join(home, ".zshenv"), f"print x > {evil}\n")
    _start_and_wait(env, a, ran)
    try:
        assert os.path.exists(ran), "control: the app ran"
        assert not os.path.exists(evil), "the app's HOME/.zshenv was sourced (f8e4ce2/4a40788: it ran)"
    finally:
        env.host.stop(a, operator)


@pytest.mark.skipif(not os.path.exists(ZSH), reason="no zsh")
def test_f5_control_without_the_host_flag_zsh_does_source_zshenv(tmp_path):
    """The instrument can fail: plain `zsh script` with HOME holding a .zshenv runs it."""
    import subprocess

    home = tmp_path / "h"
    home.mkdir()
    (home / ".zshenv").write_text(f"print x > {tmp_path}/Z1\n")
    (tmp_path / "s.zsh").write_text("true\n")
    subprocess.run([ZSH, str(tmp_path / "s.zsh")], env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, check=True)
    assert (tmp_path / "Z1").exists()
    subprocess.run([ZSH, "-f", str(tmp_path / "s.zsh")], env={"HOME": str(home), "PATH": "/usr/bin:/bin"}, check=True)


@pytest.mark.skipif(not os.path.exists(BASH), reason="no bash")
def test_f5_bash_and_sh_do_not_source_home_rc_files_or_the_hosts_bash_env(env, tmp_path, monkeypatch):
    evil = str(tmp_path / "BASH-STARTUP")
    be = tmp_path / "benv.sh"
    be.write_text(f"echo x > {evil}\n")
    monkeypatch.setenv("BASH_ENV", str(be))                       # the HOST's env: must not reach the app (closed env)
    monkeypatch.setenv("ENV", str(be))
    for prog, name in ((BASH, "bashapp"), (SH, "shapp")):
        ran = str(tmp_path / f"ran-{name}")
        mp = _manifest_with(env, name, start=[prog, "run.sh"], env={"APP_MARK": ran})
        _write(os.path.join(os.path.dirname(mp), "run.sh"), 'echo ok > "$APP_MARK"\nsleep 4\n', 0o644)
        a = env.install(mp)
        home = env.host.paths.app_home(a)
        os.makedirs(home, exist_ok=True)
        for f in (".bashrc", ".bash_profile", ".profile", ".bash_login"):
            _write(os.path.join(home, f), f"echo x > {evil}\n")
        _start_and_wait(env, a, ran)
        try:
            assert os.path.exists(ran), name
            assert not os.path.exists(evil), f"{name}: a startup file or BASH_ENV ran"
        finally:
            env.host.stop(a, operator)


# ====================================================================== R3C-F3: the cut can't leave a secret prefix
def test_f3_a_secret_straddling_the_2048_cut_leaves_no_prefix_behind():
    from gnomon.appshost.redact import redact_line

    for start in list(range(1990, 2050)) + [2100]:
        tok = secrets.token_urlsafe(32)                            # 43 url-safe characters
        line = "a " * (start // 2) + ("a" if start % 2 else "") + (" " if start % 2 else "") + tok + " tail " * 40
        assert line.index(tok) in (start, start + 1), (start, line.index(tok))
        out = redact_line(line)
        for k in range(6, 44, 6):
            assert tok[:k] not in out, f"start={start}: {k} chars of the token survive: {out[-80:]!r}"
        assert tok[-12:] not in out
    # a token glued to the left (the audit's 'preceded by a glued character' case) and a long bare run both go
    tok = secrets.token_urlsafe(32)
    out = redact_line("x" * 2030 + tok + " z" * 80)
    assert tok[:8] not in out


def test_f3_the_cut_still_cuts_marks_and_is_linear_with_literal_numbers():
    """R3C-F7(a): a test that derives its input from MAX_LINE_CHARS cannot catch a change to it. Literal numbers."""
    from gnomon.appshost import redact

    assert redact.MAX_LINE_CHARS == 2048
    out = redact.redact_line("word " * 3000)
    assert out.endswith("<line truncated>") and len(out) <= 2048 + len("<line truncated>") + len("<redacted>")
    assert redact.redact_line("short line stays") == "short line stays"
    t0 = time.perf_counter()
    for _ in range(5):
        redact.redact_line("a" * 100000)
    assert (time.perf_counter() - t0) / 5 < 0.01


def test_f3_a_log_window_that_starts_mid_line_drops_its_first_fragment(tmp_path):
    from gnomon.appshost.state import LOG_TAIL_MAX_BYTES, tail_lines

    assert LOG_TAIL_MAX_BYTES == 64 * 1024
    tok = secrets.token_urlsafe(32)
    f = tmp_path / "log"
    # one very long line whose last 23 characters are a token suffix falls at the head of the 64 KiB window
    head = "q" * (70 * 1024)
    f.write_text(head + tok[-23:] + "\nlast line\n")
    got = tail_lines(str(f))
    assert got == ["last line"], got
    # control: a window that starts exactly at a line boundary keeps its first line
    g = tmp_path / "log2"
    g.write_text("p" * (64 * 1024 - 1 - 4) + "\nfirst\nlast\n")
    assert tail_lines(str(g))[-2:] == ["first", "last"]
    # control: a small file is read whole
    h = tmp_path / "log3"
    h.write_text("one\ntwo\n")
    assert tail_lines(str(h)) == ["one", "two"]


# ====================================================================== R3C-F4: the data reads never block
def test_f4_read_json_and_read_audit_on_a_fifo_return_and_do_not_block(tmp_path, env):
    from gnomon.appshost import state

    fifo = str(tmp_path / "j")
    os.mkfifo(fifo)
    box, hung = _call_with_deadline(lambda: state.read_json(fifo))
    _release(fifo)
    assert not hung, "read_json blocked on a FIFO"
    assert isinstance(box.get("e"), ValueError)                    # reads like a corrupt file
    afifo = env.host.paths.audit("aud")
    os.makedirs(os.path.dirname(afifo), exist_ok=True)
    os.mkfifo(afifo)
    box, hung = _call_with_deadline(lambda: state.read_audit(env.host.paths, "aud"))
    _release(afifo)
    assert not hung, "read_audit blocked on a FIFO"
    assert box["v"] and box["v"][0]["outcome"] == "error"
    # controls: a missing file keeps its default, a real file still parses
    assert state.read_json(str(tmp_path / "nope"), default=7) == 7
    (tmp_path / "ok.json").write_text('{"a": 1}')
    assert state.read_json(str(tmp_path / "ok.json")) == {"a": 1}


def test_f4_status_of_a_running_app_whose_start_sig_or_settings_became_a_fifo_does_not_hang(env):
    mp = env.manifest("fifoed", mode="serve", ui_path="/ui")
    a = env.install(mp)
    assert env.host.start(a, operator)["ok"]
    env.wait_status(a, "running")
    for path in (env.host.paths.start_sig(a), env.host.paths.settings(a)):
        saved = path + ".saved"
        os.rename(path, saved)
        os.mkfifo(path)
        try:
            box, hung = _call_with_deadline(lambda: env.host.status(a), seconds=5.0)
            _release(path)
            assert not hung, f"status blocked on a FIFO at {os.path.basename(path)} (4a40788: blocked)"
            box, hung = _call_with_deadline(lambda: env.host.detail(a), seconds=5.0)
            _release(path)
            assert not hung, "detail blocked"
        finally:
            os.unlink(path)
            os.rename(saved, path)
    afifo = env.host.paths.audit(a)
    os.rename(afifo, afifo + ".saved")
    os.mkfifo(afifo)
    try:
        box, hung = _call_with_deadline(lambda: env.host.detail(a), seconds=5.0)
        _release(afifo)
        assert not hung, "detail blocked on the audit FIFO (4a40788: blocked)"
    finally:
        os.unlink(afifo)
        os.rename(afifo + ".saved", afifo)
    env.host.stop(a, operator)


# ====================================================================== R3-F9 (audit): the legacy hub launcher
def test_f9_the_legacy_hub_launcher_never_launches_a_manifest_registered_app_and_the_host_writes_no_entrypoint(env, tmp_path):
    """hub/apps.py's launcher execs [venv python, entrypoint, *args] from a hand-edited whitelist and has no pin or operand
    rule. It is NOT an Apps-tab exec path: an app the Apps host installs is registered as {"manifest": path} and the
    launcher skips such entries; the host never writes an 'entrypoint'. So no manifest-authored argv reaches it."""
    from gnomon.hub.apps import AppRegistry

    a = env.install(env.manifest("legacyskip", with_port=False))
    reg = json.load(open(env.host.paths.registry))["apps"]
    assert set(reg[a]) == {"manifest"} or ("manifest" in reg[a] and "entrypoint" not in reg[a]), reg[a]
    specs, errors = AppRegistry(env.host.paths.registry).load()
    assert a not in specs and a not in errors


# ====================================================================== R3C-F6: spec text
def _spec(name):
    return open(os.path.join(ROOT, "specs", name)).read()


@pytest.mark.skip(reason="needs the internal specs, which are not part of the public cut")
def test_f6_spec_and_checklist_state_the_exact_rule_and_drop_the_false_wording():
    t13 = _spec("13-gnomon-apps-tab.lts")
    t00 = _spec("00-gnomon-overview.lts")
    line = [l for l in t00.split("\n") if l.startswith("- g73-amendment-6-")]
    assert len(line) == 1
    for needle in ("REALPATH", "NEVER argv[0]", "-I -B", "zsh -f", "--norc --noprofile", "ABSOLUTE path", "AFTER the script",
                   "try-list", "R3C-F2", "tests/test_appshost_round3d.py"):
        assert needle in line[0], needle
    req = [l for l in t13.split("\n") if l.startswith("- requirement-argv-operand-rule:")][0]
    for needle in ("(1)", "(2)", "(3)", "(4)", "ABSOLUTE", "NEVER argv[0]", "OUTSIDE the app root", "g73-amendment-6"):
        assert needle in req, needle
    assert "and never as code" not in req
    assert "A program that is neither an allow-listed interpreter nor a file under the app root" not in req
    red = [l for l in t13.split("\n") if l.startswith("- requirement-log-redaction-linear:")][0]
    assert "what is cut cannot leak." not in red and "trailing partial word" in red
    a5 = [l for l in t00.split("\n") if l.startswith("- g73-amendment-5-")][0]
    assert "never follows a flag that makes the interpreter treat it as code" not in a5
    assert "(10) SUPERSEDED by g73-amendment-6" in a5
    assert "must cover every input the pin covers" not in a5 and "does NOT cover the out-of-root interpreter files" in a5
    drift = [l for l in t13.split("\n") if l.startswith("- requirement-running-drift:")][0]
    assert "does NOT cover the out-of-root interpreter files" in drift


# ====================================================================== R3C-F9 (P3 one-liner): url_healthy parses
@pytest.mark.parametrize("url", ["http://127.0.0.1:80@evil.example/", "http://127.0.0.1:1@[::2]:9/", "http://evil.example:1/",
                                 "http://127.0.0.1/", "https://127.0.0.1:9/", "http://user:pw@127.0.0.1:9/", "file:///etc/hosts",
                                 "http://localhost.evil.example:9/"])
def test_f9_url_healthy_refuses_a_url_whose_host_is_not_loopback_without_requesting_it(url, monkeypatch):
    from gnomon.appshost import preflight as PF

    calls = []
    monkeypatch.setattr(PF, "_get_2xx", lambda u, t: calls.append(u) or True)     # would say healthy if it were reached
    assert PF.url_healthy(url, timeout=0.2) is False
    assert calls == [], calls


def test_f9_url_healthy_still_accepts_a_real_loopback_url():
    import http.server

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        from gnomon.appshost import preflight as PF

        assert PF.url_healthy(f"http://127.0.0.1:{srv.server_port}/h") is True
        assert PF.url_healthy(f"http://localhost:{srv.server_port}/h") is True
    finally:
        srv.shutdown()

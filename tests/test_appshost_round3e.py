"""round 3e: the fixes for the round-3d delta Diamond FAIL (audits/audit-apps-tab-r3d-2026-10-03.md, on 5e85d87).

R3D-F1 (P1): every location test compares FILE IDENTITY (st_dev, st_ino), never strings: case variants, the
/System/Volumes/Data firmlink, /private aliases and Unicode spellings all name one identity. R3D-F2: argv[0] must be a
Mach-O executable. R3D-F3: the audit append never blocks on a FIFO. R3D-F4: quoted multi-word and JSON-form secrets.
R3D-F5: two surviving mutants killed (M7, M8) and case-variant / firmlink path tests.

Each test was written to FAIL on 5e85d87 (fail-before in RUN_SUMMARY.md). Hermetic: tmp_path, the launchd double. The
case-variant tests skip (loudly, by name) on a case-sensitive volume."""

import json
import os
import shutil
import stat
import tempfile
import threading
import unicodedata
from types import SimpleNamespace

import pytest

from appshost_support import PYTHON, Env
from test_appshost_round3d import _manifest_with, _write
from gnomon.appshost import rules as R
from gnomon.appshost import fsafe, state
from gnomon.appshost.host import Actor
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


def _variants(path):
    """Spellings of ``path`` that name the same file here (only those that really do, on this volume)."""
    path = os.path.realpath(path)
    out = {"case-swapped": path.swapcase(), "upper": path.upper()}
    if os.path.exists("/System/Volumes/Data" + path):
        out["firmlink"] = "/System/Volumes/Data" + path
        out["firmlink+swapcase"] = "/System/Volumes/Data" + path.swapcase()
    real = R.identity(path)
    ok = {k: v for k, v in out.items() if v != path and R.identity(v) == real}
    if not ok:
        pytest.skip("this volume has no alternative spelling of the path (case-sensitive and no firmlink)")
    return ok


def _copy_binary(dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy(TRUE, dst)
    os.chmod(dst, 0o755)
    return dst


# ======================================================================== R3D-F1: identity, not strings
def test_f1_inside_by_identity_for_every_spelling(tmp_path):
    root = tmp_path / "Root"
    (root / "sub").mkdir(parents=True)
    f = root / "sub" / "python3"
    f.write_text("x")
    for kind, v in _variants(str(root)).items():
        assert R.inside(str(f), v), kind                      # the file against a variant of the reference
        assert R.inside(v + "/sub/python3", str(root)), kind  # a variant of the candidate against the reference
        assert R.inside(v, str(root)), kind                   # equal by identity
        assert not R.inside(str(tmp_path), v), kind
    # a nonexistent child of the root is inside it (judged by its nearest existing ancestor)
    assert R.inside(str(root / "new" / "deeper" / "x"), str(root))
    assert not R.inside(str(tmp_path / "other" / "x"), str(root))


def test_f1_sibling_prefix_is_not_inside(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app2").mkdir()
    assert not R.inside(str(tmp_path / "app2" / "f"), str(tmp_path / "app"))


def test_f1_unicode_normalisation_spellings_are_one_identity(tmp_path):
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    d = tmp_path / nfc
    d.mkdir()
    (d / "f").write_text("x")
    if R.identity(str(tmp_path / nfd)) != R.identity(str(d)):
        pytest.skip("this volume does not equate NFC and NFD spellings")
    assert R.inside(str(tmp_path / nfd / "f"), str(d))
    assert R.inside(str(d / "f"), str(tmp_path / nfd))


def test_f1_final_check_refuses_a_root_binary_spelled_by_a_variant_path(tmp_path):
    root = str(tmp_path / "root")
    prog = _copy_binary(os.path.join(root, "python3"))
    _write(os.path.join(root, "app.py"), "pass\n")
    for kind, v in _variants(root).items():
        argv0 = v + "/python3"
        argv = [argv0, "-I", "-B", os.path.join(root, "app.py")]
        with pytest.raises(R.RuleViolation) as ei:
            R.final_check(argv, raw=(argv0, "app.py"), n_fixed=4, cwd=root, root=root, where="start")
        assert "inside the app root" in str(ei.value), (kind, str(ei.value))


def test_f1_home_scratch_and_firmlink_spellings_of_the_interpreter_are_refused(tmp_path, monkeypatch):
    home = tmp_path / "home"
    root = str(tmp_path / "root")
    os.makedirs(root)
    _write(os.path.join(root, "app.py"), "pass\n")
    monkeypatch.setenv("HOME", str(home))
    py = _copy_binary(str(home / "bin" / "python3"))
    scratch = tempfile.mkdtemp(dir="/private/tmp")
    try:
        sp = _copy_binary(os.path.join(scratch, "python3"))
        cases = []
        for kind, v in _variants(str(home / "bin")).items():
            cases.append((f"home/{kind}", v + "/python3", "under the user's home"))
        for kind, v in _variants(scratch).items():
            cases.append((f"scratch/{kind}", v + "/python3", "scratch directory"))
        cases.append(("scratch/PRIVATE-TMP", "/PRIVATE/TMP/" + os.path.basename(scratch) + "/python3", "scratch directory"))
        cases.append(("scratch/tmp-alias", "/tmp/" + os.path.basename(scratch) + "/python3", "scratch directory"))
        for label, argv0, why in cases:
            if not os.path.exists(argv0):
                continue
            argv = [argv0, "-I", "-B", os.path.join(root, "app.py")]
            with pytest.raises(R.RuleViolation) as ei:
                R.final_check(argv, raw=(argv0, "app.py"), n_fixed=4, cwd=root, root=root, where="start")
            assert why in str(ei.value), (label, str(ei.value))
        assert len(cases) >= 3
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_f1_end_to_end_propose_refuses_the_case_variant_root_python(env):
    """The audit's end-to-end repro: a root file named python3 spelled with the wrong case as start[0]."""
    mp = _manifest_with(env, "cv")
    d = os.path.dirname(mp)
    _copy_binary(os.path.join(d, "python3"))
    for kind, v in _variants(d).items():
        mp = _manifest_with(env, "cv", start=[v + "/python3", "app.py"])
        r = env.host.propose_install(mp, AG)
        assert r["ok"] is False and r["field"] == "start[0]", (kind, r)


def test_f1_writes_path_check_uses_identity(tmp_path):
    root = str(tmp_path / "root")
    os.makedirs(os.path.join(root, "data"))
    m = SimpleNamespace(paths=[SimpleNamespace(name="w", access="writes")])
    assert R.check_writes_outside_root(m, {"w": os.path.join(root, "data")}, root)
    for kind, v in _variants(root).items():
        assert R.check_writes_outside_root(m, {"w": v + "/data"}, root), kind
        assert R.check_writes_outside_root(m, {"w": v + "/data/not-yet"}, root), kind
        assert R.check_writes_outside_root(m, {"w": str(tmp_path)}, v), kind          # the root inside the path
    assert R.check_writes_outside_root(m, {"w": str(tmp_path / "elsewhere")}, root) is None


def test_f1_no_string_path_comparison_is_left_in_appshost():
    """Every location decision goes through rules.inside. The only os.sep / commonpath uses left are PROVEN not to be
    location decisions: host.py (building an exe path from a manifest token), launch.py (same), pins.py (splitting a
    relative path into parts to skip secret-ish names) and rules.inside's own fallback for a missing reference."""
    base = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "appshost")
    hits = {}
    for fn in sorted(os.listdir(base)):
        if fn.endswith(".py"):
            for line in open(os.path.join(base, fn)):
                if ("os.sep" in line or "commonpath" in line) and not line.lstrip().startswith("#"):
                    hits.setdefault(fn, []).append(line.strip())
    # round 3f: host.py and launch.py no longer build an exe path (they exec rules.exec_path, the judged realpath)
    assert sorted(hits) == ["pins.py", "rules.py"], hits
    assert all(len(v) == 1 for k, v in hits.items() if k != "rules.py"), hits
    assert "split(os.sep)" in hits["pins.py"][0]
    assert len(hits["rules.py"]) == 1 and "rr.rstrip(os.sep)" in hits["rules.py"][0], hits["rules.py"]


def test_f1_pins_and_host_use_the_identity_helper(tmp_path):
    from gnomon.appshost import pins
    assert pins._inside is R.inside
    root = tmp_path / "src"
    root.mkdir()
    v = next(iter(_variants(str(root)).values()))
    h = SimpleNamespace(_source_roots=(str(root),))
    from gnomon.appshost.host import AppsHost
    assert AppsHost._inside_source(h, v + "/x/y")


# ======================================================================== R3D-F2: Mach-O only
@pytest.mark.parametrize("magic", ["feedface", "feedfacf", "cefaedfe", "cffaedfe", "cafebabe", "bebafeca", "cafebabf",
                                   "bfbafeca"])
def test_f2_every_macho_and_universal_magic_is_accepted(tmp_path, magic):
    """Round 3f: a magic alone is not enough (is_macho also checks header plausibility); each magic is accepted with a
    plausible header behind it (built by the round-3f helper)."""
    from test_appshost_round3f import macho_bytes
    p = tmp_path / "bin"
    p.write_bytes(macho_bytes(magic))
    assert R.is_macho(str(p))


@pytest.mark.parametrize("content", [b"#!/bin/sh\nexit 0\n", b"#!/usr/bin/env python3\n", b"", b"abc", b"\x7fELF\x02\x01",
                                     b"MZ\x90\x00", b"\xfe\xed\xfa"])
def test_f2_scripts_text_and_short_files_are_not_macho(tmp_path, content):
    p = tmp_path / "python3"
    p.write_bytes(content)
    assert not R.is_macho(str(p))


def test_f2_real_system_binaries_and_a_fifo():
    for p in ("/bin/sh", "/usr/bin/true", PYTHON):
        assert R.is_macho(p), p
    assert not R.is_macho("/nonexistent/python3")


def test_f2_a_fifo_is_not_macho_and_does_not_hang(tmp_path):
    f = str(tmp_path / "python3")
    os.mkfifo(f)
    box = {}
    t = threading.Thread(target=lambda: box.setdefault("v", R.is_macho(f)), daemon=True)
    t.start()
    t.join(4)
    assert not t.is_alive() and box["v"] is False


@pytest.mark.parametrize("name", ["python3", "sh", "bash", "node", "perl", "ruby", "python3.13"])
def test_f2_a_script_named_like_an_interpreter_is_refused_wherever_it_lives(tmp_path, monkeypatch, name):
    """Location rules neutralised (no scratch list, no homes): ONLY the Mach-O rule can refuse a #! script."""
    monkeypatch.setattr(R, "_SCRATCH_DIRS", ())
    root = str(tmp_path / "root")
    os.makedirs(root)
    _write(os.path.join(root, "app.py"), "pass\n")
    prog = str(tmp_path / "elsewhere" / name)
    _write(prog, "#!/bin/sh\nexit 0\n", 0o755)
    flags = list(R.host_flags(prog, root))
    argv = [prog, *flags, os.path.join(root, "app.py")]
    with pytest.raises(R.RuleViolation) as ei:
        R.operand_rule(argv, raw=(prog, "app.py"), n_fixed=len(flags) + 2, cwd=root, root=root, where="start", homes=[])
    assert "not a Mach-O" in str(ei.value) and ei.value.field == "start[0]"


def test_f2_a_macho_outside_every_location_rule_is_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(R, "_SCRATCH_DIRS", ())
    root = str(tmp_path / "root")
    os.makedirs(root)
    _write(os.path.join(root, "app.py"), "pass\n")
    prog = _copy_binary(str(tmp_path / "elsewhere" / "python3"))
    argv = [prog, "-I", "-B", os.path.join(root, "app.py")]
    R.operand_rule(argv, raw=(prog, "app.py"), n_fixed=4, cwd=root, root=root, where="start", homes=[])


# ======================================================================== R3D-F3: the audit append never blocks
def _deadline(fn, seconds=4.0):
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
        os.close(os.open(fifo, os.O_RDONLY | os.O_NONBLOCK))
    except OSError:
        pass


def test_f3_open_append_fd_refuses_a_fifo_without_a_reader_and_with_one(tmp_path):
    f = str(tmp_path / "audit.jsonl")
    os.mkfifo(f)
    box, hung = _deadline(lambda: fsafe.open_append_fd(f))
    assert not hung and isinstance(box.get("e"), OSError), box          # ENXIO, at once
    rd = os.open(f, os.O_RDONLY | os.O_NONBLOCK)                        # now a reader exists
    try:
        box, hung = _deadline(lambda: fsafe.open_append_fd(f))
        assert not hung and isinstance(box.get("e"), fsafe.NotRegularFile), box
    finally:
        os.close(rd)


def test_f3_open_append_fd_appends_to_a_regular_file_and_refuses_symlink(tmp_path):
    f = str(tmp_path / "a.log")
    for text in (b"one\n", b"two\n"):
        fd = fsafe.open_append_fd(f)
        os.write(fd, text)
        os.close(fd)
    assert open(f, "rb").read() == b"one\ntwo\n"
    assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
    os.symlink(f, str(tmp_path / "link"))
    with pytest.raises(OSError):
        fsafe.open_append_fd(str(tmp_path / "link"))
    fd = fsafe.open_append_fd(f, truncate=True)
    os.close(fd)
    assert open(f, "rb").read() == b""


def test_f3_append_audit_on_a_fifo_fails_loudly_and_never_hangs(env):
    paths = env.host.paths
    p = paths.audit("zz")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    os.mkfifo(p)
    box, hung = _deadline(lambda: state.append_audit(paths, "zz", verb="stop", actor=VIEWER_ACTOR, actor_verified=False,
                                                     outcome="ok"))
    _release(p)
    assert not hung and isinstance(box.get("e"), OSError), box
    hp = os.path.join(paths.apps, state.AUDIT_FILENAME)
    os.makedirs(paths.apps, exist_ok=True)
    os.mkfifo(hp)
    box, hung = _deadline(lambda: state.append_host_audit(paths, verb="app_list", actor=VIEWER_ACTOR, actor_verified=False,
                                                          outcome="ok"))
    _release(hp)
    assert not hung and isinstance(box.get("e"), OSError), box


def test_f3_stop_with_a_fifo_swapped_in_for_audit_returns(env):
    mp = env.manifest("ff", with_port=False, mode="sleep")
    a = env.install(mp)
    env.host.start(a, operator)
    env.wait_status(a, ("running", "starting"))
    p = env.host.paths.audit(a)
    if os.path.exists(p):
        os.unlink(p)
    os.mkfifo(p)
    box, hung = _deadline(lambda: env.host.stop(a, operator))
    _release(p)
    assert not hung, "stop hung on the audit FIFO"
    os.unlink(p)
    env.host.stop(a, operator) if "e" in box else None


def test_f3_snapshot_write_never_blocks_on_a_fifo(tmp_path):
    from gnomon.appshost import pins
    d = str(tmp_path / "snap")
    os.makedirs(d)
    f = os.path.join(d, "x.txt")
    os.mkfifo(f)
    fd_box, hung = _deadline(lambda: fsafe.open_append_fd(f, truncate=True))
    assert not hung and isinstance(fd_box.get("e"), OSError)


# ======================================================================== R3D-F4: quoted and JSON-form secrets
_NAMES = ["token", "secret", "password", "passwd", "api_key", "apikey", "authorization", "credential", "bearer"]


@pytest.mark.parametrize("name", _NAMES)
def test_f4_json_form_short_secrets_are_redacted(name):
    for line in ('{"%s": "hunter2"}' % name, '{"%s":"hunter2"}' % name, '{"%s" : "hunter2", "x": 1}' % name,
                 "{'%s': 'hunter2'}" % name, '{"%s": hunter2}' % name):
        out = redact_line(line)
        assert "hunter2" not in out and "<redacted>" in out, (line, out)


@pytest.mark.parametrize("name", _NAMES)
def test_f4_quoted_multi_word_values_are_redacted_whole(name):
    for q in ('"', "'"):
        out = redact_line(f"{name}={q}correct horse battery staple{q} tail")
        assert not any(w in out for w in ("correct", "horse", "battery", "staple")), out
        assert "tail" in out


@pytest.mark.parametrize("pad", [1990, 2000, 2020, 2040])
@pytest.mark.parametrize("form", ['password="correct horse battery staple"', 'password: "correct horse battery staple"',
                                  '{"password": "correct horse battery staple"}',
                                  "secret='correct horse battery staple'"])
def test_f4_cut_at_every_column_leaves_no_word_of_a_quoted_secret(pad, form):
    """Secret start columns pad-50 .. pad+200: the 2048 cut lands at every offset of the keyword, the quote and the words."""
    for start in range(pad - 50, pad + 200):
        line = "x" * (start - 1) + " " + form + " tail words after"
        out = redact_line(line)
        for w in ("correct", "horse", "battery", "staple"):
            assert w not in out, (start, form, out[-80:])
        for part in ("correc", "orse", "batte", "stapl"):
            assert part not in out, (start, form, out[-80:])


def test_f4_an_unterminated_quote_redacts_to_the_end_of_the_line():
    out = redact_line('password="correct horse battery')
    assert out == "password=<redacted>"
    assert redact_line("token='a b c d") == "token=<redacted>"


def test_f4_redaction_stays_linear_on_quote_heavy_input():
    import time
    for s in ('token="' * 400, "password='" * 400, 'token":"' * 300 + "x", '"' * 2000):
        t0 = time.monotonic()
        redact_line(s)
        assert time.monotonic() - t0 < 0.5


# ======================================================================== R3D-F5: the two surviving mutants
def _shape_call(argv, raw, n_fixed, root, **kw):
    R.operand_rule(argv, raw=raw, n_fixed=n_fixed, cwd=root, root=root, where="start", homes=[], **kw)


def test_m7_a_plus_script_token_is_refused_by_the_operand_rule_itself(tmp_path):
    """M7 (the leading-+ check shadowed by is-a-file and by manifest.check_argv_shape): a root FILE named +O, the
    operand rule called directly, so no other layer can refuse it."""
    root = str(tmp_path / "root")
    _write(os.path.join(root, "+O"), "x\n")
    argv = [PYTHON, "-I", "-B", os.path.join(root, "+O")]
    with pytest.raises(R.RuleViolation) as ei:
        _shape_call(argv, (PYTHON, "+O"), 4, root)
    assert ei.value.field == "start[1]" and "literal file name" in str(ei.value), str(ei.value)
    for tok in ("-x", "~x", "+x"):
        _write(os.path.join(root, tok), "x\n")
        with pytest.raises(R.RuleViolation) as ei:
            _shape_call([PYTHON, "-I", "-B", os.path.join(root, tok)], (PYTHON, tok), 4, root)
        assert "literal file name" in str(ei.value), (tok, str(ei.value))


def test_m8_host_flags_in_the_wrong_order_or_missing_are_refused_by_their_own_check(tmp_path):
    """M8 (the exactly-the-host-flags check shadowed by the script-position check): swapped flags put the script in
    the right position, so only the flags check can refuse them."""
    root = str(tmp_path / "root")
    _write(os.path.join(root, "app.py"), "pass\n")
    script = os.path.join(root, "app.py")
    for bad in ([PYTHON, "-B", "-I", script], [PYTHON, "-I", "-I", script], [PYTHON, "-B", "-B", script],
                [PYTHON, "-I", "-E", script]):
        with pytest.raises(R.RuleViolation) as ei:
            _shape_call(bad, (PYTHON, "app.py"), 4, root)
        assert "exactly the host's own flags" in str(ei.value), (bad, str(ei.value))
    _shape_call([PYTHON, "-I", "-B", script], (PYTHON, "app.py"), 4, root)           # the control passes

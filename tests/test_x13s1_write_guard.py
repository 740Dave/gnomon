"""X13 S1: the write guard and the fingerprint tool, unit level (I8), each able to FAIL.

1. the audit-hook write guard raises on every write/spawn/kill/connect/send/bind class outside scratch
   (plus sqlite3.connect, os.mkfifo and os.mknod: S1 Diamond F3);
2. the fingerprint tool DOES detect add/remove/change (so 'unchanged' is meaningful).

The integration half (``--out`` refusals, a full run leaving every live root identical, the guard wired into
the real CLI path) runs in the deployment's own tests (DEPLOY workspaces/<ws>/converter/tests) and, against a
neutral world, in tests/test_x13s1_engine_neutral.py."""

import json
import os
import shutil
import socket
import subprocess

import pytest

from gnomon.workspace import fingerprint as FP, writeguard as WG


@pytest.fixture()
def scratch(tmp_path):
    d = tmp_path / "scratch"
    d.mkdir()
    return d


def test_guard_allows_writes_inside_scratch(scratch):
    with WG.scratch_only(str(scratch)):
        (scratch / "sub").mkdir()
        (scratch / "sub" / "a.txt").write_text("x")
        os.rename(scratch / "sub" / "a.txt", scratch / "sub" / "b.txt")
        os.remove(scratch / "sub" / "b.txt")
    assert (scratch / "sub").is_dir()


def test_guard_is_inert_outside_a_context(tmp_path):
    (tmp_path / "free.txt").write_text("fine")          # no context active: nothing raises
    assert WG.active_root() is None


def test_guard_blocks_write_open_outside_scratch(scratch, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("seed")
    with WG.scratch_only(str(scratch)):
        for mode in ("w", "a", "r+", "wb"):
            with pytest.raises(WG.WriteGuardViolation):
                open(victim, mode)
        with pytest.raises(WG.WriteGuardViolation):
            open(tmp_path / "new.txt", "x")
    assert not (tmp_path / "new.txt").exists()
    assert victim.read_text() == "seed"


def test_guard_allows_reading_outside_scratch(scratch, tmp_path):
    src = tmp_path / "live.txt"
    src.write_text("live")
    with WG.scratch_only(str(scratch)):
        assert open(src).read() == "live"


def test_guard_blocks_mkdir_remove_rename_truncate_chmod_symlink_outside(scratch, tmp_path):
    victim = tmp_path / "v.txt"
    victim.write_text("seed-data")
    (scratch / "in.txt").write_text("in")
    with WG.scratch_only(str(scratch)):
        with pytest.raises(WG.WriteGuardViolation):
            os.mkdir(tmp_path / "newdir")
        with pytest.raises(WG.WriteGuardViolation):
            os.remove(victim)
        with pytest.raises(WG.WriteGuardViolation):
            os.rename(victim, tmp_path / "moved.txt")
        with pytest.raises(WG.WriteGuardViolation):
            os.rename(scratch / "in.txt", tmp_path / "escaped.txt")      # out of scratch
        with pytest.raises(WG.WriteGuardViolation):
            os.truncate(victim, 0)
        with pytest.raises(WG.WriteGuardViolation):
            os.chmod(victim, 0o777)
        with pytest.raises(WG.WriteGuardViolation):
            os.symlink(str(scratch / "in.txt"), str(tmp_path / "lnk"))
        with pytest.raises(WG.WriteGuardViolation):
            shutil.copyfile(scratch / "in.txt", tmp_path / "copy.txt")
        with pytest.raises(WG.WriteGuardViolation):
            shutil.rmtree(tmp_path / "newdir-not-there")
    assert victim.read_text() == "seed-data" and not (tmp_path / "escaped.txt").exists()
    assert (scratch / "in.txt").exists()


def test_guard_blocks_spawn_signal_and_connect(scratch):
    with WG.scratch_only(str(scratch)):
        with pytest.raises(WG.WriteGuardViolation):
            subprocess.Popen(["true"])
        with pytest.raises(WG.WriteGuardViolation):
            os.system("true")
        with pytest.raises(WG.WriteGuardViolation):
            os.kill(os.getpid(), 0)              # never signal anything, :8770 included
        s = socket.socket()
        try:
            with pytest.raises(WG.WriteGuardViolation):
                s.connect(("127.0.0.1", 9))
        finally:
            s.close()


# ---- F3 (S1 Diamond 2026-09-30): sqlite-for-write, mkfifo/mknod, UDP/datagram sends ----------

def test_guard_blocks_sqlite_connections_opened_for_write_outside_scratch(scratch, tmp_path):
    import sqlite3

    live = tmp_path / "live.sqlite"
    with WG.scratch_only(str(scratch)):
        with pytest.raises(WG.WriteGuardViolation, match="sqlite3.connect"):
            sqlite3.connect(str(live)).execute("create table t(a)")
        # a read-only URI is a read: allowed (the path need not exist for the connect to be judged)
        seed = tmp_path / "seed.sqlite"
    sqlite3.connect(str(seed)).execute("create table t(a)")
    with WG.scratch_only(str(scratch)):
        sqlite3.connect(f"file:{seed}?mode=ro", uri=True).execute("select * from t").fetchall()
        # read-write / default-mode URIs are NOT reads
        with pytest.raises(WG.WriteGuardViolation, match="sqlite3.connect"):
            sqlite3.connect(f"file:{seed}?mode=rw", uri=True)
        # in scratch and in-memory are fine
        c = sqlite3.connect(str(scratch / "in.sqlite"))
        c.execute("create table t(a)")
        c.close()
        sqlite3.connect(":memory:").close()
    assert not live.exists()


def test_guard_blocks_mkfifo_and_mknod_outside_scratch(scratch, tmp_path):
    # the interpreter emits NO audit event for os.mkfifo, so the guard wraps it for the context's life
    original = os.mkfifo
    with WG.scratch_only(str(scratch)):
        with pytest.raises(WG.WriteGuardViolation, match="mkfifo"):
            os.mkfifo(str(tmp_path / "fifo"))
        with pytest.raises(WG.WriteGuardViolation, match="mknod"):
            os.mknod(str(tmp_path / "node"))
        os.mkfifo(str(scratch / "ok-fifo"))                 # in scratch: allowed
    assert not (tmp_path / "fifo").exists() and not (tmp_path / "node").exists()
    assert (scratch / "ok-fifo").exists()
    assert os.mkfifo is original, "the guard must restore os.mkfifo when the context ends"
    os.mkfifo(str(tmp_path / "free-fifo"))                   # inert again outside a context


def test_guard_blocks_datagram_sends_and_binds(scratch):
    import tempfile

    short = tempfile.mkdtemp(prefix="wg", dir="/tmp")          # AF_UNIX paths are limited to ~100 bytes
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(0.3)
    try:
        with WG.scratch_only(str(scratch)):
            tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                with pytest.raises(WG.WriteGuardViolation, match="sendto"):
                    tx.sendto(b"x", rx.getsockname())      # UDP with no connect()
                with pytest.raises(WG.WriteGuardViolation, match="sendmsg"):
                    tx.sendmsg([b"x"], [], 0, rx.getsockname())
            finally:
                tx.close()
            ux = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                with pytest.raises(WG.WriteGuardViolation, match="bind"):
                    ux.bind(os.path.join(short, "s"))            # would create a socket file
            finally:
                ux.close()
        with pytest.raises(socket.timeout):
            rx.recv(10)                                     # nothing ever arrived
    finally:
        rx.close()
        gone = not os.path.exists(os.path.join(short, "s"))
        shutil.rmtree(short)
    assert gone


def test_guard_docstring_states_its_real_coverage_and_its_limit():
    doc = WG.__doc__
    assert "ANY write-intent filesystem operation" not in doc, "the docstring overclaimed (F3)"
    for term in ("sqlite3.connect", "mkfifo", "sendto", "NOT covered"):
        assert term in doc, term


def test_guard_blocks_a_symlink_escape(scratch, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (scratch / "door").symlink_to(outside)
    with WG.scratch_only(str(scratch)):
        with pytest.raises(WG.WriteGuardViolation):
            open(scratch / "door" / "leak.txt", "w")
    assert not (outside / "leak.txt").exists()


def test_guard_contexts_nest_and_restore(scratch, tmp_path):
    inner = scratch / "inner"
    inner.mkdir()
    with WG.scratch_only(str(scratch)):
        with WG.scratch_only(str(inner)):
            with pytest.raises(WG.WriteGuardViolation):
                open(scratch / "top.txt", "w")
        (scratch / "top.txt").write_text("ok now")
    assert WG.active_root() is None


# ---- out refusal -----------------------------------------------------------------------------







# ---- fingerprints --------------------------------------------------------------------------

def test_fingerprint_detects_add_remove_change(tmp_path):
    r = tmp_path / "root"
    (r / "d").mkdir(parents=True)
    (r / "d" / "f.txt").write_text("one")
    a = FP.fingerprint([str(r)])
    (r / "d" / "f.txt").write_text("two-longer")
    (r / "d" / "new.txt").write_text("n")
    b = FP.fingerprint([str(r)])
    d = FP.diff(a, b)
    assert str(r / "d" / "f.txt") in d["changed"] and str(r / "d" / "new.txt") in d["added"]
    (r / "d" / "new.txt").unlink()
    c = FP.fingerprint([str(r)])
    assert str(r / "d" / "new.txt") in FP.diff(b, c)["removed"]
    assert FP.diff(a, a) == {"added": [], "removed": [], "changed": []}


def test_fingerprint_classify_splits_explained_from_unexplained():
    d = {"added": [], "removed": [], "changed": ["/x/sessions/a/log", "/x/other"]}
    c = FP.classify(d, ["/x/sessions/"])
    assert c["explained"]["changed"] == ["/x/sessions/a/log"] and c["unexplained"]["changed"] == ["/x/other"]


def test_fingerprint_cli_roundtrip(tmp_path):
    r = tmp_path / "root"
    r.mkdir()
    (r / "a").write_text("1")
    s1, s2 = tmp_path / "s1.json", tmp_path / "s2.json"
    assert FP.main(["snap", str(s1), str(r)]) == 0
    assert FP.main(["snap", str(s2), str(r)]) == 0
    assert FP.main(["compare", str(s1), str(s2)]) == 0
    (r / "b").write_text("2")
    assert FP.main(["snap", str(s2), str(r)]) == 0
    assert FP.main(["compare", str(s1), str(s2)]) == 1


def test_fingerprint_cli_refuses_a_vacuous_snapshot_of_a_missing_root(tmp_path, capsys):
    """Found live: an unquoted multi-root shell variable made `snap` fingerprint ONE non-existent 'root' and
    report success, so a before/after comparison proved nothing. A missing root is now a loud stop."""
    out = tmp_path / "s.json"
    assert FP.main(["snap", str(out), str(tmp_path / "no-such-root")]) == 2
    assert "vacuous" in capsys.readouterr().out and not out.exists()
    assert FP.main(["snap", str(out), "--allow-missing", str(tmp_path / "no-such-root")]) == 0 and out.exists()


# ---- the end-to-end no-writes proof ----------------------------------------------------------

def _tree_hash(roots):
    return FP.fingerprint(roots)







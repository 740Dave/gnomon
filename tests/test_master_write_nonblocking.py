"""the master-side write never blocks the relay loop.

The defect (measured 2026-10-27 on a real relaunch): the harness's one master-side write looped a BLOCKING ``os.write`` until every
byte was sent, on the same thread that relays claude's output. An injection longer than the PTY input buffer, written before claude
read its input, blocked once the buffer was full; the blocked harness could not drain claude's output, so claude blocked on its own
output write: a deadlock.

The ruling (flag ``master_write_nonblocking``, registry default OFF):

1. flag ON: the master fd is non-blocking; the writer appends to ONE ordered pending buffer and writes only what the master accepts; the
   pump selects the master for writability while the buffer is non-empty and keeps reading claude's output; EAGAIN is not an error;
   EIO / EBADF still propagate (the session ends as before);
2. flag ON: a harness injection issued before claude has produced its first output is held until it has; operator keystrokes never are;
3. an injection is logged at enqueue (the record carries the length) and a deferred flush is recorded when it completes.

Every test that needs the new path goes through :func:`_nb` so that, run against the tree WITHOUT the fix, the deadlock tests fail by the
DEADLOCK (a timeout), not by an ``unexpected keyword`` error: they were seen red that way before the fix was written.
"""
import errno
import fcntl
import hashlib
import inspect
import os
import select
import signal
import sys
import threading
import time

import pytest

from gnomon import flags as F
from gnomon.harness import HarnessProxy, PtySpawner, SingleWriteSite, sanitize_env
from gnomon.harness.hooks import SOURCE_MONITOR_GUIDANCE, InjectionGate
from gnomon.harness.injector import Injector
from gnomon.harness.spawn import SpawnResult

FLAG = "master_write_nonblocking"


def _nb():
    """kwargs that ask the writer for the non-blocking mode, or {} on a tree that has no such mode (the red run)."""
    return {"nonblocking": True} if "nonblocking" in inspect.signature(SingleWriteSite.__init__).parameters else {}


#: a child that behaves like claude at start: raw tty, then a LOT of blocking output before it ever reads its input.
_CHILD = r"""
import hashlib, os, sys, time, tty
chatter, need, delay = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
time.sleep(delay)                   # silent start: nothing written, nothing read, tty still cooked
tty.setraw(0)                       # TCSAFLUSH: input typed before this point is DISCARDED
os.write(1, b"R")
chunk = b"C" * 1024
sent = 0
while sent < chatter:               # blocking writes: they block the moment nobody drains the master
    os.write(1, chunk)
    sent += len(chunk)
got = b""
while len(got) < need:
    d = os.read(0, 65536)
    if not d:
        break
    got += d
os.write(1, b"\nGOT:" + str(len(got)).encode() + b":" + hashlib.sha256(got).hexdigest().encode() + b":" + got.hex().encode() + b":END\n")
time.sleep(0.2)
"""

PAYLOAD = "".join(hashlib.sha256(str(i).encode()).hexdigest() for i in range(80))[:4096]   # 4 KB, printable, no leading '!'
CHATTER = 256 * 1024


def _spawn(chatter=CHATTER, need=len(PAYLOAD), delay=0.0):
    env = sanitize_env(overrides={"TERM": "xterm-256color"})
    return PtySpawner().spawn([sys.executable, "-c", _CHILD, str(chatter), str(need), str(delay)], env=env)


def _parse_got(blob):
    i = blob.find(b"\nGOT:")
    j = blob.find(b":END", i)
    if i < 0 or j < 0:
        return None
    n, sha, hexed = blob[i + 5:j].split(b":", 2)
    return int(n), sha.decode(), bytes.fromhex(hexed.decode())


class _Rig:
    """A real pty child, a real HarnessProxy, and a worker thread that does what the launcher does at start: inject, then pump."""

    def __init__(self, tmp_path):
        self.out_path = str(tmp_path / "harness_stdout.bin")
        self.out_fd = os.open(self.out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.stop = threading.Event()
        self.errors = []
        self.thread = None
        self.res = None
        self.pre = b""

    def start(self, res, work):
        self.res = res
        self.thread = threading.Thread(target=self._run, args=(work,), daemon=True)
        self.thread.start()

    def _run(self, work):
        try:
            work(self.stop)
        except BaseException as exc:  # noqa: BLE001 - reported by the test
            self.errors.append(exc)

    def output(self):
        with open(self.out_path, "rb") as fh:
            return self.pre + fh.read()

    def wait_got(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            blob = self.output()
            if _parse_got(blob) is not None:
                return blob
            time.sleep(0.05)
        return self.output()

    def close(self):
        self.stop.set()
        if self.res is not None:
            try:
                os.kill(self.res.pid, signal.SIGKILL)   # a blocked master write wakes with EIO once the slave is gone
            except OSError:
                pass
            try:
                os.waitpid(self.res.pid, 0)
            except OSError:
                pass
        if self.thread is not None:
            self.thread.join(5)
        for fd in (self.out_fd, getattr(self.res, "master_fd", None)):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _wait_ready(res):
    """Read until the child's 'R' (raw mode is set) so the injection cannot race the tty mode; keep what was read."""
    got = b""
    end = time.monotonic() + 10
    while b"R" not in got and time.monotonic() < end:
        r, _, _ = select.select([res.master_fd], [], [], 0.2)
        if r:
            got += os.read(res.master_fd, 4096)
    assert b"R" in got, "the fake child never became ready"
    return got


# -- the deadlock, one level down: the writer + proxy on a real pty ---------------------------------------------------------------


def run_writer_level_once(tmp_path):
    res = _spawn()
    rig = _Rig(tmp_path)
    rig.pre = _wait_ready(res)
    writer = SingleWriteSite(res.master_fd, **_nb())
    proxy = HarnessProxy(master_fd=res.master_fd, pid=res.pid, writer=writer, stdin_fd=None, stdout_fd=rig.out_fd,
                         poll_interval=0.05)

    def work(stop):
        writer.inject(PAYLOAD.encode())           # the 4 KB injection at start, before the child reads anything
        while not stop.is_set() and proxy.child_alive:
            proxy.step(0.05)

    rig.start(res, work)
    try:
        blob = rig.wait_got(10.0)
    finally:
        rig.close()
    return blob, rig


def test_a_4kb_injection_at_start_does_not_deadlock_a_child_that_writes_first(tmp_path):
    blob, rig = run_writer_level_once(tmp_path)
    got = _parse_got(blob)
    assert got is not None, ("DEADLOCK: no injection reached the child within 10 s (the child's output reached the harness stdout: "
                             f"{blob.count(b'C')} of {CHATTER} bytes; worker errors: {rig.errors})")
    n, sha, data = got
    assert data == PAYLOAD.encode(), "the child did not receive the injection whole and in order"
    assert blob.count(b"C") == CHATTER, "the child's output did not all reach the harness's stdout"
    assert not rig.errors, rig.errors


def test_the_deadlock_test_is_able_to_fail(tmp_path):
    """Control: the SAME rig with a BLOCKING writer (the flag-OFF writer) deadlocks on this pty. If it did not, the test above would be
    a check that never failed. (macOS: the master accepts about 1 KB before it blocks; Linux ptys take more, hence the 4 KB payload.)"""
    res = _spawn()
    rig = _Rig(tmp_path)
    rig.pre = _wait_ready(res)
    writer = SingleWriteSite(res.master_fd)       # blocking: the pre-fix behaviour, by construction
    proxy = HarnessProxy(master_fd=res.master_fd, pid=res.pid, writer=writer, stdin_fd=None, stdout_fd=rig.out_fd,
                         poll_interval=0.05)

    def work(stop):
        writer.inject(PAYLOAD.encode())
        while not stop.is_set() and proxy.child_alive:
            proxy.step(0.05)

    rig.start(res, work)
    try:
        blob = rig.wait_got(3.0)
    finally:
        rig.close()
    assert _parse_got(blob) is None, "the blocking writer did NOT deadlock on this machine: the deadlock tests prove nothing here"


def test_deadlock_soak(tmp_path):
    """A concurrency claim needs N >= 30: GNOMON_SOAK_N fresh pty children, each with its own directory; every one must pass."""
    n = int(os.environ.get("GNOMON_SOAK_N", "3"))
    failures = []
    for i in range(n):
        d = tmp_path / f"run{i}"
        d.mkdir()
        blob, rig = run_writer_level_once(d)
        got = _parse_got(blob)
        if got is None or got[2] != PAYLOAD.encode() or blob.count(b"C") != CHATTER or rig.errors:
            failures.append(i)
    print(f"SOAK n={n} passed={n - len(failures)} failed={failures}")
    assert not failures, f"{len(failures)} of {n} runs deadlocked or lost bytes: {failures}"


# -- the deadlock end to end: assemble_proxy, flag ON, an injection through the gate before the child's first output -------------------


def run_assembled_once(tmp_path, delay=0.5):
    from gnomon.harness.launcher import assemble_proxy

    res = _spawn(delay=delay)
    rig = _Rig(tmp_path)
    sess = tmp_path / "sess"
    (sess / ".claude").mkdir(parents=True)
    proxy = assemble_proxy(res, stdin_fd=None, stdout_fd=rig.out_fd, session_dir=str(sess), instance_id="mwnb",
                           harness={FLAG: True})
    proxy._injector._sleep = lambda _s: None
    outcome = {}

    def work(stop):
        # what the launcher's start does: a harness reminder is issued BEFORE the child has said anything
        outcome["o"] = proxy._injector.inject_line(PAYLOAD, clear=False, source=SOURCE_MONITOR_GUIDANCE, trigger="start-reminder")
        while not stop.is_set() and proxy.child_alive:
            proxy.step(0.05)

    rig.start(res, work)
    try:
        blob = rig.wait_got(15.0)
    finally:
        rig.close()
    return blob, rig, proxy, outcome


def test_assembled_harness_holds_a_start_injection_until_first_output_and_does_not_deadlock(tmp_path):
    blob, rig, proxy, outcome = run_assembled_once(tmp_path)
    got = _parse_got(blob)
    assert got is not None, f"no injection reached the child (outcome={outcome.get('o')}, errors={rig.errors}, pump_errors={proxy.pump_errors})"
    data = got[2]
    # the child sleeps 0.5 s with a cooked tty, then sets raw (TCSAFLUSH discards anything typed before): receiving the whole payload
    # proves the injection was HELD until the child's first output.
    assert PAYLOAD.encode() in data, "the injection did not arrive whole (typed before the child was ready?)"
    assert data.endswith(b"\r") and data.index(PAYLOAD.encode()) < data.rindex(b"\r"), "text then CR, in order"
    assert blob.count(b"C") == CHATTER
    assert str(getattr(outcome.get("o"), "decision", "")).lower().endswith("defer"), outcome


# -- the writer's contract, with a fake low-level writer ----------------------------------------------------------------------------


class Pipe:
    """A fake master: accepts at most ``cap`` bytes per call; raises BlockingIOError when ``full``; records the wire."""

    def __init__(self, cap=7):
        self.cap = cap
        self.full = False
        self.wire = b""
        self.calls = 0
        self.error = None

    def __call__(self, _fd, data):
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.full:
            raise BlockingIOError(errno.EAGAIN, "full")
        chunk = bytes(data[:self.cap])
        self.wire += chunk
        return len(chunk)


def test_eagain_is_not_an_error_and_bytes_wait_in_one_ordered_buffer():
    p = Pipe()
    p.full = True
    w = SingleWriteSite(-1, writer=p, **_nb())
    w.write_keys(b"key1")
    w.inject(b"INJECT")
    w.write_keys(b"key2")                         # no exception: EAGAIN is flow control
    assert p.wire == b"" and w.has_pending()
    p.full = False
    while w.has_pending():
        w.flush_pending()
    assert p.wire == b"key1INJECTkey2"            # one order on the wire


def test_eio_and_ebadf_still_propagate_and_do_not_wedge_the_buffer():
    for code in (errno.EIO, errno.EBADF):
        p = Pipe()
        p.error = OSError(code, "gone")
        w = SingleWriteSite(-1, writer=p, **_nb())
        with pytest.raises(OSError) as ei:
            w.write_keys(b"x")
        assert ei.value.errno == code
        p.error = None
        assert not w.has_pending(), "a dead master must not keep re-raising from a stale buffer"


def test_keystrokes_and_injections_never_interleave():
    p = Pipe(cap=5)
    w = SingleWriteSite(-1, writer=p, **_nb())
    n = 40
    start = threading.Barrier(3)

    def keys():
        start.wait()
        for _ in range(n):
            w.write_keys(b"k" * 53)

    def injs():
        start.wait()
        for _ in range(n):
            w.inject(b"I" * 61)

    ts = [threading.Thread(target=keys), threading.Thread(target=injs)]
    for t in ts:
        t.start()
    start.wait()
    for t in ts:
        t.join(20)
    while w.has_pending():
        w.flush_pending()
    wire = p.wire
    assert len(wire) == n * (53 + 61)
    # every run of one letter is a whole multiple of its sequence length: nothing was split by the other source
    runs, i = [], 0
    while i < len(wire):
        j = i
        while j < len(wire) and wire[j] == wire[i]:
            j += 1
        runs.append((chr(wire[i]), j - i))
        i = j
    assert all(length % (53 if c == "k" else 61) == 0 for c, length in runs), runs[:10]


def test_a_deferred_flush_is_recorded_and_the_enqueue_record_carries_the_length():
    p = Pipe(cap=3)
    p.full = True
    recs, sink = [], []

    class Gate:
        def record(self, kind, **f):
            recs.append((kind, f))

    w = SingleWriteSite(-1, writer=p, on_inject=sink.append, **_nb())
    w.set_gate(Gate())
    w.write_keys(b"abcdefgh")
    assert not recs, "nothing is flushed yet"
    p.full = False
    while w.has_pending():
        w.flush_pending()
    flushes = [f for k, f in recs if k == "write-flushed"]
    assert len(flushes) == 1 and flushes[0]["bytes"] == 8 and flushes[0]["deferred"] is True
    w._inject_now(b"12345")                        # enqueue logging is unchanged: logged before the write, length on the record
    assert sink[-1].length == 5 and sink[-1].data == b"12345"


def test_flag_off_writer_is_the_old_writer():
    fl = fcntl.fcntl
    r, wfd = os.pipe()
    try:
        before = fl(wfd, fcntl.F_GETFL)
        w = SingleWriteSite(wfd)                   # no kwarg: the pre-fix constructor call
        assert fl(wfd, fcntl.F_GETFL) == before and not before & os.O_NONBLOCK
        assert not w.has_pending() and w.startup_hold_reason() is None
        w.write_keys(b"hello")
        assert os.read(r, 10) == b"hello"
    finally:
        os.close(r)
        os.close(wfd)


def test_flag_on_sets_the_master_nonblocking():
    r, wfd = os.pipe()
    try:
        w = SingleWriteSite(wfd, **_nb())
        assert fcntl.fcntl(wfd, fcntl.F_GETFL) & os.O_NONBLOCK
        w.write_keys(b"hello")
        assert os.read(r, 10) == b"hello"
    finally:
        os.close(r)
        os.close(wfd)


def test_the_pump_selects_the_master_for_writability_only_while_bytes_are_pending(monkeypatch):
    seen = []
    p = Pipe()
    p.full = True
    w = SingleWriteSite(-1, writer=p, **_nb())
    r, wr = os.pipe()
    proxy = HarnessProxy(master_fd=r, pid=0, writer=w, stdin_fd=None, stdout_fd=wr, poll_interval=0.01)
    real = select.select

    def spy(rl, wl, xl, t=None):
        seen.append(list(wl))
        return real(rl, wl, xl, t)

    monkeypatch.setattr(select, "select", spy)
    try:
        proxy.step(0.01)
        assert seen[-1] == []                       # nothing pending: identical to the old select call
        w.write_keys(b"x")
        proxy.step(0.01)
        assert seen[-1] == [r], "a pending buffer must put the master in the write set"
    finally:
        os.close(r)
        os.close(wr)


def test_eagain_on_the_master_read_is_not_end_of_file():
    r, wr = os.pipe()
    out_r, out_w = os.pipe()
    os.set_blocking(r, False)
    proxy = HarnessProxy(master_fd=r, pid=0, writer=SingleWriteSite(-1, writer=Pipe()), stdin_fd=None, stdout_fd=out_w)
    try:
        proxy._on_master_readable()                 # nothing to read on a non-blocking fd: EAGAIN
        assert proxy.child_alive is True
        os.write(wr, b"hi")
        proxy._on_master_readable()
        assert os.read(out_r, 10) == b"hi" and proxy.child_alive
    finally:
        for fd in (r, wr, out_r, out_w):
            os.close(fd)


# -- ruling 2: the startup hold, at the gate ---------------------------------------------------------------------------------------


def test_startup_hold_defers_harness_injections_but_never_keystrokes_until_first_output():
    p = Pipe(cap=1 << 20)
    gate = InjectionGate()
    w = SingleWriteSite(-1, writer=p, gate=gate, **_nb())
    gate.line_hold_check = w.startup_hold_reason
    inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
    out = inj.inject_line("remember the work list", source=SOURCE_MONITOR_GUIDANCE, trigger="t")
    assert out.decision.value == "defer" and p.wire == b""
    w.write_keys(b"k")                              # the operator is never held
    assert p.wire == b"k"
    w.note_child_output()
    gate.release_modal_holds()
    assert b"remember the work list" in p.wire and p.wire.endswith(b"\r")


def test_flag_off_has_no_startup_hold():
    w = SingleWriteSite(-1, writer=Pipe())
    assert w.startup_hold_reason() is None


def test_the_launcher_registers_the_flag_default_off_and_resolves_it_from_the_machine_config_only():
    from gnomon.config import GnomonConfig
    from gnomon.harness import launcher as L

    assert F.KNOWN_FLAGS[FLAG].default is False
    off = L.resolve_harness_settings({"instance_id": "x"}, "/nonexistent-sd-for-mwnb", machine_config=GnomonConfig())
    assert FLAG not in off                          # OFF: the resolved settings are exactly what they were
    on = L.resolve_harness_settings({"instance_id": "x"}, "/nonexistent-sd-for-mwnb", machine_config=GnomonConfig(flags={FLAG: True}))
    assert on[FLAG] is True
    # a session's own launch.json can NOT turn it on (it can write that file; the machine config it cannot)
    lj = L.resolve_harness_settings({"instance_id": "x", "flags": {FLAG: True}}, "/nonexistent-sd-for-mwnb", machine_config=GnomonConfig())
    assert FLAG not in lj


def test_assembled_flag_off_leaves_the_master_blocking(tmp_path):
    from gnomon.harness.launcher import assemble_proxy

    res = _spawn(chatter=1024, need=1)
    rig = _Rig(tmp_path)
    rig.res = res
    try:
        sess = tmp_path / "sess"
        (sess / ".claude").mkdir(parents=True)
        assemble_proxy(res, stdin_fd=None, stdout_fd=rig.out_fd, session_dir=str(sess), instance_id="mwnb-off")
        assert not fcntl.fcntl(res.master_fd, fcntl.F_GETFL) & os.O_NONBLOCK
    finally:
        rig.close()


# -- Diamond P2-1 / P2-2 fix round: the start hold is bounded; the pending buffer is capped for harness injections --------------------

from gnomon.harness import writer as _writer_mod   # noqa: E402  (the constants are read at call time, so a test can lower them)


def _watch(gate):
    """Record every provenance record the gate writes, still passing it on."""
    seen = []
    real = gate.record

    def rec(kind, **f):
        seen.append((kind, f))
        return real(kind, **f)
    gate.record = rec
    return seen


def test_the_named_constants_exist_and_say_15_seconds_and_1_mib():
    assert getattr(_writer_mod, "START_HOLD_TIMEOUT_SECONDS", None) == 15.0
    assert getattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", None) == 1 << 20


def test_the_start_hold_releases_after_the_timeout_against_a_child_that_never_prints(tmp_path, monkeypatch):
    monkeypatch.setattr(_writer_mod, "START_HOLD_TIMEOUT_SECONDS", 0.6, raising=False)   # a short override, not a 15 s sleep
    got_path = tmp_path / "got.bin"
    child = ("import os, sys, tty\n"
             "tty.setraw(0)\n"
             "d = b''\n"
             "while len(d) < int(sys.argv[2]):\n"
             "    c = os.read(0, 4096)\n"
             "    if not c: break\n"
             "    d += c\n"
             "open(sys.argv[1], 'wb').write(d)\n")                # prints NOTHING to the pty, ever
    text = "remember the work list"
    res = PtySpawner().spawn([sys.executable, "-c", child, str(got_path), str(len(text) + 1)],
                             env=sanitize_env(overrides={"TERM": "xterm-256color"}))
    rig = _Rig(tmp_path)
    gate = InjectionGate()
    seen = _watch(gate)
    w = SingleWriteSite(res.master_fd, gate=gate, **_nb())
    gate.line_hold_check = w.startup_hold_reason
    gate.start_hold_cause = getattr(w, "start_hold_release_cause", None)
    proxy = HarnessProxy(master_fd=res.master_fd, pid=res.pid, writer=w, stdin_fd=None, stdout_fd=rig.out_fd, poll_interval=0.05)
    inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
    try:
        time.sleep(0.3)                                               # the child is in raw mode by now
        out = inj.inject_line(text, source=SOURCE_MONITOR_GUIDANCE, trigger="t")
        assert out.decision.value == "defer"
        t0 = time.monotonic()
        released_at = None
        while time.monotonic() - t0 < 5.0 and proxy.child_alive:
            proxy.step(0.05)
            if gate.release_modal_holds() and released_at is None:
                released_at = time.monotonic() - t0
            if got_path.exists():
                break
        assert released_at is not None, "the start hold never ended against a silent child"
        assert 0.2 <= released_at < 3.0, released_at
        end = time.monotonic() + 3
        while not got_path.exists() and time.monotonic() < end:
            proxy.step(0.05)
        assert got_path.read_bytes() == text.encode() + b"\r"
    finally:
        rig.close()
    defer = [f for k, f in seen if k == "injection" and f.get("decision") == "defer"]
    assert defer and "first output after start" in defer[0]["reason"] and "operator's line is busy" not in defer[0]["reason"]
    release = [f for k, f in seen if k == "injection" and f.get("decision") == "release"]
    assert release and "released by timeout" in release[0]["reason"], release
    ended = [f for k, f in seen if k == "modal-guard" and f.get("what") == "start-hold-released"]
    assert len(ended) == 1 and ended[0]["by"] == "timeout"


def test_the_start_hold_still_releases_at_the_first_output_and_the_records_say_so(monkeypatch):
    monkeypatch.setattr(_writer_mod, "START_HOLD_TIMEOUT_SECONDS", 30.0, raising=False)
    p = Pipe(cap=1 << 20)
    gate = InjectionGate()
    seen = _watch(gate)
    w = SingleWriteSite(-1, writer=p, gate=gate, **_nb())
    gate.line_hold_check = w.startup_hold_reason
    gate.start_hold_cause = getattr(w, "start_hold_release_cause", None)
    inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
    assert inj.inject_line("hello", source=SOURCE_MONITOR_GUIDANCE, trigger="t").decision.value == "defer"
    time.sleep(0.1)
    gate.release_modal_holds()
    assert p.wire == b"", "still inside the bound and no output: still held"
    w.note_child_output()
    gate.release_modal_holds()
    assert b"hello" in p.wire
    defer = [f for k, f in seen if k == "injection" and f.get("decision") == "defer"]
    assert defer and "first output after start" in defer[0]["reason"] and "operator's line is busy" not in defer[0]["reason"]
    release = [f for k, f in seen if k == "injection" and f.get("decision") == "release"]
    assert release and "released by first output" in release[0]["reason"], release
    ended = [f for k, f in seen if k == "modal-guard" and f.get("what") == "start-hold-released"]
    assert len(ended) == 1 and ended[0]["by"] == "first output"


def test_an_over_cap_injection_is_refused_whole_and_logged_while_keystrokes_in_the_same_window_are_delivered(monkeypatch):
    monkeypatch.setattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", 100, raising=False)
    p = Pipe(cap=1 << 20)
    p.full = True                                                     # the master accepts nothing: everything queues
    gate = InjectionGate()
    seen = _watch(gate)
    w = SingleWriteSite(-1, writer=p, gate=gate, **_nb())
    w.write_keys(b"k" * 500)                                          # operator bytes: always queued, even past the cap
    w.inject(b"A" * 60)                                               # 60 harness bytes wait
    with pytest.raises(ValueError):                                   # PendingCapRefused is a ScreenRefused (a ValueError)
        w.inject(b"B" * 50)                                           # 60 + 50 > 100: refused whole
    w.write_keys(b"j" * 40)                                           # keystrokes in the same window: still queued
    refused = [f for k, f in seen if k == "inject-refused"]
    assert len(refused) >= 1 and refused[0]["cap_bytes"] == 100 and "cap" in refused[0]["reason"], refused
    p.full = False
    while w.has_pending():
        w.flush_pending()
    assert p.wire == b"k" * 500 + b"A" * 60 + b"j" * 40, "no byte of the refused injection; every keystroke, in order"
    w.inject(b"C" * 100)                                              # the buffer drained, the count with it: exactly the cap fits
    assert p.wire.endswith(b"C" * 100)


def test_a_single_injection_larger_than_the_cap_is_refused_even_into_an_empty_buffer(monkeypatch):
    monkeypatch.setattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", 100, raising=False)
    p = Pipe(cap=1 << 20)
    gate = InjectionGate()
    seen = _watch(gate)
    w = SingleWriteSite(-1, writer=p, gate=gate, **_nb())
    assert not w.has_pending()
    with pytest.raises(ValueError):
        w.inject(b"Z" * 101)
    assert p.wire == b"" and not w.has_pending()
    assert [f for k, f in seen if k == "inject-refused"], "the refusal is logged"


def test_flag_off_has_no_cap_and_no_start_timeout(monkeypatch):
    monkeypatch.setattr(_writer_mod, "PENDING_HARNESS_CAP_BYTES", 10, raising=False)
    monkeypatch.setattr(_writer_mod, "START_HOLD_TIMEOUT_SECONDS", 0.0, raising=False)
    p = Pipe(cap=1 << 20)
    w = SingleWriteSite(-1, writer=p)
    w.inject(b"Q" * 500)
    assert p.wire == b"Q" * 500 and w.startup_hold_reason() is None

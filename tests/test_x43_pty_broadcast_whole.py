"""acceptance (1) and (3), at the PTY level.

The incident (2026-10-02T04:40:19Z): a 1,097-character ``\\all`` broadcast, typed with
arrow-key edits, was logged as a 1,107-character injection, yet every recipient
received only its last 85 characters. 85 = 1107 - 1022, and 1022 is the most a macOS
PTY hands its reader per ``read()`` (``test_macos_pty_splits_a_single_large_write``
below re-measures it). Claude Code kept only the last unbracketed chunk.

These tests drive the REAL production object graph (``launcher.assemble_proxy``: the
gate, the single write site, the injector, the actuator, the command registry) and the
REAL hub fan-out (``Hub.operator_fanout``) into fake Claude Code input boxes running
as children on REAL PTYs (``tests/x43_fake_claude.py``: a line editor with arrow keys
and bracketed paste, plus the measured last-chunk-wins behaviour). Each recipient's
submitted text is hash-compared with the intended text.

AUTOMATED vs LIVE-ONLY: the kernel read split is real; Claude Code's reaction to it is
MODELLED from the incident evidence (exactly 85 = 1107 - 1022 chars, identical in 3
sessions) and the bracketed-paste control (a 1,086-char bracketed broadcast arrived
whole, sha256-verified, on 2026-09-30). A real-Claude-Code run is live-only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import signal
import sys
import time

import pytest

from gnomon.harness import PtySpawner, sanitize_env
from gnomon.harness.launcher import assemble_proxy
from gnomon.hub import Hub
from gnomon.hub.registry import SessionRegistry


#: 2026-10-03: the \all / \<id> tag carries a local-time stamp; pin it so these exact-text and
#: sha assertions stay byte-exact (the stamp's own behaviour is tested in test_stamps_in_tags.py).
STAMP = "2026-10-03 11:29"


@pytest.fixture(autouse=True)
def _pinned_stamp(monkeypatch):
    from gnomon.harness import stamps
    monkeypatch.setattr(stamps, "stamp_now", lambda now=None: STAMP)

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE = os.path.join(HERE, "x43_fake_claude.py")
ACK = b"\x06"
USER = "Operator"  # neutrality: never a real person's name (acceptance 4)

HEAD = ("Right, the shift ends soon. Hopefully the sorting helper is ready by morning "
        "so that we can then arrange the cards ")
TAIL = "to follow the usual write, review, build, and check stages for every change we make!"


def _message(total: int = 1097) -> str:
    filler_len = total - len(HEAD) - len(TAIL)
    words = ("and the haystack runs cleanly overnight with every Diamond review in place "
             "so nobody needs ")
    filler = (words * (filler_len // len(words) + 1))[:filler_len]
    # a long chat message can carry "@AB" / "@CD-E" mentions mid-message ('@' opens Claude
    # Code's file picker when TYPED); keep them in. Real-TUI evidence (test_x43_real_claude_
    # live.py): the truncation happens with or without them.
    filler = filler[:200] + " @AB " + filler[205:600] + " @CD-E, " + filler[608:]
    msg = HEAD + filler + TAIL
    assert len(msg) == total and "\n" not in msg
    return msg


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Session:
    """One fake Claude Code on a real PTY, wrapped by the real production proxy graph."""

    def __init__(self, tmp_path, name: str, hub_dispatch, harness=None) -> None:
        self.name = name
        self.session_dir = str(tmp_path / name)
        os.makedirs(os.path.join(self.session_dir, ".claude"), exist_ok=True)
        self.transcript = os.path.join(self.session_dir, "transcript.jsonl")
        open(self.transcript, "w").close()
        with open(os.path.join(self.session_dir, ".claude", "context_state.json"), "w") as fh:
            json.dump({"transcript_path": self.transcript}, fh)
        env = sanitize_env(overrides={"TERM": "xterm-256color", "FAKE_TRANSCRIPT": self.transcript})
        self.spawn = PtySpawner().spawn([sys.executable, FAKE], env=env)
        self._out = b""
        self._wait_for(b"READY\n", 10)
        self._out = b""
        self.devnull = os.open(os.devnull, os.O_WRONLY)
        h = {"user_name": USER, "prompt_command_hook": True}
        h.update(harness or {})

        self.proxy = assemble_proxy(self.spawn, hub_connection=None, stdin_fd=None,
                                    session_dir=self.session_dir, instance_id=name, harness=h,
                                    stdout_fd=self.devnull, hub_send=lambda f: hub_dispatch(name, f),
                                    local_control=_FakeLocalControl())
        self.actuator = self.proxy._local_control.actuator

    # -- I/O on the child ---------------------------------------------------------

    def _read(self, timeout: float) -> None:
        r, _, _ = select.select([self.spawn.master_fd], [], [], timeout)
        if r:
            try:
                self._out += os.read(self.spawn.master_fd, 65536)
            except OSError:
                pass

    def _wait_for(self, token: bytes, timeout: float) -> None:
        end = time.monotonic() + timeout
        while token not in self._out and time.monotonic() < end:
            self._read(0.05)
        assert token in self._out, f"{self.name}: no {token!r} from the fake child"

    def type_keys(self, keys: bytes, group: int = 4) -> None:
        """'Human' typing through the single write site (the path the operator tap uses
        for punted bytes), paced on the child's per-read ACK so keys never coalesce."""
        writer = self.proxy._writer
        for i in range(0, len(keys), group):
            before = self._out.count(ACK)
            writer.write_keys(keys[i:i + group])
            end = time.monotonic() + 5
            while self._out.count(ACK) <= before and time.monotonic() < end:
                self._read(0.05)

    def submitted(self, timeout: float = 10.0) -> str:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            self._read(0.05)
            complete = self._out.replace(ACK, b"").split(b"\n")[:-1]  # only newline-terminated lines
            for line in complete:
                if line.startswith(b"SUBMIT:"):
                    self._out = self._out.replace(line + b"\n", b"", 1)
                    return base64.b64decode(line[len(b"SUBMIT:"):]).decode("utf-8")
        raise AssertionError(f"{self.name}: nothing submitted within {timeout}s")

    def records(self, kind):
        return [r for r in self.proxy.gate.log.records if r.get("kind") == kind]

    def close(self) -> None:
        try:
            on_exit = getattr(self.proxy, "_on_exit", None)
            if on_exit is not None:
                on_exit(0, "test")
        except Exception:
            pass
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.kill(self.spawn.pid, sig)
            except OSError:
                pass
        try:
            os.waitpid(self.spawn.pid, 0)
        except OSError:
            pass
        for fd in (self.spawn.master_fd, self.devnull):
            try:
                os.close(fd)
            except OSError:
                pass


class _FakeLocalControl:
    """Stands in for LocalControlListener: just holds the actuator assemble_proxy attaches
    (the self socket the UserPromptSubmit hook would use)."""

    actuator = None

    def attach(self, actuator, _root_pid):
        self.actuator = actuator

    def fileno(self):
        return -1


class _Fleet:
    """N sessions + a REAL Hub fan-out between them (in-process downlinks)."""

    def __init__(self, tmp_path, names, harness=None) -> None:
        self.hub = Hub(registry=SessionRegistry())
        self.sessions = {}
        self.feedback = {n: [] for n in names}
        for n in names:
            self.sessions[n] = _Session(tmp_path, n, self._dispatch, harness=harness)
        for n in names:
            self.hub.registry.register(n, _Downlink(self, n))

    def deliver(self, name: str, command) -> None:
        if command.get("cmd") == "operator_feedback":
            self.feedback[name].append(command)
        from gnomon.harness.actuator import ORIGIN_HUB

        self.sessions[name].actuator.dispatch(dict(command), origin=ORIGIN_HUB)

    def _dispatch(self, name: str, frame) -> None:
        if frame.get("type") == "operator_fanout":
            self.hub.operator_fanout(_Conn(self, name), name, frame)
        elif frame.get("type") == "delivery_report" and hasattr(self.hub, "delivery_report"):
            self.hub.delivery_report(name, frame)

    def close(self) -> None:
        for s in self.sessions.values():
            s.close()


class _Downlink:
    def __init__(self, fleet, name):
        self.fleet, self.name = fleet, name

    def send(self, command):
        self.fleet.deliver(self.name, command)


class _Conn:
    """The sender's rendezvous socket as the hub sees it (feedback goes back down it)."""

    def __init__(self, fleet, name):
        self.fleet, self.name = fleet, name

    def sendall(self, data: bytes) -> None:
        for line in data.decode().splitlines():
            if line.strip():
                self.fleet.deliver(self.name, json.loads(line))


def _typed_with_arrow_edits(line: str) -> bytes:
    """Type ``line`` the way the operator did: most of it, then LEFT-arrow back into the
    middle, insert the missing piece, RIGHT-arrow back to the end, and Enter."""
    cut_a, cut_b = 300, 340
    first = line[:cut_a] + line[cut_b:]
    back = len(line) - cut_b
    keys = first.encode() + b"\x1b[D" * back + line[cut_a:cut_b].encode() + b"\x1b[C" * back + b"\r"
    return keys


@pytest.mark.skipif(sys.platform != "darwin", reason="the 1,022-byte read split is a macOS PTY property")
def test_macos_pty_splits_a_single_large_write():
    """The mechanism, re-measured: ONE 1,107-byte write reaches a raw-mode reader as
    more than one read(), the first of 1,022 bytes (TTYHOG - 2)."""
    child = (
        "import os,tty,time,select\n"
        "tty.setraw(0); os.write(1,b'READY\\n'); time.sleep(0.4)\n"
        "sizes=[]; total=0\n"
        "while total<1107:\n"
        "    r,_,_=select.select([0],[],[],3)\n"
        "    if not r: break\n"
        "    d=os.read(0,65536); sizes.append(len(d)); total+=len(d); time.sleep(0.05)\n"
        "os.write(1,('SIZES '+repr(sizes)+'\\n').encode()); time.sleep(0.3)\n")
    res = PtySpawner().spawn([sys.executable, "-c", child], env=sanitize_env())
    try:
        out = b""
        end = time.monotonic() + 5
        while b"READY" not in out and time.monotonic() < end:
            r, _, _ = select.select([res.master_fd], [], [], 0.1)
            if r:
                out += os.read(res.master_fd, 4096)
        os.write(res.master_fd, b"a" * 1107)
        end = time.monotonic() + 8
        while not (b"SIZES" in out and out.rstrip().endswith(b"]")) and time.monotonic() < end:
            r, _, _ = select.select([res.master_fd], [], [], 0.1)
            if r:
                try:
                    out += os.read(res.master_fd, 4096)
                except OSError:
                    break
        line = [l for l in out.split(b"\n") if l.startswith(b"SIZES")][0]
        sizes = eval(line[len(b"SIZES "):].decode())  # a repr of a list of ints from our own child
        assert sum(sizes) == 1107 and len(sizes) >= 2 and sizes[0] == 1022, sizes
    finally:
        try:
            os.kill(res.pid, signal.SIGKILL)
            os.waitpid(res.pid, 0)
        except OSError:
            pass
        os.close(res.master_fd)


@pytest.mark.parametrize("total,edits", [(1097, True), (1097, False), (4600, True)])
def test_all_typed_with_arrow_edits_arrives_whole_in_every_session(tmp_path, total, edits):
    """ACCEPTANCE (1): a >1,000-char ``\\all`` typed with left/right arrow edits is
    delivered byte-exact to EVERY recipient (sha256 compare). Pre-fix this FAILS: each
    recipient submits only the last read chunk (the incident's 85-char tail)."""
    names = ["sender", "peer-a", "peer-b"]
    fleet = _Fleet(tmp_path, names, harness={"operator_delivery_check": False})
    try:
        msg = _message(total)
        sender = fleet.sessions["sender"]
        # 1. the operator types the line, with arrow-key edits, into the sender's TUI
        typed = "\\all " + msg
        sender.type_keys(_typed_with_arrow_edits(typed) if edits else typed.encode() + b"\r")
        line = sender.submitted()
        assert line == "\\all " + msg  # the line editor reassembled the edits exactly
        # 2. Claude Code's UserPromptSubmit hook hands the line to the harness (self socket)
        from gnomon.harness.actuator import ORIGIN_SELF

        res = sender.actuator.dispatch({"cmd": "operator_command", "line": line}, origin=ORIGIN_SELF)
        assert res.get("decision") == "dispatched", res
        # 3. the hub fanned it out; every session's TUI submitted the WHOLE wrapped text
        intended = f"<{USER}-to-all@{STAMP}> " + msg
        got = {n: fleet.sessions[n].submitted() for n in names}
        for n in names:
            assert _sha(got[n]) == _sha(intended), (
                f"{n} received {len(got[n])} of {len(intended)} chars; tail={got[n][-60:]!r}")
        assert fleet.feedback["sender"] and "delivered to 3" in fleet.feedback["sender"][0]["text"]
    finally:
        fleet.close()


def test_a_short_broadcast_is_still_typed_unbracketed(tmp_path):
    """Regression guard: a short line keeps the legacy single unbracketed write."""
    fleet = _Fleet(tmp_path, ["sender", "peer"], harness={"operator_delivery_check": False})
    try:
        from gnomon.harness.actuator import ORIGIN_SELF

        fleet.sessions["sender"].actuator.dispatch({"cmd": "operator_command", "line": "\\all lunch in 5"},
                                                   origin=ORIGIN_SELF)
        assert fleet.sessions["peer"].submitted() == f"<{USER}-to-all@{STAMP}> lunch in 5"
        written = fleet.sessions["peer"].proxy._writer.injected_bytes
        assert written == f"<{USER}-to-all@{STAMP}> lunch in 5\r".encode() and b"\x1b[200~" not in written
    finally:
        fleet.close()


def _wait_records(session, kind, n, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        recs = session.records(kind)
        if len(recs) >= n:
            return recs
        time.sleep(0.05)
    return session.records(kind)


def test_delivery_check_matches_when_whole(tmp_path):
    """ACCEPTANCE (3), positive half: every recipient records a ``match`` carrying the
    delivered sha256, equal to the sent sha256 (the long paste is recorded wrapped in
    ``<pasted_content>`` by the fake TUI, so the normaliser is exercised), and the
    sender gets NO warning."""
    names = ["sender", "peer-a", "peer-b"]
    fleet = _Fleet(tmp_path, names, harness={"delivery_check_timeout_seconds": 15.0,
                                             "delivery_check_poll_seconds": 0.1})
    try:
        from gnomon.harness.actuator import ORIGIN_SELF

        msg = _message(1097)
        fleet.sessions["sender"].actuator.dispatch({"cmd": "operator_command", "line": "\\all " + msg},
                                                   origin=ORIGIN_SELF)
        intended = f"<{USER}-to-all@{STAMP}> " + msg
        for n in names:
            fleet.sessions[n].submitted()
        for n in names:
            recs = _wait_records(fleet.sessions[n], "operator-delivery-check", 1)
            assert len(recs) == 1, (n, recs)
            r = recs[0]
            assert r["decision"] == "match" and r["delivered_sha256"] == r["sent_sha256"] == _sha(intended), r
            inj = [x for x in fleet.sessions[n].records("injection") if x.get("source") == "operator-broadcast"]
            assert inj[0]["msg_id"] == r["msg_id"] and inj[0]["sent_sha256"] == _sha(intended)
        time.sleep(0.3)
        assert not any("WARNING" in f["text"] for f in fleet.feedback["sender"])
    finally:
        fleet.close()


def test_a_deliberately_truncated_delivery_warns_the_sending_operator(tmp_path):
    """ACCEPTANCE (3): with the whole-delivery fix switched OFF (the legacy unbracketed
    write) the recipients' TUIs truncate the text; each recipient records a ``mismatch``
    with the delivered hash, and the SENDER gets a visible warning naming the sessions."""
    names = ["sender", "peer-a", "peer-b"]
    fleet = _Fleet(tmp_path, names, harness={"inject_long_line_as_paste": False,
                                             "delivery_check_timeout_seconds": 15.0,
                                             "delivery_check_poll_seconds": 0.1})
    try:
        from gnomon.harness.actuator import ORIGIN_SELF

        msg = _message(1097)
        fleet.sessions["sender"].actuator.dispatch({"cmd": "operator_command", "line": "\\all " + msg},
                                                   origin=ORIGIN_SELF)
        intended = f"<{USER}-to-all@{STAMP}> " + msg
        for n in names:
            got = fleet.sessions[n].submitted()
            assert len(got) < len(intended) and intended.endswith(got)  # the incident, reproduced
        for n in names:
            recs = _wait_records(fleet.sessions[n], "operator-delivery-check", 1)
            assert recs and recs[0]["decision"] == "mismatch" and recs[0]["loud"] is True, (n, recs)
            assert recs[0]["delivered_sha256"] != recs[0]["sent_sha256"]
        end = time.monotonic() + 10
        warn = []
        while time.monotonic() < end:
            warn = [f for f in fleet.feedback["sender"] if f.get("delivery_mismatch")]
            if warn and len(warn[-1]["delivery_mismatch"]) == 3:
                break
            time.sleep(0.05)
        assert warn, fleet.feedback["sender"]
        assert warn[-1]["delivery_mismatch"] == sorted(names)
        assert "did NOT arrive intact" in warn[-1]["text"] and "peer-a" in warn[-1]["text"]
    finally:
        fleet.close()



def test_a_restored_long_unsent_line_arrives_whole(tmp_path):
    """ROUND 2 (Diamond F4, g64): the D50 operator-line restore (``optap.restore_unsent``)
    puts a saved >1022-byte single line back into the TUI; it must arrive whole. Pre-fix
    this FAILS: the restore wrote it unbracketed and the TUI kept only the last read chunk."""
    env = sanitize_env(overrides={"TERM": "xterm-256color"})
    spawn = PtySpawner().spawn([sys.executable, FAKE], env=env)
    sd = tmp_path / "sess"
    (sd / ".claude").mkdir(parents=True)
    stdin_r, stdin_w = os.pipe()
    devnull = os.open(os.devnull, os.O_WRONLY)
    out = b""
    try:
        end = time.monotonic() + 10
        while b"READY\n" not in out and time.monotonic() < end:
            r, _, _ = select.select([spawn.master_fd], [], [], 0.05)
            if r:
                out += os.read(spawn.master_fd, 65536)
        proxy = assemble_proxy(spawn, stdin_fd=stdin_r, session_dir=str(sd), instance_id="restore-test",
                               harness={"user_name": USER}, stdout_fd=devnull)
        tap = proxy._operator_tap
        tap._child_pid = None
        line = _message(1300)
        tap.restore_unsent(line)
        time.sleep(0.3)
        proxy._writer.write_keys(b"\r")
        out = b""
        end = time.monotonic() + 10
        got = None
        while got is None and time.monotonic() < end:
            r, _, _ = select.select([spawn.master_fd], [], [], 0.05)
            if r:
                out += os.read(spawn.master_fd, 65536)
            for l in out.replace(ACK, b"").split(b"\n")[:-1]:
                if l.startswith(b"SUBMIT:"):
                    got = base64.b64decode(l[len(b"SUBMIT:"):]).decode()
        assert got is not None, "nothing submitted"
        assert _sha(got) == _sha(line), f"restored {len(got)} of {len(line)} chars; tail={got[-40:]!r}"
    finally:
        for fd in (stdin_r, stdin_w, devnull):
            os.close(fd)
        try:
            os.kill(spawn.pid, signal.SIGKILL)
            os.waitpid(spawn.pid, 0)
        except OSError:
            pass
        os.close(spawn.master_fd)


def _fallthrough_round_trip(tmp_path, line):
    """Type ``line`` + Enter on the operator's stdin into a tap that doesn't own it (a DM shape,
    no hub), so it falls through to the fake TUI on a real PTY. Returns what the TUI submitted,
    or None."""
    env = sanitize_env(overrides={"TERM": "xterm-256color"})
    spawn = PtySpawner().spawn([sys.executable, FAKE], env=env)
    sd = tmp_path / "sess"
    (sd / ".claude").mkdir(parents=True)
    stdin_r, stdin_w = os.pipe()
    devnull = os.open(os.devnull, os.O_WRONLY)
    out = b""
    try:
        end = time.monotonic() + 10
        while b"READY\n" not in out and time.monotonic() < end:
            r, _, _ = select.select([spawn.master_fd], [], [], 0.05)
            if r:
                out += os.read(spawn.master_fd, 65536)
        proxy = assemble_proxy(spawn, stdin_fd=stdin_r, session_dir=str(sd), instance_id="fallthrough-rt",
                               harness={"user_name": USER}, stdout_fd=devnull)
        proxy._operator_tap._child_pid = None
        os.write(stdin_w, line.encode() + b"\r")
        while select.select([stdin_r], [], [], 0.05)[0]:
            proxy._on_stdin_readable()
        out = b""
        end = time.monotonic() + 4
        while time.monotonic() < end:
            r, _, _ = select.select([spawn.master_fd], [], [], 0.05)
            if r:
                out += os.read(spawn.master_fd, 65536)
            for l in out.replace(ACK, b"").split(b"\n")[:-1]:
                if l.startswith(b"SUBMIT:"):
                    return base64.b64decode(l[len(b"SUBMIT:"):]).decode()
        return None
    finally:
        for fd in (stdin_r, stdin_w, devnull):
            os.close(fd)
        try:
            os.kill(spawn.pid, signal.SIGKILL)
            os.waitpid(spawn.pid, 0)
        except OSError:
            pass
        os.close(spawn.master_fd)


@pytest.mark.parametrize("n", [39, 40, 100, 400, 512])
def test_a_middle_tier_fallthrough_line_is_submitted(tmp_path, n):
    """ROUND 4 (r3 Diamond F20; g64 amendment): a `\\`-led fall-through line of 41..512 bytes
    (here 100, 400, 512; plus the 39/40 boundary) must be SUBMITTED whole. Pre-fix the tap wrote
    line + CR in ONE write and the TUI absorbed the CR (measured on real 2.1.287 at 100/400/512)."""
    line = ("\\gn-builder " + _message(1300))[:n]
    got = _fallthrough_round_trip(tmp_path, line)
    assert got is not None, f"not submitted ({n} bytes)"
    assert _sha(got) == _sha(line), f"{n}: got {len(got)} chars"


def test_a_long_fallthrough_command_line_arrives_whole(tmp_path):
    """ROUND 3 (Diamond F15, g64): a `\\`-led line the tap does not own (here a DM shape with no
    hub) is handed back to the TUI and submitted. Over 1022 bytes it must arrive whole. Pre-fix
    this FAILS: the tap wrote line + CR in one unbracketed write and the TUI kept the last chunk."""
    env = sanitize_env(overrides={"TERM": "xterm-256color"})
    spawn = PtySpawner().spawn([sys.executable, FAKE], env=env)
    sd = tmp_path / "sess"
    (sd / ".claude").mkdir(parents=True)
    stdin_r, stdin_w = os.pipe()
    devnull = os.open(os.devnull, os.O_WRONLY)
    out = b""
    try:
        end = time.monotonic() + 10
        while b"READY\n" not in out and time.monotonic() < end:
            r, _, _ = select.select([spawn.master_fd], [], [], 0.05)
            if r:
                out += os.read(spawn.master_fd, 65536)
        proxy = assemble_proxy(spawn, stdin_fd=stdin_r, session_dir=str(sd), instance_id="fallthrough-test",
                               harness={"user_name": USER}, stdout_fd=devnull)
        proxy._operator_tap._child_pid = None
        line = "\\gn-builder " + _message(1300)
        os.write(stdin_w, line.encode() + b"\r")
        while select.select([stdin_r], [], [], 0.05)[0]:
            proxy._on_stdin_readable()
        out = b""
        end = time.monotonic() + 10
        got = None
        while got is None and time.monotonic() < end:
            r, _, _ = select.select([spawn.master_fd], [], [], 0.05)
            if r:
                out += os.read(spawn.master_fd, 65536)
            for l in out.replace(ACK, b"").split(b"\n")[:-1]:
                if l.startswith(b"SUBMIT:"):
                    got = base64.b64decode(l[len(b"SUBMIT:"):]).decode()
        assert got is not None, "nothing submitted"
        assert _sha(got) == _sha(line), f"submitted {len(got)} of {len(line)} chars; tail={got[-40:]!r}"
    finally:
        for fd in (stdin_r, stdin_w, devnull):
            os.close(fd)
        try:
            os.kill(spawn.pid, signal.SIGKILL)
            os.waitpid(spawn.pid, 0)
        except OSError:
            pass
        os.close(spawn.master_fd)

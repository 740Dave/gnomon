"""Real-PTY smoke: spawn a child, prove transparent proxying + inject reaches it.

Covers + +
 + end to end on a live POSIX pty.
"""

import os
import select
import signal
import sys
import time
import tty

from gnomon.harness import (
    Actuator,
    ControlChannel,
    HarnessProxy,
    Injector,
    Notice,
    NoticeKind,
    PtySpawner,
    SingleWriteSite,
    sanitize_env,
)

import socket

# A tiny child: put its own tty in raw mode (no echo / no canon so bytes arrive
# immediately and are not doubled), then echo everything it reads back with an
# "RX:" prefix. Exits on seeing b"QUIT".
_CHILD = r"""
import os, tty
try:
    tty.setraw(0)
except Exception:
    pass
os.write(1, b"READY")  # ready sentinel: parent drains this before writing
while True:
    try:
        data = os.read(0, 4096)
    except OSError:
        break
    if not data:
        break
    os.write(1, b"RX:" + data)
    if b"QUIT" in data:
        break
"""


def _read_for(fd, duration_s=0.6):
    """Accumulate everything readable from fd over a fixed window."""
    buf = b""
    end = time.monotonic() + duration_s
    while True:
        remaining = end - time.monotonic()
        if remaining <= 0:
            break
        r, _, _ = select.select([fd], [], [], remaining)
        if not r:
            continue
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _spawn_child():
    spawner = PtySpawner()
    env = sanitize_env(overrides={"TERM": "xterm-256color"})
    result = spawner.spawn([sys.executable, "-c", _CHILD], env=env)
    # Configure the pty to raw from the PARENT, deterministically, before any
    # I/O — so cooked-mode echo cannot leak and race the child's output. On BSD
    # ptys (macOS) termios on the master fd governs the terminal discipline.
    try:
        tty.setraw(result.master_fd)
    except Exception:
        pass
    # Wait for (and consume) the child's READY sentinel, so our first write
    # cannot race the child's arrival at its read loop.
    end = time.monotonic() + 5.0
    got = b""
    while b"READY" not in got and time.monotonic() < end:
        r, _, _ = select.select([result.master_fd], [], [], end - time.monotonic())
        if not r:
            continue
        try:
            got += os.read(result.master_fd, 64)
        except OSError:
            break
    return result


def _cleanup(pid, master_fd):
    try:
        os.write(master_fd, b"QUIT")
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
    try:
        os.close(master_fd)
    except OSError:
        pass


def test_transparent_proxy_both_directions():
    result = _spawn_child()
    writer = SingleWriteSite(result.master_fd)
    try:
        # our -> child (keystroke) ... child -> us (echo). Both directions.
        writer.write_keys(b"ping")
        out = _read_for(result.master_fd)
        assert b"RX:ping" in out
    finally:
        _cleanup(result.pid, result.master_fd)


def test_inject_line_reaches_child_as_input():
    result = _spawn_child()
    writer = SingleWriteSite(result.master_fd)
    injector = Injector(writer, beat_seconds=0.01)  # real (small) beats on a real pty
    try:
        injector.inject_line("zap", clear=True)  # PTY-hooks WS-D: the clear is opt-in now
        out = _read_for(result.master_fd)
        # The child saw the ESC-ESC, the verbatim text, and the CR submit.
        assert b"zap" in out
        assert b"\x1b\x1b" in out
        assert b"\r" in out
        # And it was all logged as an injection.
        assert writer.injected_bytes == b"\x1b\x1bzap\r"
    finally:
        _cleanup(result.pid, result.master_fd)


def test_compact_guidance_notice_reaches_child_as_submitted_turn():
    # The architect ruling: the actionable compact-guidance notice must arrive at
    # the child as a SUBMITTED user turn — the CR observed arriving at the child,
    # like test_inject_line_reaches_child_as_input above. A raw, unsubmitted
    # inject would just sit in the TUI buffer and the prepare-for-compact flow
    # would never fire.
    result = _spawn_child()
    writer = SingleWriteSite(result.master_fd)
    injector = Injector(writer, beat_seconds=0.01)  # real (small) beats
    proxy = HarnessProxy(
        master_fd=result.master_fd,
        pid=result.pid,
        writer=writer,
        injector=injector,
        stdin_fd=None,
    )
    try:
        proxy.inject_notice(
            Notice(
                kind=NoticeKind.COMPACT_GUIDANCE,
                text="prepare-for-compact",
                used_percentage=91.0,
                threshold=90,
            )
        )
        out = _read_for(result.master_fd)
        # The child saw the verbatim text and the CR SUBMIT (PTY-hooks WS-D (2026-09-27): notices are NON-disruptive now — no ESC-ESC).
        assert b"prepare-for-compact" in out
        assert b"\r" in out  # <-- the submit actually reached the child
        # ... and it was the exact (non-disruptive) inject_line sequence, all logged.
        assert writer.injected_bytes == b"prepare-for-compact\r"
    finally:
        _cleanup(result.pid, result.master_fd)


def test_fyi_notice_is_also_submitted_to_child():
    # STEP 0: the 95% FYI backstop is now a SUBMITTED turn too (all agent-facing
    # notices are). Proves the full inject_line sequence reaches the real pty —
    # ESC-ESC clear, verbatim text, CR submit — no raw-inject pollution path.
    result = _spawn_child()
    writer = SingleWriteSite(result.master_fd)
    injector = Injector(writer, beat_seconds=0.01)
    proxy = HarnessProxy(
        master_fd=result.master_fd,
        pid=result.pid,
        writer=writer,
        injector=injector,
        stdin_fd=None,
    )
    try:
        proxy.inject_notice(
            Notice(
                kind=NoticeKind.FYI,
                text="context 96 backstop",
                used_percentage=96.0,
                threshold=95,
            )
        )
        out = _read_for(result.master_fd, duration_s=0.3)
        assert b"context 96 backstop" in out
        assert b"\r" in out  # submitted with CR
        assert writer.injected_bytes == b"context 96 backstop\r"  # PTY-hooks WS-D (2026-09-27): notices are NON-disruptive now — no ESC-ESC
    finally:
        _cleanup(result.pid, result.master_fd)


def test_proxy_pump_relays_control_inject_to_child_and_copies_output():
    # End-to-end: a control command -> actuator -> injector -> single write site
    # -> child -> master readable -> copied to our (piped) stdout.
    result = _spawn_child()
    harness_sock, agent_sock = socket.socketpair()
    pipe_r, pipe_w = os.pipe()
    writer = SingleWriteSite(result.master_fd)
    injector = Injector(writer, beat_seconds=0.01)
    actuator = Actuator(injector, monitor=None)
    channel = ControlChannel(harness_sock.fileno(), actuator)
    proxy = HarnessProxy(
        master_fd=result.master_fd,
        pid=result.pid,
        writer=writer,
        control_channel=channel,
        monitor=None,
        stdin_fd=None,        # no interactive stdin in the test
        stdout_fd=pipe_w,     # capture what the proxy forwards
    )
    try:
        agent_sock.sendall(b'{"cmd": "inject", "text": "hello-from-agent"}\n')
        # Pump until the child's echo shows up on our captured stdout.
        deadline = time.monotonic() + 5.0
        seen = b""
        while time.monotonic() < deadline and b"hello-from-agent" not in seen:
            proxy.step(timeout=0.1)
            r, _, _ = select.select([pipe_r], [], [], 0.0)
            if r:
                seen += os.read(pipe_r, 4096)
        assert b"RX:" in seen  # child output was copied to our stdout
        assert b"hello-from-agent" in seen  # ... and it carried the injected text
    finally:
        _cleanup(result.pid, result.master_fd)
        harness_sock.close()
        agent_sock.close()
        os.close(pipe_r)
        os.close(pipe_w)

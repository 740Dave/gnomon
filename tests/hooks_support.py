"""Shared rig for the PTY-hooks tests (2026-09-27).

Builds the REAL production object graph via ``launcher.assemble_proxy`` over
socketpairs (no real claude): the child side of the "pty" is a socket we can
read, the operator's stdin is a pipe we can write, and the hub rendezvous is a
socketpair. Everything the harness writes to the child is observable at
``rig.child_bytes()``; every provenance record at ``rig.records()``.
"""

from __future__ import annotations

import json
import os
import select
import socket
import sys
import textwrap
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gnomon.harness.launcher import assemble_proxy  # noqa: E402
from gnomon.harness.spawn import SpawnResult  # noqa: E402


import re as _re

#: 2026-10-03: ``<NAME-to-all@YYYY-MM-DD HH:MM>`` / ``[Gnomon@YYYY-MM-DD HH:MM]`` carry a
#: local-time stamp; the many older exact-text assertions compare through :func:`unstamp`.
_STAMP_RE = _re.compile(r"@\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?=[>\]])")
_STAMP_RE_B = _re.compile(rb"@\d{4}-\d{2}-\d{2} \d{2}:\d{2}(?=[>\]])")


def unstamp(x):
    """Remove the stamp from the FIRST-class tags (str or bytes)."""
    return _STAMP_RE_B.sub(b"", x) if isinstance(x, (bytes, bytearray)) else _STAMP_RE.sub("", x)


class FakeHub:
    """Stands in for ProxyHubConnection: a socketpair; captures UP frames."""

    def __init__(self) -> None:
        self.proxy_side, self.hub_side = socket.socketpair()
        self.statuses: List[Dict[str, Any]] = []
        self.frames: List[Dict[str, Any]] = []

    def fileno(self) -> int:
        return self.proxy_side.fileno()

    def send_status(self, used_percentage=None, alive=True) -> None:
        self.statuses.append({"used_percentage": used_percentage, "alive": alive})

    def send_frame(self, frame: Dict[str, Any]) -> None:
        self.frames.append(frame)

    def send_down(self, command: Dict[str, Any]) -> None:
        self.hub_side.sendall((json.dumps(command) + "\n").encode())

    def close(self) -> None:
        self.proxy_side.close()
        self.hub_side.close()


#: the instance id every Rig runs as (one place; tests that assert on it import this)
RIG_INSTANCE_ID = "gm-test"


class Rig:
    def __init__(self, tmp_path, *, harness: Optional[Dict[str, Any]] = None, stdin: bool = True,
                 hub: bool = True, session: bool = True, beat: float = 0.0, read_context=None,
                 confirm_compact: bool = False, confirm_compact_timeout=None) -> None:
        self.child, self.master = socket.socketpair()
        self.child.setblocking(False)
        self.stdin_r: Optional[int] = None
        self.stdin_w: Optional[int] = None
        if stdin:
            self.stdin_r, self.stdin_w = os.pipe()
        self.out_r, self.out_w = os.pipe()  # the operator's terminal (feedback/echo)
        os.set_blocking(self.out_r, False)
        self.session_dir = None
        if session:
            self.session_dir = str(tmp_path / "sess")
            os.makedirs(os.path.join(self.session_dir, ".claude"), exist_ok=True)
        self.hub = FakeHub() if hub else None
        self.proxy = assemble_proxy(
            SpawnResult(pid=0, master_fd=self.master.fileno()),
            read_context=read_context,
            hub_connection=self.hub,
            stdin_fd=self.stdin_r,
            session_dir=self.session_dir,
            instance_id=RIG_INSTANCE_ID,
            harness=harness,
            stdout_fd=self.out_w,
            hub_send=self.hub.send_frame if self.hub else None,
            confirm_compact=confirm_compact,
            confirm_compact_timeout=confirm_compact_timeout,
        )
        self.proxy._injector._sleep = lambda _s: None  # no real beats
        tap = getattr(self.proxy, "operator_tap", None)
        if tap is not None:
            tap._child_pid = None  # never signal a real process from a test

    # -- drive -----------------------------------------------------------------

    def type(self, data: bytes) -> None:
        """Operator keystrokes through the REAL stdin path (proxy._on_stdin_readable)."""
        assert self.stdin_w is not None
        os.write(self.stdin_w, data)
        self.proxy._on_stdin_readable()

    def hub_down(self, command: Dict[str, Any]) -> None:
        assert self.hub is not None
        self.hub.send_down(command)
        self.proxy._on_control_readable()

    # -- observe ---------------------------------------------------------------

    def child_bytes(self) -> bytes:
        out = b""
        while True:
            r, _, _ = select.select([self.child], [], [], 0.02)
            if not r:
                return out
            chunk = self.child.recv(65536)
            if not chunk:
                return out
            out += chunk

    def operator_screen(self) -> bytes:
        try:
            return os.read(self.out_r, 65536)
        except BlockingIOError:
            return b""

    @property
    def gate(self):
        return self.proxy.gate

    def records(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        recs = list(self.proxy.gate.log.records)
        return [r for r in recs if kind is None or r.get("kind") == kind]

    def file_records(self) -> List[Dict[str, Any]]:
        path = os.path.join(self.session_dir, ".claude", "injection_log.jsonl")
        with open(path, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def close(self) -> None:
        for fd in (self.stdin_r, self.stdin_w, self.out_r, self.out_w):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self.child.close()
        self.master.close()
        if self.hub is not None:
            self.hub.close()


DENY_ALL_MANIFEST = {
    "inbound-injection": [{"kind": "deny_all", "name": "deny-everything"}],
    "operator-command": [{"kind": "deny_all", "name": "deny-every-command"}],
}


def write_fake_claude(dirpath: str, name: str = "fake-claude") -> str:
    """An executable stand-in for claude: prints a banner, then appends every
    byte it reads on its tty to $FAKE_CHILD_LOG. Ignores its argv."""
    path = os.path.join(dirpath, name)
    with open(path, "w") as fh:
        fh.write(textwrap.dedent(f"""\
            #!{sys.executable}
            import os, sys
            log = os.environ.get("FAKE_CHILD_LOG", "/dev/null")
            sys.stdout.write("fake-claude ready\\n"); sys.stdout.flush()
            with open(log, "ab") as f:
                while True:
                    try:
                        data = os.read(0, 4096)
                    except OSError:
                        break
                    if not data:
                        break
                    f.write(data); f.flush()
            """))
    os.chmod(path, 0o755)
    return path

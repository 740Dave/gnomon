"""The control channel — one fd in the pump's select set.

For this slice the control channel is a byte stream fd (e.g. one end of a
``socketpair``) carrying newline-delimited JSON commands from the agent side and
newline-delimited JSON responses back. It is exactly the fd the pump's single
``select()`` includes alongside ``[master_fd, own_stdin]``.

This is the transport shell around :class:`~gnomon.harness.actuator.Actuator`.
A later slice swaps this fd for the localhost MCP/HTTP rendezvous
 without touching the actuation vocabulary — the seam is
that ``ControlChannel`` owns framing/IO and ``Actuator`` owns meaning.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import socket
import sys
import tempfile
from typing import Any, Callable, Dict, List, Optional

from .actuator import ActuationError, Actuator
from .leading_bang import ScreenRefused


class ControlChannel:
    """Newline-delimited JSON command/response framing over one fd."""

    def __init__(
        self,
        fd: int,
        actuator: Actuator,
        reader: Callable[[int, int], bytes] = os.read,
        writer: Callable[[int, bytes], int] = os.write,
        origin: Optional[str] = None,
    ) -> None:
        self._fd = fd
        self._actuator = actuator
        self._read = reader
        self._write = writer
        self._buf = b""
        # PTY-hooks WS-A: which transport this channel IS ("hub" / "self"). It is
        # passed to Actuator.dispatch so the provenance source comes from the
        # transport, never from the frame. None = legacy (dispatch(command)).
        self._origin = origin
        #: r2d: one line per non-refusal exception contained out of dispatch (bounded by caller use).
        self.dispatch_errors: List[str] = []

    @property
    def fd(self) -> int:
        return self._fd

    def _write_response(self, response: Dict[str, Any]) -> None:
        payload = (json.dumps(response) + "\n").encode("utf-8")
        remaining = memoryview(payload)
        while remaining:
            n = self._write(self._fd, remaining)
            if n <= 0:  # pragma: no cover - defensive
                break
            remaining = remaining[n:]

    def _handle_line(self, line: bytes) -> Dict[str, Any]:
        try:
            command = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            response = {"ok": False, "error": f"bad json: {exc}"}
            self._write_response(response)
            return response
        try:
            if self._origin is None:
                response = self._actuator.dispatch(command)
            else:
                response = self._actuator.dispatch(command, origin=self._origin)
        except ActuationError as exc:
            response = {"ok": False, "error": str(exc)}
        except ScreenRefused as exc:
            # r2c (g71-amendment-r2b (3)): the writer backstop refused (the gate normally stops
            # these first). Fail loud to the CALLER, never dead: the pump keeps serving every other
            # command and the next legitimate inject. The funnel already wrote the provenance record.
            response = {"ok": False, "action": command.get("cmd") if isinstance(command, dict) else None,
                        "decision": "refused", "reasons": list(exc.reasons),
                        "error": f"REFUSED by the writer backstop: {exc}; nothing was typed"}
        except Exception as exc:  # noqa: BLE001
            # r2d (r2c F2; g71-amendment-r2c (2)): ANY other exception out of dispatch (a lone
            # surrogate's UnicodeEncodeError was the measured one) fails loud to the CALLER and the
            # pump lives on. Never swallowed silently: recorded on stderr and returned as ok:false.
            self.dispatch_errors.append(f"{type(exc).__name__}: {exc}")
            try:
                sys.stderr.write(f"gnomon: control dispatch FAILED ({type(exc).__name__}: {exc}); "
                                 "pump continues\n")
            except Exception:  # noqa: BLE001
                pass
            response = {"ok": False, "action": command.get("cmd") if isinstance(command, dict) else None,
                        "error": f"internal error in dispatch ({type(exc).__name__}): {exc}; "
                                 "nothing was typed unless an earlier phase had already been written"}
        self._write_response(response)
        return response

    def feed_readable(self, max_bytes: int = 65536) -> List[Dict[str, Any]]:
        """Read what's available, dispatch every complete line, return responses.

        Returns an empty list on EOF (peer closed). Partial trailing data is
        buffered for the next call.
        """
        chunk = self._read(self._fd, max_bytes)
        if not chunk:
            return []
        self._buf += chunk
        responses: List[Dict[str, Any]] = []
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            responses.append(self._handle_line(line))
        return responses


# --- fix-pass WS2a (2026-09-27): the LOCAL, agent-side control path ---------
#
# The hub rendezvous above is the OPERATOR path (hub -> proxy). This is the
# SELF path the fd->rendezvous migration orphaned: the harnessed child reaches
# ITS OWN proxy's Actuator through a private unix-domain socket, via the stdio
# MCP server ``gnomon.harness.self_mcp`` that Claude Code spawns as one of
# the child's own MCP servers. No hub, no token in the child.
#
# Self-scoping is enforced AT THE SOCKET, not assumed: a connecting peer is
# admitted only if its pid (LOCAL_PEERPID on macOS / SO_PEERCRED on Linux) is
# THIS proxy's claude child or a descendant of it (ancestry via hub.procinfo,
# reused, not reimplemented). A process from another session that finds the
# path is refused — there is no route from session A's tree to session B's
# Actuator. Anything that cannot be verified is refused (fail closed).

#: Hard cap on concurrent local control connections (fd-exhaustion guard).
_MAX_LOCAL_CONNECTIONS = 16
#: Ancestry walk bound (pid chains are short; this only stops a pathological loop).
_MAX_ANCESTRY_HOPS = 64

_SOL_LOCAL = 0          # <sys/un.h> SOL_LOCAL (darwin)
_LOCAL_PEERPID = 0x002  # <sys/un.h> LOCAL_PEERPID (darwin)


def peer_pid(conn: socket.socket) -> Optional[int]:
    """The connecting process's pid, or ``None`` when it cannot be determined."""
    try:
        if sys.platform == "darwin":
            pid = conn.getsockopt(_SOL_LOCAL, _LOCAL_PEERPID)
            return int(pid) if pid and pid > 0 else None
        if hasattr(socket, "SO_PEERCRED"):
            import struct

            raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
            pid = struct.unpack("3i", raw)[0]
            return pid if pid > 0 else None
    except OSError:
        return None
    return None


def is_same_or_descendant(pid: int, root_pid: int) -> bool:
    """Is ``pid`` == ``root_pid`` or a descendant of it? Fail closed on any doubt."""
    if pid <= 0 or root_pid <= 0:
        return False
    try:
        from ..hub import procinfo
    except Exception:  # pragma: no cover - import failure is a refusal
        return False
    current = pid
    for _ in range(_MAX_ANCESTRY_HOPS):
        if current == root_pid:
            return True
        if current <= 1:
            return False
        try:
            state = procinfo.probe(current)
        except Exception:
            return False
        if state.ppid is None or state.ppid == current:
            return False
        current = state.ppid
    return False


class LocalControlListener:
    """A private unix-socket listener feeding the proxy's local :class:`Actuator`.

    Lifecycle (driven by the harness launcher):

    1. construct BEFORE spawning claude — binds ``<private 0700 dir>/c.sock``
       (0600) so the path can be written into the child's MCP config;
    2. :meth:`attach` the Actuator + the claude child's pid once spawned (until
       then every connection is refused);
    3. the proxy's single ``select()`` includes :meth:`fds`; readable fds go to
       :meth:`on_readable`;
    4. :meth:`close` unlinks the socket and removes the private dir.

    Framing is the unchanged :class:`ControlChannel` (newline-delimited JSON,
    one response line per command) — the same vocabulary the hub path speaks.
    """

    def __init__(
        self,
        path: Optional[str] = None,
        *,
        peer_pid_of: Callable[[socket.socket], Optional[int]] = peer_pid,
        is_descendant: Callable[[int, int], bool] = is_same_or_descendant,
    ) -> None:
        self._owned_dir: Optional[str] = None
        if path is None:
            # Short private dir: AF_UNIX paths are limited to 104 bytes on macOS
            # and session dirs are already ~90. mkdtemp creates it 0700.
            base = "/tmp" if os.path.isdir("/tmp") else None
            self._owned_dir = tempfile.mkdtemp(prefix="gnomon-ctl-", dir=base)
            path = os.path.join(self._owned_dir, "c.sock")
        self._path = path
        self._peer_pid_of = peer_pid_of
        self._is_descendant = is_descendant
        self._actuator: Optional[Actuator] = None
        self._root_pid: Optional[int] = None
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self._sock.bind(path)
            os.chmod(path, 0o600)
            self._sock.listen(8)
        except OSError:
            self._sock.close()
            self._cleanup_path()
            raise
        self._conns: Dict[int, "tuple[socket.socket, ControlChannel]"] = {}
        self._eof: set = set()
        self.refusals: List[str] = []  # observable record of refused peers

    # -- wiring --------------------------------------------------------------

    @property
    def path(self) -> str:
        return self._path

    def attach(self, actuator: Actuator, root_pid: int) -> None:
        self._actuator = actuator
        self._root_pid = root_pid

    # -- pump surface --------------------------------------------------------

    def fds(self) -> List[int]:
        if self._sock.fileno() < 0:
            return []
        return [self._sock.fileno(), *self._conns.keys()]

    def owns(self, fd: int) -> bool:
        return fd == self._sock.fileno() or fd in self._conns

    def on_readable(self, fd: int) -> None:
        if fd == self._sock.fileno():
            self._accept()
            return
        entry = self._conns.get(fd)
        if entry is None:
            return
        _conn, channel = entry
        try:
            channel.feed_readable()
        except OSError:
            self._drop(fd)
            return
        if fd in self._eof:
            self._drop(fd)

    # -- internals -----------------------------------------------------------

    def _refuse(self, conn: socket.socket, reason: str) -> None:
        self.refusals.append(reason)
        try:
            conn.sendall((json.dumps({"ok": False, "error": f"refused: {reason}"}) + "\n").encode("utf-8"))
        except OSError:
            pass
        try:
            conn.close()
        except OSError:
            pass

    def _accept(self) -> None:
        try:
            conn, _addr = self._sock.accept()
        except OSError as exc:  # pragma: no cover - raced / interrupted
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINTR):
                return
            raise
        if self._actuator is None or self._root_pid is None:
            self._refuse(conn, "harness not attached to a child yet")
            return
        if len(self._conns) >= _MAX_LOCAL_CONNECTIONS:
            self._refuse(conn, "too many local control connections")
            return
        pid = self._peer_pid_of(conn)
        if pid is None:
            self._refuse(conn, "cannot verify the connecting process")
            return
        if not self._is_descendant(pid, self._root_pid):
            self._refuse(conn, f"peer pid {pid} is not in this session's process tree")
            return
        fd = conn.fileno()
        eof = self._eof

        def _reader(rfd: int, n: int) -> bytes:
            data = os.read(rfd, n)
            if not data:
                eof.add(rfd)
            return data

        self._conns[fd] = (conn, ControlChannel(fd, self._actuator, reader=_reader, origin="self"))

    def _drop(self, fd: int) -> None:
        entry = self._conns.pop(fd, None)
        self._eof.discard(fd)
        if entry is not None:
            try:
                entry[0].close()
            except OSError:
                pass

    def _cleanup_path(self) -> None:
        try:
            os.unlink(self._path)
        except OSError:
            pass
        if self._owned_dir is not None:
            shutil.rmtree(self._owned_dir, ignore_errors=True)
            self._owned_dir = None

    def close(self) -> None:
        for fd in list(self._conns):
            self._drop(fd)
        try:
            self._sock.close()
        except OSError:
            pass
        self._cleanup_path()

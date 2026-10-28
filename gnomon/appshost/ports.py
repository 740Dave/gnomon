"""Loopback ports and listeners (architect rulings R8).

* :func:`port_is_free` — a bind test on 127.0.0.1 (no listener there now).
* :class:`ListenerProbe` — the lsof probes: who listens on a port (to NAME a foreign holder by PID and command),
  and what a process tree listens on (the post-start non-loopback check). A probe that cannot run RAISES
  :class:`ProbeError`; it never reports "nobody is listening" from a failed instrument.
* :func:`process_tree` — a pid and its descendants (``ps``).

Injectable: the host takes a probe object; tests pass a fake for the non-loopback case (binding 0.0.0.0 in a test
would open a real network listener).
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Set

LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1"})
LSOF_CANDIDATES = ("/usr/sbin/lsof", "/usr/bin/lsof")
PROBE_TIMEOUT_S = 10.0


class ProbeError(RuntimeError):
    """The listener probe could not run (no lsof, a timeout, an unreadable answer)."""


@dataclass(frozen=True)
class Listener:
    pid: int
    command: str
    address: str      # the bound host part, e.g. "127.0.0.1", "::1", "*", "0.0.0.0"
    port: int

    @property
    def loopback(self) -> bool:
        return self.address in LOOPBACK_ADDRS

    def describe(self) -> str:
        return f"pid {self.pid} ({self.command}) listening on {self.address}:{self.port}"


def _bind_ok(family: int, host: str, port: int) -> bool:
    try:
        s = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return True                     # the family is unavailable on this host: nothing can hold it there
    # SO_REUSEADDR so a port whose last listener is merely in TIME_WAIT (a just-stopped app) counts as free, as it
    # does for the app itself (Python's HTTPServer sets it); an ACTIVE listener on the address still refuses.
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if family == socket.AF_INET6:
        try:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        except OSError:
            pass
    try:
        s.bind((host, port))
        return True
    except OSError as exc:
        return exc.errno == 49 and family == socket.AF_INET6     # EADDRNOTAVAIL: no IPv6 loopback here
    finally:
        s.close()


def port_is_free(port: int, host: str = "127.0.0.1") -> bool:
    """True when nothing is bound to ``port`` on loopback OR the wildcard, IPv4 or IPv6 (Diamond F8: on macOS a
    127.0.0.1 bind succeeds beside a 0.0.0.0 holder, so all four are tested). Each socket is closed at once."""
    return all(_bind_ok(fam, h, port) for fam, h in ((socket.AF_INET, host), (socket.AF_INET, "0.0.0.0"),
                                                      (socket.AF_INET6, "::1"), (socket.AF_INET6, "::")))


def _lsof() -> str:
    for c in LSOF_CANDIDATES:
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    w = shutil.which("lsof")
    if w:
        return w
    raise ProbeError("lsof is not available; cannot list listening sockets")


def _split_addr(name: str) -> (str, int):  # type: ignore[valid-type]
    # lsof -F n gives "127.0.0.1:8079", "[::1]:8079", "*:8079"
    host, _, port = name.rpartition(":")
    host = host.strip("[]")
    try:
        p = int(port)
    except ValueError:
        p = -1
    return host, p


def parse_lsof_f(out: str) -> List[Listener]:
    """Parse ``lsof -F pcn`` output into listeners."""
    res: List[Listener] = []
    pid: Optional[int] = None
    cmd = ""
    for line in out.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            try:
                pid = int(val)
            except ValueError:
                pid = None
            cmd = ""
        elif tag == "c":
            cmd = val
        elif tag == "n" and pid is not None:
            host, port = _split_addr(val)
            res.append(Listener(pid, cmd, host, port))
    return res


class ListenerProbe:
    """The real probe: ``lsof`` over TCP listening sockets."""

    def _run(self, args: List[str]) -> str:
        try:
            r = subprocess.run([_lsof(), "-nP", "-F", "pcn", *args], capture_output=True, text=True,
                               timeout=PROBE_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ProbeError("lsof timed out")
        except OSError as exc:
            raise ProbeError(f"lsof failed to run: {exc}")
        # lsof exits 1 when it finds nothing; that is an answer ("no listener"), not a fault
        if r.returncode not in (0, 1):
            raise ProbeError(f"lsof exited {r.returncode}: {r.stderr.strip()[:200]}")
        if r.returncode == 1 and r.stderr.strip():
            raise ProbeError(f"lsof error: {r.stderr.strip()[:200]}")
        return r.stdout

    def listeners_on_port(self, port: int) -> List[Listener]:
        return [l for l in parse_lsof_f(self._run([f"-iTCP:{int(port)}", "-sTCP:LISTEN"])) if l.port == port]

    def listeners_of(self, pids: Iterable[int]) -> List[Listener]:
        pl = sorted({int(p) for p in pids if int(p) > 0})
        if not pl:
            return []
        return parse_lsof_f(self._run(["-a", "-p", ",".join(str(p) for p in pl), "-iTCP", "-sTCP:LISTEN"]))


def process_children() -> Dict[int, List[int]]:
    """ONE ``ps`` snapshot as ``{ppid: [child pids]}``. A ps failure returns ``{}`` (every tree is then just its root)."""
    try:
        r = subprocess.run(["/bin/ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    children: Dict[int, List[int]] = {}
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        children.setdefault(ppid, []).append(pid)
    return children


def tree_in(children: Dict[int, List[int]], root_pid: int) -> Set[int]:
    """``root_pid`` and every descendant in an already-taken snapshot (:func:`process_children`). No process is started."""
    out = {root_pid}
    stack = [root_pid]
    while stack:
        p = stack.pop()
        for c in children.get(p, []):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def process_tree(root_pid: int) -> Set[int]:
    """``root_pid`` and every descendant (one ``ps`` snapshot). A ps failure returns just the root."""
    return tree_in(process_children(), root_pid)

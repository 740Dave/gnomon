"""Process introspection at the OBJECT — argv, cwd, start time, zombie state.

Used by the app-lifecycle cluster to (a) discover
whitelisted apps the hub did not launch (signature discovery over argv+cwd) and
(b) re-confirm liveness at the object, never from a stale record.

Pure stdlib (``ctypes`` on macOS, ``/proc`` on Linux) — no psutil dependency, in
keeping with the stdlib-only headless core (pyproject ``dependencies = []``).

⛔ This module deliberately has NO ``lsof`` which-pid-holds-path-open query.
Per ``lsof-secondary-only`` that is at most an optional annotation and NEVER a
safety gate (a viewer that closes its fd between saves false-negatives in
exactly the clobber window). argv+cwd is the load-bearing key.

A failure to introspect is surfaced (``ProcIntrospectionError``), never silently
treated as "no such process": an absent process and an unreadable one are
different answers and must not look the same.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import os
import sys
from dataclasses import dataclass
from typing import List, Optional


class ProcIntrospectionError(RuntimeError):
    """Introspection is unavailable on this platform / failed unexpectedly."""


#: probe() states
ABSENT = "absent"
ZOMBIE = "zombie"
ALIVE = "alive"


@dataclass
class ProcState:
    pid: int
    state: str  # ABSENT | ZOMBIE | ALIVE
    start_time: Optional[float] = None  # epoch seconds (None when absent)
    ppid: Optional[int] = None


# --------------------------------------------------------------------------
# macOS (darwin) — sysctl KERN_PROC_PID / KERN_PROCARGS2 + libproc
# --------------------------------------------------------------------------

_CTL_KERN = 1
_KERN_ARGMAX = 8
_KERN_PROC = 14
_KERN_PROC_PID = 1
_KERN_PROCARGS2 = 49
_SZOMB = 5
#: sizeof(struct kinfo_proc) on 64-bit darwin
_KINFO_PROC_SIZE = 648
#: offsets inside struct kinfo_proc (kp_proc = struct extern_proc at offset 0)
_OFF_STARTTIME_SEC = 0    # p_un.__p_starttime.tv_sec (long)
_OFF_STARTTIME_USEC = 8   # p_un.__p_starttime.tv_usec (int32 on darwin, padded)
_OFF_P_STAT = 36          # char p_stat
#: kp_eproc starts after extern_proc (296 bytes); e_ppid is at eproc+264... we
#: read ppid via libproc instead (PROC_PIDTBSDINFO) to avoid a fragile offset.
_PROC_PIDTBSDINFO = 3
_PROC_PIDTBSDINFO_SIZE = 136
_PROC_PIDVNODEPATHINFO = 9
_VNODE_INFO_SIZE = 152
_MAXPATHLEN = 1024
_PROC_PIDVNODEPATHINFO_SIZE = 2 * (_VNODE_INFO_SIZE + _MAXPATHLEN)

_libc = None
_libproc = None


def _darwin_libs():
    global _libc, _libproc
    if _libc is None:
        _libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        _libc.sysctl.argtypes = [
            ctypes.POINTER(ctypes.c_int), ctypes.c_uint,
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t),
            ctypes.c_void_p, ctypes.c_size_t,
        ]
        _libc.sysctl.restype = ctypes.c_int
        _libproc = ctypes.CDLL(ctypes.util.find_library("proc") or "/usr/lib/libproc.dylib", use_errno=True)
        _libproc.proc_pidinfo.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int,
        ]
        _libproc.proc_pidinfo.restype = ctypes.c_int
        _libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
        _libproc.proc_listallpids.restype = ctypes.c_int
    return _libc, _libproc


def _sysctl(mib: List[int], size: int, strict: bool = False) -> Optional[bytes]:
    libc, _ = _darwin_libs()
    arr = (ctypes.c_int * len(mib))(*mib)
    buf = ctypes.create_string_buffer(size)
    sz = ctypes.c_size_t(size)
    rc = libc.sysctl(arr, len(mib), buf, ctypes.byref(sz), None, 0)
    if rc != 0:
        err = ctypes.get_errno()
        # Per-pid "cannot read THIS process" answers (gone, zombie, another
        # user's, or mid-exec -> EIO) are None, not fatal: one racing pid must
        # not blind discovery of every other. The instrument as a whole is
        # positive-controlled by the caller (it must be able to read ITSELF).
        if err == errno.ESRCH or (
            not strict and err in (errno.EINVAL, errno.EPERM, errno.EACCES, errno.EIO)
        ):
            return None
        raise ProcIntrospectionError(f"sysctl{mib} failed: errno {err}")
    return buf.raw[: sz.value]


def _darwin_probe(pid: int) -> ProcState:
    # STRICT: this answer gates "reaped", so an unreadable pid
    # must never be reported ABSENT. Any errno other than ESRCH raises.
    raw = _sysctl([_CTL_KERN, _KERN_PROC, _KERN_PROC_PID, pid], _KINFO_PROC_SIZE, strict=True)
    if raw is None or len(raw) < _KINFO_PROC_SIZE:
        # KERN_PROC_PID returns size 0 (not an error) for a nonexistent pid.
        # Corroborate ABSENT with a second, independent witness (kill 0).
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return ProcState(pid=pid, state=ABSENT)
        except PermissionError:
            pass
        raise ProcIntrospectionError(
            f"pid {pid}: sysctl reports no process but kill(pid, 0) says it exists"
        )
    sec = int.from_bytes(raw[_OFF_STARTTIME_SEC:_OFF_STARTTIME_SEC + 8], sys.byteorder, signed=True)
    usec = int.from_bytes(raw[_OFF_STARTTIME_USEC:_OFF_STARTTIME_USEC + 4], sys.byteorder, signed=True)
    stat = raw[_OFF_P_STAT]
    ppid = _darwin_ppid(pid)
    state = ZOMBIE if stat == _SZOMB else ALIVE
    return ProcState(pid=pid, state=state, start_time=sec + usec / 1e6, ppid=ppid)


def _darwin_ppid(pid: int) -> Optional[int]:
    _, libproc = _darwin_libs()
    buf = ctypes.create_string_buffer(_PROC_PIDTBSDINFO_SIZE)
    n = libproc.proc_pidinfo(pid, _PROC_PIDTBSDINFO, 0, buf, _PROC_PIDTBSDINFO_SIZE)
    if n != _PROC_PIDTBSDINFO_SIZE:
        return None
    return int.from_bytes(buf.raw[16:20], sys.byteorder)


def _darwin_argv(pid: int) -> Optional[List[str]]:
    argmax_raw = _sysctl([_CTL_KERN, _KERN_ARGMAX], ctypes.sizeof(ctypes.c_int))
    argmax = int.from_bytes(argmax_raw, sys.byteorder) if argmax_raw else 1 << 20
    raw = _sysctl([_CTL_KERN, _KERN_PROCARGS2, pid], argmax)
    if not raw or len(raw) < 4:
        return None
    argc = int.from_bytes(raw[:4], sys.byteorder)
    rest = raw[4:]
    # exec_path\0 [padding \0...] argv[0]\0 argv[1]\0 ...
    end = rest.find(b"\0")
    if end < 0:
        return None
    i = end
    while i < len(rest) and rest[i] == 0:
        i += 1
    argv: List[str] = []
    while len(argv) < argc and i < len(rest):
        j = rest.find(b"\0", i)
        if j < 0:
            j = len(rest)
        argv.append(rest[i:j].decode("utf-8", "surrogateescape"))
        i = j + 1
    return argv


def _darwin_cwd(pid: int) -> Optional[str]:
    _, libproc = _darwin_libs()
    buf = ctypes.create_string_buffer(_PROC_PIDVNODEPATHINFO_SIZE)
    n = libproc.proc_pidinfo(pid, _PROC_PIDVNODEPATHINFO, 0, buf, _PROC_PIDVNODEPATHINFO_SIZE)
    if n <= 0:
        return None
    path = buf.raw[_VNODE_INFO_SIZE:_VNODE_INFO_SIZE + _MAXPATHLEN]
    path = path.split(b"\0", 1)[0]
    return path.decode("utf-8", "surrogateescape") or None


def _darwin_pids() -> List[int]:
    _, libproc = _darwin_libs()
    n = libproc.proc_listallpids(None, 0)
    if n <= 0:
        raise ProcIntrospectionError("proc_listallpids failed")
    cap = n + 256
    arr = (ctypes.c_int * cap)()
    got = libproc.proc_listallpids(arr, ctypes.sizeof(arr))
    if got <= 0:
        raise ProcIntrospectionError("proc_listallpids failed")
    return [p for p in arr[:got] if p > 0]


# --------------------------------------------------------------------------
# Linux — /proc
# --------------------------------------------------------------------------

def _linux_boot_time() -> float:
    with open("/proc/stat", "r") as fh:
        for line in fh:
            if line.startswith("btime"):
                return float(line.split()[1])
    raise ProcIntrospectionError("no btime in /proc/stat")


def _linux_probe(pid: int) -> ProcState:
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            data = fh.read().decode("utf-8", "surrogateescape")
    except (FileNotFoundError, ProcessLookupError):
        return ProcState(pid=pid, state=ABSENT)
    fields = data[data.rfind(")") + 2:].split()
    st = fields[0]
    ppid = int(fields[1])
    start_ticks = int(fields[19])
    start = _linux_boot_time() + start_ticks / os.sysconf("SC_CLK_TCK")
    if st in ("Z", "X"):
        return ProcState(pid=pid, state=ZOMBIE, start_time=start, ppid=ppid)
    return ProcState(pid=pid, state=ALIVE, start_time=start, ppid=ppid)


def _linux_argv(pid: int) -> Optional[List[str]]:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as fh:
            raw = fh.read()
    except OSError:
        return None
    if not raw:
        return None
    return [a.decode("utf-8", "surrogateescape") for a in raw.rstrip(b"\0").split(b"\0")]


def _linux_cwd(pid: int) -> Optional[str]:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return None


def _linux_pids() -> List[int]:
    return [int(n) for n in os.listdir("/proc") if n.isdigit()]


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def _platform() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    raise ProcIntrospectionError(f"process introspection unsupported on {sys.platform!r}")


def probe(pid: int) -> ProcState:
    """Liveness at the object: ABSENT / ZOMBIE / ALIVE (+ start time)."""
    if not isinstance(pid, int) or pid <= 0:
        return ProcState(pid=pid, state=ABSENT)
    return _darwin_probe(pid) if _platform() == "darwin" else _linux_probe(pid)


def argv_of(pid: int) -> Optional[List[str]]:
    """The process's exact argv, or None when unreadable (other user / gone)."""
    return _darwin_argv(pid) if _platform() == "darwin" else _linux_argv(pid)


def cwd_of(pid: int) -> Optional[str]:
    """The process's current working directory, or None when unreadable."""
    return _darwin_cwd(pid) if _platform() == "darwin" else _linux_cwd(pid)


def all_pids() -> List[int]:
    return _darwin_pids() if _platform() == "darwin" else _linux_pids()


def same_process(state: ProcState, start_time: Optional[float], tolerance: float = 1.0) -> bool:
    """Is ``state`` the SAME process that had ``start_time``? (pid-reuse guard)."""
    if state.state == ABSENT or state.start_time is None:
        return False
    if start_time is None:
        return True
    return abs(state.start_time - start_time) <= tolerance

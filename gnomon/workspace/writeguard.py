"""Process-wide WRITE GUARD for the converter (I8: dry-run by default; S1: no apply path exists).

Enforced with a ``sys.addaudithook`` hook (plus, for the two calls the interpreter does not audit,
a wrapper installed for the life of the context), not by convention: while a :func:`scratch_only`
context is active, every operation listed below that targets a path outside the scratch directory,
and every process spawn, signal or socket connect/send/bind, raises :class:`WriteGuardViolation` (the
exception propagates out of the audited call, so the operation does not happen). A check that cannot
fail is decoration: tests/test_x13s1_write_guard.py makes it fail on purpose for each class.

Covered (audit events): ``open`` with write/create/append/truncate flags; ``os.mkdir`` ``os.rmdir``
``os.remove`` ``os.rename`` (both ends) ``os.link`` ``os.symlink`` ``os.truncate`` ``os.chmod``
``os.chown`` ``os.utime`` ``os.chflags``; ``shutil.copyfile`` ``copymode`` ``copystat`` ``copytree``
``rmtree`` ``move``; ``tempfile.mkstemp`` ``mkdtemp``; ``sqlite3.connect`` to any database outside
scratch (``:memory:``, the empty temp name and a ``file:`` URI with ``mode=ro`` or ``immutable=1``
are the only exceptions: the event does not say whether a connection will write, so the guard
refuses every connection it cannot prove read-only); and the forbidden-outright events
``subprocess.Popen`` ``os.system`` ``os.exec`` ``os.fork`` ``os.forkpty`` ``os.posix_spawn``
``os.spawn`` ``os.kill`` ``os.killpg`` ``socket.connect`` ``socket.sendto`` ``socket.sendmsg``
``socket.bind`` (the converter never starts, stops or signals a process and never talks to a
service: :8770 included; ``bind`` of an AF_UNIX path would also create a file).

Covered (wrapper, because CPython emits NO audit event for them): ``os.mkfifo`` and ``os.mknod``
outside scratch. The wrapper is installed when the outermost context is entered and the original is
restored when it exits, so the guard stays inert outside a context.

NOT covered (be honest about the limit): anything that reaches the kernel without going through the
interpreter's audited entry points, i.e. writes made by C extensions or another process's code (a
native library doing its own ``open``); ``os.setxattr`` family (absent on darwin, unaudited elsewhere);
``os.symlink`` ONLY checks the link location, not its target (intended); and a ``sqlite3`` connection
handed to a path INSIDE scratch that ATTACHes a live database. The fingerprint is the independent
second proof for everything this list does not reach.

Reads are NOT guarded (the converter reads the live roots by design); the before/after fingerprint in
:mod:`.fingerprint` is the second, independent proof.

The hook is installed once per interpreter (audit hooks cannot be removed) and is INERT outside an
active context, so importing this module changes nothing for other code or tests.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import urllib.parse
from typing import Iterator, List, Optional

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

# event -> indexes of the args that are WRITE targets (must be inside scratch)
_WRITE_TARGET_ARGS = {
    "os.mkdir": (0,), "os.rmdir": (0,), "os.remove": (0,), "os.truncate": (0,), "os.chmod": (0,),
    "os.chown": (0,), "os.utime": (0,), "os.chflags": (0,), "shutil.rmtree": (0,),
    "tempfile.mkstemp": (0,), "tempfile.mkdtemp": (0,),
    "os.rename": (0, 1), "shutil.move": (0, 1),
    "os.link": (1,), "os.symlink": (1,),
    "shutil.copyfile": (1,), "shutil.copymode": (1,), "shutil.copystat": (1,), "shutil.copytree": (1,),
}
_FORBIDDEN_EVENTS = {"subprocess.Popen", "os.system", "os.exec", "os.fork", "os.forkpty",
                     "os.posix_spawn", "os.spawn", "os.kill", "os.killpg", "socket.connect",
                     "socket.sendto", "socket.sendmsg", "socket.bind"}


class WriteGuardViolation(RuntimeError):
    """The converter tried to write outside its scratch dir, or to spawn / signal / connect."""


_lock = threading.Lock()
_stack: List[str] = []          # active scratch roots (innermost last); empty => inert
_installed = False
violations: List[str] = []       # every violation this process has seen (for the run report)


def _inside(path, root: str) -> bool:
    if isinstance(path, int):
        return True              # an fd is not a path: the open() that produced it was judged
    try:
        rp = os.path.realpath(os.fsdecode(path))
    except Exception:            # noqa: BLE001 - un-decodable path: treat as outside
        return False
    return rp == root or rp.startswith(root + os.sep)


def _sqlite_ok(db, root: str) -> bool:
    """True when a sqlite3 connect target is provably not a write outside scratch."""
    try:
        name = os.fsdecode(db)
    except Exception:            # noqa: BLE001
        return False
    if name in ("", ":memory:"):
        return True
    if name.startswith("file:"):
        rest = name[5:]
        path, _, query = rest.partition("?")
        params = {kv.partition("=")[0]: kv.partition("=")[2] for kv in query.split("&") if kv}
        if params.get("mode") == "ro" or params.get("immutable") == "1":
            return True
        if path.startswith(":memory:") or params.get("mode") == "memory":
            return True
        return _inside(urllib.parse.unquote(path.split("//", 1)[-1] if path.startswith("//") else path), root)
    return _inside(name, root)


def _hook(event: str, args) -> None:
    if not _stack:
        return
    root = _stack[-1]
    if event == "open":
        path, flags = args[0], args[2]
        if isinstance(flags, int) and (flags & _WRITE_FLAGS) and not _inside(path, root):
            _violate(event, path, root)
        return
    idx = _WRITE_TARGET_ARGS.get(event)
    if idx is not None:
        for i in idx:
            if i < len(args) and args[i] is not None and not _inside(args[i], root):
                _violate(event, args[i], root)
        return
    if event == "sqlite3.connect":
        db = args[0] if args else ""
        if not _sqlite_ok(db, root):
            _violate(event, db, root)
        return
    if event in _FORBIDDEN_EVENTS:
        _violate(event, args[0] if args else "", root, forbidden=True)


def _violate(event, path, root, forbidden=False):
    what = "is forbidden in the converter" if forbidden else f"targets {path!r} outside the scratch dir"
    msg = f"WRITE GUARD: {event} {what} (scratch={root})"
    violations.append(msg)
    raise WriteGuardViolation(msg)


_PATCHED: dict = {}               # name -> original, while the wrappers are installed


def _wrap(name: str) -> None:
    orig = getattr(os, name, None)
    if orig is None or name in _PATCHED:
        return

    def guarded(path, *a, **kw):
        if _stack and not _inside(path, _stack[-1]):
            _violate(f"os.{name}", path, _stack[-1])
        return orig(path, *a, **kw)

    guarded.__name__ = orig.__name__
    guarded.__doc__ = orig.__doc__
    _PATCHED[name] = orig
    setattr(os, name, guarded)


def _unwrap_all() -> None:
    for name, orig in list(_PATCHED.items()):
        setattr(os, name, orig)
        del _PATCHED[name]


def _install() -> None:
    global _installed
    with _lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True


@contextlib.contextmanager
def scratch_only(root: str) -> Iterator[str]:
    """Inside the context, writes are allowed ONLY under ``root``. Entering creates nothing."""
    _install()
    real = os.path.realpath(root)
    with _lock:
        _stack.append(real)
        _wrap("mkfifo")
        _wrap("mknod")
    try:
        yield real
    finally:
        with _lock:
            _stack.pop()
            if not _stack:
                _unwrap_all()


def active_root() -> Optional[str]:
    return _stack[-1] if _stack else None

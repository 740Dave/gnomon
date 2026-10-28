"""Safe reads of files the open-work package reads on a thread that serves a session (R3-1).

A file under a shared, agent-writable directory can be replaced by a FIFO, a device or a directory. A plain ``open()`` of a FIFO blocks the
reader (the PTY pump thread) until someone opens the other end. So EVERY such read goes through :func:`read_regular`: open
non-blocking (``O_NONBLOCK`` returns at once for a FIFO), ``fstat`` the OPEN descriptor, and accept only a regular file. A symlink is
followed only when its target, fully resolved, is itself a regular file inside the same directory as the link; any other symlink is
refused. The result is ``(bytes, "")``, ``(None, "absent")`` (nothing there) or ``(None, "unreadable")`` (there, but not a usable regular
file, too large, or an I/O error). Never raises, never blocks. Pure stdlib.
"""

from __future__ import annotations

import os
import stat
from typing import Optional, Tuple


def regular_target(path: str) -> Tuple[Optional[str], str]:
    """``(path to open, "")`` or ``(None, "absent"|"unreadable")``. A symlink must resolve to a regular file in the SAME directory."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    if not stat.S_ISLNK(st.st_mode):
        return path, ""
    real = os.path.realpath(path)
    if os.path.dirname(real) != os.path.realpath(os.path.dirname(path)):
        return None, "unreadable"
    try:
        if not stat.S_ISREG(os.lstat(real).st_mode):
            return None, "unreadable"
    except OSError:
        return None, "unreadable"
    return real, ""


def read_regular(path: str, max_bytes: int = 64 * 1024, truncate: bool = False) -> Tuple[Optional[bytes], str]:
    target, why = regular_target(path)
    if target is None:
        return None, why
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "absent"
    except OSError:
        return None, "unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, "unreadable"
        chunks = []
        total = 0
        while True:
            b = os.read(fd, 65536)
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > max_bytes:
                if truncate:               # a head-of-file read (a long definition's frontmatter): keep what was asked for
                    return b"".join(chunks)[:max_bytes], ""
                return None, "unreadable"
        return b"".join(chunks), ""
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)


def open_lock(path: str, create: bool = True) -> int:
    """Open (create) a lock file, refusing anything that is not a regular file. Raises OSError if it is not usable. ``create=False`` (reads)
    never creates the file: a missing lock raises FileNotFoundError."""
    # O_NOFOLLOW: a symlink at the lock path (dangling or not) is refused, so O_CREAT can never create a file somewhere else (ELOOP -> OSError)
    fd = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0) | os.O_NONBLOCK | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{os.path.basename(path)} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd

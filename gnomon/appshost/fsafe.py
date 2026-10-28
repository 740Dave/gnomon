"""Open a file for reading ONLY if it is a regular file, without ever blocking (round 3c, R3-F6).

``open()`` on a FIFO blocks until a writer appears, so a same-uid swap of a hashed file (the venv interpreter, a file
under the app root, a licence, a log) for a FIFO would hang every status poll. The check that closes it is made on the
OPEN descriptor, not on the path: ``os.open(O_RDONLY | O_NONBLOCK)`` returns at once for a FIFO or a device, then
``fstat`` says what it is, so there is no stat-then-open window to swap in. Stdlib only; imported by pins, licence and
state."""

from __future__ import annotations

import errno
import os
import stat
from typing import BinaryIO


class NotRegularFile(OSError):
    """The path is a FIFO, device, socket or directory where a regular file was required."""

    def __init__(self, path: str, mode: int) -> None:
        super().__init__(errno.EINVAL, f"not a regular file ({stat.filemode(mode)})", path)


def open_regular(path: str) -> BinaryIO:
    """A binary read file object for a REGULAR file. Raises OSError (:class:`NotRegularFile` for a non-regular one);
    never blocks."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(path, st.st_mode)
        return os.fdopen(fd, "rb")
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_append_fd(path: str, mode: int = 0o600, *, truncate: bool = False) -> int:
    """A file descriptor for WRITING a REGULAR file, without ever blocking (R3D-F3). ``open(O_WRONLY)`` on a FIFO blocks
    until a reader appears, so a same-uid swap of an app-reachable file for a FIFO would hang the writer (the audit
    append hung ``stop``). ``O_NONBLOCK`` makes a reader-less FIFO fail at once (ENXIO) and ``fstat`` on the OPEN
    descriptor refuses a FIFO that has a reader, a device or a socket (:class:`NotRegularFile`); no stat-then-open
    window. Append mode, no symlink follow, created 0600. ``truncate=True`` truncates AFTER the check (never ``O_TRUNC``
    on an unchecked file). The caller closes the descriptor. Raises OSError, loudly."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | (0 if truncate else os.O_APPEND)
    fd = os.open(path, flags, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(path, st.st_mode)
        if truncate:
            os.ftruncate(fd, 0)
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_new_fd(path: str, mode: int = 0o600) -> int:
    """A descriptor for a NEW regular file at ``path`` (R3E-F2): ``O_CREAT | O_EXCL`` (an existing entry of ANY kind, a
    FIFO included, fails at once with EEXIST, so a same-uid pre-created FIFO at a predictable temp name cannot hang the
    writer), ``O_NOFOLLOW``, ``O_NONBLOCK``, and an ``fstat`` regular-file check on the open descriptor. The caller closes
    the descriptor and chooses a UNIQUE name. Raises OSError, loudly."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(path, st.st_mode)
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def open_lock_fd(path: str, mode: int = 0o600) -> int:
    """A descriptor for the host lock file (R3E-F3): ``O_RDWR | O_CREAT | O_NONBLOCK | O_NOFOLLOW`` and an ``fstat``
    regular-file check, so a FIFO, device or socket swapped in at the lock name is a clean :class:`NotRegularFile`
    (flock on a FIFO fails ENOTSUP, a raw OSError out of every mutating verb)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, mode)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise NotRegularFile(path, st.st_mode)
        return fd
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise

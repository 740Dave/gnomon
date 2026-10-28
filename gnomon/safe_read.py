"""A non-blocking read of ONE small regular file, for code that runs on a thread serving a session (stdlib only, no import-time work).

``read_regular_bytes(path, max_bytes)`` opens ``O_NONBLOCK`` (a FIFO returns at once instead of waiting for a writer), ``fstat``s the OPEN
descriptor and accepts only a regular file, then reads at most ``max_bytes`` (one byte more = too large). Symlinks are followed (the open
resolves them) and the target must still be a regular file. Raises ``OSError`` for anything else (not regular, too large, unreadable), so a
caller's existing ``except OSError`` keeps working. It lives here, outside ``workspace/``, because the workspace package's flag-gate guard
(tests/test_x13s2_flag_gate.py) treats a bare ``os.open`` as a persisting call.
"""

from __future__ import annotations

import os
import stat


def read_regular_bytes(path: str, max_bytes: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("not a regular file")
        raw = b""
        while len(raw) <= max_bytes:
            b = os.read(fd, 65536)
            if not b:
                break
            raw += b
        if len(raw) > max_bytes:
            raise OSError(f"larger than {max_bytes} bytes")
        return raw
    finally:
        os.close(fd)

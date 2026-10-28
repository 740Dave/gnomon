"""Raw mode on the proxy's own controlling terminal.

While proxying, the harness puts its OWN controlling terminal into raw mode so
that the human's keystrokes pass through untouched (no local echo, no line
buffering, no signal interpretation by the proxy's tty) — the child's real TUI
does all of that. The original terminal settings are restored on exit, always,
including on exception.

Non-tty file descriptors (e.g. a pipe in a test) are handled as a no-op, so the
harness pieces stay testable off a real terminal.
"""

from __future__ import annotations

import os
import termios
import tty
from types import TracebackType
from typing import Optional, Type


class RawMode:
    """Context manager that switches ``fd`` to raw mode and restores it.

    Usage::

        with RawMode(sys.stdin.fileno()):
            ...  # proxy loop

    If ``fd`` is not a tty the manager is inert (enter/exit do nothing), which
    keeps the harness usable and testable when stdin is redirected.
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._saved: Optional[list] = None
        self._is_tty = os.isatty(fd)

    @property
    def active(self) -> bool:
        """True while raw mode is currently applied."""
        return self._saved is not None

    def __enter__(self) -> "RawMode":
        if self._is_tty:
            self._saved = termios.tcgetattr(self._fd)
            tty.setraw(self._fd)
        return self

    def restore(self) -> None:
        """Restore the saved terminal settings (idempotent)."""
        if self._saved is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._saved)
            self._saved = None

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self.restore()

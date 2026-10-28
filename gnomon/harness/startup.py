"""Startup helpers for UNATTENDED launches (PTY-hooks WS-G, measured 2026-09-27).

A fresh session dir makes Claude Code show its first-run FOLDER-TRUST dialog
("Is this a project you created or one you trust?" — cursor on "No, exit").
With a human at the terminal they press Enter on "Yes"; an unattended
(background / agent-launched) harness has nobody, so the session blocks forever.

:class:`FolderTrustAccepter` answers it — OPT-IN ONLY (launch.json
``accept_folder_trust: true``, set by the operator's ``agent_launch
--accept-folder-trust``) — as a VERIFY-BEFORE-COMMIT state machine, because a
blind DOWN+ENTER was MEASURED to fail (live run 2, 2026-09-27): the DOWN moved
the cursor to "Yes", then Claude Code's terminal-capability probes (``ESC[?u``,
``ESC[c`` — unanswered with no real terminal) re-rendered the dialog with the
selection RESET to "No, exit", and the ENTER confirmed "No".

1. marker seen -> wait until the child's output has been QUIET ``quiet`` s;
2. press DOWN; wait for quiet again; read the rendered selection (the last
   ``❯`` in the ANSI/whitespace-stripped tail);
3. selection on "Yes" -> press ENTER (done). Still/again on "No" -> press DOWN
   again (max ``max_attempts``), else GIVE UP and record it (never ENTER on No).

Bounded: only within ``window_seconds`` of construction; each keystroke goes
through the writer's gate as a ``source: bootstrap`` event (trigger
``folder-trust-accept:down`` / ``:enter`` / ``:gave-up``) — recorded and
deniable. It trusts only the dir the harness itself just created for this
launch. ⚠ DELTA (operator to ratify): an auto-answered security prompt.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, Optional

from .hooks import SOURCE_BOOTSTRAP, ClaudeDialect, InjectionEvent

_ANSI = re.compile(rb"\x1b\[[0-9;?<>=]*[A-Za-z~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\x1b[=>78]")
_CURSOR = "❯"  # ❯


def flatten(data: bytes) -> str:
    return re.sub(r"\s+", "", _ANSI.sub(b"", data).decode("utf-8", "replace")).lower()


class FolderTrustAccepter:
    WATCH, SETTLING, MOVED, DONE, GAVE_UP = "watch", "settling", "moved", "done", "gave-up"

    def __init__(
        self,
        writer: Any,
        *,
        dialect: Any = ClaudeDialect,
        window_seconds: float = 120.0,
        quiet: float = 1.5,
        max_attempts: int = 3,
        clock: Callable[[], float] = time.monotonic,
        gate_record: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._writer = writer
        self._dialect = dialect
        self._clock = clock
        self._deadline = clock() + window_seconds
        self._quiet = quiet
        self._max_attempts = max_attempts
        self._tail = b""
        self._last_output = clock()
        self.state = self.WATCH
        self.attempts = 0
        self.sent: list = []

    @property
    def fired(self) -> bool:
        return self.state in (self.DONE, self.GAVE_UP)

    # -- output side (proxy output watcher) ----------------------------------

    def __call__(self, data: bytes) -> None:
        if self.fired or self._clock() > self._deadline:
            return
        self._last_output = self._clock()
        self._tail = (self._tail + data)[-16384:]
        if self.state == self.WATCH:
            flat = flatten(self._tail)
            if any(m in flat for m in self._dialect.TRUST_PROMPT_MARKERS):
                self.state = self.SETTLING

    # -- time side (proxy tick) ------------------------------------------------

    def selection(self) -> Optional[str]:
        """'yes' / 'no' / None from the last rendered cursor in the tail."""
        flat = flatten(self._tail)
        i = flat.rfind(_CURSOR)
        if i < 0:
            return None
        after = flat[i + 1:i + 4]
        return "yes" if after.startswith("yes") else ("no" if after.startswith("no") else None)

    def tick(self) -> None:
        if self.fired or self.state == self.WATCH:
            return
        now = self._clock()
        if now > self._deadline:
            self._send("gave-up", None)
            self.state = self.GAVE_UP
            return
        if now - self._last_output < self._quiet:
            return  # the dialog is still (re-)rendering
        if self.state == self.SETTLING:
            self._down()
            return
        # MOVED: verify before committing
        sel = self.selection()
        if sel == "yes":
            self._send("enter", self._dialect.TRUST_ACCEPT_KEYS[1])
            self.state = self.DONE
        elif self.attempts < self._max_attempts:
            self._down()
        else:
            self._send("gave-up", None)
            self.state = self.GAVE_UP

    def _down(self) -> None:
        self.attempts += 1
        self._send("down", self._dialect.TRUST_ACCEPT_KEYS[0])
        self.state = self.MOVED
        self._last_output = self._clock()

    def _send(self, step: str, key: Optional[bytes]) -> None:
        from .hooks import FOLDER_TRUST_CAP, FOLDER_TRUST_CAP_KEY

        event = InjectionEvent(source=SOURCE_BOOTSTRAP, trigger=f"folder-trust-accept:{step}",
                               content=f"<folder-trust {step} attempt={self.attempts}>",
                               meta={FOLDER_TRUST_CAP_KEY: FOLDER_TRUST_CAP})

        def _emit(_ev: Any) -> None:
            if key is not None:
                self._writer.inject(key)
                self.sent.append(key)

        self._writer.admit(event, _emit)

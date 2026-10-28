"""Operator input tap — ``/`` routing + the ``\\`` command plane (PTY-hooks WS-B).

Sits between the operator's stdin and :meth:`SingleWriteSite.write_keys`. It is
NOT a terminal emulator: it tracks ONE bounded fact —
the text typed since the last submit, as far as it can still be known — with
exactly two editing keys understood (CR submits, BACKSPACE/DEL erases). Anything
that could move a cursor or rewrite the line (an escape sequence, Tab, Ctrl-A/E/
U/K/W, ...) PUNTS: the rest of that line is raw passthrough and the assembled
text is marked unknown. The operator's bytes are forwarded unchanged except in
the one case the spec asks for: a line that STARTS with the ``\\`` sigil.

Routing, decided on the first non-whitespace byte of a line:

* ``\\…`` — the OPERATOR COMMAND PLANE. Bytes are held (echoed locally so the
  operator sees them), never reach the child. On CR the line is dispatched to a
  registered operator command. ``\\hooks off|on`` is RESERVED: it is dispatched
  before any hook runs, so a misconfigured (even deny-all) hook cannot lock the
  operator out. A ``\\…`` that matches no command FALLS THROUGH: the held bytes +
  CR are forwarded to the child unchanged (preserving the CLI's own ``\\`` use,
  e.g. Claude Code's backslash-Enter newline).
* ``/…`` — forwarded as typed; on CR the assembled line is run through the
  hook pipeline as a ``source: operator`` event BEFORE the CR is forwarded. A
  DENY withholds the CR (the line stays unsent in the prompt).
* anything else — raw passthrough, byte-identical.

Every submitted line (any route) produces ONE ``source: operator`` provenance
record — this is what turns the old ``# NOT logged`` into a recorded fact.
Every read calls ``on_activity`` (the monitor's ``note_operator_activity``).

2026-09-29: on Claude Code the ``\\`` plane is
owned by the UserPromptSubmit hook (``prompt_command_hook``) and this tap runs
with ``command_plane=False`` — it only records lines and runs the ``/`` route.
It remains the command dispatcher when no self socket exists (the fallback).
Line assembly follows: mouse motion/wheel,
query replies and focus reports are transparent in every mode; Shift-only
printable CSI-u / modifyOtherKeys keys are text; every punt records a
``punt_cause``; a punted registered ``\\command`` holds its first Enter.

Known bounds (stated, not hidden): a slash-menu Enter that SELECTS a completion
submits text the tap did not see (it records what was typed); Tab/arrows punt;
a ``\\`` typed after leading whitespace that was already forwarded is ordinary
text. Echo + feedback are written to the operator's terminal and the child is
sent SIGWINCH so its TUI redraws over them (LIVE-verify the redraw).
"""

from __future__ import annotations

import os
import re
import signal
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .hooks import (
    POINT_OPERATOR_COMMAND,
    SOURCE_OPERATOR,
    ClaudeDialect,
    Decision,
    InjectionEvent,
    InjectionGate,
    content_fields,
)

SIGIL = 0x5C  # "\\"
CR = 0x0D
BS = (0x7F, 0x08)
CTRL_C = 0x03
#: A replayed chunk this close AFTER the closing row's stamp is still ambiguous (the stamp may be early): see
#: ``OperatorInputTap.restore_line_snapshot``.
AMBIGUOUS_AFTER_STAMP = 1.0
CTRL_U = 0x15  # line-kill: clear the line (and any stale punt)
ESC = 0x1B
BEL = 0x07
#: r2: the effect-ledger trigger of the D50 line restore (same name as its record kind).
RESTORE_TRIGGER = "operator-line-restore"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"


def _not_saved(err: Optional[str]) -> str:
    """round 2 (g65(c)): the reply suffix when the full copy failed."""
    return f" WARNING: the full copy of this line was NOT saved ({err})." if err else ""
_WS = (0x20, 0x09)

#: Commands that NEVER pass through the operator-command hook pipeline.
RESERVED_COMMANDS = frozenset({"hooks"})

#: Where a ``\\command`` line was recognized:
#: the byte-level tap (fallback, non-Claude runtimes) or Claude Code's
#: UserPromptSubmit hook (the decoded prompt text — the primary route on Claude).
SURFACE_TAP = "tap"
SURFACE_PROMPT_HOOK = "prompt-hook"

_INSTANCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")

# modes
_NORMAL = "normal"
_WS_PENDING = "ws-pending"
_COMMAND = "command"
_PUNT = "punt"


@dataclass
class CommandContext:
    name: str
    args: str
    raw: str


class OperatorInputTap:
    """Feed it the operator's stdin chunks; it forwards / intercepts / records."""

    def __init__(
        self,
        writer: Any,
        gate: InjectionGate,
        *,
        stdout_fd: Optional[int] = 1,
        on_activity: Optional[Callable[[], None]] = None,
        command_plane: bool = True,
        child_pid: Optional[int] = None,
        feedback_writer: Optional[Callable[[bytes], None]] = None,
        on_submit: Optional[Callable[[], None]] = None,
        on_input: Optional[Callable[[], None]] = None,
    ) -> None:
        self._writer = writer
        # 2026-09-29 (cross-harness operator activity): called once per operator
        # Enter (any route), AFTER the line was processed (so line_state is fresh).
        self._on_submit = on_submit
        # Fix pass (audit LOW): called only when a chunk carries REAL operator
        # input — not a chunk made solely of terminal reports (focus in/out,
        # DA/DSR/DECRPM replies, OSC/DCS theme reports), mouse motion/wheel or a
        # kitty key-release (the sequences the line assembler already treats as
        # transparent). ``on_activity`` keeps its legacy every-read semantics.
        self._on_input = on_input
        self._gate = gate
        self._stdout_fd = stdout_fd
        self._on_activity = on_activity
        self.command_plane = bool(command_plane)
        self._child_pid = child_pid
        self._feedback_writer = feedback_writer
        self._commands: Dict[str, Callable[[CommandContext], Optional[str]]] = {}
        self._fallback: Optional[Callable[[CommandContext], Optional[str]]] = None
        self._help: Dict[str, str] = {}
        self._dry = False   # True only inside a replay: no writes, echoes, records, commands or flag clears
        self._forced_busy: Optional[str] = None
        self._replay_inexact: Optional[str] = None   # set by an AMBIGUOUS replay: the tap's line text is not vouched
        self._reset_line()
        self._in_paste = False
        self.dispatched: List[str] = []  # observable (tests / \status)
        # the full-copy operator-messages log (an operator_messages.
        # OperatorMessageLog) and this session's instance id. None = no copy kept.
        self.message_log: Optional[Any] = None
        self.message_log_instance: Optional[str] = None
        # round 2 (g64): restore_unsent's long-line rule (None = legacy bytes);
        # the launcher sets LONG_LINE_PASTE_BYTES when inject_long_line_as_paste is ON.
        self.long_line_paste_bytes: Optional[int] = None
        self._restore_beat_seconds = 0.2
        self._restore_sleep: Callable[[float], None] = time.sleep
        #: D3: monotonic time of the last REAL operator keystroke
        #: (terminal reports / focus / mouse do not count), ``None`` = none yet.
        self._last_input_at: Optional[float] = None
        #: D1: a bounded journal of (monotonic time, raw bytes) of every
        #: REAL operator chunk, so the dialog-close restore can REPLAY the keys typed after the dialog
        #: actually closed onto the restored line state (it discards only keys typed while it was open).
        self._key_journal: Deque[Tuple[float, bytes]] = deque(maxlen=512)

    # -- registry ------------------------------------------------------------

    def register(self, name: str, handler: Callable[[CommandContext], Optional[str]], help: str = "") -> None:
        self._commands[name] = handler
        if help:
            self._help[name] = help

    def set_fallback(self, handler: Callable[[CommandContext], Optional[str]]) -> None:
        """Handler for ``\\<word> <msg>`` when ``<word>`` is not a command (WS-F DM).
        It returns feedback text, or raises ``LookupError`` to FALL THROUGH."""
        self._fallback = handler

    def commands(self) -> Dict[str, str]:
        return {n: self._help.get(n, "") for n in sorted(self._commands)}

    # -- state ---------------------------------------------------------------

    def _clear_busy_marks(self) -> None:
        """End the forced-busy / inexact marks: call ONLY after bytes that reach the child's prompt and end its line."""
        self._forced_busy = None
        self._replay_inexact = None

    def _reset_line(self, reaches_child: bool = True) -> None:
        """Reset the tap's line assembly. ``reaches_child=False`` is for a keystroke the harness SWALLOWS (a
        ``\\command``, its Ctrl-C or sigil-Backspace): the child's prompt never saw it, so it proves nothing about
        what the prompt holds and the forced-busy / inexact marks stay (F1, round 4)."""
        if not self._dry and reaches_child:
            # ONLY a keystroke that reaches the child's prompt (the operator's Enter / Ctrl-U / Ctrl-C / ESC
            # forwarded to it, or the harness clearing the prompt) ends a forced-busy mark; a swallowed
            # operator command never does, and a replay never does (rounds 3-4)
            self._forced_busy = None
            self._replay_inexact = None
        self._mode = _NORMAL
        self._line = bytearray()
        self._held_ws = bytearray()
        self._cmd = bytearray()
        #: why this line punted (class/codes
        # only, never text), the command a punted \line was aimed at, and
        # whether its first Enter was already withheld.
        self._punt_cause: Optional[str] = None
        self._punt_cmd: Optional[str] = None
        self._withheld = False
        # D15 fix pass 2: printable text is known to be on the line even though
        # the tap punted (typed/pasted after the punt, or present when it punted).
        self._punt_typed = False

    def _punt(self, cause: str) -> None:
        if self._mode != _PUNT:
            self._punt_cause = cause or "unknown"
            if self._line:
                self._punt_typed = True   # text was on the line when it punted
        self._mode = _PUNT
        self._line.clear()

    @property
    def at_line_start(self) -> bool:
        return self._mode == _NORMAL and not self._line

    #: D15 fix pass: what the tap KNOWS about the operator's unsent line.
    LINE_EMPTY = "empty"
    LINE_PARTIAL = "partial"
    LINE_UNKNOWN = "unknown"

    @property
    def line_state(self) -> str:
        """``empty`` (nothing typed since the last submit), ``partial`` (the tap
        holds typed-but-unsent text, a pending leading space, or a \\command in
        progress), or ``unknown`` (the line punted — cursor / history / Tab /
        Ctrl-L / paste: the tap can no longer see what is in the prompt)."""
        if self._forced_busy:
            return self.LINE_PARTIAL   # the tap cannot vouch the prompt is empty (see ``force_busy``)
        if self._mode == _PUNT:
            # fix pass 2: text typed/pasted after the punt (or present when it
            # punted) means the line is PARTIAL — hold until Enter.
            return self.LINE_PARTIAL if self._punt_typed else self.LINE_UNKNOWN
        if self._mode == _NORMAL and not self._line:
            return self.LINE_EMPTY
        return self.LINE_PARTIAL

    # -- D3: the exact unsent line, and putting it back ---------------

    def seconds_since_input(self) -> Optional[float]:
        """Seconds since the last REAL operator keystroke, or ``None`` if there
        has been none since this tap was built."""
        if self._last_input_at is None:
            return None
        return max(0.0, time.monotonic() - self._last_input_at)

    def unsent_text(self) -> Optional[str]:
        """The operator's unsent line text when the tap knows it EXACTLY (a plain
        typed line, or leading whitespace withheld by the tap), else ``None``: a
        punted line (cursor / history / Tab / paste) or a ``\\command`` in progress
        cannot be reproduced, so it is never claimed (nor when the line is force-marked busy, nor when the
        line state came from an AMBIGUOUS replay: the replayed keys may be a dialog's answer, not his text)."""
        if self._forced_busy or self._replay_inexact:
            return None
        if self._mode == _NORMAL:
            return bytes(self._line).decode("utf-8", "replace")
        if self._mode == _WS_PENDING:
            return bytes(self._line + self._held_ws).decode("utf-8", "replace")
        return None

    _SNAP_FIELDS = ("_mode", "_line", "_held_ws", "_cmd", "_punt_cause", "_punt_cmd", "_withheld",
                    "_punt_typed", "_in_paste", "_forced_busy", "_replay_inexact")

    def snapshot_line(self) -> Dict[str, Any]:
        """An opaque copy of the tap's line state (for the dialog-keystroke rule: keys typed while a
        dialog is open belong to the dialog, so the line is restored to this snapshot when it closes)."""
        out: Dict[str, Any] = {}
        for k in self._SNAP_FIELDS:
            v = getattr(self, k, None)
            out[k] = bytearray(v) if isinstance(v, bytearray) else v
        out["_snap_at"] = time.monotonic()
        return out

    def input_between(self, t0: Optional[float], t1: float) -> bool:
        """True if a real operator chunk was fed at a ``time.monotonic()`` in ``[t0, t1]`` (``t0 is None``:
        in the last 2 s before ``t1``). Reads the key journal; used to spot keys typed in the window where the
        tap cannot tell pre-dialog text from the dialog's own keys."""
        lo = (t1 - 2.0) if t0 is None else t0
        return any(lo <= t <= t1 for (t, _d) in self._key_journal)

    def force_busy(self, cause: str) -> None:
        """Mark the operator line BUSY regardless of what the tap's own line buffer says. Used where the tap
        can no longer vouch that the child's prompt is empty (a dialog closed at an unknown instant, a replay
        that cannot tell the dialog's own Enter from the operator's). ``line_state`` reads ``partial`` until
        the operator's next Enter / Ctrl-U / Ctrl-C / ESC (any line reset) or ``\\approve --into-dialog``
        (``clear_forced_busy``). A held item may wait; it is never typed over his text."""
        self._forced_busy = cause or "unknown"

    def clear_forced_busy(self) -> Optional[str]:
        """Drop the forced-busy mark (the operator's explicit release). Returns the cause it had, or ``None``."""
        cause, self._forced_busy = self._forced_busy, None
        self._replay_inexact = None
        return cause

    @property
    def forced_busy(self) -> Optional[str]:
        return self._forced_busy

    def restore_line_snapshot(self, snap: Dict[str, Any], since: Optional[float] = None,
                              stamp: Optional[float] = None) -> int:
        """Put the tap's line state back to ``snap`` (no bytes are written to the child).

        ``since`` (a ``time.monotonic()`` value) is where the REPLAY starts: the operator keys the journal
        holds from then on (and not before the snapshot) are REPLAYED onto the restored state, side-effect
        free. The caller passes the closing row's stamp MINUS a safety margin, so a key typed after the true
        close but before Claude Code wrote the row is replayed, never discarded (the row trails the close by
        a measured latency). ``stamp`` (optional) is the row's own mapped time: a replayed chunk older than it
        is AMBIGUOUS (it may be a dialog key or the operator's), and so is one up to ``AMBIGUOUS_AFTER_STAMP``
        seconds AFTER it (a stamp that is too early would class the dialog's own Enter as post-close). If any ambiguous chunk was replayed and the
        snapshot held text but the replay left the line empty (the dialog's own Enter was re-processed as the
        operator's), the line is force-marked BUSY: the child's prompt may still hold that text.
        Returns how many chunks were replayed. ``None`` = no replay (the bare snapshot)."""
        snap_at = snap.get("_snap_at")
        for k, v in snap.items():
            if k in self._SNAP_FIELDS:
                setattr(self, k, bytearray(v) if isinstance(v, bytearray) else v)
        if since is None:
            return 0
        snap_busy = self.line_state != self.LINE_EMPTY
        floor = max(since, snap_at) if isinstance(snap_at, (int, float)) else since
        chunks = [(t, d) for (t, d) in self._key_journal if t >= floor]
        overflowed = (len(self._key_journal) == self._key_journal.maxlen
                      and self._key_journal[0][0] > floor)       # the oldest chunks needed were evicted
        self._dry = True
        try:
            for _t, d in chunks:
                self.feed(d)
        finally:
            self._dry = False
        if overflowed:
            self.force_busy("journal-overflow")      # cannot replay faithfully: never claim the line is empty
        ambiguous = stamp is not None and any(t < stamp + AMBIGUOUS_AFTER_STAMP for (t, _d) in chunks)
        if ambiguous:
            self._replay_inexact = "ambiguous-replay"   # unsent_text() claims nothing until a live reset
        if ambiguous and snap_busy and self.line_state == self.LINE_EMPTY:
            self.force_busy("ambiguous-replay")
        return len(chunks)

    def discard_unsent(self) -> None:
        """The harness cleared the child's prompt (the ESC ESC of a self compact):
        drop the tap's own view of the line so line_state reads ``empty``."""
        self._reset_line()   # reaches_child=True: the harness itself emptied the child's prompt

    def restore_unsent(self, text: str) -> None:
        """Put ``text`` back in the child's input line WITHOUT submitting it (no
        CR), through the operator-keystroke path (ungated, as if typed), and make
        the tap's line state ``partial`` again so no harness notice concatenates
        onto it. A multi-line text is bracketed-pasted so no newline submits."""
        raw = text.encode("utf-8")
        if not raw:
            return
        if b"\r" in raw:
            raise ValueError("refusing to restore text containing a CR (it would submit)")
        # g79 exemption 5 (round 3 P2-3): this write does not pass InjectionGate.decide, so the line-empty
        # guard lives HERE too, not only in the one caller (compact_gate.py): never type onto a busy line.
        if self.line_state != self.LINE_EMPTY:
            raise ValueError("refusing to restore: the operator's line is not empty (line_state="
                             f"{self.line_state})")
        # round 2 (Diamond F4, checklist g64): the SAME payload phases as every
        # injection. A single saved line over ``long_line_paste_bytes`` is bracketed too:
        # unbracketed, Claude Code keeps only the last 1022-byte PTY read of it. Not
        # submitted (no CR follows), so no trailing-backslash padding. ``None`` (flag OFF,
        # or a bare tap) = the legacy bytes: multi-line bracketed, single line raw.
        from .injector import payload_phases

        phases = payload_phases(text, self.long_line_paste_bytes, submit=False)

        def _write_phases(_ev: Any = None) -> None:
            for i, chunk in enumerate(phases):
                if i:
                    self._restore_sleep(self._restore_beat_seconds)
                self._forward(chunk)

        # r2 (F2): this is HARNESS-originated text typed into the PTY (acceptance-2's
        # half-typed case), so it is ledgered as its own effect (trigger operator-line-restore).
        # Flag off: run_ledgered is a plain call, byte-identical to before.
        if self._gate is not None and hasattr(self._gate, "run_ledgered"):
            from .hooks import SOURCE_HARNESS

            ev = InjectionEvent(source=SOURCE_HARNESS, content=text, trigger=RESTORE_TRIGGER,
                                meta={})
            self._gate.run_ledgered(ev, _write_phases, route="restore")
        else:
            _write_phases()
        # reaches_child=True: the text was just typed into the child; line is then set to it. Reachability: the
        # only caller (compact_gate.py:903) runs only when ``ctl.line_state() == "empty"``, which reads
        # ``partial`` while forced-busy, so no live mark can be present here (F5-5).
        self._reset_line()
        self._mode = _NORMAL
        self._line = bytearray(raw)
        # D50fix P3: a just-restored line is not idle: the takeover clock restarts
        self._last_input_at = time.monotonic()

    def _note_submit(self) -> None:
        if self._dry:
            return
        if self._on_submit is not None:
            try:
                self._on_submit()
            except Exception:  # noqa: BLE001 - an observer never breaks the tap
                pass

    # -- output helpers ------------------------------------------------------

    def _forward_submitted(self, raw_bytes: bytes) -> None:
        """(rounds 3-4; Diamond F15/F20, checklist g64 + its 2026-10-02
        amendment): type a whole line the tap hands back to the child, then submit it,
        with the writes from ``injector.submit_writes``: one ``line + CR`` write only up to
        40 bytes; longer, the CR is its own write after a beat; over 512 bytes the line is
        a bracketed paste. Long-line rule OFF (``long_line_paste_bytes is None``) = the
        legacy single write. A trailing backslash is NOT padded: it is the operator's own
        (Claude Code newline) keystroke (test_hooks_ws_b_operator_tap pins "\\\r")."""
        from .injector import submit_writes

        writes = submit_writes(raw_bytes, self.long_line_paste_bytes, pad_trailing_backslash=False)
        for i, chunk in enumerate(writes):
            if i:
                self._restore_sleep(self._restore_beat_seconds)
            self._forward(chunk)

    def _forward(self, data: bytes) -> None:
        if data and not self._dry:
            self._writer.write_keys(bytes(data))

    def _echo(self, data: bytes) -> None:
        if self._dry:
            return
        if self._feedback_writer is not None:
            self._feedback_writer(bytes(data))
            return
        if self._stdout_fd is not None:
            try:
                os.write(self._stdout_fd, data)
            except OSError:
                pass

    def feedback(self, text: str) -> None:
        if self._dry:
            return
        self._echo(b"\r\n[gm] " + text.replace("\n", "\r\n       ").encode("utf-8", "replace") + b"\r\n")
        self._redraw()

    def _redraw(self) -> None:
        if self._child_pid and not self._dry:
            try:
                os.kill(self._child_pid, signal.SIGWINCH)
            except OSError:
                pass

    # -- the feed ------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if not self._dry:
            if is_operator_input(data):
                self._key_journal.append((time.monotonic(), bytes(data)))
            if self._on_activity is not None:
                self._on_activity()
        if is_operator_input(data) and not self._dry:
            self._last_input_at = time.monotonic()
        if self._on_input is not None and not self._dry and is_operator_input(data):
            try:
                self._on_input()
            except Exception:  # noqa: BLE001 - an observer never breaks the tap
                pass
        out = bytearray()
        i = 0
        n = len(data)
        # A lone ESC / ESC-ESC keypress clears Claude's prompt -> back to line start.
        if data in (b"\x1b", b"\x1b\x1b") and self._mode != _COMMAND:
            self._forward(data)
            self._reset_line()   # reaches_child=True: the lone ESC was forwarded and clears the child's prompt
            return
        while i < n:
            b = data[i]
            # -- bracketed paste from the operator's terminal: never a submit.
            if data.startswith(PASTE_START, i):
                if self._mode == _COMMAND:
                    self._abandon_command(out, "bracketed-paste")
                elif self._mode == _WS_PENDING:
                    out += self._held_ws
                    self._held_ws.clear()
                self._in_paste = True
                self._punt("bracketed-paste")
                out += PASTE_START
                i += len(PASTE_START)
                continue
            if self._in_paste:
                if data.startswith(PASTE_END, i):
                    self._in_paste = False
                    out += PASTE_END
                    i += len(PASTE_END)
                    continue
                self._punt_typed = True   # pasted content is on the line
                out.append(b)
                i += 1
                continue

            # -- UNSOLICITED terminal report / query response (focus in/out,
            #    DA/DA2, DSR, DECRPM, bounded OSC/DCS theme reports): a response
            #    the terminal sends TO the app in reply to its own query — NOT a
            #    thing an operator keystroke can produce (keystroke-ambiguous
            #    finals u/m/R are deliberately excluded and punt). Consume it
            #    transparently — forward it to the child (its TUI may have
            #    requested it) WITHOUT punting and WITHOUT touching _line, so a
            #    subsequent \command on the same logical line is still recognized.
            # — now in EVERY mode, including while a
            #    \command is held, plus mouse / query replies / encoded keys:
            #
            if b == ESC:
                i = self._feed_escape(data, i, out)
                continue

            if self._mode == _COMMAND:
                i = self._feed_command(data, i, out)
                continue

            if self._mode == _WS_PENDING:
                if b in _WS:
                    self._held_ws.append(b)
                    i += 1
                    continue
                if b == SIGIL:
                    self._held_ws.clear()
                    self._enter_command()
                    i += 1
                    continue
                out += self._held_ws
                self._line += self._held_ws
                self._held_ws.clear()
                self._mode = _NORMAL
                # fall through: reprocess b in normal mode

            if self._mode == _NORMAL and not self._line and self.command_plane:
                if b in _WS:
                    self._mode = _WS_PENDING
                    self._held_ws.append(b)
                    i += 1
                    continue
                if b == SIGIL:
                    self._enter_command()
                    i += 1
                    continue

            # -- normal / punt processing
            if b == CR:
                self._forward(out)
                out = bytearray()
                self._submit_line()
                self._note_submit()
                i += 1
                continue
            out.append(b)
            if b in BS:
                if self._mode == _NORMAL and self._line:
                    _pop_char(self._line)
            elif b == CTRL_C:
                self._reset_line()   # reaches_child=True: the byte is in `out`, forwarded to the child
            elif b == CTRL_U:
                # line-kill: clear the assembled line AND any stale punt, so a
                # \command typed after it is recognized at line start again.
                self._reset_line()   # reaches_child=True: Ctrl-U is forwarded and kills the child's line
            elif b < 0x20 and b not in (0x0A,):
                # line editing / completion (Tab, Ctrl-A/E/K/W, ...) -> punt
                self._punt(_control_cause(b))
            elif self._mode == _NORMAL:
                self._line.append(b)
            elif self._mode == _PUNT and b >= 0x20 and b != 0x7F:
                self._punt_typed = True   # fix pass 2: text typed after the punt
            i += 1
        self._forward(out)

    # -- escape sequences -------------------

    def _feed_escape(self, data: bytes, i: int, out: bytearray) -> int:
        """Handle the ESC at ``data[i]`` in the current mode; return the new index."""
        seq = _classify_escape(data, i)
        raw = data[i:i + seq.length]
        end = i + seq.length
        act = seq.action
        if act == _ACT_MOUSE_BTN:
            # A click cannot edit a held command (not in the child's prompt) or an
            # empty line; on a non-empty line it may move the cursor -> punt.
            empty = self._mode == _WS_PENDING or (self._mode == _NORMAL and not self._line)
            act = _ACT_XPARENT if (self._mode in (_COMMAND, _PUNT) or empty) else _ACT_PUNT
        if act == _ACT_XPARENT:
            out += raw
            return end

        if self._mode == _COMMAND:
            if act == _ACT_TEXT:
                self._cmd += seq.text
                self._echo(seq.text)
                return end
            if act == _ACT_SUBMIT:
                self._forward(out)
                out.clear()
                self._dispatch_command()
                self._note_submit()
                return end
            if act == _ACT_BS:
                self._command_backspace()
                return end
            self._abandon_command(out, seq.cause)
            out += raw
            return end

        if self._mode == _WS_PENDING:
            if act == _ACT_TEXT and seq.text == b"\\":
                self._held_ws.clear()
                self._enter_command()
                return end
            out += self._held_ws
            self._line += self._held_ws
            self._held_ws.clear()
            self._mode = _NORMAL

        if act == _ACT_TEXT:
            if self._mode == _NORMAL and not self._line and self.command_plane and seq.text == b"\\":
                self._enter_command()
                return end
            out += raw
            if self._mode == _NORMAL:
                self._line += seq.text
            elif self._mode == _PUNT:
                self._punt_typed = True   # fix pass 2: an encoded text key after the punt
            return end
        if act == _ACT_SUBMIT:
            self._forward(out)
            out.clear()
            self._submit_line(submit_bytes=raw)
            self._note_submit()
            return end
        if act == _ACT_BS:
            out += raw
            if self._mode == _NORMAL and self._line:
                _pop_char(self._line)
            return end
        out += raw
        self._punt(seq.cause)
        return end

    # -- command mode --------------------------------------------------------

    def _enter_command(self) -> None:
        self._mode = _COMMAND
        self._cmd = bytearray([SIGIL])
        self._echo(b"\\")

    def _intended_command(self) -> Optional[str]:
        """The operator-plane command a held ``\\line`` was aimed at, if it names
        one: a registered command, or a DM target when the fallback is installed."""
        body = bytes(self._cmd[1:]).decode("utf-8", "replace")
        name, sep, _ = body.partition(" ")
        if name in self._commands:
            return name
        if sep and self._fallback is not None and _INSTANCE_RE.fullmatch(name or ""):
            return name
        return None

    def _abandon_command(self, out: bytearray, cause: str = "") -> None:
        """Punt out of command mode: the held bytes go to the child as typed."""
        intended = self._intended_command()
        out += self._cmd
        self._cmd = bytearray()
        self._punt(cause)
        self._punt_cmd = intended
        if intended is not None:
            self.feedback(f"\\{intended} NOT processed ({self._punt_cause}): the text was passed to the "
                          f"prompt instead. Enter is held once — Ctrl-U clears, a second Enter sends it "
                          f"to the agent as plain text.")
        else:
            self._redraw()

    def _command_backspace(self) -> None:
        if len(self._cmd) > 1:
            _pop_char(self._cmd)
            self._echo(b"\b \b")
        else:  # erased the sigil itself: back to an empty line (swallowed: the child never saw it)
            self._echo(b"\b \b")
            self._reset_line(reaches_child=False)

    def _feed_command(self, data: bytes, i: int, out: bytearray) -> int:
        b = data[i]
        if b == CR:
            self._forward(out)
            out.clear()
            self._dispatch_command()
            self._note_submit()
            return i + 1
        if b in BS:
            self._command_backspace()
            return i + 1
        if b == CTRL_C:
            self._echo(b"^C")
            self._reset_line(reaches_child=False)
            self._redraw()
            return i + 1
        if b < 0x20 and b not in _WS:
            self._abandon_command(out, _control_cause(b))
            return i  # reprocess this byte in punt mode
        self._cmd.append(b)
        self._echo(bytes([b]))
        return i + 1

    def _dispatch_command(self) -> None:
        if self._dry:       # replay: the command already ran live; only the line state matters
            self._reset_line()   # dry replay: marks untouched
            return
        raw_bytes = bytes(self._cmd)
        raw = raw_bytes.decode("utf-8", "replace")
        self._reset_line(reaches_child=False)   # swallowed by the harness: the child's prompt never saw it (F1)
        res = self.execute_command(raw, surface=SURFACE_TAP)
        if res["decision"] == "fall-through":
            # FALL-THROUGH: not ours -> the child gets exactly what was typed (and its Enter). FORWARD FIRST;
            # the marks end only once the bytes were written (a raising write leaves them set, F5-1), and
            # not at all for a trailing-backslash line (Claude Code newline: the prompt still holds text, F5-10).
            self._forward_submitted(raw_bytes)
            if not raw_bytes.endswith(b"\\"):
                self._clear_busy_marks()
            self._redraw()
            return
        self.feedback(res["reply"])

    @staticmethod
    def _note_feature_use(name: str, is_dm: bool, decision: str) -> None:
        """X26: ONE feature event when a registered operator command
        executed. The feature id comes from the CLOSED set (``command_feature_id`` reads the registry node's
        ``aliases``; an unregistered word returns None and writes nothing); the arguments never reach the
        emitter. The ``\\<instance>`` DM is dispatched by a pattern, not a registered word: it emits its own
        literal ``operator-dm`` (how that site is keyed, literal or alias, is OPEN to the architect: spec
 question-asked; one line here if he prefers an alias). Guarded: telemetry
        never changes a command's result. The actor is the viewer (``ch``)."""
        try:
            from .. import feature_events as _fe
            from .. import feature_sites as _fs

            outcome = "ok" if decision == "dispatched" else "error"
            if is_dm:
                _fe.feature_event("operator-dm", _fe.VIEWER_ACTOR, "operator-command", outcome=outcome)
            elif _fs.command_feature_id(name) is not None:
                _fe.feature_event(_fs.command_feature_id(name), _fe.VIEWER_ACTOR, "operator-command",
                                  outcome=outcome)
        except Exception:  # noqa: BLE001
            pass

    def _log_command_line(self, raw: str, name: Optional[str], decision: str, surface: str) -> Optional[str]:
        """the FULL ``\\command`` line into the operator-messages log (its
        sha256 equals the operator-command record's ``content_sha256``). Never raises.
        Returns ``None`` when saved (or no log), else the error, which the caller puts
        in the operator's reply (round 2, g65(c): never silent)."""
        log = self.message_log
        if log is None:
            return None
        try:
            if log.log_message(kind="command", text=raw, from_instance=self.message_log_instance,
                               command=name, decision=decision, surface=surface) is None:
                return getattr(log, "last_error", None) or "write failed"
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {exc}"
        return None

    def execute_command(self, raw: str, *, surface: str = SURFACE_TAP) -> Dict[str, Any]:
        """Run ONE ``\\command`` line through the registry — the SINGLE
        implementation shared by the byte-level tap and the Claude Code
 ``UserPromptSubmit`` hook.

        Returns ``{ok, decision, command, reply}``. ``decision`` is one of
        ``dispatched`` / ``error`` / ``deny`` / ``fall-through`` (tap: not ours,
        forward it) / ``unknown`` (prompt-hook: not ours, BLOCK it visibly — a
        submitted prompt cannot be handed back to the TUI as typed bytes)."""
        at_tap = surface == SURFACE_TAP
        extra = {} if at_tap else {"surface": surface}
        body = raw[1:] if raw.startswith("\\") else raw
        name, _, args = body.strip().partition(" ")
        args = args.strip()
        ctx = CommandContext(name=name, args=args, raw=raw)

        handler = self._commands.get(name) if name else None
        if handler is None and name and self._fallback is not None and args and _INSTANCE_RE.fullmatch(name):
            handler = self._fallback

        def _not_ours(reason: str = "") -> Dict[str, Any]:
            decision = "fall-through" if at_tap else "unknown"
            log_err = self._log_command_line(raw, name or None, decision, surface)
            self._gate.record("operator-command", source=SOURCE_OPERATOR, command=name or None,
                              decision=decision, **({"reason": reason} if reason else {}), **extra,
                              **content_fields(raw))
            listing = " ".join("\\" + n for n in sorted(self._commands))
            return {"ok": False, "decision": decision, "command": name or None,
                    "reply": f"unknown command \\{name} — NOT sent to the agent. Commands: {listing}"
                             + (" \\<instance> <msg>" if self._fallback is not None else "")
                             + f" (\\{name} {reason})" * bool(reason) + _not_saved(log_err)}

        if handler is None:
            return _not_ours()

        reserved = name in RESERVED_COMMANDS
        if not reserved:
            event = InjectionEvent(source=SOURCE_OPERATOR, content=raw, trigger=f"\\{name}",
                                   parsed={"kind": "operator-command", "command": "\\" + name, "args": args})
            result = self._gate.evaluate(event, self._gate.command_pipeline)
            if result.decision in (Decision.DENY, Decision.DEFER):
                log_err = self._log_command_line(raw, name, "deny", surface)
                self._gate.record("operator-command", source=SOURCE_OPERATOR, command=name,
                                  decision=Decision.DENY.value, reason=result.reason, handler=result.handler,
                                  **extra, **content_fields(raw))
                return {"ok": False, "decision": "deny", "command": name,
                        "reply": f"\\{name} blocked by hook ({result.reason}). \\hooks off disables content hooks."
                                 + _not_saved(log_err)}
        try:
            reply = handler(ctx)
            decision = "dispatched"
            reason = ""
        except LookupError as exc:  # fallback declined -> not ours
            return _not_ours(str(exc))
        except Exception as exc:  # noqa: BLE001 - never crash the pump on a command
            reply = f"\\{name} failed: {exc}"
            decision = "error"
            reason = str(exc)
        self.dispatched.append(name)
        log_err = self._log_command_line(raw, name, decision, surface)
        self._gate.record("operator-command", source=SOURCE_OPERATOR, command=name, reserved=reserved,
                          decision=decision, reason=reason, forwarded=False, **extra, **content_fields(raw))
        self._note_feature_use(name, handler is self._fallback and name not in self._commands, decision)
        if not reserved and not self._gate.command_post_pipeline.handlers:
            self._gate.command_post_pipeline.report_no_consumers()   # X26: the point was reached, nothing attached
        if not reserved and self._gate.command_post_pipeline.handlers:
            post = InjectionEvent(source=SOURCE_OPERATOR, content=raw, trigger=f"\\{name}:post",
                                  parsed={"kind": "operator-command", "command": "\\" + name, "args": args},
                                  meta={"reply": reply or ""})
            res = self._gate.command_post_pipeline.run(post)
            self._gate.record("operator-command-post", command=name, decision=res.decision.value,
                              reason=res.reason, handler=res.handler)
        return {"ok": decision == "dispatched", "decision": decision, "command": name,
                "reply": (reply or f"\\{name} ok") + _not_saved(log_err)}

    # -- submit of a non-command line ----------------------------------------

    def _submit_line(self, submit_bytes: bytes = b"\r") -> None:
        if self._dry:       # replay: same line-state transition as below, no record / feedback / write
            if self._mode == _PUNT and self._punt_cmd is not None and not self._withheld:
                self._withheld = True
            else:
                self._reset_line()   # dry replay: marks untouched
            return
        punted = self._mode == _PUNT
        punt_cause = self._punt_cause if punted else None
        if punted and self._punt_cmd is not None and not self._withheld:
            # a FAILED \command must never look
            # like a sent message: hold its first Enter, say so; a second Enter
            # (the line stays punted) sends it as plain text.
            self._withheld = True
            self._gate.record("operator-command", source=SOURCE_OPERATOR, command=self._punt_cmd,
                              decision="not-processed", reason="line punted", punt_cause=punt_cause,
                              forwarded=False)
            self.feedback(f"\\{self._punt_cmd} NOT processed ({punt_cause}) and NOT sent. "
                          f"Ctrl-U clears; Enter again sends it to the agent as plain text.")
            return
        text = None if punted else bytes(self._line).decode("utf-8", "replace")
        # F1b (round 5): the Enter may be WITHHELD (a hook deny on a slash line), in which case the child's
        # prompt still holds the text and forced-busy must stay. Reset the line assembly first WITHOUT
        # clearing the marks; clear them only once the submit was actually written to the child.
        self._reset_line(reaches_child=False)
        parsed = ClaudeDialect.parse(text) if text is not None else None
        if text is not None and text.lstrip().startswith("/"):
            event = InjectionEvent(source=SOURCE_OPERATOR, content=text, parsed=parsed, trigger="operator-submit",
                                   meta={"route": "slash"})
            keeps_text = text.endswith("\\")      # a trailing backslash is Claude Code's newline, not a submit

            def _emit(_ev: Any) -> None:
                # the Enter reaches the child NOW (immediately, or later on a hook-DEFER release): only then
                # does the prompt stop holding the text
                self._forward(submit_bytes)
                if not keeps_text:
                    self._clear_busy_marks()

            outcome = self._gate.decide(event, _emit)
            if outcome.written and not keeps_text:
                self._clear_busy_marks()
            if not outcome.written:
                # g79 direction rule (F5-3): the Enter was WITHHELD (hook deny / defer), so the child's prompt
                # STILL holds the text although the tap's line assembly was reset. Mark the line busy
                # explicitly (a held harness item then waits; it is never typed over his text). Cleared only
                # by a keystroke that reaches the child (reaches-child rule), or by the deferred Enter.
                if not self._forced_busy:      # keep an earlier cause (it is the more specific record)
                    self.force_busy("slash-submit-withheld")
                self.feedback(f"{(parsed or {}).get('command', 'line')} not submitted: {outcome.decision.value} "
                              f"({outcome.reason}). \\hooks off disables content hooks.")
            return
        extra = {"punt_cause": punt_cause} if punted else {}
        self._gate.record(
            "injection", source=SOURCE_OPERATOR, trigger="operator-submit", decision="allow",
            reason="passthrough", route="punted" if punted else "plain", clear=False,
            parsed_command=(parsed or {}).get("command"), **extra, **content_fields(text),
        )
        self._forward(submit_bytes)
        # plain / punted-second-Enter route: written to the child, so the marks end, EXCEPT a trailing
        # backslash (Claude Code newline, not a submit: the prompt still holds text, F5-10)
        if not (text is not None and text.endswith("\\")):
            self._clear_busy_marks()


def is_operator_input(data: bytes) -> bool:
    """True iff ``data`` holds at least one byte the OPERATOR produced — i.e. it is
    not made up solely of sequences :func:`_classify_escape` classifies as
    transparent (terminal reports / query replies, mouse motion & wheel, key
    releases). Reuses the line assembler's classification; pure."""
    i, n = 0, len(data)
    while i < n:
        if data[i] != ESC:
            return True
        seq = _classify_escape(data, i)
        if seq.action != _ACT_XPARENT:
            return True
        i += max(1, seq.length)
    return False


def _pop_char(buf: bytearray) -> None:
    """Remove the last UTF-8 character (not just the last byte)."""
    while buf and (buf[-1] & 0xC0) == 0x80:  # trailing continuation bytes
        buf.pop()
    if buf:  # the lead (or ASCII) byte
        buf.pop()


def _is_report_csi(private: bytes, params: bytes, intermediates: bytes, final: int) -> bool:
    """True iff a parsed CSI sequence is an UNSOLICITED, keystroke-UNAMBIGUOUS
    terminal report we make transparent — a query response the terminal sends TO
    the app, never something a keypress can produce.

    Deliberately EXCLUDES (so they PUNT as genuine operator input, the safe
    pre-fix behavior):
      * cursor/editing finals A B C D H F, and the ``~`` of Delete/PageUp/Fn keys;
      * CSI ``u`` — kitty key encoding: EVERY keypress under kitty mode;
      * CSI ``m`` — modifyOtherKeys key encoding AND SGR/SGR-mouse (e.g. the mouse
        report ESC[<0;10;5m), indistinguishable from a keystroke report;
      * CPR ``R`` — ESC[1;2R is Shift+F3, indistinguishable from a cursor-position
        report on stdin.
    (2026-09-29: ``u``/``m``/``R`` were removed after a re-Diamond found them
    matching real operator keystrokes. LATER 2026-09-29: the PRIVATE-prefixed
    forms — SGR mouse ``CSI <…M/m``, kitty-flags reply ``CSI ?…u``, DECXCPR
    ``CSI ?…R`` — are never keystrokes and are handled by
    :func:`_classify_escape`, not here.)"""
    # DECRPM report: ESC [ [?]<params> $ y  (intermediate '$', final 'y').
    if intermediates == b"$" and final == 0x79:  # 'y'
        return True
    if intermediates:
        return False  # any other intermediate: not a report we recognize
    # Focus in/out: ESC[I / ESC[O — exact, no params, no private prefix.
    if final in (0x49, 0x4F) and not params and not private:  # 'I' / 'O'
        return True
    # DA1/DA2 reply: ESC[?...c / ESC[>...c — MUST carry the private prefix (a plain
    # ESC[c is a request, never a stdin reply), so a bare 'c' is not consumed.
    if final == 0x63:  # 'c'
        return private in (b"?", b">")
    # DSR reply: ESC[?...n (e.g. theme query 2031) or ESC[<digits>n — a REPORT form,
    # requiring a '?' private prefix OR a numeric param, never a bare 'n' key. Any
    # other private prefix (> < =) is not a DSR reply.
    if final == 0x6E:  # 'n'
        if private not in (b"", b"?"):
            return False
        return private == b"?" or any(0x30 <= x <= 0x39 for x in params)
    return False


#: Max OSC/DCS body we will scan before giving up (a real theme/report reply is
#: tens of bytes; a longer run is not a report we should consume transparently).
_STRING_SCAN_CAP = 256


def _scan_string_terminated(data: bytes, start: int, body_start: int) -> int:
    """Length of an OSC/DCS string (``start`` = the ESC) whose body begins at
    ``body_start`` and ends at BEL (0x07) or ST (``ESC \\``). 0 if unterminated
    within this chunk, or malformed, so the caller keeps the safe punt.

    BOUNDED (regression fix 2026-09-29): the scan stops at the first C0 control
    byte other than BEL/ESC — a CR (0x0d), LF (0x0a) etc. is NOT part of a theme
    report and terminates the scan as a NON-match (return 0), so an embedded
    ``\\r`` is never consumed across (which would swallow a following ``\\command``
    and drop its provenance record). It also caps the scan at ``_STRING_SCAN_CAP``
    bytes; on overflow it returns 0. Only BEL and a valid ST (``ESC \\``) match."""
    n = len(data)
    limit = min(n, body_start + _STRING_SCAN_CAP)
    k = body_start
    while k < limit:
        c = data[k]
        if c == BEL:
            return k - start + 1
        if c == ESC:
            if k + 1 < n and data[k + 1] == 0x5C:  # ST = ESC '\'
                return (k + 1) - start + 1
            return 0  # a lone ESC that is not ST: not a report terminator -> punt
        if c < 0x20:  # any other C0 control (CR/LF/...): NOT a report -> punt
            return 0
        k += 1
    return 0  # unterminated within the chunk, or cap overflow -> safe punt


def _match_terminal_report(data: bytes, i: int) -> int:
    """Byte length of a COMPLETE terminal-report escape sequence starting at the
    ESC ``data[i]``, else 0. Recognizes the keystroke-unambiguous CSI reports
    (focus in/out ``I``/``O``, DA/DA2 ``c``, DSR ``n``, DECRPM ``$y``), bounded
    OSC (``ESC ] … ST/BEL``) and bounded DCS (``ESC P … ST/BEL``). Conservative by
    design: cursor/editing CSIs, keystroke-ambiguous finals (``u``/``m``/``R``) and
    any incomplete/unknown sequence return 0 so the caller keeps the safe punt."""
    n = len(data)
    if i >= n or data[i] != ESC or i + 1 >= n:
        return 0
    c1 = data[i + 1]
    if c1 == 0x5B:  # '[' -> CSI
        j = i + 2
        private_start = j
        if j < n and data[j] in (0x3F, 0x3E, 0x3D, 0x3C):  # ? > = <
            j += 1
        private = data[private_start:j]
        params_start = j
        while j < n and 0x30 <= data[j] <= 0x3F:  # parameter bytes
            j += 1
        params = data[params_start:j]
        inter_start = j
        while j < n and 0x20 <= data[j] <= 0x2F:  # intermediate bytes (e.g. '$')
            j += 1
        intermediates = data[inter_start:j]
        if j >= n:
            return 0  # incomplete: no final byte in this chunk
        final = data[j]
        if not (0x40 <= final <= 0x7E):
            return 0  # not a valid CSI final
        if _is_report_csi(private, params, intermediates, final):
            return j - i + 1
        return 0
    if c1 == 0x5D:  # ']' -> OSC
        return _scan_string_terminated(data, i, i + 2)
    if c1 == 0x50:  # 'P' -> DCS
        return _scan_string_terminated(data, i, i + 2)
    return 0


# -- escape-sequence classification for line assembly
#
# Claude Code 2.1.284 in iTerm2 pushes kitty keyboard flags 5 (CSI >5u), xterm
# modifyOtherKeys 2 (CSI >4;2m), focus events (?1004), bracketed paste (?2004)
# and SGR ANY-MOTION mouse tracking (?1000/?1002/?1003/?1006), and queries
# CSI ?u / CSI ?6n / CSI c / CSI >0q at startup (measured in harness_stdout.log).
# So the stdin stream carries mouse motion, query replies and (possibly) encoded
# keys that are NOT line edits. Each ESC sequence is classified into ONE action:

_ACT_XPARENT = "transparent"   # forward unchanged, line state untouched (any mode)
_ACT_MOUSE_BTN = "mouse-button"  # transparent unless it could move a live cursor
_ACT_TEXT = "text"             # a printable key: assemble ``text``, forward raw
_ACT_SUBMIT = "submit"         # encoded plain Enter: behaves like CR
_ACT_BS = "backspace"          # encoded plain Backspace: behaves like 0x7f
_ACT_PUNT = "punt"             # genuine editing / modified key: punt the line

_KITTY_PUA = (57344, 63743)  # kitty functional-key codepoints (not text)
_MOD_SHIFT = 1
_MOD_IGNORED = 64 | 128  # Caps Lock / Num Lock state bits: not a chord


@dataclass
class _Seq:
    length: int
    action: str
    cause: str = ""
    text: bytes = b""


def _int_or(s: bytes, default: int) -> Optional[int]:
    if not s:
        return default
    try:
        return int(s)
    except ValueError:
        return None


def _printable_cp(cp: Optional[int]) -> bool:
    if cp is None or cp < 0x20 or 0x7F <= cp <= 0x9F or cp > 0x10FFFF:
        return False
    if 0xD800 <= cp <= 0xDFFF or _KITTY_PUA[0] <= cp <= _KITTY_PUA[1]:
        return False
    return True


def _shift_upper(cp: int) -> Optional[int]:
    """The Shift form of a LETTER (layout-independent); None for anything else."""
    ch = chr(cp)
    up = ch.upper()
    if ch != up and len(up) == 1:
        return ord(up)
    return None


#: Base (unshifted) keys whose Shift form is layout-dependent: a Shift+<one of
#: these> reported by its base code cannot be resolved without a keymap -> punt.
_SHIFT_AMBIGUOUS_BASE = frozenset(b"`1234567890-=[]\\;',./")


def _classify_key(code: Optional[int], mods: Optional[int], cls: str, *, shifted: Optional[int] = None,
                  text_cp: Optional[List[int]] = None, code_is_produced: bool = False) -> "_Seq":
    """Shared decision for a CSI-u (kitty) or modifyOtherKeys encoded key."""
    if code is None or mods is None:
        return _Seq(0, _ACT_PUNT, f"{cls} malformed")
    m = (mods - 1) & ~_MOD_IGNORED if mods >= 1 else 0
    cause = f"{cls} key={code} mods={mods}"
    if m & ~_MOD_SHIFT:  # Ctrl / Alt / Super / Hyper / Meta: a chord, not text
        return _Seq(0, _ACT_PUNT, cause)
    shift = bool(m & _MOD_SHIFT)
    if code == 13:
        return _Seq(0, _ACT_PUNT if shift else _ACT_SUBMIT, cause if shift else "")
    if code in (127, 8):
        return _Seq(0, _ACT_PUNT if shift else _ACT_BS, cause if shift else "")
    if not _printable_cp(code):  # Esc 27, Tab 9, functional keys, ...
        return _Seq(0, _ACT_PUNT, cause)
    if text_cp and all(_printable_cp(c) for c in text_cp):
        return _Seq(0, _ACT_TEXT, text="".join(chr(c) for c in text_cp).encode("utf-8"))
    if not shift:
        return _Seq(0, _ACT_TEXT, text=chr(code).encode("utf-8"))
    if shifted is not None and _printable_cp(shifted):
        return _Seq(0, _ACT_TEXT, text=chr(shifted).encode("utf-8"))
    up = _shift_upper(code)
    if up is not None:
        return _Seq(0, _ACT_TEXT, text=chr(up).encode("utf-8"))
    if code_is_produced and not (code < 0x80 and code in _SHIFT_AMBIGUOUS_BASE):
        # modifyOtherKeys reports the PRODUCED character (e.g. 58 ':' for Shift+;)
        return _Seq(0, _ACT_TEXT, text=chr(code).encode("utf-8"))
    return _Seq(0, _ACT_PUNT, cause + " shifted-key-unknown")


_CURSOR_FINALS = {0x41: "up", 0x42: "down", 0x43: "right", 0x44: "left", 0x48: "home", 0x46: "end"}


def _classify_escape(data: bytes, i: int) -> _Seq:
    """Classify the escape sequence starting at ``data[i]`` (an ESC). Always
    returns a length >= 1. Pure; never touches tap state."""
    rlen = _match_terminal_report(data, i)
    if rlen:
        return _Seq(rlen, _ACT_XPARENT)
    n = len(data)
    if i + 1 >= n:
        return _Seq(1, _ACT_PUNT, "esc-incomplete")
    c1 = data[i + 1]
    if c1 == 0x4F:  # SS3 (application cursor / F1-F4)
        if i + 2 < n:
            return _Seq(3, _ACT_PUNT, f"ss3 {chr(data[i + 2])!s}" if 0x20 <= data[i + 2] < 0x7F else "ss3")
        return _Seq(2, _ACT_PUNT, "esc-incomplete")
    if c1 != 0x5B:  # ESC <x>: legacy Alt+key, or an OSC/DCS we did not match
        label = chr(c1) if 0x20 <= c1 < 0x7F else f"0x{c1:02x}"
        return _Seq(1, _ACT_PUNT, f"esc-prefix {label}")
    j = i + 2
    private = b""
    if j < n and data[j] in (0x3F, 0x3E, 0x3D, 0x3C):
        private = data[j:j + 1]
        j += 1
    ps = j
    while j < n and 0x30 <= data[j] <= 0x3F:
        j += 1
    params = data[ps:j]
    ist = j
    while j < n and 0x20 <= data[j] <= 0x2F:
        j += 1
    inter = data[ist:j]
    if j >= n or not (0x40 <= data[j] <= 0x7E):
        return _Seq(1, _ACT_PUNT, "esc-incomplete")
    final = data[j]
    length = j - i + 1
    fields = params.split(b";")

    # SGR mouse: CSI < b ; x ; y (M|m). Never a keystroke.
    if private == b"<" and final in (0x4D, 0x6D) and not inter:
        b = _int_or(fields[0], -1)
        if b is None or b < 0:
            return _Seq(length, _ACT_PUNT, "mouse malformed")
        if b & 64 or (b & 32 and (b & 3) == 3):  # wheel, or motion with no button
            return _Seq(length, _ACT_XPARENT)
        kind = "drag" if b & 32 else ("press" if final == 0x4D else "release")
        return _Seq(length, _ACT_MOUSE_BTN, f"mouse-{kind} b={b}")
    # kitty keyboard-flags reply CSI ? flags u; DECXCPR reply CSI ? row ; col R.
    if private == b"?" and not inter and final in (0x75, 0x52):
        return _Seq(length, _ACT_XPARENT)
    # kitty CSI-u key: CSI code[:shifted[:base]] [; mods[:event] [; text]] u
    if final == 0x75 and not private and not inter:
        sub0 = fields[0].split(b":")
        code = _int_or(sub0[0], -1)
        shifted = _int_or(sub0[1], -1) if len(sub0) > 1 and sub0[1] else None
        sub1 = fields[1].split(b":") if len(fields) > 1 else [b""]
        mods = _int_or(sub1[0], 1)
        event = _int_or(sub1[1], 1) if len(sub1) > 1 else 1
        if event == 3:  # a key RELEASE (only with flag 2): not input
            return _Seq(length, _ACT_XPARENT)
        text_cp = None
        if len(fields) > 2 and fields[2]:
            text_cp = [_int_or(t, -1) or -1 for t in fields[2].split(b":")]
        s = _classify_key(code if code is not None and code >= 0 else None, mods, "csi-u",
                          shifted=shifted if shifted is not None and shifted >= 0 else None, text_cp=text_cp)
        s.length = length
        return s
    # xterm modifyOtherKeys: CSI 27 ; mods ; code ~
    if final == 0x7E and not private and not inter and len(fields) == 3 and fields[0] == b"27":
        s = _classify_key(_int_or(fields[2], -1), _int_or(fields[1], 1), "modifyOtherKeys",
                          code_is_produced=True)
        s.length = length
        return s
    if final in _CURSOR_FINALS and not private:
        return _Seq(length, _ACT_PUNT, f"cursor-key {_CURSOR_FINALS[final]}")
    if final == 0x7E:
        return _Seq(length, _ACT_PUNT, f"csi-tilde {params.decode('ascii', 'replace')}")
    label = chr(final)
    return _Seq(length, _ACT_PUNT, f"csi final={label}" + (f" private={private.decode()}" if private else ""))


def _control_cause(b: int) -> str:
    if b == 0x09:
        return "tab 0x09"
    return f"control-byte 0x{b:02x}"

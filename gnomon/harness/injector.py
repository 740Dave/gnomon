"""The slash-command injection sequence.

The exact, ordered, byte-level sequence for typing a command into the child as
if a human typed it. Each phase is a SEPARATE write through the single write
site, with a beat between phases so a paste-buffering TUI ingests before submit.

    1. ESC ESC                     (clear any prompt text)   } OPT-IN since
    2. beat (0.2s)                                            } WS-D (clear=True)
    3. command text, VERBATIM, no trailing newline
       (wrapped in bracketed-paste markers iff multi-line)
    4. beat (0.2s)
    5. carriage-return            (submit — MUST be \\r, never \\n)

The beat is an injected callable (defaults to :func:`time.sleep`) so tests can
assert the phase ordering deterministically without wall-clock waits.
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from .constants import (
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    DEFAULT_BEAT_SECONDS,
    LONG_LINE_PASTE_BYTES,
    PTY_READ_CHUNK_BYTES,
    ESC_ESC,
    SUBMIT,
    SUBMIT_SINGLE_WRITE_MAX_BYTES,
)
from .hooks import Decision, SOURCE_OPERATOR, SOURCE_SELF_VERB, SOURCE_UNATTRIBUTED, ClaudeDialect, InjectionEvent, Outcome
from .leading_bang import ScreenRefused, screen_text, split_chunk_leads_with_bang
from .writer import SingleWriteSite


def payload_phases(text: str, limit: Optional[int], *, submit: bool = True) -> List[bytes]:
    """THE rule for typing text into a Claude Code TUI (checklist g64):
    every path that types text (the Injector, ``optap.restore_unsent``, any future
    one) gets its write(s) from here. Returns the ordered writes; the caller puts a
    beat between consecutive writes (a ``/token`` must register before its paste).
    A path that also SUBMITS takes its writes from :func:`submit_writes`, which adds
    the CR tier (round 4: a CR is its own write once ``line + CR`` exceeds 40 bytes).

    * multi-line -> one bracketed paste (unchanged legacy behaviour);
    * ``limit is None`` (``inject_long_line_as_paste`` OFF) -> the legacy
      single write (multi-line still bracketed);
    * a single line over ``limit`` UTF-8 bytes -> one bracketed paste, except a long
      ``/command args`` line (token at most 512 bytes), which keeps its command TOKEN bare, then the rest as a
      paste (a bracketed ``/token`` is text, not a command);
    * ``submit=True`` and the text ends in a backslash -> one trailing space, so the
      following CR submits instead of continuing the line (Claude Code 2.1.287 reads
      backslash + CR as a continuation, bracketed or not). ``submit=False`` (a
      restore that is deliberately NOT submitted) leaves the text exactly as typed.
    """
    raw = text.encode("utf-8")
    if limit is not None and submit and raw.endswith(b"\\"):
        raw = raw + b" "
    if b"\n" in raw:
        return [BRACKETED_PASTE_START + raw + BRACKETED_PASTE_END]
    if limit is None or len(raw) <= limit:
        return [raw]
    if raw.startswith(b"/") and b" " in raw:
        token, _, rest = raw.partition(b" ")
        bare = token + b" "
        # r2e (r2d Diamond F1 P1; g64): the bare token is the ONE unbracketed write here, so it obeys the
        # same rules as any raw single-line write: never over LONG_LINE_PASTE_BYTES (512) and never a
        # chunk that would lead with '!' at a PTY read split. A real command name is never that long
        # (``/clear``, ``/compact``, ``/prepare-for-compact``); a first word over the bound is not a
        # command, so the whole line is typed as ONE bracketed paste (delivered whole, as text) rather
        # than as a raw write whose last 1022-byte chunk could open bash mode.
        if (rest and len(bare) <= LONG_LINE_PASTE_BYTES
                and not split_chunk_leads_with_bang(bare, PTY_READ_CHUNK_BYTES)):
            return [bare, BRACKETED_PASTE_START + rest + BRACKETED_PASTE_END]
    return [BRACKETED_PASTE_START + raw + BRACKETED_PASTE_END]


def submit_writes(raw: bytes, limit: Optional[int], *, pad_trailing_backslash: bool,
                  cr_always_separate: bool = False, phases: Optional[Sequence[bytes]] = None) -> List[bytes]:
    """THE single implementation of "type this and submit it" (rounds 4-5;
    checklist g64 + its 2026-10-02 amendment). EVERY submitting TUI write takes its writes
    from here: the Injector (``inject_line`` and ``compact``) and the tap's ``\\command``
    fall-through. The caller writes them in order, with its beats between them; the CR is
    always the LAST element, and nothing else in the package builds a line + CR
    (``tests/test_x43_operator_messages.py`` scans for it).

    Tiers (long-line rule ON, ``limit`` not None; ``cr_always_separate=False``):

    * ``raw + CR`` is at most ``SUBMIT_SINGLE_WRITE_MAX_BYTES`` (40) and the phases leave
      the line unchanged -> ONE write ``raw + CR``;
    * longer, up to ``limit`` (512) -> the line as one UNBRACKETED write, then the CR as
      its OWN write (the real TUI absorbs a CR that rides in a >~60-byte burst);
    * over ``limit`` (or multi-line, or a long ``/token``) -> ``payload_phases`` (a
      bracketed paste), then the CR as its own write.

    ``cr_always_separate=True`` (the Injector): the CR is ALWAYS its own write, at every
    size and with the rule OFF too (stricter than the 40-byte tier; round 5 kept the
    Injector's emitted writes byte-identical, proven by a differential digest).
    ``phases`` (the Injector's ``compact``): the caller's own pre-built writes, used as-is
    (its token + always-bracketed argument shape); only the CR is added here.
    ``pad_trailing_backslash`` is ``payload_phases``' ``submit``: True for harness text,
    False for the operator's own typing (backslash + Enter is then a deliberate newline).
    Bytes that are not valid UTF-8 are never bracketed. ``limit is None`` (the flag OFF)
    with ``cr_always_separate=False`` -> the legacy single write ``raw + CR``."""
    if phases is None:
        if limit is None and not cr_always_separate:
            return [raw + SUBMIT]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            phases = [raw]
        else:
            phases = payload_phases(text, limit, submit=pad_trailing_backslash)
    phases = list(phases)
    if (not cr_always_separate and phases == [raw]
            and len(raw) + len(SUBMIT) <= SUBMIT_SINGLE_WRITE_MAX_BYTES):
        return [raw + SUBMIT]
    return phases + [SUBMIT]


class Injector:
    """Types commands into the child through the single write site.

    Parameters
    ----------
    writer:
        The :class:`SingleWriteSite`. Every phase is written via
        ``writer.inject`` (so the whole sequence is logged, and cannot be
        mistaken by the child for a keystroke).
    beat_seconds:
        Seconds to wait between phases. Live-tuned default 0.2s.
    sleep:
        The wait function. Injected for testability.
    """

    def __init__(
        self,
        writer: SingleWriteSite,
        beat_seconds: float = DEFAULT_BEAT_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        dialect: Any = ClaudeDialect,
        session_dir: Optional[str] = None,
        long_line_paste_bytes: Optional[int] = None,
    ) -> None:
        self._writer = writer
        # a SINGLE-line payload longer than this many UTF-8 bytes is typed
        # as an explicit bracketed paste (None = the legacy unbracketed write). See
        # :meth:`_payload_phases` for the measured defect this closes.
        self._long_line_paste_bytes = long_line_paste_bytes
        self._beat_seconds = beat_seconds
        self._sleep = sleep
        # WS-A: the CLI-specific recogniser (the only dialect-dependent piece).
        self._dialect = dialect
        self._session_dir = session_dir
        #: (open_work_list): ``(line_fn, sources)`` or None. None = inject_line is byte-identical to before.
        self._presence: Optional[Any] = None

    def set_presence(self, line_fn: Optional[Callable[[], Optional[str]]], sources: Sequence[str] = (),
                     on_fault: Optional[Callable[[Dict[str, Any]], None]] = None) -> None:
        """rule-presence-line: every message of a listed (harness-originated) source gets exactly ONE presence line
        appended, written by the harness. ``line_fn=None`` removes it. A failing ``line_fn`` adds nothing. If the gate REFUSES the
        message because of the line (a deny, or a screen refusal at the write), the message is sent again WITHOUT the line and
        ``on_fault({...})`` is called: presence text can never block a wake-up (round 3, F1)."""
        self._presence = (line_fn, frozenset(sources), on_fault) if line_fn is not None else None

    # -- primitives ----------------------------------------------------------

    def _beat(self) -> None:
        self._sleep(self._beat_seconds)

    @staticmethod
    def _encode_payload(text: str) -> bytes:
        """Encode command text, bracketed-paste-wrapped iff it is multi-line.

        The payload carries NO trailing newline; submission is the separate
        carriage-return phase. A multi-line payload is wrapped so the TUI does
        not treat an embedded newline as a submit.
        """
        raw = text.encode("utf-8")
        if b"\n" in raw:
            return BRACKETED_PASTE_START + raw + BRACKETED_PASTE_END
        return raw

    def _payload_phases(self, text: str) -> List[bytes]:
        """The phase-3 write(s) for ``text``.

        ⛔ MEASURED DEFECT (2026-10-02T04:40:19Z): a 1,107-byte single-line
        broadcast was written UNBRACKETED in one ``os.write``. A macOS PTY hands the
        reader at most 1,022 bytes per ``read()`` (measured: 1109 -> [1022, 87];
        3000 -> [1022, 1022, 956]), and Claude Code 2.1.287 kept only the LAST
        unbracketed chunk: every recipient received exactly the final 85 characters
        (1107 - 1022). Reproduced on a real, isolated Claude Code 2.1.287 (``--bare``,
        temp HOME): 1107 -> 85, 1142 -> 120, 2100 -> 56 submitted chars, with or
        without ``@`` mentions in the text; 1023 -> the lone last ``!`` flipped the
        prompt into bash mode. Live on 2026-10-02T05:03:47Z a 1,142-char line reached 2
        of 4 sessions as 120 chars (timing-dependent, so never safe). A bracketed
        paste is accumulated to its end marker whatever the read split (real Claude
        Code: 1107 and 4600 chars arrive whole). So a single line over
        ``long_line_paste_bytes`` is wrapped too. A long ``/command args`` line keeps
        its command TOKEN unbracketed (a bracketed ``/token`` is pasted text, not a
        command: the flight-3 lesson, see :meth:`compact`), then the rest as a paste.

        Multi-line, short, and ``long_line_paste_bytes=None`` are byte-identical to
        the legacy single write, except that (with the setting on) a payload ending in
        a backslash gets one trailing space so the CR still submits it."""
        return payload_phases(text, self._long_line_paste_bytes, submit=True)

    # -- the sequence --------------------------------------------------------

    def inject_line(
        self,
        text: str,
        *,
        clear: bool = False,
        source: str = SOURCE_UNATTRIBUTED,
        trigger: Optional[str] = None,
        target: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Outcome:
        """Type ``text`` as one submitted line, per the exact phase sequence.

        This is the general "type as if human" primitive. Slash commands
        (``/clear``, ``/prepare-for-compact``, ``/compact ...``) are ordinary
        text passed here — the harness does not special-case them.

        ``clear`` — ⚠ DEFAULT INVERTED by PTY-hooks WS-D (2026-09-27): the
        default is now ``clear=False``, the NON-DISRUPTIVE variant: no ``ESC
        ESC`` (and no clear beat), so the inject does not interrupt a working
        agent — Claude Code queues the submitted line. ⛔ CONTRACT: without the
        clear, the text CONCATENATES with anything already typed-but-unsent in
        the prompt. ``clear=True`` is the OPT-IN disruptive 5-phase sequence
        (``ESC ESC`` -> beat -> text -> beat -> ``\r``); the self-compact path
        (:meth:`compact`) opts in because it intrinsically ends the turn.
        ``ESC ESC`` is never removed unconditionally — earlier tooling
        ``ptycore.py:366`` records the collision bug that re-introduces.

        WS-A: the line is submitted to the writer as ONE ``InjectionEvent``
        (``source`` / ``trigger`` attribute it). If a hook gate is installed on
        the writer the phases are written only on ALLOW/MODIFY; the returned
        :class:`~gnomon.harness.hooks.Outcome` says what happened.
        """
        _line = None
        if self._presence is not None and source in self._presence[1]:
            try:
                _line = self._presence[0]()
            except Exception:  # noqa: BLE001 - the line is information; it can never block a wake-up
                _line = None
            if not _line or _line in text:
                _line = None
        plain = text
        if _line:
            text = text + "\n" + _line
        try:
            out = self._admit_text(text, clear, source, trigger, target, meta)
            # Only a SCREEN refusal (control bytes / leading '!') can be the line's doing; a hook's own deny of the message is final (no second
            # pass through the operator's hooks, no misleading fault).
            _why = str(getattr(out, "reason", "") or "")
            refused: Optional[BaseException] = (RuntimeError(_why) if (getattr(out, "decision", None) == Decision.DENY
                                                                    and ("control-bytes" in _why or "leading-bang" in _why)) else None)
        except ScreenRefused as exc:
            out, refused = None, exc
        if _line and refused is not None and not screen_text(plain):
            # The wake-up must arrive: send it again without the line. Only when the caller's OWN text passes the same screen (if it does
            # not, the cause was never the line: no second attempt, no misleading fault; the refusal stands).
            retry = self._admit_text(plain, clear, source, trigger, target, meta)
            cb = self._presence[2] if self._presence is not None else None
            if cb is not None:
                try:
                    cb({"source": source, "trigger": trigger, "reason": str(refused)[:200],
                        "resent": getattr(getattr(retry, "decision", None), "value", "unknown")})
                except Exception:  # noqa: BLE001
                    pass
            return retry
        if refused is not None and out is None:
            raise refused
        return out

    def _admit_text(self, text: str, clear: bool, source: str, trigger: Optional[str], target: Optional[str],
                    meta: Optional[Dict[str, Any]]) -> Outcome:
        event = InjectionEvent(
            source=source, content=text, clear=bool(clear), target=target,
            parsed=self._dialect.parse(text) if self._dialect is not None else None,
            session_dir=self._session_dir, trigger=trigger, meta=dict(meta or {}),
        )
        return self._writer.admit(event, self._emit_line)

    def _emit_line(self, event: InjectionEvent) -> None:
        """Write one admitted event's phases through the single write site."""
        # Phases 3-5 (round 5): the text (bracketed iff multi-line, or a long
        # single line; a long slash command keeps its token bare), a beat, then the CR as its
        # OWN write: all from the ONE implementation, ``submit_writes``, with the Injector's
        # always-separate CR. A beat between every consecutive write.
        raw = event.content.encode("utf-8")
        limit = self._long_line_paste_bytes
        if (limit is None and event.source != SOURCE_OPERATOR and b"\n" not in raw
                and split_chunk_leads_with_bang(raw, PTY_READ_CHUNK_BYTES)):
            # r2d (r2c F3): with the long-line rule OFF a single unbracketed write over the PTY read
            # size is cut into <=1022-byte chunks and Claude Code keeps only the LAST one; a chunk that
            # STARTS with '!' would then open bash mode, past the leading-'!' screen (which sees the
            # whole text). Bracket exactly such a payload (a paste is accumulated whole, whatever the
            # read split) by typing it under the 1022-byte long-line limit. Every other flag-OFF payload,
            # and the operator's own text, is written byte-identically to before; flag ON is untouched.
            limit = PTY_READ_CHUNK_BYTES
        writes = submit_writes(raw, limit, pad_trailing_backslash=True, cr_always_separate=True)
        # master_write_nonblocking: the pending cap is checked ONCE for the whole sequence (the clear, every write, the CR) before
        # any part is queued, so a refusal never leaves a part on the wire. Flag OFF: ``_sequence`` does nothing.
        with self._sequence((len(ESC_ESC) if event.clear else 0) + sum(len(c) for c in writes)):
            if event.clear:
                # Phase 1: clear the prompt.
                self._writer.inject(ESC_ESC)
                # Phase 2: beat.
                self._beat()
            for i, chunk in enumerate(writes):
                if i:
                    self._beat()
                self._writer.inject(chunk)

    def _sequence(self, total: int):
        reserve = getattr(self._writer, "reserve_sequence", None)
        return reserve(total) if reserve is not None else contextlib.nullcontext()

    # -- actuation-vocabulary conveniences -----------------------------------
    # These format the text; they do NOT decide to act. The harness types them
    # only when the agent sends the corresponding control command. See
    # actuator.py.

    def inject(self, text: str, **kw: Any) -> Outcome:
        """Alias for :meth:`inject_line` — the ``inject`` actuation verb."""
        return self.inject_line(text, **kw)

    def compact(self, direction: str = "", *, clear: bool = True,
                source: str = SOURCE_UNATTRIBUTED, line_takeover: bool = False) -> Outcome:
        """Type the agent's self-directed ``/compact`` as a COMMAND TOKEN plus an
        explicit bracketed-paste ARGUMENT.

        ⛔ This is a RELAY. It is invoked only when the agent sends a
        ``compact`` command over the control channel. The harness never decides
 to compact on its own (/ D23).

        Flight-3 defect fix: the old path sent ``/compact <direction>`` as ONE
        raw single-line burst. ``_encode_payload`` only bracket-wraps MULTI-line
        content, so a long single-line argument went unbracketed and tripped
        Claude Code's heuristic paste-detection — the whole burst was
        reclassified as a paste, the ``/compact`` token was swallowed into pasted
        text, and no compaction ran. Here the token is typed UNBRACKETED (so it
        parses as a slash command) and the argument is delivered as an EXPLICIT
        bracketed paste (unambiguous argument at ANY length — equivalent to a
        human typing ``/compact`` then pasting the block).

        ``clear`` — ⚠ operator ruling 2026-09-28 (reverses the 2026-09-27 default): the
        ESC ESC clear is DEFAULT-TRUE, opt-out PER CALL. With ``clear=True`` (the
        default) the leading ``ESC ESC`` + beat are emitted, then the ``/compact``
        token, the bracketed-paste argument, a beat, and the CR — the disruptive
        variant that interrupts the caller's turn. Flight 3.2 LIVE-PROVED that a
        self-``/compact`` with ``clear=False`` (no ESC ESC) does NOT fire, so the
        ESC ESC is load-bearing for self-compaction and is on by default.
        ``clear=False`` opts OUT (no ESC ESC / no clear beat — the ``/compact``
        token, argument, beat, CR) and is kept for a future ESC-ESC-vs-timing A/B;
        it is NOT known to fire.
        """
        direction = (direction or "").strip()
        content = "/compact " + direction if direction else "/compact"
        event = InjectionEvent(
            source=source, content=content, clear=bool(clear), target=None,
            parsed=self._dialect.parse(content) if self._dialect is not None else None,
            session_dir=self._session_dir, trigger="compact",
            meta={"compact_argument": direction},
        )
        if line_takeover and source == SOURCE_SELF_VERB:
            # g79: the D50 takeover compact is the one non-operator write allowed onto a partial line; bound
            # to this call path by a process-local capability (the compact gate already saved his text).
            from .hooks import LINE_TAKEOVER_CAP, LINE_TAKEOVER_CAP_KEY

            event.meta[LINE_TAKEOVER_CAP_KEY] = LINE_TAKEOVER_CAP
        return self._writer.admit(event, self._emit_compact)

    def _emit_compact(self, event: InjectionEvent) -> None:
        """Emit ``/compact`` as an UNBRACKETED command token followed by the
        argument as an EXPLICIT bracketed paste, so the command always parses
        regardless of argument length (flight-3 defect fix)."""
        argument = ""
        if isinstance(event.meta, dict):
            argument = str(event.meta.get("compact_argument") or "")
        # Phase 1-2: the leading ESC ESC clear + beat, emitted iff clear is set.
        # ⚠ clear is now PARAMETRIZED (operator ruling 2026-09-28: DEFAULT-TRUE, opt-out
        # per call) — the compact no longer unconditionally clears. When set, the
        # post-ESC_ESC beat lets the paste-buffering TUI ingest the clear before the
        # /compact token arrives.
        # Phase 3a: the command TOKEN, verbatim and UNBRACKETED -> parses as a command.
        # Phase 3b: the ARGUMENT as an EXPLICIT bracketed paste, after a beat so the
        # command token registers before the paste arrives. Phases 4-5: a beat, then the
        # CR, which (round 5) comes from ``submit_writes`` like every other submit.
        own = [b"/compact"]
        if argument:
            own += [b" ", BRACKETED_PASTE_START + argument.encode("utf-8") + BRACKETED_PASTE_END]
        writes = submit_writes(b"/compact", None, pad_trailing_backslash=False,
                               cr_always_separate=True, phases=own)
        with self._sequence((len(ESC_ESC) if event.clear else 0) + sum(len(c) for c in writes)):
            if event.clear:
                self._writer.inject(ESC_ESC)
                self._beat()
            for i, chunk in enumerate(writes):
                # no beat between the token and its separating space (the shape since flight 3)
                if i and not (argument and i == 1):
                    self._beat()
                self._writer.inject(chunk)

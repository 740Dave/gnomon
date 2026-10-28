"""Is the agent parked on an interactive modal prompt right now?

Claude Code's ``AskUserQuestion`` (and ``ExitPlanMode``) tools put a SELECT dialog
on the terminal and block the turn until the human picks. Text typed into that
dialog is not a prompt line: the characters are ignored and the CR we append to a
harness-originated injection SELECTS the highlighted (first / "Recommended")
option. Observed live 2026-10-03 02:15:31Z (a DSD incident): a DSD-answer
notice was typed while an AskUserQuestion was open, the dialog resolved 83 ms later
with its first option, and the notice never reached the model as a user turn. The
harness logged ``written`` + ``acked`` (it cannot see the screen), so the hub
deleted its durable copy: a silent loss AND a spurious answer.

The harness cannot see the TUI, but the transcript shows the pending call: an
``assistant`` row with a ``tool_use`` of an interactive tool that has no
``tool_result`` row after it. ``pending_modal_tool`` reads the transcript tail and
reports that tool's name, or ``None``. Any unreadable / unparseable state returns
``None`` (do not wedge delivery on a monitoring gap): the cost of a false negative
is today's behaviour; the cost of a false positive would be a held notice.

2026-10-03 (architect ruling, round 2): the hold belongs at the ONE place harness-
originated text reaches the PTY, not per sender. :class:`ModalGuard` is the signal
(transcript pending-call + a best-effort PTY screen-marker bridge) and
``InjectionGate.decide`` is the choke point that consults it (hooks.py): every
non-operator injection kind is DEFERred (queued FIFO) while a dialog is open and
released once, in order, after it closes. Operator keystrokes never pass the gate.

stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable, List, NamedTuple, Optional, Tuple

from .operator_activity import _TAIL_BYTES, resolve_transcript_path

#: Margin (seconds) subtracted from the closing row's stamp to get the replay start. Claude Code writes
#: the closing ``tool_result`` row AFTER the dialog resolves, so the stamp trails the true close (E2). Set
#: from the MEASURED answer-keystroke-to-row latency (max ~115 ms over 30 trials; see
#: docs/build-logs/RUN_SUMMARY-modal-hold.md): 2.0 s is a chosen margin (about 17x the measured max, above
#: the 0.5 s floor), NOT a bound: the outer PostToolUse hook timeout is 5 s (launcher POSTTOOL_HOOK_TIMEOUT),
#: so a stall beyond 2.0 s is a stated limit (L1). Conservative direction: a larger M only makes the
#: operator line look busier for longer.
CLOSE_MARGIN_SECONDS = 2.0

#: A closing-row stamp this far ahead of the guard's own clock is not a real stamp (the row was written
#: BEFORE the guard read it): treat as unknown -> BUSY, rather than clamping to now.
_FUTURE_TOLERANCE = 0.25


class CloseBoundary(NamedTuple):
    """``stamp``: when the closing row says the dialog closed (monotonic). ``replay_from``: ``stamp`` minus the
    safety margin: the journal is replayed from here; chunks older than ``stamp`` are AMBIGUOUS.
    ``CloseBoundary(None, None)`` (:data:`UNKNOWN_CLOSE`) = a dialog DID close but when is unknown."""
    stamp: Optional[float]
    replay_from: Optional[float]
    from_transcript: bool = False     # True when a closing tool_result ROW was seen (not only a force-dismiss)


UNKNOWN_CLOSE = CloseBoundary(None, None, True)


#: Tools that block the turn on a human's selection in a terminal dialog.
MODAL_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})


def pending_modal_tool(transcript_path: str) -> Optional[str]:
    """Name of an interactive tool whose call is still unanswered, else ``None``."""
    return modal_state(transcript_path)[0]


#: First read of a transcript looks this far back (F4: the old 512 KB tail lost a dialog whose
#: ``tool_use`` had scrolled out under background output). Later reads are INCREMENTAL, so a dialog
#: that opened after the first read is remembered however much output follows it.
_INITIAL_BYTES = 4 * 1024 * 1024


def _row_wall_time(row: dict) -> Optional[float]:
    """Epoch seconds of a transcript row's ``timestamp`` (ISO-8601 WITH a zone, ``Z`` ok, or a number); ``None`` if absent or naive (no zone)."""
    ts = row.get("timestamp")
    try:
        if isinstance(ts, (int, float)) and not isinstance(ts, bool):
            return float(ts)
        if isinstance(ts, str) and ts:
            from datetime import datetime

            dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None     # naive stamp: its zone is unknown, so the close time is unknown -> BUSY (F8)
            return dt.timestamp()
    except (ValueError, OverflowError, OSError):
        return None
    return None


class TranscriptScanner:
    """Incremental reader of the unanswered interactive ``tool_use`` calls in one transcript.

    Remembers its offset and the open calls between reads, so (a) a dialog that is open for a long
    time under heavy output is never forgotten (F4) and (b) a poll reads only the new bytes (F10).
    The first read covers the last ``initial_bytes``; if the file is bigger than that, ``truncated``
    is True (the caller records the possible fail-open). A shrunk / replaced file starts over.
    """

    def __init__(self, initial_bytes: int = _INITIAL_BYTES) -> None:
        self._initial = max(1, int(initial_bytes))
        self.path: Optional[str] = None
        self._ino: Optional[int] = None
        self._offset: Optional[int] = None
        self._carry = b""
        self.open_calls: dict = {}  # tool_use_id -> name
        self.open_wall: dict = {}   # tool_use_id -> epoch seconds of the tool_use row's own timestamp (None = none)
        #: wall-clock time (epoch seconds, from the tool_result row's own ``timestamp``; ``None`` = the
        #: row carried none / unparseable) of each modal call closed since ``take_closed`` last ran.
        self.closed_wall: list = []
        self.truncated = False

    def take_closed(self) -> list:
        out, self.closed_wall = self.closed_wall, []
        return out

    def _reset(self, path: str, ino: Optional[int]) -> None:
        self.path, self._ino, self._offset, self._carry = path, ino, None, b""
        self.open_calls = {}
        self.open_wall = {}
        self.closed_wall = []
        self.truncated = False

    def _row(self, line: bytes) -> bool:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return False
        if not isinstance(row, dict):
            return True
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = msg.get("content")
        if not isinstance(content, list):
            return True
        for c in content:
            if not isinstance(c, dict):
                continue
            if row.get("type") == "assistant" and c.get("type") == "tool_use" \
                    and c.get("name") in MODAL_TOOLS and isinstance(c.get("id"), str):
                self.open_calls[c["id"]] = c["name"]
                self.open_wall[c["id"]] = _row_wall_time(row)
            elif c.get("type") == "tool_result":
                if self.open_calls.pop(c.get("tool_use_id"), None) is not None:
                    self.open_wall.pop(c.get("tool_use_id"), None)
                    self.closed_wall.append(_row_wall_time(row))
        return True

    def scan(self, path: str) -> Optional[str]:
        """Update from the file; ``None`` on success, else an error string (caller fails open, says so)."""
        try:
            with open(path, "rb") as fh:
                st = os.fstat(fh.fileno())
                size = st.st_size
                if path != self.path or (self._ino is not None and st.st_ino != self._ino) \
                        or (self._offset is not None and size < self._offset):
                    self._reset(path, st.st_ino)
                first = self._offset is None
                start = max(0, size - self._initial) if first else self._offset
                fh.seek(start)
                data = fh.read(size - start)
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"
        self._offset = start + len(data)
        data = self._carry + data
        if first and start > 0:
            self.truncated = True
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl != -1 else b""
        lines = data.split(b"\n")
        tail = lines.pop()  # the (possibly empty / partial) unterminated last line
        for ln in lines:
            if ln.strip():
                self._row(ln)
        # an unterminated last row that already parses counts now; it is re-read when completed
        self._carry = tail
        if tail.strip():
            snapshot = dict(self.open_calls)
            if self._row(tail):
                self._carry = b""
            else:
                self.open_calls = snapshot
        return None

    def pending(self, ignore: frozenset = frozenset()) -> Optional[str]:
        live = [n for i, n in self.open_calls.items() if i not in ignore]
        return live[-1] if live else None


def pending_modal_tool(transcript_path: str) -> Optional[str]:
    """Name of an interactive tool whose call is still unanswered, else ``None``."""
    return modal_state(transcript_path)[0]


def modal_state(transcript_path: str) -> Tuple[Optional[str], Optional[str]]:
    """``(pending interactive tool | None, error | None)`` (stateless one-shot over the last
    ``_TAIL_BYTES``). ``error`` is set when the transcript could not be read (the caller fails OPEN,
    and says so). The guard itself uses :class:`TranscriptScanner` (incremental, wider)."""
    sc = TranscriptScanner(_TAIL_BYTES)
    err = sc.scan(transcript_path)
    if err:
        return None, err
    return sc.pending(), None


def transcript_modal_reader(session_dir: str) -> Callable[[], Optional[str]]:
    """``() -> tool name | None`` over this session's live transcript (resolved each
    call, so it follows the statusline hook's pointer)."""

    def _read() -> Optional[str]:
        p = resolve_transcript_path(session_dir)
        return pending_modal_tool(p) if p else None

    return _read


# --- the PTY screen bridge + the combined guard ------------------------------------------------

#: THE closed list of dialog kinds (checklist g123): the hold and the waiting-on-operator alert both read it. A new
#: kind brings its SCREEN_MARKERS entry, its wording in ``waiting_alert.KIND_LABEL`` and a test, which enumerates this tuple.
DIALOG_ASK = "AskUserQuestion"
DIALOG_PLAN = "ExitPlanMode"
DIALOG_PERMISSION = "permission-prompt"
DIALOG_KINDS = (DIALOG_ASK, DIALOG_PLAN, DIALOG_PERMISSION)
#: the kinds the waiting alert raises for (first release: permission prompts only). A kind NOT in this set is deliberately not
#: alerted (requirement-scope), which is not the same as being missing from DIALOG_KINDS.
WAITING_ALERT_KINDS = frozenset({DIALOG_PERMISSION})

_ANSI = re.compile(rb"\x1b\[[0-9;?<>=]*[A-Za-z~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\x1b[=>78]")

#: Dialog markers in the ANSI-stripped, whitespace-free, lowercased screen text. MEASURED against
#: the shipped Claude Code 2.1.288 binary strings (``claude.exe``; re-verify on every bump):
#: the AskUserQuestion footer is KeyHint(enter=select) + "Tab/Arrow keys to navigate" (or up/down)
#: + KeyHint(escape=cancel) and its last option is "Chat about this"; ExitPlanMode shows "Ready to
#: code?" / "Would you like to proceed?"; an ordinary tool permission prompt asks "Do you want to
#: proceed?" with a "Yes, and don't ask again for ..." option. Each entry: (kind, all-of tuple).
SCREEN_MARKERS = (
    (DIALOG_ASK, ("chataboutthis",)),
    (DIALOG_ASK, ("entertoselect", "esctocancel")),
    (DIALOG_PLAN, ("readytocode?",)),
    (DIALOG_PLAN, ("wouldyouliketoproceed?",)),
    (DIALOG_PERMISSION, ("doyouwanttoproceed?",)),
    (DIALOG_PERMISSION, ("yes,andnotaskagain", )),
    (DIALOG_PERMISSION, ("yes,anddon'taskagain",)),
)
_FLAT_KEEP = 8192
#: a transcript that grows at or after (countdown end minus this) counts as the countdown running out
_TIMEOUT_TOLERANCE_S = 1.0
#: how much of the stripped screen tail the terminal thread copies for the alert worker (a slice, no matching)
_SNAPSHOT_CHARS = 4096
_MAX_MARKER = 32


def _flat(data: bytes) -> str:
    return re.sub(r"\s+", "", _ANSI.sub(b"", data).decode("utf-8", "replace")).lower()


class ModalGuard:
    """Is a modal dialog open right now? One answer for the whole gate.

    * the TRANSCRIPT leg (authoritative for AskUserQuestion / ExitPlanMode): an unanswered
      interactive ``tool_use``. Unreadable / unresolvable transcript FAILS OPEN and is RECORDED
      (once per outage) through ``on_event`` — never silent.
    * the SCREEN leg (a best-effort bridge, fed as a PTY output watcher): a dialog marker newly
      painted. It closes the first-instant window (dialog painted, transcript row not flushed yet)
      and is the ONLY signal for an ordinary tool permission prompt (no tool_result exists yet for
      a long Bash either, so the transcript cannot tell them apart). A screen hint holds for
      ``screen_window`` seconds, then only while the transcript has not grown since it was seen
      (a blocked turn writes nothing). There is NO time-based release (a failsafe that typed into a
      dialog that might still be open was the consent defect): a hold that outlives its cap is made
      VISIBLE by the gate (``stuck-hold``) and a human clears it (``\\approve --into-dialog``).
    """

    def __init__(self, session_dir: Optional[str] = None, *,
                 transcript_path: Optional[Callable[[], Optional[str]]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 screen_window: float = 2.0, collision_window: float = 1.5,
                 initial_bytes: int = _INITIAL_BYTES,
                 on_event: Optional[Callable[..., Any]] = None,
                 wall: Callable[[], float] = time.time,
                 close_margin: float = CLOSE_MARGIN_SECONDS,
                 waiting: Optional[Any] = None) -> None:
        self._wall = wall
        #: a ``waiting_alert.WaitingHooks`` when (and only when) the flag ``waiting_on_operator_alert`` is ON, else
        #: ``None``: with ``None`` no second tail is kept, no event is added and ``check`` takes the old path (flag-OFF identity).
        self._w = waiting
        self._plain = ""                      # the second bounded tail: ANSI removed, whitespace kept
        self._episode = 0                     # hold episodes opened in this process (starts at 1 at the first)
        self._open: Optional[dict] = None     # the open permission-prompt episode the waiting alert raised for
        self.waiting_errors = 0               # exceptions caught in the waiting path (counted; logged once per episode)
        self._werr_episode = -1
        self._margin = max(0.0, float(close_margin))
        self._dismiss_at: Optional[float] = None
        if transcript_path is None:
            transcript_path = (lambda: resolve_transcript_path(session_dir)) if session_dir else (lambda: None)
        self._path = transcript_path
        self._clock = clock
        self._window = screen_window
        self._scanner = TranscriptScanner(initial_bytes)
        self._dismissed: set = set()
        self._window_noted: Optional[str] = None
        self._collision = collision_window
        self._on_event = on_event
        self._flat_tail = ""
        self._scanned = 0
        self._raw_carry = b""
        self._hint: Optional[Tuple[str, float, Optional[int]]] = None  # kind, seen_at, transcript size
        self._gap_open = False
        self._last_write: Optional[float] = None
        self.suspect_collisions = 0

    # -- recording ------------------------------------------------------------------------------
    def _event(self, what: str, **fields: Any) -> None:
        if self._on_event is not None:
            try:
                self._on_event(what, **fields)
            except Exception:  # noqa: BLE001 - provenance never breaks the guard
                pass

    # -- transcript leg -------------------------------------------------------------------------
    def _size(self) -> Optional[int]:
        try:
            p = self._path()
            return os.path.getsize(p) if p else None
        except OSError:
            return None

    def _transcript_modal(self) -> Optional[str]:
        try:
            p = self._path()
        except Exception as exc:  # noqa: BLE001
            p, err = None, f"path: {type(exc).__name__}: {exc}"
        else:
            err = None if p else "no transcript path resolvable"
        tool = None
        if p:
            err = self._scanner.scan(p)
            if err is None:
                tool = self._scanner.pending(frozenset(self._dismissed))
                if self._scanner.truncated and tool is None and self._window_noted != p:
                    # F4: the file is larger than the first look-back and no dialog was seen in it: a
                    # dialog whose tool_use predates the window would be missed. Never silent.
                    self._window_noted = p
                    self._event("transcript-window-truncated", path=p,
                                detail="transcript larger than the initial scan window; a dialog opened "
                                       "before it would not be seen (fail-open)")
        if err:
            if not self._gap_open:  # once per outage
                self._gap_open = True
                self._event("transcript-gap-fail-open", error=err)
        else:
            self._gap_open = False
        return tool

    def dismiss(self) -> None:
        """The operator force-released into the dialog (``\\approve --into-dialog``): treat what is
        open NOW (transcript calls, a screen hint) as answered, so a stuck / orphaned hold can be
        cleared by a human. A NEW dialog is still held."""
        self._dismissed.update(self._scanner.open_calls)
        self._dismiss_at = time.monotonic()
        self._hint = None
        if self._open is not None:
            self._waiting_close(None, dismissed=True)
        self._event("dialog-dismissed-by-operator", calls=sorted(self._dismissed))

    def open_boundary(self) -> Optional[float]:
        """When the dialog(s) open NOW were opened, as a ``time.monotonic()`` value: the unanswered
        ``tool_use`` row's own transcript timestamp (the dialog paints just AFTER that row is written), or
        ``None`` when unknown (no row stamp / a screen-only hold). Keys typed between this and the moment
        the guard first SAW the dialog may be either pre-dialog text or the dialog's own keys (E6)."""
        live = [w for i, w in self._scanner.open_wall.items()
                if i in self._scanner.open_calls and i not in self._dismissed]
        if not live or any(w is None for w in live):
            return None
        mono, wall = time.monotonic(), self._wall()
        w = min(live)
        if w != w or w - wall > _FUTURE_TOLERANCE:
            return None
        return mono - max(0.0, wall - w)

    def close_boundary(self) -> Optional["CloseBoundary"]:
        """When the dialog(s) that just closed closed, as a :class:`CloseBoundary` (``time.monotonic()`` values);
        :data:`UNKNOWN_CLOSE` when a transcript close was seen but its time CANNOT be determined (the caller
        force-marks the operator's line BUSY); ``None`` when no transcript close was recorded at all (a
        screen-only hold ended).

        The boundary is the closing ``tool_result`` row's own transcript ``timestamp`` mapped onto the
        monotonic clock (same machine; wall minus monotonic offset). That stamp is the row-WRITE time, which
        TRAILS the true close by Claude Code's answer-to-row latency (measured: see ``CLOSE_MARGIN_SECONDS``),
        so the replay starts at ``stamp - margin``: a key typed after the real close but before the row was
        written is replayed (the line may then look busy; a held item waits), never discarded. A stamp that is
        absent, unparseable, or in the FUTURE by more than ``_FUTURE_TOLERANCE`` is unknown (BUSY), never
        clamped to now. A force-dismiss by the operator is exact (no margin). Never the moment the guard
        NOTICED the close. Consumes the record."""
        walls = self._scanner.take_closed()
        stamps: List[float] = []
        replay: List[float] = []
        if self._dismiss_at is not None:
            stamps.append(self._dismiss_at)
            replay.append(self._dismiss_at)
            self._dismiss_at = None
        if walls:
            mono, wall = time.monotonic(), self._wall()
            for w in walls:
                if w is None or w != w or w - wall > _FUTURE_TOLERANCE:
                    return UNKNOWN_CLOSE
                st = mono - max(0.0, wall - w)
                stamps.append(st)
                replay.append(st - self._margin)
        if not stamps:
            return None
        return CloseBoundary(stamp=max(stamps), replay_from=min(replay), from_transcript=bool(walls))

    # -- screen leg (output watcher) ------------------------------------------------------------
    def __call__(self, data: bytes) -> None:
        self.feed(data)

    def feed(self, data: bytes) -> None:
        raw = self._raw_carry + data
        cut = raw.rfind(b"\x1b")  # carry an unfinished escape sequence to the next chunk
        if cut != -1 and cut > len(raw) - 24 and not _ANSI.match(raw, cut):
            self._raw_carry, raw = raw[cut:], raw[:cut]
        else:
            self._raw_carry = b""
        add = _flat(raw) if self._w is None else self._flat_and_plain(raw)
        if not add:
            return
        self._flat_tail += add
        if len(self._flat_tail) > _FLAT_KEEP:
            drop = len(self._flat_tail) - _FLAT_KEEP
            self._flat_tail = self._flat_tail[drop:]
            self._scanned = max(0, self._scanned - drop)
        start = max(0, self._scanned - _MAX_MARKER)
        window = self._flat_tail[start:]
        found = None
        for kind, parts in SCREEN_MARKERS:
            if all(p in self._flat_tail for p in parts):
                # new only if some part's LAST occurrence ends after what was already scanned
                if any(self._flat_tail.rfind(p) + len(p) > self._scanned for p in parts):
                    found = kind
                    break
        self._scanned = len(self._flat_tail)
        if found:
            now = self._clock()
            fresh = self._hint is None
            self._hint = (found, now, self._size())
            if fresh:
                extra = self._waiting_open(found, now) if self._w is not None else {}
                self._event("screen-dialog-seen", dialog=found, **extra)
                if self._last_write is not None and now - self._last_write <= self._collision:
                    self.suspect_collisions += 1
                    self._event("dialog-after-injection-suspect", dialog=found,
                                seconds_after_write=round(now - self._last_write, 3),
                                detail="a harness write landed within the collision window before a dialog "
                                       "was painted: its CR may have answered it (the residual race)")

    def note_write(self) -> None:
        """The gate calls this after every harness write (collision detection only)."""
        self._last_write = self._clock()

    # -- the answer -----------------------------------------------------------------------------
    def check(self) -> Optional[str]:
        tool = self._transcript_modal()
        if tool:
            return tool
        h = self._hint
        if h is None:
            return None
        kind, seen, size0 = h
        age = self._clock() - seen
        if age <= self._window:
            return f"{kind} (screen)"
        if size0 is not None and self._size() == size0:
            return f"{kind} (screen)"
        self._hint = None
        if self._open is not None:
            self._waiting_close(size0)
        return None

    # -- the waiting-on-operator alert (only ever reached when ``self._w`` is set) ---------------------------
    def _flat_and_plain(self, raw: bytes) -> str:
        """Same answer as ``_flat(raw)``; ALSO keeps the second tail (ANSI removed, whitespace kept, ``_FLAT_KEEP`` characters).
        Bounded work per chunk: one strip of THIS chunk (the one ``_flat`` already did), an append and a trim; no scan of the
        tail. Never raises (a fault is counted and the first tail carries on)."""
        text = _ANSI.sub(b"", raw).decode("utf-8", "replace")
        try:
            self._plain += text
            if len(self._plain) > _FLAT_KEEP:
                self._plain = self._plain[-_FLAT_KEEP:]
        except Exception as exc:  # noqa: BLE001
            self._waiting_fault("tail", exc)
        return re.sub(r"\s+", "", text).lower()

    def _waiting_fault(self, where: str, exc: BaseException) -> None:
        """Counted every time, logged ONCE per episode; nothing here may reach the relay."""
        self.waiting_errors += 1
        if self._werr_episode != self._episode:
            self._werr_episode = self._episode
            self._event("waiting-alert-error", where=where, error=f"{type(exc).__name__}: {exc}",
                        episode=self._episode)

    def _waiting_open(self, kind: str, now: float) -> dict:
        """A NEW hold episode opened: count it; for a permission prompt hand a bounded COPY of the screen tail to the alert
        worker. Nothing here reads or matches screen text (g138): the terminal thread does a slice, a dict and a hand-off. Returns
        the extra fields of the ``screen-dialog-seen`` record: the episode and the open time, NO screen text. Total."""
        try:
            self._episode += 1
            if kind not in WAITING_ALERT_KINDS:
                return {}
            wall = self._wall()
            self._open = {"episode": self._episode, "dialog": kind, "opened_ts": wall, "deadline": None}
            try:
                self._w.on_open({"episode": self._episode, "dialog": kind, "opened_ts": wall,
                                 "screen": self._plain[-_SNAPSHOT_CHARS:], "set_deadline": self._set_deadline})
            except Exception as exc:  # noqa: BLE001
                self._waiting_fault("raise", exc)
            return {"episode": self._episode, "opened_ts": wall}
        except Exception as exc:  # noqa: BLE001
            self._waiting_fault("open", exc)
            return {}

    def _set_deadline(self, episode: int, deadline: float) -> None:
        """Called by the alert worker once it has read the countdown: when it runs out (a plain store; the close reads it)."""
        o = self._open
        if o is not None and o.get("episode") == episode:
            o["deadline"] = deadline

    def _waiting_close(self, size0: Optional[int], dismissed: bool = False) -> None:
        """The hold this episode opened ended: record the close with the outcome the guard can SEE. answered = the transcript
        grew while the countdown (if any) was not yet at zero; timed out = it grew at or after the countdown's end; unknown =
        it could not be told (no transcript size, no growth, or the operator force-released). Total."""
        o, self._open = self._open, None
        if o is None:
            return
        try:
            now = self._wall()
            grew = False
            if not dismissed and size0 is not None:
                cur = self._size()
                grew = cur is not None and cur != size0
            if not grew:
                outcome = "unknown"
            elif o["deadline"] is not None and now >= o["deadline"] - _TIMEOUT_TOLERANCE_S:
                outcome = "timed out"
            else:
                outcome = "answered"
            self._event("screen-dialog-closed", dialog=o["dialog"], episode=o["episode"], outcome=outcome,
                        opened_ts=o["opened_ts"], closed_ts=now, **({"dismissed_by_operator": True} if dismissed else {}))
            try:
                self._w.on_close({"episode": o["episode"], "dialog": o["dialog"], "outcome": outcome})
            except Exception as exc:  # noqa: BLE001
                self._waiting_fault("close", exc)
        except Exception as exc:  # noqa: BLE001
            self._waiting_fault("close", exc)

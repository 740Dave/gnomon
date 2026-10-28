r"""The self-compact gate — enforce the compact-timing rule AT THE VERB.

operator ruling 2026-09-29, written in the session-end compact procedure
(property ``timing``): the compact directive must not be stale when the compact
fires. (The same ruling's numeric floor, a 90% minimum without the operator's approval, was
replaced by confirm-twice friction on 2026-09-30, see below.)
The measured failure behind the staleness half: a directive written at ~70% went stale and steered
the summarizer.

Refusals, applied ONLY to the agent's own ``request_compact`` (SELF origin):

1. **confirm-twice** (supersedes the old below-threshold floor, see
   below) below 85%; **no-reading** — no reading is available at all (fail CLOSED;
   the soft refusal needs the current % to state it).
2. **stale-directive** — ``directive_path`` was given, the provenance log holds a
   ``compact-guidance`` injection (the 90% alert) AFTER the most recent compact
   lifecycle event, and the directive file's mtime is OLDER than that alert.

3. **handover-newer-than-directive** (operator steer 2026-09-29) — ``directive_path``
   was given, a ``HANDOVER.md`` sits in the SAME directory, and its mtime is
   newer than the directive's by more than :data:`HANDOVER_TOLERANCE_SECONDS`:
   the directive was not brought up to date after the last handover edit. The
   measured case: COMPACT-DIRECTIVE.md written 22:26 (~70%), HANDOVER.md beside
   it updated 23:27, the stale directive passed at 90% — caught whether or not
   a 90% alert was logged.

4. **operator-line-busy** (the D44 line-hold applied to the agent's
   OWN compact) — the operator's line is ``partial`` (or ``unknown`` and not yet
   silent long enough), per the shared
   :func:`~gnomon.harness.line_hold.line_hold_reason` rule. Live
   2026-09-29 20:45:29Z: a self ``request_compact`` fired while the operator was
   mid-typing; the typed ``/compact`` + directive merged into his partial line
   and was submitted as an ordinary message (no compaction). Refused with
   NOTHING typed (so the ESC ESC can never destroy operator text); the agent
   re-calls once the line is empty. NOT overridable by ``ch_approved`` (it is not
 a policy refusal; it protects the operator's keystrokes). D3 bounds
   the hold for a ``partial`` line whose text is exactly known: see below.

 (operator ratified 2026-09-30, D1-D4) REPLACES the
numeric floor (D42's 90% minimum without the operator's approval and X16's 85% floor) with
CONFIRM-TWICE friction, for the agent's own compact (both the /prepare-for-compact
skill, which ends in ``request_compact``, and the self-verb go through THIS one
check; the operator's own typed ``/compact`` never reaches it):

* at or above :data:`CONFIRM_FREE_FROM` (85) no confirmation is needed; a reason,
  if given, is still logged (``compact-reasoned``);
* the FIRST request below 85% is refused softly with "Are you sure? You're only
  at N% context." (below :data:`WISDOM_BELOW` (65) the refusal ALSO carries the
  wisdom reminder). The refusal is logged (``compact-refused``, reason
  ``confirm-twice``) and arms the confirmation for :data:`CONFIRM_WINDOW_SECONDS`;
* a SECOND request (the next compact request of this session after that soft
  refusal, inside the window, with no compaction in between) proceeds only if it
  carries a NON-EMPTY, non-whitespace ``reason`` (over today's actuator frame, an
  ``approval_note`` given WITHOUT ``ch_approved`` is read as that reason, see
  :meth:`CompactGate.check`), logged ``compact-confirmed`` with the first-call
  timestamp and the percentage at each call. A second request with no reason is
  refused again (``confirm-needs-reason``); a request after the window is a new
  first request.

The freshness checks (2, 3) stay HARD: a reason never bypasses them (D4). The 90%
alert is only a nudge; nothing here gates on it except the freshness check that
dates the directive against it.

 D3 (half-typed operator line): item 4's hold now has a bounded end. If
the operator's line holds unsent text the tap can read exactly AND the operator has
made no real keystroke for :data:`LINE_TAKEOVER_IDLE_SECONDS` (600), the gate saves
the text to ``<session_dir>/operator-line-saved.json`` BEFORE anything is typed,
the compact proceeds with the ESC ESC clear, and after the compaction completes
the harness puts the text back in the operator's input line WITHOUT pressing Enter
(:meth:`CompactGate.on_post_compact`). Under 10 minutes idle, or when the text is
not exactly known (a punted / pasted / ``\command`` line), the hold is as before.
A failed restore leaves the text in that file and is logged LOUDLY
(``operator-line-restore``, decision ``failed`` / ``not-restored``).

Override (unchanged): ``ch_approved=True`` plus a non-empty ``approval_note`` saying what the
operator approved. ``ch_approved=True`` with a blank/missing note is itself refused
(**override-without-note**). A used override is logged (``compact-override``).
Every refusal is logged (``compact-refused``) with a ``reason``.

SELF-origin ``inject`` screening (re-audit 2026-09-29):

* text holding ANY C0 control byte other than ``\t`` / ``\n``, or DEL (0x7f),
  is refused (**control-bytes**) — ESC, CR, ^U, backspace, NUL, bracketed-paste
  markers etc. can rewrite the prompt so a ``/compact`` executes; clearing the
  prompt is the verb's own ``clear`` / ``skip_clear`` job, not the text's. Logged
  ``compact-refused`` when ``/compact`` appears anywhere in the text, otherwise
  ``inject-refused``;
* text in which ANY line is an operator ``\command`` (single backslash + a
  command word, per the prompt hook's own classifier) is refused
  (**operator-sigil**, logged ``inject-refused``) — the UserPromptSubmit hook
  would otherwise run the agent's line as an OPERATOR command;
* text in which ANY line (split on ``\r`` and ``\n``) starts with ``/compact``
  is refused outright (**compact-via-inject**, logged ``compact-refused``) — the
  agent is pointed at ``request_compact``.

What counts as a reading (fix pass, audit 2026-09-29) — GATE ONLY; the monitor's
own alerting is untouched:

* after a compact lifecycle event, only a statusline reading whose
  ``written_epoch`` is NEWER than that event counts — the state file keeps the
  pre-compact percentage until the statusline refreshes, and a pre-compact 92%
  must not pass a post-compact gate. No epoch / older epoch -> no reading;
* before any compaction, a fresh gauge read (``monitor.read_now``) counts;
* never the monitor's ``last_reading`` fallback — a dark gauge is no reading;
* a non-finite value (NaN / +-inf) is no reading.
No reading always fails CLOSED (overridable by the operator, like every refusal).

NOT gated (unchanged): the operator's own typed ``/compact`` (never reaches the
actuator) and the hub-origin ``request_compact`` / ``inject`` (operator
dashboard, bus).

Decided, not missed (independent audit 2026-09-29, accepted as designed):

* an inline ``direction`` skips the staleness check — it is authored in the
  call itself, so it cannot predate the alert;
* no 90% alert since the last compaction means no staleness check — there is
  no alert to date the directive against;
* zero-width characters and lookalike slashes (e.g. ``\u200b/compact``,
  ``／compact``) are NOT screened — Claude Code does not execute them as the
  ``/compact`` command, so they are ordinary text;
* F3 (operator sigil) screens only the ACCIDENTAL path — the agent's own
  ``inject`` verb. Deliberate forgery by writing an ``operator_command`` frame
  straight to the raw self socket is a documented KNOWN LIMIT, not covered;
* the directive's mtime can be refreshed by a ``touch`` without re-authoring,
  and the ``approval_note`` is on the honor system — the gate catches an honest
  agent's mistakes, it is not a defence against an adversarial one.
"""

from __future__ import annotations

import json
import math
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from .constants import ACTIONABLE_THRESHOLD
from .leading_bang import has_control_bytes, has_leading_bang, screen_text  # noqa: F401  (re-exported; the rules live in the leaf: r2c shared screen)

RULE_REF = "session-end compact procedure (timing; pilot ruling 2026-09-29)"

REASON_BELOW = "below-threshold"  # legacy: no longer produced (D50 removed the floor)
REASON_NO_READING = "no-reading"
REASON_STALE = "stale-directive"
REASON_NO_NOTE = "override-without-note"
REASON_INJECT = "compact-via-inject"
REASON_CONTROL = "control-bytes"
REASON_HANDOVER = "handover-newer-than-directive"
REASON_SIGIL = "operator-sigil"
#: operator ruling (2026-10-02): a leading ``!`` in a non-operator inject
#: puts Claude Code's prompt in bash mode (runs a shell command outside the Bash tool and deny layer).
REASON_BANG = "leading-bang"
BANG_RULE = "pilot ruling dsd-1a2b3c4d5e3 D2 (2026-10-02): refuse a leading '!' from non-operator sources"
REASON_LINE_HELD = "operator-line-busy"
#: the FIRST request below 85% (a soft refusal that arms the confirmation).
REASON_CONFIRM = "confirm-twice"
#: D1: a SECOND request that carries no non-empty reason is refused again.
REASON_CONFIRM_NEEDS_REASON = "confirm-needs-reason"
#: D3: the operator's unsent text could not be persisted, so nothing is cleared.
REASON_LINE_SAVE_FAILED = "operator-line-save-failed"

#: from this reading upward no confirmation is needed (a reason, if
#: given, is still logged). Below it the first request is refused softly.
CONFIRM_FREE_FROM = 85.0
#: Historical name of the same number (X16's floor); kept so importers still resolve.
SELF_COMPACT_FLOOR = CONFIRM_FREE_FROM
#: below this reading the soft refusal also carries the wisdom reminder.
WISDOM_BELOW = 65.0
#: D1: "second" = the next compact request of THIS session after a soft
#: refusal, within this many seconds (30 min), with no compaction in between. A
#: request after the window is a new first request.
CONFIRM_WINDOW_SECONDS = 1800.0
#: D3: a partial operator line is taken over (text saved, compact, text
#: restored) only after this many seconds without a real operator keystroke (10 min).
LINE_TAKEOVER_IDLE_SECONDS = 600.0
#: Where the operator's saved unsent text is persisted, in the session dir.
SAVED_LINE_FILENAME = "operator-line-saved.json"
#: D50fix P2-4: a pending restore is bound to ITS compact by time: a post-compact
#: event later than this many seconds after the line was saved is not that compact
#: (it is dropped loudly, the text stays in the file).
PENDING_RESTORE_MAX_SECONDS = 900.0


#: D50fix2 P2-1: an entry older than this is never auto-restored; it stays in the
#: file marked ``expired-kept`` and is listed in the launch-time report.
RESTORE_MAX_AGE_SECONDS = 24 * 3600.0
#: D50fix2 P3-5: entries already restored are pruned after this age, on next write.
RESTORED_PRUNE_SECONDS = 7 * 24 * 3600.0
STATUS_EXPIRED = "expired-kept"
STATUS_POISONED = "poisoned-kept"


def read_saved_entries(path: str) -> List[Dict[str, Any]]:
    """The entries of the saved-line file, oldest first. The file holds a LIST
    (D50fix P2-3); a legacy single-object file reads as one entry; a missing file
    is ``[]``. Raises ``OSError`` on an unreadable file and ``ValueError`` on ANY
    parse failure (D50fix2 P3-3: RecursionError and every JSON error included), so
    callers treat it as a corrupt file and never see another exception type."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return []
    except OSError:
        raise
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - RecursionError, MemoryError, ...
        raise ValueError("saved-line file could not be parsed: %s" % type(exc).__name__) from exc
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list) and all(isinstance(e, dict) for e in data):
        return data
    raise ValueError("saved-line file is neither an object nor a list of objects")


def _prune_restored(entries: List[Dict[str, Any]], now: Optional[float]) -> List[Dict[str, Any]]:
    """D50fix2 P3-5: drop entries marked restored more than 7 days ago."""
    now = _finite(now)
    if now is None:
        return entries
    keep = []
    for e in entries:
        if e.get("restored") is True:
            ts = _finite(e.get("restored_ts"))
            if ts is None:
                ts = _finite(e.get("saved_ts"))
            if ts is not None and now - ts > RESTORED_PRUNE_SECONDS:
                continue
        keep.append(e)
    return keep


def _write_saved_entries(path: str, entries: List[Dict[str, Any]], now: Optional[float] = None) -> None:
    entries = _prune_restored(entries, now)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(entries, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def unrestored_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Entries still waiting to be put back (not restored, cancelled, expired or poisoned)."""
    return [e for e in entries if e.get("restored") is False and not e.get("cancelled")
            and e.get("status") not in (STATUS_EXPIRED, STATUS_POISONED)]


def kept_entries(entries: List[Dict[str, Any]], status: str) -> List[Dict[str, Any]]:
    return [e for e in entries if e.get("restored") is False and not e.get("cancelled")
            and e.get("status") == status]


def _entry_age(e: Dict[str, Any], now: Optional[float]) -> Optional[float]:
    st, now = _finite(e.get("saved_ts")), _finite(now)
    if st is None or now is None:
        return None
    return now - st


def mark_expired(entries: List[Dict[str, Any]], now: Optional[float]) -> List[Dict[str, Any]]:
    """Mark every pending entry older than 24 h (or with no usable timestamp)
    ``expired-kept`` IN PLACE; returns the newly marked ones."""
    out = []
    for e in unrestored_entries(entries):
        age = _entry_age(e, now)
        if age is None or age > RESTORE_MAX_AGE_SECONDS:
            e["status"] = STATUS_EXPIRED
            out.append(e)
    return out


def saved_line_report(path: str, now: Optional[float], *, mark: bool = True) -> Dict[str, Any]:
    """The launch-time picture of the saved-line file (D50fix2 P3-4): corrupt
    asides, corrupt file, pending and expired-kept entries. Never raises."""
    import glob
    rep: Dict[str, Any] = {"path": path, "corrupt_asides": [], "corrupt_file": False,
                           "pending": 0, "pending_text_len": 0, "expired_kept": 0, "poisoned_kept": 0}
    try:
        rep["corrupt_asides"] = sorted(glob.glob(glob.escape(path) + ".corrupt-*"))
    except Exception:  # noqa: BLE001
        pass
    try:
        entries = read_saved_entries(path)
    except OSError:
        return rep
    except Exception:  # noqa: BLE001
        rep["corrupt_file"] = True
        return rep
    try:
        if mark and mark_expired(entries, now):
            _write_saved_entries(path, entries, now)
        elif not mark:
            pass
    except Exception:  # noqa: BLE001
        pass
    pend = unrestored_entries(entries)
    rep["pending"] = len(pend)
    rep["pending_text_len"] = sum(len(str(e.get("text") or "")) for e in pend)
    rep["expired_kept"] = len([e for e in entries if e.get("status") == STATUS_EXPIRED
                               and e.get("restored") is False and not e.get("cancelled")])
    rep["poisoned_kept"] = len(kept_entries(entries, STATUS_POISONED))
    return rep

CONFIRM_SOFT_TEXT = "Are you sure? You're only at {pct}% context."
WISDOM_TEXT = ("Your best work is done between 75 and 90% context, once you have built up enough understanding "
               "- do you really want to /compact now?")

#: HANDOVER.md may be saved alongside the directive; within this many seconds
#: the two count as written together.
HANDOVER_TOLERANCE_SECONDS = 2.0
HANDOVER_FILENAME = "HANDOVER.md"

#: The injection-log trigger of the 90% alert (``NoticeKind.COMPACT_GUIDANCE``).
ALERT_TRIGGER = "compact-guidance"


@dataclass
class GateVerdict:
    allowed: bool
    reasons: List[str] = field(default_factory=list)
    messages: List[str] = field(default_factory=list)
    used_percentage: Optional[float] = None
    alert_ts: Optional[float] = None
    directive_mtime: Optional[float] = None
    overridden: List[str] = field(default_factory=list)
    approval_note: Optional[str] = None
    self_reason: Optional[str] = None
    #: set on a SECOND request that proceeded (confirm-twice).
    confirmed: bool = False
    first_call_ts: Optional[float] = None
    first_call_percentage: Optional[float] = None
    #: D3: set when the compact may proceed over a half-typed operator
    #: line that was saved to ``takeover_path``; the actuator MUST force the ESC ESC
    #: clear and then call :meth:`CompactGate.commit_takeover` /
    #: :meth:`CompactGate.cancel_takeover`.
    takeover_text: Optional[str] = None
    takeover_path: Optional[str] = None
    takeover_id: Optional[str] = None
    takeover_saved_ts: Optional[float] = None

    @property
    def error(self) -> str:
        if self.reasons and REASON_INJECT not in self.reasons and all(
                r in (REASON_CONTROL, REASON_SIGIL, REASON_BANG) for r in self.reasons):
            # An inject refusal with no /compact in it: not a compact decision.
            return "inject REFUSED by the harness self-inject screen: " + " ".join(self.messages)
        return (f"compact REFUSED by the self-compact gate [{RULE_REF}]: "
                + " ".join(self.messages))


def load_log_records(path: Optional[str], fallback: Optional[List[Dict[str, Any]]] = None
                     ) -> List[Dict[str, Any]]:
    """The session's provenance records, from ``injection_log.jsonl`` on disk
    (spans harness restarts / resumes of the same session dir) — or the
    in-memory mirror when there is no file. Unparseable lines are skipped."""
    if path and os.path.exists(path):
        out: List[Dict[str, Any]] = []
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
        return out
    return list(fallback or [])


def _finite(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def last_compact_ts(records: List[Dict[str, Any]]) -> Optional[float]:
    """``ts`` of the most recent compact lifecycle event (pre or post), or ``None``."""
    ts: Optional[float] = None
    for rec in records:
        if rec.get("kind") == "lifecycle" and rec.get("point") == "compact":
            got = _finite(rec.get("ts"))
            if got is not None:
                ts = got
    return ts


def is_compact_command(text: Any) -> bool:
    r"""True when ANY line of ``text`` (split on ``\r`` / ``\n``) has ``/compact``
    as its first whitespace-delimited token."""
    if not isinstance(text, str):
        return False
    for line in text.replace("\r", "\n").split("\n"):
        parts = line.split(None, 1)
        if parts and parts[0].lower() == "/compact":
            return True
    return False


def is_operator_command(text: Any) -> bool:
    r"""True when ANY line (split on ``\r`` / ``\n``) is an operator ``\command``
    by the prompt hook's OWN classifier (single ``\`` + a command word; ``\\``
    is the escape and passes)."""
    if not isinstance(text, str):
        return False
    from .prompt_command_hook import classify

    return any(classify(line) is not None for line in text.replace("\r", "\n").split("\n"))


def screen_self_inject(text: Any) -> Optional[List[str]]:
    """Refusal reasons for a SELF-origin inject, or ``None`` if it may pass."""
    shared = screen_text(text)  # r2c: the ONE union screen (control bytes + leading bang)
    if shared and REASON_CONTROL in shared:
        reasons = [REASON_CONTROL]
        if "/compact" in text.lower():
            reasons.append(REASON_INJECT)
        return reasons
    reasons = []
    if is_compact_command(text):
        reasons.append(REASON_INJECT)
    if is_operator_command(text):
        reasons.append(REASON_SIGIL)
    if shared and REASON_BANG in shared:
        reasons.append(REASON_BANG)
    return reasons or None


#: Invisible format chars an attacker can put in front of a sigil (U+200B-U+200D
#: zero-width space/non-joiner/joiner, U+2060 word joiner, U+FEFF BOM/ZWNBSP).
ZERO_WIDTH_CHARS = "\u200b\u200c\u200d\u2060\ufeff"
_ZW_TABLE = {ord(c): None for c in ZERO_WIDTH_CHARS}


def screen_hub_inject(text: Any) -> Optional[List[str]]:
    r"""Refusal reasons for a HUB-origin inject (D15 fix pass 2, re-audit HIGH), or
    ``None`` if it may be typed.

    * ``control-bytes`` — any C0 control other than ``	`` / ``
``, or DEL: EXACTLY
      the self-origin predicate (:func:`has_control_bytes`). Kills the splices the
      re-audit typed (``x\x7f\hooks off``, ``junk\x15…``, ``\x1b…``, NUL,
      bracketed-paste wrappers, …) — a hub may type text, never edit keys.
    * ``operator-sigil`` — any line whose first non-whitespace char, after removing
      zero-width/BOM chars, starts an operator ``\command`` (the prompt hook's own
      :func:`classify`; ``\`` stays the escape and passes).
    * ``leading-bang`` — a payload starting with ``!`` (see :func:`has_leading_bang`),
      for EVERY hub source (no exemption: operator fan-out always starts with
      ``<NAME-to-...>``, so one never needed it). UNCONDITIONAL: there is no flag or
      config key that switches this off (an agent can write the config file)."""
    if not isinstance(text, str):
        return None
    shared = screen_text(text)  # r2c: the ONE union screen
    if shared and REASON_CONTROL in shared:
        return [REASON_CONTROL]
    from .prompt_command_hook import classify

    for line in text.split("\n"):
        if classify(line.translate(_ZW_TABLE).lstrip()) is not None:
            return [REASON_SIGIL]
    if shared and REASON_BANG in shared:
        return [REASON_BANG]
    return None


class TapLineControl:
    """Adapter from the operator input tap (looked up lazily: the tap is built
    AFTER the gate) to the small line-control surface the gate's D3 takeover
    uses. ``get_tap`` returns the tap or ``None`` (a stdin-less session)."""

    def __init__(self, get_tap: Callable[[], Any]) -> None:
        self._get_tap = get_tap

    def _tap(self) -> Any:
        return self._get_tap()

    def line_state(self) -> str:
        tap = self._tap()
        return tap.line_state if tap is not None else "empty"

    def idle_seconds(self) -> Optional[float]:
        tap = self._tap()
        return tap.seconds_since_input() if tap is not None else None

    def unsent_text(self) -> Optional[str]:
        tap = self._tap()
        return tap.unsent_text() if tap is not None else None

    def clear_unsent(self) -> None:
        tap = self._tap()
        if tap is not None:
            tap.discard_unsent()

    def restore_unsent(self, text: str) -> None:
        tap = self._tap()
        if tap is None:
            raise RuntimeError("no operator input tap to restore the line into")
        tap.restore_unsent(text)

    def feedback(self, text: str) -> None:
        tap = self._tap()
        if tap is not None:
            tap.feedback(text)


def latest_alert_since_last_compact(records: List[Dict[str, Any]]) -> Optional[float]:
    """``ts`` of the newest 90% alert AFTER the most recent compact lifecycle
    event (pre or post), or ``None``."""
    start = 0
    for i, rec in enumerate(records):
        if rec.get("kind") == "lifecycle" and rec.get("point") == "compact":
            start = i + 1
    alert_ts: Optional[float] = None
    for rec in records[start:]:
        if rec.get("kind") == "injection" and rec.get("trigger") == ALERT_TRIGGER:
            ts = rec.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                alert_ts = float(ts)
    return alert_ts


class CompactGate:
    """Decides whether a SELF ``compact`` may proceed, and logs the outcome.

    ``read_context`` -> a FRESH gauge read (``None`` = unavailable), used only
    while there has been no compaction; ``read_stamped`` -> ``(pct, written_epoch)``
    from the statusline state file (or ``None``), the ONLY reading accepted after
    a compaction; ``records`` -> the session's provenance records;
    ``record(kind, **fields)`` appends one provenance record.
    """

    def __init__(
        self,
        *,
        read_context: Callable[[], Optional[float]],
        records: Callable[[], List[Dict[str, Any]]],
        record: Callable[..., Any],
        threshold: float = ACTIONABLE_THRESHOLD,
        read_stamped: Optional[Callable[[], Optional[Tuple[float, Optional[float]]]]] = None,
        confirm_below: Optional[float] = CONFIRM_FREE_FROM,
        wisdom_below: float = WISDOM_BELOW,
        confirm_window: float = CONFIRM_WINDOW_SECONDS,
        line_hold: Optional[Callable[[], Optional[str]]] = None,
        line_control: Optional[Any] = None,
        session_dir: Optional[str] = None,
        takeover_idle_seconds: float = LINE_TAKEOVER_IDLE_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._read_context = read_context
        self._read_stamped = read_stamped
        self._records = records
        self._record = record
        #: Informational only since (logged with each record); it is
        #: NOT a gate any more: the numeric floor was replaced by confirm-twice.
        self.threshold = threshold
        #: readings below this need the confirm-twice (``None`` = no
        #: confirmation friction at any level).
        self.confirm_below = confirm_below
        self.wisdom_below = wisdom_below
        self.confirm_window = confirm_window
        self._clock = clock
        # D3: the operator-line takeover (save / compact / restore).
        self._line_control = line_control
        self._session_dir = session_dir
        self.takeover_idle_seconds = takeover_idle_seconds
        self._pending_restore: Optional[Dict[str, Any]] = None
        #: Round 4 (P2): the dialog reader (the gate's ``_modal_reason``; the launcher wires it). While it
        #: names an open dialog the saved line is NEVER typed; it stays pending and :meth:`tick_restore`
        #: retries once the dialog has closed AND the line is empty.
        self.modal_check: Optional[Callable[[], Optional[str]]] = None
        self._restore_waits_for_dialog = False
        # line-hold source: explicit, else the ContextMonitor the gate
        # already reads from (production wires ``read_context=monitor.read_now``,
        # and the monitor owns the operator line-state + silence clock the stall
        # nudge's hold uses), so the SAME rule and clock apply with no launcher
        # change. No source = never held (bare gates, relaunch's dry check).
        if line_hold is None:
            owner = getattr(read_context, "__self__", None)
            candidate = getattr(owner, "line_hold_reason", None)
            if callable(candidate):
                line_hold = candidate
        self._line_hold = line_hold

    def _line_hold_reason(self) -> Optional[str]:
        if self._line_hold is None:
            return None
        try:
            reason = self._line_hold()
        except Exception:  # noqa: BLE001 - an unreadable line is a held line (fail toward holding)
            return "operator line state unreadable"
        return reason if reason else None

    def _reading(self, compact_ts: Optional[float]) -> Tuple[Optional[float], str]:
        """``(reading, why_none)`` for the GATE. See the module docstring."""
        if compact_ts is None:
            try:
                value = _finite(self._read_context())
            except Exception:  # noqa: BLE001 - an erroring gauge is "no reading" (fail closed)
                value = None
            return value, ("" if value is not None else "the gauge is dark")
        stamped = None
        if self._read_stamped is not None:
            try:
                stamped = self._read_stamped()
            except Exception:  # noqa: BLE001
                stamped = None
        if not stamped:
            return None, "no statusline reading written since the last compaction"
        value, epoch = _finite(stamped[0]), _finite(stamped[1])
        if value is None:
            return None, "the statusline reading is not a finite number"
        if epoch is None or epoch <= compact_ts:
            return None, ("the only reading predates the last compaction (no reading newer "
                          "than the compact lifecycle event yet)")
        return value, ""

    def _safe_records(self) -> List[Dict[str, Any]]:
        try:
            return self._records()
        except Exception:  # noqa: BLE001 - unreadable log: nothing to date against
            return []

    def refuse_inject(self, text: str, reasons: Optional[List[str]] = None,
                      source: str = "self-verb", stage: Optional[str] = None) -> GateVerdict:
        """A screened ``inject`` (see :func:`screen_self_inject`; ``source`` is the
        provenance source, ``self-verb`` unless the hub path passes its own) — refused, logged."""
        reasons = list(reasons or [REASON_INJECT])
        msgs: List[str] = []
        if REASON_CONTROL in reasons:
            msgs.append(
                "The inject text contains control bytes (a C0 control character other than "
                "tab/newline, or DEL) — e.g. ESC, CR, ^U, backspace, NUL or bracketed-paste "
                "markers — which can rewrite the prompt, so it is refused. Clearing the prompt "
                "is the job of the verb's own clear / skip_clear args, not of the text.")
        if REASON_INJECT in reasons:
            msgs.append(
                "A /compact typed through the 'inject' verb bypasses the compact gate, so it is "
                "refused. Use request_compact (with directive_path): below 85% the first call "
                "is a soft 'Are you sure?', and a second call with a non-empty reason "
                "proceeds; ch_approved plus an approval_note is the pilot override.")
        if REASON_SIGIL in reasons:
            msgs.append(
                "A line starting with a single backslash + a command word is an OPERATOR command "
                "(\\all, \\dm, \\approve, \\pause, ...): typed into your own session the harness "
                "would run it with the operator's authority, so it is refused. Start the line with "
                "a doubled backslash if you mean literal text.")
        if REASON_BANG in reasons:
            msgs.append(
                "A payload whose first visible character is '!' (also after leading whitespace or "
                "invisible characters, or a fullwidth '!') puts Claude Code's prompt in bash mode and "
                "would run a shell command outside the Bash tool and its deny layer, so a non-operator "
                f"inject may not start with it [rule {BANG_RULE}]. Reword the message so it does not "
                "begin with '!'.")
        v = GateVerdict(allowed=False, reasons=reasons, messages=msgs)
        kind = "compact-refused" if REASON_INJECT in reasons else "inject-refused"
        try:
            self._record(kind, source=source, reason="; ".join(reasons), reasons=reasons,
                         detail=" ".join(msgs), text_preview=repr(text[:200]), rule=RULE_REF,
                         **({"stage": stage} if stage else {}))
        except Exception:  # noqa: BLE001
            pass
        return v

    # -- confirm-twice ------------------------------------------------

    def _armed_first_call(self, records: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """The newest soft-refusal record (reason ``confirm-twice``) of THIS compact
        cycle that is still inside the confirmation window, or ``None``. "This
        cycle" = after the most recent compact lifecycle event, so a compaction in
        between always resets the pair. Only a first-request refusal arms it (a
        ``confirm-needs-reason`` refusal does not slide the window)."""
        start = 0
        for i, rec in enumerate(records):
            if rec.get("kind") == "lifecycle" and rec.get("point") == "compact":
                start = i + 1
        now = _finite(self._clock())
        armed: Optional[Dict[str, Any]] = None
        for rec in records[start:]:
            if rec.get("kind") != "compact-refused":
                continue
            if REASON_CONFIRM not in (rec.get("reasons") or []):
                continue
            ts = _finite(rec.get("ts"))
            if ts is None or now is None:
                continue
            if 0 <= now - ts <= self.confirm_window:
                armed = rec
        return armed

    @staticmethod
    def _fmt_pct(reading: float) -> str:
        return f"{round(reading, 1):g}"

    def _confirm_message(self, reading: float) -> str:
        from . import compact_messages as _cm   # pre-review B3 (flag compact_plain_messages; OFF = today's text)

        if _cm.enabled():
            return _cm.soft_refusal_text(self._fmt_pct(reading), float(self.confirm_below),
                                         f"{self.confirm_window / 60:g}",
                                         WISDOM_TEXT if reading < self.wisdom_below else None)
        msg = CONFIRM_SOFT_TEXT.format(pct=self._fmt_pct(reading))
        if reading < self.wisdom_below:
            msg += " " + WISDOM_TEXT
        return (msg + f" If you do, call request_compact again within "
                f"{self.confirm_window / 60:g} minutes with a non-empty 'reason' (logged). "
                "This is a nudge, not a wall; the handover and directive freshness checks "
                "still apply.")

    # -- D3: the half-typed operator line --------------------------------

    def _takeover_plan(self) -> Optional[str]:
        """The operator's exact unsent text when the D3 takeover is allowed NOW
        (a ``partial`` line whose text the tap knows exactly, the operator idle for
        at least ``takeover_idle_seconds``, and a session dir to persist it in),
        else ``None`` (the hold stays as before)."""
        ctl = self._line_control
        if ctl is None or not self._session_dir:
            return None
        try:
            if ctl.line_state() != "partial":
                return None
            idle = _finite(ctl.idle_seconds())
            if idle is None or idle < self.takeover_idle_seconds:
                return None
            text = ctl.unsent_text()
        except Exception:  # noqa: BLE001 - an unreadable line is a held line
            return None
        if not isinstance(text, str) or not text:
            return None
        return text

    def _saved_line_path(self) -> str:
        return os.path.join(str(self._session_dir), SAVED_LINE_FILENAME)

    def _persist_takeover(self, v: GateVerdict, text: str) -> bool:
        """Write the operator's text to the session dir BEFORE anything is cleared."""
        path = self._saved_line_path()
        saved_ts = self._clock()
        try:
            try:
                entries = read_saved_entries(path)
            except ValueError:
                # a corrupt file is NEVER overwritten: move it aside, keep it
                aside = "%s.corrupt-%d-%s" % (path, int(time.time() * 1000), uuid.uuid4().hex[:8])
                os.replace(path, aside)
                self._safe_record("operator-line-restore", decision="corrupt-file-kept", loud=True,
                                  path=path, kept_as=aside)
                entries = []
            entry = {"id": "%r-%d" % (saved_ts, len(entries)), "text": text, "saved_ts": saved_ts,
                     "restored": False, "used_percentage": v.used_percentage}
            # P2-3: APPEND; an earlier (unrestored) entry is never overwritten
            _write_saved_entries(path, entries + [entry], saved_ts)
        except OSError as exc:
            v.allowed = False
            v.reasons = [REASON_LINE_SAVE_FAILED]
            v.messages = [
                f"The operator has unsent text on the line but it could not be saved to {path!r} "
                f"({exc}), so NOTHING was cleared or typed. Retry once the operator's line is empty."]
            v.overridden = []
            self._log_refusal(v, "")
            return False
        v.takeover_text = text
        v.takeover_path = path
        v.takeover_id = entry["id"]
        v.takeover_saved_ts = saved_ts
        self._safe_record("operator-line-saved", source="self-verb", path=path,
                          text_len=len(text), text_preview=repr(text[:80]),
                          used_percentage=v.used_percentage, rule=RULE_REF,
                          detail="operator idle >= %.0fs with an unsent line; text persisted "
                                 "BEFORE the clear" % self.takeover_idle_seconds)
        return True

    def _safe_record(self, kind: str, **fields: Any) -> None:
        try:
            self._record(kind, **fields)
        except Exception:  # noqa: BLE001 - logging must never break the verb
            pass

    def commit_takeover(self, v: GateVerdict) -> None:
        """The actuator wrote the compact (with the ESC ESC clear): the tap's
        view of the line is now empty, and the text waits for the post-compact
        restore."""
        if v.takeover_text is None:
            return
        try:
            if self._line_control is not None:
                self._line_control.clear_unsent()
        except Exception as exc:  # noqa: BLE001
            self._safe_record("operator-line-restore", decision="clear-failed",
                              path=v.takeover_path, error=f"{type(exc).__name__}: {exc}", loud=True)
        old = self._pending_restore
        if old:
            self._safe_record("operator-line-restore", decision="superseded-pending-restore",
                              loud=True, path=old.get("path"),
                              detail="an earlier pending restore never resolved; its text stays in "
                                     "the file and is restored in order with the new one")
        self._pending_restore = {"text": v.takeover_text, "path": v.takeover_path,
                                 "id": v.takeover_id, "saved_ts": v.takeover_saved_ts}

    def cancel_takeover(self, v: GateVerdict, why: str) -> None:
        """The compact was NOT written after the text was saved (e.g. a hook denied
        it): nothing was cleared, so there is nothing to restore; the file stays."""
        if v.takeover_text is None:
            return
        try:
            entries = read_saved_entries(v.takeover_path)
            for e in entries:
                if e.get("id") == v.takeover_id:
                    e["cancelled"] = True
            _write_saved_entries(v.takeover_path, entries, self._clock())
        except (OSError, ValueError, TypeError):
            pass
        self._safe_record("operator-line-restore", decision="cancelled", path=v.takeover_path,
                          reason=why, detail="the compact was not written; the operator's line was "
                                             "left alone; the saved copy stays in the file")

    def abort_restore(self, why: str) -> None:
        """The compaction was never observed: the line was cleared but a restore
        would land in an unknown prompt, so the text is left in the file, LOUDLY."""
        pend, self._pending_restore = self._pending_restore, None
        if not pend:
            return
        self._safe_record("operator-line-restore", decision="not-restored", loud=True,
                          path=pend["path"], reason=why,
                          detail="compaction not observed; the operator's unsent text was cleared "
                                 "and is preserved in the file")
        self._feedback("OPERATOR LINE NOT RESTORED (%s). Your unsent text is saved in %s"
                       % (why, pend["path"]))

    def _feedback(self, text: str) -> None:
        fb = getattr(self._line_control, "feedback", None)
        if callable(fb):
            try:
                fb(text)
            except Exception:  # noqa: BLE001
                pass

    def _dialog_open(self) -> Optional[str]:
        chk = self.modal_check
        if chk is None:
            return None
        try:
            return chk() or None
        except Exception:  # noqa: BLE001 - round 5: a raising reader FAILS CLOSED ("a dialog is open")
            return "dialog-reader-failed"

    def tick_restore(self) -> None:
        """Pump ticker: a restore deferred for an open dialog is retried once the dialog is closed AND the
        operator's line is empty (silent until then; ``on_post_compact`` records the outcome)."""
        if not self._restore_waits_for_dialog or self._dialog_open() is not None:
            return
        ctl = self._line_control
        try:
            if ctl is None or ctl.line_state() != "empty":
                return
        except Exception:  # noqa: BLE001
            return
        try:
            self.on_post_compact()
        except Exception as exc:  # noqa: BLE001 - a ticker never breaks the pump
            self._safe_record("operator-line-restore", decision="failed", loud=True,
                              error=f"{type(exc).__name__}: {exc}")

    def on_post_compact(self) -> Optional[str]:
        """After the compaction completed (the post-compact lifecycle event): put
        the saved text back in the operator's input line, NOT submitted. Returns the
        decision (``restored`` / ``failed`` / ``skipped-operator-typing`` /
        ``stale-pending-restore`` / ``expired-kept``) or ``None`` when nothing was
        pending. D50fix2 P2-1: saved text is NEVER typed into a non-empty line; an
        entry that is not restored stays PENDING in the file and is retried at the
        next post-compact event, while it is under 24 h old; older entries are marked
        ``expired-kept`` and never auto-restored."""
        pend, self._pending_restore = self._pending_restore, None
        path = pend["path"] if pend else self._saved_line_path() if self._session_dir else None
        if not path:
            return None
        if pend:
            # first-attempt matching stays bound to 900 s; a later event is retried
            # under the 24 h rule instead of being dropped
            age = None
            st, now = _finite(pend.get("saved_ts")), _finite(self._clock())
            if st is not None and now is not None:
                age = now - st
            if age is None or age < 0 or age > PENDING_RESTORE_MAX_SECONDS:
                self._safe_record("operator-line-restore", decision="stale-pending-restore", loud=True,
                                  path=path, age_seconds=age, max_age_seconds=PENDING_RESTORE_MAX_SECONDS,
                                  detail="a post-compact event arrived too long after the line was "
                                         "saved to be that compact; nothing was typed; the entry stays "
                                         "PENDING and is retried at the next opportunity (under 24 h)")
                self._feedback("Your earlier unsent text was NOT restored yet (stale window); "
                               "it stays pending in %s" % path)
                return "stale-pending-restore"
        try:
            entries = read_saved_entries(path)
        except FileNotFoundError:
            return None
        except Exception as exc:  # noqa: BLE001 - never raise out of the lifecycle path
            self._safe_record("operator-line-restore", decision="failed", loud=True, path=path,
                              error=f"{type(exc).__name__}: {exc}",
                              detail="saved-line file unreadable; nothing typed")
            return "failed" if pend else None
        expired = mark_expired(entries, self._clock())
        for e in unrestored_entries(entries):
            if "\r" in str(e.get("text") or ""):
                e["status"] = STATUS_POISONED  # a CR would SUBMIT; never typed, never blocks the others
        poisoned = kept_entries(entries, STATUS_POISONED)
        if expired or poisoned:
            self._safe_record("operator-line-restore", decision="entries-kept-not-restored", loud=True,
                              path=path, expired_kept=len(expired), poisoned_kept=len(poisoned),
                              detail="entries older than 24 h, or containing a CR, are kept in the "
                                     "file and never auto-restored")
            try:
                _write_saved_entries(path, entries, self._clock())
            except OSError:
                pass
        todo = unrestored_entries(entries)
        if not todo:
            return STATUS_EXPIRED if expired else None
        ids = [e.get("id") for e in todo]
        text = "\n".join(str(e.get("text") or "") for e in todo)
        ctl = self._line_control
        dialog = self._dialog_open()
        if dialog is not None:
            if not self._restore_waits_for_dialog:
                self._safe_record("operator-line-restore", decision="deferred-dialog-open", loud=True, path=path,
                                  dialog=dialog,
                                  detail="a dialog is open; NOTHING was typed (the saved line is never typed "
                                         "into a dialog); the entry stays pending in the file and is restored "
                                         "after the dialog closes AND the line is empty")
                self._feedback("Your earlier unsent text was NOT restored (a dialog is open); it stays pending "
                               "in %s and comes back after the dialog closes" % path)
            self._restore_waits_for_dialog = True
            return "deferred-dialog-open"
        self._restore_waits_for_dialog = False
        try:
            if ctl is None:
                raise RuntimeError("no operator line control")
            if ctl.line_state() != "empty":
                self._safe_record("operator-line-restore", decision="skipped-operator-typing",
                                  loud=True, path=path,
                                  detail="the operator's line was not empty; NOTHING was typed; the "
                                         "entry stays pending and is retried at the next opportunity")
                self._feedback("Your earlier unsent text was NOT restored (your line is not empty); "
                               "it stays pending in %s" % path)
                return "skipped-operator-typing"
            ctl.restore_unsent(text)
        except Exception as exc:  # noqa: BLE001
            self._safe_record("operator-line-restore", decision="failed", loud=True, path=path,
                              error=f"{type(exc).__name__}: {exc}",
                              detail="restore FAILED; the entry stays pending in the file")
            self._feedback("RESTORE OF YOUR UNSENT LINE FAILED (%s). Your text is saved in %s"
                           % (exc, path))
            return "failed"
        try:
            entries = read_saved_entries(path)
            for e in entries:
                if e.get("id") in ids:
                    e["restored"] = True
                    e["restored_ts"] = self._clock()
            _write_saved_entries(path, entries, self._clock())
        except Exception:  # noqa: BLE001
            pass  # the input line HAS the text; the file bookkeeping is best-effort
        self._safe_record("operator-line-restore", decision="restored", path=path,
                          text_len=len(text), submitted=False, entries=len(ids),
                          detail="the operator's unsent text was put back in the input line "
                                 "WITHOUT pressing Enter")
        return "restored"

    def check(self, *, directive_path: str = "", ch_approved: bool = False,
              approval_note: Optional[str] = None, reason: Optional[str] = None) -> GateVerdict:
        """``reason``: the logged reason (required on the SECOND request
        below 85%, logged when given at or above it).
        The actuator (outside the X16 lane) threads only ``ch_approved`` /
        ``approval_note``, so a note given WITHOUT ``ch_approved`` is read as the
        reason. A note WITH ``ch_approved`` stays the operator-approval note, as before."""
        note = (approval_note or "").strip()
        self_reason = (reason or "").strip()
        if not self_reason and not ch_approved and note:
            self_reason = note
        records = self._safe_records()
        reading, why_none = self._reading(last_compact_ts(records))
        v = GateVerdict(allowed=True, used_percentage=reading)

        if ch_approved and not note:
            v.allowed = False
            v.reasons.append(REASON_NO_NOTE)
            v.messages.append(
                "ch_approved=true was given without a non-empty approval_note. The note must say "
                "what the pilot approved (e.g. 'the pilot approved an early compact at 40% to reset before "
                "the Diamond'); an override without one is refused.")
            self._log_refusal(v, directive_path)
            return v

        blocking: List[str] = []
        msgs: List[str] = []
        reasoned = False
        if reading is None:
            blocking.append(REASON_NO_READING)
            msgs.append(
                f"No context reading is available ({why_none}), so the gate cannot tell what "
                "percentage of context you are at — it fails CLOSED.")
        elif self.confirm_below is not None and reading < self.confirm_below:
            first = self._armed_first_call(records)
            if first is None:
                blocking.append(REASON_CONFIRM)
                msgs.append(self._confirm_message(reading))
            elif not self_reason:
                blocking.append(REASON_CONFIRM_NEEDS_REASON)
                v.first_call_ts = _finite(first.get("ts"))
                v.first_call_percentage = _finite(first.get("used_percentage"))
                msgs.append(
                    f"You are at {self._fmt_pct(reading)}% context and this is your second request, "
                    "but it carries no reason. Call request_compact again with a non-empty "
                    "'reason' (logged) saying why you are compacting now.")
            else:
                v.confirmed = True
                v.first_call_ts = _finite(first.get("ts"))
                v.first_call_percentage = _finite(first.get("used_percentage"))
        elif self_reason:
            reasoned = True  # at or above 85%: no confirmation, but a given reason is logged

        if directive_path:
            alert_ts = latest_alert_since_last_compact(records)
            v.alert_ts = alert_ts
            if alert_ts is not None:
                try:
                    mtime = os.path.getmtime(directive_path)
                except OSError:
                    mtime = None  # the actuator's own unreadable-path error reports this
                v.directive_mtime = mtime
                if mtime is not None and mtime < alert_ts:
                    blocking.append(REASON_STALE)
                    msgs.append(
                        f"The directive at {directive_path!r} is STALE: it was last written before "
                        "the 90% compact-guidance alert of this compact cycle. Re-author the "
                        "directive NOW (so it reflects the session as it is at compaction), then "
                        "call request_compact again.")

            handover = os.path.join(os.path.dirname(os.path.abspath(directive_path)),
                                    HANDOVER_FILENAME)
            try:
                handover_mtime: Optional[float] = os.path.getmtime(handover)
                directive_mtime: Optional[float] = os.path.getmtime(directive_path)
            except OSError:
                handover_mtime = directive_mtime = None  # no HANDOVER.md beside it: not checked
            if (handover_mtime is not None and directive_mtime is not None
                    and handover_mtime - directive_mtime > HANDOVER_TOLERANCE_SECONDS):
                v.directive_mtime = directive_mtime
                blocking.append(REASON_HANDOVER)
                msgs.append(
                    f"The directive at {directive_path!r} is older than the {HANDOVER_FILENAME} "
                    f"beside it (by {handover_mtime - directive_mtime:.0f}s): it was not brought up "
                    "to date after the last handover edit. Re-author the directive NOW against the "
                    "current handover, then call request_compact again.")

        # the D44 line-hold for the agent's OWN compact. Checked after
        # the policy checks (so every reason is reported) and never overridable:
        # it protects the operator's keystrokes. Nothing has been typed yet.
        # D3: a partial line whose text is exactly known, after 10
        # minutes of operator silence, is taken over instead (saved, compact,
        # restored) — decided here, executed by the actuator.
        held = self._line_hold_reason()
        takeover: Optional[str] = None
        if held is not None:
            takeover = self._takeover_plan()
            if takeover is None:
                v.allowed = False
                v.reasons = blocking + [REASON_LINE_HELD]
                v.messages = msgs + [
                    f"The operator's line is not empty ({held}): a /compact typed now would merge "
                    "into their unsent text and be submitted as an ordinary message (live "
                    "2026-09-29), and the ESC ESC would destroy it. NOTHING was typed. Wait until "
                    "the operator's line is empty (they submit or clear it) or they have been idle "
                    f"{self.takeover_idle_seconds / 60:g} minutes (then the harness saves their text, "
                    "compacts and restores it), then call request_compact again. ch_approved does "
                    "not override this."]
                self._log_refusal(v, directive_path)
                return v

        if not blocking:
            if takeover is not None and not self._persist_takeover(v, takeover):
                return v
            if ch_approved:
                v.approval_note = note
                self._log_override(v, directive_path)
            elif v.confirmed:
                v.self_reason = self_reason
                self._log_confirmed(v, directive_path)
            elif reasoned:
                v.self_reason = self_reason
                self._log_reasoned(v, directive_path)
            return v

        if ch_approved:
            if takeover is not None and not self._persist_takeover(v, takeover):
                return v
            v.overridden = blocking
            v.approval_note = note
            self._log_override(v, directive_path)
            return v

        v.allowed = False
        v.reasons = blocking
        v.messages = msgs + [
            "If the pilot has approved this compact, re-call with ch_approved=true and "
            "approval_note='<what the pilot approved>'."] if REASON_CONFIRM not in blocking else msgs
        self._log_refusal(v, directive_path)
        return v

    # -- provenance ----------------------------------------------------------

    def _log_refusal(self, v: GateVerdict, directive_path: str) -> None:
        try:
            self._record("compact-refused", source="self-verb", reason="; ".join(v.reasons),
                         reasons=list(v.reasons), detail=" ".join(v.messages),
                         used_percentage=v.used_percentage, threshold=self.threshold,
                         directive_path=directive_path or None, alert_ts=v.alert_ts,
                         directive_mtime=v.directive_mtime, rule=RULE_REF,
                         confirm_window_seconds=(self.confirm_window
                                                 if REASON_CONFIRM in v.reasons else None),
                         first_call_ts=v.first_call_ts,
                         first_call_percentage=v.first_call_percentage)
        except Exception:  # noqa: BLE001 - logging must never turn a refusal into a crash
            pass

    def _log_confirmed(self, v: GateVerdict, directive_path: str) -> None:
        try:
            self._record("compact-confirmed", source="self-verb", self_reason=v.self_reason,
                         used_percentage=v.used_percentage,
                         first_call_ts=v.first_call_ts,
                         first_call_percentage=v.first_call_percentage,
                         second_call_percentage=v.used_percentage,
                         confirm_window_seconds=self.confirm_window,
                         threshold=self.threshold, directive_path=directive_path or None,
                         alert_ts=v.alert_ts, directive_mtime=v.directive_mtime, rule=RULE_REF)
        except Exception:  # noqa: BLE001
            pass

    def _log_reasoned(self, v: GateVerdict, directive_path: str) -> None:
        try:
            self._record("compact-reasoned", source="self-verb", self_reason=v.self_reason,
                         used_percentage=v.used_percentage, confirm_free_from=self.confirm_below,
                         threshold=self.threshold, directive_path=directive_path or None,
                         alert_ts=v.alert_ts, directive_mtime=v.directive_mtime, rule=RULE_REF)
        except Exception:  # noqa: BLE001
            pass

    def _log_override(self, v: GateVerdict, directive_path: str) -> None:
        try:
            self._record("compact-override", source="self-verb", ch_approved=True,
                         approval_note=v.approval_note, overridden=list(v.overridden),
                         used_percentage=v.used_percentage, threshold=self.threshold,
                         directive_path=directive_path or None, alert_ts=v.alert_ts,
                         directive_mtime=v.directive_mtime, rule=RULE_REF)
        except Exception:  # noqa: BLE001
            pass

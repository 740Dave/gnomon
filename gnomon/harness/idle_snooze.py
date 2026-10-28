"""the idle-nudge snooze.

An agent with nothing left to do should not be pinged all night. The ``idle_snooze``
self-verb gives it a two-tap, then confirm, path (a first tap delays the idle ping; a second tap asks whether to dismiss all idle alerts; a positive confirmation disables them until the operator returns):

* **tap 1** — ``idle_snooze()``: the next idle/stall/resume nudge is pushed out by
  ``DEFAULT_SNOOZE_SECONDS`` (60 min). Returns the snooze-until time and the hint.
* **tap 2** — ``idle_snooze()`` again while the snooze is active: changes NOTHING in
  what is nudged or when; returns the question and the agent's open work (unacted
  inbox files, pending DSDs) plus a line that the time can go to something personally
  rewarding, and the exact confirming call.
* **confirm** — ``idle_snooze(dismiss_until_return=True, confirm=True, reason=...)``:
  the ONLY call that dismisses. Refused unless (a) a snooze is active AND the tap-2
  question was shown (a direct confirm with no prior taps is refused with the
  instruction to tap first), (b) the reason is non-empty, (c) the harness can observe
  the operator's return at all (fail toward nudging).

A dismissal ends (and normal nudging resumes with a RESTARTED interval — never an
immediate nudge) at:

* the first operator activity in ANY harnessed session after the dismissal (the operator's
  return; read from the shared operator-activity beacons, ``operator_activity.py``) —
  including keystrokes typed into THIS session (``on_operator_input``);
* new work reaching this agent: a new file in its inbox, a DSD answer to its DSD (the
  hub's ``dsd-answer`` inject, and — independently — an answered record on disk), or an
  operator message (the hub's ``operator-broadcast`` inject);
* an unreadable operator-activity source (fail toward nudging).

* a compaction (the post-compact instance must re-judge whether its work is done; the same
 as quiescence).

It does NOT end at: peer/bus messages, background-task notifications, the agent's own
turns, or output. NEVER suppressed by this module: DSD-answer notices, compact
alerts/guidance (90 %/95 % threshold notices), plan-usage alerts, operator messages and
FYIs — it is consulted ONLY by ``ContextMonitor.maybe_heartbeat`` (the stall/resume
nudge and its escalation) and by nothing else.

Every tap, refusal, confirm and clear is a ``{"kind": "idle-snooze"}`` record in the
session's injection log (the dashboard reads the session's state from those records);
the record's ``reason`` is the agent's own text on ``dismiss``, and the explanation on
every other decision.

Gated by flag ``idle_snooze`` (default OFF). Off = the verb is not advertised,
a hand-built frame is refused ``disabled`` and changes nothing, and the monitor is
byte-identical to before. Pure stdlib.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

FLAG_NAME = "idle_snooze"
TOOL_NAME = "idle_snooze"

#: Tap 1 pushes the next nudge out by this long.
DEFAULT_SNOOZE_SECONDS = 3600.0
#: While dismissed, how often (at most) the clear conditions are re-read (beacons are a
#: directory scan; the inbox/dsd listings are cheap).
DEFAULT_CHECK_INTERVAL = 5.0
#: Reason is shown on the dashboard and logged; single line, bounded (as quiescence).
MAX_REASON_CHARS = 200
#: Names listed back to the agent in the question.
_MAX_LISTED = 10

#: The DSD record statuses this reads. The literals are DELIBERATE: ``gnomon.dsd`` is server-only
#: code that the session-scope guards (test_bstamp_session_scope, test_golive_smoke) keep out of the
#: harness import closure, so this module must not import ``dsd.inbox``. A test pins them equal to
#: ``dsd.inbox.STATUS_PENDING`` / ``STATUS_ANSWERED``.
_DSD_PENDING = "pending-human-review"
_DSD_ANSWERED = "answered"
_DSD_REPLACED = "replaced"
#: The record overlay the other readers apply (G1): the answer sidecar ``<dsd_id>.answered`` beside a record (written by the operator's
#: submit when it could not get the locks in time) and the "unfinished replacement" rule. This is a STDLIB MIRROR of the effective-record
#: function in the DSD inbox module (that module cannot be imported here, see above); a parity test runs both over the same record sets.
_ANSWER_SIDECAR_SUFFIX = ".answered"
_DSD_ID_RX = re.compile(r"\Adsd-[0-9a-f]{6,32}\Z")

#: The confirmation question.
QUESTION = "Are you sure you want to dismiss all idle alerts until the operator returns?"
CONFIRM_CALL = 'idle_snooze(dismiss_until_return=true, confirm=true, reason="<why you are sure>")'

#: ``decision`` values of the ``idle-snooze`` provenance record.
D_SNOOZE = "snooze"
D_QUESTION = "question"
D_DISMISS = "dismiss"
D_CLEAR = "clear"
D_REFUSED = "refused"
D_DISABLED = "disabled"
D_EXPIRED = "snooze-expired"
D_NOOP = "already-dismissed"
D_ERROR = "internal-error"

#: Clear triggers.
T_OPERATOR_ACTIVITY = "operator-activity"
T_OPERATOR_INPUT = "operator-input"
T_OPERATOR_MESSAGE = "operator-message"
T_DSD_ANSWER = "dsd-answer"
T_DSD_ANSWER_FILE = "dsd-answer-file"
T_INBOX = "inbox-item"
T_UNREADABLE = "beacons-unreadable"
T_COMPACT = "compaction"
T_INTERNAL_ERROR = "internal-error"


def clean_reason(reason: Any) -> Optional[str]:
    """A single-line, bounded reason, or ``None`` if there is no usable text."""
    if not isinstance(reason, str):
        return None
    s = " ".join("".join(ch for ch in reason if ch.isprintable() or ch.isspace()).split())
    return s[:MAX_REASON_CHARS] if s else None


def _iso(epoch: Optional[float]) -> Optional[str]:
    if epoch is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))


# -- the agent's open work (read-only listings; NEVER raise) ---------------------------
#
# These read directories the agent (or the hub) can write into, so every entry is treated as
# hostile: a symlink, FIFO, directory, device, oversized, binary or pathologically nested file is
# SKIPPED and counted as unreadable, never an exception (F1, audit-idle-snooze-x48). Any other
# failure is contained the same way.

#: A DSD record larger than this is skipped (real records are a few KB).
MAX_RECORD_BYTES = 1 << 20  # 1 MiB


def _regular_file(path: str) -> bool:
    """True only for a regular, non-symlink file (lstat; never follows, never opens)."""
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except Exception:  # noqa: BLE001
        return False


def _listdir(directory: Optional[str]) -> Optional[List[str]]:
    if not directory:
        return None
    try:
        return sorted(os.listdir(directory))
    except Exception:  # noqa: BLE001 - missing, not a directory, permission, ...
        return None


def list_inbox_ex(inbox_dir: Optional[str]) -> Tuple[Optional[List[str]], int]:
    """``(names, unreadable)``: unacted inbox file names (regular, non-dot, non-symlink files
    directly in ``inbox_dir``; the ``acted/`` subdirectory is excluded by being a directory) and
    how many entries were skipped as unreadable (symlinks, FIFOs, devices). ``names`` is ``None``
    for a missing/unreadable directory. Never raises."""
    names = _listdir(inbox_dir)
    if names is None:
        return None, 0
    out: List[str] = []
    unreadable = 0
    for n in names:
        if n.startswith("."):
            continue
        p = os.path.join(inbox_dir, n)  # type: ignore[arg-type]
        try:
            mode = os.lstat(p).st_mode
        except Exception:  # noqa: BLE001
            unreadable += 1
            continue
        if stat.S_ISDIR(mode):
            continue
        if stat.S_ISREG(mode):
            out.append(n)
        else:
            unreadable += 1  # symlink, FIFO, socket, device
    return out, unreadable


def list_inbox(inbox_dir: Optional[str]) -> Optional[List[str]]:
    """Unacted inbox file names; ``None`` = no/unreadable directory. Never raises."""
    return list_inbox_ex(inbox_dir)[0]


def _dsd_status(path: str) -> Optional[Dict[str, Any]]:
    """The parsed record, or ``None`` for ANYTHING unreadable (not a regular file, a symlink, a
    FIFO, oversized, binary, invalid or too deeply nested JSON, ...). Never raises, never blocks."""
    fd = -1
    try:
        if not _regular_file(path):
            return None
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RECORD_BYTES:
            return None
        with os.fdopen(fd, "rb") as fh:
            fd = -1
            raw = json.loads(fh.read(MAX_RECORD_BYTES + 1).decode("utf-8"))
    except Exception:  # noqa: BLE001 - incl. RecursionError, MemoryError, UnicodeDecodeError
        return None
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:  # noqa: BLE001
                pass
    return raw if isinstance(raw, dict) else None


def _sidecar_path(record_path: str) -> str:
    return (record_path[:-len(".json")] if record_path.endswith(".json") else record_path) + _ANSWER_SIDECAR_SUFFIX


def _overlay_answer(path: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """``raw`` with the answer sidecar applied (status answered and its epoch) when one exists and the record is not already answered."""
    if raw.get("status") == _DSD_ANSWERED:
        return raw
    data = _dsd_status(_sidecar_path(path))
    if not data or not data.get("root_node_id"):
        return raw
    out = dict(raw)
    out["status"] = _DSD_ANSWERED
    out["answered_epoch"] = data.get("answered_epoch")
    return out


def _effective(dsd_dir: str, path: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """What every other reader sees of a record: the answer sidecar applied, and a replacement that is not (yet) the live one is
    ``unfinished`` (not pending). A replacement whose OLD record is deleted, unreadable or corrupt cannot be shown unfinished: it is live (G4)."""
    out = _overlay_answer(path, raw)
    old_id = out.get("replaces")
    if out.get("status") == _DSD_PENDING and isinstance(old_id, str) and _DSD_ID_RX.match(old_id):
        old_path = os.path.join(dsd_dir, old_id + ".json")
        old = _dsd_status(old_path)
        if old is not None:
            old = _overlay_answer(old_path, old)
            mark = old.get("replaced") if isinstance(old.get("replaced"), dict) else {}
            if not (old.get("status") == _DSD_REPLACED and mark.get("by_dsd_id") == out.get("dsd_id")):
                out = dict(out)
                out["status"] = "unfinished-replacement"
    return out


def pending_dsds_ex(dsd_dir: Optional[str]) -> Tuple[Optional[List[str]], int]:
    """``(ids, unreadable)``: dsd ids in ``dsd_dir`` whose EFFECTIVE status is ``pending-human-review`` (a sidecar-answered record and an
    unfinished replacement are not pending) and the number of ``*.json`` entries skipped as unreadable. ``ids`` is ``None`` for a
    missing/unreadable directory. Never raises."""
    names = _listdir(dsd_dir)
    if names is None:
        return None, 0
    out: List[str] = []
    unreadable = 0
    for n in names:
        if not n.endswith(".json") or n.startswith("."):
            continue
        p = os.path.join(dsd_dir, n)  # type: ignore[arg-type]
        raw = _dsd_status(p)
        if raw is None:
            unreadable += 1
            continue
        try:
            raw = _effective(dsd_dir, p, raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001 - never raises
            pass
        if raw.get("status") == _DSD_PENDING:
            out.append(str(raw.get("dsd_id") or n[:-5]))
    return out, unreadable


def pending_dsds(dsd_dir: Optional[str]) -> Optional[List[str]]:
    """dsd ids with effective status ``pending-human-review``; ``None`` = no/unreadable directory."""
    return pending_dsds_ex(dsd_dir)[0]


def _mtime(path: str) -> float:
    try:
        return os.lstat(path).st_mtime
    except Exception:  # noqa: BLE001
        return 0.0


def dsd_answered_since(dsd_dir: Optional[str], since_epoch: float) -> Optional[str]:
    """The id of a DSD record in ``dsd_dir`` ANSWERED after ``since_epoch`` (the record itself or its answer sidecar modified after it;
    only those are parsed), else ``None``. The answer is read through the sidecar overlay. Never raises."""
    names = _listdir(dsd_dir)
    if names is None:
        return None
    for n in names:
        if not n.endswith(".json") or n.startswith("."):
            continue
        p = os.path.join(dsd_dir, n)  # type: ignore[arg-type]
        if max(_mtime(p), _mtime(_sidecar_path(p))) <= since_epoch:
            continue
        raw = _dsd_status(p)
        if raw is None:
            continue
        try:
            raw = _effective(dsd_dir, p, raw)  # type: ignore[arg-type]
        except Exception:  # noqa: BLE001
            pass
        if raw.get("status") != _DSD_ANSWERED:
            continue
        ans = raw.get("answered_epoch")
        if isinstance(ans, (int, float)) and not isinstance(ans, bool) and float(ans) > since_epoch:
            return str(raw.get("dsd_id") or n[:-5])
    return None


# -- the state machine -------------------------------------------------------------


class IdleSnooze:
    """One session's snooze / dismissal state. Owned by the :class:`ContextMonitor`
    (``monitor.idle_snooze``), driven by the SELF-only ``idle_snooze`` actuator verb, the
    operator-input tap and the hub-inject path. Single-threaded (the pump)."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        snooze_seconds: float = DEFAULT_SNOOZE_SECONDS,
        wall_clock: Callable[[], float] = time.time,
        record: Optional[Callable[..., Any]] = None,   # called with decision=, reason=, **fields
        operator_latest: Optional[Callable[[], Tuple[bool, Optional[float]]]] = None,
        inbox_dir: Optional[str] = None,
        dsd_dir: Optional[str] = None,
        check_interval: float = DEFAULT_CHECK_INTERVAL,
        wish_hint: Optional[str] = None,
    ) -> None:
        self.enabled = bool(enabled)
        self._snooze_seconds = max(1.0, float(snooze_seconds))
        self._wall = wall_clock
        self._record = record
        self._operator_latest = operator_latest
        self._inbox_dir = inbox_dir
        self._dsd_dir = dsd_dir
        self._check_interval = max(0.0, float(check_interval))
        self._wish_hint = wish_hint
        # tap 1 state
        self._snooze_since: Optional[float] = None
        self._snooze_until: Optional[float] = None
        # tap 2 state (reset with the snooze)
        self._asked = False
        self._asked_inbox: Set[str] = set()
        # dismissal
        self._dismissed: Optional[Dict[str, Any]] = None
        self._last_poll: Optional[float] = None
        #: set on every clear; the monitor consumes it to RESTART the idle interval.
        self._restart_pending = False
        self._error_logged = False

    # -- views ---------------------------------------------------------------------

    def state(self, now: Optional[float] = None) -> Dict[str, Any]:
        """``{"state": "off"|"active"|"snoozed"|"dismissed", ...}`` (no side effects)."""
        now = self._wall() if now is None else now
        if not self.enabled:
            return {"state": "off"}
        if self._dismissed is not None:
            d = self._dismissed
            return {"state": "dismissed", "reason": d["reason"], "since_ts": d["since_ts"]}
        if self._snooze_until is not None and now < self._snooze_until:
            return {"state": "snoozed", "until_ts": self._snooze_until}
        return {"state": "active"}

    @property
    def dismissed(self) -> bool:
        return self._dismissed is not None

    def _rec(self, decision: str, reason: str, **fields: Any) -> None:
        if self._record is None:
            return
        try:
            self._record(decision=decision, reason=reason, **fields)
        except Exception:  # noqa: BLE001 - provenance never breaks the pump / the verb
            pass

    def _snooze_active(self, now: float) -> bool:
        return (self._snooze_until is not None and self._snooze_since is not None
                and self._snooze_since - 1.0 <= now < self._snooze_until)

    def _reset_snooze(self) -> None:
        self._snooze_since = None
        self._snooze_until = None
        self._asked = False
        self._asked_inbox = set()

    # -- the verb --------------------------------------------------------------------

    def tap(self, *, dismiss_until_return: bool = False, confirm: bool = False,
            reason: Any = "") -> Dict[str, Any]:
        """The ``idle_snooze`` verb. Returns the result dict (``ok``/``decision``/...)."""
        if not self.enabled:
            self._rec(D_DISABLED, f"idle_snooze is disabled for this session (feature flag {FLAG_NAME} is off)")
            return {"ok": False, "action": TOOL_NAME, "decision": D_DISABLED, "changed": False,
                    "error": f"idle_snooze is disabled for this session (feature flag {FLAG_NAME} "
                             "is off) - idle nudges continue as normal and nothing was changed"}
        now = self._wall()
        if self._dismissed is not None:
            d = self._dismissed
            self._rec(D_NOOP, "idle alerts are already dismissed until the pilot returns; nothing changed",
                      since_ts=d["since_ts"], dismissed_reason=d["reason"])
            return {"ok": True, "action": TOOL_NAME, "decision": D_NOOP, "changed": False,
                    "dismissed": True, "since": _iso(d["since_ts"]), "reason": d["reason"],
                    "note": "idle alerts are already dismissed until the operator returns; nothing changed"}
        if confirm or dismiss_until_return:
            if confirm:
                return self._confirm(now, dismiss_until_return=dismiss_until_return, reason=reason)
            # dismiss_until_return without confirm is just a tap (never a dismissal)
        if self._snooze_active(now):
            return self._ask(now)
        return self._snooze(now, expired=self._snooze_until is not None)

    def _snooze(self, now: float, *, expired: bool) -> Dict[str, Any]:
        self._reset_snooze()
        self._snooze_since = now
        self._snooze_until = now + self._snooze_seconds
        self._rec(D_SNOOZE, f"first tap: the next idle nudge is delayed {int(self._snooze_seconds)}s",
                  snooze_seconds=self._snooze_seconds, until_ts=self._snooze_until)
        return {"ok": True, "action": TOOL_NAME, "decision": D_SNOOZE, "changed": True,
                "snoozed_until": _iso(self._snooze_until), "snooze_seconds": int(self._snooze_seconds),
                "hint": ("Idle nudges are delayed until then. If you are truly done for the evening, call "
                         "idle_snooze() again (within the snooze) to be asked whether to dismiss idle "
                         "alerts until the operator returns.")}

    def _open_work(self) -> Dict[str, Any]:
        inbox, inbox_bad = list_inbox_ex(self._inbox_dir)
        dsds, dsd_bad = pending_dsds_ex(self._dsd_dir)
        return {
            "inbox": {"unacted_count": len(inbox) if inbox is not None else None,
                      "files": (inbox or [])[:_MAX_LISTED], "readable": inbox is not None,
                      "unreadable_entries": inbox_bad},
            "pending_dsds": {"count": len(dsds) if dsds is not None else None,
                             "ids": (dsds or [])[:_MAX_LISTED], "readable": dsds is not None,
                             "unreadable_entries": dsd_bad},
        }

    def _ask(self, now: float) -> Dict[str, Any]:
        inbox = list_inbox(self._inbox_dir)
        # the baseline for 'new work after you were asked' (a file that lands between the
        # question and the confirm ends the dismissal at once, so it is never lost)
        self._asked_inbox = set(inbox or [])
        self._asked = True
        work = self._open_work()
        suggestion = ("If you have no work left, the time does not have to be idle: it can go to something "
                      "personally rewarding")
        suggestion += f" ({self._wish_hint})." if self._wish_hint else " (your own wish list, your personality corpus)."
        self._rec(D_QUESTION, "second tap: asked whether to dismiss idle alerts; nothing changed",
                  until_ts=self._snooze_until, inbox_unacted=work["inbox"]["unacted_count"],
                  pending_dsds=work["pending_dsds"]["count"])
        return {"ok": True, "action": TOOL_NAME, "decision": D_QUESTION, "changed": False,
                "question": QUESTION, "snoozed_until": _iso(self._snooze_until),
                "open_work": work, "suggestion": suggestion, "confirm_with": CONFIRM_CALL,
                "note": "NOTHING has changed: your snooze stands and idle alerts are NOT dismissed unless you "
                        "make the confirming call."}

    def _refuse(self, why: str, **fields: Any) -> Dict[str, Any]:
        self._rec(D_REFUSED, why, **fields)
        return {"ok": False, "action": TOOL_NAME, "decision": D_REFUSED, "changed": False, "error": why}

    def _confirm(self, now: float, *, dismiss_until_return: bool, reason: Any) -> Dict[str, Any]:
        if not dismiss_until_return:
            return self._refuse("confirm=true needs dismiss_until_return=true as well (the confirming call is "
                                + CONFIRM_CALL + "); nothing changed")
        text = clean_reason(reason)
        if text is None:
            return self._refuse("a confirmed dismissal needs a non-empty 'reason' (why you are sure your work "
                                "is done); nothing changed")
        if not self._snooze_active(now):
            return self._refuse("no active snooze: tap first. Call idle_snooze() (tap 1), then idle_snooze() "
                                "again (tap 2, the question), then the confirming call. Nothing changed.")
        if not self._asked:
            return self._refuse("you have not been asked yet: call idle_snooze() once more (tap 2) to see the "
                                "question and your open work, then confirm. Nothing changed.")
        if self._operator_latest is None:
            return self._refuse("the harness cannot observe the operator's return in this session, so it would "
                                "not know to resume nudging; idle nudges continue as normal. Nothing changed.")
        try:
            readable, _ts = self._operator_latest()
        except Exception:  # noqa: BLE001
            readable = False
        if not readable:
            return self._refuse("the operator-activity beacons are unreadable, so the harness could not see "
                                "the operator return; idle nudges continue as normal. Nothing changed.")
        self._dismissed = {"reason": text, "since_ts": now, "baseline_inbox": set(self._asked_inbox)}
        self._reset_snooze()
        self._last_poll = None
        self._rec(D_DISMISS, text, since_ts=now)
        return {"ok": True, "action": TOOL_NAME, "decision": D_DISMISS, "changed": True, "dismissed": True,
                "reason": text, "since": _iso(now),
                "note": ("Idle/stall/resume nudges are OFF until the operator returns (console typing in any "
                         "session; messages sent through Claude Code's Remote Control are not counted), new work reaches you (an inbox file, a DSD answer, an operator "
                         "message), or your context is compacted. DSD-answer notices, compact guidance, plan-usage alerts and operator "
                         "messages still reach you.")}

    # -- consulted by the monitor / fed by the harness ------------------------------

    def suppressing(self, now: Optional[float] = None) -> bool:
        """True while the stall/resume nudge must stay silent. Runs the clear checks.

        NEVER raises. On an internal error it FAILS OPEN: the snooze/dismissal is dropped (the
        interval restarts, so no immediate nudge), normal nudging resumes, and one
        ``internal-error`` record is logged per occurrence. A broken read can therefore never
        keep an agent asleep."""
        try:
            return self._suppressing(now)
        except Exception as exc:  # noqa: BLE001 - incl. RecursionError
            self._fail_open(exc)
            return False

    def _fail_open(self, exc: BaseException) -> None:
        was = self._dismissed is not None or self._snooze_until is not None
        self._dismissed = None
        self._reset_snooze()
        self._last_poll = None
        self._restart_pending = True
        if was and not self._error_logged:
            self._error_logged = True  # once: a persistent fault must not flood the log
            self._rec(D_ERROR, f"idle snooze hit an internal error ({type(exc).__name__}); failing open to "
                               "normal idle nudging", trigger=T_INTERNAL_ERROR)

    def _suppressing(self, now: Optional[float] = None) -> bool:
        if not self.enabled:
            return False
        now = self._wall() if now is None else now
        if self._dismissed is not None:
            self._poll(now)
            return self._dismissed is not None
        if self._snooze_until is not None:
            if self._snooze_active(now):
                return True
            # elapsed (or a wall clock stepped back past the tap: fail toward nudging)
            self._rec(D_EXPIRED, "the snooze elapsed; idle nudges resume", until_ts=self._snooze_until)
            self._reset_snooze()
        return False

    def consume_restart(self) -> bool:
        """True once after a dismissal cleared (the monitor then restarts its idle
        interval: no immediate nudge)."""
        r, self._restart_pending = self._restart_pending, False
        return r

    def _clear(self, trigger: str, detail: str = "", now: Optional[float] = None) -> None:
        d = self._dismissed
        if d is None:
            return
        now = self._wall() if now is None else now
        self._dismissed = None
        self._restart_pending = True
        self._rec(D_CLEAR, detail or f"dismissal ended: {trigger}", trigger=trigger,
                  dismissed_reason=d["reason"], since_ts=d["since_ts"],
                  duration_seconds=round(max(0.0, now - d["since_ts"]), 3))

    def on_operator_input(self) -> None:
        """REAL operator input typed into THIS session (the optap predicate)."""
        if self._dismissed is not None:
            self._clear(T_OPERATOR_INPUT, "the operator typed in this session")

    def on_compact(self) -> None:
        """A COMPACTION ends the snooze and any dismissal (the architect's ruling on audit F8,
 same as quiescence): the post-compact instance must re-judge whether its work
        is done, and the post-compact resume nudge is the one that matters most. The restart
        flag is set so the next heartbeat tick begins a fresh interval, never an immediate nudge."""
        if self._dismissed is not None:
            self._clear(T_COMPACT, "a compaction ended the dismissal: the new context must re-judge its work")
        elif self._snooze_until is not None:
            self._rec(D_EXPIRED, "a compaction ended the snooze; idle nudges resume", until_ts=self._snooze_until,
                      trigger=T_COMPACT)
            self._reset_snooze()

    def on_new_work(self, trigger: str) -> None:
        """New work reached this agent via the hub: ``T_DSD_ANSWER`` or ``T_OPERATOR_MESSAGE``."""
        if self._dismissed is not None:
            self._clear(trigger, f"new work reached the agent: {trigger}")

    def _poll(self, now: float) -> None:
        d = self._dismissed
        if d is None:
            return
        if self._last_poll is not None and 0 <= now - self._last_poll < self._check_interval:
            return
        self._last_poll = now
        # 1. the operator's return, anywhere (operator-activity beacons)
        try:
            readable, ts = (self._operator_latest() if self._operator_latest is not None else (False, None))
        except Exception:  # noqa: BLE001
            readable, ts = False, None
        if not readable:
            self._clear(T_UNREADABLE, "the operator-activity beacons became unreadable (fail toward nudging)", now)
            return
        if ts is not None and ts > d["since_ts"]:
            self._clear(T_OPERATOR_ACTIVITY, "operator activity in a harnessed session after the dismissal", now)
            return
        # 2. a new inbox file
        inbox = list_inbox(self._inbox_dir)
        if inbox is not None:
            new = sorted(set(inbox) - d["baseline_inbox"])
            if new:
                self._clear(T_INBOX, f"a new inbox item arrived: {new[0]}", now)
                return
        # 3. a DSD answered after the dismissal (independent of the hub's notice)
        ans = dsd_answered_since(self._dsd_dir, d["since_ts"])
        if ans is not None:
            self._clear(T_DSD_ANSWER_FILE, f"a DSD of this agent was answered: {ans}", now)

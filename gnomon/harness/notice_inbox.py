"""Harness side of the durable DSD-answer notice (D15
amendment, operator-approved 2026-09-29).

The hub sends a queued notice down the rendezvous as a hub-origin ``inject`` that
claims ``source: dsd-answer``. The :class:`~gnomon.harness.actuator.Actuator`
hands it here instead of typing it at once, because a DSD-answer notice is not
urgent and must never land in the middle of something:

* HELD per the operator tap's line state (D15 fix pass rule):
    - ``empty``   — deliver once the operator has been quiet ``quiet_seconds`` (3s);
    - ``partial`` — hold until their Enter (never typed into a half-written line);
    - ``unknown`` — the line punted (cursor/history/Tab/Ctrl-L/paste): after
      ``unknown_after_seconds`` (5 min) of operator silence, treat it as empty.
  Also held while the operator has ``\\pause``d injections, and until the child's
  REPL is ready (the stall-guard's ``require_ready`` rule: the context monitor has
  primed, with a bounded boot grace for sessions whose gauge never reads).
* HELD while the agent is parked on an interactive modal (``AskUserQuestion`` /
  ``ExitPlanMode``, read from the transcript): the dialog would swallow the text
 and its CR would pick the first option (2026-10-03);
* DELIVERED through the ordinary logged injector — ``clear=False`` (no ESC ESC:
  never interrupts a working turn; Claude Code queues the line), ``source:
  dsd-answer``, ``trigger: dsd-answer``, with ``dsd_id`` / ``root_id`` recorded in
  the provenance record.
* WRITTEN-RECORDED: after the injector returns (every phase incl. the CR has
  been written), a ``{kind: dsd-notice, decision: written}`` record is appended.
* ACKED (``{"type": "notice_ack", "dsd_id"}`` up the hub link) only AFTER that
  written record. The hub deletes its durable file only on that ack.
* DE-DUPLICATED by ``dsd_id``: a notice already written (this harness lifetime,
  or with a ``written`` record in this session's own injection log) is re-acked,
  never re-typed. FIX PASS: the gate's ``allow`` record is NOT proof — it is
  appended BEFORE the bytes, so a crash between the two would otherwise lose the
  notice forever.

Every method runs on the single pump thread; nothing here blocks except the
injector's own inter-phase beats.
"""

from __future__ import annotations

import collections
import json
import os
import time
from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from .hooks import SOURCE_DSD_ANSWER, Decision, Outcome
from .line_hold import line_hold_reason

TRIGGER = "dsd-answer"
#: Seconds between ack retries for a written-but-unacked notice.
_ACK_RETRY_SECONDS = 1.0
#: A notice counts as "held" in the count the ``context`` verb and the dashboard report once it has waited longer than this.
HELD_COUNT_AFTER_SECONDS = 120.0
#: Bound on remembered ids (a session answers a handful of DSDs, not thousands).
_MAX_REMEMBERED = 4096


class DsdNoticeInbox:
    def __init__(
        self,
        *,
        inject: Callable[..., Optional[Outcome]],
        ack: Callable[[Dict[str, Any]], None],
        gate: Any = None,
        line_is_empty: Optional[Callable[[], bool]] = None,
        ready: Optional[Callable[[], bool]] = None,
        quiet_seconds: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
        line_state: Optional[Callable[[], str]] = None,
        unknown_after_seconds: float = 300.0,
        modal_open: Optional[Callable[[], Optional[str]]] = None,
        inline_text: Optional[Callable[[str, str], Optional[str]]] = None,
    ) -> None:
        # pre-review B2 (dsd_notice_inline): ``inline_text(dsd_id, root_id)`` -> the inline answer notice built from the minted
        # answer nodes, or None (flag OFF / nothing to build from: today's pointer line). None here = never asked, byte-for-byte today.
        self._inline_text = inline_text
        # 2026-10-03: returns the name of an interactive tool
        # (AskUserQuestion / ExitPlanMode) the agent is parked on, else None. A CR
        # typed into that dialog selects its first option and the text is lost.
        self._modal_open = modal_open
        self._inject = inject
        self._ack = ack
        self._gate = gate
        self._line_is_empty = line_is_empty
        # D15 fix pass: tri-state ("empty"/"partial"/"unknown"); wins over
        # line_is_empty (kept for compatibility: True->empty, False->partial).
        self._line_state = line_state
        self._unknown_after = max(0.0, float(unknown_after_seconds))
        self._boot = clock()
        self._ready = ready
        self._quiet = max(0.0, float(quiet_seconds))
        self._clock = clock
        self._last_operator: Optional[float] = None
        self._pending: "collections.OrderedDict[str, Dict[str, Any]]" = collections.OrderedDict()
        self._delivered: Set[str] = set()
        self._unacked: Set[str] = set()
        self._refused: Set[str] = set()
        self._deferred: Dict[str, Any] = {}
        self._next_ack_retry = 0.0
        self._held_logged: Set[str] = set()
        # pre-review B2 (count only): when each pending notice was first offered, for held_count(). Observability
        # only; nothing in the hold or release rules reads it.
        self._held_since: Dict[str, float] = {}
        # r2 (F3): None (flag OFF) = no boot rule at all.
        self._boot_blocks: Optional[Callable[[], bool]] = None

    def set_boot_blocks(self, fn: Optional[Callable[[], bool]]) -> None:
        """Wire the post-compact boot-pending signal (``proxy._boot_blocks``: gives the
        boot prompt its chance to type, then reports whether it is still owed). While
        it is True this inbox HOLDS; it flushes once the boot prompt is written,
        denied or dropped."""
        self._boot_blocks = fn

    # -- observability ---------------------------------------------------------

    def held_ids(self) -> List[str]:
        return list(self._pending)

    def held_count(self, over_seconds: float = HELD_COUNT_AFTER_SECONDS) -> int:
        """How many notices have been held (offered, not yet typed) for MORE than ``over_seconds``. Pure read."""
        now = self._clock()
        return sum(1 for d in self._pending
                   if (now - self._held_since.get(d, now)) > over_seconds)

    def delivered_ids(self) -> List[str]:
        return sorted(self._delivered)

    def status(self) -> Dict[str, Any]:
        return {"held": self.held_ids(), "delivered": self.delivered_ids(),
                "unacked": sorted(self._unacked), "refused": sorted(self._refused),
                "deferred": sorted(self._deferred)}

    def _record(self, decision: str, dsd_id: str, **extra: Any) -> None:
        if self._gate is None:
            return
        try:
            self._gate.record("dsd-notice", decision=decision, dsd_id=dsd_id, **extra)
        except Exception:  # noqa: BLE001 - provenance must never break the pump
            pass

    # -- seeding (restart de-duplication) ---------------------------------------

    def seed_from_records(self, records: Iterable[Dict[str, Any]]) -> int:
        """Mark every ``dsd-answer`` notice this session provably WROTE as
        delivered (so a re-drained, never-acked notice is re-acked, not re-typed).

        FIX PASS: only a ``{kind: dsd-notice, decision: written}`` record counts —
        it is appended AFTER the bytes and the CR. The gate's ``injection``/
        ``allow`` record precedes the write and proves nothing."""
        n = 0
        for rec in records:
            if (isinstance(rec, dict) and rec.get("kind") == "dsd-notice"
                    and rec.get("decision") == "written"
                    and isinstance(rec.get("dsd_id"), str)):
                if rec["dsd_id"] not in self._delivered and len(self._delivered) < _MAX_REMEMBERED:
                    self._delivered.add(rec["dsd_id"])
                    n += 1
        return n

    def seed_from_log_file(self, path: Optional[str]) -> int:
        if not path or not os.path.isfile(path):
            return 0
        records: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"dsd-notice"' not in line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            return 0
        return self.seed_from_records(records)

    # -- inputs ----------------------------------------------------------------

    def note_operator_activity(self) -> None:
        self._last_operator = self._clock()

    def offer(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """Accept one hub-delivered notice. Returns the control-channel response."""
        dsd_id = command.get("dsd_id")
        root_id = command.get("root_id")
        # FIX PASS 2 (re-audit HIGH): the frame's / file's ``text`` is NEVER typed.
        # The line is REBUILT from the two ids, each held to the strict grammar
        # (dsd-<hex> / DEC-DSD-<HEX>, root derived from dsd_id); anything else is
        # refused loudly and not acked (the hub keeps — or quarantines — the file).
        from ..dsd.live import DsdCorpusError
        from ..dsd.notice import notice_line, valid_notice_ids

        if not valid_notice_ids(dsd_id, root_id):
            self._record("refused-bad-ids", str(dsd_id)[:80], root_id=str(root_id)[:80])
            return {"ok": False, "action": "inject", "decision": "refused",
                    "error": "dsd-answer notice ids outside the strict grammar — nothing typed"}
        try:
            text = notice_line(dsd_id, root_id)
        except DsdCorpusError as exc:
            # g70: a fail-closed DSD refusal closes the DSD notice, never the control channel. Not acked: the hub keeps the file.
            self._record("refused-dsd-corpus", dsd_id, error=str(exc)[:300])
            return {"ok": False, "action": "inject", "decision": "refused",
                    "error": f"dsd-corpus refused, nothing typed: {exc}"}
        if dsd_id in self._delivered:
            # already typed — the hub never saw our ack; re-ack, never re-type
            acked = self._send_ack(dsd_id)
            self._record("duplicate-reacked" if acked else "duplicate-ack-failed", dsd_id)
            return {"ok": True, "action": "inject", "decision": "duplicate", "acked": acked}
        if dsd_id in self._refused or dsd_id in self._deferred:
            return {"ok": True, "action": "inject", "decision": "already-decided"}
        if dsd_id not in self._pending:
            self._held_since.setdefault(dsd_id, self._clock())
            pending = {"dsd_id": dsd_id, "text": text, "root_id": root_id if isinstance(root_id, str) else None}
            inline = self._build_inline(dsd_id, root_id)
            if inline is not None:
                pending["pointer_text"] = text          # today's line, kept for the fallback (C10)
                pending["text"] = inline
            self._pending[dsd_id] = pending
        delivered = self._deliver_one()
        state = "delivered" if delivered == dsd_id else "held"
        if state == "held" and dsd_id not in self._held_logged:
            self._held_logged.add(dsd_id)
            self._record("held", dsd_id, reason=self._hold_reason() or "queued behind another notice")
        return {"ok": True, "action": "inject", "decision": state}

    def _build_inline(self, dsd_id: str, root_id: str) -> Optional[str]:
        """The inline answer text, or None for today's pointer line. ANY failure (the builder raising, a text that does not pass
        :func:`~gnomon.dsd.notice.validate_inline_text`) is recorded (``inline-fallback``) and answers None (condition C10)."""
        if self._inline_text is None:
            return None
        try:
            text = self._inline_text(dsd_id, root_id)
            if text is None:
                return None
            from ..dsd.notice import validate_inline_text

            bad = validate_inline_text(text, dsd_id, root_id)
            if bad is not None:
                raise ValueError(f"inline text refused: {bad}")
            return text
        except Exception as exc:  # noqa: BLE001 - the pointer notice must still arrive
            self._record("inline-fallback", dsd_id, stage="build", reason=f"{type(exc).__name__}: {exc}"[:300])
            return None

    # -- the pump tick -----------------------------------------------------------

    def tick(self) -> None:
        try:
            self._resolve_deferred()
            if self._unacked and self._clock() >= self._next_ack_retry:
                self._next_ack_retry = self._clock() + _ACK_RETRY_SECONDS
                for dsd_id in sorted(self._unacked):
                    self._send_ack(dsd_id)
            self._deliver_one()
        except Exception:  # noqa: BLE001 - never take the pump down
            pass

    # -- internals ---------------------------------------------------------------

    def _current_line_state(self) -> str:
        if self._line_state is not None:
            st = self._line_state()
            return st if st in ("empty", "partial", "unknown") else "unknown"
        if self._line_is_empty is not None:
            return "empty" if self._line_is_empty() else "partial"
        return "empty"

    def _hold_reason(self) -> Optional[str]:
        if self._ready is not None and not self._ready():
            return "child REPL not ready yet"
        if self._gate is not None and getattr(self._gate, "paused", False):
            return "operator paused injections"
        if self._modal_open is not None:
            try:
                modal = self._modal_open()
            except Exception:  # noqa: BLE001 - a monitoring gap never wedges delivery
                modal = None
            if modal:
                return f"agent is waiting on an interactive {modal} dialog (a typed CR would answer it)"
        state = self._current_line_state()
        since = self._clock() - (self._last_operator if self._last_operator is not None else self._boot)
        # 2026-09-29 (stall-hold): the partial/unknown rule is the SHARED one
        # (harness.line_hold) — the stall guard and monitor notices use it too.
        line_reason = line_hold_reason(state, since, self._unknown_after)
        if line_reason is not None:
            return line_reason
        if state != "unknown" and self._last_operator is not None and self._quiet > 0 and since < self._quiet:
            return "operator typed within the quiet window"
        if self._boot_blocks is not None:
            try:
                if self._boot_blocks():
                    return "post-compact boot prompt pending (it goes first)"
            except Exception:  # noqa: BLE001 - the boot leg can never break the pump
                pass
        return None

    def _deliver_one(self) -> Optional[str]:
        if not self._pending or self._hold_reason() is not None:
            return None
        dsd_id, notice = next(iter(self._pending.items()))
        meta = {"dsd_id": dsd_id}
        if notice.get("root_id"):
            meta["root_id"] = notice["root_id"]
        outcome = self._inject(notice["text"], clear=False, source=SOURCE_DSD_ANSWER,
                               trigger=TRIGGER, meta=meta)
        self._pending.pop(dsd_id, None)
        self._held_since.pop(dsd_id, None)
        decision = outcome.decision if isinstance(outcome, Outcome) else Decision.ALLOW
        if decision in (Decision.ALLOW, Decision.MODIFY):
            self._mark_written(dsd_id)
            return dsd_id
        if decision is Decision.DEFER:
            self._deferred[dsd_id] = outcome.held_id if isinstance(outcome, Outcome) else None
            return None
        if notice.get("pointer_text") and notice.get("text") != notice["pointer_text"]:
            # pre-review B2 (C10): the INLINE text was refused at delivery; the agent still gets today's pointer notice.
            notice["text"] = notice["pointer_text"]
            self._pending[dsd_id] = notice
            self._pending.move_to_end(dsd_id, last=False)
            self._record("inline-fallback", dsd_id, stage="deliver",
                         reason=(outcome.reason if isinstance(outcome, Outcome) else "refused")[:300])
            return None
        # DENY: not written, not acked — the durable copy stays on the hub for a
        # human; not retried in this harness lifetime.
        self._refused.add(dsd_id)
        self._record("denied-not-acked", dsd_id,
                     reason=outcome.reason if isinstance(outcome, Outcome) else "")
        return None

    def _mark_written(self, dsd_id: str) -> None:
        if len(self._delivered) < _MAX_REMEMBERED:
            self._delivered.add(dsd_id)
        # FIX PASS: the durable proof of delivery — AFTER the bytes + CR, BEFORE the ack.
        self._record("written", dsd_id)
        if not self._send_ack(dsd_id):
            self._unacked.add(dsd_id)
            self._next_ack_retry = self._clock() + _ACK_RETRY_SECONDS

    def _send_ack(self, dsd_id: str) -> bool:
        try:
            self._ack({"type": "notice_ack", "dsd_id": dsd_id})
        except Exception:  # noqa: BLE001 - disconnected: the hub re-drains on re-register
            self._unacked.add(dsd_id)
            return False
        self._unacked.discard(dsd_id)
        self._record("acked", dsd_id)
        return True

    def _resolve_deferred(self) -> None:
        """A hook DEFERred a notice: ack it once the operator releases it; a
        rejected one stays unacked (durable on the hub) and is not retried."""
        if not self._deferred or self._gate is None:
            return
        try:
            still_held = {i for i, _ev, _why in self._gate.held()}
        except Exception:  # noqa: BLE001
            return
        for dsd_id, held_id in list(self._deferred.items()):
            if held_id in still_held:
                continue
            self._deferred.pop(dsd_id, None)
            released = any(r.get("held_id") == held_id and r.get("decision") == "release"
                           for r in reversed(getattr(self._gate.log, "records", [])[-500:]))
            if released:
                self._mark_written(dsd_id)
            else:
                self._refused.add(dsd_id)
                self._record("rejected-not-acked", dsd_id)

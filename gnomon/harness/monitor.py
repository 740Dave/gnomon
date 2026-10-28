"""The context monitor.

Threshold hysteresis with prime-on-first-read, living in the proxy (it sits on
the transcript). Behaviour, verbatim from the spec:

* alerts **arm / spend and RE-ARM after a context drop** — a ``/compact`` drops
  context and re-arms the threshold, so it can fire again on the next climb;
* **prime on first read** so a resumed high session does not fire retroactively;
* hosted-child thresholds **90 / 95**;
* notice kinds FYI, compact-guidance, heartbeat, context-report.

``read_context`` is INJECTED (any ``Callable[[], Optional[float]]``; see
``context_readers.py``). The monitor emits :class:`Notice` objects to an
``on_notice`` sink and reports ``used_percentage`` to an ``on_report`` sink —
the proxy wires the former to "type the notice into the child" and the latter to
"report up to the GUI".

⚠ THRESHOLD → NOTICE-KIND MAPPING — flagged for the cold audit.
The spec (``01-...harness.lts`` lines 112-114; DESIGN.md 4.3/4.7) makes 90 the
ACTIONABLE line (the prepare-for-compact / compact-guidance flow) and 95 a
"louder FYI ... hard backstop, not an actionable trigger". So the default map is
``{90: compact-guidance, 95: FYI}``. The build-task bullet's parenthetical
("FYI(90)/compact-guidance(95 or per spec)") reads the opposite way; it is
hedged with "or per spec", and the normative spec is followed here. The mapping
is a constructor argument, so the reading can be flipped in one place if the
audit rules otherwise.
"""

from __future__ import annotations

import dataclasses
import enum
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .constants import HOSTED_CHILD_THRESHOLDS
from .stamps import stamp_gm_tag
from .line_hold import DEFAULT_UNKNOWN_AFTER_SECONDS, line_hold_reason
from . import quiescence as _q


#: Idle backoff engages at this many CONSECUTIVE idle replies (operator spec: "two").
IDLE_STREAK_MIN = 2

#: near-finish band (this band marks work nearing completion, not a slide). At or above this reading NO stall backoff applies (neither the
#: consecutive-fire factor nor the idle-reply streak), and once the agent has
#: answered a nudge 'idle' the nudge switches to the near-finish text. The
#: measured deadlock it closes: an idle agent at 86-87% never reaches the 90%
#: alert (it consumes nothing) while the backoff stretched the nudge toward
#: 60 min, so the session parked short of the finish line. ``None`` disables.
NEAR_FINISH_THRESHOLD = 75.0

#: The near-finish nudge. ``{pct}`` = the latest reading; ``{corpus}`` /
#: ``{prefix}`` = the agent's personality corpus and id prefix when the harness
#: knows them (generic placeholders otherwise; the agent knows its own).
DEFAULT_NEAR_FINISH_TEXT = (
    "[Gnomon] You're at {pct:.0f}% context and idle. This is the point to finish strongly, not to "
    "wind down: (a) take a personal-priorities item that fits the remaining context: "
    "lts_find(corpus=\"{corpus}\", prefix=\"{prefix}-WISH\", state=\"open\"), or (b) prepare "
    "the handover and compact now (request_compact: from 85% no confirmation is needed; "
    "below that the first call asks 'Are you sure?' and a second call with a logged "
    "'reason' proceeds). Reply with what you chose."
)
DEFAULT_WISH_CORPUS = "<your>-personality"
DEFAULT_WISH_PREFIX = "<P>"


class NoticeKind(enum.Enum):
    """The four notice kinds the monitor can emit."""

    FYI = "fyi"
    COMPACT_GUIDANCE = "compact-guidance"
    HEARTBEAT = "heartbeat"
    CONTEXT_REPORT = "context-report"


@dataclass(frozen=True)
class Notice:
    """A notice the monitor emits. ``threshold`` is ``None`` for non-threshold
    kinds (heartbeat, context-report)."""

    kind: NoticeKind
    text: str
    used_percentage: Optional[float] = None
    threshold: Optional[int] = None
    monotonic: float = field(default_factory=time.monotonic)


# Default text for each threshold's notice. Kept terse; the compact-guidance
# text is the "pencils-down" checklist the agent acts on at 90%.
_DEFAULT_COMPACT_GUIDANCE_TEXT = (
    "[Gnomon] Context at {pct:.0f}% (>= {threshold}%). Pencils down: "
    "dispatch the transcript-review agent and WAIT for it; act on findings; "
    "update HANDOVER + COMPACT-DIRECTIVE; then self-call request_compact (this alert is a "
    "nudge, not a gate: below 85% the first call asks 'Are you sure?' and a second call "
    "with a logged 'reason' proceeds). The harness will not compact for you."
)
_DEFAULT_FYI_TEXT = (
    "[Gnomon] FYI: context at {pct:.0f}% (>= {threshold}%). Hard "
    "backstop. Still no forced compaction; finish and self-compact."
)
_DEFAULT_HEARTBEAT_TEXT = "[Gnomon] heartbeat: still here — quiescence nudge."
_DEFAULT_CONTEXT_REPORT_TEXT = "[Gnomon] context report: {pct}"


def _default_kind_map() -> Dict[int, NoticeKind]:
    return {90: NoticeKind.COMPACT_GUIDANCE, 95: NoticeKind.FYI}


class ContextMonitor:
    """Threshold-hysteresis context monitor with prime-on-first-read.

    Parameters
    ----------
    read_context:
        Injected reader returning current usage percent (0-100) or ``None``.
    thresholds:
        Ascending thresholds to watch. Default 90/95.
    kind_map:
        threshold -> :class:`NoticeKind`. Default ``{90: compact-guidance,
        95: FYI}`` (see module docstring's audit flag).
    on_notice:
        Sink for every emitted :class:`Notice` (threshold notices, heartbeats,
        context reports). The proxy types these into the child.
    on_report:
        Sink for ``used_percentage`` on every poll that yields a reading — the
        "report up to the GUI" side. Called even when no threshold fires.
    rearm_margin:
        A threshold re-arms once the reading falls below ``threshold -
        rearm_margin``. Default 0 (re-arm as soon as the reading drops below the
        threshold). A positive margin suppresses boundary flapping.
    heartbeat_interval:
        Seconds of child quiescence before a heartbeat may fire. ``None``
        disables heartbeats.
    operator_idle_interval:
        PTY-hooks WS-C (2026-09-27) — setting this turns the heartbeat into the
        DUAL-IDLE STALL TRIGGER: it fires only when the child has been quiet for
        ``heartbeat_interval`` AND the operator has typed nothing for
        ``operator_idle_interval`` (never nudge while the operator is
        mid-thought). ``None`` (default) keeps the legacy agent-only heartbeat.
    heartbeat_text:
        The injected keep-alive text (default: the legacy heartbeat line).
    stall_escalate_after:
        One-shot-then-escalate: if the idle episode that fired is STILL idle this
        many seconds after the fire, ``on_escalate`` is called ONCE (no second
        inject — the "no blind retry" discipline). ``None`` disables.
    on_escalate:
        Sink for the escalation (a dict: ``{"kind": "stall-escalation", ...}``).
    rearm_grace:
        Child output within this many seconds AFTER a fire does not re-arm (it is
        the echo of our own inject, not new work). Default 0 = legacy.
    stall_backoff_max:
        Consecutive fires with no operator activity between them multiply the
        agent-idle threshold by ``2**n``, capped at this factor (anti-spam
        across episodes). Reset by any operator activity. Stall mode only.
    post_compact_interval:
        After :meth:`note_compact`, the NEXT episode uses this (shorter) agent-
        idle threshold — the post-compact stall is a special case of dual-idle.
    require_ready:
        Stall mode only: do not fire until the monitor has PRIMED (a real context
        reading exists => the child's main REPL is up). Measured 2026-09-27 on a
        live launch: an un-gated resume inject landed on Claude Code's startup
        folder-trust dialog and its CR selected the default "No, exit". A
        keep-alive must never answer a pre-REPL dialog. Default False (legacy).
    line_state:
        2026-09-29 (stall-hold): the operator tap's tri-state line model
        (``empty``/``partial``/``unknown``). When wired, the heartbeat/stall
        nudge is HELD by the shared :func:`~gnomon.harness.line_hold.
        line_hold_reason` rule: never on a ``partial`` line; on an ``unknown``
        line only after ``unknown_line_after`` seconds of operator silence. The
        episode stays ARMED while held (it fires once the line is empty and the
        dual-idle conditions still hold). ``None`` (default) = legacy behaviour.
        Observed live 2026-09-29 ~05:40Z: the nudge was typed onto the end of the
        the operator's half-written line.
    on_hold:
        Sink called ONCE per held episode with ``{"kind": "stall-held", "reason"}``.
    heartbeat_text_fn:
        2026-09-29 (cross-harness operator activity): called AT FIRE TIME — after
        the dual-idle and line-hold checks pass — with the local operator-idle
        seconds; returns the nudge text, or ``None`` to use ``heartbeat_text``. A
        raising composer also falls back (it can never block the nudge).
    idle_reply_since / wall_clock / idle_backoff_cap:
        Idle backoff. ``idle_reply_since(fire_epoch)`` says whether the agent's
        last reply since that nudge declared 'idle' (True), did not (False) or is
        unknown (None). Evaluated ONCE per episode, when the child has been quiet
        for the un-backed-off threshold. After TWO consecutive idle replies with
        no operator input between, the agent-idle threshold becomes
        ``base * 2**(streak-1)`` (capped at ``idle_backoff_cap``) when that exceeds
        the existing consecutive-fire backoff. Reset by operator activity here or
        :meth:`note_presence_change`; a non-idle/unknown reply breaks the streak.
        Fix pass (audit MEDIUM): ANY agent output after an evaluated reply that
        is not itself an idle reply (checked once, when the child next goes
        quiet for the base threshold) also resets the streak — a stuck agent
        woken by e.g. a DSD answer must not inherit the capped backoff.
        Output with no readable reply text counts as substantive (fail toward
        nudging).
    near_finish_threshold / near_finish_text / wish_corpus / wish_prefix:
 (stall mode only). While the latest reading is at or above
        ``near_finish_threshold`` (default 75; ``None`` disables): NO backoff,
        every episode waits only the base agent-idle threshold, and once the
        agent has answered a nudge with 'idle' (``idle_streak >= 1``) the nudge
        text becomes ``near_finish_text`` (formatted with pct/corpus/prefix),
        overriding the static and composed text. Not applied during the first
        episode after a compaction (the reading may still be the pre-compact one,
        and that nudge is the resume check). Quiescence and the line hold still
        apply unchanged.
    quiescence_enabled / quiescent_max_seconds / agent_turn_since / on_quiescent /
    quiescent_check_interval / quiescent_backstop_seconds:
 (see ``harness/quiescence.py``). ``quiescence_enabled=False``
        (default) = :meth:`enter_quiescent` is refused ``disabled`` and nothing
        else changes. While quiescent the heartbeat/stall nudge and its
        escalation are suppressed; :meth:`poll` (the 90/95 threshold notices) is
        untouched. ``agent_turn_since(since_epoch)`` -> True/False/None reads the
        agent's transcript for a new turn (consulted after child output, at most
        every ``quiescent_check_interval`` s, and every
        ``quiescent_backstop_seconds`` regardless). ``on_quiescent`` receives every
        enter / suppressed / exit / refused record.
    idle_snooze:
 (see ``harness/idle_snooze.py``). While its snooze is active or idle
        alerts are dismissed, :meth:`maybe_heartbeat` returns ``None`` (no nudge, no
        escalation); :meth:`poll` (the 90/95 threshold notices) is untouched. When a
        dismissal clears, the idle interval is RESTARTED (no immediate nudge).
    """

    def __init__(
        self,
        read_context: Callable[[], Optional[float]],
        thresholds: Sequence[int] = HOSTED_CHILD_THRESHOLDS,
        kind_map: Optional[Dict[int, NoticeKind]] = None,
        on_notice: Optional[Callable[[Notice], None]] = None,
        on_report: Optional[Callable[[float], None]] = None,
        rearm_margin: float = 0.0,
        heartbeat_interval: Optional[float] = None,
        *,
        operator_idle_interval: Optional[float] = None,
        heartbeat_text: Optional[str] = None,
        stall_escalate_after: Optional[float] = None,
        on_escalate: Optional[Callable[[Dict[str, Any]], None]] = None,
        rearm_grace: float = 0.0,
        stall_backoff_max: int = 8,
        post_compact_interval: Optional[float] = None,
        require_ready: bool = False,
        line_state: Optional[Callable[[], str]] = None,
        unknown_line_after: float = DEFAULT_UNKNOWN_AFTER_SECONDS,
        on_hold: Optional[Callable[[Dict[str, Any]], None]] = None,
        heartbeat_text_fn: Optional[Callable[[Optional[float]], Optional[str]]] = None,
        idle_reply_since: Optional[Callable[[float], Optional[bool]]] = None,
        idle_backoff_cap: float = 3600.0,
        wall_clock: Callable[[], float] = time.time,
        quiescence_enabled: bool = False,
        quiescent_max_seconds: float = _q.DEFAULT_QUIESCENT_MAX_SECONDS,
        agent_turn_since: Optional[Callable[[float], Optional[bool]]] = None,
        on_quiescent: Optional[Callable[[Dict[str, Any]], None]] = None,
        quiescent_check_interval: float = _q.DEFAULT_CHECK_INTERVAL,
        quiescent_backstop_seconds: float = _q.DEFAULT_BACKSTOP_SECONDS,
        near_finish_threshold: Optional[float] = NEAR_FINISH_THRESHOLD,
        near_finish_text: Optional[str] = None,
        wish_corpus: Optional[str] = None,
        wish_prefix: Optional[str] = None,
        idle_snooze: Optional[Any] = None,
        work_list_mode: bool = False,
    ) -> None:
        self._read_context = read_context
        self._thresholds: List[int] = sorted(thresholds)
        self._kind_map = dict(kind_map) if kind_map is not None else _default_kind_map()
        self._on_notice = on_notice
        self._on_report = on_report
        self._rearm_margin = rearm_margin
        self._heartbeat_interval = heartbeat_interval

        self._armed: Dict[int, bool] = {t: True for t in self._thresholds}
        self._primed = False
        self._last_reading: Optional[float] = None

        self._last_activity: Optional[float] = None
        self._heartbeat_armed = True

        # WS-C dual-idle stall trigger state (inert unless operator_idle_interval set).
        self._operator_idle_interval = operator_idle_interval
        self._heartbeat_text = heartbeat_text or _DEFAULT_HEARTBEAT_TEXT
        self._stall_escalate_after = stall_escalate_after
        self._on_escalate = on_escalate
        self._rearm_grace = float(rearm_grace or 0.0)
        self._stall_backoff_max = max(1, int(stall_backoff_max))
        self._post_compact_interval = post_compact_interval
        self._require_ready = bool(require_ready)
        self._last_operator_activity: Optional[float] = None
        self._fired_at: Optional[float] = None
        self._escalated = False
        self._consecutive_fires = 0
        self._post_compact = False
        #: the post-compact boot prompt is owed (armed, not yet typed).
        #: While set, the post-compact resume nudge is suppressed (no double prompt).
        self._boot_pending = False
        self.stall_fires = 0
        self.stall_escalations = 0
        # 2026-09-29 stall-hold (operator line state).
        self._line_state = line_state
        self._unknown_line_after = max(0.0, float(unknown_line_after))
        #: master_write_nonblocking (default OFF): ``() -> reason | None``, the writer's START HOLD (no harness injection before the
        #: child's first output). ``None`` (always, with the flag OFF) = ``line_hold_reason`` is exactly the old rule.
        self.startup_hold: Optional[Callable[[], Optional[str]]] = None
        self._on_hold = on_hold
        self._hold_reported = False
        self.stall_holds = 0
        # 2026-09-29 cross-harness operator activity + idle backoff.
        self._heartbeat_text_fn = heartbeat_text_fn
        self._idle_reply_since = idle_reply_since
        self._idle_backoff_cap = max(0.0, float(idle_backoff_cap))
        self._wall_clock = wall_clock
        self._fired_wall: Optional[float] = None
        self._reply_pending = False
        self.idle_streak = 0
        self._eval_wall: Optional[float] = None
        self._output_since_eval = False
        # quiescent state (inert unless quiescence_enabled).
        self.quiescence_enabled = bool(quiescence_enabled)
        self._q_max = max(0.0, float(quiescent_max_seconds))
        self._agent_turn_since = agent_turn_since
        self._on_quiescent = on_quiescent
        self._q_check_interval = max(0.0, float(quiescent_check_interval))
        self._q_backstop = max(0.0, float(quiescent_backstop_seconds))
        self._quiescent: Optional[Dict[str, Any]] = None
        self._q_output_seen = False
        self._q_last_check: Optional[float] = None
        self._q_suppress_reported = False
        self._valve_nudge_due = False
        self.quiescent_suppressed = 0
        # near-finish band.
        self._near_finish_threshold = (None if near_finish_threshold is None
                                       else float(near_finish_threshold))
        self._near_finish_text = near_finish_text or DEFAULT_NEAR_FINISH_TEXT
        self._wish_corpus = wish_corpus or DEFAULT_WISH_CORPUS
        self._wish_prefix = wish_prefix or DEFAULT_WISH_PREFIX
        self.near_finish_fires = 0
        #: the idle-nudge snooze (``harness/idle_snooze.IdleSnooze``) or None.
        #: Consulted ONLY by :meth:`maybe_heartbeat`; None = byte-identical to before.
        self.idle_snooze = idle_snooze
        #: (open_work_list ON at launch): the recurring idle / stall / "operator active elsewhere" nudge is RETIRED.
        #: Only the ONE post-compact resume check survives (rule-kept); operator activity no longer gates it. False = unchanged.
        self.work_list_mode = bool(work_list_mode)

    def seconds_quiet(self, now: Optional[float] = None) -> Optional[float]:
        """seconds since the child last produced output (None = none seen yet). Read-only."""
        if self._last_activity is None:
            return None
        now = time.monotonic() if now is None else now
        return max(0.0, now - self._last_activity)

    # -- wiring --------------------------------------------------------------

    def set_line_state(self, line_state: Optional[Callable[[], str]]) -> None:
        """Wire (or replace) the operator line-state source after construction."""
        self._line_state = line_state

    def line_hold_reason(self, now: Optional[float] = None) -> Optional[str]:
        """Why a harness-originated injection must wait for the operator's line
        right now (shared rule, measured on THIS monitor's operator clock), or
        ``None``. No line source wired = never held (apart from the start hold, when one is wired)."""
        sh = self.startup_hold
        if sh is not None:
            try:
                why = sh()
            except Exception:  # noqa: BLE001 - the start hold can only ever delay; it never wedges the monitor
                why = None
            if why:
                return why
        if self._line_state is None:
            return None
        now = time.monotonic() if now is None else now
        try:
            state = self._line_state()
        except Exception:  # noqa: BLE001 - an unreadable line is an unknown line
            state = "unknown"
        if self._last_operator_activity is None:
            # silence is measured from when we first looked
            self._last_operator_activity = now
        return line_hold_reason(state, now - self._last_operator_activity, self._unknown_line_after)

    def set_on_notice(self, on_notice: Optional[Callable[[Notice], None]]) -> None:
        """Set (or replace) the notice sink after construction.

        Used when the sink is a method of an object built AFTER the monitor —
        e.g. the proxy's ``inject_notice`` (the launcher wires monitor -> proxy
        this way, avoiding a construction-order cycle)."""
        self._on_notice = on_notice

    # -- state introspection -------------------------------------------------

    @property
    def primed(self) -> bool:
        return self._primed

    @property
    def last_reading(self) -> Optional[float]:
        return self._last_reading

    def is_armed(self, threshold: int) -> bool:
        return self._armed.get(threshold, False)

    def kind_for(self, threshold: int) -> NoticeKind:
        return self._kind_map.get(threshold, NoticeKind.FYI)

    # -- reading -------------------------------------------------------------

    def read_now(self) -> Optional[float]:
        """Read current usage without running hysteresis. Updates last_reading."""
        reading = self._read_context()
        if reading is not None:
            self._last_reading = reading
        return reading

    def poll(self) -> List[Notice]:
        """Read context and run hysteresis. Returns notices fired this poll.

        On the FIRST successful read the monitor *primes*: it sets each
        threshold's armed state from the current reading (armed iff below the
        threshold) and fires NOTHING — a resumed high session must not fire
        retroactively.
        """
        reading = self._read_context()
        if reading is None:
            return []
        self._last_reading = reading

        if self._on_report is not None:
            self._on_report(reading)

        if not self._primed:
            for t in self._thresholds:
                self._armed[t] = reading < t
            self._primed = True
            return []

        fired: List[Notice] = []
        for t in self._thresholds:
            if reading < (t - self._rearm_margin):
                # dropped below (with margin) -> re-arm for the next climb
                self._armed[t] = True
            elif reading >= t and self._armed.get(t, False):
                notice = self._make_threshold_notice(t, reading)
                self._armed[t] = False  # spend
                fired.append(notice)
                self._emit(notice)
        return fired

    # -- heartbeat -----------------------------------------------------------

    @property
    def stall_mode(self) -> bool:
        """True when the heartbeat is the WS-C dual-idle stall trigger."""
        return self._operator_idle_interval is not None

    def note_activity(self, now: Optional[float] = None) -> None:
        """Record child output activity; re-arms the heartbeat.

        WS-C: output within ``rearm_grace`` of our own fire is the echo of the
        inject, not new work — it updates the clock but does NOT re-arm."""
        now = time.monotonic() if now is None else now
        self._last_activity = now
        if self._quiescent is not None:
            self._q_output_seen = True  # look for an agent turn
        if (self.stall_mode and self.idle_streak > 0 and not self._reply_pending
                and self._eval_wall is not None):
            # output AFTER the evaluated reply: a candidate substantive turn
            self._output_since_eval = True
        if (not self._heartbeat_armed and self._fired_at is not None
                and now - self._fired_at < self._rearm_grace):
            return
        self._heartbeat_armed = True
        self._fired_at = None
        self._escalated = False
        self._hold_reported = False

    def note_operator_activity(self, now: Optional[float] = None) -> None:
        """WS-B/WS-C: the operator typed. Re-arms, resets backoff + post-compact."""
        now = time.monotonic() if now is None else now
        self._last_operator_activity = now
        if self.work_list_mode:
            return  # the operator typing changes nothing about whether the agent is alerted (rule-operator-activity-irrelevant)
        self._heartbeat_armed = True
        self._fired_at = None
        self._escalated = False
        self._consecutive_fires = 0
        self._post_compact = False
        self._hold_reported = False
        self.idle_streak = 0
        self._reply_pending = False
        self._output_since_eval = False

    def note_presence_change(self) -> None:
        """Presence-sensor flip (present<->absent): resets the idle backoff."""
        self.idle_streak = 0
        self._reply_pending = False
        self._output_since_eval = False

    @property
    def near_finish_active(self) -> bool:
        """stall mode, the band is enabled, the latest reading is at or
        above it, and this is not the first episode after a compaction."""
        if not self.stall_mode or self._near_finish_threshold is None or self._post_compact:
            return False
        reading = self._last_reading
        return reading is not None and reading >= self._near_finish_threshold

    def _near_finish_nudge_text(self) -> str:
        try:
            return self._near_finish_text.format(pct=float(self._last_reading or 0.0),
                                                 corpus=self._wish_corpus,
                                                 prefix=self._wish_prefix)
        except (KeyError, IndexError, ValueError):
            return self._near_finish_text  # a custom text with other placeholders

    @property
    def idle_backoff_active(self) -> bool:
        """True once ``IDLE_STREAK_MIN`` consecutive idle replies have been seen."""
        return self.idle_streak >= IDLE_STREAK_MIN

    def set_heartbeat_text_fn(self, fn: Optional[Callable[[Optional[float]], Optional[str]]]) -> None:
        self._heartbeat_text_fn = fn

    def note_compact(self) -> None:
        """WS-E/WS-C: a compaction completed (native SessionStart(compact) hook).
        The next idle episode uses ``post_compact_interval`` if configured.
 a compaction also ends quiescence and any idle snooze/dismissal (fail toward nudging — the
        post-compact resume check is the one that matters most)."""
        self.exit_quiescent(_q.TRIGGER_COMPACT)
        if self.idle_snooze is not None:
            try:  # a compaction ends a snooze/dismissal too (architect ruling, audit F8)
                self.idle_snooze.on_compact()
            except Exception:  # noqa: BLE001 - never take the pump down
                pass
            else:
                if self.idle_snooze.consume_restart():
                    self.restart_idle_interval()
        self._post_compact = True
        self._heartbeat_armed = True
        self._fired_at = None
        self._escalated = False
        self._hold_reported = False

    def set_boot_pending(self, pending: bool) -> None:
        """the harness owes the post-compact boot prompt. While pending,
        :meth:`maybe_heartbeat` fires nothing (the boot prompt REPLACES the resume
        nudge for this compaction). Cleared when the boot prompt is written, denied
        or dropped stale — a failed boot never leaves the session un-nudged."""
        self._boot_pending = bool(pending)

    def consume_post_compact(self) -> None:
        """the boot prompt was written for this compaction, so the
        post-compact SHORT-threshold resume state is spent: the next idle episode
        uses the ordinary interval instead of a second post-compact prompt."""
        self._post_compact = False

    def set_stall_enabled(self, enabled: bool) -> None:
        """Toggle the heartbeat/stall trigger at runtime (``None`` interval = off)."""
        self._stall_enabled = bool(enabled)

    def _base_threshold(self) -> float:
        base = float(self._heartbeat_interval)  # type: ignore[arg-type]
        if self.stall_mode and self._post_compact and self._post_compact_interval is not None:
            base = min(base, float(self._post_compact_interval))
        return base

    def _agent_threshold(self) -> float:
        if self._valve_nudge_due:
            return 0.0  # the valve's ONE nudge is due now
        base = self._base_threshold()
        if not self.stall_mode:
            return base
        if self.near_finish_active:
            return base  # no backoff of either kind in the near-finish band
        factor = min(2 ** self._consecutive_fires, self._stall_backoff_max)
        threshold = base * factor
        if self.idle_backoff_active:
            idle = min(base * 2 ** (self.idle_streak - 1), self._idle_backoff_cap)
            threshold = max(threshold, idle)
        return threshold

    def _evaluate_reply(self) -> None:
        """Once per episode: did the agent answer the last nudge with 'idle'?"""
        self._reply_pending = False
        if self._idle_reply_since is None or self._fired_wall is None:
            return
        try:
            verdict = self._idle_reply_since(self._fired_wall)
        except Exception:  # noqa: BLE001 - unknown breaks the streak
            verdict = None
        self.idle_streak = self.idle_streak + 1 if verdict is True else 0
        self._mark_evaluated()

    def _mark_evaluated(self) -> None:
        try:
            self._eval_wall = float(self._wall_clock())
        except Exception:  # noqa: BLE001
            self._eval_wall = None
        self._output_since_eval = False

    def _check_substantive(self) -> None:
        """Agent output arrived after the evaluated reply: keep the streak only if
        that output was itself an idle reply; otherwise (work, or unreadable)
        reset it."""
        verdict: Optional[bool] = None
        if self._idle_reply_since is not None and self._eval_wall is not None:
            try:
                verdict = self._idle_reply_since(self._eval_wall)
            except Exception:  # noqa: BLE001
                verdict = None
        if verdict is not True:
            self.idle_streak = 0
        self._mark_evaluated()

    def maybe_heartbeat(self, now: Optional[float] = None) -> Optional[Notice]:
        """Emit a heartbeat if the child has been quiet past the interval.

        Fires at most once per quiet stretch (re-armed by :meth:`note_activity`).

        WS-C (stall mode, ``operator_idle_interval`` set): fires only when BOTH
        the child AND the operator are idle past their thresholds; ONE inject per
        idle episode, then (if still idle ``stall_escalate_after`` later) ONE
        escalation via ``on_escalate`` — never a second inject.
        """
        if self._heartbeat_interval is None or not getattr(self, "_stall_enabled", True):
            return None
        if self._boot_pending:
            return None  # the post-compact boot prompt replaces the resume nudge
        if self.work_list_mode and not self._post_compact:
            return None  # no recurring idle/stall nudge; list reminders are the work ticker's
        if self.stall_mode and self._require_ready and not self._primed:
            return None  # the child's REPL is not up yet — never answer a startup dialog
        now = time.monotonic() if now is None else now
        snooze_hold = False
        if self.idle_snooze is not None:
            # evaluated FIRST (it polls its clear conditions) but never short-circuits
            # the quiescent tick below, so both states keep tracking their own exits.
            snooze_hold = self.idle_snooze.suppressing()
            if self.idle_snooze.consume_restart():
                self.restart_idle_interval(now)  # a dismissal just cleared: no immediate nudge
        if self._quiescent is not None and self._quiescent_tick(now):
            return None  # quiescent — no nudge, no escalation
        if snooze_hold:
            return None  # snoozed / dismissed — no nudge, no escalation
        if not self._heartbeat_armed:
            self._maybe_escalate(now)
            return None
        if self._last_activity is None:
            self._last_activity = now
            return None
        if self.stall_mode and self._last_operator_activity is None:
            self._last_operator_activity = now
            return None
        if self.stall_mode and now - self._last_activity >= self._base_threshold():
            if self._reply_pending:
                self._evaluate_reply()
            elif self._output_since_eval:
                self._check_substantive()
        if now - self._last_activity < self._agent_threshold():
            return None
        if (self.stall_mode and not self.work_list_mode
                and now - self._last_operator_activity < float(self._operator_idle_interval)):  # type: ignore[operator]
            return None  # (operator activity elsewhere no longer decides whether the resume check fires)
        # 2026-09-29 stall-hold: dual-idle is met, but the operator's line is not
        # empty (or unknown and not yet silent long enough) — stay ARMED, type
        # nothing. Report once per held episode.
        reason = self.line_hold_reason(now)
        if reason is not None:
            if not self._hold_reported:
                self._hold_reported = True
                self.stall_holds += 1
                if self._on_hold is not None:
                    try:
                        self._on_hold({"kind": "stall-held", "reason": reason})
                    except Exception:  # noqa: BLE001 - never take the pump down
                        pass
            return None
        text = self._heartbeat_text
        if self._heartbeat_text_fn is not None:
            local_idle = (now - self._last_operator_activity
                          if self._last_operator_activity is not None else None)
            try:
                composed = self._heartbeat_text_fn(local_idle)
            except Exception:  # noqa: BLE001 - a composer fault never blocks the nudge
                composed = None
            if isinstance(composed, str) and composed.strip():
                text = composed
        near_finish = self.near_finish_active and self.idle_streak >= 1
        if near_finish:
            text = self._near_finish_nudge_text()  # an idle reply in the band
        notice = Notice(kind=NoticeKind.HEARTBEAT, text=text,
                        used_percentage=self._last_reading if near_finish else None)
        self._heartbeat_armed = False
        self._valve_nudge_due = False
        if self.stall_mode:
            try:
                self._fired_wall = float(self._wall_clock())
            except Exception:  # noqa: BLE001
                self._fired_wall = None
            self._reply_pending = self._fired_wall is not None
            self._fired_at = now
            self._escalated = False
            self._consecutive_fires += 1
            self._post_compact = False
            self.stall_fires += 1
            if near_finish:
                self.near_finish_fires += 1
        if self.work_list_mode:
            self._post_compact = False   # the ONE post-compact resume check is spent whether or not stall_mode is on
        self._emit(notice)
        return notice

    @property
    def nudges_enabled(self) -> bool:
        """the stall/resume nudge exists for this session (an interval is set and
        the trigger is not switched off) - otherwise there is nothing to snooze."""
        return self._heartbeat_interval is not None and getattr(self, "_stall_enabled", True)

    def restart_idle_interval(self, now: Optional[float] = None) -> None:
        """begin a fresh idle episode from ``now`` (idle clock reset, backoff and
        the idle-reply streak cleared, nothing owed). Used when a dismissal clears so that
        normal nudging resumes WITHOUT an immediate nudge."""
        now = time.monotonic() if now is None else now
        self._last_activity = now
        self._heartbeat_armed = True
        self._fired_at = None
        self._escalated = False
        self._hold_reported = False
        self._consecutive_fires = 0
        self.idle_streak = 0
        self._reply_pending = False
        self._output_since_eval = False
        self._valve_nudge_due = False

    # -- quiescent state ------------------------------------------------

    @property
    def quiescent_state(self) -> Optional[Dict[str, Any]]:
        """``{"reason", "since_ts", "max_seconds"}`` while quiescent, else ``None``."""
        q = self._quiescent
        if q is None:
            return None
        return {"reason": q["reason"], "since_ts": q["since_ts"], "max_seconds": q["max_seconds"]}

    def _wall(self) -> Optional[float]:
        try:
            return float(self._wall_clock())
        except Exception:  # noqa: BLE001
            return None

    def _q_record(self, decision: str, **fields: Any) -> None:
        if self._on_quiescent is None:
            return
        try:
            self._on_quiescent({"decision": decision, **fields})
        except Exception:  # noqa: BLE001 - provenance never breaks the pump
            pass

    def enter_quiescent(self, reason: Any, now: Optional[float] = None) -> Dict[str, Any]:
        """The agent declared its work complete (the ``quiesce`` self-verb).

        Refused (``ok: False``) when the feature is off (``disabled``), the reason
        is empty, or the agent's next turn cannot be observed (``refused`` — fail
        toward nudging). Re-entering while quiescent renews it (new reason, the
        valve restarts)."""
        if not self.quiescence_enabled:
            return {"ok": False, "action": "quiesce", "decision": "disabled",
                    "error": f"quiescent state is disabled for this session (feature flag "
                             f"{_q.FLAG_NAME} is off) — stall nudges continue as normal"}
        text = _q.clean_reason(reason)
        if text is None:
            return {"ok": False, "action": "quiesce", "decision": "refused",
                    "error": "quiesce requires a non-empty 'reason' (what is complete)"}
        now = time.monotonic() if now is None else now
        since_ts = self._wall()
        verdict: Optional[bool] = None
        if self._agent_turn_since is not None and since_ts is not None:
            try:
                verdict = self._agent_turn_since(since_ts)
            except Exception:  # noqa: BLE001
                verdict = None
        if verdict is None:
            err = ("quiesce refused: the harness cannot observe your next agent turn (no "
                   "readable transcript), so it would not know to resume nudging — stall "
                   "nudges continue as normal")
            self._q_record("refused", reason=text, error=err)
            return {"ok": False, "action": "quiesce", "decision": "refused", "error": err}
        renewed = self._quiescent is not None
        # the episode clocks start here if nothing has been seen yet (as in
        # maybe_heartbeat's first call), so 'suppressed' is measurable
        if self._last_activity is None:
            self._last_activity = now
        if self.stall_mode and self._last_operator_activity is None:
            self._last_operator_activity = now
        self._quiescent = {"reason": text, "since_ts": since_ts, "since_mono": now,
                           "max_seconds": self._q_max}
        self._q_output_seen = False
        self._q_last_check = now
        self._q_suppress_reported = False
        self._valve_nudge_due = False
        # a pending one-shot escalation belongs to the episode the agent just closed
        self._fired_at = None
        self._escalated = False
        extra = {"renewed": True} if renewed else {}
        self._q_record("enter", reason=text, since_ts=since_ts, max_seconds=self._q_max, **extra)
        return {"ok": True, "action": "quiesce", "quiescent": True, "reason": text,
                "since_ts": since_ts, "max_seconds": self._q_max,
                "note": ("stall nudges are paused until the operator types, a DSD answer or hub "
                         "message arrives, you take another turn, or the safety valve expires; "
                         "context-threshold guidance still reaches you")}

    def exit_quiescent(self, trigger: str, now: Optional[float] = None) -> bool:
        """Leave the quiescent state (no-op, returns False, if not quiescent).

        The stall episode restarts from here: idle clock reset, backoff and the
        idle-reply streak cleared — except on the safety valve, which leaves the
        clock alone and makes ONE nudge due at once."""
        q = self._quiescent
        if q is None:
            return False
        now = time.monotonic() if now is None else now
        self._quiescent = None
        self._q_output_seen = False
        self._heartbeat_armed = True
        self._fired_at = None
        self._escalated = False
        self._hold_reported = False
        self._consecutive_fires = 0
        self.idle_streak = 0
        self._reply_pending = False
        self._output_since_eval = False
        if trigger == _q.TRIGGER_VALVE:
            self._valve_nudge_due = True
        else:
            self._last_activity = now
        self._q_record("exit", reason=q["reason"], since_ts=q["since_ts"], trigger=trigger,
                       duration_seconds=round(max(0.0, self._quiescent_elapsed(q, now)), 3))
        return True

    def _quiescent_elapsed(self, q: Dict[str, Any], now: float) -> float:
        """Seconds quiescent, on the WALL clock (fix pass: ``time.monotonic`` pauses
        while a Mac sleeps, so an 8 h valve could take days). A wall clock that
        stepped BACKWARDS past the entry counts as the valve elapsed (``inf``) —
        fail toward nudging. No wall reading at all falls back to monotonic."""
        wall = self._wall()
        if wall is None or q.get("since_ts") is None:
            return now - q["since_mono"]
        elapsed = wall - float(q["since_ts"])
        return float("inf") if elapsed < 0 else elapsed

    def _quiescent_tick(self, now: float) -> bool:
        """While quiescent: check the valve and the agent-turn signal. Returns
        True while still quiescent (the caller then suppresses the nudge)."""
        q = self._quiescent
        assert q is not None
        if self._quiescent_elapsed(q, now) >= q["max_seconds"]:
            self.exit_quiescent(_q.TRIGGER_VALVE, now)
            return False
        last = self._q_last_check if self._q_last_check is not None else q["since_mono"]
        due = ((self._q_output_seen and now - last >= self._q_check_interval)
               or now - last >= self._q_backstop)
        if due:
            self._q_last_check = now
            self._q_output_seen = False
            verdict: Optional[bool] = None
            if self._agent_turn_since is not None and q["since_ts"] is not None:
                try:
                    verdict = self._agent_turn_since(q["since_ts"])
                except Exception:  # noqa: BLE001
                    verdict = None
            if verdict is True:
                self.exit_quiescent(_q.TRIGGER_AGENT_TURN, now)
                return False
            if verdict is None:
                self.exit_quiescent(_q.TRIGGER_UNVERIFIABLE, now)
                return False
        if not self._q_suppress_reported and self._would_fire(now):
            self._q_suppress_reported = True
            self.quiescent_suppressed += 1
            self._q_record("suppressed", reason=q["reason"], since_ts=q["since_ts"])
        return True

    def _would_fire(self, now: float) -> bool:
        """Dual-idle met (ignoring the line hold) — a nudge would be due now."""
        if self._last_activity is None or now - self._last_activity < self._agent_threshold():
            return False
        if self.stall_mode and self._last_operator_activity is not None:
            return now - self._last_operator_activity >= float(self._operator_idle_interval)  # type: ignore[arg-type]
        return True

    def _maybe_escalate(self, now: float) -> None:
        if (not self.stall_mode or self._stall_escalate_after is None or self._escalated
                or self._fired_at is None):
            return
        if now - self._fired_at >= self._stall_escalate_after:
            self._escalated = True
            self.stall_escalations += 1
            if self._on_escalate is not None:
                try:  # a sink failure must never take the pump down
                    self._on_escalate({
                        "kind": "stall-escalation",
                        "idle_since_fire_seconds": now - self._fired_at,
                        "consecutive_fires": self._consecutive_fires,
                    })
                except Exception:  # noqa: BLE001
                    pass

    # -- context report (answer to an on-demand query) -----------------------

    def context_report(self) -> Notice:
        """Build (and emit) a context-report notice with the current reading."""
        reading = self.read_now()
        pct = "unknown" if reading is None else f"{reading:.0f}%"
        notice = Notice(
            kind=NoticeKind.CONTEXT_REPORT,
            text=_DEFAULT_CONTEXT_REPORT_TEXT.format(pct=pct),
            used_percentage=reading,
        )
        self._emit(notice)
        return notice

    # -- internals -----------------------------------------------------------

    def _make_threshold_notice(self, threshold: int, reading: float) -> Notice:
        kind = self.kind_for(threshold)
        if kind is NoticeKind.COMPACT_GUIDANCE:
            text = _DEFAULT_COMPACT_GUIDANCE_TEXT.format(pct=reading, threshold=threshold)
        else:
            text = _DEFAULT_FYI_TEXT.format(pct=reading, threshold=threshold)
        return Notice(
            kind=kind, text=text, used_percentage=reading, threshold=threshold
        )

    def _emit(self, notice: Notice) -> None:
        # 2026-10-03: stamp the "[Gnomon]" tag at the moment the notice is emitted
        # (== composed; any later hold keeps this text). Custom text without the tag is untouched, and a
        # CONTEXT_REPORT (answered over the control channel, never typed) is left as it was.
        stamped = notice.text if notice.kind is NoticeKind.CONTEXT_REPORT else stamp_gm_tag(notice.text)
        if stamped != notice.text:
            notice = dataclasses.replace(notice, text=stamped)
        if self._on_notice is not None:
            self._on_notice(notice)

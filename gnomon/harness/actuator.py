"""The actuation vocabulary (hub->agent; §4.6 tool surface).

The harness RECEIVES a small, fixed vocabulary from the agent over the control
channel and types it into the child. It does NOT invent these — it relays them:

* ``compact(direction)`` — relay the agent's self-directed ``/compact``.
 ⛔ NEVER a harness decision (/ D23).
* ``inject(text)``      — the general "type as if human" primitive; how
  ``/clear``, ``/prepare-for-compact`` etc. are sent.
* ``context()``         — an on-demand query; answered with a context-report.

:meth:`Actuator.dispatch` maps a decoded command dict to one of these and
returns a small response dict. The transport (a control fd here; the localhost
MCP/HTTP rendezvous in a later slice) is deliberately not this class's concern —
that is the seam that lets the rendezvous replace the fd without touching the
vocabulary.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Dict, Optional

from .hooks import (
    HUB_CLAIMABLE_SOURCES,
    SOURCE_BUS,
    SOURCE_DSD_ANSWER,
    SOURCE_OPERATOR_BROADCAST,
    SOURCE_SELF_VERB,
    SOURCE_UNATTRIBUTED,
    Decision,
    Outcome,
)
from .injector import Injector
from .monitor import ContextMonitor

#: Control-path origins (PTY-hooks WS-A). The transport that delivered a command
#: determines its provenance ``source`` — a frame cannot choose its own origin.
ORIGIN_HUB = "hub"    # the hub rendezvous socket (operator tools, bus, fan-out)
ORIGIN_SELF = "self"  # the child's own local control socket (self_mcp, native hooks)

#: Lifecycle events a native Claude hook may report over the SELF socket (WS-E).
LIFECYCLE_EVENTS = frozenset({"pre-compact", "post-compact", "post-compact-started"})



#: The wire field of a ``compact`` command that carries the operator's override approval. ONE named spelling so a
#: caller (and a test) refers to it by this constant; the neutral rename (S5-x) is then a one-line change.
COMPACT_APPROVAL_FIELD = "ch_approved"
#: g112: the `context` verb's `operator` block says what its numbers measure. ONE added key; no existing key, name, type or value changes.
OPERATOR_COUNTS_KEY = "counts"
OPERATOR_COUNTS_TEXT = "console typing only; messages sent through Claude Code's Remote Control are not counted"

class ActuationError(Exception):
    """Raised for a malformed or unknown actuation command."""


class Actuator:
    """Routes received control commands to the injector / monitor.

    ``monitor`` is optional: a harness may run without a context monitor, in
    which case a ``context`` query reports ``None``.
    """

    #: The complete, closed ACTUATION vocabulary. Anything else is refused.
    VOCABULARY = ("inject", "compact", "context")

    #: PTY-hooks internal verbs, each accepted ONLY from one origin: ``lifecycle``
    #: (a native Claude PreCompact/SessionStart hook reporting over the SELF
    #: socket, WS-E) and ``operator_feedback`` (the hub's answer to a ``\all`` /
    #: ``\<id>`` fan-out, WS-F). Neither types anything into the child.
    #: WS-G adds the launch-ledger verbs (``launch_context`` / ``_reserve`` /
    #: ``_commit`` / ``_release``) — SELF only: they are how an agent's
    #: ``agent_launch`` call reads + reserves against its OWN proxy's rails.
    INTERNAL_VOCABULARY = {
        "lifecycle": ORIGIN_SELF,
        "operator_feedback": ORIGIN_HUB,
        "launch_context": ORIGIN_SELF,
        "launch_reserve": ORIGIN_SELF,
        "launch_commit": ORIGIN_SELF,
        "launch_release": ORIGIN_SELF,
        # (2026-09-29): a ``\\command`` line
        # recognized by the harness's Claude Code UserPromptSubmit hook, run
        # through the SAME command registry the byte-level tap uses.
        "operator_command": ORIGIN_SELF,
        # D15 fix pass: the hub's greeting after a register (resets the reconnect
        # backoff once the link is stable) and its notice that ANOTHER harness
        # registered this instance_id. HUB only.
        "hub_hello": ORIGIN_HUB,
        "superseded": ORIGIN_HUB,
        "vacated": ORIGIN_HUB,   # fix pass 2: the holder left — re-register
        # 2026-09-29 (line-hold audit note 2): the PostToolUse hook claims the
        # threshold notices held for the operator's line. SELF only.
        "held_notices": ORIGIN_SELF,
        # (2026-09-29): the agent declares its work complete -> the
        # monitor's quiescent state (no stall nudges). SELF only: no hub, bus or
        # other agent can silence a session's stall guard. Types nothing.
        "quiesce": ORIGIN_SELF,
        # (2026-10-03): the idle-nudge snooze. SELF only, acts on this session's
        # monitor only; types nothing. Flag idle_snooze off = refused ``disabled``.
        "idle_snooze": ORIGIN_SELF,
        # slice 1: the plan-usage read (plan_usage_verb). SELF only, types nothing.
        # Flag off = no reader is bound, so a hand-built frame is refused ``disabled``.
        "plan_usage": ORIGIN_SELF,
        # (open_work_list): the open-work verbs and the declared-status setter. SELF only; they act on THIS
        # session's own list / identity. No handler bound (flag off) = refused ``disabled``.
        "work_add": ORIGIN_SELF, "work_update": ORIGIN_SELF, "work_done": ORIGIN_SELF, "work_drop": ORIGIN_SELF,
        "work_list": ORIGIN_SELF, "operator_status_set": ORIGIN_SELF,
        # F3: the mail verbs ask THEIR OWN proxy for a one-call ticket naming this session (the server
        # authenticates it with the per-registration secret the hub sent here). SELF only; types nothing.
        "mail_ticket": ORIGIN_SELF,
        # (core): the agent's ``alert`` verb. SELF only; raises an alert for the HUMAN (the web alert
        # column + a macOS notification) and types NOTHING into any PTY (g79 does not apply: there is no
        # PTY write). Flag alerts off at launch = no handler is bound, refused ``disabled``.
        "alert": ORIGIN_SELF,
    }

    def __init__(
        self,
        injector: Injector,
        monitor: Optional[ContextMonitor] = None,
        *,
        on_lifecycle: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_feedback: Optional[Callable[[str], None]] = None,
        on_launch: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        compact_confirmer: Optional[Any] = None,
        on_compact_confirmation: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_operator_command: Optional[Callable[[str], Dict[str, Any]]] = None,
        compact_gate: Optional[Any] = None,
        on_dsd_notice: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        on_hub_frame: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_claim_held_notices: Optional[Callable[[], Any]] = None,
        operator_context: Optional[Callable[[], Dict[str, Any]]] = None,
        held_notice_count: Optional[Callable[[], int]] = None,
        plan_usage_reader: Optional[Callable[[], Dict[str, Any]]] = None,
        alert_handler: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        on_feedback_frame: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_broadcast_written: Optional[Callable[[Dict[str, Any], float], None]] = None,
        on_work: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        claim_presence: Optional[Callable[[], Optional[str]]] = None,
        mail_identity: Optional[str] = None,
    ) -> None:
        self._injector = injector
        # F3: this session's instance id, set ONLY when agent_mail is on at launch (None = the
        # ``mail_ticket`` verb is refused ``disabled``), and the per-registration secret the hub sends in hub_hello
        # (memory only: never written, logged, or passed to the hub-frame sink).
        self._mail_identity = mail_identity
        self._hub_session_secret: Optional[bytes] = None
        #: (verb, frame) -> reply for the open-work verbs. None (flag off) = refused ``disabled``.
        self._on_work = on_work
        #: the presence line (one line of text) added to each held notice a PostToolUse hook claims. None = unchanged.
        self._claim_presence = claim_presence
        # the hub's operator_feedback FRAME (msg_id, recipients) for the
        # operator-messages log, and the post-write hook that starts a delivery check
        # for an operator broadcast/DM. None = legacy (text-only feedback, no check).
        self._on_feedback_frame = on_feedback_frame
        self._on_broadcast_written = on_broadcast_written
        # 2026-09-29 (cross-harness operator activity): the ``context`` verb's
        # ``operator`` field. None = the field is omitted (legacy reply).
        self._operator_context = operator_context
        #: pre-review B2 (count only; flag dsd_notice_inline): how many DSD notices are held > 120 s.
        self._held_notice_count = held_notice_count
        # slice 1: the ``plan_usage`` verb's reader (harness.plan_usage.read
        # bound to this session's state file, ONLY when plan_usage_verb is on). None = refused 'disabled'.
        self._plan_usage_reader = plan_usage_reader
        # the ``alert`` verb's handler, ``handler({"title","body","severity","link"}) -> result dict``,
        # bound by the launcher ONLY when alerts is on. It stamps the session's own identity as the
        # source. None = refused 'disabled'.
        self._alert_handler = alert_handler
        self._on_claim_held_notices = on_claim_held_notices
        # D15 amendment (2026-09-29): a hub-origin inject claiming source
        # ``dsd-answer`` is handed to the harness's DsdNoticeInbox, which holds it
        # for the operator's line / readiness, injects it through THIS injector,
        # and acks the hub only after the write is logged. None = legacy: it is
        # typed at once like any other hub inject.
        self._on_dsd_notice = on_dsd_notice
        # D15 fix pass: sink for the hub-only control frames (hub_hello/superseded).
        self._on_hub_frame = on_hub_frame
        # operator ruling 2026-09-29 (compact_gate.CompactGate): gates the agent's OWN
        # compact (source self-verb) on the 90% threshold + directive freshness.
        # None keeps the pre-gate behavior EXACTLY (unit tests / bare actuators).
        self._compact_gate = compact_gate
        self._on_operator_command = on_operator_command
        self._monitor = monitor
        self._on_lifecycle = on_lifecycle
        self._on_feedback = on_feedback
        self._on_launch = on_launch
        # fix-pass Part-3: when wired, the compact verb reports ok:True only after
        # a PreCompact/compaction-START signal is observed (fail-loud). None keeps
        # the pre-fix behavior EXACTLY (ok:True on the injection allow-outcome).
        self._compact_confirmer = compact_confirmer
        # AUDIT F5: called with the compact verb's result dict AFTER the
        # confirmation resolves (observed or not), so the owner can log it to the
        # provenance log and surface a NOT-observed result to the operator. Only
        # invoked when a confirmer is wired; None keeps the pre-fix path silent.
        self._on_compact_confirmation = on_compact_confirmation

    def set_sinks(
        self,
        *,
        on_lifecycle: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        on_feedback: Optional[Callable[[str], None]] = None,
        on_launch: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> None:
        if on_lifecycle is not None:
            self._on_lifecycle = on_lifecycle
        if on_feedback is not None:
            self._on_feedback = on_feedback
        if on_launch is not None:
            self._on_launch = on_launch

    # -- verbs ---------------------------------------------------------------

    @staticmethod
    def _outcome_fields(outcome: Any) -> Dict[str, Any]:
        """Response keys for a NON-allow gate outcome (none for ALLOW — so a
        hook-less response is byte-identical to the pre-hooks one)."""
        if not isinstance(outcome, Outcome) or outcome.decision in (Decision.ALLOW, Decision.MODIFY):
            if isinstance(outcome, Outcome) and outcome.decision is Decision.MODIFY:
                return {"decision": "modify"}
            return {}
        if outcome.decision is Decision.DENY:
            return {"ok": False, "decision": "deny", "error": f"denied by harness hook: {outcome.reason}"}
        return {"decision": "defer", "held_id": outcome.held_id,
                "note": f"held for operator approval: {outcome.reason}"}

    def inject(
        self,
        text: str,
        skip_clear: bool = False,
        *,
        clear: Optional[bool] = None,
        source: str = SOURCE_UNATTRIBUTED,
        trigger: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        # WS-D (2026-09-27): the clear is now OPT-IN. Resolution order:
        #   explicit ``clear`` > legacy ``skip_clear`` > default (no clear).
        # ``skip_clear=True`` still means "no clear" (fix-pass WS3 callers keep
        # working); the dispatcher maps an EXPLICIT ``skip_clear: false`` to
        # ``clear=True`` — a caller that spelled it out asked for the clear.
        do_clear = bool(clear) if clear is not None else False
        if skip_clear:
            do_clear = False if clear is None else do_clear
        # operator ruling 2026-09-29 (audit fix): a SELF-origin inject of /compact would
        # type a compact PAST the compact gate — refused outright (logged), the
        # agent is pointed at request_compact. Operator / hub origins unchanged.
        # Re-audit: control bytes (ESC/CR/^U/BS/NUL/DEL/paste markers) are refused
        # too, and /compact is checked on EVERY line (compact_gate.screen_self_inject).
        if self._compact_gate is not None and source == SOURCE_SELF_VERB:
            from .compact_gate import screen_self_inject

            reasons = screen_self_inject(text)
            if reasons:
                verdict = self._compact_gate.refuse_inject(text, reasons)
                return {"ok": False, "action": "inject", "decision": "refused",
                        "reasons": list(verdict.reasons), "error": verdict.error}
        # operator ruling (UNCONDITIONAL, no flag): a leading '!' is refused for EVERY
        # source reaching this verb, including no-origin (source "unattributed": g71, no source means
        # non-operator). The self screen above already covers self-verbs when a gate exists; this
        # also covers the no-gate case and every other source.
        from .leading_bang import gate_reason as _gate_reason, screen_text as _screen_text

        shared = _screen_text(text)  # r2c: the ONE union screen (control bytes + leading '!')
        if shared:
            if self._compact_gate is not None:
                verdict = self._compact_gate.refuse_inject(text, shared, source=source)
                return {"ok": False, "action": "inject", "decision": "refused",
                        "reasons": list(verdict.reasons), "error": verdict.error}
            return {"ok": False, "action": "inject", "decision": "refused", "reasons": list(shared),
                    "error": "inject REFUSED: " + _gate_reason(shared) + " (pilot ruling dsd-1a2b3c4d5e3 D2)"}
        outcome = self._injector.inject_line(text, clear=do_clear, source=source,
                                             trigger=trigger, meta=meta)
        out: Dict[str, Any] = {"ok": True, "action": "inject"}
        if skip_clear:
            out["skip_clear"] = True
        if do_clear:
            out["clear"] = True
        out.update(self._outcome_fields(outcome))
        return out

    def compact(self, direction: str = "", *, directive_path: str = "",
                clear: bool = True, source: str = SOURCE_UNATTRIBUTED,
                ch_approved: bool = False, approval_note: Optional[str] = None) -> Dict[str, Any]:
        # Relay only. The harness never originates a compact.
        # ⚠ operator ruling 2026-09-28 (reverses 2026-09-27): the ESC ESC clear is
        # DEFAULT-TRUE — flight 3.2 live-proved a self /compact with clear=false
        # does NOT fire, so the ESC ESC is load-bearing. Still opt-out per call
        # (``clear=False``); threaded to Injector.compact unchanged.
        # A ``directive_path`` is read HERE (at compaction time) and supplies the
        # argument for long/rich directives, without threading a huge string
        # through the MCP arg or a single inline PTY burst; it WINS over inline.
        used_path = None
        if directive_path:
            try:
                with open(directive_path, "r", encoding="utf-8") as fh:
                    direction = fh.read()
            # N1 (fix-pass): a binary / non-UTF8 file raises UnicodeDecodeError (a
            # ValueError subclass, NOT an OSError) — catch it too so an unreadable
            # OR undecodable path raises a clean ActuationError, never a raw
            # traceback out of the actuator.
            except (OSError, UnicodeDecodeError) as exc:
                raise ActuationError(f"compact directive_path unreadable: {exc}")
            # N2 (fix-pass): an empty / whitespace-only directive FILE used to
            # silently degrade to a bare `/compact` (no direction) while the
            # receipt reported the pre-strip byte length — misleading. An empty
            # directive is almost certainly a mistake, so fail LOUD rather than
            # compact with no direction. (Inline direction="" with no
            # directive_path is unaffected: that bare `/compact` is intentional.)
            if not direction.strip():
                raise ActuationError(
                    "directive_path file is empty/whitespace-only — "
                    "refusing to compact with no direction"
                )
            used_path = directive_path
        # r2c (g71-amendment-r2b (2)): the direction (inline OR the directive_path file's contents) is
        # TEXT THAT REACHES THE TUI, typed inside a bracketed paste. Screen it with the SAME union
        # screen as every other injection, before anything is typed or any gate state is touched, so an
        # embedded ESC[201~ cannot close the paste early and type a new prompt line. Allowed through:
        # (the text the injector types: direction.strip()) \t and \n (a multi-line directive is one bracketed paste and a \n never submits there);
        # refused: every other C0 control, DEL, ESC (so both paste delimiters), bare CR (a directive FILE is read in
        # universal-newline mode, so its CRLF/CR line endings already arrive as \n).
        # A leading "!" in the direction is NOT checked: it is typed after "/compact " (never first).
        from .leading_bang import REASON_CONTROL as _CTL, gate_reason as _gate_reason, screen_text as _screen_text

        arg_refusal = [r for r in (_screen_text((direction or "").strip()) or []) if r == _CTL]  # C0/DEL/C1/lone surrogate/ESC all ride the one control-bytes token
        if arg_refusal:
            where = f"directive_path {directive_path!r} contents" if used_path else "direction"
            if self._compact_gate is not None:
                verdict = self._compact_gate.refuse_inject(("/compact " + direction), arg_refusal, source=source,
                                                          stage="compact-argument")
                reasons = list(verdict.reasons)
            else:
                reasons = list(arg_refusal)
            return {"ok": False, "action": "compact", "decision": "refused", "reasons": reasons,
                    "error": f"compact REFUSED: the {where} carries control bytes; nothing was typed. "
                             + _gate_reason(arg_refusal)
                             + " Tab and newline are allowed; remove ESC, CR (use LF line endings), DEL and "
                               "other control characters."}
        # operator ruling 2026-09-29: the self-compact gate. ONLY the agent's own verb
        # (SELF origin) is gated — the operator's typed /compact never reaches
        # here and the hub-origin request is the operator's. A refusal types
        # NOTHING and is logged by the gate (compact-refused).
        approval_used: Optional[str] = None
        takeover = None
        if self._compact_gate is not None and source == SOURCE_SELF_VERB:
            verdict = self._compact_gate.check(directive_path=directive_path,
                                               ch_approved=bool(ch_approved),
                                               approval_note=approval_note)
            if not verdict.allowed:
                return {"ok": False, "action": "compact", "decision": "refused",
                        "reasons": list(verdict.reasons), "error": verdict.error,
                        "used_percentage": verdict.used_percentage}
            approval_used = verdict.approval_note
            # D3: the gate saved a half-typed operator line (idle >= 10
            # min) and lets the compact go; the ESC ESC clear is then MANDATORY,
            # whatever the caller asked for, or the compact would merge into the text.
            takeover = verdict
            if verdict.takeover_text is not None:
                clear = True
        # Part-3 fail-loud: open the confirmation epoch BEFORE actuating, so a
        # fast PreCompact cannot be attributed to a previous compact.
        if self._compact_confirmer is not None:
            self._compact_confirmer.arm()
        if takeover is not None and takeover.takeover_text is not None:
            outcome = self._injector.compact(direction, clear=bool(clear), source=source, line_takeover=True)
        else:
            outcome = self._injector.compact(direction, clear=bool(clear), source=source)
        if takeover is not None and takeover.takeover_text is not None:
            if isinstance(outcome, Outcome) and outcome.written:
                self._compact_gate.commit_takeover(takeover)
            else:
                self._compact_gate.cancel_takeover(takeover, "compact not written (hook DENY/DEFER)")
        out: Dict[str, Any] = {"ok": True, "action": "compact"}
        from . import compact_messages as _cm   # pre-review B3 (flag compact_plain_messages; OFF = no key)

        # said only when the compact was really typed: never on a hook DENY (ok false) or a DEFER (held for the operator)
        if _cm.enabled() and isinstance(outcome, Outcome) and outcome.written:
            out["message"] = _cm.ACCEPTED_TEXT
        if used_path is not None:
            out["directive_path"] = used_path
            # F6a: report the POST-strip byte length (0 when empty) — this is what
            # the injector actually types (Injector.compact strips), so a pre-strip
            # count over-reported a directive with surrounding whitespace.
            out["direction_bytes"] = len(direction.strip().encode("utf-8"))
        else:
            out["direction"] = direction or ""
        if approval_used:
            out["ch_approval_note"] = approval_used
        out.update(self._outcome_fields(outcome))
        # Part-3 fail-loud: assert the compaction actually STARTED whenever the
        # command was WRITTEN to the child.
        # ⛔ AUDIT F2: the pre-fix guard was ``"decision" not in out``, which
        # SKIPPED confirmation whenever a hook MODIFY-ed the command (the shipped
        # Prefix hook sets decision:"modify") — the command still went to the
        # child, but the old false-positive ok:True returned unconfirmed. Guard on
        # ``outcome.written`` instead: MODIFY and ALLOW both write (so both are
        # confirmed); DENY / DEFER did not actuate (nothing to confirm), exactly as
        # before.
        if (
            self._compact_confirmer is not None
            and isinstance(outcome, Outcome)
            and outcome.written
        ):
            if self._compact_confirmer.confirm():
                out["compaction_observed"] = True
            else:
                out["ok"] = False
                out["compaction_observed"] = False
                out.pop("message", None)   # B3: "accepted" is not true when no compaction was observed
                # ⛔ RE-DIAMOND N1: distinguish a child that EXITED during the wait
                # (the session ended before PreCompact — retrying will not help) from
                # a compaction simply not observed inside the window (the original
                # paste-reclassified / busy case). The launcher's confirm poll flags
                # child-exit on the confirmer.
                if getattr(self._compact_confirmer, "child_exited", False):
                    out["reason"] = (
                        "compaction not observed: the child EXITED before a PreCompact "
                        "signal arrived (the session ended) — not compacted"
                    )
                else:
                    out["reason"] = (
                        f"compaction not observed within {self._compact_confirmer.timeout}s "
                        "— command may have been paste-reclassified or the session was busy"
                    )
            # D3: no compaction observed -> the operator's cleared text is
            # NOT put back into an unknown prompt; it stays in the saved file, loudly.
            if (not out.get("ok", True) and takeover is not None
                    and takeover.takeover_text is not None):
                self._compact_gate.abort_restore(str(out.get("reason") or "compaction not observed"))
            # F5: hand the resolved result to the owner (provenance log + operator
            # surface). Never let a sink failure break the verb.
            if self._on_compact_confirmation is not None:
                try:
                    self._on_compact_confirmation(dict(out))
                except Exception:  # noqa: BLE001
                    pass
        return out

    def context(self) -> Dict[str, Any]:
        used = None
        if self._monitor is not None:
            notice = self._monitor.context_report()
            used = notice.used_percentage
        out: Dict[str, Any] = {"ok": True, "action": "context", "used_percentage": used}
        # pre-review B2: the held-notice count. Flag OFF = the key is not there at all. A failing counter is LOGGED and the key is left
        # out; the reply still answers.
        try:
            from .. import flags as _flags

            if _flags.flag("dsd_notice_inline"):
                held = int(self._held_notice_count()) if self._held_notice_count is not None else 0
                out["held_notices"] = max(0, held)
        except Exception as exc:  # noqa: BLE001 - the gauge answer must still return
            # An ERROR is shown as one (pre-review fix round, D-F2): null plus a short reason, and a log line. Never a missing key
            # that reads like flag OFF, never a zero.
            out["held_notices"] = None
            out["held_notices_error"] = f"{type(exc).__name__}: {exc}"[:200]
            logging.getLogger("gnomon.harness.actuator").warning(
                "context: held_notices unavailable, the held-notice counter failed: %s: %s", type(exc).__name__, exc)
        if self._operator_context is not None:
            try:
                out["operator"] = self._operator_context()
            except Exception as exc:  # noqa: BLE001 - the gauge answer must still return
                out["operator"] = {"error": f"{type(exc).__name__}: {exc}"}
            if isinstance(out["operator"], dict):          # present whenever the operator object is (also on its error form)
                out["operator"] = {**out["operator"], OPERATOR_COUNTS_KEY: OPERATOR_COUNTS_TEXT}
        return out

    def plan_usage(self) -> Dict[str, Any]:
        """slice 1: account plan usage (five_hour / seven_day) from the
        statusline tap. Pure read; never raises; null + reason, never a guess.
        No reader bound (plan_usage_verb off at launch) = refused ``disabled``."""
        from . import plan_usage as _pu

        if self._plan_usage_reader is None:
            return {"ok": False, "action": "plan_usage", "decision": "disabled",
                    "error": "the plan_usage verb is not enabled in this harness (plan_usage_verb)"}
        try:
            body = self._plan_usage_reader()
        except Exception:  # noqa: BLE001
            body = _pu._null_result("unreadable")
        out: Dict[str, Any] = {"ok": True, "action": "plan_usage"}
        out.update(body)
        return out

    def alert(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """the ``alert`` verb. Types are checked here (a hand-built frame with a wrong type is refused
        loudly, ``ActuationError``); the closed parameter set means a ``source`` / ``instance_id`` / any other key
        is refused too. Everything else (sanitizing, severity, de-dup, rate limit, logging) is the handler's."""
        if self._alert_handler is None:
            return {"ok": False, "action": "alert", "decision": "disabled",
                    "error": "the alert verb is not enabled in this harness (alerts)"}
        extra = set(command) - {"cmd", "title", "body", "severity", "link"}
        if extra:
            raise ActuationError(f"alert: unknown parameter(s) {sorted(extra)} (the set is closed: title, body, "
                                 "severity, link; the source is always this session)")
        title = command.get("title")
        if not isinstance(title, str) or not title.strip():
            raise ActuationError("alert 'title' is required and must be a non-empty string")
        frame: Dict[str, Any] = {"title": title}
        for key in ("body", "severity", "link"):
            v = command.get(key)
            if v is None:
                continue
            if not isinstance(v, str):
                raise ActuationError(f"alert {key!r} must be a string")
            frame[key] = v
        try:
            out = dict(self._alert_handler(frame))
        except Exception as exc:  # noqa: BLE001 - a handler fault is a loud refusal, never a pump crash
            return {"ok": False, "action": "alert", "decision": "error", "error": f"internal error: {exc}"}
        out.setdefault("action", "alert")
        return out

    def _exit_quiescent(self, trigger: str) -> None:
        """end the monitor's quiescent state (no-op when not quiescent,
        no monitor, or the feature is off). Never raises into the dispatcher."""
        exit_fn = getattr(self._monitor, "exit_quiescent", None)
        if exit_fn is None:
            return
        try:
            exit_fn(trigger)
        except Exception:  # noqa: BLE001
            pass

    def _note_new_work(self, trigger: str) -> None:
        """new work reached this agent through the hub (a DSD answer or an operator
        message): a dismissal of idle alerts ends. A peer/bus message is NOT new work here.
        Never raises into the dispatcher."""
        snooze = getattr(self._monitor, "idle_snooze", None)
        if snooze is None:
            return
        try:
            snooze.on_new_work(trigger)
        except Exception:  # noqa: BLE001
            pass

    # -- dispatch ------------------------------------------------------------

    @staticmethod
    def source_for(origin: Optional[str], command: Dict[str, Any]) -> str:
        """Provenance source from the DELIVERING transport (WS-A).

        SELF socket -> ``self-verb``. HUB socket -> the frame's ``source`` iff it
        is one a hub may claim (``bus`` / ``operator-broadcast`` /
        ``agent-launch``), else ``bus``. No origin (a direct in-process caller)
        -> ``unattributed``.
        """
        if origin == ORIGIN_SELF:
            return SOURCE_SELF_VERB
        if origin == ORIGIN_HUB:
            claimed = command.get("source")
            return claimed if claimed in HUB_CLAIMABLE_SOURCES else SOURCE_BUS
        return SOURCE_UNATTRIBUTED

    def dispatch(self, command: Dict[str, Any], origin: Optional[str] = None) -> Dict[str, Any]:
        """Route a decoded command dict. ``command["cmd"]`` selects the verb.

        ``origin`` (PTY-hooks WS-A) is supplied by the TRANSPORT (the control
        channel that read the frame), never by the frame itself."""
        if not isinstance(command, dict):
            raise ActuationError("command must be a dict")
        cmd = command.get("cmd")
        source = self.source_for(origin, command)
        trigger = command.get("trigger") if isinstance(command.get("trigger"), str) else None
        if cmd == "inject":
            text = command.get("text")
            if not isinstance(text, str):
                raise ActuationError("inject requires a string 'text'")
            skip_clear = command.get("skip_clear", False)
            if not isinstance(skip_clear, bool):
                raise ActuationError("inject 'skip_clear' must be a boolean")
            clear = command.get("clear")
            if clear is not None and not isinstance(clear, bool):
                raise ActuationError("inject 'clear' must be a boolean")
            if clear is None and "skip_clear" in command and skip_clear is False:
                clear = True  # explicit legacy opt-out-of-skip == ask for the clear
            meta: Dict[str, Any] = {}
            if origin == ORIGIN_HUB and isinstance(command.get("from"), str):
                meta["from_instance"] = command["from"]
            if origin == ORIGIN_HUB:
                # link this injection record to the operator-messages log.
                for key in ("msg_id", "sent_sha256"):
                    if isinstance(command.get(key), str):
                        meta[key] = command[key]
            if origin == ORIGIN_HUB:
                # D15 fix pass (audit HIGH 2d) + fix pass 2 (re-audit HIGH): the hub
                # may type messages, never edit keys or operator commands. Control
                # bytes (C0 except \t/\n, DEL — the SAME predicate as the self
                # screen) and any line that is a \command after zero-width/BOM
                # stripping are REFUSED.
                from .compact_gate import screen_hub_inject

                reasons = screen_hub_inject(text)
                if reasons and self._compact_gate is not None and "leading-bang" in reasons:
                    # operator ruling loud + a provenance record (kind inject-refused).
                    verdict = self._compact_gate.refuse_inject(text, reasons, source=source)
                    return {"ok": False, "action": "inject", "decision": "refused",
                            "reasons": list(verdict.reasons), "error": verdict.error}
                if reasons:
                    return {"ok": False, "action": "inject", "decision": "refused",
                            "reasons": reasons,
                            "error": "a hub-origin inject may not carry control bytes or an "
                                     f"operator \\command line (reasons: {reasons})"}
            if origin == ORIGIN_HUB:
                # an accepted hub message (after the screen above) ends
                # quiescence — new input for the agent.
                self._exit_quiescent("dsd-answer" if source == SOURCE_DSD_ANSWER else "hub-inject")
                # only a DSD answer or an operator message ends a dismissal
                # (source ``bus`` = a peer message does not).
                if source == SOURCE_DSD_ANSWER:
                    self._note_new_work("dsd-answer")
                elif source == SOURCE_OPERATOR_BROADCAST:
                    self._note_new_work("operator-message")
            if source == SOURCE_DSD_ANSWER and self._on_dsd_notice is not None:
                return dict(self._on_dsd_notice(command))
            since = time.time()
            out = self.inject(text, skip_clear=skip_clear, clear=clear, source=source,
                              trigger=trigger, meta=meta)
            # an operator broadcast/DM WRITTEN verbatim (plain ALLOW: a hook
            # MODIFY/DENY/DEFER carries a "decision") gets a delivery check.
            if (source == SOURCE_OPERATOR_BROADCAST and self._on_broadcast_written is not None
                    and out.get("ok") and "decision" not in out):
                try:
                    self._on_broadcast_written(dict(command), since)
                except Exception:  # noqa: BLE001 - a check must never fail the inject
                    pass
            return out
        if cmd == "compact":
            direction = command.get("direction", "")
            if direction is None:
                direction = ""
            if not isinstance(direction, str):
                raise ActuationError("compact 'direction' must be a string")
            directive_path = command.get("directive_path", "")
            if directive_path is None:
                directive_path = ""
            if not isinstance(directive_path, str):
                raise ActuationError("compact 'directive_path' must be a string")
            # operator ruling 2026-09-28: ESC ESC clear DEFAULTS TRUE (load-bearing for
            # self-compaction), opt-out per call. A frame that omits ``clear``
            # (e.g. the hub relay pre-F2, or a hand-built frame) now clears by
            # default; self_mcp/hub write the resolved boolean explicitly so an
            # opt-out threads correctly.
            clear = command.get("clear", True)
            if not isinstance(clear, bool):
                raise ActuationError("compact 'clear' must be a boolean")
            # operator ruling 2026-09-29: the gate's override args (optional; absent
            # frames are unchanged). Wrong types are refused loudly.
            ch_approved = command.get(COMPACT_APPROVAL_FIELD, False)
            if not isinstance(ch_approved, bool):
                raise ActuationError(f"compact '{COMPACT_APPROVAL_FIELD}' must be a boolean")
            approval_note = command.get("approval_note")
            if approval_note is not None and not isinstance(approval_note, str):
                raise ActuationError("compact 'approval_note' must be a string")
            if origin == ORIGIN_HUB:
                self._exit_quiescent("hub-inject")  #
            return self.compact(direction, directive_path=directive_path, clear=clear,
                                source=source, ch_approved=ch_approved,
                                approval_note=approval_note)
        if cmd == "context":
            return self.context()
        if cmd in self.INTERNAL_VOCABULARY:
            if origin != self.INTERNAL_VOCABULARY[cmd]:
                raise ActuationError(f"{cmd!r} is not accepted from origin {origin!r}")
            try:
                return self._dispatch_internal(cmd, command)
            except ActuationError:
                raise
            except Exception as exc:  # noqa: BLE001 - a sink failure must not kill the pump
                return {"ok": False, "action": cmd, "error": f"internal error: {exc}"}
        raise ActuationError(f"unknown command: {cmd!r}")

    def _mail_ticket(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """F3: mint a one-call ticket for THIS session (see ``gnomon.mail.ticket``). Refused
        ``disabled`` without the flag, ``no-hub-session`` with no hub secret (hub down, not yet greeted, or
        superseded): never a guess, never an unsigned ticket."""
        if self._mail_identity is None:
            return {"ok": False, "action": "mail_ticket", "decision": "disabled",
                    "error": "mail is not enabled in this harness (agent_mail)"}
        op, digest = command.get("op"), command.get("args_sha256")
        if (not isinstance(op, str) or not op or len(op) > 40 or not isinstance(digest, str)
                or len(digest) != 64):
            raise ActuationError("mail_ticket needs a string 'op' and a 64-hex 'args_sha256'")
        if self._hub_session_secret is None:
            return {"ok": False, "action": "mail_ticket", "decision": "no-hub-session",
                    "error": "this session has no hub session secret (the hub link is down, not yet greeted, or "
                             "superseded): mail cannot be authenticated right now; try again shortly"}
        from ..mail import ticket as _ticket

        try:
            tk = _ticket.mint(self._hub_session_secret, self._mail_identity, op, digest)
        except ValueError as exc:
            return {"ok": False, "action": "mail_ticket", "decision": "refused", "error": f"cannot mint a ticket: {exc}"}
        return {"ok": True, "action": "mail_ticket", "ticket": tk}

    def _idle_snooze(self, command: Dict[str, Any]) -> Dict[str, Any]:
        """the ``idle_snooze`` verb. Types are checked here (a hand-built frame
        with a wrong type is refused loudly); everything else is
        :meth:`~gnomon.harness.idle_snooze.IdleSnooze.tap`."""
        snooze = getattr(self._monitor, "idle_snooze", None)
        if snooze is None:
            return {"ok": False, "action": "idle_snooze", "decision": "disabled", "changed": False,
                    "error": "no idle-snooze support in this harness - idle nudges continue as normal"}
        flags: Dict[str, bool] = {}
        for key in ("dismiss_until_return", "confirm"):
            v = command.get(key, False)
            if v is None:
                v = False
            if not isinstance(v, bool):
                raise ActuationError(f"idle_snooze {key!r} must be a boolean")
            flags[key] = v
        reason = command.get("reason", "")
        if reason is None:
            reason = ""
        if not isinstance(reason, str):
            raise ActuationError("idle_snooze 'reason' must be a string")
        if snooze.enabled and not getattr(self._monitor, "nudges_enabled", True):
            return {"ok": False, "action": "idle_snooze", "decision": "refused", "changed": False,
                    "error": "idle nudges are not enabled for this session - there is nothing to snooze"}
        return dict(snooze.tap(reason=reason, **flags))

    def _dispatch_internal(self, cmd: str, command: Dict[str, Any]) -> Dict[str, Any]:
        """The PTY-hooks internal verbs (origin already checked by dispatch)."""
        if cmd == "lifecycle":
            event = command.get("event")
            if event not in LIFECYCLE_EVENTS:
                raise ActuationError(f"lifecycle 'event' must be one of {sorted(LIFECYCLE_EVENTS)}")
            if self._on_lifecycle is not None:
                self._on_lifecycle(event, {k: v for k, v in command.items() if k not in ("cmd", "event")})
            return {"ok": True, "action": "lifecycle", "event": event}
        if cmd in ("hub_hello", "superseded", "vacated"):
            if cmd == "hub_hello":
                command = dict(command)
                sec = command.pop("session_secret", None)  # pulled out BEFORE any sink or log can see the frame
                if isinstance(sec, str):
                    try:
                        raw = bytes.fromhex(sec)
                        self._hub_session_secret = raw if len(raw) >= 16 else None
                    except ValueError:
                        self._hub_session_secret = None
            elif cmd == "superseded":
                self._hub_session_secret = None  # no longer the registered holder: its tickets would not verify
            if self._on_hub_frame is not None:
                self._on_hub_frame(cmd, command)
            return {"ok": True, "action": cmd}
        if cmd == "mail_ticket":
            return self._mail_ticket(command)
        if cmd == "plan_usage":
            return self.plan_usage()
        if cmd in ("work_add", "work_update", "work_done", "work_drop", "work_list", "operator_status_set"):
            if self._on_work is None:
                return {"ok": False, "action": cmd, "decision": "disabled", "changed": False,
                        "error": "the open-work list is not enabled in this harness (open_work_list)"}
            return dict(self._on_work(cmd, dict(command)))
        if cmd == "alert":
            return self.alert(command)
        if cmd == "quiesce":
            if self._monitor is None or not hasattr(self._monitor, "enter_quiescent"):
                return {"ok": False, "action": cmd, "decision": "disabled",
                        "error": "no stall monitor in this harness — nothing to quiesce"}
            return dict(self._monitor.enter_quiescent(command.get("reason")))
        if cmd == "idle_snooze":
            return self._idle_snooze(command)
        if cmd == "held_notices":
            texts = self._on_claim_held_notices() if self._on_claim_held_notices is not None else []
            if self._claim_presence is not None and texts:
                try:  # the claimed notice is a harness-originated wake-up: it carries the presence line (rule-presence-line-covers)
                    _pl = self._claim_presence()
                except Exception:  # noqa: BLE001 - information only; never blocks the notice
                    _pl = None
                if _pl and isinstance(texts[-1], str) and _pl not in texts[-1]:
                    texts = list(texts[:-1]) + [texts[-1] + "\n" + _pl]   # ONE presence line per claim, on the last notice
            return {"ok": True, "action": cmd,
                    "notices": [t for t in (texts or []) if isinstance(t, str)]}
        if cmd == "operator_command":
            line = command.get("line")
            if not isinstance(line, str) or not line.strip():
                raise ActuationError("operator_command requires a non-empty string 'line'")
            if self._on_operator_command is None:
                return {"ok": False, "action": cmd, "decision": "unavailable",
                        "reply": "the operator command plane is not available in this harness"}
            out = dict(self._on_operator_command(line))
            out.setdefault("action", cmd)
            return out
        if cmd.startswith("launch_"):
            if self._on_launch is None:
                return {"ok": False, "error": "agent launch is not wired in this harness"}
            return self._on_launch(cmd, command)
        text = command.get("text")  # operator_feedback
        if not isinstance(text, str):
            raise ActuationError("operator_feedback requires a string 'text'")
        if self._on_feedback is not None:
            self._on_feedback(text)
        if self._on_feedback_frame is not None:  #
            try:
                self._on_feedback_frame(dict(command))
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "action": "operator_feedback"}

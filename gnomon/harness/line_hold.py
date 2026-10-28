"""The operator-line HOLD rule — ONE rule for every harness-originated injection.

The operator tap (:attr:`~gnomon.harness.optap.OperatorInputTap.line_state`,
D15 fix pass) knows one of three things about the operator's unsent line:

* ``empty``   — nothing typed since the last submit;
* ``partial`` — typed-but-unsent text is in the prompt;
* ``unknown`` — the line punted (cursor / history / Tab / Ctrl-L / paste).

A harness-originated injection (DSD-answer notice, stall/resume heartbeat incl.
the post-compact resume nudge, compact-guidance, FYI) must never be typed onto a
half-written operator line — it would concatenate with the operator's text and
submit it. Observed live 2026-09-29 ~05:40Z: the stall nudge was appended to the
the operator's half-typed line. The rule:

* ``partial`` — HOLD (until the operator's Enter / line-kill empties the line);
* ``unknown`` — HOLD until ``unknown_after_seconds`` (default 300) of operator
  silence, then treat as empty;
* ``empty``   — no hold from the line (the caller's own rule applies unchanged).

An unrecognised state is treated as ``unknown`` (fail toward holding).
Operator-initiated actions are NOT subject to this rule.
"""

from __future__ import annotations

from typing import Optional

LINE_EMPTY = "empty"
LINE_PARTIAL = "partial"
LINE_UNKNOWN = "unknown"
LINE_STATES = (LINE_EMPTY, LINE_PARTIAL, LINE_UNKNOWN)

#: The 5-minute operator-silence rule for an ``unknown`` line.
DEFAULT_UNKNOWN_AFTER_SECONDS = 300.0


def normalize_line_state(state: object) -> str:
    return state if state in LINE_STATES else LINE_UNKNOWN  # type: ignore[return-value]


def line_hold_reason(state: object, operator_silent_seconds: float,
                     unknown_after_seconds: float = DEFAULT_UNKNOWN_AFTER_SECONDS) -> Optional[str]:
    """Why a harness-originated injection must be held right now, or ``None``.

    ``operator_silent_seconds`` is how long the operator has gone without a
    keystroke (the caller's clock)."""
    st = normalize_line_state(state)
    if st == LINE_PARTIAL:
        return "operator has a partially typed line (held until Enter)"
    if st == LINE_UNKNOWN:
        if operator_silent_seconds < unknown_after_seconds:
            return (f"operator line state unknown (cursor/history/paste) — held until "
                    f"{unknown_after_seconds:.0f}s of operator silence")
    return None

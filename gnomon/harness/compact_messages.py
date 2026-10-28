"""Plain-words compact messages (pre-review batch B3; flag ``compact_plain_messages``, default OFF).

Three texts an agent reads around a compact. With the flag OFF every caller keeps its previous text, byte for byte; this module
only answers "is the flag on?" and holds the new strings. The strings are fixed text agents are told to recognise: change one
only together with the rule that names them.

The flag is read from the MACHINE config and the registry default only (never from a session's ``launch.json``, which the
session can write), at the moment a text is built, so a flip applies to the next compact. Any failure to read it means OFF.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

FLAG_NAME = "compact_plain_messages"

#: ``request_compact``'s accepted result (key ``message``).
ACCEPTED_TEXT = ("Compact accepted. Your turn is being interrupted now: this interrupt IS the compact, "
                 "not a refusal by the operator.")

#: One line added to the post-compact context after a compact completed.
COMPLETED_LINE = ("A compact completed at {utc}. Any 'call request_compact' step in your summary is already done; "
                  "do not call it again.")


def enabled() -> bool:
    """Is the flag on? Never raises: any error reading it is OFF (today's texts)."""
    try:
        from .. import flags as flags

        return bool(flags.flag(FLAG_NAME))
    except Exception:  # noqa: BLE001
        return False


def completed_line(now: Optional[datetime] = None) -> str:
    """The post-compact line, with the UTC time read from the clock when the hook runs."""
    now = now or datetime.now(timezone.utc)
    return COMPLETED_LINE.format(utc=now.strftime("%Y-%m-%dT%H:%M:%SZ"))


def soft_refusal_text(pct: str, threshold: float, minutes: str, wisdom: Optional[str]) -> str:
    """The below-threshold first-call refusal: says what the operator sees and what the second call does."""
    msg = (f"This first call did NOT compact: nothing was typed into the terminal, so the operator's screen shows no "
           f"change. You are at {pct}% context, below {threshold:g}%.")
    if wisdom:
        msg += " " + wisdom
    return (msg + f" To compact anyway, call request_compact again within {minutes} minutes with a non-empty 'reason' "
            "(logged). That second call interrupts your turn: the interrupt IS the compact, not a refusal by the "
            "operator. The handover and directive freshness checks still apply.")

"""DSD submit detection (operator ruling checklist g76).

A DSD submit must carry a short summary of the operator's REAL keyboard / pointer activity in the DSD tab
(``{count, first_ms_ago, last_ms_ago}`` from ``web/static/dsd_input_activity.js``, which counts only browser-
generated events). The server REFUSES a submit whose summary is missing, malformed, empty, or whose last
activity is older than :data:`MAX_LAST_ACTIVITY_S` (300 s). There is NO bypass: no header, flag value in the
request, or caller identity relaxes it. (The only OFF switch is the machine flag ``dsd_submit_proof``.)

Every minted answer records the evidence on its root node (``submit-evidence``): activity count, last-activity
age, first-activity age, the User-Agent, and the server time the check ran.

FRAME: this stops an over-eager AGENT (which has no in-tab input to report, and is also told by its tool rules
not to call the route). It does NOT stop a hostile same-uid process, which can forge the summary; closing that
needs a real credential on the loopback server (the FIDO roadmap item, operator).
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Tuple

#: The submit is refused when the newest in-tab input is older than this (seconds). 5 minutes covers reading a
#: long DSD and typing a comment, while an agent calling the route from a shell has no input at all.
MAX_LAST_ACTIVITY_S = 300
MAX_COUNT = 10_000_000
FLAG = "dsd_submit_proof"
_UA_CTRL = re.compile(r"[\x00-\x1f\x7f\[\]]")


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def check_activity(activity: Any, user_agent: str = "",
                   max_age_s: float = MAX_LAST_ACTIVITY_S) -> Tuple[bool, Dict[str, Any], str]:
    """(ok, evidence, refusal_message). ``evidence`` is filled whenever it can be, even on refusal."""
    ua = _UA_CTRL.sub(" ", user_agent or "").strip()[:200] or "(none)"
    ev: Dict[str, Any] = {"count": 0, "last_age_s": None, "first_age_s": None, "user_agent": ua}
    why = ("This save needs your keyboard or mouse activity in the DSD tab and none was seen (a save from a "
           "script or an agent is refused). Click or type in the DSD tab, then save again.")
    if not isinstance(activity, dict):
        return False, ev, why
    count, last, first = _num(activity.get("count")), _num(activity.get("last_ms_ago")), _num(activity.get("first_ms_ago"))
    if count is None or count != int(count) or count < 1 or count > MAX_COUNT or last is None or last < 0:
        return False, ev, why
    ev["count"] = int(count)
    ev["last_age_s"] = round(last / 1000.0, 1)
    if first is not None and first >= last:
        ev["first_age_s"] = round(first / 1000.0, 1)
    if last / 1000.0 > max_age_s:
        return False, ev, (f"Your last keyboard or mouse activity in the DSD tab was {ev['last_age_s']:.0f} s ago "
                           f"(the limit is {int(max_age_s)} s). Click or type in the DSD tab, then save again.")
    return True, ev, ""


def evidence_text(ev: Dict[str, Any], checked_at_iso: str) -> str:
    """One line for the answer's root node: no brackets, no newlines."""
    return (f"in-tab-activity count={ev.get('count')}; last-activity-age-s={ev.get('last_age_s')}; "
            f"first-activity-age-s={ev.get('first_age_s')}; user-agent={ev.get('user_agent')}; "
            f"checked-at={checked_at_iso}; limit-s={MAX_LAST_ACTIVITY_S}")


def evidence_text_off(checked_at_iso: str) -> str:
    return f"proof-disabled (dsd_submit_proof OFF); checked-at={checked_at_iso}"

"""Inject text cleaning and the origin label (requirement-text, requirement-origin-prefix).

The label is INFORMATIONAL ONLY (requirement-label-not-authority): nothing in this package, or anywhere, may decide
trust from it. The proof of origin is the target session's injection_log trigger.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Optional, Tuple

from .limits import LIMITS

_CSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_ESC_OTHER = re.compile(r"\x1b[\x20-\x7e]?")
_NL_RUN = re.compile(r"[\n\r\u2028\u2029\x85]+")


def clean_text(raw: str) -> str:
    """Strip terminal escape sequences, C0/C1 control bytes, DEL and format characters (zero-width, bidi);
    keep AT MOST ONE plain line break (the first run of line breaks becomes one ``\\n``, later runs become a space).
    Linear-time (no backtracking pattern)."""
    s = _OSC.sub("", raw)
    s = _CSI.sub("", s)
    s = _ESC_OTHER.sub("", s)
    out = []
    for c in s:
        o = ord(c)
        if c in "\n\r\u2028\u2029\x85":
            out.append("\n")
        elif c == "\t":
            out.append(" ")
        elif o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
            continue
        elif unicodedata.category(c) in ("Cf", "Cc", "Cs", "Co", "Cn"):
            continue
        else:
            out.append(c)
    s = "".join(out)
    seen_nl = [False]

    def _nl(m: "re.Match[str]") -> str:
        if not seen_nl[0]:
            seen_nl[0] = True
            return "\n"
        return " "

    s = _NL_RUN.sub(_nl, s)
    return s.strip()


def first_visible(text: str) -> Optional[str]:
    for c in text:
        if not c.isspace():
            return c
    return None


def validate_text(raw: object) -> Tuple[Optional[str], Optional[str]]:
    """``(cleaned, None)`` or ``(None, reason)``."""
    if not isinstance(raw, str):
        return None, "the text must be a string"
    cleaned = clean_text(raw)
    if not cleaned:
        return None, "the text is empty after cleaning"
    if len(cleaned) > LIMITS["text_max_chars"]:
        return None, (f"the text is {len(cleaned)} characters after cleaning; the limit is "
                      f"{LIMITS['text_max_chars']}")
    if first_visible(cleaned) == "!":
        return None, "the text may not start with '!' (a leading '!' is refused on every injection path, g71)"
    return cleaned, None


def fire_time_utc(epoch: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(epoch), tz=_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_label(job_id: str, proposer: str, fire_epoch: float) -> str:
    """The fixed label. ``job_id`` is grammar-checked and ``proposer`` is the verified instance id, never free text."""
    return f"[scheduled: {job_id} by {proposer} at {fire_time_utc(fire_epoch)}]"


def labelled(job_id: str, proposer: str, fire_epoch: float, cleaned_text: str) -> str:
    """The delivered text: the REAL label first, then a space, then the cleaned text. A text that itself starts with
    a label keeps it after the real one."""
    return make_label(job_id, proposer, fire_epoch) + " " + cleaned_text

"""Date/time stamps inside the harness's own message tags (operator 2026-10-03).

``<Operator-to-all@2026-10-03 11:29> msg`` and ``[Gnomon@2026-10-03 11:29] ...``:
the machine's LOCAL time zone, 24-hour clock, zero-padded, minute resolution.
The stamp is taken when the text is COMPOSED (so a held / queued item keeps its
original send time; nothing re-stamps at release).
"""

from __future__ import annotations

import time
from typing import Optional

#: ``YYYY-MM-DD HH:MM`` (24 h). Used by the hub's fan-out prefix check too.
STAMP_RE_SRC = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}"
_GNOMON_PLAIN = "[Gnomon]"


def stamp_now(now: Optional[float] = None) -> str:
    """Local-time, 24 h stamp for ``now`` (epoch seconds; default = the wall clock)."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(time.time() if now is None else now))


def stamp_gm_tag(text: str, now: Optional[float] = None) -> str:
    """``[Gnomon] x`` -> ``[Gnomon@<stamp>] x``. Text without the plain
    leading tag (custom operator text, an already-stamped text) is returned unchanged."""
    if text.startswith(_GNOMON_PLAIN):
        return "[Gnomon@" + stamp_now(now) + "]" + text[len(_GNOMON_PLAIN):]
    return text

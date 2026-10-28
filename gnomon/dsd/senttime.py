"""DSD sent-time payload fields (pure, no I/O, no clock).

The record's ``created_epoch`` (float seconds, stamped when the DSD was filed) is the
ONE source of the sent time. ``sent_fields`` turns it into the pair the API carries:
``created_epoch`` (float, or ``None`` when unusable) and ``created_iso`` (UTC ISO-8601
``...Z``, or ``None``). A missing / zero / negative / non-numeric / non-finite epoch
yields ``(None, None)`` - the UI then says "sent time unknown" and never renders an
epoch-zero (1970) date. The browser formats local time + relative age from these
(``web/static/dsd_senttime.js``); the ISO string is the hover text.
"""

from __future__ import annotations

import datetime as _dt
import math
from typing import Any, Dict, Optional

#: 2000-01-01Z; anything earlier is treated as a corrupt stamp, not a real DSD time.
_MIN_PLAUSIBLE = 946684800.0
#: year 9999 bound keeps datetime from raising.
_MAX_PLAUSIBLE = 253402300799.0


def clean_epoch(value: Any) -> Optional[float]:
    """``float`` seconds if ``value`` is a usable epoch, else ``None``."""
    if isinstance(value, bool):
        return None
    try:
        ep = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(ep) or ep < _MIN_PLAUSIBLE or ep > _MAX_PLAUSIBLE:
        return None
    return ep


def iso_utc(epoch: Optional[float]) -> Optional[str]:
    ep = clean_epoch(epoch)
    if ep is None:
        return None
    return (_dt.datetime.fromtimestamp(ep, _dt.timezone.utc)
            .strftime("%Y-%m-%dT%H:%M:%SZ"))


def sent_fields(created_epoch: Any) -> Dict[str, Any]:
    ep = clean_epoch(created_epoch)
    return {"created_epoch": ep, "created_iso": iso_utc(ep)}

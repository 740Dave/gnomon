"""The licence helper: fingerprint + expiry ONLY (requirement-licences).

It returns exactly two values for the file at the licence's path parameter:

* ``fingerprint`` — the sha256 of the file's bytes (16 hex chars shown; the full digest is kept);
* ``expiry`` — an ISO date read from the FIRST line of the form ``<key> <sep> <YYYY-MM-DD>`` where key is one of
  expires / expiry / expiration / expiration_date / valid_until / not_after (case-insensitive), optionally quoted,
  and sep is ``:`` or ``=``; the value may be quoted. This matches a real licence file's JSON-style line
  ``"expiry": "YYYY-MM-DD",`` (round 2, Diamond F9: the format was read from that ONE line only).

Nothing else from the file leaves this module: no line, no field, no substring goes to a log, a status, an audit
entry or an exception message. A file whose expiry cannot be read FAILS CLOSED (the start is refused, licence).
It never consults the graph.

[A, builder] The expiry line format is the builder's choice: the spec says what is read (fingerprint, expiry),
not the licence file's format. A real licence format must be checked against this reader before an app
declares such a licence (listed as an open item in the build report).
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
from dataclasses import dataclass
from typing import Optional

from .fsafe import open_regular

WARN_DAYS = 30
MAX_LICENCE_BYTES = 1024 * 1024
#: ROUND 3c (R3-F12): horizontal whitespace only. The old ``^\s*`` with re.M let every line start consume ALL the
#: newlines after it (quadratic on a newline-heavy file); each line is now matched within itself.
_HWS = rb"[ \t\r\f\v]*"
_EXPIRY_RE = re.compile(
    rb"^" + _HWS + rb"[\"']?(?:expires|expiry|expiration|expiration_date|valid_until|not_after)[\"']?" + _HWS + rb"[:=]" + _HWS +
    rb"[\"']?"
    rb"(\d{4}-\d{2}-\d{2})\b",
    re.I | re.M)


@dataclass(frozen=True)
class LicenceInfo:
    fingerprint: str              # full sha256 hex
    expiry: Optional[_dt.date]

    @property
    def short(self) -> str:
        return self.fingerprint[:16]


class LicenceUnreadable(Exception):
    """Carries only a SAFE reason (never file content)."""


def read_licence(path: str) -> LicenceInfo:
    try:
        st = os.stat(path)
    except OSError:
        raise LicenceUnreadable("the licence file does not exist or cannot be read")
    if not os.path.isfile(path):
        raise LicenceUnreadable("the licence path is not a regular file")
    if st.st_size > MAX_LICENCE_BYTES:
        raise LicenceUnreadable("the licence file is larger than the helper reads")
    try:
        with open_regular(path) as fh:                       # fstat on the open descriptor: a FIFO swapped in never blocks
            data = fh.read(MAX_LICENCE_BYTES + 1)
    except OSError:
        raise LicenceUnreadable("the licence file cannot be read")
    fp = hashlib.sha256(data).hexdigest()
    m = _EXPIRY_RE.search(data)
    expiry = None
    if m:
        try:
            expiry = _dt.date.fromisoformat(m.group(1).decode("ascii"))
        except ValueError:
            expiry = None
    del data
    return LicenceInfo(fp, expiry)


def assess(info: LicenceInfo, today: _dt.date) -> (str, str):  # type: ignore[valid-type]
    """``("ok"|"warn"|"expired"|"unknown", safe message)``."""
    if info.expiry is None:
        return "unknown", f"expiry unreadable (fingerprint {info.short})"
    days = (info.expiry - today).days
    if days < 0:
        return "expired", f"expired {info.expiry.isoformat()} (fingerprint {info.short})"
    if days <= WARN_DAYS:
        return "warn", f"expires {info.expiry.isoformat()}, in {days} day(s) (fingerprint {info.short})"
    return "ok", f"valid until {info.expiry.isoformat()} (fingerprint {info.short})"

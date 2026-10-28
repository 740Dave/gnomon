"""The declared operator status (rule-setter-self-verb, rule-status-storage). Files under the data root, ``<data_root>/open-work/``.

MACHINE-READ FILES (g25), read by every harness:
* ``operator-status.json``      the current status: {schema_version, state, text, setter_id, setter_name, source, set_at, last_in_office_at}
* ``operator-status.prev.json`` the previous GOOD copy, kept beside it (a recovery aid for a person; nothing reads it back automatically)
* ``operator-status.marker.json`` a durable marker written at the FIRST set and refreshed on every set: {first_set_at, last_in_office_at}

THREE KINDS of absence, never confused (the operator: an away status never quietly turns into present):
* NEVER SET: no status file, no previous copy, no marker. Holds are inert, as specified.
* LOST or UNREADABLE: a status was written at some time (marker or previous copy exists) and the file is missing, truncated, garbage, or
  fails validation. The state is ``unknown``. ``unknown`` is NOT in-office: a hold that would be effective under ``away`` stays silent;
  nothing is cleared; the presence line says the status is unreadable; a fault is recorded. Recovery is ONLY by a new declared status.
* A good status: ``in-office`` or ``away``.

Every value is validated at READ as strictly as at WRITE (types, the enum, finite times, bounded and control-free strings, the setter id
shape); a value that fails makes the status unreadable. Nothing from the file reaches a wake-up unvalidated. The DECLARED status and
OBSERVED activity are separate; nothing here reads the beacons.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from . import constants as C
from .safeio import read_regular
from .store import valid_instance_id
from .text import clean_text

OPERATOR_SETTER = "operator"
STATES = ("away", "in-office")
SETTER_NAME_MAX = 40
_TIME_MAX = 1e11   # year ~5138; anything larger is not a time we wrote


@dataclass(frozen=True)
class Status:
    declared: bool = False                 # a status was ever written (also True for lost/unreadable)
    state: str = "in-office"               # "in-office" | "away" | "unknown"
    text: str = ""
    setter_id: str = ""
    setter_name: str = ""
    source: str = ""
    set_at: float = 0.0
    last_in_office_at: float = 0.0         # when the latest in-office declaration was made (0 = never): clears holds
    error: str = ""                        # fixed wording only; never file content
    lost: bool = False                     # a status existed and cannot be read now

    @property
    def away(self) -> bool:
        return self.declared and self.state == "away" and not self.error

    @property
    def unknown(self) -> bool:
        return self.declared and self.state == "unknown"

    @property
    def silences_holds(self) -> bool:
        """Under ``away`` AND under ``unknown`` a hold is effective (unknown is not in-office)."""
        return self.away or self.unknown


def _dir(data_root: str) -> str:
    return os.path.join(data_root, C.DIRNAME)


def status_path(data_root: str) -> str:
    return os.path.join(_dir(data_root), "operator-status.json")


def prev_path(data_root: str) -> str:
    return os.path.join(_dir(data_root), "operator-status.prev.json")


def marker_path(data_root: str) -> str:
    return os.path.join(_dir(data_root), "operator-status.marker.json")


def _finite_time(v: Any) -> bool:
    return (not isinstance(v, bool)) and isinstance(v, (int, float)) and math.isfinite(v) and 0.0 <= float(v) <= _TIME_MAX


def _clean_str(v: Any, limit: int) -> bool:
    return isinstance(v, str) and len(v) <= limit and clean_text(v) == v


def validate_record(d: Any) -> Tuple[Optional[Status], str]:
    """``(Status, "")`` for a valid stored record, else ``(None, <fixed reason naming the field>)``."""
    if not isinstance(d, dict):
        return None, "not an object"
    if d.get("state") not in STATES:
        return None, "state"
    if not _clean_str(d.get("text", ""), C.STATUS_TEXT_MAX):
        return None, "text"
    if not _clean_str(d.get("source", ""), C.STATUS_SOURCE_MAX):
        return None, "source"
    sid = d.get("setter_id", "")
    if not (sid == OPERATOR_SETTER or valid_instance_id(sid)):
        return None, "setter_id"
    if not _clean_str(d.get("setter_name", ""), SETTER_NAME_MAX):
        return None, "setter_name"
    if not _finite_time(d.get("set_at")) or not _finite_time(d.get("last_in_office_at", 0.0)):
        return None, "time"
    return Status(declared=True, state=d["state"], text=d.get("text", ""), setter_id=sid, setter_name=d.get("setter_name", ""),
                  source=d.get("source", ""), set_at=float(d["set_at"]), last_in_office_at=float(d.get("last_in_office_at", 0.0))), ""


def _read_json(path: str) -> Tuple[Optional[Any], str]:
    """``(obj, "")`` | ``(None, "absent")`` | ``(None, "unreadable")``."""
    raw, why = read_regular(path, 64 * 1024)          # non-blocking, regular files only (R3-1)
    if raw is None:
        return None, why
    try:
        return json.loads(raw.decode("utf-8")), ""
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, "unreadable"


def _marker_last_in_office(data_root: str) -> float:
    """The last in-office time kept in the marker or, failing that, the previous copy (0.0 if neither is usable)."""
    m, why = _read_json(marker_path(data_root))
    if why == "" and isinstance(m, dict) and _finite_time(m.get("last_in_office_at", 0.0)):
        return float(m.get("last_in_office_at", 0.0))
    p, why = _read_json(prev_path(data_root))
    if why == "":
        st, bad = validate_record(p)
        if st is not None:
            return st.last_in_office_at
    return 0.0


def read_status(data_root: str) -> Status:
    """The declared status. NEVER raises, never returns an unvalidated value."""
    d, why = _read_json(status_path(data_root))
    if why == "":
        st, bad = validate_record(d)
        if st is not None:
            return st
        return Status(declared=True, state="unknown", lost=True, last_in_office_at=_marker_last_in_office(data_root),
                      error=f"status file failed validation ({bad})")
    existed_before = os.path.exists(marker_path(data_root)) or os.path.exists(prev_path(data_root))
    if why == "absent":
        if existed_before:
            return Status(declared=True, state="unknown", lost=True, last_in_office_at=_marker_last_in_office(data_root),
                          error="status file is missing (a status was declared earlier)")
        return Status()          # never set
    return Status(declared=True, state="unknown", lost=True, last_in_office_at=_marker_last_in_office(data_root),
                  error="status file is unreadable")


def validate_set(state: Any, text: Any, source: Any, *, role_setter: bool) -> Dict[str, Any]:
    """``{"ok": True, "state","text","source"}`` or ``{"ok": False, "error": ...}``. No file is touched. Text and source are cleaned
    (control characters dropped, newlines collapsed to a space) and must then fit the bounds: a call outside them is REFUSED, never cut."""
    if state not in STATES:
        return {"ok": False, "error": "state must be 'away' or 'in-office'"}
    if text is not None and not isinstance(text, str):
        return {"ok": False, "error": "text must be a string"}
    if source is not None and not isinstance(source, str):
        return {"ok": False, "error": "source must be a string"}
    t = clean_text(text or "")
    s = clean_text(source or "")
    if len(t) > C.STATUS_TEXT_MAX:
        return {"ok": False, "error": f"text is over {C.STATUS_TEXT_MAX} characters (it is never cut: shorten it)"}
    if len(s) > C.STATUS_SOURCE_MAX:
        return {"ok": False, "error": f"source is over {C.STATUS_SOURCE_MAX} characters (it is never cut: shorten it)"}
    if role_setter and not s:
        return {"ok": False, "error": "source is required for a role setter: say where the operator's word is recorded "
                                      "(a graph key or a transcript line)"}
    if role_setter and state == "away" and not t:
        return {"ok": False, "error": "text is required for a role setter: quote the operator's own words"}
    return {"ok": True, "state": state, "text": t, "source": s}


def _atomic_json(path: str, obj: Any) -> None:
    d = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".status-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_status(data_root: str, *, state: str, text: str, source: str, setter_id: str, setter_name: str, now: float) -> Status:
    """Declare a new status. Order: marker first (so a crash can never leave a status with no marker), then the previous good copy is
    kept beside the file, then the new file replaces the old atomically."""
    os.makedirs(_dir(data_root), mode=0o700, exist_ok=True)
    prev = read_status(data_root)          # may be unknown/lost: recovery is exactly this new declaration
    last_in = now if state == "in-office" else prev.last_in_office_at
    old, why = _read_json(marker_path(data_root))
    first = float(old["first_set_at"]) if (why == "" and isinstance(old, dict) and _finite_time(old.get("first_set_at"))) else now
    _atomic_json(marker_path(data_root), {"schema_version": C.SCHEMA_VERSION,
                                          "note": "MACHINE-READ (g25): a status was declared at least once; do not delete.",
                                          "first_set_at": first, "last_in_office_at": last_in})
    cur = status_path(data_root)
    good, wh = _read_json(cur)
    if wh == "" and validate_record(good)[0] is not None:
        tmp = prev_path(data_root) + ".tmp"
        shutil.copyfile(cur, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, prev_path(data_root))
    rec = {"schema_version": C.SCHEMA_VERSION, "note": "MACHINE-READ (g25): the operator's declared status; do not edit by hand.",
           "state": state, "text": text, "setter_id": setter_id, "setter_name": setter_name, "source": source,
           "set_at": now, "last_in_office_at": last_in}
    _atomic_json(cur, rec)
    return read_status(data_root)

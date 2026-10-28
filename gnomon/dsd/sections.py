"""- DSD tab master-pane sections (no clock of its own; the only I/O is the lazily-read
deployment role-label table, :mod:`gnomon.role_labels`).

Three sections, keyed on the ANSWER time (never the filing time):

  ``pending``  - every DSD still awaiting the human (incl. non-form payloads).
  ``recent``   - answered within the last 24 h of ``now``; newest answer first.
  ``previous`` - every other answered DSD, grouped by sender, newest answer first.

Answer-time provenance (``answered_source``), most to least trustworthy:

  ``recorded``        - ``answered_epoch`` persisted by ``DsdInbox.mark_answered``
                        at the moment the hub recorded the answer (sub-second).
  ``healed-from-sent``- migration (``heal_answer_time``) set answered_epoch to the
                        DSD's filing time (operator 2026-09-30); APPROXIMATE by design.
  ``derived-marker``  - the record has no ``answered_epoch`` but its markdown
                        carries the write-back marker ``<!-- dsd-status: answered
                        <UTC ISO> -->`` (second precision => APPROXIMATE).
  ``missing``         - neither exists. The time is ``None``; the DSD sorts LAST in
                        ``previous`` and is never placed by its submit time.

Withdrawn / superseded / replaced records appear in no section, EXCEPT (pre-review B1, ``dsd_self_verbs`` on) a
fourth section, ``withdrawn``, below the others, listing the ones an agent withdrew or replaced.
"""

from __future__ import annotations

import calendar
import re
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

WINDOW_SECONDS = 24 * 3600

SECTION_PENDING = "pending"
SECTION_RECENT = "recent"
SECTION_PREVIOUS = "previous"
#: pre-review B1: the DSDs an agent withdrew or replaced; shown (only when the DSD self-verbs are on), never silently gone.
SECTION_MARKED = "withdrawn"
MARKED_KINDS = ("withdrawn", "replaced")

EXCLUDED_STATUSES = ("withdrawn", "superseded", "replaced", "unfinished")

_MARKER_TS_RE = re.compile(
    r"<!--\s*dsd-status:\s*answered\s+(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})Z\s*-->",
    re.IGNORECASE)


def sender_label(instance_id: str) -> str:
    """The short label for a sender: the deployment's role-label table (:mod:`gnomon.role_labels`), else the
    instance id itself (the neutral fallback; X13 S5-3)."""
    from .. import role_labels

    return role_labels.labels().get(instance_id, instance_id)


def is_excluded_status(status: str) -> bool:
    s = (status or "").strip().lower()
    return any(s.startswith(x) for x in EXCLUDED_STATUSES)


def answer_time(answered_epoch: Any, markdown: str = "",
                recorded_source: str = "") -> Tuple[Optional[float], str]:
    """``(epoch | None, source)`` - see the module docstring. Never uses created time."""
    try:
        ep = float(answered_epoch or 0.0)
    except (TypeError, ValueError):
        ep = 0.0
    if ep > 0:
        # "healed-from-sent" = the one-time migration set it to the FILING time
        # (operator-accepted, approximate); anything else stamped is "recorded".
        return ep, ("healed-from-sent" if recorded_source == "healed-from-sent"
                    else "recorded")
    m = _MARKER_TS_RE.search((markdown or "")[:400])
    if m:
        try:
            return float(calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))), \
                "derived-marker"
        except ValueError:
            pass
    return None, "missing"


def section_for(kind: str, answered_epoch: Optional[float], now: float) -> str:
    """``kind`` is the list kind (pending | answered | unrenderable). Exactly 24 h old
    is still 'recent' (inclusive); one tick older is 'previous'. A missing answer
    time, or one in the future (clock skew), is never 'recent' by guess: missing ->
    previous; future -> recent (it was answered 'just now' by the writer's clock)."""
    if kind in MARKED_KINDS:
        return SECTION_MARKED
    if kind != "answered":
        return SECTION_PENDING
    if answered_epoch is None:
        return SECTION_PREVIOUS
    return SECTION_RECENT if (now - answered_epoch) <= WINDOW_SECONDS else SECTION_PREVIOUS


def _sender_rank(label: str, order: Tuple[str, ...] = ()) -> Tuple[int, str]:
    """Sender accordion rank: the deployment's ``order`` first, then every other label alphabetically."""
    return (order.index(label), "") if label in order else (len(order), label)


def order_rows(rows: Sequence[Dict[str, Any]], now: float) -> List[Dict[str, Any]]:
    """Annotate each row dict with ``section`` / ``sender`` and return them in display
    order: pending (newest filed first), recent (newest ANSWER first), previous
    (sender order, then newest ANSWER first, missing answer time last).
    Rows need ``kind``, ``instance_id``, ``created_epoch``, ``answered_epoch``."""
    out: List[Dict[str, Any]] = []
    for r in rows:
        r = dict(r)
        r["sender"] = sender_label(str(r.get("instance_id", "")))
        r["section"] = section_for(str(r.get("kind", "")), r.get("answered_epoch"), now)
        out.append(r)

    def ans_key(r):  # newest answer first; missing last
        ep = r.get("answered_epoch")
        return (ep is None, -(ep or 0.0), str(r.get("dsd_id", "")))

    from .. import role_labels

    order = role_labels.order()
    pend = sorted((r for r in out if r["section"] == SECTION_PENDING),
                  key=lambda r: (-(r.get("created_epoch") or 0.0), str(r.get("dsd_id", ""))))
    rec = sorted((r for r in out if r["section"] == SECTION_RECENT), key=ans_key)
    prev = sorted((r for r in out if r["section"] == SECTION_PREVIOUS),
                  key=lambda r: (_sender_rank(r["sender"], order),) + ans_key(r))
    marked = sorted((r for r in out if r["section"] == SECTION_MARKED),
                    key=lambda r: (-(r.get("created_epoch") or 0.0), str(r.get("dsd_id", ""))))
    return pend + rec + prev + marked

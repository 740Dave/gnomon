"""The DSD INBOX — the READ side of the on-disk DSD store the hub writes.

``send_dsd_for_human_review`` (slice 2a) persists each DSD as
``<agents-root>/<instance_id>/dsd/<dsd_id>.json`` via
:class:`~gnomon.hub.mcp.DSDStore` (the WRITE side). The DSD tab needs the
READ side: enumerate those records for the left-pane list (pending + prior),
read one back for rendering, and flip a record to ``answered`` once the human has
saved — recording a POINTER to the minted root node (D13: answers are stored as
pointers to graph nodes, not cached snapshots), never the full answer text.

This is headless (no CustomTkinter) so the :class:`~gnomon.app.dsd_controller.
DsdTabController` is unit-testable without a display. It re-reads on demand rather
than caching, so a DSD that arrives while the tab is open shows on the next
refresh.
"""

from __future__ import annotations

import contextlib
import glob
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..profile import _validate_instance_id, default_intelligences_root
from .senttime import clean_epoch

STATUS_PENDING = "pending-human-review"
STATUS_ANSWERED = "answered"
#: pre-review B1: the two marks a verb can put on a still-pending record (the record is KEPT, never deleted).
STATUS_WITHDRAWN = "withdrawn"
STATUS_REPLACED = "replaced"
#: pre-review fix round (O-F5): a replacement filed by a replace that LOST to an answer is kept on disk but marked this and never offered.
STATUS_SUPERSEDED = "superseded-by-answer"
#: pre-review fix round (D1): a replacement is FILED in this state (never offered, hidden, not pending) and becomes pending only in the
#: status flip under the lock that names it; a process that dies in between leaves the OLD DSD pending and this one never offered.
STATUS_UNFINISHED = "unfinished-replacement"
STATUS_LOCK_NAME = ".dsd-status.lock"
#: pre-review fix round (D2): held ONLY around one record's read-check-write, by BOTH writers of a record's status (the answer flip and the
#: withdraw / replace marker). Unlike the status lock, nobody gives up waiting for it: two writers can never both pass their check and then
#: both write, so after the answer path stopped waiting for the status lock the marker and the answer still cannot both win.
WRITE_LOCK_NAME = ".dsd-write.lock"
#: how long the ANSWER path waits for the status lock before it records the answer anyway (the answer wins; O-F9).
ANSWER_LOCK_WAIT_SECONDS = 2.0
#: the answer's own record when it could not get the locks in time: ``<dsd_id>.answered`` beside the record (NOT ``*.json``, so no record
#: scan sees it as a DSD). EVERY reader overlays it (status answered, the root pointer), and a marker's re-read sees it, so the answer wins.
ANSWER_SIDECAR_SUFFIX = ".answered"


@contextlib.contextmanager
def status_lock(root: str, timeout: Optional[float] = None):
    """Exclusive flock on ``<root>/.dsd-status.lock``: every change of a record's STATUS (the answer flip in
    :meth:`DsdInbox.mark_answered`, and the withdraw / replace marks of ``dsd_ops``) is made under it, so an answer and a
    withdraw can never interleave their read-modify-write of one record (condition C7). Process- and thread-safe.

    ``timeout=None`` waits as long as it takes. With a ``timeout`` (seconds) it polls without blocking and, when the time is up,
    YIELDS ``False`` without the lock (the caller decides what to do); it yields ``True`` when it holds the lock."""
    import fcntl
    import time as _time

    os.makedirs(root, exist_ok=True)
    fd = os.open(os.path.join(root, STATUS_LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o666)
    got = False
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            got = True
        else:
            end = _time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except OSError:
                    if _time.monotonic() >= end:
                        break
                    _time.sleep(0.02)
        yield got
    finally:
        os.close(fd)


@contextlib.contextmanager
def write_lock(root: str, timeout: Optional[float] = None):
    """Exclusive flock on ``<root>/.dsd-write.lock`` around ONE record's re-read and write (see :data:`WRITE_LOCK_NAME`). ``timeout=None``
    waits; with a ``timeout`` (seconds) it yields ``False`` when it could not get the lock in time, ``True`` when it holds it."""
    import fcntl
    import time as _time

    os.makedirs(root, exist_ok=True)
    fd = os.open(os.path.join(root, WRITE_LOCK_NAME), os.O_RDWR | os.O_CREAT, 0o666)
    got = False
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            got = True
        else:
            end = _time.monotonic() + max(0.0, float(timeout))
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    got = True
                    break
                except OSError:
                    if _time.monotonic() >= end:
                        break
                    _time.sleep(0.02)
        yield got
    finally:
        os.close(fd)


def sidecar_path(record_path: str) -> str:
    return (record_path[:-len(".json")] if record_path.endswith(".json") else record_path) + ANSWER_SIDECAR_SUFFIX


_DSD_ID_RX = re.compile(r"\Adsd-[0-9a-f]{6,32}\Z")


def _load(path: str) -> Optional[Dict[str, Any]]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _overlay_answer(path: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """``raw`` with the answer sidecar applied (status answered and its pointer) when one exists and the record is not already answered."""
    if raw.get("status") == STATUS_ANSWERED:
        return raw
    data = _load(sidecar_path(path))
    if not data or not data.get("root_node_id"):
        return raw
    out = dict(raw)
    out["status"] = STATUS_ANSWERED
    out["root_node_id"] = data["root_node_id"]
    out["answered_epoch"] = data.get("answered_epoch")
    out["answer_time_source"] = "recorded"
    out["answer_summary"] = data.get("answer_summary", "")
    if data.get("written_markdown") is not None:
        dsd = dict(out.get("dsd") or {})
        dsd["markdown"] = data["written_markdown"]
        out["dsd"] = dsd
    if isinstance(data.get("answer_comments"), dict):      # comment-fidelity: the exact text rides the sidecar too
        out["answer_comments"] = data["answer_comments"]
    return out


def effective_raw(path: str, raw: Dict[str, Any]) -> Dict[str, Any]:
    """What every READER sees of a record (pre-review fix round, E1 and E2):
    (1) the answer sidecar, if any, is applied: an answer recorded without the locks is the answer;
    (2) a record that names ``replaces`` is LIVE (pending, offered) only when the record it names points back at it as replaced-by (that
    mark is the SINGLE flip of a replace); until then it is an UNFINISHED replacement, never offered. So a replace that dies at any
    instant leaves exactly one offered DSD. (G4) When the record it names is DELETED, unreadable or corrupt the replacement cannot be shown
    unfinished, so it stays live: the operator is still asked.)"""
    out = _overlay_answer(path, raw)
    old_id = out.get("replaces")
    if out.get("status") == STATUS_PENDING and isinstance(old_id, str) and _DSD_ID_RX.match(old_id):
        old = _load(os.path.join(os.path.dirname(path), old_id + ".json"))
        live = True                                   # G4: an old record that is gone / unreadable / corrupt does not demote the replacement
        if old is not None:
            old = _overlay_answer(os.path.join(os.path.dirname(path), old_id + ".json"), old)
            mark = old.get("replaced") if isinstance(old.get("replaced"), dict) else {}
            live = old.get("status") == STATUS_REPLACED and mark.get("by_dsd_id") == out.get("dsd_id")
        if not live:
            out = dict(out)
            out["status"] = STATUS_UNFINISHED
    return out


@dataclass
class DsdInboxRecord:
    """One persisted DSD record, as the left pane needs it."""

    dsd_id: str
    instance_id: str
    path: str
    status: str = STATUS_PENDING
    created_epoch: float = 0.0
    has_markdown: bool = False
    markdown: str = ""
    root_node_id: str = ""          # set once answered (the pointer, D13)
    answered_epoch: float = 0.0
    #: provenance of answered_epoch - "recorded" (hub stamped it at answer
    #: time) | "healed-from-sent" (one-time migration; = the filing time) | "" (legacy).
    answer_time_source: str = ""
    answer_summary: str = ""        # a short human gloss, NOT the full answer text
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def answered(self) -> bool:
        return self.status == STATUS_ANSWERED

    @property
    def renderable(self) -> bool:
        """A record the DSD tab can render as a form needs DSD markdown."""
        return self.has_markdown and bool(self.markdown.strip())


class DsdInbox:
    """Reads (and status-updates) the on-disk DSD records under an agents
    root. Mirrors :class:`~gnomon.hub.mcp.DSDStore`'s layout exactly."""

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = os.path.abspath(
            os.path.expanduser(root if root is not None else default_intelligences_root()))

    @property
    def root(self) -> str:
        return self._root

    # -- enumerate --------------------------------------------------------------

    def list_records(self) -> List[DsdInboxRecord]:
        """Every DSD record under the root, NEWEST FIRST. A single corrupt JSON
        file is skipped, never blanks the whole list."""
        records: List[DsdInboxRecord] = []
        pattern = os.path.join(self._root, "*", "dsd", "*.json")
        for path in glob.glob(pattern):
            rec = self._read_path(path)
            if rec is not None:
                records.append(rec)
        records.sort(key=lambda r: (r.created_epoch, r.dsd_id), reverse=True)
        return records

    def pending(self) -> List[DsdInboxRecord]:
        return [r for r in self.list_records() if not r.answered and r.status not in (STATUS_WITHDRAWN, STATUS_REPLACED, STATUS_SUPERSEDED, STATUS_UNFINISHED)]

    def answered(self) -> List[DsdInboxRecord]:
        return [r for r in self.list_records() if r.answered]

    def read(self, dsd_id: str) -> Optional[DsdInboxRecord]:
        for rec in self.list_records():
            if rec.dsd_id == dsd_id:
                return rec
        return None

    def read_direct(self, dsd_id: str) -> Optional[DsdInboxRecord]:
        """Look ``dsd_id`` up by its file name (``<root>/*/dsd/<dsd_id>.json``, the
        DSDStore layout) WITHOUT scanning every record. Returns ``None`` for a
        path-unsafe id or no match. Used on the hot DSD-review presence path."""
        if (not isinstance(dsd_id, str) or not dsd_id or dsd_id.startswith(".")
                or any(c in dsd_id for c in "/\\*?[]")):
            return None
        for path in glob.glob(os.path.join(self._root, "*", "dsd", dsd_id + ".json")):
            rec = self._read_path(path)
            if rec is not None and rec.dsd_id == dsd_id:
                return rec
        return None

    # -- status update (answered) ----------------------------------------------

    def mark_answered(self, dsd_id: str, root_node_id: str,
                      *, answer_summary: str = "", answered_epoch: Optional[float] = None,
                      written_markdown: Optional[str] = None,
                      answer_comments: Optional[Dict[str, str]] = None) -> DsdInboxRecord:
        """Flip a record to ``answered`` and record the POINTER to its minted root
        node (D13). Optionally store the written-back markdown so the read-only
        browse view renders exactly what was saved, and ``answer_comments`` (``dec_id -> the comment EXACTLY as
 submitted`` comment-fidelity). Atomic (write-temp-rename).
        Raises ``KeyError`` for an unknown dsd_id.

        The operator's submit never waits behind an agent's marker longer than :data:`ANSWER_LOCK_WAIT_SECONDS` IN TOTAL for the two locks.
        When it cannot get them in time the answer is recorded ANYWAY as the record's answer SIDECAR, which every reader overlays and which a
        marker's re-read (taken under the same locks) sees: the answer wins and cannot be overwritten."""
        import time as _time

        end = _time.monotonic() + ANSWER_LOCK_WAIT_SECONDS
        with status_lock(self._root, timeout=max(0.0, end - _time.monotonic())) as got_s:
            if got_s:
                with write_lock(self._root, timeout=max(0.0, end - _time.monotonic())) as got_w:
                    if got_w:
                        return self._mark_answered_locked(dsd_id, root_node_id, answer_summary=answer_summary,
                                                          answered_epoch=answered_epoch, written_markdown=written_markdown,
                                                          answer_comments=answer_comments)
        return self._record_answer_sidecar(dsd_id, root_node_id, answer_summary=answer_summary,
                                           answered_epoch=answered_epoch, written_markdown=written_markdown,
                                           answer_comments=answer_comments)

    def _find_path(self, dsd_id: str) -> Optional[str]:
        for p in glob.glob(os.path.join(self._root, "*", "dsd", "*.json")):
            raw = _load(p)
            if raw is not None and str(raw.get("dsd_id") or _basename_id(p)) == dsd_id:
                return p
        return None

    def _record_answer_sidecar(self, dsd_id: str, root_node_id: str, *, answer_summary: str,
                               answered_epoch: Optional[float], written_markdown: Optional[str],
                               answer_comments: Optional[Dict[str, str]] = None) -> DsdInboxRecord:
        import time

        path = self._find_path(dsd_id)
        if path is None:
            raise KeyError(f"no DSD record for {dsd_id!r}")
        data = {"dsd_id": dsd_id, "root_node_id": root_node_id, "answer_summary": answer_summary,
                "answered_epoch": answered_epoch if answered_epoch is not None else time.time(),
                "written_markdown": written_markdown, "recorded_without_locks": True}
        if answer_comments is not None:
            data["answer_comments"] = dict(answer_comments)
        self._atomic_write_json(sidecar_path(path), data)
        return self._read_path(path)  # type: ignore[return-value]

    def reconcile(self, path: str) -> None:
        """Fold a record's answer sidecar INTO the record (drop a stale withdraw / replace mark, supersede a replacement) and remove it.
        The caller holds the write lock. A no-op when there is no sidecar."""
        data = _load(sidecar_path(path))
        raw = _load(path)
        if data is None or raw is None or not data.get("root_node_id"):
            return
        if raw.get("status") == STATUS_ANSWERED:
            # G2: an already-answered record is the answer of record: it is left ALONE (not rewritten from the sidecar); only the sidecar goes
            try:
                os.unlink(sidecar_path(path))
            except OSError:
                pass
            return
        self._apply_answer(path, raw, data["root_node_id"], data.get("answer_summary", ""), data.get("answered_epoch"),
                           data.get("written_markdown"),
                           data.get("answer_comments") if isinstance(data.get("answer_comments"), dict) else None)
        try:
            os.unlink(sidecar_path(path))
        except OSError:
            pass

    def _mark_answered_locked(self, dsd_id: str, root_node_id: str, *, answer_summary: str,
                              answered_epoch: Optional[float], written_markdown: Optional[str],
                              answer_comments: Optional[Dict[str, str]] = None) -> DsdInboxRecord:
        path = self._find_path(dsd_id)
        if path is None:
            raise KeyError(f"no DSD record for {dsd_id!r}")
        raw = _load(path)
        if raw is None:
            raise KeyError(f"no DSD record for {dsd_id!r}")
        rec = self._apply_answer(path, raw, root_node_id, answer_summary, answered_epoch, written_markdown, answer_comments)
        try:
            os.unlink(sidecar_path(path))                  # an earlier answer recorded without the locks is superseded by this one
        except OSError:
            pass
        return rec

    def _apply_answer(self, path: str, stored: Dict[str, Any], root_node_id: str, answer_summary: str,
                      answered_epoch: Optional[float], written_markdown: Optional[str],
                      answer_comments: Optional[Dict[str, str]] = None) -> DsdInboxRecord:
        import time

        raw = dict(stored)
        dsd_id = str(raw.get("dsd_id") or _basename_id(path))
        was = raw.get("status")
        if was in (STATUS_WITHDRAWN, STATUS_REPLACED):
            # An ANSWER WINS (C7): a withdraw / replace that landed between the answer's mint and this flip is overridden. The mark is
            # dropped, the override is RECORDED, and a replacement the losing replace filed is marked superseded and never offered.
            old = raw.pop(was, None)
            raw["answer_overrode"] = {"was": was, "mark": old, "at_epoch": time.time()}
            new_id = (old or {}).get("by_dsd_id") if isinstance(old, dict) else None
            if was == STATUS_REPLACED and isinstance(new_id, str) and _DSD_ID_RX.match(new_id):
                self._supersede(os.path.join(os.path.dirname(path), new_id + ".json"), dsd_id)
        raw["status"] = STATUS_ANSWERED
        raw["root_node_id"] = root_node_id
        raw["answered_epoch"] = answered_epoch if answered_epoch is not None else time.time()
        raw["answer_time_source"] = "recorded"   # precise, stamped at answer time
        raw["answer_summary"] = answer_summary
        if written_markdown is not None:
            dsd = dict(raw.get("dsd") or {})
            dsd["markdown"] = written_markdown
            raw["dsd"] = dsd
        if answer_comments is not None:
            raw["answer_comments"] = dict(answer_comments)   # comment-fidelity: the exact text
        self._atomic_write_json(path, raw)
        return self._read_path(path)  # type: ignore[return-value]

    def _supersede(self, path: str, answered_dsd_id: str) -> None:
        """Mark a still-pending replacement ``superseded-by-answer`` (kept, hidden, never offered). Never raises."""
        import time

        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict) and raw.get("status") == STATUS_PENDING:
                raw["status"] = STATUS_SUPERSEDED
                raw["superseded"] = {"by_answer_to": answered_dsd_id, "at_epoch": time.time()}
                self._atomic_write_json(path, raw)
        except (OSError, ValueError):
            pass

    # -- internals --------------------------------------------------------------

    def _read_path(self, path: str, overlay: bool = True) -> Optional[DsdInboxRecord]:
        raw = _load(path)
        if raw is None:
            return None
        if overlay:
            raw = effective_raw(path, raw)
        dsd = raw.get("dsd") if isinstance(raw.get("dsd"), dict) else {}
        markdown = dsd.get("markdown") if isinstance(dsd.get("markdown"), str) else ""
        dsd_id = raw.get("dsd_id") or _basename_id(path)
        instance_id = raw.get("instance_id") or _instance_from_path(path, self._root)
        return DsdInboxRecord(
            dsd_id=str(dsd_id),
            instance_id=str(instance_id),
            path=path,
            status=str(raw.get("status", STATUS_PENDING)),
            created_epoch=_safe_float(raw.get("created_epoch")),
            has_markdown=bool(markdown),
            markdown=markdown or "",
            root_node_id=str(raw.get("root_node_id", "") or ""),
            answered_epoch=_safe_float(raw.get("answered_epoch")),
            answer_time_source=str(raw.get("answer_time_source", "") or ""),
            answer_summary=str(raw.get("answer_summary", "") or ""),
            raw=raw,
        )

    @staticmethod
    def _atomic_write_json(path: str, obj: Dict[str, Any]) -> None:
        d = os.path.dirname(path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".dsd-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _safe_float(v: Any) -> float:
    """A hand-edited / corrupt epoch must not crash the whole inbox read (a bare
    ``float("garbage")`` raised out of ``_read_path``); bad -> 0.0 (= unknown, the same
    value a MISSING epoch gets, so the newest-first sort treats it as missing). NaN / +-inf
    AND any finite value outside the plausible-epoch window (``senttime.clean_epoch``: the
    ONE source of the bounds, the same test the API/UI use for "sent time unknown") are not
    usable either: a raw 1e300 would sort FIRST while the UI says "unknown"."""
    ep = clean_epoch(v or 0.0)
    return 0.0 if ep is None else ep


def _basename_id(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def _instance_from_path(path: str, root: str) -> str:
    # <root>/<instance_id>/dsd/<file>.json
    rel = os.path.relpath(path, root)
    parts = rel.split(os.sep)
    return parts[0] if parts and parts[0] != ".." else ""

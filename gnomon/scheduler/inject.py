"""The ONE module that puts text toward a session's input (requirement-delivery-path;
checklist proposal one-inject-path). Everything else in the scheduler package reaches a session only through
:func:`send`, and a test (tests/test_scheduler_stage1a_inject_path.py) fails if any other scheduler module touches
``registry.route``, a pty, a writer or a socket downlink.

``send`` calls ``SessionRegistry.route(instance_id, frame)`` in-process, like gnomon/dsd/roundtrip.py does.
The harness then delivers through Actuator.inject -> Injector.inject_line -> InjectionGate.decide; the frame is
recorded with source ``bus`` (a hub frame cannot claim another) and the unique trigger, and it is NEVER sent through
the operator's exempt console path.

This module also READS a session's ``.claude/injection_log.jsonl`` (compaction ordering and delivery evidence). The
reads are bounded: the delivery reader keeps a per-run byte offset and reads only new bytes; the compaction check
reads backwards from the end in growing chunks (R8: never the whole log each tick).
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, List, Optional, Tuple

from .vocab import FRAME_TRIGGER_PREFIX

LOG_RELATIVE = os.path.join(".claude", "injection_log.jsonl")
_CHUNKS = (512 * 1024, 4 * 1024 * 1024)      # the compaction check never reads more than 4 MB (P3e)
_MAX_READ = 8 * 1024 * 1024


class LogUnreadable(Exception):
    """The session's injection log cannot be read (missing, not a regular file, no session_dir). NEVER treated as
    'delivered' and never as 'clear to send': the caller waits and flags."""


def frame_trigger(job_id: str, run_id: str) -> str:
    return f"{FRAME_TRIGGER_PREFIX}{job_id}:{run_id}"


def make_frame(text: str, trigger: str) -> Dict[str, Any]:
    return {"cmd": "inject", "text": text, "clear": False, "trigger": trigger}


def send(registry: Any, instance_id: str, frame: Dict[str, Any]) -> None:
    """THE send. Raises the registry's UnknownSession, or OSError for a dead socket; the caller turns either into
    SKIPPED(target-not-live). Returns None on success (the hub accepted the frame: SENT, not DELIVERED)."""
    registry.route(instance_id, frame)


def harness_screen(text: str) -> Optional[List[str]]:
    """The harness's OWN hub-inject screen (control bytes, an operator ``\\command`` line, a leading ``!``), applied
    here so a proposal that the harness would refuse is refused LOUDLY at proposal and again just before a send,
    instead of becoming a run that stays SENT. One predicate, not a copy (g16). Fails CLOSED: if the screen cannot be
    loaded the text is refused."""
    try:
        from ..harness.compact_gate import screen_hub_inject

        return screen_hub_inject(text)
    except Exception as exc:  # noqa: BLE001
        return [f"screen-unavailable ({type(exc).__name__})"]


def expected_hashes(text: str) -> Tuple[str, ...]:
    """The injector may add ONE space after a trailing backslash, so the recorded hash can be of either form."""
    base = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
    if text.endswith("\\"):
        return (base, hashlib.sha256((text + " ").encode("utf-8", "surrogateescape")).hexdigest())
    return (base,)


def session_log_path(session_dir: Any) -> str:
    if not isinstance(session_dir, str) or not session_dir.strip() or not os.path.isabs(session_dir):
        raise LogUnreadable("the registry has no session_dir for this instance")
    return os.path.join(session_dir, LOG_RELATIVE)


def _parse(chunk: bytes, drop_first: bool) -> List[Dict[str, Any]]:
    lines = chunk.split(b"\n")
    if drop_first and lines:
        lines = lines[1:]          # the first line of a mid-file chunk is probably partial
    out: List[Dict[str, Any]] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            rec = json.loads(ln.decode("utf-8", "replace"))
        except ValueError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out


def _open_log(path: str):
    fd = None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        st = os.fstat(fd)
        import stat as _st

        if not _st.S_ISREG(st.st_mode):
            raise LogUnreadable(f"{path} is not a regular file")
        fh = os.fdopen(fd, "rb")
        fd = None
        return fh, st.st_size
    except FileNotFoundError:
        raise LogUnreadable(f"{path} does not exist") from None
    except OSError as exc:
        raise LogUnreadable(f"{path}: {exc}") from None
    finally:
        if fd is not None:
            os.close(fd)


def log_size(path: str) -> int:
    fh, size = _open_log(path)
    fh.close()
    return size


def compaction_state(path: str, now: Optional[float] = None) -> Tuple[str, str]:
    """``("clear"|"compacting", detail)`` per requirement-compaction-ordering: compacting iff
    the most recent ``kind: lifecycle`` record (other than gnomon-relaunch's own ``point: relaunch``) is a compact
    (``point: compact``, phase pre or post) not yet followed by a ``kind: post-compact-boot`` record with decision
    ``written``. STALENESS CAP (owner ruling 2026-10-09): the check applies only while that compact event is
    less than ``compaction_stale_s`` (30 min) old by its recorded ``ts``; an older compact never holds an inject (some
    historical logs have compacts with no boot record). Raises :class:`LogUnreadable` for an unreadable log."""
    fh, size = _open_log(path)
    with fh:
        for chunk in _CHUNKS:
            start = max(0, size - chunk)
            fh.seek(start)
            recs = _parse(fh.read(size - start), drop_first=start > 0)
            last_i = None
            for i in range(len(recs) - 1, -1, -1):
                r = recs[i]
                if r.get("kind") == "lifecycle" and r.get("point") != "relaunch":
                    last_i = i
                    break
            if last_i is not None or start == 0:
                break
    if last_i is None:
        return "clear", "no lifecycle record in the last 4 MB of the log (a compact that old is past the 30-minute cap)"
    last = recs[last_i]
    if last.get("point") != "compact":
        return "clear", f"the last lifecycle event is {last.get('point')!r}"
    phase = (last.get("payload") or {}).get("phase") if isinstance(last.get("payload"), dict) else None
    import time as _time
    from .limits import LIMITS

    t = _time.time() if now is None else float(now)
    ts = last.get("ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and t - ts >= LIMITS["compaction_stale_s"]:
        return "clear", f"the last compact is {int(t - ts)} s old (older than the {LIMITS['compaction_stale_s']} s cap)"
    for r in recs[last_i + 1:]:
        if r.get("kind") == "post-compact-boot" and r.get("decision") == "written":
            return "clear", "the post-compact boot instruction was written"
    return "compacting", f"the last lifecycle event is a compact (phase {phase}) with no post-compact-boot 'written' after it"


def read_new(path: str, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """Records appended since ``offset`` (complete lines only) and the new offset. A log that shrank restarts from 0.
    Raises :class:`LogUnreadable`."""
    fh, size = _open_log(path)
    with fh:
        if offset > size:
            offset = 0
        if size == offset:
            return [], offset
        fh.seek(offset)
        data = fh.read(min(size - offset, _MAX_READ))
    end = data.rfind(b"\n")
    if end < 0:
        return [], offset
    return _parse(data[: end + 1], drop_first=False), offset + end + 1


def classify_delivery(records: List[Dict[str, Any]], trigger: str, hashes: Tuple[str, ...]) -> Dict[str, Any]:
    """Read ONLY what the log shows for this run's unique trigger.

    Returns ``{"state": "none"|"held"|"delivered"|"refused"|"effect-failed", "evidence": {...}}``. ``delivered`` needs
    an ``injection`` record for the trigger with decision allow or release AND a content_sha256 in ``hashes``;
    ``evidence.effect`` is ``completed`` / ``failed`` / ``absent`` from the effect ledger when it is on. A defer
    with no later release is ``held``."""
    state, ev = "none", {}  # type: str, Dict[str, Any]
    held_id = None
    for r in records:
        if r.get("kind") == "injection" and r.get("trigger") == trigger:
            dec = r.get("decision")
            sha = r.get("content_sha256")
            if dec == "defer":
                held_id = r.get("held_id")
                if state != "delivered":
                    state, ev = "held", {"held_id": held_id, "handler": r.get("handler"), "seq": r.get("seq")}
            elif dec in ("allow", "release"):
                if sha in hashes:
                    state, ev = "delivered", {"decision": dec, "seq": r.get("seq"), "held_id": r.get("held_id"),
                                              "content_sha256": sha, "ts_iso": r.get("ts_iso")}
                else:
                    state, ev = "refused", {"decision": dec, "reason": "the recorded content hash differs from the "
                                            "text that was sent", "recorded": sha}
            elif dec in ("deny", "refuse", "refused", "reject", "drop"):
                state, ev = "refused", {"decision": dec, "reason": r.get("reason"), "seq": r.get("seq")}
    if state == "delivered":
        eff = "absent"
        for r in records:
            if r.get("kind") == "effect" and r.get("effect_trigger") == trigger:
                if r.get("phase") == "failed":
                    eff = "failed"
                elif r.get("phase") == "completed" and eff != "failed":
                    eff = "completed"
        ev["effect"] = eff
        if eff == "failed":
            state = "effect-failed"
    return {"state": state, "evidence": ev}

"""The effect ledger — flag ``effect_ledger``.

Every harness-mediated write to the child's PTY (an *effect*) is bracketed by TWO
provenance records sharing one ``effect_id`` (the correlation id):

* ``phase: started``   — written BEFORE the bytes are emitted;
* ``phase: completed`` — written after the emit returned, or
  ``phase: failed``    — written (then the exception re-raised) if it raised.

A crash between the two (text half-typed, ESC-ESC wiped, process killed) leaves a
``started`` with no closing record. That is an **unresolved** effect: it must NEVER
be read as done. :func:`unresolved_started` finds them; the CLI
(``python3 -m gnomon.harness.effect_ledger unresolved <session_dir>``) lists
them.

Sits beside D37's fail-loud layer (``compact_gate`` / ``confirm_compact``): that layer
confirms a *compact* landed; this ledger records that *any* injection was begun and
whether it finished. They are complementary, not shared.

Where it lives: NEW record kind ``effect`` in the EXISTING per-session
``<session_dir>/.claude/injection_log.jsonl`` (same ``ProvenanceLog``, same ``seq``,
same O_APPEND whole-line discipline). No new file: the log is already per-session,
backed up with the session dir, and every reader tolerates an unknown kind.

MACHINE-READ interface (checklist g25). The record schema below is read by
:func:`read_effects` / :func:`unresolved_started` and the CLI, and is a contract:

    kind            "effect"
    phase           "started" | "completed" | "failed"      (closed set, PHASES)
    effect_id       32-hex correlation id, one per effect (started/closed share it)
    effect_route    "decide" | "release" | "restore"   (which path emitted it)
    effect_source   the event's provenance source   (NOT named ``source``: usage_metrics
    effect_trigger  the event's trigger              would count the ledger as usage)
    decision_seq    seq of the ``injection`` decision record this effect belongs to
    held_id         (release route) the held id being released
    content_len / content_sha256   the event content (never the text: no preview)
    started_seq     (closing records) seq of the ``started`` record
    duration_ms     (closing records) monotonic ms between started and closing; >= 0
    error           (failed) ``"<ExceptionType>: <message[:200]>"``

Change the schema only together with the readers here (and g25/g27x in the checklist).
The ledger NEVER breaks or blocks an injection: a ledger write fault is counted
(:attr:`InjectionGate.effect_ledger_errors`) and reported once on stderr; the effect
still runs (a full disk must not lock the operator out — same rule as ProvenanceLog).
A REAL fault is an ``OSError`` that ``ProvenanceLog.append`` swallows (it only bumps
its own ``write_errors``); the gate detects it by that counter moving around each
ledger write, so the ledger counter is live on the realistic path.

KNOWN LIMITS (say so, don't hide them):
* FALSE-UNRESOLVED: if the ``started`` write succeeds, the emit succeeds, and the CLOSING
  write then fails, the effect reads as unresolved though it completed. That is the safe,
  loud direction; ``effect_ledger_errors`` > 0 (and the one stderr line) tells it apart
  from a genuine crash.
* If the ``started`` write itself fails, a later crash leaves NO trace on disk (the
  in-memory mirror only). Inherent to a file ledger; the same counter flags it.
* ``seq``/``started_seq``/``decision_seq`` are per-process; ``effect_id`` is the key.
* ``unresolved`` with the default ``--min-age 0`` also lists an effect legitimately in
  flight (the emit sleeps between phases); use ``--min-age`` for a live session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Optional

FLAG_NAME = "effect_ledger"
RECORD_KIND = "effect"
PHASE_STARTED = "started"
PHASE_COMPLETED = "completed"
PHASE_FAILED = "failed"
PHASES = frozenset({PHASE_STARTED, PHASE_COMPLETED, PHASE_FAILED})
ROUTE_DECIDE = "decide"
ROUTE_RELEASE = "release"
#: harness text typed outside a gate decision (optap.restore_unsent, the D50 line restore).
ROUTE_RESTORE = "restore"
ROUTES = frozenset({ROUTE_DECIDE, ROUTE_RELEASE, ROUTE_RESTORE})

#: Bounds for the ONLY duration parameter this feature has (g27b (h)): the age below
#: which an unresolved ``started`` is presumed still IN FLIGHT and therefore not
#: reported. 0 is meaningful (report everything); ceiling 24h.
MIN_AGE_FLOOR_SECONDS = 0.0
MIN_AGE_CEILING_SECONDS = 86400.0

_ERROR_CHARS = 200


def new_effect_id() -> str:
    return uuid.uuid4().hex


def _content_fields(text: Optional[str]) -> Dict[str, Any]:
    if text is None:
        return {"content_len": None, "content_sha256": None}
    return {"content_len": len(text),
            "content_sha256": hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()}


def run_effect(
    record: Callable[..., Any],
    event: Any,
    emit: Callable[[Any], None],
    *,
    route: str,
    decision_seq: Optional[int] = None,
    held_id: Optional[int] = None,
    on_ledger_error: Optional[Callable[[BaseException], None]] = None,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Run ``emit(event)`` bracketed by started / completed|failed ledger records.

    ``record(kind, **fields)`` is the gate's record method. ``emit``'s own exception
    propagates unchanged (after the ``failed`` record); a ledger fault never does.
    """
    effect_id = new_effect_id()
    base = {"effect_id": effect_id, "effect_route": route,
            "effect_source": getattr(event, "source", None),
            "effect_trigger": getattr(event, "trigger", None),
            "decision_seq": decision_seq}
    if held_id is not None:
        base["held_id"] = held_id
    started_seq = None

    def _ledger(phase: str, **extra: Any) -> Any:
        try:
            return record(RECORD_KIND, phase=phase, **base, **extra)
        except Exception as exc:  # noqa: BLE001 - the ledger never breaks an injection
            if on_ledger_error is not None:
                try:
                    on_ledger_error(exc)
                except Exception:  # noqa: BLE001
                    pass
            return None

    rec = _ledger(PHASE_STARTED, **_content_fields(getattr(event, "content", None)))
    if isinstance(rec, dict):
        started_seq = rec.get("seq")
    t0 = clock()

    def _elapsed_ms() -> float:
        return max(0.0, round((clock() - t0) * 1000.0, 3))

    try:
        emit(event)
    except BaseException as exc:
        msg = f"{type(exc).__name__}: {exc}"[:_ERROR_CHARS]
        _ledger(PHASE_FAILED, started_seq=started_seq, duration_ms=_elapsed_ms(), error=msg)
        raise
    _ledger(PHASE_COMPLETED, started_seq=started_seq, duration_ms=_elapsed_ms())


# --- the query helper ----------------------------------------------------------


def log_path(session_dir: str) -> str:
    from .hooks import injection_log_path

    return injection_log_path(session_dir)


def read_effects(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The ledger records (kind ``effect`` with a valid phase) from ``records``."""
    return [r for r in records if isinstance(r, dict) and r.get("kind") == RECORD_KIND
            and r.get("phase") in PHASES and isinstance(r.get("effect_id"), str)]


def read_session_log(session_dir: str) -> List[Dict[str, Any]]:
    """Every parseable record of the session's injection_log.jsonl. A missing log
    RAISES ``FileNotFoundError`` (never an empty answer that reads as 'all clear');
    a torn/garbled line raises ``ValueError`` naming the line number."""
    path = log_path(session_dir)
    out: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise ValueError(f"{path}:{n}: unparseable record ({exc})") from exc
    return out


def read_session_log_lenient(session_dir: str) -> "tuple[List[Dict[str, Any]], List[int]]":
    """Like :func:`read_session_log` but a torn line does not stop the read: returns
    ``(intact_records, torn_line_numbers)``. A missing log still raises."""
    path = log_path(session_dir)
    out: List[Dict[str, Any]] = []
    torn: List[int] = []
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                torn.append(n)
    return out, torn


def check_min_age(min_age_seconds: float) -> float:
    try:
        v = float(min_age_seconds)
    except (TypeError, ValueError):
        raise ValueError(f"min_age_seconds must be a number, got {min_age_seconds!r}") from None
    if not (MIN_AGE_FLOOR_SECONDS <= v <= MIN_AGE_CEILING_SECONDS) or v != v:
        raise ValueError(f"min_age_seconds must be within [{MIN_AGE_FLOOR_SECONDS:g}, "
                         f"{MIN_AGE_CEILING_SECONDS:g}], got {min_age_seconds!r}")
    return v


def unresolved_started(records: Iterable[Dict[str, Any]], *, min_age_seconds: float = 0.0,
                       now: Optional[float] = None) -> List[Dict[str, Any]]:
    """``started`` records with NO ``completed``/``failed`` sharing their effect_id.

    ``min_age_seconds`` (bounded, see :func:`check_min_age`) skips a ``started``
    younger than that: it may simply still be in flight. Default 0 = report all.
    Each result is the started record plus ``age_seconds`` (None if no ``ts``).
    """
    min_age = check_min_age(min_age_seconds)
    eff = read_effects(records)
    closed = {r["effect_id"] for r in eff if r["phase"] in (PHASE_COMPLETED, PHASE_FAILED)}
    now = time.time() if now is None else now
    out = []
    for r in eff:
        if r["phase"] != PHASE_STARTED or r["effect_id"] in closed:
            continue
        ts = r.get("ts")
        age = round(now - ts, 3) if isinstance(ts, (int, float)) else None
        if age is not None and age < min_age:
            continue
        out.append({**r, "age_seconds": age})
    return out


def session_effects(session_dir: str, *, instance_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """All ledger records for a session, in log order (optionally one instance_id)."""
    eff = read_effects(read_session_log(session_dir))
    return [r for r in eff if instance_id is None or r.get("instance_id") == instance_id]


def session_unresolved(session_dir: str, *, min_age_seconds: float = 0.0,
                       now: Optional[float] = None) -> List[Dict[str, Any]]:
    return unresolved_started(read_session_log(session_dir), min_age_seconds=min_age_seconds, now=now)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="effect_ledger",
                                 description="Query the effect ledger of a session.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("unresolved", "list started effects with no completed/failed (exit 1 if any)"),
                        ("list", "list every ledger record")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("session_dir")
        if name == "unresolved":
            p.add_argument("--min-age", type=float, default=0.0,
                           help=f"skip effects younger than N seconds "
                                f"[{MIN_AGE_FLOOR_SECONDS:g}..{MIN_AGE_CEILING_SECONDS:g}]")
    args = ap.parse_args(argv)
    try:
        records, torn = read_session_log_lenient(args.session_dir)
        if args.cmd == "list":
            rows = read_effects(records)
        else:
            rows = unresolved_started(records, min_age_seconds=args.min_age)
    except (OSError, ValueError) as exc:
        sys.stderr.write(f"effect_ledger: ERROR {exc}\n")
        return 2
    for r in rows:
        if args.cmd == "list":
            print(json.dumps(r, sort_keys=True))
        else:
            print(json.dumps({k: r.get(k) for k in ("effect_id", "seq", "ts_iso", "age_seconds", "effect_route",
                                                    "effect_source", "effect_trigger", "content_len")},
                             sort_keys=True))
    if torn:  # what parsed was still printed above; the torn lines are reported and win the exit code
        sys.stderr.write(f"effect_ledger: ERROR torn/unparseable line(s) {torn} in "
                         f"{log_path(args.session_dir)} (intact records listed above)\n")
        return 2
    return 1 if (args.cmd == "unresolved" and rows) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

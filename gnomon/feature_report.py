"""the BUILT / VALUED / UNUSED report (spec).

``python -m gnomon.usage_metrics report --features [--since D] [--until D] [--json]
[--registry DIR] [--include-probes] [--min-sessions N]`` joins the usage DB's ``feature_events`` table
to the feature registry (:mod:`gnomon.feature_registry`) and the static call-site scan
(:mod:`gnomon.feature_sites`), and says per feature whether it is built, valued, unused,
underused, unrated and so on, judged ONLY against the registry's WRITTEN ``expected-rate`` (the operator's
 choice; the report never infers, adjusts or smooths a rate), with the
DENOMINATOR on every row and :data:`usage_metrics.SIGNAL_LABEL` ('signal -- investigate'), never a
defect wording.

Status vocabulary (one status per feature; no feature appears in two flags)::

    registered-not-wired   a registry node, but the static scan finds no call site for its id
    unobservable           an INVISIBLE class (subagent / skill dispatch is not measured; g23)
    emitter-off            zero events, and the emitter flag was not ON for the whole window
    wired-never-emitted    wired, zero events, emitter ON the whole window, and NOT valued
    window-too-short       valued, but the window is shorter than one unit of the expected rate
    unused                 valued AND zero events in the window (a zero, with its denominator)
    underused              valued AND observed rate < expected rate
    unrated                wired, expected-rate EMPTY, but events exist (never flagged unused)
    as-expected            valued, observed rate >= expected rate
    unregistered-emitting  events seen for a feature_id with no registry node (a g1 violation, caught live)

Precedence, top to bottom: registered-not-wired, unobservable, emitter-off (zero events only),
then valued features by window-too-short / unused / underused / as-expected, then unvalued by
wired-never-emitted (zero events) / unrated (events). The spec leaves the overlap of ``unused``,
``unrated`` and ``wired-never-emitted`` for a zero-event feature UNRULED; this precedence is the
builder's reading, isolated in :func:`classify` so a ruling is a one-function change.

Events with outcome ``noop`` are NEVER usage: they appear only in the ``hook_noops`` section. Probe
events (``GNOMON_PROBE=1``) are excluded by default and counted per actor.

Neutral (g3): no deployment identifier.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from . import usage_metrics as um
from .feature_events import DROPPED_FEATURE_ID, RESERVED_FEATURE_IDS
from .feature_registry import RATE_UNITS, RegistryNode, load_registry

REPORT_NAME = "feature usage"
#: surface classes whose invocations the hooks / static scan cannot see yet: INVISIBLE, never unused (g23)
INVISIBLE_SURFACES = frozenset({"subagent", "skill"})
INVISIBLE_REASON = ("INVISIBLE class: the harness has not yet measured which subagent / skill dispatches a hook can "
                    "see (spec evidence-dispatch-visibility is EMPTY), so a zero here is "
                    "not a finding")

FLAGGED = frozenset({"registered-not-wired", "unused", "underused", "wired-never-emitted",
                     "unregistered-emitting"})
UNOBSERVABLE = frozenset({"unobservable", "emitter-off"})


# --- emitter flag evidence ---------------------------------------------------------------------------------


def flag_log(con: sqlite3.Connection) -> List[Tuple[float, bool]]:
    row = con.execute("SELECT value FROM meta WHERE key=?", (um.META_FLAG_LOG,)).fetchone()
    try:
        return sorted((float(t), bool(s)) for t, s in (json.loads(row[0]) if row else []))
    except (ValueError, TypeError):
        return []


def emitter_on_whole_window(log: Sequence[Tuple[float, bool]], lo: Optional[float], hi: Optional[float]) -> Optional[bool]:
    """True iff the emitter flag was ON at every sampled point of [lo, hi]; False if any sample in (or
    governing) the window was OFF; None when it was never sampled (no evidence, never a guess). The state
    before the first sample is taken to be the first sample's state (steady-state assumption, stated)."""
    if not log:
        return None
    lo = lo if lo is not None else log[0][0]
    hi = hi if hi is not None else log[-1][0]
    state_at_lo = log[0][1]
    for t, st in log:
        if t <= lo:
            state_at_lo = st
    if not state_at_lo:
        return False
    return all(st for t, st in log if lo < t <= hi)


# --- classification ----------------------------------------------------------------------------------------


def classify(node: RegistryNode, *, wired: bool, events: int, units: Optional[float], observed: Optional[float],
             emitter_on: Optional[bool]) -> str:
    """The ONE place the status precedence lives (see the module docstring)."""
    if not wired:
        if set(node.surfaces) <= INVISIBLE_SURFACES:
            return "unobservable"
        return "registered-not-wired"
    if set(node.surfaces) <= INVISIBLE_SURFACES:
        return "unobservable"
    if events == 0 and emitter_on is not True:
        return "emitter-off"
    if node.valued:
        if units is not None and units < 1:
            return "window-too-short"
        if events == 0:
            return "unused"
        assert node.rate is not None and observed is not None
        return "underused" if observed < node.rate[0] else "as-expected"
    return "wired-never-emitted" if events == 0 else "unrated"


def _iso(ts: Optional[float]) -> Optional[str]:
    if ts is None:
        return None
    return _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _units(node: RegistryNode, lo: Optional[float], hi: Optional[float], sessions: int) -> Optional[float]:
    """Whole units of the expected rate in the window: floor(seconds / unit) for day/week; the number of
    sessions for ``session``. None for an unrated node or an unbounded window."""
    if node.rate is None:
        return None
    unit = node.rate[1]
    if unit == "session":
        return float(sessions)
    if lo is None or hi is None:
        return 0.0
    return float(math.floor(max(0.0, hi - lo) / RATE_UNITS[unit]))


# --- the report ---------------------------------------------------------------------------------------------


def peek_schema_version(db_path: str) -> Optional[str]:
    """The DB's stamped schema version WITHOUT touching it (no rebuild), or None (no DB / no stamp)."""
    if not os.path.exists(db_path):
        return None
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return row[0] if row else None
        finally:
            con.close()
    except sqlite3.Error:
        return None


def build_feature_report(db_path: Optional[str] = None, registry_dir: Optional[str] = None, *,
                         since: Optional[str] = None, until: Optional[str] = None,
                         min_sessions: int = um.DEFAULT_MIN_SESSIONS, include_probes: bool = False,
                         nodes: Optional[List[RegistryNode]] = None, registry_source: Optional[str] = None,
                         wired_ids: Optional[Set[str]] = None, now: Optional[float] = None,
                         index_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Build the report. Raises :class:`~gnomon.feature_registry.RegistryError` (CLI exit 2) on an
    unreadable or empty registry, ``ValueError`` on an unparseable --since/--until."""
    db_path = db_path or um.default_db_path()
    now = time.time() if now is None else now
    lo_req, hi_req = um._parse_when(since), um._parse_when(until)
    if nodes is None:
        nodes, registry_source = load_registry(registry_dir, with_source=True)
    dynamic: List[Dict[str, Any]] = []
    if wired_ids is None:
        wired_ids, dynamic = _scan()
    con = um.connect(db_path)
    try:
        con.row_factory = sqlite3.Row
        where, params = [], []
        if lo_req is not None:
            where.append("ts >= ?")
            params.append(lo_req)
        if hi_req is not None:
            where.append("ts < ?")
            params.append(hi_req)
        sql = ("SELECT feature_id, actor, surface, outcome, session_run, pollution, count, ts "
               "FROM feature_events" + (" WHERE " + " AND ".join(where) if where else ""))
        rows = [dict(r) for r in con.execute(sql, params)]
        first_ts = con.execute("SELECT MIN(ts) FROM feature_events").fetchone()[0]
        newest_ts = con.execute("SELECT MAX(ts) FROM feature_events").fetchone()[0]
        total_indexed = con.execute("SELECT COUNT(*) FROM feature_events").fetchone()[0]
        run_row = con.execute("SELECT value FROM meta WHERE key=?", (um.META_RUN_AT,)).fetchone()
        index_run_at = float(run_row[0]) if run_row else None
        log = flag_log(con)
        # sessions with X7 records in the window: the report's denominator (as in the X7 report)
        sw, sp = [], []
        if lo_req is not None:
            sw.append("ts >= ?")
            sp.append(lo_req)
        if hi_req is not None:
            sw.append("ts < ?")
            sp.append(hi_req)
        sessions = con.execute("SELECT COUNT(DISTINCT session) FROM records WHERE pollution IS NULL"
                               + ("".join(" AND " + w for w in sw)), sp).fetchone()[0]
        first_rec = con.execute("SELECT MIN(ts) FROM records").fetchone()[0]
    finally:
        con.close()

    # window bounds: since/until if given, else from the first indexed evidence to the index run (or now)
    evidence = [t for t in (first_ts, first_rec, log[0][0] if log else None) if t is not None]
    lo = lo_req if lo_req is not None else (min(evidence) if evidence else None)
    hi = hi_req if hi_req is not None else (index_run_at if index_run_at is not None else now)
    emitter_on = emitter_on_whole_window(log, lo, hi)

    probes_excluded: Dict[str, int] = {}
    usage: List[Dict[str, Any]] = []
    noops: List[Dict[str, Any]] = []
    reserved: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        if r["pollution"] and not include_probes:
            a = r["actor"] or "<none>"
            probes_excluded[a] = probes_excluded.get(a, 0) + 1
            continue
        if r["feature_id"] in RESERVED_FEATURE_IDS:
            e = reserved.setdefault(r["feature_id"], {"events": 0, "count": 0})
            e["events"] += 1
            e["count"] += r["count"] or 0
            continue
        if r["outcome"] == "noop":
            noops.append(r)
            continue
        usage.append(r)

    by_fid: Dict[str, List[Dict[str, Any]]] = {}
    for r in usage:
        by_fid.setdefault(r["feature_id"], []).append(r)

    low_den = sessions < min_sessions
    features: List[Dict[str, Any]] = []

    def _row(fid: str, node: Optional[RegistryNode], status: str, evs: List[Dict[str, Any]],
             wired: bool, units: Optional[float], observed: Optional[float]) -> Dict[str, Any]:
        actors: Dict[str, int] = {}
        for e in evs:
            actors[e["actor"] or "<none>"] = actors.get(e["actor"] or "<none>", 0) + 1
        ts_list = [e["ts"] for e in evs if e["ts"] is not None]
        flagged = status in FLAGGED
        row: Dict[str, Any] = {
            "feature_id": fid, "node_id": node.node_id if node else None,
            "surfaces": list(node.surfaces) if node else sorted({e["surface"] for e in evs if e["surface"]}),
            "status": status, "flagged": flagged, "unobservable": status in UNOBSERVABLE,
            "built": bool(node is not None and wired), "wired": wired,
            "valued": bool(node and node.valued),
            "window": {"since": _iso(lo), "until": _iso(hi)},
            "sessions_in_window": sessions, "events": len(evs), "actors": actors,
            "distinct_actors": len(actors), "last_seen": _iso(max(ts_list)) if ts_list else None,
            "expected_rate": node.expected_rate if node else None, "observed_rate": observed,
            "units_in_window": units, "low_denominator": low_den,
            "emitter_flag_on": emitter_on, "index_run_at": _iso(index_run_at), "newest_event_ts": _iso(newest_ts),
            "zero_display": ("0?" if (not evs and low_den) else str(len(evs))),
        }
        if flagged or status in UNOBSERVABLE:
            row["label"] = um.SIGNAL_LABEL
        if status == "unobservable":
            row["reason"] = INVISIBLE_REASON
        elif status == "emitter-off":
            row["reason"] = ("the emitter flag was not demonstrably ON for the whole window"
                             + (" (never sampled)" if emitter_on is None else ""))
        elif status == "window-too-short":
            row["reason"] = "the window is shorter than one unit of the expected rate"
        return row

    registered: Set[str] = set()
    for node in nodes:
        registered.add(node.feature_id)
        evs = by_fid.get(node.feature_id, [])
        wired = node.feature_id in wired_ids
        units = _units(node, lo, hi, sessions)
        observed = None
        if node.rate is not None and units:
            observed = round(len(evs) / units, 4)
        status = classify(node, wired=wired, events=len(evs), units=units, observed=observed,
                          emitter_on=emitter_on)
        features.append(_row(node.feature_id, node, status, evs, wired, units, observed))
    for fid in sorted(set(by_fid) - registered):
        features.append(_row(fid, None, "unregistered-emitting", by_fid[fid], fid in wired_ids, None, None))

    hook_noops = None
    if noops:
        agg: Dict[str, Dict[str, Any]] = {}
        for r in noops:
            a = agg.setdefault(r["feature_id"], {"feature_id": r["feature_id"], "count": 0, "first_ts": None,
                                                 "last_ts": None, "session_runs": set()})
            a["count"] += 1
            if r["ts"] is not None:
                a["first_ts"] = r["ts"] if a["first_ts"] is None else min(a["first_ts"], r["ts"])
                a["last_ts"] = r["ts"] if a["last_ts"] is None else max(a["last_ts"], r["ts"])
            if r["session_run"]:
                a["session_runs"].add(r["session_run"])
        hook_noops = [{"feature_id": a["feature_id"], "count": a["count"], "first_ts": _iso(a["first_ts"]),
                       "last_ts": _iso(a["last_ts"]), "distinct_session_runs": len(a["session_runs"]),
                       "label": um.SIGNAL_LABEL} for a in sorted(agg.values(), key=lambda x: x["feature_id"])]

    rep: Dict[str, Any] = {
        "report": REPORT_NAME, "schema_version": um.SCHEMA_VERSION, "db": os.path.abspath(db_path),
        "registry_source": registry_source, "window": {"since": _iso(lo), "until": _iso(hi),
                                                       "requested": {"since": since, "until": until}},
        "min_sessions": min_sessions, "sessions_in_window": sessions, "low_denominator": low_den,
        "include_probes": include_probes, "probes_excluded": probes_excluded,
        "index_run_at": _iso(index_run_at), "newest_event_ts": _iso(newest_ts),
        "events_indexed_total": total_indexed, "no_events_indexed": total_indexed == 0,
        "emitter_flag_on_whole_window": emitter_on,
        "features": features,
        "unobservable": [f["feature_id"] for f in features if f["unobservable"]],
        "reserved_events": reserved,
        "label": um.SIGNAL_LABEL,
        "call_site_violations": dynamic,
    }
    if hook_noops is not None:
        rep["hook_noops"] = hook_noops
    if index_stats is not None:
        rep["index"] = index_stats
    return rep


def _scan() -> Tuple[Set[str], List[Dict[str, Any]]]:
    from . import feature_scan

    return feature_scan.scan_call_sites()


# --- text --------------------------------------------------------------------------------------------------


def format_feature_report(rep: Dict[str, Any]) -> str:
    L: List[str] = []
    L.append(f"{REPORT_NAME} report  (every flag below: {um.SIGNAL_LABEL})")
    L.append(f"db: {rep['db']}   registry: {rep['registry_source']}")
    w = rep["window"]
    L.append(f"window: {w['since'] or '-'} .. {w['until'] or '-'}   sessions in window: {rep['sessions_in_window']}"
             f"{' (low denominator: fewer than ' + str(rep['min_sessions']) + ')' if rep['low_denominator'] else ''}"
             f"   probes: {'INCLUDED' if rep['include_probes'] else 'excluded'}")
    L.append(f"index run at: {rep['index_run_at'] or 'never'}   newest indexed event: {rep['newest_event_ts'] or 'none'}"
             f"   emitter flag ON whole window: {rep['emitter_flag_on_whole_window']}")
    if rep["no_events_indexed"]:
        L.append("no events indexed")
    if rep["probes_excluded"]:
        L.append("probe events excluded (use --include-probes): "
                 + ", ".join(f"{a}={n}" for a, n in sorted(rep["probes_excluded"].items())))
    L.append("")
    order = ["unused", "underused", "wired-never-emitted", "registered-not-wired", "unregistered-emitting",
             "window-too-short", "unrated", "as-expected", "emitter-off", "unobservable"]
    for st in order:
        grp = [f for f in rep["features"] if f["status"] == st]
        if not grp:
            continue
        tag = f" [{um.SIGNAL_LABEL}]" if st in FLAGGED or st in UNOBSERVABLE else ""
        L.append(f"{st} -- {len(grp)}{tag}")
        for f in grp:
            acts = ", ".join(f"{a}={n}" for a, n in sorted(f["actors"].items())) or "-"
            rate = f"expected {f['expected_rate']}" if f["expected_rate"] is not None else "unregistered"
            obs = f"observed {f['observed_rate']}/unit" if f["observed_rate"] is not None else "observed n/a"
            L.append(f"  {f['feature_id']:<34} events {f['zero_display']:>4}  actors {f['distinct_actors']} ({acts})  "
                     f"{rate}  {obs}  sessions {f['sessions_in_window']}  last {f['last_seen'] or '-'}"
                     + (f"  -- {f['reason']}" if f.get("reason") else ""))
        L.append("")
    if "hook_noops" in rep:
        L.append(f"hook-noops -- {len(rep['hook_noops'])} [{um.SIGNAL_LABEL}]   (never counted as usage)")
        for n in rep["hook_noops"]:
            L.append(f"  {n['feature_id']:<34} count {n['count']}  first {n['first_ts']}  last {n['last_ts']}  "
                     f"session-runs {n['distinct_session_runs']}")
    else:
        L.append("hook no-op reporting off or no no-ops in window")
    if rep["reserved_events"].get(DROPPED_FEATURE_ID):
        d = rep["reserved_events"][DROPPED_FEATURE_ID]
        L.append(f"emitter drops: {d['events']} summary event(s), {d['count']} dropped call(s) "
                 f"[{um.SIGNAL_LABEL}]  (feature-events-dropped is a system id, not a feature)")
    if rep["call_site_violations"]:
        L.append(f"call sites that do not use a literal / closed-set feature id: {len(rep['call_site_violations'])} "
                 f"[{um.SIGNAL_LABEL}]")
    return "\n".join(L)

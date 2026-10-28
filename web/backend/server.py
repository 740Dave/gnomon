"""Gnomon WEB backend — the thin HTTP seam over the reused MVC Controller.

slice 1. This is the browser-facing presentation plane for
Gnomon: a small FastAPI app that serves the single-page frontend shell and
a JSON API for the Agents tab. It **reuses** the existing, CTk-free
:class:`gnomon.app.controller.Controller` — the same logic the CustomTkinter
GUI drives — and re-implements none of it. The CTk app (`gnomon.app.gui`)
is left completely intact; this is an ADDITIVE second view over one controller.

Design intent ("thin browser client over the services / hub surface"):

* The Controller owns every decision: list profiles, launch a colleague, scan
  live sessions, read per-session context %. This module only marshals those
  calls to/from JSON and serves the SPA. Treat it as the clean seed of the
  eventual ``services`` daemon.
* ⛔ The terminal launch stays DAEMON-SIDE. ``POST /api/launch`` calls
  ``Controller.launch(...)``, which on this (the daemon) host runs the existing
  ``LaunchManager.open_terminal`` osascript path. The browser only TRIGGERS it;
 it never launches a terminal itself (it cannot, and must not —
  ``terminal-launch``).
* The co-hosted :class:`gnomon.hub.Hub` is started at app startup, exactly
  as the CTk shell does, so a launched session wires the live hub URL and the
  live-sessions view can read the hub's per-session context %.

Run:  ``python -m uvicorn backend.server:app`` from ``~/gnomon/web`` (or
``python ~/gnomon/web/backend/server.py``). See ``web/README.md``.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# --- make the existing gnomon package importable from any cwd ----------
# web/ lives at <project_root>/web; the reusable package is <project_root>/
# gnomon. In dev the package is usually already importable (editable
# install), but we make the seed self-contained so the daemon runs standalone.
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))          # .../web/backend
_WEB_DIR = os.path.dirname(_THIS_DIR)                            # .../web
_PROJECT_ROOT = os.path.dirname(_WEB_DIR)                        # .../gnomon
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from fastapi import FastAPI, HTTPException, Request              # noqa: E402
from fastapi.concurrency import run_in_threadpool                # noqa: E402
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles                      # noqa: E402
from pydantic import BaseModel                                   # noqa: E402

from gnomon.app.controller import Controller              # noqa: E402
from gnomon.app.config_controller import ConfigController  # noqa: E402
from gnomon import flags as flags                      # noqa: E402
from gnomon.config import runtime_root_default            # noqa: E402  (X13 S5-4)
from gnomon.harness import verb_policy as verb_policy_mod   # noqa: E402
from gnomon import buildstamp                             # noqa: E402
from gnomon import golive_monitor as golive_monitor_mod    # noqa: E402
from gnomon import usage_metrics as usage_metrics_mod      # noqa: E402
from gnomon import feature_events as feature_events_mod     # noqa: E402
from gnomon import feature_sites as feature_sites_mod       # noqa: E402
from gnomon import usage_index_job as usage_index_job_mod   # noqa: E402
from gnomon import deliverables as deliverables_mod        # noqa: E402
from gnomon import def_preview as def_preview_mod         # noqa: E402
from gnomon import alerts as alerts_mod                    # noqa: E402
from gnomon.dsd.sections import order_rows                     # noqa: E402
from gnomon.dsd.senttime import sent_fields as _sent_fields      # noqa: E402  (DSD sent time)
from gnomon.dsd import documents as dsd_documents_mod          # noqa: E402
from gnomon.dsd import submit_proof as dsd_submit_proof_mod    # noqa: E402
from gnomon.singleflight import SingleFlightTTL                  # noqa: E402  (poll-pileup fix)
from datetime import datetime, timezone                               # noqa: E402

try:
    # The persisted machine config (slice 4 of the CTk build) — same source the
    # CTk shell loads, so the web view launches with this machine's settings.
    from gnomon.config import ConfigStore
except Exception:  # pragma: no cover - config module should exist
    ConfigStore = None  # type: ignore[assignment]


STATIC_DIR = os.path.join(_WEB_DIR, "static")


# --- the build this server process is running --------------------------
#
# Stamped ONCE at import (= server startup) and cached for the process: the code
# this :8770 process loaded. Compared against ``buildstamp.current_build()`` (the
# tree on disk now, ~30 s TTL) to say "dashboard running stale build". A stamp
# failure is recorded, never fatal — the server must still come up.
try:
    SERVER_BUILD: dict = dict(buildstamp.build_info(_PROJECT_ROOT, scope=buildstamp.SERVER))
except Exception as _exc:  # noqa: BLE001
    SERVER_BUILD = {"build_id": None, "scope": buildstamp.SERVER,
                    "build_error": f"{type(_exc).__name__}: {_exc}"}


def _current_build(scope: str = buildstamp.SERVER) -> dict:
    """The tree on disk now, in ``scope`` (like with like: the server's own id is
    compared with the SERVER scope, a session's launch.json id with SESSION)."""
    try:
        return dict(buildstamp.current_build(_PROJECT_ROOT, scope=scope))
    except Exception as exc:  # noqa: BLE001
        return {"build_id": None, "scope": scope, "build_error": f"{type(exc).__name__}: {exc}"}


def _server_build_view(current: dict) -> dict:
    sid, cid = SERVER_BUILD.get("build_id"), current.get("build_id")
    return {"id": sid, "current_id": cid,
            "stale": bool(sid and cid and sid != cid),
            "computed_at": SERVER_BUILD.get("computed_at"),
            "error": SERVER_BUILD.get("build_error") or current.get("build_error")}


# --- controller wiring --------------------------------------------------------

def build_controller() -> Controller:
    """Construct the reused Controller against the persisted machine config.

    Mirrors ``gnomon.app.gui.GnomonApp.__init__``: load the config
    if available, else fall back to in-code defaults. No CTk, no widgets.
    """
    if ConfigStore is not None:
        try:
            return Controller(config=ConfigStore().load())
        except Exception as exc:  # a bad config must not stop the daemon
            print(f"[gm-web] config load failed ({exc}); using defaults")
    return Controller()


# A single process-wide Controller + hub, shared across requests. The hub runs on
# background threads (co-hosted, in-process) exactly as under the CTk shell.
controller: Controller = build_controller()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Start the co-hosted hub before serving, so a launch wires the live hub URL
    # and live-sessions can read the hub's per-session context %.
    try:
        info = controller.start_hub()
        print(f"[gm-web] hub started: {info.get('mcp_url')}")
    except Exception as exc:  # a bind failure must not stop the web server
        print(f"[gm-web] hub failed to start: {exc}")
    # schedule ONE go-live smoke ~20 s out, on a background thread. Never
    # blocks startup; any failure here is recorded and logged, never raised.
    _start_golive()
    _warn_security_flags_off()   # confirm-proof F1(c): a security flag that is OFF is announced at start
    _start_feature_index()   # the background feature-event indexer (never fatal)
    _start_app_activity()    # the app-activity poller (never fatal; idle while its flag is OFF)
    _start_scheduler()       # stage 1a: the 30 s tick loop (never fatal; ticks only while its flag is ON)
    _start_transcript_archive()   # the transcript archive timer (never fatal; runs only while its flag is ON)
    try:  # sweep def-preview temp dirs abandoned by a previous run (never fatal)
        def_preview_mod.sweep(_def_preview_base)
    except Exception:  # noqa: BLE001
        pass
    try:
        yield
    finally:
        try:
            if golive_monitor is not None:
                golive_monitor.cancel()   # a still-pending run never outlives the server
        except Exception:
            pass
        try:
            if _feature_index_job is not None:
                _feature_index_job.stop()
        except Exception:
            pass
        try:
            if _app_activity_poller is not None:
                _app_activity_poller.stop()
        except Exception:
            pass
        try:
            _stop_scheduler()
        except Exception:
            pass
        try:
            if _transcript_archive_job is not None:
                _transcript_archive_job.stop()
        except Exception:
            pass
        try:
            controller.stop_hub()
        except Exception:
            pass


app = FastAPI(title="Gnomon (web)", lifespan=lifespan)


# --- feature-usage telemetry in the web process ---------------------------------------------------
#
# (1) ONE middleware emits a ``web-api`` feature event per SUCCESSFUL user-initiated action, keyed by the closed
#     (method, route template) table in ``gnomon.feature_sites`` (a route absent from the table, or listed
#     ``no-event`` there, emits nothing; the coverage test fails on an unlisted route). Actor = the viewer (``ch``);
#     session_run and project are null (the UI has no picker yet). The emitter never raises.
# (2) the BACKGROUND INDEXER (``usage_index_job``) loads the feature-event JSONL into the usage DB every
#     FU_INDEX_INTERVAL_S while ``feature_events`` is ON. It starts in the lifespan; never under pytest
#     (g31) unless a test starts it explicitly through the seam below.
feature_events_mod.set_process_kind("web")

#: the running background indexer (None before the lifespan has started it, or when not started)
_feature_index_job = None
#: Test seam: an IndexJob factory. None = the production job (and none at all under pytest).
_feature_index_factory = None


# --- poll-pileup fix (2026-10-04): single-flight short-TTL caches for the three polled, heavy GETs -------------
#
# The page polls /api/sessions, /api/dsd/list and the deliverables ledger on timers; each used to recompute on
# every call, so concurrent polls (and several browser tabs) multiplied the work. Now concurrent callers share
# ONE computation. What holds: a value is stamped with the time its flight STARTED and is served only while
# now - start < POLL_CACHE_TTL_S, and the data was read after the start, so a reused value is never older than
# the TTL. If a computation takes LONGER than the TTL the stored value is already expired when it lands: callers
# that arrived during the flight share it (coalescing), nothing is reused afterwards. A mutating route
# (anything but GET under the owning prefix) invalidates: the next read starts a fresh flight (a caller already
# waiting on a flight that began before the write still gets that flight's pre-write answer, once). Reads that
# change WITHOUT a web write (an agent filing a DSD, a session starting) are bounded by the TTL, except
# /api/dsd/arrivals: a non-empty arrivals result invalidates the DSD list. /api/config (POST) invalidates all
# three: the flags in the config shape the content of each (the chips in /api/sessions, the notice column in
# /api/dsd/list, the ledger's enabled/open flags). Not cached: errors (raised to that flight's callers only).
# The TTL is a module attribute so tests can set it to 0.
POLL_CACHE_TTL_S: float = 2.0
_sessions_cache = SingleFlightTTL(POLL_CACHE_TTL_S)
_dsd_list_cache = SingleFlightTTL(POLL_CACHE_TTL_S)
_ledger_cache = SingleFlightTTL(POLL_CACHE_TTL_S)
_POLL_CACHES = (_sessions_cache, _dsd_list_cache, _ledger_cache)


def set_poll_cache_ttl(ttl: float) -> None:
    """Set every polled-GET cache TTL (0 = no reuse, single-flight only) and drop what is cached."""
    global POLL_CACHE_TTL_S
    POLL_CACHE_TTL_S = float(ttl)
    for c in _POLL_CACHES:
        c.ttl = float(ttl)
        c.invalidate()


@app.middleware("http")
async def _poll_cache_invalidation(request: Request, call_next):
    try:
        return await call_next(request)
    finally:
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            path = request.url.path
            if path.startswith("/api/dsd"):
                _dsd_list_cache.invalidate()
            elif path.startswith("/api/deliverables"):
                _ledger_cache.invalidate()
            elif path.startswith("/api/launch") or path.startswith("/api/sessions"):
                _sessions_cache.invalidate()
            elif path.startswith("/api/config"):
                for c in _POLL_CACHES:       # flags in the config change what all three views contain
                    c.invalidate()


_REVALIDATE_TYPES = ("application/javascript", "text/javascript", "text/css")


@app.middleware("http")
async def _revalidate_ui_assets(request: Request, call_next):
    """The page and EVERY response whose content type is JavaScript or CSS (/static and each flag-gated UI route: ledger, apps, scheduler, mail, work, guard
    banner, alerts, and any later one) are sent with ``Cache-Control: no-cache`` (revalidate against the ETag every time, 304 when unchanged; NOT no-store).
    Keyed on the content type, not a path list, so a new script route is covered without being named. Without a directive Safari applies heuristic freshness
    from Last-Modified and kept a pre-land stylesheet (the stale-CSS logo defect)."""
    resp = await call_next(request)
    if request.method in ("GET", "HEAD") and resp.status_code in (200, 304):
        ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
        if request.url.path == "/" or ctype in _REVALIDATE_TYPES:
            resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.middleware("http")
async def _feature_usage_middleware(request: Request, call_next):
    response = await call_next(request)
    try:
        if response.status_code < 400:
            tpl = getattr(request.scope.get("route"), "path", None)
            if feature_sites_mod.route_feature_id(request.method, tpl) is not None:
                feature_events_mod.set_process_kind("web")
                feature_events_mod.feature_event(feature_sites_mod.route_feature_id(request.method, tpl),
                                                 feature_events_mod.VIEWER_ACTOR, "web-api", outcome="ok")
    except Exception:  # noqa: BLE001 - telemetry never fails a request
        pass
    return response


def _start_feature_index() -> None:
    global _feature_index_job
    try:
        if _feature_index_factory is not None:
            job = _feature_index_factory()
        elif os.environ.get("PYTEST_CURRENT_TEST"):
            return   # g31: no test run starts the live indexer
        else:
            job = usage_index_job_mod.IndexJob()
        _feature_index_job = job
        job.start()
        print(f"[gm-web] feature-event indexer started (every {job.interval:.0f}s, while "
              f"{feature_events_mod.FLAG_EVENTS} is on)", flush=True)
    except Exception as exc:  # noqa: BLE001 - the server must come up regardless
        print(f"[gm-web] feature-event indexer NOT started: {type(exc).__name__}: {exc}", flush=True)


# --- golive_smoke on every start -------------------------------------
#
# The :8770 process (this module's lifespan) is the real service entrypoint: it
# co-hosts the hub in-process (``controller.start_hub``), so "hub start" and
# "server start" are the same event. One GoliveMonitor per start; its result is in
# memory (``GET /api/golive``) and in ``<run_base>/rt-<key>/golive.json``.
#
# No on-demand re-run: the :8770 server has no operator-token convention on its
# mutating routes (/api/launch, /api/config, /api/dsd/submit are unauthenticated;
# the operator token lives on the hub's MCP HTTP only), so per the dispatch the
# surface is GET-only. A re-run is a server restart.

#: Seconds after start before the smoke runs (relaunched harnesses re-register).
GOLIVE_DELAY_S: float = golive_monitor_mod.DEFAULT_DELAY_S
#: Test seam: an injected runner ``(port, runtime_root, run_base, timeout) ->
#: (exit, stdout, stderr)``. None = the real smoke subprocess.
_golive_runner = None
#: The monitor for THIS process start (None before the lifespan has run).
golive_monitor = None
#: A setup failure (the monitor could not even be built/scheduled), surfaced by the API.
_golive_setup_error: Optional[str] = None


def _golive_enabled() -> bool:
    """``auto_golive_smoke`` (the roadmap-cited flag), default ON.

    The registry default is ON (architect, 2026-09-29: a read-only check);
    ``default=True`` here is kept as a belt-and-braces fallback."""
    return _flag(golive_monitor_mod.FLAG, default=True)


def _start_golive() -> None:
    global golive_monitor, _golive_setup_error
    golive_monitor, _golive_setup_error = None, None
    try:
        try:
            runtime_root = controller.config.runtime_root
        except Exception:  # noqa: BLE001
            runtime_root = runtime_root_default()
        mon = golive_monitor_mod.GoliveMonitor(
            port=int(os.environ.get("GNOMON_WEB_PORT", "8770")), runtime_root=runtime_root,
            build_id=SERVER_BUILD.get("build_id"), runner=_golive_runner)
        golive_monitor = mon
        if _golive_enabled():
            mon.schedule(GOLIVE_DELAY_S)
            print(f"[gm-web] golive smoke scheduled in {GOLIVE_DELAY_S:.0f}s", flush=True)
        else:
            mon.mark_disabled(f"{golive_monitor_mod.FLAG} is off")
            print(f"[gm-web] golive smoke NOT scheduled ({golive_monitor_mod.FLAG} off)", flush=True)
    except Exception as exc:  # noqa: BLE001 - the server must come up regardless
        _golive_setup_error = f"{type(exc).__name__}: {exc}"
        print(f"[gm-web] golive ERROR: could not schedule the smoke: {_golive_setup_error}", flush=True)


@app.get("/api/golive")
def golive_get() -> dict:
    """the latest start-time go-live smoke result (read-only)."""
    if golive_monitor is not None:
        return golive_monitor.snapshot()
    enabled = _golive_enabled()
    if _golive_setup_error is not None:
        return {"enabled": enabled, "state": "error", "error": _golive_setup_error,
                "checks": [], "counts": None, "on_demand": False}
    return {"enabled": enabled, "state": "not-started" if enabled else "disabled",
            "checks": [], "counts": None, "on_demand": False,
            "build_id": SERVER_BUILD.get("build_id")}


# --- request models -----------------------------------------------------------

class LaunchRequest(BaseModel):
    instance_id: str


class DsdSelection(BaseModel):
    """One decision's answer as the browser posts it: the chosen option id (an
    ``option_id`` from the presented form, incl. the escape-hatch id) plus the
    per-decision comment. ``option_id`` absent/blank means the decision was left
    unanswered — which the SERVER-SIDE completeness gate then refuses."""

    option_id: Optional[str] = None
    comment: str = ""


class DsdSubmitRequest(BaseModel):
    """The whole pending DSD's answers: ``{decision_id: DsdSelection}``. The
    browser never decides completeness — it posts what the human entered and the
 controller/persistence path is the authority (
    completeness-gate; the CTk build's UI-only gate defect must not recur)."""

    selections: Dict[str, DsdSelection] = {}
    #: {count, first_ms_ago, last_ms_ago} of the operator's real in-tab input (validated by hand).
    activity: Any = None


class ConfigSaveRequest(BaseModel):
    """A whole-config save as the browser posts it (Config tab, slice 4).

    ``fields`` carries the scalar/list fields keyed by their ``FieldSpec.key``,
    each as the RAW string the user typed — the reused
    :class:`ConfigController.set` parses+validates every one per its field kind
    (path/text/csv/int_csv). ``custom_terminal_templates`` is the full desired
    ``{name: command-template}`` map (the tab reconciles add/remove against it).

    ⛔ The browser is NEVER the authority: every validation (int lists, the
    non-empty MCP set, and the D9 measurement-server refusal) is enforced SERVER-SIDE by
    the reused ``ConfigController`` — exactly as the DSD completeness gate is."""

    fields: Dict[str, str] = {}
    custom_terminal_templates: Dict[str, str] = {}


# --- JSON API (Agents tab) ------------------------------------------

@app.get("/api/health")
def health() -> dict:
    """Liveness + what the daemon is wired to (surfaced in the UI footer)."""
    return {
        "ok": True,
        "hub_url": controller.hub_url,
        "intelligences_root": controller.intelligences_root,
        "sessions_root": controller.sessions_root,
        # this process's build (unconditional — golive_smoke reads it) and,
        # behind build_stamp_chip, the header's staleness view.
        "build_id": SERVER_BUILD.get("build_id"),
        "build_scope": buildstamp.SERVER,
        "build": _server_build_view(_current_build()) if _flag("build_stamp_chip") else None,
        # round 3: refused registrations are operator-visible (key absent when OFF).
        **_caller_binding_health(),
        # g68-amendment: a presentation table that fell back to its neutral built-in is operator-visible (key absent when none).
        **_deploy_data_health(),
    }


def _deploy_data_health() -> dict:
    try:
        from gnomon import deploy_data

        faults = deploy_data.fault_summary()
    except Exception:  # noqa: BLE001 - health never fails on this
        return {}
    return {"deploy_data_faults": faults} if faults else {}


def _caller_binding_health() -> dict:
    try:
        status = controller.hub.binding_status()
    except Exception:  # noqa: BLE001 - health never fails on this
        return {}
    return {"caller_binding": status} if status is not None else {}


@app.get("/api/build")
def build() -> dict:
    """the running server's build stamp vs the tree on disk now."""
    current = _current_build()
    view = _server_build_view(current)
    return {"server": SERVER_BUILD, "current": current, "stale": view["stale"]}


# --- agent-def SIZE (operator ruling 2026-09-29) -------------
#
# "No warning: show agent-def SIZE on the dashboard (as a reminder, and a place to
# launch a def-optimization session with the optimizer agent)." Sizes come from the
# render record ``agent_def.render_agent_def`` already writes into each session's
# launch.json (``agent_def_expansion.chars``) and from the profile's
# ``claude_md_source`` on disk; the arithmetic is ``agent_def.size_info`` (reused).

def optimizer_profile() -> str:
    """The agent that runs a def-optimization pass: DEPLOYMENT data (``<deploy_root>/defaults/optimizer.json``,
    ``{"profile": "<instance id>"}``; X13 S5-3). ``""`` = none configured (neutral default): the optimize action
    is not offered. Read lazily on every call, never at import."""
    from gnomon import deploy_data

    v = deploy_data.section("optimizer", "profile")
    if isinstance(v, str) and re.match(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z", v):
        return v
    if v is not None:               # g68-amendment: a present-but-bad value degrades to "none configured" VISIBLY
        deploy_data.record_fault("optimizer", "'profile' must be an instance id string; ignored (no optimizer configured)")
    return ""


def _session_def_size(session_dir: str) -> Optional[dict]:
    """The session's CURRENT rendered agent-def size, from its launch.json record
    (None when there is no record — e.g. a pre-2026-09-29 launch)."""
    from gnomon.agent_def import size_info

    try:
        with open(os.path.join(session_dir, ".claude", "launch.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(cfg, dict):
        return None
    exp = cfg.get("agent_def_expansion") or {}
    chars = exp.get("chars") if isinstance(exp, dict) else None
    if not isinstance(chars, int):
        return None
    out = size_info(chars)
    out.update(path=cfg.get("agent_def_path"), rendered_at=cfg.get("agent_def_rendered_at"),
               event=cfg.get("agent_def_event"), session_dir=session_dir)
    return out


def _session_notice_hold(session_dir: str, now: Optional[float] = None) -> Optional[dict]:
    """Line-hold audit note 3: "notice held — operator line partial (N min)", built
    from the session's ``notice-hold`` records (current harness pid only)."""
    recs = []
    try:
        with open(os.path.join(session_dir, ".claude", "injection_log.jsonl"), encoding="utf-8",
                  errors="replace") as fh:
            recs = [json.loads(ln) for ln in fh if '"notice-hold"' in ln]
    except (OSError, ValueError):
        return None
    recs = [r for r in recs if isinstance(r, dict) and r.get("kind") == "notice-hold"]
    if not recs:
        return None
    held: Dict[tuple, dict] = {}
    for r in (r for r in recs if r.get("pid") == recs[-1].get("pid")):
        key = (r.get("notice_kind"), r.get("threshold"))
        if r.get("decision") == "held":
            held.setdefault(key, r)
        elif r.get("decision") != "superseded":
            held.pop(key, None)
    if not held:
        return None
    first = min(held.values(), key=lambda r: r.get("ts") or 0)
    minutes = int(((now or time.time()) - float(first.get("ts") or 0)) // 60)
    why = "partial" if "partial" in str(first.get("reason")) else "unknown"
    # acceptance-3: the chip names WHAT is held (the notice kind).
    kinds = sorted({str(k[0]) for k in held if k[0]})
    return {"held": len(held), "since_ts": first.get("ts"), "minutes": minutes,
            "label": f"notice held — operator line {why} ({minutes} min)", "kinds": kinds}


#: pre-review B2 (count only): a DSD answer notice counts as "held" once it has waited longer than this (seconds). Defined ONCE, in the
#: harness inbox; the context verb and this route share it.
from gnomon.harness.notice_inbox import HELD_COUNT_AFTER_SECONDS as HELD_NOTICE_COUNT_AFTER_S  # noqa: E402


def _session_held_notice_count(session_dir: str, now: Optional[float] = None,
                               over: float = HELD_NOTICE_COUNT_AFTER_S) -> Optional[int]:
    """How many DSD answer notices the session's CURRENT harness has held for longer than ``over`` seconds, from its
    ``dsd-notice`` provenance records: a ``held`` record opens a hold, any later record for the same dsd_id (written,
    acked, denied, ...) closes it. Only the newest harness pid's records count. Read-only. A session with no log at all holds nothing
    (0); a log that cannot be READ is ``None``: an error, never a zero (pre-review fix round, D-F2)."""
    recs = []
    try:
        with open(os.path.join(session_dir, ".claude", "injection_log.jsonl"), encoding="utf-8",
                  errors="replace") as fh:
            for ln in fh:
                if '"dsd-notice"' not in ln:
                    continue
                try:
                    r = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(r, dict) and r.get("kind") == "dsd-notice" and isinstance(r.get("dsd_id"), str):
                    recs.append(r)
    except FileNotFoundError:
        return 0
    except OSError:
        return None
    if not recs:
        return 0
    pid = recs[-1].get("pid")
    held: Dict[str, float] = {}
    for r in recs:
        if r.get("pid") != pid:
            continue
        if r.get("decision") == "held":
            ts = r.get("ts")
            if isinstance(ts, (int, float)) and not isinstance(ts, bool):
                held.setdefault(r["dsd_id"], float(ts))
        elif r.get("decision") == "inline-fallback":
            continue            # the inline text was dropped for the pointer line; the notice itself is still held
        else:
            held.pop(r["dsd_id"], None)
    t = now if now is not None else time.time()
    return sum(1 for ts in held.values() if t - ts > over)


def _held_notice_counts(now: Optional[float] = None) -> Optional[Dict[str, Optional[int]]]:
    """``{instance_id: count | None}`` over the live sessions (see :func:`_session_held_notice_count`); ``None`` for an instance with
    an unreadable log; the whole answer is ``None`` when the live sessions cannot be listed. Never a silent zero."""
    try:
        views = controller.live_sessions()
    except Exception as exc:  # noqa: BLE001
        print(f"[gm-web] held-notice count: live sessions unavailable: {exc}", flush=True)
        return None
    out: Dict[str, Optional[int]] = {}
    for v in views:
        if getattr(v, "live", False):
            n = _session_held_notice_count(v.session_dir, now)
            prev = out.get(v.instance_id, 0)
            out[v.instance_id] = None if (n is None or prev is None) else prev + n
            if n is None:
                print(f"[gm-web] held-notice count: cannot read {v.session_dir}/.claude/injection_log.jsonl", flush=True)
    return out


def _quiescent_harness_live(session_dir: str, pid, rec_ts) -> bool:
    """fix pass: is ``pid`` this session's CURRENT live harness?

    Checked at the OBJECT (``hub.procinfo``; no pid registry): the process is
    alive, its argv is ``... gnomon.harness <session_dir>`` (the
    LaunchManager's command), and it started no later than the record (a reused
    pid started afterwards is not the harness that wrote it). Any doubt -> False."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        from gnomon.hub import procinfo

        st = procinfo.probe(pid)
        if st.state != procinfo.ALIVE:
            return False
        if isinstance(rec_ts, (int, float)) and st.start_time is not None and st.start_time > rec_ts + 1.0:
            return False
        argv = procinfo.argv_of(pid) or []
    except Exception:  # noqa: BLE001 - unreadable = not provably live
        return False
    if "gnomon.harness" not in argv:
        return False
    want = os.path.realpath(session_dir)
    return any(os.path.realpath(a) == want for a in argv if a and not a.startswith("-"))


def _session_modal_hold(session_dir: str) -> Optional[dict]:
    """``{"held", "since_ts", "why", "label"}`` while the session's
    CURRENT live harness has a STUCK modal hold (a ``modal-guard`` ``stuck-hold`` record with no later
    ``stuck-hold-cleared``): harness text is held behind a dialog that may still be open (or the
    operator's busy line) past the cap, and a human must clear it. Read-only, from the provenance log."""
    path = os.path.join(session_dir, ".claude", "injection_log.jsonl")
    state = None
    last_line = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                last_line = ln
                if '"modal-guard"' not in ln or "stuck-hold" not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("kind") != "modal-guard":
                    continue
                if rec.get("what") == "stuck-hold":
                    state = rec
                elif rec.get("what") == "stuck-hold-cleared":
                    state = None
    except OSError:
        return None
    if state is None:
        return None
    try:
        last = json.loads(last_line)
        last_pid = last.get("pid") if isinstance(last, dict) else None
    except ValueError:
        last_pid = state.get("pid")
    if state.get("pid") != last_pid or not _quiescent_harness_live(session_dir, state.get("pid"), state.get("ts")):
        return None  # a dead / replaced harness never shows a stale chip
    return {"held": state.get("held"), "since_ts": state.get("since_ts"), "why": state.get("why"),
            "label": state.get("label") or "harness text held; a dialog may still be open"}


def _session_waiting(session_dir: str, store: Any = None) -> Optional[dict]:
    """(gated by waiting_on_operator_alert): the permission prompt the session's CURRENT live harness is waiting on, or
    None. Built from the session log's ALLOWLISTED records: the newest ``modal-guard`` ``screen-dialog-seen`` record that carries an
    ``episode`` with no LATER ``screen-dialog-closed`` from the same harness process, and its ``screen-dialog-read`` record (kind,
    confidential, countdown seconds). The log holds NO screen text; the QUESTION is taken from the STORED ALERT
    (``store.find_body``) and only for a session that is not confidential, so a prompt that raised no alert (alerts off, the
    console typed in, a dropped hand-off) shows the kind only. A record that says nothing about confidentiality counts as
    confidential. A dead or replaced harness never shows a stale badge. Read-only."""
    path = os.path.join(session_dir, ".claude", "injection_log.jsonl")
    state = None
    read = None
    last_line = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                last_line = ln
                if '"modal-guard"' not in ln or "screen-dialog-" not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if not isinstance(rec, dict) or rec.get("kind") != "modal-guard":
                    continue
                what = rec.get("what")
                if what == "screen-dialog-seen" and "episode" in rec:
                    state, read = rec, None
                elif what == "screen-dialog-read" and state is not None and rec.get("pid") == state.get("pid") \
                        and rec.get("episode") == state.get("episode"):
                    read = rec
                elif what == "screen-dialog-closed" and state is not None \
                        and rec.get("pid") == state.get("pid") and rec.get("episode") == state.get("episode"):
                    state, read = None, None
    except OSError:
        return None
    if state is None:
        return None
    try:
        last = json.loads(last_line)
        last_pid = last.get("pid") if isinstance(last, dict) else None
    except ValueError:
        last_pid = state.get("pid")
    if state.get("pid") != last_pid or not _quiescent_harness_live(session_dir, state.get("pid"), state.get("ts")):
        return None
    conf = read is None or read.get("confidential") is not False
    left = (read or {}).get("seconds_left")
    opened = state.get("opened_ts")
    if not conf and isinstance(left, (int, float)) and not isinstance(left, bool) and isinstance(opened, (int, float)):
        left = max(0, int(left - max(0.0, time.time() - opened)))
    else:
        left = None
    question = None
    iid = state.get("instance_id")
    if not conf and store is not None and isinstance(iid, str):
        try:
            body = store.find_body("harness:" + iid, f"waiting:{iid}:{state.get('episode')}")
        except Exception:  # noqa: BLE001 - an unreadable store shows no question
            body = None
        if isinstance(body, str) and "\n" in body:
            question = body.split("\n", 1)[1]
    return {"dialog": state.get("dialog") if isinstance(state.get("dialog"), str) else "unknown",
            "question": question, "confidential": conf, "seconds_left": left,
            "opened_ts": opened if isinstance(opened, (int, float)) else None, "episode": state.get("episode")}


def _session_quiescent(session_dir: str) -> Optional[dict]:
    """``{"reason", "since_ts", "label"}`` when the session's CURRENT
    harness last recorded ``quiescent: enter`` with no later ``exit``; else None.
    CURRENT = the pid of the log's last record AND (fix pass) a live process that
    is this session's harness (:func:`_quiescent_harness_live`) — a harness that
    died, or a new one that has not logged yet, never shows a stale chip. Built
    from the ``{kind: quiescent}`` provenance records (read-only)."""
    path = os.path.join(session_dir, ".claude", "injection_log.jsonl")
    recs = []
    last_line = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                last_line = ln
                if '"quiescent"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == "quiescent":
                    recs.append(rec)
    except OSError:
        return None
    if not recs:
        return None
    try:
        last = json.loads(last_line)
        last_pid = last.get("pid") if isinstance(last, dict) else None
    except ValueError:
        last_pid = recs[-1].get("pid")
    state = None
    for r in recs:
        if r.get("pid") != last_pid:
            continue
        if r.get("decision") == "enter":
            state = r
        elif r.get("decision") == "exit":
            state = None
    if state is None or not isinstance(state.get("reason"), str):
        return None
    if not _quiescent_harness_live(session_dir, state.get("pid"), state.get("ts")):
        return None
    since = state.get("since_ts")
    when = (time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(float(since)))
            if isinstance(since, (int, float)) else "?")
    return {"reason": state["reason"], "since_ts": since,
            "label": f"quiescent: {state['reason']} since {when}"}


def _session_idle_snooze(session_dir: str, now: Optional[float] = None) -> Optional[dict]:
    """the session's CURRENT idle-snooze state, built read-only from the
    ``{kind: idle-snooze}`` provenance records of its CURRENT harness (the pid of the log's last
    record, which must still be a live harness of this session, :func:`_quiescent_harness_live`):

    * ``{"state": "dismissed", "reason", "since_ts", "label"}`` after a ``dismiss`` with no later
      ``clear`` ("idle alerts dismissed until operator returns: <reason>");
    * ``{"state": "snoozed", "until_ts", "label"}`` after a ``snooze`` whose time has not passed
      and with no later dismiss/clear/expiry;
    * ``None`` otherwise (nothing to show)."""
    path = os.path.join(session_dir, ".claude", "injection_log.jsonl")
    recs = []
    last_line = ""
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                last_line = ln
                if '"idle-snooze"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == "idle-snooze":
                    recs.append(rec)
    except OSError:
        return None
    if not recs:
        return None
    try:
        last = json.loads(last_line)
        last_pid = last.get("pid") if isinstance(last, dict) else None
    except ValueError:
        last_pid = recs[-1].get("pid")
    state = None
    for r in recs:
        if r.get("pid") != last_pid:
            continue
        d = r.get("decision")
        if d == "snooze":
            state = {"state": "snoozed", "until_ts": r.get("until_ts"), "rec": r}
        elif d == "dismiss":
            state = {"state": "dismissed", "reason": r.get("reason"), "since_ts": r.get("since_ts"), "rec": r}
        elif d in ("clear", "snooze-expired"):
            state = None
    if state is None:
        return None
    rec = state.pop("rec")
    if not _quiescent_harness_live(session_dir, rec.get("pid"), rec.get("ts")):
        return None
    now = time.time() if now is None else now
    if state["state"] == "snoozed":
        until = state.get("until_ts")
        if not isinstance(until, (int, float)) or isinstance(until, bool) or until <= now:
            return None
        state["label"] = "idle alerts snoozed until " + time.strftime("%H:%MZ", time.gmtime(float(until)))
        return state
    if not isinstance(state.get("reason"), str):
        return None
    state["label"] = f"idle alerts dismissed until operator returns: {state['reason']}"
    return state


def _session_skill_sync(session_dir: str) -> Optional[dict]:
    """the session's MOST RECENT ``sync_my_skills`` call, read back from its
    ``{kind: skill-sync}`` provenance records (counts and skill names only; never file content):
    ``{decision, ok, ts, counts, written, blocked, corpus, label}`` or None when the session never called it."""
    path = os.path.join(session_dir, ".claude", "injection_log.jsonl")
    last = None
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if '"skill-sync"' not in ln:
                    continue
                try:
                    rec = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(rec, dict) and rec.get("kind") == "skill-sync":
                    last = rec
    except OSError:
        return None
    if last is None:
        return None
    counts = last.get("counts") if isinstance(last.get("counts"), dict) else {}
    written = [str(x) for x in (last.get("written") or [])][:20]
    blocked = [str(x) for x in (last.get("blocked") or [])][:20]
    decision = str(last.get("decision"))
    n_new = int(counts.get("CREATE", 0) or 0) + int(counts.get("UPDATE", 0) or 0)
    if decision in ("applied", "applied-partial"):
        label = f"skills applied: {len(written)} written" + (f", {len(blocked)} blocked" if blocked else "")
    elif decision == "dry-run":
        label = f"skills dry run: {n_new} to write" + (f", {len(blocked)} blocked" if blocked else "")
    else:
        label = f"skills sync {decision}"
    return {"decision": decision, "ok": bool(last.get("ok")), "ts": last.get("ts"), "corpus": last.get("corpus"),
            "counts": counts, "written": written, "blocked": blocked, "label": label}


def _source_def_size(source: Optional[str]) -> tuple:
    """(abs source path or None, size_info of the source file or None, error or None)."""
    from gnomon.agent_def import size_info

    if not source:
        return None, None, None
    path = os.path.abspath(os.path.expanduser(source))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return path, size_info(len(fh.read())), None
    except (OSError, UnicodeDecodeError) as exc:
        return path, None, f"{type(exc).__name__}: {exc}"


def _latest_renders() -> Dict[str, dict]:
    """instance_id -> the newest recorded render across that id's sessions."""
    latest: Dict[str, dict] = {}
    try:
        statuses = controller.scan_sessions()
    except Exception:  # a scan hiccup must not 500 the profiles list
        return latest
    for st in statuses:
        size = _session_def_size(st.session_dir)
        if size is None:
            continue
        prev = latest.get(st.instance_id)
        if prev is None or str(size.get("rendered_at") or "") > str(prev.get("rendered_at") or ""):
            latest[st.instance_id] = size
    return latest


def _profile_agent_def(prof, latest: Dict[str, dict]) -> dict:
    source = (getattr(prof, "claude_md_source", "") or "") if prof else ""
    path, size, err = _source_def_size(source)
    return {"source": path, "source_size": size, "source_error": err,
            "latest_render": latest.get(prof.instance_id) if prof else None,
            "optimizer": optimizer_profile() or None}


def optimize_prompt(instance_id: str, agent_def: dict) -> str:
    """The def-optimization request handed to the optimizer agent: names the def file(s)
    and their size (chars + approximate tokens)."""
    lines = [f"Agent-definition optimization pass, requested from the Gnomon for "
             f"the `{instance_id}` profile."]
    src, ss = agent_def.get("source"), agent_def.get("source_size")
    if src:
        sz = f"{ss['chars']:,} chars, ~{ss['approx_tokens']:,} tokens approx." if ss else "size unreadable"
        lines.append(f"Source def file (edit this one): {src} ({sz})")
    else:
        lines.append("Source def file: none configured (profile has no claude_md_source).")
    lr = agent_def.get("latest_render")
    if lr:
        lines.append(f"Latest rendered def (what the agent boots with, after lts expansion): "
                     f"{lr.get('path')} ({lr['chars']:,} chars, ~{lr['approx_tokens']:,} tokens approx., "
                     f"rendered {lr.get('rendered_at')}).")
    lines.append("Please run a def-optimization pass on this definition: reduce its size while "
                 "keeping every operative instruction, fetched pointer and node id; prefer fetching "
                 "over restating, and report the before/after size and anything you chose not to cut.")
    return "\n".join(lines)


@app.get("/api/agent-def/optimize/{instance_id}")
def agent_def_optimize(instance_id: str) -> dict:
    """What the "Optimize with <optimizer>" action needs: the prompt (naming the
    def path + size), the live optimizer session if one is running, and a
    COPYABLE command through the EXISTING launch mechanism
    (``python -m gnomon.harness.agent_launch launch --profile … --prompt …``).

    ⛔ ``POST /api/launch`` takes no custom prompt, so the web does NOT launch this
    itself — no new launch path is invented (``web_launch_supported: false``)."""
    by_id = {p.instance_id: p for p in controller.list_profiles()}
    prof = by_id.get(instance_id)
    if prof is None:
        raise HTTPException(status_code=404, detail=f"no readable profile for {instance_id!r}")
    ad = _profile_agent_def(prof, _latest_renders())
    prompt = optimize_prompt(instance_id, ad)
    opt = optimizer_profile()
    live = None
    try:
        for v in controller.live_sessions():
            if opt and v.instance_id == opt and v.live:
                live = {"instance_id": v.instance_id, "session_dir": v.session_dir}
                break
    except Exception:
        live = None
    command = (f"cd {shlex.quote(_PROJECT_ROOT)} && {shlex.quote(sys.executable)} -m "
               f"gnomon.harness.agent_launch launch --profile {shlex.quote(opt)} "
               f"--prompt {shlex.quote(prompt)}") if opt else ""
    return {
        "instance_id": instance_id,
        "agent_def": ad,
        "optimizer_profile": opt,
        "optimizer_profile_exists": bool(opt) and opt in by_id,
        "prompt": prompt,
        "command": command,
        "live_session": live,
        "web_launch_supported": False,
        "reason": ("The web launch (POST /api/launch) takes no custom prompt; run this command "
                   f"on the dashboard host (the existing agent_launch CLI), or, if a {opt} "
                   "session is live, paste the prompt into it.") if opt else
                  "No optimizer agent is configured for this deployment (defaults/optimizer.json).",
    }


@app.get("/api/profiles")
def list_profiles() -> list[dict]:
    """Agent profiles for the Agents list.

    Parity with the CTk ``AgentsTab._render_profiles``: every id that has a
    ``profile.json`` is listed; an id whose JSON does not parse is still listed
    (so Delete/Edit remain reachable) but marked unreadable.
    """
    by_id = {p.instance_id: p for p in controller.list_profiles()}
    latest = _latest_renders()
    rows: list[dict] = []
    for instance_id in controller.list_profile_ids():
        prof = by_id.get(instance_id)
        rows.append(
            {
                "instance_id": instance_id,
                "display_name": (prof.display_name if prof else "") or "",
                "readable": prof is not None,
                "model": (prof.model if prof else "") or "",
                "identity_class": (prof.identity_class if prof else "") or "",
                "agent_def": _profile_agent_def(prof, latest) if prof else None,
            }
        )
    return rows


@app.get("/api/sessions")
def list_sessions() -> list[dict]:
    """Live-sessions rows. Parity with the CTk ``AgentsTab._render_live``:
    on-disk liveness merged with the hub's per-session context %.
    Single-flight + short TTL (poll-pileup fix): concurrent polls share one computation."""
    return _sessions_cache.get("sessions", _list_sessions_uncached)


def _list_sessions_uncached() -> list[dict]:
    try:
        views = controller.live_sessions()
    except Exception as exc:  # a scan hiccup must not 500 the whole tab
        raise HTTPException(status_code=503, detail=f"liveness scan error: {exc}")
    hold_on = _flag("notice_hold_chip")
    modal_on = _flag("modal_hold")
    quiescent_on = _flag("quiescent_state")
    snooze_on = _flag("idle_snooze")
    skill_sync_on = _flag("sync_my_skills")
    build_on = _flag("build_stamp_chip")
    current_id = _current_build(buildstamp.SESSION).get("build_id") if build_on else None

    def _row_build(session_dir: str) -> Optional[dict]:
        if not build_on:
            return None
        sid = buildstamp.read_session_build_id(session_dir)
        return {"id": sid, "current_id": current_id,
                "state": buildstamp.build_state(sid, current_id)}

    waiting_on = _flag("waiting_on_operator_alert")

    def _row(v) -> dict:
        row = _row_base(v)
        if waiting_on:      # the key exists ONLY with the flag ON (OFF = the rows are byte-identical to before)
            row["waiting"] = _session_waiting(v.session_dir, _alerts_store())
        return row

    def _row_base(v) -> dict:
        return {
            "instance_id": v.instance_id,
            "session_dir": v.session_dir,
            "live": v.live,
            "reason": v.reason,
            "used_percentage": v.used_percentage,
            "agent_def": _session_def_size(v.session_dir),
            # gated by notice_hold_chip.
            "notice_hold": _session_notice_hold(v.session_dir) if hold_on else None,
            # gated by modal_hold: a STUCK hold (null otherwise).
            "modal_hold": _session_modal_hold(v.session_dir) if modal_on else None,
            # gated by quiescent_state: "quiescent: <reason> since <time>".
            "quiescent": _session_quiescent(v.session_dir) if quiescent_on else None,
            # gated by idle_snooze: "idle alerts dismissed until operator returns: <reason>".
            "idle_snooze": _session_idle_snooze(v.session_dir) if snooze_on else None,
            # gated by sync_my_skills: "skills applied: 1 written" (the last sync_my_skills call).
            "skill_sync": _session_skill_sync(v.session_dir) if skill_sync_on else None,
            # gated by build_stamp_chip: the session's launch.json
            # build_id vs the tree on disk now (the chip logic is build_chip.js).
            "build": _row_build(v.session_dir),
        }

    return [_row(v) for v in views]


@app.post("/api/launch")
def launch(req: LaunchRequest) -> dict:
    """Trigger a DAEMON-SIDE launch of a colleague (terminal-launch).

    Calls the reused ``Controller.launch`` with ``open_terminal=True``: on this
    host that runs the existing ``LaunchManager.open_terminal`` osascript path,
    opening the configured terminal (iTerm2 / Terminal.app) and starting the
    harness-wrapped ``claude``. The browser only triggered it — the daemon acted.
    """
    instance_id = (req.instance_id or "").strip()
    if not instance_id:
        raise HTTPException(status_code=400, detail="instance_id is required")
    try:
        result = controller.launch(instance_id, open_terminal=True)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"no profile for {instance_id!r}")
    except ValueError as exc:  # unsafe instance_id, etc.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # launch/terminal failure
        raise HTTPException(status_code=500, detail=f"launch failed: {exc}")
    return {
        "ok": True,
        "instance_id": result.instance_id,
        "session_dir": result.session_dir,
        "lts_expansion_pending": result.lts_expansion_pending,
    }


# --- DSD tab (slice 2 —) ------------------------------------
#
# The browser-facing presentation plane for the DSD tab. It REUSES the CTk-free
# ``DsdTabController`` (``gnomon.app.dsd_controller``) verbatim — the SAME
# controller ``gnomon.app.dsd_tab.DsdTab`` (the CustomTkinter view) drives —
# and reimplements NONE of its logic. Every gate lives in that controller and the
# ``DsdRoundTrip`` beneath it:
#
# * the CONTEXT GUARD — ``present`` refuses a DSD missing a
#     required What-this-is / Why-it-matters — runs server-side in the controller;
#   * the COMPLETENESS GATE + escape-hatch-requires-a-comment + single-select-v1
# run server-side in ``DsdRoundTrip.answer``, which
#     ``submit`` calls. ⛔ The browser posts what the human entered; it is NEVER the
# gate. This is the exact defect guards against — the original CTk build
#     minted a "complete" answer from a partial form via a UI-ONLY gate.
#
# The auto-switch-on-new-submission behaviour (operator-requested; the CTk build's
# ``DsdArrivalWatcher``) is exposed as ``GET /api/dsd/arrivals``: a poll that
# reports pending DSDs that ARRIVED since the previous poll (priming on first call),
# which the frontend uses to focus the DSD tab + select the new DSD.

# One DSD controller + arrival watcher per underlying Controller identity. The
# watcher is stateful (its "already seen" set), so it must persist across polls;
# it is rebuilt only when the process Controller is swapped (the test seam).
_dsd_state: dict = {"controller_id": None, "dsd": None, "watcher": None}


def get_dsd_controller():
    """The reused ``DsdTabController`` for the current process Controller.

    Built via ``Controller.dsd_controller()`` — the SAME factory the CTk shell uses
    (``gui.GnomonApp`` -> ``controller.dsd_controller()``): the DSD inbox
    (read side of the hub's on-disk DSD store), a ``DsdRoundTrip`` over the co-hosted
    hub's live registry (so a saved answer injects the root node-id to the requesting
    proxy, D15), the REAL typed writer (D14), and the bounded supersession resolver.
    """
    from gnomon.app.dsd_controller import DsdArrivalWatcher

    if _dsd_state["controller_id"] != id(controller):
        controller.flag_reader = lambda name: _flag(name)      # ONE flag reader for the tab, the count and the answer-node fields
        dsd = controller.dsd_controller()
        _dsd_state.update(controller_id=id(controller), dsd=dsd,
                          watcher=DsdArrivalWatcher(dsd))
    return _dsd_state["dsd"]


def get_dsd_watcher():
    get_dsd_controller()  # ensures the watcher is bound to the current controller
    return _dsd_state["watcher"]


# -- serialization: dataclasses -> JSON-safe dicts (no logic; just marshalling) --

def _live_to_dict(live) -> Optional[dict]:
    if live is None:
        return None
    return {
        "live": live.live,
        "corpus_id": live.corpus_id,
        "engine_available": live.engine_available,
        "registered": live.registered,
        "reachable": live.reachable,
        "node_visible": live.node_visible,
        "registered_corpora": list(live.registered_corpora),
        "banner": live.banner(),
        "error": live.error,
    }


def _writer_status_to_dict(ws) -> Optional[dict]:
    if ws is None:
        return None
    return {"kind": ws.kind, "target": ws.target, "detail": ws.detail,
            "live": _live_to_dict(ws.live)}


def _decision_to_dict(d) -> dict:
    return {
        "id": d.id,
        "question": d.question,
        "what_this_is": d.what_this_is,
        "why_it_matters": d.why_it_matters,
        "theme": d.theme,
        "blocker": d.blocker,
        "options": [
            {
                "option_id": o.option_id,
                "label": o.label,
                "tradeoffs": o.tradeoffs,
                "recommended_row": o.recommended_row,
                "is_preselected": o.is_preselected,   # always False (operator 2026-09-29)
                "is_recommended": o.is_recommended,
                "is_escape_hatch": o.is_escape_hatch,
            }
            for o in d.options
        ],
        "preselect_id": d.preselect_id,               # always None (operator 2026-09-29)
        "recommended_id": d.recommended_id,
        "recommendation_source": d.recommendation_source,
        "has_recommendation": d.has_recommendation,
        "reco_text": d.reco_text,
        "multi": d.multi,
        "escape_hatch_id": d.escape_hatch_id,
        "escape_hatch_label": d.escape_hatch_label,
        "notes": list(d.notes),
        "documents": list(getattr(d, "documents", []) or []),   #
    }


def _iso_or_none(epoch):
    from gnomon.dsd.senttime import iso_utc
    return iso_utc(epoch)


def _present_to_dict(view) -> dict:
    return {
        "ok": view.ok,
        "message": view.message,
        "dsd_id": view.dsd_id,
        "instance_id": view.instance_id,
        "n_decisions": view.n_decisions,
        **_sent_fields(getattr(view, "created_epoch", 0.0)),
        "documents": list(getattr(view, "documents", []) or []),   # (DSD level)
        "form": (
            None if view.form is None else {
                "dsd_id": view.form.dsd_id,
                "instance_id": view.form.instance_id,
                "decisions": [_decision_to_dict(d) for d in view.form.decisions],
            }
        ),
    }


def _supersession_to_dict(ss) -> Optional[dict]:
    if ss is None:
        return None
    return {
        "node_id": ss.node_id,
        "superseded": ss.superseded,
        "referrers": list(ss.referrers),
        "mechanism": ss.mechanism,
        "confidence": ss.confidence,
        "note": ss.note,
    }


# -- routes --------------------------------------------------------------------

@app.get("/api/dsd/status")
def dsd_status() -> dict:
    """The writer target + the LIVE lts-engine verdict for the tab banner (D1/D2).
    When the DSD-answer corpus is not live-registered, ``live.banner`` complains
    LOUDLY (D2: no silent degrade to stale local data)."""
    ws = get_dsd_controller().writer_status()
    return {"writer": _writer_status_to_dict(ws)}


# -- the answer-notice state per DSD ("queued for <instance>") ----------
#
# READ-ONLY over the hub's durable notice queue (``NoticeQueue.peek`` — never
# ``pending``, which MOVES expired/corrupt files) and the owning harness's own
# ``{kind: dsd-notice, decision: written}`` provenance record (the delivery proof).
# A DSD waiting on the operator's own answer ("pending") never carries a notice state.

#: path -> (mtime_ns, size, {dsd_id: ts}) — the 5s DSD poll re-reads a session log
#: only when it changed.
_written_cache: Dict[str, tuple] = {}


def _notice_queue():
    try:
        return getattr(controller.hub, "notice_queue", None)
    except Exception:  # noqa: BLE001 - a hub hiccup must not 500 the DSD list
        return None


def _queue_snapshots() -> Optional[Dict[str, dict]]:
    """``{instance_id: peek}`` or None when no queue is wired / readable."""
    q = _notice_queue()
    if q is None:
        return None
    try:
        return q.peek_all()
    except Exception as exc:  # noqa: BLE001
        print(f"[gm-web] notice queue peek failed: {exc}", flush=True)
        return None


def _written_times_for(instance_ids) -> Dict[str, float]:
    """``{dsd_id: first-written ts}`` across every on-disk session of these ids."""
    from gnomon.dsd.notice import written_notice_times
    from gnomon.harness.hooks import injection_log_path

    wanted = set(instance_ids)
    out: Dict[str, float] = {}
    if not wanted:
        return out
    try:
        statuses = controller.scan_sessions()
    except Exception:  # noqa: BLE001
        return out
    for st in statuses:
        if st.instance_id not in wanted:
            continue
        path = injection_log_path(st.session_dir)
        try:
            fst = os.stat(path)
        except OSError:
            continue
        key = (fst.st_mtime_ns, fst.st_size)
        hit = _written_cache.get(path)
        if hit is None or hit[:2] != key:
            hit = key + (written_notice_times(path),)
            _written_cache[path] = hit
        for dsd_id, ts in hit[2].items():
            if dsd_id not in out or ts < out[dsd_id]:
                out[dsd_id] = ts
    return out


def _notice_states(items) -> Dict[str, Optional[dict]]:
    """dsd_id -> notice state for each ANSWERED item:
    ``{state: queued, instance_id, since_epoch}`` | ``{state: expired, instance_id}``
    | ``{state: delivered, instance_id, delivered_epoch}`` | None (not knowable)."""
    answered = [it for it in items if it.kind == "answered"]
    states: Dict[str, Optional[dict]] = {it.dsd_id: None for it in items}
    if not answered:
        return states
    snaps = _queue_snapshots() or {}
    unresolved = []
    for it in answered:
        snap = snaps.get(it.instance_id) or {}
        queued = {q["dsd_id"]: q for q in snap.get("queued", [])}
        if it.dsd_id in queued:
            states[it.dsd_id] = {"state": "queued", "instance_id": it.instance_id,
                                 "since_epoch": queued[it.dsd_id].get("enqueued_epoch")}
        elif it.dsd_id in set(snap.get("expired_ids", [])):
            states[it.dsd_id] = {"state": "expired", "instance_id": it.instance_id}
        else:
            unresolved.append(it)
    if unresolved:
        written = _written_times_for({it.instance_id for it in unresolved})
        for it in unresolved:
            if it.dsd_id in written:
                states[it.dsd_id] = {"state": "delivered", "instance_id": it.instance_id,
                                     "delivered_epoch": written[it.dsd_id]}
    return states


@app.get("/api/dsd/list")
def dsd_list() -> list[dict]:
    """Left-pane list: pending DSDs to present + prior DSDs to browse, newest first
    (D12 dual-pane). Parity with the CTk ``DsdTab.refresh`` / ``controller.list_dsds``.
 each row carries ``notice`` (the answer-notice state, see
    :func:`_notice_states`) — read-only, safe to poll."""
    return _dsd_list_cache.get("dsd-list", _dsd_list_uncached)


def _dsd_list_uncached() -> list[dict]:
    # pre-review B1: with dsd_self_verbs ON a withdrawn / replaced DSD is listed as such; OFF = hidden, as before.
    marked_on = _flag("dsd_self_verbs")
    items = get_dsd_controller().list_dsds(include_marked=True) if marked_on else get_dsd_controller().list_dsds()
    # gated by dsd_queued_display: off -> no chip data.
    notices = _notice_states(items) if _flag("dsd_queued_display") else {}
    rows = [
        {
            "dsd_id": it.dsd_id,
            "instance_id": it.instance_id,
            "kind": it.kind,           # "pending" | "answered" | "unrenderable"
            "title": it.title,
            "created_epoch": it.created_epoch,   # raw: order_rows sorts on it (0.0 = unknown)
            "created_iso": _sent_fields(it.created_epoch)["created_iso"],   # null if unusable
            "root_node_id": it.root_node_id,
            "notice": notices.get(it.dsd_id),
            # ANSWER time + provenance ("recorded" | "derived-marker"
            # (approximate) | "missing" -> null). Never the filing time.
            "answered_epoch": it.answered_epoch,
            "answered_iso": _iso_or_none(it.answered_epoch),
            "answered_source": it.answered_source,
            **({"mark": it.mark} if it.mark is not None else {}),
        }
        for it in items
    ]
    # section/sender annotation + display order, re-evaluated against
    # now() on EVERY call (the UI polls), so the 24 h boundary moves with the clock.
    return order_rows(rows, time.time())


@app.get("/api/dsd/notices")
def dsd_notices() -> dict:
    """quiet warning: per-instance queued / expired / corrupt counts over
    the durable notice queue. PURE read (``NoticeQueue.peek``).
 ``enabled`` mirrors dsd_queued_display; off -> no counts."""
    if not _flag("dsd_queued_display"):
        return {"enabled": False, "available": False, "instances": {}, "total_queued": 0,
                "total_expired": 0, "total_corrupt": 0}
    snaps = _queue_snapshots()
    if snaps is None:
        return {"enabled": True, "available": False, "instances": {}, "total_queued": 0,
                "total_expired": 0, "total_corrupt": 0}
    inst = {i: {"queued": len(s.get("queued", [])), "expired": int(s.get("expired", 0)),
                "corrupt": int(s.get("corrupt", 0))} for i, s in snaps.items()}
    out = {"enabled": True, "available": True, "instances": inst,
           "total_queued": sum(v["queued"] for v in inst.values()),
           "total_expired": sum(v["expired"] for v in inst.values()),
           "total_corrupt": sum(v["corrupt"] for v in inst.values())}
    if _flag("dsd_notice_inline"):
        # pre-review B2 (count only), flag OFF = these keys do not exist: notices held longer than 120 s, per agent.
        held = _held_notice_counts()
        if held is None:      # the live sessions could not be listed: an ERROR, shown as one, never zeros
            for row in inst.values():
                row["held_notices"] = None
                row["held_notices_error"] = "live sessions unavailable"
            out["total_held_notices"] = None
            out["held_notices_error"] = "live sessions unavailable"
            return out
        for name, row in inst.items():
            n = held.get(name, 0)
            row["held_notices"] = n
            if n is None:
                row["held_notices_error"] = "session log unreadable"
        for name, n in held.items():       # a live session with no queue directory still gets its row
            if name not in inst:
                inst[name] = {"queued": 0, "expired": 0, "corrupt": 0, "held_notices": n}
                if n is None:
                    inst[name]["held_notices_error"] = "session log unreadable"
        out["total_held_notices"] = sum(v["held_notices"] or 0 for v in inst.values())
        if any(v["held_notices"] is None for v in inst.values()):
            out["held_notices_error"] = "some counts unavailable"
    return out


@app.get("/api/dsd/present/{dsd_id}")
def dsd_present(dsd_id: str) -> dict:
    """Open a PENDING DSD as a form (right pane). GUARDED server-side: an empty /
    whitespace-only DSD, or one missing the required What-this-is / Why-it-matters
 context, returns ``ok=false`` with the refusal (context-guard) —
    the browser never gets a form to render for a context-less DSD."""
    return _present_to_dict(get_dsd_controller().present(dsd_id))


@app.get("/api/dsd/browse/{dsd_id}")
def dsd_browse(dsd_id: str) -> dict:
    """Render an ANSWERED DSD read-only (D12: responses non-editable): the written-
    back markdown, the recorded answers, and the live supersession status (bounded
 confidence today)."""
    view = get_dsd_controller().browse(dsd_id)
    notice = None
    if view.ok and _flag("dsd_queued_display"):
        try:
            notice = _notice_states(
                [it for it in get_dsd_controller().list_dsds() if it.dsd_id == view.dsd_id]
            ).get(view.dsd_id)
        except Exception:  # noqa: BLE001 - the chip is advisory; never fail the browse
            notice = None
    return {
        "notice": notice,
        "ok": view.ok,
        "message": view.message,
        "dsd_id": view.dsd_id,
        "instance_id": view.instance_id,
        "root_node_id": view.root_node_id,
        **_sent_fields(getattr(view, "created_epoch", 0.0)),
        "answered_epoch": getattr(view, "answered_epoch", None),
        "answered_iso": _iso_or_none(getattr(view, "answered_epoch", None)),
        "answered_source": getattr(view, "answered_source", "missing"),
        "markdown": view.markdown,
        "answers": [
            {"decision_id": a.decision_id, "question": a.question,
             "your_call": a.your_call, "comment": a.comment}
            for a in view.answers
        ],
        "supersession": _supersession_to_dict(view.supersession),
        "documents": list(getattr(view, "documents", []) or []),   # (all scopes)
    }


# -- DSD documents - the operator's Open action ---------------------------
#
# ONE route, operator-only BY CONSTRUCTION: it is a web-UI route and there is no agent verb
# for it (no hub tool, no self-verb, no \command). It takes (dsd_id, scope, index), NEVER a
# path: only a document the stored record lists can be opened, and the policy
# (regular file, allow-listed root after symlink resolution, document extension, no
# executable bit) is re-evaluated at open time. The launcher is injected; tests set
# ``_dsd_doc_opener`` and nothing in a test run launches anything.
_DOC_OPEN_KEYS = frozenset({"dsd_id", "scope", "index"})
#: Test seam: ``callable(path) -> None``. None = the real macOS ``open``.
_dsd_doc_opener = None
_DOC_REFUSAL_STATUS = {
    dsd_documents_mod.REFUSAL_UNKNOWN_DSD: 404,
    dsd_documents_mod.REFUSAL_BAD_INDEX: 404,
    dsd_documents_mod.REFUSAL_BAD_SCOPE: 400,
    dsd_documents_mod.REFUSAL_OPENER_FAILED: 502,
}


@app.post("/api/dsd/documents/open")
async def dsd_document_open(request: Request) -> dict:
    """Open one document of a stored DSD (``{dsd_id, scope: "top"|"D<n>", index}``).
    Every refusal is NAMED in the error detail (``<code>: <reason>``).

    KNOWN LIMIT (alpha-architect ruling 2026-10-02): this route is NOT authenticated and carries no
    operator token. Every same-uid agent already has a shell and can run ``open <file>`` itself, so
    gating the route would add no protection. Its protections are: only documents the stored DSD
    lists can be named (dsd_id + scope + index, never a path), the allow-listed roots (after symlink
    resolution), the extension list, and the no-executable-bit rule; .html/.htm/.svg open as text
    (``open -t``), never in a browser."""
    _open_guard(request)
    # Same posture as the deliverables mark-seen route: only application/json, so a
    # cross-origin form / text/plain "simple request" (no CORS preflight) is refused.
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="open requires Content-Type: application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    extra = sorted(set(body) - _DOC_OPEN_KEYS)
    if extra or set(body) != _DOC_OPEN_KEYS:
        raise HTTPException(status_code=400, detail=(
            f"body must be exactly {{dsd_id, scope, index}}; refused key(s): {extra or '(none)'}; "
            f"a raw path is never accepted - name a document by its dsd_id + scope + index"))
    opener = _dsd_doc_opener or dsd_documents_mod.system_opener
    try:
        res = await run_in_threadpool(
            lambda: get_dsd_controller().open_document(
                body["dsd_id"], body["scope"], body["index"], opener=opener))
    except dsd_documents_mod.DocumentRefused as exc:
        raise HTTPException(status_code=_DOC_REFUSAL_STATUS.get(exc.code, 403),
                            detail=f"{exc.code}: {exc.message}")
    except Exception as exc:  # noqa: BLE001 - loud, never a silent no-op
        raise HTTPException(status_code=500, detail=f"open failed: {type(exc).__name__}: {exc}")
    print(f"[gm-web] dsd document open {body['dsd_id']} {body['scope']}[{body['index']}] "
          f"-> {res['name']}", flush=True)
    return {"ok": True, "name": res["name"], "scope": res["scope"], "index": res["index"]}


@app.post("/api/dsd/submit/{dsd_id}")
def dsd_submit(dsd_id: str, req: DsdSubmitRequest, request: Request) -> dict:
    """Save the human's answers for a pending DSD.

    ⛔ The COMPLETENESS GATE is enforced SERVER-SIDE, not here and not in the
    browser: ``controller.submit`` -> ``DsdRoundTrip.answer`` refuses the whole save
    (mints nothing, injects nothing, leaves the record PENDING) when any decision is
    unanswered, the escape hatch has no comment, or single-select v1 is violated.
    A refused save returns ``ok=false`` + ``gate_failures`` — a real refusal a
    partial form cannot slip past."""
    selections = {
        dec_id: ((sel.option_id or None), sel.comment or "")
        for dec_id, sel in req.selections.items()
    }
    # g76: the submit must come with recent real in-tab input; there is no bypass. Checked
    # BEFORE anything is minted or injected; a refusal leaves the record PENDING (the page keeps the draft).
    evidence_text = None
    if _flag(dsd_submit_proof_mod.FLAG):
        ok_act, ev, why = dsd_submit_proof_mod.check_activity(req.activity, request.headers.get("user-agent", ""))
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if not ok_act:
            print(f"[gm-web] dsd submit {dsd_id}: REFUSED no recent in-tab input "
                  f"({dsd_submit_proof_mod.evidence_text(ev, stamp)})", flush=True)
            return {"ok": False, "message": why, "dsd_id": dsd_id, "root_node_id": None, "minted_node_ids": [],
                    "injected": False, "notice_queued": False, "gate_failures": [], "writer": None, "live": None,
                    "activity_refused": True}
        evidence_text = dsd_submit_proof_mod.evidence_text(ev, stamp)
    else:
        evidence_text = dsd_submit_proof_mod.evidence_text_off(
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    result = get_dsd_controller().submit(dsd_id, selections, submit_evidence=evidence_text)
    # D15 amendment (2026-09-29): LOG the delivery outcome — a bare 200 is what
    # hid the silent drop. One line per save.
    print(f"[gm-web] dsd submit {dsd_id}: ok={result.ok} root={result.root_node_id or '-'} "
          f"agent={getattr(result, 'instance_id', '') or '-'} "
          f"notice_queued={getattr(result, 'notice_queued', False)} "
          f"handed_to_live_harness={result.injected}", flush=True)
    if result.ok:
        _clear_dsd_review(dsd_id)
    return {
        "ok": result.ok,
        "message": result.message,
        "dsd_id": result.dsd_id,
        "root_node_id": result.root_node_id,
        "minted_node_ids": list(result.minted_node_ids),
        "injected": result.injected,
        "notice_queued": bool(getattr(result, "notice_queued", False)),
        "gate_failures": list(result.gate_failures),
        "writer": _writer_status_to_dict(result.writer),
        "live": _live_to_dict(result.live),
    }


# -- DSD review activity -> operator presence (operator, 2026-09-29) ---------------------
#
# The review page beacons ``{dsd_id}`` on radio change / comment input (throttled
# in the page: first interaction + at most 1 / 10 s per dsd_id; fire-and-forget).
# This route validates the id, resolves the OWNING instance from the DSD record,
# and writes a review beacon into the SAME hardened run dir the X1 operator
# beacons use (``harness/operator_activity.py``), rate-limited per dsd_id. Every
# harness's stall nudge then knows "<operator> is reviewing <owner>'s DSD". Never any
# answer content: only the dsd_id is read from the body, and the beacon holds
# ``{schema, kind, dsd_id, owner_instance, last_ts}``. Flag
# ``dsd_review_presence`` (default ON) off = 204 and nothing written.

#: Server-side floor between two beacon WRITES for one dsd_id (the page sends at
#: most one per 10 s; this collapses bursts from several tabs).
DSD_ACTIVITY_MIN_INTERVAL = 5.0
_dsd_activity_clock = time.time
_dsd_activity_lock = threading.Lock()
#: dsd_id -> last write ts. The status check AND the beacon write happen under
#: ``_dsd_activity_lock``, the same lock ``_clear_dsd_review`` (called after a
#: successful submit) takes — so a beacon landing after a submit either finished
#: before the clear (and is removed by it) or sees the record ANSWERED and writes
#: nothing. It can never recreate the file the submit just cleared.
_dsd_activity_last: Dict[str, float] = {}


def _dsd_activity_reset() -> None:
    """Test seam: forget the per-dsd_id rate limit."""
    with _dsd_activity_lock:
        _dsd_activity_last.clear()


class DsdActivityRequest(BaseModel):
    dsd_id: Any = None         # validated by hand: a non-string is a 400, not a 500


@app.api_route("/api/dsd/id-lookup", methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
               include_in_schema=False)
async def dsd_id_lookup_used(request: Request) -> Response:
    """usage beacon (g1/g23): the DSD tab's open-by-id box was used. ``POST {}`` (JSON), 204, nothing stored
    here; the web-api feature middleware records the ``dsd-id-lookup`` event (feature_sites.WEB_ROUTES). Flag OFF
    (``dsd_id_lookup``): EVERY method answers the 404 an unknown path gets, as before the route existed.
    Flag ON: a foreign Host/Origin is 403 (_open_guard), a non-JSON content type 415, an empty or non-object body 400,
    any method but POST 405."""
    if not _flag("dsd_id_lookup"):
        raise HTTPException(status_code=404, detail="Not Found")
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _open_guard(request)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="the beacon requires Content-Type: application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="the beacon requires a JSON object body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="the beacon requires a JSON object body")
    return Response(status_code=204)


@app.post("/api/dsd/activity", status_code=204)
def dsd_activity(req: DsdActivityRequest) -> Response:
    from gnomon.harness import operator_activity as oa

    dsd_id = req.dsd_id
    if not isinstance(dsd_id, str) or not oa.DSD_REVIEW_ID_RE.match(dsd_id):
        raise HTTPException(status_code=400, detail="invalid dsd_id")
    if not _flag("dsd_review_presence"):
        return Response(status_code=204)
    now = _dsd_activity_clock()
    with _dsd_activity_lock:
        last = _dsd_activity_last.get(dsd_id)
        if last is not None and now - last < DSD_ACTIVITY_MIN_INTERVAL:
            return Response(status_code=204)          # rate-limited: already fresh
        # status is read at WRITE time (never cached): only a PENDING DSD is
        # "being reviewed". An answered one (stale tab / late beacon) -> quiet 204.
        state, owner = get_dsd_controller().review_owner(dsd_id)
        if state == "missing":
            raise HTTPException(status_code=404, detail="no such DSD")
        if state != "pending":
            return Response(status_code=204)
        try:
            oa.write_dsd_review_beacon(dsd_id, owner or "", now=now)
        except (OSError, ValueError) as exc:
            # presence is advisory: never fail the page, but say so in the log
            print(f"[gm-web] dsd review beacon for {dsd_id} not written: {exc}", flush=True)
            return Response(status_code=204)
        _dsd_activity_last[dsd_id] = now
    return Response(status_code=204)


def _clear_dsd_review(dsd_id: str) -> None:
    """An answered DSD is no longer being reviewed: drop its beacon (best-effort).
    Under the activity lock, so an in-flight beacon write finishes first."""
    try:
        from gnomon.harness import operator_activity as oa

        with _dsd_activity_lock:
            oa.remove_dsd_review_beacon(dsd_id)
            _dsd_activity_last.pop(dsd_id, None)
    except Exception:  # noqa: BLE001 - housekeeping never fails a save
        pass


@app.get("/api/dsd/arrivals")
def dsd_arrivals() -> dict:
    """Poll for pending DSDs that ARRIVED since the previous poll (auto-switch, operator-
    requested; the CTk build's ``DsdArrivalWatcher``). The FIRST poll primes and
    returns ``[]`` — DSDs already on disk when the page opens do NOT trigger an
    auto-switch; only a genuine arrival while the page is open does."""
    new_items = get_dsd_watcher().poll()
    if new_items:
        # A DSD that just arrived (filed by an agent, outside this process) must be in the very next list read,
        # not up to one TTL later. Only on a NON-empty result: invalidating on every empty tick would defeat the cache.
        _dsd_list_cache.invalidate()
    _alert_dsd_arrivals(new_items)   # an info alert per arrival (flag off: nothing); never raises
    return {
        "arrivals": [
            {"dsd_id": it.dsd_id, "instance_id": it.instance_id,
             "kind": it.kind, "title": it.title}
            for it in new_items
        ]
    }


# --- Config tab (slice 4 — machine config over ConfigStore) --------
#
# The browser-facing presentation plane for the Configuration tab. It REUSES the
# CTk-free ``ConfigController`` (``gnomon.app.config_controller``) verbatim
# — the SAME controller the CustomTkinter ``ConfigTab`` drives — over the SAME
# ``ConfigStore`` (``~/gnomon-data/config.json``), and reimplements NONE of its
# logic. Every field description, every validation, and the D9 guard live in that
# controller:
#
#   * field kinds + labels + help come from ``ConfigController.field_specs`` (the
#     tab renders what the controller describes — parity with ``config_tab.py``);
#   * per-field parsing/validation (path/text/csv/int_csv, the non-empty MCP set)
#     runs in ``ConfigController.set``;
#   * ⛔ the D9 invariant — a measurement-surface server must never be in the default MCP set — is
#     refused SERVER-SIDE in ``ConfigController.set`` AND again in ``.save``. The
#     browser is not the gate; a POST carrying that server is refused and NOTHING is
#     persisted (the same shape as the DSD completeness gate).
#
# A save notifies the process ``Controller`` (``reload_config``) so launch-time
# bits take effect on the next launch — exactly as ``config_tab.build_config_tab``
# wires ``on_saved=app_controller.reload_config``.

#: Test seam: an injected ``ConfigStore`` (tmp path) overrides the machine default
#: ``~/gnomon-data/config.json``. Production leaves this None.
_config_store = None  # type: ignore[assignment]


def get_config_controller() -> ConfigController:
    """The reused ``ConfigController`` over this machine's config store.

    Fresh per request (stateless: a GET reads, a POST validates+saves+reloads),
    seeded from the store so GET and POST see one source of truth. On save it
    notifies the process Controller so the next launch uses the new config.
    """
    store = _config_store if _config_store is not None else (
        ConfigStore() if ConfigStore is not None else None
    )
    return ConfigController(store=store, on_saved=controller.reload_config)


def _reserved(spec) -> bool:
    """A field the CTk tab renders but that drives NO live behavior today (it is
    labelled "(reserved)" in its ``FieldSpec``). Surfaced so the web tab flags it
    exactly as the CTk tab does, rather than silently presenting it as live."""
    return "reserved" in spec.label.lower()


def _machine_config_store():
    """The config store the flags read — the same test seam as the Config tab."""
    if _config_store is not None:
        return _config_store
    return ConfigStore() if ConfigStore is not None else None


# --- the app-activity poller (Apps host -> idle message) -------------------------------------------------
#: the running poller (None before the lifespan starts it, or when not started)
_app_activity_poller = None
#: Test seam: an ActivityPoller factory. None = the production poller (and none at all under pytest, g31).
_app_activity_factory = None


def _start_app_activity() -> None:
    global _app_activity_poller
    try:
        if _app_activity_factory is not None:
            poller = _app_activity_factory()
        elif os.environ.get("PYTEST_CURRENT_TEST"):
            return   # g31: no test run starts the live poller
        else:
            from gnomon.appshost import activity as _activity

            poller = _activity.ActivityPoller(_apps_host, _flag, log=lambda m: print(m, flush=True))
        _app_activity_poller = poller
        poller.start()
        print(f"[gm-web] app-activity poller started (every {poller.interval:.0f}s, while app_activity_presence "
              "and apps_tab are on)", flush=True)
    except Exception as exc:  # noqa: BLE001 - the server must come up regardless
        print(f"[gm-web] app-activity poller NOT started: {type(exc).__name__}: {exc}", flush=True)


# --- (revision 2026-10-27): the transcript archive timer ---------------------------------------------------
#: the running archive job (None before the lifespan starts it, or when not started)
_transcript_archive_job = None
#: Test seam: an ArchiveJob factory. None = the production job (and none at all under pytest, g31).
_transcript_archive_factory = None


def _archive_subagents_setting() -> str:
    store = _machine_config_store()
    try:
        cfg = store.load() if store is not None else None
    except Exception:  # noqa: BLE001
        cfg = None
    from gnomon import transcript_archive as _ta

    return _ta.configured_subagents(cfg) if cfg is not None else _ta.DEFAULT_SUBAGENTS


def _start_transcript_archive() -> None:
    """Archive once now and every 10 minutes while ``transcript_archive`` is ON. Never fatal, never raises."""
    global _transcript_archive_job
    try:
        if _transcript_archive_factory is not None:
            job = _transcript_archive_factory()
        elif os.environ.get("PYTEST_CURRENT_TEST"):
            return   # g31: no test run starts the live archive timer
        else:
            from gnomon import transcript_archive_job as _taj

            job = _taj.ArchiveJob(flag_reader=_flag, subagents=_archive_subagents_setting,
                                  log=lambda m: print(m, flush=True))
        _transcript_archive_job = job
        job.start()
        print(f"[gm-web] transcript archive timer started (now, then every {job.interval:.0f}s, while "
              "transcript_archive is on)", flush=True)
    except Exception as exc:  # noqa: BLE001 - the server must come up regardless
        print(f"[gm-web] transcript archive timer NOT started: {type(exc).__name__}: {exc}", flush=True)


def _flag(name: str, default: Optional[bool] = None) -> bool:
    """a machine-level feature flag (no profile — these are page-wide
    display features). Never raises: a broken config falls back to the default.
 ``default`` is passed through to ``flags.flag`` — None keeps the
    registry default, exactly as before."""
    store = _machine_config_store()
    try:
        cfg = store.load() if store is not None else None
    except Exception:  # noqa: BLE001
        cfg = None
    return flags.flag(name, config=cfg if cfg is not None else flags._Empty, default=default)


@app.get("/api/flags")
def flags_get() -> dict:
    """every feature flag with its effective value and source
    (``machine`` / ``default``; profile overrides are per-instance and not shown
    at this page-wide level). READ-ONLY — editing flags from the web is out of
    scope; set ``flags`` in the machine config."""
    store = _machine_config_store()
    try:
        cfg = store.load() if store is not None else None
    except Exception:  # noqa: BLE001
        cfg = None
    return {
        "config_path": store.path if store is not None else None,
        "flags": flags.effective_flags(config=cfg if cfg is not None else flags._Empty),
    }


# --- the Config tab's read-only Verb policy (TOML) view ----------------------

#: Test seams: None = the loader's own defaults (``controller.config.runtime_root`` for the
#: override, the package ``verb_policy.toml`` for the default).
_verb_policy_runtime_root: Optional[str] = None
_verb_policy_default_path: Optional[str] = None


def _rule_dict(rule) -> dict:
    """Serialise EVERY field of the loader's ``Rule`` (g38b: derived from the type, so a new
    field shows up here automatically; tuples become lists for JSON)."""
    import dataclasses
    return {f.name: (list(v) if isinstance(v, tuple) else v)
            for f in dataclasses.fields(rule) for v in [getattr(rule, f.name)]}


def _raw_policy_file(path: str) -> dict:
    """Verbatim text of ONE of the two fixed policy files, via the loader's own reader.
    g38c: a symlink at the path is reported (with its resolved target), not hidden; the
    content served is still exactly what the loader reads."""
    text, why = verb_policy_mod._read_text(path)
    link = os.path.islink(path)
    return {"path": path, "present": text is not None, "text": text,
            "error": None if text is not None or why == "absent" else why,
            "symlink": link, "resolved": os.path.realpath(path) if link else None}


# --- Home tab "read" button -- read-only def view + macro-expander preview ---
#
# Instance ids resolve ONLY through the profile registry (controller's ProfileStore, which validates
# the id); no path ever comes from the request. The preview is the launch-path render into a fresh
# mkdtemp dir (see gnomon.def_preview); DELETE removes it; stale ones are swept by age.

#: Test seam: the dir previews live under (None = tempfile.gettempdir(), i.e. /var/folders/... on macOS).
_def_preview_base: Optional[str] = None


def _def_view_profile(instance_id: str):
    try:
        return controller._store.read(instance_id)
    except ValueError:  # unsafe / traversal-shaped id, or an unparseable profile
        raise HTTPException(status_code=400, detail="invalid instance_id")
    except (KeyError, OSError):
        raise HTTPException(status_code=404, detail=f"no profile for {instance_id!r}")


@app.get("/api/def-view/{instance_id}/raw")
def def_view_raw(instance_id: str) -> dict:
    """The profile's claude_md_source, macros UNEXPANDED (read-only)."""
    prof = _def_view_profile(instance_id)
    src = (prof.claude_md_source or "")
    if not src:
        raise HTTPException(status_code=404, detail="profile has no claude_md_source")
    path = os.path.abspath(os.path.expanduser(src))
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=404, detail=f"def unreadable: {type(exc).__name__}")
    return {"instance_id": instance_id, "source": path, "chars": len(text), "text": text}


@app.post("/api/def-view/{instance_id}/preview")
def def_view_preview(instance_id: str) -> dict:
    """One-off launch-path render of the def into a fresh temp dir; returns the rendered text, the
    per-macro status summary and a preview id. Writes NOTHING outside that temp dir."""
    prof = _def_view_profile(instance_id)
    return def_preview_mod.run_preview(prof, controller._launch_manager(), base=_def_preview_base)


@app.delete("/api/def-view/preview/{preview_id}")
def def_view_close(preview_id: str) -> dict:
    """Delete that preview's temp dir. 404 for a malformed / unknown id (idempotent for the UI)."""
    if not def_preview_mod.close(preview_id, _def_preview_base):
        raise HTTPException(status_code=404, detail="no such preview")
    return {"closed": True, "preview_id": preview_id}


@app.get("/api/verb-policy")
def verb_policy_get() -> dict:
    """the EFFECTIVE verb policy exactly as ``verb_policy.load_policy`` merges it
    (no second parser), per-verb provenance (default vs override), the loader's INVALID /
    narrowing reports, and the raw text of the two policy files ONLY (the shipped default and
    ``<runtime_root>/verb-policy.toml``; no path is ever taken from the request). READ-ONLY:
    this route is GET-only, so every write method is refused (405)."""
    root = _verb_policy_runtime_root
    if root is None:
        try:
            root = controller.config.runtime_root
        except Exception:  # noqa: BLE001
            root = runtime_root_default()
    pol = verb_policy_mod.load_policy(root, default_path=_verb_policy_default_path)
    verbs = {}
    for name in sorted(pol.verbs):
        vp = pol.verbs[name]
        src = pol.verb_sources.get(name)
        verbs[name] = {
            "source": "override" if (src is not None and src != pol.default_path) else "default",
            "source_path": src,
            "confirm_window_seconds": vp.window_seconds,
            **_rule_dict(vp.rule),
            "targets": {t: _rule_dict(r) for t, r in sorted(vp.targets.items())},
        }
    files = {"default": _raw_policy_file(pol.default_path),
             "override": _raw_policy_file(pol.override_path)}
    return {
        "symlink": any(f["symlink"] for f in files.values()),
        "default_path": pol.default_path,
        "override_path": pol.override_path,
        "overridden": pol.overridden,
        "invalid": pol.invalid,
        "invalid_reason": pol.invalid_reason,
        "errors": list(pol.errors),
        "narrowing": list(pol.narrowing),
        "verbs": verbs,
        "files": files,
    }


# --- /(a): the Knowledge tab's usage matrix ----------------------

#: Test seams: the usage DB path and the session roots. None = the module defaults
#: (``~/gnomon-data/metrics/harness_usage.sqlite``; ``~/gnomon-data/sessions`` +
#: any configured legacy roots).
_usage_db_path: Optional[str] = None
_usage_roots = None


def _usage_enabled() -> bool:
    """``usage_metrics``: follows the REGISTRY default (``flags.KNOWN_FLAGS``,
    False as of 2026-09-29 after the X7 secret-persistence audit) unless the
    machine config sets it. No local ``default=`` override: a local default that
    disagrees with the registry makes /api/flags report one value while the panel
    acts on another (X7 owed fix)."""
    return _flag(usage_metrics_mod.FLAG_NAME)


@app.get("/api/usage")
def usage_get(scope: str = "intelligence", since: Optional[str] = None) -> dict:
    """The category x lineage usage matrix + signals. READ-ONLY on the
    session logs; indexes incrementally into the usage DB on request (creating
    its directory if missing). ``scope`` = ``intelligence`` (default: the
    lineages launched from an agent definition) or ``all``."""
    name = usage_metrics_mod.FLAG_NAME
    if not _usage_enabled():
        return {"enabled": False, "flag": name,
                "message": f"enable {name} in the machine config (flags) to show this panel"}
    if scope not in usage_metrics_mod.PANEL_SCOPES:
        raise HTTPException(status_code=400,
                            detail=f"scope must be one of {list(usage_metrics_mod.PANEL_SCOPES)}")
    db = _usage_db_path or usage_metrics_mod.DEFAULT_DB_PATH
    try:
        rep = usage_metrics_mod.panel_report(db, roots=_usage_roots, scope=scope, since=since)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - surfaced loudly to the panel, never a blank matrix
        raise HTTPException(status_code=500, detail=f"usage index/report failed: {exc}")
    rep["enabled"] = True
    rep["flag"] = name
    return rep


# --- the deliverable ledger ------------------------------------------------
#
# Behind ``deliverable_ledger`` (registry default OFF, g2; read through ``_flag`` with
# NO local default). With the flag OFF nothing below exists as far as a client can tell:
#   * every route answers the SAME 404 an unknown path gets (``HTTPException(404, "Not Found")``
#     is byte-identical to the framework's own), for EVERY method, so a GET on the POST path is
#     not a 405 that would reveal the route;
#   * the routes are ``include_in_schema=False`` (``/openapi.json`` is unchanged);
#   * ``/`` serves index.html untouched (the ledger script is injected only when ON), and the
#     UI assets live in ``web/ledger_ui/`` (NOT under /static) behind the same gate.
# The tailer runs ON REQUEST inside this process with a bounded deadline (see
# ``gnomon.deliverables`` CHOICE-1). Test seams: the four module attributes below.

_deliverables_store: Optional[str] = None
_deliverables_roots = None
_deliverables_projects_root: Optional[str] = None
_deliverables_deadline: Optional[float] = None
#: True while the LAST scan could not persist the ledger (set by _ledger_list). During that outage an
#: entry can exist only in memory, so a mark-seen for it cannot be honoured: it is refused with a 503.
_deliverables_persist_failed = False
LEDGER_UI_DIR = os.path.join(_WEB_DIR, "ledger_ui")
LEDGER_UI_ASSETS = {"deliverable_ledger.js": "application/javascript"}
LEDGER_UI_SCRIPT_TAG = '<script src="/deliverables-ui/deliverable_ledger.js"></script>'
_LEDGER_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _deliverables_enabled() -> bool:
    return _flag(deliverables_mod.FLAG_NAME)


def _deliverable_open_enabled() -> bool:
    """The Open button + endpoint: the ledger is ON and deliverable_open (registry default ON) is ON."""
    return _deliverables_enabled() and _flag(deliverables_mod.OPEN_FLAG_NAME)


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Not Found")


def _ledger_list(q: dict) -> dict:
    """Scan (bounded, incremental) then list. Blocking: call via the threadpool."""
    store = _deliverables_store or deliverables_mod.default_store_root()
    scan = deliverables_mod.scan_all(
        store, _deliverables_roots, projects_root=_deliverables_projects_root,
        deadline_seconds=(_deliverables_deadline if _deliverables_deadline is not None
                          else deliverables_mod.DEFAULT_DEADLINE_SECONDS),
        only_session=q["session"])
    global _deliverables_persist_failed
    _deliverables_persist_failed = not scan["persisted"]
    out = deliverables_mod.list_entries(store, viewer=q["viewer"], filter=q["filter"], session=q["session"],
                                        since=q["since"], limit=q["limit"], offset=q["offset"],
                                        overlay=scan["overlay"], author=q["author"])
    out["enabled"] = True
    out["flag"] = deliverables_mod.FLAG_NAME
    out["open_enabled"] = _deliverable_open_enabled()      # the UI shows the Open button only when True
    # F1 (round 3): EVERY scan warning reaches the client. ``scan.complete`` stays about COVERAGE;
    # ``scan.persisted`` is False when a state write failed (the entries then come from the IN-MEMORY
    # result of this very pass, not from disk, and are re-derived on the next request).
    warns = list(scan["warnings"]) + list(out.get("warnings") or [])
    for m in out.get("sessions") or []:
        if m.get("caps_hit"):
            warns.append(f"{m['session']}: limit reached ({', '.join(m['caps_hit'])}); older history may be missing")
    if not scan["persisted"]:
        warns.insert(0, "the ledger could not be saved to disk; this list comes from the in-memory scan and may not survive a restart")
    warns = list(dict.fromkeys(warns))          # de-duplicate, order kept
    cap = deliverables_mod.WARNINGS_CAP
    out["warnings"] = warns[:cap] + ([f"... and {len(warns) - cap} more warnings"] if len(warns) > cap else [])
    out["scan"] = {"complete": scan["scan_complete"], "persisted": scan["persisted"], "stop_reasons": scan["stop_reasons"],
                   "seconds": scan["seconds"], "deadline_seconds": scan["deadline_seconds"],
                   "sessions_seen": scan["sessions_seen"], "sessions_scanned": scan["sessions_scanned"],
                   "problems": [{"session": s["session"], "status": s["status"]} for s in scan["sessions"]
                                if s["status"].startswith(("chain-error", "skipped"))]}
    return out


def _ledger_query(params) -> dict:
    from gnomon.harness.transcript_search import SearchError, _time_param
    try:
        since = _time_param({"since": params.get("since")}, "since") if params.get("since") else None
    except SearchError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        limit = int(params.get("limit", deliverables_mod.LIST_LIMIT_DEFAULT))
        offset = int(params.get("offset", 0))
    except ValueError:
        raise HTTPException(status_code=400, detail="limit and offset must be integers")
    if limit < 1 or offset < 0:
        raise HTTPException(status_code=400, detail="limit must be >= 1 and offset >= 0")
    return {"viewer": params.get("viewer"), "filter": params.get("filter", "all"),
            "session": params.get("session") or None, "since": since,
            "author": params.get("author") or None,        # operator 2026-10-03: filter by the authoring agent
            "limit": min(limit, deliverables_mod.LIST_LIMIT_CEILING), "offset": offset}


@app.api_route("/api/deliverables", methods=_LEDGER_METHODS, include_in_schema=False)
async def deliverables_api(request: Request):
    """GET = the ledger (per session or all; ``filter=unseen|all``; ``since``; ``viewer``); POST =
    mark seen (``{"ids": [...]}`` | ``{"session": ...}`` | ``{"all": true}``, optional ``viewer``).
    READ-ONLY on the deliverable files themselves: only paths are ever returned."""
    if not _deliverables_enabled():
        raise _not_found()
    if request.method == "GET":
        q = _ledger_query(request.query_params)
        try:
            ckey = tuple(sorted((k, repr(v)) for k, v in q.items()))
            return await run_in_threadpool(_ledger_cache.get, ckey, lambda: _ledger_list(q))
        except deliverables_mod.LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - loud, never a blank ledger
            raise HTTPException(status_code=500, detail=f"deliverable ledger failed: {type(exc).__name__}: {exc}")
    if request.method == "POST":
        # P3-4: only application/json. A cross-origin form / text/plain "simple request" (no CORS
        # preflight) is refused. Systemic Origin/CORS checking is out of scope (web-platform item).
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise HTTPException(status_code=415, detail="mark-seen requires Content-Type: application/json")
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=400, detail="body must be a JSON object")
        if not isinstance(body, dict) or set(body) - {"ids", "session", "all", "viewer"}:
            raise HTTPException(status_code=400,
                                detail="body must be an object with only ids / session / all / viewer")
        if "ids" in body and not isinstance(body["ids"], list):
            raise HTTPException(status_code=400, detail="ids must be a list")
        if "all" in body and not isinstance(body["all"], bool):
            raise HTTPException(status_code=400, detail="all must be a boolean")
        store = _deliverables_store or deliverables_mod.default_store_root()
        try:
            res = await run_in_threadpool(
                lambda: deliverables_mod.mark_seen(store, viewer=body.get("viewer"), ids=body.get("ids"),
                                                   session=body.get("session"),
                                                   mark_all=bool(body.get("all", False))))
            if res["unknown"] and _deliverables_persist_failed:
                # P3-5: an id the on-disk ledger does not have, while the ledger cannot be saved, is an
                # in-memory-only entry: say so loudly instead of a quiet "unknown".
                raise HTTPException(status_code=503, detail=(
                    "the ledger could not be saved to disk, so %d entr%s exist only in memory and cannot be "
                    "marked seen yet; %d other entr%s were marked. Retry once the store is writable."
                    % (len(res["unknown"]), "y" if len(res["unknown"]) == 1 else "ies",
                       res["marked"], "y" if res["marked"] == 1 else "ies")))
            return res
        except HTTPException:
            raise
        except deliverables_mod.LedgerError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=500, detail=f"mark-seen failed: {type(exc).__name__}: {exc}")
    raise HTTPException(status_code=405, detail="Method Not Allowed")


#: Test seams: the opener (default = the DSD documents' real opener, which refuses under pytest) and the
#: allowed roots (default = the DSD documents' allow-list).
_deliverables_opener = None
def _open_guard(request: Request) -> None:
    """Host/Origin guard for the two file-OPEN routes (Deliverables Open, DSD document open): a DNS-rebinding
    page reaches the loopback port with a FOREIGN Host and/or Origin; both are refused 403. No CSRF token (the
    actions are read-by-id and open a viewer). A request with no Origin (curl, same-uid tool) is accepted if the
    Host is this server's own loopback host. Defined before first use; shares _apps_host_ok."""
    if not _apps_host_ok(request):
        raise HTTPException(status_code=403, detail="refused: the open routes answer only this server's own "
                                                    "loopback Host (127.0.0.1 / localhost on its port)")
    origin = request.headers.get("origin")
    if origin is not None and origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="refused: a foreign Origin may not open files")
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None and sfs not in ("same-origin", "none"):
        raise HTTPException(status_code=403, detail="refused: Sec-Fetch-Site is not same-origin")


_deliverables_open_roots = None
_DELIVERABLE_OPEN_STATUS = {
    "bad-id": 400, "bad-viewer": 400, "unknown-id": 404, "missing": 404,
    "opener-failed": 502, "unreadable": 500, "changed-before-open": 409,
}


@app.api_route("/api/deliverables/open", methods=_LEDGER_METHODS, include_in_schema=False)
async def deliverables_open_api(request: Request):
    """POST ``{"id": "<16-hex entry id>", "viewer"?: str}``: open that ledger entry with ``/usr/bin/open`` and
    mark it seen. NEVER accepts a path: the server looks the id up in the ledger and applies the Open policy
    (``deliverables.check_openable``: allow-listed roots by file identity, no symlinks, regular file with a
    listed extension and no executable bit, or a non-bundle directory). Every refusal is NAMED in the detail.
    Flag OFF (``deliverable_open`` or the ledger flag): the same 404 an unknown path gets, for every
    method. NOT authenticated, but Host/Origin-guarded (_open_guard, 403 on a foreign one: DNS rebinding); a same-uid process can already run ``open``."""
    if not _deliverable_open_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _open_guard(request)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="open requires Content-Type: application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(body, dict) or "id" not in body or set(body) - {"id", "viewer"}:
        raise HTTPException(status_code=400, detail=(
            "body must be {id[, viewer]}; a path is never accepted - name a deliverable by its id"))
    store = _deliverables_store or deliverables_mod.default_store_root()
    opener = _deliverables_opener or dsd_documents_mod.system_opener
    try:
        res = await run_in_threadpool(
            lambda: deliverables_mod.open_entry(store, entry_id=body["id"], viewer=body.get("viewer"),
                                                opener=opener, roots=_deliverables_open_roots))
    except deliverables_mod.LedgerError as exc:
        raise HTTPException(status_code=_DELIVERABLE_OPEN_STATUS.get(exc.kind, 403), detail=f"{exc.kind}: {exc}")
    except Exception as exc:  # noqa: BLE001 - loud, never a silent no-op
        raise HTTPException(status_code=500, detail=f"open failed: {type(exc).__name__}: {exc}")
    print(f"[gm-web] deliverable open {res['id']} -> {res['path']}", flush=True)
    return res


@app.api_route("/deliverables-ui/{name}", methods=_LEDGER_METHODS, include_in_schema=False)
async def deliverables_ui_asset(name: str, request: Request):
    """The ledger's UI script, served ONLY while the flag is ON (closed name allowlist)."""
    if not _deliverables_enabled() or name not in LEDGER_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(LEDGER_UI_DIR, name), media_type=LEDGER_UI_ASSETS[name])


def _index_with_ledger() -> HTMLResponse:
    return _index_with_scripts([LEDGER_UI_SCRIPT_TAG])


def _index_with_scripts(tags) -> HTMLResponse:
    """index.html with each flag-gated feature's script tag injected before </body> (ledger, Apps tab)."""
    with open(os.path.join(STATIC_DIR, "index.html"), "r", encoding="utf-8") as fh:
        html = fh.read()
    for tag in tags:
        i = html.rfind("</body>")
        html = (html[:i] + "  " + tag + "\n" + html[i:]) if i >= 0 else html + tag
    return HTMLResponse(html)


# --- (spec 13): the Apps tab ---------------------------------------------------------------------
#
# Behind ``apps_tab`` (registry default OFF). OFF: every route below answers the SAME 404 an unknown path
# gets, for EVERY method (the ledger pattern above), the routes are not in the schema, and "/" is untouched.
# ON: the Apps tab script is injected into "/" and these routes drive the ONE AppsHost the hub verbs use
# (``controller.apps_host()``). The CONFIRM, DECLINE, SETTINGS, BACKUP and ACTION routes exist ONLY here: no
# agent MCP verb reaches them (known limit known-limits: this loopback server has no operator
# credential on its mutating routes, so a same-uid process could still POST here; the confirm is human-only
# for the agent VERBS, not against a local process). Every POST requires Content-Type: application/json (a
# cross-origin form / text/plain "simple request" is refused). The actor of a page action is the viewer, "ch",
# marked unverified. The page's Open is server-side (the browser window is opened by this process), so the
# ui_token is never in any HTTP response.

#: Test seam: an AppsHost (None = the controller's).
_apps_host_override = None
APPS_UI_DIR = os.path.join(_WEB_DIR, "apps_ui")
APPS_UI_ASSETS = {"apps_tab.js": "application/javascript"}
APPS_UI_SCRIPT_TAG = '<script src="/apps-ui/apps_tab.js"></script>'
_APPS_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
_APPS_POST_OPS = ("start", "stop", "open", "confirm", "decline", "settings", "backup", "uninstall")


def _apps_enabled() -> bool:
    try:
        from gnomon.appshost.verb_tools import FLAG as _APPS_FLAG
    except Exception as exc:  # noqa: BLE001 - F5: a broken Apps host turns the Apps tab OFF, never the page
        print(f"[gm-web] Apps tab unavailable: {type(exc).__name__}: {exc}", flush=True)
        return False
    return _flag(_APPS_FLAG)


# --- round 2, Diamond F4: who may drive the Apps routes ---------------------------------------------------
# (1) HOST: every Apps route answers only a request whose Host header is this server's own loopback address on
#     its bound port (127.0.0.1 / localhost / [::1]). A DNS-rebinding page arrives as Host: evil.example:<port>
#     and is refused, so it can neither read the CSRF token nor POST.
# (2) ORIGIN: every POST must carry Origin == http://<that host>, and Sec-Fetch-Site (when sent) == same-origin.
# (3) CSRF: every POST carries X-Gnomon-CSRF equal to this process's token, which the page reads from a
#     same-origin GET /api/apps (a cross-origin page cannot read that response).
# KNOWN LIMIT (unchanged known-limits, open-question-confirm-human-only): a same-uid process can
# send the right Host, Origin and token itself (it can GET the token like the page does). These checks stop a WEB
# PAGE, not a local process. The actor is therefore recorded as "operator-page" and marked unverified.
_APPS_CSRF = __import__("secrets").token_urlsafe(32)
APPS_CSRF_HEADER = "x-gnomon-csrf"


def _apps_host_ok(request: Request) -> bool:
    server = request.scope.get("server")
    if not server or len(server) < 2:
        return False
    port = server[1]
    host = request.headers.get("host", "")
    return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}


def _apps_guard(request: Request, *, write: bool) -> None:
    import hmac

    if not _apps_host_ok(request):
        raise HTTPException(status_code=403, detail="refused: the Apps routes answer only this server's own "
                                                    "loopback Host (127.0.0.1 / localhost on its port)")
    if not write:
        return
    origin = request.headers.get("origin")
    if origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="refused: an Apps action needs a same-origin Origin header")
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None and sfs != "same-origin":
        raise HTTPException(status_code=403, detail="refused: Sec-Fetch-Site is not same-origin")
    tok = request.headers.get(APPS_CSRF_HEADER, "")
    if not hmac.compare_digest(tok.encode(), _APPS_CSRF.encode()):
        raise HTTPException(status_code=403, detail="refused: missing or wrong CSRF token (reload the Apps tab)")


def _apps_host():
    return _apps_host_override if _apps_host_override is not None else controller.apps_host()


def _apps_actor():
    from gnomon.appshost.host import Actor

    # F4: the page cannot prove WHO clicked (a same-uid process can make the same request): say so.
    return Actor("operator-page", False)


async def _apps_json_body(request: Request) -> dict:
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="Apps actions require Content-Type: application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def _apps_result(res: dict):
    if isinstance(res, dict) and res.get("ok") is False:
        return JSONResponse(status_code=409, content=res)
    return res


def _apps_call(fn, *args, **kwargs):
    from gnomon.appshost.host import AppsError

    try:
        return fn(*args, **kwargs)
    except AppsError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@app.api_route("/api/apps", methods=_APPS_METHODS, include_in_schema=False)
async def apps_root(request: Request):
    """GET = the master list + the Pending section; POST = the operator proposes an install ({"manifest_path"})."""
    if not _apps_enabled():
        raise _not_found()
    host = _apps_host()
    if request.method == "GET":
        _apps_guard(request, write=False)
        out = await run_in_threadpool(_apps_call, host.list)
        return {**out, "csrf": _APPS_CSRF}
    if request.method == "POST":
        _apps_guard(request, write=True)
        body = await _apps_json_body(request)
        if set(body) - {"manifest_path"}:
            raise HTTPException(status_code=400, detail="body must be {\"manifest_path\": \"/abs/path/manifest.json\"}")
        res = await run_in_threadpool(_apps_call, host.propose_install, body.get("manifest_path"), _apps_actor())
        return _apps_result(res)
    raise HTTPException(status_code=405, detail="Method Not Allowed")


@app.api_route("/api/apps/{app_id}", methods=_APPS_METHODS, include_in_schema=False)
async def apps_detail(app_id: str, request: Request):
    """GET = the detail page model (installed app or pending proposal). Never carries a ui_token."""
    if not _apps_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _apps_guard(request, write=False)
    return await run_in_threadpool(_apps_call, _apps_host().detail, app_id)


#: ops that take an act-class effect only on the operator's say-so: the server asks macOS for device-owner
#: authentication AFTER the click and BEFORE committing (spec 13 amendment 2026-10-03, g76). ``action`` (the
#: manifest-action route) runs code, so it is gated too (audit F2).
_APPS_PROOF_OPS = ("confirm", "settings", "action")
_APPS_PROOF_VERB = {"confirm": "confirm", "settings": "settings", "action": "action"}


async def _apps_proof_gate(host, app_id: str, op: str, actor, *, values=None, seen=None, settings=None, action=None):
    """(evidence | None, act digest | None, refusal JSONResponse | None). Flag ``confirm_proof`` OFF ->
    (None, None, None): the previous behaviour. Nothing to confirm -> the same: the host refuses it itself, the operator
    is not prompted (the digest of the empty state is still returned, F-1). The digest (audit F4) is computed at click time, BEFORE the prompt; the host recomputes it
    inside its op lock and refuses a commit whose act changed during the prompt."""
    from gnomon import human_confirm as hc

    if op not in _APPS_PROOF_OPS or not _flag(hc.FLAG):
        return None, None, None
    # ONE locked read gives both the prompt text and the digest (delta audit D6): the operator is asked about the very
    # state that is bound, and the host re-reads under its op lock and commits only if that state is unchanged.
    what, digest = await run_in_threadpool(lambda: host.proof_snapshot(
        app_id, op, values=values, seen=seen, settings=settings, action=action))
    if what is None:
        # nothing to confirm NOW: no prompt, but the digest of that empty state is still passed (delta2 F-1): a
        # proposal that lands before the host's lock changes it and the host refuses, instead of committing unproved
        return None, digest, None
    ev = (await run_in_threadpool(hc.prove_presence_detail, f"{what} the app {app_id}")).evidence()
    if ev["ok"]:
        return ev, digest, None
    await run_in_threadpool(host.record_proof_refusal, app_id, _APPS_PROOF_VERB[op], actor, ev)
    msg = {"timeout": "no answer within 2 minutes", "refused": "the check was cancelled or failed",
           "busy": "another confirmation prompt is still waiting for you",
           "unavailable": "this Mac cannot run the check (no login password / no GUI session)"}.get(
               ev["result"], "the check could not run (" + (ev.get("detail") or "error") + ")")
    return None, None, JSONResponse(status_code=409, content={
        "ok": False, "app_id": app_id, "presence_refused": True, "presence": ev,
        "error": f"NOT confirmed: your Mac password / Touch ID was needed and {msg}. Nothing was changed. "
                 f"Click again to retry."})


async def _apps_op(app_id: str, op: str, request: Request):
    if not _apps_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _apps_guard(request, write=True)
    body = await _apps_json_body(request)
    host, actor = _apps_host(), _apps_actor()
    proof, digest, refusal = await _apps_proof_gate(
        host, app_id, op, actor, values=(body.get("values") or {}) if op == "confirm" else None,
        seen=body.get("seen") if op == "confirm" else None,
        settings=(body.get("settings") or {}) if op == "settings" else None)
    if refusal is not None:
        return refusal
    if op == "start":
        res = await run_in_threadpool(_apps_call, host.start, app_id, actor)
    elif op == "stop":
        res = await run_in_threadpool(_apps_call, host.stop, app_id, actor)
    elif op == "open":   # the PAGE's Open: never starts the app (an app that is not running is refused)
        res = await run_in_threadpool(_apps_call, host.open, app_id, actor)
    elif op == "confirm":
        # round-2 F11: the confirm echoes what the screen showed ({manifest_sha256, tree_sha256}); the host refuses a
        # mismatch. (An uninstall confirm carries no pin, so the host asks for it only for an install / re-confirm.)
        res = await run_in_threadpool(_apps_call, host.confirm, app_id, actor, body.get("values") or {},
                                      seen=body.get("seen"), require_seen=True, proof=proof, expect_digest=digest, proof_waived=(digest is None))
    elif op == "decline":
        res = await run_in_threadpool(_apps_call, host.decline, app_id, actor)
    elif op == "settings":
        res = await run_in_threadpool(_apps_call, host.save_settings, app_id, body.get("settings") or {}, actor,
                                      proof=proof, expect_digest=digest, proof_waived=(digest is None))
    elif op == "backup":
        res = await run_in_threadpool(_apps_call, host.record_backup, app_id, body.get("name"),
                                      body.get("backup_path"), actor)
    elif op == "uninstall":
        res = await run_in_threadpool(_apps_call, host.propose_uninstall, app_id, actor)
    else:  # pragma: no cover - the routes below are a closed set
        raise _not_found()
    return _apps_result(res)


@app.api_route("/api/apps/{app_id}/start", methods=_APPS_METHODS, include_in_schema=False)
async def apps_start(app_id: str, request: Request):
    return await _apps_op(app_id, "start", request)


@app.api_route("/api/apps/{app_id}/stop", methods=_APPS_METHODS, include_in_schema=False)
async def apps_stop(app_id: str, request: Request):
    return await _apps_op(app_id, "stop", request)


@app.api_route("/api/apps/{app_id}/open", methods=_APPS_METHODS, include_in_schema=False)
async def apps_open(app_id: str, request: Request):
    return await _apps_op(app_id, "open", request)


@app.api_route("/api/apps/{app_id}/confirm", methods=_APPS_METHODS, include_in_schema=False)
async def apps_confirm(app_id: str, request: Request):
    """The operator's confirm of an install / uninstall proposal, or a re-confirm after a pin change. Body:
    {"values": {run_at_boot, scope, paths: {...}, user: {...}}} for an install."""
    return await _apps_op(app_id, "confirm", request)


@app.api_route("/api/apps/{app_id}/decline", methods=_APPS_METHODS, include_in_schema=False)
async def apps_decline(app_id: str, request: Request):
    return await _apps_op(app_id, "decline", request)


@app.api_route("/api/apps/{app_id}/settings", methods=_APPS_METHODS, include_in_schema=False)
async def apps_settings(app_id: str, request: Request):
    """Body: {"settings": {run_at_boot?, scope?, paths?: {...}, user?: {...}}}; validated server-side."""
    return await _apps_op(app_id, "settings", request)


@app.api_route("/api/apps/{app_id}/backup", methods=_APPS_METHODS, include_in_schema=False)
async def apps_backup(app_id: str, request: Request):
    """Body: {"name": <backup_before_first_start path name>, "backup_path": <existing absolute path>}."""
    return await _apps_op(app_id, "backup", request)


@app.api_route("/api/apps/{app_id}/uninstall", methods=_APPS_METHODS, include_in_schema=False)
async def apps_uninstall(app_id: str, request: Request):
    """The operator PROPOSES an uninstall from the page; it takes effect on his confirm (same gate as an agent's)."""
    return await _apps_op(app_id, "uninstall", request)


@app.api_route("/api/apps/{app_id}/actions/{name}", methods=_APPS_METHODS, include_in_schema=False)
async def apps_action(app_id: str, name: str, request: Request):
    """Run a manifest action after the page's confirm dialog. Body: {"confirm": true}."""
    if not _apps_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _apps_guard(request, write=True)
    body = await _apps_json_body(request)
    host, actor = _apps_host(), _apps_actor()
    confirmed = body.get("confirm") is True
    proof, digest, refusal = (None, None, None)
    if confirmed:       # an unconfirmed call is refused by the host itself; the operator is not prompted for it
        proof, digest, refusal = await _apps_proof_gate(host, app_id, "action", actor, action=name)
    if refusal is not None:
        return refusal
    res = await run_in_threadpool(_apps_call, host.run_action, app_id, name, actor, confirmed=confirmed,
                                  proof=proof, expect_digest=digest, proof_waived=(digest is None))
    return _apps_result(res)


@app.api_route("/apps-ui/{name}", methods=_APPS_METHODS, include_in_schema=False)
async def apps_ui_asset(name: str, request: Request):
    """The Apps tab script, served ONLY while the flag is ON (closed name allowlist)."""
    if not _apps_enabled() or name not in APPS_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(APPS_UI_DIR, name), media_type=APPS_UI_ASSETS[name])


# --- (spec 14, stage 1a): the Scheduler and Run Log tabs ---------------------------------------
#
# Behind ``scheduler`` AND ``confirm_proof``. OFF: every route below answers the SAME 404 an unknown
# path gets, for EVERY method, and "/" is untouched. ON: the tab script is injected into "/". The CONFIRM, DECLINE,
# CANCEL, PAUSE and ENABLE routes exist ONLY here: no agent MCP verb reaches them. CONFIRM runs the shared
# device-owner presence check (gnomon.human_confirm) after the click and before the commit, bound to the
# proposal by a digest taken at click time; there is no waiver. Known limit (as for the Apps tab): this loopback
# server has no operator credential on its mutating routes, so a same-uid local process could still POST here.

#: Test seam: a Scheduler (None = the controller's).
_sched_override = None
SCHED_UI_DIR = os.path.join(_WEB_DIR, "scheduler_ui")
SCHED_UI_ASSETS = {"scheduler_tab.js": "application/javascript"}
SCHED_UI_SCRIPT_TAG = '<script src="/scheduler-ui/scheduler_tab.js"></script>'
_SCHED_CSRF = __import__("secrets").token_urlsafe(32)
SCHED_CSRF_HEADER = "x-gnomon-csrf"
_SCHED_OPS = ("confirm", "decline", "cancel", "pause", "enable")


def _sched_enabled() -> bool:
    try:
        from gnomon import human_confirm as _hc
        from gnomon.scheduler.vocab import FLAG as _SCHED_FLAG
    except Exception as exc:  # noqa: BLE001 - a broken scheduler turns the tabs OFF, never the page
        print(f"[gm-web] Scheduler unavailable: {type(exc).__name__}: {exc}", flush=True)
        return False
    return _flag(_SCHED_FLAG) and _flag(_hc.FLAG)


def _sched():
    return _sched_override if _sched_override is not None else controller.scheduler()


def _sched_guard(request: Request, *, write: bool) -> None:
    import hmac

    if not _apps_host_ok(request):
        raise HTTPException(status_code=403, detail="refused: the Scheduler routes answer only this server's own "
                                                    "loopback Host (127.0.0.1 / localhost on its port)")
    if not write:
        return
    origin = request.headers.get("origin")
    if origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="refused: a Scheduler action needs a same-origin Origin header")
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None and sfs != "same-origin":
        raise HTTPException(status_code=403, detail="refused: Sec-Fetch-Site is not same-origin")
    if not hmac.compare_digest(request.headers.get(SCHED_CSRF_HEADER, "").encode(), _SCHED_CSRF.encode()):
        raise HTTPException(status_code=403, detail="refused: missing or wrong CSRF token (reload the Scheduler tab)")


@app.api_route("/api/scheduler", methods=_APPS_METHODS, include_in_schema=False)
async def scheduler_root(request: Request):
    """GET = the job list + the Pending section + the banner of flagged runs. No POST: the operator never creates a job
 here (agents propose; requirement-view-only-authoring)."""
    if not _sched_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _sched_guard(request, write=False)
    sch = _sched()
    out = await run_in_threadpool(sch.list_jobs)
    banner = await run_in_threadpool(sch.banner)
    return {**out, "banner": banner, "csrf": _SCHED_CSRF}


@app.api_route("/api/scheduler/runlog", methods=_APPS_METHODS, include_in_schema=False)
async def scheduler_runlog(request: Request):
    if not _sched_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _sched_guard(request, write=False)
    q = request.query_params
    try:
        limit = int(q.get("limit", "200"))
    except ValueError:
        limit = 200
    return await run_in_threadpool(lambda: {**_sched().run_log(job_id=q.get("job") or None, result=q.get("result") or None,
                                                              outcome=q.get("outcome") or None, limit=limit),
                                           "csrf": _SCHED_CSRF})


@app.api_route("/api/scheduler/runs/{job_id}/{run_id}", methods=_APPS_METHODS, include_in_schema=False)
async def scheduler_receipt(job_id: str, run_id: str, request: Request):
    if not _sched_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _sched_guard(request, write=False)
    try:
        res = await run_in_threadpool(_sched().run_receipt, job_id, run_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="no such run")
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", "no such run"))
    return res


@app.api_route("/api/scheduler/{job_id}", methods=_APPS_METHODS, include_in_schema=False)
async def scheduler_detail(job_id: str, request: Request):
    if not _sched_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _sched_guard(request, write=False)
    res = await run_in_threadpool(_sched().detail, job_id)
    if not res.get("ok"):
        raise HTTPException(status_code=404, detail=res.get("error", "not found"))
    return {**res, "csrf": _SCHED_CSRF}


async def _sched_op(job_id: str, op: str, request: Request):
    if not _sched_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _sched_guard(request, write=True)
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="Scheduler actions require Content-Type: application/json")
    sch, actor = _sched(), "operator-page"      # the page cannot prove WHO clicked: recorded as such
    try:
        body = await request.json()
    except ValueError:
        body = {}
    seen = body.get("seen") if isinstance(body, dict) else None
    if op == "confirm":
        from gnomon import human_confirm as hc

        digest = await run_in_threadpool(sch.proposal_digest, job_id)
        if digest is None:
            return JSONResponse(status_code=409, content={"ok": False, "error": f"no pending proposal {job_id!r}"})
        # F-1: the confirm is bound to what the operator SAW (the Apps tab's seen / require_seen, round-2 F11). The tab
        # echoes the digest of the exact Pending row it rendered; a missing or different digest is refused BEFORE any
        # presence prompt and the proposal as it is now is returned for a fresh look.
        if not isinstance(seen, str) or seen != digest:
            now_row = (await run_in_threadpool(sch.detail, job_id)).get("proposal")
            await run_in_threadpool(sch._audit, "confirm", job_id, actor, False, "refused",
                                    {"reason": "the proposal changed since the operator opened it" if isinstance(seen, str)
                                     else "no seen digest"})
            return JSONResponse(status_code=409, content={
                "ok": False, "stale": True, "job_id": job_id, "proposal": now_row,
                "error": "NOT confirmed: this proposal is not the one you were looking at (it changed, or the page did "
                         "not say which version you saw). The current version is shown; read it and confirm again."})
        what = await run_in_threadpool(sch.prompt_text, job_id)
        ev = (await run_in_threadpool(hc.prove_presence_detail, what)).evidence()
        if not ev["ok"]:
            await run_in_threadpool(sch._audit, "confirm", job_id, actor, False, "refused",
                                    {"reason": "presence check " + str(ev.get("result"))})
            return JSONResponse(status_code=409, content={
                "ok": False, "job_id": job_id, "presence_refused": True, "presence": ev,
                "error": "NOT confirmed: your Mac password / Touch ID was needed and the check did not pass "
                         f"({ev.get('result')}). Nothing was changed. Click again to retry."})
        res = await run_in_threadpool(lambda: sch.confirm(job_id, actor=actor, presence=ev, expect_digest=seen))
    elif op == "decline":
        res = await run_in_threadpool(lambda: sch.decline(job_id, actor=actor, seen=seen))
    elif op == "cancel":
        res = await run_in_threadpool(lambda: sch.cancel(job_id, actor=actor, seen=seen))
    elif op in ("pause", "enable"):
        res = await run_in_threadpool(lambda: sch.set_enabled(job_id, op == "enable", actor=actor))
    else:  # pragma: no cover - the routes below are a closed set
        raise _not_found()
    if isinstance(res, dict) and res.get("ok") is False:
        return JSONResponse(status_code=409, content=res)
    return res


for _op in _SCHED_OPS:
    def _make(op):
        async def _route(job_id: str, request: Request):
            return await _sched_op(job_id, op, request)
        _route.__name__ = "scheduler_" + op
        return _route
    app.add_api_route("/api/scheduler/{job_id}/" + _op, _make(_op), methods=_APPS_METHODS, include_in_schema=False)


@app.api_route("/scheduler-ui/{name}", methods=_APPS_METHODS, include_in_schema=False)
async def scheduler_ui_asset(name: str, request: Request):
    """The Scheduler tab script, served ONLY while the flag is ON (closed name allowlist)."""
    if not _sched_enabled() or name not in SCHED_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(SCHED_UI_DIR, name), media_type=SCHED_UI_ASSETS[name])


# The tick loop: ONE daemon thread in the web job (the hub is in this process, so the scheduler calls the registry
# in-process). It runs `tick()` only while the flag is ON, re-reading the flag every iteration, so turning it OFF
# stops every scheduled fire within one interval (BREAK-GLASS g11). Never started under pytest (g31).
_scheduler_stop = None
_scheduler_thread = None
_scheduler_loop_factory = None   # test seam: replaces the thread body


def _start_scheduler() -> None:
    global _scheduler_stop, _scheduler_thread
    try:
        if os.environ.get("PYTEST_CURRENT_TEST") and _scheduler_loop_factory is None:
            return   # g31: no test run starts the live loop
        import threading

        from gnomon.scheduler.limits import LIMITS as _L

        _scheduler_stop = threading.Event()

        def _loop() -> None:
            if _scheduler_stop.wait(_L["start_grace_s"]):    # let harnesses re-register with a restarted hub
                return
            while not _scheduler_stop.is_set():
                try:
                    if _sched_enabled():
                        _sched().tick()
                except Exception as exc:  # noqa: BLE001 - the loop never dies
                    print(f"[gm-web] scheduler tick error: {type(exc).__name__}: {exc}", flush=True)
                if _scheduler_stop.wait(_L["tick_s"]):
                    return

        _scheduler_thread = threading.Thread(target=_scheduler_loop_factory or _loop, name="gm-scheduler", daemon=True)
        _scheduler_thread.start()
    except Exception as exc:  # noqa: BLE001 - the server must come up regardless
        print(f"[gm-web] scheduler loop NOT started: {type(exc).__name__}: {exc}", flush=True)


def _stop_scheduler() -> None:
    if _scheduler_stop is not None:
        _scheduler_stop.set()


@app.get("/api/config")
def config_get() -> dict:
    """The full machine-config surface for the tab: the described fields with
    their current values, the custom terminal templates, and (read-only) the MCP
    endpoints. Parity with the CTk ``ConfigTab`` render, which shows the scalar
    fields + the custom-templates section."""
    cc = get_config_controller()
    return {
        "store_path": cc.store_path,
        "config_exists": cc.config_exists(),
        "fields": [
            {
                "key": s.key,
                "label": s.label,
                "kind": s.kind,
                "help": s.help,
                "unit": getattr(s, "unit", ""),
                "value": cc.get_display(s.key),
                "reserved": _reserved(s),
            }
            for s in cc.field_specs()
        ],
        # Ordered master groups for the master-detail Config tab. Each entry is
        # {key,title,field_keys[,surface]}; the union of field_keys covers every
        # field above. The tab renders these; field_specs stays the source of
        # per-field kind/label/value/help.
        "field_groups": cc.field_groups(),
        "custom_terminal_templates": cc.custom_templates(),
        "available_templates": cc.available_templates(),
        # mcp_endpoints has a live consumer (LaunchManager wires them at launch)
        # but is NOT tab-editable in the CTk build either — surfaced read-only.
        "mcp_endpoints": cc.mcp_endpoints(),
    }


def _security_flags_off() -> List[str]:
    out = [n for n in flags.SECURITY_FLAGS if not _flag(n)]
    # top-level security settings (flags.SECURITY_SETTINGS) are OFF only when the machine config holds the boolean false
    store = _machine_config_store()
    try:
        cfg = store.load() if store is not None else None
    except Exception:  # noqa: BLE001 - an unreadable config reads as the defaults (ON)
        cfg = None
    out.extend(n for n in flags.SECURITY_SETTINGS if cfg is not None and getattr(cfg, n, True) is False)
    return out


def _warn_security_flags_off() -> None:
    """Server start: a security flag that is OFF is a loud line on the operator screen (the server log); the
    dashboard banner (``/api/security-warnings``) shows it for as long as it stays OFF. Never raises."""
    try:
        off = _security_flags_off()
        for n in off:
            if n in flags.SECURITY_SETTINGS:
                print(f"[gm-web] ⚠⚠ SECURITY: {n} is OFF in the machine config: a deny guard that cannot start ALLOWS commands "
                      f"(sessions launched from now on). Turn it back ON in the Config tab (Harness / advanced).", flush=True)
                continue
            print(f"[gm-web] ⚠⚠ SECURITY: {n} is OFF in the machine config: the human-presence proof it guards is NOT "
                  f"enforced. Turn it back ON in the Config tab (Harness / advanced).", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[gm-web] security-flag check failed: {exc}", flush=True)


@app.get("/api/security-warnings")
def security_warnings() -> dict:
    """The dashboard banner: which security flags are OFF right now (live, so a flag switched OFF while the server
    runs shows too). Empty list = nothing to show."""
    off = _security_flags_off()
    out = [{"flag": n, "text": f"SECURITY: {n} is OFF; the check it guards is not enforced."} for n in off]
    out.extend(_deny_guard_degraded_warnings())
    if _guard_banner_enabled():
        # guard_start_failure_banner ON: the new banner (24-hour window, clears by itself) replaces this older entry for the same log,
        # which never clears; "guard-degraded" (warnings.json, a different file) stays.
        out = [w for w in out if w.get("kind") != "guard-could-not-start"]
    return {"warnings": out}


def _deny_guard_degraded_warnings() -> List[dict]:
    """X58 class (b): the deny guard could not start, or its install is broken, so commands are being ALLOWED without its
    checks. The hook process cannot reach this server, so it leaves PERSISTENT files in the guard's runtime directory (``deny-guard/``, beside the machine config) and
    this reads them: ``warnings.json`` (written by the guard itself) and ``startup-failures.log`` (appended by the
    launcher line when the guard process exited with a status other than 0/2). They stay until the operator deletes
    them (docs/BREAK-GLASS-relaunch.md). Never raises."""
    out: List[dict] = []
    try:
        from gnomon.harness import deny_guard as _dg

        try:
            with open(_dg.warnings_path(), "r", encoding="utf-8") as fh:
                w = json.load(fh)
            out.append({"flag": "deny_guard", "kind": "guard-degraded", "count": w.get("count"), "last-ts": w.get("last-ts"),
                        "text": str(w.get("text") or "SECURITY: the deny guard is degraded.")})
        except (OSError, ValueError, AttributeError):
            pass
        try:
            from gnomon import guard_banner as _gb

            # ONE line shape: "<UTC time> rc=<n> class=<c> mode=<blocked|allowed> count=<n>". Only the LAST 1 MiB is read (the
            # banner's cap), so a log that grew while a fleet was wedged cannot make this route read an unbounded file. A line that is not
            # in the shape (an older writer's, a damaged one) is COUNTED and shown, never dropped.
            with open(_dg.startup_failures_path(), "rb") as fh:
                size = os.fstat(fh.fileno()).st_size
                cut = max(0, size - _gb.MAX_BYTES)
                fh.seek(cut)
                raw = fh.read(_gb.MAX_BYTES)
            lines = raw.decode("utf-8", errors="replace").splitlines()
            if cut:
                lines = lines[1:]                                   # the first line of a tail read may be a partial one
            allowed, blocked, bad, last_allowed, last_blocked = 0, 0, 0, None, None
            for ln in lines:
                if not ln.strip():
                    continue
                m = _gb.LINE_RE.match(ln)
                if not m:
                    bad += 1
                elif m.group(9) == "blocked":
                    blocked += int(m.group(10))
                    last_blocked = ln
                else:
                    allowed += int(m.group(10))
                    last_allowed = ln
            if allowed:
                out.append({"flag": "deny_guard", "kind": "guard-could-not-start", "count": allowed, "last": last_allowed[:120],
                            "text": "SECURITY: the deny guard could not start %d time(s) (last: %s); those commands were allowed "
                                    "without its checks. Fix the install or see docs/BREAK-GLASS-relaunch.md; delete the log once fixed."
                                    % (allowed, last_allowed[:80])})
            if blocked:
                out.append({"flag": "deny_guard", "kind": "guard-start-blocked", "count": blocked, "last": last_blocked[:120],
                            "text": "SECURITY: the deny guard could not start or did not finish %d time(s) (last: %s); those commands were "
                                    "REFUSED. Fix the install or see docs/BREAK-GLASS-relaunch.md; move the log aside once fixed."
                                    % (blocked, last_blocked[:80])})
            if bad:
                out.append({"flag": "deny_guard", "kind": "guard-log-unreadable-lines", "count": bad,
                            "text": "unreadable guard log lines: %d (not in the start-up failure line shape; a session may still run an older "
                                    "hook line). See docs/BREAK-GLASS-relaunch.md." % bad})
        except OSError:
            pass
    except Exception:  # noqa: BLE001 - a banner helper must never break the endpoint
        pass
    return out


def _security_flag_off_requests(cc, fields: dict) -> List[str]:
    """Security flags this save would turn OFF (currently ON, posted OFF)."""
    out = []
    for name in flags.SECURITY_FLAGS:
        key = "flags." + name
        if key not in fields:
            continue
        try:
            new_on = cc._parse_bool(key, fields[key])
        except Exception:  # noqa: BLE001 - the controller's own validation will refuse it below
            continue
        if not new_on and _flag(name):
            out.append(name)
    # the top-level security settings, in the same order ConfigStore.security_flags_turned_off lists them (after the flags),
    # because the presence grant is bound to the digest of this exact list. "Currently ON" = what the file on disk reads now.
    store = _machine_config_store()
    for name in flags.SECURITY_SETTINGS:
        if name not in fields:
            continue
        try:
            new_on = cc._parse_bool(name, fields[name])
        except Exception:  # noqa: BLE001
            continue
        try:
            was_on = getattr(store.load(), name, True) is not False if store is not None else True
        except Exception:  # noqa: BLE001
            was_on = True
        if not new_on and was_on:
            out.append(name)
    return out


def _security_flag_digest(names: List[str]) -> str:
    import hashlib

    store = _machine_config_store()
    try:
        disk = dict(getattr(store.load(), "flags", {}) or {}) if store is not None else {}
        disk.update({n: getattr(store.load(), n, True) for n in flags.SECURITY_SETTINGS} if store is not None else {})
    except Exception:  # noqa: BLE001
        disk = {}
    blob = json.dumps({"off": sorted(names), "disk_flags": disk}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@app.post("/api/config")
def config_save(req: ConfigSaveRequest):
    """Persist a whole-config edit through the reused ``ConfigController``.

    ⛔ Server-side authority: unknown keys, malformed int/csv values, an empty MCP
    set, and the D9 measurement-server refusal all raise inside the reused controller and
    return a 400 with the human message — NOTHING is persisted on refusal. The
    browser's disabled-Save state is a convenience mirror, never the gate."""
    cc = get_config_controller()
    # confirm-proof F1(b): turning a SECURITY flag OFF needs a passed presence check, bound to this exact change
    # (the digest of what is turned off + the flags on disk is taken before the prompt and re-checked after it).
    going_off = _security_flag_off_requests(cc, req.fields)
    grant = None
    if going_off:
        from gnomon import human_confirm as hc

        digest = _security_flag_digest(going_off)
        res, grant = hc.prove_presence_grant("turn OFF the security setting " + ", ".join(going_off),
                                             hc.change_digest(going_off))
        ev = res.evidence()
        if not ev["ok"] or _security_flag_digest(going_off) != digest:
            why = ("the configuration changed while you were confirming" if ev["ok"]
                   else "your Mac password / Touch ID was needed and the check did not pass (" + ev["result"] + ")")
            print(f"[gm-web] config: turning OFF {going_off} REFUSED: {why}", flush=True)
            msg = f"NOT saved: {why}. Nothing was changed."
            return JSONResponse(status_code=409, content={"ok": False, "presence_refused": True, "error": msg,
                                                          "detail": msg, "presence": ev})
        print(f"[gm-web] config: security flag(s) {going_off} turned OFF with presence {ev['result']} at "
              f"{ev['ts']}", flush=True)
    try:
        for key, raw in req.fields.items():
            cc.set(key, raw)  # validated per field kind; unknown key raises
        # Reconcile the custom-template map to exactly what was posted, via the
        # controller's validated add/remove (reimplements no template logic).
        for name in list(cc.custom_templates()):
            if name not in req.custom_terminal_templates:
                cc.remove_terminal_template(name)
        for name, tpl in req.custom_terminal_templates.items():
            cc.add_terminal_template(name, tpl)
        path = cc.save(presence_grant=grant)  # D9 guard re-checked here, server-side
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"ok": False, "error": str(exc)})
    return {"ok": True, "saved_to": path, "config_exists": True}


# --- V1: the agent mail tool (Mail panel + the loopback verb broker) ------------------------------------
#
# Behind ``agent_mail`` (registry default OFF). OFF: every /api/mail/* route answers the SAME 404 an unknown
# path gets (every method), /mail-ui/* is a 404, "/" is untouched. ON: the Mail panel script is injected into "/".
# The OAuth token lives ONLY in this process / <DATA>/mail/ (0600); the self-verbs reach it through POST
# /api/mail/verb, whose result (never the token) goes back to the agent's harness.
# WHO MAY DRIVE WHAT: the operator's panel actions (config, connect, disconnect) go through the SAME guard the Apps tab uses
# for its operator-only writes, ``_apps_guard(write=True)`` (loopback Host, same-origin Origin, Sec-Fetch-Site, and its CSRF
# token ``_APPS_CSRF``), plus a MANDATORY Sec-Fetch-Site header (a browser always sends it; a bare curl/urllib does
# not). A request with no Origin is refused. The verb route takes NO identity from its body: the caller is whoever the
# per-session TICKET authenticates (``SessionRegistry.verify_ticket``; see gnomon.mail.ticket), and a browser
# page (an Origin header) is refused. KNOWN LIMIT (stated in RUN_SUMMARY-mail-v1.md): this loopback server has no
# operator credential, so a same-uid PROCESS can still forge browser headers and fetch the CSRF token (as it can for
# the Apps tab): "operator-only" holds against agent verbs and web pages, not against a determined local process.

#: Test seam: a MailRuntime (None = built lazily from the machine config's DATA root).
_mail_runtime_override = None
_mail_runtime_cache = None
MAIL_UI_DIR = os.path.join(_WEB_DIR, "mail_ui")
MAIL_UI_ASSETS = {"mail_panel.js": "application/javascript"}
MAIL_UI_SCRIPT_TAG = '<script src="/mail-ui/mail_panel.js"></script>'
_MAIL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _mail_enabled() -> bool:
    try:
        from gnomon.mail import FLAG_NAME as _MAIL_FLAG
    except Exception as exc:  # noqa: BLE001 - a broken mail package turns mail OFF, never the page
        print(f"[gm-web] mail unavailable: {type(exc).__name__}: {exc}", flush=True)
        return False
    return _flag(_MAIL_FLAG)


def _mail_runtime():
    global _mail_runtime_cache
    if _mail_runtime_override is not None:
        return _mail_runtime_override
    if _mail_runtime_cache is None:
        from gnomon.appshost.state import data_root
        from gnomon.mail.runtime import MailRuntime

        store = _machine_config_store()
        cfg = store.load() if store is not None else None

        def _verify(ticket, op, args):
            hub = getattr(controller, "_hub", None)
            return hub.registry.verify_ticket(ticket, op, args) if hub is not None else None

        # state lives in ~/Library/Application Support/Gnomon/mail (not the cloud-sync DATA root); the DATA root is only
        # checked for a leftover token at the old location.
        _mail_runtime_cache = MailRuntime(data_root(cfg), ticket_verifier=_verify)
    return _mail_runtime_cache


def _mail_guard(request: Request, *, write: bool) -> None:
    """The mail routes' guard: the Apps tab's ``_apps_guard`` (the existing mechanism for operator-only browser writes),
    and for a write a Sec-Fetch-Site header is MANDATORY (browsers always send it)."""
    _apps_guard(request, write=write)
    if write and request.headers.get("sec-fetch-site") != "same-origin":
        raise HTTPException(status_code=403, detail="refused: a mail action needs Sec-Fetch-Site: same-origin "
                                                    "(it comes from the dashboard page)")


async def _mail_json_body(request: Request) -> dict:
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="mail actions require Content-Type: application/json")
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    return body


def _mail_fail(exc: Exception, status: int = 400):
    return JSONResponse(status_code=status, content={"ok": False, "error": str(exc)})


@app.api_route("/api/mail/status", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_status(request: Request):
    if not _mail_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _mail_guard(request, write=False)
    return {**await run_in_threadpool(_mail_runtime().status), "csrf": _APPS_CSRF}


@app.api_route("/api/mail/config", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_config(request: Request):
    """GET = the config (+ derived examples); POST = the operator's whole-config save (strictly validated)."""
    from gnomon.mail.errors import MailError

    if not _mail_enabled():
        raise _not_found()
    rt = _mail_runtime()
    try:
        if request.method == "GET":
            _mail_guard(request, write=False)
            return await run_in_threadpool(rt.get_config)
        if request.method == "POST":
            _mail_guard(request, write=True)
            body = await _mail_json_body(request)
            return {"ok": True, "config": await run_in_threadpool(rt.save_config, body)}
    except MailError as exc:
        return _mail_fail(exc)
    raise HTTPException(status_code=405, detail="Method Not Allowed")


@app.api_route("/api/mail/connect", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_connect(request: Request):
    """The operator presses 'Connect mail': returns the Google consent URL (the page opens it in the local browser)."""
    from gnomon.mail.errors import MailError

    if not _mail_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _mail_guard(request, write=True)
    await _mail_json_body(request)
    port = (request.scope.get("server") or ("127.0.0.1", 8770))[1]
    try:
        url = await run_in_threadpool(_mail_runtime().connect_start, f"http://127.0.0.1:{port}/api/mail/oauth/callback")
    except MailError as exc:
        return _mail_fail(exc)
    return {"ok": True, "auth_url": url}


@app.api_route("/api/mail/oauth/callback", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_oauth_callback(request: Request):
    """Google's loopback redirect: exchanges the code (scope set asserted exactly), stores the token (0600)."""
    from html import escape

    from gnomon.mail.errors import MailError

    if not _mail_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _mail_guard(request, write=False)
    q = request.query_params
    if q.get("error"):
        msg, code = "Consent was not granted: " + str(q.get("error"))[:80], 400
    else:
        try:
            res = await run_in_threadpool(_mail_runtime().connect_complete, q.get("state", ""), q.get("code", ""))
            msg, code = "Mail connected for " + res["account"] + ". You can close this tab.", 200
        except MailError as exc:
            msg, code = "Mail was NOT connected: " + str(exc), 400
    return HTMLResponse("<!doctype html><meta charset=utf-8><title>Gnomon mail</title><body style=\"font-family:"
                        "-apple-system,system-ui,sans-serif;background:#1e1e1e;color:#eee;padding:2em\"><p>"
                        + escape(msg) + "</p></body>", status_code=code)


@app.api_route("/api/mail/disconnect", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_disconnect(request: Request):
    if not _mail_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _mail_guard(request, write=True)
    await _mail_json_body(request)
    return await run_in_threadpool(_mail_runtime().disconnect)


@app.api_route("/api/mail/activity", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_activity(request: Request):
    if not _mail_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _mail_guard(request, write=False)
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        limit = 100
    return {"rows": await run_in_threadpool(_mail_runtime().activity.tail, limit)}


@app.api_route("/api/mail/verb", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_verb(request: Request):
    """The broker the harness's self-verbs call (loopback, no browser). Body: {ticket, op, args}. The identity is the
    ticket's (a per-session MAC the caller's own proxy minted); a body that names an instance is refused."""
    if not _mail_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _mail_guard(request, write=False)
    if "origin" in request.headers:
        raise HTTPException(status_code=403, detail="refused: the mail verb route is not for browser pages")
    body = await _mail_json_body(request)
    if set(body) - {"ticket", "op", "args"}:
        raise HTTPException(status_code=400, detail="body must be {ticket, op, args} (no instance_id: identity is the ticket's)")
    return await run_in_threadpool(_mail_runtime().verb, body.get("ticket"), body.get("op"), body.get("args", {}))


@app.api_route("/mail-ui/{name}", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_ui_asset(name: str, request: Request):
    """The Mail panel script, served ONLY while the flag is ON (closed name allowlist)."""
    if not _mail_enabled() or name not in MAIL_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(MAIL_UI_DIR, name), media_type=MAIL_UI_ASSETS[name])


# P3 (Diamond F10): EVERYTHING else under /api/mail and /mail-ui is a plain 404 whatever the flag, so the framework's
# trailing-slash redirect (307) never reveals a mail route behind a flag-OFF server. Registered LAST on purpose.
@app.api_route("/api/mail", methods=_MAIL_METHODS, include_in_schema=False)
@app.api_route("/api/mail/{tail:path}", methods=_MAIL_METHODS, include_in_schema=False)
@app.api_route("/mail-ui", methods=_MAIL_METHODS, include_in_schema=False)
@app.api_route("/mail-ui/{tail:path}", methods=_MAIL_METHODS, include_in_schema=False)
async def mail_not_found(request: Request, tail: str = ""):
    raise _not_found()


# --- corpus tabs + the PROCs tab (graph viewer — the D3 lts-engine window) -----
#
# Read-only master/detail tabs over the LIVE decision graph: the PROCs tab, plus any corpus tabs the deployment declares. The routes are a
# thin JSON seam over the CTk-free ``GraphViewer`` (``gnomon.app.
# graph_viewer``), which builds the SAME in-process store the lts-engine daemon
# builds (``factlattice_engine.config.build_store`` over the corpus registry) and
# queries it through the engine's own ``LtsEngine.find`` / ``.node`` — reimplementing
# no query logic. The engine self-heals on edits, so a changed ``.lts`` shows up on
# the next request. ⛔ If the registry cannot be resolved or the engine cannot be
# built, every route returns 503 with the reason (loud; no stale fallback).

#: Test seam: tests inject a ``GraphViewer`` over tmp fixture corpora. Production
#: builds one lazily (first request; ~7s for the full registry, then cached).
_graph_viewer = None  # type: ignore[assignment]
_graph_viewer_lock = threading.Lock()


def get_graph_viewer():
    global _graph_viewer
    from gnomon.app.graph_viewer import GraphViewer

    with _graph_viewer_lock:
        if _graph_viewer is None:
            _graph_viewer = GraphViewer()
        return _graph_viewer


def _graph_call(fn, *args):
    from gnomon.app.graph_viewer import GraphNodeNotFound, GraphViewerUnavailable

    try:
        return fn(*args)
    except GraphNodeNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except GraphViewerUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"lts-engine unavailable: {exc}")


@app.get("/api/graph/status")
def graph_status() -> dict:
    """Is the graph viewer live, and against which registry (shown as the tab banner)."""
    return get_graph_viewer().status()


@app.get("/api/branding/logo")
def branding_logo() -> Response:
    """The header logo, when this DEPLOYMENT carries one (``<deploy_root>/defaults/branding.json``; round 5 item 8). EXACTLY one file, chosen by
    the deployment table, validated and size-capped in :mod:`gnomon.branding`; no parameter, no directory serving. 404 when there is none
    (the page then keeps its empty span)."""
    from gnomon import branding

    asset = branding.logo_asset()
    if asset is None:
        raise HTTPException(status_code=404, detail="no deployment logo")
    data, ctype = asset
    return Response(content=data, media_type=ctype, headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"})


@app.get("/api/corpus-tabs")
def corpus_tabs_config() -> dict:
    """The corpus tabs this DEPLOYMENT declares (``<deploy_root>/defaults/corpus-tabs.json``; X13 S5-3). Gnomon
    ships none: with no deployment data this is ``{"tabs": []}``. ``errors`` names every rejected tab."""
    from gnomon.app.graph_viewer import load_corpus_tabs

    tabs, errors = load_corpus_tabs()
    return {"tabs": tabs, "errors": errors}


@app.get("/api/corpus-tabs/{tab_id}")
def corpus_tab_list(tab_id: str) -> dict:
    """Master list of one configured tab: the roster node (header) + every item with its tab-declared fields."""
    from gnomon.app.graph_viewer import get_corpus_tab

    return _graph_call(lambda: get_graph_viewer().corpus_tab(get_corpus_tab(tab_id)))


@app.get("/api/corpus-tabs/{tab_id}/{node_id}")
def corpus_tab_detail(tab_id: str, node_id: str) -> dict:
    """Detail pane: one node of the tab's corpus (properties + body + edges, lts_node full)."""
    from gnomon.app.graph_viewer import get_corpus_tab

    return _graph_call(lambda: get_graph_viewer().corpus_tab_node(get_corpus_tab(tab_id), node_id))


@app.get("/api/procs")
def procs_list() -> dict:
    """Master list: lts_find(prefix="PROC") across every corpus, grouped by corpus,
    with the funnel (corpora/nodes in scope, total matched, complete)."""
    return _graph_call(get_graph_viewer().procs)


@app.get("/api/procs/{corpus}/{node_id}")
def proc_detail(corpus: str, node_id: str) -> dict:
    """Detail pane: one PROC's properties + body + its STEP children + edges."""
    return _graph_call(get_graph_viewer().proc, corpus, node_id)


# --- (specs/15, slice 2): the Work tab ------------------------------------------------------------
#
# Behind ``open_work_list`` (registry default OFF). OFF: every route below answers the SAME 404 an unknown path gets, for EVERY method,
# the routes are not in the schema, and "/" is untouched (byte-identical page). ON: the Work tab script is injected into "/" and the two GET
# routes only CALL the slice-1 read functions (``worklist.api``). READ-ONLY by construction: no route accepts a write (any method but GET is
# 405), there is no CSRF token because there is nothing to protect, and every route answers only this server's own loopback Host (the same
# DNS-rebinding refusal the Apps routes use: other agents' lists are not for a foreign page). All agent-written text is returned as data
# and drawn with textContent by the script.

#: Test seam: a dict {data_root, deploy_root, label, known, now} (None = the machine's real values).
_work_override = None
WORK_UI_DIR = os.path.join(_WEB_DIR, "work_ui")
WORK_UI_ASSETS = {"work_tab.js": "application/javascript"}
WORK_UI_SCRIPT_TAG = '<script src="/work-ui/work_tab.js"></script>'
_WORK_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _work_enabled() -> bool:
    try:
        from gnomon.worklist.constants import FLAG_NAME as _WORK_FLAG
    except Exception as exc:  # noqa: BLE001 - a broken package turns the tab OFF, never the page
        print(f"[gm-web] Work tab unavailable: {type(exc).__name__}: {exc}", flush=True)
        return False
    return _flag(_WORK_FLAG)


def _work_context() -> dict:
    if _work_override is not None:
        return dict(_work_override)
    from gnomon.worklist import launch as _wl

    store = _machine_config_store()
    cfg = store.load() if store is not None else None
    ow = _wl.resolve({}, cfg, "")
    try:
        known = list(controller.list_profile_ids())
    except Exception:  # noqa: BLE001 - the roster unreadable: nothing is marked retired
        known = None
    return {"data_root": ow["data_root"], "deploy_root": ow.get("deploy_root"), "label": ow["operator_label"], "known": known, "now": None}


def _work_gate(request: Request) -> dict:
    if not _work_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _apps_guard(request, write=False)
    return _work_context()


@app.api_route("/api/work", methods=_WORK_METHODS, include_in_schema=False)
async def work_root(request: Request):
    """GET = the Work tab's overview: every agent with its counts and badges, the cross-agent 'Waiting on you' list, the declared status."""
    ctx = _work_gate(request)
    from gnomon.worklist import api as _wapi

    now = ctx.get("now") if ctx.get("now") is not None else time.time()
    return await run_in_threadpool(lambda: _wapi.work_overview(ctx["data_root"], now, ctx.get("deploy_root"), ctx.get("known"), ctx["label"]))


@app.api_route("/api/work/{instance_id}", methods=_WORK_METHODS, include_in_schema=False)
async def work_agent_detail(instance_id: str, request: Request):
    """GET = one agent's open items (the right-hand pane)."""
    ctx = _work_gate(request)
    from gnomon.worklist import api as _wapi

    now = ctx.get("now") if ctx.get("now") is not None else time.time()
    out = await run_in_threadpool(lambda: _wapi.work_agent(ctx["data_root"], instance_id, now, ctx.get("deploy_root"), ctx["label"], ctx.get("known")))
    if out.get("ok") is False:
        raise HTTPException(status_code=400, detail=out.get("error") or "bad request")
    return out


@app.api_route("/work-ui/{name}", methods=_WORK_METHODS, include_in_schema=False)
async def work_ui_asset(name: str, request: Request):
    """The Work tab script, served ONLY while the flag is ON (closed name allowlist)."""
    if not _work_enabled() or name not in WORK_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(WORK_UI_DIR, name), media_type=WORK_UI_ASSETS[name])


# --- the "a deny guard could not start" banner (gnomon.guard_banner) ---------------------------------------------------
#
# Behind ``guard_start_failure_banner`` (registry default OFF). OFF: both routes below answer the SAME 404 an unknown path gets, for EVERY
# method, they are not in the schema, and "/" is untouched (byte-identical page). ON: the script is injected into "/" and the one GET route reads
# <data root>/deny-guard/startup-failures.log through the never-block bounded reader and answers what the banner shows. Read-only (any other
# method is 405); it creates no file, lock or directory (g106); loopback Host only (the Apps routes' DNS-rebinding refusal).

#: Test seam: a dict {path, now} (None = the guard's real log path and the server's clock).
_guard_banner_override = None
GUARD_BANNER_UI_DIR = os.path.join(_WEB_DIR, "guard_banner_ui")
GUARD_BANNER_UI_ASSETS = {"guard_banner.js": "application/javascript"}
GUARD_BANNER_UI_SCRIPT_TAG = '<script src="/guard-banner-ui/guard_banner.js"></script>'
_GUARD_BANNER_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _guard_banner_enabled() -> bool:
    try:
        from gnomon.guard_banner import FLAG_NAME as _GB_FLAG
    except Exception as exc:  # noqa: BLE001 - a broken package turns the banner OFF, never the page
        print(f"[gm-web] guard start-failure banner unavailable: {type(exc).__name__}: {exc}", flush=True)
        return False
    return _flag(_GB_FLAG)


@app.api_route("/api/guard-start-failures", methods=_GUARD_BANNER_METHODS, include_in_schema=False)
async def guard_start_failures(request: Request):
    """GET = what the banner shows now: state none | failures | unreadable, with the exact text."""
    if not _guard_banner_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _apps_guard(request, write=False)
    from gnomon import guard_banner as _gb

    if _guard_banner_override is not None:
        path, now = _guard_banner_override.get("path"), _guard_banner_override.get("now")
    else:
        try:
            from gnomon.harness import deny_guard as _dg
            path, now = _dg.startup_failures_path(), None
        except Exception as exc:  # noqa: BLE001 - a path that cannot even be worked out is a reading that could not be taken
            return _gb._unreadable("the log's location could not be worked out (%s)" % type(exc).__name__, time.time())
    return await run_in_threadpool(lambda: _gb.read_state(path, now))


@app.api_route("/guard-banner-ui/{name}", methods=_GUARD_BANNER_METHODS, include_in_schema=False)
async def guard_banner_ui_asset(name: str, request: Request):
    """The banner script, served ONLY while the flag is ON (closed name allowlist)."""
    if not _guard_banner_enabled() or name not in GUARD_BANNER_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(GUARD_BANNER_UI_DIR, name), media_type=GUARD_BANNER_UI_ASSETS[name])


# --- (core): the Gnomon alert service --------------------------------------------------------------
#
# Behind ``alerts`` (registry default OFF). OFF: every route below answers the SAME 404 an unknown path
# gets, for EVERY method (the Apps pattern), the routes are not in the schema, "/" is untouched (no script, no
# stylesheet) and no producer raises. ON: the page loads /alerts-ui/alerts.js (the stacked alert column) and
# these routes serve it. The store, de-duplication, rate limit, plain-text rules and the macOS channel are
# ``gnomon.alerts``; this block is only the HTTP face. Alerts are UI only: nothing here (or there) can type
# into an agent's PTY. There is deliberately NO create route (producers are in-process: the harness, the
# DSD-arrival poll, the agent verb); the app callback POST /api/apps/<id>/alert is a later slice.
#
# Who may call: like the Apps routes, only a request whose Host is this server's own loopback address (a DNS-
# rebinding page is refused), and every POST must carry a same-origin Origin, Sec-Fetch-Site same-origin (when
# sent), Content-Type: application/json and this process's CSRF token (read from GET /api/alerts, which a
# cross-origin page cannot read). KNOWN LIMIT (as for Apps): a same-uid local process can read the token like the
# page does; these checks stop a web page, not a local process. What a local process could do here is read alert
# text or dismiss alerts, never create one or reach an agent.

#: Test seam: an AlertStore (None = the machine's DATA-root store).
_alerts_store_override = None
ALERTS_UI_DIR = os.path.join(_WEB_DIR, "alerts_ui")
ALERTS_UI_ASSETS = {"alerts.js": "application/javascript", "alerts.css": "text/css"}
ALERTS_UI_TAGS = ['<link rel="stylesheet" href="/alerts-ui/alerts.css">',
                  '<script src="/alerts-ui/alerts.js"></script>']
_ALERTS_CSRF = __import__("secrets").token_urlsafe(32)
ALERTS_CSRF_HEADER = "x-gnomon-csrf"
_ALERTS_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


def _alerts_enabled() -> bool:
    return _flag(alerts_mod.FLAG_NAME)


def _alerts_store():
    if _alerts_store_override is not None:
        return _alerts_store_override
    try:
        root = controller.config.runtime_root
    except Exception:  # noqa: BLE001
        root = None
    return alerts_mod.AlertStore(alerts_mod.default_dir(root))


def _alert_dsd_arrivals(items) -> None:
    """proof producer #2: a newly arrived pending DSD raises one info alert (the existing banner and
    auto-switch are unchanged). Gated by the flag; keyed by the DSD id so a repeat is de-duplicated."""
    try:
        if not items or not _alerts_enabled():
            return
        store = _alerts_store()
        for it in items:
            alerts_mod.raise_from_producer(
                "dsd-arrival", "dsd", "info", f"New DSD from {it.instance_id}", getattr(it, "title", "") or "", "dsd",
                key=f"dsd:{it.dsd_id}", store=store)
    except Exception:  # noqa: BLE001 - an alert never fails the arrivals poll
        pass


def _alerts_guard(request: Request, *, write: bool) -> None:
    import hmac

    if not _apps_host_ok(request):
        raise HTTPException(status_code=403, detail="refused: the alert routes answer only this server's own "
                                                    "loopback Host (127.0.0.1 / localhost on its port)")
    if not write:
        return
    origin = request.headers.get("origin")
    if origin != "http://" + request.headers.get("host", ""):
        raise HTTPException(status_code=403, detail="refused: an alert action needs a same-origin Origin header")
    sfs = request.headers.get("sec-fetch-site")
    if sfs is not None and sfs != "same-origin":
        raise HTTPException(status_code=403, detail="refused: Sec-Fetch-Site is not same-origin")
    ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if ctype != "application/json":
        raise HTTPException(status_code=415, detail="alert actions require Content-Type: application/json")
    tok = request.headers.get(ALERTS_CSRF_HEADER, "")
    if not hmac.compare_digest(tok.encode(), _ALERTS_CSRF.encode()):
        raise HTTPException(status_code=403, detail="refused: missing or wrong CSRF token (reload the page)")


@app.api_route("/api/alerts", methods=_ALERTS_METHODS, include_in_schema=False)
async def alerts_list(request: Request):
    """GET = the active alerts, newest first, plus the CSRF token the dismiss POSTs need."""
    if not _alerts_enabled():
        raise _not_found()
    if request.method != "GET":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _alerts_guard(request, write=False)
    def _snapshot():
        st = _alerts_store()
        items, err = [], None
        try:
            items = st.active()
        except alerts_mod.AlertStoreError as exc:   # a FIFO / directory at a store path, an unusable dir: say so
            err = str(exc)
        except Exception as exc:  # noqa: BLE001 - any other store fault is still a clear body, never a bare 500
            err = f"alerts store unavailable: {type(exc).__name__}: {exc}"
        try:
            by_file = st.failure_counts()      # windowed (last 24 h), written by the harnesses AND this process
        except alerts_mod.AlertStoreError as exc:   # F-R3: e.g. a FIFO at producer-failures.jsonl
            by_file = {}
            err = (err + "; " if err else "") + str(exc)
        mine = alerts_mod.producer_counters()
        return {"alerts": items, "csrf": _ALERTS_CSRF, "now": time.time(), "store_error": err,
                "producer_failures": {"total": sum(by_file.values()), "window_hours": alerts_mod.FAILURES_WINDOW_S / 3600,
                                      "by_producer": by_file, "this_process": mine["failures"],
                                      "dropped": mine["dropped"]}}
    return await run_in_threadpool(_snapshot)


@app.api_route("/api/alerts/dismiss-all", methods=_ALERTS_METHODS, include_in_schema=False)
async def alerts_dismiss_all(request: Request):
    if not _alerts_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _alerts_guard(request, write=True)
    # F6: dismiss exactly the ids the page DISPLAYED; a body without a valid id list is refused, nothing dismissed.
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = None
    ids = body.get("ids") if isinstance(body, dict) else None
    if not isinstance(ids, list) or len(ids) > alerts_mod.ACTIVE_LIMIT or not all(isinstance(i, str) for i in ids):
        raise HTTPException(status_code=400, detail='dismiss-all needs {"ids": [<displayed alert ids>]} '
                                                    f"(a list of at most {alerts_mod.ACTIVE_LIMIT} strings)")
    try:
        n = await run_in_threadpool(lambda: _alerts_store().dismiss_all(ids))
    except alerts_mod.AlertStoreError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "dismissed": n}


@app.api_route("/api/alerts/{alert_id}/dismiss", methods=_ALERTS_METHODS, include_in_schema=False)
async def alerts_dismiss(alert_id: str, request: Request):
    if not _alerts_enabled():
        raise _not_found()
    if request.method != "POST":
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    _alerts_guard(request, write=True)
    found = await run_in_threadpool(lambda: _alerts_store().dismiss(alert_id))
    if not found:
        raise HTTPException(status_code=404, detail="no such alert")
    return {"ok": True, "dismissed": 1}


@app.api_route("/alerts-ui/{name}", methods=_ALERTS_METHODS, include_in_schema=False)
async def alerts_ui_asset(name: str, request: Request):
    if not _alerts_enabled() or name not in ALERTS_UI_ASSETS:
        raise _not_found()
    if request.method not in ("GET", "HEAD"):
        raise HTTPException(status_code=405, detail="Method Not Allowed")
    return FileResponse(os.path.join(ALERTS_UI_DIR, name), media_type=ALERTS_UI_ASSETS[name])


# --- SPA shell ----------------------------------------------------------------

@app.get("/")
def index() -> Response:
    # every flag OFF -> the exact same FileResponse as before (byte-identical).
    tags = []
    if _deliverables_enabled():
        tags.append(LEDGER_UI_SCRIPT_TAG)
    if _apps_enabled():
        tags.append(APPS_UI_SCRIPT_TAG)
    if _alerts_enabled():   #
        tags.extend(ALERTS_UI_TAGS)
    if _sched_enabled():
        tags.append(SCHED_UI_SCRIPT_TAG)
    if _work_enabled():
        tags.append(WORK_UI_SCRIPT_TAG)
    if _guard_banner_enabled():
        tags.append(GUARD_BANNER_UI_SCRIPT_TAG)
    if _mail_enabled():
        tags.append(MAIL_UI_SCRIPT_TAG)
    if tags:
        return _index_with_scripts(tags)
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


# Gnomon's OWN product mark (product asset, not deployment data), served by FIXED NAME from <tree>/assets/brand/.
# Exactly three routes, no parameter and no directory listing; the file is read at request time so replacing an SVG needs no code change.
BRAND_DIR = os.path.join(_PROJECT_ROOT, "assets", "brand")
BRAND_ASSETS = ("gnomon-dark.svg", "gnomon-light.svg", "gnomon-mark.svg")


def _brand_route(name: str) -> None:
    def _serve() -> Response:
        try:
            with open(os.path.join(BRAND_DIR, name), "rb") as fh:
                data = fh.read()
        except OSError:
            raise HTTPException(status_code=404, detail="brand asset missing")
        return Response(content=data, media_type="image/svg+xml",
                        headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache",
                                 "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"})

    _serve.__name__ = "brand_" + name.replace("-", "_").replace(".", "_")
    app.get("/brand/" + name)(_serve)


for _brand_name in BRAND_ASSETS:
    _brand_route(_brand_name)


# Static assets (css/js/logo) under /static.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main() -> None:  # pragma: no cover - real server entry point
    import uvicorn

    host = os.environ.get("GNOMON_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("GNOMON_WEB_PORT", "8770"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    main()

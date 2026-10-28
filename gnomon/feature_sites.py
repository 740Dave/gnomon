"""the CLOSED-SET feature-id mappings the call sites use, and the static call-site scan
(spec-* requirement-known-categories).

A feature id is NEVER derived from typed, model-generated or file-content input
(field-feature-id): a call site passes a string literal, a module constant, or the
result of ONE of the closed-set functions below, each of which returns an id only for a name in a
registered closed set and ``None`` otherwise (and a ``None`` writes no event):

========================  ===========================================================
``command_feature_id``    an operator ``\\command`` word -> the registry node's ``aliases``
``dispatch_feature_id``   a subagent / skill name -> a registered agent-def / skill feature
``self_verb_feature_id``  a self-verb tool name -> the derived verb set
``hub_tool_feature_id``   a hub MCP tool name -> the hub's own tool table (gnomon.hub.feature_ids)
``route_feature_id``      (method, route template) -> :data:`WEB_ROUTES`
``hook_point_feature_id`` a hook point name -> :data:`gnomon.feature_events.HOOK_POINTS`
========================  ===========================================================

:func:`scan_call_sites` is the static AST scan the report and the coverage test use: the feature ids
found at ``feature_event(`` / ``emit_hook*(`` call sites, resolved through the closed-set
functions above, plus any call whose first argument is NOT one of those forms (``dynamic``: a
violation of). The scan lives in :mod:`gnomon.feature_scan`
(NOT here): it reaches into the hub package, and the harness (which imports THIS module) must not
acquire the hub / DSD code as a dependency (the session build stamp's scope, tests/test_bstamp_session_scope.py).

Neutral (g3): feature ids are generic Gnomon feature names.
"""

from __future__ import annotations

import json
import os
import stat
import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from . import feature_events as _fe

# --- web routes --------------------------------------------------------------
#
# USER-INITIATED = a POST, PUT or DELETE, plus the GET that opens a named tab or view (architect
# ruling 2026-09-30). Polling GETs and asset GETs are NOT user-initiated: they are listed
# ``no-event`` with a one-line reason (g41), so the exclusion is a recorded decision, not an omission.
# The split below was derived from the frontend's fetch sites (web/static/*.js): which calls sit on a
# timer, which run once at page load, which open a tab or view. It is DATA: the architect/auditor
# confirms it by editing this table (and the registry node) only.
#: (method, route template) -> feature_id
WEB_ROUTES: Dict[Tuple[str, str], str] = {
    ("POST", "/api/launch"): "launch-session",
    ("POST", "/api/dsd/submit/{dsd_id}"): "dsd-answer",
    ("POST", "/api/dsd/documents/open"): "dsd-document-open",
    ("POST", "/api/dsd/id-lookup"): "dsd-id-lookup",
    ("POST", "/api/def-view/{instance_id}/preview"): "agent-def-preview",
    ("POST", "/api/config"): "config-save",
    ("GET", "/api/config"): "config-tab",
    ("GET", "/api/flags"): "flags-view",
    ("GET", "/api/verb-policy"): "verb-policy-view",
    ("GET", "/api/usage"): "usage-panel",
    ("GET", "/api/procs"): "procs-tab",
    ("GET", "/api/procs/{corpus}/{node_id}"): "proc-open",
    ("GET", "/api/graph/status"): "graph-tab",
    ("GET", "/api/corpus-tabs/{tab_id}"): "corpus-tab",
    ("GET", "/api/corpus-tabs/{tab_id}/{node_id}"): "corpus-tab-item-open",
    ("GET", "/api/dsd/present/{dsd_id}"): "dsd-present",
    ("GET", "/api/dsd/browse/{dsd_id}"): "dsd-browse",
    ("GET", "/api/def-view/{instance_id}/raw"): "agent-def-view",
    ("GET", "/api/agent-def/optimize/{instance_id}"): "agent-def-optimize",
    ("GET", "/api/deliverables"): "deliverables-panel",
    ("POST", "/api/deliverables"): "deliverables-update",
    ("POST", "/api/deliverables/open"): "deliverable-open",
    ("PUT", "/api/deliverables"): "deliverables-update",
    ("PATCH", "/api/deliverables"): "deliverables-update",
    ("DELETE", "/api/deliverables"): "deliverables-update",
}

#: (method, route template) -> the one-line reason it emits nothing
WEB_NO_EVENT: Dict[Tuple[str, str], str] = {
    ("GET", "/api/golive"): "polled by the header chip on a timer; no user action",
    ("GET", "/api/security-warnings"): "polled by the security banner on a timer; no user action",
    ("GET", "/api/health"): "polled on a timer; no user action",
    ("GET", "/api/build"): "build-stamp read for the header chip; no user action",
    ("GET", "/api/profiles"): "loaded once at page load to fill the launch menu; the launch itself is launch-session",
    ("GET", "/api/branding/logo"): "the header logo asset, fetched once at page load (an asset GET), only when the deployment carries one",
    ("GET", "/brand/gnomon-dark.svg"): "Gnomon's own header mark, an asset GET at page load or README render; no user action",
    ("GET", "/brand/gnomon-light.svg"): "Gnomon's own mark, light variant, an asset GET at page load or README render; no user action",
    ("GET", "/brand/gnomon-mark.svg"): "Gnomon's own sundial, the tab icon, an asset GET at page load or README render; no user action",
    ("GET", "/api/corpus-tabs"): "loaded once at page load to build the tab bar from the deployment's tab declarations; the tab open itself is corpus-tab",
    ("GET", "/api/sessions"): "polled on a timer; no user action",
    ("GET", "/api/dsd/status"): "polled/loaded by the DSD list; no user action",
    ("GET", "/api/dsd/list"): "polled on a timer; no user action",
    ("GET", "/api/dsd/notices"): "loaded with the DSD list; no user action",
    ("GET", "/api/dsd/arrivals"): "polled on a timer; no user action",
    ("POST", "/api/dsd/activity"): "review-page presence beacon (throttled, never content), not a user action",
    ("DELETE", "/api/def-view/preview/{preview_id}"): "automatic cleanup of an ephemeral preview on close; the preview itself is agent-def-preview",
    ("GET", "/"): "the SPA shell (an asset GET)",
    ("GET", "/openapi.json"): "framework-generated API docs, not a Gnomon feature",
    ("HEAD", "/openapi.json"): "framework-generated API docs, not a Gnomon feature",
    ("GET", "/docs"): "framework-generated API docs, not a Gnomon feature",
    ("HEAD", "/docs"): "framework-generated API docs, not a Gnomon feature",
    ("GET", "/docs/oauth2-redirect"): "framework-generated API docs, not a Gnomon feature",
    ("HEAD", "/docs/oauth2-redirect"): "framework-generated API docs, not a Gnomon feature",
    ("GET", "/redoc"): "framework-generated API docs, not a Gnomon feature",
    ("HEAD", "/redoc"): "framework-generated API docs, not a Gnomon feature",
    ("GET", "/deliverables-ui/{name}"): "static asset GET",
    ("HEAD", "/deliverables-ui/{name}"): "static asset GET",
    ("OPTIONS", "/deliverables-ui/{name}"): "static asset (CORS preflight)",
    ("POST", "/deliverables-ui/{name}"): "asset route: not a user action",
    ("PUT", "/deliverables-ui/{name}"): "asset route: not a user action",
    ("PATCH", "/deliverables-ui/{name}"): "asset route: not a user action",
    ("DELETE", "/deliverables-ui/{name}"): "asset route: not a user action",
    ("HEAD", "/api/deliverables"): "not a user action (probe)",
    ("OPTIONS", "/api/deliverables"): "CORS preflight, not a user action",
    ("GET", "/api/deliverables/open"): "route answers a method it does not serve (404/405): not a user action",
    ("HEAD", "/api/deliverables/open"): "not a user action (probe)",
    ("OPTIONS", "/api/deliverables/open"): "CORS preflight, not a user action",
    ("PUT", "/api/deliverables/open"): "route answers a method it does not serve (404/405): not a user action",
    ("PATCH", "/api/deliverables/open"): "route answers a method it does not serve (404/405): not a user action",
    ("DELETE", "/api/deliverables/open"): "route answers a method it does not serve (404/405): not a user action",
}
#: path prefixes (mounts) that are assets by construction
WEB_NO_EVENT_PREFIXES: Dict[str, str] = {"/static": "static asset mount"}

# --- (spec 13): the Apps tab routes (g41). Each route is registered for every method (a flag-OFF 404 must
# not be a revealing 405), so every (method, route) pair is either a feature or no-event, generated here from ONE
# table so a new Apps route is listed in the same change.
_APPS_ALL_METHODS = ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
#: (route template) -> {method: feature_id}; every other method of that route is no-event
_APPS_ROUTE_FEATURES: Dict[str, Dict[str, str]] = {
    "/api/apps": {"POST": "apps-propose"},
    "/api/apps/{app_id}": {"GET": "apps-detail"},
    "/api/apps/{app_id}/start": {"POST": "apps-start"},
    "/api/apps/{app_id}/stop": {"POST": "apps-stop"},
    "/api/apps/{app_id}/open": {"POST": "apps-open"},
    "/api/apps/{app_id}/confirm": {"POST": "apps-confirm"},
    "/api/apps/{app_id}/decline": {"POST": "apps-decline"},
    "/api/apps/{app_id}/settings": {"POST": "apps-settings"},
    "/api/apps/{app_id}/backup": {"POST": "apps-record-backup"},
    "/api/apps/{app_id}/uninstall": {"POST": "apps-propose-uninstall"},
    "/api/apps/{app_id}/actions/{name}": {"POST": "apps-run-action"},
    "/apps-ui/{name}": {},
}
for _path, _feats in _APPS_ROUTE_FEATURES.items():
    for _m in _APPS_ALL_METHODS:
        if _m in _feats:
            WEB_ROUTES[(_m, _path)] = _feats[_m]
        elif _path == "/api/apps" and _m == "GET":
            WEB_NO_EVENT[(_m, _path)] = "polled by the Apps tab (list + the Pending notice); opening an app is apps-detail"
        elif _path == "/apps-ui/{name}":
            WEB_NO_EVENT[(_m, _path)] = "the Apps tab's static script (asset route): not a user action"
        else:
            WEB_NO_EVENT[(_m, _path)] = "an Apps route called with a method it does not serve (404/405): not a user action"
del _path, _feats, _m
# --- (spec 14, stage 1a): the Scheduler / Run Log routes (g41), generated from one table like the Apps routes.
#: (route template) -> {method: feature_id}; every other method of that route is no-event. The tab's list poll, the
#: detail GET and the receipt GET are reads; the five POSTs are the operator's actions (a job is never created here).
_SCHED_ROUTE_FEATURES: Dict[str, Dict[str, str]] = {
    "/api/scheduler": {},
    "/api/scheduler/runlog": {"GET": "scheduler-run-log"},
    "/api/scheduler/runs/{job_id}/{run_id}": {"GET": "scheduler-run-receipt"},
    "/api/scheduler/{job_id}": {"GET": "scheduler-detail"},
    "/api/scheduler/{job_id}/confirm": {"POST": "scheduler-confirm"},
    "/api/scheduler/{job_id}/decline": {"POST": "scheduler-decline"},
    "/api/scheduler/{job_id}/cancel": {"POST": "scheduler-cancel"},
    "/api/scheduler/{job_id}/pause": {"POST": "scheduler-pause"},
    "/api/scheduler/{job_id}/enable": {"POST": "scheduler-enable"},
    "/scheduler-ui/{name}": {},
}
for _path, _feats in _SCHED_ROUTE_FEATURES.items():
    for _m in _APPS_ALL_METHODS:
        if _m in _feats:
            WEB_ROUTES[(_m, _path)] = _feats[_m]
        elif _path == "/api/scheduler" and _m == "GET":
            WEB_NO_EVENT[(_m, _path)] = "polled by the Scheduler tab (list + the Pending notice): not a user action"
        elif _path == "/scheduler-ui/{name}":
            WEB_NO_EVENT[(_m, _path)] = "the Scheduler tab's static script (asset route): not a user action"
        else:
            WEB_NO_EVENT[(_m, _path)] = "a Scheduler route called with a method it does not serve (404/405): not a user action"
del _path, _feats, _m
# --- (specs/15, slice 2): the Work tab routes (g41), generated from one table like the Apps routes. All reads: the overview is polled
# (no event); opening an agent's pane is the one user action.
_WORK_ROUTE_FEATURES: Dict[str, Dict[str, str]] = {
    "/api/work": {},
    "/api/work/{instance_id}": {"GET": "work-agent-detail"},
    "/work-ui/{name}": {},
}
for _path, _feats in _WORK_ROUTE_FEATURES.items():
    for _m in _APPS_ALL_METHODS:
        if _m in _feats:
            WEB_ROUTES[(_m, _path)] = _feats[_m]
        elif _path == "/api/work" and _m == "GET":
            WEB_NO_EVENT[(_m, _path)] = "polled by the Work tab (the overview); opening an agent's pane is work-agent-detail"
        elif _path == "/work-ui/{name}":
            WEB_NO_EVENT[(_m, _path)] = "the Work tab's static script (asset route): not a user action"
        else:
            WEB_NO_EVENT[(_m, _path)] = "a Work route called with a method it does not serve (404/405): not a user action"
del _path, _feats, _m
# --- the deny-guard start-failure banner (guard_start_failure_banner), g41: every route is polling or an asset, so none emits an event.
for _path in ("/api/guard-start-failures", "/guard-banner-ui/{name}"):
    for _m in _APPS_ALL_METHODS:
        if _path == "/api/guard-start-failures" and _m == "GET":
            WEB_NO_EVENT[(_m, _path)] = "polled by the deny-guard start-failure banner on a timer; no user action"
        elif _path == "/guard-banner-ui/{name}":
            WEB_NO_EVENT[(_m, _path)] = "the banner's static script (asset route): not a user action"
        else:
            WEB_NO_EVENT[(_m, _path)] = "a banner route called with a method it does not serve (404/405): not a user action"
del _path, _m
# the id-lookup beacon is registered for every method (flag OFF: 404 for all, never a revealing 405)
for _m in _APPS_ALL_METHODS:
    if _m != "POST":
        WEB_NO_EVENT[(_m, "/api/dsd/id-lookup")] = "the usage beacon answers only POST (404/405 otherwise): not a user action"
del _m

# --- V1: the mail routes (g41). Same shape as the Apps table: every route is registered for every method
# (a flag-OFF 404 must not be a revealing 405), so each (method, route) pair is a feature or no-event, from ONE table.
#: (route template) -> {method: feature_id}; every other method of that route is no-event
_MAIL_ROUTE_FEATURES: Dict[str, Dict[str, str]] = {
    "/api/mail/status": {},
    "/api/mail/config": {"POST": "mail-config-save"},
    "/api/mail/connect": {"POST": "mail-connect"},
    "/api/mail/oauth/callback": {"GET": "mail-connect-complete"},
    "/api/mail/disconnect": {"POST": "mail-disconnect"},
    "/api/mail/activity": {},
    "/api/mail/verb": {},
    "/mail-ui/{name}": {},
    "/api/mail": {},
    "/api/mail/{tail:path}": {},
    "/mail-ui": {},
    "/mail-ui/{tail:path}": {},
}
_MAIL_NO_EVENT_REASONS: Dict[str, Dict[str, str]] = {
    "/api/mail/status": {"GET": "polled by the Mail tab; no user action"},
    "/api/mail/config": {"GET": "loaded with the Mail tab; no user action"},
    "/api/mail/activity": {"GET": "polled by the Mail tab; no user action"},
    "/api/mail/verb": {"POST": "the loopback broker for the mail self-verbs, which emit their own self-verb events (mail-list, mail-read, mail-draft, mail-to-operator)"},
    "/mail-ui/{name}": {"GET": "the Mail tab's static script (asset route): not a user action",
                        "HEAD": "the Mail tab's static script (asset route): not a user action"},
}
for _p in ("/api/mail", "/api/mail/{tail:path}", "/mail-ui", "/mail-ui/{tail:path}"):
    _MAIL_NO_EVENT_REASONS[_p] = {m: "the mail catch-all: always a plain 404 (flag off or not), never a user action"
                                  for m in _APPS_ALL_METHODS}
del _p
for _path, _feats in _MAIL_ROUTE_FEATURES.items():
    for _m in _APPS_ALL_METHODS:
        if _m in _feats:
            WEB_ROUTES[(_m, _path)] = _feats[_m]
        else:
            WEB_NO_EVENT[(_m, _path)] = (_MAIL_NO_EVENT_REASONS.get(_path, {}).get(_m)
                                         or "a mail route called with a method it does not serve (404/405): not a user action")
del _path, _feats, _m


# --- (core): the alert routes (g41). Like the Apps routes, each is registered for every method (a flag-OFF
# 404 must not be a revealing 405), so every (method, route) pair is a feature or no-event, from ONE table.
_ALERT_ROUTE_FEATURES: Dict[str, Dict[str, str]] = {
    "/api/alerts": {},
    "/api/alerts/dismiss-all": {"POST": "alert-dismiss-all"},
    "/api/alerts/{alert_id}/dismiss": {"POST": "alert-dismiss"},
    "/alerts-ui/{name}": {},
}
for _path, _feats in _ALERT_ROUTE_FEATURES.items():
    for _m in _APPS_ALL_METHODS:
        if _m in _feats:
            WEB_ROUTES[(_m, _path)] = _feats[_m]
        elif _path == "/api/alerts" and _m == "GET":
            WEB_NO_EVENT[(_m, _path)] = "polled by the alert column on a timer; no user action"
        elif _path == "/alerts-ui/{name}":
            WEB_NO_EVENT[(_m, _path)] = "the alert column's static script and stylesheet (asset route): not a user action"
        else:
            WEB_NO_EVENT[(_m, _path)] = "an alert route called with a method it does not serve (404/405): not a user action"
del _path, _feats, _m


# DEPLOY-supplied rows (g36): routes that belong to ONE deployment (an Apps-tab plug-in) are not in this table.
# They are read from a JSON file ``{"routes": [{"method": "GET", "path": "/api/x", "feature_id": "some-feature"}]}``
# named by ``GNOMON_FEATURE_ROUTES_FILE`` (default ``~/gnomon-data/feature-routes.json``), resolved at CALL time and
# re-read when its mtime changes (checked at most every ROUTES_RECHECK_S seconds). A missing or malformed file contributes nothing (never an error): such a route
# then emits nothing and its registry node reads registered-not-wired.
ENV_ROUTES_FILE = "GNOMON_FEATURE_ROUTES_FILE"
#: the mtime is re-checked at most once per this many seconds (F-C): the lookup runs on the web event loop for every
#: matched request, so it must not touch the filesystem each time; a changed file is picked up within this window
ROUTES_RECHECK_S = 5.0
#: a routes file larger than this is treated as malformed (it contributes nothing)
ROUTES_MAX_BYTES = 1 << 20
_deploy_cache: Dict[str, Any] = {"key": None, "rows": {}, "checked": None}


def deploy_routes_file() -> str:
    from .config import runtime_root_default

    return os.environ.get(ENV_ROUTES_FILE) or os.path.join(runtime_root_default(), "feature-routes.json")


def _read_routes_file(path: str) -> Dict[Tuple[str, str], str]:
    """Parse the file; a non-regular file (a FIFO would hang a blocking open), an oversized one or a malformed one gives
    ``{}``; one bad ROW is skipped and the good rows survive (F-H)."""
    rows: Dict[Tuple[str, str], str] = {}
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > ROUTES_MAX_BYTES:
                return {}
            with os.fdopen(fd, "r", encoding="utf-8", closefd=False) as fh:
                data = json.load(fh)
        finally:
            os.close(fd)
        routes = data.get("routes") if isinstance(data, dict) else None
        for r in (routes if isinstance(routes, list) else []):
            try:
                m, p, f = r.get("method"), r.get("path"), r.get("feature_id")
                if (isinstance(m, str) and isinstance(p, str) and p.startswith("/")
                        and isinstance(f, str) and _fe.FEATURE_ID_RE.match(f)):
                    rows[(m.upper(), p)] = f
            except Exception:  # noqa: BLE001 - a non-dict or odd row is skipped, the others survive
                continue
    except Exception:  # noqa: BLE001 - a bad file contributes nothing
        return {}
    return rows


def deploy_routes() -> Dict[Tuple[str, str], str]:
    """The DEPLOY rows, validated: feature ids must match the feature-id shape; a bad ROW is skipped and the rest
    survive, a bad FILE contributes nothing. The file's mtime is checked at most once per :data:`ROUTES_RECHECK_S`."""
    path = deploy_routes_file()
    now = time.monotonic()
    ck = _deploy_cache["checked"]
    if ck is not None and ck[0] == path and now - ck[1] < ROUTES_RECHECK_S:
        return _deploy_cache["rows"]
    try:
        key = (path, os.stat(path).st_mtime_ns)
    except OSError:
        _deploy_cache.update(key=None, rows={}, checked=(path, now))
        return {}
    if _deploy_cache["key"] == key:
        _deploy_cache["checked"] = (path, now)
        return _deploy_cache["rows"]
    rows = _read_routes_file(path)
    _deploy_cache.update(key=key, rows=rows, checked=(path, now))
    return rows


def all_web_routes() -> Dict[Tuple[str, str], str]:
    """The package table plus the DEPLOY rows."""
    return {**deploy_routes(), **WEB_ROUTES}


def route_feature_id(method: Any, template: Any) -> Optional[str]:
    """Closed-set lookup (method, route template) -> feature_id; an unlisted route, or a ``no-event`` route,
    returns None. A route table entry never derives from the request body or the concrete URL."""
    try:
        k = (str(method).upper(), template)
        return WEB_ROUTES.get(k) or deploy_routes().get(k)
    except Exception:  # noqa: BLE001
        return None


# --- self-verbs ---------------------------------------------------------------


def self_verb_names() -> FrozenSet[str]:
    """The self-verb set DERIVED from the code (never a hand list): the same derivation as
    ``usage_metrics.known_categories()['verb']``."""
    from .harness import self_mcp

    return frozenset(set(self_mcp.TOOL_NAMES) | {
        self_mcp.LAUNCH_AGENT_TOOL["name"], self_mcp.QUIESCE_TOOL["name"],
        self_mcp.SEARCH_TRANSCRIPT_TOOL["name"], self_mcp.RESTART_SERVICE_TOOL["name"],
        self_mcp.IDLE_SNOOZE_TOOL["name"], self_mcp.PLAN_USAGE_TOOL["name"], self_mcp.ALERT_TOOL["name"],
        self_mcp.SYNC_MY_SKILLS_TOOL["name"]} | set(self_mcp.DSD_SELF_TOOL_NAMES) | set(self_mcp._OWL_NAMES)
        | {t["name"] for t in self_mcp.MAIL_TOOLS})


def _kebab(name: str) -> str:
    return name.replace("_", "-")


def self_verb_feature_id(name: Any) -> Optional[str]:
    """The verb name with underscores as hyphens, only for a name in the derived verb set."""
    try:
        if isinstance(name, str) and name in self_verb_names():
            return _kebab(name)
    except Exception:  # noqa: BLE001
        pass
    return None


# --- the registry-backed mappings ----------------------------------------------------------------------

_registry_cache: Optional[List[Any]] = None
#: a failed load is remembered for this long before the next attempt, so a missing corpus costs ONE engine
#: build per window on the harness's pump thread, not one per operator command
REGISTRY_RETRY_S = 60.0
_registry_failed: Optional[Tuple[float, Exception]] = None


def _registry() -> List[Any]:
    global _registry_cache, _registry_failed
    if _registry_cache is None:
        if _registry_failed is not None and time.monotonic() - _registry_failed[0] < REGISTRY_RETRY_S:
            raise _registry_failed[1]
        from .feature_registry import load_registry

        try:
            _registry_cache = load_registry()
            _registry_failed = None
        except Exception as exc:  # noqa: BLE001 - remembered, re-raised: callers count it
            _registry_failed = (time.monotonic(), exc)
            raise
    return _registry_cache


def warm_registry() -> bool:
    """Load the registry into the process cache NOW (the harness calls this off its pump thread at
    start so the first operator command never pays the engine build). Returns False, never raises."""
    try:
        _registry()
        return True
    except Exception:  # noqa: BLE001
        return False


def _reset_for_tests() -> None:
    global _registry_cache, _registry_failed
    _registry_cache = None
    _registry_failed = None
    _deploy_cache.update(key=None, rows={}, checked=None)


def command_alias_map() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for rn in _registry():
        for w in rn.aliases:
            out[w.lstrip("\\")] = rn.feature_id
    return out


def command_feature_id(word: Any) -> Optional[str]:
    """An operator ``\\command`` word -> its feature id, read from the registry node's ``aliases``
    declaration (never a naming rule). An unregistered word returns None and NO event is written (an
    unregistered typed word never reaches the emitter). If the registry cannot be read the answer is
    None AND the failure is COUNTED (it surfaces in the dropped-events summary), never silent."""
    try:
        if not isinstance(word, str):
            return None
        return command_alias_map().get(word.lstrip("\\"))
    except Exception:  # noqa: BLE001
        _fe._note_drop("registry unreadable", failure=True)
        return None


def dispatch_feature_id(kind: Any, name: Any) -> Optional[str]:
    """A subagent / skill name -> a registered feature id, ONLY if ``name`` is a registered agent-def
    or skill (a registry node whose surface is ``subagent`` / ``skill``); else None. The class is
 INVISIBLE until the dispatch visibility is measured, so no call site
    uses this yet; the closed set is empty until a node is registered."""
    try:
        if kind not in ("subagent", "skill") or not isinstance(name, str):
            return None
        for rn in _registry():
            if kind in rn.surfaces and name in (rn.aliases or ()):
                return rn.feature_id
    except Exception:  # noqa: BLE001
        return None
    return None

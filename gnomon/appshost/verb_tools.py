"""The Apps verbs' DESCRIPTORS only: constants, no imports of the host (g47b/g67).

``gnomon.hub.mcp`` is in every hook's import closure, so it reads these lazily (inside its functions);
this module holds data and nothing else, so even an eager import does no work.
"""

from __future__ import annotations

from typing import Any, Dict, List

FLAG = "apps_tab"

_ID = {"type": "string", "description": "The app's manifest id (e.g. identity-adjudicator)."}

APP_VERB_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "app_list",
        "description": (
            "List every installed Gnomon app with its status (stopped | starting | running | port-conflict | "
            "failed | refused) and scope, plus the PENDING install/uninstall proposals awaiting the operator's confirm. "
            "Returns {apps: [{id, name, status, label, scope}], pending: [{id, kind, name, proposed_by}]}."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "app_status",
        "description": (
            "One installed app's status. 'running' = its health URL answers; 'starting/unhealthy' = process "
            "alive, health not answering; 'refused(<reason>)' names the failed precondition (licence, backup, "
            "requires, requires_files, hardware, manifest-changed, code-changed, non-loopback, path-unset); "
            "'port-conflict' names the PID and command holding its port; 'failed' carries the log tail."
        ),
        "inputSchema": {"type": "object", "properties": {"app_id": _ID}, "required": ["app_id"]},
    },
    {
        "name": "app_start",
        "description": (
            "Start an INSTALLED app through Gnomon (launchd + the preflight that checks every precondition). "
            "Refused with the reason when a precondition fails; a merely PROPOSED app is refused (the operator has not "
            "confirmed it). Returns the resulting status."
        ),
        "inputSchema": {"type": "object", "properties": {"app_id": _ID}, "required": ["app_id"]},
    },
    {
        "name": "app_stop",
        "description": (
            "Stop a running installed app (its declared stop signal, the grace period, then SIGKILL). Returns "
            "{was_running, signal, killed}."
        ),
        "inputSchema": {"type": "object", "properties": {"app_id": _ID}, "required": ["app_id"]},
    },
    {
        "name": "app_open",
        "description": (
            "Open an INSTALLED app's UI in the operator's browser (a new window at http://127.0.0.1:{port}{ui_path}). "
            "Takes the app id ONLY (no URL or path). If the app is not running it is first started through the "
            "same pinned, confirmed start path, and every start refusal applies. A headless app (no ui_path) is "
            "refused. The response never contains the app's ui_token."
        ),
        "inputSchema": {"type": "object", "properties": {"app_id": _ID}, "required": ["app_id"],
                        "additionalProperties": False},
    },
    {
        "name": "app_propose_install",
        "description": (
            "PROPOSE installing an app from its manifest file (an absolute path to the JSON manifest shipped in "
            "the app's own directory). The manifest is validated now; NOTHING is installed and NOTHING runs until "
            "the operator confirms it on the Apps tab (it appears in the Pending section). Agents cannot confirm."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"manifest_path": {"type": "string", "description": "Absolute path of the manifest JSON."}},
            "required": ["manifest_path"],
        },
    },
    {
        "name": "app_propose_uninstall",
        "description": (
            "PROPOSE uninstalling an installed app. It stays installed until the operator confirms on the Apps tab; "
            "then its launchd agent and port are removed and its host state is archived (never deleted)."
        ),
        "inputSchema": {"type": "object", "properties": {"app_id": _ID}, "required": ["app_id"]},
    },
]

APP_VERB_NAMES = frozenset(t["name"] for t in APP_VERB_TOOLS)

#: g60c caller scopes for the seven verbs (see the module doc for why all are "any" in this tree).
CALLER_SCOPES: Dict[str, str] = {name: "any" for name in sorted(APP_VERB_NAMES)}

#: g73-amendment item 7: each legacy alias's target, removal date and owner. [A, builder] The date is the
#: builder's reading of ruling R13's "removed after one release" (no release date is recorded); the architect
#: owns it and may move it. tests/test_appshost_verbs.py FAILS once the date has passed.
ALIASES: Dict[str, Dict[str, str]] = {
    "launch_app": {"alias_of": "app_start", "remove_after": "2026-11-02", "owner": "alpha-architect"},
    "terminate_app": {"alias_of": "app_stop", "remove_after": "2026-11-02", "owner": "alpha-architect"},
    "list_running_apps": {"alias_of": "app_list", "remove_after": "2026-11-02", "owner": "alpha-architect"},
}

#: operator-only operations that are NOT verbs (asserted absent from every tool list by a test)
OPERATOR_ONLY_OPERATIONS = ("confirm", "decline", "run_action", "save_settings", "record_backup")



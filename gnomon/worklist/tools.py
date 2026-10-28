"""The MCP tool definitions of the work verbs (g14: a cold agent can use each correctly on the first try). Constants only: this
module is imported lazily by ``harness/self_mcp.py`` and only when the verbs are advertised."""

from __future__ import annotations

from typing import Any, Dict, List

from . import constants as C

_NOTE = ("YOUR OWN open-work list. SELF-SCOPED: it is always the list of THIS session's agent; there is no instance_id. ")
_WAIT = (f"waiting_on: who the item waits on: 'self', '{C.WAIT_OPERATOR}' (the operator), 'agent:<instance id>' or 'process:<short text>'. ")
_IV = (f"remind_every_min: minutes between reminders, {C.MIN_INTERVAL_MIN} to {C.MAX_INTERVAL_MIN}; leave it out to use the workspace "
       "default (60 unless configured); the string 'none' turns this item's reminders off. Raising it is your 'snooze'. ")
_WAITING_SCHEMA = {"type": "string", "description": C.WAITING_FORMS}
_IV_SCHEMA = {"description": f"Minutes between reminders ({C.MIN_INTERVAL_MIN} to {C.MAX_INTERVAL_MIN}), or 'none'. Omit for the default.",
              "anyOf": [{"type": "integer"}, {"type": "string", "enum": ["none", "default"]}]}
_LINKS = {"type": "object", "description": "Optional: blocked_by (a DSD id) and/or requested_by (who asked you; your own statement).",
          "properties": {"blocked_by": {"type": "string"}, "requested_by": {"type": "string"}}, "additionalProperties": False}
_FLAGS = {
    "standing": {"type": "boolean", "description": "Meant to stay open: reminds for as long as it is open, never pauses."},
    "hold_until_return": {"type": "boolean",
                          "description": f"Silence this item while the operator is declared away AND the item waits on '{C.WAIT_OPERATOR}'; "
                                         "cleared when he declares his return."},
}

WORK_ADD_TOOL: Dict[str, Any] = {
    "name": "work_add",
    "description": (
        "Add an item to your own open-work list, in your own words. Only listed work earns reminders: an agent with an empty "
        "list is left alone. " + _NOTE + _WAIT + _IV +
        "An item with no update after 8 reminders PAUSES (it stays open; any work_update resumes it) unless it is 'standing'. "
        "Returns the item as stored (its id is w-<n>); a refused call says why and changes nothing."),
    "inputSchema": {"type": "object", "properties": {
        "title": {"type": "string", "description": f"What you are working on, up to {C.TITLE_MAX} characters."},
        "waiting_on": _WAITING_SCHEMA, "remind_every_min": _IV_SCHEMA, **_FLAGS, "links": _LINKS,
        "note": {"type": "string", "description": "Optional short note."}},
        "required": ["title", "waiting_on"], "additionalProperties": False}}

WORK_UPDATE_TOOL: Dict[str, Any] = {
    "name": "work_update",
    "description": (
        "Update one or several of your open items (pass id, or ids to change several at once). Any update resumes a paused item "
        "and restarts its reminder clock. Give at least one field (a note is enough). " + _NOTE + _WAIT + _IV +
        "If you set hold_until_return and it cannot take effect yet (no away status declared, or the item does not wait on "
        "the operator), it is stored, the item keeps reminding, and the reply says what is missing."),
    "inputSchema": {"type": "object", "properties": {
        "id": {"type": "string", "description": "One item id, e.g. w-3."},
        "ids": {"type": "array", "items": {"type": "string"}, "description": "Several item ids."},
        "title": {"type": "string"}, "waiting_on": _WAITING_SCHEMA, "remind_every_min": _IV_SCHEMA, **_FLAGS, "links": _LINKS,
        "note": {"type": "string"}}, "additionalProperties": False}}

WORK_DONE_TOOL: Dict[str, Any] = {
    "name": "work_done",
    "description": "Mark one of your items done (it stops reminding; it stays in your log). " + _NOTE,
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "note": {"type": "string"}},
                    "required": ["id"], "additionalProperties": False}}

WORK_DROP_TOOL: Dict[str, Any] = {
    "name": "work_drop",
    "description": "Drop one of your items (you will not do it); a reason is required. It stays in your log. " + _NOTE,
    "inputSchema": {"type": "object", "properties": {"id": {"type": "string"}, "reason": {"type": "string"}},
                    "required": ["id", "reason"], "additionalProperties": False}}

WORK_LIST_TOOL: Dict[str, Any] = {
    "name": "work_list",
    "description": ("List your open items with their state: next reminder, interval (own or default), paused, held, standing. "
                    + _NOTE),
    "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False}}

WORK_TOOLS: List[Dict[str, Any]] = [WORK_ADD_TOOL, WORK_UPDATE_TOOL, WORK_DONE_TOOL, WORK_DROP_TOOL, WORK_LIST_TOOL]

OPERATOR_STATUS_TOOL: Dict[str, Any] = {
    "name": "operator_status_set",
    "description": (
        "Declare the operator's status (away / in-office) at HIS word. Offered only to the sessions the workspace lists as allowed "
        "setters. Quote his OWN words as 'text' and say where they are recorded in 'source' (a graph key or a transcript line): "
        "both are required. Set a state only when his words plainly name it; a sideways remark is not a declaration (ask him one "
        "line, or set nothing). Declaring in-office clears every hold and sends no message to anyone. There is no expiry: an "
        "away status never turns into present by itself. The setter is you; there is no argument for that."),
    "inputSchema": {"type": "object", "properties": {
        "state": {"type": "string", "enum": ["away", "in-office"]},
        "text": {"type": "string", "description": "His own words, quoted."},
        "source": {"type": "string", "description": "Where his word is recorded."}},
        "required": ["state"], "additionalProperties": False}}

WORK_TOOL_NAMES = frozenset(t["name"] for t in WORK_TOOLS)

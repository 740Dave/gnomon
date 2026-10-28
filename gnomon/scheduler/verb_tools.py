"""The six scheduler hub verbs' DESCRIPTORS only: constants, no imports of the engine (g47b/g67).

``gnomon.hub.mcp`` is in every hook's import closure, so it reads these LAZILY (inside its functions); this
module holds data and nothing else. Exactly six verbs (requirement-verbs); none confirms a job, edits
a confirmed definition, writes a receipt or runs a script by path. Stage 1a: ``sched_run_now`` is advertised (the set
stays exactly six) and always refuses, naming stage 1b (it is for script jobs).
"""

from __future__ import annotations

from typing import Any, Dict, List

FLAG = "scheduler"
VERB_PREFIX = "sched_"

_ID = {"type": "string", "description": "The job id (lowercase letters, digits and hyphens, 2 to 41 characters, "
                                         "starting with a letter), e.g. remind-check-inbox."}

SCHED_VERB_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "sched_list",
        "description": (
            "List YOUR scheduled jobs (confirmed jobs with their schedule, next run and last result) and YOUR proposals "
            "still waiting for the operator's confirmation. Read-only. Returns {jobs: [...], pending: [...]}."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "sched_status",
        "description": (
            "One of YOUR jobs (or pending proposals): its definition, schedule, next run times, whether its pin still "
            "matches, and its latest runs. Read-only."
        ),
        "inputSchema": {"type": "object", "properties": {"job_id": _ID}, "required": ["job_id"]},
    },
    {
        "name": "sched_propose",
        "description": (
            "PROPOSE a timed reminder: at the set time the scheduler types ONE short labelled note into YOUR OWN running "
            "session ('[scheduled: <job id> by <you> at <time>] <text>'). Nothing is scheduled and nothing fires until "
            "the operator confirms it once in the Scheduler tab; you cannot confirm. Fields: job_id; title; trigger = exactly "
            "ONE of {\"once_at\": \"2026-10-10T14:00:00Z\"} (a UTC instant) | {\"times\": [\"06:10\"], \"weekdays\"?: "
            "[\"mon\"]} (local time) | {\"every_minutes\": 30} | {\"cron\": \"m h dom mon dow\"}; action = "
            "{\"type\": \"inject\", \"text\": \"...\"} (at most 1,000 characters; control characters are stripped; may "
            "not start with '!'). Limits: 20 active jobs per agent; no job more often than every 5 minutes; a recurring "
            "inject at least every 15 minutes. Refused: a script, an agent launch, boot/login triggers, and any session "
            "but your own. Reusing an existing job_id proposes a CHANGE that needs re-confirmation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": _ID,
                "title": {"type": "string", "description": "A short plain title shown in the Scheduler tab."},
                "trigger": {"type": "object", "description": "Exactly one trigger form (see the tool description)."},
                "action": {"type": "object", "description": "{\"type\": \"inject\", \"text\": \"...\"}"},
            },
            "required": ["job_id", "title", "trigger", "action"],
        },
    },
    {
        "name": "sched_propose_remove",
        "description": (
            "Remove one of YOUR jobs. A job still PENDING is withdrawn at once (nothing was installed). A CONFIRMED job "
            "keeps running until the operator confirms its removal in the Scheduler tab."
        ),
        "inputSchema": {"type": "object", "properties": {"job_id": _ID}, "required": ["job_id"]},
    },
    {
        "name": "sched_run_now",
        "description": (
            "Run one of YOUR script jobs now. Script jobs are stage 1b and are NOT available yet: this always refuses "
            "with that reason. (Timed reminders cannot be run early.)"
        ),
        "inputSchema": {"type": "object", "properties": {"job_id": _ID}, "required": ["job_id"]},
    },
    {
        "name": "sched_log",
        "description": (
            "The run history of YOUR jobs, newest first: when each fired or was skipped, missed or refused, and whether "
            "the target session's log showed the delivery (SENT is not DELIVERED). Optional job_id and limit (default "
            "50). Read-only; text is redacted."
        ),
        "inputSchema": {"type": "object", "properties": {"job_id": _ID, "limit": {"type": "integer"}}},
    },
]

SCHED_VERB_NAMES = frozenset(t["name"] for t in SCHED_VERB_TOOLS)

#: operator-only operations that are NOT verbs (asserted absent from every tool list by a test)
OPERATOR_ONLY_OPERATIONS = ("confirm", "decline", "cancel", "pause", "enable")

"""Every bound and name of the open-work list, in ONE place (operator change 2026-10-11: max interval 2880 min)."""

FLAG_NAME = "open_work_list"

#: reminder interval bounds, minutes (rule-interval-floor; rule-interval-bounds, ceiling 2880 = 48 h).
MIN_INTERVAL_MIN = 15
MAX_INTERVAL_MIN = 2880
#: the workspace default when nothing is configured (rule-default-starts-60); bounded like any interval.
DEFAULT_INTERVAL_MIN = 60
#: an unmarked item with this many reminders and no update PAUSES (rule-pause-after-eight).
PAUSE_AFTER = 8
MAX_OPEN_ITEMS = 50
TITLE_MAX = 200
PROCESS_TEXT_MAX = 80
NOTE_MAX = 500
#: the presence line is ONE line and never longer than this (round 3, F1).
PRESENCE_LINE_MAX = 400
STATUS_TEXT_MAX = 200
STATUS_SOURCE_MAX = 200
#: the shape of a DSD id, the SAME pattern the DSD code defines (gnomon/dsd/notice.py NOTICE_DSD_ID_RE; a test keeps them equal).
#: blocked_by must match it; the Work tab never builds a selector, URL or HTML from anything else.
DSD_ID_PATTERN = r"\Adsd-[0-9a-f]{6,32}\Z"
#: boot digest: a warning when MORE than this many items are open (eight warns, seven does not).
DIGEST_WARN_OVER = 7
DIGEST_CH_ROWS = 5
#: an away status older than this is shown "not renewed" (rule-away-age-shown).
AWAY_NOT_RENEWED_SECONDS = 24 * 3600
#: the agent must have produced no output for this long before a reminder is typed (rule-only-when-quiet).
QUIET_SECONDS = 20.0
#: the ticker reads the log at most this often.
TICK_SECONDS = 5.0
#: after a reservation that was NOT delivered (the gate DENIED it, a write refusal) the next try waits this long; each further denial in a row
#: doubles the wait (60, 120, 240 ...) up to the item's own reminder interval. A delivered reminder or any update to the item resets it.
VOID_RETRY_SECONDS = 60.0
#: this many denials in a row for one item write ONE fault record, and the Work tab shows the fault on the item.
DENY_FAULT_AFTER = 8
#: the Work tab warns when an agent's log passes this (log-growth).
LOG_WARN_BYTES = 5 * 1024 * 1024

SCHEMA_VERSION = 1
READER_FLOOR = 1
DIRNAME = "open-work"

#: deploy-data tables (deploy_data.TABLE_CLASSES declares their failure class).
TABLE_DEFAULTS = "open-work"            # {"default_interval_minutes": 60, "waiting_on_aliases": {"<word>": "operator"}}   degrade-visibly
TABLE_SETTERS = "status-setters"        # {"setters": {"<instance id>": "<display name>"}}   fail-closed

STATES = ("open", "done", "dropped")
#: The stored value of waiting_on that means "waiting on the operator". NEUTRAL: a deployment may map its own words to it with
#: ``waiting_on_aliases`` in deploy / workspace data (normalised at write); no role word is spelled in product code.
WAIT_OPERATOR = "operator"
#: how the operator is named in reminder text, the digest and the reads when the caller passes no name; the real name is the
#: deployment's operator name (config ``user_name`` / the operator's login), the same one the nudge texts use.
DEFAULT_OPERATOR_LABEL = "the operator"
WAITING_FORMS = f"self | {WAIT_OPERATOR} | agent:<instance id> | process:<text up to 80 characters>"
#: the key under which a WORKSPACE overrides these settings (``workspaces/<ws>/workspace.json``, a top-level key, not ``settings``).
WORKSPACE_KEY = "open-work"

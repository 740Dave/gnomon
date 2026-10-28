"""The scheduler's closed vocabularies (requirement-results and the stage 1 additions;
checklist proposal result-strings: a new string is added here AND to the Run Log filter in the same change)."""

from __future__ import annotations

FLAG = "scheduler"
SCHEMA_VERSION = 1

# result vocabulary (spec 14, requirement-results)
OK, OK_UNVERIFIED, FAIL, MISSED, SKIPPED, WAITING, NEVER_RUN = (
    "OK", "OK-UNVERIFIED", "FAIL", "MISSED", "SKIPPED", "WAITING", "NEVER-RUN")
RESULTS = (OK, OK_UNVERIFIED, FAIL, MISSED, SKIPPED, WAITING, NEVER_RUN)

# receipt outcomes for an inject (requirement-receipt-outcomes) plus ERROR
SENT, DELIVERED, HELD, DELIVERED_QUEUED, ERROR = "SENT", "DELIVERED", "HELD", "DELIVERED-QUEUED", "ERROR"
OUTCOMES = (SENT, DELIVERED, HELD, DELIVERED_QUEUED, ERROR)

# skip / miss / refusal reasons
TARGET_NOT_LIVE = "target-not-live"
TARGET_COMPACTING = "target-compacting"
TARGET_LOG_UNREADABLE = "target-log-unreadable"
PLAN_USAGE_HIGH = "plan-usage-high"
PLAN_USAGE_UNKNOWN = "plan-usage-unknown"
ONE_SHOT_TOO_LATE = "one-shot-too-late"
MISSED_SLOT = "missed-slot"
JOB_CHANGED = "job-changed"
SKIP_REASONS = (TARGET_NOT_LIVE, TARGET_COMPACTING, TARGET_LOG_UNREADABLE, PLAN_USAGE_HIGH, PLAN_USAGE_UNKNOWN)
MISS_REASONS = (ONE_SHOT_TOO_LATE, MISSED_SLOT)

# triggers recorded in a receipt
T_SCHEDULED, T_ONE_SHOT, T_ONE_SHOT_LATE = "scheduled", "one-shot", "one-shot-late"
TRIGGERS = (T_SCHEDULED, T_ONE_SHOT, T_ONE_SHOT_LATE)

# the Run Log filter vocabulary is derived from these, never typed a second time
RUN_LOG_RESULT_FILTER = RESULTS
RUN_LOG_OUTCOME_FILTER = OUTCOMES

# the frame trigger prefix (also the g77(2) dismissal-clear trigger prefix, see the build report)
FRAME_TRIGGER_PREFIX = "scheduler:"

# stage refusals (the named reasons the spec requires)
STAGE_1B = "script jobs are stage 1b and are not available yet"
STAGE_2_LAUNCH = "scheduled agent launch is stage 2 and needs the operator's approval"
STAGE_2_TRIGGER = "boot, login and on-gnomon-start triggers are not part of stage 1"
OTHER_SESSION = "an inject to another agent's session needs a confirmation rule that is not part of stage 1"

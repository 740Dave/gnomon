"""The stage 1 limits in ONE table, read by the validator, the engine and the tab; pinned by a test
(requirement-one-table; checklist proposal limits-table). Constants only."""

from __future__ import annotations

from typing import Dict

LIMITS: Dict[str, int] = {
    #
    "max_active_jobs_per_owner": 20,
    "min_fire_interval_s": 300,        # one fire per job per 5 minutes
    "min_inject_interval_s": 900,      # a recurring inject at least every 15 minutes
    # requirement-text
    "text_max_chars": 1000,
    "title_max_chars": 120,
    # default /
    "tick_s": 30,
    "on_time_s": 60,                   # an on-time fire is within 60 s of its instant
    "miss_grace_s": 300,               # default: 5 minutes past the slot
    "one_shot_late_window_s": 3600,    # requirement-inject-late-window
    # requirement-compaction-ordering
    "compaction_wait_max_s": 1800,     # the most an inject waits on a compaction (owner ruling 2026-10-09)
    "compaction_stale_s": 1800,        # a compact event older than this never holds an inject
    "compaction_recheck_s": 60,
    # (the seam only, see plan_usage.py)
    "plan_usage_retry_s": 3600,
    # delivery evidence polling (requirement-receipt-outcomes)
    "sent_poll_horizon_s": 7200,       # a SENT run with no log evidence is closed as SENT after this
    "held_poll_horizon_s": 7 * 86400,
    "max_proposals_per_owner": 40,
    # the web job's loop waits this long after a start before its first tick, so harnesses can re-register with a
    # restarted hub first (otherwise a due reminder would be skipped as target-not-live, R9)
    "start_grace_s": 90,
}

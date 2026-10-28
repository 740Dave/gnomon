"""The plan-usage seam (requirement-plan-usage).

RULING (owner, 2026-10-09, delta audit N1): spec 14's account-level plan-usage reader
(requirement-account-aggregator) is NOT built, so applying the bands to injects would make every
confirmed inject wait forever. In sub-stage 1a injects are EXEMPT from the bands, like scripts, until the reader
exists. KNOWN LIMIT, recorded. This function is the ONE seam: it returns ``"run"`` today. When the reader lands it
returns ``"run"`` (below 85%), ``"wait"`` (85% to 95% inclusive, or unreadable/unknown, flagged "usage unknown"),
or ``"skip"`` (above 95%), and the engine already acts on all four answers. The engine consults it through the
module attribute at every send decision, and a test proves that.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

RUN, WAIT, SKIP, UNKNOWN = "run", "wait", "skip", "unknown"
BANDS = (RUN, WAIT, SKIP, UNKNOWN)


def plan_usage_band(job: Optional[Dict[str, Any]] = None) -> str:
    """Today: always ``"run"`` (see the module doc). Spec node: requirement-bands. ``job`` is the
    job about to be sent (the spec's seam takes the job, so the reader can apply per-kind rules); unused today."""
    return RUN

"""Skills as a GENERATED PROJECTION of the PROC graph (ported from the original services package
its ``skills`` package, branch sync-skills-from-procs 38948fc, spec in the legacy proposals archive,
2026-09-26).
The design keeps skills in step with the procedures they come from instead of letting them go stale.

The durable substance of a skill lives in a ``node-kind: skill-projection`` node (SSOT);
the ``~/.claude/skills/<name>/SKILL.md`` file is *derived* from it, so the pointer cannot
drift. This package is a plain library (testable WITHOUT a graph) that the CLI
(``python -m gnomon.skills``) and the ``sync_my_skills`` self-verb both call. It READS the
PROC graph in-process through Gnomon's own lts access and WRITES ``~/.claude/skills/``.

The four constraints (operator-agreed 2026-09-26):
  1. Opt-in, never inference  — project only ``node-kind: skill-projection`` nodes.
  2. Reconcile, not mint; own only what you stamp — never touch a file lacking a
     ``generated-from`` stamp.
  3. Dry-run + fail-closed — default is a diff; a malformed contract refuses THAT skill.
  4. Top rung of a provisioning ladder — assumes corpora/lts-engine/the services package are up.
"""

from .contract import (
    ContractError,
    SkillProjection,
    parse_projection,
)
from .render import (
    STAMP_GENERATED_FROM,
    STAMP_SOURCE_HASH,
    compute_source_hash,
    parse_stamps,
    render_skill,
)
from .reconcile import Action, PlanEntry, classify
from .sync import SyncResult, apply_plan, build_plan, sync_skills

__all__ = [
    "ContractError",
    "SkillProjection",
    "parse_projection",
    "STAMP_GENERATED_FROM",
    "STAMP_SOURCE_HASH",
    "compute_source_hash",
    "parse_stamps",
    "render_skill",
    "Action",
    "PlanEntry",
    "classify",
    "SyncResult",
    "sync_skills",
    "build_plan",
    "apply_plan",
]

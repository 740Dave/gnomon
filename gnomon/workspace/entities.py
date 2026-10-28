"""Entity dispositions (the converter's per-ENT rule) and the ENT-TOPIC shape.

``ENTITY_DISPOSITION`` maps every ``-*`` node of the spec to how S1's converter treats it.
It is an EXTENSION POINT with a guard: tests/test_x13s1_entity_dispositions.py reads the spec's ENT ids
(a g25 reader with a floor) and fails when an ENT node has no disposition here, or this table names an
ENT that no longer exists. A new entity added to the model therefore cannot be forgotten by the
converter.

Disposition values:
  convert   : the converter produces tagged/unassigned/held/skipped records of this kind (a per-kind rule)
  seed      : created by the scaffold from the seeds, not converted from existing state
  inventory : S1 only counts and classifies the existing store (no rule needed until S3 / its slice)
  none-yet  : no store exists today (feature unbuilt); count is 0 and the dry run says why
  derived   : a read model computed from other records; nothing to convert
  not-an-entity / as-today : explicit non-conversions
"""

from __future__ import annotations

import re
from typing import Dict, List

ENTITY_DISPOSITION: Dict[str, dict] = {
    "WORKSPACE": {"how": "seed", "kind": "workspace", "note": "the deployment's workspaces, from seeds.json; slug stable forever"},
    "PROJECT": {"how": "seed", "kind": "project", "note": "portfolio seed + explicit seeds + reserved <ws>/unassigned"},
    "BACKLOG": {"how": "seed", "kind": "workspace.json backlogs[]", "note": "the deployment's research and roadmap backlogs, from seeds.json; operator-gate-value per ENT-BACKLOG"},
    "ITEM": {"how": "convert", "kind": "roadmap-item", "note": "overrides, else <ws>/unassigned; project required"},
    "TOPIC": {"how": "none-yet", "kind": "topic", "note": "X21 topics are unbuilt; verify validates any TOP-*.json found with topic_shape_errors"},
    "ROLE-DEFINITION": {"how": "inventory", "kind": "role", "note": "S1 reports def presence and symlink status; the S3 cutover writes workspaces/<home>/roles/<id>.md"},
    "ROLE-DATA": {"how": "convert", "kind": "role", "note": "an intelligences/<id>/ dir; home workspace from role-aliases.json"},
    "AGENT-LAUNCH-CONFIG": {"how": "convert", "kind": "launch-config", "note": "profile.json -> launch-config.json rename PLAN (S3 applies)"},
    "CONVERSATION": {"how": "convert", "kind": "conversation", "note": "key rules: session_id, else resume_session_id, else legacy-<dir-id>"},
    "SESSION-RUN": {"how": "convert", "kind": "session-run", "note": "launch workspace = the role's home workspace; foreign/no-launch.json skipped"},
    "HANDOVER": {"how": "convert", "kind": "handover", "note": "legacy handovers -> type normal, keyed legacy-* (conversation not derivable)"},
    "DETACHED-PROCESS": {"how": "as-today", "kind": "-", "note": "stored inside its handover; not separately enumerated"},
    "CORPUS": {"how": "convert", "kind": "corpus", "note": "explicit ordered corpus-map; proj.* HELD"},
    "SHIPPED-ASSETS": {"how": "as-today", "kind": "-", "note": "installed with the package; not converted"},
    "DSD-RECORD": {"how": "convert", "kind": "dsd-record", "note": "machine-owned per role; withdrawn/superseded skipped"},
    "DSD-DECISION": {"how": "convert", "kind": "dsd-decision", "note": "overrides/dsd.json, else the authoring role's home workspace + <ws>/unassigned"},
    "SCHEDULED-JOB": {"how": "none-yet", "kind": "scheduled-job", "note": "X23 Scheduler unbuilt; no store"},
    "JOB-RUN": {"how": "none-yet", "kind": "job-run", "note": "X23 Scheduler unbuilt; no store"},
    "BOOTSTRAP-JOB": {"how": "inventory", "kind": "launchagent", "note": "LaunchAgents recorded; the web agent NOT renamed (resolved-question-532)"},
    "ACCOUNT-USAGE": {"how": "as-today", "kind": "-", "note": "fed by the statusline; not converted"},
    "ALERT-EVENT": {"how": "inventory", "kind": "notice-dir", "note": "notices/<role>/ map to the event log at S3"},
    "TIMELINE-ENTRY": {"how": "none-yet", "kind": "timeline-entry", "note": "X25 unbuilt; no timeline corpus"},
    "ATTENTION-STATE": {"how": "derived", "kind": "-", "note": "derived read model; ignores workspace config (I6)"},
    "VIEWER": {"how": "seed", "kind": "viewer", "note": "DATA viewers/ch.json with operator-id; substrate is a builder choice (spec names none)"},
    "SETTINGS": {"how": "convert", "kind": "settings-key", "note": "config.json keys -> machine.json settings / flags; registry ships with the package"},
    "PREFIX-REGISTRY": {"how": "seed", "kind": "prefix-registry", "note": "built from the corpora; legacy-shared grandfathered"},
    "SNAPSHOTS-HUB-BREAKGLASS": {"how": "as-today", "kind": "store-inventory", "note": "machine-owned as today"},
    "NON-HARNESSED-SESSION": {"how": "not-an-entity", "kind": "-", "note": "a broadcast target only"},
}

_ENT_ID_RE = re.compile(r"^##\s.*\[GM-WSM-ENT-([A-Z0-9-]+)\]\s*$")


def ent_ids_in_spec(text: str) -> List[str]:
    """The ENT ids of the spec (MACHINE-READ under g25: reads ``##... [-<ID>]`` headings)."""
    return [m.group(1) for line in text.splitlines() for m in [_ENT_ID_RE.match(line)] if m]


# F-R2-3 / P3: no trailing-newline allowance (``\Z`` + ``fullmatch``) and ASCII digits only (``[0-9]``, not ``\d``).
_TOP_RE = re.compile(r"TOP-[0-9]{3,}\Z", re.ASCII)
_QUAL_RE = re.compile(r"[a-z][a-z0-9-]*/[a-z][a-z0-9-]*\Z", re.ASCII)


def topic_shape_errors(obj: dict, *, ws_dir: str, workspaces: Dict[str, List[str]]) -> List[str]:
    """Shape errors of a topic record at DATA ``workspaces/<ws_dir>/topics/TOP-nnn.json`` (ENT-TOPIC).

    ``workspaces`` maps each workspace id to its project slugs (projects.json). The rules: id ``TOP-nnn``;
    exactly ONE project, stored qualified ``<ws>/<slug>`` (I4); the workspace is DERIVED from the project
    and NEVER stored (I9), and the file must live under that workspace's dir; state active|archived; an
    archived topic carries ``archived-at`` (the 90-day purge clock)."""
    errs: List[str] = []
    if not isinstance(obj, dict):
        return ["topic is not an object"]
    if not _TOP_RE.fullmatch(str(obj.get("id", ""))):
        errs.append(f"id {obj.get('id')!r} is not TOP-nnn")
    proj = obj.get("project")
    if isinstance(proj, list) or "projects" in obj:
        errs.append("a topic has exactly ONE project (topics-rulings D2), not a list")
    elif not isinstance(proj, str) or not _QUAL_RE.fullmatch(proj):
        errs.append(f"project {proj!r} is not a qualified <ws>/<slug>")
    else:
        ws, slug = proj.split("/", 1)
        if ws not in workspaces:
            errs.append(f"project {proj!r}: workspace {ws!r} is not a known workspace")
        elif slug not in workspaces[ws]:
            errs.append(f"project {proj!r} is not in {ws}'s projects.json")
        if ws != ws_dir:
            errs.append(f"topic lives under workspace {ws_dir!r} but its project belongs to {ws!r} (I9)")
    if "workspace" in obj:
        errs.append("a topic must NOT store a workspace: it is derived from its project (I9 stored-workspace-rule)")
    state = obj.get("state")
    if state not in ("active", "archived"):
        errs.append(f"state {state!r} must be active or archived")
    if state == "archived" and not obj.get("archived-at"):
        errs.append("an archived topic must carry archived-at (the 90-day purge clock)")
    return errs

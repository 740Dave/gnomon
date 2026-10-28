"""The I9 STAMPING LIBRARY (``precedence``, ``scope-session``, ``snapshot-rule``, ``stored-workspace-rule``).

I9: if a record carries a project, its workspace IS that project's workspace, DERIVED (never stored separately, except
where the stored-workspace-rule permits). Scope is stamped at WRITE time as a snapshot, per record kind, by tiers:

* tier 0: an explicit project named by the writer (must be active and pass the kind's own checks; items: in the
  backlog's workspace, not the reserved project);
* tier 1: the SUBJECT's project, when the subjects carry exactly one project (one subject, or several that all carry the
  SAME project: architect ruling 1). Subjects that DIFFER: spanning
  several projects in ONE workspace stamp ``<ws>/unassigned``; subjects spanning WORKSPACES are refused (split the
  record, or name a tier-0 project);
* tier 2: the SCOPE SESSION's ``current-project`` (a scope source for records the session writes; ``scope-session`` is
  defined per kind). An archived current-project is not used: it logs an event and falls to tier 3;
* tier 3: no project, workspace = the scope session's launch workspace (STORED: stored-workspace-rule (a)). Legal only
  for kinds whose project rule is 0..1; for required-project kinds (items, topics) reaching it is a REFUSAL naming the
  missing project.

I2: evidence and history kinds (DSD decisions, Built/Live stamps, timeline entries) may name an ARCHIVED project; new
assignments (an item, a topic, a re-file, a current-project) may not.

This module is PURE (no IO, no flag needed): it decides a stamp and returns it with the events the caller should log.
Persistence is the writers' job (:mod:`.writers`), behind ``workspace_scope_core``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .model import Backlog, Model
from .scopebase import (RESERVED_PROJECT, ScopeRefusal, atomic_write_json, read_json, require_enabled, split_qualified)


@dataclass(frozen=True)
class KindSpec:
    name: str
    #: ``required`` (1, items/topics), ``optional`` (0..1)
    project_rule: str
    #: evidence/history: may name an archived project (I2)
    evidence: bool
    #: tier 1 (subject) applies
    subject: bool


KINDS: Dict[str, KindSpec] = {
    "item": KindSpec("item", "required", False, False),
    "topic": KindSpec("topic", "required", False, False),
    "dsd-decision": KindSpec("dsd-decision", "optional", True, True),
    "built-live-stamp": KindSpec("built-live-stamp", "optional", True, True),
}


@dataclass(frozen=True)
class ScopeSession:
    """The session a record's scope falls back to (tiers 2 and 3). ``role_workspaces`` = the workspaces the role may
    work in (its home plus any others it is listed in); ``current_project`` is a qualified ``<ws>/<slug>`` or None."""

    launch_workspace: str
    current_project: Optional[str] = None
    role_workspaces: Tuple[str, ...] = ()
    #: change log of current-project: ``[{"ts", "project", "by"}]`` (oldest first)
    history: Tuple[Mapping[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {"schema": 1, "launch-workspace": self.launch_workspace, "current-project": self.current_project,
                "role-workspaces": list(self.role_workspaces), "current-project-history": [dict(h) for h in self.history]}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ScopeSession":
        if not isinstance(d, dict) or d.get("schema") != 1 or not isinstance(d.get("launch-workspace"), str):
            raise ScopeRefusal("scope-state-malformed", "scope-state.json is not a schema-1 session scope")
        return cls(d["launch-workspace"], d.get("current-project"), tuple(d.get("role-workspaces") or ()),
                   tuple(d.get("current-project-history") or ()))


@dataclass(frozen=True)
class Subject:
    """A thing a record is ABOUT (a DSD decision's ``subject-items[]``; a Built/Live stamp's item) and the project(s)
    it carries. ``projects`` are qualified."""

    id: str
    projects: Tuple[str, ...] = ()


@dataclass(frozen=True)
class Stamp:
    project: Optional[str]
    #: the record's workspace: DERIVED from ``project`` (I9), or the launch workspace at tier 3
    workspace: str
    tier: int
    #: set ONLY at tier 3 (stored-workspace-rule (a)): the workspace the record stores because it has no project
    stored_workspace: Optional[str] = None
    subject_ids: Tuple[str, ...] = ()
    events: Tuple[Dict[str, Any], ...] = ()

    def record_fields(self) -> Dict[str, Any]:
        """The scope properties a writer stamps on the record: ``project`` if any, ``workspace`` only at tier 3."""
        out: Dict[str, Any] = {}
        if self.project is not None:
            out["project"] = self.project
        if self.stored_workspace is not None:
            out["workspace"] = self.stored_workspace
        if self.subject_ids:
            out["subject-items"] = list(self.subject_ids)
        return out


def _refuse(code: str, message: str, prompt: str = "", events: Sequence[Dict[str, Any]] = ()) -> ScopeRefusal:
    err = ScopeRefusal(code, message, prompt)
    err.events = list(events)
    return err


def _check_new_assignment(model: Model, project: str, spec: KindSpec, backlog: Optional[Backlog], *, events=()) -> None:
    """The kind's own checks for a NEW assignment of ``project`` (tier 0 / tier 2)."""
    p = model.project(project)
    if not spec.evidence and not p.active:
        raise _refuse("project-archived", f"project {project!r} is archived: no new assignment (I2)",
                      "re-pick: choose an active project", events)
    if spec.name == "item":
        if backlog is None:
            raise _refuse("backlog-required", "an item is stamped against its backlog", "", events)
        if p.workspace != backlog.workspace:
            raise _refuse("project-not-in-backlog-workspace",
                          f"project {project!r} is not in backlog {backlog.ref}'s workspace {backlog.workspace!r}",
                          f"pick a project in workspace {backlog.workspace!r}", events)
        if p.slug == RESERVED_PROJECT:
            raise _refuse("reserved-project", f"{project!r} is reserved: only the converter writes into it, the typed "
                          "writer refuses new items in it", "pick a real project", events)


def _tier1(model: Model, spec: KindSpec, subjects: Sequence[Subject]) -> Optional[Stamp]:
    carrying = [s for s in subjects if s.projects]
    if not carrying:
        return None
    projects = sorted({p for s in carrying for p in s.projects})
    for p in projects:
        model.project(p)                      # unknown or malformed -> refuse (an archived one is FINE: evidence)
    ids = tuple(s.id for s in subjects)
    if len(projects) == 1:
        # architect ruling (1), 2026-09-30: several subjects that all carry the SAME project stamp tier 1; only subjects
        # that DIFFER are handled below (same workspace: <ws>/unassigned; across workspaces: refused)
        return Stamp(projects[0], model.workspace_of_project(projects[0]), 1, None, ids)
    wss = sorted({model.workspace_of_project(p) for p in projects})
    if len(wss) > 1:
        raise _refuse("subjects-span-workspaces", f"subjects span workspaces {wss}: split the record, or name a project (tier 0)",
                      "split the decision per workspace, or give an explicit project")
    un = f"{wss[0]}/{RESERVED_PROJECT}"
    return Stamp(un, wss[0], 1, None, ids)


def stamp(model: Model, kind: str, *, explicit: Optional[str] = None, subjects: Sequence[Subject] = (),
          session: Optional[ScopeSession] = None, backlog: Optional[Backlog] = None) -> Stamp:
    """Stamp a record of ``kind`` by I9 ``precedence``. Raises :class:`ScopeRefusal` (a refusal may carry ``.events``)."""
    spec = KINDS.get(kind)
    if spec is None:
        raise ScopeRefusal("kind-unknown", f"no stamping rule for record kind {kind!r}")
    events: List[Dict[str, Any]] = []

    # tier 0: the writer named a project
    if explicit is not None:
        split_qualified(explicit)
        _check_new_assignment(model, explicit, spec, backlog)
        return Stamp(explicit, model.workspace_of_project(explicit), 0, None, tuple(s.id for s in subjects))

    # tier 1: the subject
    if spec.subject and subjects:
        st = _tier1(model, spec, subjects)
        if st is not None:
            return st

    # tier 2: the scope session's current-project
    if session is None:
        raise _refuse("no-scope-session", f"a {kind} needs a scope session (tiers 2 and 3) and none was given")
    cp = session.current_project
    if cp is not None:
        p = model.project(cp)
        if not p.active:
            events.append({"kind": "current-project-archived", "severity": "warn",
                           "message": f"current-project {cp!r} is archived; a {kind} write fell past tier 2",
                           "detail": {"project": cp, "record-kind": kind}, "project_tag": cp})
        else:
            try:
                _check_new_assignment(model, cp, spec, backlog, events=events)
            except ScopeRefusal as exc:
                exc.events = list(events)
                raise
            return Stamp(cp, p.workspace, 2, None, tuple(s.id for s in subjects), tuple(events))

    # tier 3: no project
    if spec.project_rule == "required":
        raise _refuse("project-required", f"a {kind} needs a project and none resolved (tiers 0-2 gave none): tier 3 is "
                      "illegal for required-project kinds", "name a project", events)
    return Stamp(None, session.launch_workspace, 3, session.launch_workspace, tuple(s.id for s in subjects), tuple(events))


# --- timeline entries: the scope session is the REVIEWED session-run -----------------------------------------------

@dataclass(frozen=True)
class TimelineScope:
    #: the workspace component of the entry KEY (stored-workspace-rule (b): a denormalised key component the writer
    #: computes from the projects; a test asserts equality)
    workspace: str
    projects: Tuple[str, ...]
    tier: int


def _ts(v: str) -> datetime:
    return datetime.fromisoformat(v.replace("Z", "+00:00"))


def stamp_timeline(model: Model, *, launch_workspace: str, change_events: Sequence[Mapping[str, Any]],
                   segment_start: str, segment_end: str) -> List[TimelineScope]:
    """Scope of a timeline segment (ENT-TIMELINE-ENTRY): the projects ACTIVE during the segment are read from the
    REVIEWED session-run's current-project change events (not the reviewer's), split PER WORKSPACE (a segment whose
    projects span workspaces yields one entry per workspace, each carrying only that workspace's projects). With no
    project in force the entry is tier 3: workspace = the reviewed run's launch workspace, projects empty. A project
    that is archived now may still appear (evidence: I2)."""
    start, end = _ts(segment_start), _ts(segment_end)
    if end < start:
        raise ScopeRefusal("segment-inverted", "segment end precedes its start")
    evs = sorted(((_ts(e["ts"]), e.get("project")) for e in change_events), key=lambda x: x[0])
    in_force: Optional[str] = None
    active: List[str] = []
    for ts, proj in evs:
        if ts <= start:
            in_force = proj
        elif ts < end:
            if proj is not None:
                active.append(proj)
    if in_force is not None:
        active.insert(0, in_force)
    projects = sorted(set(active))
    if not projects:
        return [TimelineScope(launch_workspace, (), 3)]
    by_ws: Dict[str, List[str]] = {}
    for p in projects:
        by_ws.setdefault(model.workspace_of_project(p), []).append(p)
    return [TimelineScope(ws, tuple(ps), 2) for ws, ps in sorted(by_ws.items())]


# --- session scope state (DATA sessions/<dir-id>/.claude/scope-state.json) ----------------------------------------

SCOPE_STATE_FILE = "scope-state.json"


def scope_state_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", SCOPE_STATE_FILE)


def load_session_scope(session_dir: str) -> ScopeSession:
    try:
        return ScopeSession.from_dict(read_json(scope_state_path(session_dir)))
    except (OSError, ValueError) as exc:
        raise ScopeRefusal("scope-state-unreadable", f"{scope_state_path(session_dir)!r}: {exc}") from exc


def save_session_scope(session_dir: str, scope: ScopeSession, *, enabled: Optional[bool] = None, config: Any = None) -> str:
    require_enabled(enabled, config, "save_session_scope")
    path = scope_state_path(session_dir)
    atomic_write_json(path, scope.to_dict(), enabled=enabled, config=config)
    return path


def initial_current_project(model: Model, *, previous_value: Optional[str], default_project: Optional[str],
                            allowed_workspaces: Sequence[str]) -> Optional[str]:
    """ENT-SESSION-RUN ``current-project-initial``: at launch, current-project = the same conversation's previous
    session-run's last value if it is still ACTIVE and in an allowed workspace; else the launch-config
    ``default-project`` (same two checks); else none."""
    for cand in (previous_value, default_project):
        if not cand:
            continue
        try:
            p = model.project(cand)
        except ScopeRefusal:
            continue
        if p.active and p.workspace in allowed_workspaces:
            return cand
    return None

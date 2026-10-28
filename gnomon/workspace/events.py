"""The machine EVENT LOG: an append-only JSON-lines file, machine-owned.

This is a MACHINE SAFETY PATH (I6): it never imports the workspace model loader and never opens a path under a
``workspaces/`` tree. Its location is the machine setting ``event_log_path`` (registry row; read by the caller through
the settings resolver and passed in as ``path``): this module is handed a path, it never looks anything up.

Append-only: there is no edit and no delete. A re-tag (I3: a job's or alert's workspace tag may be re-tagged) is a
NEW ``retag`` event referring to the original; :func:`effective_tags` folds them. Workspace/project tags on an event
are an opaque write-time ``<ws>/<slug>`` string, EXEMPT from I9 derivation and never re-derived (ENT-ALERT-EVENT
tag-rule); an alert may name an ARCHIVED project (evidence/history: I2, R4).

Persisting is gated by ``workspace_scope_core``: with it OFF :func:`append_event` raises
:class:`~.scopebase.ScopeCoreDisabled` and writes nothing.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Iterator, List, Optional

from .scopebase import QUALIFIED_RE, SLUG_RE, ScopeRefusal, now_iso, require_enabled


def _check_tags(workspace_tag: Optional[str], project_tag: Optional[str]) -> None:
    if workspace_tag is not None and not SLUG_RE.fullmatch(str(workspace_tag)):
        raise ScopeRefusal("tag-malformed", f"workspace tag {workspace_tag!r} is not a workspace slug")
    if project_tag is not None and not QUALIFIED_RE.fullmatch(str(project_tag)):
        raise ScopeRefusal("tag-malformed", f"project tag {project_tag!r} is not a qualified <ws>/<slug> (I4)")


def append_event(path: str, kind: str, *, severity: str = "info", message: str = "", detail: Optional[Dict[str, Any]] = None,
                 targets: Optional[List[str]] = None, workspace_tag: Optional[str] = None, project_tag: Optional[str] = None,
                 ref: Optional[str] = None, clock: Optional[Callable[[], datetime]] = None, enabled: Optional[bool] = None,
                 config: Any = None) -> Dict[str, Any]:
    """Append one event line and fsync it. Returns the record written (``id``, ``ts``, ...).

    ``targets`` are enumerated at send time (ENT-ALERT-EVENT); per-target delivery receipts are appended by the
    delivery code as further events (``kind='receipt'``, ``ref`` = this id). ``ref`` links a follow-up event
    (a receipt or a re-tag) to an earlier one."""
    require_enabled(enabled, config, "append_event")
    _check_tags(workspace_tag, project_tag)
    rec: Dict[str, Any] = {"id": "EV-" + uuid.uuid4().hex[:12], "ts": now_iso(clock), "kind": kind, "severity": severity,
                           "message": message}
    if detail:
        rec["detail"] = detail
    if targets is not None:
        rec["targets"] = list(targets)
    if workspace_tag is not None:
        rec["workspace-tag"] = workspace_tag
    if project_tag is not None:
        rec["project-tag"] = project_tag
    if ref is not None:
        rec["ref"] = ref
    line = json.dumps(rec, sort_keys=True, ensure_ascii=False) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return rec


def read_events(path: str) -> Iterator[Dict[str, Any]]:
    """Every well-formed event line, oldest first. A missing file yields nothing; a torn or malformed line is
    skipped LOUDLY by being yielded as ``{'kind': 'corrupt-line', 'line': n}`` (never silently dropped)."""
    if not os.path.isfile(path):
        return
    with open(path, "r", encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict):
                    raise ValueError("not an object")
            except ValueError:
                yield {"kind": "corrupt-line", "line": n}
                continue
            yield rec


def retag_event(path: str, event_id: str, *, workspace_tag: Optional[str] = None, project_tag: Optional[str] = None,
                by: str = "", clock: Optional[Callable[[], datetime]] = None, enabled: Optional[bool] = None,
                config: Any = None) -> Dict[str, Any]:
    """Re-tag an event's workspace/project tag (I3: machine-owned records with TAGS may be re-tagged). The log is
    append-only, so this appends a ``retag`` event; the original line is untouched."""
    require_enabled(enabled, config, "retag_event")
    if workspace_tag is None and project_tag is None:
        raise ScopeRefusal("retag-empty", "nothing to re-tag")
    if not any(e.get("id") == event_id for e in read_events(path)):
        raise ScopeRefusal("event-unknown", f"no event {event_id!r} in the log")
    return append_event(path, "retag", message=f"re-tag of {event_id}", detail={"by": by}, workspace_tag=workspace_tag,
                        project_tag=project_tag, ref=event_id, clock=clock, enabled=enabled, config=config)


def effective_tags(path: str, event_id: str) -> Dict[str, Optional[str]]:
    """The current workspace/project tag of an event: the original, overridden by each later ``retag`` in order."""
    cur: Dict[str, Optional[str]] = {"workspace-tag": None, "project-tag": None}
    for e in read_events(path):
        if e.get("id") == event_id or (e.get("kind") == "retag" and e.get("ref") == event_id):
            for k in cur:
                if k in e:
                    cur[k] = e[k]
    return cur


def event_log_path_from(reg: dict, machine_layer=None) -> str:
    """The event log path from the MACHINE layer (I6: machine.json, outside ``workspaces/``): the ``event_log_path`` setting;
    a RELATIVE value (the registry default ``events/events.jsonl``) resolves under the DATA root, i.e. the ``runtime_root``
    setting, so nothing here spells a location (architect ruling 11). ``~`` expands against the current HOME."""
    from .settings_resolve import SettingsResolver

    r = SettingsResolver(reg, machine=machine_layer)
    p = os.path.expanduser(str(r.value("event_log_path")))
    if os.path.isabs(p):
        return p
    rr = r.get("runtime_root")
    if rr.source == "default":
        # no layer set the DATA root: the registry default is the LEGACY literal, so ask the one resolver (g69, S5-4b)
        from ..config import runtime_root_default

        return os.path.join(runtime_root_default(), p)
    return os.path.join(os.path.expanduser(str(rr.value)), p)


def resolve_event_log_path(deploy_root: str, reg: Optional[dict] = None) -> str:
    """:func:`event_log_path_from` with the layer read from the live ``<deploy_root>/machine.json``; when that is
    unreadable the registry defaults apply (the DATA-root default, then the relative log default)."""
    from . import settings_registry as SR
    from .machine import load_machine
    from .scopebase import ModelError
    from .settings_resolve import layer_from_canonical

    reg = reg if reg is not None else SR.load()
    try:
        layer = layer_from_canonical(reg, "machine", load_machine(deploy_root).settings)
    except ModelError:
        layer = None
    return event_log_path_from(reg, layer)

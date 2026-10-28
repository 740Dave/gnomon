"""Read-only AUDITS of stored scope (I9 coherence, I4 qualified project references, I3 re-file integrity).

These are the functions the invariant tests run, and that a sweep can run against a real deployment: each returns a list
of violations (empty = coherent). They read; they never write.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, List, Mapping, Optional, Tuple

from . import ltsread
from .model import Model
from .scopebase import QUALIFIED_RE, ScopeRefusal

#: record kinds whose project rule is 1 (required) and 0..1 (optional)
_REQUIRED = ("item", "topic")
_OPTIONAL = ("dsd-decision", "built-live-stamp")


def record_workspace(record: Mapping[str, Any], model: Model) -> Optional[str]:
    """The workspace a record is in, by I9: DERIVED from its project; only a no-project record (tier 3) stores one."""
    p = record.get("project")
    if p:
        return model.workspace_of_project(p)
    return record.get("workspace")


def check_record_scope(kind: str, record: Mapping[str, Any], model: Model) -> List[str]:
    """Violations of I9 for one stored record of ``kind``."""
    v: List[str] = []
    proj = record.get("project")
    if kind in _REQUIRED and not proj:
        v.append(f"{kind} has no project (required)")
    if proj:
        if not isinstance(proj, str) or not QUALIFIED_RE.fullmatch(proj):
            v.append(f"project {proj!r} is not qualified <ws>/<slug> (I4)")
        else:
            try:
                model.project(proj)
            except ScopeRefusal as exc:
                v.append(f"project {proj!r}: {exc.message}")
        if "workspace" in record:
            v.append("a workspace is stored alongside a project: it must be derived (I9 stored-workspace-rule)")
    elif "workspace" in record:
        if record["workspace"] not in model.workspaces:
            v.append(f"stored workspace {record['workspace']!r} is not a workspace")
    if kind not in _REQUIRED + _OPTIONAL + ("timeline-entry",):
        v.append(f"unknown record kind {kind!r}")
    return v


def check_timeline_key(entry: Mapping[str, Any], model: Model) -> List[str]:
    """Stored-workspace-rule (b): the timeline key's workspace component equals the workspace derived from the entry's
    projects (and every project is in it)."""
    v: List[str] = []
    ws = entry.get("workspace")
    for p in entry.get("projects") or []:
        if model.workspace_of_project(p) != ws:
            v.append(f"timeline entry key workspace {ws!r} != workspace of {p!r}")
    return v


_HIST = re.compile(r"^(\S+) (\S+) -> (\S+) by (.*)$")


def check_history_chain(props: Mapping[str, List[str]]) -> List[str]:
    """I3 integrity of an item's ``project-history``: entries are well formed, each entry's OLD equals the previous
    entry's NEW, and the last NEW equals the current ``project``. A direct edit of ``project`` (bypassing the writer)
    breaks the chain. (An item never re-filed has no history: nothing to check; see the S2 report's stated limit.)"""
    v: List[str] = []
    hist = props.get("project-history") or []
    cur = (props.get("project") or [None])[0]
    prev_new = None
    for h in hist:
        m = _HIST.match(h)
        if not m:
            v.append(f"malformed project-history entry {h!r}")
            continue
        if prev_new is not None and m.group(2) != prev_new:
            v.append(f"history breaks: entry {h!r} starts from {m.group(2)!r}, previous ended at {prev_new!r}")
        prev_new = m.group(3)
    if hist and prev_new is not None and cur != prev_new:
        v.append(f"project {cur!r} does not match the last history entry's new project {prev_new!r}: a direct edit?")
    return v


_PROJECT_KEYS = ("project", "project-tag", "current-project", "default-project")


def _walk_json(obj: Any, path: str, out: List[Tuple[str, str, Any]]) -> None:
    if isinstance(obj, dict):
        for k, val in obj.items():
            if k in _PROJECT_KEYS and isinstance(val, str):
                if not QUALIFIED_RE.fullmatch(val):
                    out.append((path, k, val))
            elif k == "projects" and isinstance(val, list) and all(isinstance(x, str) for x in val):
                out.extend((path, k, x) for x in val if not QUALIFIED_RE.fullmatch(x))
            else:
                _walk_json(val, path, out)
    elif isinstance(obj, list):
        for x in obj:
            _walk_json(x, path, out)


def scan_project_refs(roots: Iterable[str]) -> List[Tuple[str, str, Any]]:
    """I4: every stored project reference outside ``projects.json`` must be a qualified ``<ws>/<slug>``. Scans ``*.json``,
    ``*.jsonl`` (events, scope state, topics, bundles) and ``*.lts`` (``project:`` properties) under ``roots`` and returns
    ``[(file, key, value)]`` for each that is not. ``projects.json`` itself (where bare slugs are the format) is skipped;
    so is ``converter`` data (a deployment's pre-conversion input)."""
    bad: List[Tuple[str, str, Any]] = []
    for root in roots:
        for d, dirs, files in os.walk(root):
            dirs[:] = sorted(x for x in dirs if x not in ("__pycache__", "converter"))
            for fn in sorted(files):
                p = os.path.join(d, fn)
                if fn == "projects.json" or ".bak-" in fn:
                    continue
                if fn.endswith(".json"):
                    try:
                        with open(p, "r", encoding="utf-8") as fh:
                            _walk_json(json.load(fh), p, bad)
                    except ValueError:
                        bad.append((p, "<unparseable>", None))
                elif fn.endswith(".jsonl"):
                    with open(p, "r", encoding="utf-8") as fh:
                        for line in fh:
                            if line.strip():
                                try:
                                    _walk_json(json.loads(line), p, bad)
                                except ValueError:
                                    bad.append((p, "<unparseable line>", None))
                elif fn.endswith(".lts"):
                    for n in ltsread.read_file(p):
                        for val in n.props.get("project", []):
                            if not QUALIFIED_RE.fullmatch(val):
                                bad.append((p, f"{n.id}.project", val))
    return bad

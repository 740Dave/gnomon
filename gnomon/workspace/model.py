"""The deployment MODEL as the S2 scope core reads it: ``machine.json``, ``workspaces/<ws>/workspace.json`` and
``projects.json`` under the DEPLOY root (/ ENT-PROJECT / ENT-BACKLOG; the file shapes are the
ones the S1 scaffold writes: :func:`convert.write_scaffold`).

I6: this is WORKSPACE CONFIG. A machine safety path (alerts, relaunch, wind-down, bootstrap jobs, attention state)
must NEVER import or call anything here; ``tests/test_x13s2_safety_paths.py`` enforces that with an import check
and a file-access trace. Only ordinary work (writers, launch) loads the model.

Reads only; nothing in this module writes. A missing or malformed file raises :class:`ModelError` (loud stop).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Tuple

from .machine import Machine, load_machine, machine_path  # noqa: F401  (re-exported: the machine layer lives in .machine)
from .scopebase import (RESERVED_PROJECT, SLUG_RE, ModelError, ScopeRefusal, read_json, split_qualified)

PROJECT_STATES = ("active", "archived")
WORKSPACE_STATES = ("active", "archived")
_BACKLOG_FIELDS = ("id", "label", "corpus", "prefix", "root-node", "operator-gate-value")


@dataclass(frozen=True)
class Backlog:
    workspace: str
    id: str
    label: str
    corpus: str
    prefix: str
    root_node: str
    operator_gate_value: str

    @property
    def ref(self) -> str:
        return f"{self.workspace}/{self.id}"


@dataclass(frozen=True)
class Project:
    workspace: str
    slug: str
    name: str
    state: str

    @property
    def qualified(self) -> str:
        return f"{self.workspace}/{self.slug}"

    @property
    def active(self) -> bool:
        return self.state == "active"


@dataclass(frozen=True)
class Workspace:
    id: str
    display_name: str
    state: str
    order: int
    backlogs: Tuple[Backlog, ...]
    projects: Mapping[str, Project]
    settings: Mapping[str, object]
    path: str

    @property
    def active(self) -> bool:
        return self.state == "active"


@dataclass(frozen=True)
class Model:
    deploy_root: str
    workspaces: Mapping[str, Workspace]

    def workspace(self, ws: str) -> Workspace:
        try:
            return self.workspaces[ws]
        except KeyError:
            raise ScopeRefusal("workspace-unknown", f"workspace {ws!r} is not in this deployment") from None

    def project(self, qualified: str) -> Project:
        ws, slug = split_qualified(qualified)
        w = self.workspace(ws)
        try:
            return w.projects[slug]
        except KeyError:
            raise ScopeRefusal("project-unknown", f"project {qualified!r} is not in {ws}'s projects.json",
                               "pick a project that exists in that workspace") from None

    def workspace_of_project(self, qualified: str) -> str:
        """I9: a record's workspace IS its project's workspace (derived, never stored separately)."""
        return self.project(qualified).workspace

    def backlog(self, ref: str) -> Backlog:
        """``'<ws>/<backlog-id>'`` -> the backlog declaration."""
        ws, bid = split_qualified(ref)
        for b in self.workspace(ws).backlogs:
            if b.id == bid:
                return b
        raise ScopeRefusal("backlog-unknown", f"backlog {ref!r} is not declared in {ws}'s workspace.json")

    def backlog_by_prefix(self, prefix: str) -> Optional[Backlog]:
        for w in self.workspaces.values():
            for b in w.backlogs:
                if b.prefix == prefix:
                    return b
        return None

    def all_backlogs(self) -> List[Backlog]:
        return [b for w in self.workspaces.values() for b in w.backlogs]


def workspaces_dir(deploy_root: str) -> str:
    return os.path.join(deploy_root, "workspaces")


def _load_workspace(deploy_root: str, ws_dir: str) -> Workspace:
    base = os.path.join(workspaces_dir(deploy_root), ws_dir)
    wpath, ppath = os.path.join(base, "workspace.json"), os.path.join(base, "projects.json")
    try:
        wj = read_json(wpath)
        pj = read_json(ppath)
    except (OSError, ValueError) as exc:
        raise ModelError(f"workspace {ws_dir!r}: {exc}") from exc
    if not isinstance(wj, dict) or wj.get("schema") != 1 or wj.get("id") != ws_dir or not SLUG_RE.fullmatch(str(wj.get("id"))):
        raise ModelError(f"{wpath!r}: not a schema-1 workspace whose id equals its directory {ws_dir!r}")
    if wj.get("state") not in WORKSPACE_STATES:
        raise ModelError(f"{wpath!r}: state {wj.get('state')!r} must be one of {WORKSPACE_STATES}")
    if not isinstance(pj, dict) or pj.get("schema") != 1 or pj.get("workspace") != ws_dir or not isinstance(pj.get("projects"), list):
        raise ModelError(f"{ppath!r}: not a schema-1 projects file for {ws_dir!r}")
    projects: Dict[str, Project] = {}
    for p in pj["projects"]:
        slug = p.get("slug") if isinstance(p, dict) else None
        if not isinstance(slug, str) or not SLUG_RE.fullmatch(slug) or slug in projects:
            raise ModelError(f"{ppath!r}: bad or duplicate project slug {slug!r}")
        if p.get("state") not in PROJECT_STATES:
            raise ModelError(f"{ppath!r}: project {slug!r} state {p.get('state')!r} must be one of {PROJECT_STATES}")
        projects[slug] = Project(ws_dir, slug, str(p.get("name", slug)), p["state"])
    if RESERVED_PROJECT not in projects:
        raise ModelError(f"{ppath!r}: the reserved project {RESERVED_PROJECT!r} is missing (ENT-PROJECT reserved-project)")
    backlogs: List[Backlog] = []
    seen_ids = set()
    for b in wj.get("backlogs") or []:
        missing = [k for k in _BACKLOG_FIELDS if not isinstance(b, dict) or not isinstance(b.get(k), str) or not b.get(k)]
        if missing:
            raise ModelError(f"{wpath!r}: backlog {b!r} lacks {missing} (ENT-BACKLOG fields)")
        if b["id"] in seen_ids:
            raise ModelError(f"{wpath!r}: duplicate backlog id {b['id']!r}")
        seen_ids.add(b["id"])
        backlogs.append(Backlog(ws_dir, b["id"], b["label"], b["corpus"], b["prefix"], b["root-node"], b["operator-gate-value"]))
    settings = wj.get("settings") if isinstance(wj.get("settings"), dict) else {}
    return Workspace(ws_dir, str(wj.get("display-name", ws_dir)), wj["state"], int(wj.get("order", 0) or 0),
                     tuple(backlogs), projects, settings, base)


def load_model(deploy_root: str) -> Model:
    """Every workspace under ``<deploy>/workspaces/``. Backlog prefixes must be machine-unique (I4)."""
    root = workspaces_dir(deploy_root)
    if not os.path.isdir(root):
        raise ModelError(f"no workspaces directory at {root!r}")
    out: Dict[str, Workspace] = {}
    for name in sorted(os.listdir(root)):
        if os.path.isfile(os.path.join(root, name, "workspace.json")):
            out[name] = _load_workspace(deploy_root, name)
    prefixes: Dict[str, str] = {}
    for w in out.values():
        for b in w.backlogs:
            if b.prefix in prefixes:
                raise ModelError(f"backlog prefix {b.prefix!r} is declared by both {prefixes[b.prefix]} and {b.ref} (I4)")
            prefixes[b.prefix] = b.ref
    return Model(deploy_root=deploy_root, workspaces=out)


def home_workspace_of(deploy_root: str, role_id: str) -> Optional[str]:
    """The HOME workspace of a role: the one whose ``roles/<role_id>.md`` exists (ENT-ROLE-DEFINITION: a role
    definition is owned by its home workspace). ``None`` when no workspace holds one (unresolved: the caller
    decides, loudly); more than one holder violates I1 (exactly one owner) and raises."""
    root = workspaces_dir(deploy_root)
    if not os.path.isdir(root):
        return None
    holders = [n for n in sorted(os.listdir(root)) if os.path.isfile(os.path.join(root, n, "roles", f"{role_id}.md"))]
    if len(holders) > 1:
        raise ModelError(f"role {role_id!r} has a definition in several workspaces {holders}: I1 allows exactly one owner")
    return holders[0] if holders else None


def load_corpus_owners(corpus_config_path: str) -> Dict[str, str]:
    """``corpus id -> owner`` from the lts corpus registry's per-corpus ``owner`` key (ENT-CORPUS substrate:
    'corpus-config entry gains owner'). The owner value is ``machine`` or ``workspace:<ws>`` (the same spelling the
    S1 corpus-map uses). A corpus whose entry has no ``owner`` is ABSENT from the result (owner unknown: the
    caller refuses, fail closed)."""
    try:
        cfg = read_json(corpus_config_path)
    except (OSError, ValueError) as exc:
        raise ModelError(f"corpus registry unreadable at {corpus_config_path!r}: {exc}") from exc
    if not isinstance(cfg, dict):
        raise ModelError(f"{corpus_config_path!r}: not an object")
    owners: Dict[str, str] = {}
    for cid, spec in cfg.items():
        owner = spec.get("owner") if isinstance(spec, dict) else None
        if owner is None:
            continue
        if owner != "machine" and not (isinstance(owner, str) and owner.startswith("workspace:") and SLUG_RE.fullmatch(owner[len("workspace:"):])):
            raise ModelError(f"corpus {cid!r}: owner {owner!r} must be 'machine' or 'workspace:<ws>'")
        owners[cid] = owner
    return owners


#: the largest workspace.json the lenient launch-path reader will read (the real files are a few KB).
_WORKSPACE_JSON_MAX = 1024 * 1024


def _read_json_nonblocking(path: str):
    """``read_json`` that cannot block (``safe_read.read_regular_bytes``: ``O_NONBLOCK``, the open descriptor must be a regular file, bounded
    at 1 MB). Symlinks are followed exactly as before. This is the ONE reader of a workspace's data file for the launch path AND for the
 open-work override, so the two can never disagree on what a valid file is."""
    import json

    from ..safe_read import read_regular_bytes

    raw = read_regular_bytes(path, _WORKSPACE_JSON_MAX)
    try:
        return json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(str(exc)) from exc


def read_workspace_record(deploy_root: str, ws: str):
    """LENIENT read of one ``workspace.json`` for the launch path: ``(record_or_None, reason)``. Unlike
    :func:`load_model` it never raises: an unreadable or malformed file is reported as ``(None, reason)`` so the caller
    can SKIP the workspace settings layer loudly (I6) instead of failing every launch."""
    path = os.path.join(workspaces_dir(deploy_root), ws, "workspace.json")
    try:
        rec = _read_json_nonblocking(path)
    except (OSError, ValueError) as exc:
        return None, f"{path}: {exc}"
    if not isinstance(rec, dict):
        return None, f"{path}: not an object"
    if "settings" in rec and not isinstance(rec["settings"], dict):
        return None, f"{path}: settings is not an object"
    return rec, ""

"""The TYPED WRITERS of the X13 S2 scope core (I2, I3, I4, I5, I9; ENT-ITEM, ENT-TOPIC, ENT-PREFIX-REGISTRY).

Every persisting method calls :func:`~.scopebase.require_enabled` first: with ``workspace_scope_core`` OFF
nothing is written (:class:`~.scopebase.ScopeCoreDisabled`). Every root is INJECTED (``deploy_root``, ``data_root``,
the corpus registry, the event log path): nothing here resolves a live default (g31 test isolation).

What it enforces (each by a persisted test; see ``tests/test_x13s2_*``):

* items: a project is REQUIRED and must be in the BACKLOG's workspace; the backlog's corpus must be owned by that same
  workspace (ENT-BACKLOG; an item in a machine-owned corpus is refused); ``unassigned`` is refused for new items;
* topics: EXACTLY ONE project (topics-rulings D2); the workspace is derived, never stored (I9);
* archived projects are refused only for NEW assignments (create, re-file into, set current-project); evidence and
  history records may name them (I2);
* a re-file goes through the writer and appends ``project-history`` (``<iso-ts> <old> -> <new> by <who>``); items
  re-file only within their backlog's workspace (a cross-workspace move is 'close here, create there, cross-link'); a
  topic's cross-workspace re-file is a move (write new, archive old, cross-link); a DIRECT edit of the scope keys is
  refused (:meth:`ScopeWriter.edit_item_property`);
* add-backlog / add-corpus / add-workspace refuse a prefix already registered anywhere on the machine (I4);
* a workspace is never deleted, only renamed (display name; the slug is stable forever) or archived (I5).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence

from . import events as ev
from . import ltsread, registry as prefix_registry
from .entities import topic_shape_errors
from .model import Backlog, Model, load_model
from .ownership import authorize_delete
from .scope import ScopeSession, Subject, load_session_scope, save_session_scope, stamp
from .scopebase import (QUALIFIED_RE, RESERVED_PROJECT, SLUG_RE, ScopeRefusal, atomic_write_json, now_iso, qualify, read_json,
                        require_enabled, split_qualified)
from .store import CorpusRegistry, LtsFiles, TopicStore, check_key, check_node_id, check_title, check_value

#: scope properties only the typed writer may set (I3: a direct edit is refused)
SCOPE_KEYS = ("project", "workspace", "project-history")
#: a workspace slug reserved because 'All' is a VIEW, never a stored workspace (T2)
RESERVED_WORKSPACES = ("all",)


#: an item whose status starts with one of these words is CLOSED (architect ruling 7, 2026-09-30); every other status, and a
#: missing one, is OPEN
CLOSED_STATUS_WORDS = ("done", "dropped")


def is_open_status(status: str) -> bool:
    m = re.match(r"\s*([A-Za-z]+)", status or "")
    return not (m and m.group(1).lower() in CLOSED_STATUS_WORDS)


@dataclass(frozen=True)
class ArchiveReport:
    project: str
    #: items carrying the project whose status is not done or dropped: these need RE-FILING (ruling 7)
    open_items: List[Dict[str, str]]
    #: the rest (done or dropped), listed separately so nothing is silently omitted
    other_items: List[Dict[str, str]]
    topics: List[str]


class ScopeWriter:
    def __init__(self, deploy_root: str, data_root: str, *, event_log_path: str, corpora: Optional[CorpusRegistry] = None,
                 clock: Optional[Callable[[], datetime]] = None, enabled: Optional[bool] = None, config: Any = None) -> None:
        self.deploy_root = os.path.abspath(deploy_root)
        self.data_root = os.path.abspath(data_root)
        self.event_log_path = event_log_path
        self.corpora = corpora
        self._clock = clock
        self._enabled = enabled
        self._config = config
        self.topics = TopicStore(self.data_root)

    # ---- plumbing ------------------------------------------------------------------------------------------------
    def _gate(self, what: str) -> None:
        require_enabled(self._enabled, self._config, what)

    def model(self) -> Model:
        """A FRESH model each time (writes change projects.json / workspace.json; a stale snapshot would lie)."""
        return load_model(self.deploy_root)

    def _ts(self) -> str:
        return now_iso(self._clock)

    def _emit(self, events: Sequence[Mapping[str, Any]]) -> None:
        for e in events:
            e = dict(e)
            ev.append_event(self.event_log_path, e.pop("kind"), clock=self._clock, enabled=self._enabled, config=self._config, **e)

    def _need_corpora(self) -> CorpusRegistry:
        if self.corpora is None:
            raise ScopeRefusal("corpus-registry-missing", "this writer was built without a corpus registry")
        return self.corpora

    def _corpus_owner(self, corpus: str) -> Optional[str]:
        spec = self._need_corpora().load().get(corpus)
        return spec.get("owner") if isinstance(spec, dict) else None

    def _prefix_registry_path(self) -> str:
        return os.path.join(self.data_root, "prefix-registry.json")

    def _load_prefix_registry(self) -> dict:
        try:
            return read_json(self._prefix_registry_path())
        except (OSError, ValueError) as exc:
            raise ScopeRefusal("prefix-registry-unreadable", f"{self._prefix_registry_path()!r}: {exc}") from exc

    def _check_prefix_free(self, reg: dict, prefix: str, *, pending: Sequence[str] = ()) -> None:
        if not isinstance(prefix, str) or not re.fullmatch(r"[A-Z][A-Z0-9]*(-[A-Z0-9]+)*", prefix):
            raise ScopeRefusal("prefix-malformed", f"prefix {prefix!r} must be upper-case dash-separated tokens")
        if prefix_registry.refuses(reg, prefix) or prefix in pending:
            raise ScopeRefusal("prefix-registered", f"prefix {prefix!r} is already registered on this machine (I4: ids are machine-unique)",
                               "choose a prefix no backlog, corpus or id family uses")

    # ---- items ---------------------------------------------------------------------------------------------------
    def _backlog_item_file(self, model: Model, backlog: Backlog) -> str:
        owner = self._corpus_owner(backlog.corpus)
        if owner != f"workspace:{backlog.workspace}":
            raise ScopeRefusal("backlog-corpus-not-owned",
                               f"backlog {backlog.ref}'s corpus {backlog.corpus!r} has owner {owner!r}, not workspace:{backlog.workspace} "
                               "(ENT-BACKLOG: its corpus must be owned by the same workspace; no items in a shared corpus)")
        f = self._need_corpora().find_node(backlog.corpus, backlog.root_node)
        if f is None:
            raise ScopeRefusal("backlog-root-missing", f"root node {backlog.root_node!r} of {backlog.ref} not found in corpus {backlog.corpus!r}")
        return f

    def create_item(self, backlog: str, node_id: str, title: str, *, props: Optional[Mapping[str, str]] = None,
                    project: Optional[str] = None, session: Optional[ScopeSession] = None, by: str = "") -> Dict[str, Any]:
        """Create an item node in the backlog's corpus. ``project`` is tier 0; else the session's current-project (tier
        2); a required project that resolves to none is a refusal (tier 3 is illegal for items)."""
        self._gate("create_item")
        # F4: every text that lands in the lts file is validated (keys, VALUES, title, id), not only the scope keys
        check_title(title)
        check_node_id(node_id)
        check_value("by", by)
        for k, v in (props or {}).items():
            check_key(k)
            check_value(f"value of {k}", v)
        model = self.model()
        b = model.backlog(backlog)
        if not node_id.startswith(b.prefix + "-"):
            raise ScopeRefusal("item-id-prefix", f"item id {node_id!r} must start with the backlog prefix {b.prefix + '-'!r} (I4)")
        for k in (props or {}):
            if k in SCOPE_KEYS:
                raise ScopeRefusal("scope-key-not-settable", f"{k!r} is stamped by the writer (I9); it cannot be passed as a property")
        f = self._backlog_item_file(model, b)
        if self._need_corpora().find_node(b.corpus, node_id) is not None:
            raise ScopeRefusal("item-exists", f"{node_id!r} already exists in corpus {b.corpus!r}")
        try:
            st = stamp(model, "item", explicit=project, session=session, backlog=b)
        except ScopeRefusal as exc:
            self._emit(exc.events)
            raise
        self._emit(st.events)
        with open(f, "r", encoding="utf-8") as fh:
            text = fh.read()
        lines: List = [(k, v) for k, v in (props or {}).items()]
        lines.append(("project", st.project))
        from .scopebase import atomic_write_text

        atomic_write_text(f, LtsFiles.append_node(text, f"## {title} [{node_id}]", lines), enabled=self._enabled, config=self._config)
        return {"node": node_id, "file": f, "project": st.project, "workspace": st.workspace, "tier": st.tier}

    def _item_location(self, model: Model, backlog: str, node_id: str):
        b = model.backlog(backlog)
        f = self._need_corpora().find_node(b.corpus, node_id)
        if f is None:
            raise ScopeRefusal("item-unknown", f"{node_id!r} is not in corpus {b.corpus!r}")
        with open(f, "r", encoding="utf-8") as fh:
            text = fh.read()
        props = LtsFiles.get_props(text, node_id) or {}
        cur = (props.get("project") or [None])[0]
        return b, f, text, cur

    def refile_item(self, backlog: str, node_id: str, new_project: str, *, by: str) -> Dict[str, Any]:
        """Re-file an item to another project IN ITS BACKLOG'S WORKSPACE (I3): append ``project-history``."""
        self._gate("refile_item")
        check_value("by", by)
        model = self.model()
        b, f, text, cur = self._item_location(model, backlog, node_id)
        ws, _slug = split_qualified(new_project)
        if ws != b.workspace:
            raise ScopeRefusal("cross-workspace-refile",
                               f"{node_id!r} belongs to workspace {b.workspace!r}; a move to {ws!r} is not a re-file",
                               "close it here, create it there, and cross-link them (T13)")
        p = model.project(new_project)
        if not p.active:
            raise ScopeRefusal("project-archived", f"project {new_project!r} is archived: no re-file into it (I2)", "re-pick: choose an active project")
        if p.slug == RESERVED_PROJECT:
            raise ScopeRefusal("reserved-project", f"{new_project!r} is reserved: only the converter writes into it")
        if cur == new_project:
            raise ScopeRefusal("already-in-project", f"{node_id!r} is already in {new_project!r}")
        entry = f"{self._ts()} {cur} -> {new_project} by {by}"
        text = LtsFiles.set_property(text, node_id, "project", new_project)
        text = LtsFiles.add_property(text, node_id, "project-history", entry)
        from .scopebase import atomic_write_text

        atomic_write_text(f, text, enabled=self._enabled, config=self._config)
        return {"node": node_id, "project": new_project, "workspace": p.workspace, "history": entry}

    def edit_item_property(self, backlog: str, node_id: str, key: str, value: str) -> None:
        """The generic property editor for an item. The scope keys are NOT editable here (I3: a re-file goes through
        :meth:`refile_item`; a direct edit of ``project`` / ``workspace`` / ``project-history`` is refused)."""
        self._gate("edit_item_property")
        if key in SCOPE_KEYS:
            raise ScopeRefusal("direct-scope-edit", f"{key!r} cannot be edited directly (I3): use refile_item", "re-file through the typed writer")
        check_key(key)
        check_value(f"value of {key}", value)
        model = self.model()
        _b, f, text, _cur = self._item_location(model, backlog, node_id)
        from .scopebase import atomic_write_text

        atomic_write_text(f, LtsFiles.set_property(text, node_id, key, value), enabled=self._enabled, config=self._config)

    def record_built_live(self, backlog: str, node_id: str, which: str, session_run: str) -> Dict[str, Any]:
        """A Built/Live stamp (ENT-ITEM ``built-in-session`` / ``live-in-session``): ``<session-run>@<iso-ts>``. The
        stamp's scope is its SUBJECT's (the item's project, tier 1) and it may name an ARCHIVED project (evidence: I2)."""
        self._gate("record_built_live")
        if which not in ("built", "live"):
            raise ScopeRefusal("stamp-kind", "which must be 'built' or 'live'")
        check_value("session-run", session_run)
        model = self.model()
        _b, f, text, cur = self._item_location(model, backlog, node_id)
        st = stamp(model, "built-live-stamp", subjects=[Subject(node_id, (cur,) if cur else ())])
        from .scopebase import atomic_write_text

        atomic_write_text(f, LtsFiles.set_property(text, node_id, f"{which}-in-session", f"{session_run}@{self._ts()}"), enabled=self._enabled, config=self._config)
        return {"node": node_id, "project": st.project, "workspace": st.workspace, "tier": st.tier}

    # ---- topics --------------------------------------------------------------------------------------------------
    def _topic_shape(self, model: Model, ws: str, rec: Mapping[str, Any]) -> None:
        errs = topic_shape_errors(dict(rec), ws_dir=ws, workspaces={w: list(x.projects) for w, x in model.workspaces.items()})
        if errs:
            raise ScopeRefusal("topic-shape", "; ".join(errs))

    def create_topic(self, title: str, *, project: Optional[str] = None, session: Optional[ScopeSession] = None,
                     by: str = "", body: str = "") -> Dict[str, Any]:
        """Create a topic: EXACTLY ONE project (required; tier 0 or tier 2; tier 3 refuses); the workspace is derived
        from it and never stored (I9). Stored at DATA ``workspaces/<ws>/topics/TOP-nnn.json``."""
        self._gate("create_topic")
        model = self.model()
        try:
            st = stamp(model, "topic", explicit=project, session=session)
        except ScopeRefusal as exc:
            self._emit(exc.events)
            raise
        self._emit(st.events)
        tid = self.topics.allocate_id(enabled=self._enabled, config=self._config)
        rec = {"id": tid, "title": title, "project": st.project, "state": "active", "created": self._ts(), "created-by": by, "body": body}
        self._topic_shape(model, st.workspace, rec)
        path = self.topics.write(st.workspace, rec, enabled=self._enabled, config=self._config)
        return {"id": tid, "path": path, "project": st.project, "workspace": st.workspace, "tier": st.tier}

    def refile_topic(self, topic_id: str, new_project: str, *, by: str) -> Dict[str, Any]:
        """Re-file a topic. Same workspace: the project changes in place and ``project-history`` is appended. Other
        workspace: a MOVE (write new, archive old, cross-link; I3)."""
        self._gate("refile_topic")
        model = self.model()
        found = self.topics.find(topic_id)
        if found is None:
            raise ScopeRefusal("topic-unknown", f"no topic {topic_id!r}")
        ws, _path, rec = found
        if rec.get("state") != "active":
            raise ScopeRefusal("topic-archived", f"{topic_id!r} is archived: it cannot be re-filed")
        p = model.project(new_project)
        if not p.active:
            raise ScopeRefusal("project-archived", f"project {new_project!r} is archived: no re-file into it (I2)", "re-pick: choose an active project")
        if rec["project"] == new_project:
            raise ScopeRefusal("already-in-project", f"{topic_id!r} is already in {new_project!r}")
        entry = f"{self._ts()} {rec['project']} -> {new_project} by {by}"
        if p.workspace == ws:
            new = dict(rec, project=new_project)
            new["project-history"] = list(rec.get("project-history", [])) + [entry]
            self._topic_shape(model, ws, new)
            self.topics.write(ws, new, enabled=self._enabled, config=self._config)
            return {"id": topic_id, "moved": False, "project": new_project, "workspace": ws, "history": entry}
        new_id = self.topics.allocate_id(enabled=self._enabled, config=self._config)
        created = {k: v for k, v in rec.items() if k not in ("id", "project", "project-history", "state", "archived-at", "moved-to")}
        created.update({"id": new_id, "project": new_project, "state": "active", "moved-from": topic_id, "project-history": [entry]})
        self._topic_shape(model, p.workspace, created)
        self.topics.write(p.workspace, created, enabled=self._enabled, config=self._config)
        old = dict(rec, state="archived", **{"archived-at": self._ts(), "moved-to": new_id})
        self._topic_shape(model, ws, old)
        self.topics.write(ws, old, enabled=self._enabled, config=self._config)
        return {"id": new_id, "moved": True, "moved-from": topic_id, "project": new_project, "workspace": p.workspace, "history": entry}

    def delete_topic(self, topic_id: str, *, acting: str) -> None:
        """Delete a topic: allowed only at its OWNER scope (I1)."""
        self._gate("delete_topic")
        found = self.topics.find(topic_id)
        if found is None:
            raise ScopeRefusal("topic-unknown", f"no topic {topic_id!r}")
        ws, path, _rec = found
        authorize_delete("TOPIC", acting=acting, workspace=ws)
        require_enabled(self._enabled, self._config, "delete_topic (unlink)")      # at the primitive too: a no-op _gate override cannot delete
        os.unlink(path)

    # ---- current-project (session state) -------------------------------------------------------------------------
    def set_current_project(self, session_dir: str, project: Optional[str], *, by: str) -> ScopeSession:
        """Set the session's current-project: a NEW assignment, so an archived project is refused with a re-pick prompt;
        the project's workspace must be one of the role's workspaces. ``None`` clears it. Logs an event."""
        self._gate("set_current_project")
        model = self.model()
        scope = load_session_scope(session_dir)
        if project is not None:
            p = model.project(project)
            if not p.active:
                raise ScopeRefusal("project-archived", f"project {project!r} is archived: it cannot become the current-project (I2)", "re-pick: choose an active project")
            if p.workspace not in scope.role_workspaces:
                raise ScopeRefusal("project-not-in-role-workspaces", f"{project!r} is in workspace {p.workspace!r}, which is not in this role's workspaces {list(scope.role_workspaces)}")
        new = replace(scope, current_project=project, history=tuple(scope.history) + ({"ts": self._ts(), "project": project, "by": by},))
        save_session_scope(session_dir, new, enabled=self._enabled, config=self._config)
        self._emit([{"kind": "current-project-changed", "message": f"current-project {scope.current_project} -> {project}",
                     "detail": {"by": by, "session-dir": os.path.basename(session_dir.rstrip('/'))},
                     **({"project_tag": project} if project else {})}])
        return new

    # ---- projects / workspaces ------------------------------------------------------------------------------------
    def _write_workspace_json(self, ws: str, mut: Callable[[dict], None]) -> dict:
        path = os.path.join(self.deploy_root, "workspaces", ws, "workspace.json")
        obj = read_json(path)
        mut(obj)
        atomic_write_json(path, obj, enabled=self._enabled, config=self._config)
        return obj

    def archive_project(self, workspace: str, slug: str, *, by: str) -> ArchiveReport:
        """Archive a project (I2). Returns the OPEN items (status not done or dropped) and topics still carrying it, for
        re-filing, plus the other items separately; nothing is re-filed here. The reserved project cannot be archived (every workspace must keep its stamping target)."""
        self._gate("archive_project")
        model = self.model()
        q = qualify(workspace, slug)
        p = model.project(q)
        if p.slug == RESERVED_PROJECT:
            raise ScopeRefusal("reserved-project", f"{q!r} is reserved and cannot be archived")
        if not p.active:
            raise ScopeRefusal("already-archived", f"{q!r} is already archived")
        path = os.path.join(self.deploy_root, "workspaces", workspace, "projects.json")
        obj = read_json(path)
        for row in obj["projects"]:
            if row["slug"] == slug:
                row["state"] = "archived"
                row["archived-at"] = self._ts()
                row["archived-by"] = by
        atomic_write_json(path, obj, enabled=self._enabled, config=self._config)
        open_items: List[Dict[str, str]] = []
        other_items: List[Dict[str, str]] = []
        if self.corpora is not None:
            for b in model.workspace(workspace).backlogs:
                seen = set()
                for f in self.corpora.files(b.corpus):
                    for n in ltsread.read_file(f):
                        if n.get("project") == q and (f, n.id) not in seen:
                            seen.add((f, n.id))
                            row = {"node": n.id, "backlog": b.ref, "status": n.get("status")}
                            (open_items if is_open_status(row["status"]) else other_items).append(row)
        topics = [r["id"] for w, _p, r in self.topics.iter_all() if r.get("project") == q and r.get("state") == "active"]
        return ArchiveReport(q, open_items, other_items, topics)

    def add_workspace(self, slug: str, display_name: str, *, backlogs: Sequence[Mapping[str, str]] = (), order: Optional[int] = None) -> str:
        """Create a workspace (siblings only: T13). Its backlogs' prefixes must be unregistered machine-wide (I4)."""
        self._gate("add_workspace")
        if not SLUG_RE.fullmatch(slug or "") or slug in RESERVED_WORKSPACES:
            raise ScopeRefusal("workspace-slug", f"{slug!r} is not a valid workspace slug (reserved: {list(RESERVED_WORKSPACES)}: 'All' is a view)")
        base = os.path.join(self.deploy_root, "workspaces", slug)
        if os.path.exists(base):
            raise ScopeRefusal("workspace-exists", f"workspace {slug!r} already exists (a slug is stable forever)")
        reg = self._load_prefix_registry()
        pending: List[str] = []
        entries = []
        for b in backlogs:
            entry = self._backlog_entry(slug, b, reg, pending)
            pending.append(entry["prefix"])
            entries.append(entry)
        model_order = order if order is not None else len(self.model().workspaces) + 1
        require_enabled(self._enabled, self._config, "add_workspace (makedirs)")   # at the primitive too: a no-op _gate override leaves no directories
        os.makedirs(os.path.join(base, "brand"))
        os.makedirs(os.path.join(base, "roles"))
        os.makedirs(os.path.join(self.data_root, "workspaces", slug, "topics"), exist_ok=True)
        atomic_write_json(os.path.join(base, "projects.json"), {"schema": 1, "workspace": slug, "projects": [
            {"slug": RESERVED_PROJECT, "name": "Unassigned", "state": "active", "source": "reserved"}]}, enabled=self._enabled, config=self._config)
        atomic_write_json(os.path.join(base, "workspace.json"), {
            "schema": 1, "id": slug, "display-name": display_name, "order": model_order, "coordinating-architect": None,
            "backlogs": entries, "logo": None, "settings": {}, "state": "active"}, enabled=self._enabled, config=self._config)
        for e in entries:
            reg["prefixes"][e["prefix"]] = {"owner": f"{slug}/{e['id']}", "kind": "backlog", "corpus": e["corpus"]}
        atomic_write_json(self._prefix_registry_path(), reg, enabled=self._enabled, config=self._config)
        return base

    def _backlog_entry(self, ws: str, b: Mapping[str, str], reg: dict, pending: Sequence[str]) -> Dict[str, str]:
        need = ("id", "label", "corpus", "prefix", "root-node", "operator-gate-value")
        missing = [k for k in need if not b.get(k)]
        if missing:
            raise ScopeRefusal("backlog-fields", f"backlog lacks {missing} (ENT-BACKLOG fields)")
        if not SLUG_RE.fullmatch(b["id"]):
            raise ScopeRefusal("backlog-id", f"backlog id {b['id']!r} must be a slug")
        self._check_prefix_free(reg, b["prefix"], pending=pending)
        owner = self._corpus_owner(b["corpus"])
        if owner != f"workspace:{ws}":
            raise ScopeRefusal("backlog-corpus-not-owned", f"corpus {b['corpus']!r} has owner {owner!r}, not workspace:{ws}: a backlog's corpus must be owned by its workspace")
        return {k: b[k] for k in need}

    def add_backlog(self, workspace: str, backlog: Mapping[str, str]) -> Dict[str, str]:
        """Declare a backlog in a workspace: its prefix must be unregistered machine-wide (I4) and its corpus must be
        owned by the same workspace."""
        self._gate("add_backlog")
        model = self.model()
        w = model.workspace(workspace)
        if not w.active:
            raise ScopeRefusal("workspace-archived", f"workspace {workspace!r} is archived")
        if any(b.id == backlog.get("id") for b in w.backlogs):
            raise ScopeRefusal("backlog-exists", f"backlog {backlog.get('id')!r} already exists in {workspace!r}")
        reg = self._load_prefix_registry()
        entry = self._backlog_entry(workspace, backlog, reg, ())
        self._write_workspace_json(workspace, lambda o: o.setdefault("backlogs", []).append(entry))
        reg["prefixes"][entry["prefix"]] = {"owner": f"{workspace}/{entry['id']}", "kind": "backlog", "corpus": entry["corpus"]}
        atomic_write_json(self._prefix_registry_path(), reg, enabled=self._enabled, config=self._config)
        return entry

    def add_corpus(self, corpus: str, *, roots: Sequence[str], prefix: str, owner: str, projects: Sequence[str] = (),
                   visible_in: Sequence[str] = (), domain_suffix: Optional[str] = None) -> Dict[str, Any]:
        """Register a corpus in the corpus registry with its ``owner`` (machine OR workspace, never both), ``projects``
        and ``visible-in`` (workspace-owned only). Its prefix must be unregistered machine-wide (I4)."""
        self._gate("add_corpus")
        model = self.model()
        if not SLUG_RE.fullmatch(corpus or "") and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", corpus or ""):
            raise ScopeRefusal("corpus-id", f"corpus id {corpus!r} is malformed")
        if owner != "machine" and not (owner.startswith("workspace:") and owner[len("workspace:"):] in model.workspaces):
            raise ScopeRefusal("corpus-owner", f"owner {owner!r} must be 'machine' or 'workspace:<an existing workspace>' (exactly one, never both)")
        if visible_in and owner == "machine":
            raise ScopeRefusal("visible-in-machine-owned", "visible-in[] applies only to workspace-owned corpora (ENT-CORPUS)")
        for v in visible_in:
            if v not in model.workspaces:
                raise ScopeRefusal("visible-in-unknown", f"visible-in workspace {v!r} does not exist")
        for q in projects:
            p = model.project(q)
            if owner != "machine" and p.workspace != owner[len("workspace:"):]:
                raise ScopeRefusal("corpus-project-workspace", f"project {q!r} is not in the owning workspace {owner!r}")
        reg = self._load_prefix_registry()
        self._check_prefix_free(reg, prefix)
        entry: Dict[str, Any] = {"roots": [{"path": r, "area": "specs"} for r in roots], "owner": owner, "projects": list(projects), "visible-in": list(visible_in)}
        if domain_suffix:
            entry["domain_suffix"] = domain_suffix
        self._need_corpora().add(corpus, entry, enabled=self._enabled, config=self._config)
        reg["prefixes"][prefix] = {"owner": owner, "kind": "corpus", "corpus": corpus}
        atomic_write_json(self._prefix_registry_path(), reg, enabled=self._enabled, config=self._config)
        return entry

    def rename_workspace(self, workspace: str, display_name: str) -> None:
        """Rename = change the DISPLAY name only: the slug is stable forever (I5). Role data is never touched."""
        self._gate("rename_workspace")
        self.model().workspace(workspace)
        if not display_name.strip():
            raise ScopeRefusal("display-name-empty", "a display name is required")
        self._write_workspace_json(workspace, lambda o: o.__setitem__("display-name", display_name))

    def archive_workspace(self, workspace: str, *, by: str) -> None:
        """Archive a workspace (I5: never deleted). Role data (DATA ``intelligences/<id>/``) is not touched."""
        self._gate("archive_workspace")
        w = self.model().workspace(workspace)
        if not w.active:
            raise ScopeRefusal("already-archived", f"workspace {workspace!r} is already archived")
        def mut(o: dict) -> None:
            o["state"] = "archived"
            o["archived-at"] = self._ts()
            o["archived-by"] = by
        self._write_workspace_json(workspace, mut)

    def delete_workspace(self, workspace: str, *, acting: str = "machine") -> None:
        """Always refused: a workspace is never deleted, only archived (I5)."""
        authorize_delete("WORKSPACE", acting=acting, workspace=workspace)

    # ---- evidence / tags ------------------------------------------------------------------------------------------
    def write_alert(self, message: str, *, severity: str = "warn", workspace_tag: Optional[str] = None,
                    project_tag: Optional[str] = None, targets: Optional[List[str]] = None) -> Dict[str, Any]:
        """An alert event. Its tags are an opaque write-time ``<ws>/<slug>`` string (exempt from I9) and MAY name an
        ARCHIVED project (evidence/history: I2, R4)."""
        self._gate("write_alert")
        return ev.append_event(self.event_log_path, "alert", severity=severity, message=message, targets=targets,
                               workspace_tag=workspace_tag, project_tag=project_tag, clock=self._clock, enabled=self._enabled, config=self._config)

    def retag_alert(self, event_id: str, *, workspace_tag: Optional[str] = None, project_tag: Optional[str] = None, by: str = "") -> Dict[str, Any]:
        self._gate("retag_alert")
        return ev.retag_event(self.event_log_path, event_id, workspace_tag=workspace_tag, project_tag=project_tag, by=by,
                              clock=self._clock, enabled=self._enabled, config=self._config)

    def _decision_corpus_of(self, file: str) -> str:
        """F-R2-9: the corpus a decision node may be appended to. ``file`` must be an existing, regular (not symlinked) ``.lts`` file
        whose real path lies under a root of a REGISTERED, MACHINE-owned corpus (ENT-DSD-DECISION: 'one machine answers corpus').
        Returns that corpus id. LIMIT: the registry has no 'answers' marker, so any machine-owned corpus qualifies; a workspace-owned
        corpus (a backlog) or a path outside every registered corpus is refused."""
        if not isinstance(file, str) or not file.endswith(".lts") or os.path.islink(file) or not os.path.isfile(file):
            raise ScopeRefusal("decision-file-invalid", f"{file!r} is not an existing regular .lts file (a symlink is never followed)")
        real = os.path.realpath(file)
        cfg = self._need_corpora().load()
        for cid in sorted(cfg):
            spec = cfg[cid]
            if not isinstance(spec, dict):
                continue
            for r in spec.get("roots") or []:
                root = os.path.realpath(r["path"])
                if real.startswith(root.rstrip(os.sep) + os.sep):
                    if spec.get("owner") != "machine":
                        raise ScopeRefusal("decision-file-not-machine-corpus",
                                           f"{file!r} is in corpus {cid!r} (owner {spec.get('owner')!r}); decisions are machine-owned (ENT-DSD-DECISION)")
                    return cid
        raise ScopeRefusal("decision-file-not-in-corpus", f"{file!r} is not under any root of a registered corpus",
                           "pass a file of the machine's answers corpus")

    def stamp_decision(self, file: str, node_id: str, title: str, *, subjects: Sequence[Subject] = (), project: Optional[str] = None,
                       session: Optional[ScopeSession] = None, answered_by: str = "") -> Dict[str, Any]:
        """Write a DSD-decision node stamped by I9 precedence (tier 0 explicit / tier 1 subject / tier 2 session / tier 3
        none) into an answers-corpus file. It is a write-time SNAPSHOT: re-filing a subject later does not change it
        (it keeps ``subject-items`` for linkage). ``workspace`` is stored only at tier 3."""
        self._gate("stamp_decision")
        check_node_id(node_id)
        check_title(title)
        check_value("answered-by", answered_by)
        for s in subjects:
            check_value("subject id", s.id)
        corpus = self._decision_corpus_of(file)
        if self._need_corpora().find_node(corpus, node_id) is not None:       # F-R2-2: as create_item's item-exists
            raise ScopeRefusal("decision-exists", f"{node_id!r} already exists in corpus {corpus!r}: a second node with the same id would shadow the first's stamped scope",
                               "choose a new decision id")
        model = self.model()
        try:
            st = stamp(model, "dsd-decision", explicit=project, subjects=subjects, session=session)
        except ScopeRefusal as exc:
            self._emit(exc.events)
            raise
        self._emit(st.events)
        with open(file, "r", encoding="utf-8") as fh:
            text = fh.read()
        props = [(k, v) for k, v in st.record_fields().items() if k != "subject-items"]
        for sid in st.subject_ids:
            props.append(("subject-items", sid))
        if answered_by:
            props.append(("answered-by", answered_by))
        from .scopebase import atomic_write_text

        atomic_write_text(file, LtsFiles.append_node(text, f"## {title} [{node_id}]", props), enabled=self._enabled, config=self._config)
        return {"node": node_id, "project": st.project, "workspace": st.workspace, "tier": st.tier}


def retag_record(record: Mapping[str, Any], *, workspace_tag: Optional[str] = None, project_tag: Optional[str] = None) -> Dict[str, Any]:
    """Re-tag a machine-owned record that carries workspace/project TAGS (a scheduled job: I3). Tags are opaque
    write-time strings, exempt from I9; only their FORMAT is checked. Pure: returns the new record."""
    if workspace_tag is not None and not SLUG_RE.fullmatch(workspace_tag):
        raise ScopeRefusal("tag-malformed", f"workspace tag {workspace_tag!r} is not a slug")
    if project_tag is not None and not QUALIFIED_RE.fullmatch(project_tag):
        raise ScopeRefusal("tag-malformed", f"project tag {project_tag!r} is not a qualified <ws>/<slug> (I4)")
    out = dict(record)
    if workspace_tag is not None:
        out["workspace-tag"] = workspace_tag
    if project_tag is not None:
        out["project-tag"] = project_tag
    return out

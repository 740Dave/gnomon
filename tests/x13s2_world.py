"""A SYNTHETIC deployment for the X13 S2 tests (g31: tmp roots only; nothing here touches a live root).

Neutral names on purpose (source neutrality D1): workspaces ``wsa`` / ``wsb``, projects ``alpha`` / ``beta`` /
``gamma`` / ``delta``, roles ``role-one`` / ``role-two``. The file shapes are the ones the S1 scaffold writes
(``convert.write_scaffold``): machine.json, ``workspaces/<ws>/{workspace.json, projects.json, roles/}``, DATA
``topic-counter.json`` / ``prefix-registry.json`` / ``workspaces/<ws>/topics/``.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from gnomon.workspace import registry as prefix_registry
from gnomon.workspace.scope import ScopeSession
from gnomon.workspace.store import CorpusRegistry
from gnomon.workspace.writers import ScopeWriter

T0 = datetime(2026, 9, 30, 12, 0, 0, tzinfo=timezone.utc)


class Clock:
    """A deterministic clock: each call advances one second."""

    def __init__(self, start: datetime = T0) -> None:
        self.t = start

    def __call__(self) -> datetime:
        self.t = self.t + timedelta(seconds=1)
        return self.t


def _dump(path: str, obj) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
        fh.write("\n")


def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def tree_digest(*roots: str) -> str:
    """A content digest of every file under ``roots`` (path + bytes): 'nothing was written' is provable by equality."""
    h = hashlib.sha256()
    for root in roots:
        for d, dirs, files in os.walk(root):
            dirs.sort()
            for fn in sorted(files):
                p = os.path.join(d, fn)
                h.update(os.path.relpath(p, root).encode())
                with open(p, "rb") as fh:
                    h.update(fh.read())
    return h.hexdigest()


@dataclass
class World:
    root: str
    deploy: str
    data: str
    sessions: str
    intelligences: str
    corpus_config: str
    event_log: str
    home: str

    def corpora(self) -> CorpusRegistry:
        return CorpusRegistry(self.corpus_config)

    def writer(self, enabled: bool = True, clock=None) -> ScopeWriter:
        return ScopeWriter(self.deploy, self.data, event_log_path=self.event_log, corpora=self.corpora(),
                           clock=clock or Clock(), enabled=enabled)

    def roadmap_file(self) -> str:
        return os.path.join(self.root, "corpora", "a", "roadmap.lts")

    def research_file(self) -> str:
        return os.path.join(self.root, "corpora", "b", "research.lts")

    def answers_file(self) -> str:
        return os.path.join(self.root, "answers", "answers.lts")

    def session(self, name: str, *, current=None, launch_ws: str = "wsa", role_workspaces=("wsa",)) -> str:
        """A session dir with a scope-state.json (written through the real writer of it)."""
        from gnomon.workspace.scope import save_session_scope

        d = os.path.join(self.sessions, name)
        os.makedirs(os.path.join(d, ".claude"), exist_ok=True)
        save_session_scope(d, ScopeSession(launch_ws, current, tuple(role_workspaces)), enabled=True)
        return d

    def events(self) -> List[dict]:
        from gnomon.workspace.events import read_events

        return list(read_events(self.event_log))

    def project_state(self, ws: str, slug: str) -> str:
        with open(os.path.join(self.deploy, "workspaces", ws, "projects.json")) as fh:
            return {p["slug"]: p["state"] for p in json.load(fh)["projects"]}[slug]

    def set_project_state(self, ws: str, slug: str, state: str) -> None:
        p = os.path.join(self.deploy, "workspaces", ws, "projects.json")
        with open(p) as fh:
            obj = json.load(fh)
        for row in obj["projects"]:
            if row["slug"] == slug:
                row["state"] = state
        _dump(p, obj)


def _projects(ws: str, slugs: Dict[str, str]) -> dict:
    rows = [{"slug": "unassigned", "name": "Unassigned", "state": "active", "source": "reserved"}]
    rows += [{"slug": s, "name": s.title(), "state": st, "source": "test"} for s, st in slugs.items()]
    return {"schema": 1, "workspace": ws, "projects": rows}


def build_world(tmp_path) -> World:
    root = str(tmp_path)
    deploy, data = os.path.join(root, "deploy"), os.path.join(root, "data")
    sessions, intel = os.path.join(data, "sessions"), os.path.join(data, "intelligences")
    event_log = os.path.join(data, "events", "events.jsonl")
    cc = os.path.join(root, "corpus-config.json")
    home = os.path.join(root, "home")
    os.makedirs(home, exist_ok=True)
    _dump(os.path.join(deploy, "machine.json"), {
        "schema": 1, "machine-id": "test-machine", "timezone": "UTC", "operator-id": "op", "flags": {}, "bootstrap": {},
        "settings": {"machine_duty_role": "duty-role", "timezone": "UTC", "runtime_root": data}})   # event_log_path: the registry default, under the DATA root
    backlogs = {"wsa": [{"id": "roadmap", "label": "Roadmap", "corpus": "corp-a", "prefix": "RM", "root-node": "RM-ROOT", "operator-gate-value": "op"}],
                "wsb": [{"id": "research", "label": "Research", "corpus": "corp-b", "prefix": "RQ", "root-node": "RQ-ROOT", "operator-gate-value": "op"}]}
    projs = {"wsa": _projects("wsa", {"alpha": "active", "beta": "active", "gamma": "active"}),
             "wsb": _projects("wsb", {"delta": "active", "epsilon": "active"})}
    for i, ws in enumerate(("wsa", "wsb"), 1):
        base = os.path.join(deploy, "workspaces", ws)
        _dump(os.path.join(base, "workspace.json"), {"schema": 1, "id": ws, "display-name": ws.upper(), "order": i, "coordinating-architect": None,
                                                        "backlogs": backlogs[ws], "logo": None, "settings": {}, "state": "active"})
        _dump(os.path.join(base, "projects.json"), projs[ws])
        os.makedirs(os.path.join(base, "brand"), exist_ok=True)
        os.makedirs(os.path.join(data, "workspaces", ws, "topics"), exist_ok=True)
    _write(os.path.join(deploy, "workspaces", "wsa", "roles", "role-one.md"), "# Role one\n\nYou are role one.\n")
    _write(os.path.join(deploy, "workspaces", "wsb", "roles", "role-two.md"), "# Role two\n")
    _dump(os.path.join(data, "topic-counter.json"), {"next": 1})
    declared = {"RM": {"owner": "wsa/roadmap", "kind": "backlog", "corpus": "corp-a"}, "RQ": {"owner": "wsb/research", "kind": "backlog", "corpus": "corp-b"}}
    _dump(os.path.join(data, "prefix-registry.json"), prefix_registry.build_registry(declared, {"corp-a": {"RM": 2}, "corp-b": {"RQ": 1}, "corp-x": {"XFAM": 3}}, {}))
    os.makedirs(os.path.join(intel, "role-one", "personality"), exist_ok=True)
    _write(os.path.join(intel, "role-one", "personality", "lifelog.lts"), "# Life [ONE-LIFELOG]\n\n- note: x\n")
    _write(os.path.join(root, "corpora", "a", "roadmap.lts"),
           "# Roadmap [RM-ROOT]\n\n- status: ready\n\n## Item one [RM-1]\n\n- project: wsa/alpha\n- status: ready\n\n## Item two [RM-2]\n\n- project: wsa/alpha\n- status: done\n")
    _write(os.path.join(root, "corpora", "b", "research.lts"), "# Research [RQ-ROOT]\n\n- status: ready\n\n## Q one [RQ-1]\n\n- project: wsb/delta\n- status: ready\n")
    _write(os.path.join(root, "corpora", "shared", "shared.lts"), "# Shared [SH-ROOT]\n\n- status: ready\n")
    _write(os.path.join(root, "answers", "answers.lts"), "# Answers [ANS-ROOT]\n\n- status: ready\n")
    _dump(cc, {
        "corp-a": {"roots": [{"path": os.path.join(root, "corpora", "a"), "area": "specs"}], "owner": "workspace:wsa"},
        "corp-b": {"roots": [{"path": os.path.join(root, "corpora", "b"), "area": "specs"}], "owner": "workspace:wsb"},
        "corp-shared": {"roots": [{"path": os.path.join(root, "corpora", "shared"), "area": "specs"}], "owner": "machine"},
        "corp-answers": {"roots": [{"path": os.path.join(root, "answers"), "area": "specs"}], "owner": "machine"}})
    return World(root, deploy, data, sessions, intel, cc, event_log, home)


def launched_session(w: World, instance: str = "role-one") -> str:
    """A session dir produced by the REAL LaunchManager with the scope core ON (so it holds a real relaunch bundle), made
    resumable the way tests/test_relaunch.py's ``session`` fixture makes one. Returns the session dir."""
    import time

    from test_relaunch import END_TURN, USER, _write_transcript
    from gnomon.config import GnomonConfig
    from gnomon.launch import LaunchManager
    from gnomon.profile import Profile
    from gnomon.workspace.scopebase import SCOPE_CORE_FLAG

    src = os.path.join(w.root, "role-source.md")
    _write(src, "# Role one\n\nYou are role one.\n")
    cfg = GnomonConfig(deploy_root=w.deploy, flags={SCOPE_CORE_FLAG: True})
    res = LaunchManager(sessions_root=w.sessions, intelligences_root=w.intelligences, config=cfg, clock=Clock()).launch(
        Profile(instance_id=instance, claude_md_source=src))
    sd = res.session_dir
    tr = os.path.join(w.root, "proj", "RID.jsonl")
    os.makedirs(os.path.dirname(tr), exist_ok=True)
    _write_transcript(tr, [USER, END_TURN])
    cdir = os.path.join(sd, ".claude")
    _dump(os.path.join(cdir, "context_state.json"), {"used_percentage": 50.0, "transcript_path": tr, "written_epoch": time.time()})
    lj = json.load(open(os.path.join(cdir, "launch.json")))
    lj.update({"resume_session_id": "RID", "resume_cwd": "/x"})
    _dump(os.path.join(cdir, "launch.json"), lj)
    _write(os.path.join(cdir, "injection_log.jsonl"), "")
    return sd


def use_machine_config(monkeypatch, path: str) -> None:
    """Point BOTH machine-config readers at ``path``: ``flags.flag``'s default store (the conftest pins it to an absent file) and
    ``ConfigStore()``'s default path (read directly by relaunch's refusal alert)."""
    from gnomon import config as cfgmod, flags

    monkeypatch.setattr(cfgmod, "DEFAULT_CONFIG_PATH", path)
    monkeypatch.setattr(flags, "_config_store", lambda: cfgmod.ConfigStore(path))

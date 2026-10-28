"""the emitter, and each safety-path hook that calls it, open nothing under a
workspace tree, and the hook verdicts are byte-identical to the emitter-disabled run (g5)."""

import builtins
import io
import json
import os

import pytest

from fu_support import INSTANCE_ID, read_events, session_dir
from gnomon import feature_events as FE
from gnomon.harness import deny_guard, lifecycle_hook, posttool_hook


class _Trace:
    def __init__(self):
        self.paths = []

    def install(self, monkeypatch):
        real_open, real_os_open = builtins.open, os.open

        def t_open(file, *a, **k):
            self.paths.append(os.fspath(file) if not isinstance(file, int) else "<fd>")
            return real_open(file, *a, **k)

        def t_os_open(path, *a, **k):
            self.paths.append(os.fspath(path))
            return real_os_open(path, *a, **k)

        monkeypatch.setattr(builtins, "open", t_open)
        monkeypatch.setattr(os, "open", t_os_open)


def _world(tmp_path):
    """DEPLOY and DATA with readable workspaces trees; a session dir whose scope state names a project in one"""
    deploy, data = tmp_path / "deploy", tmp_path / "data"
    for root in (deploy, data):
        (root / "workspaces" / "wsa").mkdir(parents=True)
        (root / "workspaces" / "wsa" / "projects.json").write_text("[]")
    sd = session_dir(tmp_path, scope={"schema": 1, "launch-workspace": "wsa", "current-project": "wsa/p1"})
    return deploy, data, sd


def _hooks(sd):
    """each safety-path hook, driven in-process; returns its verdicts"""
    out = {}
    out["deny-allow"] = deny_guard.main(io.StringIO(json.dumps({"tool_input": {"command": "ls"}})), io.StringIO())
    out["deny-block"] = deny_guard.main(io.StringIO(json.dumps({"tool_input": {"command": "sudo ls"}})), io.StringIO())
    so = io.StringIO()
    out["pre-compact"] = (lifecycle_hook.main(["--event", "pre-compact"], io.StringIO("{}"), so), so.getvalue())
    so = io.StringIO()
    out["session-start"] = (lifecycle_hook.main(["--event", "post-compact"], io.StringIO("{}"), so), so.getvalue())
    out["post-tool"] = posttool_hook.main(["--socket", os.path.join(sd, "no-such.sock")], io.StringIO("{}"), io.StringIO())
    return out


def test_no_workspaces_open(tmp_path, monkeypatch):
    deploy, data, sd = _world(tmp_path)
    monkeypatch.setenv("GNOMON_SESSION_DIR", sd)
    tr = _Trace()
    with monkeypatch.context() as m:
        tr.install(m)
        assert FE.feature_event("deny-guard", None, "hook", outcome="ok")
        _hooks(sd)
    assert tr.paths, "the trace saw nothing: the test is not tracing"
    assert not [p for p in tr.paths if os.sep + "workspaces" + os.sep in p or p.rstrip(os.sep).endswith("workspaces")], \
        [p for p in tr.paths if "workspaces" in p]
    ev = read_events()
    assert {"deny-guard", "pre-compact-hook", "session-start-hook", "post-tool-use-hook"} <= {e["feature_id"] for e in ev}


def test_tracing_can_see_a_workspaces_open(tmp_path, monkeypatch):
    """negative control: the tracer MUST fail if something did open a workspaces path"""
    deploy, data, sd = _world(tmp_path)
    tr = _Trace()
    with monkeypatch.context() as m:
        tr.install(m)
        open(str(deploy / "workspaces" / "wsa" / "projects.json")).close()
    assert [p for p in tr.paths if os.sep + "workspaces" + os.sep in p]


def test_trees_removed_and_state_corrupted_the_verdicts_are_identical_and_scope_is_null(tmp_path, monkeypatch):
    deploy, data, sd = _world(tmp_path)
    monkeypatch.setenv("GNOMON_SESSION_DIR", sd)
    # the emitter-DISABLED reference run
    with monkeypatch.context() as m:
        m.setattr(deny_guard, "_emit_feature_event", lambda outcome: None)
        m.setattr(posttool_hook, "_emit_feature_event", lambda outcome: None)
        m.setattr(lifecycle_hook, "_emit_feature_event", lambda *a: None)
        ref = _hooks(sd)
    import shutil

    shutil.rmtree(deploy)
    shutil.rmtree(data)
    with open(os.path.join(sd, ".claude", "scope-state.json"), "w") as fh:
        fh.write("{ this is not json")
    got = _hooks(sd)
    assert got == ref
    ev = read_events()
    assert ev and all(e["workspace"] is None and e["project"] is None for e in ev)


def test_a_hook_verdict_survives_a_dead_emitter(tmp_path, monkeypatch):
    monkeypatch.setenv("GNOMON_SESSION_DIR", session_dir(tmp_path))
    monkeypatch.setitem(__import__("sys").modules, "gnomon.feature_events", None)   # import fails
    assert deny_guard.main(io.StringIO(json.dumps({"tool_input": {"command": "sudo ls"}})), io.StringIO()) == 2
    assert deny_guard.main(io.StringIO(json.dumps({"tool_input": {"command": "ls"}})), io.StringIO()) == 0

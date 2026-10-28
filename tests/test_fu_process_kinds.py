"""process-kinds (R2): the closed kind table, and each process declares its own kind (g24(a)).

web = the :8770 process (it also hosts the hub); harness = the harness process; hooks = every hook entry point;
cli = command-line tools (the default)."""

import io
import json
import os

from fu_support import event_files, read_events, session_dir
from gnomon import feature_events as FE


def _names():
    return sorted(os.path.basename(p) for p in event_files())


def test_each_hook_entry_point_is_a_hooks_kind_process(tmp_path, monkeypatch):
    from gnomon.harness import deny_guard, lifecycle_hook, posttool_hook, prompt_command_hook

    monkeypatch.setenv("GNOMON_SESSION_DIR", session_dir(tmp_path))
    assert FE.process_kind() == "cli"
    deny_guard.main(io.StringIO(json.dumps({"tool_input": {"command": "ls"}})), io.StringIO())
    FE._reset_for_tests()
    lifecycle_hook.main(["--event", "pre-compact"], io.StringIO("{}"), io.StringIO())
    FE._reset_for_tests()
    lifecycle_hook.main(["--event", "post-compact"], io.StringIO("{}"), io.StringIO())
    FE._reset_for_tests()
    posttool_hook.main(["--socket", str(tmp_path / "x")], io.StringIO("{}"), io.StringIO())
    FE._reset_for_tests()
    prompt_command_hook.main([], io.StringIO(json.dumps({"prompt": "hi"})), io.StringIO())
    assert _names() == ["hooks.jsonl"] and len(read_events()) == 5


def test_the_harness_declares_itself_and_its_session(tmp_path, monkeypatch):
    from gnomon.harness import launcher

    sd = session_dir(tmp_path)
    monkeypatch.setattr("gnomon.feature_sites.warm_registry", lambda: True)
    launcher._init_feature_events(sd)
    assert FE.process_kind() == "harness"
    assert FE.feature_event("deny-guard", None, "hook", outcome="ok")          # actor derived from the session set above
    ev = read_events(kind="harness")
    assert len(ev) == 1 and ev[0]["session_run"] == os.path.basename(sd)


def test_launcher_init_never_raises(monkeypatch):
    from gnomon.harness import launcher

    monkeypatch.setattr(FE, "set_process_kind", lambda k: (_ for _ in ()).throw(RuntimeError("x")))
    launcher._init_feature_events("/nowhere")          # must not raise: telemetry never fails a launch


def test_the_web_backend_declares_the_web_kind():
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web"))
    import backend.server as srv

    FE._reset_for_tests()
    from fastapi.testclient import TestClient

    TestClient(srv.app).get("/api/flags")
    assert _names() == ["web.jsonl"]


def test_the_self_mcp_server_kind_is_one_named_constant():
    assert FE.SELF_MCP_PROCESS_KIND in FE.KINDS and FE.SELF_MCP_PROCESS_KIND == "harness"


def test_the_kind_set_is_closed():
    assert FE.KINDS == ("hooks", "harness", "web", "cli") and FE.DEFAULT_KIND in FE.KINDS

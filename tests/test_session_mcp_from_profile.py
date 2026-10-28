"""Hub-MCP wiring fix: session_mcp_from_profile (default OFF).

Investigation: the hub-MCP and tool-limits investigation of 2026-10-01, part 1. Every test builds the REAL harness.mcp.json through ``prepare_child_launch`` (what ``--mcp-config``
points at) and reads it back; each has a negative control (the same fixture with the other flag state, or a
runnable/live variant) so the assertion can fail.
"""

import json
import os
import socket
import sys

import pytest

from gnomon import flags as flags
from gnomon.harness import launcher as L
from gnomon.harness import session_mcp as SM

FLAG = "session_mcp_from_profile"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GNOMON_RUN_DIR", str(tmp_path / "run"))
    sess = tmp_path / "sess"
    (sess / ".claude").mkdir(parents=True)
    return tmp_path, home, sess


@pytest.fixture
def live_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(4)
    yield s.getsockname()[1]
    s.close()


def _dead_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def _profile_mcp(sess, servers):
    (sess / ".claude" / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))


def _rendezvous(tmp_path, port, binding="on", name="rv.json"):
    """A hub discovery file. profile delivery needs the HUB to publish binding: on, so the
    default fixture says on; ``binding=None`` writes no key (a hub with the flag OFF)."""
    p = tmp_path / name
    rec = {"schema": 1, "rendezvous_host": "127.0.0.1", "rendezvous_port": 1,
           "hub_instance_id": "h", "hub_pid": 1, "started_epoch": 1.0}
    if port is not None:
        rec["mcp_url"] = f"http://127.0.0.1:{port}/mcp"
    if binding is not None:
        rec["binding"] = binding
    p.write_text(json.dumps(rec))
    os.chmod(p, 0o600)
    return str(p)


def _mcp_paths(argv):
    """Every file passed to the variadic --mcp-config (up to the next flag or ``--``)."""
    i = argv.index("--mcp-config") + 1
    out = []
    while i < len(argv) and not argv[i].startswith("-"):
        out.append(argv[i])
        i += 1
    return out


def _launch(sess, rv, on, **kw):
    """Launch through the REAL prepare_child_launch. ``rv=None`` means "a binding-on hub that is not running"
    (so delivery is not refused for want of a hub, and no hub url resolves). Returns the argv, the MERGED servers
    of every --mcp-config file, and the raw bytes of harness.mcp.json (the first file)."""
    if rv is None:
        rv = _rendezvous(sess.parent, None, name="rv-no-hub.json")
    cfg = {"claude_bin": "c", "bootstrap_prompt": "p"}
    argv, listener = L.prepare_child_launch(cfg, str(sess), python_bin="/py/bin/python3",
                                            session_mcp_from_profile=on, rendezvous_file=rv, **kw)
    try:
        paths = _mcp_paths(argv)
        servers = {}
        for path in paths:
            servers.update(json.loads(open(path).read())["mcpServers"])
        return argv, servers, open(paths[0], "rb").read()
    finally:
        listener.close()
        L.cleanup_caller_credential(cfg)


def _runnable(tmp_path):
    exe = tmp_path / "fakeserver"
    exe.write_text("#!/bin/sh\n")
    os.chmod(exe, 0o755)
    return str(exe)


# --- registry ---------------------------------------------------------------

def test_flag_registered_default_off():
    assert flags.KNOWN_FLAGS[FLAG].default is False
    assert L.resolve_harness_settings({}, "/nonexistent-session")["session_mcp_from_profile"] is False
    on = L.resolve_harness_settings({"flags": {FLAG: True}}, "/nonexistent-session")
    assert on["session_mcp_from_profile"] is True  # negative control: the flag is read


# --- OFF: byte-identical to today ---------------------------------------------

def test_off_config_is_byte_identical_even_when_profile_lists_the_hub(env, live_port):
    tmp_path, _home, sess = env
    _profile_mcp(sess, {"gnomon": {"type": "http", "url": f"http://127.0.0.1:{live_port}/mcp"}})
    rv = _rendezvous(tmp_path, live_port)
    _a, servers_off, raw_off = _launch(sess, rv, False)
    assert list(servers_off) == ["gnomon-self"]
    # byte-identical to what write_self_mcp_config produces with no extras at all
    plain = tmp_path / "plain"
    (plain / ".claude").mkdir(parents=True)
    path = L.write_self_mcp_config(str(plain), "/py/bin/python3", json.loads(raw_off)["mcpServers"]
                                   ["gnomon-self"]["args"][-1])
    assert open(path, "rb").read() == raw_off
    # negative control: ON delivers the hub (in a SECOND --mcp-config file: the 0600 caller credential)
    argv_on, servers_on, raw_on = _launch(sess, rv, True)
    assert "gnomon" in servers_on and len(_mcp_paths(argv_on)) == 2
    assert len(_mcp_paths(_a)) == 1


def test_extra_servers_none_or_empty_changes_nothing(tmp_path):
    a = L.write_self_mcp_config(str(tmp_path), "py", "/sock")
    base = open(a, "rb").read()
    b = L.write_self_mcp_config(str(tmp_path), "py", "/sock", extra_servers={})
    assert open(b, "rb").read() == base
    c = L.write_self_mcp_config(str(tmp_path), "py", "/sock", extra_servers={"x": {"type": "http", "url": "u"}})
    assert open(c, "rb").read() != base  # negative control


def test_extra_servers_cannot_replace_the_self_server(tmp_path):
    p = L.write_self_mcp_config(str(tmp_path), "py", "/sock",
                                extra_servers={"gnomon-self": {"type": "http", "url": "evil"}})
    assert json.loads(open(p).read())["mcpServers"]["gnomon-self"]["type"] == "stdio"


# --- ON: hub at the CURRENT port ----------------------------------------------

def test_on_includes_hub_at_the_current_port_not_the_provision_time_one(env, live_port):
    tmp_path, _h, sess = env
    stale = _dead_port()
    _profile_mcp(sess, {"gnomon": {"type": "http", "url": f"http://127.0.0.1:{stale}/mcp"}})
    _argv, servers, _raw = _launch(sess, _rendezvous(tmp_path, live_port), True)
    hub = servers["gnomon"]
    assert (hub["type"], hub["url"]) == ("http", f"http://127.0.0.1:{live_port}/mcp")
    assert list(hub["headers"]) == ["X-Gnomon-Caller"]  # the per-launch credential
    assert "gnomon-self" in servers
    # negative control: if the hub restarted on another live port, the NEW one wins
    s2 = socket.socket(); s2.bind(("127.0.0.1", 0)); s2.listen(1)
    try:
        newp = s2.getsockname()[1]
        _a, servers2, _r = _launch(sess, _rendezvous(tmp_path, newp), True)
        assert servers2["gnomon"]["url"] == f"http://127.0.0.1:{newp}/mcp"
    finally:
        s2.close()


def test_hub_down_is_skipped_with_a_loud_warning_and_a_provenance_record(env, capsys):
    tmp_path, _h, sess = env
    dead = _dead_port()
    _profile_mcp(sess, {"gnomon": {"type": "http", "url": f"http://127.0.0.1:{dead}/mcp"}})
    _a, servers, _r = _launch(sess, _rendezvous(tmp_path, dead), True)
    assert "gnomon" not in servers
    err = capsys.readouterr().err
    assert "session-mcp gnomon: skip" in err and "refused a connection" in err
    rec = [json.loads(x) for x in open(sess / ".claude" / "injection_log.jsonl")]
    wiring = [r for r in rec if r.get("event") == "session-mcp-wiring"][0]
    assert wiring["profile_servers"] == ["gnomon"] and wiring["delivered"] == []


def test_hub_inclusion_states_the_caller_binding_bound(env, live_port, capsys):
    """REPLACES test_hub_inclusion_carries_the_identity_blocker_warning: the hub is now
    delivered only to a binding hub, so the warning states the binding's bound instead of the old blocker."""
    tmp_path, _h, sess = env
    _profile_mcp(sess, {"gnomon": {"type": "http", "url": "x"}})
    _launch(sess, _rendezvous(tmp_path, live_port), True)
    err = capsys.readouterr().err
    assert "per-launch caller credential" in err and "does NOT stop a same-uid process" in err
    assert "does NOT identify the calling session" not in err


# --- unresolvable / duplicate servers -------------------------------------------

def test_unresolvable_stdio_is_skipped_with_warning_runnable_is_included(env, capsys):
    tmp_path, _h, sess = env
    good = _runnable(tmp_path)
    _profile_mcp(sess, {"missing-tool": {"type": "stdio", "command": "missing-tool-not-a-binary-xyz"},
                        "okserver": {"type": "stdio", "command": good, "_note": "x"}})
    _a, servers, _r = _launch(sess, None, True)
    assert "missing-tool" not in servers
    assert servers["okserver"] == {"type": "stdio", "command": good}  # _note stripped; negative control
    assert "session-mcp missing-tool: skip: stdio command 'missing-tool-not-a-binary-xyz' is not a runnable" \
        in capsys.readouterr().err


def test_placeholder_hub_entry_with_no_url_is_skipped_loudly(env, capsys):
    _t, _h, sess = env
    _profile_mcp(sess, {"gnomon": {"type": "http", "url": None, "_note": "wired at launch"}})
    _a, servers, _r = _launch(sess, _rendezvous(_t, None), True)  # a binding hub that publishes no mcp_url
    assert "gnomon" not in servers
    assert "no hub url resolvable" in capsys.readouterr().err


def test_server_already_provided_by_user_scope_is_not_duplicated(env, capsys):
    tmp_path, home, sess = env
    good = _runnable(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.local.json").write_text(json.dumps({"mcpServers": {"lts-engine": {"command": "x"}}}))
    (home / ".claude.json").write_text(json.dumps({"mcpServers": {"kicad": {}}}))
    _profile_mcp(sess, {"lts-engine": {"type": "stdio", "command": good}, "kicad": {"type": "stdio", "command": good},
                        "other": {"type": "stdio", "command": good}})
    _a, servers, _r = _launch(sess, None, True)
    assert "lts-engine" not in servers and "kicad" not in servers
    assert "other" in servers  # negative control: a server no scope provides IS delivered
    err = capsys.readouterr().err
    assert "already provided by user scope (~/.claude/settings.local.json)" in err


def test_unreadable_user_scope_file_is_reported_not_swallowed(env, capsys):
    tmp_path, home, sess = env
    (home / ".claude.json").write_text("{not json")
    _profile_mcp(sess, {"s": {"type": "stdio", "command": _runnable(tmp_path)}})
    _launch(sess, None, True)
    assert "could not read" in capsys.readouterr().err


# --- the checklist rule: profile mcp_servers vs the generated config -------------------

def _diff(profile_names, servers, events):
    skipped = {e["server"] for e in events if e["action"] == "skip"}
    return [n for n in profile_names if n not in servers and n not in skipped]


def test_every_profile_server_is_delivered_or_has_a_skip_reason(env, live_port):
    tmp_path, home, sess = env
    good = _runnable(tmp_path)
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.local.json").write_text(json.dumps({"mcpServers": {"lts-engine": {}}}))
    _profile_mcp(sess, {
        "lts-engine": {"type": "stdio", "command": "lts-engine"},
        "missing-tool": {"type": "stdio", "command": "nope-nope-xyz"},
        "gnomon": {"type": "http", "url": "stale"},
        "fine": {"type": "stdio", "command": good},
    })
    extra, events, names = SM.resolve_session_servers(str(sess), rendezvous_file=_rendezvous(tmp_path, live_port),
                                                       home=str(home))
    assert sorted(names) == ["fine", "gnomon", "lts-engine", "missing-tool"]
    assert _diff(names, extra, events) == []
    assert sorted(extra) == ["fine", "gnomon"]
    # negative control: the diff catches a silently dropped server
    assert _diff(names, {k: v for k, v in extra.items() if k != "fine"}, [e for e in events if e["server"] != "fine"]) == ["fine"]


def test_missing_profile_mcp_file_warns_and_adds_nothing(env, capsys):
    _t, _h, sess = env
    _a, servers, _r = _launch(sess, None, True)
    assert list(servers) == ["gnomon-self"]
    assert "nothing to add" in capsys.readouterr().err


# --- hook reachability (g47/g48e) -------------------------------------------------

def test_flag_off_never_imports_session_mcp(env):
    import subprocess
    code = ("import sys,json,os;from gnomon.harness import launcher as L;"
            "L.resolve_harness_settings({}, '/nonexistent');"
            "print('gnomon.harness.session_mcp' in sys.modules)")
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert out.stdout.strip() == "False", out.stderr


@pytest.mark.parametrize("hook", ["deny_guard", "posttool_hook", "lifecycle_hook", "prompt_command_hook",
                                  "statusline_hook", "subagent_stop_hook"])
def test_no_hook_entry_loads_session_mcp(hook):
    import subprocess
    code = (f"import sys,importlib;importlib.import_module('gnomon.harness.{hook}');"
            "print('gnomon.harness.session_mcp' in sys.modules)")
    out = subprocess.run([sys.executable, "-B", "-c", code], capture_output=True, text=True,
                         env=dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    assert out.stdout.strip() == "False", out.stderr


# --- the operator blocker's tripwire: REPLACED by (acceptance 9)
#
# The original tripwire (test_tripwire_hub_does_not_bind_the_caller_identity) passed while ANY caller could file a
# DSD under any registered instance_id, and was built to FAIL once the hub binds callers. It now fails against a
# binding hub, so it is replaced by tests/test_hub_caller_binding.py::test_a9_old_tripwire_fails_against_a_binding_hub,
# which runs the old assertion against a binding hub (and must see it fail) and pins the new behaviour.

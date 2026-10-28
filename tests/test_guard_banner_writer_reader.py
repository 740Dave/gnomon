"""The WRITER and the READER of the deny guard's start-up failure log agree: the REAL hook line (gnomon/harness/launcher.py,
deny_guard_hook_settings) is run by /bin/sh with an interpreter that cannot start, and what it appends is read by the banner's reader. This is the
tie that makes a later change to the hook line's record format (slice 2 adds the session's id) fail HERE if guard_banner.LINE_RE is not changed with it."""
import os
import subprocess

import pytest

from gnomon import guard_banner as gb
from gnomon import config as gm_config
from gnomon.harness import launcher


def _run_hook(tmp_path, monkeypatch, python_bin, payload=b"{}"):
    # the writer under test is the OFF variant (X79): it ALLOWS and leaves a mode=allowed line; the ON variant is pinned in test_x79_*
    home = tmp_path / "home"
    root = home / "data"
    (home).mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(gm_config, "runtime_root_default", lambda: str(root))
    command = launcher.deny_guard_hook_settings(python_bin, fail_closed=False)["PreToolUse"][0]["hooks"][0]["command"]
    env = dict(os.environ, HOME=str(home))
    r = subprocess.run(["/bin/sh", "-c", command], input=payload, capture_output=True, env=env, timeout=30)
    return r, root / "deny-guard" / "startup-failures.log"


def test_a_guard_that_cannot_start_is_allowed_and_leaves_a_line_the_banner_reads(tmp_path, monkeypatch):
    r, log = _run_hook(tmp_path, monkeypatch, "/nonexistent/python3-for-the-banner-test")
    assert r.returncode == 0                                               # allowed, on purpose (the defect's own text)
    assert log.is_file()
    line = log.read_text().splitlines()
    assert len(line) == 1 and line[0].endswith(" rc=127 class=interpreter mode=allowed count=1")
    s = gb.read_state(str(log))                                           # the real clock: the line was written just now
    assert s["state"] == "failures" and s["count"] == 1
    assert s["text"].startswith("A deny guard could not start: commands in at least one session went through unchecked. 1 time(s) in the last 24 hours; most recent ")
    # a second failure is counted (the writer writes an identical failure of one session at most once a minute, so the session's
    # state file is removed to make this a different session)
    for st in log.parent.glob(".startup-state-*"):
        st.unlink()
    r2 = subprocess.run(["/bin/sh", "-c", launcher.deny_guard_hook_settings("/nonexistent/python3-for-the-banner-test", fail_closed=False)["PreToolUse"][0]["hooks"][0]["command"]],
                        input=b"{}", capture_output=True, env=dict(os.environ, HOME=str(tmp_path / "home")), timeout=30)
    assert r2.returncode == 0 and gb.read_state(str(log))["count"] == 2


def test_a_healthy_guard_leaves_no_log(tmp_path, monkeypatch):
    # positive control for the line above: the same hook line, a working interpreter and a harmless input, writes nothing
    import sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    monkeypatch.setenv("PYTHONPATH", here)
    r, log = _run_hook(tmp_path, monkeypatch, sys.executable)
    assert r.returncode == 0 and not log.exists(), (r.returncode, r.stderr[-300:])
    # and the guard really ran: it REFUSES a catastrophic command (exit 2, which is not a failure to start) and still leaves no log
    (tmp_path / "second").mkdir()
    r2, log2 = _run_hook(tmp_path / "second", monkeypatch, sys.executable, b'{"tool_name": "Bash", "tool_input": {"command": "sudo true"}}')
    assert r2.returncode == 2 and not log2.exists()

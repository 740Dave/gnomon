"""Delta F2 (g48b(a)): an unusable roots file must never write to a HOOK's stderr (the guard is in the config module's closure).
The fault is reported through the logger (the hook entry silences logging); a long-lived process with no handler still sees it."""
import json
import logging
import os
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _guard(home, payload, roots):
    """The REAL entry with a minimal environment and the given roots file; returns (rc, stderr bytes)."""
    env = {"PATH": "/usr/bin:/bin", "HOME": home, "PYTHONPATH": ROOT, "PYTHONDONTWRITEBYTECODE": "1", "GNOMON_CONFIG": roots,
           "GNOMON_SESSION_DIR": os.path.join(home, "sess"), "GNOMON_INSTANCE_ID": "probe"}
    r = subprocess.run([sys.executable, "-m", "gnomon.harness.deny_guard"], input=json.dumps(payload).encode(), capture_output=True,
                       cwd=home, env=env, timeout=120)
    return r.returncode, r.stderr


@pytest.mark.parametrize("kind", ["unreadable", "malformed"])
def test_the_guard_writes_nothing_to_stderr_on_an_allow_and_no_roots_line_on_a_deny(tmp_path, kind):
    home = str(tmp_path)
    os.makedirs(os.path.join(home, "sess"))
    roots = os.path.join(home, "roots.toml")
    with open(roots, "wb") as fh:
        fh.write(b'data_root = = "x\n\x00\xff' if kind == "malformed" else b'data_root = "/x"\n')
    if kind == "unreadable":
        os.chmod(roots, 0)
    try:
        allow = _guard(home, {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": home}, roots)
        deny = _guard(home, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}, "cwd": home}, roots)
    finally:
        os.chmod(roots, 0o600)
    assert allow[0] == 0 and allow[1] == b""
    assert deny[0] == 2 and b"gnomon roots" not in deny[1] and deny[1].startswith(b"gnomon deny-guard")


def test_the_fault_is_still_reported_through_the_logger(tmp_path, monkeypatch, caplog):
    from gnomon import config as C
    from gnomon import roots as R

    cfg = tmp_path / "bad.toml"
    cfg.write_bytes(b"data_root = = \x00")
    monkeypatch.setenv(R.ENV_CONFIG, str(cfg))
    monkeypatch.setenv("HOME", str(tmp_path))
    R.reset_for_tests()
    C._reported_roots.clear()
    try:
        with caplog.at_level(logging.WARNING, logger=C.__name__):
            assert C._adopted_roots() is None
        assert any("IGNORING an unusable roots file" in rec.getMessage() for rec in caplog.records)
    finally:
        R.reset_for_tests()
        C._reported_roots.clear()

"""confirm-proof (g76): the harness deny layer refuses an agent shell call that points an HTTP
client at the human-confirm routes (DSD submit; Apps confirm / settings), without touching reads of the path.
FRAME: stops an over-eager agent, not a hostile same-uid process (a script file that already holds the call, etc.)."""

import json
import os
import subprocess
import sys

import pytest

from gnomon.harness import deny_guard as G

BLOCK = [
    "curl -X POST http://127.0.0.1:8770/api/dsd/submit/dsd-1a2b3c4d5e2 -H 'Content-Type: application/json' -d @a.json",
    "curl -sS -d '{}' localhost:8770/api/dsd/submit/x",
    "wget -qO- --post-data='{}' http://127.0.0.1:8770/api/dsd/submit/x",
    "http POST :8770/api/dsd/submit/x selections:={}",
    "xh post 127.0.0.1:8770/api/dsd/submit/x",
    "python3 -c \"import urllib.request as u; u.urlopen('http://127.0.0.1:8770/api/dsd/submit/x', b'{}')\"",
    "python -c 'import requests; requests.post(\"http://localhost:8770/api/dsd/submit/x\", json={})'",
    "node -e \"fetch('http://127.0.0.1:8770/api/dsd/submit/x',{method:'POST'})\"",
    "ruby -rnet/http -e 'Net::HTTP.post(URI(\"http://127.0.0.1:8770/api/dsd/submit/x\"),\"{}\")'",
    "sh -c 'curl -X POST http://127.0.0.1:8770/api/dsd/submit/x'",
    "bash -c \"echo hi; wget http://127.0.0.1:8770/api/dsd/submit/x\"",
    "echo $(curl -X POST localhost:8770/api/dsd/submit/x)",
    "cd /tmp && true; curl -X POST http://[::1]:8770/api/dsd/submit/x",
    "curl -X POST http://127.0.0.1:8770/api/dsd/'sub'\"mit\"/x",
    "P=/api/dsd; curl -X POST 127.0.0.1:8770${P}/submit/x",
    "curl -X POST http://127.0.0.1:8770/api/dsd%2Fsubmit/x",
    "curl -X POST http://127.0.0.1:8770/API/DSD/SUBMIT/x",
    "curl -X POST http://127.0.0.1:8770/api/dsd/submit/x 'unterminated",         # lexer fails: raw-text rule still holds
    "cat <<EOF | python3\nimport urllib.request\nurllib.request.urlopen('http://127.0.0.1:8770/api/dsd/submit/x')\nEOF",
    "curl -X POST http://127.0.0.1:8770/api/apps/identity-adjudicator/confirm -H 'Content-Type: application/json' -d '{}'",
    "curl -X POST localhost:8770/api/apps/foo/settings -d '{}'",
    "env FOO=1 nohup curl -X POST http://127.0.0.1:8770/api/dsd/submit/x &",
]
ALLOW = [
    'grep -rn "/api/dsd/submit" web/',
    "/usr/bin/grep -n 'api/dsd/submit' web/backend/server.py",
    "sed -n 1214,1230p web/backend/server.py   # @app.post(\"/api/dsd/submit/{dsd_id}\")",
    "cat specs/03-gnomon-dsd.lts | head",
    "curl -s http://127.0.0.1:8770/api/dsd/list",
    "curl -s http://127.0.0.1:8770/api/dsd/present/dsd-1",
    "curl -s http://127.0.0.1:8770/api/apps",
    "python3 -m pytest tests/test_web_dsd_api.py -q",
    "git commit -m 'refuse a DSD submit without activity'",
    "ls", "rm -rf ./build",
]


@pytest.mark.parametrize("cmd", BLOCK)
def test_agent_http_call_to_a_human_route_is_blocked(cmd):
    reason = G.decide(cmd)
    assert reason and reason.startswith(G.ROUTE_TAG), (cmd, reason)


@pytest.mark.parametrize("cmd", ALLOW)
def test_reading_the_route_or_other_calls_are_allowed(cmd):
    assert G.decide(cmd) is None, cmd


def test_oversize_command_is_still_checked():
    cmd = "curl -X POST localhost:8770/api/dsd/submit/x # " + "a" * (G.MAX_ANALYSE_CHARS + 10)
    assert G.decide(cmd)


def test_the_block_message_names_g76_not_the_catastrophic_list():
    msg = G.block_message(G.decide(BLOCK[0]))
    assert "deny-guard: BLOCKED" in msg and "g76" in msg and "DSD" in msg and "sudo" not in msg


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run(cmd):
    # Pin THIS tree: an earlier test may chdir, and a bare ``-m gnomon...`` would then import the editable
    # install (the live repo) and test the wrong code (measured in the full run, 2026-10-03).
    env = dict(os.environ, PYTHONPATH=ROOT)
    return subprocess.run([sys.executable, "-m", "gnomon.harness.deny_guard"], cwd=ROOT, env=env,
                          input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
                          capture_output=True, text=True, timeout=30)


def test_the_real_hook_process_blocks_with_exit_2(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)          # the failure mode: a different cwd must not change which tree is tested
    p = _run(BLOCK[0])
    assert p.returncode == 2 and "g76" in p.stderr
    assert _run(ALLOW[0]).returncode == 0


def test_the_catastrophic_rules_are_unchanged():
    assert G.decide("rm -rf /") and not G.decide("rm -rf /").startswith(G.ROUTE_TAG)
    assert G.selftest() == 0

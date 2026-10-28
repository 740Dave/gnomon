"""operator ruling (2026-10-02; X43 Diamond F3): refuse a leading '!'
from non-operator sources (self-verb / hub / agent / dsd-answer injects). Claude Code runs a
prompt starting '!' as a shell command, outside the Bash tool and its deny layer.

Operator input (the signed 'operator-broadcast' fan-out) is never refused."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from hooks_support import Rig

from gnomon.harness import compact_gate as cg

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

REFUSED = [
    "!cmd",
    "  !cmd",
    "\t!cmd",
    "\n!cmd",                      # leading blank line: still the start of the prompt
    "​!cmd",                  # zero-width space
    "‍⁠﻿!cmd",      # ZWJ, word joiner, BOM
    "﻿!cmd",
    "‎!cmd",                  # LRM (Cf)
    " ​ \n !cmd",             # mixed whitespace + invisibles
    "！cmd",                   # fullwidth !
    "﹗cmd",                   # small !
    "​！cmd",
    "!cmd\nsecond line",           # multi-line, bang first
    "\n\n!cmd\nmore",              # multi-line, leading blank lines
]
PASSED = ["a!b", "#x", "hello !", "hello\n!interior line", "\\!bang", "x!", "", "   ", "Wow! done",
          "<deployment-to-all> !cmd", "ok"]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, read_context=lambda: 50.0)
    yield r
    r.close()


def _recs(rig, kind="inject-refused"):
    return [r for r in rig.file_records() if r.get("kind") == kind]


# -- the pure predicate --------------------------------------------------------


@pytest.mark.parametrize("text", REFUSED)
def test_has_leading_bang_true(text):
    assert cg.has_leading_bang(text) is True, repr(text)


@pytest.mark.parametrize("text", PASSED)
def test_has_leading_bang_false(text):
    assert cg.has_leading_bang(text) is False, repr(text)


def test_has_leading_bang_non_str():
    assert cg.has_leading_bang(None) is False and cg.has_leading_bang(5) is False


# -- the two screens -----------------------------------------------------------


@pytest.mark.parametrize("text", REFUSED)
def test_screen_self_refuses(text):
    assert cg.screen_self_inject(text) == [cg.REASON_BANG], repr(text)


@pytest.mark.parametrize("text", REFUSED)
def test_screen_hub_refuses(text):
    assert cg.screen_hub_inject(text) == [cg.REASON_BANG], repr(text)


@pytest.mark.parametrize("text", PASSED + ["/clear", "hi"])
def test_screens_pass_negative_controls(text):
    assert cg.screen_self_inject(text) is None
    assert cg.screen_hub_inject(text) is None


def test_slash_command_not_refused_by_the_bang_rule():
    # "/" is a slash command by design; the bang rule must not touch it (the /compact rule is separate)
    assert cg.REASON_BANG not in (cg.screen_self_inject("/compact") or [])
    assert cg.screen_hub_inject("/status") is None


# -- end to end through the actuator (real Rig, real injection log) -------------


@pytest.mark.parametrize("text", ["!cmd", "  !cmd", "​!cmd", "﻿!cmd", "！cmd", "\n!cmd\nx"])
def test_self_verb_inject_refused_loudly_and_recorded(rig, text):
    resp = rig.proxy.actuator.dispatch({"cmd": "inject", "text": text}, origin="self")
    assert resp["ok"] is False and resp.get("decision") == "refused", resp
    assert resp["reasons"] == ["leading-bang"]
    assert "dsd-1a2b3c4d5e3" in resp["error"] and "bash mode" in resp["error"]
    assert rig.child_bytes() == b""
    recs = _recs(rig)
    assert len(recs) == 1 and recs[0]["source"] == "self-verb"
    assert recs[0]["reasons"] == ["leading-bang"] and recs[0]["reason"] == "leading-bang"


@pytest.mark.parametrize("text", ["!cmd", "  !cmd", "​!cmd", "！cmd", "\n!cmd\nx"])
def test_hub_inject_refused_loudly_and_recorded(rig, text):
    rig.hub_down({"cmd": "inject", "text": text})
    assert b"cmd" not in rig.child_bytes()
    recs = _recs(rig)
    assert len(recs) == 1 and recs[0]["source"] == "bus" and recs[0]["reasons"] == ["leading-bang"]


def test_hub_inject_response_shape_is_the_existing_refusal(rig):
    resp = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "!cmd"}, origin="hub")
    assert resp["ok"] is False and resp["decision"] == "refused" and resp["action"] == "inject"
    assert resp["reasons"] == ["leading-bang"] and "'!'" in resp["error"]


@pytest.mark.parametrize("src", ["agent-launch", "dsd-answer", "operator-broadcast", "bus"])
def test_other_hub_sources_refused(rig, src):
    resp = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "!cmd", "source": src}, origin="hub")
    assert resp["ok"] is False and resp["reasons"] == ["leading-bang"]


@pytest.mark.parametrize("text", ["a!b", "#x", "hello\n!interior", "Wow!"])
def test_negative_controls_typed_through_self_and_hub(rig, text):
    assert rig.proxy.actuator.dispatch({"cmd": "inject", "text": text}, origin="self")["ok"] is True
    assert rig.proxy.actuator.dispatch({"cmd": "inject", "text": text}, origin="hub")["ok"] is True
    assert _recs(rig) == []


# -- g47b / g67: nothing at import time ----------------------------------------


def test_hook_entry_imports_do_no_flag_or_config_work_at_import():
    code = (
        "import sys, importlib\n"
        "import gnomon.flags as F\n"
        "calls=[]\n"
        "F.flag=lambda *a, **k: calls.append(a) or True\n"
        "F.resolve=lambda *a, **k: calls.append(a) or (True,'x')\n"
        "for m in ('gnomon.harness.compact_gate','gnomon.harness.prompt_command_hook',"
        "'gnomon.harness.lifecycle_hook','gnomon.harness.posttool_hook',"
        "'gnomon.harness.statusline_hook','gnomon.harness.subagent_stop_hook',"
        "'gnomon.harness.deny_guard'):\n"
        "    importlib.import_module(m)\n"
        "assert not calls, calls\n"
        "print('ok')\n")
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": REPO, "HOME": os.environ.get("HOME", "")}
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, cwd=REPO)
    assert out.returncode == 0 and out.stdout.strip() == "ok", (out.stdout, out.stderr)

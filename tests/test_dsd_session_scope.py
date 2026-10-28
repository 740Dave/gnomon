"""A (pre-review part 2 fix round): MEASURE, at call time, which gnomon modules a session's tool-server process imports when each of
the four DSD verbs is called, flag ON and flag OFF, and tie that measurement to the named session-scope list.

One fresh process per call (no static scan): the process imports ``self_mcp`` (the startup set), then ``handle()`` runs the verb against a
tmp store with a faked control socket; the modules that appeared during the call are read from ``sys.modules``.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from dsd_session_scope import SESSION_DSD_MODULES

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHILD = r'''
import json, os, sys, tempfile
os.environ.pop("GNOMON_INSTANCE_ID", None)
verb, flag = sys.argv[1], sys.argv[2] == "on"
home = tempfile.mkdtemp(); os.environ["HOME"] = home
root = os.path.join(home, "agents"); os.makedirs(os.path.join(root, "alpha-agent", "dsd"))
GOOD = ("### D1 · Ship it?\n**What this is:** the release.\n**Why it matters:** the demo.\n| Option | Tradeoffs |\n|---|---|\n"
        "| **Yes** | fast |\n| No | slow |\n**My recommendation:** Yes\n**Your call:** ____\n")
rec = {"dsd_id": "dsd-1a0e00000a", "instance_id": "alpha-agent", "created_epoch": 1.0, "status": "pending-human-review", "dsd": {"markdown": GOOD}}
json.dump(rec, open(os.path.join(root, "alpha-agent", "dsd", "dsd-1a0e00000a.json"), "w"))
from gnomon.harness import self_mcp as S
from gnomon.config import GnomonConfig
class Cfg(GnomonConfig):
    def intelligences_root(self): return root
S.actuate = lambda sp, c: {"ok": True, "instance_id": "alpha-agent", "session_dir": home}
S._machine_config = lambda config=None: Cfg(flags={"dsd_self_verbs": flag})
before = {m for m in sys.modules if m.startswith("gnomon")}
args = {"send_dsd": {"markdown": GOOD}, "my_dsds": {}, "withdraw_dsd": {"dsd_id": "dsd-1a0e00000a", "reason": "x"},
        "replace_dsd": {"dsd_id": "dsd-1a0e00000a", "markdown": GOOD}}[verb]
r = S.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": verb, "arguments": args}}, "sock", dsd_self_verbs=True)
after = {m for m in sys.modules if m.startswith("gnomon")}
print(json.dumps({"result": json.loads(r["result"]["content"][0]["text"]), "imported": sorted(after - before)}))
'''


def measure(verb, flag):
    r = subprocess.run([sys.executable, "-c", CHILD, verb, "on" if flag else "off"], capture_output=True, text=True,
                       env={**os.environ, "PYTHONPATH": ROOT}, timeout=120)
    assert r.returncode == 0, r.stderr[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def as_files(mods):
    return {m.replace(".", "/") + ".py" if not m.endswith("dsd") else "gnomon/dsd/__init__.py" for m in mods
            if m.startswith("gnomon.dsd")}


@pytest.mark.parametrize("verb", ["send_dsd", "my_dsds", "withdraw_dsd", "replace_dsd"])
def test_flag_off_a_verb_call_imports_no_dsd_module(verb):
    out = measure(verb, False)
    assert out["result"]["ok"] is False and out["result"]["decision"] == "disabled"
    assert not [m for m in out["imported"] if m.startswith("gnomon.dsd")], out["imported"]


@pytest.mark.parametrize("verb", ["send_dsd", "my_dsds", "withdraw_dsd", "replace_dsd"])
def test_flag_on_the_modules_a_verb_call_imports_are_inside_the_named_session_list(verb):
    out = measure(verb, True)
    assert out["result"]["ok"] is True, out["result"]
    got = as_files(out["imported"])
    assert "gnomon/dsd/dsd_ops.py" in got and "gnomon/dsd/__init__.py" in got        # positive control: it DID import them
    assert got <= SESSION_DSD_MODULES, sorted(got - SESSION_DSD_MODULES)
    assert not [m for m in out["imported"] if m.startswith("gnomon.app") or m == "gnomon.dsd.answer_stats"]


def test_the_package_init_is_what_pulls_the_other_modules_in():
    """Why the list is as wide as it is: importing ANY dsd submodule runs ``dsd/__init__.py``, whose eager imports are named here."""
    src = open(os.path.join(ROOT, "gnomon", "dsd", "__init__.py"), encoding="utf-8").read()
    eager = {ln.split()[1].lstrip(".") for ln in src.splitlines() if ln.startswith("from .")}
    assert {"model", "persist", "resolve", "roundtrip", "inbox", "live", "writer"} <= eager

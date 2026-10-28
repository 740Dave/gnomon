"""Round 6 item 5 (the architect's condition on the relaunch fix): what a session's launch.json ``flags`` can reach.

MEASURED TRUTH: live's existing behaviour, a pre-existing lever, on the hardening backlog; the relaunch fix adds one more flag to it.
The launch config reaches the flags resolver (``profile=<launch cfg>``) at the call sites pinned below: launcher.py, self_mcp.py and
telemetry.py resolve their harness flags with launch.json as the override (launch.json > machine config > default), exactly as live does
(ruling, round 6 item 5: NO narrowing). The relaunch fix adds exactly ONE resolver site (``resume_keys.flag_on``) and one inline dict read in
``launcher.py``. Every other flag reader in the tree (the hub caller binding, the Apps tab, the deliverable ledger, ...) never sees
launch.json at all, so a launch.json ``flags`` entry for them is inert. It is therefore NOT true that launch.json can turn on only the
relaunch flag; it can turn on the harness flags those modules read, as in live. Live has 13 such sites in launcher.py; gnomon's launcher has a
14th, the reader of the gnomon-only flag session_mcp_from_profile (default OFF, already reachable this way before the relaunch fix): so TWO
gnomon-only flags are reachable through launch.json (that one and the relaunch flag).
"""
import ast
import os
import subprocess

import pytest

from gnomon import flags as F
from gnomon import resume_keys as RK
from gnomon.config import GnomonConfig
from gnomon.harness import launcher as L

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELAUNCH = "relaunch_derives_resume_keys"
INERT = ("hub_caller_binding", "apps_tab", "deliverable_ledger")     # all default OFF, none read with launch.json

#: resolver calls that pass the LAUNCH config (any spelling: ``cfg``, ``lcfg``, ``self.cfg``, ``launch``, ``launch_cfg`` ...) as ``profile=``, per file.
#: live's own sites: 13 in launcher.py (live ed09501), 2 in self_mcp.py, 2 in telemetry.py ...
LIVE_SITES = {"gnomon/harness/launcher.py": 13, "gnomon/harness/self_mcp.py": 2, "gnomon/harness/telemetry.py": 2}
#: ... plus ONE gnomon-only flag that was already reachable this way before the relaunch fix: session_mcp_from_profile (launcher.py)
GNOMON_ONLY_SITES = {"gnomon/harness/launcher.py": 1}
#: ONE more gnomon-only harness flag read here (app_activity_presence, default OFF, read once at harness launch)
X72_SITES = {"gnomon/harness/launcher.py": 1}
#: ONE more gnomon-only harness flag read here (agent_mail, default OFF, read once at harness launch)
X53_SITES = {"gnomon/harness/launcher.py": 1}
#: ONE more gnomon-only harness flag read here (alerts, default OFF, read once at harness launch)
X50_SITES = {"gnomon/harness/launcher.py": 1}
GNOMON_ONLY_FLAG = "session_mcp_from_profile"
#: ... plus the ONE the relaunch fix adds
FIX_SITES = {"gnomon/resume_keys.py": 1}


def _spells_the_launch_config(expr):
    text = ast.unparse(expr).lower()
    return "cfg" in text or "launch" in text


def launch_cfg_calls(sources):
    """``[(file, first-argument source)]`` for every call to ``flag`` / ``resolve`` / ``effective_flags`` whose ``profile=`` argument spells the
    launch config in any way (a name or attribute containing 'cfg' or 'launch')."""
    out = []
    for rel, src in sources:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                fn = n.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name in ("flag", "resolve", "effective_flags"):
                    for k in n.keywords:
                        if k.arg == "profile" and _spells_the_launch_config(k.value):
                            out.append((rel, ast.unparse(n.args[0]) if n.args else ""))
    return out


def launch_cfg_sites(sources):
    """``{file: n}`` view of :func:`launch_cfg_calls`."""
    out = {}
    for rel, _first in launch_cfg_calls(sources):
        out[rel] = out.get(rel, 0) + 1
    return out


def _tree_sources():
    r = subprocess.run(["git", "ls-files", "gnomon", "web", "tools"], cwd=ROOT, capture_output=True, text=True)
    files = r.stdout.split()
    if r.returncode != 0 or len([f for f in files if f.endswith(".py")]) < 100:
        raise RuntimeError("tests/test_launch_json_flags_scope.py needs a git checkout of the tree (git ls-files failed or found too few "
                           f"files: rc={r.returncode}, {len(files)} files, stderr={r.stderr.strip()[:200]!r}); it must not pass on an empty scan")
    return [(f, open(os.path.join(ROOT, f), encoding="utf-8").read()) for f in files if f.endswith(".py")]


def test_the_scanner_has_a_positive_and_a_negative_control():
    planted = [("x.py", "from . import flags\nflags.flag('a', profile=cfg, config=mc)\nflags.resolve('b', profile=lcfg)\n")]
    assert launch_cfg_sites(planted) == {"x.py": 2}
    other_spellings = [("z.py", "flags.flag('a', profile=self.cfg)\nflags.flag('b', profile=launch)\n"
                                "flags.resolve('c', profile=self._launch_cfg)\nflags.flag('d', profile=load_launch(sd))\n")]
    assert launch_cfg_sites(other_spellings) == {"z.py": 4}
    assert launch_cfg_sites([("y.py", "flags.flag('a')\nflags.flag('a', profile=agent_profile)\nother(profile=cfg)\n")]) == {}


def test_the_scan_refuses_to_run_without_a_git_checkout(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 128, stdout="", stderr="fatal: not a git repository"))
    with pytest.raises(RuntimeError, match="needs a git checkout"):
        _tree_sources()


def test_the_launch_config_reaches_the_resolver_at_exactly_the_known_sites():
    srcs = _tree_sources()
    found = launch_cfg_sites(srcs)
    expected = {}
    for table in (LIVE_SITES, GNOMON_ONLY_SITES, FIX_SITES, X72_SITES, X53_SITES, X50_SITES):
        for f, n in table.items():
            expected[f] = expected.get(f, 0) + n
    assert found == expected, found
    assert sum(FIX_SITES.values()) == 1                         # the relaunch fix adds ONE resolver site
    # the second gnomon-only flag reachable this way (already in the base, default OFF) is named, not hidden in the count
    firsts = [first for rel, first in launch_cfg_calls(srcs) if rel == "gnomon/harness/launcher.py"]
    assert firsts.count(repr(GNOMON_ONLY_FLAG)) == 1 and len(firsts) == 17
    assert F.KNOWN_FLAGS[GNOMON_ONLY_FLAG].default is False


def test_the_fix_flag_is_honoured_from_launch_json_and_only_by_its_own_readers(monkeypatch):
    cfg = {"flags": {RELAUNCH: True}}
    mc = GnomonConfig()
    assert RK.flag_on(cfg, mc) is True                          # launch.json wins over the machine config for THIS flag
    assert RK.flag_on({}, mc) is False and RK.flag_on(None, mc) is False
    assert RK.flag_on({"flags": {RELAUNCH: "yes"}}, mc) is False           # non-bool ignored, fail OFF


def test_three_other_flags_set_true_in_launch_json_resolve_from_machine_config_or_default(tmp_path, monkeypatch):
    from gnomon.app import controller as C
    from gnomon.appshost.verbs import FLAG as APPS_FLAG

    flags_in_launch_json = {n: True for n in INERT}
    for n in INERT:
        assert F.KNOWN_FLAGS[n].default is False                                # a launch.json value would be the ONLY way these read True
    assert APPS_FLAG == "apps_tab"
    mc_off = GnomonConfig()
    # the real readers take the machine config (or nothing): they cannot see launch.json
    assert C._hub_caller_binding_flag(mc_off) is False
    assert F.flag(APPS_FLAG, config=mc_off) is False and F.flag("deliverable_ledger", config=mc_off) is False
    mc_on = GnomonConfig(flags={n: True for n in INERT})
    assert C._hub_caller_binding_flag(mc_on) is True                          # control: the MACHINE config does turn it on
    # the launcher: resolving the harness flags with these in launch.json changes nothing about them (and nothing else)
    sd = tmp_path / "sess"
    (sd / ".claude").mkdir(parents=True)
    base_cfg = {"instance_id": "alpha-agent"}
    with_flags = dict(base_cfg, flags=flags_in_launch_json)
    a = L.resolve_harness_settings(dict(base_cfg), str(sd), machine_config=mc_off)
    b = L.resolve_harness_settings(dict(with_flags), str(sd), machine_config=mc_off)
    assert a == b
    # every reader that names one of them, through the resolver, reports a non-profile source
    rows = {r["name"]: r for r in F.effective_flags(config=mc_off)}
    assert all(rows[n]["source"] == "default" and rows[n]["value"] is False for n in INERT)


def test_the_instrument_can_see_a_launch_json_override_where_one_exists(tmp_path):
    """Control, and the recorded pre-existing behaviour: a harness flag the launcher reads WITH the launch config IS overridden by it."""
    sd = tmp_path / "sess"
    (sd / ".claude").mkdir(parents=True)
    mc = GnomonConfig()
    off = L.resolve_harness_settings({"instance_id": "alpha-agent"}, str(sd), machine_config=mc)
    on = L.resolve_harness_settings({"instance_id": "alpha-agent", "flags": {"idle_snooze": True}}, str(sd), machine_config=mc)
    assert off["idle_snooze"] is False and on["idle_snooze"] is True

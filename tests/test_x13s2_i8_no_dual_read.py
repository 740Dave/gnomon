"""X13 S2, invariant I8, the part that lives in the Gnomon source: 'leaves no dual-read code in Gnomon' (
D2: no shims, no dual-name transition window).

The other I8 clauses are proven where they live: the converter has NO apply mode and is dry-run by default, and its dry run
is idempotent (DEPLOY ``workspaces/<ws>/converter/tests``: ``test_cli_requires_exactly_one_mode_and_has_no_apply``,
``test_dry_run_is_idempotent``, ``test_converter_has_no_apply_code_path``, and this repo's ``test_x13s1_write_guard.py``).
'Apply it, run it again, assert the second run changes nothing' cannot be tested until the S3 apply path exists.

The detector: a module that passes BOTH the legacy file name ``profile.json`` and a new launch-config name as a call
argument (``os.path.join``, ``open``, ...) is reading both names: a dual-read. Today the package reads only ``profile.json``;
S3 renames it in ONE change and this test then guards the other direction (the old name may not linger).
"""

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEGACY = ("profile.json",)
NEW = ("launch-config.json", "agent-launch.json", "launch_config.json")


def _call_string_args(source):
    out = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            for a in list(node.args) + [k.value for k in node.keywords]:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    out.add(a.value)
    return out


def dual_read(source):
    args = _call_string_args(source)
    return bool(args & set(LEGACY)) and bool(args & set(NEW))


def test_the_detector_can_fail():
    planted = 'import os\ndef f(d):\n    for n in ("x",):\n        pass\n    return os.path.join(d, "launch-config.json"), os.path.join(d, "profile.json")\n'
    assert dual_read(planted)
    assert dual_read('open("agent-launch.json"); open("profile.json")')
    assert not dual_read('os.path.join(d, "profile.json")')
    assert not dual_read('os.path.join(d, "launch-config.json")')
    assert not dual_read('x = {"a": "profile.json", "b": "launch-config.json"}')      # a doc table is not a read


#: the ONE module allowed to name both: the one-time converter ENGINE (it plans/performs the rename; it is the thing that
#: replaces the shims, D49 D2). Nothing that runs in the dashboard or the harness may.
CONVERTER_ENGINE = "gnomon/workspace/convert.py"


def test_no_module_of_the_package_reads_both_the_legacy_and_a_new_launch_config_name():
    offenders = []
    scanned = 0
    for d, dirs, files in os.walk(os.path.join(ROOT, "gnomon")):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if f.endswith(".py") and ".bak" not in f:
                scanned += 1
                if dual_read(open(os.path.join(d, f), encoding="utf-8").read()):
                    offenders.append(os.path.relpath(os.path.join(d, f), ROOT))
    assert scanned > 50, "the scan is not vacuous"
    assert offenders == [], f"dual-read code outside the converter engine (D49 D2: no shims): {offenders}"


def test_the_package_reads_the_legacy_name_in_exactly_the_profile_store_today():
    """Context for S3: the single read site of the legacy name (so the rename is one change)."""
    sites = []
    for d, dirs, files in os.walk(os.path.join(ROOT, "gnomon")):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        for f in files:
            if f.endswith(".py") and ".bak" not in f and "profile.json" in _call_string_args(open(os.path.join(d, f), encoding="utf-8").read()):
                sites.append(os.path.relpath(os.path.join(d, f), ROOT))
    assert "gnomon/profile.py" in sites, sites

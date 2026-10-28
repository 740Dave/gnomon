"""a child interpreter must import the TREE UNDER TEST.

The framework Python carries an editable install of the live tree, which names its package ``gnomon`` like any clone. ``-I`` and
``-E`` ignore PYTHONPATH, ``-I`` and ``-P`` drop the cwd, and an ``env=`` that omits PYTHONPATH loses conftest's pin; a child started
any of those ways imports the LIVE tree. ``tests/tree_child.py`` puts an explicit sys.path entry in the child.

* every flag/env/cwd shape a test can use, started through the helper, reports a ``gnomon`` package inside this tree;
* positive control: the shapes that lose PYTHONPATH, run WITHOUT the helper's path entry, do not (they import the installed tree, or
  nothing), so the check above can fail;
* a static scan: a test that starts a child with ``-I`` or ``-E`` must go through the helper (or carry its own sys.path entry), or be
  listed in ``DELIBERATE`` with the reason its child does not import ``gnomon`` from the installed tree."""
import ast
import os

import pytest

from tree_child import TREE_ROOT, child_env, run_child

PKG = os.path.realpath(os.path.join(TREE_ROOT, "gnomon"))
PROBE = "import gnomon, os; print(os.path.realpath(os.path.dirname(gnomon.__file__)))"
BARE = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}          # an env= that carries no PYTHONPATH at all

#: (flags, env, cwd): the shapes a test can use. ``loses_pythonpath``: the child cannot see PYTHONPATH, so only the path entry saves it.
SHAPES = [
    ((), child_env(), None, False),
    (("-P",), child_env(), "/", False),
    (("-I",), child_env(), "/", True),
    (("-E",), child_env(), "/", True),
    (("-I", "-E"), child_env(), "/", True),
    ((), BARE, "/", True),
    (("-I",), BARE, "/", True),
    (("-E",), BARE, os.path.expanduser("~"), True),
]


def _id(shape):
    flags, env, cwd, _ = shape
    return "%s-%s-cwd=%s" % ("".join(flags) or "noflags", "pinned" if "PYTHONPATH" in env else "bare", cwd)


@pytest.mark.parametrize("shape", SHAPES, ids=_id)
def test_a_child_started_through_the_helper_imports_the_tree_under_test(shape):
    flags, env, cwd, _ = shape
    r = run_child(PROBE, flags=flags, env=env, cwd=cwd)
    assert r.returncode == 0, (_id(shape), r.stderr[-400:])
    assert r.stdout.strip() == PKG, (_id(shape), r.stdout.strip(), PKG)


def test_a_decoy_gnomon_on_pythonpath_and_in_the_cwd_does_not_win(tmp_path):
    decoy = tmp_path / "decoy" / "gnomon"
    decoy.mkdir(parents=True)
    (decoy / "__init__.py").write_text("DECOY = True\n")
    env = child_env()
    env["PYTHONPATH"] = str(decoy.parent)
    r = run_child(PROBE, flags=(), env=env, cwd=str(decoy.parent))
    assert r.returncode == 0 and r.stdout.strip() == PKG, (r.stdout, r.stderr[-400:])


@pytest.mark.parametrize("shape", [s for s in SHAPES if s[3]], ids=_id)
def test_control_without_the_path_entry_the_same_child_is_not_the_tree_under_test(shape):
    """The check above can fail: drop the helper's path entry and a child that cannot see PYTHONPATH does not land in this tree."""
    flags, env, cwd, _ = shape
    r = run_child(PROBE, flags=flags, env=env, cwd=cwd, with_tree_entry=False)
    landed_here = r.returncode == 0 and r.stdout.strip() == PKG
    assert not landed_here, (_id(shape), "a child without the path entry still imported the tree under test; the guard could not fail")


# --------------------------------------------------------------------------------------------------------- static scan
#: tests/<file> -> why its -I/-E child does not import ``gnomon`` from the installed tree.
DELIBERATE = {
    "test_x58_rehome_hook_path.py": "its -I child exec()s the guard SOURCE's stdlib-only prefix; it imports no gnomon (measured: no marker)",
    "test_fetchverbatim_round3.py": "its -I -S child puts dirname(extract.__file__) on sys.path in its own boot code (the tree the test imported)",
    "test_cutover_switch.py": "its -S -I child is the subject: a FAKE site dir (site.addsitedir) shows which tree a .pth maps to",
    "test_child_python_tree.py": "this file",
}
_SPAWN = {"run", "Popen", "check_output", "check_call", "call"}


def _isolating_children(src):
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in _SPAWN):
            continue
        if ast.unparse(node.func.value).split(".")[0] not in ("subprocess", "sp"):
            continue
        argv = node.args[0] if node.args else None
        if isinstance(argv, ast.List) and any(isinstance(e, ast.Constant) and e.value in ("-I", "-E") for e in argv.elts):
            yield node.lineno, ast.get_source_segment(src, node) or ""


def _scan(tests_dir):
    bad = []
    for fn in sorted(os.listdir(tests_dir)):
        if not fn.endswith(".py") or fn in DELIBERATE:
            continue
        with open(os.path.join(tests_dir, fn), encoding="utf-8") as fh:
            src = fh.read()
        bad += ["%s:%d" % (fn, line) for line, seg in _isolating_children(src) if "sys.path.insert" not in seg]
    return bad


def test_no_test_starts_an_isolated_child_without_an_explicit_tree_entry():
    assert _scan(os.path.join(TREE_ROOT, "tests")) == []


def test_the_scan_can_fail(tmp_path):
    (tmp_path / "test_planted.py").write_text(
        "import subprocess, sys\nsubprocess.run([sys.executable, '-I', '-c', 'import gnomon'])\n"
        "subprocess.run([sys.executable, '-E', '-c', 'import gnomon'])\n"
        "subprocess.run([sys.executable, '-I', '-c', 'import sys; sys.path.insert(0, \"/x\"); import gnomon'])\n"
        "subprocess.run([sys.executable, '-c', 'import gnomon'])\n")
    assert _scan(str(tmp_path)) == ["test_planted.py:2", "test_planted.py:3"]

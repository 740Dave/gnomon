"""X13 S5-3: the deployment-data seam (``gnomon/deploy_data.py``).

What is asserted, and why each can FAIL:

* an absent table is the neutral default (``None``), a present one is parsed, an edit is picked up (mtime cache);
* a present-but-unusable table (not JSON, not an object, over the size cap, a directory, a FIFO) is ``None`` AND
  recorded in ``errors()``: an errored read never looks like an absent one;
* the reader NEVER writes (the tree under the root is byte-identical after a read of every kind of table);
* it is LAZY: importing it, in a fresh interpreter, opens nothing under HOME (g47b / g67);
* the data lives under ``<root>/defaults/``, never under ``workspaces/`` (I6: machine safety paths must not open
  ``workspaces/**``);
* name validation refuses path separators.
"""

import json
import os
import subprocess
import sys

import pytest

from gnomon import deploy_data as DD

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tree(root):
    out = {}
    for dp, dn, fn in os.walk(root):
        for f in fn:
            p = os.path.join(dp, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = fh.read()
        for d in dn:
            out[os.path.relpath(os.path.join(dp, d), root) + "/"] = b""
    return out


def test_absent_table_is_none_and_not_an_error(gn_deploy_root):
    assert DD.load_json("role-labels") is None and DD.section("role-labels", "labels") is None
    assert DD.errors() == {}


def test_a_present_table_is_parsed_and_an_edit_is_picked_up(put_deploy_data):
    path = put_deploy_data("sample", {"a": 1, "labels": {"x": "X"}})
    assert DD.load_json("sample") == {"a": 1, "labels": {"x": "X"}}
    assert DD.section("sample", "labels") == {"x": "X"} and DD.section("sample", "nope") is None
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"a": 2, "padding": "to-change-the-size"}, fh)
    assert DD.load_json("sample")["a"] == 2                      # the (mtime, size) key changed: re-read
    assert DD.errors() == {}


@pytest.mark.parametrize("raw", ["not json at all", "[1, 2, 3]", '"a string"', "\xff\xfe binary"])
def test_an_unusable_table_is_none_and_recorded(put_deploy_data, raw):
    path = put_deploy_data("bad.json", raw)
    assert DD.load_json("bad") is None
    assert path in DD.errors() and DD.errors()[path]


def test_an_oversize_table_is_refused(put_deploy_data):
    path = put_deploy_data("big.json", '{"x": "' + "a" * DD.MAX_BYTES + '"}')
    assert DD.load_json("big") is None and "larger than" in DD.errors()[path]


def test_a_directory_or_fifo_in_the_place_of_a_table_never_blocks(gn_deploy_root):
    d = gn_deploy_root / "defaults"
    d.mkdir()
    (d / "dir.json").mkdir()
    assert DD.load_json("dir") is None and "not a regular file" in DD.errors()[str(d / "dir.json")]
    os.mkfifo(str(d / "fifo.json"))
    assert DD.load_json("fifo") is None                          # O_NONBLOCK + fstat: returns, never hangs
    assert "not a regular file" in DD.errors()[str(d / "fifo.json")]


def test_a_fixed_table_clears_its_recorded_fault(put_deploy_data):
    path = put_deploy_data("flaky.json", "oops")
    assert DD.load_json("flaky") is None and path in DD.errors()
    put_deploy_data("flaky", {"ok": True})
    assert DD.load_json("flaky") == {"ok": True} and path not in DD.errors()
    os.remove(path)
    assert DD.load_json("flaky") is None and DD.errors() == {}


def test_record_fault_surfaces_a_semantic_fault_without_changing_the_value(put_deploy_data):
    put_deploy_data("sem", {"field": 5})
    assert DD.load_json("sem") == {"field": 5}                   # parsed fine; the CONSUMER finds the shape wrong ...
    DD.record_fault("sem", "field must be a string; ignored")    # ... and records it
    assert DD.load_json("sem") == {"field": 5}                   # (cached: the same bytes do not clear the record)
    assert any(p.endswith("sem.json") and "must be a string" in m for p, m in DD.errors().items())


@pytest.mark.parametrize("name", ["../x", "a/b", "", "A", "1x", "x" * 80, "x.json", None, 5])
def test_names_are_validated(name):
    with pytest.raises(ValueError):
        DD.load_json(name)


def test_reading_never_writes(put_deploy_data, gn_deploy_root):
    put_deploy_data("good", {"a": 1})
    put_deploy_data("bad.json", "nope")
    put_deploy_data("verb-policy.toml", "schema = 1\n")
    before = _tree(str(gn_deploy_root))
    for name in ("good", "bad", "missing"):
        DD.load_json(name)
        DD.section(name, "a")
    DD.data_dir(), DD.data_path("verb-policy.toml"), DD.errors()
    assert _tree(str(gn_deploy_root)) == before


def test_reading_with_no_defaults_directory_creates_nothing(gn_deploy_root):
    """The installer creates ``defaults/``; a reader never does (the mutant that mkdirs before reading is caught here)."""
    assert _tree(str(gn_deploy_root)) == {}
    for name in ("good", "role-labels"):
        assert DD.load_json(name) is None and DD.section(name, "x") is None
    DD.read_bytes(DD.data_path("verb-policy.toml"))
    assert not (gn_deploy_root / "defaults").exists() and _tree(str(gn_deploy_root)) == {}


def test_data_lives_under_defaults_never_under_workspaces(gn_deploy_root):
    p = DD.data_path("role-labels.json")
    assert p == os.path.join(str(gn_deploy_root), "defaults", "role-labels.json")
    assert "workspaces" not in p.split(os.sep)                   # I6: safety paths may read this, never workspaces/**
    assert DD.data_dir("") == "" and DD.data_path("x.json", "") == ""       # no root resolvable: nothing, not a crash
    assert DD.load_json("anything", root="") is None


def test_the_real_resolver_reads_the_machine_config_lazily_and_degrades_to_empty(tmp_path, monkeypatch):
    """The un-patched ``deploy_root`` (the seam S5-4 re-points): follows ``config.deploy_root`` and never raises."""
    import importlib

    from gnomon import config as C

    mod = importlib.reload(DD)                                   # drop the conftest patch for this module object
    try:
        cfg = tmp_path / "config.json"
        cfg.write_text(json.dumps({"deploy_root": str(tmp_path / "somewhere")}))
        monkeypatch.setattr(C.ConfigStore, "load", lambda self: C.GnomonConfig(deploy_root=str(tmp_path / "somewhere")))
        assert mod.deploy_root() == str(tmp_path / "somewhere")

        def boom(self):
            raise RuntimeError("config unreadable")

        monkeypatch.setattr(C.ConfigStore, "load", boom)
        assert mod.deploy_root() == "" and mod.load_json("x") is None      # a broken config means no data, not a crash
    finally:
        importlib.reload(DD)


def test_importing_the_module_does_no_filesystem_work(tmp_path):
    """g47b / g67: in a fresh interpreter with an empty HOME, the import opens nothing under HOME (an audit hook
    watches ``open`` and ``os.listdir`` / ``os.scandir`` while the module is imported)."""
    home = tmp_path / "home"
    home.mkdir()
    code = (
        "import sys\n"
        "seen = []\n"
        "home = sys.argv[1]\n"
        "def hook(event, args):\n"
        "    if event in ('open', 'os.listdir', 'os.scandir', 'os.mkdir', 'os.rename', 'os.remove'):\n"
        "        a = args[0]\n"
        "        if isinstance(a, (str, bytes)) and str(a).startswith(home):\n"
        "            seen.append((event, str(a)))\n"
        "sys.addaudithook(hook)\n"
        "import gnomon.deploy_data as dd\n"
        "assert dd.DATA_DIRNAME == 'defaults'\n"
        "print(repr(seen))\n"
    )
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": _ROOT, "PYTHONDONTWRITEBYTECODE": "1"}
    r = subprocess.run([sys.executable, "-c", code, str(home)], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]", r.stdout
    assert list(home.iterdir()) == []                            # and nothing was created


def test_the_detector_for_the_lazy_test_can_fail(tmp_path):
    """Positive control: the same audit hook DOES see an open under HOME (so an empty list means something)."""
    home = tmp_path / "home"
    home.mkdir()
    (home / "f").write_text("x")
    code = (
        "import sys\n"
        "seen = []\n"
        "def hook(event, args):\n"
        "    if event == 'open' and str(args[0]).startswith(sys.argv[1]):\n"
        "        seen.append(str(args[0]))\n"
        "sys.addaudithook(hook)\n"
        "open(sys.argv[1] + '/f').read()\n"
        "print(len(seen))\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(home)], capture_output=True, text=True, timeout=60)
    assert r.stdout.strip() == "1", (r.stdout, r.stderr)


def test_str_map_and_str_list_are_forgiving():
    assert DD.str_map({"a": "A", "b": 1, "": "x", 5: "y"}) == {"a": "A"} and DD.str_map([1]) == {} and DD.str_map(None) == {}
    assert DD.str_list(["a", 1, "", "b"]) == ["a", "b"] and DD.str_list("ab") == [] and DD.str_list(None) == []

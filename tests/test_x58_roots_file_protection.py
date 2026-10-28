"""Spot-check (Opus, 2026-10-08, x13 s5-3/s5-4) P2: the roots pointer file decides where the machine config and the deployment data are read
from, so a session that can write it can redirect the operator's security settings. The deny guard's config-write rule now covers the
pointer file too, wherever the roots resolver looks for it: the default under HOME and the path ``GNOMON_CONFIG`` names in the hook's own
environment, by file identity (realpath, then ``samestat``) as well as by spelling, through the Bash rule and the Edit / Write tools alike.

These tests mirror tests/test_confirm_proof_fixround.py's config ones. Nothing here touches a real roots or config file: HOME is a tmp dir.
Import origin is asserted (the editable install of a DIFFERENT tree must never answer for the tree under test).
Run the case lists against another tree with ``probe`` (see ``cases``): no pytest session is needed to import this module.
"""
import json
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TREE = os.path.realpath(os.environ.get("X58_TREE") or ROOT)
if TREE != ROOT:
    sys.path.insert(0, TREE)
    for _m in [m for m in sys.modules if m == "gnomon" or m.startswith("gnomon.")]:
        del sys.modules[_m]

import gnomon  # noqa: E402
from gnomon import roots as R  # noqa: E402
from gnomon.harness import deny_guard as G  # noqa: E402

REL = R.DEFAULT_CONFIG_RELPATH            # ".config/gnomon/roots.toml": the one spelling lives in roots.py


def cases(home, pointer):
    """(must_block, must_allow) shell commands for a HOME and a GNOMON_CONFIG pointer path (both absolute, scratch)."""
    d = "~/" + REL
    block = [
        "echo 'data_root = \"/x\"' > " + d,
        "echo x >> $HOME/" + REL,
        "printf 'data_root=\"/x\"' | tee " + d,
        "sed -i '' 's/a/b/' " + d,
        "perl -pi -e 's/a/b/' " + d,
        "mv /tmp/p.toml " + d,
        "cp /tmp/p.toml " + d,
        "rm " + d,
        "truncate -s 0 " + d,
        "P=" + d + "; echo '' > $P",
        "python3 -c \"open('" + os.path.join(home, REL) + "','w').write('x')\"",
        "python3 - <<'EOF'\nimport pathlib\npathlib.Path('" + d + "').expanduser().write_text('x')\nEOF",
        "bash -c 'echo x > " + d + "'",
        # the file GNOMON_CONFIG names in the hook's environment
        "echo 'data_root = \"/x\"' > " + pointer,
        "cp /tmp/p.toml " + pointer,
        "rm " + pointer,
        "sed -i '' 's/a/b/' " + pointer,
        "python3 -c \"open('" + pointer + "','w').write('x')\"",
    ]
    allow = [
        "cat " + d,
        "grep -n data_root " + d,
        "cp " + d + " /tmp/roots-backup.toml",
        "ls -la ~/" + os.path.dirname(REL),
        "echo x > ~/" + os.path.dirname(REL) + "/other-notes.toml",
        "git commit -m 'document that agents never write ~/" + REL + "'",
        "echo " + d,
        "cat " + pointer,
        "python3 -c \"print(open('" + pointer + "').read())\"",
    ]
    return block, allow


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / os.path.dirname(REL)).mkdir(parents=True)
    pointer = tmp_path / "elsewhere" / "pointer.toml"
    pointer.parent.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GNOMON_CONFIG", str(pointer))
    R.reset_for_tests()
    yield home, pointer
    R.reset_for_tests()


def test_the_tree_under_test_is_the_one_imported():
    print("gnomon imported from:", gnomon.__file__)
    assert os.path.realpath(gnomon.__file__).startswith(TREE + os.sep)
    assert os.path.realpath(G.__file__).startswith(TREE + os.sep)


def test_shell_writes_to_the_roots_file_are_denied(scratch):
    home, pointer = scratch
    block, _ = cases(str(home), str(pointer))
    for cmd in block:
        reason = G.decide(cmd, str(home))
        assert reason and reason.startswith(G.CONFIG_TAG), (cmd, reason)


def test_reading_the_roots_file_or_other_files_passes(scratch):
    home, pointer = scratch
    _, allow = cases(str(home), str(pointer))
    for cmd in allow:
        assert G.decide(cmd, str(home)) is None, cmd


@pytest.mark.parametrize("tool", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_the_file_edit_tools_aimed_at_the_roots_file_are_denied(tool, scratch, tmp_path):
    home, pointer = scratch
    key = "notebook_path" if tool == "NotebookEdit" else "file_path"
    default = home / REL
    for path in (str(default), "~/" + REL, str(home) + "/x/../" + REL, str(pointer)):
        assert (G.decide_tool(tool, {key: path}) or "").startswith(G.CONFIG_TAG), path
    assert G.decide_tool(tool, {key: str(tmp_path / "unrelated.toml")}) is None
    assert G.decide_tool("Read", {"file_path": str(default)}) is None


def test_by_file_identity_a_symlink_or_hard_link_to_the_roots_file_is_denied(scratch, tmp_path):
    home, pointer = scratch
    pointer.write_text('data_root = "/x"\n')
    link = tmp_path / "elsewhere" / "sym.toml"
    link.symlink_to(pointer)
    hard = tmp_path / "elsewhere" / "hard.toml"
    os.link(pointer, hard)
    for path in (str(link), str(hard)):
        assert (G.decide_tool("Write", {"file_path": path}) or "").startswith(G.CONFIG_TAG), path
    other = tmp_path / "elsewhere" / "other.toml"
    other.write_text("x")
    assert G.decide_tool("Write", {"file_path": str(other)}) is None          # control: a different file stays writable


def test_a_trailing_dot_dot_cannot_hide_the_roots_file(scratch):
    home, _ = scratch
    cmd = "echo x > ~/" + REL + " ; echo /../"
    assert (G.decide(cmd, str(home)) or "").startswith(G.CONFIG_TAG)


def test_the_refusal_text_names_the_pointer_file_and_the_config(scratch):
    home, pointer = scratch
    msg = G.block_message(G.decide("rm " + str(pointer), str(home)))
    assert "roots pointer file" in msg and "g71-amendment" in msg and "roots.toml" in msg


def test_the_real_hook_process_blocks_an_edit_of_the_roots_file(scratch, tmp_path):
    home, pointer = scratch
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "PYTHONPATH": TREE, "GNOMON_CONFIG": str(pointer)}
    who = subprocess.run([sys.executable, "-P", "-c", "import gnomon, sys; sys.stdout.write(gnomon.__file__)"],
                         capture_output=True, text=True, env=env, cwd="/")
    assert os.path.realpath(who.stdout).startswith(TREE + os.sep), who.stdout          # which tree answered
    for payload in ({"tool_name": "Write", "tool_input": {"file_path": str(home / REL), "content": "x"}},
                    {"tool_name": "Write", "tool_input": {"file_path": str(pointer), "content": "x"}},
                    {"tool_name": "Bash", "tool_input": {"command": "rm " + str(pointer)}, "cwd": str(home)}):
        p = subprocess.run([sys.executable, "-P", "-m", "gnomon.harness.deny_guard"], input=json.dumps(payload),
                           capture_output=True, text=True, env=env, cwd="/", timeout=60)
        assert p.returncode == 2 and "g71-amendment" in p.stderr, (payload, p.returncode, p.stderr[-200:])
    ok = subprocess.run([sys.executable, "-P", "-m", "gnomon.harness.deny_guard"],
                        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "x.txt"), "content": ""}}),
                        capture_output=True, text=True, env=env, cwd="/", timeout=60)
    assert ok.returncode == 0


# ---------------------------------------------------------------- re-home fix round (Diamond F1, F2, F4): protection is by FILE IDENTITY
def _make_files(home, pointer):
    """The protected files EXIST in the scratch HOME; returns {name: path} of aliases (hard link, symlink) to each."""
    from gnomon import config as C
    roots_file = home / REL
    roots_file.write_text('data_root = "/x"\n')
    pointer.write_text('data_root = "/y"\n')
    cfg = home / os.path.relpath(os.path.expanduser(C.DEFAULT_CONFIG_PATH), os.path.expanduser("~"))
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("{}")
    out = {}
    for tag, target in (("roots", roots_file), ("pointer", pointer), ("config", cfg)):
        hard = home / ("hl-" + tag)
        sym = home / ("sl-" + tag)
        os.link(target, hard)
        sym.symlink_to(target)
        out[tag] = (target, hard, sym)
    return out


def test_bash_writes_through_an_alias_of_a_protected_file_are_denied(scratch):
    """F2: a hard link or a differently named symlink, whose NAME mentions neither 'config' nor the file name, is the same file."""
    home, pointer = scratch
    for tag, (target, hard, sym) in _make_files(home, pointer).items():
        for alias in (hard, sym):
            for cmd in ("echo x > %s" % alias, "echo x >> %s" % alias, "cp /tmp/a %s" % alias, "printf x | tee %s" % alias,
                        "sed -i '' s/a/b/ %s" % alias, "mv /tmp/a %s" % alias, "rm %s" % alias, "dd if=/dev/null of=%s" % alias,
                        "truncate -s 0 %s" % alias):
                reason = G.decide(cmd, str(home))
                assert reason and reason.startswith(G.CONFIG_TAG), (tag, cmd, reason)
        assert G.decide("cat %s" % hard, str(home)) is None                          # reading an alias stays allowed
        assert G.decide("cp %s /tmp/backup-%s" % (sym, tag), str(home)) is None


def test_tool_writes_through_an_alias_of_a_protected_file_are_denied(scratch):
    home, pointer = scratch
    for tag, (target, hard, sym) in _make_files(home, pointer).items():
        for alias in (hard, sym):
            assert (G.decide_tool("Write", {"file_path": str(alias)}) or "").startswith(G.CONFIG_TAG), (tag, alias)


def test_a_scratch_roots_fixture_is_a_different_file_and_is_allowed(scratch, tmp_path):
    """F4: every scratch HOME has its own .config/gnomon/roots.toml; it is a different inode, so it is not the protected file."""
    home, pointer = scratch
    _make_files(home, pointer)
    other = tmp_path / "otherhome"
    (other / os.path.dirname(REL)).mkdir(parents=True)
    existing = other / REL
    existing.write_text("x")
    to_create = tmp_path / "otherhome2" / REL
    to_create.parent.mkdir(parents=True)
    for cmd in ("echo x > %s" % existing, "cp /tmp/a %s" % existing, "cp %s %s.bak" % (existing, existing),
                "echo x > %s" % to_create, "sed -i '' s/a/b/ %s" % existing, "rm %s" % existing):
        assert G.decide(cmd, str(home)) is None, cmd
    for path in (existing, to_create):
        assert G.decide_tool("Write", {"file_path": str(path)}) is None, path
    # controls: the same names in a place where NOTHING exists are still refused (another user's home, a typo'd prefix), and the real
    # protected directory still refuses a file that does not exist yet
    assert G.decide("echo x > /nonexistent-home-x58/" + REL, str(home)) is None                 # a roots fixture nowhere near the real file
    assert (G.decide("echo x > /nonexistent-home-x58/" + G._lexical_rels()[0], str(home)) or "").startswith(G.CONFIG_TAG)   # config.json keeps its suffix rule
    assert (G.decide_tool("Write", {"file_path": str(home / REL) + ""}) or "").startswith(G.CONFIG_TAG)
    gone = home / os.path.dirname(REL) / "roots.toml"
    gone.unlink()                                    # the protected file is created afresh: judged by its spelled path / parent identity
    assert (G.decide("echo x > %s" % gone, str(home)) or "").startswith(G.CONFIG_TAG)


def test_no_realpath_on_the_per_command_path():
    """F1: the decision for a tool payload or a redirect target must not walk the path in Python (os.path.realpath stalled 4 to 26 s)."""
    import ast
    tree = ast.parse(open(G.__file__, encoding="utf-8").read())
    funcs = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for name in ("_is_config_path", "_protected_target", "_protected_ids", "_identity", "decide_tool", "check_config_write", "_config_analyze"):
        attrs = {n.attr for n in ast.walk(funcs[name]) if isinstance(n, ast.Attribute)}      # calls, not docstring words
        assert not ({"realpath", "exists", "lexists", "isfile", "isdir"} & attrs), (name, attrs)


def test_one_identity_snapshot_per_decision(scratch, monkeypatch):
    home, pointer = scratch
    _make_files(home, pointer)
    calls = []
    real = G._identity
    monkeypatch.setattr(G, "_identity", lambda p: (calls.append(p), real(p))[1])
    G.decide_tool("Write", {"file_path": str(home / "x1.txt")})
    n_first = len([c for c in calls if c != str(home / "x1.txt")])
    calls.clear()
    G.decide("echo a > %s; echo b > %s; echo c > %s" % (home / "a", home / "b", home / "c"), str(home))
    snapshot = [c for c in calls if c not in (str(home / "a"), str(home / "b"), str(home / "c"))]
    bound = 2 * len(G._protected_paths()) + 3                                      # a file and a directory per protected path, plus the target's parent
    assert n_first <= bound and len(snapshot) <= bound, (n_first, bound, snapshot)   # the protected files are stat'ed once, not per target


# ------------------------------------------------ mention-only commands are not writes (coordinator's corpus-pass REPORT, shapes rebuilt by hand)
def test_commands_that_only_mention_the_roots_file_are_not_refused(scratch, tmp_path):
    home, pointer = scratch
    _make_files(home, pointer)
    fx = tmp_path / "fx"
    mention_only = [
        # a script editing a spec / code file whose text names roots.toml and roots.py next to write-like words
        "python3 - <<'EOF'\nimport pathlib\nnote = 'the resolver reads roots.toml via gnomon/roots.py'\npathlib.Path('notes.md').write_text(note)\nEOF",
        "python3 -c \"open('/tmp/x58-notes.txt','w').write('see roots.toml and roots.py: pointer file')\"",
        "python3 -c \"import json; json.dump({'file': 'roots.toml'}, open('/tmp/x58-out.json','w'))\"",
        "sed -i '' 's/roots.toml/roots.toml (pointer)/' docs/NOTES.md",
        "cat > /tmp/x58-doc.md <<'EOF'\nThe pointer file roots.toml is written by the installer, and read by roots.py.\nEOF",
        "git commit -m 'document roots.toml; roots.py reads it, the installer writes it'",
        "grep -rn roots.toml gnomon/ > /tmp/x58-hits.txt",
        "echo 'roots.toml' >> /tmp/x58-list.txt",
        # fixtures: a path that spells the same relative name somewhere else, literal or through a variable the command itself assigns
        "mkdir -p %s/.config/gnomon && printf x > %s/.config/gnomon/roots.toml" % (fx, fx),
        "F=%s/.config/gnomon; mkdir -p $F; cat > $F/roots.toml <<'EOF'\ndata_root = \"/x\"\nEOF" % fx,
        "cp roots.toml roots.toml.bak",
    ]
    for cmd in mention_only:
        assert G.decide(cmd, str(home)) is None, cmd
    real_writes = [
        "echo 'data_root = \"/x\"' > ~/%s" % REL,
        "echo x > $HOME/%s" % REL,
        "mkdir -p ~/%s && cp -p /tmp/inst/roots.toml ~/%s" % (os.path.dirname(REL), REL),
        "python3 -c \"open('%s','w').write('x')\"" % (home / REL),
        "python3 -c \"import os; open(os.path.expanduser('~/%s'),'w').write('x')\"" % REL,
    ]
    for cmd in real_writes:
        reason = G.decide(cmd, str(home))
        assert reason and reason.startswith(G.CONFIG_TAG), cmd


def test_data_heredocs_scripts_handed_the_file_and_joined_literals(scratch, tmp_path):
    home, pointer = scratch
    _make_files(home, pointer)
    d = "~/" + REL
    # a heredoc that only WRITES a note / probe file is data, even when its text contains a redirect aimed at the real path
    data = "cat > /tmp/x58-probe.py <<'EOF'\nimport os\nos.system(\"echo x > %s\")\nEOF" % d
    # ROUND 3 (F1): the heredoc-stripped text is only an ADDED reading, so a redirect to a protected path INSIDE a data body is judged too (as
    # base 4ca95e9's rule already did for config.json). A body that merely MENTIONS the file stays allowed.
    assert (G.decide(data, str(home)) or "").startswith(G.CONFIG_TAG)
    mention = "cat > /tmp/x58-probe.py <<'EOF'\nprint('the pointer file is ~/%s; agents must not write it')\nEOF" % REL
    assert G.decide(mention, str(home)) is None
    # the same body piped into a shell is a command, and is refused
    piped = "cat <<'EOF' | bash\necho x > %s\nEOF" % d
    assert (G.decide(piped, str(home)) or "").startswith(G.CONFIG_TAG)
    # a script handed the protected file as its argument, whose code writes: refused; handed a scratch file: allowed
    handed = "F=%s; python3 - \"$F\" <<'EOF'\nimport sys\nopen(sys.argv[1], 'w').write('x')\nEOF" % d
    assert (G.decide(handed, str(home)) or "").startswith(G.CONFIG_TAG)
    scratch_file = tmp_path / "scratch.toml"
    ok = "F=%s; python3 - \"$F\" <<'EOF'\nimport sys\nopen(sys.argv[1], 'w').write('x')\nEOF" % scratch_file
    assert G.decide(ok, str(home)) is None
    # a path built from joined literals and held in a variable, then written
    joined = "python3 -c \"import os; p=os.path.expanduser('~/'+'%s/'+'roots.toml'); open(p,'w').write('x')\"" % os.path.dirname(REL)
    assert (G.decide(joined, str(home)) or "").startswith(G.CONFIG_TAG)
    # a variable rooted in a substitution we cannot resolve names a roots fixture, not the real file; the config file keeps its suffix rule
    assert G.decide("H=$(mktemp -d); printf x > $H/%s" % REL, str(home)) is None
    assert (G.decide("H=$(mktemp -d); printf x > $H/%s" % G._lexical_rels()[0], str(home)) or "").startswith(G.CONFIG_TAG)
    # a read of the real file inside a script is not a write
    assert G.decide("python3 -c \"print(open('%s').read())\"" % (home / REL), str(home)) is None


def test_a_stalled_stat_cannot_hold_the_decision_and_a_write_target_it_cannot_clear_is_refused(scratch, monkeypatch, tmp_path):
    """F1 (measured): one stat on a cold synced directory can block for seconds and SIGALRM cannot interrupt it. The decision still returns
    within its stat budget. DELTA D3: a WRITE or DELETE target whose identity could not be verified in time is REFUSED ("could not verify
    file identity in time; retry"), whatever its name; a READ-ONLY operand (the source of a cp) keeps the spelling-only judgement."""
    import time
    home, pointer = scratch
    slow_dir = str(tmp_path / "coldsync")
    os.makedirs(slow_dir)
    real_stat = os.stat

    def stat(path, *a, **k):
        if str(path).startswith(slow_dir):
            time.sleep(4)
        return real_stat(path, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    t = time.time()
    v = G.decide_tool("Write", {"file_path": slow_dir + "/notes.md"})
    assert (v or "").startswith(G.UNVERIFIED_TAG) and G.UNVERIFIED_TEXT in v, v
    assert time.time() - t < G.STAT_BUDGET_S + 0.5, time.time() - t
    t = time.time()
    assert (G.decide_tool("Write", {"file_path": slow_dir + "/roots.toml"}) or "").startswith((G.UNVERIFIED_TAG, G.CONFIG_TAG))
    assert time.time() - t < G.STAT_BUDGET_S + 0.5
    # a delete / write by command
    for cmd in ("rm %s/old.txt" % slow_dir, "echo a > %s/a" % slow_dir, "tee %s/b < /dev/null" % slow_dir, "cp x %s/d" % slow_dir):
        t = time.time()
        assert (G.decide(cmd, str(home)) or "").startswith(G.UNVERIFIED_TAG), cmd
        assert time.time() - t < G.STAT_BUDGET_S + 1.0, cmd
    # a read-only operand: the SOURCE of a copy to an ordinary place is not refused
    assert G.decide("cp %s/src.txt /tmp/x58-dst.txt" % slow_dir, str(home)) is None
    msg = G.block_message(G.UNVERIFIED_TAG + G.UNVERIFIED_TEXT)
    assert "could not verify file identity in time; retry" in msg and "not a rule hit" in msg


def test_a_commands_own_content_cannot_spend_the_stat_budget(scratch):
    """D3 question measured: thousands of distinct named targets that all answer at once spend a few milliseconds, nowhere near the 1.0 s
    budget, and no later target is downgraded. (A budget spent by a genuinely slow file system is the case above.)"""
    import time
    home, _pointer = scratch
    names = " ".join("/tmp/x58-budget-%d.txt" % i for i in range(3000))
    G._ID_CACHE.clear()
    t = time.time()
    v = G.decide("rm " + names, str(home))
    took = time.time() - t
    assert v is None, v
    assert G._ID_CACHE.get("spent", 0.0) < 0.5 * G.STAT_BUDGET_S and took < 2.0, (G._ID_CACHE.get("spent"), took)
    assert not G._ID_CACHE.get("ids_incomplete")

"""Delta Diamond D2 and D10 (X58 fix round 2).

D2 (ruling 2026-10-09): X58 loses NO deny against live 4ca95e9. The config rule of that base, over-match included, is a UNION member:
whatever it refuses, X58 refuses. Narrowing it is the operator's later item. The test loads the base's own guard source from this
repository's history (``git show 4ca95e9:...``, read-only) and compares verdicts on a set of command shapes built from pieces.
D10: ``_without_data_heredocs`` removes a heredoc body by SPAN (where the lexer says it stands), never the first occurrence of its text."""
import os
import subprocess
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
CLONE = os.path.dirname(HERE)
from gnomon.harness import deny_guard as G  # noqa: E402

BASE_SHA = "4ca95e9b015a09a8322a1c209619855d327e791d"


def _base_module():
    r = subprocess.run(["git", "-C", CLONE, "show", BASE_SHA + ":gnomon/harness/deny_guard.py"], capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("base commit not in this clone's history")
    m = types.ModuleType("gnomon.harness.deny_guard_base_4ca95e9")
    m.__package__ = "gnomon.harness"
    m.__file__ = os.path.join(CLONE, "gnomon", "harness", "deny_guard_base_4ca95e9.py")
    sys.modules[m.__name__] = m
    exec(compile(r.stdout, m.__file__, "exec"), m.__dict__)
    return m


def _shapes(home):
    rel = G._lexical_rels()[0]                       # "<data root dir>/config.json": the one spelling lives in the resolver
    cfg = "~/" + rel
    other = os.path.join(home, "fixture", rel)
    shapes = [
        # the over-match class: scripts and prose that carry the path as TEXT next to a write
        "python3 - <<'EOF'\np='specs/a.lts'\ns=open(p).read()\ns=s.replace('x','see %s')\nopen(p,'w').write(s)\nEOF" % cfg,
        "python3 -c \"open('notes.md','w').write('the file %s holds flags')\"" % cfg,
        "cat > doc.md <<'EOF'\nan agent must never run: echo x > %s\nEOF" % cfg,
        "git commit -m 'never write %s' && sed -i '' s/a/b/ README.md" % cfg,
        "sed -i '' 's/a/b/' %s" % other,
        "cp /tmp/a %s" % other,
        "echo x > %s" % other,
        "H=$(mktemp -d); printf x > $H/%s" % rel,
        "python3 - <<'EOF'\nimport json\np='%s'\njson.dump({}, open(p,'w'))\nEOF" % other,
        "node -e \"require('fs').writeFileSync('%s','{}')\"" % other,
        # plain writes of the real path
        "echo x > %s" % cfg, "tee %s < /dev/null" % cfg, "rm %s" % cfg, "mv /tmp/a %s" % cfg, "truncate -s 0 %s" % cfg,
        # reads and unrelated
        "cat %s" % cfg, "ls -la", "git status", "grep -rn config.json src/", "cp %s /tmp/backup.json" % cfg,
        "python3 -c \"print(open('%s').read())\"" % os.path.expanduser(cfg),
    ]
    return shapes


def test_every_command_the_base_config_rule_refuses_is_refused_here(tmp_path):
    base = _base_module()
    home = str(tmp_path)
    n_base = n_both = 0
    for cmd in _shapes(home):
        try:
            base.check_config_write(cmd)
            base_denies = False
        except base.Deny:
            base_denies = True
        tip = G.decide(cmd, home)
        if base_denies:
            n_base += 1
            assert tip is not None, "LOST a deny that live 4ca95e9's config rule has: " + cmd[:100]
            n_both += 1
    assert n_base >= 8, n_base                       # the control: the base rule really does refuse a good number of these shapes


def test_the_union_is_what_keeps_the_over_match(tmp_path):
    """The mention-only shapes stay refused (narrowing is deferred): the legacy members alone refuse them."""
    home = str(tmp_path)
    shapes = [c for c in _shapes(home) if "see " in c or "holds flags" in c or "never run" in c]
    assert shapes
    for cmd in shapes:
        with pytest.raises(G.Deny):
            G._legacy_check_config_write(cmd)
        assert G.decide(cmd, home) is not None


def test_heredoc_body_removal_is_by_span_not_by_first_occurrence():
    body = "AAAAAAAAAA"
    text = "echo %s; cat > f <<'EOF'\n%s\nEOF\n" % (body, body)
    out = G._without_data_heredocs(text)
    assert out == "echo %s; cat > f <<'EOF'\n\nEOF\n" % body, repr(out)        # the echo argument stays; the heredoc body goes


def test_heredoc_removal_never_touches_a_piped_or_interpreted_body_or_a_repeated_text():
    body = "BBBBBBBBBB > /tmp/never-written"
    piped = "cat <<'EOF' | bash\n%s\nEOF\n" % body
    assert G._without_data_heredocs(piped) == piped
    twice = "cat > a <<'E1'\n%s\nE1\ncat > b <<'E2'\n%s\nE2\n" % (body, body)
    assert G._without_data_heredocs(twice) == "cat > a <<'E1'\n\nE1\ncat > b <<'E2'\n\nE2\n"
    shell = "bash <<'EOF'\n%s\nEOF\n" % body
    assert G._without_data_heredocs(shell) == shell

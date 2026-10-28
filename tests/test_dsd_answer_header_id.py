"""every per-DSD answer file written by the
real writer carried frontmatter ``id: SPEC:<CORPUS>`` (the shared-file id), so ~108 files shared
one frontmatter id. Now each new file's frontmatter id equals its OWN root heading id.
Existing files are not rewritten by code (the corpus owner backfills them)."""

import os
import re

import pytest

from gnomon.dsd import (
    DecisionAnswer, TypedLtsDecisionWriter, factlattice_available, mint_dsd_nodes, parse_dsd,
)
from gnomon.dsd import persist

pytestmark = pytest.mark.skipif(not factlattice_available(),
                                reason="factlattice not importable in this environment")

_MD = ("### D1 · Should we do thing one?\n**What this is:** a.\n**Why it matters:** b.\n"
       "| Option | Tradeoffs |\n|---|---|\n| **Yes** | fast |\n| No | slow |\n**Your call:** ____\n")


def _nodes(dsd_id):
    doc = parse_dsd(_MD)
    answers = {d.id: DecisionAnswer(labels=["Yes"], comment="ok") for d in doc.decisions}
    return mint_dsd_nodes(dsd_id, "alpha-x", doc.decisions, answers, decided_on="2026-10-08")


def _front_and_root(path):
    text = open(path, encoding="utf-8").read()
    fm = re.match(r"---\n(.*?)\n---\n", text, re.S).group(1)
    fid = re.search(r'^id:\s*"([^"]+)"', fm, re.M).group(1)
    root = re.search(r"^# .*\[(SPEC:[A-Z0-9-]+)\]\s*$", text, re.M).group(1)
    return fid, root


def test_two_answer_files_from_the_real_writer_have_distinct_frontmatter_ids_equal_to_their_root_heading(tmp_path):
    d = tmp_path / "dsd-answers"
    d.mkdir()
    w = TypedLtsDecisionWriter(str(d / "dsd-answers.lts"))
    a = w.mint(_nodes("dsd-aaa"))
    b = w.mint(_nodes("dsd-bbb"))
    fa, ra = _front_and_root(a)
    fb, rb = _front_and_root(b)
    assert fa == ra and fb == rb
    assert fa != fb
    assert fa.endswith("-DSD-AAA") and fb.endswith("-DSD-BBB")
    # every NEW answer id carries the product's own GM- spec-id prefix, whatever the deployment's corpus id is
    assert fa == "SPEC:GM-DSD-ANSWERS-DSD-AAA" and fb == "SPEC:GM-DSD-ANSWERS-DSD-BBB"


def test_a_shard_header_uses_the_gm_prefix_under_any_deployment_corpus_id(monkeypatch):
    """The prefix does not follow the corpus id (it used to: SPEC:<CORPUS>-<DSD-ID>); the shared-file header still does."""
    from gnomon.dsd import live, writer

    monkeypatch.setattr(live, "dsd_settings", lambda: {"corpus_id": "some-other-answers", "domain": "X.Y", "product": "P"})
    fid, root = _front_and_root_text(writer._shard_header("dsd-ccc"))
    assert fid == root == "SPEC:GM-DSD-ANSWERS-DSD-CCC"
    assert _front_and_root_text(persist.corpus_header())[0] == "SPEC:SOME-OTHER-ANSWERS"


def test_both_files_still_parse_and_lint_clean_on_a_scratch_copy(tmp_path):
    import shutil
    import factlattice
    from factlattice_engine.lint import lint

    d = tmp_path / "dsd-answers"
    d.mkdir()
    w = TypedLtsDecisionWriter(str(d / "dsd-answers.lts"))
    files = [w.mint(_nodes("dsd-aaa")), w.mint(_nodes("dsd-bbb"))]
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for f in files:
        shutil.copy(f, scratch / os.path.basename(f))
        assert any(n.id.startswith("DEC-DSD-") for n in factlattice.parse_file(f).nodes)
    res = lint(str(scratch))
    errors = [f for f in res.findings if getattr(f, "severity", "") == "error"]
    assert not errors, errors
    assert res.node_count >= 4


def test_the_shared_file_header_default_is_unchanged():
    h = persist.corpus_header()
    fid, root = _front_and_root_text(h)
    assert fid == root and ("-DSD-" not in fid)


def _front_and_root_text(text):
    fid = re.search(r'^id:\s*"([^"]+)"', text, re.M).group(1)
    root = re.search(r"^# .*\[(SPEC:[A-Z0-9-]+)\]\s*$", text, re.M).group(1)
    return fid, root


def test_each_root_id_is_defined_exactly_once_across_a_corpus_of_shards_and_a_default_header_file(tmp_path):
    """The defect's own acceptance: root + several real-writer shards in one directory; every
    frontmatter id AND every root heading id appears once, and the root heading id of each file
    equals its frontmatter id."""
    from collections import Counter

    d = tmp_path / "dsd-answers"
    d.mkdir()
    root_file = d / "00-root.lts"
    root_file.write_text(persist.corpus_header(), encoding="utf-8")  # the default (shared-file) header
    w = TypedLtsDecisionWriter(str(d / "dsd-answers.lts"))
    for n in ("dsd-aaa", "dsd-bbb", "dsd-ccc", "dsd-ddd"):
        w.mint(_nodes(n))
    front, roots = Counter(), Counter()
    for name in sorted(os.listdir(d)):
        if not name.endswith(".lts"):
            continue
        fid, rid = _front_and_root(str(d / name))
        assert fid == rid, name
        front[fid] += 1
        roots[rid] += 1
    assert len(front) == 5, front          # the default file + 4 shards
    assert all(c == 1 for c in front.values()), front
    assert all(c == 1 for c in roots.values()), roots

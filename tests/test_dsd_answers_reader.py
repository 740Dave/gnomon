"""O-F1 / D-F1 (Diamond, pre-review part 2): ONE shared reader of the answers corpus for the inline notice, my_dsds and the helper.

The corpus is a DIRECTORY: one file per DSD (``<dsd_id>.lts``, written by the production TYPED writer since 2026-10-02) plus the legacy
single file. Every test here MINTS WITH THE REAL TypedLtsDecisionWriter (two shards) and the legacy writer (the legacy file); the earlier
tests minted with the single-file writer only, which is why they could not see the defect.
"""
from __future__ import annotations

import os
import stat

import pytest

from gnomon.config import GnomonConfig
from gnomon.dsd import answer_stats as AS
from gnomon.dsd import dsd_ops as OPS
from gnomon.dsd import notice as N
from gnomon.dsd.answers_reader import AnswersReadError, read_answers
from gnomon.dsd.model import DecisionAnswer, parse_dsd
from gnomon.dsd.persist import LtsFileDecisionWriter, mint_dsd_nodes
from gnomon.dsd.writer import TypedLtsDecisionWriter, factlattice_available
from test_dsd_ops import TWO, ME, root  # noqa: F401

pytestmark = pytest.mark.skipif(not factlattice_available(), reason="the typed writer needs factlattice")

A, B, C = "dsd-1a0e00000a", "dsd-1a0e00000b", "dsd-1a0e00000c"
F = {"sent_epoch": 10.0, "answered_epoch": 70.0}


def answers(c1="yes"):
    return {"D1": DecisionAnswer(labels=["Yes"], comment=c1), "D2": DecisionAnswer(labels=["Later"], comment="")}


def nodes(dsd_id, c1="yes", fields=True):
    return mint_dsd_nodes(dsd_id, ME, parse_dsd(TWO).decisions, answers(c1), decided_on="2026-10-05",
                          **({"answer_fields": F} if fields else {}))


@pytest.fixture
def corpus(tmp_path):
    d = tmp_path / "dsd-answers"
    d.mkdir()
    legacy = str(d / "dsd-answers.lts")
    typed = TypedLtsDecisionWriter(legacy)
    typed.mint(nodes(A, "from shard A"))
    typed.mint(nodes(B, "from shard B"))
    LtsFileDecisionWriter(legacy).mint(nodes(C, "from the legacy file", fields=False))     # an older answer, in the legacy file
    (d / "00-conventions.lts").write_text("## not an answer [DEC-DSD-ZZZ-D1]\n\n- chosen: x\n")
    return {"dir": str(d), "legacy": legacy}


def test_the_typed_writer_really_wrote_one_file_per_dsd(corpus):
    names = sorted(os.listdir(corpus["dir"]))
    assert f"{A}.lts" in names and f"{B}.lts" in names and "dsd-answers.lts" in names


def test_per_dsd_file_first_then_legacy_and_files_read_is_reported(corpus):
    got = read_answers(corpus["legacy"], A)
    assert [n["node_id"] for n in got["nodes"]] == [N_ for N_ in ("DEC-DSD-1A0E00000A-D1", "DEC-DSD-1A0E00000A-D2")]
    assert got["files_read"] == [os.path.join(corpus["dir"], f"{A}.lts"), corpus["legacy"]]      # shard first, legacy second
    legacy_only = read_answers(corpus["legacy"], C)
    assert [n["props"]["comment"] for n in legacy_only["nodes"]][:1] == ["from the legacy file"]
    assert legacy_only["files_read"] == [corpus["legacy"]]
    assert read_answers(corpus["legacy"], "dsd-1a0e0000ff")["nodes"] == []                      # unknown DSD: nothing, no error


def test_a_directory_or_a_file_is_accepted_and_the_whole_corpus_is_read(corpus):
    for p in (corpus["dir"], corpus["legacy"]):
        got = read_answers(p)
        assert len(got["nodes"]) == 6 and len(got["files_read"]) == 3
        assert not any("00-" in os.path.basename(f) for f in got["files_read"])               # the conventions file is not an answer


def test_the_inline_notice_finds_a_shard_only_dsd(corpus):
    cfg = GnomonConfig(flags={"dsd_notice_inline": True}, dsd_answers_corpus_path=corpus["legacy"])
    t = N.inline_text_for(A, "DEC-DSD-1A0E00000A", config=cfg)
    assert "| comment: from shard A" in t.split("\n") and "option 1 of 2" in t


def test_my_dsds_lists_the_answers_of_a_shard_only_dsd(root, corpus):
    out = OPS.send(ME, TWO, root=root)["dsd_id"]
    # answer it the way the dashboard does: nodes into a shard, then flip the record
    typed = TypedLtsDecisionWriter(corpus["legacy"])
    typed.mint(nodes(out, "shard comment"))
    from gnomon.dsd.inbox import DsdInbox
    DsdInbox(root).mark_answered(out, "DEC-DSD-" + out[4:].upper())
    row = OPS.list_mine(ME, root=root, answers_path=corpus["legacy"])["dsds"][0]
    assert row["status"] == "answered" and [a["comment"] for a in row["answers"]] == ["shard comment", "(none)"]
    assert "answers_error" not in row


def test_the_helper_counts_over_the_whole_directory_and_reports_the_files(corpus):
    agg = AS.group_counts(corpus["dir"])
    assert agg["total"] == 6 and agg["with_fields"] == 4 and agg["without_fields"] == 2
    assert len(agg["files_read"]) == 3 and agg["groups"] == {("1", "1"): 2, ("1", "2"): 2}
    assert AS.group_counts(corpus["legacy"])["total"] == 6                                   # the configured file reaches the directory too


def test_an_unreadable_path_raises_everywhere_it_is_used(corpus, tmp_path, root):
    gone = str(tmp_path / "nowhere" / "dsd-answers.lts")
    with pytest.raises(AnswersReadError):
        read_answers(gone)
    with pytest.raises(AnswersReadError):
        AS.group_counts(gone)                                                                 # the helper reports the error, not zeros
    cfg = GnomonConfig(flags={"dsd_notice_inline": True}, dsd_answers_corpus_path=gone)
    with pytest.raises(AnswersReadError):
        N.inline_text_for(A, "DEC-DSD-1A0E00000A", config=cfg)                               # so the inbox falls back to the pointer line
    dsd = OPS.send(ME, TWO, root=root)["dsd_id"]
    from gnomon.dsd.inbox import DsdInbox
    DsdInbox(root).mark_answered(dsd, "DEC-DSD-" + dsd[4:].upper())
    row = OPS.list_mine(ME, root=root, answers_path=gone)["dsds"][0]
    assert row["answers"] is None and "AnswersReadError" in row["answers_error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads everything")
def test_an_unreadable_shard_raises(corpus):
    shard = os.path.join(corpus["dir"], f"{A}.lts")
    os.chmod(shard, 0)
    try:
        with pytest.raises(AnswersReadError):
            read_answers(corpus["legacy"], A)
        with pytest.raises(AnswersReadError):
            read_answers(corpus["dir"])
    finally:
        os.chmod(shard, stat.S_IRUSR | stat.S_IWUSR)


def test_a_shard_that_is_a_directory_raises(corpus):
    os.mkdir(os.path.join(corpus["dir"], "dsd-1a0e0000ee.lts"))
    with pytest.raises(AnswersReadError):
        read_answers(corpus["legacy"], "dsd-1a0e0000ee")


def test_the_inbox_falls_back_to_the_pointer_when_the_corpus_is_unreadable(tmp_path):
    from test_dsd_inline_notice import POINTER, Rig
    gone = str(tmp_path / "nowhere" / "dsd-answers.lts")
    cfg = GnomonConfig(flags={"dsd_notice_inline": True}, dsd_answers_corpus_path=gone)
    rig = Rig(lambda d, r: N.inline_text_for(d, r, config=cfg))
    rig.offer()
    assert rig.typed == [POINTER] and any(r.get("decision") == "inline-fallback" for r in rig.records)

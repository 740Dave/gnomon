"""B2 (pre-review batch, spec section 4): the INLINE ANSWER in the notice, flag ``dsd_notice_inline`` (default OFF), and C10.

The text is built from the MINTED answer nodes in the corpus (never a request body, a notice file or a frame), typed only after the
harness re-screens it, and any failure at all means the agent gets TODAY's pointer line. NOT built here: the timed release of a held
notice (condition C4; see MEASURE-held-notice-release-2026-10-05.md).
"""
from __future__ import annotations

import pytest

from gnomon import flags as F
from gnomon.config import GnomonConfig
from gnomon.dsd import notice as N
from gnomon.dsd.model import DecisionAnswer, parse_dsd
from gnomon.dsd.persist import LtsFileDecisionWriter, mint_dsd_nodes
from gnomon.harness.hooks import Decision, Outcome
from gnomon.harness.leading_bang import has_leading_bang, screen_text
from gnomon.harness.notice_inbox import DsdNoticeInbox
from test_dsd_ops import TWO

DSD = "dsd-1a0e00000a"
ROOT = "DEC-DSD-1A0E00000A"
POINTER = N.notice_line(DSD, ROOT)


def nodes_for(comment1="go for it", comment2="", labels=("Yes", "Later"), fields=True):
    doc = parse_dsd(TWO)
    answers = {"D1": DecisionAnswer(labels=[labels[0]], comment=comment1), "D2": DecisionAnswer(labels=[labels[1]], comment=comment2)}
    minted = mint_dsd_nodes(DSD, "alpha-agent", doc.decisions, answers, decided_on="2026-10-05",
                            **({"answer_fields": {"sent_epoch": 10.0, "answered_epoch": 70.0}} if fields else {}))
    return [{"node_id": n.node_id, "props": {k: v for k, v in n.properties.items()}} for n in minted[1:]]


def corpus(tmp_path, **kw):
    path = str(tmp_path / "dsd-answers.lts")
    doc = parse_dsd(TWO)
    answers = {"D1": DecisionAnswer(labels=["Yes"], comment=kw.get("c1", "ship it")), "D2": DecisionAnswer(labels=["Later"], comment="")}
    LtsFileDecisionWriter(path).mint(mint_dsd_nodes(DSD, "alpha-agent", doc.decisions, answers, decided_on="2026-10-05",
                                                    answer_fields={"sent_epoch": 1.0, "answered_epoch": 9.0}))
    return path


# --------------------------------------------------------------------------- the text

def test_the_inline_text_carries_ref_option_position_match_comment_and_node():
    t = N.compose_inline_notice(DSD, ROOT, nodes_for())
    lines = t.split("\n")
    assert lines[0] == POINTER                                              # today's pointer line stays the first line
    assert "| D1: chose option 1 of 2: Yes; matched your recommendation: yes" in lines
    assert "| comment: go for it" in lines and "| node: DEC-DSD-1A0E00000A-D1" in lines
    assert "| D2: chose option 2 of 2: Later; matched your recommendation: no" in lines
    assert "| comment: (none)" in lines and "| node: DEC-DSD-1A0E00000A-D2" in lines


def test_nodes_minted_without_the_fields_still_give_text_and_just_omit_the_position():
    t = N.compose_inline_notice(DSD, ROOT, nodes_for(fields=False))
    assert "| D1: chose Yes; matched your recommendation: yes" in t.split("\n")


def test_the_comment_is_verbatim_apart_from_control_characters():
    c = "Yes, but only after the demo; ¿qué tal? 日本語 — and a | pipe and `ticks`"
    t = N.compose_inline_notice(DSD, ROOT, nodes_for(comment1=c))
    assert "| comment: " + c in t.split("\n")


def test_a_comment_starting_with_a_bang_cannot_reach_the_line_as_a_bang():
    t = N.compose_inline_notice(DSD, ROOT, nodes_for(comment1="!rm -rf the build"))
    assert not has_leading_bang(t) and screen_text(t) is None
    assert all(ln.startswith("| ") for ln in t.split("\n")[1:]) and t[0] == "["
    assert N.validate_inline_text(t, DSD, ROOT) is None
    assert "| comment: !rm -rf the build" in t.split("\n")      # still shown verbatim, but never at the start of a line


def test_an_escape_sequence_and_a_newline_in_a_comment_are_shown_not_acted_on():
    nasty = "ok\x1b[2J\x1b]0;x\x07\nnext line \r\x7f\x00\u2028\x85 end"
    nodes = nodes_for()
    nodes[0]["props"]["comment"] = nasty            # straight onto the node: a node file can hold what the mint would have collapsed
    t = N.compose_inline_notice(DSD, ROOT, nodes)
    assert not any(ord(ch) < 0x20 and ch not in "\n" for ch in t) and "\x7f" not in t and "\x1b" not in t
    assert "\u2028" not in t and "\x85" not in t
    assert "\\x1b[2J" in t and "\\x07" in t and "\\x00" in t and "\\u2028" in t
    assert all(ln.startswith("| ") for ln in t.split("\n")[1:])           # the embedded newline cannot open a bare line
    assert screen_text(t) is None and N.validate_inline_text(t, DSD, ROOT) is None


def test_a_comment_that_went_through_the_mint_cannot_carry_a_newline_either(tmp_path):
    nodes = nodes_for(comment1="line one\nline two")
    assert "\n" not in nodes[0]["props"]["comment"]


def test_over_4000_characters_the_comments_are_cut_at_a_stated_length():
    big = "x" * 3000
    nodes = nodes_for(comment1=big, comment2=big)
    t = N.compose_inline_notice(DSD, ROOT, nodes)
    assert len(t) <= N.INLINE_LIMIT_CHARS and N.INLINE_CUT_MARK in t
    assert "(comments over" in t.split("\n")[-1] and "characters are cut; full text at the node" in t.split("\n")[-1]
    assert t.count(N.INLINE_CUT_MARK) >= 2 and N.validate_inline_text(t, DSD, ROOT) is None
    small = N.compose_inline_notice(DSD, ROOT, nodes_for(comment1="short"))
    assert N.INLINE_CUT_MARK not in small


def test_too_many_decisions_to_fit_even_cut_raises_so_the_caller_falls_back():
    many = nodes_for() * 60
    with pytest.raises(ValueError):
        N.compose_inline_notice(DSD, ROOT, many)
    with pytest.raises(ValueError):
        N.compose_inline_notice(DSD, ROOT, [])


@pytest.mark.parametrize("bad,why", [
    ("!hello", "pointer"), (POINTER + "\nno prefix", "prefix"), (POINTER + "\n| ok\x1b[2J", "screens"),
    (POINTER + "\n" + "| x" * 3000, "length"), (123, "not text"), (POINTER + "\n| \\fine\n/compact", "prefix")])
def test_validate_inline_text_refuses_each_of_these(bad, why):
    assert N.validate_inline_text(bad, DSD, ROOT) is not None


# --------------------------------------------------------------------------- built from the minted nodes, flag-gated

def _cfg(on):
    return GnomonConfig(flags={"dsd_notice_inline": bool(on)})


def test_inline_text_for_is_built_from_the_corpus_nodes_not_from_anything_else(tmp_path):
    path = corpus(tmp_path, c1="from the node")
    t = N.inline_text_for(DSD, ROOT, config=_cfg(True), answers_path=path)
    assert "| comment: from the node" in t.split("\n") and t.split("\n")[0] == POINTER


def test_flag_off_means_none_so_the_notice_is_todays_pointer(tmp_path):
    assert N.inline_text_for(DSD, ROOT, config=_cfg(False), answers_path=corpus(tmp_path)) is None
    assert F.KNOWN_FLAGS["dsd_notice_inline"].default is False


def test_flag_on_with_no_nodes_raises_so_the_caller_falls_back(tmp_path):
    with pytest.raises(ValueError):
        N.inline_text_for(DSD, ROOT, config=_cfg(True), answers_path=str(tmp_path / "missing.lts"))


# --------------------------------------------------------------------------- the inbox: C10 fallbacks

class Rig:
    def __init__(self, inline=None, deny_inline=False):
        self.typed, self.records = [], []
        self.deny_inline = deny_inline

        class Gate:
            paused = False

            def record(g, kind, **kw):
                self.records.append(kw)
        self.inbox = DsdNoticeInbox(inject=self._inject, ack=lambda f: None, gate=Gate(), quiet_seconds=0.0, ready=lambda: True,
                                    inline_text=inline)

    def _inject(self, text, **kw):
        self.typed.append(text)
        if self.deny_inline and text != POINTER:
            return Outcome(Decision.DENY, "refused by a hook")
        return Outcome(Decision.ALLOW)

    def offer(self):
        return self.inbox.offer({"dsd_id": DSD, "root_id": ROOT, "text": "ignored"})


def good_inline(d, r):
    return N.compose_inline_notice(d, r, nodes_for(comment1="hello"))


def test_with_no_builder_or_flag_off_the_typed_text_is_exactly_todays():
    for inline in (None, lambda d, r: None):
        rig = Rig(inline)
        assert rig.offer()["decision"] == "delivered"
        assert rig.typed == [POINTER] and not [r for r in rig.records if r.get("decision") == "inline-fallback"]


def test_a_good_inline_text_is_typed_in_place_of_the_pointer():
    rig = Rig(good_inline)
    rig.offer()
    assert len(rig.typed) == 1 and rig.typed[0].startswith(POINTER + "\n| D1:")


def test_a_builder_that_raises_falls_back_to_the_pointer_and_says_so():
    def boom(d, r):
        raise OSError("corpus unreadable")
    rig = Rig(boom)
    rig.offer()
    assert rig.typed == [POINTER]
    fb = [r for r in rig.records if r.get("decision") == "inline-fallback"]
    assert fb and fb[0]["stage"] == "build" and "corpus unreadable" in fb[0]["reason"]


@pytest.mark.parametrize("bad", ["!rm -rf x", POINTER + "\nbare line", POINTER + "\n| esc\x1b[2J", "something else entirely", POINTER + "\n" + "| y\n" * 4000])
def test_a_builder_that_returns_an_unsafe_text_is_never_typed(bad):
    rig = Rig(lambda d, r: bad)
    rig.offer()
    assert rig.typed == [POINTER]
    assert any(r.get("decision") == "inline-fallback" for r in rig.records)


def test_an_inline_text_refused_at_delivery_falls_back_to_the_pointer_notice():
    rig = Rig(good_inline, deny_inline=True)
    first = rig.offer()
    assert first["decision"] == "held"                       # the hook refused the inline text; not acked, not lost
    rig.inbox.tick()
    assert rig.typed[0].startswith(POINTER + "\n") and rig.typed[-1] == POINTER and len(rig.typed) == 2
    fb = [r for r in rig.records if r.get("decision") == "inline-fallback"]
    assert fb and fb[0]["stage"] == "deliver"
    assert [r["decision"] for r in rig.records if r.get("decision") in ("written", "acked")] == ["written", "acked"]


def test_the_notice_file_text_is_still_never_typed():
    rig = Rig(None)
    rig.inbox.offer({"dsd_id": DSD, "root_id": ROOT, "text": "!rm -rf /\x1b[2J"})
    assert rig.typed == [POINTER]


def test_through_the_real_injector_the_typed_bytes_carry_one_paste_wrapper_and_no_other_escape():
    """The multi-line inline text goes through the ordinary inject path: one bracketed paste (its own delimiters), then the CR."""
    from gnomon.harness import Injector, SingleWriteSite

    sent = []
    site = SingleWriteSite(master_fd=-1, writer=lambda fd, data: sent.append(bytes(data)) or len(data))
    inj = Injector(site, sleep=lambda s: None)
    nodes = nodes_for(comment1="!rm -rf x \x1b[2J")
    nodes[0]["props"]["comment"] = "!rm -rf x \x1b[2J \x1b[201~ evil"             # even a paste-closing delimiter inside a comment
    text = N.compose_inline_notice(DSD, ROOT, nodes)
    inj.inject_line(text, clear=False, source="dsd-answer", trigger="dsd-answer")
    wire = b"".join(sent)
    assert wire.count(b"\x1b[200~") == 1 and wire.count(b"\x1b[201~") == 1 and wire.endswith(b"\r")
    body = wire.replace(b"\x1b[200~", b"").replace(b"\x1b[201~", b"")
    assert b"\x1b" not in body and body.count(b"\r") == 1


def test_a_hold_is_not_closed_by_an_inline_fallback_record():
    import json, os, time, tempfile
    import backend.server as srv
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".claude"))
    now = time.time()
    recs = [{"kind": "dsd-notice", "decision": "held", "dsd_id": DSD, "pid": 1, "ts": now - 500},
            {"kind": "dsd-notice", "decision": "inline-fallback", "dsd_id": DSD, "pid": 1, "ts": now - 400}]
    open(os.path.join(d, ".claude", "injection_log.jsonl"), "w").write("".join(json.dumps(r) + "\n" for r in recs))
    assert srv._session_held_notice_count(d, now=now) == 1

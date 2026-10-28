"""docs/design-principles.md must be exactly what gen_design_principles_md.py renders from
docs/design-principles.lts, and the render must carry every node and every principle,
implication and test line of the source.

Layout it finds, in order: env DP_LTS / DP_MD / DP_GEN (explicit paths), then a repo with
docs/design-principles.{lts,md} and the generator beside this test's repo root (scripts/ or
tools/ or the root), then the folder this test sits in (the scratch layout).

Run: python3 -m pytest test_design_principles_in_sync.py   (or: python3 test_design_principles_in_sync.py)
No temporary files are created; the negative controls work on strings in memory.
"""
import importlib.util
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
GEN_NAME = "gen_design_principles_md.py"


def _find():
    env = (os.environ.get("DP_LTS"), os.environ.get("DP_MD"), os.environ.get("DP_GEN"))
    if all(env):
        return tuple(pathlib.Path(p) for p in env)
    for root in (HERE.parent, HERE):
        lts, md = root / "docs" / "design-principles.lts", root / "docs" / "design-principles.md"
        gens = [root / d / GEN_NAME for d in ("scripts", "tools", ".")]
        gens = [g for g in gens if g.is_file()]
        if lts.is_file() and gens:
            return lts, md, gens[0]
    lts, md, gen = HERE / "design-principles.lts", HERE / "design-principles.md", HERE / GEN_NAME
    if lts.is_file() and gen.is_file():
        return lts, md, gen
    raise AssertionError("cannot locate design-principles.lts / .md / generator (a missing input is a failure, not a skip)")


LTS, MD, GEN = _find()
_spec = importlib.util.spec_from_file_location("gen_design_principles_md", GEN)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

LTS_TEXT = LTS.read_text(encoding="utf-8")


def test_md_exists():
    assert MD.is_file(), f"{MD} does not exist: run {GEN_NAME} --out {MD}"


def test_md_is_exactly_what_the_generator_renders():
    assert MD.read_text(encoding="utf-8") == gen.render(LTS_TEXT), (
        f"{MD.name} is out of sync with {LTS.name}: regenerate with {GEN_NAME} --out; never edit the .md by hand")


def test_header_says_generated():
    first = MD.read_text(encoding="utf-8").split("\n", 1)[0]
    assert first == f"> Generated from design-principles.lts by {GEN_NAME}; do not edit by hand."


def test_every_node_and_every_principle_line_is_carried():
    md = MD.read_text(encoding="utf-8")
    md_lines = set(md.split("\n"))
    ids = re.findall(r"^#{1,6} .*\[([A-Z][A-Z0-9-]*)\]\s*$", LTS_TEXT, re.M)
    assert len(ids) == len(set(ids)) and len(ids) >= 28, ids
    dp = [i for i in ids if i.startswith("DP-GN-")]
    ep = [i for i in ids if i.startswith("EP-GN-")]
    assert len(dp) == 23 and len(ep) == 5, (len(dp), len(ep))
    for line in LTS_TEXT.split("\n"):
        if (line.startswith("#") or line.startswith("> ") or line.startswith("- implication:")
                or line.startswith("- test:")):
            assert line in md_lines, f"missing from the .md: {line[:100]}"
    assert md.count("- test:") == LTS_TEXT.count("- test:")
    assert md.count("- implication:") == LTS_TEXT.count("- implication:")


# ---- the test must be able to fail (negative controls, all in memory) ----

def test_control_a_hand_edit_of_the_md_is_detected():
    good = gen.render(LTS_TEXT)
    assert good != good.replace("Gnomon", "Gnomen", 1)
    assert good != good + "x\n"


def test_control_a_change_to_the_lts_changes_the_render():
    changed = LTS_TEXT.replace("the design has failed.", "the design has FAILED.", 1)
    assert changed != LTS_TEXT
    assert gen.render(changed) != gen.render(LTS_TEXT)


def test_control_a_node_without_a_test_line_is_refused():
    broken = LTS_TEXT.replace("- test:", "- tst:", 1)
    try:
        gen.render(broken)
    except gen.SourceError:
        return
    raise AssertionError("generator accepted a node with no test line")


def test_control_a_duplicate_node_is_refused():
    broken = LTS_TEXT.replace("[DP-GN-2]", "[DP-GN-1]", 1)
    try:
        gen.render(broken)
    except gen.SourceError:
        return
    raise AssertionError("generator accepted a duplicate node id")


def test_control_a_wrong_count_is_refused():
    broken = LTS_TEXT.replace("- principle-count: 23", "- principle-count: 24", 1)
    assert broken != LTS_TEXT
    try:
        gen.render(broken)
    except gen.SourceError:
        return
    raise AssertionError("generator accepted a wrong principle-count")


if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                fails += 1
                print("FAIL", name, e)
    sys.exit(1 if fails else 0)

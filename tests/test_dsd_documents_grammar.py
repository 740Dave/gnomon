"""(1) GRAMMAR: the optional ``**Documents:**`` line, at DSD level and per decision,
';'-separated; ``parse_dsd`` carries ``documents: [{path, name}]``. Existing DSDs without the line
parse exactly as before (the live differential over every real record is tools/dsd_documents_parse_differential.py,
exercised by test_dsd_documents_differential.py)."""
from dsd_documents_support import SAMPLE_DOCS
from gnomon.dsd import parse_dsd, validate_dsd
from gnomon.dsd.model import MAX_DOCS_PER_SCOPE, parse_documents_line

NO_DOCS = SAMPLE_DOCS.replace("**Documents:** {top}\n", "").replace("**Documents:** {d1a}; {d1b}\n", "")


def test_dsd_level_and_per_decision_documents_with_basename_names():
    doc = parse_dsd(SAMPLE_DOCS.format(top="/r/top-notes.md", d1a="~/a/design.pdf", d1b="/r/data.csv"))
    assert doc.documents == [{"path": "/r/top-notes.md", "name": "top-notes.md"}]
    d1, d2 = doc.decisions
    assert d1.documents == [{"path": "~/a/design.pdf", "name": "design.pdf"},
                            {"path": "/r/data.csv", "name": "data.csv"}]
    assert d2.documents == []


def test_documents_line_does_not_leak_into_the_context_paragraph():
    # the Documents line sits between What-this-is and Why-it-matters: it must end the
    # soft-wrapped paragraph, not be swallowed into it
    doc = parse_dsd(SAMPLE_DOCS.format(top="/r/t.md", d1a="/r/a.md", d1b="/r/b.md"))
    d1 = doc.decisions[0]
    assert d1.what_this_is == "the release."
    assert d1.why_it_matters == "the demo."
    assert validate_dsd(doc).ok


def test_whitespace_backticks_and_empty_entries():
    assert parse_documents_line(" `/a/b c.md` ;; ; /x/y.pdf ;") == [
        {"path": "/a/b c.md", "name": "b c.md"}, {"path": "/x/y.pdf", "name": "y.pdf"}]
    assert parse_documents_line("") == [] and parse_documents_line(" ; ; ") == []
    assert parse_documents_line("/a/dir/")[0]["name"] == "dir"      # trailing slash: the directory's name


def test_only_the_strict_bold_form_matches():
    # a prose line that merely starts "Documents:" is NOT reinterpreted (existing DSDs stay byte-identical)
    for line in ("Documents: /r/a.md", "**Document:** /r/a.md", "- Documents: /r/a.md", "**documents:** /r/a.md",
                 "See **Documents:** /r/a.md"):
        md = NO_DOCS.replace("# A DSD\n", "# A DSD\n" + line + "\n")
        doc = parse_dsd(md)
        assert doc.documents == [], line


def test_documents_line_after_the_first_decision_is_not_dsd_level():
    md = NO_DOCS + "**Documents:** /r/late.md\n"
    doc = parse_dsd(md)
    assert doc.documents == []
    assert doc.decisions[-1].documents == [{"path": "/r/late.md", "name": "late.md"}]


def test_no_documents_line_means_empty_lists_everywhere():
    doc = parse_dsd(NO_DOCS)
    assert doc.documents == [] and all(d.documents == [] for d in doc.decisions)


def test_cap_per_scope():
    many = "; ".join(f"/r/f{i}.md" for i in range(MAX_DOCS_PER_SCOPE + 15))
    doc = parse_dsd(SAMPLE_DOCS.format(top=many, d1a=many, d1b="/r/z.md"))
    assert len(doc.documents) == MAX_DOCS_PER_SCOPE
    assert len(doc.decisions[0].documents) == MAX_DOCS_PER_SCOPE


def test_write_back_preserves_the_documents_lines_and_decisions_parse_identically():
    md = SAMPLE_DOCS.format(top="/r/t.md", d1a="/r/a.md", d1b="/r/b.md")
    doc = parse_dsd(md)
    from gnomon.dsd import DecisionAnswer
    out = doc.render_answers({"D1": DecisionAnswer(labels=["Yes"], comment="ok")}, mark_answered=False)
    assert "**Documents:** /r/t.md" in out and "**Documents:** /r/a.md; /r/b.md" in out
    again = parse_dsd(out)
    assert again.documents == doc.documents and again.decisions[0].documents == doc.decisions[0].documents


def test_documents_line_is_not_mistaken_for_an_options_row_or_slot():
    doc = parse_dsd(SAMPLE_DOCS.format(top="/r/a|b.md", d1a="/r/a.md", d1b="/r/b.md"))
    # a pipe in a DSD-level path (before any decision) cannot create options; decision options stay 2 + recommendation intact
    assert [o.label for o in doc.decisions[0].options] == ["Yes", "No"]
    assert doc.decisions[0].recommended_id is not None

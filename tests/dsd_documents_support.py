"""Shared fixtures for the (DSD documents) tests: a hermetic roots tree under tmp_path
(g31: nothing here touches the real home, the data root or the hub run base) and sample DSD markdown."""
import hashlib
import os

SAMPLE_DOCS = """\
# A DSD
**Documents:** {top}
## Theme
### D1 · Ship it?
**What this is:** the release.
**Documents:** {d1a}; {d1b}
**Why it matters:** the demo.
| Option | Tradeoffs |
|---|---|
| **Yes** | fast |
| No | slow |
**My recommendation:** Yes
**Your call:** ____
### D2 · Announce it?
**What this is:** the blog post.
**Why it matters:** the launch.
| Option | Tradeoffs |
|---|---|
| **Now** | loud |
| Later | quiet |
**Your call:** ____
"""


def sha(path):
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def make_tree(tmp_path):
    """root (allow-listed) + outside (NOT allow-listed) dirs, with a few real files."""
    root = tmp_path / "docs-root"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    top = root / "top-notes.md"
    top.write_text("top notes v1\n")
    d1a = root / "design.pdf"
    d1a.write_bytes(b"%PDF-1.4 fake\n")
    d1b = root / "data.csv"
    d1b.write_text("a,b\n1,2\n")
    return {"root": str(root), "outside": str(outside), "top": str(top), "d1a": str(d1a), "d1b": str(d1b)}


def docs_markdown(t):
    return SAMPLE_DOCS.format(top=t["top"], d1a=t["d1a"], d1b=t["d1b"])

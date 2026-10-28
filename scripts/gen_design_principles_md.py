#!/usr/bin/env python3
"""Render docs/design-principles.md from docs/design-principles.lts.

The .lts file is the source of truth; the .md is a generated reading copy for people who
do not use lts-engine. Nothing is added to the wording: every line of the body is carried
over as it stands, in order. The front matter's descriptive keys become a short list under
the header.

This is a plain parser (Python standard library only), not the lts-engine, so it runs in a
fresh clone with nothing installed.

usage:
  gen_design_principles_md.py [LTS]              print the markdown to stdout
  gen_design_principles_md.py [LTS] --out MD     write it to MD (MD must not be an existing directory)
  gen_design_principles_md.py [LTS] --check MD   exit 1 if MD differs from what this script renders

Default LTS: docs/design-principles.lts, found relative to this script's folder or its parent.

The render refuses (exit 2) a source it cannot account for, instead of guessing:
  - a node heading "[ID]" that appears twice;
  - a principle node (DP-GN-n or EP-GN-n) without exactly one blockquote principle and
    exactly one "- test:" line;
  - a count of DP-GN / EP-GN nodes that differs from the file's own
    "principle-count" / "experience-principle-count" lines;
  - front matter that is missing or lacks a title.
"""
import json
import pathlib
import re
import sys

SCRIPT_NAME = pathlib.Path(__file__).name
HEADER = f"> Generated from design-principles.lts by {SCRIPT_NAME}; do not edit by hand."
FRONT_KEYS = ("scope", "audience", "boundary", "not-this", "status", "version")
NODE_HEADING = re.compile(r"^(#{1,6}) .*\[([A-Z][A-Z0-9-]*)\]\s*$")


class SourceError(Exception):
    pass


def _unquote(v: str) -> str:
    v = v.strip()
    if v.startswith('"') and v.endswith('"'):
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    return v


def split_front_matter(text: str):
    lines = text.split("\n")
    if not lines or lines[0] != "---":
        raise SourceError("no front matter (first line is not '---')")
    try:
        end = lines.index("---", 1)
    except ValueError:
        raise SourceError("front matter is not closed")
    front = {}
    for ln in lines[1:end]:
        m = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", ln)
        if m:
            front[m.group(1)] = _unquote(m.group(2))
    if "title" not in front:
        raise SourceError("front matter has no title")
    return front, lines[end + 1:]


def parse_nodes(body_lines):
    """Return [(id, heading_line_index, [lines of the node up to the next heading])]."""
    nodes, seen, cur = [], set(), None
    for i, ln in enumerate(body_lines):
        m = NODE_HEADING.match(ln)
        if m:
            nid = m.group(2)
            if nid in seen:
                raise SourceError(f"node id {nid} appears twice")
            seen.add(nid)
            cur = (nid, i, [])
            nodes.append(cur)
        elif ln.startswith("#"):
            cur = None  # an un-id'd heading ends the previous node
        elif cur is not None:
            cur[2].append(ln)
    return nodes


def validate(front_lines_body, nodes):
    body = "\n".join(front_lines_body)
    counts = {}
    for key in ("principle-count", "experience-principle-count"):
        m = re.search(rf"^- {key}: (\d+)\s*$", body, re.M)
        if not m:
            raise SourceError(f"no '- {key}:' line in the header node")
        counts[key] = int(m.group(1))
    dp = [n for n in nodes if n[0].startswith("DP-GN-")]
    ep = [n for n in nodes if n[0].startswith("EP-GN-")]
    if len(dp) != counts["principle-count"]:
        raise SourceError(f"{len(dp)} DP-GN nodes but principle-count says {counts['principle-count']}")
    if len(ep) != counts["experience-principle-count"]:
        raise SourceError(f"{len(ep)} EP-GN nodes but experience-principle-count says {counts['experience-principle-count']}")
    for nid, _, lines in dp + ep:
        quotes = [l for l in lines if l.startswith("> ")]
        tests = [l for l in lines if l.startswith("- test:")]
        if len(quotes) != 1:
            raise SourceError(f"{nid}: expected exactly one blockquote principle, found {len(quotes)}")
        if len(tests) != 1:
            raise SourceError(f"{nid}: expected exactly one '- test:' line, found {len(tests)}")


def render(text: str) -> str:
    if "\r" in text:
        raise SourceError("source has carriage returns")
    front, body = split_front_matter(text)
    nodes = parse_nodes(body)
    validate(body, nodes)
    out = [HEADER, ""]
    out.append("Document properties (from the source's front matter):")
    out.append("")
    for k in FRONT_KEYS:
        if k in front:
            out.append(f"- {k}: {front[k]}")
    out.append("")
    # The body is carried over line for line, with its leading blank lines dropped.
    while body and body[0] == "":
        body = body[1:]
    out.extend(body)
    s = "\n".join(out)
    if not s.endswith("\n"):
        s += "\n"
    return s


def default_lts() -> pathlib.Path:
    here = pathlib.Path(__file__).resolve().parent
    for cand in (here / "docs" / "design-principles.lts", here.parent / "docs" / "design-principles.lts",
                 here / "design-principles.lts"):
        if cand.is_file():
            return cand
    raise SystemExit("cannot find design-principles.lts; pass its path")


def main(argv):
    args = list(argv)
    out_path = check_path = None
    for flag in ("--out", "--check"):
        if flag in args:
            i = args.index(flag)
            try:
                val = args[i + 1]
            except IndexError:
                print(f"{flag} needs a path", file=sys.stderr)
                return 2
            del args[i:i + 2]
            if flag == "--out":
                out_path = pathlib.Path(val)
            else:
                check_path = pathlib.Path(val)
    lts = pathlib.Path(args[0]) if args else default_lts()
    try:
        md = render(lts.read_text(encoding="utf-8"))
    except (SourceError, OSError) as e:
        print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    if check_path is not None:
        have = check_path.read_text(encoding="utf-8") if check_path.is_file() else None
        if have != md:
            print(f"OUT OF SYNC: {check_path} differs from the render of {lts}", file=sys.stderr)
            return 1
        print(f"in sync: {check_path}")
        return 0
    if out_path is not None:
        out_path.write_text(md, encoding="utf-8")
        return 0
    sys.stdout.write(md)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

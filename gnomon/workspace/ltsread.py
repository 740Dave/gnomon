"""A minimal, READ-ONLY reader for .lts corpus files (nodes, properties, source file).

The converter must see corpus nodes without the lts-engine daemon (a CLI that needs a live MCP
server is not a converter), so this reads the files the way ``factlattice.hashing.list_lts_files``
does: a recursive walk admitting ``*.lts`` only. It understands exactly what the converter needs:

* a node is a markdown heading (levels 1-6) that carries a ``[ID]`` or ``[DOCTYPE:ID]`` token;
  the id is the FIRST token when the heading starts with one (``## [PROD-X] Title``), else the
 LAST token (``## Title``);
* a property is a ``- key: value`` line (key grammar ``^[a-z][a-z0-9_-]*$``) under a node;
* headings inside fenced code blocks are ignored.

It never writes, and never follows a directory symlink loop (``os.walk`` on the root, symlinked
roots such as ~/gnomon and ~/gnomon-data are passed as given: g10).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Sequence

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_TOKEN_RE = re.compile(r"\[(?:([A-Z][A-Z0-9]*):)?([A-Z][A-Z0-9._-]*)\]")
_PROP_RE = re.compile(r"^- ([a-z][a-z0-9_-]*):\s?(.*)$")
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


@dataclass
class Node:
    id: str
    doctype: Optional[str]
    title: str
    file: str
    line: int
    level: int
    props: Dict[str, List[str]] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        vals = self.props.get(key)
        return vals[0] if vals else default


def iter_lts_files(root: str) -> Iterator[str]:
    """Every ``*.lts`` under ``root`` (recursive, sorted: deterministic). A missing root yields nothing
    (the caller decides whether that is fatal: a silent skip is NOT this function's call)."""
    if not os.path.isdir(root):
        return
    found: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            if fn.endswith(".lts"):
                found.append(os.path.join(dirpath, fn))
    yield from found


def heading_id(text: str) -> Optional[tuple]:
    """``(doctype, id)`` for a heading's text, or None when it carries no id token."""
    toks = list(_TOKEN_RE.finditer(text))
    if not toks:
        return None
    first = toks[0]
    pick = first if text.lstrip().startswith(first.group(0)) else toks[-1]
    return pick.group(1), pick.group(2)


def parse_text(text: str, file: str = "<memory>") -> List[Node]:
    nodes: List[Node] = []
    cur: Optional[Node] = None
    in_fence = False
    for lineno, raw in enumerate(text.splitlines(), 1):
        if _FENCE_RE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(raw)
        if m:
            hid = heading_id(m.group(2))
            if hid is None:
                cur = None          # a heading with no id ends the previous node's property block
                continue
            cur = Node(id=hid[1], doctype=hid[0], title=m.group(2), file=file, line=lineno,
                       level=len(m.group(1)))
            nodes.append(cur)
            continue
        if cur is not None:
            pm = _PROP_RE.match(raw)
            if pm:
                cur.props.setdefault(pm.group(1), []).append(pm.group(2).strip())
    return nodes


def read_file(path: str) -> List[Node]:
    with open(path, "r", encoding="utf-8") as fh:
        return parse_text(fh.read(), file=path)


def read_roots(roots: Sequence[str]) -> List[Node]:
    """Every node of every ``*.lts`` under the given roots. The same physical file reached through
    several roots (shared ``deliverables`` dirs) is read ONCE (deduplicated by realpath)."""
    seen = set()
    out: List[Node] = []
    for root in roots:
        for f in iter_lts_files(root):
            real = os.path.realpath(f)
            if real in seen:
                continue
            seen.add(real)
            out.extend(read_file(f))
    return out

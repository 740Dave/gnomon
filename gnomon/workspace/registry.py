"""The machine-wide PREFIX REGISTRY (I4) as the converter builds it.

S1 builds it (in scratch) from today's state; S2's add-backlog / add-corpus / add-workspace writers
consult it and REFUSE a prefix that is already registered anywhere on the machine. Two parts:

* ``prefixes``: DECLARED prefixes (a backlog's ``prefix``): authoritative, machine-unique.
* ``families``: prefixes DERIVED from the node ids present (:func:`family_of`). Informational and
  heuristic: real per-corpus prefix declarations come with each corpus's conventions file. They exist
  so a writer can refuse a NEW prefix that would collide with a family already in use.
* ``legacy-shared``: node ids that appear in several physical files today. They are GRANDFATHERED
  (reported, never refused); only NEW collisions are refused. The same physical file reached through
  several corpus roots (shared ``deliverables`` dirs) is NOT a collision.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Dict, Iterable, List, Sequence

from .ltsread import Node


def family_of(node_id: str, declared_prefixes: Sequence[str] = ()) -> str:
    """The id's prefix family: a declared backlog prefix if the id starts with ``<prefix>-``; else the
    first two dash-separated segments (first one when the id has fewer than three)."""
    for p in sorted(declared_prefixes, key=len, reverse=True):
        if node_id.startswith(p + "-"):
            return p
    parts = node_id.split("-")
    return "-".join(parts[:2]) if len(parts) >= 3 else parts[0]


def find_collisions(nodes: Iterable[Node]) -> Dict[str, List[str]]:
    """``id -> sorted distinct realpaths`` for every id defined in MORE THAN ONE physical file."""
    where: Dict[str, set] = defaultdict(set)
    for n in nodes:
        where[n.id].add(os.path.realpath(n.file))
    return {i: sorted(fs) for i, fs in sorted(where.items()) if len(fs) > 1}


def build_registry(declared: Dict[str, dict], family_counts: Dict[str, Dict[str, int]],
                   collisions: Dict[str, List[str]]) -> dict:
    return {
        "schema": 1,
        "prefixes": declared,
        "families": {"derived": "heuristic (family_of)", "by-corpus": family_counts},
        "legacy-shared": {"count": len(collisions), "ids": collisions},
        "rule": "add-backlog / add-corpus / add-workspace refuse a prefix already registered anywhere on the machine; legacy-shared ids are grandfathered and only NEW collisions are refused",
    }


def refuses(registry: dict, new_prefix: str) -> bool:
    """True if registering ``new_prefix`` must be refused (S2's writers call this)."""
    if new_prefix in registry.get("prefixes", {}):
        return True
    for fams in registry.get("families", {}).get("by-corpus", {}).values():
        if new_prefix in fams:
            return True
    return False

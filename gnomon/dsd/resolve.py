"""The "is-this-DSD-answer superseded?" resolver — a SEAM ([-
LINKING], D13).

A DSD answer is stored as a POINTER to a graph node (D13); the DSD tab resolves
its CURRENT status live rather than caching a snapshot. "Superseded?" means: does
any node — possibly in ANOTHER corpus — record an overrule of this DSD's root id?

⛔ TWO MECHANISMS, ONE INTERFACE. Reverse-lookup (``lts_reverse``) is landed-but-
NOT-operational (an engine restart is held), so TODAY the resolver uses the
``lts_find`` TEXT-SEARCH FALLBACK (cross-corpus-
discovery-mechanism): with ``corpus`` omitted, ``lts_find`` searches every loaded
corpus and matches string PROPERTY VALUES, so an overrule recorded anywhere as
``supersedes: DEC-DSD-<slug>`` is found by ``lts_find(text="DEC-DSD-<slug>")``.
When ``lts_reverse`` is live, swap in :class:`ReverseLookupSupersessionResolver`
via :func:`make_supersession_resolver` — the interface does not change.

⚠ The fallback's THREE CAVEATS travel with the verdict ([-
LINKING]): (1) the overrule author must record the DSD id as a property VALUE;
(2) that corpus must be loaded; (3) a WALKABLE edge exists only if the overrule is
in the SAME corpus. So an absence-of-supersession from the fallback is BOUNDED,
not proof — reflected in ``confidence``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol


# Property keys whose VALUE naming this node means "this node overrules it".
_OVERRULE_KEYS = ("supersedes", "overrules", "replaces", "retracts", "supersede")


@dataclass
class SupersessionStatus:
    node_id: str
    superseded: bool
    referrers: List[str] = field(default_factory=list)   # node ids that overrule
    mechanism: str = ""                                   # "text-search" | "reverse-lookup"
    confidence: str = ""                                  # "bounded" | "authoritative"
    note: str = ""


class SupersessionResolver(Protocol):
    def resolve(self, node_id: str) -> SupersessionStatus: ...


def _referrer_overrules(match: Dict[str, Any], node_id: str) -> bool:
    """True when *match* records an overrule of *node_id* as a property value.

    A property value can be a string or (LTS duplicate-key ACCUMULATION) a list of
    strings — both are checked. The bare id must appear in the value, on an
    overrule-flavoured key."""
    props = match.get("properties") or {}
    if not isinstance(props, dict):
        return False
    for key, val in props.items():
        if not any(tok in str(key).lower() for tok in _OVERRULE_KEYS):
            continue
        vals = val if isinstance(val, (list, tuple)) else [val]
        if any(node_id in str(v) for v in vals):
            return True
    return False


class TextSearchSupersessionResolver:
    """The CURRENT resolver: the ``lts_find`` text-search fallback.

    ``find_fn(text)`` is the injected search — the live wiring passes
    ``lambda t: lts_find(text=t, corpus=None)`` (corpus omitted => every loaded
    corpus). It returns a list of match dicts, each ``{"id": ..., "properties":
    {...}}``. A match whose id differs from ``node_id`` and which records an
    overrule of it is a referrer; any referrer => superseded. Absence is BOUNDED
    (see the module caveats)."""

    def __init__(self, find_fn: Callable[[str], List[Dict[str, Any]]]) -> None:
        self._find = find_fn

    def resolve(self, node_id: str) -> SupersessionStatus:
        matches = self._find(node_id) or []
        referrers = [
            m.get("id", "") for m in matches
            if m.get("id") != node_id and _referrer_overrules(m, node_id)
        ]
        referrers = [r for r in referrers if r]
        if referrers:
            return SupersessionStatus(
                node_id=node_id, superseded=True, referrers=sorted(set(referrers)),
                mechanism="text-search", confidence="bounded",
                note="an overrule references this DSD id as a property value")
        return SupersessionStatus(
            node_id=node_id, superseded=False, referrers=[],
            mechanism="text-search", confidence="bounded",
            note="no overrule found by lts_find text search; BOUNDED — caveats: "
                 "author must record the id as a property value, the corpus must "
                 "be loaded, and only a same-corpus overrule is edge-walkable")


class ReverseLookupSupersessionResolver:
    """The UPGRADE, wired once ``lts_reverse`` is operational: an authoritative
    reverse edge walk. ``reverse_fn(node_id)`` returns the overruling node ids
    directly. Kept here so the swap is a one-line change at the factory, not a
    rewrite of every caller."""

    def __init__(self, reverse_fn: Callable[[str], List[str]]) -> None:
        self._reverse = reverse_fn

    def resolve(self, node_id: str) -> SupersessionStatus:
        referrers = [r for r in (self._reverse(node_id) or []) if r]
        return SupersessionStatus(
            node_id=node_id, superseded=bool(referrers),
            referrers=sorted(set(referrers)), mechanism="reverse-lookup",
            confidence="authoritative",
            note="resolved via lts_reverse edge walk")


def make_supersession_resolver(
    *,
    find_fn: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    reverse_fn: Optional[Callable[[str], List[str]]] = None,
) -> SupersessionResolver:
    """Prefer the authoritative ``lts_reverse`` resolver when a ``reverse_fn`` is
    supplied (the engine is live); else fall back to the ``lts_find`` text search.
 This is the single swap point."""
    if reverse_fn is not None:
        return ReverseLookupSupersessionResolver(reverse_fn)
    if find_fn is not None:
        return TextSearchSupersessionResolver(find_fn)
    raise ValueError("a find_fn (fallback) or reverse_fn (upgrade) is required")

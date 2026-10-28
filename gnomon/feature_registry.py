"""the FEATURE REGISTRY reader (spec-*).

The registry is data in the graph: one node per user-facing feature, id
``-<FEATURE>``, with the five keys ``feature-id`` / ``surface`` /
``expected-users`` / ``expected-rate`` / ``docs`` (an unfilled value is the literal
``EMPTY``, never absent) and an OPTIONAL sixth, ``aliases`` (command words that map to the
feature id, read by :func:`gnomon.feature_sites.command_feature_id`).

:func:`load_registry` REUSES :mod:`gnomon.lts_expand`'s engine access (the registry
resolved the way the agent-def expander resolves it, or an ad-hoc corpus directory for
``--registry DIR`` and tests); there is NO direct dependency on the factlattice parser here.

It FAILS LOUD (raises :class:`RegistryError`) on: no registry nodes at all, a node missing any of
the five keys, a duplicate feature-id, a malformed expected-rate, an unknown surface, a bad
feature-id shape, a reserved id. It never falls back to an empty registry: an empty registry
reads as 'nothing is unused'.

Neutral (g3): the registry CONTENT is tenant data in the corpus; no deployment identifier is
hard-coded here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

from .feature_events import FEATURE_ID_RE, RESERVED_FEATURE_IDS, SURFACES

EMPTY = "EMPTY"
CORPUS_ID = "gnomon"
NODE_PREFIX = "GM-FU-REGISTRY-"
SHAPE_NODE = "GM-FU-REGISTRY"
REQUIRED_KEYS = ("feature-id", "surface", "expected-users", "expected-rate", "docs")
RATE_UNITS = {"day": 86400.0, "week": 7 * 86400.0, "session": None}
_RATE_RE = re.compile(r"^(\d+(?:\.\d+)?)/(day|week|session)$")

#: OPEN to the operator/architect (spec open-question-zero-rate): is ``expected-rate: 0/week``
#: legal? The spec does not decide. Until it does the loader REFUSES a zero rate loudly rather than
#: silently choosing a reading; the ruling is a change to this one constant.
ZERO_RATE_POLICY = "reject"

#: the default registry directory override (tests and ``--registry`` use an explicit dir instead)
ENV_REGISTRY_DIR = "GNOMON_FEATURE_REGISTRY_DIR"


class RegistryError(Exception):
    """The registry cannot be trusted: the caller must fail loud (CLI exit 2), never continue."""


@dataclass(frozen=True)
class RegistryNode:
    node_id: str
    feature_id: str
    surfaces: Tuple[str, ...]
    expected_users: str
    expected_rate: str                      # the raw value, ``EMPTY`` or ``<number>/<unit>``
    rate: Optional[Tuple[float, str]]       # (number, unit) or None when EMPTY
    docs: str
    aliases: Tuple[str, ...] = ()
    see_also: Tuple[str, ...] = ()

    @property
    def valued(self) -> bool:
        """'Valued' = expected-rate non-EMPTY (architect ruling 2026-09-30; expected-users does not gate it)."""
        return self.rate is not None


def parse_rate(text: Any) -> Optional[Tuple[float, str]]:
    """``EMPTY`` -> None; ``<number>/<unit>`` (unit day/week/session) -> (number, unit); anything
    else raises ValueError."""
    if not isinstance(text, str):
        raise ValueError(f"expected-rate must be text, got {type(text).__name__}")
    t = text.strip()
    if t == EMPTY:
        return None
    m = _RATE_RE.match(t)
    if not m:
        raise ValueError(f"malformed expected-rate {text!r} (want EMPTY or <number>/<day|week|session>)")
    n = float(m.group(1))
    if n == 0.0 and ZERO_RATE_POLICY == "reject":
        raise ValueError(f"expected-rate {text!r} is zero: whether a zero rate is legal is OPEN (spec "
                         "open-question-zero-rate); refused until it is ruled")
    return n, m.group(2)


def _as_text(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v)
    return "" if v is None else str(v)


def _ids_in(text: str) -> Tuple[str, ...]:
    return tuple(re.findall(r"\[([A-Za-z0-9_.:-]+)\]", text))


def _words(text: str) -> Tuple[str, ...]:
    return tuple(w.strip() for w in text.split(",") if w.strip())


def node_from_properties(node_id: str, props: Dict[str, Any]) -> RegistryNode:
    """Validate ONE node's properties into a RegistryNode (raises RegistryError)."""
    missing = [k for k in REQUIRED_KEYS if k not in props or not _as_text(props.get(k)).strip()]
    if missing:
        raise RegistryError(f"registry node {node_id} is missing key(s) {missing} (an unfilled value is the literal "
                            f"{EMPTY}, never absent)")
    fid = _as_text(props["feature-id"]).strip()
    if not FEATURE_ID_RE.match(fid) or fid in RESERVED_FEATURE_IDS:
        raise RegistryError(f"registry node {node_id}: feature-id {fid!r} is not a valid, non-reserved feature id")
    surfaces = _words(_as_text(props["surface"]))
    bad = [s for s in surfaces if s not in SURFACES]
    if not surfaces or bad:
        raise RegistryError(f"registry node {node_id}: unknown surface(s) {bad or '(none)'}; allowed {list(SURFACES)}")
    try:
        rate = parse_rate(_as_text(props["expected-rate"]))
    except ValueError as exc:
        raise RegistryError(f"registry node {node_id}: {exc}") from None
    return RegistryNode(node_id=node_id, feature_id=fid, surfaces=surfaces,
                        expected_users=_as_text(props["expected-users"]).strip(),
                        expected_rate=_as_text(props["expected-rate"]).strip(), rate=rate,
                        docs=_as_text(props["docs"]).strip(),
                        aliases=_words(_as_text(props.get("aliases"))),
                        see_also=_ids_in(_as_text(props.get("see-also"))))


def _default_dir() -> Optional[str]:
    return os.environ.get(ENV_REGISTRY_DIR) or None


def _engine_for(corpus_dir: Optional[str], engine_builder: Optional[Callable[[str, dict], Any]]):
    """(engine, corpus_id, source) through lts_expand's engine access."""
    from . import lts_expand

    corpus_dir = corpus_dir or _default_dir()
    if corpus_dir:
        path = os.path.abspath(os.path.expanduser(corpus_dir))
        if not os.path.isdir(path):
            raise RegistryError(f"registry directory {corpus_dir!r} does not exist or is not a directory")
        corpus = "fu-registry-adhoc"
        build = engine_builder or lts_expand._default_engine_builder
        try:
            return build(corpus, {"roots": [{"path": path, "area": "specs"}]}), corpus, path
        except Exception as exc:  # noqa: BLE001 - an unbuildable engine is a loud failure
            raise RegistryError(f"lts engine could not be built over {path}: {type(exc).__name__}: {exc}") from None
    exp = lts_expand.LtsExpander(engine_builder=engine_builder)
    try:
        eng, paths = exp._engine(CORPUS_ID)
    except lts_expand._Failure as exc:
        raise RegistryError(f"registry corpus {CORPUS_ID!r} unavailable: {exc.reason}") from None
    return eng, CORPUS_ID, ", ".join(paths) or CORPUS_ID


def load_registry(corpus_dir: Optional[str] = None, *,
                  engine_builder: Optional[Callable[[str, dict], Any]] = None,
                  with_source: bool = False):
    """The registry nodes, validated. ``corpus_dir`` None = the machine's ``gnomon`` corpus (the
    ``GNOMON_FEATURE_REGISTRY_DIR`` env, if set, names a directory instead). Raises RegistryError; never
    returns an empty list. ``with_source=True`` returns ``(nodes, source)``."""
    eng, corpus, source = _engine_for(corpus_dir, engine_builder)
    res = eng.find(corpus=corpus, prefix=SHAPE_NODE, tier="full", token_budget=4_000_000)
    if not res.get("success", False):
        raise RegistryError(f"registry query failed: {res.get('error') or res.get('reason') or res}")
    if res.get("truncated") or res.get("complete") is False or list(res.get("stale_corpora") or []):
        raise RegistryError("registry query returned an INCOMPLETE or STALE node set; refusing a partial registry")
    nodes: List[RegistryNode] = []
    seen: Dict[str, str] = {}
    for n in res.get("nodes") or []:
        nid = str(n.get("id"))
        if not nid.startswith(NODE_PREFIX):
            continue
        rn = node_from_properties(nid, n.get("properties") or {})
        if rn.feature_id in seen:
            raise RegistryError(f"duplicate feature-id {rn.feature_id!r} on {seen[rn.feature_id]} and {nid}")
        seen[rn.feature_id] = nid
        nodes.append(rn)
    if not nodes:
        raise RegistryError(f"no {NODE_PREFIX}* registry nodes found in {source}: an empty registry would read as "
                            "'nothing is unused'")
    nodes.sort(key=lambda r: r.node_id)
    return (nodes, source) if with_source else nodes

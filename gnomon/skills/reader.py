"""NodeReader — the seam between the pure library and the graph.

The library depends only on the :class:`NodeReader` protocol (``scan()`` returns a list of node
dicts), so it is fully testable with an in-memory fake.

X49 change from the original services package: the real reader is :class:`InProcessReader`, which
reuses GNOMON'S OWN in-process lts access (the same registry resolution and engine build as
``lts_expand.LtsExpander`` and ``memory.remember``: ``app.graph_viewer.resolve_registry_path`` +
``factlattice_engine`` ``LtsEngine``) instead of spawning a second lts-engine as an MCP client. The
old ``LtsEngineReader`` (stdio MCP subprocess) is gone.

The reader is fail-closed: an unregistered corpus, an engine that cannot be built, a failed or
stale or TRUNCATED find all raise :class:`ReaderError`. A partial scan is never returned, because
a skill missing from a partial scan would read as an orphan or a missing node.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Protocol, Tuple

DEFAULT_SCAN_TEXT = "skill-projection"
_FIND_TOKEN_BUDGET = 4_000_000


class ReaderError(RuntimeError):
    """The graph could not be read completely. Nothing is planned or written on this."""


class NodeReader(Protocol):
    def scan(self) -> list[dict[str, Any]]:
        """Return the projection-candidate nodes (dicts with id/corpus/properties)."""
        ...


class StaticReader:
    """A NodeReader over an in-memory list — for tests and for replaying a captured scan."""

    def __init__(self, nodes: list[dict[str, Any]]):
        self._nodes = nodes

    def scan(self) -> list[dict[str, Any]]:
        return list(self._nodes)


def _default_registry_resolver() -> Tuple[Optional[str], str]:
    from ..app.graph_viewer import resolve_registry_path

    return resolve_registry_path()


def _default_engine_builder(corpus_id: str, spec: dict):
    """Build the engine exactly as the daemon does, for ONE corpus only."""
    from factlattice_engine.config import build_store
    from factlattice_engine.tools import LtsEngine

    return LtsEngine(build_store({corpus_id: spec}), self_heal=True)


class InProcessReader:
    """Scan named corpora (or every registered corpus, ``corpora=None``) for opt-in nodes, in this
    process, through Gnomon's own lts access. ``registry_resolver`` / ``engine_builder`` are the
    test seams."""

    def __init__(self, corpora: Iterable[str] | None,
                 registry_resolver: Optional[Callable[[], Tuple[Optional[str], str]]] = None,
                 engine_builder: Optional[Callable[[str, dict], Any]] = None,
                 scan_text: str = DEFAULT_SCAN_TEXT):
        self.corpora = None if corpora is None else list(corpora)
        self._resolve = registry_resolver or _default_registry_resolver
        self._build = engine_builder or _default_engine_builder
        self.scan_text = scan_text
        self.registry_path: Optional[str] = None

    def _registry(self) -> Dict[str, dict]:
        path, how = self._resolve()
        if path is None:
            raise ReaderError(f"lts-engine corpus registry not resolvable ({how})")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                reg = json.load(fh)
        except (OSError, ValueError) as exc:
            raise ReaderError(f"corpus registry {path} unreadable: {type(exc).__name__}: {exc}") from None
        if not isinstance(reg, dict):
            raise ReaderError(f"corpus registry {path} is not a JSON object")
        self.registry_path = path
        return reg

    def scan(self) -> List[dict[str, Any]]:
        reg = self._registry()
        names = sorted(reg) if self.corpora is None else self.corpora
        nodes: List[dict[str, Any]] = []
        for corpus in names:
            spec = reg.get(corpus)
            if not isinstance(spec, dict):
                raise ReaderError(f"corpus {corpus!r} is not registered in {self.registry_path}")
            try:
                eng = self._build(corpus, spec)
                res = eng.find(corpus=corpus, text=self.scan_text, tier="full",
                               token_budget=_FIND_TOKEN_BUDGET)
            except Exception as exc:  # noqa: BLE001 - an unreadable graph is a LOUD stop
                raise ReaderError(f"lts-engine could not scan {corpus!r}: {type(exc).__name__}: {exc}") from None
            if not isinstance(res, dict) or not res.get("success", False):
                raise ReaderError(f"lts_find failed for {corpus!r}: {res.get('error') if isinstance(res, dict) else res}")
            if res.get("stale_corpora"):
                raise ReaderError(f"corpus {corpus!r} is STALE and could not be healed: {res.get('stale_corpora')}")
            if res.get("truncated") or res.get("complete") is False:
                raise ReaderError(f"lts_find for {corpus!r} returned an INCOMPLETE node set; refusing a partial scan")
            for n in res.get("nodes") or []:
                if isinstance(n, dict) and n.get("corpus", corpus) == corpus:
                    nodes.append(n)
        return nodes

"""LIVE lts-engine check for DSD status (D1 — "Live daemon check").

The DSD status Gnomon shows must reflect REALITY NOW — is the DSD-answer
corpus actually registered + reachable through the engine, and is a just-written
node actually visible — not a build-time ``registration_held=True`` guess.

⛔ THE HONEST MECHANISM (report gaps, don't fake — D1's mechanism clause).
Gnomon has NO separate MCP client to a long-running ``lts-engine`` daemon
process. What it DOES have, in-process (``factlattice`` is pip-installed editable,
the same import the typed writer already uses), is ``factlattice_engine.config.
build_store`` — the EXACT store-construction the daemon runs at boot
(``server.build_tools`` -> ``config.build_store``). This module builds that store
from the live corpus registry (``FACTLATTICE_CORPUS_CONFIG`` / ``FACTLATTICE_
CORPUS_ROOT`` in THIS process's environment) and asks the engine's own store:

* is ``corpus_id`` a loaded partition?            -> ``registered``
* can that partition be resolved + queried?       -> ``reachable``
* is a just-written ``probe_node_id`` served?      -> ``node_visible``

So this is a genuine engine-level check reading the engine's real registry — NOT
a hardcoded claim. Its honest limit: it sees exactly what THIS process's corpus
config registers. If Gnomon is launched without the DSD-answer corpus
registered, the check reports NOT registered and the UI complains LOUDLY — which
is precisely the intent (an unreachable lts-engine must show as a loud error), never a silent degrade to stale local data.

⛔ NO LOCAL INDEX FALLBACK (D2). If the engine cannot be built/imported at all,
``engine_available`` is False and ``live`` is False — a loud "lts-engine
unreachable", never a quiet "looks fine".
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

# The corpus id the typed writer mints under is a DEPLOYMENT/REGISTRATION decision (X13 S5-3): the neutral
# built-in is ``NEUTRAL_CORPUS_ID`` and a deployment overrides it in ``<deploy_root>/defaults/dsd-corpus.json``
# (read lazily by :func:`dsd_settings`; nothing at import). ``registered_corpora`` on the status surfaces what the
# engine really knows so a mismatch is visible, not silently read as "offline".
NEUTRAL_CORPUS_ID = "dsd-answers"
NEUTRAL_DOMAIN = "GNOMON"
NEUTRAL_PRODUCT = "Gnomon"

_CORPUS_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9._-]{0,63}\Z")
_DOMAIN_RE = re.compile(r"\A[A-Z0-9][A-Z0-9._-]{0,63}\Z")
_PRODUCT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}\Z")


class DsdCorpusError(RuntimeError):
    """``<deploy_root>/defaults/dsd-corpus.json`` is PRESENT but unusable. The DSD writer must not guess: minting nodes
    under neutral ids into a corpus that already holds deployment-id nodes would mix domains in one file (g68, X13 S5-4b).
    The message names the file. An ABSENT file is not this error: it is the neutral default."""


def dsd_settings() -> dict:
    """``{"corpus_id", "domain", "product"}``: the neutral defaults overlaid by the deployment's ``dsd-corpus.json``.
    FAILS CLOSED (:class:`DsdCorpusError`, naming the file) when that file is present but unusable (not JSON, not an
    object, unreadable, or a field of the wrong shape); an absent file is the neutral default."""
    from .. import deploy_data

    out = {"corpus_id": NEUTRAL_CORPUS_ID, "domain": NEUTRAL_DOMAIN, "product": NEUTRAL_PRODUCT}
    table = deploy_data.load_json("dsd-corpus")
    path = deploy_data.data_path("dsd-corpus.json")
    fault = deploy_data.errors().get(path, "") if path else ""
    if fault and fault != "absent":
        raise DsdCorpusError(f"{path}: {fault}; refusing to use neutral ids for the DSD-answer corpus "
                             "(fix or remove the file)")
    if table:
        for key, rx in (("corpus_id", _CORPUS_ID_RE), ("domain", _DOMAIN_RE), ("product", _PRODUCT_RE)):
            if key not in table:
                continue
            v = table[key]
            if isinstance(v, str) and rx.match(v):
                out[key] = v
            else:
                raise DsdCorpusError(f"{path}: field {key!r} must be a string matching {rx.pattern}; "
                                     "refusing to use neutral ids for the DSD-answer corpus (fix or remove the file)")
    return out


def default_corpus_id() -> str:
    """The DSD-answer corpus id (neutral default, or the deployment's)."""
    return dsd_settings()["corpus_id"]


def default_domain_suffix() -> str:
    """The domain suffix a minted node carries: the corpus domain, lower-cased (``GNOMON`` -> ``gnomon``)."""
    return dsd_settings()["domain"].lower()


_MECHANISM = (
    "in-process factlattice_engine store (config.build_store) — the same store the "
    "lts-engine daemon builds at boot; Gnomon has no separate MCP client to a "
    "running daemon, so this reads the engine's corpus registry from THIS process's "
    "FACTLATTICE_CORPUS_CONFIG/FACTLATTICE_CORPUS_ROOT environment"
)


@dataclass
class LiveDaemonStatus:
    """The result of a live engine check for the DSD-answer corpus.

    ``live`` is the single load-bearing predicate the UI keys on: True only when
    the engine built, the corpus is registered + reachable, and (if a node was
    probed) it was actually visible. Anything else is NOT live and MUST surface
    loudly (D2: no silent degrade)."""

    corpus_id: str
    engine_available: bool = False
    registered: bool = False
    reachable: bool = False
    node_visible: Optional[bool] = None   # None = not probed
    probe_node_id: str = ""
    registered_corpora: List[str] = field(default_factory=list)
    mechanism: str = _MECHANISM
    error: str = ""
    checked_epoch: float = 0.0

    @property
    def live(self) -> bool:
        if not (self.engine_available and self.registered and self.reachable):
            return False
        if self.node_visible is False:
            return False
        return True

    def banner(self) -> str:
        """A loud one-line UI warning when NOT live; empty string when live."""
        if self.live:
            return ""
        if not self.engine_available:
            return ("⛔ lts-engine UNREACHABLE — DSD status cannot be verified live. "
                    + (self.error or "the engine could not be built in this process"))
        if not self.registered:
            known = ", ".join(self.registered_corpora) or "none"
            return (f"⛔ lts-engine reachable but corpus {self.corpus_id!r} is NOT "
                    f"registered (loaded: {known}) — answers are minted to the .lts "
                    f"file but are NOT live-queryable until the corpus is registered "
                    f"+ the engine reloads")
        if not self.reachable:
            return (f"⛔ corpus {self.corpus_id!r} registered but its partition could "
                    f"not be queried — {self.error or 'unreachable'}")
        if self.node_visible is False:
            return (f"⛔ wrote node {self.probe_node_id!r} but the engine does NOT "
                    f"serve it — the write is not live-visible")
        return "⛔ DSD status not live"


def build_scoped_store(corpus_id: str):
    """Build an engine store holding ONLY ``corpus_id`` (perf: indexing the whole
    37-corpus registry took ~7.8 s per DSD save; one corpus is ~0.2 s).

    The corpus spec is read from the SAME registry the daemon uses
    (``config.default_corpora``: FACTLATTICE_CORPUS_CONFIG), so the check still
    reflects what THIS process's config registers: if ``corpus_id`` is absent from
    the registry the returned store is EMPTY, ``registered`` is False and the UI
    complains loudly, exactly as before. No node is assumed: the caller still asks the
    partition whether the probe node resolves."""
    from factlattice_engine.config import build_store, default_corpora

    spec = default_corpora().get(corpus_id)
    return build_store({corpus_id: spec} if spec else {})


def check_live_status(
    corpus_id: Optional[str] = None,
    *,
    probe_node_id: Optional[str] = None,
    store_builder: Optional[Callable[[], object]] = None,
) -> LiveDaemonStatus:
    """Ask the in-process engine store whether the DSD-answer corpus is live.

    ``store_builder`` is injected in tests (a fake store / a raiser); the default
    is :func:`build_scoped_store` — ``config.build_store`` over ONLY ``corpus_id``'s registry entry.
    ``probe_node_id`` (a just-minted node id) adds the visibility leg; omit it for
    a plain registered+reachable banner check."""
    checked = time.time()
    probe = probe_node_id or ""
    try:
        corpus_id = corpus_id or default_corpus_id()
    except DsdCorpusError as exc:      # a status check REPORTS the refusal (not live, file named); it never raises into a handler
        return LiveDaemonStatus(corpus_id=corpus_id or "", engine_available=False, probe_node_id=probe,
                                error=f"DsdCorpusError: {exc}", checked_epoch=checked)

    if store_builder is None:
        def store_builder():  # noqa: E306 — local default, lazily imports the engine
            return build_scoped_store(corpus_id)

    try:
        store = store_builder()
    except Exception as exc:  # noqa: BLE001 — engine unbuildable == lts-engine offline, LOUD
        return LiveDaemonStatus(
            corpus_id=corpus_id, engine_available=False, probe_node_id=probe,
            error=f"{type(exc).__name__}: {exc}", checked_epoch=checked)

    try:
        corpora = sorted(store.corpus_ids())
    except Exception as exc:  # noqa: BLE001
        return LiveDaemonStatus(
            corpus_id=corpus_id, engine_available=True, probe_node_id=probe,
            error=f"corpus registry query failed: {type(exc).__name__}: {exc}",
            checked_epoch=checked)

    registered = corpus_id in corpora
    reachable = False
    node_visible: Optional[bool] = None
    err = ""

    if registered:
        try:
            part = store.get_partition(corpus_id)
            reachable = part is not None
            if reachable and probe:
                node_visible = bool(part.has_node(probe))
        except Exception as exc:  # noqa: BLE001
            err = f"partition query failed: {type(exc).__name__}: {exc}"
            if probe and node_visible is None:
                node_visible = False
    else:
        err = (f"corpus {corpus_id!r} not registered with the lts-engine "
               f"(loaded: {', '.join(corpora) or 'none'})")

    return LiveDaemonStatus(
        corpus_id=corpus_id, engine_available=True, registered=registered,
        reachable=reachable, node_visible=node_visible, probe_node_id=probe,
        registered_corpora=corpora, error=err, checked_epoch=checked)

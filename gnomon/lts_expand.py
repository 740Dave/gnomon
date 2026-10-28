"""Real ``{{lts: … }}`` macro expansion for agent definitions (D8, 2026-09-29).

The launch pipeline's :class:`~gnomon.launch.ClaudeMdRenderer` routes every
``{{lts: <query> }}`` marker in a profile's ``claude_md_source`` to an expander.
Until now the only expander was the no-op seam (marker left verbatim). This module
is the REAL one: deterministic Python, in-process, no subagent, no MCP round-trip.

MACRO SYNTAX
============

::

    {{lts: <corpus>:<SELECTOR> [key=value ...] [form=full|digest] [empty=ok] }}

* ``<corpus>`` — a registered corpus id (``^[a-z0-9][a-z0-9._-]*$``). Expansion is
  STRICTLY scoped to that one corpus: only its registry entry is loaded, and a node
  from any other corpus can never appear.
* ``<SELECTOR>`` — one node id (``AA-VC-EXAMPLE-NODE``) or an id GLOB
  (``AA-VC-*``, ``AA-NOTE-?-X``) matched with :func:`fnmatch.fnmatchcase`
  (case-sensitive, whole id) against every node in the corpus.
* ``key=value`` — a PROPERTY filter (key grammar ``^[a-z][a-z0-9_-]*$``). Keeps
  nodes whose property ``key`` equals ``value`` (trimmed, case-insensitive; a
  list-valued property matches if ANY element does). Several filters AND. Quote a
  value with spaces: ``kind="note-to-self (operating commitment)"``.
* ``form=full`` (default) — every matched node rendered in FULL: title, every
  property, and its body prose. No digesting, no truncation.
* ``form=digest`` — a COMPLETE one-line-per-node digest, ``id · when · summary``
  (the ``when`` column is omitted for a node that has none)
  (``summary`` property; else ``title``; else the first ~100 chars of ``what``),
  newest ``when`` first (ties: later in the file = newer), NO count cutoff, ending
  with a one-line hint on how to fetch a node's full content.
* ``order=newest|file`` — ``newest``: newest ``when`` first (ties: later in the file
  = newer; nodes with no ``when`` last). ``file``: forward source order. Default:
  ``file`` for ``form=full``, ``newest`` for ``form=digest``. E.g. the lexicon boot
  digest ``{{lts: example-lexicon:LEX-* form=digest order=file }}`` starts at tier 1.
* ``budget=<N>`` — cap this macro at ~N APPROXIMATE tokens (chars / 4, not a
  tokenizer count). Entries (a full-form node / a digest line) are rendered WHOLE, in
  the macro's ``order=``, as a contiguous prefix while they fit; the first entry that
  would not fit and everything after it are WITHHELD, and ONE explicit overflow line
  names K of N, ~T tokens shown, and the exact query for the rest::

      > (lts budget: showing K of N nodes, ~T tokens; the rest via lts_find(corpus="…",
      > prefix="…", tier="digest") minus the K shown above — first withheld: <ID>, …)

  The budget covers the ENTRIES only; the header comment, the digest footer and the
  overflow line are fixed overhead. ``budget=0`` (or a budget smaller than the first
  entry) renders ZERO entries plus the overflow line. ``budget=off`` = unbounded, and
  overrides a config default. A macro that fits its budget renders byte-identically to
  one without. With NO ``budget=``, the expander's ``default_budget`` applies (the
  machine config's ``lts_macro_budget``, applied by ``agent_def.render_agent_def``);
  that default is OFF (``None``) unless someone opts in — so existing renders are
  unchanged. Silent truncation is impossible by construction: withholding and the
  overflow line are one code path.
* ``empty=ok`` — opt-in: zero matches renders a visible "0 nodes matched" NOTICE
  (status ``empty-ok``) instead of the failure banner. Use it ONLY for sets that are
  legitimately empty at times (an accumulating ``*-MEM-*`` class). Engine / registry
  / corpus failures STILL fail loud under ``empty=ok``.

``form`` / ``order`` / ``empty`` / ``budget`` are reserved option keys — never property filters.

WHAT IS A MACRO (2026-09-29 remediation): only a marker whose FIRST token is
``<corpus>:<SELECTOR>`` (registry-id-shaped corpus, uppercase id/glob) is expanded
(:meth:`LtsExpander.accepts`). Any other ``{{lts: … }}`` — e.g. instructional prose
*about* the macro syntax — is left verbatim, never a false FAILED banner. To show a
well-formed macro literally, escape it: ``\\{{lts: c:X }}`` renders as ``{{lts: c:X }}``.

Examples::

    {{lts: alpha-architect-personality:AA-VC-* }}
    {{lts: alpha-architect-personality:AA-NOTE-* state=active }}
    {{lts: alpha-architect-personality:AA-MEM-* state=active form=digest empty=ok }}
    {{lts: example-corpus:EXAMPLE-NODE }}

FAIL LOUD, NEVER SILENT
=======================

If the registry cannot be resolved, ``factlattice_engine`` cannot be imported or
built, the corpus is unregistered or stale, the query is malformed, or it matches
ZERO nodes (absent ``empty=ok``), the marker is replaced by a visible banner::

    > ⚠ lts expansion FAILED for <query>: <reason> — your memory was NOT loaded;
    > read <corpus file path> directly and flag it to the operator

and the failure is returned in the :class:`ExpansionOutcome` (the caller records it
in ``launch.json`` + the provenance log). A zero is a failure, not an empty section.

HOW IT READS THE GRAPH (reuse, not reimplementation)
====================================================

The same way ``gnomon.app.graph_viewer`` and the lts-engine daemon do: the
registry resolved by :func:`~gnomon.app.graph_viewer.resolve_registry_path`
(``GNOMON_LTS_CORPUS_CONFIG`` → ``FACTLATTICE_CORPUS_CONFIG`` → the ``lts-engine`` entry
in ``~/.mcp.json``), ``factlattice_engine.config.build_store`` over ONLY the named
corpus, queried through ``LtsEngine.find`` / ``LtsEngine.node`` with
``self_heal=True``. Body prose is extracted with graph_viewer's ``split_body``.

⚠ This checks the corpus DATA PATH (registry + engine library + the corpus files),
the same path the daemon serves. It does not ping the running MCP daemon process.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

_CORPUS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SELECTOR_RE = re.compile(r"^[A-Z*?][A-Z0-9_*?-]*$")
_PROP_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]*$")
FORMS = ("full", "digest")
ORDERS = ("newest", "file")
RESERVED_OPTIONS = ("form", "order", "empty", "budget")
#: the budget unit is APPROXIMATE tokens — chars / 4, the same basis as the
#: D4 agent-def size fields (agent_def.APPROX_CHARS_PER_TOKEN). Always labelled.
BUDGET_CHARS_PER_TOKEN = 4
BUDGET_OFF = "off"
BUDGET_SOURCE_MACRO = "macro"
BUDGET_SOURCE_MACRO_OFF = "macro-off"
BUDGET_SOURCE_CONFIG = "config"
#: the head a marker must carry to be treated as a macro (else left verbatim)
MARKER_HEAD_RE = re.compile(r"^\s*[a-z0-9][a-z0-9._-]*:[A-Z*?][A-Z0-9_*?-]*(\s|$)")
#: an expansion larger than this is FLAGGED (visible line + launch.json), never truncated
EXPANSION_WARN_CHARS = 60_000
DIGEST_WHAT_CHARS = 100

_FIND_TOKEN_BUDGET = 4_000_000
_NODE_TOKEN_BUDGET = 400_000

#: an ``{{include: …}}`` marker inside lts-expanded content is NOT expanded
#: (single pass, fixed order include -> ${symbols} -> lts); the outcome FLAGS the node ids
#: that carry one so the renderer can warn, never pass it through silently.
INCLUDE_MARKER_RE = re.compile(r"\{\{\s*include:")

STATUS_OK = "ok"
STATUS_EMPTY_OK = "empty-ok"
STATUS_FAILED = "failed"


class LtsQueryError(ValueError):
    """A malformed ``{{lts: … }}`` query (reported as a loud expansion failure)."""


@dataclass(frozen=True)
class LtsQuery:
    raw: str
    corpus: str
    selector: str
    filters: Tuple[Tuple[str, str], ...] = ()
    form: str = "full"
    empty_ok: bool = False
    order: Optional[str] = None  # None -> the form's default
    budget: Optional[int] = None  # approx tokens; None -> the expander default (unless budget_off)
    budget_off: bool = False      # ``budget=off``: unbounded even if a config default is set

    @property
    def effective_order(self) -> str:
        return self.order or ("newest" if self.form == "digest" else "file")

    @property
    def is_pattern(self) -> bool:
        return any(c in self.selector for c in "*?")


def parse_lts_query(raw: str) -> LtsQuery:
    """Parse the text inside ``{{lts: … }}``. Raises :class:`LtsQueryError`."""
    text = (raw or "").strip()
    if not text:
        raise LtsQueryError("empty query")
    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise LtsQueryError(f"unparseable query ({exc})") from exc
    head, rest = tokens[0], tokens[1:]
    if ":" not in head:
        raise LtsQueryError(f"expected '<corpus>:<NODE-ID or GLOB>', got {head!r}")
    corpus, selector = head.split(":", 1)
    if not _CORPUS_RE.match(corpus):
        raise LtsQueryError(f"invalid corpus id {corpus!r}")
    if not _SELECTOR_RE.match(selector):
        raise LtsQueryError(f"invalid node id / glob {selector!r} (uppercase id; '*'/'?' globs)")
    filters: List[Tuple[str, str]] = []
    form = "full"
    empty_ok = False
    order: Optional[str] = None
    budget: Optional[int] = None
    budget_off = False
    for tok in rest:
        if "=" not in tok:
            raise LtsQueryError(f"expected key=value, got {tok!r}")
        key, value = tok.split("=", 1)
        if key == "form":
            if value not in FORMS:
                raise LtsQueryError(f"form must be one of {FORMS}, got {value!r}")
            form = value
        elif key == "order":
            if value not in ORDERS:
                raise LtsQueryError(f"order must be one of {ORDERS}, got {value!r}")
            order = value
        elif key == "budget":
            if value == BUDGET_OFF:
                budget, budget_off = None, True
            elif value.isdigit() and value.isascii():
                budget, budget_off = int(value), False
            else:
                raise LtsQueryError(f"budget= takes a non-negative integer (approx tokens) or "
                                    f"'{BUDGET_OFF}', got {value!r}")
        elif key == "empty":
            if value != "ok":
                raise LtsQueryError(f"empty= only accepts 'ok', got {value!r}")
            empty_ok = True
        elif _PROP_KEY_RE.match(key):
            filters.append((key, value))
        else:
            raise LtsQueryError(f"invalid property key {key!r}")
    return LtsQuery(raw=text, corpus=corpus, selector=selector, filters=tuple(filters),
                    form=form, empty_ok=empty_ok, order=order, budget=budget, budget_off=budget_off)


@dataclass
class ExpansionOutcome:
    query: str
    status: str                      # ok | empty-ok | failed
    text: str                        # the replacement text (banner on failure)
    corpus: Optional[str] = None
    matched_ids: List[str] = field(default_factory=list)
    reason: str = ""
    corpus_paths: List[str] = field(default_factory=list)
    # budget accounting (all APPROXIMATE, chars / 4)
    budget: Optional[int] = None            # the budget in force (None = unbounded)
    budget_source: Optional[str] = None     # macro | macro-off | config | None
    shown_ids: List[str] = field(default_factory=list)
    withheld_ids: List[str] = field(default_factory=list)
    approx_tokens_shown: int = 0            # the rendered ENTRIES (what the budget caps)
    approx_tokens_withheld: int = 0         # the withheld entries, measured, not rendered
    remainder_query: Optional[Dict[str, Any]] = None
    # freshness inputs. ``node_hashes`` maps every MATCHED node id to the engine's
    # content_hash (so a rendered def is STALE when a node it touched changes);
    # ``reentrant_include_ids`` names the shown nodes whose text carries an unexpanded
    # ``{{include: …}}`` marker.
    node_hashes: Dict[str, str] = field(default_factory=dict)
    reentrant_include_ids: List[str] = field(default_factory=list)

    @property
    def approx_tokens_rendered(self) -> int:
        """Everything this macro put into the def (entries + header + overhead)."""
        return self.chars // BUDGET_CHARS_PER_TOKEN

    @property
    def chars(self) -> int:
        return len(self.text)

    @property
    def oversize(self) -> bool:
        return self.chars > EXPANSION_WARN_CHARS

    @property
    def ok(self) -> bool:
        return self.status != STATUS_FAILED

    def to_record(self) -> Dict[str, Any]:
        return {"query": self.query, "status": self.status, "corpus": self.corpus,
                "matched": len(self.matched_ids), "matched_ids": list(self.matched_ids),
                "reason": self.reason, "corpus_paths": list(self.corpus_paths),
                "chars": self.chars, "oversize": self.oversize,
                "budget": self.budget, "budget_source": self.budget_source,
                "shown": len(self.shown_ids), "shown_ids": list(self.shown_ids),
                "withheld": len(self.withheld_ids), "withheld_ids": list(self.withheld_ids),
                "approx_tokens_rendered": self.approx_tokens_rendered,
                "approx_tokens_shown": self.approx_tokens_shown,
                "approx_tokens_withheld": self.approx_tokens_withheld,
                "approx_basis": "approximate: chars / 4 (not a tokenizer count)",
                "remainder_query": (self.remainder_query or {}).get("call"),
                "node_hashes": dict(self.node_hashes),
                "reentrant_include_ids": list(self.reentrant_include_ids)}


def failure_banner(query: str, reason: str, paths: List[str]) -> str:
    where = ", ".join(paths) if paths else "(the corpus files — corpus not resolvable from the registry)"
    return (f"\n> ⚠ lts expansion FAILED for `{query}`: {reason} — your memory was NOT loaded; "
            f"read {where} directly and flag it to the pilot\n")


# --- rendering ---------------------------------------------------------------------

def _prop_values(v: Any) -> List[str]:
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return ["" if v is None else str(v)]


def _matches_filters(props: Dict[str, Any], filters: Tuple[Tuple[str, str], ...]) -> bool:
    for key, want in filters:
        if key not in (props or {}):
            return False
        wanted = want.strip().lower()
        if not any(val.strip().lower() == wanted for val in _prop_values(props[key])):
            return False
    return True


def _file_order(nd: Dict[str, Any]) -> Tuple[str, int, str]:
    lr = nd.get("line_range") or [0]
    return (str(nd.get("source_file") or ""), int(lr[0] if lr else 0), str(nd.get("id")))


def render_node_full(nd: Dict[str, Any], body: str) -> str:
    lines = [f"#### {nd.get('title') or nd['id']} [{nd['id']}]", ""]
    for key, value in (nd.get("properties") or {}).items():
        vals = _prop_values(value)
        if len(vals) == 1:
            lines.append(f"- **{key}:** {vals[0]}")
        else:
            lines.append(f"- **{key}:**")
            lines.extend(f"  - {v}" for v in vals)
    if body:
        lines += ["", body]
    return "\n".join(lines).rstrip() + "\n"


def digest_summary(nd: Dict[str, Any]) -> str:
    props = nd.get("properties") or {}
    for key in ("summary",):
        val = props.get(key)
        if val:
            return " ".join(_prop_values(val)).strip()
    if nd.get("title"):
        return str(nd["title"]).strip()
    what = " ".join(_prop_values(props.get("what"))).strip() if props.get("what") else ""
    what = " ".join(what.split())
    return (what[:DIGEST_WHAT_CHARS] + "…") if len(what) > DIGEST_WHAT_CHARS else what


def _newest_first(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Newest ``when`` first; ties → later in the file first. Missing ``when`` last."""
    def when(nd):
        v = (nd.get("properties") or {}).get("when")
        return " ".join(_prop_values(v)).strip() if v else ""
    with_when = [n for n in nodes if when(n)]
    without = [n for n in nodes if not when(n)]
    with_when.sort(key=lambda n: (when(n), _file_order(n)), reverse=True)
    without.sort(key=_file_order, reverse=True)
    return with_when + without


# --- the expander -------------------------------------------------------------------

def _default_registry_resolver() -> Tuple[Optional[str], str]:
    from .app.graph_viewer import resolve_registry_path

    return resolve_registry_path()


def _default_engine_builder(corpus_id: str, spec: dict):
    """Build the engine exactly as the daemon does — for ONE corpus only."""
    from factlattice_engine.config import build_store
    from factlattice_engine.tools import LtsEngine

    return LtsEngine(build_store({corpus_id: spec}), self_heal=True)


class _Failure(Exception):
    def __init__(self, reason: str, paths: Optional[List[str]] = None):
        super().__init__(reason)
        self.reason = reason
        self.paths = paths or []


class LtsExpander:
    """The real expander. Callable as ``expander(query) -> str`` (the renderer seam)
    and ``expander.expand(query) -> ExpansionOutcome`` (the detailed form the renderer
    uses when present). Every outcome is also appended to :attr:`outcomes`.

    ``registry_resolver`` / ``engine_builder`` are the test seams: production reads the
    real registry and builds a real engine per named corpus (cached per instance).
    """

    def __init__(self,
                 registry_resolver: Optional[Callable[[], Tuple[Optional[str], str]]] = None,
                 engine_builder: Optional[Callable[[str, dict], Any]] = None,
                 default_budget: Optional[int] = None) -> None:
        #: the budget for macros that set none (approx tokens). None = OFF
        #: (unbounded, the pre-RM7A behaviour). Production passes the machine config's
        #: ``lts_macro_budget`` (see agent_def.render_agent_def).
        if default_budget is not None and (isinstance(default_budget, bool)
                                           or not isinstance(default_budget, int) or default_budget < 0):
            raise ValueError(f"default_budget must be a non-negative int or None, got {default_budget!r}")
        self.default_budget = default_budget
        self._resolve = registry_resolver or _default_registry_resolver
        self._build = engine_builder or _default_engine_builder
        self._registry: Optional[Dict[str, dict]] = None
        self._registry_path: Optional[str] = None
        self._engines: Dict[str, Any] = {}
        self.outcomes: List[ExpansionOutcome] = []

    # -- plumbing ----------------------------------------------------------------
    def _load_registry(self) -> Dict[str, dict]:
        if self._registry is not None:
            return self._registry
        path, how = self._resolve()
        if path is None:
            raise _Failure(f"lts-engine corpus registry not resolvable ({how})")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                reg = json.load(fh)
        except (OSError, ValueError) as exc:
            raise _Failure(f"corpus registry {path} unreadable: {type(exc).__name__}: {exc}")
        if not isinstance(reg, dict):
            raise _Failure(f"corpus registry {path} is not a JSON object")
        self._registry, self._registry_path = reg, path
        return reg

    @staticmethod
    def _paths(spec: dict) -> List[str]:
        return [str(r.get("path")) for r in (spec.get("roots") or []) if isinstance(r, dict) and r.get("path")]

    def _engine(self, corpus: str) -> Tuple[Any, List[str]]:
        reg = self._load_registry()
        spec = reg.get(corpus)
        if not isinstance(spec, dict):
            raise _Failure(f"corpus {corpus!r} is not registered in {self._registry_path}")
        paths = self._paths(spec)
        if corpus not in self._engines:
            try:
                self._engines[corpus] = self._build(corpus, spec)
            except Exception as exc:  # noqa: BLE001 — engine unbuildable == down, LOUD
                raise _Failure(f"lts-engine could not be built for {corpus!r}: {type(exc).__name__}: {exc}", paths)
        return self._engines[corpus], paths

    # -- the expansion -------------------------------------------------------------
    @staticmethod
    def accepts(query: str) -> bool:
        """Is this marker a MACRO (``<corpus>:<SELECTOR> …``)? Anything else — prose
        that merely mentions ``{{lts: … }}`` — is left verbatim by the renderer."""
        return bool(MARKER_HEAD_RE.match(query or ""))

    def __call__(self, query: str) -> str:
        return self.expand(query).text

    def expand(self, raw: str) -> ExpansionOutcome:
        out = self._expand(raw)
        self.outcomes.append(out)
        return out

    def _expand(self, raw: str) -> ExpansionOutcome:
        text = (raw or "").strip()
        corpus: Optional[str] = None
        paths: List[str] = []
        try:
            q = parse_lts_query(text)
            corpus = q.corpus
            eng, paths = self._engine(q.corpus)
            res = eng.find(corpus=q.corpus, tier="full", token_budget=_FIND_TOKEN_BUDGET)
            if not res.get("success", False):
                raise _Failure(f"lts_find failed: {res.get('error') or res.get('reason') or res}", paths)
            stale = list(res.get("stale_corpora") or [])
            if stale:
                raise _Failure(f"corpus is STALE and could not be healed: {stale}", paths)
            if res.get("truncated") or res.get("complete") is False:
                raise _Failure("lts_find returned an INCOMPLETE node set (truncated) — refusing a partial expansion",
                               paths)
            nodes = [n for n in (res.get("nodes") or [])
                     if n.get("corpus", q.corpus) == q.corpus
                     and fnmatch.fnmatchcase(str(n.get("id")), q.selector)
                     and _matches_filters(n.get("properties") or {}, q.filters)]
            if not nodes:
                total = res.get("total_matched")
                if q.empty_ok:
                    return ExpansionOutcome(
                        query=text, status=STATUS_EMPTY_OK, corpus=q.corpus, corpus_paths=paths,
                        reason=f"0 nodes matched (empty=ok declared; corpus reachable, {total} nodes)",
                        text=(f"\n> (lts: `{text}` — 0 nodes matched; empty=ok declared. Corpus "
                              f"{q.corpus} is reachable ({total} nodes); nothing in this set yet.)\n"))
                raise _Failure(f"ZERO nodes matched (corpus {q.corpus} has {total} nodes) — "
                               "a zero is a failure, not an empty section", paths)
            ordered = _newest_first(nodes) if q.effective_order == "newest" else sorted(nodes, key=_file_order)
            budget, budget_source = self._budget_for(q)
            if q.form == "digest":
                entries = self._digest_entries(ordered)
            else:
                entries = self._full_entries(eng, q, ordered, paths)
            k = _fit(entries, budget)
            shown, withheld = ordered[:k], ordered[k:]
            if q.form == "digest":
                body = self._digest_body(q, entries[:k], total=len(ordered))
            else:
                body = "\n".join(entries[:k])
            header = (f"<!-- lts: {text} — {len(nodes)} node(s) from corpus {q.corpus}, "
                      f"form={q.form} order={q.effective_order} -->\n")
            out = ExpansionOutcome(query=text, status=STATUS_OK, corpus=q.corpus, corpus_paths=paths,
                                   matched_ids=[n["id"] for n in nodes], text=header + body,
                                   budget=budget, budget_source=budget_source,
                                   shown_ids=[n["id"] for n in shown],
                                   withheld_ids=[n["id"] for n in withheld],
                                   approx_tokens_shown=_joined_chars(entries[:k]) // BUDGET_CHARS_PER_TOKEN,
                                   approx_tokens_withheld=_joined_chars(entries[k:]) // BUDGET_CHARS_PER_TOKEN)
            out.node_hashes = {str(n["id"]): str(n.get("content_hash") or "") for n in nodes}
            out.reentrant_include_ids = [str(n["id"]) for n, e in zip(ordered[:k], entries[:k])
                                         if INCLUDE_MARKER_RE.search(e)]
            if withheld:  # ONE code path: withholding always writes the overflow line
                out.remainder_query = remainder_query(q, out.shown_ids)
                out.text += overflow_line(q, out)
            if out.oversize:  # FLAG, never truncate
                out.text += (f"\n> ⚠ lts expansion `{text}` is LARGE ({out.chars:,} chars > "
                             f"{EXPANSION_WARN_CHARS:,}) — expanded in full; consider form=digest or a filter.\n")
            return out
        except LtsQueryError as exc:
            reason = f"malformed query ({exc})"
        except _Failure as exc:
            reason, paths = exc.reason, (exc.paths or paths)
        except Exception as exc:  # noqa: BLE001 — anything unexpected is a LOUD failure
            reason = f"unexpected error: {type(exc).__name__}: {exc}"
        return ExpansionOutcome(query=text, status=STATUS_FAILED, corpus=corpus, reason=reason,
                                corpus_paths=paths, text=failure_banner(text, reason, paths))

    def _budget_for(self, q: LtsQuery) -> Tuple[Optional[int], Optional[str]]:
        if q.budget_off:
            return None, BUDGET_SOURCE_MACRO_OFF
        if q.budget is not None:
            return q.budget, BUDGET_SOURCE_MACRO
        if self.default_budget is not None:
            return self.default_budget, BUDGET_SOURCE_CONFIG
        return None, None

    def _full_entries(self, eng: Any, q: LtsQuery, nodes: List[Dict[str, Any]], paths: List[str]) -> List[str]:
        """One rendered entry per node (joined with "\\n" — the pre-RM7A layout)."""
        from .app.graph_viewer import split_body

        parts = []
        for nd in nodes:
            res = eng.node(q.corpus, nd["id"], tier="full", token_budget=_NODE_TOKEN_BUDGET)
            if not res.get("success", False) or res.get("node") is None:
                raise _Failure(f"lts_node failed for {nd['id']}: "
                               f"{res.get('reason') or res.get('error') or res.get('note')}", paths)
            parts.append(render_node_full(res["node"], split_body(res.get("source_excerpt"))))
        return parts

    @staticmethod
    def _digest_entries(nodes: List[Dict[str, Any]]) -> List[str]:
        lines = []
        for nd in nodes:  # already ordered by the caller (order=newest|file)
            when = (nd.get("properties") or {}).get("when")
            when_s = " ".join(_prop_values(when)).strip() if when else ""
            cols = [nd["id"]] + ([when_s] if when_s else []) + [digest_summary(nd)]
            lines.append("- " + " · ".join(cols))
        return lines

    @staticmethod
    def _digest_body(q: LtsQuery, lines: List[str], total: int) -> str:
        lines = list(lines)
        lines.append("")
        if len(lines) - 1 == total:
            lines.append(f"_Digest: {total} node(s), complete (no cutoff). Full content is query-on-demand: "
                         f"`lts_node(corpus=\"{q.corpus}\", node_id=\"<ID>\")`._")
        else:  # a budget withheld some: NEVER claim completeness
            lines.append(f"_Digest: {len(lines) - 1} of {total} node(s) shown — lts budget cutoff (the rest: "
                         f"see the budget line below). Full content is query-on-demand: "
                         f"`lts_node(corpus=\"{q.corpus}\", node_id=\"<ID>\")`._")
        return "\n".join(lines) + "\n"


# --- budget helpers ---------------------------------------------------------

def _joined_chars(entries: List[str]) -> int:
    return sum(len(e) for e in entries) + max(len(entries) - 1, 0)


def _fit(entries: List[str], budget: Optional[int]) -> int:
    """How many LEADING entries fit ``budget`` approx tokens (chars <= budget*4), whole.
    A contiguous prefix: stops at the first entry that does not fit (the remainder must
    be a clean suffix of the macro's order). ``None`` = all."""
    if budget is None:
        return len(entries)
    cap = budget * BUDGET_CHARS_PER_TOKEN
    used = 0
    for i, e in enumerate(entries):
        used += len(e) + (1 if i else 0)
        if used > cap:
            return i
    return len(entries)


def _literal_prefix(selector: str) -> str:
    cut = [i for i, c in enumerate(selector) if c in "*?"]
    return selector[:cut[0]] if cut else selector


def remainder_query(q: LtsQuery, shown_ids: List[str]) -> Dict[str, Any]:
    """The EXACT lts-engine call (+ stated post-steps) whose result is the withheld set.

    The engine cannot express file order, an fnmatch glob or an arbitrary property
    equality, so the remainder is stated as a SET: the call returns every candidate,
    the ``keep_*`` steps apply the macro's own selector/filters, and ``minus_ids`` (the
    K entries already shown above) is removed. A single-id macro -> ``lts_node``.
    """
    if not q.is_pattern:
        args = {"corpus": q.corpus, "node_id": q.selector}
        return {"tool": "lts_node", "args": args, "minus_ids": list(shown_ids),
                "call": f'lts_node(corpus="{q.corpus}", node_id="{q.selector}")'}
    prefix = _literal_prefix(q.selector)
    args: Dict[str, Any] = {"corpus": q.corpus}
    if prefix:
        args["prefix"] = prefix
    args["tier"] = "digest"
    call = "lts_find(" + ", ".join(f'{k}="{v}"' for k, v in args.items()) + ")"
    keep_glob = None if q.selector == prefix + "*" else q.selector
    return {"tool": "lts_find", "args": args, "keep_glob": keep_glob,
            "keep_filters": [list(f) for f in q.filters], "minus_ids": list(shown_ids), "call": call}


def overflow_line(q: LtsQuery, out: "ExpansionOutcome") -> str:
    rq = out.remainder_query or {}
    k, n = len(out.shown_ids), len(out.shown_ids) + len(out.withheld_ids)
    steps = []
    if rq.get("keep_glob"):
        steps.append(f"keep ids matching {rq['keep_glob']}")
    for key, val in rq.get("keep_filters") or []:
        steps.append(f"keep {key}={val}")
    steps.append(f"minus the {k} shown above" if k else "none shown above")
    return (f"\n> (lts budget: showing {k} of {n} nodes, ~{out.approx_tokens_shown} tokens; "
            f"the rest via {rq.get('call')} — {', '.join(steps)}; "
            f"{n - k} withheld (~{out.approx_tokens_withheld} tokens), first withheld: {out.withheld_ids[0]} "
            f"in order={q.effective_order}; budget={out.budget} [{out.budget_source}], "
            f"approximate: chars/4)\n")

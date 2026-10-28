"""Graph-viewer controller — the corpus tabs' and the PROCs tab's window onto the decision graph.

 web port / the D3 lts-engine-integration vision: the web dashboard reads the
factlattice/LTS corpora LIVE, through the engine's own Python API, rather than from a
snapshot. This module is CTk-free and HTTP-free: ``web/backend/server.py`` only marshals
what it returns.

⛔ HOW IT READS THE GRAPH (reuse, not reimplementation). It builds the SAME in-process
store the ``lts-engine`` MCP daemon builds at boot — ``factlattice_engine.config.
build_store`` over the corpus registry — and queries it through the engine's own
``LtsEngine.find`` / ``LtsEngine.node`` (the functions ``lts_find`` / ``lts_node`` wrap).
This is the same mechanism ``gnomon.dsd.live`` already uses for the DSD banner.
The engine is constructed with ``self_heal=True``, so an edit to a ``.lts`` file is
picked up on the next request (the engine's freshness gate re-indexes the changed
corpus) — the tabs are a live window, not a cached export.

⛔ WHERE THE REGISTRY COMES FROM (no guessed locations). Resolution order:

1. ``GNOMON_LTS_CORPUS_CONFIG`` (a Gnomon-specific override), then
2. ``FACTLATTICE_CORPUS_CONFIG`` (the engine's own variable), then
3. the ``lts-engine`` server entry in ``~/.mcp.json`` — ``env.FACTLATTICE_CORPUS_CONFIG``,
   i.e. the registry the running MCP daemon itself serves.

If none resolves to a readable file, the viewer is NOT live and says so LOUDLY (every
query raises :class:`GraphViewerUnavailable`; the status reports why). There is no
fallback to hardcoded corpus roots or a stale local export.

Discovery is by the graph, not by a hardcoded list:

* **Corpus tabs** (X13 S5-3) = DEPLOYMENT data: ``<deploy_root>/defaults/corpus-tabs.json`` declares each tab
  (its corpus, an optional roster node returned separately as the list header, an optional item type
  prefix, and the presentation: group-by properties, header chips, featured properties). Gnomon ships NO
  tab: with no deployment data there are none, and the PROCs tab below is the only graph tab.
* **PROCs** = ``lts_find(prefix="PROC")`` across EVERY registered corpus (the P14-correct
  query — a type-prefix match, never ``text="PROC"``, which over-matches "PROCESS" prose),
  grouped by the corpus that contains each node. The funnel (corpora/nodes in scope,
  total matched, complete) is returned so the UI can show the denominator.
"""

from __future__ import annotations

import json
import os
import re
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple

PROC_PREFIX = "PROC"

#: generous token ceiling for a single-node ``tier='full'`` read — ``full`` ERRORS rather
#: than truncating, so the budget must comfortably exceed the largest node we render.
_NODE_TOKEN_BUDGET = 400_000
_FIND_TOKEN_BUDGET = 2_000_000

_NODE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
_CORPUS_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PROP_LINE_RE = re.compile(r"^\s*- [a-z][a-z0-9_-]*:")


class GraphViewerUnavailable(RuntimeError):
    """The engine could not be built (no registry / import failure / build error)."""


class GraphNodeNotFound(LookupError):
    """The requested node is not in scope for this tab."""


# --- registry resolution -------------------------------------------------------

def resolve_registry_path(
    env: Optional[Dict[str, str]] = None,
    mcp_json_path: Optional[str] = None,
) -> Tuple[Optional[str], str]:
    """Return ``(path_or_None, how_it_was_resolved)``. Never guesses a location."""
    env = os.environ if env is None else env
    tried: List[str] = []
    for var in ("GNOMON_LTS_CORPUS_CONFIG", "FACTLATTICE_CORPUS_CONFIG"):
        val = env.get(var)
        if val:
            if os.path.isfile(val):
                return val, f"env {var}"
            tried.append(f"env {var}={val} (not a file)")
    mcp = mcp_json_path or os.path.expanduser("~/.mcp.json")
    try:
        with open(mcp, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        val = (((cfg.get("mcpServers") or {}).get("lts-engine") or {})
               .get("env") or {}).get("FACTLATTICE_CORPUS_CONFIG")
        if val and os.path.isfile(val):
            return val, f"lts-engine entry in {mcp}"
        tried.append(f"{mcp}: lts-engine FACTLATTICE_CORPUS_CONFIG="
                     f"{val!r} (missing or not a file)")
    except FileNotFoundError:
        tried.append(f"{mcp} not found")
    except Exception as exc:  # noqa: BLE001 — a bad json is a loud no-registry
        tried.append(f"{mcp} unreadable: {type(exc).__name__}: {exc}")
    return None, "no corpus registry resolved — tried: " + "; ".join(tried)


def _default_engine_factory(registry_path: str):
    """Build the engine exactly as the daemon does: registry -> build_store -> LtsEngine."""
    from factlattice_engine.config import build_store
    from factlattice_engine.tools import LtsEngine

    with open(registry_path, "r", encoding="utf-8") as fh:
        corpora = json.load(fh)
    store = build_store(corpora)
    return LtsEngine(store, self_heal=True)


# --- helpers ---------------------------------------------------------------------

def _natural_key(node_id: str):
    """ALPHA-P2 < ALPHA-P2B < ALPHA-P10 (numeric-aware)."""
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", node_id)]


def split_body(excerpt: Optional[str]) -> str:
    """The node's prose body: its source excerpt minus the heading line, the property
    lines (``- key: value`` + their indented continuations) and ``---`` rules. The
    properties are rendered from the parsed ``properties`` map, so dropping their raw
    lines here avoids showing them twice; what remains is the prose the parser keeps
    only in the body (the part ``lts_find`` cannot see)."""
    if not excerpt:
        return ""
    lines = excerpt.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        lines = lines[1:]
    out: List[str] = []
    in_prop = False
    for raw in lines:
        if _PROP_LINE_RE.match(raw) and not raw.startswith((" ", "\t")):
            in_prop = True
            continue
        if in_prop and raw.startswith((" ", "\t")) and raw.strip():
            continue  # an indented continuation / sub-item of the property
        in_prop = False
        if raw.strip() == "---":
            continue
        out.append(raw[2:] if raw.startswith("> ") else (raw[1:] if raw == ">" else raw))
    return "\n".join(out).strip()


def _props_list(props: Dict[str, Any]) -> List[dict]:
    """Ordered ``[{key, value}]`` — value is a str or a list of str, as parsed."""
    rows = []
    for k, v in (props or {}).items():
        if isinstance(v, (list, tuple)):
            rows.append({"key": k, "value": [str(x) for x in v]})
        else:
            rows.append({"key": k, "value": "" if v is None else str(v)})
    return rows


# --- corpus tabs (DEPLOYMENT data; X13 S5-3) ------------------------------------------

TAB_ID_RE = re.compile(r"\A[a-z][a-z0-9-]{0,31}\Z")
#: ids of the tabs the page ships: a deployment tab must never shadow one (its DOM id is ``panel-<id>``)
RESERVED_TAB_IDS = frozenset({"agents", "intelligences", "dsd", "decisions", "procs", "knowledge", "config"})
_PROP_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,63}\Z")
_PREFIX_RE = re.compile(r"\A[A-Z][A-Z0-9_-]{0,31}\Z")
_TEXT_RE = re.compile(r"\A[^\x00-\x1f\x7f<>]{1,80}\Z")
_TEMPLATE_RE = re.compile(r"\A[^\x00-\x1f\x7f<>]{1,80}\Z")
TAB_KEYS = frozenset({"id", "label", "corpus", "roster_id", "item_prefix", "item_noun", "fields", "group_by",
                      "group_labels", "group_notes", "chips", "lede", "chip_list", "featured", "in_header",
                      "roster_heading", "other_heading"})
CHIP_KEYS = frozenset({"property", "text", "humanize", "warn_values", "accent"})


def _text(v: Any) -> bool:
    return isinstance(v, str) and _TEXT_RE.match(v) is not None


def _props(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) and _PROP_RE.match(x) for x in v)


def validate_tab(raw: Any) -> Tuple[Optional[dict], List[str]]:
    """``(tab, [])`` or ``(None, [reasons])``. STRICT: an unknown key, a bad id/regex, or a presentation field of
    the wrong shape rejects the whole tab (loudly, via the returned reasons); never raises. The returned dict is a
    fresh copy holding only validated values, safe to hand to the browser."""
    errs: List[str] = []
    if not isinstance(raw, dict):
        return None, ["tab must be an object"]
    tid = raw.get("id")
    name = tid if isinstance(tid, str) else repr(tid)

    def bad(k: str, why: str) -> None:
        errs.append(f"tab {name!r}.{k}: {why}")

    unknown = sorted(str(k) for k in raw if k not in TAB_KEYS)
    if unknown:
        bad("*", f"unknown key(s) {unknown}")
    if not (isinstance(tid, str) and TAB_ID_RE.match(tid)):
        bad("id", f"must match {TAB_ID_RE.pattern}")
    elif tid in RESERVED_TAB_IDS:
        bad("id", f"{tid!r} is a built-in tab id")
    if not _text(raw.get("label")):
        bad("label", "required: a short printable string")
    if not (isinstance(raw.get("corpus"), str) and _CORPUS_ID_RE.match(raw.get("corpus"))):
        bad("corpus", f"required: a corpus id matching {_CORPUS_ID_RE.pattern}")
    if raw.get("roster_id") is not None and not (isinstance(raw["roster_id"], str) and _NODE_ID_RE.match(raw["roster_id"])):
        bad("roster_id", f"must match {_NODE_ID_RE.pattern}")
    if raw.get("item_prefix") is not None and not (isinstance(raw["item_prefix"], str) and _PREFIX_RE.match(raw["item_prefix"])):
        bad("item_prefix", f"must match {_PREFIX_RE.pattern}")
    if raw.get("item_noun") is not None and not _text(raw["item_noun"]):
        bad("item_noun", "must be a short printable string")
    for k in ("fields", "group_by", "featured", "in_header"):
        if raw.get(k) is not None and not _props(raw[k]):
            bad(k, f"must be a list of property names matching {_PROP_RE.pattern}")
    if _props(raw.get("group_by") or []) and not set(raw.get("group_by") or []) <= set(raw.get("fields") or []):
        bad("group_by", "every group_by property must also be listed in fields")
    for k in ("group_labels", "group_notes"):
        v = raw.get(k)
        if v is not None and not (isinstance(v, dict) and all(isinstance(a, str) and _PROP_RE.match(a)
                                                            and isinstance(b, str) and _TEMPLATE_RE.match(b)
                                                            for a, b in v.items())):
            bad(k, "must map a property name to a short template (use {value})")
    for k in ("roster_heading", "other_heading"):
        if raw.get(k) is not None and not _text(raw[k]):
            bad(k, "must be a short printable string")
    chips = raw.get("chips")
    if chips is not None:
        ok = isinstance(chips, list) and all(isinstance(c, dict) and set(c) <= CHIP_KEYS
                                             and isinstance(c.get("property"), str) and _PROP_RE.match(c["property"])
                                             and (c.get("text") is None or _TEMPLATE_RE.match(str(c["text"])))
                                             and (c.get("humanize") is None or isinstance(c["humanize"], bool))
                                             and (c.get("accent") is None or isinstance(c["accent"], bool))
                                             and (c.get("warn_values") is None or (isinstance(c["warn_values"], list) and all(isinstance(w, str) for w in c["warn_values"])))
                                             for c in chips)
        if not ok:
            bad("chips", f"must be a list of objects with a property name and only the keys {sorted(CHIP_KEYS)}")
    for k, keys in (("lede", {"property", "label"}), ("chip_list", {"property", "label", "strip_prefix"})):
        v = raw.get(k)
        if v is not None and not (isinstance(v, dict) and set(v) <= keys and isinstance(v.get("property"), str)
                                  and _PROP_RE.match(v["property"]) and _text(v.get("label"))
                                  and (v.get("strip_prefix") is None or _text(v["strip_prefix"]))):
            bad(k, f"must be an object with a property name and a label (keys {sorted(keys)})")
    if errs:
        return None, errs
    tab = {k: raw[k] for k in TAB_KEYS if k in raw and raw[k] is not None}
    tab.setdefault("item_noun", "items")
    tab.setdefault("fields", [])
    tab.setdefault("group_by", [])
    tab.setdefault("featured", [])
    tab.setdefault("in_header", [])
    tab.setdefault("chips", [])
    tab.setdefault("roster_heading", "Roster properties")
    tab.setdefault("other_heading", "Other properties")
    return json.loads(json.dumps(tab)), []


def load_corpus_tabs() -> Tuple[List[dict], List[str]]:
    """The deployment's tabs, validated: ``(tabs, errors)``. Lazy (reads ``corpus-tabs.json`` when CALLED); a bad tab
    is skipped with its reasons in ``errors`` and the good ones survive; duplicate ids keep the first."""
    from .. import deploy_data

    table = deploy_data.load_json("corpus-tabs")
    errs = [f"{os.path.basename(p)}: {why}" for p, why in deploy_data.errors().items() if p.endswith("corpus-tabs.json")]
    if table is None:
        return [], errs
    raw = table.get("tabs")
    if not isinstance(raw, list):
        return [], errs + ["corpus-tabs.json: 'tabs' must be a list"]
    tabs: List[dict] = []
    seen = set()
    for r in raw:
        tab, e = validate_tab(r)
        errs.extend(e)
        if tab is None:
            continue
        if tab["id"] in seen:
            errs.append(f"tab {tab['id']!r}: duplicate id; ignored")
            continue
        seen.add(tab["id"])
        tabs.append(tab)
    return tabs, errs


def get_corpus_tab(tab_id: str) -> dict:
    """One configured tab by id; raises :class:`GraphNodeNotFound` (a 404) for an unknown id."""
    for t in load_corpus_tabs()[0]:
        if t["id"] == tab_id:
            return t
    raise GraphNodeNotFound(f"no corpus tab {tab_id!r} is configured")


# --- the controller ----------------------------------------------------------------

class GraphViewer:
    """Live read-side over the lts-engine store for the corpus tabs + the PROCs tab.

    ``engine_factory`` is the test seam: a zero-arg callable returning an ``LtsEngine``
    (tests build one over tmp fixture corpora). Production leaves it None and the engine
    is built lazily, once, from the resolved registry (~7s for the full 27-corpus
    registry on first use; ~20-60ms per query thereafter, self-healing on edits).
    """

    def __init__(self, engine_factory: Optional[Callable[[], Any]] = None,
                 registry_path: Optional[str] = None,
                 registry_source: Optional[str] = None):
        self._factory = engine_factory
        self._registry_path = registry_path
        self._registry_source = registry_source or ("injected engine factory"
                                                    if engine_factory else "")
        self._engine = None
        self._error = ""
        self._lock = threading.RLock()

    # -- engine lifecycle ----------------------------------------------------------
    def _build(self):
        if self._factory is not None:
            return self._factory()
        path = self._registry_path
        if path is None:
            path, how = resolve_registry_path()
            self._registry_source = how
            if path is None:
                raise GraphViewerUnavailable(how)
            self._registry_path = path
        return _default_engine_factory(path)

    def engine(self):
        """The built engine; raises :class:`GraphViewerUnavailable` LOUDLY otherwise.
        A failed build is retried on the next call (so fixing the registry recovers
        without a restart), never silently degraded."""
        with self._lock:
            if self._engine is None:
                try:
                    self._engine = self._build()
                    self._error = ""
                except GraphViewerUnavailable as exc:
                    self._error = str(exc)
                    raise
                except Exception as exc:  # noqa: BLE001 — engine unbuildable == offline, LOUD
                    self._error = f"lts-engine could not be built: {type(exc).__name__}: {exc}"
                    raise GraphViewerUnavailable(self._error) from exc
            return self._engine

    def status(self) -> dict:
        try:
            eng = self.engine()
        except GraphViewerUnavailable as exc:
            return {"live": False, "error": str(exc),
                    "registry_path": self._registry_path,
                    "registry_source": self._registry_source, "corpora": []}
        return {"live": True, "error": "",
                "registry_path": self._registry_path,
                "registry_source": self._registry_source,
                "corpora": sorted(eng.store.corpus_ids()),
                "mechanism": ("in-process factlattice_engine store (config.build_store) "
                              "queried via LtsEngine.find/node, self-healing on edits")}

    # -- low-level queries ---------------------------------------------------------
    def _find(self, **kw) -> dict:
        with self._lock:
            res = self.engine().find(**kw)
        if not res.get("success", False):
            raise GraphViewerUnavailable(
                f"lts_find failed: {res.get('error') or res.get('reason') or res}")
        return res

    def _node(self, corpus: str, node_id: str) -> dict:
        if not _CORPUS_ID_RE.match(corpus or "") or not _NODE_ID_RE.match(node_id or ""):
            raise GraphNodeNotFound(f"invalid id {corpus!r}/{node_id!r}")
        with self._lock:
            res = self.engine().node(corpus, node_id, tier="full",
                                     token_budget=_NODE_TOKEN_BUDGET)
        if not res.get("success", False):
            reason = res.get("reason") or res.get("error") or ""
            # the engine's unknown-corpus payload carries `corpora_available`
            # (factlattice_engine.extent.unknown_corpus) — a 404, not an outage.
            if "corpora_available" in res:
                raise GraphNodeNotFound(f"corpus {corpus!r} not loaded: {reason}")
            raise GraphViewerUnavailable(f"lts_node failed for {corpus}/{node_id}: {reason}")
        if res.get("node") is None:
            raise GraphNodeNotFound(res.get("note") or f"{node_id} not in {corpus}")
        return res

    def _children(self, corpus: str, parent_id: str) -> List[dict]:
        """Nodes whose ``parent_section`` is ``parent_id`` (e.g. a PROC's STEP nodes),
        in source order. Read from the partition the gate just freshness-checked."""
        with self._lock:
            part = self.engine().store.get_partition(corpus)
            if part is None:
                return []
            kids = [n for n in part.nodes.values() if n.get("parent_section") == parent_id]
        return sorted(kids, key=lambda n: ((n.get("line_range") or [0])[0], n["id"]))

    def _detail(self, corpus: str, node_id: str, *, with_children: bool) -> dict:
        res = self._node(corpus, node_id)
        nd = res["node"]
        out = {
            "corpus": corpus,
            "id": nd["id"],
            "type_prefix": nd.get("type_prefix"),
            "title": nd.get("title") or "",
            "domain": nd.get("domain"),
            "source_file": nd.get("source_file"),
            "line_range": nd.get("line_range"),
            "properties": _props_list(nd.get("properties") or {}),
            "body": split_body(res.get("source_excerpt")),
            "edges": res.get("edges") or {},
            "content_hash": nd.get("content_hash"),
        }
        if with_children:
            steps = []
            for kid in self._children(corpus, node_id):
                kres = self._node(corpus, kid["id"])
                knd = kres["node"]
                steps.append({
                    "id": knd["id"], "type_prefix": knd.get("type_prefix"),
                    "title": knd.get("title") or "",
                    "properties": _props_list(knd.get("properties") or {}),
                    "body": split_body(kres.get("source_excerpt")),
                })
            out["children"] = steps
        return out

    # -- corpus tabs (deployment-declared) -----------------------------------------
    def corpus_tab(self, tab: dict) -> dict:
        """The list payload of one configured tab: the roster node (header), the items (each carrying the tab's
        ``fields`` properties) in natural id order, and the denominator."""
        res = self._find(corpus=tab["corpus"], tier="full", token_budget=_FIND_TOKEN_BUDGET)
        roster = None
        items = []
        for nd in res.get("nodes") or []:
            props = nd.get("properties") or {}
            if tab.get("roster_id") and nd["id"] == tab["roster_id"]:
                roster = {"id": nd["id"], "title": nd.get("title") or "", "properties": _props_list(props)}
                continue
            if tab.get("item_prefix") and nd.get("type_prefix") != tab["item_prefix"]:
                continue
            item = {"id": nd["id"], "title": nd.get("title") or ""}
            for f in tab["fields"]:
                v = props.get(f)
                item[f] = v.strip() or None if isinstance(v, str) else None
            items.append(item)
        items.sort(key=lambda g: _natural_key(g["id"]))
        return {"tab": tab["id"], "corpus": tab["corpus"], "roster": roster, "items": items,
                "total_nodes": res.get("total_matched"), "complete": res.get("complete")}

    def corpus_tab_node(self, tab: dict, node_id: str) -> dict:
        return self._detail(tab["corpus"], node_id, with_children=False)

    # -- PROCs tab -----------------------------------------------------------------
    def procs(self) -> dict:
        res = self._find(prefix=PROC_PREFIX, tier="digest")
        groups: Dict[str, List[dict]] = {}
        for row in res.get("digest") or []:
            groups.setdefault(row["corpus"], []).append({
                "id": row["id"], "title": row.get("title") or "",
                "source_file": row.get("source_file"),
                "line_range": row.get("line_range"),
            })
        extent = res.get("extent") or {}
        stale = list(res.get("stale_corpora") or [])
        return {
            "query": 'lts_find(prefix="PROC", tier="digest") across every registered corpus',
            "funnel": {
                "corpora_in_scope": len(extent.get("corpora_consulted") or []),
                "nodes_in_scope": extent.get("nodes_in_scope"),
                "total_matched": res.get("total_matched"),
                # ⛔ the engine's cross-corpus find SKIPS a stale corpus (it could not
                # be healed) and names it only in `stale_corpora`, while its own
                # `complete` still reads True — so a whole corpus group can vanish from
                # the list. For THIS list that is incomplete, and it says so.
                "complete": bool(res.get("complete")) and not stale,
                "engine_complete": res.get("complete"),
                "truncated": res.get("truncated"),
                "stale_corpora": stale,
            },
            "groups": [{"corpus": c, "procs": sorted(v, key=lambda r: r["id"])}
                       for c, v in sorted(groups.items())],
        }

    def proc(self, corpus: str, node_id: str) -> dict:
        if not (node_id or "").startswith(PROC_PREFIX):
            raise GraphNodeNotFound(f"{node_id!r} is not a PROC node")
        detail = self._detail(corpus, node_id, with_children=True)
        if detail.get("type_prefix") != PROC_PREFIX:
            raise GraphNodeNotFound(f"{corpus}/{node_id} is not PROC-typed")
        return detail

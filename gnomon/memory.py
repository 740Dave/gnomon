"""Deterministic personality-memory writer — ``remember`` (2026-09-28).

WHY THIS EXISTS
    Claude Code auto-memory is disabled here; each agent's durable
    memory is its OWN lts personality corpus (e.g. alpha-architect ->
    ``~/gnomon-data/agents/alpha-architect/personality/lifelog.lts``, corpus
    ``alpha-architect-personality``, ids ``AA-NOTE-*`` / ``AA-VC-*`` /
    ``AA-EXP-*``). Writing one memory through the MCP ``lts_add_node`` costs a
    ~10k-token receipt (mostly the registered-roots listing). This module makes
    the sanctioned path ONE cheap deterministic call with a ONE-LINE receipt.

WHAT IT REUSES (nothing forked)
    * The WRITE goes through ``factlattice_engine.authoring.add_node`` — the exact
      function the ``lts_add_node`` MCP tool dispatches to — which wraps
      ``factlattice.authoring.edit.add_node`` and its GATE: the whole-file parsed
      delta must be exactly the new node + its properties and NOTHING on any
      existing node moves. A refusal is a result (nothing written); we surface
      its reason in one line.
    * The INDEX check builds the engine the way the lts-engine daemon does
      (``factlattice_engine.config.build_store`` over the registry resolved by
      ``gnomon.app.graph_viewer.resolve_registry_path``, i.e. the
      ``lts-engine`` entry in ``~/.mcp.json``) — for the ONE corpus whose root
      contains the file — BEFORE the write, then reads the node back through
      ``LtsEngine.node`` AFTER it. That read goes through the engine's own
      freshness gate + ``[LEC-SELF-HEAL]``, the same path the running daemon takes
      on its next query of that corpus. If the node cannot be read back, the
      receipt says so LOUDLY (``ok`` is False even though the file was written).

IDENTITY SCOPING
    ``remember`` takes an ``instance_id`` and resolves the target file ONLY from
    that instance's profile / the directory convention. There is no path or corpus
    argument anywhere. The agent-facing surfaces (the ``gnomon-self``
    ``remember`` verb and the ``\\memory`` operator command) obtain
    ``instance_id`` from the HARNESS (the proxy's ``launch_context`` / the
    harness's own ``instance_id``), never from the caller — so an agent can
    only write its own self.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

#: kind -> (id category, the property that carries ``what``, allowed extras)
#: ``mem`` (operator design 2026-09-28) is the DEFAULT, untyped, NON-ELEVATED class: a
#: plain memory — saved, queryable, expanded next session — with no weight. The
#: typed classes (note / vc / exp) are the lineage's curated categories.
KINDS: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    "mem": ("MEM", "what", ("how_to_apply",)),
    "note": ("NOTE", "what", ("how_to_apply",)),
    "vc": ("VC", "what-changed", ("how_to_apply",)),
    "exp": ("EXP", "what", ("how_to_apply", "through_line")),
    # (design note 2026-09-29: an agent should have room for more than its assigned work): a personal-priorities item, <P>-WISH-*, state: open, found
    # by the near-finish nudge (lts_find prefix=<P>-WISH state=open). Unweighted.
    "wish": ("WISH", "what", ("why_meaningful", "size", "shared")),
}
#: THE ONE PLACE the memory verb's types are listed (checklist rule proposed as (4)): every other reader
#: (the ``gnomon-self`` tool schema, the ``\\memory`` parser and its usage/help text, the tests) takes the list from
#: :func:`type_tokens`. The exact type tokens a ``\memory`` line may lead with (anything else is text).
TYPE_TOKENS = ("note", "vc", "exp", "wish", "mem")
#: the ``progress`` type exists ONLY while the flag ``progress_entries`` is on; with it off it is not offered
#: anywhere and is refused exactly like any unknown type (nothing written).
PROGRESS_TYPE = "progress"
PROGRESS_FLAG = "progress_entries"


#: The type list the ``\\help`` line shows. With every flag-gated type OFF it is the text the PARENT showed, byte for
#: byte: ``wish`` was never in it (a pre-existing omission, not flag-gated: ``wish`` has no flag), and that is left as it
#: was. A flag-gated type appears here only while its flag is on.
MEMORY_HELP_TYPES = "note|vc|exp|mem"


def help_types(progress: bool = False) -> str:
    return MEMORY_HELP_TYPES + ("|" + PROGRESS_TYPE if progress else "")


def type_tokens(progress: bool = False) -> Tuple[str, ...]:
    """The type tokens in force: :data:`TYPE_TOKENS`, plus ``progress`` when ``progress`` (the flag is on)."""
    return TYPE_TOKENS + (PROGRESS_TYPE,) if progress else TYPE_TOKENS


KIND_ALIASES = {"mem": "mem", "memory": "mem", "": "mem",
                "note": "note", "notes": "note",
                "vc": "vc", "view-change": "vc", "view_change": "vc",
                "exp": "exp", "experience": "exp",
                "wish": "wish", "wishes": "wish"}
WEIGHTS = ("high", "medium", "low")
DEFAULT_WEIGHT = "medium"
NOTE_KIND_VALUE = "note-to-self"
MEM_KIND_VALUE = "memory"
WISH_KIND_VALUE = "wish"
#: a wish is OPEN until taken up or done (the near-finish query filters state=open)
WISH_STATE = "open"
#: a wish is shareable across the team unless its author says otherwise (operator)
WISH_SHARED_DEFAULT = "yes"
WISH_SHARED_VALUES = {"yes": "yes", "no": "no", "true": "yes", "false": "no"}
#: kinds that carry no ``weight`` property
UNWEIGHTED_KINDS = ("mem", "wish")
#: operator refinement 2026-09-28: every memory carries an author-written digest line.
SUMMARY_MAX = 140
DEFAULT_LIFELOG_RELPATH = os.path.join("personality", "lifelog.lts")

_NODE_ID_RE = re.compile(r"^[A-Z][A-Z0-9_-]*$")
_PREFIX_RE = re.compile(r"^[A-Z][A-Z0-9]*$")
_LIFELOG_ID_RE = re.compile(r"^([A-Z][A-Z0-9]*)-LIFELOG$")
SLUG_MAX = 48


class MemoryError_(Exception):
    """A refusal BEFORE anything is written (bad args / unresolvable target)."""


@dataclass
class PersonalityTarget:
    instance_id: str
    path: str
    prefix: str
    how: str


@dataclass
class RememberResult:
    ok: bool                      # written AND read back fresh
    written: bool                 # the file changed (gate passed)
    receipt: str                  # the ONE line an agent/operator sees
    node_id: Optional[str] = None
    path: Optional[str] = None
    line: Optional[int] = None
    corpus: Optional[str] = None
    gate_ok: bool = False
    index_fresh: bool = False
    reason: str = ""
    backup_path: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "written": self.written, "receipt": self.receipt,
                "node_id": self.node_id, "path": self.path, "line": self.line,
                "corpus": self.corpus, "gate_ok": self.gate_ok,
                "index_fresh": self.index_fresh, "reason": self.reason}


# --- pure helpers ---------------------------------------------------------------

def normalize_kind(kind: Any, progress: bool = False) -> str:
    """``None``/empty -> ``mem`` (the untyped default). ``progress`` (the flag is on) also admits ``progress``."""
    key = str(kind or "").strip().lower()
    k = KIND_ALIASES.get(key)
    if k is None and progress and key == PROGRESS_TYPE:
        return PROGRESS_TYPE
    if k is None:
        raise MemoryError_(f"unknown type {kind!r} — use {' | '.join(type_tokens(progress))} (or omit it)")
    return k


def check_summary(summary: Any) -> Tuple[Optional[str], str, bool]:
    """``(summary_or_None, error, folded)`` — required, single line, <= SUMMARY_MAX.

    Newlines/runs of whitespace are folded (and the fold is REPORTED); an empty
    or over-long summary is an error (never silently truncated)."""
    if not isinstance(summary, str) or not summary.strip():
        return None, "short_summary is required (the memory's one-line digest)", False
    folded, did_fold = _one_line(summary)
    if len(folded) > SUMMARY_MAX:
        return None, (f"short_summary is {len(folded)} chars; the cap is {SUMMARY_MAX} — "
                      f"shorten it (put the full text in 'what')"), did_fold
    return folded, "", did_fold


def heading_title(summary: str) -> str:
    """The summary as a SAFE heading: square brackets become parentheses so a
    bracketed node id in the summary is never parsed as the heading's id."""
    return summary.replace("[", "(").replace("]", ")")


def slugify(title: str, max_len: int = SLUG_MAX) -> str:
    """Title -> an id-safe UPPER-KEBAB slug (ASCII; never empty)."""
    ascii_ = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Z0-9]+", "-", ascii_.upper()).strip("-")
    if len(slug) > max_len:
        cut = slug[:max_len]
        slug = cut.rsplit("-", 1)[0] if "-" in cut[max_len // 2:] else cut
        slug = slug.strip("-")
    return slug or "ENTRY"


def make_node_id(prefix: str, kind: str, title: str, existing: "set[str] | frozenset[str]") -> str:
    """``<PREFIX>-<NOTE|VC|EXP>-<SLUG>``; a collision gets ``-2``, ``-3`` ..."""
    if not _PREFIX_RE.match(prefix or ""):
        raise MemoryError_(f"personality prefix {prefix!r} is not id-shaped (^[A-Z][A-Z0-9]*$)")
    cat = KINDS[normalize_kind(kind)][0]
    base = f"{prefix}-{cat}-{slugify(title)}"
    node_id, n = base, 1
    while node_id in existing:
        n += 1
        node_id = f"{base}-{n}"
    if not _NODE_ID_RE.match(node_id):  # defensive; slugify guarantees the shape
        raise MemoryError_(f"generated id {node_id!r} is not id-shaped")
    return node_id


def _one_line(value: str) -> Tuple[str, bool]:
    """The gated writer refuses multi-line values; fold whitespace and SAY so."""
    folded = " ".join(value.split())
    return folded, folded != value.strip()


def _parse_ids(path: str) -> List[str]:
    from factlattice.parser import parse_text

    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    return [n.id for n in parse_text(text, path).nodes]


# --- target resolution (identity-scoped) --------------------------------------------

def _profile_store(profile_store: Any = None):
    if profile_store is not None:
        return profile_store
    from .config import ConfigStore
    from .profile import ProfileStore

    return ProfileStore(ConfigStore().load().intelligences_root())


def _lifelog_path(instance_id: str, store: Any) -> Tuple[Any, str, List[str]]:
    """``(profile or None, absolute lifelog path, how it was found)`` — the ONE place the instance's personality
    corpus path is derived (``resolve_target`` and the X92 progress writer both use it; the progress file lives
    in this path's DIRECTORY, never at a fixed relative path)."""
    try:
        prof = store.read(instance_id)
    except KeyError:
        prof = None  # no profile.json: pure convention (loud if the file is absent too)
    how: List[str] = []
    if prof is not None and prof.personality_corpus_path:
        path = os.path.expanduser(prof.personality_corpus_path)
        how.append("profile.personality_corpus_path")
    else:
        path = os.path.join(store.instance_dir(instance_id), DEFAULT_LIFELOG_RELPATH)
        how.append("convention path")
    return prof, os.path.abspath(path), how


def resolve_target(instance_id: str, *, profile_store: Any = None) -> PersonalityTarget:
    """The instance's OWN lifelog file + id prefix. Raises :class:`MemoryError_`."""
    from .profile import _validate_instance_id

    try:
        _validate_instance_id(instance_id)
    except ValueError as exc:
        raise MemoryError_(str(exc)) from None
    store = _profile_store(profile_store)
    prof, path, how = _lifelog_path(instance_id, store)
    if not os.path.isfile(path):
        raise MemoryError_(f"no personality corpus for {instance_id!r}: {path} is not a file "
                           f"(set personality_corpus_path in its profile.json)")
    if prof is not None and prof.personality_prefix:
        prefix = prof.personality_prefix
        how.append("profile.personality_prefix")
    else:
        roots = [m.group(1) for m in (_LIFELOG_ID_RE.match(i) for i in _parse_ids(path)) if m]
        if len(roots) != 1:
            raise MemoryError_(f"cannot derive the id prefix for {instance_id!r}: {path} has "
                               f"{len(roots)} <PREFIX>-LIFELOG root node(s) — set "
                               f"personality_prefix in its profile.json")
        prefix = roots[0]
        how.append("<PREFIX>-LIFELOG root id")
    if not _PREFIX_RE.match(prefix):
        raise MemoryError_(f"personality prefix {prefix!r} is not id-shaped (^[A-Z][A-Z0-9]*$)")
    return PersonalityTarget(instance_id, path, prefix, " + ".join(how))


# --- index (the engine the daemon builds) -------------------------------------------

def _registry_corpora(registry_path: Optional[str]) -> Tuple[Optional[Dict[str, dict]], str]:
    if registry_path is None:
        from .app.graph_viewer import resolve_registry_path

        registry_path, how = resolve_registry_path()
        if registry_path is None:
            return None, how
    try:
        with open(registry_path, "r", encoding="utf-8") as fh:
            return json.load(fh), registry_path
    except (OSError, ValueError) as exc:
        return None, f"registry {registry_path} unreadable: {exc}"


def corpus_for_path(path: str, corpora: Dict[str, dict]) -> Optional[str]:
    """The registered corpus whose root contains ``path`` (realpath on both sides)."""
    real = os.path.realpath(path)
    hits = []
    for cid, spec in (corpora or {}).items():
        roots = [r.get("path") for r in (spec.get("roots") or []) if isinstance(r, dict)]
        if spec.get("root"):
            roots.append(spec["root"])
        for root in roots:
            if not root:
                continue
            rr = os.path.realpath(os.path.expanduser(root))
            try:
                if os.path.commonpath([rr, real]) == rr:
                    hits.append(cid)
            except ValueError:
                continue
    return hits[0] if len(hits) == 1 else None


def _default_engine_factory(corpus_id: str, spec: dict):
    from factlattice_engine.config import build_store
    from factlattice_engine.tools import LtsEngine

    return LtsEngine(build_store({corpus_id: spec}), self_heal=True)


# --- the writer ---------------------------------------------------------------------

def remember(instance_id: str, kind: Optional[str], summary: str, what: str, *,
             profile_store: Any = None,
             registry_path: Optional[str] = None,
             registry: Optional[Dict[str, dict]] = None,
             engine_factory: Optional[Callable[[str, dict], Any]] = None,
             today: Optional[date] = None,
             **extra: Any) -> RememberResult:
    """Append ONE memory node to ``instance_id``'s OWN personality corpus.

 ``kind``: ``progress`` (flag ``progress_entries``): see :func:`remember_progress` — a per-session
    progress file, ``session_dir`` given by the harness; refused as an unknown type while the flag is off.
    ``None``/``mem`` (the untyped default class, id ``<P>-MEM-*``,
    ``kind: memory``, no weight) | ``note`` | ``vc`` | ``exp`` | ``wish``
 (id ``<P>-WISH-*``, ``kind: wish``, ``state: open``, no weight;
    extras ``why_meaningful`` / ``size`` / ``shared`` (yes|no, default yes)).
    ``summary``: REQUIRED one-line digest (<= 140 chars) — stored as the
    ``summary`` property, used as the heading title, and slugged into the id.
    ``what``: the full body.
    ``extra``: ``how_to_apply`` (any type), ``through_line`` (exp), ``weight``
    (typed classes only; high|medium|low, default medium). Anything else — in
    particular any path / corpus / instance argument — is refused.
    Never raises for a user-facing condition — returns a :class:`RememberResult`
    whose ``receipt`` is one line.
    """
    def refuse(reason: str, **kw: Any) -> RememberResult:
        return RememberResult(ok=False, written=False, reason=reason,
                              receipt=f"remember REFUSED (nothing written): {reason}", **kw)

    # ---- the progress type (flag progress_entries) has its own writer; with the flag OFF it falls through to
    # normalize_kind below and is refused exactly like any unknown type (nothing written). ----
    if str(kind or "").strip().lower() == PROGRESS_TYPE:
        on = extra.pop("progress_enabled", None)
        if on is None:
            on = progress_flag_on()
        if on:
            seams = {kk: extra.pop(kk) for kk in ("now", "add_node_fn", "max_bytes") if kk in extra}
            return remember_progress(instance_id, summary, what, session_dir=extra.pop("session_dir", None),
                                     profile_store=profile_store, registry_path=registry_path, registry=registry,
                                     engine_factory=engine_factory, **seams, **extra)

    # ---- validate args (before touching anything) ----
    try:
        k = normalize_kind(kind)
    except MemoryError_ as exc:
        return refuse(str(exc))
    if not isinstance(what, str) or not what.strip():
        return refuse("what is empty")
    summ, err, summary_folded = check_summary(summary)
    if summ is None:
        return refuse(err)
    cat, what_key, allowed = KINDS[k]
    allowed_all = set(allowed) | ({"weight"} if k not in UNWEIGHTED_KINDS else set())
    unknown = sorted(set(extra) - allowed_all)
    if unknown:
        return refuse(f"unknown/inapplicable field(s) for type={k}: {unknown} "
                      f"(allowed: {sorted(allowed_all)})")
    weight = str(extra.get("weight") or DEFAULT_WEIGHT).strip().lower()
    if weight not in WEIGHTS:
        return refuse(f"weight {weight!r} not in {WEIGHTS}")
    if k == "wish" and extra.get("shared") is not None:
        sv = extra.get("shared")
        norm = WISH_SHARED_VALUES.get(sv.strip().lower()) if isinstance(sv, str) else None
        if norm is None:
            return refuse(f"shared must be yes | no, got {sv!r}")
        extra = dict(extra, shared=norm)

    # ---- resolve the caller's OWN target ----
    try:
        target = resolve_target(instance_id, profile_store=profile_store)
        existing = set(_parse_ids(target.path))
    except MemoryError_ as exc:
        return refuse(str(exc))
    except (OSError, UnicodeDecodeError) as exc:
        return refuse(f"personality corpus unreadable: {exc}")

    # ---- index handle BEFORE the write (so the read-back exercises the self-heal) ----
    corpora = registry
    reg_how = "injected registry"
    if corpora is None:
        corpora, reg_how = _registry_corpora(registry_path)
    corpus_id = corpus_for_path(target.path, corpora) if corpora else None
    engine = None
    engine_err = ""
    if corpus_id is None:
        engine_err = (f"{os.path.basename(target.path)} is under NO registered lts-engine corpus "
                      f"root ({reg_how}) — the graph will NOT see this memory")
    else:
        # ids elsewhere in the corpus (other files) also collide in the graph
        try:
            engine = (engine_factory or _default_engine_factory)(corpus_id, corpora[corpus_id])
            part = engine.store.get_partition(corpus_id)
            if part is not None:
                existing |= set(part.nodes)
        except Exception as exc:  # noqa: BLE001 — surfaced in the receipt, never swallowed
            engine_err = f"lts-engine could not be built for {corpus_id}: {type(exc).__name__}: {exc}"
            engine = None

    # ---- build the node ----
    try:
        node_id = make_node_id(target.prefix, k, summ, existing)
    except MemoryError_ as exc:
        return refuse(str(exc))
    folded_any = summary_folded
    w, f = _one_line(what)
    folded_any |= f
    props: Dict[str, str] = {}
    if k == "note":
        props["kind"] = NOTE_KIND_VALUE
    elif k == "mem":
        props["kind"] = MEM_KIND_VALUE
    elif k == "wish":
        props["kind"] = WISH_KIND_VALUE
    props["summary"] = summ
    if k not in UNWEIGHTED_KINDS:
        props["weight"] = weight
    props["state"] = WISH_STATE if k == "wish" else "active"
    props["when"] = (today or date.today()).isoformat()
    props[what_key] = w
    for ek in allowed:
        v = extra.get(ek)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if not isinstance(v, str):
            return refuse(f"{ek} must be a string")
        v1, f = _one_line(v)
        folded_any |= f
        props[ek.replace("_", "-")] = v1
    if k == "wish" and "shared" not in props:
        props["shared"] = WISH_SHARED_DEFAULT

    # ---- the gated write (the SAME function lts_add_node dispatches to) ----
    from factlattice_engine.authoring import add_node as _gated_add_node

    # The HEADING title is sanitised: a "[ID]" in it would be read as the node id
    # (the gate then refuses). The `summary` PROPERTY keeps the text verbatim.
    res = _gated_add_node(target.path, node_id, heading_title(summ), props)
    if not res.get("success"):
        reason = " ".join(str(res.get("reason") or res.get("error") or "gate refused").split())
        return refuse(f"gate: {reason}", node_id=node_id, path=target.path)
    fname = os.path.basename(res["path"])
    line = res.get("heading_line") or res.get("line_number")  # compact receipts (D7) carry line_number
    base = RememberResult(ok=False, written=True, receipt="", node_id=node_id, path=res["path"],
                          line=line, corpus=corpus_id, gate_ok=True,
                          backup_path=res.get("backup_path"))

    # ---- index fresh? read it back through the engine's gate + self-heal ----
    if engine is None and corpus_id is not None and not engine_err:
        engine_err = "no engine"
    fresh_err = engine_err
    if not fresh_err:
        try:
            nres = engine.node(corpus_id, node_id)
        except Exception as exc:  # noqa: BLE001
            nres = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        nd = nres.get("node") if nres.get("success") else None
        if nd is None:
            why = nres.get("reason") or nres.get("error") or nres.get("note") or "not found"
            if nres.get("stale"):
                why = f"index STALE after self-heal: {why}"
            fresh_err = f"read-back failed from {corpus_id}: {' '.join(str(why).split())}"
        else:
            got = {kk: str(vv) for kk, vv in (nd.get("properties") or {}).items()}
            missing = sorted(kk for kk, vv in props.items() if got.get(kk) != vv)
            if missing:
                fresh_err = f"read-back from {corpus_id} differs on {missing}"

    note = " (newlines folded to spaces)" if folded_any else ""
    vc_note = ""
    if k == "vc":
        n_vc = sum(1 for i in existing | {node_id} if i.startswith(f"{target.prefix}-VC-"))
        vc_note = (f" — reminder: VC is for GENUINE view-changes only, rare by design "
                   f"({n_vc} {target.prefix}-VC entries now)")
    if fresh_err:
        base.reason = fresh_err
        base.receipt = (f"remembered {node_id} → {fname}:{line} (gate ok) BUT INDEX NOT FRESH: "
                        f"{fresh_err}{note}{vc_note}")
        return base
    base.ok = True
    base.index_fresh = True
    base.receipt = f"remembered {node_id} → {fname}:{line} (gate ok, index fresh){note}{vc_note}"
    return base


# --- progress entries -----------------------------------------------------
#
# A progress entry is a memory of a new TYPE, ``progress`` (flag ``progress_entries``, default OFF). It goes to a
# SECOND file beside the lifelog — ``progress-<session folder name>.lts`` in the DIRECTORY of the profile's personality
# corpus path — one file per session, so a new session starts a new file and the growth of one file is bounded per
# session. Two fields are stamped by CODE and refused from the caller: ``written-at`` (UTC to the second, read from the
# clock at write time) and ``session`` (the session folder's name). The write is the same in-process typed writer
# (no daemon needed); the first write of a session CREATES the file (create mode), a later one appends.
# Read-back: :func:`recall_progress` finds the latest compaction time of THIS session from THIS session's injection log
# (a bounded tail read) and returns this session's entries written after it. No boot macro selects the ``-PROG-`` id
# category (tests/test_x92_progress_entries.py renders a definition against a corpus that holds entries).

#: The file's schema: the MAJOR version rides in the front matter ``doctype`` as ``progress-v<major>`` (the engine's
#: create writer takes no other front-matter key). A reader MUST refuse a file whose major is above its floor.
PROGRESS_SCHEMA_VERSION = 1
PROGRESS_READER_FLOOR = 1     # the highest major this code reads or appends to
PROGRESS_DOCTYPE_RE = re.compile(r"^progress-v(\d+)$")
PROGRESS_CATEGORY = "PROG"
PROGRESS_FILE_PREFIX = "progress-"
PROGRESS_FILE_SUFFIX = ".lts"
#: the byte bound of ONE session's progress file, set from the timed-write measurement in PROGRESS_BOUND_MEASUREMENT
#: (g120: a bound in bytes, not a count of entries). Past it a write is REFUSED naming this number.
PROGRESS_FILE_MAX_BYTES = 200000
PROGRESS_BOUND_MEASUREMENT = (
    "tools/x92_measure_progress_write.py, 2026-10-16. (1) Time: the typed writer re-parses the whole file around every "
    "append; one append at 25011 B = 0.027 s, 100290 B = 0.020 s, 250242 B = 0.074 s, 400194 B = 0.104 s, 800271 B = "
    "0.568 s, 1600356 B = 1.059 s (load 9.1); again at 200400 B = 0.042 s and 400194 B = 0.106 s (load 15.5). Budget "
    "for a call the agent waits on: 0.25 s. (2) Disk, which is what sets the bound: the engine copies the WHOLE file to "
    "FACTLATTICE_BACKUP_DIR before every append and never prunes (one append at 200000 B left a 199958 B backup), and "
    "ledgers each write in its state dir (about 1510 B). A real fill of one session's file with typed-writer appends: "
    "to 100274 B = 235 appends = 11766395 B of backups + 354921 B state = 12.1 MB; to 200384 B = 470 appends = "
    "47043655 B of backups + 709771 B state = 47.75 MB. Arithmetic: n = B/e entries (e about 426 B), backups = n*B/2 = "
    "B^2/(2e), so B = 200000 gives 47.0 MB, under the 50 MB per-session ceiling; the earlier 400000 gave about 188 MB. "
    "The bound is 200000 bytes (about 470 entries); one append there took 0.042 s.")
#: the writing agent is warned ONCE per session: by the write that takes the file from under to at-or-over this share
#: of the bound (the file only grows, so exactly one write crosses it, and a restarted tool server cannot repeat it).
PROGRESS_WARN_FRACTION = 0.8
#: what a created file's front matter adds to the first entry's size, for the bound check made BEFORE the write
PROGRESS_CREATE_OVERHEAD_BYTES = 256
#: the read of the session's injection log is a TAIL read of at most this many bytes (never the whole log)
PROGRESS_LOG_TAIL_MAX_BYTES = 4 * 1024 * 1024
#: the read-back returns at most this many entries / characters (newest kept); the reply says how many it left out
PROGRESS_RECALL_MAX_ENTRIES = 100
PROGRESS_RECALL_MAX_CHARS = 60000
_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SESSION_STAMP_RE = re.compile(r"(\d{8}-\d{6})$")
_WRITTEN_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_STAMPED_FIELDS = ("written_at", "written-at", "session")


def progress_flag_on(config: Any = None) -> bool:
    """Is the flag ``progress_entries`` on (machine config; ``config`` is injectable)? Fails CLOSED: any error -> False."""
    try:
        from . import flags as _flags

        return bool(_flags.flag(PROGRESS_FLAG, config=config))
    except Exception:  # noqa: BLE001 - an unreadable config means the feature is simply not offered
        return False


def session_name(session_dir: Any) -> Tuple[Optional[str], str]:
    """``(name, error)``: the session's folder name (the harness's own ``session_dir``), safe to use as a file name."""
    if not isinstance(session_dir, str) or not session_dir.strip():
        return None, "no session folder from the harness (a progress entry is stamped with, and filed under, its session)"
    name = os.path.basename(session_dir.rstrip(os.sep))
    if not _SESSION_NAME_RE.match(name):
        return None, f"the session folder name {name!r} is not file-name-safe (^[A-Za-z0-9][A-Za-z0-9._-]*$)"
    return name, ""


def progress_file_name(session: str) -> str:
    return f"{PROGRESS_FILE_PREFIX}{session}{PROGRESS_FILE_SUFFIX}"


def _progress_location(instance_id: str, session_dir: Any, profile_store: Any = None
                       ) -> Tuple[Optional[Tuple[str, str, str, str]], str]:
    """``((identity folder, lifelog path, session name, progress file path), error)``. The folder is the directory of the
    profile's personality corpus path; it must already exist (a missing one is refused with the folder NAMED)."""
    from .profile import _validate_instance_id

    try:
        _validate_instance_id(instance_id)
    except ValueError as exc:
        return None, str(exc)
    name, err = session_name(session_dir)
    if name is None:
        return None, err
    store = _profile_store(profile_store)
    _prof, lifelog, _how = _lifelog_path(instance_id, store)
    folder = os.path.dirname(lifelog)
    if not os.path.isdir(folder):
        return None, f"the identity folder {folder} does not exist (set personality_corpus_path in its profile.json)"
    return (folder, lifelog, name, os.path.join(folder, progress_file_name(name))), ""


def check_progress_schema(path: str) -> str:
    """'' if ``path`` is a progress file this code may read or append to, else the reason (loud): no recognisable
    ``doctype: progress-v<major>``, or a major above :data:`PROGRESS_READER_FLOOR` (a NEWER format is never read or
    appended to by an older reader)."""
    try:
        from factlattice.parser import parse_text

        with open(path, "r", encoding="utf-8") as fh:
            fm = parse_text(fh.read(), path).frontmatter or {}
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return f"progress file unreadable: {exc}"
    m = PROGRESS_DOCTYPE_RE.match(str(fm.get("doctype") or "").strip())
    if not m:
        return (f"{os.path.basename(path)} carries no progress schema version (front matter doctype "
                f"{fm.get('doctype')!r}, expected progress-v<major>); not read or written")
    if int(m.group(1)) > PROGRESS_READER_FLOOR:
        return (f"{os.path.basename(path)} is progress schema v{m.group(1)}, newer than this reader's floor "
                f"v{PROGRESS_READER_FLOOR}; refusing to read or append (update the reader)")
    return ""


def _utc_stamp(now: Optional[datetime] = None) -> str:
    n = now or datetime.now(timezone.utc)
    return n.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_size(node_id: str, title: str, props: Dict[str, str]) -> int:
    """Bytes the writer adds for one node (heading + property lines + separators), UTF-8."""
    text = f"\n## {title} [{node_id}]\n\n" + "".join(f"- {k}: {v}\n" for k, v in props.items())
    return len(text.encode("utf-8"))


def _progress_id(prefix: str, session: str, existing: "set[str] | frozenset[str]") -> str:
    """``<PREFIX>-PROG-<session stamp>-<n>``: time-derived from the session folder's name (so it is unique across the
    corpus, where every session has its own file), ``n`` counting this session's entries."""
    m = _SESSION_STAMP_RE.search(session)
    tag = m.group(1) if m else slugify(session)
    base = f"{prefix}-{PROGRESS_CATEGORY}-{tag}"
    n = 1
    while f"{base}-{n}" in existing:
        n += 1
    node_id = f"{base}-{n}"
    if not _NODE_ID_RE.match(node_id):
        raise MemoryError_(f"generated id {node_id!r} is not id-shaped")
    return node_id


def _reread(path: str, node_id: str) -> Tuple[bool, bool]:
    """``(file exists, node_id is in it)`` read afresh from the path (the step between an unknown create outcome and
    any retry)."""
    if not os.path.lexists(path):
        return False, False
    try:
        return True, node_id in set(_parse_ids(path))
    except (OSError, UnicodeDecodeError, ValueError):
        return True, False


def _refusal_code(res: Dict[str, Any]) -> str:
    ev = res.get("evidence") if isinstance(res.get("evidence"), dict) else {}
    return str(ev.get("refusal") or res.get("code") or "")


def _is_unknown(res: Dict[str, Any]) -> bool:
    return res.get("outcome") == "unknown" or _refusal_code(res) == "create_outcome_unknown"


def _open_index(path: str, registry_path: Optional[str], registry: Optional[Dict[str, dict]],
                engine_factory: Optional[Callable[[str, dict], Any]]) -> Tuple[Optional[str], Any, str, str]:
    """``(corpus id, engine or None, why-not, 'root' | 'unknown')``: the corpus whose root contains ``path`` and an engine
    over it, built BEFORE the write (so the read-back exercises the engine's self-heal). ``why-not`` is '' when both
    exist. The 4th item is ``'root'`` when the registry itself says no corpus root contains the path (a certain
    answer) and ``'unknown'`` when the reason cannot be told from a down engine."""
    corpora = registry
    reg_how = "injected registry"
    if corpora is None:
        corpora, reg_how = _registry_corpora(registry_path)
    if not corpora:
        return None, None, f"the corpus registry could not be read ({reg_how})", "unknown"
    corpus_id = corpus_for_path(path, corpora)
    if corpus_id is None:
        return None, None, (f"{os.path.dirname(path)} is under NO registered lts-engine corpus root in {reg_how}"), "root"
    try:
        engine = (engine_factory or _default_engine_factory)(corpus_id, corpora[corpus_id])
    except Exception as exc:  # noqa: BLE001 - surfaced in the receipt, never swallowed
        return corpus_id, None, f"lts-engine could not be built for {corpus_id}: {type(exc).__name__}: {exc}", "unknown"
    return corpus_id, engine, "", "unknown"


def _not_confirmed(why: str, kind: str, folder: str) -> str:
    """The reply fragment for an entry that is written but whose indexing could not be confirmed. It never says
    'indexed'. When the reason cannot be told apart (daemon down vs not a corpus root) it names BOTH."""
    if kind == "root":
        return (f"written but NOT CONFIRMED INDEXED: {why} — the graph will not see this entry until that folder "
                f"is a registered corpus root")
    return (f"written but NOT CONFIRMED INDEXED: {why}. The engine's reply cannot tell two causes apart: the "
            f"lts-engine daemon may be down, OR {folder} may not be a registered corpus root")


def remember_progress(instance_id: str, summary: str, what: str, *, session_dir: Any,
                      profile_store: Any = None, registry_path: Optional[str] = None,
                      registry: Optional[Dict[str, dict]] = None,
                      engine_factory: Optional[Callable[[str, dict], Any]] = None,
                      now: Optional[datetime] = None, add_node_fn: Optional[Callable[..., Any]] = None,
                      max_bytes: Optional[int] = None, **extra: Any) -> RememberResult:
    """Write ONE progress entry to ``instance_id``'s OWN per-session progress file (see the block comment above).

    Called by :func:`remember` for ``type=progress`` once the flag is known to be on. ``session_dir`` is the harness's
    own (never the caller's). The caller supplies ONLY ``summary`` and ``what``: ``written-at`` and ``session`` are
    stamped here and refused if offered. Never raises for a user-facing condition."""
    def refuse(reason: str, **kw: Any) -> RememberResult:
        return RememberResult(ok=False, written=False, reason=reason,
                              receipt=f"remember REFUSED (nothing written): {reason}", **kw)

    bound = PROGRESS_FILE_MAX_BYTES if max_bytes is None else max_bytes
    if extra:
        stamped = sorted(k for k in extra if k in _STAMPED_FIELDS)
        note = (f" ({stamped} are stamped by the code and cannot be supplied)" if stamped else "")
        return refuse(f"unknown/inapplicable field(s) for type={PROGRESS_TYPE}: {sorted(extra)} "
                      f"(allowed: none beyond the summary and the text){note}")
    if not isinstance(what, str) or not what.strip():
        return refuse("what is empty")
    summ, err, summary_folded = check_summary(summary)
    if summ is None:
        return refuse(err)
    loc, err = _progress_location(instance_id, session_dir, profile_store)
    if loc is None:
        return refuse(err)
    folder, _lifelog, session, ppath = loc
    try:
        target = resolve_target(instance_id, profile_store=profile_store)
        existing = set(_parse_ids(ppath)) if os.path.isfile(ppath) else set()
    except MemoryError_ as exc:
        return refuse(str(exc))
    except (OSError, UnicodeDecodeError) as exc:
        return refuse(f"progress file unreadable: {exc}")
    if os.path.isfile(ppath):
        bad = check_progress_schema(ppath)
        if bad:
            return refuse(bad, path=ppath)

    corpus_id, engine, idx_err, idx_kind = _open_index(ppath, registry_path, registry, engine_factory)
    if engine is not None:
        try:
            part = engine.store.get_partition(corpus_id)
            if part is not None:
                existing |= set(part.nodes)
        except Exception as exc:  # noqa: BLE001
            engine, idx_err, idx_kind = None, f"lts-engine could not be read for {corpus_id}: {type(exc).__name__}: {exc}", "unknown"

    try:
        node_id = _progress_id(target.prefix, session, existing)
    except MemoryError_ as exc:
        return refuse(str(exc))
    w, folded_what = _one_line(what)
    props: Dict[str, str] = {"written-at": _utc_stamp(now), "session": session, "summary": summ, "what": w}
    title = heading_title(summ)

    # ---- the byte bound (g120): refuse BEFORE writing, naming the bound ----
    try:
        size_before = os.path.getsize(ppath) if os.path.isfile(ppath) else 0
    except OSError as exc:
        return refuse(f"progress file unreadable: {exc}")
    created_overhead = PROGRESS_CREATE_OVERHEAD_BYTES if not os.path.lexists(ppath) else 0
    projected = size_before + created_overhead + _entry_size(node_id, title, props)
    if projected > bound:
        return refuse(f"this session's progress file {os.path.basename(ppath)} is {size_before} bytes and this entry "
                      f"would make it about {projected}, past the bound of {bound} bytes "
                      f"(memory.PROGRESS_FILE_MAX_BYTES); it takes no more entries this session — put the rest "
                      f"in your handover", node_id=node_id, path=ppath)

    # ---- the write: create on the first write of the session, append otherwise ----
    if add_node_fn is None:
        from factlattice_engine.authoring import add_node as add_node_fn  # type: ignore[no-redef]
    created = False
    res: Dict[str, Any]
    if not os.path.lexists(ppath):
        fm = _lifelog_frontmatter(_lifelog)
        create_kw = dict(create=True, doc_title=f"Progress entries — {session}",
                         domain=fm.get("domain"), doctype=f"progress-v{PROGRESS_SCHEMA_VERSION}")
        res = add_node_fn(ppath, node_id, title, props, **create_kw)
        if res.get("success"):
            created = True
        elif _refusal_code(res) == "create_file_exists":
            res = add_node_fn(ppath, node_id, title, props)            # the file appeared meanwhile: APPEND
        elif _is_unknown(res):
            exists, has_ours = _reread(ppath, node_id)                # RE-READ the path before any retry
            if has_ours:
                res = {"success": True, "path": ppath, "line_number": None, "reread": True}
                created = True
            elif exists:
                res = add_node_fn(ppath, node_id, title, props)
            else:
                res = add_node_fn(ppath, node_id, title, props, **create_kw)   # nothing there: one retry
                created = bool(res.get("success"))
                if _is_unknown(res):
                    return refuse("the create's outcome is still unknown after a re-read and one retry; nothing "
                                  "further is attempted (check the progress file before writing again)",
                                  node_id=node_id, path=ppath)
    else:
        res = add_node_fn(ppath, node_id, title, props)
    if not res.get("success"):
        reason = " ".join(str(res.get("reason") or res.get("error") or "gate refused").split())
        return refuse(f"gate: {reason}", node_id=node_id, path=ppath)

    fname = os.path.basename(ppath)
    line = res.get("heading_line") or res.get("line_number")
    base = RememberResult(ok=False, written=True, receipt="", node_id=node_id, path=ppath, line=line,
                          corpus=corpus_id, gate_ok=True, backup_path=res.get("backup_path"))
    try:
        size_after = os.path.getsize(ppath)
    except OSError:
        size_after = size_before
    warn_at = PROGRESS_WARN_FRACTION * bound
    warning = ""
    if size_before < warn_at <= size_after:
        warning = (f" WARNING: this session's progress file is now {size_after} bytes, {size_after * 100 // bound}% of its "
                   f"{bound}-byte bound; past the bound writes are refused (shown once per session).")

    # ---- confirm indexed: read it back through the engine (its freshness gate + self-heal) ----
    fresh_err, fresh_kind = idx_err, idx_kind
    if engine is not None and not fresh_err:
        try:
            nres = engine.node(corpus_id, node_id)
        except Exception as exc:  # noqa: BLE001
            nres = {"success": False, "error": f"{type(exc).__name__}: {exc}"}
        nd = nres.get("node") if nres.get("success") else None
        if nd is None:
            why = nres.get("reason") or nres.get("error") or nres.get("note") or "not found"
            fresh_err = f"read-back failed from {corpus_id}: {' '.join(str(why).split())}"
            fresh_kind = "unknown"
        else:
            got = {kk: str(vv) for kk, vv in (nd.get("properties") or {}).items()}
            missing = sorted(kk for kk, vv in props.items() if got.get(kk) != vv)
            if missing:
                fresh_err, fresh_kind = f"read-back from {corpus_id} differs on {missing}", "differs"
    note = " (newlines folded to spaces)" if (summary_folded or folded_what) else ""
    hint = (" — after a compaction, read your entries back with memory(type=progress, recall=true)"
            if created else "")
    head = f"progress {node_id} → {fname}:{line} (gate ok"
    if fresh_err:
        base.reason = fresh_err
        if fresh_kind == "differs":
            base.receipt = f"{head}) BUT INDEX NOT FRESH: {fresh_err}{note}{warning}{hint}"
        else:
            base.receipt = f"{head}) {_not_confirmed(fresh_err, fresh_kind, folder)}{note}{warning}{hint}"
        base.extra.update(created=created, size_after=size_after, warned=bool(warning))
        return base
    base.ok = True
    base.index_fresh = True
    base.receipt = f"{head}, index fresh){note}{warning}{hint}"
    base.extra.update(created=created, size_after=size_after, warned=bool(warning))
    return base


def _lifelog_frontmatter(lifelog: str) -> Dict[str, str]:
    """``domain`` of the lifelog's front matter (so a created progress file shares its corpus domain)."""
    try:
        from factlattice.parser import parse_text

        with open(lifelog, "r", encoding="utf-8") as fh:
            fm = parse_text(fh.read(), lifelog).frontmatter or {}
    except Exception:  # noqa: BLE001 - optional metadata only
        return {}
    return {k: str(v) for k, v in fm.items() if k == "domain" and v}


def last_compaction(log_path: Optional[str], *, max_bytes: int = PROGRESS_LOG_TAIL_MAX_BYTES) -> Dict[str, Any]:
    """The two latest COMPACTIONS in ONE session's injection log, from a BOUNDED TAIL read (at most ``max_bytes``).

    ``{"found", "ts_iso", "phase", "previous_ts_iso", "count", "log_bytes", "bytes_read", "window_covers_log", "error"}``.
    A compaction is the log's ``point: compact`` lines for one compaction: a ``pre`` line then its ``post`` line (a lone
    line of either phase is one compaction). Its time is its ``post`` time, else its ``pre`` time. ``ts_iso`` is the
    LATEST compaction's time, ``previous_ts_iso`` the one before it (``None`` when the window holds only one), ``count``
    how many the window holds."""
    out: Dict[str, Any] = {"found": False, "ts_iso": None, "phase": None, "previous_ts_iso": None, "count": 0,
                           "log_bytes": 0, "bytes_read": 0, "window_covers_log": True, "error": ""}
    if not log_path:
        out["error"] = "no injection log path"
        return out
    try:
        fd = os.open(log_path, os.O_RDONLY)
    except FileNotFoundError:
        return out                      # a session that has logged nothing yet: no compaction recorded
    except OSError as exc:
        out["error"] = f"injection log unreadable: {exc}"
        return out
    try:
        size = os.fstat(fd).st_size
        start = max(0, size - max_bytes)
        os.lseek(fd, start, os.SEEK_SET)
        chunks: List[bytes] = []
        total = 0
        while total < size - start:
            chunk = os.read(fd, min(1 << 16, size - start - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        out["error"] = f"injection log unreadable: {exc}"
        return out
    finally:
        os.close(fd)
    data = b"".join(chunks)
    out.update(log_bytes=size, bytes_read=len(data), window_covers_log=(start == 0))
    lines = data.split(b"\n")
    if start > 0 and lines:
        lines = lines[1:]               # the first line of a mid-file window is partial
    events: List[Tuple[float, Any]] = []
    for raw in lines:
        if b'"compact"' not in raw:
            continue
        try:
            rec = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("point") != "compact":
            continue
        ts = rec.get("ts")
        if isinstance(ts, bool) or not isinstance(ts, (int, float)):
            continue
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
        events.append((float(ts), payload.get("phase")))
    events.sort(key=lambda e: e[0])
    comps: List[Dict[str, Any]] = []    # one per compaction: {"t": time, "post": bool}
    for ts, phase in events:
        if phase == "post" and comps and not comps[-1]["post"]:
            comps[-1].update(t=ts, post=True)
        else:
            comps.append({"t": ts, "post": phase == "post", "phase": phase})
    out["count"] = len(comps)
    if comps:
        out.update(found=True, ts_iso=_utc_stamp(datetime.fromtimestamp(comps[-1]["t"], timezone.utc)),
                   phase="post" if comps[-1]["post"] else comps[-1].get("phase"))
    if len(comps) >= 2:
        out["previous_ts_iso"] = _utc_stamp(datetime.fromtimestamp(comps[-2]["t"], timezone.utc))
    return out


def recall_progress(instance_id: str, *, session_dir: Any, profile_store: Any = None,
                    log_path: Optional[str] = None, max_log_bytes: int = PROGRESS_LOG_TAIL_MAX_BYTES,
                    max_entries: int = PROGRESS_RECALL_MAX_ENTRIES,
                    max_chars: int = PROGRESS_RECALL_MAX_CHARS) -> Dict[str, Any]:
    """This session's progress entries written AFTER its last compaction (the boundary is found here, from the
    session's own injection log; the caller supplies no number or time). Never raises."""
    def fail(reason: str) -> Dict[str, Any]:
        return {"ok": False, "entries": [], "receipt": f"recall REFUSED (nothing read): {reason}"}

    loc, err = _progress_location(instance_id, session_dir, profile_store)
    if loc is None:
        return fail(err)
    _folder, _lifelog, session, ppath = loc
    if log_path is None:
        from .harness.hooks import injection_log_path

        log_path = injection_log_path(session_dir)
    boundary = last_compaction(log_path, max_bytes=max_log_bytes)
    if boundary["error"]:
        return fail(boundary["error"])
    if not os.path.isfile(ppath):
        return {"ok": True, "entries": [], "boundary": boundary, "total_in_file": 0, "omitted": 0,
                "receipt": f"no progress entries yet for session {session}"}
    bad = check_progress_schema(ppath)
    if bad:
        return fail(bad)
    try:
        size = os.path.getsize(ppath)
        if size > 2 * PROGRESS_FILE_MAX_BYTES:
            return fail(f"{os.path.basename(ppath)} is {size} bytes, over twice the {PROGRESS_FILE_MAX_BYTES}-byte bound; "
                        f"not read")
        from factlattice.parser import parse_text

        with open(ppath, "r", encoding="utf-8") as fh:
            nodes = parse_text(fh.read(), ppath).nodes
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return fail(f"progress file unreadable: {exc}")
    # the stretch that JUST ENDED: everything since the PREVIOUS boundary (the second-latest compaction), including
    # what was written after the latest one; with one compaction (or none) that is since the session's first entry
    after = boundary["previous_ts_iso"]
    rows: List[Dict[str, str]] = []
    skipped = 0
    for n in nodes:
        pr = n.properties or {}
        wat = str(pr.get("written-at") or "")
        if not _WRITTEN_AT_RE.match(wat) or str(pr.get("session") or "") != session:
            skipped += 1
            continue
        if after is not None and not wat > after:
            continue
        rows.append({"id": n.id, "written_at": wat, "summary": str(pr.get("summary") or n.title or ""),
                     "what": str(pr.get("what") or "")})
    rows.sort(key=lambda r: r["written_at"])
    total = len(rows)
    kept: List[Dict[str, str]] = []
    chars = 0
    for r in reversed(rows):            # keep the NEWEST that fit
        c = len(r["id"]) + len(r["summary"]) + len(r["what"]) + 40
        if len(kept) >= max_entries or chars + c > max_chars:
            break
        kept.append(r)
        chars += c
    kept.reverse()
    omitted = total - len(kept)
    if after is not None:
        since = f"since the previous compaction boundary ({after}; the latest compaction was {boundary['ts_iso']})"
    elif boundary["found"] and boundary["window_covers_log"]:
        since = f"since the session's first entry (one compaction, at {boundary['ts_iso']})"
    elif boundary["found"]:
        since = (f"in the part of the log read ({boundary['bytes_read']} of {boundary['log_bytes']} bytes) only one "
                 f"compaction ({boundary['ts_iso']}) was found, an earlier one may exist; all of this session's entries")
    elif boundary["window_covers_log"]:
        since = "no compaction is recorded in this session's log, so all of this session's entries"
    else:
        since = (f"no compaction found in the last {boundary['bytes_read']} bytes of the {boundary['log_bytes']}-byte "
                 f"log (not the whole log was read), so all of this session's entries")
    receipt = f"{len(kept)} progress entr{'y' if len(kept) == 1 else 'ies'} {since}"
    if omitted:
        receipt += f"; {omitted} older not shown (bounded reply)"
    if skipped:
        receipt += f"; {skipped} node(s) in the file skipped (no valid written-at or another session)"
    return {"ok": True, "entries": kept, "boundary": boundary, "total_in_file": len(nodes), "omitted": omitted,
            "skipped": skipped, "receipt": receipt}


# --- operator-surface parsing (the ``\\memory`` command) -------------------

def memory_usage(progress: bool = False) -> str:
    """The ``\\memory`` usage line, its type list taken from :func:`type_tokens` (the one place)."""
    return (r"usage: \memory [" + "|".join(type_tokens(progress)) + r"] <short summary> :: <full what>   "
            r"(no ' :: ' = the text is both summary and what, <= 140 chars)")


MEMORY_USAGE = memory_usage()
#: `` :: `` delimited by whitespace or a string edge (``std::vector`` never splits)
_SUMMARY_SEP = re.compile(r"(?:^|\s)::(?:\s|$)")


def parse_memory_args(args: str, progress: bool = False) -> Tuple[str, str, str]:
    """``[type] <short summary> :: <full what>`` -> (kind, summary, what).

    The leading token is a TYPE only if it is exactly one of :func:`type_tokens` (``progress``
    only while ``progress`` is true, i.e. the flag is on; case-insensitive); otherwise the whole remainder is the memory text. The
    FIRST space-delimited `` :: `` splits summary from what (so ``std::vector``
    is untouched). With no `` :: `` the whole text is BOTH summary and what and
    must fit the 140-char summary cap — else the caller is asked for `` :: ``.
    Raises :class:`MemoryError_` (nothing written) on any violation.
    """
    s = (args or "").strip()
    kind = "mem"
    parts = s.split(None, 1)
    if parts and parts[0].lower() in type_tokens(progress):
        kind = parts[0].lower()
        s = parts[1].strip() if len(parts) > 1 else ""
    m = _SUMMARY_SEP.search(s)
    if m:
        summary, what = s[:m.start()].strip(), s[m.end():].strip()
        if not summary:
            raise MemoryError_("short summary is empty — " + memory_usage(progress))
        if not what:
            raise MemoryError_("what is empty — " + memory_usage(progress))
    else:
        if not s:
            raise MemoryError_("what is empty — " + memory_usage(progress))
        summary = what = s
        if len(" ".join(s.split())) > SUMMARY_MAX:
            raise MemoryError_(f"{len(s)} chars is too long for a one-line summary (cap {SUMMARY_MAX}) — "
                               f"use '<short summary> :: <full what>'")
    return kind, summary, what


def parse_memory_command(args: str, progress: bool = False) -> Tuple[str, str, str]:
    """Back-compat name for :func:`parse_memory_args`."""
    return parse_memory_args(args, progress)

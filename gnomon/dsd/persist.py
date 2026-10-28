"""DSD answer persistence — mint ``[DEC-DSD-*]`` nodes into the DSD decision
graph (D13/D14/D15).

On save, an answered DSD becomes graph nodes:

* ONE **root** node ``DEC-DSD-<slug>`` — the answered DSD as a whole. Its id is
  stable (derived from the persisted ``dsd_id``) and distinctive, so a later
  overrule recorded anywhere as a ``supersedes: DEC-DSD-<slug>`` property VALUE is
 found by a cross-corpus ``lts_find`` text search on that id ([-
  CORPUS-LINKING]). This is the node-id injected back to the requesting agent (D15).
* ONE **per-decision** child node ``DEC-DSD-<slug>-D<n>`` per answered decision,
  carrying the chosen option + comment + the question it answers.

⛔ THE WRITE TARGET IS A SEAM (on-save; D14 — a corpus SEPARATE
from Gnomon's own spec corpus). :class:`DecisionWriter` is INJECTED. The GUI
(slice 3b) wires the real one (:class:`LtsFileDecisionWriter`, appending to the D14
DSD-answer corpus); tests inject :class:`FakeDecisionWriter` so NO real graph is
touched. There is deliberately no silent real-graph default: a coordinator built
without a writer cannot quietly write to a live corpus.

⛔ D2: the write mints nodes into the corpus ``.lts`` FILE and
builds NO local index. The live lts-engine reads corpus content directly; whether
an answer is live-queryable is a LIVE question (see :mod:`gnomon.dsd.live`),
never a cached local-index flag.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Dict, Iterator, List, Optional, Protocol

try:  # POSIX advisory locks; absent on Windows, where the lock degrades to a no-op
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - not a supported host
    _fcntl = None  # type: ignore[assignment]

from .model import DecisionAnswer, DsdDecision, agreed_with_recommendation, fold_comment  # noqa: F401 (fold_comment re-exported: one shared fold)


# ── the minted node ─────────────────────────────────────────────────────────

@dataclass
class DsdNode:
    """One node to mint. ``properties`` keys are already grammar-legal
    (``^[a-z][a-z0-9_-]*$``); values are free text placed AFTER the colon."""
    node_id: str
    title: str
    properties: Dict[str, str] = field(default_factory=dict)

    def to_lts(self, level: int = 2) -> str:
        """Render as a `.lts` heading + property block. Keys are asserted
        grammar-legal so a value glyph can never turn the whole line to prose
        (the key-grammar hazard); a bad key is a programming error, raised here,
        not a silent loss."""
        hashes = "#" * max(2, level)
        out = [f"{hashes} {self.title} [{self.node_id}]", ""]
        for k, v in self.properties.items():
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", k):
                raise ValueError(f"illegal property key (not ^[a-z][a-z0-9_-]*$): {k!r}")
            # A newline in a value would break the single-line property grammar;
            # collapse to spaces so the value stays on its own line.
            one_line = re.sub(r"\s*\n\s*", " ", str(v)).strip()
            out.append(f"- {k}: {one_line}")
        out.append("")
        return "\n".join(out)


class DsdMintRefused(RuntimeError):
    """The typed write gate refused a mint (X20). Carries the gate's own reason so
    the web layer can show the operator something better than a bare 500. Raised only
    AFTER a preflight, so when it is raised NOTHING was written (all-or-nothing).

    ``node_id`` is the node whose parse was refused; ``reason`` is the gate's
    reason text, verbatim."""

    def __init__(self, node_id: str, reason: str) -> None:
        self.node_id = node_id
        self.reason = reason
        super().__init__(
            f"DSD answer NOT saved (nothing was written): the corpus gate refused "
            f"{node_id}: {reason}")


class DsdRollbackIncomplete(DsdMintRefused):
    """A real write failed midway AND the corpus was changed underneath it by a
    writer outside :func:`corpus_lock` (X20 N4), so the all-or-nothing rollback
    could not remove this mint's own nodes without clobbering that other write. The
    file keeps the other writer's content; ``remaining`` lists this mint's node ids
    that may still be in it. A subclass of :class:`DsdMintRefused` so the controller
    reports it as ``ok=False`` rather than a 500."""

    def __init__(self, node_id: str, reason: str, remaining: List[str]) -> None:
        super().__init__(node_id, reason)
        self.remaining = list(remaining)
        self.args = (
            f"DSD answer PARTIALLY saved: writing {node_id} failed ({reason}) and the "
            f"corpus was changed concurrently, so the rollback kept that change "
            f"instead of overwriting it. Possibly still present: "
            f"{', '.join(self.remaining) or '(none)'}",)


def corpus_lock_path(corpus_path: str) -> str:
    """The sidecar advisory-lock file for ``corpus_path``: a hidden ``.<name>.lock``
    in the same directory. It holds no data and is not a ``.lts`` file."""
    d, b = os.path.split(os.path.abspath(corpus_path))
    return os.path.join(d, "." + b + ".lock")


@contextlib.contextmanager
def corpus_lock(corpus_path: str) -> Iterator[None]:
    """Hold an exclusive advisory lock on the DSD-answer corpus for a whole
    read-modify-write (X20 N4). Every Gnomon writer takes it, so two mints
    (threads or processes) can never interleave a snapshot and a rollback, or two
    read-then-replace appends. It cannot bind writers that ignore it (e.g. an
    ``lts_add_node`` from another session); the typed writer's rollback handles
    those by refusing to clobber (:class:`DsdRollbackIncomplete`). The lock is on a
    sidecar, not the corpus, because the typed gate replaces the corpus file."""
    lp = corpus_lock_path(corpus_path)
    os.makedirs(os.path.dirname(lp) or ".", exist_ok=True)
    fd = os.open(lp, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        yield
    finally:
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
        finally:
            os.close(fd)


_LINE_SEP_RE = re.compile(r"\s*[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]+\s*")


def collapse_lines(text: str) -> str:
    """Collapse every line break / line separator run (CR, LF, VT, FF, NEL, U+2028,
    U+2029 ...) in a free-text value to a SINGLE space, keeping the text otherwise
    (X20b). The typed gate refuses a multi-line property value, so a multi-paragraph
    comment from the DSD textarea must be flattened BEFORE the write."""
    return _LINE_SEP_RE.sub(" ", str(text)).strip()


_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")


def neutralise_ids(text: str) -> str:
    """Make free text safe to use inside a ``.lts`` heading (X20). The heading
    grammar reads a trailing ``[ID]`` as the node id, so a question ending
 ```` displaces the real id. Rewrite every ``[x]`` to ``x`` (drop
    the brackets), remove any stray bracket, and collapse whitespace/newlines so
    the heading stays one line. Property VALUES are not touched (they sit after
    the colon and cannot displace a heading)."""
    out = _BRACKET_RE.sub(r"\1", str(text))
    out = out.replace("[", "").replace("]", "")
    return re.sub(r"\s+", " ", out).strip()


@dataclass
class MintResult:
    """What a mint produced: the root node-id (injected to the agent, D15) and
    every node written."""
    root_node_id: str
    nodes: List[DsdNode]
    target: str = ""                  # a human string naming where they landed


# ── the write seam ──────────────────────────────────────────────────────────

class DecisionWriter(Protocol):
    """Writes minted ``[DEC-DSD-*]`` nodes to the DSD decision graph. INJECTED —
    the GUI wires the real one, tests wire a fake."""

    def mint(self, nodes: List[DsdNode]) -> str:
        """Persist *nodes*; return a human string naming the target."""
        ...


class FakeDecisionWriter:
    """Captures minted nodes in memory — the test double. Writes NO real graph."""

    def __init__(self) -> None:
        self.minted: List[DsdNode] = []
        self.calls: int = 0

    def mint(self, nodes: List[DsdNode]) -> str:
        self.calls += 1
        self.minted.extend(nodes)
        return f"fake:{len(self.minted)} nodes"

    def node_ids(self) -> List[str]:
        return [n.node_id for n in self.minted]

    def by_id(self, node_id: str) -> Optional[DsdNode]:
        for n in self.minted:
            if n.node_id == node_id:
                return n
        return None


class LtsFileDecisionWriter:
    """The fallback real target: APPEND node blocks to a ``.lts`` corpus FILE — the
    D14 DSD-answer corpus, separate from the Gnomon spec corpus. Used only
    when the typed authoring writer is unavailable.

    ⛔ D2: this writes the corpus FILE only, and deliberately builds NO local index.
    The live lts-engine reads corpus content directly; there is no
    ``index_refresh_owed`` concept anymore (a local index would hide an lts-engine
    connectivity problem — the operator's reasoning). Prefer :class:`~gnomon.dsd.writer.
    TypedLtsDecisionWriter` (gate-verified typed write) where ``factlattice`` is
    importable.

    Append never inserts BELOW a ``## Relationships`` / ``## Causal Graph`` section
    (those bullets are edge triples); this file carries none, and new nodes go at
    the tail.
    """

    def __init__(self, corpus_path: str, *, node_level: int = 2) -> None:
        self._path = os.path.abspath(os.path.expanduser(corpus_path))
        self._level = node_level

    @property
    def path(self) -> str:
        return self._path

    def mint(self, nodes: List[DsdNode]) -> str:
        # X20 N4: the read-then-replace below loses an update if two mints
        # interleave; the corpus lock serialises every Gnomon writer.
        blocks = "\n".join(n.to_lts(self._level) for n in nodes)
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with corpus_lock(self._path):
            return self._append_locked(blocks)

    def _append_locked(self, blocks: str) -> str:
        existing = ""
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as fh:
                existing = fh.read()
        else:
            existing = corpus_header()
        if existing and not existing.endswith("\n"):
            existing += "\n"
        new_text = existing + "\n" + blocks
        # Atomic append (write-temp-then-rename) so a crash never truncates the
        # corpus the DSD tab reads.
        d = os.path.dirname(self._path) or "."
        fd, tmp = tempfile.mkstemp(prefix=".dec-dsd-", suffix=".tmp", dir=d)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(new_text)
            os.replace(tmp, self._path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return self._path


def corpus_header(root_id: Optional[str] = None) -> str:
    """The header a NEW DSD-answer corpus file is seeded with. Built from the neutral defaults overlaid by the
    deployment's ``dsd-corpus.json`` (:func:`gnomon.dsd.live.dsd_settings`) at CALL time, never at import.

    ``root_id`` is the file's root heading id (a per-DSD shard passes ``SPEC:<CORPUS>-<DSD-ID>``); the
    frontmatter ``id`` and the root heading carry the SAME value, so no two files share a frontmatter id
. Default: the shared-file id ``SPEC:<CORPUS>``."""
    from .live import dsd_settings

    cfg = dsd_settings()
    node_id = root_id if root_id is not None else "SPEC:" + cfg["corpus_id"].upper()
    return (
        "---\n"
        f"title: \"{cfg['product']} \u2014 DSD Answers (DEC-DSD-*)\"\n"
        f"id: \"{node_id}\"\n"
        f"domain: {cfg['domain']}\n"
        "scope: \"Minted DSD-answer nodes (D14 separate corpus). One root per answered "
        "DSD + one child per answered decision. Machine-written; do not hand-edit.\"\n"
        "status: runtime\n"
        "---\n"
        f"# {cfg['product']} \u2014 DSD Answers [{node_id}]\n"
    )


# ── node minting ────────────────────────────────────────────────────────────

_SLUG_RE = re.compile(r"[^A-Z0-9-]")


def root_node_id(dsd_id: str) -> str:
    """The stable, distinctive root id for an answered DSD, derived from the
    persisted ``dsd_id`` (e.g. ``dsd-18f3a`` -> ``DEC-DSD-18F3A``). Matches the
    LTS node-id grammar ``^[A-Z][A-Z0-9_-]*$``."""
    tail = dsd_id[4:] if dsd_id.lower().startswith("dsd-") else dsd_id
    slug = _SLUG_RE.sub("-", tail.upper()).strip("-") or "X"
    return f"DEC-DSD-{slug}"


#: pre-review B2: the plain scalar properties written on each decision's answer node WHEN ``dsd_notice_inline`` is ON, so the
#: answers corpus can be grouped (``recommended-position`` x ``option-position-chosen``, or by day). CONVENTIONS (the checklist rule that
#: names this interface says the same): positions are 1-BASED; ``0`` means NONE (a comment-only answer, the "Neither / None" escape hatch,
#: or no recommendation on the decision); ``has-comment`` is ``true``/``false``; ``seconds-from-sent-to-answered`` is a whole number of
#: seconds, or the string ``unknown`` when the send time was not known; ``answered-at`` is a UTC instant, ISO 8601 with a trailing ``Z``
#: (the day a node falls in is the ANALYST's to choose, with a stated zone: ``answer_stats.group_counts(zone=...)``); the older
#: ``decided-in-session`` is the web job's LOCAL calendar date at the moment of the mint, date only. A machine-read interface (g16).
ANSWER_FIELD_KEYS = ("option-position-chosen", "option-count", "recommended-position", "has-comment",
                     "seconds-from-sent-to-answered", "answered-at")


def answer_field_props(dec, labels, comment, fields) -> Dict[str, str]:
    """The :data:`ANSWER_FIELD_KEYS` values for one answered decision. Pure; ``dec`` may be ``None`` (counts are then 0)."""
    from .model import _normalize

    options = list(getattr(dec, "options", []) or [])
    chosen = 0
    if len(labels) == 1:
        key = _normalize(labels[0])
        for i, o in enumerate(options, 1):
            if _normalize(o.label) == key:
                chosen = i
                break
    rec_pos = 0
    rid = getattr(dec, "recommended_id", None)
    if rid is not None:
        for i, o in enumerate(options, 1):
            if o.option_id == rid:
                rec_pos = i
                break
    sent, answered = fields.get("sent_epoch"), fields.get("answered_epoch")
    try:
        secs = str(max(0, int(round(float(answered) - float(sent))))) if float(sent) > 0 else "unknown"
    except (TypeError, ValueError):
        secs = "unknown"
    import time as _time

    try:
        at = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(float(answered)))
    except (TypeError, ValueError, OverflowError, OSError):
        at = "unknown"
    return {"option-position-chosen": str(chosen), "option-count": str(len(options)), "recommended-position": str(rec_pos),
            "has-comment": "true" if (comment or "").strip() else "false", "seconds-from-sent-to-answered": secs,
            "answered-at": at}


def mint_dsd_nodes(
    dsd_id: str,
    instance_id: str,
    decisions: List[DsdDecision],
    answers: Dict[str, DecisionAnswer],
    *,
    source_path: str = "",
    decided_on: Optional[str] = None,
    clock: Callable[[], date] = date.today,
    documents: Optional[List[Dict[str, object]]] = None,
    submit_evidence: Optional[str] = None,
    answer_fields: Optional[Dict[str, object]] = None,
) -> List[DsdNode]:
    """Build the ROOT node + one child per ANSWERED decision. Pure — returns the
    nodes; the caller's :class:`DecisionWriter` persists them. Each carries the
 answer + a stable id.

 ``documents`` = the send-time document entries of the DSD record. When
    given and non-empty, the root carries ``documents-at-send`` (every document, name +
    sha256) and each child carries its own decision's. Absent/empty -> no property, so
    the nodes of a DSD with no documents are unchanged.

    ``answer_fields`` (pre-review B2, only with ``dsd_notice_inline`` ON; ``None`` = the nodes are exactly what they were):
    ``{"sent_epoch": float, "answered_epoch": float}``. Each decision's node then also carries the plain scalar properties
    :data:`ANSWER_FIELD_KEYS` for bin analysis (see :func:`answer_field_props`)."""
    from .documents import documents_at_send

    root_id = root_node_id(dsd_id)
    decided = decided_on or clock().isoformat()
    by_id = {d.id: d for d in decisions}

    answered_ids: List[str] = []
    child_nodes: List[DsdNode] = []
    for dec_id, ans in answers.items():
        labels = [l for l in ans.labels if l and l.strip()]
        comment = fold_comment(ans.comment or "")  # comment-fidelity: the SAME fold as the record's Comments line
        if not labels and not comment:
            continue                              # unanswered: not minted
        answered_ids.append(dec_id)
        dec = by_id.get(dec_id)
        props: Dict[str, str] = {
            "part-of": f"[{root_id}]",
            "dsd-id": dsd_id,
            "decision-ref": dec_id,
            "chosen": ", ".join(labels) if labels else "(comment only)",
            "comment": comment or "(none)",
            # operator 2026-09-29: the form pre-selects nothing, so assent is explicit and
            # captured: true | false | n/a (no recommendation in the DSD).
            "agreed-with-recommendation": agreed_with_recommendation(dec, ans),
            "decided-in-session": decided,
        }
        if dec is not None and dec.question:
            props["question"] = collapse_lines(dec.question)
        if dec is not None and dec.theme:
            props["theme"] = collapse_lines(dec.theme)
        if documents:
            dec_docs = documents_at_send(documents, scope=dec_id)
            if dec_docs:
                props["documents-at-send"] = collapse_lines(dec_docs)
        if answer_fields is not None:
            props.update(answer_field_props(dec, labels, comment, answer_fields))
        child_nodes.append(DsdNode(
            node_id=f"{root_id}-{dec_id}",
            title=neutralise_ids(
                f"DSD answer {dec_id}"
                + (f" — {dec.question}" if dec is not None and dec.question else "")),
            properties=props,
        ))

    root_props: Dict[str, str] = {
        "dsd-id": dsd_id,
        "instance-id": instance_id,
        "answered-count": str(len(answered_ids)),
        "answered-decisions": ", ".join(answered_ids) if answered_ids else "(none)",
        "status": "answered",
        "source-path": source_path or "(in-memory)",
        "decided-in-session": decided,
    }
    if documents:
        root_docs = documents_at_send(documents)
        if root_docs:
            root_props["documents-at-send"] = collapse_lines(root_docs)
    if submit_evidence:       # how the submit was proven to come from the operator's tab
        root_props["submit-evidence"] = collapse_lines(neutralise_ids(submit_evidence))
    root = DsdNode(
        node_id=root_id,
        title=neutralise_ids(f"DSD answers for {instance_id} ({dsd_id})"),
        properties=root_props,
    )
    return [root] + child_nodes

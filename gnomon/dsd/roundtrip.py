"""The DSD present -> collect ROUND-TRIP, headless (D15).

The full loop the Gnomon MCP tool ``send_dsd_for_human_review`` fronts:

    agent -> Gnomon MCP -> [DSD tab renders — slice 3b] -> human answers
          -> save (write-back) -> persist (mint DEC-DSD-* nodes)
          -> inject ONLY the DSD ROOT NODE-ID into the requesting agent's PTY (D15)

This module implements the loop HEADLESSLY. The GUI render+answer (slice 3b) is
stubbed by a headless answer path (:func:`default_answers_from_preselect`), so the
whole round-trip is testable without a window.

Separation of concerns:
* ``present`` runs the CONTEXT GUARD — a DSD missing What-this-
  is / Why-it-matters is REFUSED here, publishing/persisting NOTHING and reporting
  loudly (the same predicate the hub's present tool uses).
* ``answer`` runs the FORM gates (single-select v1 + completeness/escape-hatch),
  writes the answers back into the slots, mints the nodes via the injected
  :class:`DecisionWriter`, and injects the root node-id down the hub->proxy path.

⛔ The write target is INJECTED (D14 seam) — no silent real-graph default; a
coordinator built without a :class:`DecisionWriter` cannot quietly write a live
corpus.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ..profile import _validate_instance_id
from .model import (
    DecisionAnswer,
    DsdDocument,
    answer_is_complete,
    enforce_single_select,
    fold_comment,
    parse_dsd,
    validate_dsd,
)
from .persist import DecisionWriter, DsdNode, mint_dsd_nodes, root_node_id


@dataclass
class PresentedDsd:
    """A DSD that PASSED the context guard and is ready to render+answer."""
    instance_id: str
    dsd_id: str
    doc: DsdDocument
    source_path: str = ""
    #: the DSD record's send-time document entries (name + sha256...);
    #: carried onto the answer nodes as ``documents-at-send``.
    documents: List[dict] = field(default_factory=list)
    #: pre-review B2: when the DSD was filed (epoch; 0 = unknown), for ``seconds-from-sent-to-answered``.
    sent_epoch: float = 0.0


@dataclass
class PresentResult:
    ok: bool
    message: str
    presented: Optional[PresentedDsd] = None
    n_decisions: int = 0


@dataclass
class CompleteResult:
    ok: bool
    message: str
    root_node_id: str = ""
    written_text: str = ""
    saved_path: str = ""
    mint_target: str = ""
    minted_node_ids: List[str] = field(default_factory=list)
    injected: bool = False
    inject_text: str = ""
    gate_failures: List[str] = field(default_factory=list)
    #: D15 amendment: the notice was durably written to the queue (before any
    #: delivery attempt). ``injected`` now means "handed to the live harness";
    #: only the harness's notice_ack (after its injector logged the write)
    #: removes the queued file.
    notice_queued: bool = False
    notice_path: str = ""


class DsdRoundTrip:
    """Coordinates the headless DSD round-trip against a live session registry.

    ``registry`` is the hub's :class:`~gnomon.hub.registry.SessionRegistry`
    (or any object with ``route(instance_id, command)`` and, optionally,
    ``is_registered``). ``decision_writer`` is the D14 write seam.
    """

    def __init__(self, registry, decision_writer: DecisionWriter,
                 *, decided_on: Optional[str] = None,
                 notice_queue=None, answered_by: Optional[str] = None,
                 answer_fields: Optional[Callable[[], bool]] = None) -> None:
        self._registry = registry
        self._writer = decision_writer
        self._decided_on = decided_on
        # D15 amendment (operator-approved 2026-09-29): the durable notice queue
        # (gnomon.dsd.notice.NoticeQueue). None = NOT wired: the notice is
        # still attempted live, but an unregistered agent gets NOTHING — the
        # result says so loudly (never "it will pull").
        self._notice_queue = notice_queue
        self._answered_by = answered_by
        # pre-review B2: a zero-arg flag reader (dsd_notice_inline). None / False = the nodes are minted exactly as before.
        self._answer_fields = answer_fields

    # -- present (the context guard) -----------------------------------------

    def present(self, instance_id: str, markdown: str, *, dsd_id: str,
                source_path: str = "", documents: Optional[List[dict]] = None) -> PresentResult:
        """Parse + context-guard a DSD. A DSD missing required context is REFUSED
        here — nothing downstream runs. Mirrors the hub present tool's gate."""
        _validate_instance_id(instance_id)
        doc = parse_dsd(markdown, path=source_path or None)
        verdict = validate_dsd(doc)
        if not verdict.ok:
            return PresentResult(ok=False, message=verdict.message,
                                 n_decisions=verdict.n_decisions)
        return PresentResult(
            ok=True, message=verdict.message, n_decisions=verdict.n_decisions,
            presented=PresentedDsd(instance_id=instance_id, dsd_id=dsd_id, doc=doc,
                                   source_path=source_path,
                                   documents=list(documents or [])))

    # -- answer (gates -> write-back -> persist -> inject) -------------------

    def answer(self, presented: PresentedDsd, answers: Dict[str, DecisionAnswer],
               *, save: bool = False, mark_answered: bool = True,
               submit_evidence: Optional[str] = None) -> CompleteResult:
        """Complete the round-trip for a presented DSD.

        1. FORM gates: single-select-v1 + escape-hatch over each answered decision,
           AND the pipeline-enforced completeness gate — every answerable (slotted)
           decision must be covered, so an OMITTED decision is refused here, not just
           disabled in the UI. Any failure REFUSES the whole save (no partial
           persist, no inject, no answered-flip).
        2. WRITE-BACK: render answers into the ``Your call:`` / ``Comments:`` slots
           (atomic save to ``source_path`` when ``save`` and a path exists).
        3. PERSIST: mint the root + per-decision ``DEC-DSD-*`` nodes via the
           injected writer.
        4. NOTICE (D15 amended 2026-09-29): enqueue the pointer notice durably,
           THEN route it down the logged inject path (source=dsd-answer).
        """
        doc = presented.doc
        by_id = {d.id: d for d in doc.decisions}

        # (1) gates — PER-ANSWER form gates (single-select v1 + escape-hatch/
        #     answer-is-complete), over the decisions the human actually answered.
        failures: List[str] = []
        for dec_id, ans in answers.items():
            dec = by_id.get(dec_id)
            if dec is None:
                failures.append(f"answer for unknown decision {dec_id!r}")
                continue
            ok_single, why_single = enforce_single_select(dec, ans)
            if not ok_single:
                failures.append(why_single)
            ok_complete, why_complete = answer_is_complete(ans)
            if not ok_complete:
                failures.append(f"{dec_id}: {why_complete}")

        # (1b) COMPLETENESS gate — PIPELINE-ENFORCED, not UI-only
        # (completeness-gate). Every parsed, ANSWERABLE
        #      decision must be covered by an answer. "Answerable" = has a writable
        #      ``Your call:`` slot; a slot-less (read-only) decision is structurally
        #      BLOCKED from being answered and is exempt. A decision simply OMITTED
        #      from ``answers`` is otherwise silently dropped by write-back + mint —
        #      and because D12 makes an answered DSD browse-only/non-editable, the
        #      omission becomes PERMANENTLY unanswerable with no error anywhere. This
        #      mirrors how the escape-hatch gate is enforced HERE (server-side), so
        #      the CTk Save-disabled state is a convenience, not the only guard.
        for dec in doc.decisions:
            if dec.slot_line is None:
                continue                       # read-only decision: cannot be answered
            if dec.id not in answers:
                failures.append(
                    f"{dec.id}: decision not answered — the completeness gate "
                    f"requires every decision to be answered before save")

        if failures:
            return CompleteResult(
                ok=False,
                message="REFUSED before save — form gate(s) failed:\n  - "
                        + "\n  - ".join(failures),
                gate_failures=failures)

        # (2) write-back (rendered here, written to disk only after the fidelity check below)
        written = doc.render_answers(answers, mark_answered=mark_answered)
        if save and not presented.source_path:
            return CompleteResult(
                ok=False,
                message="save requested but the presented DSD has no source_path")

        # (3) persist — mint nodes via the injected D14 writer. THIS is the success
        #     criterion: if the answer persisted (nodes minted + durable), the submit
        #     SUCCEEDED. A mint failure raises out of here — a genuine, loud refusal.
        fields_on = False
        if self._answer_fields is not None:
            try:
                fields_on = bool(self._answer_fields())
            except Exception:  # noqa: BLE001 - a flag-read fault means OFF: the nodes are today's
                fields_on = False
        import time as _time

        nodes: List[DsdNode] = mint_dsd_nodes(
            presented.dsd_id, presented.instance_id, doc.decisions, answers,
            source_path=presented.source_path, decided_on=self._decided_on,
            documents=presented.documents or None,
            **({"submit_evidence": submit_evidence} if submit_evidence else {}),
            **({"answer_fields": {"sent_epoch": presented.sent_epoch, "answered_epoch": _time.time()}} if fields_on else {}))
        root_id = root_node_id(presented.dsd_id)
        # (2b/3a) comment-fidelity: BEFORE anything is written, the record's Comments line and each
        #     node's comment must be exactly the shared fold of what was submitted. A difference (a cut at a line break,
        #     at N chars, anywhere) refuses the WHOLE save loudly: nothing minted, the record stays pending.
        fidelity = comment_fidelity_failures(written, nodes, answers, root_id)
        if fidelity:
            return CompleteResult(
                ok=False,
                message="REFUSED before save: the comment would not be stored exactly as typed "
                        "(nothing was written; your answers are kept as a draft):\n  - " + "\n  - ".join(fidelity),
                gate_failures=fidelity)
        saved_path = ""
        if save:
            _atomic_write(presented.source_path, written)
            saved_path = presented.source_path
        mint_target = self._writer.mint(nodes)
        persisted = bool(nodes)

        # (4) the D15-AMENDED notice (operator-approved 2026-09-29): a small actionable
        #     POINTER (dsd_id + root id + the lts_node call), never the answer text.
        #     ENQUEUE durably FIRST, then attempt live delivery down the hub->proxy
        #     path as a logged inject (source=dsd-answer, clear=False). Both are
        # DECOUPLED from persist: nothing here can turn a
        #     minted save into a refusal.
        from .live import DsdCorpusError
        from .notice import compose_notice, notice_command

        n_decisions = max(len(nodes) - 1, 0)
        notice_queued = False
        notice_path = ""
        queue_err = ""
        try:
            notice_text = compose_notice(presented.dsd_id, root_id, n_decisions=n_decisions,
                                         answered_by=self._answered_by)
        except (ValueError, DsdCorpusError) as exc:
            # g70: a refused dsd-corpus.json (DsdCorpusError) closes the NOTICE here, never the save handler.
            # FIX PASS 2: a dsd_id outside the strict notice grammar gets NO notice
            # (the harness would refuse it anyway) — said LOUDLY, never dropped silently.
            return CompleteResult(
                ok=persisted, root_node_id=root_id, written_text=written,
                saved_path=saved_path, mint_target=str(mint_target),
                minted_node_ids=[n.node_id for n in nodes],
                message=(f"Answer saved + queryable ({len(nodes)} node(s) minted, root {root_id}); "
                         f"NO notice sent or queued: {exc} — the agent must pull {root_id} "
                         f"from the graph"))
        notice = {"dsd_id": presented.dsd_id, "root_id": root_id, "text": notice_text}
        if self._notice_queue is not None:
            try:
                rec = self._notice_queue.enqueue(
                    presented.instance_id, presented.dsd_id, root_id=root_id,
                    text=notice_text, n_decisions=n_decisions, answered_by=self._answered_by)
                notice_queued = True
                notice_path = str(rec.get("path", ""))
            except Exception as exc:                   # a full disk must not refuse the save
                queue_err = f"{type(exc).__name__}: {exc}"

        injected = False
        inject_err = ""
        try:
            self._registry.route(presented.instance_id, notice_command(notice))
            injected = True
        except Exception as exc:                       # UnknownSession et al.
            inject_err = f"{type(exc).__name__}: {exc}"

        head = (f"Answer saved + queryable ({len(nodes)} node(s) minted, root {root_id})")
        if notice_queued and injected:
            msg = (f"{head}; notice queued durably and handed to {presented.instance_id}'s "
                   f"live harness (the queue file is removed when the harness acks)")
        elif notice_queued:
            msg = (f"{head}; agent {presented.instance_id} not live ({inject_err}) — notice "
                   f"QUEUED durably, delivered when its harness next registers")
        elif injected:
            msg = (f"{head}; notice handed to {presented.instance_id}'s live harness but NOT "
                   f"queued durably ({queue_err or 'no notice queue wired'}) — if that "
                   f"delivery is lost the agent must pull {root_id} from the graph")
        else:
            msg = (f"{head}; agent {presented.instance_id} not live ({inject_err}) and the "
                   f"notice was NOT queued ({queue_err or 'no notice queue wired'}) — NOTHING "
                   f"will be delivered; the agent must pull {root_id} from the graph")
        return CompleteResult(
            ok=persisted, message=msg, root_node_id=root_id, written_text=written,
            saved_path=saved_path, mint_target=str(mint_target),
            minted_node_ids=[n.node_id for n in nodes],
            injected=injected, inject_text=notice_text,
            notice_queued=notice_queued, notice_path=notice_path)

    # -- convenience: present + answer in one call ---------------------------

    def run(self, instance_id: str, markdown: str, answers: Dict[str, DecisionAnswer],
            *, dsd_id: str, source_path: str = "", save: bool = False) -> CompleteResult:
        pres = self.present(instance_id, markdown, dsd_id=dsd_id, source_path=source_path)
        if not pres.ok:
            return CompleteResult(ok=False, message=pres.message)
        return self.answer(pres.presented, answers, save=save)


def comment_fidelity_failures(written: str, nodes: List[DsdNode], answers: Dict[str, DecisionAnswer],
                              root_id: str) -> List[str]:
    """comment-fidelity (3): one failure line per answered decision whose written-back
    ``**Comments:**`` line (found by re-parsing ``written`` exactly as every record reader will) or whose answer node's
    ``comment`` property is not EXACTLY ``fold_comment(<submitted comment>)``. Compares whole strings, never a regex
    group, so trailing markdown-ish characters cannot hide a difference. Empty list = faithful."""
    doc = parse_dsd(written)
    lines = doc.lines
    by_id = {d.id: d for d in doc.decisions}
    node_by_id = {n.node_id: n for n in nodes}
    out: List[str] = []
    for dec_id, ans in answers.items():
        want = fold_comment(ans.comment or "")
        labels = [l for l in ans.labels if l and l.strip()]
        if not labels and not want:
            continue                                   # unanswered: nothing written, nothing minted
        node = node_by_id.get(f"{root_id}-{dec_id}")
        got_node = node.properties.get("comment") if node is not None else None
        if got_node != (want or "(none)"):
            out.append(f"{dec_id}: the answer node's comment holds {len(got_node or '')} chars, "
                       f"the submitted comment folds to {len(want)}")
        if not want:
            continue
        dec = by_id.get(dec_id)
        line = lines[dec.comment_line] if dec is not None and dec.comment_line is not None else None
        if line != f"**Comments:** {want}":
            out.append(f"{dec_id}: the record's Comments line holds "
                       f"{'no line' if line is None else str(len(line)) + ' chars'}, "
                       f"the submitted comment folds to {len(want)}")
    return out


def default_answers_from_preselect(doc: DsdDocument) -> Dict[str, DecisionAnswer]:
    """The HEADLESS answer stub (tests / scripted round-trips): EXPLICITLY accept
    each decision's recommendation (recommendation-line -> bold-row -> none). A
    decision with no recommendation is left UNANSWERED (no label), so it is not
    minted — the same shape a human who skipped it would produce.

    (Name kept for API continuity; the GUI form pre-selects NOTHING since the operator
    ruling of 2026-09-29 — this stub models a human who chose to agree.)"""
    out: Dict[str, DecisionAnswer] = {}
    for dec in doc.decisions:
        if dec.recommended_id is None:
            continue
        opt = dec.option_by_id(dec.recommended_id)
        if opt is not None:
            out[dec.id] = DecisionAnswer(labels=[opt.label], comment="")
    return out


def _atomic_write(path: str, text: str) -> None:
    """Write-temp-then-rename so a failure leaves the original untouched
 (save-behavior atomic)."""
    path = os.path.abspath(path)
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dsd-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

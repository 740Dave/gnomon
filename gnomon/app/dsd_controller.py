"""The DSD tab CONTROLLER — all DSD-tab logic, ZERO CustomTkinter dependency.

The DSD tab is split MVC-style exactly like the Agents tab (slice 2b): this
module holds every decision the tab makes and is unit-tested headlessly;
``dsd_tab.py`` is the only DSD module that imports ``customtkinter`` and only
builds widgets + forwards clicks.

It sits ON slice 3a (``gnomon.dsd.*`` — parse, context guard, form gates,
round-trip, persistence, supersession) and reads the on-disk DSD store the hub
writes via :class:`~gnomon.dsd.inbox.DsdInbox`. It builds a
:class:`~gnomon.dsd.roundtrip.DsdRoundTrip` over the hub's live registry
(so a saved answer injects the root node-id back to the requesting proxy, D15) and
the REAL index-refreshing writer (:class:`~gnomon.dsd.writer.
TypedLtsDecisionWriter`, carry-forward item 1).

The tab is DUAL-PANE (D12): the left pane lists DSDs (pending + prior); the right
pane PRESENTS a pending DSD as a form or BROWSES an answered one read-only. Every
render is GUARDED — a whitespace-only/empty DSD is REFUSED before render (carry-
forward item 2), never shown as an empty form.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from ..dsd import (
    DecisionAnswer,
    DsdDecision,
    NEITHER_NONE_LABEL,
    SupersessionStatus,
    answer_is_complete,
    parse_dsd,
)
from ..dsd import documents as dsd_documents
from ..dsd.inbox import DsdInbox, DsdInboxRecord
from ..dsd.inbox import STATUS_REPLACED, STATUS_SUPERSEDED, STATUS_UNFINISHED, STATUS_WITHDRAWN
from ..dsd.sections import answer_time, is_excluded_status
from ..dsd.persist import DsdMintRefused
from ..dsd.live import DsdCorpusError, LiveDaemonStatus, check_live_status, default_corpus_id
from ..dsd.roundtrip import DsdRoundTrip
from ..dsd.writer import WriterStatus, writer_status
from ..dsd.model import _YOURCALL_RE, _COMMENTS_RE, _strip_md


def _mark_view(rec: Any) -> Dict[str, Any]:
    """The reason / time / new id a withdraw or replace put on a record (pre-review B1)."""
    m = rec.raw.get(rec.status) if isinstance(getattr(rec, "raw", None), dict) else None
    m = m if isinstance(m, dict) else {}
    out: Dict[str, Any] = {"status": rec.status, "at_epoch": m.get("at_epoch")}
    if m.get("reason"):
        out["reason"] = str(m.get("reason"))
    if m.get("by_dsd_id"):
        out["replaced_by"] = str(m.get("by_dsd_id"))
    return out


# ── view models (what dsd_tab.py renders; no CTk here) ──────────────────────

@dataclass
class DsdListItem:
    """One left-pane row."""

    dsd_id: str
    instance_id: str
    kind: str                       # "pending" | "answered" | "unrenderable"
    title: str
    created_epoch: float = 0.0
    root_node_id: str = ""
    #: ANSWER time (None = unknown) + its provenance
    #: ("recorded" | "derived-marker" | "missing"); never the filing time.
    answered_epoch: Optional[float] = None
    answered_source: str = "missing"
    #: pre-review B1: only on a ``withdrawn`` / ``replaced`` row (kind says which): the reason, the time and, for a replace, the
    #: new id. ``None`` on every other row, so those rows are exactly what they were.
    mark: Optional[Dict[str, Any]] = None


@dataclass
class OptionChoice:
    option_id: str
    label: str
    tradeoffs: str
    recommended_row: bool = False
    #: ALWAYS False (operator, 2026-09-29: no pre-selected answer). Kept on the wire so
    #: a page still running the pre-change JS checks nothing either.
    is_preselected: bool = False
    #: The DSD's recommendation — drives the visible "recommended" badge only.
    is_recommended: bool = False
    is_escape_hatch: bool = False


@dataclass
class DecisionField:
    """One decision rendered as a radio group + comment."""

    id: str
    question: str
    what_this_is: str
    why_it_matters: str
    theme: str = ""
    blocker: bool = False           # the ⛔ gating-decision glyph was present
    options: List[OptionChoice] = field(default_factory=list)
    #: ALWAYS None (operator, 2026-09-29): the form pre-selects nothing; every radio
    #: starts unset and the completeness gate needs an explicit choice.
    preselect_id: Optional[str] = None
    recommended_id: Optional[str] = None
    recommendation_source: str = "none"  # "reco-line" | "bold-row" | "none"
    has_recommendation: bool = False
    reco_text: str = ""
    multi: bool = False             # parsed but GATED OFF in v1 (single-select)
    escape_hatch_id: str = ""
    escape_hatch_label: str = NEITHER_NONE_LABEL
    notes: List[str] = field(default_factory=list)
    #: this decision's documents (the view model from
    #: ``dsd.documents.view_documents``, scope == this decision id).
    documents: List[dict] = field(default_factory=list)


@dataclass
class DsdFormModel:
    """The whole pending DSD as a form. ``dsd_tab.py`` renders a radio group +
    comment box per decision with NO radio selected (operator, 2026-09-29), showing the
    recommended chip on ``recommended_id``; Save stays disabled until
    :meth:`is_complete`."""

    dsd_id: str
    instance_id: str
    decisions: List[DecisionField] = field(default_factory=list)

    def build_answers(
        self, selections: Dict[str, Tuple[Optional[str], str]]
    ) -> Dict[str, DecisionAnswer]:
        """Map ``dec_id -> (selected option_id, comment)`` to the round-trip's
        ``dec_id -> DecisionAnswer``. A decision with no selection is omitted
        (unanswered), matching the headless round-trip's shape."""
        out: Dict[str, DecisionAnswer] = {}
        by_id = {d.id: d for d in self.decisions}
        for dec_id, (option_id, comment) in selections.items():
            field_ = by_id.get(dec_id)
            if field_ is None or not option_id:
                continue
            label = _label_for_option(field_, option_id)
            if label is None:
                continue
            out[dec_id] = DecisionAnswer(labels=[label], comment=comment or "")
        return out

    def completeness(
        self, selections: Dict[str, Tuple[Optional[str], str]]
    ) -> Tuple[bool, List[str]]:
        """Every decision MUST be answered, and the escape hatch REQUIRES a comment
 (completeness-gate + escape-hatch). Returns
        ``(all_complete, [reason, ...])``."""
        reasons: List[str] = []
        answers = self.build_answers(selections)
        for dec in self.decisions:
            ans = answers.get(dec.id)
            if ans is None:
                reasons.append(f"{dec.id}: no option selected")
                continue
            ok, why = answer_is_complete(ans)
            if not ok:
                reasons.append(f"{dec.id}: {why}")
        return (not reasons), reasons

    def is_complete(self, selections: Dict[str, Tuple[Optional[str], str]]) -> bool:
        return self.completeness(selections)[0]


@dataclass
class PresentView:
    """The guarded result of opening a pending DSD for presentation."""

    ok: bool
    message: str
    dsd_id: str = ""
    instance_id: str = ""
    form: Optional[DsdFormModel] = None
    n_decisions: int = 0
    #: DSD-level documents (scope "top"); per-decision ones ride on the form.
    documents: List[dict] = field(default_factory=list)
    #: sent time (the record's created_epoch; 0.0 = unknown) - the API turns it into
    #: created_epoch/created_iso via dsd.senttime.sent_fields.
    created_epoch: float = 0.0


@dataclass
class RecordedAnswer:
    decision_id: str
    question: str
    your_call: str = ""
    comment: str = ""


@dataclass
class BrowseView:
    """The read-only browse of an answered DSD (D12: responses non-editable)."""

    ok: bool
    message: str
    dsd_id: str = ""
    instance_id: str = ""
    root_node_id: str = ""
    markdown: str = ""
    answers: List[RecordedAnswer] = field(default_factory=list)
    supersession: Optional[SupersessionStatus] = None
    #: every listed document (all scopes) with send-time + current facts.
    documents: List[dict] = field(default_factory=list)
    created_epoch: float = 0.0
    #: answer time + provenance, from the same answer_time() the list uses.
    answered_epoch: Optional[float] = None
    answered_source: str = "missing"


@dataclass
class SubmitResult:
    ok: bool
    message: str
    dsd_id: str = ""
    root_node_id: str = ""
    minted_node_ids: List[str] = field(default_factory=list)
    injected: bool = False
    gate_failures: List[str] = field(default_factory=list)
    writer: Optional[WriterStatus] = None
    live: Optional[LiveDaemonStatus] = None
    #: D15 amendment: the answer notice was durably queued for the agent.
    notice_queued: bool = False
    instance_id: str = ""


# ── the controller ──────────────────────────────────────────────────────────

class DsdTabController:
    """Headless DSD-tab logic over the inbox + round-trip + supersession resolver.

    ``inbox`` reads/updates the on-disk DSD store; ``roundtrip`` runs the slice-3a
    present -> gate -> write-back -> persist -> inject loop; ``resolver`` answers
    "is this answer superseded?" for the browse pane (bounded text-search fallback
 today); ``writer`` is the injected D14 writer,
    kept so the UI can read its refresh/registration status."""

    def __init__(
        self,
        inbox: DsdInbox,
        roundtrip: DsdRoundTrip,
        *,
        resolver=None,
        writer: object = None,
        live_check=None,
        doc_roots=None,
    ) -> None:
        # the allow-listed document roots (None = the operator's list, resolved at
        # call time). Injectable so tests never read the real home.
        self._doc_roots = doc_roots
        self._inbox = inbox
        self._rt = roundtrip
        self._resolver = resolver
        self._writer = writer
        # ⛔ D1 — the LIVE lts-engine check (injectable for tests; the default is the
        # real in-process engine store check). Replaces the hardcoded
        # ``registration_held=True`` guess: status must reflect reality NOW.
        self._live_check = live_check if live_check is not None else check_live_status

    # -- left pane: list --------------------------------------------------------

    def list_dsds(self, include_marked: bool = False) -> List[DsdListItem]:
        """Both panes' source: pending DSDs to present + prior DSDs to browse,
        newest first. ``include_marked`` (pre-review B1, the web layer passes ``dsd_self_verbs``): a record an agent
        WITHDREW or REPLACED is listed as such (never silently gone); otherwise it stays hidden, as before."""
        items: List[DsdListItem] = []
        for rec in self._inbox.list_records():
            if is_excluded_status(rec.status):   # withdrawn/superseded hidden
                if include_marked and rec.status in (STATUS_WITHDRAWN, STATUS_REPLACED):
                    items.append(DsdListItem(
                        dsd_id=rec.dsd_id, instance_id=rec.instance_id, kind=rec.status, title=_summary_title(rec),
                        created_epoch=rec.created_epoch, mark=_mark_view(rec)))
                continue
            if rec.answered:
                kind = "answered"
            elif rec.renderable:
                kind = "pending"
            else:
                kind = "unrenderable"
            a_ep, a_src = (answer_time(rec.answered_epoch, rec.markdown, rec.answer_time_source)
                           if rec.answered else (None, "missing"))
            items.append(DsdListItem(
                dsd_id=rec.dsd_id, instance_id=rec.instance_id, kind=kind,
                title=_summary_title(rec), created_epoch=rec.created_epoch,
                root_node_id=rec.root_node_id,
                answered_epoch=a_ep, answered_source=a_src))
        return items

    # -- DSD review presence (2026-09-29) ---------------------------------------

    def review_owner(self, dsd_id: str) -> Tuple[str, Optional[str]]:
        """``("missing", None)`` | ``("pending", owner)`` | ``("answered", owner)`` —
        the status + sending instance for the DSD-review presence beacon, via a
        DIRECT record lookup (no full scan). Only ``pending`` is "being reviewed"."""
        rec = self._inbox.read_direct(dsd_id)
        if rec is None:
            return "missing", None
        if rec.status in (STATUS_WITHDRAWN, STATUS_REPLACED, STATUS_SUPERSEDED, STATUS_UNFINISHED):    # D-F6: a marked DSD is neither pending nor answered
            return rec.status, rec.instance_id
        return ("answered" if rec.answered else "pending"), rec.instance_id

    # -- right pane: PRESENT a pending DSD (guarded) ---------------------------

    def present(self, dsd_id: str, *, with_documents: bool = True) -> PresentView:
        """Open a pending DSD for presentation. GUARDED (carry-forward item 2):
        a missing/whitespace-only/empty DSD is REFUSED before any form is built,
        as is a DSD failing the context guard (missing What-this-is/Why-it-matters).
        """
        rec = self._inbox.read(dsd_id)
        if rec is None:
            return PresentView(ok=False, message=f"no DSD record for {dsd_id!r}",
                               dsd_id=dsd_id)
        if rec.status in (STATUS_WITHDRAWN, STATUS_REPLACED, STATUS_SUPERSEDED, STATUS_UNFINISHED):      # pre-review B1: a marked DSD is never offered for an answer
            return PresentView(ok=False, dsd_id=dsd_id, instance_id=rec.instance_id,
                               message=f"this DSD was {rec.status} by {rec.instance_id}; it is not waiting for an answer"
                                       + (f" ({_mark_view(rec).get('reason')})" if _mark_view(rec).get("reason") else ""))
        # ⛔ guard-before-render: an empty / whitespace-only DSD never reaches a form.
        if not rec.has_markdown or not rec.markdown.strip():
            return PresentView(
                ok=False, dsd_id=dsd_id, instance_id=rec.instance_id,
                message=("REFUSED: this DSD carries no renderable markdown "
                         "(empty or whitespace-only) — the context guard refuses "
                         "to present an empty DSD (context-guard)."))
        # the slice-3a context guard (parse + What-this-is/Why-it-matters present)
        pres = self._rt.present(rec.instance_id, rec.markdown, dsd_id=rec.dsd_id)
        if not pres.ok:
            return PresentView(ok=False, message=pres.message, dsd_id=dsd_id,
                               instance_id=rec.instance_id,
                               n_decisions=pres.n_decisions)
        form = _build_form(rec.dsd_id, rec.instance_id, pres.presented.doc.decisions)
        # documents (DSD level + per decision) with send-time + current sha.
        # (submit() re-presents only to gate/answer, so it skips the per-document hashing.)
        docs = self._documents_view(rec) if with_documents else []
        for f in form.decisions:
            f.documents = [d for d in docs if d["scope"] == f.id]
        return PresentView(ok=True, message=pres.message, dsd_id=rec.dsd_id,
                           instance_id=rec.instance_id, form=form,
                           n_decisions=pres.n_decisions,
                           documents=[d for d in docs if d["scope"] == "top"],
                           created_epoch=rec.created_epoch)

    # -- right pane: SUBMIT (gate -> write-back -> persist -> inject) -----------

    def submit(
        self, dsd_id: str, selections: Dict[str, Tuple[Optional[str], str]],
        *, submit_evidence: Optional[str] = None,
    ) -> SubmitResult:
        """Save the human's answers for a pending DSD. Re-presents from the record
        (stateless), runs the round-trip (form gates + write-back + persist via the
        real refreshing writer + inject of the root id, D15), then flips the record
        to answered with a POINTER to the minted root (D13)."""
        view = self.present(dsd_id, with_documents=False)
        if not view.ok or view.form is None:
            return SubmitResult(ok=False, message=view.message, dsd_id=dsd_id)

        answers = view.form.build_answers(selections)
        if not answers:
            return SubmitResult(ok=False, dsd_id=dsd_id,
                                message="no decisions answered — nothing to save")

        rec = self._inbox.read(dsd_id)
        # the send-time document entries ride into the answer nodes
        # (kwarg only when there are some, so a roundtrip double is unaffected).
        sent_docs = dsd_documents.entries_for_record(rec.raw, rec.markdown)
        if sent_docs:
            pres = self._rt.present(rec.instance_id, rec.markdown, dsd_id=rec.dsd_id,
                                    documents=sent_docs)
        else:
            pres = self._rt.present(rec.instance_id, rec.markdown, dsd_id=rec.dsd_id)
        if getattr(pres, "presented", None) is not None:      # pre-review B2: the filing time, for the answer node's fields
            # A record with no usable filing time must not break the submit: sent_epoch stays 0.0, which the node's
            # seconds-from-sent-to-answered field writes as "unknown" (never a made-up duration).
            ce = getattr(rec, "created_epoch", None)
            if isinstance(ce, (int, float)) and not isinstance(ce, bool) and ce == ce and ce > 0:
                pres.presented.sent_epoch = float(ce)
        # write-back is stored into the JSON record (save=False: no stray .md file)
        ws = writer_status(self._writer) if self._writer is not None else None
        try:
            out = self._rt.answer(pres.presented, answers, save=False,
                                  **({"submit_evidence": submit_evidence} if submit_evidence else {}))
        except DsdCorpusError as e:
            # g70: a refused dsd-corpus.json closes the SAVE (nothing minted, the record stays pending), not the handler
            return SubmitResult(ok=False, message=f"REFUSED: {e}", dsd_id=dsd_id, writer=ws)
        except DsdMintRefused as e:
            # X20b: the typed gate refused the mint (nothing written). Show the operator
            # the reason instead of letting it become a 500.
            return SubmitResult(ok=False, message=str(e), dsd_id=dsd_id, writer=ws)

        if out.gate_failures:
            return SubmitResult(ok=False, message=out.message, dsd_id=dsd_id,
                                gate_failures=list(out.gate_failures), writer=ws)

        # persist landed (minted) even if inject failed soft; record the pointer.
        # ⛔ DEFECT FIX: out.ok now reflects PERSIST, not inject, so
        # an answer for an agent not launched by Gnomon flips to answered and
        # reports success — never a false "Save refused" when the nodes were minted.
        if out.minted_node_ids:
            self._inbox.mark_answered(
                dsd_id, out.root_node_id,
                answer_summary=_answer_summary(view.form, answers),
                written_markdown=out.written_text,
                # comment-fidelity (1): the comment EXACTLY as submitted, line breaks and all.
                answer_comments={k: a.comment for k, a in answers.items() if (a.comment or "").strip()})

        # ⛔ D1 — verify the just-minted root node is actually LIVE-visible through the
        # engine (a genuine post-write check, not a build-time guess). Best-effort:
        # a live-check hiccup must not fail a save that persisted.
        live = None
        if out.minted_node_ids:
            live = self._run_live_check(probe_node_id=out.root_node_id)
            if ws is not None:
                ws.live = live

        return SubmitResult(
            ok=out.ok, message=out.message, dsd_id=dsd_id,
            root_node_id=out.root_node_id, minted_node_ids=list(out.minted_node_ids),
            injected=out.injected, writer=ws, live=live,
            notice_queued=bool(getattr(out, "notice_queued", False)),
            instance_id=rec.instance_id)

    # -- right pane: BROWSE an answered DSD (read-only) -------------------------

    def browse(self, dsd_id: str) -> BrowseView:
        """Render an answered DSD READ-ONLY (D12): the written-back markdown, the
        recorded answers parsed from it, and the live supersession status (bounded
        text-search today; the caveats travel on the status)."""
        rec = self._inbox.read(dsd_id)
        if rec is None:
            return BrowseView(ok=False, message=f"no DSD record for {dsd_id!r}",
                              dsd_id=dsd_id)
        if rec.status in (STATUS_WITHDRAWN, STATUS_REPLACED, STATUS_SUPERSEDED, STATUS_UNFINISHED):     # D-F6: not an answered DSD
            return BrowseView(ok=False, dsd_id=rec.dsd_id, instance_id=rec.instance_id,
                              message=f"this DSD was {rec.status} by {rec.instance_id}; it was never answered")
        supersession = None
        if self._resolver is not None and rec.root_node_id:
            try:
                supersession = self._resolver.resolve(rec.root_node_id)
            except Exception as exc:  # noqa: BLE001 — a resolver hiccup must not crash browse
                supersession = SupersessionStatus(
                    node_id=rec.root_node_id, superseded=False,
                    mechanism="unavailable", confidence="bounded",
                    note=f"supersession lookup failed: {type(exc).__name__}: {exc}")
        answers = _recorded_answers(rec.markdown, exact=rec.raw.get("answer_comments")) if rec.markdown else []
        a_ep, a_src = answer_time(rec.answered_epoch, rec.markdown, rec.answer_time_source)
        return BrowseView(
            ok=True, message="answered (read-only)", dsd_id=rec.dsd_id,
            instance_id=rec.instance_id, root_node_id=rec.root_node_id,
            markdown=rec.markdown, answers=answers, supersession=supersession,
            documents=self._documents_view(rec),
            created_epoch=rec.created_epoch,
            answered_epoch=a_ep, answered_source=a_src)

    # -- documents --------------------------------------------------

    def _documents_view(self, rec: DsdInboxRecord) -> List[dict]:
        try:
            return dsd_documents.view_documents(rec.raw, rec.markdown, self._doc_roots)
        except Exception as exc:  # noqa: BLE001 - never break the DSD view, but NEVER hide it either
            return [{"scope": "top", "index": 0, "name": "(documents unavailable)", "path": "",
                     "sent_status": "error", "sent_sha256": "", "sent_size": None,
                     "sent_mtime": None, "openable": False, "refusal": "documents-view-failed",
                     "refusal_reason": f"{type(exc).__name__}: {exc}",
                     "warning": f"DOCUMENTS VIEW FAILED: {type(exc).__name__}: {exc}",
                     "current_sha256": "", "changed": False, "missing_now": False}]

    def open_document(self, dsd_id: str, scope, index, *, opener) -> dict:
        """The operator Open action: ``(dsd_id, scope, index)`` -> opens a document THE
        RECORD LISTS (never a raw path), through the injected ``opener``. Raises
        ``dsd.documents.DocumentRefused`` (a named refusal) on any refusal."""
        rec = self._inbox.read_direct(dsd_id) if isinstance(dsd_id, str) else None
        return dsd_documents.open_document(
            None if rec is None else rec.raw,
            "" if rec is None else rec.markdown, scope, index,
            opener=opener, roots=self._doc_roots)

    # -- UI status --------------------------------------------------------------

    def writer_status(self) -> Optional[WriterStatus]:
        """The writer target + the LIVE lts-engine verdict for the banner (D1/D2).
        The live status replaces the old ``registration_held`` guess; when it is not
        live the banner complains LOUDLY (D2: no silent degrade)."""
        if self._writer is None:
            return None
        ws = writer_status(self._writer)
        ws.live = self._run_live_check()
        return ws

    def live_status(self, *, probe_node_id: Optional[str] = None) -> LiveDaemonStatus:
        """The raw live lts-engine verdict for the DSD-answer corpus (D1)."""
        return self._run_live_check(probe_node_id=probe_node_id)

    def _run_live_check(self, *, probe_node_id: Optional[str] = None) -> LiveDaemonStatus:
        try:
            corpus_id = getattr(self._writer, "corpus_id", None) or default_corpus_id()
        except DsdCorpusError as exc:      # g68 (S5-4b): report the refusal (not live, file named); never raise into a handler
            return LiveDaemonStatus(corpus_id="", engine_available=False, probe_node_id=probe_node_id or "",
                                    error=f"DsdCorpusError: {exc}")
        return self._live_check(corpus_id, probe_node_id=probe_node_id)


# ── auto-switch: detect a NEWLY-ARRIVED pending DSD ──────────────────────────

class DsdArrivalWatcher:
    """Headless detector for the operator's auto-switch feature: report pending DSDs that
    have ARRIVED since the last poll, so the GUI can automatically navigate to the
    DSD tab and select the new DSD.

    ⭐ ONE mechanism covers "a fresh pending-human-review record on disk AND/OR the
    hub's ``send_dsd_for_human_review``": both sinks write the same JSON record under
    ``<agents-root>/<id>/dsd/`` (``DSDStore.persist``), so polling the inbox's
    pending set catches either path.

    The FIRST :meth:`poll` PRIMES — it records whatever is already pending at startup
    and returns ``[]`` — so DSDs sitting on disk when the app opens do NOT trigger an
    auto-switch (only a genuine ARRIVAL while running does). Detection is fully
    headless + unit-tested; the tab-switch + select is the CTk-only wiring on top."""

    def __init__(self, controller: "DsdTabController") -> None:
        self._controller = controller
        self._seen: Optional[set] = None   # None until the priming poll

    def poll(self) -> List[DsdListItem]:
        """Return the pending DSDs that arrived since the previous poll (newest
        first). Primes on the first call (returns ``[]``)."""
        pending = {it.dsd_id: it for it in self._controller.list_dsds()
                   if it.kind == "pending"}
        if self._seen is None:
            self._seen = set(pending)
            return []
        new_items = [pending[d] for d in pending if d not in self._seen]
        self._seen = set(pending)
        new_items.sort(key=lambda it: it.created_epoch, reverse=True)
        return new_items


# ── builders / helpers ───────────────────────────────────────────────────────

def _build_form(dsd_id: str, instance_id: str,
                decisions: List[DsdDecision]) -> DsdFormModel:
    fields: List[DecisionField] = []
    for dec in decisions:
        options: List[OptionChoice] = []
        for opt in dec.options:
            options.append(OptionChoice(
                option_id=opt.option_id, label=opt.label, tradeoffs=opt.tradeoffs,
                recommended_row=opt.recommended_row,
                is_preselected=False,
                is_recommended=(opt.option_id == dec.recommended_id)))
        # the always-present escape hatch (escape-hatch)
        escape_id = f"{dec.id}-neither"
        options.append(OptionChoice(
            option_id=escape_id, label=NEITHER_NONE_LABEL, tradeoffs="",
            is_escape_hatch=True))
        fields.append(DecisionField(
            id=dec.id, question=dec.question, what_this_is=dec.what_this_is,
            why_it_matters=dec.why_it_matters, theme=dec.theme,
            blocker=bool(dec.marker), options=options,
            preselect_id=None, recommended_id=dec.recommended_id,
            recommendation_source=dec.recommendation_source,
            has_recommendation=dec.recommended_id is not None,
            reco_text=dec.reco_text, multi=dec.multi,
            escape_hatch_id=escape_id, notes=list(dec.notes)))
    return DsdFormModel(dsd_id=dsd_id, instance_id=instance_id, decisions=fields)


def _label_for_option(field_: DecisionField, option_id: str) -> Optional[str]:
    for opt in field_.options:
        if opt.option_id == option_id:
            return opt.label
    return None


def _summary_title(rec: DsdInboxRecord) -> str:
    if rec.markdown.strip():
        doc = parse_dsd(rec.markdown)
        if doc.decisions:
            first = doc.decisions[0]
            n = len(doc.decisions)
            head = first.question or first.id
            suffix = f"  (+{n - 1} more)" if n > 1 else ""
            return f"{head}{suffix}"
    return f"{rec.dsd_id} (non-markdown payload)"


def _answer_summary(form: DsdFormModel, answers: Dict[str, DecisionAnswer]) -> str:
    """A short human gloss for the record — NOT the full answer text (D15: the full
    answer never travels; the pointer does)."""
    parts = []
    for dec in form.decisions:
        ans = answers.get(dec.id)
        if ans and ans.labels:
            parts.append(f"{dec.id}={ans.labels[0]}")
    return "; ".join(parts)


def _recorded_answers(markdown: str, exact: Optional[Dict[str, str]] = None) -> List[RecordedAnswer]:
    """Parse the written-back ``Your call:`` / ``Comments:`` slots for the read-only
 browse view, keyed to each decision. ``exact`` (the record's ``answer_comments``
    comment-fidelity) is the comment exactly as submitted and wins over the parsed line when present."""
    doc = parse_dsd(markdown)
    lines = doc.lines
    out: List[RecordedAnswer] = []
    for dec in doc.decisions:
        your_call = ""
        comment = ""
        if dec.slot_line is not None and 0 <= dec.slot_line < len(lines):
            m = _YOURCALL_RE.match(lines[dec.slot_line])
            if m:
                your_call = _strip_md(m.group(1))
        if dec.comment_line is not None and 0 <= dec.comment_line < len(lines):
            mc = _COMMENTS_RE.match(lines[dec.comment_line])
            if mc:
                comment = _strip_md(mc.group(1))
        if isinstance(exact, dict) and isinstance(exact.get(dec.id), str):
            comment = exact[dec.id]
        out.append(RecordedAnswer(decision_id=dec.id, question=dec.question,
                                  your_call=your_call, comment=comment))
    return out

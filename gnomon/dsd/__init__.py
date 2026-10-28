"""Gnomon DSD headless core (slice 3a).

The canvas-free DSD model + persistence + present/collect round-trip — everything
the DSD tab (slice 3b, the CTk GUI) sits ON TOP of. Clean-room reimplementation of
earlier tooling's model, reconciled by node id against the
Gnomon contract; nothing here imports
earlier tooling.

Public surface
--------------
Model        : ``DsdOption``, ``DsdDecision``, ``DecisionAnswer``, ``DsdDocument``,
               ``parse_dsd``, ``load_dsd``
Context guard: ``validate_dsd``, ``validate_dsd_file``, ``DsdValidation``
Form gates   : ``answer_is_complete``, ``enforce_single_select``,
               ``NEITHER_NONE_LABEL``
Persist      : ``DecisionWriter``, ``FakeDecisionWriter``, ``LtsFileDecisionWriter``,
               ``DsdNode``, ``mint_dsd_nodes``, ``root_node_id``
Supersession : ``SupersessionResolver``, ``TextSearchSupersessionResolver``,
               ``ReverseLookupSupersessionResolver``, ``make_supersession_resolver``,
               ``SupersessionStatus``
Round-trip   : ``DsdRoundTrip``, ``PresentedDsd``, ``PresentResult``,
               ``CompleteResult``, ``default_answers_from_preselect``
"""

from __future__ import annotations

from .model import (
    NEITHER_NONE_LABEL,
    DecisionAnswer,
    DsdDecision,
    DsdDocument,
    DsdOption,
    DsdValidation,
    answer_is_complete,
    enforce_single_select,
    load_dsd,
    parse_dsd,
    validate_dsd,
    validate_dsd_file,
)
from .persist import (
    DecisionWriter,
    DsdNode,
    FakeDecisionWriter,
    LtsFileDecisionWriter,
    MintResult,
    mint_dsd_nodes,
    root_node_id,
)
from .resolve import (
    ReverseLookupSupersessionResolver,
    SupersessionResolver,
    SupersessionStatus,
    TextSearchSupersessionResolver,
    make_supersession_resolver,
)
from .roundtrip import (
    CompleteResult,
    DsdRoundTrip,
    PresentedDsd,
    PresentResult,
    default_answers_from_preselect,
)
from .inbox import DsdInbox, DsdInboxRecord
from .live import (
    DsdCorpusError,
    LiveDaemonStatus,
    check_live_status,
    default_corpus_id,
    dsd_settings,
)
from .writer import (
    RefreshRecordingWriter,
    TypedLtsDecisionWriter,
    WriterStatus,
    factlattice_available,
    make_decision_writer,
    writer_status,
)

__all__ = [
    "NEITHER_NONE_LABEL",
    "DecisionAnswer",
    "DsdDecision",
    "DsdDocument",
    "DsdOption",
    "DsdValidation",
    "answer_is_complete",
    "enforce_single_select",
    "load_dsd",
    "parse_dsd",
    "validate_dsd",
    "validate_dsd_file",
    "DecisionWriter",
    "DsdNode",
    "FakeDecisionWriter",
    "LtsFileDecisionWriter",
    "MintResult",
    "mint_dsd_nodes",
    "root_node_id",
    "ReverseLookupSupersessionResolver",
    "SupersessionResolver",
    "SupersessionStatus",
    "TextSearchSupersessionResolver",
    "make_supersession_resolver",
    "CompleteResult",
    "DsdRoundTrip",
    "PresentedDsd",
    "PresentResult",
    "default_answers_from_preselect",
    "DsdInbox",
    "DsdInboxRecord",
    "default_corpus_id",
    "dsd_settings",
    "DsdCorpusError",
    "LiveDaemonStatus",
    "check_live_status",
    "RefreshRecordingWriter",
    "TypedLtsDecisionWriter",
    "WriterStatus",
    "factlattice_available",
    "make_decision_writer",
    "writer_status",
]

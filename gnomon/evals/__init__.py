"""Gnomon behavioural evals — Pydantic Evals over headless ``agent_launch``.

* :func:`task` launches one headless agent in an isolated tmp runtime root;
* :mod:`.evaluators` score the evidence (provenance log, operator launch ledger,
  D40 transcript, files written) — see :mod:`.checks` for the primitives;
* :mod:`.cases` is the registry; :mod:`.runner` the CLI
  (``python -m gnomon.evals run [--case N] [--dry-run]``).

Live runs require ``GNOMON_EVALS_LIVE=1``. Needs ``pydantic-evals`` (the ``evals`` extra).
"""

from .case import CaseInputs, EvalCase, validate_case, validate_cases
from .evaluators import (BudgetGuard, FileMatches, ModelUsed, NoRecord, ProvenanceHas, RunSucceeded, ToolCalled,
                         TranscriptPresent)
from .evidence import RunEvidence
from .task import EvalLaunchRefused, TaskOptions, make_task, task

__all__ = [
    "BudgetGuard", "CaseInputs", "EvalCase", "EvalLaunchRefused", "FileMatches", "ModelUsed", "NoRecord",
    "ProvenanceHas", "RunEvidence", "RunSucceeded", "TaskOptions", "ToolCalled", "TranscriptPresent",
    "make_task", "task", "validate_case", "validate_cases",
]

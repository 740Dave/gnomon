"""Case definitions + the dry-run validator.

An :class:`EvalCase` is a named prompt for a headless agent plus the evaluators
that score the evidence it leaves. :func:`validate_case` checks a definition
WITHOUT launching anything — that is what ``run --dry-run`` executes, and a live
run refuses any case that fails it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Sequence

from pydantic_evals import Case
from pydantic_evals.evaluators import Evaluator

from .evaluators import BudgetGuard, RunSucceeded, TranscriptPresent, GmEvaluator

#: The case name becomes part of the child's instance id (and so a directory
#: name) — keep it a strict slug.
CASE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_TIMEOUT_S = 1800.0
#: The cheapest model the launcher path can select (passed to the headless
#: claude as ``ANTHROPIC_MODEL`` — see :mod:`.task`).
DEFAULT_MODEL = "haiku"


@dataclass
class CaseInputs:
    """What the task receives (pydantic-evals passes only ``inputs``)."""

    case_name: str
    prompt: str
    model: str = DEFAULT_MODEL
    timeout_s: float = 180.0


@dataclass
class EvalCase:
    name: str
    prompt: str
    evaluators: Sequence[Evaluator] = field(default_factory=tuple)
    model: str = DEFAULT_MODEL
    timeout_s: float = 180.0
    description: str = ""

    def inputs(self) -> CaseInputs:
        return CaseInputs(case_name=self.name, prompt=self.prompt, model=self.model, timeout_s=self.timeout_s)

    def to_pydantic_case(self) -> Case:
        return Case(name=self.name, inputs=self.inputs(), metadata={"description": self.description},
                    evaluators=tuple(self.evaluators))


def validate_case(case: Any) -> List[str]:
    """Errors in one case definition (empty = valid). Launches nothing."""
    if not isinstance(case, EvalCase):
        return [f"not an EvalCase: {type(case).__name__}"]
    errs: List[str] = []
    if not isinstance(case.name, str) or not CASE_NAME_RE.match(case.name):
        errs.append(f"name {case.name!r} must match {CASE_NAME_RE.pattern}")
    if not isinstance(case.prompt, str) or not case.prompt.strip():
        errs.append("prompt must be a non-empty string")
    if not isinstance(case.model, str) or not case.model.strip() or case.model.startswith("-"):
        errs.append("model must be a non-empty model name/alias")
    if (not isinstance(case.timeout_s, (int, float)) or isinstance(case.timeout_s, bool)
            or not 0 < case.timeout_s <= MAX_TIMEOUT_S):
        errs.append(f"timeout_s must be in (0, {MAX_TIMEOUT_S:.0f}]")
    evs = list(case.evaluators or ())
    if not evs:
        errs.append("no evaluators — a case with nothing to check cannot pass or fail")
    # rm2a2 F2: every case must prove the run happened and left its transcript —
    # without these, a case of only negative checks could pass over a run that never ran.
    for required in (RunSucceeded, TranscriptPresent):
        if not any(isinstance(e, required) for e in evs):
            errs.append(f"no {required.__name__} evaluator — every case must carry RunSucceeded and "
                        "TranscriptPresent")
    guards = [e for e in evs if isinstance(e, BudgetGuard)]
    if not any(g.has_limit() for g in guards):
        errs.append("no BudgetGuard with a limit — every live case must bound its turns/cost")
    for i, ev in enumerate(evs):
        if not isinstance(ev, Evaluator):
            errs.append(f"evaluator[{i}] is not a pydantic_evals Evaluator: {type(ev).__name__}")
            continue
        if isinstance(ev, GmEvaluator):
            errs.extend(f"evaluator[{i}] {ev.label()}: {e}" for e in ev.validate())
    return errs


def validate_cases(cases: Sequence[Any]) -> Dict[str, List[str]]:
    """``{case_name: errors}`` for every case (duplicates flagged on each)."""
    out: Dict[str, List[str]] = {}
    seen: Dict[str, int] = {}
    for c in cases:
        name = getattr(c, "name", repr(c))
        seen[name] = seen.get(name, 0) + 1
    for i, c in enumerate(cases):
        name = getattr(c, "name", f"<case {i}>")
        errs = validate_case(c)
        if seen.get(name, 0) > 1:
            errs.append(f"duplicate case name {name!r}")
        key = name if name not in out else f"{name}#{i}"
        out[key] = errs
    return out

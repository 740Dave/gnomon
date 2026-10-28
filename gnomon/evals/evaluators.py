"""Pydantic Evals ``Evaluator`` wrappers over :mod:`.checks`.

Pydantic Evals' own agentic/span evaluators do NOT fit here: they only see tools
that pydantic-ai runs in-process (local OTel spans), and Claude Code's tools run
out of process (``pydantic-ai-vs-pty-harness-2026-09-28.md`` row 1). So every Gnomon
evaluator reads the on-disk evidence (:class:`RunEvidence`) instead.

Every evaluator here has ``validate() -> list[str]`` for the dry-run, and a
descriptive report name so two instances of one class never collapse into an
opaque ``Name_2`` column.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext

from . import checks
from .evidence import RunEvidence


def _reason(res: checks.CheckResult) -> EvaluationReason:
    return EvaluationReason(value=bool(res.passed), reason=res.reason)


def _evidence(ctx: EvaluatorContext) -> Optional[RunEvidence]:
    out = ctx.output
    return out if isinstance(out, RunEvidence) else None


_NOT_EVIDENCE = EvaluationReason(value=False, reason="task output is not RunEvidence — nothing to evaluate")


class GmEvaluator(Evaluator):
    """Base: subclasses implement ``check(ev)`` and ``validate()``."""

    def check(self, ev: RunEvidence) -> checks.CheckResult:  # pragma: no cover - abstract
        raise NotImplementedError

    def validate(self) -> List[str]:
        return []

    def label(self) -> str:
        return type(self).__name__

    def get_default_evaluation_name(self) -> str:
        return self.label()

    def evaluate(self, ctx: EvaluatorContext) -> EvaluationReason:
        ev = _evidence(ctx)
        if ev is None:
            return _NOT_EVIDENCE
        return _reason(self.check(ev))


def _pattern_label(match: Dict[str, Any]) -> str:
    parts = []
    for k, v in match.items():
        parts.append(f"{k}={v}" if not isinstance(v, dict) else f"{k}~")
    s = ",".join(parts)
    return s if len(s) <= 60 else s[:57] + "..."


@dataclass(repr=False)
class ProvenanceHas(GmEvaluator):
    """``source`` (default the session ``injection_log``) holds a matching record."""

    match: Dict[str, Any]
    source: str = "injection_log"
    min_count: int = 1

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_record_present(ev, self.match, self.source, self.min_count)

    def validate(self) -> List[str]:
        errs = checks.validate_source(self.source) + checks.validate_pattern(self.match)
        if not isinstance(self.min_count, int) or self.min_count < 1:
            errs.append("min_count must be an int >= 1")
        return errs

    def label(self) -> str:
        return f"has[{self.source}]{{{_pattern_label(self.match)}}}"


@dataclass(repr=False)
class NoRecord(GmEvaluator):
    """The NEGATIVE case: no record in ``source`` matches ``match``."""

    match: Dict[str, Any]
    source: str = "injection_log"

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_record_absent(ev, self.match, self.source)

    def validate(self) -> List[str]:
        return checks.validate_source(self.source) + checks.validate_pattern(self.match)

    def label(self) -> str:
        return f"none[{self.source}]{{{_pattern_label(self.match)}}}"


@dataclass(repr=False)
class ToolCalled(GmEvaluator):
    """The transcript holds a ``tool_use`` named ``name`` (input optionally matched)."""

    name: str
    input_match: Optional[Dict[str, Any]] = None
    min_count: int = 1

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_tool_call(ev, self.name, self.input_match, self.min_count)

    def validate(self) -> List[str]:
        errs: List[str] = []
        if not isinstance(self.name, str) or not self.name:
            errs.append("name must be a non-empty string")
        if self.input_match is not None:
            errs.extend(checks.validate_pattern(self.input_match, "input_match"))
        if not isinstance(self.min_count, int) or self.min_count < 1:
            errs.append("min_count must be an int >= 1")
        return errs

    def label(self) -> str:
        return f"tool[{self.name}]" + (f"{{{_pattern_label(self.input_match)}}}" if self.input_match else "")


@dataclass(repr=False)
class FileMatches(GmEvaluator):
    """A file (relative = under the session dir) exists and matches."""

    path: str
    equals: Optional[str] = None
    contains: Optional[str] = None
    regex: Optional[str] = None
    exists: bool = True
    strip: bool = True

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_file(ev, self.path, exists=self.exists, equals=self.equals, contains=self.contains,
                                 regex=self.regex, strip=self.strip)

    def validate(self) -> List[str]:
        import re

        errs: List[str] = []
        if not isinstance(self.path, str) or not self.path.strip():
            errs.append("path must be a non-empty string")
        if self.exists and self.equals is None and self.contains is None and self.regex is None:
            errs.append("give at least one of equals / contains / regex (or exists=False)")
        if not self.exists and any(x is not None for x in (self.equals, self.contains, self.regex)):
            errs.append("exists=False cannot also assert content")
        if self.regex is not None:
            try:
                re.compile(self.regex)
            except re.error as exc:
                errs.append(f"bad regex {self.regex!r}: {exc}")
        return errs

    def label(self) -> str:
        return f"file[{self.path}]" + ("" if self.exists else "(absent)")


@dataclass(repr=False)
class BudgetGuard(GmEvaluator):
    """Max-turns / cost / output-token guard (post-hoc, from the transcript).

    Every case MUST carry one with at least one limit (the dry-run enforces it).
    The hard wall-clock stop during the run is the case ``timeout_s``."""

    max_turns: Optional[int] = None
    max_cost_usd: Optional[float] = None
    max_output_tokens: Optional[int] = None

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_budget(ev, max_turns=self.max_turns, max_cost_usd=self.max_cost_usd,
                                   max_output_tokens=self.max_output_tokens)

    def validate(self) -> List[str]:
        errs: List[str] = []
        if self.max_turns is None and self.max_cost_usd is None and self.max_output_tokens is None:
            errs.append("BudgetGuard needs at least one limit")
        for name in ("max_turns", "max_output_tokens"):
            v = getattr(self, name)
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
                errs.append(f"{name} must be an int >= 1")
        if self.max_cost_usd is not None and (not isinstance(self.max_cost_usd, (int, float))
                                              or self.max_cost_usd <= 0):
            errs.append("max_cost_usd must be > 0")
        return errs

    def has_limit(self) -> bool:
        return any(v is not None for v in (self.max_turns, self.max_cost_usd, self.max_output_tokens))

    def label(self) -> str:
        return "budget"


@dataclass(repr=False)
class RunSucceeded(GmEvaluator):
    """The headless launch succeeded and ``claude -p`` exited 0."""

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_run_ok(ev)

    def label(self) -> str:
        return "run-ok"


@dataclass(repr=False)
class TranscriptPresent(GmEvaluator):
    """D40: the transcript exists (non-empty) at its recorded location."""

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_transcript_present(ev)

    def label(self) -> str:
        return "transcript"


@dataclass(repr=False)
class ModelUsed(GmEvaluator):
    """Every model request ran on a model whose id contains ``contains``."""

    contains: str

    def check(self, ev: RunEvidence) -> checks.CheckResult:
        return checks.check_model_used(ev, self.contains)

    def validate(self) -> List[str]:
        return [] if isinstance(self.contains, str) and self.contains else ["contains must be a non-empty string"]

    def label(self) -> str:
        return f"model[{self.contains}]"

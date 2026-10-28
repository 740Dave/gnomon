"""Evaluator primitives — pure functions over :class:`RunEvidence`.

Each returns a :class:`CheckResult` (``passed`` + a human ``reason``). The
pydantic-evals ``Evaluator`` classes in :mod:`.evaluators` are thin wrappers;
keeping the logic here keeps it stdlib-only and directly unit-testable.

Record patterns (``match``) are JSON-shaped dicts, matched as a SUBSET of the
record:

* a plain value must be equal;
* a nested dict recurses (subset match on the nested object);
* an operator dict — every key starting with ``$`` — applies operators:
  ``{"$regex": "..."}`` (``re.search`` on ``str(value)``),
  ``{"$contains": x}`` (substring of a string, or member of a list),
  ``{"$exists": bool}`` (key presence; ``False`` passes when the key is absent).

Patterns must be JSON-serializable so a case definition can be validated (and
later stored as a YAML/JSON dataset) without running anything.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .evidence import SOURCES, RunEvidence

OPERATORS = ("$regex", "$contains", "$exists")


@dataclass(frozen=True)
class CheckResult:
    passed: bool
    reason: str


# --- pattern matching --------------------------------------------------------


def _is_operator_dict(value: Any) -> bool:
    return isinstance(value, dict) and bool(value) and all(isinstance(k, str) and k.startswith("$") for k in value)


def _apply_ops(present: bool, value: Any, ops: Dict[str, Any]) -> bool:
    for op, arg in ops.items():
        if op == "$exists":
            if bool(arg) != present:
                return False
            continue
        if not present:
            return False
        if op == "$regex":
            if re.search(str(arg), value if isinstance(value, str) else json.dumps(value, default=str)) is None:
                return False
        elif op == "$contains":
            if isinstance(value, str):
                if not isinstance(arg, str) or arg not in value:
                    return False
            elif isinstance(value, (list, tuple)):
                if arg not in value:
                    return False
            else:
                return False
        else:  # validate_pattern rejects these; be strict at match time too
            return False
    return True


def match_record(record: Any, pattern: Dict[str, Any]) -> bool:
    """True when ``record`` matches ``pattern`` (see module docstring)."""
    if not isinstance(record, dict):
        return False
    for key, want in pattern.items():
        present = key in record
        have = record.get(key)
        if _is_operator_dict(want):
            if not _apply_ops(present, have, want):
                return False
        elif isinstance(want, dict):
            if not present or not match_record(have, want):
                return False
        else:
            if not present or have != want:
                return False
    return True


def validate_pattern(pattern: Any, where: str = "match") -> List[str]:
    """Errors in a record pattern (empty list = valid). Used by the dry-run."""
    errs: List[str] = []
    if not isinstance(pattern, dict) or not pattern:
        return [f"{where}: must be a non-empty dict"]
    try:
        json.dumps(pattern)
    except (TypeError, ValueError) as exc:
        errs.append(f"{where}: not JSON-serializable ({exc})")
    for key, want in pattern.items():
        if not isinstance(key, str) or not key:
            errs.append(f"{where}: keys must be non-empty strings")
            continue
        if key.startswith("$"):
            errs.append(f"{where}.{key}: operator used as a field name")
            continue
        if _is_operator_dict(want):
            for op, arg in want.items():
                if op not in OPERATORS:
                    errs.append(f"{where}.{key}: unknown operator {op!r} (known: {', '.join(OPERATORS)})")
                elif op == "$regex":
                    try:
                        re.compile(str(arg))
                    except re.error as exc:
                        errs.append(f"{where}.{key}: bad $regex {arg!r}: {exc}")
                elif op == "$exists" and not isinstance(arg, bool):
                    errs.append(f"{where}.{key}: $exists takes a bool")
        elif isinstance(want, dict):
            errs.extend(validate_pattern(want, f"{where}.{key}"))
    return errs


def _brief(pattern: Dict[str, Any]) -> str:
    s = json.dumps(pattern, sort_keys=True, default=str)
    return s if len(s) <= 120 else s[:117] + "..."


# --- the primitives ------------------------------------------------------------


def check_record_present(ev: RunEvidence, match: Dict[str, Any], source: str = "injection_log",
                         min_count: int = 1) -> CheckResult:
    """``source`` contains at least ``min_count`` records matching ``match``."""
    if source not in ev.loaded_sources:
        return CheckResult(False, f"{source} was not loaded ({'; '.join(ev.load_errors) or 'no reason recorded'})")
    recs = ev.records(source)
    hits = [r for r in recs if match_record(r, match)]
    if len(hits) >= min_count:
        return CheckResult(True, f"{len(hits)} record(s) in {source} match {_brief(match)} (need >= {min_count})")
    return CheckResult(False, f"{len(hits)} of {len(recs)} {source} record(s) match {_brief(match)} "
                              f"(need >= {min_count})")


def check_record_absent(ev: RunEvidence, match: Dict[str, Any], source: str = "injection_log") -> CheckResult:
    """NO record in ``source`` matches ``match``. FAILS if ``source`` never loaded:
    an absence claim over a source that was not searched is not evidence."""
    if source not in ev.loaded_sources:
        return CheckResult(False, f"cannot assert absence: {source} was not loaded "
                                  f"({'; '.join(ev.load_errors) or 'no reason recorded'})")
    recs = ev.records(source)
    # rm2a2 F2: an absence claim over ZERO parsed records is vacuous — the source
    # "loaded" but there was nothing to search (empty / all-garbage file).
    if not recs and not (source == "tool_calls" and ev.transcript):
        return CheckResult(False, f"cannot assert absence: {source} has 0 records parsed")
    hits = [r for r in recs if match_record(r, match)]
    if hits:
        return CheckResult(False, f"{len(hits)} {source} record(s) match forbidden {_brief(match)}; "
                                  f"first: {_brief(hits[0])}")
    return CheckResult(True, f"none of {len(recs)} {source} record(s) match {_brief(match)}")


def check_tool_call(ev: RunEvidence, name: str, input_match: Optional[Dict[str, Any]] = None,
                    min_count: int = 1) -> CheckResult:
    """The transcript holds >= ``min_count`` ``tool_use`` calls named ``name``
    (whose ``input`` matches ``input_match``, when given)."""
    if "tool_calls" not in ev.loaded_sources:
        return CheckResult(False, f"transcript was not loaded ({'; '.join(ev.load_errors) or 'no reason'})")
    calls = ev.tool_calls()
    named = [c for c in calls if c.name == name]
    hits = [c for c in named if input_match is None or match_record(c.input, input_match)]
    desc = f"tool {name!r}" + (f" with input {_brief(input_match)}" if input_match else "")
    if len(hits) >= min_count:
        return CheckResult(True, f"{len(hits)} call(s) to {desc}")
    seen = sorted({c.name for c in calls})
    return CheckResult(False, f"{len(hits)} call(s) to {desc} (need >= {min_count}); "
                              f"{len(named)} call(s) named {name!r}; tools seen: {seen}")


def check_file(ev: RunEvidence, path: str, *, exists: bool = True, equals: Optional[str] = None,
               contains: Optional[str] = None, regex: Optional[str] = None, strip: bool = True) -> CheckResult:
    """A file the agent was to write exists (or not) and its text matches."""
    full = ev.resolve_path(path)
    if not exists:
        if os.path.exists(full):
            return CheckResult(False, f"{full} exists but must not")
        return CheckResult(True, f"{full} absent, as required")
    if not os.path.isfile(full):
        return CheckResult(False, f"{full} does not exist")
    try:
        with open(full, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError as exc:
        return CheckResult(False, f"{full} unreadable: {exc}")
    body = text.strip() if strip else text
    if equals is not None and body != equals:
        return CheckResult(False, f"{full} content {body[:80]!r} != expected {equals[:80]!r}")
    if contains is not None and contains not in body:
        return CheckResult(False, f"{full} does not contain {contains[:80]!r}")
    if regex is not None and re.search(regex, body) is None:
        return CheckResult(False, f"{full} does not match /{regex}/")
    return CheckResult(True, f"{full} exists ({len(text)} chars) and matches")


def check_budget(ev: RunEvidence, *, max_turns: Optional[int] = None, max_cost_usd: Optional[float] = None,
                 max_output_tokens: Optional[int] = None) -> CheckResult:
    """Post-hoc max-turns / cost guard over the transcript.

    A limit that cannot be MEASURED fails (no transcript, or a cost that is
    ``unknown``) — an unmeasured budget is not a kept one."""
    problems: List[str] = []
    notes: List[str] = []
    have_transcript = "transcript" in ev.loaded_sources
    if (max_turns is not None or max_output_tokens is not None or max_cost_usd is not None) and not have_transcript:
        return CheckResult(False, "budget unmeasurable: transcript not loaded")
    if not ev.transcript:   # rm2a2 F2: turns=0 on an empty transcript is not a kept budget
        return CheckResult(False, "budget unmeasurable: transcript has 0 parsed records")
    if max_turns is not None:
        turns = len(ev.model_requests())
        (problems if turns > max_turns else notes).append(f"turns={turns} (max {max_turns})")
    if max_output_tokens is not None:
        out = ev.usage_totals()["output_tokens"]
        (problems if out > max_output_tokens else notes).append(f"output_tokens={out} (max {max_output_tokens})")
    if max_cost_usd is not None:
        usd, src = ev.cost()
        if usd is None:
            problems.append(f"cost unknown (models {ev.models_used()}; no cost-state, no price) — max ${max_cost_usd}")
        else:
            (problems if usd > max_cost_usd else notes).append(f"cost=${usd:.4f} [{src}] (max ${max_cost_usd})")
    if problems:
        return CheckResult(False, "OVER BUDGET: " + "; ".join(problems + notes))
    return CheckResult(True, "within budget: " + "; ".join(notes))


def check_run_ok(ev: RunEvidence) -> CheckResult:
    if ev.launch_ok and ev.exit_code == 0:
        return CheckResult(True, "headless run exited 0")
    return CheckResult(False, f"run not ok: launch_ok={ev.launch_ok} exit_code={ev.exit_code} error={ev.error!r}")


def check_transcript_present(ev: RunEvidence) -> CheckResult:
    """D40: the transcript exists at the location recorded in launch.json."""
    if "transcript" in ev.loaded_sources and ev.transcript:
        return CheckResult(True, f"transcript at {ev.transcript_path} ({len(ev.transcript)} records)")
    return CheckResult(False, f"transcript missing/empty at recorded path {ev.transcript_path!r}")


def check_model_used(ev: RunEvidence, contains: str) -> CheckResult:
    """Every model request ran on a model whose id contains ``contains``."""
    models = ev.models_used()
    if not models:
        return CheckResult(False, "no model requests in the transcript")
    off = [m for m in models if contains not in m]
    if off:
        return CheckResult(False, f"models used {models}; not all contain {contains!r}")
    return CheckResult(True, f"models used {models}")


def validate_source(source: Any) -> List[str]:
    return [] if source in SOURCES else [f"source {source!r} unknown (known: {', '.join(SOURCES)})"]

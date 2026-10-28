"""The eval runner + CLI.

    python -m gnomon.evals list
    python -m gnomon.evals run [--case NAME ...] [--dry-run] [--report-dir DIR]
                                      [--work-root DIR] [--claude-bin PATH] [--allow-agent-launcher]

``--dry-run`` validates every selected case definition (and builds the Pydantic
Evals ``Dataset``) WITHOUT launching anything. A real run costs tokens, so it
additionally requires ``GNOMON_EVALS_LIVE=1`` in the environment — without it the
runner refuses (exit 2) and launches nothing.

Verdict per case: PASS only if the task produced evidence, at least one
assertion ran, every assertion passed, and no evaluator raised. A case that
produced no result at all is a FAIL, never a silent omission.

Exit codes: 0 = every case PASS (or every case VALID on a dry-run);
1 = any FAIL / INVALID; 2 = usage error, unknown case, or live run not enabled.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from pydantic_evals import Dataset

from .case import EvalCase, validate_cases
from .evidence import RunEvidence
from .task import DEFAULT_WORK_ROOT, LIVE_ENV, TaskOptions, live_enabled, make_task

DEFAULT_REPORT_DIR = os.path.join(DEFAULT_WORK_ROOT, "reports")


@dataclass
class AssertionRow:
    name: str
    passed: bool
    reason: str


@dataclass
class CaseVerdict:
    name: str
    passed: bool
    assertions: List[AssertionRow] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    evidence: Optional[Dict[str, Any]] = None

    @property
    def verdict(self) -> str:
        return "PASS" if self.passed else "FAIL"


def build_dataset(cases: Sequence[EvalCase], name: str = "gnomon-evals") -> Dataset:
    return Dataset(name=name, cases=[c.to_pydantic_case() for c in cases], evaluators=[])


def verdicts_from_report(report: Any, expected: Sequence[str]) -> List[CaseVerdict]:
    out: Dict[str, CaseVerdict] = {}
    for rc in report.cases:
        rows = [AssertionRow(k, bool(v.value), v.reason or "") for k, v in rc.assertions.items()]
        errors = [f"evaluator {f.name} raised: {f.error_message}" for f in rc.evaluator_failures]
        if not rows:
            errors.append("no assertions ran — a case cannot pass vacuously")
        ev = rc.output.summary() if isinstance(rc.output, RunEvidence) else None
        out[rc.name] = CaseVerdict(rc.name, bool(rows) and all(r.passed for r in rows) and not errors,
                                   rows, errors, ev)
    for f in report.failures:
        out[f.name] = CaseVerdict(f.name, False, [], [f"task failed: {f.error_message}"])
    for name in expected:
        if name not in out:
            out[name] = CaseVerdict(name, False, [], ["case produced no result (neither output nor failure)"])
    return [out[n] for n in expected] + [v for k, v in out.items() if k not in expected]


def run_cases(cases: Sequence[EvalCase], task_fn: Callable[[Any], Any]) -> List[CaseVerdict]:
    report = build_dataset(cases).evaluate_sync(task_fn, max_concurrency=1, progress=False)
    return verdicts_from_report(report, [c.name for c in cases])


def format_table(verdicts: Sequence[CaseVerdict]) -> str:
    lines = []
    width = max([len("CASE")] + [len(v.name) for v in verdicts])
    lines.append(f"{'CASE'.ljust(width)}  VERDICT  DETAIL")
    lines.append(f"{'-' * width}  -------  ------")
    for v in verdicts:
        npass = sum(1 for a in v.assertions if a.passed)
        lines.append(f"{v.name.ljust(width)}  {v.verdict:<7}  {npass}/{len(v.assertions)} assertions passed")
        pad = " " * (width + 11)
        for a in v.assertions:
            mark = "ok  " if a.passed else "FAIL"
            if not a.passed or not v.passed:
                lines.append(f"{pad}{mark} {a.name}: {a.reason}")
        for e in v.errors:
            lines.append(f"{pad}ERR  {e}")
        if v.evidence:
            e = v.evidence
            cost = "?" if e.get("cost_usd") is None else f"${e['cost_usd']:.4f} [{e.get('cost_source')}]"
            lines.append(f"{pad}run: exit={e.get('exit_code')} turns={e.get('turns')} cost={cost} "
                         f"models={e.get('models_used')} session={e.get('session_dir')}")
    return "\n".join(lines)


def write_report(payload: Dict[str, Any], report_dir: str) -> str:
    os.makedirs(report_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    path = os.path.join(report_dir, f"evals-{payload.get('mode', 'run')}-{stamp}-{os.getpid()}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
        fh.write("\n")
    return path


def dry_run(cases: Sequence[EvalCase]) -> Tuple[Dict[str, List[str]], bool]:
    """Validate definitions + build the Dataset. Launches nothing."""
    results = validate_cases(cases)
    try:
        build_dataset(cases)
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        results.setdefault("<dataset>", []).append(f"Dataset construction failed: {type(exc).__name__}: {exc}")
    return results, all(not errs for errs in results.values())


def _format_dry(results: Dict[str, List[str]]) -> str:
    width = max([len("CASE")] + [len(k) for k in results])
    lines = [f"{'CASE'.ljust(width)}  DRY-RUN  DETAIL", f"{'-' * width}  -------  ------"]
    for name, errs in results.items():
        lines.append(f"{name.ljust(width)}  {'VALID' if not errs else 'INVALID':<7}  "
                     f"{'definition ok; nothing launched' if not errs else f'{len(errs)} problem(s)'}")
        for e in errs:
            lines.append(f"{' ' * (width + 11)}- {e}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None, *, task_fn: Optional[Callable[[Any], Any]] = None,
         env: Optional[Dict[str, str]] = None, cases: Optional[Sequence[EvalCase]] = None,
         out=None) -> int:
    out = out or sys.stdout
    parser = argparse.ArgumentParser(prog="python -m gnomon.evals",
                                     description="Gnomon behavioural evals (headless agent_launch).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list registered cases")
    rp = sub.add_parser("run", help="run (or --dry-run validate) cases")
    rp.add_argument("--case", action="append", default=[], help="case name (repeatable; default all)")
    rp.add_argument("--dry-run", action="store_true", help="validate definitions; launch nothing")
    rp.add_argument("--report-dir", default=DEFAULT_REPORT_DIR)
    rp.add_argument("--work-root", default=None, help=f"tmp root for isolated runs (default {DEFAULT_WORK_ROOT})")
    rp.add_argument("--claude-bin", default=None, help="claude binary (default: the machine config's)")
    rp.add_argument("--allow-agent-launcher", action="store_true",
                    help="permit a run from inside a harnessed agent (its own rails then decide)")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 2 if exc.code else 0

    if cases is None:
        from .cases import CASES

        cases = CASES
    if args.cmd == "list":
        for c in cases:
            print(f"{c.name}\t{c.model}\t{c.description}", file=out)
        return 0

    by_name = {c.name: c for c in cases}
    unknown = [n for n in args.case if n not in by_name]
    if unknown:
        print(f"unknown case(s): {', '.join(unknown)} (known: {', '.join(sorted(by_name))})", file=out)
        return 2
    selected = [by_name[n] for n in args.case] if args.case else list(cases)

    results, valid = dry_run(selected)
    if args.dry_run:
        print(_format_dry(results), file=out)
        path = write_report({"mode": "dry-run", "valid": valid, "cases": results}, args.report_dir)
        print(f"\nreport: {path}", file=out)
        return 0 if valid else 1
    if not valid:
        print(_format_dry(results), file=out)
        print("\nrefusing a live run: fix the INVALID case definitions first", file=out)
        return 2
    if not live_enabled(env):
        print(f"refusing a LIVE run: real runs launch agents and cost tokens; set {LIVE_ENV}=1 "
              "(or use --dry-run). Nothing was launched.", file=out)
        return 2

    if task_fn is None:
        task_fn = make_task(TaskOptions(work_root=args.work_root, claude_bin=args.claude_bin,
                                        allow_agent_launcher=args.allow_agent_launcher, live_env=env))
    t0 = time.time()
    verdicts = run_cases(selected, task_fn)
    print(format_table(verdicts), file=out)
    passed = all(v.passed for v in verdicts)
    path = write_report({"mode": "run", "passed": passed, "started_at": t0, "duration_s": time.time() - t0,
                         "cases": [asdict(v) | {"verdict": v.verdict} for v in verdicts]}, args.report_dir)
    print(f"\n{sum(v.passed for v in verdicts)}/{len(verdicts)} PASS — report: {path}", file=out)
    return 0 if passed else 1

"""CLI: ``python3 -m gnomon.skills`` — sync skills from the PROC graph.

Equivalent to the original services package's skills CLI, which it replaces. Dry-run by default
(prints the diff, writes nothing). ``--apply`` performs CREATE/UPDATE; ``--apply --prune`` also
removes ``generated-from`` orphans.

Operator tool (X49 fix round, F-11): the CLI is the architect's tool, NOT the agent self-verb, so the
verb's dry-run receipt does not apply here (``--apply`` needs no preceding dry run). It is still
logged: every ``--apply`` appends one ``skill-sync`` record (same format as the verb's, with
``source``/``surface`` = ``cli``) to ``<--session-dir>/.claude/injection_log.jsonl`` (default: the
current directory), and an apply whose log cannot be written is refused with nothing written.

Reader selection (exactly one):
  --corpus ID       scan ONE named corpus in-process through Gnomon's own lts access (repeatable);
                    the run is SCOPED to those corpora: other corpora's generated skills are never
                    reported as orphans or pruned
  --all-corpora     scan every registered corpus (the original default; unscoped)
  --scan-file PATH  read nodes from a captured JSON scan (a list, or an lts_find payload); no graph
                    needed; used by tests and for replaying a captured graph state

The old ``--mcp-json`` (spawn a second lts-engine over stdio) is gone: the in-process reader
replaces it. Exit codes: 0 in sync, 1 dry-run drift, 2 apply-time or read failure.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .reader import InProcessReader, NodeReader, ReaderError, StaticReader
from .reconcile import Action
from .sync import SyncResult, sync_skills

DEFAULT_TARGET = "~/.claude/skills"

# Actions that mean "the tree does not match the graph" — a non-zero exit in --check-style use.
_DRIFT_ACTIONS = {Action.CREATE, Action.UPDATE, Action.CONFLICT, Action.COLLISION, Action.REFUSE, Action.ORPHAN}


def _load_scan_file(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and "nodes" in data:
        return list(data["nodes"])
    raise SystemExit(f"--scan-file {path}: expected a JSON list or an object with a 'nodes' key")


def _build_reader(args: argparse.Namespace) -> NodeReader:
    if args.scan_file:
        return StaticReader(_load_scan_file(Path(args.scan_file)))
    return InProcessReader(args.corpus if args.corpus else None)


def _print_human(result: SyncResult) -> None:
    counts = result.plan.to_dict()["counts"]
    mode = "APPLY" if result.applied else "DRY-RUN"
    if result.applied and result.pruned:
        mode = "APPLY+PRUNE"
    print(f"[gnomon.skills] mode={mode} scanned={result.scanned}")
    order = [Action.CREATE, Action.UPDATE, Action.NOOP, Action.CONFLICT, Action.COLLISION, Action.REFUSE, Action.ORPHAN]
    for action in order:
        entries = result.plan.by_action(action)
        if not entries:
            continue
        print(f"\n{action.value} ({len(entries)}):")
        for e in entries:
            nid = f"{e.node_id}@{e.corpus}" if e.node_id and e.corpus else (e.node_id or "-")
            print(f"  - {e.skill_name or '?'}  [{nid}]  {e.detail}")
            print(f"      {e.path}")
    print("\ncounts:", json.dumps(counts))
    if result.applied:
        print(f"performed {len(result.performed)} action(s)")
    if result.errors:
        print(f"⛔ {len(result.errors)} apply error(s):", file=sys.stderr)
        for err in result.errors:
            print(f"  - {err.get('skill_name')}: {err.get('error')}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m gnomon.skills", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", default=DEFAULT_TARGET,
                   help=f"skills directory to reconcile (default {DEFAULT_TARGET})")
    p.add_argument("--apply", action="store_true", help="perform CREATE/UPDATE (default: dry-run)")
    p.add_argument("--prune", action="store_true", help="with --apply, remove generated-from orphans")
    p.add_argument("--json", action="store_true", dest="as_json", help="emit the plan as JSON")
    p.add_argument("--corpus", action="append", default=[], metavar="ID",
                   help="scan this corpus in-process and scope the run to it (repeatable)")
    p.add_argument("--all-corpora", action="store_true", help="scan every registered corpus (unscoped)")
    p.add_argument("--session-dir", default=None, metavar="DIR",
                   help="session dir whose .claude/injection_log.jsonl receives the apply record "
                        "(default: the current directory)")
    p.add_argument("--scan-file", default=None,
                   help="read nodes from a captured JSON scan instead of spawning lts-engine")
    args = p.parse_args(argv)

    if args.prune and not args.apply:
        p.error("--prune requires --apply")
    chosen = sum(bool(x) for x in (args.corpus, args.all_corpora, args.scan_file))
    if chosen != 1:
        p.error("choose exactly one of --corpus ID (repeatable), --all-corpora, --scan-file PATH")

    scope = args.corpus if args.corpus else None
    session_dir = str(Path(args.session_dir or Path.cwd()).expanduser())
    if args.apply:
        from . import verb as _verb
        lerr = _verb._log_writable(session_dir)
        if lerr:
            print(f"[gnomon.skills] STOPPED, nothing written: the injection log cannot be written "
                  f"({lerr}); an unlogged generation is not allowed", file=sys.stderr)
            return 2
    try:
        reader = _build_reader(args)
        result = sync_skills(reader, args.target, apply=args.apply, prune=args.prune, scope_corpora=scope)
    except ReaderError as exc:
        print(f"[gnomon.skills] STOPPED, nothing planned or written: {exc}", file=sys.stderr)
        return 2

    log_err = None
    if args.apply:
        from . import verb as _verb
        counts = result.plan.to_dict()["counts"]
        log_err = _verb._log(
            session_dir, "cli", ",".join(scope) if scope else None,
            "applied" if result.ok else "failed", apply=True, ok=result.ok,
            reason=f"cli apply{' +prune' if args.prune else ''}: {len(result.performed)} performed, "
                   f"{len(result.errors)} error(s)",
            counts=counts, written=sorted(p["skill_name"] for p in result.performed if p.get("skill_name")),
            blocked=sorted(e.skill_name for e in result.plan.entries
                           if e.skill_name and e.action in (Action.CONFLICT, Action.COLLISION, Action.REFUSE)),
            source="cli")

    if args.as_json:
        print(json.dumps(result.to_dict(), indent=2))
    else:
        _print_human(result)

    if log_err:
        print(f"⛔ the apply was carried out but its log row was NOT written: {log_err}", file=sys.stderr)
        return 2
    if not result.ok:
        return 2  # apply-time failure
    # Dry-run drift → exit 1 (usable as a provisioning "is the tree in sync?" check).
    if not args.apply and any(e.action in _DRIFT_ACTIONS for e in result.plan.entries):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Orchestrate a sync: scan -> validate -> reconcile -> diff -> (optionally) apply/prune.

Dry-run is the default (constraint 3): ``sync_skills(...)`` computes and returns the plan and
writes NOTHING unless ``apply=True``. Under ``apply`` it performs CREATE/UPDATE (atomic
write); orphans are removed only under ``apply and prune``. CONFLICT, COLLISION and REFUSE are
NEVER written or deleted.

X49: the plan build and the plan apply are separate (:func:`build_plan` / :func:`apply_plan`) so
the ``sync_my_skills`` self-verb can show a plan, digest it, and later apply EXACTLY that plan.
``scope_corpora`` restricts a run to the named corpora: a node from any other corpus is skipped
(never planned, never written), and orphan detection ignores other corpora's generated skills.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .contract import ContractError, SkillProjection, is_projection_candidate, parse_projection
from .reader import NodeReader
from .reconcile import Action, Plan, PlanEntry, classify, generated_corpus
from .render import parse_stamps


@dataclass
class SyncResult:
    plan: Plan
    scanned: int
    applied: bool
    pruned: bool
    skipped: int = 0  # non-opt-in nodes (text-scan false hits) — invisible, not refused
    out_of_scope: int = 0  # opt-in nodes from a corpus outside scope_corpora — never planned
    performed: list[dict[str, Any]] = field(default_factory=list)  # actions actually carried out
    errors: list[dict[str, Any]] = field(default_factory=list)  # apply-time failures (loud, never swallowed)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "scanned": self.scanned,
            "skipped": self.skipped,
            "out_of_scope": self.out_of_scope,
            "applied": self.applied,
            "pruned": self.pruned,
            "plan": self.plan.to_dict(),
            "performed": self.performed,
            "errors": self.errors,
        }


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".skill-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@dataclass
class BuiltPlan:
    plan: Plan
    scanned: int
    skipped: int
    out_of_scope: int


def build_plan(reader: NodeReader, target_dir: str | os.PathLike[str], *,
               scope_corpora: Iterable[str] | None = None) -> BuiltPlan:
    """Scan, validate and reconcile. Reads the graph and the disk; writes NOTHING."""
    target = Path(target_dir).expanduser()
    scope = frozenset(scope_corpora) if scope_corpora is not None else None
    nodes = reader.scan()
    projections: list[SkillProjection] = []
    refusals: list[ContractError] = []
    skipped = 0
    out_of_scope = 0
    for node in nodes:
        if not is_projection_candidate(node):
            # Not opted in (may be a text-scan false hit) — INVISIBLE, never a refusal.
            skipped += 1
            continue
        if scope is not None and node.get("corpus") not in scope:
            out_of_scope += 1
            continue
        try:
            projections.append(parse_projection(node))
        except ContractError as err:
            refusals.append(err)
        except Exception as exc:  # noqa: BLE001 - X49 F-1: one malformed node never blocks the corpus
            nid = node.get("id") if isinstance(node, dict) else None
            refusals.append(ContractError(
                f"node could not be validated ({type(exc).__name__}: {exc})",
                node_id=nid if isinstance(nid, str) else None))
    return BuiltPlan(classify(projections, refusals, target, scope), len(nodes), skipped, out_of_scope)


def apply_plan(plan: Plan, *, prune: bool = False, scope_corpora: Iterable[str] | None = None,
               ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Carry out a plan's CREATE/UPDATE (and, under ``prune``, ORPHAN) entries.

    Returns ``(performed, errors)``. Before each write the entry's disk state is RE-CHECKED against
    what the plan assumed (CREATE: the directory still does not exist; UPDATE: the file is still
    stamped for the SAME corpus), and the content to write must carry a ``generated-from`` stamp for
    the entry's own node and corpus (and, if ``scope_corpora`` is set, a corpus in scope). A failed
    re-check is an error entry, never a write.
    """
    scope = frozenset(scope_corpora) if scope_corpora is not None else None
    performed: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for entry in plan.entries:
        try:
            if entry.action in (Action.CREATE, Action.UPDATE):
                assert entry.rendered is not None
                _check_write(entry, scope)
                _atomic_write(entry.path, entry.rendered)
                performed.append(entry.to_dict())
            elif entry.action is Action.ORPHAN and prune:
                _check_prune(entry, scope)
                _prune_orphan(entry)
                performed.append(entry.to_dict())
            # NOOP / CONFLICT / COLLISION / REFUSE, and ORPHAN without prune: nothing to do.
        except (OSError, WriteRefused) as e:  # apply-time failure — reported loudly, never a silent skip
            errors.append({**entry.to_dict(), "error": f"{type(e).__name__}: {e}"})
    return performed, errors


class WriteRefused(Exception):
    """A pre-write re-check failed (the disk or the content is not what the plan assumed)."""


def _check_write(entry: PlanEntry, scope: frozenset[str] | None) -> None:
    stamps = parse_stamps(entry.rendered or "")
    if stamps.generated_from != f"{entry.node_id}@{entry.corpus}":
        raise WriteRefused(f"content to write is not stamped generated-from {entry.node_id}@{entry.corpus}")
    if scope is not None and entry.corpus not in scope:
        raise WriteRefused(f"corpus {entry.corpus!r} is outside the sync scope")
    skill_dir = entry.path.parent
    if skill_dir.is_symlink() or entry.path.is_symlink():
        raise WriteRefused("skill directory or SKILL.md became a symlink since the plan was built")
    if entry.action is Action.CREATE:
        if skill_dir.exists():
            raise WriteRefused("the skill directory appeared since the plan was built")
        return
    try:
        existing = parse_stamps(entry.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise WriteRefused(f"existing SKILL.md no longer readable ({type(exc).__name__})") from None
    if not existing.is_generated or generated_corpus(existing.generated_from) != entry.corpus:
        raise WriteRefused("existing SKILL.md is no longer generated from this corpus since the plan was built")


def _check_prune(entry: PlanEntry, scope: frozenset[str] | None) -> None:
    if entry.path.parent.is_symlink() or entry.path.is_symlink():
        raise WriteRefused("orphan path is a symlink; not pruned")
    try:
        stamps = parse_stamps(entry.path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise WriteRefused(f"orphan no longer readable ({type(exc).__name__})") from None
    if not stamps.is_generated:
        raise WriteRefused("orphan is no longer generated-from stamped; not pruned")
    if scope is not None and generated_corpus(stamps.generated_from) not in scope:
        raise WriteRefused("orphan is stamped for a corpus outside the sync scope; not pruned")


def sync_skills(
    reader: NodeReader,
    target_dir: str | os.PathLike[str],
    *,
    apply: bool = False,
    prune: bool = False,
    scope_corpora: Iterable[str] | None = None,
) -> SyncResult:
    """Run one reconcile against ``target_dir``. Dry-run unless ``apply``."""
    built = build_plan(reader, target_dir, scope_corpora=scope_corpora)
    result = SyncResult(plan=built.plan, scanned=built.scanned, applied=apply, pruned=prune,
                        skipped=built.skipped, out_of_scope=built.out_of_scope)
    if not apply:
        return result
    result.performed, result.errors = apply_plan(built.plan, prune=prune, scope_corpora=scope_corpora)
    return result


def _prune_orphan(entry: PlanEntry) -> None:
    """Remove an orphan's SKILL.md and its (now-empty) directory. Only the generated file."""
    skill_file = entry.path
    if skill_file.exists():
        skill_file.unlink()
    parent = skill_file.parent
    try:
        # Remove the directory only if empty (never a recursive blow-away).
        parent.rmdir()
    except OSError:
        pass

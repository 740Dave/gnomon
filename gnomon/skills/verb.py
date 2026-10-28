"""the ``sync_my_skills`` self-verb's logic (the MCP plumbing is in ``harness/self_mcp``).

An agent that writes a procedure (a ``node-kind: skill-projection`` node in ITS OWN corpus) can turn
it into a ``~/.claude/skills/<name>/SKILL.md`` thin launcher, safely. Guardrails (spec
design (a)-(e)); each has a test in ``tests/test_x49_sync_my_skills.py``:

(a) SCOPE. The corpus is mapped from the CALLING session's identity (resolved by the harness, never
    an argument) through its profile (``skill_corpus``); an unknown mapping is a refusal. The reader
    scans only that corpus and the sync is scoped to it (other corpora's nodes are never planned,
    other corpora's generated skills are never reported as orphans).
(b) WRITES. Only ``~/.claude/skills/<name>/SKILL.md`` for a validated skill name, with content that
    carries a ``generated-from: <node>@<own-corpus>`` stamp, re-checked at the moment of the write.
    The verb never prunes and never deletes.
(c) DRY RUN FIRST. ``apply=false`` (default) returns the plan and a per-skill diff and writes nothing
    but a dry-run receipt. ``apply=true`` runs only if THIS session recorded a dry run, under an hour
    ago, for the same corpus, whose plan digest equals the plan computed now; otherwise it is refused
    and the fresh plan is shown (and recorded, so the next ``apply=true`` can go ahead). A receipt is
    spent by the apply.
(d) LOGGED. Every call (refusals too) appends one ``{"kind": "skill-sync"}`` record to the session's
    injection log: counts and skill names, never file content. The dashboard reads the latest one
    back for the session row. An apply is refused (nothing written) if the log cannot be written.
(e) COLLISIONS. A skill name already taken by anything not generated from the caller's own corpus (a
    hand-written skill, another corpus's generated skill, a bare directory, a symlink, or two nodes
    claiming the name) is reported and never written.

⛔ HONEST BOUNDARY (g29b): the session state files this verb reads and writes (``launch.json``, the
plan receipt, the injection log) are agent-writable. The identity check is scope HYGIENE at the
argument boundary, not a security boundary; a determined agent can forge a receipt. What the verb
guarantees is that an HONEST call cannot write outside its own corpus's generated files, and that
every call leaves a record.

Flag ``sync_my_skills`` (default OFF). Off = the verb is not advertised, and a hand-built
call is refused ``disabled`` here.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .reader import NodeReader, ReaderError
from .reconcile import Action, Plan, PlanEntry, WRITE_ACTIONS
from .sync import apply_plan, build_plan

FLAG_NAME = "sync_my_skills"
TOOL_NAME = "sync_my_skills"
#: The verb's closed parameter set (g29b).
PARAM_NAMES = frozenset({"apply"})
RECEIPT_FILENAME = "sync_my_skills_plan.json"
#: A dry-run receipt older than this no longer authorises an apply.
RECEIPT_TTL_SECONDS = 3600.0
MAX_DIFF_CHARS = 20000
_CORPUS_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: ``decision`` values of the ``skill-sync`` provenance record (usage_metrics.VERB_LOGGED_AS).
D_DRY_RUN = "dry-run"
D_APPLIED = "applied"
D_APPLIED_PARTIAL = "applied-partial"
D_REFUSED = "refused"
D_DISABLED = "disabled"
D_FAILED = "failed"

#: The actions that mean "something was reported but not written".
_BLOCKING = frozenset({Action.CONFLICT, Action.COLLISION, Action.REFUSE})


def default_skills_dir() -> str:
    """``~/.claude/skills`` (HOME-relative, so a test with an isolated HOME never touches the real one).
    Deliberately NOT a verb argument."""
    return os.path.join(os.path.expanduser("~"), ".claude", "skills")


def resolve_corpus(instance_id: str, profile_store: Any) -> tuple[Optional[str], str]:
    """``(corpus, "")`` from the instance's own profile ``skill_corpus``, or ``(None, why)``."""
    try:
        prof = profile_store.read(instance_id)
    except KeyError:
        return None, f"no profile.json for instance {instance_id!r}, so its corpus is unknown"
    except Exception as exc:  # noqa: BLE001 - a bad profile is a refusal, never a crash
        return None, f"profile for {instance_id!r} unreadable ({type(exc).__name__})"
    corpus = prof.skill_corpus
    if not isinstance(corpus, str) or not corpus.strip():
        return None, (f"profile for {instance_id!r} has no skill_corpus, so its corpus is unknown "
                      f"(an operator sets it in profile.json; it is never an argument)")
    corpus = corpus.strip()
    if not _CORPUS_RE.match(corpus):
        return None, f"profile skill_corpus {corpus!r} is not corpus-id shaped"
    return corpus, ""


def plan_digest(plan: Plan) -> str:
    """sha256 over everything an apply would do or skip: per entry the action, names, path, detail
    and the hash of the content that would be written."""
    rows = []
    for e in plan.entries:
        rows.append([e.action.value, e.skill_name, e.node_id, e.corpus, str(e.path), e.detail,
                     hashlib.sha256((e.rendered or "").encode("utf-8")).hexdigest() if e.rendered else None])
    blob = json.dumps(sorted(rows, key=lambda r: json.dumps(r, sort_keys=True)),
                      sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _entry_view(entry: PlanEntry) -> Dict[str, Any]:
    out = entry.to_dict()
    if entry.action in WRITE_ACTIONS and entry.rendered is not None:
        try:
            before = entry.path.read_text(encoding="utf-8") if entry.action is Action.UPDATE else ""
        except (OSError, UnicodeDecodeError):
            before = ""
        diff = "".join(difflib.unified_diff(
            before.splitlines(keepends=True), entry.rendered.splitlines(keepends=True),
            fromfile=f"{entry.path} (now)" if before else "(new file)", tofile=f"{entry.path} (after apply)"))
        if len(diff) > MAX_DIFF_CHARS:
            diff = diff[:MAX_DIFF_CHARS] + "\n... (diff truncated)"
            out["diff_truncated"] = True
        out["diff"] = diff
    return out


def _view(plan: Plan, scanned: int, skipped: int, out_of_scope: int) -> Dict[str, Any]:
    d = plan.to_dict()
    return {"counts": d["counts"], "scanned": scanned, "skipped": skipped, "out_of_scope": out_of_scope,
            "entries": [_entry_view(e) for e in plan.entries]}


def _receipt_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", RECEIPT_FILENAME)


def _read_receipt(session_dir: str) -> Optional[Dict[str, Any]]:
    try:
        with open(_receipt_path(session_dir), "r", encoding="utf-8") as fh:
            obj = json.load(fh)
        return obj if isinstance(obj, dict) else None
    except (OSError, ValueError):
        return None


def _write_receipt(session_dir: str, rec: Dict[str, Any]) -> None:
    path = _receipt_path(session_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh)
    os.replace(tmp, path)


def _spend_receipt(session_dir: str) -> None:
    try:
        os.unlink(_receipt_path(session_dir))
    except OSError:
        pass


def _log_path(session_dir: str) -> str:
    from ..harness.hooks import injection_log_path

    return injection_log_path(session_dir)


def _log_writable(session_dir: str) -> Optional[str]:
    """None if the injection log can be appended to, else the reason (touches nothing but the file's mtime)."""
    try:
        path = _log_path(session_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8"):
            pass
        return None
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"


def _log(session_dir: str, instance_id: str, corpus: Optional[str], decision: str, *, apply: bool, ok: bool,
         reason: str = "", counts: Optional[Dict[str, int]] = None, written: Optional[List[str]] = None,
         blocked: Optional[List[str]] = None, digest: Optional[str] = None,
         source: str = "self-verb") -> Optional[str]:
    """Append the ONE ``skill-sync`` record. Returns an error string if it could not be written."""
    if not session_dir:
        return "no session_dir from the harness: provenance row NOT written"
    from ..harness.hooks import append_record

    path = _log_path(session_dir)
    try:
        before = os.path.getsize(path) if os.path.exists(path) else 0
        # NOTE: ProvenanceLog.append SWALLOWS an OSError (counts it, prints once, keeps the record in
        # memory), so the try/except here would never see a failed write. The size check below does.
        append_record(path, {
            "kind": "skill-sync", "source": source, "surface": source,
            "instance_id": instance_id, "corpus": corpus, "decision": decision, "apply": bool(apply),
            "ok": bool(ok), "reason": reason[:300], "counts": counts or {}, "written": written or [],
            "blocked": blocked or [], "plan_digest": digest})
        if not os.path.isfile(path) or os.path.getsize(path) <= before:
            return "the injection log did not grow: provenance row NOT written"
    except OSError as exc:
        return str(exc)
    return None


def run(*, instance_id: str, session_dir: str, profile_store: Any,
        reader_factory: Callable[[str], NodeReader], skills_dir: Optional[str] = None,
        flag_on: bool = False, apply: bool = False,
        now: Callable[[], float] = time.time) -> Dict[str, Any]:
    """One ``sync_my_skills`` call. Never raises; every outcome is a dict with ``ok`` and ``decision``."""
    skills_dir = skills_dir or default_skills_dir()
    out: Dict[str, Any] = {"ok": False, "action": TOOL_NAME, "apply": bool(apply)}

    def finish(decision: str, ok: bool, *, reason: str = "", corpus: Optional[str] = None,
               view: Optional[Dict[str, Any]] = None, digest: Optional[str] = None,
               written: Optional[List[str]] = None, blocked: Optional[List[str]] = None) -> Dict[str, Any]:
        out.update(ok=ok, decision=decision)
        if reason:
            out["reason"] = reason
            if not ok:
                out["error"] = reason
        if corpus:
            out["corpus"] = corpus
        if view is not None:
            out["plan"] = view
        if digest:
            out["plan_digest"] = digest
        if written is not None:
            out["written"] = written
        if blocked:
            out["blocked"] = blocked
        err = _log(session_dir, instance_id, corpus, decision, apply=apply, ok=ok, reason=reason,
                   counts=(view or {}).get("counts"), written=written, blocked=blocked, digest=digest)
        if err:
            out["provenance_error"] = err
        return out

    if not flag_on:
        return finish(D_DISABLED, False, reason=f"sync_my_skills is disabled for this session (feature flag {FLAG_NAME} is off)")
    if not instance_id:
        return finish(D_REFUSED, False, reason="no calling identity")
    corpus, why = resolve_corpus(instance_id, profile_store)
    if corpus is None:
        return finish(D_REFUSED, False, reason=f"refused (nothing read or written): {why}")
    if not session_dir:
        return finish(D_REFUSED, False, corpus=corpus,
                      reason="refused (nothing written): no session_dir, so the dry-run receipt and the log have nowhere to go")

    try:
        built = build_plan(reader_factory(corpus), skills_dir, scope_corpora=[corpus])
    except ReaderError as exc:
        return finish(D_FAILED, False, corpus=corpus, reason=f"STOPPED, nothing planned or written: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface, never crash the server
        return finish(D_FAILED, False, corpus=corpus, reason=f"planning failed: {type(exc).__name__}: {exc}")
    plan = built.plan
    digest = plan_digest(plan)
    view = _view(plan, built.scanned, built.skipped, built.out_of_scope)
    blocked = sorted({e.skill_name or "?" for e in plan.entries if e.action in _BLOCKING})
    t = now()

    def record_receipt() -> Optional[str]:
        try:
            _write_receipt(session_dir, {"instance_id": instance_id, "corpus": corpus, "plan_digest": digest,
                                         "ts": t, "skills_dir": str(skills_dir)})
            return None
        except OSError as exc:
            return f"{type(exc).__name__}: {exc}"

    if not apply:
        rerr = record_receipt()
        if rerr:
            return finish(D_FAILED, False, corpus=corpus, view=view, digest=digest,
                          reason=f"dry run computed but its receipt could not be written ({rerr}), so apply is not possible")
        out["next"] = (f"Review the plan. To apply exactly this plan call {TOOL_NAME}(apply=true) in this session "
                       f"within {int(RECEIPT_TTL_SECONDS // 60)} minutes.")
        return finish(D_DRY_RUN, True, corpus=corpus, view=view, digest=digest, blocked=blocked)

    # --- apply: needs a fresh receipt for exactly this plan
    receipt = _read_receipt(session_dir)
    problem = ""
    if receipt is None:
        problem = "no dry run was recorded in this session"
    elif receipt.get("instance_id") != instance_id or receipt.get("corpus") != corpus:
        problem = "the recorded dry run belongs to a different instance or corpus"
    elif str(receipt.get("skills_dir")) != str(skills_dir):
        problem = "the recorded dry run was for a different skills directory"
    elif not isinstance(receipt.get("ts"), (int, float)) or t - float(receipt["ts"]) > RECEIPT_TTL_SECONDS \
            or t < float(receipt["ts"]):
        problem = "the recorded dry run has expired (or its clock is unreadable)"
    elif receipt.get("plan_digest") != digest:
        problem = "the plan changed since your dry run (the graph or the skills directory moved)"
    if problem:
        rerr = record_receipt()  # the fresh plan is being shown now: a following apply=true may go ahead
        if rerr:
            out["receipt_error"] = rerr
        out["next"] = f"Read the plan shown here, then call {TOOL_NAME}(apply=true) again to apply exactly it."
        return finish(D_REFUSED, False, corpus=corpus, view=view, digest=digest, blocked=blocked,
                      reason=f"apply refused, nothing written: {problem}. The fresh plan is shown for review.")

    lerr = _log_writable(session_dir)
    if lerr:
        return finish(D_REFUSED, False, corpus=corpus, view=view, digest=digest, blocked=blocked,
                      reason=f"apply refused, nothing written: the injection log cannot be written ({lerr}); "
                             f"an unlogged generation is not allowed")

    performed, errors = apply_plan(plan, prune=False, scope_corpora=[corpus])
    _spend_receipt(session_dir)
    written = sorted(p["skill_name"] for p in performed if p.get("skill_name"))
    if errors:
        out["errors"] = errors
        return finish(D_FAILED, False, corpus=corpus, view=view, digest=digest, written=written, blocked=blocked,
                      reason=f"{len(errors)} write(s) refused or failed; {len(written)} written")
    return finish(D_APPLIED_PARTIAL if blocked else D_APPLIED, True, corpus=corpus, view=view, digest=digest,
                  written=written, blocked=blocked,
                  reason=(f"{len(written)} skill file(s) written; {len(blocked)} name(s) blocked and left untouched: "
                          f"{', '.join(blocked)}" if blocked else ""))

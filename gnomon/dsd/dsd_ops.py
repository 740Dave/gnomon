"""DSD operations for an agent's OWN decisions: send, list, withdraw, replace (pre-review batch B1; flag ``dsd_self_verbs``).

ONE module holds the CORE functions, REGISTERED BY NAME (:data:`CORE_FUNCTIONS`, :func:`get`) so a later PROC runner
can call them; the self-verbs on the session's tool server (``harness/self_mcp.py``) are thin wrappers and do not wait for it.

Every function takes the calling session's ``instance_id`` as its FIRST argument. In production that value comes from the
HARNESS (``self_mcp.resolve_self_identity``: the proxy's ``launch_context`` over this session's private control socket),
never from tool input; no verb has an instance-id parameter. A function that is handed an id that is not a plain instance id
(a separator, a traversal, a dot-file) refuses it, and a store folder that is a symlink out of the store root is refused by
the same containment check the direct write uses.

The write goes through the SAME store code and lock as the direct write: :class:`gnomon.hub.mcp.DSDStore.persist`
(the collision-free id minting under ``<root>/.dsd-mint.lock``, the zero-options refusal, the containment check), and a status
change is made under :func:`gnomon.dsd.inbox.status_lock`, the lock the answer flip (:meth:`DsdInbox.mark_answered`)
also takes, so an ANSWER WINS a race with a withdraw or replace (condition C7).

This module is imported lazily by the verb handler only; no hook entry imports it (measured: tools/measure_hook_closure.py).

stdlib plus the DSD model; lazy imports for the store.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional

from ..profile import _validate_instance_id, default_intelligences_root, ensure_contained
from .inbox import (STATUS_ANSWERED, STATUS_PENDING, STATUS_REPLACED, STATUS_UNFINISHED, STATUS_WITHDRAWN, DsdInbox,
                    effective_raw, sidecar_path, status_lock, write_lock)
from .model import parse_dsd, validate_dsd
from .notice import NOTICE_DSD_ID_RE

#: the largest markdown a verb will file (bytes of UTF-8).
MAX_MARKDOWN_BYTES = 200 * 1024
#: how long a first "the operator has it open" refusal stays armed for the confirming second call.
CONFIRM_WINDOW_SECONDS = 600.0
#: a review beacon this fresh means the operator has that DSD open now.
PRESENCE_FRESH_SECONDS = 90.0
MAX_REASON_CHARS = 500

#: the status words an agent sees in a reply (the stored value of a pending record stays ``pending-human-review``: C6).
STATUS_WORDS = ("pending", "answered", "withdrawn", "replaced")
_STORED_TO_WORD = {STATUS_PENDING: "pending", STATUS_ANSWERED: "answered",
                   STATUS_WITHDRAWN: "withdrawn", STATUS_REPLACED: "replaced",
                   STATUS_UNFINISHED: "unfinished-replacement"}


class DsdRefused(Exception):
    """A verb was refused; the message is plain words safe to hand back to the agent."""


# --------------------------------------------------------------------------- registry

CORE_FUNCTIONS: Dict[str, Callable[..., Dict[str, Any]]] = {}


def register(name: str) -> Callable[[Callable[..., Dict[str, Any]]], Callable[..., Dict[str, Any]]]:
    def deco(fn: Callable[..., Dict[str, Any]]) -> Callable[..., Dict[str, Any]]:
        CORE_FUNCTIONS[name] = fn
        return fn
    return deco


def get(name: str) -> Callable[..., Dict[str, Any]]:
    """The registered core function ``name`` (``send`` | ``list_mine`` | ``withdraw`` | ``replace``)."""
    try:
        return CORE_FUNCTIONS[name]
    except KeyError:
        raise KeyError(f"no DSD core function named {name!r}; registered: {sorted(CORE_FUNCTIONS)}") from None


# --------------------------------------------------------------------------- helpers

def _root(root: Optional[str]) -> str:
    return os.path.abspath(os.path.expanduser(root if root is not None else default_intelligences_root()))


def _own_dir(root: str, instance_id: str) -> str:
    """``<root>/<instance_id>/dsd``, for a plain instance id only; a folder that resolves outside ``root`` is refused."""
    try:
        _validate_instance_id(instance_id)
    except ValueError as exc:
        raise DsdRefused(f"refused: not a usable instance id ({exc})") from None
    if instance_id.startswith("."):
        raise DsdRefused(f"refused: not a usable instance id ({instance_id!r})")
    d = os.path.join(root, instance_id, "dsd")
    try:
        ensure_contained(root, d)
    except ValueError as exc:
        raise DsdRefused(f"refused: that instance's DSD folder is not inside the store ({exc})") from None
    # EXACT: the folder's real path must be this instance's own, not another instance's folder reached through a link
    # (pre-review fix round, O-F3). A link that stays inside the store but lands on someone else's folder is refused.
    if os.path.realpath(d) != os.path.join(os.path.realpath(root), instance_id, "dsd"):
        raise DsdRefused(f"refused: {instance_id}'s DSD folder is a link to somewhere other than its own folder")
    return d


def _iso(epoch: Any) -> Optional[str]:
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(epoch)))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _title_of(markdown: str) -> str:
    for line in (markdown or "").splitlines():
        t = line.strip()
        if t.startswith("#"):
            return t.lstrip("#").strip()[:200]
    for line in (markdown or "").splitlines():
        if line.strip():
            return line.strip()[:200]
    return ""


def _decision_name(dec: Any) -> str:
    return (f"{dec.id} · {dec.question}".strip() if getattr(dec, "question", "") else dec.id)


_NONE_RECO_RE = re.compile(r"^\s*(none|no recommendation|no preference|n/a|not applicable|nothing)\s*\.?\s*(:.*)?$", re.I | re.S)


_NONE_WORD_RE = re.compile(r"^\s*(none|no recommendation|no preference|n/a|not applicable|nothing)\s*\.?\s*[-\u2013\u2014,]", re.I)


def _none_recommendation(reco_text: str, labels: List[str]) -> Optional[str]:
    """``"ok"`` when the line explicitly says there is NO recommendation (``None``, ``None: this is your judgment``: the word alone, or
    the word followed by a colon) AND no option label starts with that word; ``"ambiguous"`` when it reads that way but an option label
    starts with the word (``None (keep as is)`` as an option: which one is meant?); ``None`` when it is not a none-line at all (``None of
    the above; do Maybe`` is not: it is refused as a recommendation that matches no option)."""
    m = _NONE_RECO_RE.match(reco_text or "")
    if not m:
        return None
    word = m.group(1).lower()
    if any(str(l).strip().lower().startswith(word) for l in labels):
        return "ambiguous"
    return "ok"


def check_markdown(markdown: Any) -> List[str]:
    """Every reason this DSD must be refused at submit, each naming the decision; ``[]`` = fine. Pure."""
    if not isinstance(markdown, str) or not markdown.strip():
        return ["the markdown is empty"]
    if len(markdown.encode("utf-8")) > MAX_MARKDOWN_BYTES:
        return [f"the markdown is {len(markdown.encode('utf-8')) // 1024} KB, over the {MAX_MARKDOWN_BYTES // 1024} KB limit"]
    doc = parse_dsd(markdown)
    if not doc.decisions:
        return [f"it parsed to zero decisions ({doc.parse_error or 'no `### D<n>` decision headings found'})"]
    problems: List[str] = []
    for dec in doc.decisions:
        name = _decision_name(dec)
        if not dec.options:
            problems.append(f"{name}: parsed ZERO options (the options table header must be exactly "
                            f"`| Option | Tradeoffs |`, followed by a `|---|---|` row)")
        if dec.slot_line is None:
            problems.append(f"{name}: has no `Your call:` answer slot, so it could never be answered")
        if dec.reco_text and dec.recommendation_source != "reco-line":
            none = _none_recommendation(dec.reco_text, [o.label for o in dec.options])
            if none == "ok":
                pass
            elif none == "ambiguous":
                problems.append(f"{name}: its recommendation {dec.reco_text!r} is ambiguous: it reads as 'no recommendation' but an option "
                                f"label starts with that word; say which option you recommend by its exact label")
            elif re.match(_NONE_WORD_RE, dec.reco_text):
                problems.append(f"{name}: its recommendation {dec.reco_text!r} is a 'none' line with a dash or a comma after the word, which is not an accepted "
                                f"form. To say there is no recommendation write `None` alone, or `None: <reason>` (a colon after the word; "
                                f"a dash or a comma is not accepted); otherwise repeat one option's label exactly")
            else:
                problems.append(f"{name}: its recommendation {dec.reco_text!r} matches no option label")
    verdict = validate_dsd(doc)
    if not verdict.ok:
        for m in verdict.missing:
            problems.append(f"{m['heading']}: missing {m['label']}")
    return problems


def _refuse_problems(problems: List[str]) -> DsdRefused:
    return DsdRefused("REFUSED (nothing was filed): " + "; ".join(problems))


# --------------------------------------------------------------------------- send

@register("send")
def send(instance_id: str, markdown: Any, *, root: Optional[str] = None, clock: Callable[[], float] = time.time,
         doc_roots: Optional[List[str]] = None, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """File ``markdown`` as a decision for the operator, as ``instance_id``. Returns ``{ok, dsd_id, record_path, decisions}``;
    raises :class:`DsdRefused` (naming the decision) when it must not be filed."""
    root = _root(root)
    _own_dir(root, instance_id)                       # the id and the folder are checked BEFORE anything is parsed or written
    problems = check_markdown(markdown)
    if problems:
        raise _refuse_problems(problems)
    from ..hub.mcp import DSDStore, ToolError

    store = DSDStore(root, clock=clock, doc_roots=doc_roots)
    try:
        rec = store.persist(instance_id, {"markdown": markdown}, extra=extra)
    except (ToolError, ValueError) as exc:
        raise DsdRefused(f"REFUSED (nothing was filed): {exc}") from None
    n = len(parse_dsd(markdown).decisions)
    out: Dict[str, Any] = {"ok": True, "dsd_id": rec.dsd_id, "record_path": rec.path, "decisions": n}
    if rec.document_warnings:
        out["warnings"] = list(rec.document_warnings)
    return out


# --------------------------------------------------------------------------- list

def default_answers_path(root: str) -> str:
    """FALLBACK only, for a direct call with no machine config: ``<runtime home>/dsd-answers/dsd-answers.lts`` beside the store root.
    Production callers pass ``config.resolved_dsd_answers_corpus_path()`` (see :func:`answers_path_for`)."""
    return os.path.join(os.path.dirname(root), "dsd-answers", "dsd-answers.lts")


def answers_path_for(config: Any) -> str:
    """The answers corpus path a machine config resolves to (its explicit path, else the derived default)."""
    return config.resolved_dsd_answers_corpus_path()


def read_answer_nodes(answers_path: Optional[str], dsd_id: str) -> List[Dict[str, Any]]:
    """The minted per-decision answer nodes of ``dsd_id``, each ``{node_id, props}``, from the ONE shared reader
    (:mod:`gnomon.dsd.answers_reader`: per-DSD file first, the legacy file second). Raises ``AnswersReadError`` when the
    corpus cannot be read."""
    from .answers_reader import read_answers

    return read_answers(str(answers_path or ""), str(dsd_id))["nodes"]


def _summary(raw: Dict[str, Any], answers_path: Optional[str]) -> Dict[str, Any]:
    md = (raw.get("dsd") or {}).get("markdown") if isinstance(raw.get("dsd"), dict) else ""
    word = _STORED_TO_WORD.get(str(raw.get("status", STATUS_PENDING)), str(raw.get("status")))
    row: Dict[str, Any] = {"dsd_id": raw.get("dsd_id"), "title": _title_of(md if isinstance(md, str) else ""),
                           "status": word, "sent": _iso(raw.get("created_epoch"))}
    if word == "answered":
        row["answered"] = _iso(raw.get("answered_epoch"))
        row["answer_node"] = raw.get("root_node_id") or None
        try:
            row["answers"] = [{"decision": n["props"].get("decision-ref"), "chosen": n["props"].get("chosen"),
                               "comment": n["props"].get("comment"),
                               "agreed_with_recommendation": n["props"].get("agreed-with-recommendation"),
                               "node": n["node_id"]} for n in read_answer_nodes(answers_path, str(raw.get("dsd_id")))]
        except Exception as exc:  # noqa: BLE001 - an unreadable corpus is SAID, never an empty list
            row["answers"] = None
            row["answers_error"] = f"{exc.__class__.__name__}: {exc}"
    if word == "withdrawn":
        w = raw.get("withdrawn") if isinstance(raw.get("withdrawn"), dict) else {}
        row.update(withdrawn=_iso(w.get("at_epoch")), reason=w.get("reason"))
    if word == "replaced":
        w = raw.get("replaced") if isinstance(raw.get("replaced"), dict) else {}
        row.update(replaced=_iso(w.get("at_epoch")), replaced_by=w.get("by_dsd_id"), reason=w.get("reason"))
    if raw.get("replaces"):
        row["replaces"] = raw.get("replaces")
    if str(raw.get("status")) == STATUS_UNFINISHED:
        row["note"] = (f"an UNFINISHED replacement of {raw.get('replaces')}: it was filed but the swap never completed, so the operator "
                       f"was never shown it and {raw.get('replaces')} is still the one waiting. Retry replace_dsd on {raw.get('replaces')} "
                       f"(this one is discarded then), or discard it now with withdraw_dsd")
    ov = raw.get("answer_overrode")
    if isinstance(ov, dict):
        row["note"] = (f"answered after you {ov.get('was')} it (your {ov.get('was')} reported ok first); the answer stands"
                       + (f"; your replacement {(ov.get('mark') or {}).get('by_dsd_id')} was superseded and will not be offered"
                          if ov.get("was") == "replaced" else ""))
    return row


@register("list_mine")
def list_mine(instance_id: str, status: Optional[str] = None, *, root: Optional[str] = None,
              answers_path: Optional[str] = None) -> Dict[str, Any]:
    """The caller's OWN DSDs, newest first: id, title, status (pending | answered | withdrawn | replaced), sent time and, for an
    answered one, the per-decision answers. ``status`` filters. The stored value ``pending-human-review`` is never renamed on disk."""
    if status is not None and status not in STATUS_WORDS:
        raise DsdRefused(f"unknown status {status!r}; use one of {', '.join(STATUS_WORDS)} (or leave it out for all)")
    root = _root(root)
    d = _own_dir(root, instance_id)
    ap = answers_path if answers_path is not None else default_answers_path(root)
    rows: List[Dict[str, Any]] = []
    for path in glob.glob(os.path.join(d, "*.json")):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict) and raw.get("instance_id") == instance_id:       # a record with no instance_id is not the caller's
            raw = effective_raw(path, raw)
            raw.setdefault("dsd_id", os.path.splitext(os.path.basename(path))[0])
            rows.append((float(raw.get("created_epoch") or 0.0), _summary(raw, ap)))
    rows.sort(key=lambda r: (r[0], str(r[1].get("dsd_id"))), reverse=True)
    out = [r for _t, r in rows if status is None or r["status"] == status]
    return {"ok": True, "count": len(out), "dsds": out}


# --------------------------------------------------------------------------- withdraw / replace

#: (instance, dsd_id, op) -> time of the first, refused, call: the confirm-twice state for "the operator has it open".
_ARMED: Dict[Any, float] = {}


def _operator_has_open(dsd_id: str, now: float) -> bool:
    """The dashboard's presence signal: a review beacon for ``dsd_id`` written in the last :data:`PRESENCE_FRESH_SECONDS`."""
    try:
        from ..harness import operator_activity as oa

        rows = oa.read_dsd_reviews(now)
    except Exception:  # noqa: BLE001 - presence is advisory; unreadable = not open
        return False
    return any(r["dsd_id"] == dsd_id and r["seconds_ago"] <= PRESENCE_FRESH_SECONDS for r in (rows or []))


def _try_reconcile(root: str, path: str) -> None:
    """Best effort, bounded: fold a record's answer sidecar into the record under both locks. Never raises, never waits long."""
    try:
        if not os.path.exists(sidecar_path(path)):
            return
        with status_lock(root, timeout=0.5) as got_s:
            if got_s:
                with write_lock(root, timeout=0.5) as got_w:
                    if got_w:
                        DsdInbox(root).reconcile(path)
    except OSError:
        pass


def _read_own(path: str, instance_id: str, dsd_id: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise DsdRefused(f"could not read {dsd_id}: {exc}") from None
    if not isinstance(raw, dict) or raw.get("instance_id") != instance_id:
        raise DsdRefused(f"you have no DSD {dsd_id}")
    return effective_raw(path, raw)      # what a reader sees: an answer sidecar applied, an unconfirmed replacement UNFINISHED


def _require_pending(raw: Dict[str, Any], dsd_id: str, op: str) -> None:
    stored = str(raw.get("status", STATUS_PENDING))
    if stored == STATUS_ANSWERED:
        raise DsdRefused(f"{dsd_id} is already answered; an answered DSD can never be "
                         + ("withdrawn" if op == "withdraw" else "replaced"))
    if stored == STATUS_UNFINISHED and op == "withdraw":
        return                                            # discarding an unfinished replacement is allowed
    if stored != STATUS_PENDING:
        raise DsdRefused(f"{dsd_id} is already {_STORED_TO_WORD.get(stored, stored)}")


def _mark(instance_id: str, dsd_id: str, op: str, root: str, reason: str, confirm: bool, now: float,
          presence: Optional[Callable[[str, float], bool]], build: Callable[[], Dict[str, Any]],
          write_marks: Callable[[Dict[str, Any], Dict[str, Any]], None],
          after: Optional[Callable[[Dict[str, Any]], None]] = None) -> Dict[str, Any]:
    """Shared body of withdraw and replace. Three steps, so the status lock is held only for the flip, never while a new DSD is filed
    and its documents hashed (O-F9): (1) NO lock: the record is the caller's and pending, and the operator-presence rule;
    (2) NO lock: ``build()`` (for replace: file the new DSD); (3) the status lock: re-read, and only if the record is STILL pending put
    the marks on it. If an answer landed in between, the answer wins: a replacement filed in step 2 is marked superseded-by-answer
    (kept, never offered) and the call is refused saying so."""
    if not isinstance(dsd_id, str) or not NOTICE_DSD_ID_RE.match(dsd_id):
        raise DsdRefused(f"{dsd_id!r} is not a DSD id (they look like dsd-1a2b3c4d5e6)")
    d = _own_dir(root, instance_id)
    path = os.path.join(d, dsd_id + ".json")
    if not os.path.isfile(path):
        raise DsdRefused(f"you have no DSD {dsd_id}; list yours with my_dsds()")
    raw = _read_own(path, instance_id, dsd_id)
    try:
        _require_pending(raw, dsd_id, op)
    except DsdRefused:
        _try_reconcile(root, path)                  # an answer recorded without the locks is folded into the record when we can
        raise
    key = (instance_id, dsd_id, op)
    check = presence if presence is not None else _operator_has_open
    if check(dsd_id, now):
        armed = _ARMED.get(key)
        if not (confirm and armed is not None and now - armed <= CONFIRM_WINDOW_SECONDS):
            _ARMED[key] = now
            raise DsdRefused(f"the operator has {dsd_id} open right now. Nothing was changed. To {op} it anyway, call "
                             f"{'withdraw_dsd' if op == 'withdraw' else 'replace_dsd'} again within "
                             f"{int(CONFIRM_WINDOW_SECONDS // 60)} minutes with confirm=true")
    _ARMED.pop(key, None)
    built = build()
    with status_lock(root):
        with write_lock(root):
            # NOTHING slow in here (no filing, hashing or network): one re-read and one write, the same lock the answer flip takes.
            raw = _read_own(path, instance_id, dsd_id)          # IMMEDIATELY before the write; sees an answer recorded without the locks
            try:
                _require_pending(raw, dsd_id, op)
            except DsdRefused:
                DsdInbox(root).reconcile(path)                  # fold a sidecar answer into the record (no stale mark is left)
                if op == "replace" and built.get("dsd_id"):
                    _supersede_replacement(d, str(built["dsd_id"]), dsd_id, now)
                    raise DsdRefused(f"{dsd_id} was answered while you were replacing it, so the answer stands. Your replacement "
                                     f"{built['dsd_id']} was filed but is marked superseded-by-answer and will not be offered") from None
                raise
            write_marks(raw, built)                              # for a replace this IS the single flip: the old record's mark
            DsdInbox._atomic_write_json(path, raw)
            if os.path.exists(sidecar_path(path)):               # an answer landed (unlocked) while we wrote: it wins, fold it in now
                DsdInbox(root).reconcile(path)
    if after is not None:
        after(built)
    return built


def _discard_unfinished_siblings(folder: str, old_id: str, *, keep: str, now: float) -> None:
    """A retry of replace discards the earlier unfinished replacements of the same DSD (kept, marked withdrawn)."""
    for p in glob.glob(os.path.join(folder, "*.json")):
        try:
            with open(p, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            continue
        if (isinstance(raw, dict) and raw.get("replaces") == old_id and raw.get("dsd_id") != keep
                and effective_raw(p, raw).get("status") == STATUS_UNFINISHED):
            raw["status"] = STATUS_WITHDRAWN
            raw["withdrawn"] = {"reason": "discarded: an earlier replacement attempt that never completed", "at_epoch": now,
                                "by": raw.get("instance_id")}
            DsdInbox._atomic_write_json(p, raw)


def _supersede_replacement(folder: str, new_id: str, answered_id: str, now: float) -> None:
    from .inbox import STATUS_SUPERSEDED

    p = os.path.join(folder, new_id + ".json")
    try:
        with open(p, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        raw["status"] = STATUS_SUPERSEDED
        raw["superseded"] = {"by_answer_to": answered_id, "at_epoch": now}
        DsdInbox._atomic_write_json(p, raw)
    except (OSError, ValueError):
        pass


@register("withdraw")
def withdraw(instance_id: str, dsd_id: str, reason: Any, *, root: Optional[str] = None, confirm: bool = False,
             now: Optional[float] = None, presence: Optional[Callable[[str, float], bool]] = None) -> Dict[str, Any]:
    """Withdraw one of your own PENDING DSDs. The record is KEPT and marked (status, reason, time); the DSD tab shows it as
    withdrawn. Refused for an answered, already withdrawn or replaced DSD, and while the operator has it open unless confirmed."""
    if not isinstance(reason, str) or not reason.strip():
        raise DsdRefused("give a reason (a short plain sentence); it is kept on the record")
    root = _root(root)
    now = time.time() if now is None else now
    reason = reason.strip()[:MAX_REASON_CHARS]

    def build() -> Dict[str, Any]:
        return {"ok": True, "dsd_id": dsd_id, "status": "withdrawn", "reason": reason}

    def marks(raw: Dict[str, Any], _b: Dict[str, Any]) -> None:
        raw["status"] = STATUS_WITHDRAWN
        raw["withdrawn"] = {"reason": reason, "at_epoch": now, "by": instance_id}

    return _mark(instance_id, dsd_id, "withdraw", root, reason, bool(confirm), now, presence, build, marks)


@register("replace")
def replace(instance_id: str, dsd_id: str, markdown: Any, *, root: Optional[str] = None, confirm: bool = False,
            now: Optional[float] = None, presence: Optional[Callable[[str, float], bool]] = None,
            clock: Callable[[], float] = time.time, doc_roots: Optional[List[str]] = None) -> Dict[str, Any]:
    """Replace one of your own PENDING DSDs with a corrected one. The new markdown runs the SAME submit checks as ``send`` first
    (a refusal leaves the old DSD untouched); the new record names the one it replaces; the old record is KEPT, marked replaced."""
    root = _root(root)
    now = time.time() if now is None else now
    problems = check_markdown(markdown)
    if problems:
        raise _refuse_problems(problems)

    def build() -> Dict[str, Any]:
        # Filed carrying ``replaces``; every reader treats it as an UNFINISHED replacement (never offered) until the OLD record names it as
        # replaced-by: that one write (the old record's mark) is the single flip (E1).
        new = send(instance_id, markdown, root=root, clock=clock, doc_roots=doc_roots, extra={"replaces": dsd_id})
        return {"ok": True, "dsd_id": new["dsd_id"], "replaced": dsd_id, "record_path": new["record_path"],
                "decisions": new["decisions"]}

    def marks(raw: Dict[str, Any], built: Dict[str, Any]) -> None:
        raw["status"] = STATUS_REPLACED
        raw["replaced"] = {"by_dsd_id": built["dsd_id"], "at_epoch": now, "by": instance_id}

    def after(built: Dict[str, Any]) -> None:
        _discard_unfinished_siblings(os.path.dirname(built["record_path"]), dsd_id, keep=built["dsd_id"], now=now)

    return _mark(instance_id, dsd_id, "replace", root, "", bool(confirm), now, presence, build, marks, after)

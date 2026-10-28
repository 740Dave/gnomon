"""DSD-decision SUBJECT resolution for the X13 converter (S1 dry run; ``rule-dsd``, I9 ``precedence``).

A decision belongs to the project of its SUBJECT, not of the role that happened to author it. This module is the
pure, read-only, workspace-NEUTRAL half of that rule: it takes text plus lookup tables the engine built from the
state it already read, and returns which rule fired and what it resolved to. Every deployment-specific literal
(which words mean which project) arrives as DATA in the converter dir's ``subject-rules.json``; nothing here
names a product, a corpus or a workspace.

PRECEDENCE. The decision's words are split into SCOPES, narrowest first; inside a scope the evidence TIERS apply
in order. The first (scope, tier) that yields unambiguous evidence decides; the author is the LAST resort.

  0 override           overrides/dsd.json names the decision (handled by the engine, before this module).
  scope own-subject    the decision's subject line: its question + its theme (what it is ABOUT)
  scope own-answer     what was chosen and said: chosen + comment + the decision's own section of the DSD document
  scope dsd            the DSD the decision was filed in: the document's headings + every sibling decision's
                       subject line ("the topic it was filed against")
  tier  item           the scope names a backlog item / product node the converter already placed
                       (``item_projects``: id -> project). Hard evidence.
  tier  corpus         the scope names a node that lives in a corpus owned by a workspace
                       (``corpus_workspace``: id -> workspace). Hard evidence, workspace-level.
  tier  term           the scope matches a term of ``subject-rules.json`` (word -> project). Heuristic evidence.
  rule  dsd-siblings   (R6, between the scopes and the author; applied by the engine in a second pass, see
                       :func:`sibling_workspace`) the decision resolved to nothing, but EVERY sibling decision of its DSD
                       that was filed by a non-fallback rule (a subject rule or an override) is in ONE workspace: file it
                       there, ``<ws>/unassigned`` (workspace only: a sibling's project says nothing about this decision's).
  last  author-fallback  the authoring role's home workspace + ``<ws>/unassigned`` (decided by the engine, not here).

WHAT a scope may name. A heuristic WORD is trusted to name a PROJECT only in the ``own-subject`` scope; in ``own-answer`` it
is evidence for the WORKSPACE only (the project stays ``<ws>/unassigned``): an answer's prose mentions other products in
passing, and filing into a real project on that would be a confident misfile. Hard evidence (an item's project) names its
project in either ``own-*`` scope. The ``dsd`` scope is a SIBLING's or the document's subject, not this decision's: whatever
it finds (item, corpus node or word) names the WORKSPACE only.

The rule label is ``<scope>:<tier>`` (e.g. ``own-subject:term``, ``own-answer:item``, ``dsd:term``), ``dsd-siblings``
or ``author-fallback``.

WITHIN one (scope, tier) the evidence is reduced with the ruling-1 wording (I9 precedence tier 1):
  * exactly one project             -> that project;
  * several projects, ONE workspace -> ``<ws>/unassigned`` (the decision stays visible in that workspace);
  * several WORKSPACES              -> REFUSED (requires a split or an override), but ONLY for the hard tiers (item,
                                       corpus) of an ``own-*`` scope. Everywhere else a cross-workspace span is just
                                       AMBIGUOUS evidence: that (scope, tier) cannot decide and the next is tried (a
                                       word list, or a sweep of the whole DSD, must never refuse a decision).
  * the HARD tiers are pooled: inside one ``own-*`` scope an item of workspace A and a node of workspace B's corpus
    are the same conflict whichever tier each lies in (F-1 of audit-x13-convmap, 2026-10-01); the label then reads
    ``<scope>:item+corpus``. Across SCOPES the narrowest still wins (own-subject over own-answer): a decision's subject
    line is what it is about, and an answer's passing mention of another workspace's node does not override it.
The reserved ``<ws>/unassigned`` project is itself a project: two items both in ``gnomon/unassigned`` give
``gnomon/unassigned``; an item in ``gnomon/unassigned`` and one in ``gnomon/x`` span two projects.

g54(c): ASCII-only patterns, ``fullmatch`` / ``\\Z``, never ``$``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

SUBJECT_RULES_FILE = "subject-rules.json"

#: A node id as it appears in prose: upper-case ASCII words joined by hyphens, at least two parts, not glued to
#: a longer token (a non-ASCII character glued to it counts as part of that longer token). ASCII only ([A-Z0-9], never
#: \\w / \\d), so a fullwidth or Arabic-Indic digit is not an id character.
ID_REF = re.compile(r"(?<![A-Za-z0-9_\-\u0080-\U0010ffff])[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)+(?![A-Za-z0-9_\-\u0080-\U0010ffff])", re.ASCII)

#: A qualified project id, ``<workspace>/<slug>``.
_PROJECT = re.compile(r"[a-z][a-z0-9-]*/[a-z][a-z0-9-]*")

#: the three evidence tiers, in precedence order.
TIERS = ("item", "corpus", "term")
#: scopes in which a heuristic term may name a PROJECT (elsewhere it names only the workspace).
PROJECT_LEVEL_TERM_SCOPES = ("own-subject",)
#: the scope whose evidence (of any tier) names only a WORKSPACE: it is another decision's or the document's subject.
WORKSPACE_ONLY_SCOPES = ("dsd",)
#: tiers whose own-scope cross-workspace span REFUSES (hard evidence); the rest are ambiguous and fall through.
HARD_TIERS = ("item", "corpus")

RESOLVED, REFUSED, NONE = "resolved", "refused", "none"


class SubjectRulesError(ValueError):
    """A subject-rules file that is unreadable or malformed: a loud stop, never a silently empty table."""


@dataclass(frozen=True)
class Term:
    name: str
    project: str
    pattern: "re.Pattern[str]"


def ws_of(project: str) -> str:
    return project.split("/", 1)[0]


def load_terms(converter_data: str, valid_projects: Set[str]) -> Tuple[List[Term], List[str]]:
    """Read ``<converter_data>/subject-rules.json`` -> ``(terms, errors)``. An ABSENT file is a legal, empty table
    (the deployment supplies no word list; tiers 1, 2 and 5 still apply). A present file that is malformed, or names a
    project that is not seeded, yields errors the engine turns into FATAL (the dry run fails; it never guesses)."""
    path = os.path.join(converter_data, SUBJECT_RULES_FILE)
    if not os.path.isfile(path):
        return [], []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except (OSError, ValueError) as exc:
        return [], [f"{SUBJECT_RULES_FILE}: unreadable: {exc}"]
    if not isinstance(obj, dict):
        return [], [f"{SUBJECT_RULES_FILE}: top level must be an object"]
    errors: List[str] = []
    extra = sorted(k for k in obj if k != "terms" and not k.startswith("_doc"))
    if extra:
        errors.append(f"{SUBJECT_RULES_FILE}: unknown top-level keys {extra} (allowed: terms, _doc*)")
    raw = obj.get("terms", [])
    if not isinstance(raw, list):
        return [], errors + [f"{SUBJECT_RULES_FILE}: 'terms' must be a list"]
    terms: List[Term] = []
    seen: Set[str] = set()
    for i, t in enumerate(raw):
        where = f"{SUBJECT_RULES_FILE}: terms[{i}]"
        if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not isinstance(t.get("project"), str) or not isinstance(t.get("pattern"), str):
            errors.append(f"{where}: needs string keys name, project, pattern")
            continue
        if t["name"] in seen:
            errors.append(f"{where}: duplicate name {t['name']!r}")
        seen.add(t["name"])
        if not _PROJECT.fullmatch(t["project"]):
            errors.append(f"{where} ({t['name']}): project {t['project']!r} is not <workspace>/<slug>")
            continue
        if t["project"] not in valid_projects:
            errors.append(f"{where} ({t['name']}): project {t['project']!r} is not a seeded project")
            continue
        try:
            rx = re.compile(t["pattern"], re.ASCII | (0 if t.get("case-sensitive") else re.IGNORECASE))
        except re.error as exc:
            errors.append(f"{where} ({t['name']}): pattern does not compile: {exc}")
            continue
        terms.append(Term(t["name"], t["project"], rx))
    return terms, errors


@dataclass(frozen=True)
class Resolution:
    status: str                       # RESOLVED | REFUSED | NONE
    rule: str = ""                    # the rule label that fired ("subject-item", "dsd-subject-term", ...)
    project: str = ""                 # the resolved project (RESOLVED)
    span: bool = False                # several projects in one workspace collapsed to <ws>/unassigned
    workspace_only: bool = False      # evidence that names the workspace, not a project (see WHAT a scope may name)
    workspaces: Tuple[str, ...] = ()  # the workspaces involved (REFUSED lists all of them)
    evidence: Tuple[str, ...] = ()    # tokens that produced it (ids / term names), sorted, for the report


def _reduce(found: Mapping[str, str]) -> Tuple[str, str, bool, Tuple[str, ...]]:
    """``token -> project`` -> (kind, project, span, workspaces); kind is ``project``, ``span`` or ``cross``."""
    projects = set(found.values())
    wss = tuple(sorted({ws_of(p) for p in projects}))
    if len(wss) > 1:
        return "cross", "", False, wss
    if len(projects) == 1:
        return "project", next(iter(projects)), False, wss
    return "span", f"{wss[0]}/unassigned", True, wss


def gather(text: str, item_projects: Mapping[str, str], corpus_workspace: Mapping[str, Optional[str]],
           terms: Sequence[Term], exclude: Set[str] = frozenset()) -> Dict[str, Dict[str, str]]:
    """The evidence in ``text``: per tier, ``token -> project``. ``exclude`` drops ids that name the decision itself."""
    out: Dict[str, Dict[str, str]] = {t: {} for t in TIERS}
    for tok in ID_REF.findall(text):
        if tok in exclude:
            continue
        if tok in item_projects:
            out["item"][tok] = item_projects[tok]
        elif corpus_workspace.get(tok):
            out["corpus"][tok] = f"{corpus_workspace[tok]}/unassigned"
    for term in terms:
        if term.pattern.search(text):
            out["term"][term.name] = term.project
    return out


#: (scope name, text) in precedence order. ``own-*`` scopes may REFUSE on hard evidence; ``dsd`` never does.
Scope = Tuple[str, str]


def resolve(scopes: Sequence[Scope], item_projects: Mapping[str, str], corpus_workspace: Mapping[str, Optional[str]],
            terms: Sequence[Term], exclude: Set[str] = frozenset()) -> Resolution:
    """Apply the precedence (see the module docstring). ``NONE`` means: fall back to the author (the engine's call)."""
    for scope, text in scopes:
        ev = gather(text, item_projects, corpus_workspace, terms, exclude)
        if scope.startswith("own-"):
            # F-1: hard evidence is pooled across its tiers; a conflict between workspaces refuses wherever each id lies.
            hard = {tok: proj for tier in HARD_TIERS for tok, proj in ev[tier].items()}
            hard_ws = tuple(sorted({ws_of(p) for p in hard.values()}))
            if len(hard_ws) > 1:
                label = f"{scope}:" + "+".join(t for t in HARD_TIERS if ev[t])
                return Resolution(REFUSED, label, workspaces=hard_ws, evidence=tuple(sorted(hard)))
        for tier in TIERS:
            found = ev[tier]
            if not found:
                continue
            kind, project, span, wss = _reduce(found)
            label = f"{scope}:{tier}"
            if kind == "cross":
                if scope.startswith("own-") and tier in HARD_TIERS:
                    return Resolution(REFUSED, label, workspaces=wss, evidence=tuple(sorted(found)))
                continue                      # ambiguous: this (scope, tier) cannot decide; try the next
            ws_only = False
            degrade = scope in WORKSPACE_ONLY_SCOPES or (tier == "term" and scope not in PROJECT_LEVEL_TERM_SCOPES)
            if degrade and not project.endswith("/unassigned"):
                project, ws_only = f"{wss[0]}/unassigned", True
            return Resolution(RESOLVED, label, project, span=span, workspace_only=ws_only, workspaces=wss, evidence=tuple(sorted(found)))
    return Resolution(NONE)


#: the rule label of the sibling rule (R6).
SIBLINGS_RULE = "dsd-siblings"


def sibling_workspace(sibling_workspaces: Mapping[str, str]) -> Optional[str]:
    """R6: ``sibling decision id -> workspace`` (ONLY siblings filed by a non-fallback rule: a subject rule or an override;
    refused, untagged and author-fallback siblings are not evidence, or the rule would feed on itself) -> the one workspace
    they ALL agree on, else ``None`` (no sibling, or siblings in several workspaces: the author stays the last resort).
    Pure and deterministic; the caller records the rule label :data:`SIBLINGS_RULE` and the sibling ids as evidence."""
    wss = {w for w in sibling_workspaces.values() if w}
    return next(iter(wss)) if len(wss) == 1 else None

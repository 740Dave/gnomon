"""The DSD document model — parse, validate (context guard), write-back.

CLEAN-ROOM reimplementation of earlier tooling's canvas-free DSD grammar
reconciled by node id against that tooling's DSD viewer spec and the Gnomon DSD contract. Nothing here imports earlier tooling — the
specs are the behavioural AUTHORITY, not a dependency.

The grammar (a FIXED parse target, authored by the decision-support-doc skill)::

    ## Theme <label>                 a level-2 heading grouping the decisions
    ### D<n> · <question>            a level-3 heading; text matches ^D<n>
                                     (may carry a ⛔ blocker glyph / a [multi] tag)
    **What this is:** ...            REQUIRED decision-context gloss (the guard)
    **Why it matters:** ...          REQUIRED stakes line (the guard)
    | Option | Tradeoffs |           the option set (FIRST such table wins)
    |---|---|
    | **Lean row** | ... |           a bolded Option cell => recommended row
    | Other option | ... |
    **My recommendation:** <opt>     the recommendation (matched by equality)
    **Your call:** <slot>            the write-back target
    **Comments:** <text>             optional; re-written on re-answer
 **Documents:** a.md; ~/b/c.pdf optional, at DSD level (before the
                                     first decision) and/or inside a decision block;
                                     paths separated by ';' (absolute or ~-relative)

RECOMMENDATION PRECEDENCE: (1) the ``**My recommendation:**``
line's option, matched to a row label by normalized equality; (2) else the BOLDED
table row; (3) else none (the decision is flagged "no recommendation").

⛔ NO PRE-SELECTION (operator, 2026-09-29): the resolved recommendation is shown as a
"recommended" badge ONLY — the form checks no radio, so the human's assent to a
recommendation is an explicit, captured act (``agreed-with-recommendation`` on the
minted answer node). The fields were renamed ``preselect_*`` -> ``recommended_id``
/ ``recommendation_source`` so nothing downstream can mistake the recommendation
for an answer.

CONTEXT GUARD (context-guard, operator 2026-08-17): a DSD with any
decision missing a non-empty What-this-is OR Why-it-matters is REFUSED. The bar is
STRUCTURAL PRESENCE + NON-EMPTY CONTENT, nothing more — boilerplate that says
nothing structurally passes, because detecting vacuity is a judgment a regex loses
and the cost of losing it is a FALSE REFUSAL that trains authors to route around
the guard.

WRITE-BACK (save-behavior): each answered decision's slot
line becomes ``**Your call:** <chosen>``; a non-empty comment adds/replaces an
immediately-following ``**Comments:** <comment>`` line; an unanswered decision's
slot is left byte-for-byte as authored; every other line is preserved. Edits apply
bottom-to-top so a comment insert never shifts an earlier decision's slot index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


# ── grammar ────────────────────────────────────────────────────────────────
# A decision heading: a level-3 heading whose text starts with D<n>.
_DECISION_RE = re.compile(r"^\s*#{3}\s+(D(\d+)\b.*?)\s*$")
# Any ATX heading (a decision block ends at the next heading of any level).
_ANY_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")
# A level-2 `## …` Theme heading. `##\s+` requires whitespace after exactly two
# hashes, so a `### D<n>` decision heading never matches it.
_THEME_RE = re.compile(r"^\s*##\s+(.*?)\s*$")
# A `**My recommendation:**` (or plural) line; captures the trailing option text.
_RECO_RE = re.compile(
    r"^\s*\*{0,2}\s*My\s+recommendations?\s*:?\s*\*{0,2}\s*(.*?)\s*$", re.IGNORECASE)
# A `Your call:` slot line — tolerant of `**Your call:** X`, `**Your call: X**`,
# and a leading `- ` bullet.
_YOURCALL_RE = re.compile(
    r"^\s*(?:-\s*)?\*{0,2}\s*Your\s+call\s*:\s*(.*?)\s*\*{0,2}\s*$", re.IGNORECASE)
# An existing `Comments:` line (re-written on re-answer, never duplicated).
_COMMENTS_RE = re.compile(
    r"^\s*(?:-\s*)?\*{0,2}\s*Comments\s*:\s*(.*?)\s*\*{0,2}\s*$", re.IGNORECASE)
# The idempotent answered marker (replaced in place on re-save).
_MARKER_RE = re.compile(r"^\s*<!--\s*dsd-status:\s*answered\b.*-->\s*$", re.IGNORECASE)
# A `[multi]` / `[multi-select]` tag on a decision heading (parsed, gated OFF v1).
_MULTI_TAG_RE = re.compile(r"\[multi(?:-select)?\]", re.IGNORECASE)
# the optional `**Documents:** path; path` line (DSD level and per
# decision). STRICT on purpose: only the bold-label form matches, so a prose line
# that merely starts "Documents:" in an existing DSD is never reinterpreted.
_DOCUMENTS_RE = re.compile(r"^\s*\*\*Documents:\*\*[ \t]*(.*?)\s*$")
#: a hard cap per Documents scope (DSD level / one decision), so a hostile or
#: runaway line cannot make the hub hash thousands of files.
MAX_DOCS_PER_SCOPE = 20


def parse_documents_line(value: str) -> List[Dict[str, str]]:
    """The `;`-separated paths of one ``**Documents:**`` line -> ``[{path, name}]``
    (``name`` = the basename). Surrounding whitespace/backticks are stripped and empty
    entries dropped. Pure: no filesystem access, no ``~`` expansion (the path is carried
    as authored; resolution happens at persist/open time)."""
    out: List[Dict[str, str]] = []
    for part in (value or "").split(";"):
        p = part.strip().strip("`").strip()
        if not p:
            continue
        name = p.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or p
        out.append({"path": p, "name": name})
    return out

# The label terminator for the context lines is an ALTERNATION anchored right
# after the label (never scanning forward into the prose), covering the three live
# punctuation forms — `**What this is:**`, `**What this is.**`, `**What this is**
# —` — plus an optional parenthetical qualifier and a bare label with the prose on
# the next line (picked up by _collect_paragraph). Assuming a bare colon here once
# silently dropped authored context in the source corpus; the alternation is why.
_LABEL_TAIL = (
    r"(?:\s*\([^)]*\))?"              # optional parenthetical qualifier
    r"(?:"
    r"\s*[:.]\s*\*{0,2}"             #   …is:**  /  …is.**
    r"|\s*\*{1,2}\s*[:.—–-]*"  #   …is** — /  …is**
    r"|\s*[—–-]+"          #   …is —   (unemphasised)
    r")"
    r"\s*(.*?)\s*$"
)
_WHATIS_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}\s*What\s+this\s+is\b" + _LABEL_TAIL, re.IGNORECASE)
_WHY_RE = re.compile(
    r"^\s*(?:[-*]\s+)?\*{0,2}\s*Why\s+it\s+matters\b" + _LABEL_TAIL, re.IGNORECASE)

_QUESTION_SEP = "·"   # · the middot between the D<n> id and the question
_BLOCKER = "⛔"        # ⛔ the gating-decision glyph (preserved, must not break parse)

#: The always-present escape-hatch option the form adds (
#: escape-hatch). Selecting it REQUIRES a comment (see :func:`answer_is_complete`).
NEITHER_NONE_LABEL = "Neither / None — explain"


#: comment-fidelity: the visible mark a line-break run becomes in a folded comment (the pilcrow).
PARAGRAPH_MARK = "\u00b6"
_COMMENT_BREAK_RE = re.compile(r"\s*[\r\n\v\f\x1c\x1d\x1e\x85\u2028\u2029]+\s*")


def fold_comment(text: str) -> str:
    """THE one single-line form of a DSD comment (comment-fidelity), shared by the record's
    ``**Comments:**`` write-back and the answer node's ``comment`` property so the two can never disagree.
    Trims the ends; every run of line breaks / line separators (with the whitespace around it) becomes
    `` \u00b6 `` (a visible paragraph mark); everything else (tabs, quotes, inner spaces, any length) is kept as is.
    The exact text is kept separately in the record (``answer_comments``)."""
    return _COMMENT_BREAK_RE.sub(f" {PARAGRAPH_MARK} ", str(text or "").strip())


def _strip_md(text: str) -> str:
    """Plain text of an inline-markdown cell: drop ``**``/``__``/backticks,
    collapse internal whitespace, trim."""
    s = text.replace("**", "").replace("__", "").replace("`", "")
    return re.sub(r"\s+", " ", s).strip()


def _normalize(text: str) -> str:
    """Match key for recommendation<->option equality: lower-cased, markdown
    stripped, trailing sentence punctuation dropped."""
    return _strip_md(text).lower().rstrip(".,;:!?").strip()


@dataclass
class DsdOption:
    """One option row: its plain LABEL, its Tradeoffs text (shown, not editable),
    whether its Option cell was BOLD (the lean row), and a stable per-decision id
    (the radio value)."""
    option_id: str
    label: str
    tradeoffs: str
    recommended_row: bool = False


@dataclass
class DsdDecision:
    """One parsed ``### D<n>`` decision."""
    id: str
    question: str
    what_this_is: str = ""
    why_it_matters: str = ""
    theme: str = ""
    marker: str = ""                  # the ⛔ blocker glyph, or ""
    options: List[DsdOption] = field(default_factory=list)
    recommended_id: Optional[str] = None          # an option_id, or None (NOT a selection)
    recommendation_source: str = "none"           # "reco-line" | "bold-row" | "none"
    reco_text: str = ""
    multi: bool = False               # [multi] parsed but gated OFF for v1
    heading_line: int = -1
    slot_line: Optional[int] = None   # None => no writable slot (read-only)
    comment_line: Optional[int] = None
    notes: List[str] = field(default_factory=list)
    #: this decision's ``**Documents:**`` entries, ``[{path, name}]``.
    documents: List[Dict[str, str]] = field(default_factory=list)

    def option_by_id(self, option_id: str) -> Optional[DsdOption]:
        for o in self.options:
            if o.option_id == option_id:
                return o
        return None

    def option_by_label(self, label: str) -> Optional[DsdOption]:
        key = _normalize(label)
        for o in self.options:
            if _normalize(o.label) == key:
                return o
        return None


@dataclass
class DecisionAnswer:
    """The human's answer for one decision: the chosen option LABELS (0 or 1 for
    single-select v1) + the free-text comment."""
    labels: List[str] = field(default_factory=list)
    comment: str = ""


class DsdDocument:
    """A parsed DSD: the source lines + the decisions, with a precise, fail-safe
    write-back of an answer map into the slots."""

    def __init__(self, text: str, path: Optional[str] = None) -> None:
        self.path = path
        self.raw = text
        self._lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        self.decisions: List[DsdDecision] = []
        self.parse_error: Optional[str] = None
        #: DSD-level ``**Documents:**`` entries (before the first decision).
        self.documents: List[Dict[str, str]] = []
        self._parse()

    @property
    def lines(self) -> List[str]:
        return list(self._lines)

    @property
    def ok(self) -> bool:
        """True when at least one decision parsed (else the viewer shows the
        explicit 'no decisions parsed' state)."""
        return bool(self.decisions)

    # -- parse ----------------------------------------------------------------

    def _parse(self) -> None:
        lines = self._lines
        n = len(lines)
        heads: List[Tuple[int, str, str, str]] = []   # (idx, id, heading_text, theme)
        cur_theme = ""
        for i, line in enumerate(lines):
            mt = _THEME_RE.match(line)
            if mt and not _DECISION_RE.match(line):
                cur_theme = _strip_md(mt.group(1))
                continue
            m = _DECISION_RE.match(line)
            if m:
                heads.append((i, "D" + m.group(2), m.group(1), cur_theme))
        if not heads:
            self.parse_error = "no D<n> decisions found"
            return
        # DSD-level documents = Documents lines BEFORE the first decision.
        for line in lines[:heads[0][0]]:
            md = _DOCUMENTS_RE.match(line)
            if md:
                self.documents.extend(parse_documents_line(md.group(1)))
        del self.documents[MAX_DOCS_PER_SCOPE:]
        for (idx, dec_id, htext, theme) in heads:
            end = n
            for j in range(idx + 1, n):
                if _ANY_HEADING_RE.match(lines[j]):
                    end = j
                    break
            self.decisions.append(
                self._parse_decision(dec_id, htext, idx, idx + 1, end, theme))

    def _parse_decision(self, dec_id: str, htext: str, head_idx: int,
                        start: int, end: int, theme: str) -> DsdDecision:
        lines = self._lines
        multi = bool(_MULTI_TAG_RE.search(htext))
        clean_h = _MULTI_TAG_RE.sub("", htext).strip()
        marker = _BLOCKER if _BLOCKER in clean_h else ""
        if _QUESTION_SEP in clean_h:
            question = clean_h.split(_QUESTION_SEP, 1)[1].strip()
        else:
            question = re.sub(r"^D\d+\b[\s:.\-⛔]*", "", clean_h).strip()
        dec = DsdDecision(id=dec_id, question=question, multi=multi,
                          theme=theme, marker=marker, heading_line=head_idx)

        # decision context: the REQUIRED What-this-is gloss + Why-it-matters
        # stakes, each a soft-wrapped paragraph.
        i = start
        while i < end:
            line = lines[i]
            if (not dec.what_this_is and _WHATIS_RE.match(line)
                    and "what this is" in line.lower()):
                dec.what_this_is, i = self._collect_paragraph(
                    lines, i, end, _WHATIS_RE.match(line).group(1))
                continue
            if (not dec.why_it_matters and _WHY_RE.match(line)
                    and "why it matters" in line.lower()):
                dec.why_it_matters, i = self._collect_paragraph(
                    lines, i, end, _WHY_RE.match(line).group(1))
                continue
            i += 1

        # this decision's `**Documents:**` line(s).
        for j in range(start, end):
            md = _DOCUMENTS_RE.match(lines[j])
            if md:
                dec.documents.extend(parse_documents_line(md.group(1)))
        del dec.documents[MAX_DOCS_PER_SCOPE:]

        # options: the FIRST `| Option | Tradeoffs |` header + delimiter row.
        i = start
        while i < end:
            if self._is_options_header(lines[i]) and i + 1 < end \
                    and self._is_delimiter(lines[i + 1]):
                i += 2
                while i < end and "|" in lines[i] and lines[i].strip():
                    opt = self._parse_option_row(dec_id, len(dec.options), lines[i])
                    if opt is not None:
                        dec.options.append(opt)
                    i += 1
                break
            i += 1
        if not dec.options:
            dec.notes.append("no options table parsed")

        # recommendation line + your-call slot + an existing comments line.
        for j in range(start, end):
            mreco = _RECO_RE.match(lines[j])
            if mreco and not dec.reco_text and self._looks_like_reco(lines[j]):
                dec.reco_text = _strip_md(mreco.group(1))
                continue
            myc = _YOURCALL_RE.match(lines[j])
            if myc and dec.slot_line is None:
                dec.slot_line = j
                if j + 1 < end and _COMMENTS_RE.match(lines[j + 1]):
                    dec.comment_line = j + 1
                continue
        if dec.slot_line is None:
            dec.notes.append("no writable slot")

        self._resolve_recommendation(dec)
        return dec

    @staticmethod
    def _collect_paragraph(lines: List[str], i: int, end: int,
                           first_text: str) -> Tuple[str, int]:
        """Collect a soft-wrapped context paragraph starting at lines[i] (whose
        label-stripped remainder is *first_text*), consuming following lines until
        a blank line, a table row, another labelled line, or a heading."""
        parts: List[str] = [first_text] if first_text else []
        j = i + 1
        while j < end:
            s = lines[j]
            if (not s.strip() or "|" in s or _ANY_HEADING_RE.match(s)
                    or _WHATIS_RE.match(s) or _WHY_RE.match(s)
                    or _YOURCALL_RE.match(s) or _COMMENTS_RE.match(s)
                    or _DOCUMENTS_RE.match(s)
                    or (_RECO_RE.match(s) and DsdDocument._looks_like_reco(s))):
                break
            parts.append(s.strip())
            j += 1
        return _strip_md(" ".join(p for p in parts if p)).strip(), j

    @staticmethod
    def _looks_like_reco(line: str) -> bool:
        """Guard the permissive _RECO_RE to the real recommendation line — the
        token 'recommendation' must actually be present."""
        return "recommendation" in line.lower()

    @staticmethod
    def _is_options_header(line: str) -> bool:
        cells = _split_row(line)
        if len(cells) != 2:
            return False
        return _normalize(cells[0]) == "option" and _normalize(cells[1]) == "tradeoffs"

    @staticmethod
    def _is_delimiter(line: str) -> bool:
        cells = _split_row(line)
        return bool(cells) and all(
            re.fullmatch(r":?-{1,}:?", c.strip()) for c in cells if c.strip() != "")

    @staticmethod
    def _parse_option_row(dec_id: str, idx: int, line: str) -> Optional[DsdOption]:
        cells = _split_row(line)
        if len(cells) < 2:
            return None
        raw_opt = cells[0].strip()
        if raw_opt == "":
            return None
        recommended = raw_opt.startswith("**") and raw_opt.endswith("**")
        return DsdOption(option_id=f"{dec_id}o{idx}", label=_strip_md(raw_opt),
                         tradeoffs=_strip_md(cells[1]), recommended_row=recommended)

    def _resolve_recommendation(self, dec: DsdDecision) -> None:
        # (1) recommendation line matched to an option by normalized equality.
        if dec.reco_text:
            key = _normalize(dec.reco_text)
            for o in dec.options:
                if _normalize(o.label) == key:
                    dec.recommended_id = o.option_id
                    dec.recommendation_source = "reco-line"
                    return
        # (2) the bolded (lean) row.
        for o in dec.options:
            if o.recommended_row:
                dec.recommended_id = o.option_id
                dec.recommendation_source = "bold-row"
                return
        # (3) nothing — flagged; the viewer still renders the decision.
        dec.recommendation_source = "none"
        if dec.options:
            dec.notes.append("no recommendation")

    # -- write-back -----------------------------------------------------------

    def render_answers(self, answers: Dict[str, DecisionAnswer],
                       *, mark_answered: bool = True,
                       now: Optional[float] = None) -> str:
        """Return the DSD text with *answers* written into the slots — PURE (no
        I/O). ``answers`` maps decision id -> DecisionAnswer. Only slot lines (+
        their comment lines) change; every other line is byte-identical."""
        lines = list(self._lines)
        for dec in sorted(self.decisions, key=lambda d: (d.slot_line if d.slot_line
                                                         is not None else -1),
                          reverse=True):
            if dec.slot_line is None:
                continue
            ans = answers.get(dec.id)
            if ans is None:
                continue
            labels = [l for l in ans.labels if l]
            # comment-fidelity: ONE line, the same fold the node gets. A raw multi-line comment
            # here became several markdown lines and every record reader saw only the first paragraph.
            comment = fold_comment(ans.comment or "")
            if not labels and not comment:
                continue
            if labels:
                lines[dec.slot_line] = f"**Your call:** {', '.join(labels)}"
            if comment:
                comment_line = f"**Comments:** {comment}"
                if dec.comment_line is not None:
                    lines[dec.comment_line] = comment_line
                else:
                    lines.insert(dec.slot_line + 1, comment_line)
        if mark_answered:
            lines = self._apply_marker(lines, now)
        return "\n".join(lines)

    @staticmethod
    def _apply_marker(lines: List[str], now: Optional[float]) -> List[str]:
        import time
        ts = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                           time.gmtime(now if now is not None else time.time()))
        marker = f"<!-- dsd-status: answered {ts} -->"
        for i in range(min(4, len(lines))):
            if _MARKER_RE.match(lines[i]):
                out = list(lines)
                out[i] = marker
                return out
        return [marker] + lines


def _split_row(line: str) -> List[str]:
    """Split a markdown table row into cell strings (drop leading/trailing pipe
    fences). A line with no pipe yields []."""
    s = line.strip()
    if "|" not in s:
        return []
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def parse_dsd(text: str, path: Optional[str] = None) -> DsdDocument:
    """Parse *text* into a :class:`DsdDocument` (never raises on malformed input —
    an unparseable DSD yields ``ok == False``)."""
    return DsdDocument(text, path=path)


def load_dsd(path: str) -> DsdDocument:
    """Read a DSD markdown file (READ-ONLY) and parse it."""
    with open(path, "r", encoding="utf-8") as fh:
        return DsdDocument(fh.read(), path=path)


# ── the CONTEXT GUARD (context-guard) ──────────────────────
_REQUIRED_CONTEXT: Tuple[Tuple[str, str], ...] = (
    ("what_this_is", "What this is"),
    ("why_it_matters", "Why it matters"),
)


@dataclass
class DsdValidation:
    """The verdict on one DSD: ``ok``, and — when not ok — WHICH decision is
    missing WHICH field, by heading. ``missing`` is the machine form (one
    ``{decision_id, heading, field, label}`` per offence); ``message`` names every
    offence, not just the first."""
    ok: bool
    message: str
    missing: List[Dict[str, str]] = field(default_factory=list)
    n_decisions: int = 0
    parse_error: str = ""


ACCEPTED_OPTIONS_HEADER = "| Option | Tradeoffs |"


def zero_options_message(doc: DsdDocument) -> str:
    """Empty string when every decision parsed at least one option; otherwise a REFUSAL message naming each
    decision with zero parsed options and the ONE accepted table header (no synonyms: a differently
    headed table parses as zero options and the operator would see only 'Neither / None'). Pure. Enforced in ONE place,
    ``DSDStore.persist``, which BOTH the direct path and the hub send_dsd_for_human_review path go through.
    (Deliberately NOT folded into :func:`validate_dsd`: that verdict is pinned against the frozen pre-X46 model.)"""
    bad = [dec for dec in doc.decisions if not dec.options]
    if not bad:
        return ""
    lines = [f"  - {(dec.id + ' · ' + dec.question).strip() if dec.question else dec.id} "
             f"— parsed ZERO options" for dec in bad]
    return (f"REFUSED: {len(bad)} of {len(doc.decisions)} decisions parsed zero options "
            f"(the options table header must be exactly `{ACCEPTED_OPTIONS_HEADER}`, followed by a "
            f"`|---|---|` delimiter row; any other header, e.g. `| Option | What happens |`, parses as no options):\n"
            + "\n".join(lines))


def validate_dsd(doc: DsdDocument) -> DsdValidation:
    """REFUSE a DSD that any decision leaves without context — the ONE predicate
    the present tool and the (slice-3b) viewer both gate on. Pure: takes a parsed
    document, returns a verdict, touches no filesystem."""
    if not doc.decisions:
        err = doc.parse_error or "no D<n> decisions found"
        return DsdValidation(ok=False, message=f"not a parseable DSD: {err}",
                             n_decisions=0, parse_error=err)
    missing: List[Dict[str, str]] = []
    for dec in doc.decisions:
        heading = f"{dec.id} · {dec.question}".strip() if dec.question else dec.id
        for attr, label in _REQUIRED_CONTEXT:
            if not (getattr(dec, attr, "") or "").strip():
                missing.append({"decision_id": dec.id, "heading": heading,
                                "field": attr, "label": label})
    if not missing:
        return DsdValidation(
            ok=True,
            message=f"{len(doc.decisions)} decisions, every one carries "
                    f"What this is + Why it matters",
            n_decisions=len(doc.decisions))
    by_dec: Dict[str, List[str]] = {}
    headings: Dict[str, str] = {}
    for m in missing:
        by_dec.setdefault(m["decision_id"], []).append(m["label"])
        headings[m["decision_id"]] = m["heading"]
    lines = [f"  - {headings[d]} — missing: {' + '.join(labs)}"
             for d, labs in by_dec.items()]
    msg = (f"REFUSED: {len(by_dec)} of {len(doc.decisions)} decisions lack the "
           f"required context (every decision MUST carry both a 'What this is' and "
           f"a 'Why it matters' line):\n" + "\n".join(lines)
           + "\n  Fix: add the missing line(s) under the decision heading, above "
             "the options table. A label with no prose after it counts as blank.")
    return DsdValidation(ok=False, message=msg, missing=missing,
                         n_decisions=len(doc.decisions))


def validate_dsd_file(path: str) -> DsdValidation:
    """`validate_dsd` over a path — an unreadable file is a refusal, never an
    exception into a message loop."""
    try:
        return validate_dsd(load_dsd(path))
    except Exception as exc:                       # noqa: BLE001 — never raise
        return DsdValidation(ok=False,
                             message=f"unreadable DSD {path!r}: "
                                     f"{type(exc).__name__}: {exc}",
                             parse_error=str(exc))


# ── form-behaviour predicates ─────────────────────

def answer_is_complete(answer: DecisionAnswer) -> Tuple[bool, str]:
    """The completeness gate (completeness-gate +
    escape-hatch). Refuses save when: nothing is chosen; OR the always-present
    ``Neither / None — explain`` escape hatch is chosen with a BLANK comment.
    Returns ``(ok, reason)``."""
    labels = [l for l in answer.labels if l and l.strip()]
    if not labels:
        return False, "no option selected"
    comment = (answer.comment or "").strip()
    if any(_normalize(l) == _normalize(NEITHER_NONE_LABEL) for l in labels) and not comment:
        return False, "the 'Neither / None — explain' escape hatch requires a comment"
    return True, ""


AGREED_TRUE = "true"
AGREED_FALSE = "false"
AGREED_NA = "n/a"


def agreed_with_recommendation(decision: Optional[DsdDecision],
                               answer: DecisionAnswer) -> str:
    """``true`` / ``false`` / ``n/a`` — did the human's EXPLICIT choice match the
    decision's recommendation? ``n/a`` when the decision has no recommendation
    (or is unknown). The escape hatch, another option, or a comment-only answer
    is ``false``. Matching is by normalized label equality (the same key the
    recommendation line is resolved with)."""
    if decision is None or decision.recommended_id is None:
        return AGREED_NA
    reco = decision.option_by_id(decision.recommended_id)
    if reco is None:
        return AGREED_NA
    labels = [l for l in answer.labels if l and l.strip()]
    if len(labels) == 1 and _normalize(labels[0]) == _normalize(reco.label):
        return AGREED_TRUE
    return AGREED_FALSE


def enforce_single_select(decision: DsdDecision, answer: DecisionAnswer) -> Tuple[bool, str]:
    """The v1 single-select gate (select-mode-v1): even a
    ``[multi]``-tagged decision collects at most ONE selection in v1 (multi is
    parsed but gated off). More than one chosen label is refused. Returns
    ``(ok, reason)``."""
    labels = [l for l in answer.labels if l and l.strip()]
    if len(labels) > 1:
        return False, (f"single-select only for v1: decision {decision.id} got "
                       f"{len(labels)} selections ({', '.join(labels)})")
    return True, ""

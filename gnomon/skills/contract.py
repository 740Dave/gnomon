"""The projection contract — validate a graph node into a SkillProjection, or REFUSE it.

Constraint 1 (opt-in, never inference): a node is a projection candidate ONLY if it carries
``node-kind: skill-projection``. The scan is expected to pre-filter on that, but we re-check
here so the library is safe against any reader.

Constraint 3 (fail-closed): a malformed contract (missing ``skill-name``, illegal skill name,
missing node id/corpus) raises :class:`ContractError`. The caller refuses THAT skill and
continues — it never emits a broken file.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

NODE_KIND = "skill-projection"

# A skill name becomes a directory under ~/.claude/skills/ and an invocation token.
# Keep it filesystem- and invocation-safe: lowercase letters, digits, hyphen; no traversal.
# X49 fix round (F-1): at most 64 characters, so a name can never exceed the filesystem's file-name
# limit (an over-long name used to raise OSError out of the per-node handler and block the corpus).
SKILL_NAME_MAX = 64
_SKILL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,%d}$" % (SKILL_NAME_MAX - 1))

# The operational-pointer fields the body resolves, in emit order. Presence-driven:
# only fields the node actually carries are rendered.
POINTER_FIELDS = (
    "skill-policy-nodes",
    "skill-grammar-sot",
    "skill-delivery",
    "skill-verify",
    "skill-exec-refs",
)

# Every field that feeds the source-hash (drift detector): all keys with this prefix.
SKILL_FIELD_PREFIX = "skill-"


def is_projection_candidate(node: Mapping[str, Any]) -> bool:
    """True iff the node OPTS IN via ``node-kind: skill-projection`` (constraint 1).

    A node that merely mentions the term in prose (and so may come back from a text scan) is
    NOT a candidate — it is invisible to the generator, never a refusal.
    """
    props = node.get("properties") or {}
    if not isinstance(props, Mapping):
        return False
    return _as_str(props.get("node-kind", "")).strip() == NODE_KIND


class ContractError(ValueError):
    """A projection contract is malformed. Carries the skill name (if known) for reporting."""

    def __init__(self, reason: str, *, skill_name: str | None = None, node_id: str | None = None):
        self.reason = reason
        self.skill_name = skill_name
        self.node_id = node_id
        super().__init__(reason)


@dataclass(frozen=True)
class SkillProjection:
    """A validated projection contract, ready to render."""

    node_id: str
    corpus: str
    skill_name: str
    skill_desc: str
    skill_trigger: str | None
    pointers: dict[str, str]  # subset of POINTER_FIELDS actually present, in emit order
    subset_today: str | None
    subset_pending: str | None
    what_changes_this: str | None
    # The canonical map of every ``skill-*`` field (as strings), for source-hash + audit.
    skill_fields: dict[str, str] = field(default_factory=dict)
    # X49 liveness inputs (NOT hashed; see liveness_warning in render.py): the newest
    # ``decided-in-session`` / ``updated`` date on the node, and the newest ISO date that ends a
    # NON-``skill-`` property key (a dated lesson such as ``mosaic-check-2026-10-02``).
    node_date: str | None = None
    lesson_date: str | None = None


_DATE_PREFIX_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2})")
_KEY_DATE_SUFFIX_RE = re.compile(r"-(\d{4}-\d{2}-\d{2})$")
#: The node properties whose date says when the node was last decided/revised.
NODE_DATE_KEYS = ("decided-in-session", "updated")


def _valid_date(text: str) -> str | None:
    try:
        _dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


def compute_liveness_inputs(props: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """``(node_date, lesson_date)`` for the X49 liveness check (both ISO dates or None).

    node_date   = the newest date found at the START of ``decided-in-session`` / ``updated``
                  (a list-valued property contributes every element).
    lesson_date = the newest ISO date that ENDS a property key which does not start with
                  ``skill-`` (a dated lesson/amendment property, e.g. ``mosaic-check-2026-10-02``).
    """
    node_dates: list[str] = []
    for key in NODE_DATE_KEYS:
        if key not in props:
            continue
        raw = props[key]
        for item in (raw if isinstance(raw, list) else [raw]):
            m = _DATE_PREFIX_RE.match(str(item))
            if m and _valid_date(m.group(1)):
                node_dates.append(m.group(1))
    lesson_dates: list[str] = []
    for key in props:
        if not isinstance(key, str) or key.startswith(SKILL_FIELD_PREFIX):
            continue
        m = _KEY_DATE_SUFFIX_RE.search(key)
        if m and _valid_date(m.group(1)):
            lesson_dates.append(m.group(1))
    return (max(node_dates) if node_dates else None, max(lesson_dates) if lesson_dates else None)


def _as_str(value: Any) -> str:
    """Normalize a property value to a string. A duplicate LTS key ACCUMULATES into a list
    (reference_lts_duplicate_key_accumulates) — join deterministically rather than lose a value."""
    if isinstance(value, list):
        return "\n".join(_as_str(v) for v in value)
    return str(value)


def parse_projection(node: Mapping[str, Any]) -> SkillProjection:
    """Validate one scan node into a :class:`SkillProjection`, or raise :class:`ContractError`.

    ``node`` is the lts_find/lts_node node shape: at least ``id``, ``corpus`` and
    ``properties`` (a mapping of property key -> value).
    """
    node_id = node.get("id")
    corpus = node.get("corpus")
    props = node.get("properties") or {}
    if not isinstance(props, Mapping):
        raise ContractError("node has no properties mapping", node_id=node_id if isinstance(node_id, str) else None)

    skill_name_raw = props.get("skill-name")
    skill_name = _as_str(skill_name_raw).strip() if skill_name_raw is not None else None

    # node-kind opt-in re-check (constraint 1)
    node_kind = _as_str(props.get("node-kind", "")).strip()
    if node_kind != NODE_KIND:
        raise ContractError(
            f"node-kind is {node_kind!r}, not {NODE_KIND!r} (not a projection candidate)",
            skill_name=skill_name, node_id=node_id if isinstance(node_id, str) else None,
        )

    if not isinstance(node_id, str) or not node_id.strip():
        raise ContractError("missing/invalid node id", skill_name=skill_name)
    node_id = node_id.strip()
    if not isinstance(corpus, str) or not corpus.strip():
        raise ContractError("missing/invalid corpus", skill_name=skill_name, node_id=node_id)
    corpus = corpus.strip()

    if not skill_name:
        raise ContractError("missing skill-name", node_id=node_id)
    if not _SKILL_NAME_RE.match(skill_name):
        raise ContractError(
            f"illegal skill-name {skill_name!r} (must match {_SKILL_NAME_RE.pattern})",
            skill_name=skill_name, node_id=node_id,
        )

    skill_desc_raw = props.get("skill-desc")
    skill_desc = _as_str(skill_desc_raw).strip() if skill_desc_raw is not None else ""
    if not skill_desc:
        raise ContractError("missing skill-desc", skill_name=skill_name, node_id=node_id)

    trigger_raw = props.get("skill-trigger")
    skill_trigger = _as_str(trigger_raw).strip() if trigger_raw is not None else None

    pointers: dict[str, str] = {}
    for key in POINTER_FIELDS:
        if key in props:
            val = _as_str(props[key]).strip()
            if val:
                pointers[key] = val

    def _opt(key: str) -> str | None:
        if key in props:
            v = _as_str(props[key]).strip()
            return v or None
        return None

    # Every skill-* field, as canonical strings, for the source-hash.
    skill_fields = {
        k: _as_str(v) for k, v in sorted(props.items()) if isinstance(k, str) and k.startswith(SKILL_FIELD_PREFIX)
    }

    node_date, lesson_date = compute_liveness_inputs(props)

    return SkillProjection(
        node_id=node_id,
        corpus=corpus,
        skill_name=skill_name,
        skill_desc=skill_desc,
        skill_trigger=skill_trigger,
        pointers=pointers,
        subset_today=_opt("skill-projects-subset-today"),
        subset_pending=_opt("skill-projects-subset-pending"),
        what_changes_this=_opt("what-changes-this"),
        skill_fields=skill_fields,
        node_date=node_date,
        lesson_date=lesson_date,
    )

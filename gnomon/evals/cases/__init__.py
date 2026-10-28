"""The case registry. Add a case: define an ``EvalCase`` in a module here and
append it to ``CASES`` (names must be unique slugs; the dry-run enforces it)."""

from typing import List

from ..case import EvalCase
from .seed import SEED_WRITE_FILE

CASES: List[EvalCase] = [
    SEED_WRITE_FILE,
]


def get_cases(names=None) -> List[EvalCase]:
    """The registered cases, or the named subset. Unknown names raise KeyError."""
    if not names:
        return list(CASES)
    by_name = {c.name: c for c in CASES}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise KeyError(f"unknown case(s): {', '.join(missing)} (known: {', '.join(sorted(by_name))})")
    return [by_name[n] for n in names]

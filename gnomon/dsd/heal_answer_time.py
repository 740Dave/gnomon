"""- one-time, idempotent, backed-up heal of missing DSD answer times.

A record with no answer time takes its filing time as the answer time; nothing is reconstructed from transcripts. So an ANSWERED record with no usable
``answered_epoch`` gets ``answered_epoch = created_epoch`` (its filing time) and
``answer_time_source = "healed-from-sent"`` (API: ``answered_source`` reports it, so
the UI/consumers can tell it is approximate).

Rules (each is tested):
  * NEVER overwrites a real answer time: only records whose answered_epoch is
    missing / non-numeric / <= 0 are touched.
  * Idempotent: a second run finds nothing to do.
  * Backs each file up BEFORE touching it (``<file>.bak-20260930-X37``; never
    overwrites an existing backup).
  * No transcript crawl, no mtime guess. A record without a usable created_epoch
    is reported as skipped, not guessed.
  * Runs ONLY as an explicit command (``--apply``; default is a dry run). Never at
    import time, never from the hub, never from tests against real data (g31).

    python3 -m gnomon.dsd.heal_answer_time [--root ~/gnomon-data/agents] [--apply]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import shutil
import sys
import tempfile
from typing import Any, Dict, List, Optional

BACKUP_SUFFIX = ".bak-20260930-X37"
SOURCE = "healed-from-sent"


def _num(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def heal(root: str, *, apply: bool = False) -> Dict[str, List[str]]:
    """Scan ``<root>/*/dsd/*.json``. Returns {"healed", "would_heal", "skipped_no_created",
    "untouched", "unreadable"} as lists of file paths (healed only when ``apply``)."""
    res: Dict[str, List[str]] = {k: [] for k in
                                 ("healed", "would_heal", "skipped_no_created",
                                  "untouched", "unreadable")}
    for path in sorted(glob.glob(os.path.join(root, "*", "dsd", "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, ValueError):
            res["unreadable"].append(path)
            continue
        if (not isinstance(raw, dict) or raw.get("status") != "answered"
                or _num(raw.get("answered_epoch")) is not None):
            res["untouched"].append(path)
            continue
        created = _num(raw.get("created_epoch"))
        if created is None:
            res["skipped_no_created"].append(path)
            continue
        if not apply:
            res["would_heal"].append(path)
            continue
        bak = path + BACKUP_SUFFIX
        if not os.path.exists(bak):
            shutil.copy2(path, bak)
        raw["answered_epoch"] = created
        raw["answer_time_source"] = SOURCE
        fd, tmp = tempfile.mkstemp(prefix=".dsd-", suffix=".tmp", dir=os.path.dirname(path))
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
        res["healed"].append(path)
    return res


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    from ..config import agents_root_default

    ap.add_argument("--root", default=agents_root_default())
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    a = ap.parse_args(argv)
    if not os.path.isdir(a.root):
        print(f"ERROR: root {a.root!r} does not exist", file=sys.stderr)
        return 2
    r = heal(a.root, apply=a.apply)
    print(("APPLY" if a.apply else "DRY RUN"), a.root)
    for k, v in r.items():
        print(f"  {k}: {len(v)}")
        if k != "untouched":
            for p in v:
                print("    ", p)
    return 0


if __name__ == "__main__":
    sys.exit(main())

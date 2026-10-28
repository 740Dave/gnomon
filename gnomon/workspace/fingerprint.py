"""Stat fingerprints: the before/after proof that a read-only run changed nothing.

A fingerprint maps every path under the given roots (files, directories AND symlinks, never
followed) to ``[inode, mtime_ns, size, mode]``. Comparing two fingerprints yields ``added``,
``removed`` and ``changed`` path lists. Directory mtimes are included on purpose: a created or
deleted entry changes its parent directory's mtime, so even a write that left no surviving file
shows up.

LIVE-WRITER CAVEAT (honest boundary): on a machine with live sessions some roots are written by OTHER
processes while the converter runs (a harnessed session's ``injection_log.jsonl`` and context state,
the usage DB, notices). A fingerprint diff therefore proves "unchanged" only for paths no live writer
touches; the converter-specific proof is :mod:`.writeguard` (the process cannot write outside scratch),
and the two are reported together. ``classify`` splits a diff into paths under a caller-named
live-writer set versus everything else, so the unexplained remainder is visible, never hidden.
"""

from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Sequence, Tuple

from .scopebase import UNPASSED, _gate_primitive

Fingerprint = Dict[str, List[int]]


def fingerprint(roots: Sequence[str]) -> Fingerprint:
    out: Fingerprint = {}
    for root in roots:
        if not os.path.lexists(root):
            out[root + "::MISSING"] = [0, 0, 0, 0]
            continue
        _add(out, root)
        if os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
                dirnames.sort()
                for name in sorted(dirnames) + sorted(filenames):
                    _add(out, os.path.join(dirpath, name))
    return out


def _add(out: Fingerprint, path: str) -> None:
    try:
        st = os.lstat(path)
    except OSError:
        out[path] = [-1, -1, -1, -1]
        return
    out[path] = [st.st_ino, st.st_mtime_ns, st.st_size, st.st_mode]


def diff(before: Fingerprint, after: Fingerprint) -> Dict[str, List[str]]:
    b, a = set(before), set(after)
    return {
        "added": sorted(a - b),
        "removed": sorted(b - a),
        "changed": sorted(p for p in b & a if before[p] != after[p]),
    }


def classify(d: Dict[str, List[str]], live_writer_prefixes: Iterable[str]) -> Dict[str, Dict[str, List[str]]]:
    """Split a diff into ``explained`` (under a named live-writer prefix) and ``unexplained``."""
    prefixes = tuple(live_writer_prefixes)
    out = {"explained": {k: [] for k in d}, "unexplained": {k: [] for k in d}}
    for k, paths in d.items():
        for p in paths:
            bucket = "explained" if prefixes and p.startswith(prefixes) else "unexplained"
            out[bucket][k].append(p)
    return out


def summarize(fp: Fingerprint) -> Dict[str, int]:
    return {"entries": len(fp)}


def dump(fp: Fingerprint, path: str, *, enabled=UNPASSED, config=None) -> None:
    """Write a fingerprint to ``path``. PUBLIC, so it gates itself, first (g54b e, audit-x13-runtime-gate P2-2): the caller passes its own
    ``enabled`` / ``config`` (``None`` = read ``workspace_scope_core``); omitted, it refuses. The CLI ``snap`` below does NOT go
    through this: it uses the module-private :func:`_write_snapshot`, because the converter's stat proof is flag-less by design (S1 ruling)."""
    _gate_primitive(enabled, config, "fingerprint.dump")
    _write_snapshot(fp, path)


def _write_snapshot(fp: Fingerprint, path: str) -> None:
    """The ungated write of the converter's CLI proof tool (S1: flag-less by design, scratch ``--out`` only). Module-private on purpose."""
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fp, fh, sort_keys=True)


def load(path: str) -> Fingerprint:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def main(argv=None) -> int:
    """``python3 -m gnomon.workspace.fingerprint snap <out.json> <root>...`` or
    ``... compare <before.json> <after.json>`` (prints the diff; exit 1 if any difference). ``snap`` refuses
    a root that does not exist (a vacuous snapshot proves nothing) unless ``--allow-missing``."""
    import sys

    argv = list(sys.argv[1:] if argv is None else argv)
    allow_missing = "--allow-missing" in argv
    argv = [a for a in argv if a != "--allow-missing"]
    if len(argv) >= 3 and argv[0] == "snap":
        fp = fingerprint(argv[2:])
        missing = [k[:-len("::MISSING")] for k in fp if k.endswith("::MISSING")]
        if allow_missing:
            print(f"note: {len(missing)} root(s) absent and recorded as MISSING (allowed by flag)")
        elif missing:     # a fingerprint of a root that is not there proves nothing: loud stop, nothing written
            print(f"ERROR: {len(missing)} root(s) do not exist (a vacuous snapshot proves nothing): {missing[:3]}")
            return 2
        _write_snapshot(fp, argv[1])
        print(f"snapshot of {len(argv) - 2} root(s), {len(fp)} entries -> {argv[1]}")
        return 0
    if len(argv) == 3 and argv[0] == "compare":
        d = diff(load(argv[1]), load(argv[2]))
        print(json.dumps({k: len(v) for k, v in d.items()}, sort_keys=True))
        for k, v in d.items():
            for p in v[:50]:
                print(f"  {k}: {p}")
        return 0 if not any(d.values()) else 1
    print(main.__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

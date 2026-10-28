"""Shared helpers for the X26 feature-usage tests.

Neutral by design (g3): no deployment identifier appears here or in the ``test_fu_*`` files; actor and
workspace values are generic (``alpha-architect-20260929-062652`` has the SHAPE of a dated instance id, which is
what the redaction-exemption test needs; the spec's own fixtures use deployment lineage ids and whether fixtures
count as code under g3 is an open ruling: the neutral shape proves the same property).

Every helper writes under a pytest tmp dir. NOTHING here may touch ``~/gnomon-data/metrics`` (g31): conftest pins
``GNOMON_FEATURE_EVENTS_DIR`` / ``GNOMON_USAGE_DB`` to temp paths for every test.
"""

from __future__ import annotations

import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Sequence

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from gnomon import feature_events as FE  # noqa: E402

INSTANCE_ID = "alpha-architect-20260929-062652"     # a real-shaped dated instance id (the entropy rule redacts it)
OTHER_INSTANCE = "analyst-20260930"


def events_dir() -> str:
    return os.environ["GNOMON_FEATURE_EVENTS_DIR"]


def event_files(root: Optional[str] = None) -> List[str]:
    root = root or events_dir()
    return sorted(glob.glob(os.path.join(root, "*", "*.jsonl")))


def read_events(root: Optional[str] = None, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in event_files(root):
        if kind and os.path.basename(p) != f"{kind}.jsonl":
            continue
        with open(p, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    out.append(json.loads(line))
    return out


def snapshot(root: Optional[str] = None) -> List[str]:
    """every raw event line now present (multi-file: a later append can sort BEFORE earlier files, so a plain length
    offset is not a valid 'new since' marker)"""
    out: List[str] = []
    for p in event_files(root):
        with open(p, "r", encoding="utf-8") as fh:
            out += [l for l in fh.read().splitlines() if l.strip()]
    return out


def new_since(snap: List[str], root: Optional[str] = None) -> List[Dict[str, Any]]:
    """the events written since ``snap`` (a multiset difference of raw lines)"""
    from collections import Counter

    have = Counter(snap)
    out = []
    for line in snapshot(root):
        if have.get(line, 0) > 0:
            have[line] -= 1
        else:
            out.append(json.loads(line))
    return out


def fresh_emitter(**env) -> None:
    """Reset the emitter's process state (the conftest fixture already does it per test; a test that changes
    the environment mid-test calls this again)."""
    FE._reset_for_tests()


def session_dir(tmp_path, name: str = INSTANCE_ID, *, scope: Optional[Dict[str, Any]] = None,
                instance_id: Optional[str] = None) -> str:
    d = tmp_path / name
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    (d / ".claude" / "launch.json").write_text(json.dumps({"instance_id": instance_id or name}))
    if scope is not None:
        (d / ".claude" / "scope-state.json").write_text(json.dumps(scope))
    return str(d)


# -- registry fixtures --------------------------------------------------------------------------------------

def registry_node_text(node_id: str, **props: str) -> str:
    lines = [f"## Registry fixture [{node_id}]", "", "- part-of: [GM-FU-REGISTRY]", "- status: draft"]
    for k, v in props.items():
        lines.append(f"- {k.replace('_', '-')}: {v}")
    lines.append("- decided-in-session: 2026-09-30")
    return "\n".join(lines) + "\n"


def write_registry(dirpath, nodes: Sequence[Dict[str, str]]) -> str:
    """A tiny registry corpus directory: one .lts file with a shape node and the given entry nodes. Each entry is
 ``{"id": "", "feature_id":..., "surface":..., "expected_users":..., "expected_rate":...,
    "docs": ..., [aliases]}``; a missing key is simply not written."""
    os.makedirs(str(dirpath), exist_ok=True)
    body = ["---", 'title: "Registry fixture"', 'id: "SPEC:GM-FU-FIXTURE-ROOT"', "domain: TEST.FIXTURE",
            "status: draft", "created: 2026-09-30", "updated: 2026-09-30", "---",
            "# Registry fixture [SPEC:GM-FU-FIXTURE-ROOT]", "", "> fixture.", "",
            "## Shape [GM-FU-REGISTRY]", "", "- part-of: [GM-FU]", "- status: draft", ""]
    for n in nodes:
        n = dict(n)
        nid = n.pop("id")
        body.append(registry_node_text(nid, **n))
    p = os.path.join(str(dirpath), "10-fixture.lts")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("\n".join(body))
    return str(dirpath)


def good_node(slug: str, **over: str) -> Dict[str, str]:
    d = {"id": f"GM-FU-REGISTRY-{slug.upper()}", "feature_id": slug, "surface": "operator-command",
         "expected_users": "ch", "expected_rate": "EMPTY", "docs": "GM-HARNESS"}
    d.update(over)
    return d

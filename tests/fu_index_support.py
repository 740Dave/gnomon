"""Index fixtures for the X26 indexer tests (neutral, g3)."""

import json
import os
import sqlite3

from gnomon import usage_metrics as UM


def ev(**o):
    base = {"v": 1, "ts": 1790000000.0, "feature_id": "inject", "actor": "ch", "surface": "self-verb",
            "session_run": None, "workspace": None, "project": None}
    base.update(o)
    return base


def write_events(root, day, kind, lines, mode="w"):
    d = os.path.join(str(root), day)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{kind}.jsonl")
    with open(p, mode, encoding="utf-8") as fh:
        for l in lines:
            fh.write((l if isinstance(l, str) else json.dumps(l)) + "\n")
    return p


def rows(db, sql="SELECT * FROM feature_events ORDER BY id"):
    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(sql)]
    finally:
        con.close()


def index(db, root, **kw):
    return UM.index_feature_events(str(db), [str(root)], **kw)

"""the v4 -> v5 migration rebuilds without losing X7 data and refuses a newer DB
."""

import json
import os
import sqlite3

import pytest

from fu_index_support import ev, index, rows, write_events
from gnomon import usage_metrics as UM


def _session_logs(tmp_path):
    root = tmp_path / "sess"
    for name, n in (("alpha-architect-20260929-062652", 4), ("analyst-20260930", 3)):
        d = root / name / ".claude"
        d.mkdir(parents=True)
        with open(d / "injection_log.jsonl", "w") as fh:
            for i in range(n):
                fh.write(json.dumps({"kind": "injection", "source": "operator", "trigger": "operator-submit",
                                     "decision": "allow", "ts": 1790000000.0 + i, "instance_id": name}) + "\n")
    return str(root)


def _downgrade_to_v4(db):
    """a REAL v4-shaped DB: the X7 tables with real rows, no feature_events table, no stream / retired columns"""
    con = sqlite3.connect(db)
    con.executescript("DROP TABLE feature_events;")
    con.execute("ALTER TABLE files DROP COLUMN stream")
    con.execute("ALTER TABLE files DROP COLUMN retired")
    con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    con.commit()
    con.close()


def test_v4_to_v5_and_refuse_newer(tmp_path):
    root = _session_logs(tmp_path)
    db = str(tmp_path / "u.sqlite")
    UM.index(db, roots=[root])
    n_records = sqlite3.connect(db).execute("SELECT COUNT(*) FROM records").fetchone()[0]
    assert n_records == 7
    rep_before = UM.build_report(db, known={"source": set(), "verb": set(), "cmd": set(), "kind": set()})
    _downgrade_to_v4(db)
    assert sqlite3.connect(db).execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "4"
    UM.index(db, roots=[root])                                                           # the rebuild
    con = sqlite3.connect(db)
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "5"
    assert con.execute("SELECT COUNT(*) FROM records").fetchone()[0] == n_records       # no X7 data lost
    assert {r[1] for r in con.execute("PRAGMA table_info(files)")} >= {"stream", "retired"}
    assert con.execute("SELECT COUNT(*) FROM feature_events").fetchone()[0] == 0        # no orphaned table, recreated
    con.close()
    rep_after = UM.build_report(db, known={"source": set(), "verb": set(), "cmd": set(), "kind": set()})
    for k in ("report", "schema_version", "db"):
        rep_before.pop(k, None)
        rep_after.pop(k, None)
    assert rep_before == rep_after, "the X7 report for the same window must be identical after the migration"
    # a DB stamped with a NEWER schema is refused
    con = sqlite3.connect(db)
    con.execute("UPDATE meta SET value='6' WHERE key='schema_version'")
    con.commit()
    con.close()
    with pytest.raises(RuntimeError) as ei:
        UM.connect(db)
    assert "refusing to mix" in str(ei.value)


def test_the_drop_list_includes_feature_events_so_no_orphan_survives(tmp_path):
    """a v4 DB that somehow holds a feature_events table (a half-migrated one) has it dropped and recreated empty"""
    db = str(tmp_path / "u.sqlite")
    UM.connect(db).close()
    con = sqlite3.connect(db)
    con.execute("INSERT INTO files(path, session, stream) VALUES ('/x', '', 'feature_events')")
    con.execute("INSERT INTO feature_events(file_id, byte_offset, feature_id) VALUES (1, 0, 'planted-feature')")
    con.execute("UPDATE meta SET value='4' WHERE key='schema_version'")
    con.commit()
    con.close()
    UM.connect(db).close()
    con = sqlite3.connect(db)
    assert con.execute("SELECT COUNT(*) FROM feature_events").fetchone()[0] == 0
    assert con.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0

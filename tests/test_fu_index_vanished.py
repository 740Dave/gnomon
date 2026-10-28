"""a raw event file that vanishes (the 90-day retention job deleted it) is
retired with its rows KEPT, never treated as rotated."""

import json
import os

from fu_index_support import ev, index, rows, write_events


def _counts(db):
    return {r["path"]: r["n"] for r in rows(str(db), "SELECT f.path AS path, COUNT(e.id) AS n FROM files f "
                                                   "LEFT JOIN feature_events e ON e.file_id=f.id "
                                                   "WHERE f.stream='feature_events' GROUP BY f.id")}


def test_vanished_file_retired_rows_kept(tmp_path):
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    a = write_events(root, "20260101", "hooks", [ev(actor=f"a{i}") for i in range(4)])
    b = write_events(root, "20260930", "hooks", [ev(actor=f"b{i}") for i in range(3)])
    index(db, root)
    before = rows(str(db))
    assert _counts(db) == {a: 4, b: 3}
    os.remove(a)                                                  # the retention job's action
    s = index(db, root)
    assert s["events_files_retired"] == 1 and s["events_files_reset"] == 0
    assert _counts(db)[a] == 4, "A's rows are all still present"
    assert [r for r in rows(str(db)) if r["file_id"] == [x for x in before if x["actor"] == "a0"][0]["file_id"]] == \
        [r for r in before if r["actor"].startswith("a")], "byte-identical"
    f = rows(str(db), "SELECT path, retired, resets FROM files WHERE stream='feature_events' ORDER BY path")
    assert {r["path"]: (r["retired"], r["resets"]) for r in f} == {a: (1, 0), b: (0, 0)}
    snap = rows(str(db))
    for _ in range(2):                                            # idempotent
        s = index(db, root)
        assert s["events_files_retired"] == 0 and s["events_files_reset"] == 0
    assert rows(str(db)) == snap
    # a NEW file at a different path indexes normally
    c = write_events(root, "20261001", "hooks", [ev(actor="c0")])
    assert index(db, root)["events_added"] == 1 and _counts(db)[c] == 1


def test_negative_control_a_replaced_inode_at_the_same_path_IS_a_rotation(tmp_path):
    """the vanished-vs-rotated distinction is what kept A's rows: replace B in place with a new inode and its old
    rows ARE dropped and it is re-read from byte 0"""
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    b = write_events(root, "20260930", "hooks", [ev(actor=f"b{i}") for i in range(3)])
    index(db, root)
    new = b + ".tmp"
    open(new, "w").write(json.dumps(ev(actor="fresh")) + "\n")
    os.replace(new, b)
    s = index(db, root)
    assert s["events_files_reset"] == 1 and s["events_files_retired"] == 0
    assert [r["actor"] for r in rows(str(db))] == ["fresh"]


def test_a_retired_file_that_reappears_is_unretired(tmp_path):
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    a = write_events(root, "20260101", "hooks", [ev(actor="a0")])
    index(db, root)
    os.remove(a)
    index(db, root)
    write_events(root, "20260101", "hooks", [ev(actor="a-new")])
    s = index(db, root)
    assert s["events_files_reset"] == 1                           # a genuinely new inode at that path: a rotation
    assert rows(str(db), "SELECT retired FROM files WHERE stream='feature_events'")[0]["retired"] == 0

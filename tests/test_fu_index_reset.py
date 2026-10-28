"""rotate, truncate and rewrite-in-place each trigger a reset and no stale
rows survive."""

import json
import os

import pytest

from fu_index_support import ev, index, rows, write_events
from gnomon import usage_metrics as UM


def _setup(tmp_path):
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    p = write_events(root, "20260930", "hooks", [ev(feature_id="inject", actor=f"a{i}") for i in range(5)])
    index(db, root)
    assert len(rows(str(db))) == 5
    return root, db, p


def _resets(db):
    return rows(str(db), "SELECT resets FROM files WHERE stream='feature_events'")[0]["resets"]


def test_reset_classes_rotate(tmp_path):
    root, db, p = _setup(tmp_path)
    new = p + ".new"
    with open(new, "w") as fh:                                   # a different inode at the same path
        for i in range(2):
            fh.write(json.dumps(ev(feature_id="memory", actor=f"n{i}")) + "\n")
    os.replace(new, p)
    s = index(db, root)
    assert s["events_files_reset"] == 1 and _resets(db) == 1
    rs = rows(str(db))
    assert [r["actor"] for r in rs] == ["n0", "n1"], "old rows must be gone and the file re-read from byte 0"


def test_reset_classes_truncate(tmp_path):
    root, db, p = _setup(tmp_path)
    with open(p, "r+b") as fh:
        fh.truncate(os.path.getsize(p) // 3)                     # below the stored offset
    s = index(db, root)
    assert s["events_files_reset"] == 1 and _resets(db) == 1
    assert [r["actor"] for r in rows(str(db))] == ["a0"]        # only the surviving complete line, re-read from 0


def test_reset_classes_rewrite_in_place(tmp_path):
    root, db, p = _setup(tmp_path)
    data = bytearray(open(p, "rb").read())
    data[10:18] = b"XXXXXXXX"                                    # same length, different head bytes, same inode
    ino = os.stat(p).st_ino
    with open(p, "r+b") as fh:
        fh.write(bytes(data))
    assert os.stat(p).st_ino == ino
    s = index(db, root)
    assert s["events_files_reset"] == 1 and _resets(db) == 1
    # the mangled first line is now unparseable (counted bad); the other four are re-read from byte 0, none doubled
    assert s["events_bad_lines"] == 1 and [r["actor"] for r in rows(str(db))] == ["a1", "a2", "a3", "a4"]


def test_an_unchanged_file_is_not_reset(tmp_path):
    root, db, p = _setup(tmp_path)
    assert index(db, root)["events_files_reset"] == 0 and _resets(db) == 0


def test_an_oversized_line_is_skipped_and_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(UM, "_MAX_LINE_BYTES", 4096)             # the same code path as 8 MiB, without writing 9 MiB
    monkeypatch.setattr(UM, "_CHUNK_BYTES", 1024)
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    write_events(root, "20260930", "hooks", [ev(), json.dumps(ev(pad="x" * 10000)), ev(feature_id="memory")])
    s = index(db, root)
    assert s["events_bad_lines"] == 1 and s["events_added"] == 2
    assert [r["feature_id"] for r in rows(str(db))] == ["inject", "memory"]


def test_the_real_8_mib_bound_is_the_default():
    assert UM._MAX_LINE_BYTES == 8 << 20 and UM._COMMIT_EVERY == 5000


def test_offset_checkpoints_and_an_interrupted_run_leaves_no_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(UM, "_COMMIT_EVERY", 5)
    root, db = tmp_path / "ev", tmp_path / "u.sqlite"
    write_events(root, "20260930", "hooks", [ev(actor=f"a{i}") for i in range(12)])
    real = UM.normalize_feature_event
    n = {"i": 0}

    def flaky(rec):
        n["i"] += 1
        if n["i"] == 8:
            raise RuntimeError("interrupted")
        return real(rec)

    monkeypatch.setattr(UM, "normalize_feature_event", flaky)
    with pytest.raises(RuntimeError):
        index(db, root)
    # the first checkpoint (5 rows) was COMMITTED with its offset
    f = rows(str(db), "SELECT offset FROM files WHERE stream='feature_events'")[0]
    assert f["offset"] > 0
    monkeypatch.setattr(UM, "normalize_feature_event", real)
    index(db, root)
    actors = [r["actor"] for r in rows(str(db))]
    assert sorted(actors) == sorted(f"a{i}" for i in range(12)) and len(set(actors)) == 12

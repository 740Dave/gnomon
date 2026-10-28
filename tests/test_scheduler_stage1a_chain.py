"""Scheduler stage 1a: the hash-chained audit.jsonl and runs.jsonl (requirement-log-chain;
ac-s1-audit-chain-tamper)."""

from __future__ import annotations

import json
import os

from scheduler_support import T0, confirm, make, propose_inject, run_ticks
from gnomon.scheduler import store


def _populate(tmp_path, n_runs=3):
    sch, clock, reg, tgt = make(tmp_path)
    reg.on_route = lambda iid, frame: tgt.deliver(frame)           # emulate the harness: every run is DELIVERED
    propose_inject(sch, trigger={"every_minutes": 15}); confirm(sch)
    sch.set_enabled("remind-me", False, actor="operator-page"); sch.set_enabled("remind-me", True, actor="operator-page")
    run_ticks(sch, clock, n_runs * 15 * 60 + 60)
    return sch


def _lines(path):
    return open(path, "rb").read().split(b"\n")[:-1]


def _write(path, lines):
    open(path, "wb").write(b"\n".join(lines) + b"\n")


def test_untouched_logs_verify_ok_and_the_first_line_carries_the_empty_prev(tmp_path):
    sch = _populate(tmp_path)
    for path in (sch.paths.audit, sch.paths.run_chain("remind-me")):
        ok, recs, breaks = store.verify_chain(path)
        assert ok and breaks == [] and len(recs) >= 3
        assert recs[0]["prev_sha256"] == "" and all(r["schema_version"] == 1 for r in recs)
        ls = _lines(path)
        for i in range(1, len(ls)):                                  # each line carries the sha256 of the previous line's bytes
            assert json.loads(ls[i])["prev_sha256"] == store.sha256_hex(ls[i - 1])
    assert sch.run_log()["audit_chain"]["ok"] is True and sch.run_log()["chains"]["remind-me"]["ok"] is True


def test_a_hand_edited_middle_line_is_reported_at_the_line_after_it(tmp_path):
    sch = _populate(tmp_path)
    for path in (sch.paths.audit, sch.paths.run_chain("remind-me")):
        ls = _lines(path)
        assert len(ls) >= 4
        k = 1                                                        # edit line 2 (index 1)
        rec = json.loads(ls[k]); rec["outcome"] = "tampered"; ls2 = list(ls); ls2[k] = json.dumps(rec, sort_keys=True).encode()
        backup = open(path, "rb").read()
        _write(path, ls2)
        ok, _, breaks = store.verify_chain(path)
        assert not ok and breaks[0]["line"] == k + 2                 # 1-based line number of the FIRST line that no longer chains
        open(path, "wb").write(backup)
        assert store.verify_chain(path)[0] is True                   # control: restoring it verifies again


def test_a_deleted_middle_line_is_reported_at_the_right_line(tmp_path):
    sch = _populate(tmp_path)
    for path in (sch.paths.audit, sch.paths.run_chain("remind-me")):
        ls = _lines(path)
        _write(path, ls[:1] + ls[2:])                                # remove line 2
        ok, _, breaks = store.verify_chain(path)
        assert not ok and [b["line"] for b in breaks] == [2]


def test_an_inserted_line_is_reported_at_the_right_line(tmp_path):
    sch = _populate(tmp_path)
    for path in (sch.paths.audit, sch.paths.run_chain("remind-me")):
        ls = _lines(path)
        forged = json.dumps({"schema_version": 1, "prev_sha256": "", "verb": "forged"}, sort_keys=True).encode()
        _write(path, ls[:2] + [forged] + ls[2:])                     # insert after line 2 (becomes line 3)
        ok, _, breaks = store.verify_chain(path)
        assert not ok and breaks[0]["line"] == 3
        # a cleverer forger who copies the right prev still breaks the NEXT line
        forged2 = json.dumps({"schema_version": 1, "prev_sha256": store.sha256_hex(ls[1]), "verb": "forged"},
                             sort_keys=True).encode()
        _write(path, ls[:2] + [forged2] + ls[2:])
        ok, _, breaks = store.verify_chain(path)
        assert not ok and breaks[0]["line"] == 4


def test_a_nonempty_first_prev_and_a_garbage_line_are_breaks_never_skipped(tmp_path):
    p = str(tmp_path / "x.jsonl")
    store.append_chained(p, {"a": 1}); store.append_chained(p, {"a": 2})
    ls = _lines(p)
    rec = json.loads(ls[0]); rec["prev_sha256"] = "abc"
    _write(p, [json.dumps(rec).encode()] + ls[1:])
    assert store.verify_chain(p)[2][0]["line"] == 1
    _write(p, ls[:1] + [b"this is not json"] + ls[1:])
    br = store.verify_chain(p)[2]
    assert br[0]["line"] == 2 and "not a JSON object" in br[0]["reason"]


def test_the_known_limit_an_edited_last_line_is_not_detectable_by_the_chain_alone(tmp_path):
    """Stated, not solved (store.py docstring): nothing follows the last line to disagree with it."""
    sch = _populate(tmp_path)
    path = sch.paths.audit
    ls = _lines(path)
    rec = json.loads(ls[-1]); rec["outcome"] = "tampered"
    _write(path, ls[:-1] + [json.dumps(rec, sort_keys=True).encode()])
    assert store.verify_chain(path)[0] is True


def test_the_run_log_reports_a_broken_chain_loudly(tmp_path):
    sch = _populate(tmp_path)
    path = sch.paths.run_chain("remind-me")
    ls = _lines(path)
    _write(path, ls[:1] + ls[2:])
    rl = sch.run_log()
    assert rl["chains"]["remind-me"]["ok"] is False and rl["chains"]["remind-me"]["breaks"][0]["line"] == 2


def test_concurrent_appenders_keep_the_chain_whole(tmp_path):
    import threading
    p = str(tmp_path / "c.jsonl")

    def w(n):
        for i in range(25):
            store.append_chained(p, {"w": n, "i": i})
    ts = [threading.Thread(target=w, args=(n,)) for n in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    ok, recs, br = store.verify_chain(p)
    assert ok and len(recs) == 150

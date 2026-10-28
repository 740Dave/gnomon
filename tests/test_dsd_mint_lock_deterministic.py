"""P3-F5: a DETERMINISTIC collision test for the cross-directory mint lock (no reliance on a lucky race).

Two instance dirs, one clock value (so both want the same id). Writer A is parked INSIDE the lock (its os.link is held); writer B must
BLOCK on the lock, and once A is released B must see A's id taken and bump its own, so the ids differ and nothing is overwritten. A
mutant with no lock lets B finish while A is parked, with the same id: this test fails it."""
import os
import threading
import time

from gnomon.hub import DSDStore

CLOCK = lambda: 1_700_000_000.123  # noqa: E731  (every call in the same millisecond)
GOOD = ("### D1 · Ship it?\n**What this is:** the release.\n**Why it matters:** the demo.\n"
        "| Option | Tradeoffs |\n|---|---|\n| **Yes** | fast |\n| No | slow |\n**Your call:** ____\n")


def _run(tmp_path, monkeypatch, lock_on=True):
    store = DSDStore(root=str(tmp_path), clock=CLOCK)
    if not lock_on:
        import contextlib
        monkeypatch.setattr(DSDStore, "_mint_lock", lambda self: contextlib.nullcontext())
    real_link = os.link
    a_in, a_go = threading.Event(), threading.Event()
    results = {}

    def link(src, dst, *a, **kw):
        if threading.current_thread().name == "writer-A" and not a_in.is_set():
            a_in.set()                                    # A is now inside the lock, about to publish
            assert a_go.wait(20)
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", link)

    def go(name, inst):
        results[name] = store.persist(inst, {"markdown": GOOD, "who": name})

    ta = threading.Thread(target=go, args=("A", "alpha-a"), name="writer-A")
    tb = threading.Thread(target=go, args=("B", "alpha-b"), name="writer-B")
    ta.start()
    assert a_in.wait(10)
    tb.start()
    time.sleep(0.4)                                       # B has had every chance to run
    b_done_while_a_parked = not tb.is_alive()
    a_go.set()
    ta.join(20)
    tb.join(20)
    return results, b_done_while_a_parked


def test_b_blocks_on_the_lock_then_bumps_its_id(tmp_path, monkeypatch):
    results, b_done = _run(tmp_path, monkeypatch, lock_on=True)
    assert not b_done, "writer B finished while A held the mint lock: the lock is not serialising"
    ida, idb = results["A"].dsd_id, results["B"].dsd_id
    assert ida != idb and int(idb[4:], 16) == int(ida[4:], 16) + 1
    for r in results.values():
        assert os.path.exists(r.path)


def test_a_mutant_without_the_lock_is_caught(tmp_path, monkeypatch):
    results, b_done = _run(tmp_path, monkeypatch, lock_on=False)
    assert b_done and results["A"].dsd_id == results["B"].dsd_id, "the control mutant must show the collision this test detects"

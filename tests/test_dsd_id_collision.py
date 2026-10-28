"""C5 (pre-review batch, spec section 3): ids are collision-free on the SHARED store path, with the id FORMAT unchanged.

The real instance: one DSD silently overwrote another because two ids were minted from the same millisecond. At 0464104 the store
ALREADY mints collision-free (``DSDStore.persist``: exclusive ``os.link`` publish under ``<root>/.dsd-mint.lock``; fix
), so these tests PIN that behaviour for the direct write and for the new verbs; they pass at
0464104 too (measured), they were not failing-before. The direct store write is also a case in the flag-OFF differential.
"""
from __future__ import annotations

import json
import os
import threading

import pytest

from gnomon.dsd import dsd_ops as OPS
from gnomon.hub.mcp import DSDStore
from test_dsd_ops import GOOD, TWO, ME, root, stored  # noqa: F401

# --------------------------------------------------------------------------- C5: collision-free ids on the shared store path

def test_two_sends_in_the_same_millisecond_get_different_ids_and_neither_is_lost(root):
    """The real instance: one DSD silently overwrote another because two ids were minted from the same millisecond."""
    clock = lambda: 1_790_000_000.123          # noqa: E731 - a frozen clock: every send mints the same millisecond
    a = OPS.send(ME, GOOD, root=root, clock=clock)
    b = OPS.send(ME, TWO, root=root, clock=clock)
    assert a["dsd_id"] != b["dsd_id"]
    assert stored(root, a["dsd_id"])["dsd"]["markdown"] == GOOD and stored(root, b["dsd_id"])["dsd"]["markdown"] == TWO
    import re
    assert all(re.fullmatch(r"dsd-[0-9a-f]+", x["dsd_id"]) for x in (a, b))       # the id FORMAT is unchanged


def test_the_direct_store_write_cannot_collide_either(root):
    """The unflagged shared path: DSDStore.persist itself (what the hub's send_dsd_for_human_review calls)."""
    store = DSDStore(root, clock=lambda: 1_790_000_000.5)
    r1, r2 = store.persist(ME, {"markdown": GOOD}), store.persist(ME, {"markdown": TWO})
    assert r1.dsd_id != r2.dsd_id and os.path.isfile(r1.path) and os.path.isfile(r2.path)
    other = store.persist("beta-agent", {"markdown": GOOD})          # another instance dir, same millisecond
    assert other.dsd_id not in (r1.dsd_id, r2.dsd_id)


def test_many_threads_filing_in_one_millisecond_all_survive(root):
    clock = lambda: 1_790_000_000.0            # noqa: E731
    ids, errs = [], []

    def go(i):
        try:
            ids.append(OPS.send(ME, GOOD.replace("Ship it?", f"Ship {i}?"), root=root, clock=clock)["dsd_id"])
        except Exception as exc:               # noqa: BLE001
            errs.append(exc)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(16)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errs and len(set(ids)) == 16
    assert len(os.listdir(os.path.join(root, ME, "dsd"))) == 16



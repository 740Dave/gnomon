"""DSDStore.persist safety: no id collision overwrite and
no silent zero-option decision (zero-options part).

The multi-process test runs GNOMON_DSD_COLLISION_ROUNDS rounds (default 30) of 4 processes x 25 persists into
ONE instance dir, started on a shared wall-clock gate so they collide in the same millisecond.
"""

import json
import os
import re
import subprocess
import sys
import time

import pytest

from gnomon.hub import DSDStore, SessionRegistry, GmMCPService
from gnomon.hub.mcp import ToolError
from gnomon.hub.registry import RecordingDownlink

ID_RE = re.compile(r"\Adsd-[0-9a-f]{6,32}\Z")          # the readers' format (notice.py / operator_activity.py)
ROUNDS = int(os.environ.get("GNOMON_DSD_COLLISION_ROUNDS", "30"))
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GOOD = (
    "### D1 · Ship it?\n"
    "**What this is:** the release.\n"
    "**Why it matters:** the demo.\n"
    "| Option | Tradeoffs |\n|---|---|\n| **Yes** | fast |\n| No | slow |\n"
    "**Your call:** ____\n"
)
ZERO_OPTS = (
    "### D1 · Ship it?\n"
    "**What this is:** the release.\n"
    "**Why it matters:** the demo.\n"
    "| Option | Tradeoffs |\n|---|---|\n| **Yes** | fast |\n"
    "**Your call:** ____\n\n"
    "### D2 · Grouped?\n"
    "**What this is:** the grouping.\n"
    "**Why it matters:** the operator sees nothing.\n"
    "| Option | What happens |\n|---|---|\n| A | x |\n| B | y |\n"
    "**Your call:** ____\n"
)


def _files(root, inst="alpha-x"):
    d = os.path.join(str(root), inst, "dsd")
    return sorted(n for n in os.listdir(d))


def test_tight_loop_of_50_persists_makes_50_distinct_intact_files_fixed_clock(tmp_path):
    store = DSDStore(root=str(tmp_path), clock=lambda: 1_700_000_000.123)   # every call in the SAME ms
    recs = [store.persist("alpha-x", {"n": i}) for i in range(50)]
    assert len({r.dsd_id for r in recs}) == 50
    names = _files(tmp_path)
    assert names == sorted(r.dsd_id + ".json" for r in recs)                 # 50 files, no temp debris
    for i, r in enumerate(recs):
        rec = json.load(open(r.path))
        assert rec["dsd"] == {"n": i} and rec["dsd_id"] == r.dsd_id
        assert ID_RE.match(r.dsd_id)


def test_tight_loop_of_50_persists_real_clock(tmp_path):
    store = DSDStore(root=str(tmp_path))
    recs = [store.persist("alpha-x", {"n": i}) for i in range(50)]
    assert len({r.dsd_id for r in recs}) == 50 and len(_files(tmp_path)) == 50
    assert all(json.load(open(r.path))["dsd"] == {"n": i} for i, r in enumerate(recs))


def test_pre_existing_file_is_never_overwritten(tmp_path):
    d = tmp_path / "alpha-x" / "dsd"
    d.mkdir(parents=True)
    ms = 1_700_000_000_123
    victim = d / f"dsd-{ms:x}.json"
    victim.write_bytes(b'{"precious": true}\n')
    r = DSDStore(root=str(tmp_path), clock=lambda: ms / 1000).persist("alpha-x", {"n": 1})
    assert victim.read_bytes() == b'{"precious": true}\n'
    assert r.dsd_id == f"dsd-{ms + 1:x}"


def test_id_is_not_reused_across_instance_dirs(tmp_path):
    store = DSDStore(root=str(tmp_path), clock=lambda: 1_700_000_000.5)
    a = store.persist("gn-a", {"n": 1})
    b = store.persist("gn-b", {"n": 2})          # readers look an id up across ALL instance dirs
    assert a.dsd_id != b.dsd_id


def test_well_formed_dsd_record_is_byte_identical_to_the_pre_change_format(tmp_path):
    ts = 1_700_000_000.123
    r = DSDStore(root=str(tmp_path), clock=lambda: ts, doc_roots=[]).persist("alpha-x", {"markdown": GOOD})
    expected = json.dumps({"dsd_id": r.dsd_id, "instance_id": "alpha-x", "created_epoch": ts,
                           "status": "pending-human-review", "dsd": {"markdown": GOOD}}, indent=2) + "\n"
    assert r.dsd_id == f"dsd-{int(ts * 1000):x}"
    assert open(r.path, "rb").read() == expected.encode("utf-8")
    assert (os.stat(r.path).st_mode & 0o777) == (0o666 & ~_umask())


def _umask():
    m = os.umask(0)
    os.umask(m)
    return m


_WORKER = """
import sys, time
sys.path.insert(0, {repo!r})
from gnomon.hub import DSDStore
root, wid, n, gate = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
store = DSDStore(root=root)
while time.time() < gate:
    pass
for i in range(n):
    store.persist("alpha-x", {{"w": wid, "i": i}})
"""


@pytest.mark.parametrize("rnd", range(ROUNDS))
def test_multiprocess_4x25_persists_lose_nothing(tmp_path, rnd):
    procs = []
    gate = time.time() + 1.0
    for w in range(4):
        procs.append(subprocess.Popen([sys.executable, "-c", _WORKER.format(repo=REPO), str(tmp_path), str(w), "25", repr(gate)]))
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    names = _files(tmp_path)
    assert len(names) == 100, f"{100 - len(names)} DSD(s) lost (overwritten) in round {rnd}"
    seen = set()
    for n in names:
        assert n.endswith(".json") and ID_RE.match(n[:-5]), n
        rec = json.load(open(tmp_path / "alpha-x" / "dsd" / n))
        assert rec["dsd_id"] == n[:-5]
        seen.add((rec["dsd"]["w"], rec["dsd"]["i"]))
    assert seen == {(w, i) for w in range(4) for i in range(25)}


# ---- zero options -----------------------------------------------------------

def test_direct_persist_refuses_a_zero_option_decision_naming_it_and_the_header(tmp_path):
    store = DSDStore(root=str(tmp_path))
    with pytest.raises(ToolError) as ei:
        store.persist("alpha-x", {"markdown": ZERO_OPTS})
    msg = str(ei.value)
    assert "D2" in msg and "| Option | Tradeoffs |" in msg
    assert "D1 ·" not in msg                        # D1 parsed fine, so it is not named
    assert not (tmp_path / "alpha-x").exists() or _files(tmp_path) == []


def test_hub_send_path_refuses_the_same_dsd_with_the_same_message(tmp_path):
    reg = SessionRegistry()
    reg.register("alpha-x", RecordingDownlink())
    service = GmMCPService(reg, DSDStore(root=str(tmp_path)))
    out = service.dispatch("send_dsd_for_human_review", {"instance_id": "alpha-x", "dsd": {"markdown": ZERO_OPTS}})
    assert out["ok"] is False and "D2" in out["error"] and "| Option | Tradeoffs |" in out["error"]
    assert list(tmp_path.rglob("*.json")) == []
    # the hub path reaches persist, which holds the ONE predicate: same decision named, same header
    from gnomon.dsd.model import parse_dsd, zero_options_message
    assert zero_options_message(parse_dsd(ZERO_OPTS)) in out["error"]


def test_hub_send_path_still_persists_a_well_formed_dsd(tmp_path):
    reg = SessionRegistry()
    reg.register("alpha-x", RecordingDownlink())
    service = GmMCPService(reg, DSDStore(root=str(tmp_path)))
    out = service.dispatch("send_dsd_for_human_review", {"instance_id": "alpha-x", "dsd": {"markdown": GOOD}})
    assert out["ok"] is True and ID_RE.match(out["dsd_id"])


def test_non_markdown_and_decisionless_payloads_are_unaffected(tmp_path):
    store = DSDStore(root=str(tmp_path))
    store.persist("alpha-x", {"question": "ship?", "options": ["y", "n"]})
    store.persist("alpha-x", {"markdown": "just prose, no decisions"})
    assert len(_files(tmp_path)) == 2


# ---- cross-dir uniqueness (P2-1): scan-then-link must be atomic across instance dirs --------------------------

_XWORKER = """
import sys, time
sys.path.insert(0, {repo!r})
from gnomon.hub import DSDStore
root, inst, n, gate = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4])
store = DSDStore(root=root, clock=lambda: 1_700_000_000.123)      # frozen: every writer wants the same id
while time.time() < gate:
    pass
for i in range(n):
    store.persist(inst, {{"i": i}})
"""


def _all_ids(root):
    ids = []
    for inst in os.listdir(str(root)):
        d = os.path.join(str(root), inst, "dsd")
        if os.path.isdir(d):
            ids += [n[:-5] for n in os.listdir(d) if n.endswith(".json")]
    return ids


@pytest.mark.parametrize("rnd", range(ROUNDS))
def test_multiprocess_across_instance_dirs_ids_are_globally_unique(tmp_path, rnd):
    gate = time.time() + 1.0
    procs = [subprocess.Popen([sys.executable, "-c", _XWORKER.format(repo=REPO), str(tmp_path), f"gn-{w % 3}", "10", repr(gate)])
             for w in range(4)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0, 0]
    ids = _all_ids(tmp_path)
    assert len(ids) == 40 and len(set(ids)) == 40, f"{40 - len(set(ids))} duplicate id(s) across dirs in round {rnd}"


@pytest.mark.parametrize("rnd", range(ROUNDS))
def test_threads_across_instance_dirs_ids_are_globally_unique(tmp_path, rnd):
    import threading
    store = DSDStore(root=str(tmp_path), clock=lambda: 1_700_000_000.123)
    go = threading.Barrier(6)

    def work(k):
        go.wait()
        for i in range(10):
            store.persist(f"gn-{k % 3}", {"k": k, "i": i})

    ts = [threading.Thread(target=work, args=(k,)) for k in range(6)]
    [t.start() for t in ts]
    [t.join(60) for t in ts]
    ids = _all_ids(tmp_path)
    assert len(ids) == 60 and len(set(ids)) == 60, f"{60 - len(set(ids))} duplicate id(s) across dirs in round {rnd}"
    assert not [n for i in os.listdir(tmp_path) if i.startswith("gn-") for n in os.listdir(tmp_path / i / "dsd") if n.endswith(".tmp")]

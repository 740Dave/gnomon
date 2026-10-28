"""Helper (NOT a test): a scratch data root for the Work tab tests and screenshots. A fake clock; nothing outside ``root`` is touched.

Agents (``known`` = the ones Gnomon still lists as profiles):
  alpha    open items in every state the tab shows (waiting on the operator with and without a DSD, standing at 15 min, paused, held,
           hold stored but not in effect, 'no reminder', markup in a title and a note)
  beta     known, no list at all                      -> "no open items"
  gamma    known, its session cannot call the verbs   -> "verbs not available"
  delta    known, its log fails verification          -> the list's fault
  epsilon  NOT known (retired), has a list            -> the Retired group
  zeta     known, a log over 5 MB                     -> the log warning
The declared status is written by ``set_status`` (away / in-office / lost).
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from gnomon.worklist import api, constants as C, store  # noqa: E402
from gnomon.worklist import status as owl_status  # noqa: E402
from gnomon.worklist.verbs import WorkVerbs  # noqa: E402

T0 = 1_800_000_000.0
NOW = T0 + 20 * 3600.0
MARKUP = '<img src=x onerror=alert(1)> <script>alert(2)</script> **bold** &amp;'
KNOWN = ["alpha", "beta", "gamma", "delta", "zeta"]


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def seed(root, deploy):
    """Builds the data root. Returns {'data_root', 'deploy_root', 'known', 'now'}."""
    data = os.path.join(root, "data")
    os.makedirs(os.path.join(deploy, "defaults"), exist_ok=True)
    ck = Clock(T0)
    a = WorkVerbs("alpha", data, ck, deploy_root=deploy, operator_label="Sam")

    def at(t):
        ck.t = t
        return a
    at(T0).work_add({"title": "answer the schema question", "waiting_on": "operator", "links": {"blocked_by": "dsd-1a0e000001"}})        # w-1
    at(T0 + 3 * 3600).work_add({"title": "which corpus do we use", "waiting_on": "operator"})                                         # w-2 (no DSD)
    at(T0 + 4 * 3600).work_add({"title": "nightly run watcher", "waiting_on": "process:nightly run", "standing": True,
                                "remind_every_min": 15})                                                                              # w-3 short + standing
    at(T0 + 5 * 3600).work_add({"title": "the long drift", "waiting_on": "self", "remind_every_min": 15})                              # w-4 (paused below)
    at(T0 + 6 * 3600).work_add({"title": "held until he is back", "waiting_on": "operator", "hold_until_return": True})               # w-5 (held)
    at(T0 + 7 * 3600).work_add({"title": "no reminder please", "waiting_on": "self", "remind_every_min": "none"})                     # w-6
    at(T0 + 8 * 3600).work_add({"title": "hold stored only", "waiting_on": "self", "hold_until_return": True,
                                "links": {"requested_by": "requester"}, "note": "stored, not in effect"})                             # w-7
    at(T0 + 9 * 3600).work_add({"title": MARKUP, "waiting_on": "agent:beta", "note": MARKUP, "remind_every_min": 120})               # w-8
    at(T0 + 10 * 3600).work_add({"title": "done soon", "waiting_on": "self"})                                                        # w-9
    at(T0 + 10 * 3600 + 60).work_done({"id": "w-9"})
    path = store.log_path(data, "alpha")
    for k in range(8):                                                   # eight reminders on w-4 with no update -> paused
        store.transact(path, T0 + 5 * 3600 + (k + 1) * 900.0, lambda items, n: ("remind", {"ids": ["w-4"]}))
    for k in (2, 1, 0):                                                   # a few recent reminders on w-3 (cost, last 24 h), oldest first
        store.transact(path, NOW - 3600.0 * (k + 1), lambda items, n: ("remind", {"ids": ["w-3"]}))
    ck2 = Clock(T0 + 12 * 3600)
    WorkVerbs("epsilon", data, ck2, deploy_root=deploy).work_add({"title": "left behind by a retired agent", "waiting_on": "self"})
    WorkVerbs("delta", data, ck2, deploy_root=deploy).work_add({"title": "will be damaged", "waiting_on": "self"})
    dp = store.log_path(data, "delta")
    raw = open(dp, "rb").read().replace(b"will be damaged", b"will be DAMAGED")
    open(dp, "wb").write(raw)
    api.mark_verbs_unavailable(data, "gamma", "tools line lacks work_add, work_list", T0)
    WorkVerbs("zeta", data, ck2, deploy_root=deploy).work_add({"title": "a very long log", "waiting_on": "self"})
    _pad_log(store.log_path(data, "zeta"), 5 * 1024 * 1024 + 4096)
    return {"data_root": data, "deploy_root": deploy, "known": list(KNOWN), "now": NOW, "label": "Sam"}


def _pad_log(path, target_bytes):
    """Append VALID chained 'update' events until the file passes ``target_bytes`` (built directly: transact() would re-verify every time)."""
    r = store.read_events(path)
    prev, seq = r.head_hash, r.head_seq + 1
    lines = []
    size = r.size
    while size < target_bytes:
        line, prev = store._line(seq, T0 + 13 * 3600, "update", {"ids": ["w-1"], "changes": {"note": "x" * 60}}, prev)
        lines.append(line)
        size += len(line)
        seq += 1
    with open(path, "ab") as fh:
        fh.write("".join(lines).encode())
    head = store.read_head(path) or {}
    store._write_head(path, seq - 1, prev, int(head.get("next_id", 2)), 0, None, True)


def set_status(data_root, kind, now=NOW):
    p = owl_status.status_path(data_root)
    for f in (p, owl_status.prev_path(data_root), owl_status.marker_path(data_root)):
        if os.path.lexists(f):
            os.unlink(f)
    if kind == "never":
        return
    if kind in ("away", "away-old", "in-office", "lost"):
        t = now - (3 * 3600.0 if kind in ("away", "in-office", "lost") else 49 * 3600.0)
        owl_status.write_status(data_root, state="in-office" if kind == "in-office" else "away",
                                text="" if kind == "in-office" else "at the dentist, reachable by remote-control",
                                source="" if kind == "in-office" else "transcript line 41", setter_id="operator", setter_name="Sam", now=t)
        if kind == "lost":
            os.unlink(p)

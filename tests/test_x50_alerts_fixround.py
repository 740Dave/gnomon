"""fix round (Diamond audit-x50-alerts-2026-10-03 F1/F3/F4/F5/F6): each test below was written RED first
against 8c41731 (the merged build) and fails there; see docs/build-logs/RUN_SUMMARY-x50-fixround.md.

AUTOMATED: torn log tail, producer failure loudness, non-regular store files (a FIFO never hangs), the pump-thread
hand-off (bounded queue, drop-and-count), the macOS budget classes, Dismiss All by displayed ids. LIVE-ONLY: a real
macOS notification, the column in the operator's browser.
"""

import json
import logging
import os
import subprocess
import sys
import textwrap
import threading
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from hooks_support import Rig  # noqa: E402
from gnomon import alerts as A  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402

T0 = 1_800_000_000.0
BASE = "http://127.0.0.1:8770"
J = {"Content-Type": "application/json"}


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv(A.ENV_DIR, str(tmp_path / "alerts-env"))
    monkeypatch.setattr(A, "_macos_runner", lambda argv: None)
    _reset = getattr(A, "reset_producer_state", lambda: None)
    _reset()
    yield
    _reset()


@pytest.fixture
def store(tmp_path):
    return A.AlertStore(str(tmp_path / "alerts"))


# == F1: a torn tail never swallows the next alert ===============================================================


def test_f1_torn_tail_does_not_swallow_the_next_alert(store):
    os.makedirs(store.dir)
    with open(store.alerts_path, "w") as fh:
        fh.write('{"id":"a-old","ts":%s,"source":"s","key":"k","severity":"info","title":"old","body":"","link":null}\n' % T0
                 + '{"id":"a-torn","ts":1,"source":"s","title":"x"')          # no newline: a crash mid-write
    r = A.notify("s", "info", "fresh", store=store, now=T0 + 5, macos=False)
    assert r["status"] == "raised"
    assert [a["title"] for a in store.active(now=T0 + 6)] == ["fresh", "old"]    # raised AND visible
    raw = open(store.alerts_path, "rb").read()
    assert raw.endswith(b"\n") and raw.count(b"\n") == 3                          # the torn line stayed on its own line


def test_f1_torn_dismissed_tail_does_not_lose_a_dismissal(store):
    a = A.notify("s", "info", "one", store=store, now=T0, macos=False)
    with open(store.dismissed_path, "w") as fh:
        fh.write('{"ts":1,"ids":["zz')
    assert store.dismiss(a["id"], now=T0 + 1) is True
    assert store.active(now=T0 + 2) == []


def test_f1_reader_tolerates_a_torn_tail_and_a_lone_partial_file(store):
    os.makedirs(store.dir)
    with open(store.alerts_path, "w") as fh:
        fh.write('{"id":"a1","ts":%s,"source":"s","key":"k","severity":"info","title":"ok","body":"","link":null}\n{"id":"a2","ts' % T0)
    assert [a["id"] for a in store.active(now=T0 + 1)] == ["a1"]
    with open(store.alerts_path, "w") as fh:
        fh.write('{"id":"only-par')
    assert store.active(now=T0 + 1) == []


# == F4: a non-regular store file is refused, never opened blocking ===============================================


def _run_py(code, timeout):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "PYTHONPATH": _ROOT})


@pytest.mark.parametrize("name", ["alerts.jsonl", "alerts.lock"])
def test_f4_fifo_at_a_store_path_is_refused_with_a_clear_error_and_never_hangs(tmp_path, name):
    d = tmp_path / "fifo-alerts"
    d.mkdir()
    os.mkfifo(str(d / name))
    code = f"""
        import json, time
        from gnomon import alerts as A
        st = A.AlertStore({str(d)!r})
        t = time.time()
        r1 = A.notify("s", "info", "x", store=st, macos=False)
        r2 = A.notify("s", "info", "y", store=st, macos=False)     # a second call is not blocked behind the first
        print(json.dumps([r1, r2, time.time() - t]))
    """
    try:
        p = _run_py(code, 20)
    except subprocess.TimeoutExpired:
        pytest.fail("notify hung on a FIFO at " + name)
    assert p.returncode == 0, p.stderr
    r1, r2, dt = json.loads(p.stdout)
    assert r1["ok"] is False and r1["status"] == "error" and "not a regular file" in r1["error"]
    assert r2["status"] == "error" and dt < 10


def test_f4_fifo_as_dismissed_file_makes_active_and_dismiss_refuse_without_hanging(tmp_path):
    d = tmp_path / "fifo-dismissed"
    d.mkdir()
    os.mkfifo(str(d / "dismissed.jsonl"))
    code = f"""
        import json, time
        from gnomon import alerts as A
        st = A.AlertStore({str(d)!r})
        r = A.notify("s", "info", "x", store=st, macos=False)          # does not read dismissed.jsonl: fine
        out = [r["status"]]
        for fn in (st.active, lambda: st.dismiss(r["id"]), lambda: st.dismiss_all([r["id"]])):
            try:
                fn(); out.append("no-error")
            except A.AlertStoreError as exc:
                out.append("not a regular file" in str(exc))
        print(json.dumps(out))
    """
    try:
        p = _run_py(code, 20)
    except subprocess.TimeoutExpired:
        pytest.fail("a store call hung on a FIFO at dismissed.jsonl")
    assert p.returncode == 0, p.stderr
    assert json.loads(p.stdout) == ["raised", True, True, True]


def test_f4_directory_and_symlink_at_a_store_path_are_refused(tmp_path):
    d = tmp_path / "odd"
    d.mkdir()
    (d / "alerts.jsonl").mkdir()
    r = A.notify("s", "info", "x", store=A.AlertStore(str(d)), macos=False)
    assert r["status"] == "error" and "not a regular file" in r["error"]
    d2 = tmp_path / "odd2"
    d2.mkdir()
    target = tmp_path / "target.txt"
    target.write_text("keep\n")
    os.symlink(str(target), str(d2 / "alerts.jsonl"))
    r = A.notify("s", "info", "x", store=A.AlertStore(str(d2)), macos=False)
    assert r["status"] == "error" and target.read_text() == "keep\n"            # the symlink was not followed


def test_f4_a_lock_held_elsewhere_gives_up_after_a_short_budget(tmp_path, monkeypatch):
    import fcntl
    monkeypatch.setattr(A, "LOCK_WAIT_S", 0.3)
    st = A.AlertStore(str(tmp_path / "busy"))
    os.makedirs(st.dir)
    fd = os.open(os.path.join(st.dir, A.LOCK_FILE), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        t = time.time()
        r = A.notify("s", "info", "x", store=st, macos=False)
        assert r["status"] == "error" and "busy" in r["error"] and time.time() - t < 5
    finally:
        os.close(fd)


# == F4: the pump never blocks on alert I/O ======================================================================


from test_modal_hold_choke_point import _append, _bus, _row, paint, pump, _guard, ANSI_PERM  # noqa: E402


def _stuck_rig(tmp_path, extra):
    h = {"dsd_notice_boot_grace_seconds": 0.0, "dsd_notice_operator_quiet_seconds": 0.0}
    h.update(extra)
    return Rig(tmp_path, harness=h)


def _go_stuck(rig):
    t = {"n": 1000.0}
    rig.gate._modal_clock = lambda: t["n"]
    g = _guard(rig)
    gt = {"n": 1000.0}
    g._clock = lambda: gt["n"]
    _append(rig, _row("use", "t1", "Bash"))
    paint(rig, ANSI_PERM)
    _bus(rig, "CR-INTO-PERMISSION")
    rig.operator_screen()
    t["n"] += 1801
    t0 = time.time()
    pump(rig)
    return time.time() - t0


def test_f4_stuck_hold_hands_off_to_a_worker_so_the_pump_never_blocks(tmp_path, monkeypatch):
    release = threading.Event()
    entered = threading.Event()
    real = A.notify

    def slow_notify(*a, **k):
        entered.set()
        release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(A, "notify", slow_notify)
    adir = str(tmp_path / "pump-alerts")
    rig = _stuck_rig(tmp_path, {"alerts": True, "alerts_dir": adir})
    try:
        dt = _go_stuck(rig)
        assert dt < 3, f"the pump waited {dt:.1f}s on alert I/O"
        assert len([r for r in rig.records("modal-guard") if r.get("what") == "stuck-hold"]) == 1
        assert entered.wait(5)                                  # the worker did get the job
    finally:
        release.set()
        rig.close()
    assert A.drain(10)
    assert len(A.AlertStore(adir).active()) == 1                # and the alert still lands


def test_f4_stuck_hold_with_a_fifo_store_does_not_hang_the_pump(tmp_path):
    adir = tmp_path / "pump-fifo"
    adir.mkdir()
    os.mkfifo(str(adir / "alerts.jsonl"))
    rig = _stuck_rig(tmp_path, {"alerts": True, "alerts_dir": str(adir)})
    try:
        dt = _go_stuck(rig)
        assert dt < 3
        assert b"HOLD STUCK" in rig.operator_screen() and len(rig.gate.held()) == 1
        assert A.drain(10)
        assert A.producer_counters()["failures"].get("stuck-hold", 0) == 1      # loud, not silent
    finally:
        rig.close()


def test_f4_queue_is_bounded_and_overflow_is_dropped_and_counted(monkeypatch):
    gate = threading.Event()
    monkeypatch.setattr(A, "notify", lambda *a, **k: (gate.wait(10), {"ok": True, "status": "raised"})[1])
    monkeypatch.setattr(A, "PRODUCER_QUEUE_MAX", 3)
    getattr(A, "reset_producer_state", lambda: None)()
    try:
        t = time.time()
        res = [A.submit_producer("stuck-hold", "harness:x", "warning", f"t{i}") for i in range(20)]
        assert time.time() - t < 2                                              # never blocks the caller
        dropped = res.count(False)
        assert dropped >= 20 - 3 - 1 and res.count(True) >= 3                   # 3 queued (+1 in the worker's hand)
        assert A.producer_counters()["dropped"].get("stuck-hold", 0) == dropped
    finally:
        gate.set()
    assert A.drain(10)


# == F3: producers fail LOUD =====================================================================================


def test_f3_stuck_hold_failure_logs_one_line_and_bumps_the_counter(tmp_path, caplog, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    rig = _stuck_rig(tmp_path, {"alerts": True, "alerts_dir": str(blocker / "sub")})
    try:
        with caplog.at_level(logging.WARNING, logger="gnomon.alerts"):
            _go_stuck(rig)
            assert A.drain(10)
        lines = [r.getMessage() for r in caplog.records if "stuck-hold" in r.getMessage()]
        assert len(lines) == 1 and "error" in lines[0].lower()
        assert A.producer_counters()["failures"] == {"stuck-hold": 1}
        assert len(rig.gate.held()) == 1 and b"HOLD STUCK" in rig.operator_screen()   # the pump kept running
    finally:
        rig.close()


@pytest.fixture(autouse=True)
def _caplog_sees_the_non_propagating_alerts_logger(caplog):
    """F-R5: the alerts logger no longer propagates to root (its own stderr handler is the emitter), so caplog's
    root handler is attached to it directly for the tests that read log records."""
    lg = logging.getLogger("gnomon.alerts")
    lg.addHandler(caplog.handler)
    yield
    lg.removeHandler(caplog.handler)


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = ConfigStore(str(tmp_path / "config.json"))
    cfg.save(GnomonConfig(flags={A.FLAG_NAME: True}))
    st = A.AlertStore(str(tmp_path / "web-alerts"))
    monkeypatch.setattr(srv, "_config_store", cfg)
    monkeypatch.setattr(srv, "_alerts_store_override", st)
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._ALERTS_CSRF})
    return c, st


class _Item:
    def __init__(self, dsd_id, inst):
        self.dsd_id, self.instance_id, self.kind, self.title = dsd_id, inst, "dsd", "a decision"

    def to_dict(self):
        return {"dsd_id": self.dsd_id, "instance_id": self.instance_id, "kind": self.kind, "title": self.title}


class _Watcher:
    def __init__(self, items):
        self.items = items

    def poll(self):
        out, self.items = self.items, []
        return out


def test_f3_dsd_producer_failure_is_logged_counted_and_shown_by_the_api(world, monkeypatch, caplog):
    c, st = world
    monkeypatch.setattr(srv, "get_dsd_watcher", lambda: _Watcher([_Item("dsd-9", "ds")]))
    with monkeypatch.context() as m:
        m.setattr(A, "notify", lambda *a, **k: {"ok": False, "status": "error", "error": "OSError: disk gone"})
        with caplog.at_level(logging.WARNING, logger="gnomon.alerts"):
            r = c.get("/api/dsd/arrivals")
    assert r.status_code == 200 and r.json()["arrivals"][0]["dsd_id"] == "dsd-9"       # the poll is untouched
    assert [x for x in caplog.records if "dsd-arrival" in x.getMessage() and "disk gone" in x.getMessage()]
    assert A.producer_counters()["failures"] == {"dsd-arrival": 1}
    body = c.get("/api/alerts").json()                 # the column's data carries the count
    assert body["producer_failures"]["this_process"] == {"dsd-arrival": 1} and body["producer_failures"]["total"] >= 1
    assert body["producer_failures"]["by_producer"] == {"dsd-arrival": 1}               # and the cross-process file


def test_f3_a_raising_notify_never_escapes_the_producer(monkeypatch):
    monkeypatch.setattr(A, "notify", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    A.raise_from_producer("dsd-arrival", "dsd", "info", "t")
    assert A.producer_counters()["failures"] == {"dsd-arrival": 1}


# == F5: the macOS budget has classes ============================================================================


def test_f5_one_agent_cannot_starve_the_harness_macos_budget(store, monkeypatch):
    calls = []
    monkeypatch.setattr(A, "_macos_runner", lambda argv: calls.append(argv))
    got = [A.notify("agent:noisy", "warning", f"w{i}", store=store, now=T0 + i, rate_max=A.AGENT_RATE_MAX)["macos"]
           for i in range(5)]
    assert got.count("posted") == A.MACOS_AGENT_PER_SOURCE and got.count("rate-limited") == 5 - A.MACOS_AGENT_PER_SOURCE
    r = A.notify("harness:alpha-test", "warning", "stuck", store=store, now=T0 + 10)
    assert r["macos"] == "posted"
    # and another agent still gets its own share
    assert A.notify("agent:other", "warning", "ow", store=store, now=T0 + 11)["macos"] == "posted"
    assert len([a for a in store.active(now=T0 + 12)]) == 7                    # every alert is in the column regardless


def test_f5_agent_class_total_is_bounded_and_system_class_is_separate(store, monkeypatch):
    monkeypatch.setattr(A, "_macos_runner", lambda argv: None)
    res = [A.notify(f"agent:a{i}", "error", "e", store=store, now=T0 + i)["macos"] for i in range(A.MACOS_MAX + 3)]
    assert res.count("posted") == A.MACOS_MAX
    assert A.notify("dsd", "error", "d", store=store, now=T0 + 20)["macos"] == "posted"


# == F6: Dismiss All dismisses exactly the displayed ids =========================================================


def test_f6_dismiss_all_only_dismisses_the_ids_the_client_displayed(world):
    c, st = world
    shown = A.notify("s", "info", "shown", store=st, now=time.time(), macos=False)
    seen = [a["id"] for a in c.get("/api/alerts").json()["alerts"]]
    assert seen == [shown["id"]]
    A.notify("s", "info", "late1", store=st, now=time.time() + 1, macos=False)
    A.notify("s", "info", "late2", store=st, now=time.time() + 2, macos=False)
    r = c.post("/api/alerts/dismiss-all", headers=J, content=json.dumps({"ids": seen}).encode())
    assert r.json() == {"ok": True, "dismissed": 1}
    assert sorted(a["title"] for a in c.get("/api/alerts").json()["alerts"]) == ["late1", "late2"]


def test_f6_dismiss_all_without_an_id_list_is_refused_and_changes_nothing(world):
    c, st = world
    A.notify("s", "info", "keep", store=st, macos=False)
    for body in (b"{}", b'{"ids": "x"}', b'{"ids": [1]}', b"not json", b'{"ids": ' + b"[" + b'"a",' * 300 + b'"b"]}'):
        assert c.post("/api/alerts/dismiss-all", headers=J, content=body).status_code == 400
    assert len(st.active()) == 1


def test_f6_dismiss_all_ignores_unknown_and_already_dismissed_ids(store):
    a = A.notify("s", "info", "a", store=store, now=T0, macos=False)
    b = A.notify("s", "info", "b", store=store, now=T0 + 1, macos=False)
    store.dismiss(a["id"], now=T0 + 2)
    assert store.dismiss_all([a["id"], "nope"], now=T0 + 3) == 0
    assert [x["id"] for x in store.active(now=T0 + 4)] == [b["id"]]


# == P3 one-liners: F7 invisible characters, F8 lone surrogates ====================================================


def test_p3_f7_invisible_format_characters_are_stripped():
    assert A.clean_text("a\u200bb\u2060c\u00add\U000e0041e", 50) == "abcde"


def test_p3_f8_a_lone_surrogate_cannot_make_notify_error(store):
    r = A.notify("s", "info", "bad\ud800title", "b\udfff", store=store, now=T0, macos=False)
    assert r["status"] == "raised" and store.active(now=T0 + 1)[0]["title"] == "badtitle"

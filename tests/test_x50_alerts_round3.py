"""round 3 (delta Diamond audit-x50-alerts-delta-2026-10-03: F-D1, F-D2, F-D3). Written RED first against
ec85e1b (the merge of live c803fa6 onto the fix round), see docs/build-logs/RUN_SUMMARY-x50-round3.md.

AUTOMATED: the failure log line reaches the stderr of the process that hit it; a harness process's failures and
drops reach the web server's /api/alerts through producer-failures.jsonl (separate PROCESSES, so the web
process's in-memory counters are provably empty); an unusable store directory is a clear API body; the failures
file shares the ONE append helper (fresh-line repair) and a torn-tail-tolerant reader; the count is windowed to
24 h and the file rotates so it never freezes. The page showing the error banner is in test_x50_alerts_ui.py.
"""

import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(_ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon import alerts as A  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402

T0 = 1_800_000_000.0
BASE = "http://127.0.0.1:8770"


@pytest.fixture(autouse=True)
def _isolation(tmp_path, monkeypatch):
    monkeypatch.setenv(A.ENV_DIR, str(tmp_path / "alerts-env"))
    monkeypatch.setattr(A, "_macos_runner", lambda argv: None)
    getattr(A, "reset_producer_state", lambda: None)()


@pytest.fixture
def world(tmp_path, monkeypatch):
    cfg = ConfigStore(str(tmp_path / "config.json"))
    cfg.save(GnomonConfig(flags={A.FLAG_NAME: True}))
    st = A.AlertStore(str(tmp_path / "web-alerts"))
    monkeypatch.setattr(srv, "_config_store", cfg)
    monkeypatch.setattr(srv, "_alerts_store_override", st)
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._ALERTS_CSRF})
    return c, st


def _py(code, timeout=60):
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, "PYTHONPATH": _ROOT, "GNOMON_ALERTS_DIR": ""})


# == F-D1(a): the log line is emitted ============================================================================


def test_fd1a_a_producer_failure_line_reaches_stderr_of_the_process_that_hit_it(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    p = _py(f"""
        from gnomon import alerts as A
        A.raise_from_producer("stuck-hold", "harness:x", "warning", "t", store=A.AlertStore({str(blocker / 'sub')!r}))
    """)
    assert p.returncode == 0, p.stderr
    assert "alert producer stuck-hold failed" in p.stderr and "not usable" in p.stderr, p.stderr
    assert p.stderr.count("alert producer stuck-hold failed") == 1                       # ONE line


def test_fd1a_a_drop_line_reaches_stderr_too(tmp_path):
    p = _py(f"""
        import threading
        from gnomon import alerts as A
        g = threading.Event()
        A.notify = lambda *a, **k: (g.wait(5), {{"ok": True, "status": "raised"}})[1]
        A.PRODUCER_QUEUE_MAX = 2
        st = A.AlertStore({str(tmp_path / 'd')!r})
        for i in range(8):
            A.submit_producer("stuck-hold", "harness:x", "warning", f"t{{i}}", store=st)
        g.set(); A.drain(10)
    """)
    assert p.returncode == 0, p.stderr
    assert "alert producer stuck-hold dropped" in p.stderr, p.stderr


# == F-D1(b): a harness process's failures and drops reach the web column =========================================


def test_fd1b_failures_and_drops_from_another_process_reach_api_alerts(world, tmp_path):
    c, st = world
    p = _py(f"""
        import threading
        from gnomon import alerts as A
        st = A.AlertStore({st.dir!r})
        A.raise_from_producer("stuck-hold", "harness:x", "warning", "", store=st)       # invalid (empty title)
        g = threading.Event()
        real = A.notify
        A.notify = lambda *a, **k: (g.wait(5), {{"ok": True, "status": "raised"}})[1]
        A.PRODUCER_QUEUE_MAX = 2
        for i in range(12):
            A.submit_producer("stuck-hold", "harness:x", "warning", f"t{{i}}", store=st)
        g.set(); assert A.drain(10)
    """)
    assert p.returncode == 0, p.stderr
    assert A.producer_counters() == {"failures": {}, "dropped": {}}                    # THIS process saw nothing
    pf = c.get("/api/alerts").json()["producer_failures"]
    assert pf["this_process"] == {} and pf["dropped"] == {}
    assert pf["by_producer"]["stuck-hold"] >= 1 + 8 and pf["total"] == pf["by_producer"]["stuck-hold"]


def test_fd1b_the_drop_is_never_written_on_the_callers_thread(tmp_path, monkeypatch):
    import threading
    gate = threading.Event()
    monkeypatch.setattr(A, "notify", lambda *a, **k: (gate.wait(10), {"ok": True, "status": "raised"})[1])
    monkeypatch.setattr(A, "PRODUCER_QUEUE_MAX", 1)
    st = A.AlertStore(str(tmp_path / "d"))
    caller = threading.get_ident()
    seen = []
    real = A.AlertStore.record_failure

    def spy(self, *a, **k):
        seen.append(threading.get_ident())
        return real(self, *a, **k)
    monkeypatch.setattr(A.AlertStore, "record_failure", spy)
    try:
        for i in range(6):
            A.submit_producer("stuck-hold", "harness:x", "warning", f"t{i}", store=st)
    finally:
        gate.set()
    assert A.drain(10)
    assert seen and caller not in seen


# == F-D1(c): an unusable store directory is a clear error body ==================================================


def test_fd1c_unwritable_alerts_dir_gives_a_clear_error_body_not_a_bare_500(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    cfg = ConfigStore(str(tmp_path / "config.json"))
    cfg.save(GnomonConfig(flags={A.FLAG_NAME: True}))
    monkeypatch.setattr(srv, "_config_store", cfg)
    monkeypatch.setattr(srv, "_alerts_store_override", A.AlertStore(str(blocker / "sub")))
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._ALERTS_CSRF},
                   raise_server_exceptions=False)
    r = c.get("/api/alerts")
    assert r.status_code == 200
    body = r.json()
    assert body["alerts"] == [] and "not usable" in body["store_error"] and "blocker" in body["store_error"]


# == F-D2: ONE append helper, fresh-line repair, torn-tail-tolerant reader ========================================


def test_fd2_torn_tail_in_the_failures_file_does_not_swallow_the_next_failure(tmp_path):
    st = A.AlertStore(str(tmp_path / "a"))
    os.makedirs(st.dir)
    with open(st.failures_path, "w") as fh:
        fh.write('{"ts":%s,"producer":"old","kind":"failed","n":1,"error":"e"}\n{"ts":%s,"producer":"tor' % (T0, T0))
    st.record_failure("real", "boom", now=T0 + 1)
    assert st.failure_counts(now=T0 + 2) == {"old": 1, "real": 1}


def test_fd2_all_three_files_share_the_one_append_helper():
    import inspect
    src = inspect.getsource(A)
    assert src.count("os.O_APPEND") == 1                      # the only O_APPEND open is inside _append_jsonl
    for fn in (A.AlertStore.record_failure, A.AlertStore.dismiss, A.AlertStore.dismiss_all):
        assert "_append_jsonl(" in inspect.getsource(fn)
    assert "_append_jsonl(" in inspect.getsource(A.notify)


# == F-D3: windowed count, newest records kept ===================================================================


def test_fd3_six_thousand_failures_are_all_counted_over_the_window(tmp_path):
    st = A.AlertStore(str(tmp_path / "a"))
    for i in range(6000):
        st.record_failure("stuck-hold", "OSError: disk gone " + "x" * 300, now=T0 + i)
    now = T0 + 6000
    assert st.failure_counts(now=now) == {"stuck-hold": 6000}
    assert sum(os.path.getsize(p) for p in (st.failures_path, st.failures_path + ".1") if os.path.exists(p)) < 2 * A.FAILURES_FILE_MAX + 4096


def test_fd3_only_the_last_24_hours_count_and_the_banner_clears(tmp_path):
    st = A.AlertStore(str(tmp_path / "a"))
    st.record_failure("old", "e", now=T0)
    st.record_failure("new", "e", now=T0 + 20 * 3600)
    assert st.failure_counts(now=T0 + 23 * 3600) == {"old": 1, "new": 1}
    assert st.failure_counts(now=T0 + 25 * 3600) == {"new": 1}                   # the 25 h old one aged out
    assert st.failure_counts(now=T0 + 50 * 3600) == {}                           # clears when the window has none


def test_fd3_at_the_cap_the_newest_records_are_kept_and_counting_never_freezes(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "FAILURES_FILE_MAX", 20_000)
    st = A.AlertStore(str(tmp_path / "a"))
    for i in range(2000):
        st.record_failure("p", f"e{i}", now=T0 + i)
    before = st.failure_counts(now=T0 + 2000)["p"]
    assert 100 < before < 2000                                                   # old ones were rotated away
    for i in range(50):
        st.record_failure("p", "late", now=T0 + 2000 + i)
    after = st.failure_counts(now=T0 + 2050)["p"]
    assert after >= before - 50 and after > 100                                  # still counting, newest present
    last = [json.loads(ln) for ln in open(st.failures_path)][-1]
    assert last["error"] == "late"
    assert os.path.getsize(st.failures_path) <= 20_000 + 400


def test_fd3_api_total_is_the_windowed_count(world):
    c, st = world
    st.record_failure("old", "e", now=time.time() - 30 * 3600)
    assert c.get("/api/alerts").json()["producer_failures"]["total"] == 0
    st.record_failure("stuck-hold", "e")
    pf = c.get("/api/alerts").json()["producer_failures"]
    assert pf["total"] == 1 and pf["window_hours"] == 24

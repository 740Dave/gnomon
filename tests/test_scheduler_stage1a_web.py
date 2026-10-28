"""Scheduler stage 1a: the web routes, the flag, the confirm flow with the device-owner presence check (stubbed through
human_confirm.set_runner: never a real dialog), the tick loop's flag gate (-RUN-LOG, -FLAG,
). Hermetic: tmp DATA root, a fake registry, a tmp machine config."""

from __future__ import annotations

import json
import os
import sys
import time

import pytest

from scheduler_support import PRESENCE_OK, T0, confirm, make, propose_inject, run_ticks

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_WEB = os.path.join(ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)

from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon import flags as flags, human_confirm as hc  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402
from gnomon.scheduler.vocab import FLAG  # noqa: E402

J = {"Content-Type": "application/json"}
BASE = "http://127.0.0.1:8770"
ROUTES = ["/api/scheduler", "/api/scheduler/runlog", "/api/scheduler/runs/remind-me/20261010T120000Z-000001",
          "/api/scheduler/remind-me", "/api/scheduler/remind-me/confirm", "/api/scheduler/remind-me/decline",
          "/api/scheduler/remind-me/cancel", "/api/scheduler/remind-me/pause", "/api/scheduler/remind-me/enable",
          "/scheduler-ui/scheduler_tab.js"]
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS")


@pytest.fixture
def world(tmp_path, monkeypatch):
    sch, clock, reg, tgt = make(tmp_path)
    store = ConfigStore(str(tmp_path / "config.json"))
    # the library writer refuses to turn a SECURITY flag off without a presence grant; this test writes a tmp config
    # directly to model "the proof flag is off" (it never touches the real config)
    monkeypatch.setattr(store, "security_flags_turned_off", lambda cfg: [])

    def flags(sched=True, proof=True):
        store.save(GnomonConfig(flags={FLAG: sched, hc.FLAG: proof}))
    flags()
    monkeypatch.setattr(srv, "_config_store", store)
    monkeypatch.setattr(srv, "_sched_override", sch)
    asked = []

    def runner(reason, timeout_s):
        asked.append(reason)
        return hc.ProofResult(world.verdict == "ok", "authenticated" if world.verdict == "ok" else world.verdict,
                              hc.METHOD, time.time())
    hc.set_runner(runner)
    c = TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": srv._SCHED_CSRF})

    class W:
        pass
    world = W()
    world.verdict = "ok"
    world.c, world.sch, world.clock, world.reg, world.tgt, world.flags, world.asked = c, sch, clock, reg, tgt, flags, asked
    yield world
    hc.set_runner(None)


def confirm_post(w, job="remind-me", **kw):
    """POST a confirm the way the tab does: echoing the digest of the Pending row it rendered (F-1)."""
    seen = w.c.get(f"/api/scheduler/{job}").json()["proposal"]["digest"]
    return w.c.post(f"/api/scheduler/{job}/confirm", headers=J, content=json.dumps({"seen": seen}).encode())


def test_flag_registered_default_off():
    assert flags.KNOWN_FLAGS[FLAG].default is False


# --- ac-s1-flag-off-inert (web) / ac-s1-flag-off-until-confirm-proof ---------------------------------------------------
def test_flag_off_every_route_is_a_plain_404_for_every_method_and_the_shell_is_byte_identical(world):
    w = world
    w.flags(sched=False)
    for path in ROUTES:
        for m in METHODS:
            r = w.c.request(m, path, headers=J, content=b"{}" if m in ("POST", "PUT", "PATCH") else None)
            assert r.status_code == 404, (m, path, r.status_code)
    shell = w.c.get("/").content
    assert shell == open(os.path.join(srv.STATIC_DIR, "index.html"), "rb").read()
    assert b"scheduler_tab.js" not in shell
    assert "/api/scheduler" not in json.dumps(w.c.get("/openapi.json").json())
    assert w.asked == []                                             # no presence prompt ever
    w.flags(sched=True)                                              # control: ON answers
    assert w.c.get("/api/scheduler").status_code == 200


def test_the_scheduler_is_off_whenever_the_confirm_proof_is_off(world):
    """requirement-flag-off-until-confirm-proof: with confirm_proof OFF the scheduler's routes
    answer 404 even with scheduler ON, so no confirm can ever be taken without the device-owner check."""
    w = world
    w.flags(sched=True, proof=False)
    for path in ROUTES:
        assert w.c.get(path).status_code == 404, path
    assert w.c.post("/api/scheduler/remind-me/confirm", headers=J, content=b"{}").status_code == 404
    assert srv._sched_enabled() is False
    w.flags(sched=True, proof=True)
    assert srv._sched_enabled() is True


def test_controller_scheduler_enabled_needs_both_flags(tmp_path, monkeypatch):
    from gnomon import flags as F
    from gnomon.app.controller import Controller
    ctl = Controller(intelligences_root=str(tmp_path / "i"), sessions_root=str(tmp_path / "s"))
    state = {}
    monkeypatch.setattr(F, "flag", lambda name, **k: state.get(name, F.KNOWN_FLAGS[name].default))
    assert ctl.scheduler_enabled() is False                         # default OFF
    state[FLAG] = True
    assert ctl.scheduler_enabled() is True                          # proof defaults ON
    state[hc.FLAG] = False
    assert ctl.scheduler_enabled() is False
    # the service the controller built carries the same gate, and no scheduler object exists until used
    assert getattr(ctl, "_scheduler", None) is None
    assert ctl.hub.service.sched_enabled() is False


def test_flag_on_injects_the_script_and_serves_it(world):
    w = world
    assert srv.SCHED_UI_SCRIPT_TAG in w.c.get("/").text
    r = w.c.get("/scheduler-ui/scheduler_tab.js")
    assert r.status_code == 200 and "GmSchedulerTab" in r.text
    assert w.c.get("/scheduler-ui/other.js").status_code == 404


# --- list, detail, the confirm flow ---------------------------------------------------------------------------------
def test_list_empty_state_pending_and_detail(world):
    w = world
    body = w.c.get("/api/scheduler").json()
    assert body["jobs"] == [] and body["pending"] == [] and body["banner"] == [] and body["csrf"] == srv._SCHED_CSRF
    propose_inject(w.sch, trigger={"every_minutes": 30})
    body = w.c.get("/api/scheduler").json()
    assert [p["id"] for p in body["pending"]] == ["remind-me"] and body["jobs"] == []
    d = w.c.get("/api/scheduler/remind-me").json()
    assert d["proposal"]["text"] == "check the inbox" and d["proposal"]["target"] == "agent-a"
    assert w.c.get("/api/scheduler/nope-nope").status_code == 404


def test_confirm_asks_for_the_presence_check_commits_and_never_fires(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30})
    r = confirm_post(w)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    assert len(w.asked) == 1 and "remind-me" in w.asked[0]
    assert os.path.exists(w.sch.paths.job("remind-me")) and os.path.exists(w.sch.paths.pin("remind-me"))
    assert w.reg.routed == [] and w.sch.run_log()["runs"] == []     # confirming never fires
    pin = json.load(open(w.sch.paths.pin("remind-me")))
    assert pin["presence"]["result"] == "authenticated"
    audit = [json.loads(l) for l in open(w.sch.paths.audit)]
    assert audit[-1]["verb"] == "confirm" and audit[-1]["detail"]["presence"]["result"] == "authenticated"
    assert audit[-1]["actor"] == "operator-page" and audit[-1]["actor_verified"] is False


@pytest.mark.parametrize("verdict", ["refused", "timeout", "unavailable", "busy", "error"])
def test_a_failed_presence_check_confirms_nothing(world, verdict):
    w = world
    w.verdict = verdict
    propose_inject(w.sch, trigger={"every_minutes": 30})
    r = confirm_post(w)
    assert r.status_code == 409 and r.json()["presence_refused"] is True and "Nothing was changed" in r.json()["error"]
    assert not os.path.exists(w.sch.paths.job("remind-me")) and os.path.exists(w.sch.paths.proposal("remind-me"))
    assert json.loads(open(w.sch.paths.audit).read().splitlines()[-1])["outcome"] == "refused"
    w.verdict = "ok"                                                # control: a pass commits
    assert confirm_post(w).status_code == 200


def test_a_proposal_changed_while_the_prompt_was_open_is_not_confirmed(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30}, text="first")
    real = hc._runner

    def runner(reason, timeout_s):
        propose_inject(w.sch, trigger={"every_minutes": 30}, text="swapped under the prompt")   # an agent re-proposes
        return hc.ProofResult(True, "authenticated", hc.METHOD, time.time())
    hc.set_runner(runner)
    r = confirm_post(w)
    assert r.status_code == 409 and "changed" in r.json()["error"]
    assert not os.path.exists(w.sch.paths.job("remind-me"))
    hc.set_runner(real)


def test_a_malformed_job_id_is_a_clean_refusal_never_a_server_error(world):
    w = world
    for jid in ("Bad%20Id", "..%2f..%2fetc", "a", "x" * 80, "UPPER"):
        for op in ("confirm", "decline", "cancel", "pause", "enable"):
            r = w.c.post(f"/api/scheduler/{jid}/{op}", headers=J, content=b"{}")
            assert r.status_code in (404, 409, 422), (jid, op, r.status_code)
        assert w.c.get(f"/api/scheduler/{jid}").status_code in (404, 422), jid
    assert w.asked == []


def test_decline_pause_enable_cancel(world):
    w = world
    propose_inject(w.sch, job_id="declined", trigger={"every_minutes": 30})
    seen = w.c.get("/api/scheduler/declined").json()["proposal"]["digest"]
    assert w.c.post("/api/scheduler/declined/decline", headers=J, content=json.dumps({"seen": seen}).encode()).json()["status"] == "declined"
    assert not os.path.exists(w.sch.paths.proposal("declined"))
    propose_inject(w.sch, trigger={"every_minutes": 30}); confirm(w.sch)
    assert w.c.post("/api/scheduler/remind-me/pause", headers=J, content=b"{}").json()["enabled"] is False
    assert w.c.get("/api/scheduler").json()["jobs"][0]["next_run"].startswith("not scheduled")
    assert w.c.post("/api/scheduler/remind-me/enable", headers=J, content=b"{}").json()["enabled"] is True
    pin = w.c.get("/api/scheduler/remind-me").json()["job"]["pin"]["sha256"]
    assert w.c.post("/api/scheduler/remind-me/cancel", headers=J, content=json.dumps({"seen": pin}).encode()).json()["status"] == "cancelled"
    assert w.c.get("/api/scheduler").json()["jobs"] == []
    assert w.c.post("/api/scheduler/remind-me/cancel", headers=J, content=b"{}").status_code == 409


# --- no builder; guards ------------------------------------------------------------------------------------------
def test_no_route_creates_or_edits_a_job_definition(world):
    w = world
    for m in ("POST", "PUT", "PATCH", "DELETE"):
        r = w.c.request(m, "/api/scheduler", headers=J, content=json.dumps(
            {"job_id": "x1", "title": "t", "trigger": {"every_minutes": 30}, "action": {"type": "inject", "text": "x"}}))
        assert r.status_code == 405, m
    for m in ("PUT", "PATCH", "DELETE"):
        assert w.c.request(m, "/api/scheduler/remind-me", headers=J, content=b"{}").status_code == 405
        assert w.c.request(m, "/api/scheduler/remind-me/confirm", headers=J, content=b"{}").status_code == 405
    paths = {getattr(r, "path", "") for r in srv.app.routes if str(getattr(r, "path", "")).startswith("/api/scheduler")}
    ops = {p.rsplit("/", 1)[1] for p in paths if p.count("/") == 4}
    assert ops == {"confirm", "decline", "cancel", "pause", "enable"}
    assert w.sch.list_jobs()["jobs"] == [] and w.sch.pending() == []


def test_guards_host_origin_csrf_content_type(world):
    w = world
    propose_inject(w.sch, trigger={"every_minutes": 30})
    url = "/api/scheduler/remind-me/confirm"
    assert TestClient(srv.app, base_url=BASE, headers={"Origin": BASE}).post(url, headers=J, content=b"{}").status_code == 403   # no CSRF
    assert TestClient(srv.app, base_url=BASE, headers={"Origin": BASE, "X-Gnomon-CSRF": "wrong"}).post(url, headers=J, content=b"{}").status_code == 403
    assert TestClient(srv.app, base_url=BASE, headers={"Origin": "http://evil.example", "X-Gnomon-CSRF": srv._SCHED_CSRF}).post(url, headers=J, content=b"{}").status_code == 403
    assert TestClient(srv.app, base_url="http://evil.example:8770", headers={"Origin": "http://evil.example:8770", "X-Gnomon-CSRF": srv._SCHED_CSRF}).get("/api/scheduler").status_code == 403
    assert w.c.post(url, content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
    assert w.c.get(url).status_code == 405
    assert w.asked == [] and not os.path.exists(w.sch.paths.job("remind-me"))        # no prompt, no commit for any of them
    assert confirm_post(w).status_code == 200                                        # control


# --- the Run Log ----------------------------------------------------------------------------------------------------
def test_run_log_lists_runs_newest_first_filters_and_opens_a_receipt(world):
    w = world
    w.reg.on_route = lambda iid, f: w.tgt.deliver(f)
    propose_inject(w.sch, job_id="job-a", trigger={"every_minutes": 30}); confirm(w.sch, "job-a")
    propose_inject(w.sch, job_id="job-b", trigger={"once_at": "2026-10-10T12:05:00Z"}); confirm(w.sch, "job-b")
    run_ticks(w.sch, w.clock, 3700)
    body = w.c.get("/api/scheduler/runlog").json()
    assert [r["ts"] for r in body["runs"]] == sorted((r["ts"] for r in body["runs"]), reverse=True)
    assert {r["job_id"] for r in body["runs"]} == {"job-a", "job-b"}
    only_b = w.c.get("/api/scheduler/runlog?job=job-b").json()["runs"]
    assert {r["job_id"] for r in only_b} == {"job-b"}
    ok = w.c.get("/api/scheduler/runlog?result=OK").json()["runs"]
    assert ok and all(r["result"] == "OK" for r in ok)
    assert w.c.get("/api/scheduler/runlog?result=FAIL").json()["runs"] == []
    run = only_b[0]
    rec = w.c.get(f"/api/scheduler/runs/job-b/{run['run_id']}").json()["receipt"]
    assert rec["run_id"] == run["run_id"] and rec["frame_trigger"].startswith("scheduler:job-b:")
    assert w.c.get("/api/scheduler/runs/job-b/20200101T000000Z-abcdef").status_code == 404
    assert w.c.get("/api/scheduler/runs/job-b/..%2f..%2fetc").status_code in (404, 422)


def test_flagged_runs_appear_in_the_banner(world):
    w = world
    w.reg.sessions.clear()                                           # the target is not live
    propose_inject(w.sch); confirm(w.sch)
    w.clock.t = T0 + 305
    w.sch.tick()
    b = w.c.get("/api/scheduler").json()
    assert b["banner"] and b["banner"][0]["reason"] == "target-not-live" and b["banner"][0]["flagged"]


# --- the tick loop: ticks only while the flag is ON, never under pytest by default (g31) -----------------------------
def test_the_loop_is_not_started_under_pytest_by_default(monkeypatch):
    monkeypatch.setattr(srv, "_scheduler_thread", None)
    srv._start_scheduler()
    assert srv._scheduler_thread is None


def test_the_loop_ticks_only_while_the_flag_is_on(world, monkeypatch):
    from gnomon.scheduler.limits import LIMITS
    w = world
    ticks = []
    monkeypatch.setattr(w.sch, "tick", lambda *a, **k: ticks.append(1) or 0)
    monkeypatch.setitem(LIMITS, "start_grace_s", 0)
    monkeypatch.setitem(LIMITS, "tick_s", 0.01)
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    w.flags(sched=False)
    srv._start_scheduler()
    time.sleep(0.25)
    assert ticks == []                                              # flag OFF: no tick runs, no registry call
    w.flags(sched=True)
    time.sleep(0.25)
    assert len(ticks) >= 3                                          # control: ON ticks
    srv._stop_scheduler()
    n = len(ticks)
    time.sleep(0.15)
    assert len(ticks) <= n + 1
    w.flags(sched=False)
    assert w.reg.routed == []

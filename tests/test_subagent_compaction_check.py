"""flag ``subagent_compaction_check`` (default OFF).

Claude Code runs the session's PreCompact / SessionStart(compact) hooks for a SUBAGENT's compaction too; the harness used to treat
that as the session's own and typed the post-compact boot prompt. With the flag ON the lifecycle hook asks the session's MAIN
transcript whether a new main-thread ``compact_boundary`` appeared (ruling ``fix-ruling-2026-10-27``).

The fixtures use the real shapes (measured 2026-10-27 from live transcripts): a main row is ``{"isSidechain": false, "type":
"system", "subtype": "compact_boundary", "timestamp": ...}`` in ``<project>/<session>.jsonl``; a subagent's is the same with
``"isSidechain": true, "agentId": ...`` in ``<project>/<session>/subagents/agent-<id>.jsonl``.

The end-to-end tests run the REAL hook (``lifecycle_hook.main``) against a real unix socket and hand each frame it sends to the
REAL proxy actuator, so what is asserted (boot prompt pending or not, log records) is what the harness would do.

Deliberately imports ``compact_origin`` only inside the tests that need it, so the behaviour tests fail by assertion (not by
ImportError) on the unfixed tree.

NOT covered (live-only): a real Claude Code compaction / subagent compaction firing these hooks; the hook's 10 s timeout.
"""
from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
from datetime import datetime, timezone

import pytest

from hooks_support import Rig

from gnomon import flags as F
from gnomon.config import GnomonConfig
from gnomon.harness import lifecycle_hook
from gnomon.harness.constants import merge_stall_inject

FLAG = "subagent_compaction_check"
IGNORED = "subagent compaction, ignored"
BOOT = "Please complete the bootstrap process (test text)."
STALL = merge_stall_inject(None, {"agent_idle_seconds": 100, "operator_idle_seconds": 1, "post_compact_idle_seconds": 2,
                                  "content": "PC-RESUME", "escalate_after_seconds": None})
#: the frame as the unfixed hook sends it, byte for byte
OLD_FRAME = b'{"cmd": "lifecycle", "event": "post-compact", "trigger": "auto"}\n'


class _Store:
    def __init__(self, cfg):
        self._cfg = cfg

    def load(self):
        return self._cfg


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(F, "_config_store", lambda: _Store(GnomonConfig(flags={FLAG: True})))


@pytest.fixture
def flag_off(monkeypatch):
    monkeypatch.setattr(F, "_config_store", lambda: _Store(GnomonConfig()))


@pytest.fixture(autouse=True)
def _short_wait(monkeypatch):
    """The 5 s retry is the production value; tests wait 0.3 s. (Only when the module exists: the unfixed tree has none.)"""
    try:
        from gnomon.harness import compact_origin as co
    except ImportError:
        return
    monkeypatch.setattr(co, "WAIT_SECONDS", 0.3)
    monkeypatch.setattr(co, "POLL_SECONDS", 0.05)


# ---------------------------------------------------------------- fixtures: real transcript shapes

def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _boundary(*, sidechain, agent_id=None, ts=None, session_id="sess-1"):
    row = {"parentUuid": None, "logicalParentUuid": "00000000-0000-4000-8000-000000000003", "isSidechain": sidechain,
           "type": "system", "subtype": "compact_boundary", "content": "Conversation compacted", "level": "info",
           "compactMetadata": {"trigger": "auto", "preTokens": 971885}, "uuid": "00000000-0000-4000-8000-000000000004",
           "timestamp": ts or _now_iso(), "sessionId": session_id}
    if agent_id:
        row["agentId"] = agent_id
    return json.dumps(row) + "\n"


def _assistant_row(text="hi"):
    return json.dumps({"type": "assistant", "isSidechain": False, "message": {"role": "assistant", "content": text}}) + "\n"


class World:
    """<projects>/<project>/<sid>.jsonl (main), .../<sid>/subagents/agent-*.jsonl, and the session dir's transcript link."""

    def __init__(self, tmp_path, rig):
        self.project = tmp_path / "projects" / "-proj"
        self.sid = "11111111-2222-3333-4444-555555555555"
        self.main = self.project / f"{self.sid}.jsonl"
        self.sub = self.project / self.sid / "subagents" / "agent-aafb8d152402e1358.jsonl"
        self.sub.parent.mkdir(parents=True)
        # a main transcript that already holds one OLD real compaction (so "count" is not 0) and ordinary rows
        self.main.write_text(_assistant_row() + _boundary(sidechain=False, ts="2026-10-10T01:15:13.000Z") + _assistant_row())
        self.sub.write_text(_assistant_row("sub work") .replace('"isSidechain": false', '"isSidechain": true'))
        self.session_dir = rig.session_dir
        link = os.path.join(self.session_dir, ".claude", "transcript.jsonl")
        os.symlink(str(self.main), link)

    def append_main(self, text):
        with open(self.main, "a", encoding="utf-8") as fh:
            fh.write(text)

    def append_sub(self, text):
        with open(self.sub, "a", encoding="utf-8") as fh:
            fh.write(text)


class FrameServer:
    """A tiny unix socket standing in for the proxy's self socket: replies ok, keeps the raw bytes it was sent."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="sc")
        self.path = os.path.join(self.dir, "s")
        self.raw = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(8)
        self._srv.settimeout(0.2)
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
            except (socket.timeout, OSError):
                continue
            with conn:
                buf = b""
                while b"\n" not in buf:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    buf += chunk
                self.raw.append(buf)
                conn.sendall(b'{"ok": true}\n')

    def close(self):
        self._stop = True
        self._t.join(timeout=2)
        self._srv.close()
        for name in (self.path,):
            try:
                os.unlink(name)
            except OSError:
                pass
        try:
            os.rmdir(self.dir)
        except OSError:
            pass


@pytest.fixture
def rig(tmp_path):
    harness = {"stall": STALL, "post_compact_boot": {"enabled": True, "text": BOOT, "role": "beta-test", "text_source": "test",
                                                     "settle_seconds": 0.0, "max_hold_seconds": 1800.0}}
    r = Rig(tmp_path, harness=harness, read_context=lambda: 10.0)
    r.proxy._monitor.poll()
    srv = FrameServer()
    r.srv = srv
    r.note_calls = []
    orig = r.proxy._monitor.note_compact
    r.proxy._monitor.note_compact = lambda *a, **k: (r.note_calls.append(1), orig(*a, **k))[1]
    yield r
    srv.close()
    r.close()


def _hook(rig, event, payload=None):
    """Run the real hook once, then deliver the frame it sent to the real actuator. Returns (stdout, frame bytes or None)."""
    out = io.StringIO()
    before = len(rig.srv.raw)
    rc = lifecycle_hook.main(["--event", event, "--socket", rig.srv.path, "--session-dir", rig.session_dir],
                             stdin=io.StringIO(json.dumps(payload if payload is not None else {"trigger": "auto"})), stdout=out)
    assert rc == 0
    sent = rig.srv.raw[before:]
    assert sent, "the hook sent no frame"
    # flag ON: a post-compact-started frame precedes the verdict frame (before-flag-on-hook-timeout); deliver them all in order and
    # return the LAST frame (the one that carries the verdict) as the frame the hook sent
    for frame in sent:
        rig.proxy.actuator.dispatch(json.loads(frame), origin="self")
    raw = sent[-1]
    return out.getvalue(), raw


def _lifecycle_points(rig):
    return [(r.get("point"), (r.get("payload") or {}).get("phase")) for r in rig.records("lifecycle")]


# ---------------------------------------------------------------- the flag

def test_flag_is_registered_default_off_and_names_the_defect():
    spec = F.KNOWN_FLAGS[FLAG]
    assert spec.default is False
    assert spec.description.startswith("GM-DEFECT-SUBAGENT-COMPACTION-TREATED-AS-THE-SESSIONS")
    assert "default off" in spec.description.lower()


# ---------------------------------------------------------------- flag ON: the three real shapes

def test_on_real_compaction_arms_the_boot_prompt(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    w.append_main(_boundary(sidechain=False))                      # the session's own new boundary
    out, raw = _hook(rig, "post-compact")
    assert rig.proxy.post_compact_boot.pending
    assert raw == OLD_FRAME or json.loads(raw) == {"cmd": "lifecycle", "event": "post-compact", "trigger": "auto"}
    assert out.startswith("[Gnomon] Compaction complete.")
    assert not [r for r in rig.records("lifecycle") if r.get("point") == "compact-ignored"]
    assert ("compact", "post") in _lifecycle_points(rig) and rig.note_calls == [1]


def test_on_subagent_compaction_applies_no_effect_and_logs_one_ignored_record(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    w.append_sub(_boundary(sidechain=True, agent_id="aafb8d152402e1358"))   # ONLY the subagent file gains a boundary
    out, _ = _hook(rig, "post-compact", {"trigger": "auto", "agent_id": "aafb8d152402e1358", "agent_type": "build-agent"})
    assert not rig.proxy.post_compact_boot.pending, "a subagent's compaction armed the boot prompt"
    assert rig.note_calls == [], "a subagent's compaction re-armed the idle nudge (note_compact)"
    assert ("compact", "post") not in _lifecycle_points(rig), "a subagent's compaction fired the post-compact lifecycle point"
    assert out == "", "the post-compact context line / boot text was printed for a subagent's compaction"
    ign = [r for r in rig.records("lifecycle") if r.get("point") == "compact-ignored"]
    assert len(ign) == 1
    assert ign[0]["reason"] == IGNORED and ign[0]["decision"] == "ignored"
    assert ign[0]["payload"]["agent_id"] == "aafb8d152402e1358" and ign[0]["payload"]["agent_type"] == "build-agent"
    rig.proxy.step(0.0)
    assert rig.child_bytes() == b"", "text was typed into the PTY after a subagent's compaction"
    # the ignored record is the LAST lifecycle record, so the scheduler's compaction_state reads it as clear (not "compacting")
    from gnomon.scheduler.inject import compaction_state
    assert compaction_state(os.path.join(rig.session_dir, ".claude", "injection_log.jsonl"))[0] == "clear"


def test_on_subagent_compaction_without_payload_agent_fields_still_ignored(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    w.append_sub(_boundary(sidechain=True, agent_id="a1"))
    _hook(rig, "post-compact")                                       # payload carries no agent_id: the transcript decides
    assert not rig.proxy.post_compact_boot.pending
    ign = [r for r in rig.records("lifecycle") if r.get("point") == "compact-ignored"]
    assert len(ign) == 1 and ign[0]["payload"]["agent_id"] is None


def test_on_sidechain_row_in_the_main_file_does_not_count(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    w.append_main(_boundary(sidechain=True, agent_id="zz"))          # isSidechain true: not the session's
    _hook(rig, "post-compact")
    assert not rig.proxy.post_compact_boot.pending


def test_on_unreadable_main_transcript_keeps_todays_behaviour_and_records_why(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    os.unlink(w.main)                                                # the link now dangles: the main transcript is unreadable
    out, _ = _hook(rig, "post-compact")
    assert rig.proxy.post_compact_boot.pending, "unreadable transcript must keep today's behaviour"
    assert out.startswith("[Gnomon] Compaction complete.")
    why = rig.records("compact-origin")
    assert len(why) == 1 and why[0]["decision"] == "unknown-treated-as-session" and why[0]["check"]


def test_on_unreadable_main_transcript_already_at_pre_keeps_todays_behaviour(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    os.unlink(w.main)
    _hook(rig, "pre-compact")
    _hook(rig, "post-compact")
    assert rig.proxy.post_compact_boot.pending
    assert len(rig.records("compact-origin")) == 1


def test_on_no_pre_record_keeps_todays_behaviour(tmp_path, rig, flag_on):
    World(tmp_path, rig)
    _hook(rig, "post-compact")                                       # no pre-compact ran first
    assert rig.proxy.post_compact_boot.pending
    assert len(rig.records("compact-origin")) == 1


# ---------------------------------------------------------------- flag OFF: unchanged

def test_off_subagent_compaction_is_treated_as_the_sessions_exactly_as_before(tmp_path, rig, flag_off):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    w.append_sub(_boundary(sidechain=True, agent_id="a1"))
    out, raw = _hook(rig, "post-compact", {"trigger": "auto", "agent_id": "a1", "agent_type": "build-agent"})
    assert raw == OLD_FRAME, "flag OFF changed the bytes the hook sends"
    assert rig.proxy.post_compact_boot.pending                       # today's (defective) behaviour, kept
    assert out.startswith("[Gnomon] Compaction complete.")
    assert not rig.records("compact-origin")
    assert not [r for r in rig.records("lifecycle") if r.get("point") == "compact-ignored"]


def test_off_writes_no_state_file_and_pre_frame_is_unchanged(tmp_path, rig, flag_off):
    World(tmp_path, rig)
    _, raw = _hook(rig, "pre-compact")
    assert raw == b'{"cmd": "lifecycle", "event": "pre-compact", "trigger": "auto"}\n'
    assert not os.path.exists(os.path.join(rig.session_dir, ".claude", "compact-main-boundaries.json"))


def test_off_hook_stdout_without_session_dir_or_socket_is_todays_text():
    out = io.StringIO()
    assert lifecycle_hook.main(["--event", "post-compact"], stdin=io.StringIO("{}"), stdout=out) == 0
    assert out.getvalue() == lifecycle_hook.POST_COMPACT_CONTEXT + "\n"


# ---------------------------------------------------------------- compact_origin units

def test_main_boundaries_counts_only_system_compact_boundary_rows_not_sidechain(tmp_path):
    from gnomon.harness import compact_origin as co
    p = tmp_path / "m.jsonl"
    p.write_text(_assistant_row() + _boundary(sidechain=False) + _boundary(sidechain=True) + "not json compact_boundary\n"
                 + json.dumps({"type": "user", "content": "the words compact_boundary"}) + "\n"
                 + json.dumps({"type": "system", "subtype": "other", "isSidechain": False}) + "\n")
    got = co.main_boundaries(str(p))
    assert got is not None and len(got) == 1
    assert co.main_boundaries(str(tmp_path / "missing.jsonl")) is None


def test_verdict_requires_the_new_boundary_to_be_at_or_after_the_pre_event(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    w = World(tmp_path, rig)
    assert co.note_pre(rig.session_dir)
    w.append_main(_boundary(sidechain=False, ts="2020-01-01T00:00:00.000Z"))   # a boundary stamped BEFORE the pre event
    assert co.verdict_post(rig.session_dir, wait=0.0)["verdict"] == co.SUBAGENT


def test_verdict_session_returns_without_waiting_when_the_boundary_is_already_there(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    w = World(tmp_path, rig)
    co.note_pre(rig.session_dir)
    w.append_main(_boundary(sidechain=False))
    slept = []
    v = co.verdict_post(rig.session_dir, wait=5.0, sleep=slept.append)
    assert v["verdict"] == co.SESSION and slept == []


def test_verdict_picks_up_a_boundary_that_lands_during_the_retry_window(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    w = World(tmp_path, rig)
    co.note_pre(rig.session_dir)
    calls = []

    def sleep(_s):
        calls.append(1)
        if len(calls) == 2:
            w.append_main(_boundary(sidechain=False))

    t = {"v": 0.0}
    v = co.verdict_post(rig.session_dir, wait=5.0, poll=0.25, sleep=sleep, clock=lambda: t.__setitem__("v", t["v"] + 0.01) or t["v"])
    assert v["verdict"] == co.SESSION and len(calls) == 2


def test_verdict_is_bounded_by_the_wait(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    World(tmp_path, rig)
    co.note_pre(rig.session_dir)
    t = {"v": 0.0}
    sleeps = []

    def sleep(s):
        sleeps.append(s)
        t["v"] += s

    v = co.verdict_post(rig.session_dir, wait=5.0, poll=0.25, sleep=sleep, clock=lambda: t["v"])
    assert v["verdict"] == co.SUBAGENT and 19 <= len(sleeps) <= 21 and t["v"] <= 5.25


# ---------------------------------------------------------------- fix round 1: F1 (re-pointed link) and F4 (the two untested branches)

def _repoint_link(w, rig, content=None):
    """Re-point the launcher's ``.claude/transcript.jsonl`` link at a DIFFERENT file (the link's own path never changes)."""
    other = w.project / "other-session.jsonl"
    other.write_text(content if content is not None else open(w.main, encoding="utf-8").read())
    link = os.path.join(rig.session_dir, ".claude", "transcript.jsonl")
    os.unlink(link)
    os.symlink(str(other), link)
    return other


def test_f1_link_repointed_between_pre_and_post_is_unknown_not_ignored_unit(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    w = World(tmp_path, rig)
    assert co.note_pre(rig.session_dir)
    _repoint_link(w, rig)                                  # the new target has no new boundary: the old code said SUBAGENT
    v = co.verdict_post(rig.session_dir, wait=0.0)
    assert v["verdict"] == co.UNKNOWN and "path changed" in v["reason"], v


def test_f1_link_repointed_end_to_end_keeps_todays_behaviour_and_records_why(tmp_path, rig, flag_on):
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    _repoint_link(w, rig)
    out, _ = _hook(rig, "post-compact")
    assert rig.proxy.post_compact_boot.pending, "a re-pointed transcript link made a possibly-real compaction 'ignored'"
    assert out.startswith("[Gnomon] Compaction complete.")
    assert not [r for r in rig.records("lifecycle") if r.get("point") == "compact-ignored"]
    why = rig.records("compact-origin")
    assert len(why) == 1 and why[0]["decision"] == "unknown-treated-as-session" and "path changed" in why[0]["check"]


def test_f1_same_target_through_a_different_link_spelling_is_not_a_change(tmp_path, rig, flag_on):
    """Positive control for the realpath compare: re-creating the link to the SAME file is not a change (no false 'unknown')."""
    from gnomon.harness import compact_origin as co
    w = World(tmp_path, rig)
    co.note_pre(rig.session_dir)
    link = os.path.join(rig.session_dir, ".claude", "transcript.jsonl")
    os.unlink(link)
    os.symlink(str(w.main), link)
    assert co.verdict_post(rig.session_dir, wait=0.0)["verdict"] == co.SUBAGENT


def test_f4_statusline_pointer_changed_between_pre_and_post_is_unknown(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    w = World(tmp_path, rig)
    co.note_pre(rig.session_dir)
    other = w.project / "elsewhere.jsonl"
    other.write_text(open(w.main, encoding="utf-8").read())
    with open(os.path.join(rig.session_dir, ".claude", "context_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"transcript_path": str(other)}, fh)
    v = co.verdict_post(rig.session_dir, wait=0.0)
    assert v["verdict"] == co.UNKNOWN and "path changed" in v["reason"], v


def test_f4_pre_record_incomplete_end_to_end_keeps_todays_behaviour(tmp_path, rig, flag_on):
    """The pre record exists but holds no count (the main transcript was unread at pre): post keeps today's behaviour."""
    w = World(tmp_path, rig)
    _hook(rig, "pre-compact")
    state = os.path.join(rig.session_dir, ".claude", "compact-main-boundaries.json")
    rec = json.load(open(state, encoding="utf-8"))
    rec["count"] = None
    json.dump(rec, open(state, "w", encoding="utf-8"))
    w.append_sub(_boundary(sidechain=True, agent_id="a1"))
    out, _ = _hook(rig, "post-compact")
    assert rig.proxy.post_compact_boot.pending and out.startswith("[Gnomon] Compaction complete.")
    why = rig.records("compact-origin")
    assert len(why) == 1 and "incomplete" in why[0]["check"]


def test_f4_pre_record_without_the_resolved_path_is_incomplete(tmp_path, rig, flag_on):
    from gnomon.harness import compact_origin as co
    World(tmp_path, rig)
    co.note_pre(rig.session_dir)
    state = os.path.join(rig.session_dir, ".claude", "compact-main-boundaries.json")
    rec = json.load(open(state, encoding="utf-8"))
    del rec["real"]                                        # a record written before the realpath was kept
    json.dump(rec, open(state, "w", encoding="utf-8"))
    v = co.verdict_post(rig.session_dir, wait=0.0)
    assert v["verdict"] == co.UNKNOWN and "incomplete" in v["reason"], v

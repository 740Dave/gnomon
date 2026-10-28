"""A hub ``bus`` inject goes through the operator-line hold like every other harness injection (g79 direction
rule: every error falls on "a held item waits", never "typed over the operator's text"), and a hook-denied /
withheld operator slash submit marks the line forced-busy (the child still holds the text).

Pre-existing defects (fix-progress-2026-10-03, audits delta3/4/5):
  * a hub ``bus`` inject typed onto a partial operator line (no dialog needed);
  * F5-3: with no forced-busy, a hook-denied slash submit left the tap ``empty`` while the child held the text,
    and a later inject typed over it.
All on the real ``assemble_proxy`` rig (``hooks_support.Rig``); each test below FAILS on the base commit eb414f1.
"""

from __future__ import annotations

import pytest

from hooks_support import Rig

from test_modal_hold_choke_point import _append, _bus, _row, child, close_dialog, open_dialog, pump
from test_modal_hold_choke_point import _broadcast, _self

MSG = "BUS-MSG"


def _tap(rig):
    return rig.proxy.operator_tap


def _quiet(**extra):
    return {"dsd_notice_boot_grace_seconds": 0.0, "dsd_notice_operator_quiet_seconds": 0.0, **extra}


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, harness=_quiet())
    yield r
    r.close()


@pytest.fixture
def deny_rig(tmp_path):
    r = Rig(tmp_path, harness=_quiet(hook_manifest={"inbound-injection": [
        {"kind": "deny_all", "sources": ["operator"], "commands": ["/compact"], "name": "no-compact"}]}))
    yield r
    r.close()


def _settle(rig):
    pump(rig)
    got = child(rig)
    pump(rig)
    return got + child(rig)


def _over(got, needle=MSG):
    return needle.encode() in got


# ---- (1) bus inject on an operator line --------------------------------------------------------------

def test_control_bus_on_an_empty_line_is_delivered_at_once(rig):
    pump(rig)
    _bus(rig, MSG)
    assert child(rig).count(MSG.encode()) == 1
    assert not rig.gate.held()


def test_bus_on_a_partial_line_is_held_not_typed_over_then_delivered_once_after_ctrl_u(rig):
    pump(rig)
    rig.type(b"abc")                      # his unsent text, no dialog anywhere
    child(rig)
    _bus(rig, MSG)
    got = _settle(rig)
    assert not _over(got), got
    assert len(rig.gate.held()) == 1
    rig.type(b"\x15")                     # Ctrl-U reaches the child: the line is clear
    child(rig)
    pump(rig)
    assert child(rig).count(MSG.encode()) == 1
    assert not rig.gate.held()
    pump(rig)
    assert MSG.encode() not in child(rig)  # exactly once


def test_bus_held_on_a_partial_line_is_delivered_after_his_enter(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    _bus(rig, MSG)
    assert not _over(_settle(rig))
    rig.type(b"\r")
    child(rig)
    pump(rig)
    assert child(rig).count(MSG.encode()) == 1


def test_bus_on_an_unknown_line_is_held(rig):
    pump(rig)
    rig.type(b"\x1b[A")                   # Up (history recall) punts the tap on an empty line: UNKNOWN
    child(rig)
    assert _tap(rig).line_state == "unknown"
    _bus(rig, MSG)
    assert not _over(_settle(rig))
    assert len(rig.gate.held()) == 1


def test_several_bus_messages_are_delivered_in_order_when_the_line_clears(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    for t in ("ONE", "TWO", "THREE"):
        _bus(rig, t)
    assert not any(_over(_settle(rig), t) for t in ("ONE", "TWO", "THREE"))
    assert len(rig.gate.held()) == 3
    rig.type(b"\x15")
    child(rig)
    pump(rig)
    out = child(rig)
    assert out.index(b"ONE") < out.index(b"TWO") < out.index(b"THREE")
    assert not rig.gate.held()


def test_a_bus_message_arriving_after_a_held_one_does_not_jump_the_queue(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    _bus(rig, "FIRST")
    rig.type(b"\x15")                     # line clear; the next decide releases the queue FIRST, in order
    child(rig)
    _bus(rig, "SECOND")
    pump(rig)
    out = child(rig)
    assert out.count(b"FIRST") == 1 and out.count(b"SECOND") == 1
    assert out.index(b"FIRST") < out.index(b"SECOND")


def test_the_hold_is_recorded_and_the_release_rechecks_the_line(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    _bus(rig, MSG)
    held = [r for r in rig.records() if r.get("decision") == "defer" and r.get("handler") == "modal-hold"]
    assert held and held[-1].get("line_hold")
    rig.type(b"\x15")
    child(rig)
    rig.type(b"x")                        # he starts typing again before the next tick releases it
    pump(rig)
    assert not _over(child(rig))
    assert len(rig.gate.held()) == 1


def test_a_plain_approve_does_not_force_a_line_held_bus_message_onto_his_text(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    _bus(rig, MSG)
    assert rig.gate.approve("all") == []      # the operator's plain approve re-checks the line
    got = _settle(rig)
    assert not _over(got), got
    assert len(rig.gate.held()) == 1


def test_dialog_open_and_line_partial_bus_waits_for_both(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    open_dialog(rig)
    pump(rig)
    _bus(rig, MSG)
    close_dialog(rig)
    got = _settle(rig)
    assert not _over(got), got              # dialog closed, his line is still busy
    assert len(rig.gate.held()) == 1


def test_a_later_non_bus_injection_queues_behind_a_line_held_bus_message_in_order(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    _bus(rig, "BUS1")
    _self(rig, "SELF1")                    # behind the held item: order preserved (never ahead of it)
    got = _settle(rig)
    assert b"BUS1" not in got and b"SELF1" not in got
    rig.type(b"\x15")
    child(rig)
    pump(rig)
    out = child(rig)
    assert out.index(b"BUS1") < out.index(b"SELF1")


def test_bus_hold_is_independent_of_other_flags_but_follows_the_modal_hold_kill_switch(tmp_path):
    r = Rig(tmp_path, harness=_quiet(modal_hold=False))
    try:
        pump(r)
        r.type(b"abc")
        child(r)
        _bus(r, MSG)
        # STATED: the kill switch (modal_hold OFF = "no hold at all") also removes this hold.
        assert _over(_settle(r))
    finally:
        r.close()


# ---- (2) F5-3: a withheld slash submit leaves the child holding the text -------------------------------

def test_f53_a_hook_denied_slash_submit_marks_forced_busy_and_a_bus_message_waits(deny_rig):
    rig = deny_rig
    pump(rig)
    rig.type(b"/compact\r")               # the gate DENIES: text is in the child's prompt, the CR is withheld
    got = child(rig)
    assert b"/compact" in got and b"/compact\r" not in got
    assert _tap(rig).forced_busy == "slash-submit-withheld"
    assert _tap(rig).line_state == "partial" and _tap(rig).unsent_text() is None
    _bus(rig, MSG)
    got = _settle(rig)
    assert not _over(got), got
    assert len(rig.gate.held()) == 1
    rig.type(b"\x15")                     # a keystroke that reaches the child clears it (reaches-child rule)
    child(rig)
    assert _tap(rig).forced_busy is None
    pump(rig)
    assert child(rig).count(MSG.encode()) == 1


def test_f53_the_mark_is_on_the_tap_so_every_reader_of_the_line_sees_it(deny_rig):
    rig = deny_rig
    pump(rig)
    rig.type(b"/compact\r")
    child(rig)
    # the mark is on the TAP: anything that reads the line (notices, the D50 takeover) sees it
    assert rig.proxy._monitor.line_hold_reason() is not None


def test_f53_an_allowed_slash_submit_that_is_written_leaves_no_mark(deny_rig):
    rig = deny_rig
    pump(rig)
    rig.type(b"/clear\r")                 # not denied: the Enter reaches the child
    child(rig)
    assert _tap(rig).forced_busy is None and _tap(rig).line_state == "empty"
    _bus(rig, MSG)
    assert child(rig).count(MSG.encode()) == 1


def test_f53_a_later_written_slash_submit_over_a_denied_one_clears_the_mark(deny_rig):
    rig = deny_rig
    pump(rig)
    rig.type(b"/compact\r")               # denied, text stays in the child's prompt
    child(rig)
    assert _tap(rig).forced_busy == "slash-submit-withheld"
    rig.type(b"\x15/clear\r")             # Ctrl-U (reaches the child) then an allowed slash that is written
    child(rig)
    assert _tap(rig).forced_busy is None


def test_f53_a_swallowed_command_does_not_clear_the_withheld_mark(deny_rig):
    rig = deny_rig
    pump(rig)
    rig.type(b"/compact\r")
    child(rig)
    rig.type(b"\\status\r")               # swallowed by the harness: the child never saw it
    child(rig)
    assert _tap(rig).forced_busy == "slash-submit-withheld"
    _bus(rig, MSG)
    assert not _over(_settle(rig))


def test_f53_an_earlier_more_specific_forced_busy_cause_is_kept(deny_rig):
    rig = deny_rig
    pump(rig)
    _tap(rig).force_busy("dialog-close-time-unknown")
    rig.type(b"/compact\r")
    child(rig)
    assert _tap(rig).forced_busy == "dialog-close-time-unknown"


def test_f53_a_hook_deferred_slash_submit_marks_busy_and_its_release_clears_it(tmp_path):
    r = Rig(tmp_path, harness=_quiet(hook_manifest={"inbound-injection": [
        {"kind": "defer", "sources": ["operator"], "commands": ["/compact"], "name": "hold-compact"}]}))
    try:
        pump(r)
        r.type(b"/compact\r")            # DEFERRED: text in the child's prompt, the Enter held
        got = child(r)
        assert b"/compact" in got and b"/compact\r" not in got
        assert _tap(r).forced_busy == "slash-submit-withheld"
        _bus(r, MSG)
        assert not _over(_settle(r))
        r.gate.approve("all")             # the deferred Enter is written now: the child's line is submitted
        out = child(r)
        assert b"\r" in out
        assert _tap(r).forced_busy is None
        pump(r)
        assert child(r).count(MSG.encode()) + out.count(MSG.encode()) == 1
    finally:
        r.close()


# ---- (3) round 2: the g79 direction rule covers EVERY non-operator source -------------------------------
# Architect's ruling: the default is HELD. Never line-held: the operator's own keystrokes, his explicit
# ``\\approve --into-dialog``, the folder-trust accepter and the D50 takeover compact (each bound to its CALL PATH).

from hooks_support import Rig as _Rig  # noqa: E402  (same helper; kept near its users)
from test_modal_hold_choke_point import KINDS  # noqa: E402
from gnomon.harness import hooks as _hooks  # noqa: E402
from gnomon.harness.hooks import FOLDER_TRUST_CAP, FOLDER_TRUST_CAP_KEY, InjectionEvent  # noqa: E402

# looked up (not imported) so this file also COLLECTS on the pre-fix base, where the tests must fail, not error
LINE_TAKEOVER_CAP_KEY = getattr(_hooks, "LINE_TAKEOVER_CAP_KEY", "_gm_line_takeover_cap")
LINE_TAKEOVER_CAP = getattr(_hooks, "LINE_TAKEOVER_CAP", object())

#: sources held by the GATE queue (the notice kinds and the DSD notice have their own earlier hold, below)
GATE_QUEUED = ["bus", "operator-broadcast", "self-inject", "raw-writer-inject", "boot-prompt"]


def _unthresholded(kind):
    """A threshold notice whose threshold is still crossed would be dropped as STALE on release by the
    notice path (pre-existing); send it unthresholded so the test measures the hold, not the staleness."""
    from gnomon.harness.monitor import Notice, NoticeKind
    k = {"compact-guidance": NoticeKind.COMPACT_GUIDANCE, "fyi": NoticeKind.FYI}[kind]
    return lambda rig, t: rig.proxy.inject_notice(Notice(kind=k, text=t))


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_source_is_held_on_a_partial_line_and_delivered_once_when_it_clears(rig, kind):
    send, needle = KINDS[kind]
    if kind in ("compact-guidance", "fyi"):
        send = _unthresholded(kind)
    pump(rig)
    rig.type(b"abc")                       # his unsent text, no dialog anywhere
    child(rig)
    send(rig, needle)
    got = _settle(rig)
    assert not _over(got, needle), f"{kind} typed onto his partial line: {got!r}"
    assert b"\r" not in got
    rig.type(b"\x15")                      # Ctrl-U reaches the child: the line is clear
    child(rig)
    pump(rig)
    pump(rig)
    out = child(rig)
    if kind == "stall-resume-heartbeat":
        # a keep-alive is only meaningful NOW: the notice path drops it when held (never types it late)
        assert needle.encode() not in out
        return
    assert out.count(needle.encode()) == 1, f"{kind}: not delivered exactly once after the clear: {out!r}"
    pump(rig)
    assert needle.encode() not in child(rig)


def test_the_gate_queued_sources_are_released_in_arrival_order(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    needles = []
    for kind in GATE_QUEUED:
        send, needle = KINDS[kind]
        send(rig, needle)
        needles.append(needle.encode())
    assert not any(n in _settle(rig) for n in needles)
    assert len(rig.gate.held()) == len(GATE_QUEUED)
    rig.type(b"\x15")
    child(rig)
    pump(rig)
    out = child(rig)
    idx = [out.index(n) for n in needles]
    assert idx == sorted(idx) and all(out.count(n) == 1 for n in needles), out
    assert not rig.gate.held()


@pytest.mark.parametrize("kind", GATE_QUEUED)
def test_a_held_source_shows_the_visible_stuck_hold_after_the_threshold(rig, kind):
    send, needle = KINDS[kind]
    pump(rig)
    rig.type(b"abc")
    child(rig)
    send(rig, needle)
    rig.operator_screen()
    rig.gate.modal_stuck_after = 0.0       # the 30 min threshold, compressed
    rig.gate.release_modal_holds()
    assert rig.gate.modal_stuck is not None and rig.gate.modal_stuck["why"] == "line"
    assert b"HOLD STUCK" in rig.operator_screen()
    assert len(rig.gate.held()) == 1       # visible, never released


def test_default_stuck_threshold_is_thirty_minutes(rig):
    assert rig.gate.modal_stuck_after == 1800.0


def test_a_held_operator_broadcast_is_visible_on_the_operator_screen_at_once(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    rig.operator_screen()
    _broadcast(rig, "<d-to-all> hello")
    screen = rig.operator_screen()
    assert b"HELD" in screen and b"WAITING" in screen, screen
    assert b"hello" not in child(rig)


def test_a_non_broadcast_hold_does_not_post_the_broadcast_waiting_line(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    rig.operator_screen()
    _bus(rig, MSG)
    assert b"WAITING" not in rig.operator_screen()


def test_the_operators_own_text_is_never_line_held(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    rig.proxy._injector.inject_line("/status", clear=False, source="operator", trigger="op")
    assert b"/status" in child(rig)
    assert not rig.gate.held()


def test_the_operators_force_approve_releases_a_line_held_item(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    _bus(rig, MSG)
    assert len(rig.gate.held()) == 1
    rig.gate.approve("all", force=True)    # his explicit \approve --into-dialog
    assert child(rig).count(MSG.encode()) == 1


def test_the_folder_trust_accepter_is_exempt_by_call_path_not_by_frame(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    wrote = []
    ev = InjectionEvent(source="bootstrap", trigger="folder-trust-accept:enter", content="<folder-trust>",
                        meta={FOLDER_TRUST_CAP_KEY: FOLDER_TRUST_CAP})
    out = rig.gate.decide(ev, lambda e: wrote.append(e))
    assert out.written and wrote
    forged = InjectionEvent(source="bootstrap", trigger="folder-trust-accept:enter", content="<folder-trust>",
                            meta={FOLDER_TRUST_CAP_KEY: "FOLDER_TRUST_CAP"})   # what a JSON frame could carry
    out = rig.gate.decide(forged, lambda e: wrote.append(e))
    assert not out.written and len(wrote) == 1


# --- spoofing: a hub / control frame cannot claim a treatment that bypasses the hold ---------------------

SPOOFS = [
    {"source": "operator"}, {"source": "bootstrap"}, {"source": "self-verb"}, {"source": "harness"},
    {"source": "unattributed"}, {"source": "resume"}, {"source": "monitor-guidance"},
    {"source": "bootstrap", "trigger": "folder-trust-accept:enter"},
    {"source": "bus", "trigger": "operator-submit"},
    {"source": "bus", "trigger": "compact", "meta": {FOLDER_TRUST_CAP_KEY: "x", LINE_TAKEOVER_CAP_KEY: "x"}},
    {"source": "bus", "_gm_folder_trust_cap": True, "_gm_line_takeover_cap": True, "meta": {"_gm_line_takeover_cap": 1}},
    {"source": "operator-broadcast", "trigger": "folder-trust-accept:down"},
    {"source": "agent-launch"},
]


@pytest.mark.parametrize("extra", SPOOFS, ids=[str(i) for i in range(len(SPOOFS))])
def test_a_hub_frame_cannot_claim_a_source_or_marker_that_bypasses_the_line_hold(rig, extra):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    rig.hub_down({"cmd": "inject", "text": MSG, **extra})
    got = _settle(rig)
    assert not _over(got), (extra, got)
    assert len(rig.gate.held()) == 1, extra
    rig.type(b"\x15")
    child(rig)
    pump(rig)
    assert child(rig).count(MSG.encode()) == 1


def test_a_local_control_frame_cannot_claim_operator_either(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    for origin in (None, "local"):
        rig.proxy.actuator.dispatch({"cmd": "inject", "text": MSG, "source": "operator",
                                     "meta": {FOLDER_TRUST_CAP_KEY: FOLDER_TRUST_CAP}}, origin=origin)
    assert not _over(_settle(rig))
    assert len(rig.gate.held()) == 2


def test_a_forged_takeover_marker_on_a_non_self_verb_event_is_held(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    out = rig.proxy._injector.inject_line("x-takeover", clear=False, source="bus", trigger="compact",
                                          meta={LINE_TAKEOVER_CAP_KEY: LINE_TAKEOVER_CAP})
    assert not out.written
    out = rig.proxy._injector.compact("dir", clear=True, source="bus", line_takeover=True)   # cap not stamped
    assert not out.written
    assert b"x-takeover" not in child(rig)


def test_a_self_verb_event_with_a_truthy_non_capability_marker_is_held(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    for marker in ("x", True, 1, {"a": 1}):
        ev = InjectionEvent(source="self-verb", trigger="compact", content="/compact",
                            meta={LINE_TAKEOVER_CAP_KEY: marker})
        assert not rig.gate.decide(ev, lambda e: None).written
    assert len(rig.gate.held()) == 4


def test_the_d50_takeover_compact_is_the_only_self_verb_write_allowed_onto_a_partial_line(rig):
    pump(rig)
    rig.type(b"abc")
    child(rig)
    out = rig.proxy._injector.compact("dir", clear=True, source="self-verb")          # no takeover verdict: held
    assert not out.written
    rig.gate.reject("all")
    child(rig)
    out = rig.proxy._injector.compact("dir", clear=True, source="self-verb", line_takeover=True)
    assert out.written and b"/compact" in child(rig)


# ---- flag-off behaviour (stated) -------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["bus", "operator-broadcast", "self-inject", "raw-writer-inject"])
def test_flag_off_modal_hold_removes_every_source_hold_as_before(tmp_path, kind):
    r = Rig(tmp_path, harness=_quiet(modal_hold=False))
    try:
        send, needle = KINDS[kind]
        pump(r)
        r.type(b"abc")
        child(r)
        send(r, needle)
        assert needle.encode() in _settle(r)     # STATED: kill switch OFF = no hold at all (pre-existing)
    finally:
        r.close()


# ---- stated limits (pinned) -----------------------------------------------------------------------------

def test_limit_a_gate_less_writer_still_writes_raw_bytes(tmp_path):
    """STATED: with NO gate installed there is no line knowledge to consult, the raw writer writes (legacy
    behaviour; production always installs a gate). Pinned so a change to it is a visible decision."""
    from gnomon.harness.writer import SingleWriteSite

    sink = []
    w = SingleWriteSite(99, writer=lambda fd, data: sink.append(bytes(data)) or len(data))
    w.inject(b"RAW")
    assert sink == [b"RAW"]

"""Remediation-P3 guard tests (2026-09-28) — the independent Diamond audit's findings.

Covers audit findings F2, F3 (incl. F3d), F5 and F6a on the fail-loud compact
confirmation. F1 + F4 (the deadlock and the missing real-socket coverage) live in
``test_confirm_poll_realpty.py``; N1/N2/P3 stay in ``test_fixpass_compact_confirm.py``.

Each guard here FAILS on the pre-fix tree for its own reason (verified by
reconstructing the pre-fix state on a temp copy and running each test against it):

* F2  — the confirm was SKIPPED whenever a hook MODIFY-ed the command (guard was
        ``"decision" not in out``); a MODIFY still writes to the child, so the old
        false-positive ok:True returned unconfirmed.
* F3  — ``confirm_compact_timeout`` accepted NaN/Infinity/<=0/too-large;
        ``confirm_compact`` accepted the truthy STRING "false"; the poll busy-spun
        on a dead transport (F3d).
* F5  — the fail-loud result reached nobody (not logged to provenance, not
        surfaced to the operator).
* F6a — ``direction_bytes`` reported the PRE-strip length.

⚠ LIVE-ONLY (NOT here): a real claude PreCompact round-trip. See the SUMMARY.
"""

import json
import math
import threading
import time

import pytest

from hooks_support import Rig

from gnomon.harness import Actuator, ContextMonitor, Injector, SingleWriteSite
from gnomon.harness.compact_confirm import COMPACT_CONFIRM_TIMEOUT_SECONDS, CompactConfirmer
from gnomon.harness.hooks import (
    POINT_INBOUND,
    HookPipeline,
    InjectionGate,
    ProvenanceLog,
    build_handlers,
)

PREFIX_MODIFY_MANIFEST = {"inbound-injection": [{"kind": "prefix", "prefix": "[TAG] ", "name": "tag"}]}
DENY_MANIFEST = {"inbound-injection": [{"kind": "deny_all", "name": "deny"}]}


class _FakeConfirmer:
    """A confirmer whose confirm() result is preset — isolates the actuator's
    fail-loud DECISION from the wait mechanics (those are covered by CompactConfirmer
    tests and the real-pty probes)."""

    def __init__(self, result, timeout=5.0):
        self.result = result
        self.timeout = timeout
        self.armed = 0
        self.confirmed = 0

    def arm(self):
        self.armed += 1

    def confirm(self, timeout=None):
        self.confirmed += 1
        return self.result


def _gated_actuator(manifest, confirmer):
    captured = []

    def fake_write(_fd, data):
        captured.append(bytes(data))
        return len(data)

    log = ProvenanceLog()
    gate = InjectionGate(HookPipeline(build_handlers(manifest, POINT_INBOUND)), log)
    writer = SingleWriteSite(master_fd=-1, writer=fake_write, gate=gate)
    injector = Injector(writer, sleep=lambda _s: None)
    monitor = ContextMonitor(read_context=lambda: 50.0)
    actuator = Actuator(injector, monitor, compact_confirmer=confirmer)
    return actuator, writer, log


# -- F2: a MODIFY-ed command is still confirmed ----------------------------------


def test_f2_modify_hook_still_confirms_and_fails_loud_when_not_observed():
    # A hook MODIFIES the command (the shipped Prefix hook) -> decision:"modify".
    # The command is still typed into the child, so the compaction MUST be
    # confirmed. Pre-fix the guard was ``"decision" not in out`` -> confirmation
    # SKIPPED -> false-positive ok:True.
    conf = _FakeConfirmer(False)
    actuator, writer, _log = _gated_actuator(PREFIX_MODIFY_MANIFEST, conf)
    resp = actuator.dispatch({"cmd": "compact", "direction": "keep ids"}, origin="self")
    assert resp.get("decision") == "modify"           # the MODIFY hook fired
    assert conf.confirmed == 1                         # pre-fix: 0 (confirm skipped)
    assert resp["ok"] is False                         # pre-fix: True (false positive)
    assert resp["compaction_observed"] is False
    assert "not observed" in resp["reason"]
    assert b"/compact" in writer.injected_bytes        # it WAS written to the child


def test_f2_modify_hook_confirms_ok_when_observed():
    conf = _FakeConfirmer(True)
    actuator, _writer, _log = _gated_actuator(PREFIX_MODIFY_MANIFEST, conf)
    resp = actuator.dispatch({"cmd": "compact", "direction": "keep ids"}, origin="self")
    assert resp.get("decision") == "modify"
    assert resp["ok"] is True and resp["compaction_observed"] is True
    assert conf.confirmed == 1


def test_f2_denied_compact_is_not_confirmed():
    # BEHAVIOUR-UNCHANGED guard (passes pre- AND post-fix): a DENY never actuated,
    # so there is nothing to confirm. Proves F2 did not over-reach into DENY/DEFER.
    conf = _FakeConfirmer(True)  # would claim observed if it were ever asked
    actuator, writer, _log = _gated_actuator(DENY_MANIFEST, conf)
    resp = actuator.dispatch({"cmd": "compact", "direction": "x"}, origin="self")
    assert resp["ok"] is False and resp.get("decision") == "deny"
    assert conf.confirmed == 0
    assert "compaction_observed" not in resp
    assert writer.injected_bytes == b""  # nothing typed


# -- F3 (a-c): confirm_compact* launch.json validation ---------------------------


def test_f3_nan_timeout_refused():
    from gnomon.harness.launcher import validate_confirm_compact_config

    with pytest.raises(ValueError):
        validate_confirm_compact_config({"confirm_compact_timeout": float("nan")})


def test_f3_infinity_timeout_refused_as_json_parses_it():
    from gnomon.harness.launcher import validate_confirm_compact_config

    # Python's json parses bare Infinity -> float('inf'); float() accepts it. This
    # is exactly the value an operator's launch.json would yield.
    cfg = json.loads('{"confirm_compact_timeout": Infinity}')
    assert math.isinf(cfg["confirm_compact_timeout"])
    with pytest.raises(ValueError):
        validate_confirm_compact_config(cfg)


def test_f3_zero_and_negative_timeout_refused():
    from gnomon.harness.launcher import validate_confirm_compact_config

    for bad in (0.0, -1.0):
        with pytest.raises(ValueError):
            validate_confirm_compact_config({"confirm_compact_timeout": bad})


def test_f3_timeout_at_or_above_self_mcp_response_timeout_refused():
    from gnomon.harness.launcher import validate_confirm_compact_config
    from gnomon.harness.self_mcp import RESPONSE_TIMEOUT_SECONDS

    with pytest.raises(ValueError):
        validate_confirm_compact_config({"confirm_compact_timeout": RESPONSE_TIMEOUT_SECONDS})
    with pytest.raises(ValueError):
        validate_confirm_compact_config({"confirm_compact_timeout": 999.0})


def test_f3_false_string_confirm_compact_refused():
    from gnomon.harness.launcher import validate_confirm_compact_config

    # Pre-fix inline logic: bool(listener is not None and cfg.get("confirm_compact", True));
    # with the STRING "false" that is bool("false") == True -> confirmation ON. A
    # typed check refuses it loudly.
    with pytest.raises(ValueError):
        validate_confirm_compact_config({"confirm_compact": "false"})
    with pytest.raises(ValueError):
        validate_confirm_compact_config({"confirm_compact": 1})


def test_f3_valid_values_and_default_off():
    from gnomon.harness.launcher import validate_confirm_compact_config

    assert validate_confirm_compact_config({"confirm_compact": True, "confirm_compact_timeout": 5.0}) == (True, 5.0)
    # ⭐ default OFF (Architect design call) + no timeout.
    assert validate_confirm_compact_config({}) == (False, None)
    assert validate_confirm_compact_config({"confirm_compact": False}) == (False, None)


# -- F3d: the poll must not busy-spin on a dead transport ------------------------


def test_f3d_dead_poll_breaks_instead_of_busy_spinning():
    calls = {"n": 0}

    def dead_poll(_rem):
        calls["n"] += 1
        return False  # transport dead (launcher._confirm_poll returns False on select error)

    c = CompactConfirmer(timeout=5.0)  # REAL monotonic clock
    c.set_poll(dead_poll)
    c.arm()
    t0 = time.monotonic()
    assert c.confirm() is False
    elapsed = time.monotonic() - t0
    assert calls["n"] == 1        # broke after the first dead poll (pre-fix: spun millions)
    assert elapsed < 1.0          # returned promptly (pre-fix: spun ~5s to the deadline)


def test_f3d_nonfinite_bound_clamps_to_finite_deadline():
    # Re-Diamond N3: the OLD form of this test used a poll that returned False
    # (dead transport), so it exited via the F3d dead-transport BREAK — removing the
    # non-finite CLAMP (compact_confirm.confirm) left it green, mislabelling the
    # clamp as covered. This form exercises the clamp for real: the poll ALWAYS
    # returns True ("keep waiting"), so the ONLY thing that can terminate the loop is
    # the clamp turning the non-finite bound into the finite default (whose deadline
    # a monotonically-advancing clock then reaches). Remove the clamp -> the deadline
    # is +inf -> remaining stays +inf -> the loop never ends -> this test goes red.
    clock_vals = [0.0, 0.0]  # arm/first-remaining at t=0; then jump far past any finite clamp

    def clock():
        return clock_vals.pop(0) if clock_vals else 1e9

    polls = {"n": 0}

    def keep_waiting(_rem):
        polls["n"] += 1
        return True  # never signals a dead transport — the clamp is the only exit

    c = CompactConfirmer(timeout=float("inf"), clock=clock)
    c.set_poll(keep_waiting)
    c.arm()
    result = {}
    th = threading.Thread(target=lambda: result.__setitem__("r", c.confirm()), daemon=True)
    th.start()
    th.join(2.0)
    assert not th.is_alive(), (
        "confirm() did not terminate with a non-finite bound — the clamp to the "
        "finite default is missing (deadline was +inf, loop never ended)"
    )
    assert result.get("r") is False
    assert polls["n"] >= 1  # the wait actually polled (did not short-circuit)


# -- F5: the fail-loud result must reach someone ---------------------------------


def test_f5a_confirmation_result_is_recorded_to_provenance(tmp_path):
    # No local_control wired in the Rig -> the confirmer's poll is unwired -> with
    # timeout 0 it times out immediately -> ok:False. The launcher's
    # on_compact_confirmation sink must record it to the provenance log.
    rig = Rig(tmp_path, confirm_compact=True, confirm_compact_timeout=0.0)
    try:
        rig.hub_down({"cmd": "compact", "direction": "keep ids"})
        recs = rig.records("compact-confirm")
        assert len(recs) == 1, recs                    # pre-fix: 0 (never recorded)
        assert recs[0]["compaction_observed"] is False
        assert recs[0]["confirmation_ok"] is False
        assert recs[0].get("reason")
    finally:
        rig.close()


def test_f5b_not_observed_is_surfaced_to_the_operator(tmp_path):
    rig = Rig(tmp_path, confirm_compact=True, confirm_compact_timeout=0.0)
    try:
        rig.hub_down({"cmd": "compact", "direction": "keep ids"})
        screen = rig.operator_screen()
        assert b"COMPACT" in screen and b"NO compaction" in screen, screen  # pre-fix: silent
    finally:
        rig.close()


def test_f5_observed_result_is_recorded_true(tmp_path):
    rig = Rig(tmp_path, confirm_compact=True, confirm_compact_timeout=1.0)
    try:
        conf = rig.proxy.compact_confirmer
        assert conf is not None
        # Make the (unwired) poll deliver the PreCompact signal, as _on_lifecycle would.
        conf.set_poll(lambda _rem: conf.note_pre_compact())
        rig.hub_down({"cmd": "compact", "direction": "keep ids"})
        recs = rig.records("compact-confirm")
        assert len(recs) == 1 and recs[0]["compaction_observed"] is True
        assert recs[0]["confirmation_ok"] is True
        # An observed result is NOT surfaced as a failure to the operator.
        assert b"NO compaction" not in rig.operator_screen()
    finally:
        rig.close()


# -- F6a: direction_bytes reports the POST-strip length --------------------------


def test_f6a_direction_bytes_is_post_strip(tmp_path):
    p = tmp_path / "d.directive"
    p.write_text("   keep ids   \n\n")  # surrounding whitespace
    actuator, _writer, _log = _gated_actuator({}, None)  # no confirmer
    resp = actuator.dispatch({"cmd": "compact", "directive_path": str(p)}, origin="self")
    assert resp["ok"] is True
    assert resp["direction_bytes"] == len("keep ids".encode("utf-8"))  # pre-fix: 17 (pre-strip)

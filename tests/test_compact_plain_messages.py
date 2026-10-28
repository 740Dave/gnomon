"""B3 (pre-review batch, spec section 5): the three compact texts in plain words, flag ``compact_plain_messages``.

OFF (default): every text is byte-equal to what 03b41cc gave (the OLD strings are pinned below as literals, copied from the
base tree, so a changed default fails here). ON: the new texts. The flag is read from the machine config store only.
"""
from __future__ import annotations

import io
import re

import pytest

from hooks_support import Rig

from gnomon import flags as F
from gnomon.config import GnomonConfig
from gnomon.harness import compact_messages as CM
from gnomon.harness import lifecycle_hook

FLAG = "compact_plain_messages"

#: the texts as 03b41cc produces them (literals, not re-derived from the code under test)
OLD_SOFT_70 = ("Are you sure? You're only at 70% context. If you do, call request_compact again within 30 minutes "
               "with a non-empty 'reason' (logged). This is a nudge, not a wall; the handover and directive freshness "
               "checks still apply.")
OLD_POST_COMPACT = ("[Gnomon] Compaction complete. Resume your in-flight task from your handover notes. If you stay "
                    "idle with no operator input, the harness will send one resume nudge.\n")
ACCEPTED = "Compact accepted. Your turn is being interrupted now: this interrupt IS the compact, not a refusal by the operator."


class _Store:
    def __init__(self, cfg):
        self._cfg = cfg

    def load(self):
        return self._cfg


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(F, "_config_store", lambda: _Store(GnomonConfig(flags={FLAG: True})))


def _self_compact(rig, **frame):
    command = {"cmd": "compact", "direction": "", "clear": True}
    command.update(frame)
    return rig.proxy.actuator.dispatch(command, origin="self")


@pytest.fixture
def rig_factory(tmp_path):
    rigs = []

    def make(reading):
        rig = Rig(tmp_path, read_context=lambda: reading)
        rigs.append(rig)
        return rig

    yield make
    for r in rigs:
        r.close()


def _hook_stdout(session_dir):
    out = io.StringIO()
    rc = lifecycle_hook.main(["--event", "post-compact", "--session-dir", str(session_dir)],
                             stdin=io.StringIO("{}"), stdout=out)
    assert rc == 0
    return out.getvalue()


def _session(tmp_path):
    d = tmp_path / "sess"
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    return d


def test_flag_is_registered_default_off_with_a_plain_description():
    spec = F.KNOWN_FLAGS[FLAG]
    assert spec.default is False
    assert "compact" in spec.description.lower() and "default off" in spec.description.lower()
    assert "_flag_" not in spec.description        # words, not identifiers


# --------------------------------------------------------------------------- flag OFF: byte-equal to 03b41cc

def test_off_soft_refusal_is_todays_text_exactly(rig_factory):
    resp = _self_compact(rig_factory(70.0), direction="x")
    assert resp["reasons"] == ["confirm-twice"]
    assert OLD_SOFT_70 in resp["error"]


def test_off_accepted_result_has_no_message_key(rig_factory):
    resp = _self_compact(rig_factory(92.0), direction="x")
    assert resp["ok"] is True and "message" not in resp
    assert set(resp) >= {"ok", "action"} and resp["action"] == "compact"


def test_off_post_compact_context_is_todays_text_exactly(tmp_path):
    assert _hook_stdout(_session(tmp_path)) == OLD_POST_COMPACT


# --------------------------------------------------------------------------- flag ON

def test_on_accepted_result_says_the_interrupt_is_the_compact(rig_factory, flag_on):
    resp = _self_compact(rig_factory(92.0), direction="x")
    assert resp["ok"] is True and resp["message"] == ACCEPTED
    assert list(resp)[:3] == ["ok", "action", "message"]            # reads first


def test_on_soft_refusal_says_what_the_operator_sees_and_what_the_second_call_does(rig_factory, flag_on):
    resp = _self_compact(rig_factory(70.0), direction="x")
    err = resp["error"]
    assert resp["reasons"] == ["confirm-twice"]
    assert "This first call did NOT compact" in err
    assert "nothing was typed into the terminal" in err and "operator's screen shows no change" in err
    assert "You are at 70% context, below 85%" in err
    assert "call request_compact again within 30 minutes with a non-empty 'reason' (logged)" in err
    assert "the interrupt IS the compact, not a refusal by the operator" in err
    assert "Are you sure?" not in err


def test_on_soft_refusal_below_65_keeps_the_wisdom_reminder(rig_factory, flag_on):
    err = _self_compact(rig_factory(50.0), direction="x")["error"]
    assert "Your best work is done between 75 and 90% context" in err


def test_on_post_compact_context_gains_one_line_with_the_utc_time(tmp_path, flag_on):
    got = _hook_stdout(_session(tmp_path))
    assert got.startswith(OLD_POST_COMPACT)                           # everything before is unchanged
    extra = got[len(OLD_POST_COMPACT):]
    assert re.fullmatch(r"A compact completed at \d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\. Any 'call request_compact' step in your "
                        r"summary is already done; do not call it again\.\n", extra), extra


def test_completed_line_uses_the_given_clock():
    from datetime import datetime, timezone
    assert CM.completed_line(datetime(2026, 10, 5, 22, 1, 2, tzinfo=timezone.utc)).startswith(
        "A compact completed at 2026-10-05T22:01:02Z.")


def test_a_launch_json_flag_entry_cannot_turn_it_on(tmp_path, rig_factory):
    """The flag is machine-config only: a session can write its own launch.json, so it must not reach this text."""
    s = _session(tmp_path)
    (s / ".claude" / "launch.json").write_text('{"flags": {"%s": true}}' % FLAG)
    assert _hook_stdout(s) == OLD_POST_COMPACT


def test_an_unreadable_flag_means_off(monkeypatch, tmp_path):
    def boom():
        raise RuntimeError("config store down")
    monkeypatch.setattr(F, "_config_store", boom)
    assert CM.enabled() is False
    assert _hook_stdout(_session(tmp_path)) == OLD_POST_COMPACT


# --------------------------------------------------------------------------- Diamond F2: said only when the compact was written

def _compact_with_manifest(tmp_path, manifest):
    rig = Rig(tmp_path, read_context=lambda: 92.0, harness={"hook_manifest": manifest})
    try:
        return _self_compact(rig, direction="x")
    finally:
        rig.close()


def test_on_a_hook_deny_does_not_say_the_compact_was_accepted(tmp_path, flag_on):
    from hooks_support import DENY_ALL_MANIFEST
    from gnomon.harness.hooks import validate_manifest
    resp = _compact_with_manifest(tmp_path, validate_manifest(DENY_ALL_MANIFEST))
    assert resp.get("ok") is False and resp.get("decision") == "deny", resp      # precondition: it really was denied
    assert "message" not in resp, resp


def test_on_a_hook_defer_does_not_say_the_compact_was_accepted(tmp_path, flag_on):
    from gnomon.harness.hooks import SOURCE_SELF_VERB
    resp = _compact_with_manifest(tmp_path, {"inbound-injection": [{"kind": "defer", "sources": [SOURCE_SELF_VERB]}]})
    assert resp.get("decision") == "defer", resp                                  # precondition: it really was deferred
    assert "message" not in resp, resp

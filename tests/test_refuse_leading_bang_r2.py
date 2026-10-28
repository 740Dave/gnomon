"""Round 2 of the leading-'!' rule (operator ruling Diamond audit-refuse-leading-bang
F1-F5): UNCONDITIONAL (no flag), no operator-broadcast exemption, harness-composed prompts screened
(monitor notices, the post-compact boot text), no-origin dispatch screened, and an allow-list scan of
every call site that writes text to the TUI."""

from __future__ import annotations

import ast
import os

import pytest

from hooks_support import Rig

from gnomon import flags as zflags
from gnomon.harness import compact_gate as cg
from gnomon.harness import leading_bang as lb
from gnomon.harness.hooks import Decision, InjectionEvent, InjectionGate, SOURCE_OPERATOR
from gnomon.harness.injector import Injector
from gnomon.harness.monitor import Notice, NoticeKind
from gnomon.harness.post_compact_boot import validate_text
from gnomon.harness.writer import SingleWriteSite

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANGS = ["!id", "  !id", "​!id", "﻿!id", "！id", "\n!id", "‎ ⁠!id"]


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path, read_context=lambda: 50.0)
    yield r
    r.close()


def _recs(rig, kind="inject-refused"):
    return [r for r in rig.file_records() if r.get("kind") == kind]


def _all_flags_false(monkeypatch):
    monkeypatch.setattr(zflags, "_machine_flags", lambda config: {k: False for k in zflags.KNOWN_FLAGS})


# -- F1: unconditional, nothing can switch it off -------------------------------


def test_the_flag_is_gone_from_the_registry_and_the_rule_modules():
    assert "refuse_leading_bang" not in zflags.KNOWN_FLAGS
    for rel in ("leading_bang.py", "compact_gate.py", "hooks.py", "writer.py", "post_compact_boot.py"):
        src = open(os.path.join(REPO, "gnomon", "harness", rel), encoding="utf-8").read()
        assert "refuse_leading_bang" not in src, rel
        assert "_bang_refusal_enabled" not in src, rel
    lb_src = open(os.path.join(REPO, "gnomon", "harness", "leading_bang.py"), encoding="utf-8").read()
    tree = ast.parse(lb_src)
    imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
    assert {getattr(n, "module", None) or n.names[0].name for n in imports} <= {"__future__", "typing", "unicodedata"}


@pytest.mark.parametrize("text", BANGS)
def test_every_flag_false_and_every_config_key_cannot_disable_it(monkeypatch, rig, text):
    _all_flags_false(monkeypatch)
    # also a hand-planted flag the old design would have honoured
    monkeypatch.setattr(zflags, "_machine_flags",
                        lambda config: dict({k: False for k in zflags.KNOWN_FLAGS},
                                            refuse_leading_bang=False))
    assert cg.screen_self_inject(text) == [cg.REASON_BANG]
    assert cg.screen_hub_inject(text) == [cg.REASON_BANG]
    assert lb.has_leading_bang(text)
    assert rig.proxy.actuator.dispatch({"cmd": "inject", "text": text}, origin="self")["ok"] is False
    assert rig.proxy.actuator.dispatch({"cmd": "inject", "text": text}, origin="hub")["ok"] is False
    # the choke point under the same all-flags-false config (a monitor notice / boot prompt path)
    for src in ("monitor-guidance", "harness", "bus", "unattributed"):
        assert rig.proxy._injector.inject_line(text, source=src).decision is Decision.DENY, src
    assert rig.child_bytes() == b""
    with pytest.raises(ValueError):
        validate_text(text, "text")


def test_hooks_off_and_pause_cannot_disable_it(rig):
    gate = rig.proxy._writer.gate
    gate.set_hooks_enabled(False)
    out = rig.proxy._injector.inject_line("!id", source="monitor-guidance")
    assert out.decision is Decision.DENY and "leading-bang" in out.reason
    gate.set_hooks_enabled(True)
    gate.set_paused(True)
    out = rig.proxy._injector.inject_line("!id", source="bus")
    assert out.decision is Decision.DENY, out  # denied, not merely deferred
    assert rig.child_bytes() == b""


# -- F3: no operator-broadcast exemption ----------------------------------------


@pytest.mark.parametrize("text", BANGS)
def test_operator_broadcast_source_is_refused_everywhere(rig, text):
    resp = rig.proxy.actuator.dispatch(
        {"cmd": "inject", "text": text, "source": "operator-broadcast", "trigger": "all"}, origin="hub")
    assert resp["ok"] is False and resp["reasons"] == ["leading-bang"], resp
    assert rig.child_bytes() == b""
    out = rig.proxy._injector.inject_line(text, source="operator-broadcast")
    assert out.decision is Decision.DENY


def test_operator_broadcast_with_its_real_prefix_still_delivers(rig):
    resp = rig.proxy.actuator.dispatch(
        {"cmd": "inject", "text": "<deployment-to-all> !fine inside", "source": "operator-broadcast"}, origin="hub")
    assert resp["ok"] is True, resp
    assert b"!fine inside" in rig.child_bytes()


def test_the_operators_own_keystrokes_are_not_injection_and_pass(rig):
    rig.type(b"!ls\r")
    assert b"!ls" in rig.child_bytes()


def test_gate_exempts_only_the_operator_provenance_source():
    gate = InjectionGate()
    sunk = []
    ev = lambda src: InjectionEvent(source=src, content="!ls", trigger="t")
    assert gate.decide(ev(SOURCE_OPERATOR), sunk.append).decision is Decision.ALLOW
    for src in ("self-verb", "bus", "operator-broadcast", "monitor-guidance", "resume", "harness",
                "agent-launch", "dsd-answer", "bootstrap", "unattributed"):
        assert gate.decide(ev(src), sunk.append).decision is Decision.DENY, src
    assert len(sunk) == 1


# -- F2: monitor notices and the post-compact boot prompt -----------------------


@pytest.mark.parametrize("kind", [NoticeKind.HEARTBEAT, NoticeKind.COMPACT_GUIDANCE]
                         if hasattr(NoticeKind, "COMPACT_GUIDANCE") else list(NoticeKind)[:2])
def test_monitor_notice_with_leading_bang_is_not_typed_and_is_recorded(rig, kind):
    rig.proxy._type_notice(Notice(kind=kind, text="!id from a notice"))
    assert rig.child_bytes() == b""
    recs = [r for r in rig.file_records() if r.get("kind") == "injection" and r.get("decision") == "deny"]
    assert recs and recs[-1]["reason"].startswith("leading-bang")


def test_monitor_notice_normal_text_still_typed(rig):
    rig.proxy._type_notice(Notice(kind=NoticeKind.HEARTBEAT, text="heartbeat ok"))
    assert b"heartbeat ok" in rig.child_bytes()


@pytest.mark.parametrize("text", BANGS)
def test_validate_text_rejects_a_leading_bang(text):
    with pytest.raises(ValueError):
        validate_text(text, "text")


def test_validate_text_message_names_the_bang_for_plain_cases():
    for text in ("!id", "  !id", "！id"):
        with pytest.raises(ValueError, match="'!'"):
            validate_text(text, "text")


@pytest.mark.parametrize("text", ["Resume work.", "ok !", "a!b", "#x"])
def test_validate_text_still_accepts_normal_text(text):
    assert validate_text(text, "text") == text


def test_post_compact_boot_prompt_with_bang_is_denied_at_the_gate(rig):
    out = rig.proxy._injector.inject_line("!id", source="harness", trigger="post-compact-boot")
    assert out.decision is Decision.DENY and rig.child_bytes() == b""


# -- F4: no origin means non-operator --------------------------------------------


@pytest.mark.parametrize("text", BANGS)
def test_dispatch_without_an_origin_is_screened(rig, text):
    resp = rig.proxy.actuator.dispatch({"cmd": "inject", "text": text})
    assert resp["ok"] is False and resp["decision"] == "refused" and resp["reasons"] == ["leading-bang"], resp
    assert rig.child_bytes() == b""
    recs = _recs(rig)
    assert len(recs) == 1 and recs[0]["source"] == "unattributed"


def test_dispatch_without_an_origin_still_types_normal_text(rig):
    assert rig.proxy.actuator.dispatch({"cmd": "inject", "text": "hello"})["ok"] is True
    assert b"hello" in rig.child_bytes()


def test_no_gate_writer_still_refuses():
    import socket

    a, b = socket.socketpair()
    try:
        w = SingleWriteSite(a.fileno())
        inj = Injector(w, beat_seconds=0, sleep=lambda _s: None)
        out = inj.inject_line("!id", source="bus")
        assert out.decision is Decision.DENY
        b.setblocking(False)
        with pytest.raises(BlockingIOError):
            b.recv(10)
        assert inj.inject_line("fine", source="bus").decision is Decision.ALLOW
    finally:
        a.close()
        b.close()


# -- F5: every call site that writes text to the TUI is accounted for -------------

#: callee names that put bytes toward the child's PTY. A NEW site (file, function, callee) not listed
#: here fails the test: add it only after routing it through Writer.admit (the leading-'!' choke point:
#: hooks.evaluate when a gate is installed, writer.admit when not) or classifying it below.
_SINKS = {"inject_line", "admit", "write_keys", "submit_writes", "payload_phases", "run_ledgered"}

CHOKE = "goes through Writer.admit -> leading-'!' refusal"
OPERATOR = "the operator's own keystrokes / own unsent text (not an injection)"
BUILDER = "byte builder only (no write); its callers are listed separately"
ALLOW = {
    # round 5: inject_line's single admit now lives in _admit_text (the old body, moved verbatim, so that the SAME call can be made twice:
    # once with the presence line and, only if the writer/gate refused it on a SCREEN reason, once without it). Both attempts are
    # Writer.admit calls on the caller's own text (+ the fixed presence line on the first); tests/test_owl_round3.py::test_r5_* proves that
    # every text reaching it is screened identically on both attempts and that the retry skips no check.
    ("injector.py", "_admit_text", "self._writer.admit"): CHOKE + " (inject_line's first send AND its retry without the presence line)",
    ("injector.py", "compact", "self._writer.admit"): CHOKE + " (fixed '/compact' text)",
    ("injector.py", "inject", "self.inject_line"): CHOKE,
    ("injector.py", "submit_writes", "payload_phases"): BUILDER,
    ("injector.py", "_payload_phases", "payload_phases"): BUILDER,
    ("injector.py", "_emit_line", "submit_writes"): "post-admission emit of an admitted event",
    ("injector.py", "_emit_compact", "submit_writes"): "post-admission emit of an admitted '/compact'",
    ("writer.py", "inject", "self.admit"): CHOKE + " (raw writer.inject outside an admitted emit)",
    ("startup.py", "_send", "self._writer.admit"): CHOKE + " (fixed folder-trust key content)",
    ("proxy.py", "_type_notice", "self._injector.inject_line"): CHOKE + " (monitor notices, F2)",
    ("actuator.py", "inject", "self._injector.inject_line"): CHOKE + " (+ the actuator's own early refusal)",
    ("proxy.py", "_on_stdin_readable", "self._writer.write_keys"): OPERATOR,
    ("optap.py", "_forward", "self._writer.write_keys"): OPERATOR,
    ("optap.py", "_forward_submitted", "submit_writes"): OPERATOR,
    ("optap.py", "restore_unsent", "payload_phases"): OPERATOR + " (D50 restore of the operator's text)",
    ("optap.py", "restore_unsent", "self._gate.run_ledgered"): OPERATOR + " (ledger bracket only)",
}
# optap's many self._forward(...) call sites are operator keystroke forwarding by construction
_OPERATOR_INTERNAL = {"_forward"}


def _sites():
    found = {}
    root = os.path.join(REPO, "gnomon", "harness")
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".py"):
            continue
        tree = ast.parse(open(os.path.join(root, fn), encoding="utf-8").read())

        class V(ast.NodeVisitor):
            def __init__(self):
                self.stack = []

            def visit_FunctionDef(self, n):
                self.stack.append(n.name)
                self.generic_visit(n)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_Call(self, n):
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
                full = ast.unparse(f)
                is_sink = name in _SINKS or (name == "inject" and full.endswith("writer.inject"))
                if is_sink and name not in _OPERATOR_INTERNAL:
                    found[(fn, ".".join(self.stack[-1:]) or "<module>", full)] = True
                self.generic_visit(n)

        V().visit(tree)
    return set(found)


def test_every_tui_write_site_is_on_the_allow_list():
    sites = _sites()
    # the writer.inject emissions inside already-admitted emits are covered by their admit site
    sites = {s for s in sites if not s[2].endswith("writer.inject")}
    unknown = sorted(sites - set(ALLOW))
    stale = sorted(set(ALLOW) - sites)
    assert not unknown, ("a NEW call site writes text toward the TUI without being on the leading-'!' "
                         f"allow-list (route it through Writer.admit, then list it): {unknown}")
    assert not stale, f"allow-list entries with no matching call site (remove them): {stale}"


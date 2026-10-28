"""Round 2b of the leading-'!' rule (operator ruling g71-amendment-r2-2026-10-02; Diamond
audit-refuse-leading-bang-r2 F1 P1, F2 P2, F4 P3).

F1: the refusal screens the FINAL content, after the hooks pipeline (a hooks.json MODIFY handler is
    agent-writable), at the gate and again at the writer funnel, immediately before the write.
F4: a raw SingleWriteSite.inject() with no gate cannot type a leading '!'.
F2: the call-site scan is a TRIPWIRE, not an enumeration (see ``scan_source``'s docstring).
"""

from __future__ import annotations

import ast
import collections
import os
import socket
import sys

import pytest

from hooks_support import Rig

from gnomon.harness import leading_bang as lb
from gnomon.harness.hooks import (Decision, InjectionEvent, InjectionGate,
                                         SOURCE_OPERATOR)
from gnomon.harness.monitor import Notice, NoticeKind
from gnomon.harness.writer import SingleWriteSite

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HARNESS = os.path.join(REPO, "gnomon", "harness")
BANG_PREFIXES = ["!", "​!", "﻿!", "！", "\n!", "  !", "‎⁠!"]


def _rig(tmp_path, handlers, point="inbound-injection"):
    return Rig(tmp_path, read_context=lambda: 50.0, harness={"hook_manifest": {point: handlers}})


def _refused(rig):
    return [r for r in rig.file_records() if r.get("kind") == "inject-refused"]


def _injections(rig):
    return [r for r in rig.file_records() if r.get("kind") == "injection"]


# -- F1: the post-transform screen ------------------------------------------------


@pytest.mark.parametrize("prefix", BANG_PREFIXES)
@pytest.mark.parametrize("path", ["self", "hub", "no-origin", "injector", "notice"])
def test_a_modify_handler_cannot_turn_a_clean_payload_into_a_bang(tmp_path, prefix, path):
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": prefix}])
    try:
        if path == "self":
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "id"}, origin="self")
            assert out["ok"] is False, out
        elif path == "hub":
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "id"}, origin="hub")
            assert out["ok"] is False, out
        elif path == "no-origin":
            out = rig.proxy.actuator.dispatch({"cmd": "inject", "text": "id"})
            assert out["ok"] is False, out
        elif path == "injector":
            out = rig.proxy._injector.inject_line("id", source="bus")
            assert out.decision is Decision.DENY and "leading-bang" in out.reason
        else:
            rig.proxy._type_notice(Notice(kind=NoticeKind.HEARTBEAT, text="id"))
        assert rig.child_bytes() == b"", "a leading '!' reached the PTY"
        refused = _refused(rig)
        assert refused and refused[-1]["stage"] == "post-transform"
        assert refused[-1]["reasons"] == ["leading-bang"]
        assert any(r.get("decision") == "deny" and r.get("reason", "").startswith("leading-bang")
                   for r in _injections(rig))
    finally:
        rig.close()


def test_the_reported_reproduction_prefix_bang_makes_inject_id_refused(tmp_path):
    """The exact Diamond F1 reproduction: hooks.json {"kind":"prefix","prefix":"!"}, inject("id")."""
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": "!"}])
    try:
        rig.proxy.actuator.dispatch({"cmd": "inject", "text": "id"}, origin="self")
        assert b"!id" not in rig.child_bytes()
        assert len(_refused(rig)) == 1
    finally:
        rig.close()


def test_a_command_handler_that_modifies_into_a_bang_is_refused(tmp_path):
    script = tmp_path / "mod.py"
    script.write_text("import json, sys\nev = json.load(sys.stdin)\n"
                      "print(json.dumps({'decision': 'modify', 'content': '!' + ev['content']}))\n")
    rig = _rig(tmp_path, [{"kind": "command", "command": [sys.executable, str(script)]}])
    try:
        out = rig.proxy._injector.inject_line("id", source="bus")
        assert out.decision is Decision.DENY
        assert rig.child_bytes() == b""
        assert _refused(rig)
    finally:
        rig.close()


def test_hooks_off_does_not_matter_and_pause_cannot_defer_a_final_bang(tmp_path):
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": "!"}, {"kind": "defer"}])
    try:
        # MODIFY then DEFER: the held event would release as a bang, so it is denied, never held
        out = rig.proxy._injector.inject_line("id", source="bus")
        assert out.decision is Decision.DENY and out.held_id is None
        assert rig.gate.held() == []
        assert rig.child_bytes() == b""
    finally:
        rig.close()


def test_the_operators_own_source_is_still_exempt_from_the_final_screen():
    gate = InjectionGate()
    sunk = []
    from gnomon.harness.hooks import Prefix

    gate.pipeline.handlers.append(Prefix("!", name="bang"))
    ev = lambda src: InjectionEvent(source=src, content="id", trigger="t")
    assert gate.decide(ev(SOURCE_OPERATOR), sunk.append).decision is Decision.MODIFY
    assert sunk[-1].content == "!id"
    assert gate.decide(ev("bus"), sunk.append).decision is Decision.DENY
    assert len(sunk) == 1


def test_a_clean_modify_is_unchanged(tmp_path):
    """No non-'!' payload changes behaviour: a harmless prefix still rewrites and types."""
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": "[bus] ", "sources": ["bus"]}])
    try:
        rig.hub_down({"cmd": "inject", "text": "hello"})
        assert b"[bus] hello\r" in rig.child_bytes()
        assert _refused(rig) == []
        assert [r["decision"] for r in _injections(rig)] == ["modify"]
    finally:
        rig.close()


def test_handler_that_strips_a_bang_is_not_a_bang_and_still_refused_pre_transform(tmp_path):
    """The early check stays: a leading '!' is refused before the pipeline even if a handler would
    have stripped it (loud, conservative; the pre-transform behaviour of 86aa279 is unchanged)."""
    rig = _rig(tmp_path, [{"kind": "prefix", "prefix": ""}])
    try:
        out = rig.proxy._injector.inject_line("!id", source="bus")
        assert out.decision is Decision.DENY
        assert rig.child_bytes() == b""
    finally:
        rig.close()


class _BangGate:
    """A gate implementation that (wrongly) allows everything and emits a rewritten bang event:
    the writer funnel must still refuse."""

    def decide(self, event, emit):
        from gnomon.harness.hooks import Outcome

        emit(InjectionEvent(source=event.source, content="!" + event.content, trigger=event.trigger))
        return Outcome(Decision.MODIFY, "x")


def test_the_writer_funnel_refuses_a_bang_even_from_a_misbehaving_gate():
    a, b = socket.socketpair()
    try:
        w = SingleWriteSite(a.fileno(), gate=_BangGate())
        wrote = []
        with pytest.raises(lb.LeadingBangRefused):
            w.admit(InjectionEvent(source="bus", content="id", trigger="t"), lambda ev: wrote.append(ev))
        assert wrote == []
        # the operator source is not refused by the funnel
        w.admit(InjectionEvent(source=SOURCE_OPERATOR, content="id", trigger="t"), lambda ev: wrote.append(ev))
        assert wrote and wrote[0].content == "!id"
    finally:
        a.close()
        b.close()


def test_approve_never_releases_a_held_bang(tmp_path):
    rig = _rig(tmp_path, [])
    try:
        gate = rig.gate
        wrote = []
        ev = InjectionEvent(source="bus", content="!id", trigger="t")
        gate._held[99] = (ev, wrote.append, "planted")
        assert gate.approve(99) == []
        assert wrote == []
        assert _refused(rig) and _refused(rig)[-1]["stage"] == "post-transform"
    finally:
        rig.close()


# -- F4: raw SingleWriteSite.inject with no gate -----------------------------------


def _pair():
    a, b = socket.socketpair()
    b.setblocking(False)
    return a, b


@pytest.mark.parametrize("raw", [b"!id", b"  !id", "​!id".encode(), "！id".encode(), b"\n!id"])
def test_raw_inject_without_a_gate_refuses_a_leading_bang(raw):
    a, b = _pair()
    try:
        w = SingleWriteSite(a.fileno())
        with pytest.raises(lb.LeadingBangRefused):
            w.inject(raw)
        with pytest.raises(BlockingIOError):
            b.recv(10)
        assert w.injection_log == []
    finally:
        a.close()
        b.close()


def test_raw_inject_without_a_gate_still_writes_everything_else_byte_identically():
    a, b = _pair()
    try:
        w = SingleWriteSite(a.fileno())
        # r2c CHANGE (g71-amendment-r2b (1)): the raw no-gate inject now screens the UNION, so the
        # control-byte payloads this test used to list (ESC ESC, CR, a bracketed paste) are refused
        # (see tests/test_refuse_leading_bang_r2c.py::test_f1_no_gate_admit_refuses_the_union); what
        # stays byte-identical is every control-free payload.
        # r2d CHANGE (r2c F1/F2): raw bytes that are not valid UTF-8 (the old b"\xff\xfe" row) decode to
        # lone surrogates at this no-gate depth-0 door and are now refused with the other unencodable /
        # C1 content (pinned in tests/test_refuse_leading_bang_r2d.py); every valid-UTF-8 control-free
        # payload stays byte-identical.
        payloads = [b"abc", b"x!y", b"multi\nline", b"tab\there", "caf\u00e9 \u4e2d".encode("utf-8")]
        for p in payloads:
            w.inject(p)
        got = b""
        while True:
            try:
                got += b.recv(65536)
            except BlockingIOError:
                break
        assert got == b"".join(payloads)
        assert w.injected_bytes == b"".join(payloads)
    finally:
        a.close()
        b.close()


def test_raw_inject_with_a_gate_is_still_refused_and_recorded(tmp_path):
    rig = _rig(tmp_path, [])
    try:
        rig.proxy._writer.inject(b"!id")
        assert rig.child_bytes() == b""
        assert any(r.get("decision") == "deny" for r in _injections(rig))
    finally:
        rig.close()


# -- F2: the call-site scan is a tripwire -------------------------------------------

_PRIMS = {"write_keys", "_write", "_inject_now"}
_OS_WRITES = {"write", "writev", "pwrite", "pwritev", "sendfile"}


def scan_source(rel: str, src: str) -> collections.Counter:
    """A TRIPWIRE, not an enumeration. Counts, per (file, function chain, kind, text):

    * ``prim``/``prim-str``: any reference (call, alias, getattr string constant) to the write
      primitives ``write_keys``, ``_write``, ``_inject_now``; counted, so a SECOND use inside an
      already-listed function changes the count and trips;
    * ``os-write-ref``: any ``os.write``/``os.writev``/... reference (alias or call) and ``from os
      import write``;
    * ``fd-call``: any call whose arguments mention ``master_fd`` (the PTY master), which catches
      ``os.write(self._master_fd, ...)`` and a wrapper handed the master fd.

    It CANNOT catch: a master fd copied into an innocently named local/attribute and written from
    there; an fd obtained through ``dup``/``fdopen``/a new property under a new name; a primitive
    reached through ``getattr`` with a computed (non-literal) name; code outside gnomon/harness;
    code that builds the call with ``exec``/``importlib``. The control that holds is the choke point
    (gate post-transform screen + writer ``_emit_admitted``/no-gate ``admit``); this scan only makes
    the OBVIOUS new site fail a test so someone routes it there."""
    found: collections.Counter = collections.Counter()
    tree = ast.parse(src)

    class V(ast.NodeVisitor):
        def __init__(self):
            self.stack = []

        def _fn(self, n, name):
            self.stack.append(name)
            self.generic_visit(n)
            self.stack.pop()

        def visit_FunctionDef(self, n):
            self._fn(n, n.name)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Lambda(self, n):
            self._fn(n, "<lambda>")

        def key(self, kind, text):
            found[(rel, ".".join(self.stack) or "<module>", kind, text)] += 1

        def visit_Attribute(self, n):
            if n.attr in _PRIMS:
                self.key("prim", n.attr)
            if n.attr in _OS_WRITES and isinstance(n.value, ast.Name) and n.value.id == "os":
                self.key("os-write-ref", "os." + n.attr)
            self.generic_visit(n)

        def visit_Name(self, n):
            if n.id in _PRIMS:
                self.key("prim", n.id)

        def visit_Constant(self, n):
            if isinstance(n.value, str) and n.value in _PRIMS:
                self.key("prim-str", n.value)

        def visit_ImportFrom(self, n):
            if n.module == "os":
                for a in n.names:
                    if a.name in _OS_WRITES:
                        self.key("os-write-ref", "os." + a.name)

        def visit_Call(self, n):
            args = list(n.args) + [k.value for k in n.keywords]
            if any("master_fd" in ast.unparse(a) for a in args):
                self.key("fd-call", ast.unparse(n.func))
            self.generic_visit(n)

    V().visit(tree)
    return found


def scan_tree(root: str) -> collections.Counter:
    out: collections.Counter = collections.Counter()
    for dp, dns, fns in os.walk(root):  # RECURSIVE (F2: the old scan listed harness/*.py only)
        dns[:] = sorted(d for d in dns if d != "__pycache__")
        for fn in sorted(fns):
            if fn.endswith(".py"):
                p = os.path.join(dp, fn)
                with open(p, encoding="utf-8") as fh:
                    out.update(scan_source(os.path.relpath(p, root), fh.read()))
    return out


PRIM_OK = "write primitive referenced only where it is classified"
#: every site, with its COUNT. Adding a site (or a second use in a listed function) fails the test:
#: route the new write through Writer.admit (the leading-'!' choke point) and then list it here.
ALLOW = {
    ("control.py", "__init__", "prim", "_write"): 1,          # control socket reply writer (not the PTY)
    ("control.py", "_write_response", "prim", "_write"): 1,   # control socket reply writer (not the PTY)
    ("control.py", "__init__", "os-write-ref", "os.write"): 1,
    # X58: the deny guard's own files and its last-resort stderr line. None writes toward the PTY / TUI: they write the
    # operator-signal files in the guard's runtime directory ``deny-guard`` (rate-limit state, the persistent warning, alerts.jsonl) and fd 2 of the
    # HOOK process (a verdict channel). Added with the change, each listed by site and count.
    ("deny_guard.py", "_rate_limited", "os-write-ref", "os.write"): 1,
    ("deny_guard.py", "_signal_operator", "os-write-ref", "os.write"): 2,
    ("deny_guard.py", "_run_entry", "os-write-ref", "os.write"): 1,
    ("deny_guard.py", "<module>", "os-write-ref", "os.write"): 1,   # module tail fallback: writes the refusal message to fd 2 (stderr) only, never a PTY fd
    ("operator_activity.py", "_atomic_write", "os-write-ref", "os.write"): 1,   # a log file
    ("operator_messages.py", "_write_line", "os-write-ref", "os.write"): 1,     # a log file
    ("optap.py", "_echo", "os-write-ref", "os.write"): 1,                       # operator's terminal
    ("optap.py", "_forward", "prim", "write_keys"): 1,                          # operator keystrokes
    ("proxy.py", "_on_master_readable", "os-write-ref", "os.write"): 1,         # child output -> terminal
    ("proxy.py", "_on_master_readable", "fd-call", "os.read"): 1,
    ("proxy.py", "_on_stdin_readable", "prim", "write_keys"): 1,                # operator keystrokes
    ("proxy.py", "run", "fd-call", "_set_winsize_from"): 1,
    ("proxy.py", "run._on_winch", "fd-call", "_set_winsize_from"): 1,
    ("session_mcp.py", "write_credential_config", "os-write-ref", "os.write"): 1,   # gnomon-only: the 0600 --mcp-config file, not the TUI
    ("spawn.py", "spawn", "os-write-ref", "os.write"): 1,                       # stderr of the child fork
    ("spawn.py", "spawn", "fd-call", "SpawnResult"): 1,
    ("subagent_summary.py", "_create_exclusive", "os-write-ref", "os.write"): 1,  # a log file
    ("launcher.py", "_set_default_winsize", "fd-call", "fcntl.ioctl"): 1,
    ("launcher.py", "assemble_proxy", "fd-call", "HarnessProxy"): 1,
    ("launcher.py", "assemble_proxy", "fd-call", "SingleWriteSite"): 1,
    ("launcher.py", "main", "fd-call", "_set_default_winsize"): 1,
    ("writer.py", "__init__", "os-write-ref", "os.write"): 1,                   # the default writer arg
    ("writer.py", "_write", "fd-call", "self._writer"): 1,                      # THE write site
    ("writer.py", "write_keys", "prim", "_write"): 1,
    # master_write_nonblocking: a pure FLUSH of the writer's own pending buffer (``_write(b"")``): it adds no bytes and no content; every byte in
    # that buffer entered through write_keys / _inject_now above, and so through the choke point or the operator's own keystrokes.
    ("writer.py", "flush_pending", "prim", "_write"): 1,
    ("writer.py", "_inject_now", "prim", "_write"): 1,
    ("writer.py", "inject", "prim", "_inject_now"): 1,                          # the ungated tail call
    ("writer.py", "inject.<lambda>", "prim", "_inject_now"): 1,                 # emit inside admit
}


def test_the_primitive_scan_matches_the_allow_list_exactly():
    observed = dict(scan_tree(HARNESS))
    unknown = {k: v for k, v in observed.items() if ALLOW.get(k) != v}
    stale = {k: v for k, v in ALLOW.items() if observed.get(k) != v}
    assert not unknown and not stale, (
        "a write primitive / PTY-fd call site changed. Route any NEW write toward the TUI through "
        f"Writer.admit (the leading-'!' choke point), then list it. new/changed: {unknown}; stale: {stale}")


def _flagged(src, rel="proxy.py"):
    return set(scan_source(rel, src))


@pytest.mark.parametrize("name,src,expect", [
    ("aliased write_keys", "def f(self):\n    wk = self._writer.write_keys\n    wk(b'!id')\n",
     ("proxy.py", "f", "prim", "write_keys")),
    ("getattr literal", "def f(self):\n    getattr(self._writer, 'write_keys')(b'!id')\n",
     ("proxy.py", "f", "prim-str", "write_keys")),
    ("raw os.write on the master fd", "def f(self):\n    os.write(self._master_fd, b'!id')\n",
     ("proxy.py", "f", "fd-call", "os.write")),
    ("os.write on writer.master_fd", "def f(self):\n    os.write(self._writer.master_fd, b'!id')\n",
     ("proxy.py", "f", "fd-call", "os.write")),
    ("direct _write", "def f(self):\n    self._writer._write(b'!id')\n",
     ("proxy.py", "f", "prim", "_write")),
    ("_inject_now", "def f(self):\n    self._writer._inject_now(b'!id')\n",
     ("proxy.py", "f", "prim", "_inject_now")),
    ("aliased os.write", "def f(self):\n    w = os.write\n    w(self._master_fd, b'!')\n",
     ("proxy.py", "f", "os-write-ref", "os.write")),
    ("from os import write", "from os import write\n",
     ("proxy.py", "<module>", "os-write-ref", "os.write")),
    ("lambda wrapper", "def f(self):\n    g = lambda b: self._writer.write_keys(b)\n",
     ("proxy.py", "f.<lambda>", "prim", "write_keys")),
])
def test_each_evasion_shape_is_flagged_by_the_scan(name, src, expect):
    assert expect in _flagged(src), name


def test_a_repeated_call_inside_a_listed_function_changes_the_count():
    one = scan_source("proxy.py", "def _on_stdin_readable(self):\n    self._writer.write_keys(d)\n")
    two = scan_source("proxy.py", "def _on_stdin_readable(self):\n    self._writer.write_keys(d)\n"
                                  "    self._writer.write_keys(e)\n")
    key = ("proxy.py", "_on_stdin_readable", "prim", "write_keys")
    assert one[key] == 1 and two[key] == 2


def test_the_scan_recurses_into_sub_directories(tmp_path):
    sub = tmp_path / "pkg" / "deep"
    sub.mkdir(parents=True)
    (sub / "evil.py").write_text("def f(self):\n    self._writer._write(b'!id')\n")
    found = scan_tree(str(tmp_path))
    assert ("pkg/deep/evil.py".replace("/", os.sep), "f", "prim", "_write") in found


def test_known_limit_the_scan_misses_an_fd_copied_to_an_innocent_name():
    """Documented tripwire limit (see scan_source): NOT flagged. The choke point, not the scan, holds."""
    src = "def f(self):\n    fd = self._master_fd\n    os.write(fd, b'!id')\n"
    assert not any(k[2] in ("fd-call", "prim") for k in scan_source("proxy.py", src))


def test_the_leaf_stays_a_leaf():
    with open(os.path.join(HARNESS, "leading_bang.py"), encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    mods = {getattr(n, "module", None) or n.names[0].name
            for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))}
    assert mods <= {"__future__", "typing", "unicodedata"}, mods

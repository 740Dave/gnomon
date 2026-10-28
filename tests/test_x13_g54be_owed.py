"""X13 g54b(e) owed items (audit-x13-runtime-gate P2-1..P2-4 and P3): the workspace-scope flag holds at the PRIMITIVE layer, not only at
the objects above it. Every test here FAILS on the pre-fix code (the tree audited by audit-x13-runtime-gate). Flag OFF unless a test says so.

* P2-1: ``launchhook.prepare``'s alert closure passes the resolved enabled/config, never a literal True.
* P2-2: ``scopebase.atomic_write_*`` and ``fingerprint.dump`` gate themselves (omitted flag arguments refuse), so a ``ScopeWriter`` subclass
  with a no-op ``_gate``, or a class defined outside the package, cannot write through them.
* P2-3: ``Prepared.write`` is a SNAPSHOT of what prepare resolved when minted with ``enabled=True`` (the LaunchManager path) and a LIVE
  re-read when minted with ``enabled=None``; the docstring says exactly that.
* P2-4: ``write_alert`` / ``retag_alert`` / ``set_current_project`` pass the writer's own enabled/config (a no-op ``_gate`` does not open them).
* P3: ``require_enabled`` accepts only a real bool True (or None = read the flag).
Tmp roots only (g31)."""

import inspect
import os
import re

import pytest

from x13s2_world import Clock, build_world, tree_digest
from gnomon.config import GnomonConfig
from gnomon.workspace import fingerprint, launchhook, scopebase
from gnomon.workspace.scopebase import SCOPE_CORE_FLAG, ScopeCoreDisabled, require_enabled
from gnomon.workspace.store import TopicStore
from gnomon.workspace.writers import ScopeWriter

OFF = GnomonConfig()


@pytest.fixture
def w(tmp_path):
    return build_world(tmp_path)


class NoGate(ScopeWriter):
    """A subclass whose ``_gate`` is a no-op: the gate at the OBJECT/primitive must still hold."""

    def _gate(self, what):
        return None


def _nogate(w, **kw):
    return NoGate(w.deploy, w.data, event_log_path=w.event_log, corpora=w.corpora(), clock=Clock(), config=OFF, **kw)


def _calls(w, wr):
    sd = w.session("s1", current=None)
    return {
        "create_item": lambda: wr.create_item("wsa/roadmap", "RM-9", "t", project="wsa/alpha", by="x"),
        "refile_item": lambda: wr.refile_item("wsa/roadmap", "RM-1", "wsa/beta", by="x"),
        "edit_item_property": lambda: wr.edit_item_property("wsa/roadmap", "RM-1", "status", "done"),
        "record_built_live": lambda: wr.record_built_live("wsa/roadmap", "RM-1", "built", "r1"),
        "archive_project": lambda: wr.archive_project("wsa", "gamma", by="x"),
        "add_workspace": lambda: wr.add_workspace("wsc", "WSC"),
        "add_backlog": lambda: wr.add_backlog("wsa", {"id": "b2", "label": "B", "corpus": "corp-a", "prefix": "ZZ", "root-node": "RM-ROOT",
                                                       "operator-gate-value": "op"}),
        "rename_workspace": lambda: wr.rename_workspace("wsa", "New"),
        "archive_workspace": lambda: wr.archive_workspace("wsa", by="x"),
        "delete_topic": lambda: wr.delete_topic("TOP-001", acting="machine"),
        "delete_workspace": lambda: wr.delete_workspace("wsa"),
        "create_topic": lambda: wr.create_topic("t", project="wsa/alpha", by="x"),
        "refile_topic": lambda: wr.refile_topic("TOP-001", "wsa/beta", by="x"),
        "add_corpus": lambda: wr.add_corpus("c2", roots=[w.root], prefix="CC", owner="machine"),
        "stamp_decision": lambda: wr.stamp_decision(w.answers_file(), "DEC-1", "t"),
        # P2-4: the three sites that used to hard-code enabled=True
        "write_alert": lambda: wr.write_alert("m"),
        "retag_alert": lambda: wr.retag_alert("EV-x", workspace_tag="wsa", by="x"),
        "set_current_project": lambda: wr.set_current_project(sd, "wsa/alpha", by="x"),
    }


_NAMES = list(_calls(type("W", (), {"root": "/x", "answers_file": lambda s: "/x", "session": lambda s, n, **k: "/x"})(), None))


def _digest(w):
    """Full-tree digest INCLUDING directories (a leaked empty directory changes it) and event-log existence."""
    import hashlib

    h = hashlib.sha256()
    for d, dirs, files in os.walk(w.root):
        dirs.sort()
        h.update(("D:" + os.path.relpath(d, w.root)).encode())
        for fn in sorted(files):
            h.update(fn.encode())
            with open(os.path.join(d, fn), "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


# ------------------------------------------------------------------ P2-2 / P2-4: a no-op _gate override does not open ANY write
@pytest.mark.parametrize("name", _NAMES)
def test_p22_p24_a_noop_gate_subclass_cannot_write_through_any_public_method_flag_off(w, name):
    calls = _calls(w, _nogate(w))
    before = _digest(w)
    # a ScopeCoreDisabled from the primitive/object, or a business refusal raised before any write: never a returned write
    try:
        calls[name]()
    except ScopeCoreDisabled:
        pass
    except Exception as exc:  # noqa: BLE001 - a business-rule refusal before the write is also "nothing written"
        assert type(exc).__name__ in ("ScopeRefusal", "ModelError"), exc
    else:
        pytest.fail(f"{name} returned normally with the flag OFF and a no-op _gate")
    assert _digest(w) == before, f"{name}: a byte or a directory changed with the flag OFF"
    assert not os.path.exists(w.event_log), f"{name}: an event was appended with the flag OFF"


@pytest.mark.parametrize("name", ["add_workspace", "archive_workspace", "create_item", "write_alert", "set_current_project", "delete_topic"])
def test_p22_the_same_calls_with_the_flag_on_still_write(w, name):
    """Control: with the flag ON the gated paths write (so the refusals above are the gate, not a broken path)."""
    on = NoGate(w.deploy, w.data, event_log_path=w.event_log, corpora=w.corpora(), clock=Clock(), enabled=True)
    t = on.create_topic("t", project="wsa/alpha", by="x")
    calls = _calls(w, on)
    calls["delete_topic"] = lambda: on.delete_topic(t["id"], acting="workspace:wsa")
    before = _digest(w)
    calls[name]()
    assert _digest(w) != before or os.path.exists(w.event_log)


# ------------------------------------------------------------------ P2-2: the primitives themselves
def test_p22_atomic_write_primitives_refuse_when_the_flag_arguments_are_not_passed(w, tmp_path):
    p, q = str(tmp_path / "a.json"), str(tmp_path / "a.txt")
    with pytest.raises(ScopeCoreDisabled):
        scopebase.atomic_write_json(p, {"a": 1})
    with pytest.raises(ScopeCoreDisabled):
        scopebase.atomic_write_text(q, "a")
    assert not os.path.exists(p) and not os.path.exists(q)


def test_p22_atomic_write_primitives_refuse_flag_off_by_value_and_by_config_and_write_flag_on(tmp_path):
    p = str(tmp_path / "d" / "a.json")
    for kw in ({"enabled": False}, {"enabled": None, "config": OFF}):
        with pytest.raises(ScopeCoreDisabled):
            scopebase.atomic_write_json(p, {"a": 1}, **kw)
        with pytest.raises(ScopeCoreDisabled):
            scopebase.atomic_write_text(p, "a", **kw)
    assert not os.path.exists(os.path.dirname(p)), "the primitive made a directory before refusing"
    scopebase.atomic_write_json(p, {"a": 1}, enabled=True)             # control: ON writes
    assert os.path.isfile(p)
    on = GnomonConfig(flags={SCOPE_CORE_FLAG: True})
    scopebase.atomic_write_text(p, "b", enabled=None, config=on)
    assert open(p).read() == "b"


def test_p22_fingerprint_dump_gates_itself(tmp_path):
    p = str(tmp_path / "fp.json")
    with pytest.raises(ScopeCoreDisabled):
        fingerprint.dump({"a": [1, 2, 3, 4]}, p)
    with pytest.raises(ScopeCoreDisabled):
        fingerprint.dump({"a": [1, 2, 3, 4]}, p, enabled=None, config=OFF)
    assert not os.path.exists(p)
    fingerprint.dump({"a": [1, 2, 3, 4]}, p, enabled=True)
    assert fingerprint.load(p) == {"a": [1, 2, 3, 4]}


def test_p22_the_fingerprint_cli_snap_stays_flag_less_by_the_s1_ruling(tmp_path, capsys):
    out, root = str(tmp_path / "snap.json"), str(tmp_path / "root")
    os.makedirs(root)
    assert fingerprint.main(["snap", out, root]) == 0
    assert os.path.isfile(out)


def test_p22_an_outside_subclass_calling_the_primitives_directly_refuses_flag_off(w):
    class Outside(ScopeWriter):
        def leak_json(self):
            scopebase.atomic_write_json(os.path.join(self.data_root, "leak.json"), {})

        def leak_text(self):
            scopebase.atomic_write_text(os.path.join(self.data_root, "leak.txt"), "x")

    o = Outside(w.deploy, w.data, event_log_path=w.event_log, config=OFF)
    before = _digest(w)
    for fn in (o.leak_json, o.leak_text):
        with pytest.raises(ScopeCoreDisabled):
            fn()
    assert _digest(w) == before


def test_p22_every_primitive_call_site_in_the_package_passes_its_flag_arguments():
    """A call to ``atomic_write_json`` / ``atomic_write_text`` in gnomon/workspace/ (outside scopebase) without ``enabled=`` would fail
    closed at run time; this finds it statically so the author sees it before a flag-ON run does."""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gnomon", "workspace")
    import ast

    bad = []
    for fn in sorted(os.listdir(root)):
        if not fn.endswith(".py") or fn == "scopebase.py":
            continue
        tree = ast.parse(open(os.path.join(root, fn)).read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                nm = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if nm in ("atomic_write_json", "atomic_write_text") and not any(k.arg == "enabled" for k in n.keywords):
                    bad.append(f"{fn}:{n.lineno}")
    assert bad == []


# ------------------------------------------------------------------ P2-1: the alert closure
def _prepared(w, **kw):
    sd = os.path.join(w.sessions, "g54-prep")
    os.makedirs(sd, exist_ok=True)
    md = os.path.join(w.root, "claude.md")
    with open(md, "w") as fh:
        fh.write("# Role one\n")
    return launchhook.prepare(instance_id="role-one", deploy_root=w.deploy, profile_raw={}, sessions_root=w.sessions, session_dir=sd, **kw), sd, md


def test_p21_the_alert_closure_appends_nothing_after_the_flag_goes_off(w):
    cfg = GnomonConfig(flags={SCOPE_CORE_FLAG: True})
    prep, sd, md = _prepared(w, config=cfg)
    n_on = len(w.events())
    prep.resolver.get("a_key_nobody_registered", safety=True)
    assert len(w.events()) == n_on + 1, "control: with the flag ON the alert is appended"
    cfg.flags[SCOPE_CORE_FLAG] = False
    prep.resolver.get("another_key_nobody_registered", safety=True)      # must not raise, must not write
    assert len(w.events()) == n_on + 1, "an alert was appended after the flag went OFF (hard-coded enabled=True)"


def test_p21_no_literal_enabled_true_is_left_in_launchhook_code():
    src = inspect.getsource(launchhook)
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert "enabled=True" not in code


# ------------------------------------------------------------------ P2-3: Prepared.write, said precisely
def test_p23_the_docstring_states_the_snapshot_and_the_live_reread_precisely():
    doc = launchhook.Prepared.write.__doc__
    assert "SNAPSHOT" in doc and "enabled=None" in doc and "enabled=True" in doc and "LaunchManager" in doc


def test_p23_enabled_none_mint_rereads_the_real_flag_at_write(w):
    cfg = GnomonConfig(flags={SCOPE_CORE_FLAG: True})
    prep, sd, md = _prepared(w, config=cfg)
    cfg.flags[SCOPE_CORE_FLAG] = False
    before = _digest(w)
    with pytest.raises(ScopeCoreDisabled):
        prep.write(sd, md)
    assert _digest(w) == before


def test_p23_enabled_true_mint_is_a_snapshot_and_writes_flag_on_path_still_works(w):
    """The production path (LaunchManager passes enabled=True): the write succeeds although the registry flag is OFF, because the
    snapshot IS the resolved value. A real re-read here would break flag-ON launches (7-mode differential)."""
    prep, sd, md = _prepared(w, enabled=True)
    out = prep.write(sd, md)
    assert os.path.isfile(out["bundle"])


def test_p23_the_snapshot_is_forwarded_to_the_primitives_not_a_literal_true(w, monkeypatch):
    seen = []
    real = scopebase.atomic_write_text

    def spy(path, text, *, enabled=scopebase.UNPASSED, config=None):
        seen.append((enabled, config))
        return real(path, text, enabled=enabled, config=config)

    import gnomon.workspace.bundle as bundle_mod

    monkeypatch.setattr(bundle_mod, "atomic_write_text", spy)
    cfg = GnomonConfig(flags={SCOPE_CORE_FLAG: True})
    prep, sd, md = _prepared(w, config=cfg)                         # enabled=None: the primitive must receive None + the config
    prep.write(sd, md)
    assert seen and all(e is None and c is cfg for e, c in seen), seen


# ------------------------------------------------------------------ P2-4: source-level pin as well
def test_p24_writers_never_hard_code_enabled_true():
    src = inspect.getsource(__import__("gnomon.workspace.writers", fromlist=["x"]))
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    code = re.sub(r'""".*?"""', "", code, flags=re.S)
    assert "enabled=True" not in code


# ------------------------------------------------------------------ P3: strict bool
@pytest.mark.parametrize("v", ["false", "0", "x", "True", 1, 2, 1.0, [1], {"a": 1}, (1,), object()])
def test_p3_require_enabled_refuses_every_truthy_non_bool(v):
    with pytest.raises(ScopeCoreDisabled):
        require_enabled(v, OFF, "t")


@pytest.mark.parametrize("v", [False, 0, "", [], {}])
def test_p3_require_enabled_refuses_falsy(v):
    with pytest.raises(ScopeCoreDisabled):
        require_enabled(v, OFF, "t")


def test_p3_require_enabled_accepts_a_real_true_and_none_reads_the_flag():
    require_enabled(True, OFF, "t")
    require_enabled(None, GnomonConfig(flags={SCOPE_CORE_FLAG: True}), "t")
    with pytest.raises(ScopeCoreDisabled):
        require_enabled(None, OFF, "t")


def test_p3_a_truthy_string_does_not_write_through_a_store(w):
    before = _digest(w)
    for v in ("false", "0", "x", 1):
        with pytest.raises(ScopeCoreDisabled):
            TopicStore(w.data).allocate_id(enabled=v)
    assert _digest(w) == before


def test_p22_delete_topic_unlink_is_gated_at_the_call_even_with_a_noop_gate(w):
    """The business rule is satisfied (owner acting, topic exists), so the ONLY thing stopping the unlink is the gate at the unlink."""
    on = w.writer(True)
    t = on.create_topic("t", project="wsa/alpha", by="x")
    off = _nogate(w)
    with pytest.raises(ScopeCoreDisabled):
        off.delete_topic(t["id"], acting="workspace:wsa")
    assert os.path.isfile(t["path"])


def test_p22_add_workspace_leaves_no_directory_with_a_noop_gate(w):
    off = _nogate(w)
    before = _digest(w)
    with pytest.raises(ScopeCoreDisabled):
        off.add_workspace("wsc", "WSC")
    assert _digest(w) == before and not os.path.exists(os.path.join(w.deploy, "workspaces", "wsc"))


def test_p24_retag_alert_with_a_noop_gate_refuses_at_the_event_layer_for_a_REAL_event(w):
    """The event exists (appended with the flag ON), so a refusal for 'event-unknown' cannot stand in for the gate: only the flag passed down
    to ``events.retag_event`` can refuse."""
    ev_id = w.writer(True).write_alert("m")["id"]
    log = open(w.event_log).read()
    with pytest.raises(ScopeCoreDisabled):
        _nogate(w).retag_alert(ev_id, workspace_tag="wsa", by="x")
    assert open(w.event_log).read() == log


def test_p24_set_current_project_and_write_alert_with_a_noop_gate_write_nothing(w):
    sd = w.session("s9", current=None)
    state = open(os.path.join(sd, ".claude", "scope-state.json")).read()
    off = _nogate(w)
    with pytest.raises(ScopeCoreDisabled):
        off.set_current_project(sd, "wsa/alpha", by="x")
    with pytest.raises(ScopeCoreDisabled):
        off.write_alert("m")
    assert open(os.path.join(sd, ".claude", "scope-state.json")).read() == state and not os.path.exists(w.event_log)

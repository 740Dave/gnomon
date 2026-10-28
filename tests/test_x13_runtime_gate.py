"""X13 runtime gate (audit-x13-f2-detector F-1 and F-5 g54b(d)): the workspace-scope flag holds at RUNTIME, not only in the
scan. Every test here FAILS on the pre-fix code (the scan-only gate). With ``workspace_scope_core`` OFF:

* F-1: five spellings of a write through an ungated PUBLIC ``ScopeWriter`` method (and a bare store, and an event append) refuse at the object
  (the store write methods and the event append gate themselves) and write nothing, append no event;
* F-5: a ``launchhook.Prepared`` cannot be forged into writing (``enabled=True``, ``dataclasses.replace``, a hand-built one, even a forged
  capability); ``write`` re-reads the real flag, so a flag turned OFF after ``prepare`` also refuses.

Controls: the same calls with the flag ON write, so each refusal is the gate. Tmp roots only (g31)."""

import dataclasses
import os

import pytest

from x13s2_world import Clock, build_world, tree_digest
from gnomon.config import GnomonConfig
from gnomon.workspace import launchhook
from gnomon.workspace.scopebase import SCOPE_CORE_FLAG, ScopeCoreDisabled
from gnomon.workspace.store import CorpusRegistry, TopicStore
from gnomon.workspace.writers import ScopeWriter


@pytest.fixture
def w(tmp_path):
    return build_world(tmp_path)


def _off_writer(w):
    """A writer with NO explicit value: it reads the real flag (registry default OFF) from an injected, empty machine config."""
    return ScopeWriter(w.deploy, w.data, event_log_path=w.event_log, corpora=w.corpora(), clock=Clock(), config=GnomonConfig())


class Leaky(ScopeWriter):
    """Public methods a careless future author could add: none gates first, each reaches a persisting object another way."""

    def alias_variable(self):
        t = self.topics
        return t.write("wsa", {"id": "TOP-777", "title": "x"})

    def fresh_store(self):
        return TopicStore(self.data_root).allocate_id()

    def foreign_receiver(self, other):
        return other.topics.allocate_id()

    def via_getattr(self):
        return getattr(self.topics, "write")("wsa", {"id": "TOP-778", "title": "x"})

    def aliased_corpora(self):
        c = self._need_corpora()
        return c.add("leaked-corpus", {"roots": [], "owner": "machine"})

    def via_emit(self):
        return self._emit([{"kind": "alert", "message": "leak"}])

    def bare_registry(self, path):
        return CorpusRegistry(path).add("bare-corpus", {"roots": []})


def _calls(w, writer, other):
    return {
        "alias-variable": lambda: writer.alias_variable(),
        "fresh-store": lambda: writer.fresh_store(),
        "foreign-receiver": lambda: writer.foreign_receiver(other),
        "getattr": lambda: writer.via_getattr(),
        "aliased-corpora": lambda: writer.aliased_corpora(),
        "emit-event": lambda: writer.via_emit(),
        "bare-registry": lambda: writer.bare_registry(w.corpus_config),
        "bare-topic-store-write": lambda: TopicStore(w.data).write("wsa", {"id": "TOP-779", "title": "x"}),
        "bare-topic-store-counter": lambda: TopicStore(w.data).allocate_id(),
    }


_NAMES = ["alias-variable", "fresh-store", "foreign-receiver", "getattr", "aliased-corpora", "emit-event", "bare-registry",
          "bare-topic-store-write", "bare-topic-store-counter"]


@pytest.mark.parametrize("name", _NAMES)
def test_f1_every_spelling_of_a_store_or_event_write_through_an_ungated_public_method_refuses_flag_off(w, name):
    writer = Leaky(w.deploy, w.data, event_log_path=w.event_log, corpora=w.corpora(), clock=Clock(), config=GnomonConfig())
    other = _off_writer(w)
    digest = tree_digest(w.root)
    with pytest.raises(ScopeCoreDisabled):
        _calls(w, writer, other)[name]()
    assert tree_digest(w.root) == digest, "a byte changed with the flag OFF"
    assert not os.path.exists(w.event_log), "an event was appended with the flag OFF"


def test_f1_an_explicit_off_writer_refuses_too_not_only_the_registry_default(w):
    writer = Leaky(w.deploy, w.data, event_log_path=w.event_log, corpora=w.corpora(), clock=Clock(), enabled=False)
    digest = tree_digest(w.root)
    for fn in (writer.alias_variable, writer.fresh_store, writer.via_getattr, writer.aliased_corpora, writer.via_emit):
        with pytest.raises(ScopeCoreDisabled):
            fn()
    assert tree_digest(w.root) == digest and not os.path.exists(w.event_log)


def test_f1_control_flag_on_the_same_store_calls_write(w):
    """With the flag explicitly ON the gated entry points still write (so the refusals above are the gate, not a broken path)."""
    on = w.writer(True)
    digest = tree_digest(w.root)
    tid = on.topics.allocate_id(enabled=True)
    on.topics.write("wsa", {"id": tid, "title": "x", "project": "wsa/alpha", "state": "active"}, enabled=True)
    on.corpora.add("on-corpus", {"roots": []}, enabled=True)
    on._emit([{"kind": "alert", "message": "ok"}])
    assert tree_digest(w.root) != digest and os.path.exists(w.event_log)
    # and a writer-level method still creates a topic through the gated store calls
    out = on.create_topic("t", project="wsa/alpha", by="r")
    assert os.path.isfile(out["path"])


# ------------------------------------------------------------------ F-5: Prepared cannot be forged into writing
def _prepared(w, **kw):
    sd = os.path.join(w.sessions, "rt-prep")
    os.makedirs(sd, exist_ok=True)
    md = os.path.join(w.root, "claude.md")
    with open(md, "w") as fh:
        fh.write("# Role one\n")
    prep = launchhook.prepare(instance_id="role-one", deploy_root=w.deploy, profile_raw={}, sessions_root=w.sessions, session_dir=sd,
                              conversation_key=None, **kw)
    return prep, sd, md


def _init_fields(prep):
    return [getattr(prep, f.name) for f in dataclasses.fields(prep) if f.init]


def test_f5_prepared_enabled_true_cannot_be_passed_there_is_no_such_parameter(w):
    prep, sd, md = _prepared(w, enabled=True)
    with pytest.raises(TypeError):
        launchhook.Prepared(*_init_fields(prep), enabled=True)
    with pytest.raises(TypeError):
        dataclasses.replace(prep, enabled=True)
    with pytest.raises((TypeError, ValueError)):
        dataclasses.replace(prep, _grant=launchhook._GRANT)


def test_f5_a_replaced_or_hand_built_prepared_writes_nothing_even_when_the_flag_resolution_is_on(w):
    prep, sd, md = _prepared(w, enabled=True)
    digest = tree_digest(w.root)
    for bad in (dataclasses.replace(prep), launchhook.Prepared(*_init_fields(prep)), dataclasses.replace(prep, instance_id="role-two")):
        with pytest.raises(ScopeCoreDisabled):
            bad.write(sd, md)
        assert tree_digest(w.root) == digest


def test_f5_even_a_forged_capability_cannot_write_with_the_real_flag_off(w):
    """Someone who reaches into the module and copies the sentinel still meets the live flag read (registry default OFF here)."""
    prep, sd, md = _prepared(w, enabled=True)
    forged = launchhook.Prepared(*_init_fields(prep))
    object.__setattr__(forged, "_grant", launchhook._GRANT)               # forged capability, flag arguments left at their default (None)
    digest = tree_digest(w.root)
    with pytest.raises(ScopeCoreDisabled):
        forged.write(sd, md)
    assert tree_digest(w.root) == digest


def test_f5_write_rereads_the_real_flag_a_flag_turned_off_after_prepare_refuses(w):
    cfg = GnomonConfig(flags={SCOPE_CORE_FLAG: True})
    prep, sd, md = _prepared(w, config=cfg)                               # enabled=None: prepare resolved the flag from cfg
    cfg.flags[SCOPE_CORE_FLAG] = False
    digest = tree_digest(w.root)
    with pytest.raises(ScopeCoreDisabled):
        prep.write(sd, md)
    assert tree_digest(w.root) == digest
    cfg.flags[SCOPE_CORE_FLAG] = True                                  # control: back ON, the same Prepared writes
    out = prep.write(sd, md)
    assert os.path.isfile(out["bundle"]) and tree_digest(w.root) != digest


def test_f5_the_docstring_claim_matches_the_object():
    doc = launchhook.Prepared.write.__doc__
    assert "REAL flag" in doc and "dataclasses.replace" in doc
    assert "enabled" not in {f.name for f in dataclasses.fields(launchhook.Prepared)}

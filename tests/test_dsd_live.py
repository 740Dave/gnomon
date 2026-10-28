"""The LIVE lts-engine check for DSD status (D1).

Replaces the hardcoded ``registration_held=True`` guess with a real query of the
engine's own corpus store. These tests inject a FAKE store (so no ambient corpus
config is needed) to prove each verdict, plus one loud-fail case for D2 ("complain
loudly when lts-engine is offline / not registered — never a silent degrade").
"""

import pytest

from gnomon.dsd.live import (
    default_corpus_id,
    LiveDaemonStatus,
    check_live_status,
)


class _FakeStore:
    """A minimal stand-in for the engine's LtsStore: corpus_ids + get_partition +
    a partition with has_node — the exact surface the live check reads."""

    def __init__(self, corpora):
        # corpora: {corpus_id: set(node_ids)}
        self._corpora = corpora

    def corpus_ids(self):
        return list(self._corpora)

    def get_partition(self, cid):
        nodes = self._corpora.get(cid)
        if nodes is None:
            return None
        return _FakePartition(nodes)


class _FakePartition:
    def __init__(self, nodes):
        self._nodes = set(nodes)

    def has_node(self, node_id):
        return node_id in self._nodes


# -- registered + reachable + visible = LIVE ---------------------------------

def test_registered_reachable_and_node_visible_is_live():
    store = _FakeStore({default_corpus_id(): {"DEC-DSD-CAFE"}})
    st = check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-CAFE",
                           store_builder=lambda: store)
    assert st.engine_available and st.registered and st.reachable
    assert st.node_visible is True
    assert st.live is True
    assert st.banner() == ""            # live => no loud banner


def test_registered_reachable_without_probe_is_live():
    store = _FakeStore({default_corpus_id(): set()})
    st = check_live_status(default_corpus_id(), store_builder=lambda: store)
    assert st.node_visible is None      # not probed
    assert st.live is True


# -- the just-written node is NOT visible -> not live, loud ------------------

def test_written_node_not_visible_is_not_live_and_loud():
    store = _FakeStore({default_corpus_id(): {"DEC-DSD-OTHER"}})
    st = check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-CAFE",
                           store_builder=lambda: store)
    assert st.registered and st.reachable
    assert st.node_visible is False
    assert st.live is False
    assert "does NOT serve it" in st.banner()


# -- corpus NOT registered -> not live, loud (D2) ----------------------------

def test_corpus_not_registered_is_not_live_and_loud():
    store = _FakeStore({"alpha": set(), "beta": set()})   # DSD corpus absent
    st = check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-CAFE",
                           store_builder=lambda: store)
    assert st.engine_available is True
    assert st.registered is False
    assert st.live is False
    assert st.registered_corpora == ["alpha", "beta"]     # what the engine DOES know
    b = st.banner()
    assert "NOT" in b and "registered" in b


# -- the engine cannot be built at all -> lts-engine offline, loud (D2) ------

def test_engine_unbuildable_is_offline_and_loud():
    def _raise():
        raise RuntimeError("no corpora loaded — check FACTLATTICE_CORPUS_CONFIG")

    st = check_live_status(default_corpus_id(), store_builder=_raise)
    assert st.engine_available is False
    assert st.live is False
    assert "UNREACHABLE" in st.banner()
    assert "RuntimeError" in st.error


# -- the real default builder is wired (smoke; no assertion on ambient state) -

def test_default_builder_runs_without_crashing():
    """The default store_builder (factlattice_engine.config.build_store) must at
    least run and return a status — its live/registered verdict depends on this
    machine's corpus config, which the test does not assert on."""
    st = check_live_status(default_corpus_id())
    assert isinstance(st, LiveDaemonStatus)
    assert st.mechanism  # the honest mechanism string is populated


# -- default builder indexes ONLY the DSD-answer corpus (perf: was the whole registry) ---

def _scoped_env(tmp_path, monkeypatch, with_answers=True):
    import json
    a = tmp_path / "answers"
    a.mkdir()
    (a / "dsd-x.lts").write_text(
        "# SPEC:DSD-ANSWERS-X [DEC-DSD-X]\n\n- **status:** active\n", encoding="utf-8")
    other = tmp_path / "other"
    other.mkdir()
    (other / "o.lts").write_text("# SPEC:OTHER [OTHER-1]\n\n- **status:** active\n", encoding="utf-8")
    cfg = {"gm-other": {"root": str(other), "domain_suffix": "gn.other"}}
    if with_answers:
        cfg["dsd-answers"] = {"root": str(a), "domain_suffix": "gn.dsd-answers"}
    cf = tmp_path / "cc.json"
    cf.write_text(json.dumps(cfg), encoding="utf-8")
    monkeypatch.setenv("FACTLATTICE_CORPUS_CONFIG", str(cf))


def test_default_check_builds_a_store_of_only_the_dsd_corpus(tmp_path, monkeypatch):
    pytest.importorskip("factlattice_engine")
    from gnomon.dsd.live import build_scoped_store
    _scoped_env(tmp_path, monkeypatch)
    assert sorted(build_scoped_store("dsd-answers").corpus_ids()) == ["dsd-answers"]
    st = check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-X")
    assert st.live and st.node_visible is True
    assert st.registered_corpora == ["dsd-answers"]       # NOT the other corpus


def test_default_check_still_fails_loud_when_the_minted_node_does_not_resolve(tmp_path, monkeypatch):
    pytest.importorskip("factlattice_engine")
    _scoped_env(tmp_path, monkeypatch)
    st = check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-MISSING")
    assert st.registered and st.reachable and st.node_visible is False
    assert not st.live and "does NOT serve" in st.banner()


def test_default_check_still_fails_loud_when_the_corpus_is_not_registered(tmp_path, monkeypatch):
    pytest.importorskip("factlattice_engine")
    _scoped_env(tmp_path, monkeypatch, with_answers=False)
    st = check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-X")
    assert st.engine_available and not st.registered and not st.live
    assert "NOT registered" in st.banner()


def test_default_check_does_not_build_other_corpora(tmp_path, monkeypatch):
    pytest.importorskip("factlattice_engine")
    import factlattice_engine.config as cfg
    _scoped_env(tmp_path, monkeypatch)
    seen = []
    real = cfg.build_partition
    monkeypatch.setattr(cfg, "build_partition", lambda cid, *a, **k: (seen.append(cid), real(cid, *a, **k))[1])
    check_live_status(default_corpus_id(), probe_node_id="DEC-DSD-X")
    assert seen == ["dsd-answers"]


def test_writer_status_and_live_status_never_build_a_corpus_other_than_the_dsd_one(tmp_path, monkeypatch):
    """The DSD tab LOAD (GET /api/dsd/status -> writer_status) and every other caller of
    the default live check go through ONE scoped builder: no other corpus is ever built."""
    pytest.importorskip("factlattice_engine")
    import factlattice_engine.config as cfg
    from gnomon.app.dsd_controller import DsdTabController
    from gnomon.dsd import DsdInbox, DsdRoundTrip, TypedLtsDecisionWriter
    from gnomon.hub import SessionRegistry

    _scoped_env(tmp_path, monkeypatch)
    built = []
    for fn in ("build_partition", "build_merged_partition"):
        real = getattr(cfg, fn)
        monkeypatch.setattr(cfg, fn, lambda cid, *a, _r=real, **k: (built.append(cid), _r(cid, *a, **k))[1])
    writer = TypedLtsDecisionWriter(str(tmp_path / "answers" / "dsd-answers.lts"))
    ctrl = DsdTabController(DsdInbox(str(tmp_path / "inbox")),
                            DsdRoundTrip(SessionRegistry(), writer, decided_on="2026-10-02"),
                            writer=writer)                # DEFAULT live check (no injection)
    ws = ctrl.writer_status()
    lv = ctrl.live_status(probe_node_id="DEC-DSD-X")
    assert ws.live.live and lv.live and lv.node_visible is True
    assert built and set(built) == {"dsd-answers"}, built

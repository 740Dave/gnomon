"""Config-tab UX rework (2026-09-28) — data-driven master groups + structured
stall_inject sub-fields, and the new "int"/"multiline" FieldSpec kinds.

Covers:
* ``field_groups()`` structure + that its field_keys cover EVERY field spec;
* the widened stall-timing defaults ("fires too quickly" fix);
* the ``int`` kind parse (incl. nullable blanks where the schema permits);
* ``stall_inject.<sub>`` get/set round-trip, enable/disable, blank->built-ins;
* ``merge_stall_inject`` rejection surfaced on a bad sub-field value;
* the ``/api/config`` GET exposes field_groups and round-trips the structured
  stall fields through the existing save path.

These are additive: they do NOT replace the existing config/stall tests.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

from gnomon.app.config_controller import ConfigController, FIELD_SPECS
from gnomon.config import ConfigStore, GnomonConfig
from gnomon.harness.constants import DEFAULT_STALL_INJECT


# -- Part A: widened timing defaults ------------------------------------------

def test_default_stall_timing_widened():
    """The "fires too quickly" fix: 180/120/45 -> 300/300/150; the rest holds."""
    assert DEFAULT_STALL_INJECT["agent_idle_seconds"] == 300
    assert DEFAULT_STALL_INJECT["operator_idle_seconds"] == 300
    assert DEFAULT_STALL_INJECT["post_compact_idle_seconds"] == 150
    assert DEFAULT_STALL_INJECT["enabled"] is True
    assert DEFAULT_STALL_INJECT["escalate_after_seconds"] == 600
    assert DEFAULT_STALL_INJECT["rearm_grace_seconds"] == 5
    assert DEFAULT_STALL_INJECT["backoff_max"] == 8
    assert isinstance(DEFAULT_STALL_INJECT["content"], str) and DEFAULT_STALL_INJECT["content"].strip()


# -- Part B: field_groups() structure + full coverage -------------------------

def _cc(tmp_path, cfg=None):
    return ConfigController(ConfigStore(str(tmp_path / "config.json")),
                            config=cfg if cfg is not None else GnomonConfig())


def test_field_groups_shape_and_order(tmp_path):
    groups = _cc(tmp_path).field_groups()
    assert [g["key"] for g in groups] == [
        "paths", "launch", "terminal", "context", "stall", "advanced", "mcp"]
    for g in groups:
        assert set(g) >= {"key", "title", "field_keys"}
        assert isinstance(g["title"], str) and g["title"]
        assert isinstance(g["field_keys"], list)
    by_key = {g["key"]: g for g in groups}
    assert by_key["terminal"]["surface"] == "terminal_templates"
    assert by_key["mcp"]["surface"] == "mcp_endpoints"


def test_field_groups_cover_every_field_exactly_once(tmp_path):
    groups = _cc(tmp_path).field_groups()
    covered = []
    for g in groups:
        covered.extend(g["field_keys"])
    assert set(covered) == {s.key for s in FIELD_SPECS}, "a field is orphaned or double-covered"
    assert len(covered) == len(set(covered)), "a field key appears in more than one group"


def test_stall_group_lists_the_eight_sub_fields(tmp_path):
    groups = {g["key"]: g for g in _cc(tmp_path).field_groups()}
    assert groups["stall"]["field_keys"] == [
        "stall_inject.enabled", "stall_inject.agent_idle_seconds",
        "stall_inject.operator_idle_seconds", "stall_inject.escalate_after_seconds",
        "stall_inject.post_compact_idle_seconds", "stall_inject.rearm_grace_seconds",
        "stall_inject.backoff_max", "stall_inject.content"]


def test_field_specs_still_works_and_carries_new_kinds(tmp_path):
    specs = {s.key: s for s in _cc(tmp_path).field_specs()}
    assert specs["stall_inject.agent_idle_seconds"].kind == "int"
    assert specs["stall_inject.agent_idle_seconds"].unit == "seconds"
    assert specs["stall_inject.enabled"].kind == "bool"
    assert specs["stall_inject.content"].kind == "multiline"
    # the former single json stall field is GONE from the rendered specs
    assert "stall_inject" not in specs


# -- Part B: int kind + nullable blanks ---------------------------------------

def test_stall_int_round_trip_and_blank_nullable(tmp_path):
    cc = _cc(tmp_path)
    # a fresh machine shows the (widened) built-in defaults
    assert cc.get_display("stall_inject.agent_idle_seconds") == "300"
    cc.set("stall_inject.agent_idle_seconds", "450")
    assert cc.get_display("stall_inject.agent_idle_seconds") == "450"
    assert cc.config.stall_inject["agent_idle_seconds"] == 450
    # nullable fields accept a blank -> None (mechanism disabled)
    cc.set("stall_inject.escalate_after_seconds", "")
    assert cc.get_display("stall_inject.escalate_after_seconds") == ""
    assert cc.config.stall_inject["escalate_after_seconds"] is None
    cc.set("stall_inject.post_compact_idle_seconds", "")
    assert cc.config.stall_inject["post_compact_idle_seconds"] is None


def test_stall_non_nullable_blank_and_non_int_rejected(tmp_path):
    cc = _cc(tmp_path)
    with pytest.raises(ValueError):
        cc.set("stall_inject.agent_idle_seconds", "")     # not nullable
    with pytest.raises(ValueError):
        cc.set("stall_inject.agent_idle_seconds", "soon")  # not an int


def test_stall_bool_and_content_round_trip(tmp_path):
    cc = _cc(tmp_path)
    assert cc.get_display("stall_inject.enabled") == "true"
    cc.set("stall_inject.enabled", "off")
    assert cc.get_display("stall_inject.enabled") == "false"
    assert cc.config.stall_inject["enabled"] is False
    cc.set("stall_inject.content", "RESUME NOW")
    assert cc.get_display("stall_inject.content") == "RESUME NOW"
    assert cc.config.stall_inject["content"] == "RESUME NOW"


def test_stall_merge_rejection_is_surfaced(tmp_path):
    cc = _cc(tmp_path)
    with pytest.raises(ValueError):
        cc.set("stall_inject.agent_idle_seconds", "0")   # must be positive (merge guard)
    with pytest.raises(ValueError):
        cc.set("stall_inject.content", "   ")            # non-empty content (merge guard)
    with pytest.raises(ValueError):
        cc.set("stall_inject.backoff_max", "0")          # must be positive (merge guard)


def test_blank_back_to_builtins_collapses_to_none(tmp_path):
    cc = _cc(tmp_path)
    cc.set("stall_inject.agent_idle_seconds", "450")
    assert cc.config.stall_inject is not None            # now an explicit override
    cc.set("stall_inject.agent_idle_seconds", "300")     # exactly the built-in default again
    assert cc.config.stall_inject is None, "assembled == built-ins must store None (blank=built-ins)"


def test_stall_edit_stores_sparse_delta_so_a_later_default_bump_reaches_unedited_keys(tmp_path, monkeypatch):
    """Finding 2: editing ONE stall field must persist only a SPARSE DELTA (the
    edited key), never the full 8-key dict — otherwise a later change to a
    DIFFERENT built-in default (e.g. the 180->300 bump) never reaches this user
    because every key is pinned. Regression guard against the full-dict pinning."""
    import gnomon.harness.constants as constants

    cc = _cc(tmp_path)
    cc.set("stall_inject.content", "MY RESUME TEXT")   # edit exactly one field
    stored = cc.config.stall_inject
    assert stored == {"content": "MY RESUME TEXT"}, \
        f"only the edited key must be stored (sparse delta), got {stored!r}"
    # an unedited key still resolves to the current built-in default ...
    assert cc.get_display("stall_inject.agent_idle_seconds") == "300"

    # ... now simulate a LATER default bump on an UNEDITED key and confirm it is
    # inherited (it was never pinned into the stored dict).
    bumped = dict(constants.DEFAULT_STALL_INJECT)
    bumped["agent_idle_seconds"] = 900
    monkeypatch.setattr(constants, "DEFAULT_STALL_INJECT", bumped)
    assert cc.get_display("stall_inject.agent_idle_seconds") == "900", \
        "an unedited key must inherit the NEW default, not a pinned old value"
    assert cc.config.stall_inject == {"content": "MY RESUME TEXT"}, \
        "the stored delta must be unchanged by a default bump on an unedited key"


def test_stall_multi_edit_accumulates_a_minimal_delta_and_keeps_none_distinct(tmp_path):
    """Editing several fields keeps ONLY the differing keys; a nullable numeric
    blanked to None (disabled) is a real override, stored as None and kept
    distinct from the default and from 0 (folds in finding 5)."""
    cc = _cc(tmp_path)
    cc.set("stall_inject.operator_idle_seconds", "360")     # differs from 300
    cc.set("stall_inject.rearm_grace_seconds", "5")         # equals default -> not stored
    cc.set("stall_inject.escalate_after_seconds", "")       # blank -> None (disabled), differs from 600
    stored = cc.config.stall_inject
    assert stored == {"operator_idle_seconds": 360, "escalate_after_seconds": None}, \
        f"delta must hold only differing keys (None override kept), got {stored!r}"
    assert stored["escalate_after_seconds"] is None          # None, not 0, not 600
    # the effective view still merges sparse-over-defaults for the unedited keys
    assert cc.get_display("stall_inject.agent_idle_seconds") == "300"
    assert cc.get_display("stall_inject.backoff_max") == "8"


def test_raw_json_escape_hatch_still_honoured(tmp_path):
    """The bare stall_inject key remains a programmatic JSON escape hatch even
    though it is no longer a rendered field."""
    cc = _cc(tmp_path)
    cc.set("stall_inject", '{"agent_idle_seconds": 222, "enabled": true}')
    assert json.loads(cc.get_display("stall_inject")) == {"agent_idle_seconds": 222, "enabled": True}
    # and the structured view reflects it (222 explicit, other keys = defaults)
    assert cc.get_display("stall_inject.agent_idle_seconds") == "222"
    assert cc.get_display("stall_inject.operator_idle_seconds") == "300"
    cc.set("stall_inject", "")
    assert cc.config.stall_inject is None


# -- Part D: /api/config exposes groups + round-trips structured stall ---------

@pytest.fixture()
def client(tmp_path, monkeypatch):
    import web.backend.server as srv
    monkeypatch.setattr(srv, "_config_store", ConfigStore(str(tmp_path / "config.json")))
    return TestClient(srv.app)


def test_api_config_get_exposes_field_groups(client):
    body = client.get("/api/config").json()
    assert "field_groups" in body
    assert [g["key"] for g in body["field_groups"]] == [
        "paths", "launch", "terminal", "context", "stall", "advanced", "mcp"]
    # sub-fields are present in the flat fields with their kinds + unit hints
    by_key = {f["key"]: f for f in body["fields"]}
    assert by_key["stall_inject.agent_idle_seconds"]["kind"] == "int"
    assert by_key["stall_inject.agent_idle_seconds"]["unit"] == "seconds"
    assert by_key["stall_inject.agent_idle_seconds"]["value"] == "300"
    assert by_key["stall_inject.content"]["kind"] == "multiline"


def test_api_config_round_trips_structured_stall_fields(client, tmp_path):
    r = client.post("/api/config", json={"fields": {
        "stall_inject.enabled": "true",
        "stall_inject.agent_idle_seconds": "420",
        "stall_inject.operator_idle_seconds": "360",
        "stall_inject.escalate_after_seconds": "",   # nullable blank
        "stall_inject.post_compact_idle_seconds": "150",
        "stall_inject.rearm_grace_seconds": "5",
        "stall_inject.backoff_max": "8",
        "stall_inject.content": "CUSTOM RESUME",
    }})
    assert r.status_code == 200 and r.json()["ok"] is True
    stored = ConfigStore(str(tmp_path / "config.json")).load().stall_inject
    assert stored["agent_idle_seconds"] == 420
    assert stored["operator_idle_seconds"] == 360
    assert stored["escalate_after_seconds"] is None
    assert stored["content"] == "CUSTOM RESUME"
    # and the GET reflects it
    by_key = {f["key"]: f["value"] for f in client.get("/api/config").json()["fields"]}
    assert by_key["stall_inject.agent_idle_seconds"] == "420"
    assert by_key["stall_inject.escalate_after_seconds"] == ""


def test_api_config_rejects_bad_stall_value_server_side(client, tmp_path):
    r = client.post("/api/config", json={"fields": {"stall_inject.agent_idle_seconds": "0"}})
    assert r.status_code == 400 and r.json()["ok"] is False
    # nothing persisted on refusal
    assert not os.path.isfile(str(tmp_path / "config.json"))

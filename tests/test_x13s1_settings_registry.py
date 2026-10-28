"""X13 S1: settings-registry.json (registry, I7; MACHINE-READ under g25)."""

import dataclasses
import json
import os

import pytest

from gnomon import config as cfgmod, flags as F, profile as profmod
from gnomon.harness import agent_launch as al
from gnomon.launch import LaunchManager
from gnomon.workspace import settings_registry as SR


@pytest.fixture(scope="module")
def reg():
    return SR.load()


def test_every_setting_has_one_default_a_type_and_a_scope_floor(reg):
    assert len(reg["settings"]) >= SR.MIN_SETTINGS
    for name, row in reg["settings"].items():
        assert "default" in row and row["type"] in SR._TYPES, name
        assert row["scope-floor"] and set(row["scope-floor"]) <= set(SR.LAYERS), name


def test_loader_fails_rather_than_shrinking(reg):
    small = json.loads(json.dumps(reg))
    for k in list(small["settings"])[5:]:
        del small["settings"][k]
    with pytest.raises(SR.RegistryError, match="floor"):
        SR.validate(small)


def test_loader_rejects_missing_default_bad_layer_and_alias_clash(reg):
    bad = json.loads(json.dumps(reg))
    del bad["settings"]["model"]["default"]
    with pytest.raises(SR.RegistryError, match="default"):
        SR.validate(bad)
    bad = json.loads(json.dumps(reg))
    bad["settings"]["model"]["scope-floor"] = ["galaxy"]
    with pytest.raises(SR.RegistryError, match="scope-floor"):
        SR.validate(bad)
    bad = json.loads(json.dumps(reg))
    bad["settings"]["terminal"]["keys"]["machine"] = "default_model"      # alias of 'model' already
    with pytest.raises(SR.RegistryError, match="already the alias"):
        SR.validate(bad)


def _default_of(dc_field):
    if dc_field.default is not dataclasses.MISSING:
        return dc_field.default
    return dc_field.default_factory()


def test_registry_defaults_equal_the_code_defaults_one_default_per_setting(reg):
    """I7: no read site carries its own default. A registry default that disagrees with config.py is a second default."""
    idx = SR.alias_index(reg)
    cfg_fields = {f.name: f for f in dataclasses.fields(cfgmod.GnomonConfig)}
    for key, f in cfg_fields.items():
        if key == "flags":
            continue
        name = idx[("machine", key)]
        assert reg["settings"][name]["default"] == _default_of(f), f"machine key {key}"


def test_every_config_and_profile_field_is_registered(reg):
    for f in dataclasses.fields(cfgmod.GnomonConfig):
        assert SR.classify_key(reg, f.name) != "UNREGISTERED", f"config.py field {f.name} has no registry row (I7)"
    for f in dataclasses.fields(profmod.Profile):
        if f.name == "instance_id":
            continue
        assert SR.classify_key(reg, f.name) != "UNREGISTERED", f"profile.py field {f.name} has no registry row (I7)"
    assert SR.classify_key(reg, "instance_id") == "run-state"


def test_profile_defaults_agree_where_the_layer_default_is_not_inherit(reg):
    idx = SR.alias_index(reg)
    for f in dataclasses.fields(profmod.Profile):
        if f.name == "instance_id":
            continue
        name = idx.get(("launch-config", f.name))
        if name is None:
            continue
        d = _default_of(f)
        if d is None:         # launch-config None = INHERIT from the lower layer: legal
            continue
        assert reg["settings"][name]["default"] == d, f"profile key {f.name}"


def test_agent_launch_caps_default_matches_the_code(reg):
    assert reg["settings"]["agent_launch"]["default"] == {"max_depth": al.DEFAULT_MAX_DEPTH, "max_children": al.DEFAULT_MAX_CHILDREN}


def test_every_key_the_real_launcher_writes_to_launch_json_is_classified(reg, tmp_path):
    """g29-style: write launch.json through the REAL launcher writer and classify EVERY key it emitted."""
    from datetime import datetime, timezone

    src = tmp_path / "role.md"
    src.write_text("# Role\n")
    prof = profmod.Profile(instance_id="probe-role", claude_md_source=str(src), confirm_compact=True, confirm_compact_timeout=30.0,
                           context_thresholds=[80, 90], stall_inject={"enabled": False}, resume_session_id="abc", resume_cwd=str(tmp_path),
                           resume_prompt="hi", agent_launch_enabled=True)
    mgr = LaunchManager(sessions_root=str(tmp_path / "s"), intelligences_root=str(tmp_path / "i"),
                        clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc))
    res = mgr.launch(prof)
    with open(os.path.join(res.claude_dir, "launch.json")) as fh:
        keys = set(json.load(fh))
    assert len(keys) >= 15, "the real writer emitted too few keys: the test is not reading it"
    unregistered = sorted(k for k in keys if SR.classify_key(reg, k) == "UNREGISTERED")
    assert unregistered == [], f"launch.json keys with no registry classification: {unregistered}"


def test_flag_family_points_at_the_flags_registry_and_the_new_flag_is_in_it(reg):
    assert reg["flag-family"]["registry"] == "gnomon.flags.KNOWN_FLAGS"
    assert "workspace_model" in F.KNOWN_FLAGS and F.KNOWN_FLAGS["workspace_model"].default is False
    assert all(F.FLAG_NAME_RE.match(n) for n in F.KNOWN_FLAGS)


def test_machine_only_keys_and_restrictive_on_skip(reg):
    s = reg["settings"]
    assert s["machine_duty_role"]["scope-floor"] == ["machine"] and s["timezone"]["scope-floor"] == ["machine"]
    assert s["services"]["scope-floor"] == ["machine"]
    assert s["agent_launch_enabled"]["restrictive-on-skip"] is True and s["env_passthrough"]["restrictive-on-skip"] is True
    assert "default_project" in s and set(s["default_project"]["scope-floor"]) == {"launch-config", "session"}


def test_package_data_declares_the_registry_and_no_deployment_converter_data():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    text = open(os.path.join(root, "pyproject.toml")).read()
    assert "settings-registry.json" in text
    assert "converter_data" not in text, "deployment converter data lives in DEPLOY, never in the package (ruling-f4)"


def test_f10_classify_key_respects_the_layer(reg):
    """S1 Diamond F10: a key that is an alias at only ONE layer must not classify at another."""
    assert SR.classify_key(reg, "default_terminal") == "setting:terminal"              # machine alias (legacy, any layer)
    assert SR.classify_key(reg, "default_terminal", "machine") == "setting:terminal"
    assert SR.classify_key(reg, "default_terminal", "launch-config") == "UNREGISTERED"
    assert SR.classify_key(reg, "terminal", "launch-config") == "setting:terminal"
    assert SR.classify_key(reg, "terminal", "machine") == "UNREGISTERED"
    fam = reg["flag-family"]
    assert SR.classify_key(reg, fam["machine-key"], "machine") == "flag-family"
    assert SR.classify_key(reg, fam["machine-key"], "launch-config") in ("flag-family", "UNREGISTERED")
    if fam["machine-key"] != fam["launch-config-key"]:
        assert SR.classify_key(reg, fam["machine-key"], "launch-config") == "UNREGISTERED"
    with pytest.raises(SR.RegistryError):
        SR.classify_key(reg, "terminal", "galaxy")

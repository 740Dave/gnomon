"""X13 S2: the layered settings resolver and restrictive-on-skip (I6 half of ENT-SETTINGS). Reuses S1's packaged
registry (no second registry). Pure tests: no file IO except reading the packaged registry.
"""

import ast
import copy
import inspect
import os

import pytest

from gnomon.workspace import settings_registry as SR
from gnomon.workspace import settings_resolve as RS
from gnomon.workspace.settings_resolve import (Layer, SettingsResolver, UnregisteredSetting, WORKSPACE_UNAVAILABLE,
                                                      layer_from_canonical, layer_from_raw, unavailable)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="module")
def reg():
    return SR.load()


@pytest.fixture
def wreg(reg):
    """The real registry plus one synthetic key that IS settable at the workspace layer (the shipped registry has none yet)."""
    r = copy.deepcopy(reg)
    r["settings"]["ws_probe"] = {"type": "string", "default": "d", "scope-floor": ["machine", "workspace", "launch-config", "session"],
                                 "keys": {"machine": "ws_probe", "workspace": "ws_probe", "launch-config": "ws_probe", "session": "ws_probe"}, "description": "probe"}
    r["settings"]["ws_locked"] = {"type": "boolean", "default": False, "scope-floor": ["machine", "workspace"], "restrictive-on-skip": True,
                                  "restrictive-value": False, "keys": {"machine": "ws_locked", "workspace": "ws_locked"}, "description": "probe"}
    SR.validate(r)
    return r


# ---------------------------------------------------------------- I7 test property, part 1: each key has exactly one default and a scope-floor
def test_i7_every_registered_key_has_exactly_one_default_a_type_a_scope_floor_and_a_layer_alias(reg):
    assert len(reg["settings"]) >= SR.MIN_SETTINGS
    for name, row in reg["settings"].items():
        assert "default" in row and SR.value_matches_type(row["type"], row["default"]), f"{name}: the default must match its type"
        assert row["scope-floor"] and set(row["scope-floor"]) <= set(SR.LAYERS), name
        assert set(row["keys"]) <= set(row["scope-floor"]), name


def test_i7_a_registry_row_marked_restrictive_on_skip_must_name_a_restrictive_value_of_its_type(reg):
    for name, row in reg["settings"].items():
        if row.get("restrictive-on-skip"):
            assert "restrictive-value" in row and SR.value_matches_type(row["type"], row["restrictive-value"]), name
    bad = copy.deepcopy(reg)
    del bad["settings"]["agent_launch_enabled"]["restrictive-value"]
    with pytest.raises(SR.RegistryError, match="restrictive-value"):
        SR.validate(bad)
    bad = copy.deepcopy(reg)
    bad["settings"]["agent_launch_enabled"]["restrictive-value"] = "no"
    with pytest.raises(SR.RegistryError, match="does not match type"):
        SR.validate(bad)
    assert SR.value_matches_type("integer", True) is False and SR.value_matches_type("boolean", True) is True
    assert SR.value_matches_type("integer|null", None) and not SR.value_matches_type("integer", None)


# ---------------------------------------------------------------- I7 part 2: a query returns the effective value AND its source layer
def test_i7_a_query_returns_the_effective_value_and_its_source_layer_with_layer_precedence(wreg):
    mk = lambda n, v: Layer(n, v)  # noqa: E731
    r = SettingsResolver(wreg)
    assert (r.get("ws_probe").value, r.get("ws_probe").source) == ("d", "default")
    r = SettingsResolver(wreg, machine=mk("machine", {"ws_probe": "m"}))
    assert (r.get("ws_probe").value, r.get("ws_probe").source) == ("m", "machine")
    r = SettingsResolver(wreg, machine=mk("machine", {"ws_probe": "m"}), workspace=mk("workspace", {"ws_probe": "w"}))
    assert (r.get("ws_probe").value, r.get("ws_probe").source) == ("w", "workspace")
    r = SettingsResolver(wreg, machine=mk("machine", {"ws_probe": "m"}), workspace=mk("workspace", {"ws_probe": "w"}),
                         launch_config=mk("launch-config", {"ws_probe": "l"}))
    assert (r.get("ws_probe").value, r.get("ws_probe").source) == ("l", "launch-config")
    r = SettingsResolver(wreg, machine=mk("machine", {"ws_probe": "m"}), workspace=mk("workspace", {"ws_probe": "w"}),
                         launch_config=mk("launch-config", {"ws_probe": "l"}), session=mk("session", {"ws_probe": "s"}))
    g = r.get("ws_probe")
    assert (g.value, g.source, g.skipped, g.restrictive) == ("s", "session", (), False)
    eff = r.effective()
    assert set(eff) == set(wreg["settings"]) and eff["ws_probe"].source == "session" and eff["terminal"].source == "default"


def test_i7_a_value_set_outside_the_keys_scope_floor_is_never_applied_and_is_reported(reg):
    # machine_duty_role is machine-only (floor ['machine']): a launch-config or session value must be ignored
    r = SettingsResolver(reg, machine=Layer("machine", {"machine_duty_role": "duty"}), launch_config=Layer("launch-config", {"machine_duty_role": "evil"}),
                         session=Layer("session", {"machine_duty_role": "worse"}))
    g = r.get("machine_duty_role")
    assert (g.value, g.source, g.ignored) == ("duty", "machine", ("launch-config", "session"))


def test_i7_per_layer_key_names_translate_through_the_registrys_alias_map(reg):
    """S1 finding (d): key names differ per layer (default_terminal at machine, terminal at launch-config)."""
    m = layer_from_raw(reg, "machine", {"default_terminal": "kitty", "flags": {}, "surprise": 1})
    lc = layer_from_raw(reg, "launch-config", {"terminal": "wezterm", "default_terminal": "nope"})
    assert m.values == {"terminal": "kitty"} and m.unknown == ("surprise",), "an unregistered key is returned, never silently dropped"
    assert lc.values == {"terminal": "wezterm"} and lc.unknown == ("default_terminal",)
    r = SettingsResolver(reg, machine=m, launch_config=lc)
    assert (r.get("terminal").value, r.get("terminal").source) == ("wezterm", "launch-config")


def test_i7_reading_an_unregistered_key_fails_and_there_is_no_inline_default_parameter(reg):
    r = SettingsResolver(reg)
    with pytest.raises(UnregisteredSetting) as e:
        r.get("not_a_setting")
    assert e.value.code == "unregistered-setting"
    for fn in (SettingsResolver.get, SettingsResolver.value):
        assert "default" not in inspect.signature(fn).parameters, "a read site must never carry its own default (I7)"


# ---------------------------------------------------------------- I7 part 3: grep read sites, none passes an inline default
_KEYS = None


def _registered_key_names():
    reg = SR.load()
    names = set()
    for n, r in reg["settings"].items():
        names.add(n)
        names.update(r["keys"].values())
    return names


def _inline_default_sites(root_dir, only=None):
    """``[(relpath, key)]`` of ``getattr(obj, "<registered key>", <default>)`` sites, and any ``flag(..., default=...)`` call."""
    keys = _registered_key_names()
    out = []
    for d, dirs, files in os.walk(root_dir):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            if not f.endswith(".py") or ".bak" in f:
                continue
            p = os.path.join(d, f)
            rel = os.path.relpath(p, ROOT)
            if only and not only(rel):
                continue
            tree = ast.parse(open(p, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) == 3 \
                        and isinstance(node.args[1], ast.Constant) and node.args[1].value in keys:
                    out.append((rel, node.args[1].value))
                if isinstance(node.func, ast.Attribute) and node.func.attr == "flag" and any(k.arg == "default" for k in node.keywords):
                    out.append((rel, "flag(default=)"))
    return out


def test_i7_the_detector_can_fail(tmp_path):
    p = tmp_path / "planted.py"
    p.write_text("def f(cfg):\n    return getattr(cfg, 'model', 'x')\n")
    tree = ast.parse(p.read_text())
    assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr" and len(n.args) == 3 for n in ast.walk(tree))
    # run the real scanner over a tmp tree holding the planted file
    global ROOT
    old = ROOT
    try:
        ROOT = str(tmp_path)
        assert _inline_default_sites(str(tmp_path)) == [("planted.py", "model")]
    finally:
        ROOT = old


def test_i7_the_s2_modules_carry_no_inline_default_and_no_flag_passes_its_own_default():
    mine = _inline_default_sites(os.path.join(ROOT, "gnomon", "workspace"))
    assert mine == [], f"S2/S1 workspace code reads a registered setting with an inline default: {mine}"
    whole = [s for s in _inline_default_sites(os.path.join(ROOT, "gnomon")) if s[1] == "flag(default=)"]
    assert whole == [], f"a flag read passes its own default (g2): {whole}"


#: PRE-EXISTING I7 violations, measured 2026-09-30 by this scan (44 sites; 12 of them inline a value that differs from the
#: registry default, mostly None meaning 'unset'). A RATCHET: this set may only SHRINK. Burning it down (each read site
#: goes through the resolver or the dataclass default) is owed to a later slice; it is not S2's flag-off-identical scope.
_BASELINE = {
    ("gnomon/agent_def.py", "lts_macro_budget"), ("gnomon/launch.py", "resume_session_id"), ("gnomon/launch.py", "resume_cwd"),
    ("gnomon/launch.py", "resume_prompt"), ("gnomon/launch.py", "confirm_compact_timeout"), ("gnomon/launch.py", "context_thresholds"),
    ("gnomon/launch.py", "stall_inject"), ("gnomon/launch.py", "extra_args"), ("gnomon/launch.py", "env_passthrough"),
    ("gnomon/launch.py", "agent_launch_enabled"), ("gnomon/launch.py", "confirm_compact"), ("gnomon/launch.py", "hook_manifest"),
    ("gnomon/launch.py", "user_name"), ("gnomon/launch.py", "operator_command_plane"), ("gnomon/def_preview.py", "resume_session_id"),
    ("gnomon/def_preview.py", "resume_cwd"), ("gnomon/app/controller.py", "agent_launch_enabled"), ("gnomon/app/controller.py", "user_name"),
    ("gnomon/harness/telemetry.py", "otel_endpoint"), ("gnomon/harness/telemetry.py", "otel_protocol"),
    ("gnomon/harness/self_mcp.py", "services"), ("gnomon/harness/self_mcp.py", "runtime_root"),
    ("gnomon/harness/launcher.py", "operator_command_plane"), ("gnomon/harness/launcher.py", "stall_inject"),
    ("gnomon/harness/launcher.py", "runtime_root"), ("gnomon/harness/launcher.py", "user_name"), ("gnomon/harness/launcher.py", "services"),
    ("gnomon/harness/launcher.py", "hook_manifest"), ("gnomon/harness/post_compact_boot.py", "post_compact_boot"),
    ("gnomon/harness/agent_launch.py", "resume_session_id"), ("gnomon/harness/agent_launch.py", "resume_cwd"),
    ("gnomon/harness/agent_launch.py", "resume_prompt"), ("gnomon/harness/agent_launch.py", "model"),
}


def test_i7_ratchet_no_NEW_inline_default_read_site_of_a_registered_setting_anywhere_in_the_package():
    sites = {s for s in _inline_default_sites(os.path.join(ROOT, "gnomon")) if s[1] != "flag(default=)"}
    new = sorted(sites - _BASELINE)
    assert new == [], f"new inline-default read sites of registered settings (I7): {new}"
    assert len(_BASELINE) == 33 and len(sites) <= len(_BASELINE)


# ---------------------------------------------------------------- restrictive-on-skip (I6 test property, last clause)
def test_i6_an_unreadable_workspace_layer_is_skipped_with_a_banner_the_named_source_and_an_alert(reg):
    alerts = []
    r = SettingsResolver(reg, machine=Layer("machine", {"agent_launch_enabled": True}), workspace=unavailable("workspace", "workspace.json: corrupt"),
                         launch_config=Layer("launch-config", {"agent_launch_enabled": True, "terminal": "t"}), alert=alerts.append)
    assert r.workspace_skipped and WORKSPACE_UNAVAILABLE in r.banner and "corrupt" in r.banner
    assert len(alerts) == 1 and alerts[0]["kind"] == "settings-layer-skipped" and alerts[0]["detail"]["source"] == WORKSPACE_UNAVAILABLE
    r.effective()
    assert len(alerts) == 1, "one alert per skip, not one per read"
    # restrictive-on-skip keys take their most restrictive value and record the named source, over every other layer
    g = r.get("agent_launch_enabled")
    assert (g.value, g.source, g.restrictive, g.skipped) == (False, WORKSPACE_UNAVAILABLE, True, (WORKSPACE_UNAVAILABLE,))
    # every other key resolves from the remaining layers and is marked as resolved with a layer skipped
    t = r.get("terminal")
    assert (t.value, t.source, t.restrictive, t.skipped) == ("t", "launch-config", False, (WORKSPACE_UNAVAILABLE,))
    for name, row in reg["settings"].items():
        if row.get("restrictive-on-skip"):
            assert r.get(name).value == row["restrictive-value"] and r.get(name).source == WORKSPACE_UNAVAILABLE, name


def test_i6_with_the_workspace_layer_readable_nothing_is_skipped_and_no_alert_fires(reg):
    alerts = []
    r = SettingsResolver(reg, machine=Layer("machine", {"agent_launch_enabled": True}), workspace=Layer("workspace", {}), alert=alerts.append)
    assert r.banner is None and alerts == []
    g = r.get("agent_launch_enabled")
    assert (g.value, g.source, g.restrictive, g.skipped) == (True, "machine", False, ())


def test_i6_a_workspace_layer_value_is_used_when_readable_and_lost_to_restriction_when_skipped(wreg):
    ok = SettingsResolver(wreg, machine=Layer("machine", {"ws_locked": False}), workspace=Layer("workspace", {"ws_locked": True}))
    assert (ok.get("ws_locked").value, ok.get("ws_locked").source) == (True, "workspace")
    skipped = SettingsResolver(wreg, machine=Layer("machine", {"ws_locked": True}), workspace=unavailable("workspace", "gone"))
    assert (skipped.get("ws_locked").value, skipped.get("ws_locked").source) == (False, WORKSPACE_UNAVAILABLE)


def test_i6_an_unregistered_key_on_a_safety_path_alerts_loudly_and_takes_the_restrictive_branch(reg):
    alerts = []
    r = SettingsResolver(reg, alert=alerts.append)
    g = r.get("nobody_registered_this", safety=True)
    assert (g.value, g.source, g.restrictive) == (None, RS.UNREGISTERED, True)
    assert [a["kind"] for a in alerts] == ["unregistered-setting-read"] and alerts[0]["severity"] == "error"


def test_layer_helpers_reject_unknown_layers_and_misnamed_layers(reg):
    with pytest.raises(SR.RegistryError):
        layer_from_raw(reg, "galaxy", {})
    with pytest.raises(SR.RegistryError):
        layer_from_canonical(reg, "galaxy", {})
    with pytest.raises(SR.RegistryError):
        SettingsResolver(reg, machine=Layer("session", {}))
    assert layer_from_canonical(reg, "machine", {"timezone": "UTC", "zzz": 1}).unknown == ("zzz",)


def test_the_four_restrictive_values_are_pinned_literally_and_no_other_key_is_restrictive_on_skip(reg):
    """Diamond F11 / architect ruling 2: each is the MOST CONSERVATIVE value for its key. agent_launch_enabled false (no child
    spawning), otel_endpoint '' (exports nothing), env_passthrough null (scrub every credential), services null (no configured
    additions; the shipped defaults cannot be removed, so null is the most restrictive expressible value). Changing one is a
    deliberate, reviewed act: this test must change with it."""
    got = {n: r["restrictive-value"] for n, r in reg["settings"].items() if r.get("restrictive-on-skip")}
    assert got == {"agent_launch_enabled": False, "otel_endpoint": "", "services": None, "env_passthrough": None}
    assert {n: r["default"] for n, r in reg["settings"].items() if n in got} == got, "today each equals its default; a divergence needs a ruling"

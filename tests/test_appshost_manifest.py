"""manifest schema + registration (-RESOURCES, -GUARDS).

Each test name carries the acceptance id it discharges (ac-*), so the mapping is greppable."""

import copy
import json
import os

import pytest

from appshost_support import Env, ROOT
from gnomon.appshost.host import Actor
from gnomon.appshost.manifest import ManifestError, parse_manifest, SCHEMA

BASE = {
    "schema_version": 1, "id": "demo-app", "name": "Demo", "version": "1.0", "purpose": "demo", "owner": "test",
    "start": ["/usr/bin/python3", "serve.py"],
}


def m(**kw):
    d = copy.deepcopy(BASE)
    for k, v in kw.items():
        if v is _DEL:
            d.pop(k, None)
        else:
            d[k] = v
    return d


_DEL = object()


def refused(data):
    with pytest.raises(ManifestError) as ei:
        parse_manifest(data)
    return ei.value


# ---------------------------------------------------------------- ac-manifest-registration-errors
@pytest.mark.parametrize("field", ["schema_version", "id", "name", "version", "purpose", "owner", "start"])
def test_ac_manifest_registration_errors_missing_required_field_named(field):
    e = refused(m(**{field: _DEL}))
    assert e.field == field and "missing" in str(e)


def test_ac_manifest_registration_errors_string_start_refused():
    e = refused(m(start="python3 serve.py --port 1"))
    assert e.field == "start" and "never a shell string" in str(e)


def test_ac_manifest_registration_errors_undeclared_path_placeholder_named():
    e = refused(m(start=["/usr/bin/python3", "serve.py", "--store", "{path:store}"]))
    assert e.field == "start[3]" and "{path:store}" in str(e)


def test_ac_manifest_registration_errors_undeclared_setting_placeholder_named():
    e = refused(m(start=["/usr/bin/python3", "serve.py", "--who", "{setting:who}"]))
    assert e.field == "start[3]" and "{setting:who}" in str(e)


def test_ac_manifest_registration_errors_port_arg_without_health_path():
    e = refused(m(port_arg="--port {port}"))
    assert e.field == "health_path"


def test_ac_manifest_registration_errors_refused_proposal_assigns_no_port_and_registers_nothing(tmp_path):
    env = Env(tmp_path)
    try:
        d = env.app_dir("bad")
        p = os.path.join(d, "gnomon-app.json")
        with open(p, "w") as fh:
            json.dump(m(id="bad-app", port_arg="--port {port}", preferred_port=45001), fh)   # no health_path
        r = env.host.propose_install(p, Actor("agent"))
        assert r["ok"] is False and r["field"] == "health_path" and r["port_assigned"] is False
        assert not os.path.exists(env.host.paths.ports) or "bad-app" not in json.load(open(env.host.paths.ports))
        assert not os.path.exists(env.host.paths.proposal("bad-app"))
        assert env.launchd.calls == []
    finally:
        env.cleanup()


# ---------------------------------------------------------------- ac-unknown-field-refused
def test_ac_unknown_field_refused_top_level_named():
    e = refused(m(embed={"mode": "iframe"}))
    assert e.field == "embed" and "unknown field" in str(e)


def test_ac_unknown_field_refused_nested_named():
    e = refused(m(paths=[{"name": "store", "description": "d", "kind": "file", "access": "writes", "secret": 1}]))
    assert e.field == "paths[0].secret"


@pytest.mark.parametrize("bad", ["A-app", "x", "1app", "app_with_underscore", "a" * 42, "app/../x", ""])
def test_ac_unknown_field_refused_id_outside_grammar(bad):
    e = refused(m(id=bad))
    assert e.field == "id"


def test_id_grammar_accepts_the_spec_examples():
    for good in ("identity-adjudicator", "fl-image", "ab", "a" * 41):
        assert parse_manifest(m(id=good)).id == good


def test_schema_version_unsupported_refused():
    assert refused(m(schema_version=3)).field == "schema_version"   # X72: 2 is now supported (activity_path)
    assert refused(m(schema_version=True)).field == "schema_version"


def test_one_schema_registry_lists_every_field_the_parser_reads():
    """g73-amendment item 6: one registry; every top-level field is in SCHEMA[1]."""
    import inspect
    from gnomon.appshost import manifest as M

    src = inspect.getsource(M.parse_manifest)
    import re
    read = set(re.findall(r'data\["([a-z_]+)"\]', src)) | set(re.findall(r'data\.get\("([a-z_]+)"', src)) | \
        set(re.findall(r'"([a-z_]+)" in data', src))
    assert read <= set(SCHEMA[2]), read - set(SCHEMA[2])   # X72: the newest version's registry lists every field


# ---------------------------------------------------------------- ac-optional-path-flag (registration half)
def _path(name, **kw):
    d = {"name": name, "description": "d", "kind": "file", "access": "read_only"}
    d.update(kw)
    return d


def test_ac_optional_path_flag_optional_without_flag_refused():
    e = refused(m(paths=[_path("events", optional=True)]))
    assert e.field == "paths[0].flag"


def test_ac_optional_path_flag_optional_used_as_placeholder_refused():
    e = refused(m(paths=[_path("events", optional=True, flag="--events")],
                  start=["/usr/bin/python3", "serve.py", "--events", "{path:events}"]))
    assert e.field == "start[3]" and "optional" in str(e)


def test_ac_optional_path_flag_declared_ok():
    mm = parse_manifest(m(paths=[_path("events", optional=True, flag="--events")]))
    assert mm.path("events").optional and mm.path("events").flag == "--events"


# ---------------------------------------------------------------- ac-setting-types (registration half)
def test_ac_setting_types_unknown_type_refused_at_registration():
    e = refused(m(user_editable=[{"name": "x", "type": "colour", "default": "red"}]))
    assert e.field == "user_editable[0].type"


@pytest.mark.parametrize("decl", [
    {"name": "n", "type": "number", "default": "3"},
    {"name": "t", "type": "text", "default": 3},
    {"name": "b", "type": "yes/no", "default": "yes"},
    {"name": "c", "type": "choice", "default": "z", "options": ["a", "b"]},
    {"name": "p", "type": "path", "kind": "dir", "default": "relative/path"},
])
def test_ac_setting_types_default_must_match_type(decl):
    assert refused(m(user_editable=[decl])).field == "user_editable[0].default"


def test_ac_setting_types_all_five_v1_types_accepted():
    mm = parse_manifest(m(user_editable=[
        {"name": "who", "type": "text", "default": "owner"}, {"name": "n", "type": "number", "default": 3},
        {"name": "b", "type": "yes/no", "default": False}, {"name": "c", "type": "choice", "default": "a", "options": ["a", "b"]},
        {"name": "p", "type": "path", "kind": "file", "default": "/tmp/x"}]))
    assert [s.type for s in mm.user_editable] == ["text", "number", "yes/no", "choice", "path"]


# ---------------------------------------------------------------- error-states
def test_requires_mode_other_than_check_or_start_refused():
    assert refused(m(requires=[{"id": "fl-image", "mode": "supervise"}])).field == "requires[0].mode"


def test_backup_name_not_declared_refused():
    assert refused(m(backup_before_first_start=["store"])).field == "backup_before_first_start[0]"


def test_requires_files_name_not_declared_refused():
    assert refused(m(requires_files=["gt_vocab"])).field == "requires_files[0]"


def test_ui_path_without_port_refused_and_headless_ok():
    assert refused(m(ui_path="/ui")).field == "ui_path"
    assert parse_manifest(m()).headless is True


def test_env_reserved_prefix_refused_and_ui_token_never_declarable():
    assert refused(m(env={"GNOMON_APP_UI_TOKEN": "x"})).field == "env.GNOMON_APP_UI_TOKEN"


def test_stop_defaults_parsed():
    mm = parse_manifest(m())
    assert (mm.stop_signal, mm.stop_grace_s) == ("SIGTERM", 10.0)
    mm = parse_manifest(m(stop={"signal": "SIGINT", "grace": 2}))
    assert (mm.stop_signal, mm.stop_grace_s) == ("SIGINT", 2.0)
    assert refused(m(stop={"signal": "SIGKILL"})).field == "stop.signal"


def test_scope_default_and_grammar():
    assert parse_manifest(m()).scope == "all"
    assert parse_manifest(m(scope="alpha")).scope == "alpha"
    assert refused(m(scope="deployment Workspace")).field == "scope"


def test_venv_path_param_must_be_read_only_dir():
    assert refused(m(paths=[_path("venv", kind="dir", access="writes")])).field == "paths[0]"


# ---------------------------------------------------------------- ac-neutrality
def _manifests_in(tree):
    found = []
    for d, dirs, files in os.walk(tree):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules")]
        for f in files:
            if not f.endswith(".json"):
                continue
            p = os.path.join(d, f)
            try:
                data = json.load(open(p, encoding="utf-8"))
            except Exception:
                continue
            if isinstance(data, dict) and "schema_version" in data and "start" in data and "id" in data:
                found.append(p)
    return found


def test_ac_neutrality_no_app_manifest_ships_inside_the_package(tmp_path):
    """requirement-apps-outside-repo: the package's own tree registers no app. The Apps host has NO
    built-in registry; apps come only from <DATA>/apps.json. Negative control: a planted manifest is found."""
    assert _manifests_in(os.path.join(ROOT, "gnomon")) == []
    assert _manifests_in(os.path.join(ROOT, "web")) == []
    planted = tmp_path / "pkg"
    planted.mkdir()
    (planted / "x.json").write_text(json.dumps(BASE))
    assert _manifests_in(str(planted)) == [str(planted / "x.json")]


def test_ac_neutrality_a_manifest_inside_gnomon_source_is_refused(tmp_path):
    env = Env(tmp_path)
    try:
        inside = os.path.join(ROOT, "gnomon", "appshost", "__init__.py")   # any path under the source root
        from gnomon.appshost.host import AppsError
        with pytest.raises(AppsError, match="inside Gnomon's source tree"):
            env.host.propose_install(inside, Actor("agent"))
    finally:
        env.cleanup()

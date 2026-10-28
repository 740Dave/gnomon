"""round 2: the Diamond FAIL on 2e9264a (audits/audit-apps-tab-2026-10-02.md). One test per reproduced
bypass (A5, A5b, A5c, A6, A7, A8, A8b, C-HC, C-HC2) and per P1/P2 fix (F4, F5, F6, F8, F9, F11, F15, F16, P3).
Every test here FAILS on 2e9264a (measured; see the round-2 addendum). Hermetic: tmp_path, the launchd double."""

import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import time

import pytest

from appshost_support import PYTHON, Env, ROOT
from gnomon.appshost.host import Actor, AppsError
from gnomon.appshost.launchd import label_for
from gnomon.feature_events import VIEWER_ACTOR

operator = Actor(VIEWER_ACTOR, False)
GIT = shutil.which("git") or "/usr/bin/git"


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    e.cleanup()


def _git(d, *args):
    r = subprocess.run([GIT, "-C", d, *args], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "HOME": d, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@x",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@x"})
    assert r.returncode == 0, r.stderr
    return r.stdout


def _commit_all(d, msg="v1"):
    _git(d, "add", "-A")
    _git(d, "commit", "-q", "-m", msg)


def _marker_app(path, marker):
    """A tiny app that writes ``marker`` when it runs (the 'tampered code ran' witness)."""
    with open(path, "w") as fh:
        fh.write(f"import time\nopen({marker!r}, 'w').write('ran')\nwhile True: time.sleep(0.2)\n")


def _refused(env, a, reason="code-changed"):
    r = env.host.start(a, operator)
    assert r["ok"] is False and r.get("reason") == reason, r
    return r


# ============================================================ F1: the code pin covers what the start can run
def test_f1_a6_an_absolute_entrypoint_outside_the_app_root_is_refused_at_install(env):
    """Round 3 (g73-amendment-4 rule 2): the script must lie INSIDE the pinned root. The round-2 answer (hash the
    one named file) left its sibling modules unpinned (R2-F2 SIB); the answer now is to refuse, and to let the manifest
    declare an app_root that contains the code."""
    code = os.path.join(env.tmp, "elsewhere")
    os.makedirs(code)
    script = os.path.join(code, "serve.py")
    _marker_app(script, os.path.join(env.tmp, "unused"))
    r = env.host.propose_install(env.manifest("absent", with_port=False, start=[PYTHON, script]), Actor("agent-x"))
    assert r["ok"] is False and "app root" in r["error"] and "app_root" in r["error"], r


def test_f1_a_tilde_entrypoint_is_refused_at_install(env):
    r = env.host.propose_install(env.manifest("tilde", with_port=False, start=[PYTHON, "~/tool.py"]), Actor("agent-x"))
    assert r["ok"] is False and r["field"] == "start[1]", r


@pytest.mark.parametrize("how", ["gitignore", "info-exclude"])
def test_f1_a5_a5b_an_ignored_planted_module_refuses(env, how):
    mp = env.manifest("ign-" + how.replace("-", ""), with_port=False, mode="sleep")
    d = os.path.dirname(mp)
    _git(d, "init", "-q")
    if how == "gitignore":
        open(os.path.join(d, ".gitignore"), "w").write("json.py\n")
    _commit_all(d)
    a = env.install(mp)
    if how == "info-exclude":
        open(os.path.join(d, ".git", "info", "exclude"), "a").write("json.py\n")
    marker = os.path.join(env.tmp, "PWNED")
    open(os.path.join(d, "json.py"), "w").write(f"open({marker!r}, 'w').write('ran')\n")   # shadows stdlib json
    assert _git(d, "status", "--porcelain") == ""                    # git calls the tree clean
    _refused(env, a)
    assert not os.path.exists(marker) and env.evidence(a) == []


def test_f1_a5c_assume_unchanged_edit_refuses(env):
    mp = env.manifest("assumed", with_port=False, mode="sleep")
    d = os.path.dirname(mp)
    _git(d, "init", "-q")
    _commit_all(d)
    a = env.install(mp)
    _git(d, "update-index", "--assume-unchanged", "app.py")
    with open(os.path.join(d, "app.py"), "a") as fh:
        fh.write("\n# tampered\n")
    assert _git(d, "status", "--porcelain") == ""
    _refused(env, a)


@pytest.mark.parametrize("use_git", [True, False])
def test_f1_a8_a8b_symlink_target_edit_refuses(env, use_git):
    """A symlinked script whose TARGET (inside the root) is edited after confirm: the target's content is in the hash.
    (A script symlinked to a target OUTSIDE the root is refused at install: see the next test.)"""
    mp = env.manifest(f"linked-{str(use_git).lower()}", with_port=False, mode="sleep")
    d = os.path.dirname(mp)
    target = os.path.join(d, "real_app.py")
    os.rename(os.path.join(d, "app.py"), target)
    os.symlink(target, os.path.join(d, "app.py"))
    if use_git:
        _git(d, "init", "-q")
        _commit_all(d)
    a = env.install(mp)
    marker = os.path.join(env.tmp, f"PWNED-{use_git}")
    _marker_app(target, marker)                                      # the link's TARGET edited
    _refused(env, a)
    assert not os.path.exists(marker)


def test_f1_a_script_symlinked_to_a_target_outside_the_root_is_refused_at_install(env):
    target_dir = os.path.join(env.tmp, "target-out")
    os.makedirs(target_dir)
    target = os.path.join(target_dir, "real_app.py")
    _marker_app(target, os.path.join(env.tmp, "unused"))
    mp = env.manifest("linkout", with_port=False, mode="sleep")
    d = os.path.dirname(mp)
    os.unlink(os.path.join(d, "app.py"))
    os.symlink(target, os.path.join(d, "app.py"))
    r = env.host.propose_install(mp, Actor("agent-x"))
    assert r["ok"] is False and "app root" in r["error"], r


def test_f1_adjudicator_layout_code_one_level_above_deploy_is_pinned(env):
    root = os.path.join(env.tmp, "cluster-viewer")
    os.makedirs(os.path.join(root, "deploy"))
    run_sh = os.path.join(root, "run.sh")
    open(run_sh, "w").write("#!/bin/sh\ncd \"$(dirname \"$0\")\"\nexec python3 serve.py \"$@\"\n")
    os.chmod(run_sh, 0o755)
    _marker_app(os.path.join(root, "serve.py"), os.path.join(env.tmp, "unused"))
    mp = os.path.join(root, "deploy", "app.json")
    json.dump({"schema_version": 1, "id": "adjlike", "name": "A", "version": "1", "purpose": "p", "owner": "o",
               "start": [PYTHON, "../serve.py"], "app_root": ".."}, open(mp, "w"))   # round 3d: a root file is never argv[0]
    a = env.install(mp)
    pin = json.load(open(env.host.paths.pin(a)))
    assert pin["code"]["root"] == os.path.realpath(root) and {"run.sh", "serve.py"} <= set(pin["code"]["files"])
    with open(os.path.join(root, "serve.py"), "a") as fh:
        fh.write("# edited\n")
    _refused(env, a)


def test_f1_in_tree_pycache_is_pinned_not_excluded_and_no_prefix_is_set(env):
    """Round 3 supersedes the round-2 fresh-PYTHONPYCACHEPREFIX answer: __pycache__ is IN the whole-tree pin, and the
    host sets no bytecode-cache variable at all (PYTHONDONTWRITEBYTECODE and -B only)."""
    mp = env.manifest("pyc", mode="sleep", with_port=False)
    a = env.install(mp)
    env.host.start(a, operator)
    env.wait_status(a, "running")
    ev = env.wait_evidence(a)[-1]
    assert ev["pycache_prefix"] is None and ev["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert not os.path.exists(os.path.join(os.path.dirname(mp), "__pycache__"))      # -B: the run wrote no bytecode


@pytest.mark.parametrize("key", ["PATH", "PYTHONPATH", "DYLD_INSERT_LIBRARIES", "NODE_OPTIONS"])
def test_f1_env_that_redirects_what_runs_is_refused_at_registration(key):
    from gnomon.appshost.manifest import ManifestError, parse_manifest

    with pytest.raises(ManifestError) as ei:
        parse_manifest({"schema_version": 1, "id": "envy", "name": "n", "version": "1", "purpose": "p", "owner": "o",
                        "start": ["/usr/bin/python3", "x.py"], "env": {key: "/tmp"}})
    assert ei.value.field == f"env.{key}"


# ============================================================ F2: one manifest path
def test_f2_a7_repointed_apps_json_entry_is_refused_manifest_moved(env):
    mp = env.manifest("moved", with_port=False, mode="sleep")
    a = env.install(mp)
    other = os.path.join(env.tmp, "evil-copy")
    os.makedirs(other)
    shutil.copy(mp, os.path.join(other, "gnomon-app.json"))           # byte-identical manifest
    marker = os.path.join(env.tmp, "PWNED")
    _marker_app(os.path.join(other, "app.py"), marker)
    reg = json.load(open(env.host.paths.registry))
    reg["apps"][a]["manifest"] = os.path.join(other, "gnomon-app.json")
    json.dump(reg, open(env.host.paths.registry, "w"))
    s = env.host.status(a)
    assert s["status"] == "refused" and s["reason"] == "manifest-moved"
    _refused(env, a, "manifest-moved")
    assert not os.path.exists(marker)
    d = env.host.detail(a)
    assert d["change"]["moved"]["now"] == os.path.realpath(os.path.join(other, "gnomon-app.json"))
    # the wrapper (launchd's path) refuses the same way
    from gnomon.appshost import launch as L, preflight as PF
    execd = []
    assert L.run(a, env.data, env.host._deps(operator, (a,)), exec_fn=lambda *x: execd.append(x), watch=False) == PF.REFUSAL_EXIT
    assert execd == []


# ============================================================ F3: the host never runs an unpinned argv
# Round 3 (g73-amendment-4 rule 1): a manifest health_check no longer exists (refused at install), so the three
# round-2 tests that edited one after confirm are retired; their witnesses live in test_appshost_round3.py
# (test_r1_a .. test_r1_f: nothing a poll does can run app-controlled code).


# ============================================================ F4: the page's routes
@pytest.fixture
def web(tmp_path, monkeypatch):
    sys.path.insert(0, os.path.join(ROOT, "web"))
    from fastapi.testclient import TestClient
    import backend.server as srv
    from gnomon.appshost.verb_tools import FLAG
    from gnomon.config import ConfigStore, GnomonConfig

    e = Env(tmp_path)
    store = ConfigStore(os.path.join(e.tmp, "config.json"))
    store.save(GnomonConfig(flags={FLAG: True}))
    monkeypatch.setattr(srv, "_config_store", store)
    monkeypatch.setattr(srv, "_apps_host_override", e.host)
    yield TestClient(srv.app, base_url="http://127.0.0.1:8770"), e, srv
    e.cleanup()


def test_f4_confirm_refused_without_loopback_host_same_origin_and_csrf(web):
    c, e, srv = web
    assert e.host.propose_install(e.manifest("webc", with_port=False), Actor("agent"))["ok"]
    good = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8770", "X-Gnomon-CSRF": srv._APPS_CSRF}
    bad_cases = [
        {**good, "Host": "evil.example:8770", "Origin": "http://evil.example:8770"},   # DNS rebinding
        {**good, "Origin": "https://evil.example"},                                    # cross-origin page
        {k: v for k, v in good.items() if k != "Origin"},                             # no Origin (curl)
        {**good, "X-Gnomon-CSRF": "wrong"},
        {k: v for k, v in good.items() if k != "X-Gnomon-CSRF"},
        {**good, "Sec-Fetch-Site": "cross-site"},
    ]
    for h in bad_cases:
        r = c.post("/api/apps/webc/confirm", headers=h, content=b"{}")
        assert r.status_code == 403, (h, r.status_code, r.text)
    assert "webc" not in e.host._installed()
    r = c.get("/api/apps", headers={"Host": "evil.example:8770"})          # a rebinding page cannot read the token
    assert r.status_code == 403 and srv._APPS_CSRF not in r.text
    seen = c.get("/api/apps/webc", headers={"Host": "127.0.0.1:8770"}).json()["seen"]      # what the screen shows
    r = c.post("/api/apps/webc/confirm", headers=good, content=json.dumps({"seen": seen}).encode())
    assert r.status_code == 200 and r.json()["installed"] is True
    ent = [x for x in e.audit("webc") if x["verb"] == "confirm-install"][-1]
    assert ent["actor"] == "operator-page" and ent["actor_verified"] is False


def test_f11_the_web_confirm_must_echo_what_the_screen_showed(web):
    """Round-2 F11 (round 3): the page's confirm carries {seen}; a body without it is refused (409) and installs nothing."""
    c, e, srv = web
    assert e.host.propose_install(e.manifest("needseen", with_port=False), Actor("agent"))["ok"]
    h = {"Content-Type": "application/json", "Origin": "http://127.0.0.1:8770", "X-Gnomon-CSRF": srv._APPS_CSRF}
    r = c.post("/api/apps/needseen/confirm", headers=h, json={"values": {}})
    assert r.status_code == 409 and "reload" in r.text and "needseen" not in e.host._installed()
    seen = c.get("/api/apps/needseen").json()["seen"]
    assert c.post("/api/apps/needseen/confirm", headers=h, json={"values": {}, "seen": seen}).status_code == 200


def test_f4_the_page_gets_the_csrf_token_from_a_same_origin_get(web):
    c, e, srv = web
    assert c.get("/api/apps").json()["csrf"] == srv._APPS_CSRF


# ============================================================ F5: a broken Apps host fails only the app verbs
@pytest.mark.parametrize("plant", ["verb_tools.py", "__init__.py"])
def test_f5_a_syntax_error_in_appshost_breaks_only_the_app_verbs(tmp_path, plant):
    pkg = tmp_path / "pkg"
    shutil.copytree(os.path.join(ROOT, "gnomon"), pkg / "gnomon",
                    ignore=shutil.ignore_patterns("__pycache__"))
    with open(pkg / "gnomon" / "appshost" / plant, "a") as fh:
        fh.write("\nthis is not python (\n")
    prog = r"""
import json
from gnomon.hub import mcp, feature_ids
from gnomon.hub.registry import SessionRegistry
svc = mcp.GmMCPService(SessionRegistry(), apps_host=lambda: None, apps_enabled=lambda: True)
out = {}
out["context"] = svc.dispatch("context", {"instance_id": "nope"}, caller="x")
out["list_running_apps"] = svc.dispatch("list_running_apps", {}, caller="x")
out["inject"] = svc.dispatch("inject", {}, caller="x")
out["app_list"] = svc.dispatch("app_list", {}, caller="x")
out["tools"] = [t["name"] for t in svc.tools()]
out["fid"] = feature_ids.hub_tool_feature_id("context")
import gnomon
out["pkg"] = gnomon.__file__
print(json.dumps(out, default=str))
"""
    r = subprocess.run([sys.executable, "-c", prog], capture_output=True, text=True, timeout=60, cwd=str(tmp_path),
                       env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "PYTHONPATH": str(pkg),
                            "PYTHONDONTWRITEBYTECODE": "1"})
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["pkg"].startswith(str(pkg))                                       # the BROKEN copy was imported
    assert "no registered session" in out["context"]["error"]                  # a non-app verb works
    assert "apps" in out["list_running_apps"] and "apps_host_error" in out["list_running_apps"]
    assert out["inject"]["ok"] is False and "instance_id" in out["inject"]["error"]
    assert out["app_list"]["ok"] is False and "Apps host is unavailable" in out["app_list"]["error"]
    assert "context" in out["tools"] and not [t for t in out["tools"] if t.startswith("app_")]
    assert out["fid"] == "context"


# ============================================================ F6: the wrapper's post-start listener check
def test_f6_watcher_stops_a_late_non_loopback_listener_and_records_the_refusal(tmp_path):
    from gnomon.appshost import launch as L
    from gnomon.appshost.ports import Listener
    from gnomon.appshost.state import HostPaths

    paths = HostPaths(str(tmp_path))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    calls = {"n": 0}

    class Probe:
        def listeners_of(self, pids):
            calls["n"] += 1
            return [Listener(child.pid, "python", "*", 9999)] if calls["n"] >= 3 else []   # binds LATE

    try:
        res = L.watch_listeners(paths, "late", child.pid, probe=Probe(), window_s=10, interval_s=0.05,
                                alive=lambda p: child.poll() is None)
        assert res == "refused"
        child.wait(10)
        rec = json.load(open(paths.refusal_record("late")))
        assert rec["reason"] == "non-loopback" and rec["by"] == "wrapper" and "*:9999" in rec["detail"]
    finally:
        if child.poll() is None:
            child.kill()


def _firewall_on() -> bool:
    fw = "/usr/libexec/ApplicationFirewall/socketfilterfw"
    if not os.path.exists(fw):
        return False
    r = subprocess.run([fw, "--getglobalstate"], capture_output=True, text=True)
    return "enabled" in r.stdout.lower()


@pytest.mark.skipif(sys.platform != "darwin" or _firewall_on(),
                    reason="a real 0.0.0.0 bind would raise the macOS firewall prompt")
def test_f6_end_to_end_a_real_late_wildcard_bind_is_stopped_by_the_wrapper_with_no_status_poll(env):
    mpl = env.manifest("latebind", with_port=False, start=[PYTHON, "late.py"])
    late = os.path.join(os.path.dirname(mpl), "late.py")
    open(late, "w").write("import socket, time\ntime.sleep(1.5)\ns=socket.socket()\n"
                          "s.bind(('0.0.0.0', 0)); s.listen(1)\nwhile True: time.sleep(0.2)\n")
    a = env.install(mpl)
    env.host.start_settle_s = 0.2
    env.host.start(a, operator)
    lab = label_for(a)
    deadline = time.time() + 20
    while time.time() < deadline and env.launchd.state(lab).pid:
        time.sleep(0.1)                                               # NO host.status() call in this loop
    assert not env.launchd.state(lab).pid, "the wrapper's watcher did not stop the app"
    rec = json.load(open(env.host.paths.refusal_record(a)))
    assert rec["by"] == "wrapper" and rec["reason"] == "non-loopback"
    assert env.host.status(a)["label"].startswith("refused(non-loopback)")


# ============================================================ P2s
def test_f8_a_wildcard_held_port_is_not_free():
    from gnomon.appshost.ports import port_is_free

    for fam, host in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
        s = socket.socket(fam)
        if fam == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((host, 0))
        s.listen(1)
        try:
            assert port_is_free(s.getsockname()[1]) is False, host
        finally:
            s.close()


def test_f9_the_real_vendor_licence_line_shape_parses(tmp_path):
    """The shape of a vendor licence file's expiry line: JSON-style,
    quoted key and value. The fixture reproduces the SHAPE, not the file."""
    from gnomon.appshost import licence as Lc

    p = tmp_path / "vendor.lic"
    p.write_text('{\n  "a": "SENTINEL",\n    "expiry": "2027-01-10",\n  "b": 1\n}\n')
    assert str(Lc.read_licence(str(p)).expiry) == "2027-01-10"


def test_f11_text_value_starting_with_dash_refused_and_substitution_is_single_pass(env):
    a = env.install(env.manifest("dash", with_port=False, mode="sleep",
                                 user_editable=[{"name": "who", "type": "text", "default": "x"},
                                                {"name": "other", "type": "text", "default": "OTHER"}],
                                 extra_start=["--who={setting:who}"]))
    with pytest.raises(AppsError, match="start with '-'"):
        env.host.save_settings(a, {"user": {"who": "--import-v1=/etc/passwd"}}, operator)
    from gnomon.appshost import preflight as PF
    from gnomon.appshost.manifest import parse_manifest
    m = parse_manifest({"schema_version": 1, "id": "sp", "name": "n", "version": "1", "purpose": "p", "owner": "o",
                        "start": ["/usr/bin/python3", "x.py", "{setting:a}{setting:b}"],
                        "user_editable": [{"name": "a", "type": "text", "default": "{setting:b}"},
                                          {"name": "b", "type": "text", "default": "B"}]})
    assert PF.substitute(m.start, m, {}, None)[2:] == ["{setting:b}B"]


def test_f15_snapshot_is_private_and_skips_secret_named_files(env):
    mp = env.manifest("snap", with_port=False)
    d = os.path.dirname(mp)
    open(os.path.join(d, ".env"), "w").write("API_SECRET=hunter2\n")
    open(os.path.join(d, "notes.txt"), "w").write("ok\n")
    a = env.install(mp)
    snap = env.host.paths.pin_snapshot(a)
    assert stat.S_IMODE(os.stat(snap).st_mode) == 0o700
    copied = [os.path.join(r, f) for r, _d, fs in os.walk(snap) for f in fs]
    assert copied and all(stat.S_IMODE(os.stat(p).st_mode) == 0o600 for p in copied)
    assert not [p for p in copied if p.endswith(".env")]
    assert not any("hunter2" in open(p, errors="replace").read() for p in copied)


def test_f16_log_tail_never_carries_the_ui_token(env):
    import secrets

    tok = secrets.token_urlsafe(32)                      # the shape the host mints (43 characters)
    a = env.install(env.manifest("leaky-log", mode="exit1", with_port=False))
    os.makedirs(env.host.paths.runtime(a), exist_ok=True)
    open(env.host.paths.ui_token(a), "w").write(tok)
    os.makedirs(env.host.paths.logs(a), exist_ok=True)
    open(env.host.paths.app_log(a), "a").write(f"open http://127.0.0.1:1/ui?gnomon_ui_token={tok} now\n"
                                               f"token is {tok}\n")
    lines = env.host._redact(a, open(env.host.paths.app_log(a)).read().splitlines())
    assert not any(tok in l for l in lines) and "gnomon_ui_token=<redacted>" in lines[0]
    env.host.start(a, operator)
    s = env.wait_status(a, "failed")
    assert tok not in json.dumps(s)


def test_p3_apps_json_mode_is_preserved(env):
    json.dump({"apps": {}}, open(env.host.paths.registry, "w"))
    os.chmod(env.host.paths.registry, 0o600)
    env.install(env.manifest("modey", with_port=False))
    assert stat.S_IMODE(os.stat(env.host.paths.registry).st_mode) == 0o600


def test_p3_a_loaded_agent_is_reloaded_so_a_fresh_plist_is_read(env):
    a = env.install(env.manifest("reload", mode="exit0", with_port=False))
    env.host.start(a, operator)
    env.wait_status(a, ("stopped", "failed"))
    lab = label_for(a)
    assert lab in env.launchd.loaded                                # exited on its own: still loaded
    n = len(env.launchd.calls)
    env.host.start(a, operator)
    seq = [c[0] for c in env.launchd.calls[n:] if c[1] == lab]
    assert seq[:4] == ["write_plist", "bootout", "bootstrap", "kickstart"]

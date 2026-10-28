"""Adoption-hardening — resume-mode Diamond findings (M1-M4, L1) + Group B.

Fix-pass 2026-09-28 against the LIVE ``~/gnomon`` tree. Each guard below is
authored to FAIL on the pre-change code for the reason it names (verified against a
temp copy of the ``.bak-adopt-*`` files), and to pass on the delivered code.

GROUP A (code-verifiable):
  M1  a failed --dry-run reports ok=False (was: ok stayed True, rc 0 on error)
  M2  --headless + a resume id is REFUSED loud (was: silently ran `claude -p` fresh)
  M3  --prompt + a resume id is REFUSED loud (was: prompt merged into a discarded bootstrap)
  M4  the load-bearing resume wiring is guarded (5 sabotage seams)
  L1  a resume id that is dash-leading / not UUID-shaped is REFUSED loud

GROUP B (settings-merge; the LIVE modal-suppression claim is LIVE-ONLY):
  the built harness.settings.json carries the operator's ``permissions`` block.
"""

import json

import pytest

from gnomon.config import GnomonConfig
from gnomon.harness import agent_launch as al
from gnomon.harness.context_readers import claude_project_dir_name
from gnomon.harness.launcher import (
    build_agent_start_payload,
    build_child_argv,
    default_context_reader,
    prepare_child_launch,
    resolve_child_cwd,
    resolve_harness_permissions,
    validate_resume_session_id,
    write_harness_settings,
)
from gnomon.profile import Profile, ProfileStore

# A real, UUID-shaped claude session id (the shape claude actually writes).
UUID = "00000000-0000-4000-8000-000000000001"


def _cfg(tmp_path):
    """A config + a plain (fresh) 'target' profile — mirrors the WS-G test helper."""
    cfg = GnomonConfig(runtime_root=str(tmp_path / "zs"))
    ProfileStore(cfg.intelligences_root()).create(
        Profile(instance_id="target", bootstrap_prompt="BOOT", terminal="iterm2")
    )
    return cfg


# ============================================================================
# M1 — a failed dry-run must report ok=False (and main() rc!=0)
# ============================================================================


def test_m1_dry_run_resume_without_cwd_reports_not_ok(tmp_path):
    """--resume-session with no --resume-cwd fails resolve_child_cwd in the dry-run;
    _fail must now force ok=False. PRE-CHANGE: dry-run set ok=True first and _fail
    never cleared it, so the CLI exited 0 on a real error."""
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", config=cfg, launcher=al.OperatorLauncher(),
                          hub_port="none", dry_run=True, resume_session_id=UUID)  # no resume_cwd
    assert rep.ok is False
    assert rep.error and "resume_cwd" in rep.error


def test_m1_main_returns_nonzero_on_failed_dry_run(tmp_path, monkeypatch):
    """The CLI must exit non-zero when the dry-run failed (rc = 0 if ok else 1)."""
    cfg = _cfg(tmp_path)
    # Drive launch_child through main() by pointing ConfigStore.load at our cfg.
    from gnomon import config as cfgmod
    monkeypatch.setattr(cfgmod.ConfigStore, "load", lambda self: cfg)
    rc = al.main(["launch", "--profile", "target", "--resume-session", UUID, "--dry-run"])
    assert rc == 1


# ============================================================================
# M2 — --headless together with a resume id is REFUSED loud
# ============================================================================


def test_m2_headless_plus_resume_is_refused(tmp_path):
    """PRE-CHANGE: headless.py has no resume handling, so this ran `claude -p`
    fresh while dry-run falsely reported resumed:true."""
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", headless=True, config=cfg, launcher=al.OperatorLauncher(),
                          hub_port="none", dry_run=True, resume_session_id=UUID, resume_cwd="/orig")
    assert rep.ok is False
    assert "headless" in rep.error.lower()


# ============================================================================
# M3 — a task --prompt together with a resume id is REFUSED loud
# ============================================================================


def test_m3_prompt_plus_resume_is_refused(tmp_path):
    """PRE-CHANGE: --prompt was merged into the cold-start bootstrap, which the
    resume argv discards — a silent drop. REFUSE (v1); --resume-prompt is the route."""
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", "please do a task", config=cfg, launcher=al.OperatorLauncher(),
                          hub_port="none", dry_run=True, resume_session_id=UUID, resume_cwd="/orig")
    assert rep.ok is False
    assert "prompt" in rep.error.lower()


def test_m3_resume_prompt_is_still_allowed(tmp_path):
    """The sanctioned re-orient channel (--resume-prompt) is NOT refused."""
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", config=cfg, launcher=al.OperatorLauncher(), hub_port="none",
                          dry_run=True, resume_session_id=UUID, resume_cwd="/orig",
                          resume_prompt="welcome back")
    assert rep.ok is True
    assert rep.resumed is True
    dd = rep.child_argv.index("--")
    assert rep.child_argv[dd + 1:] == ["welcome back"]


# ============================================================================
# L1 — resume id validation (dash-leading / non-UUID refused)
# ============================================================================


def test_l1_validate_resume_session_id_accepts_uuid_and_rejects_bad():
    assert validate_resume_session_id(UUID) == UUID
    for bad in ("--mcp-config", "-x", "not-a-uuid", "00000000-abcd", "", 123, None):
        with pytest.raises(ValueError):
            validate_resume_session_id(bad)


def test_l1_build_child_argv_rejects_dash_leading_resume_id(tmp_path):
    """The argv chokepoint refuses a dash-leading id (defense in depth) — it would
    otherwise splice a flag into argv and push --mcp-config off the tail."""
    with pytest.raises(ValueError):
        build_child_argv({"claude_bin": "c", "resume_session_id": "--mcp-config"},
                         str(tmp_path), settings_path=None, mcp_config_path=None)


def test_l1_launch_child_refuses_dash_leading_resume_id(tmp_path):
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", config=cfg, launcher=al.OperatorLauncher(), hub_port="none",
                          dry_run=True, resume_session_id="--mcp-config", resume_cwd="/orig")
    assert rep.ok is False and "resume_session_id" in rep.error


def test_l1_launch_child_refuses_non_uuid_resume_id(tmp_path):
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", config=cfg, launcher=al.OperatorLauncher(), hub_port="none",
                          dry_run=True, resume_session_id="not-a-uuid", resume_cwd="/orig")
    assert rep.ok is False and "UUID" in rep.error


# ============================================================================
# M4 — guard the 5 load-bearing resume wirings (seam tests)
# ============================================================================


def test_m4_1_child_cwd_resolves_to_resume_cwd_not_session_dir(tmp_path):
    """Sabotage (1): main() starting the child in session_dir instead of child_cwd.
    Seam: resolve_child_cwd must return resume_cwd (≠ session_dir) in resume mode
    and the harness session_dir in fresh mode."""
    session_dir = str(tmp_path / "harness_session")
    assert resolve_child_cwd({"resume_session_id": UUID, "resume_cwd": "/orig/proj"}, session_dir) \
        == "/orig/proj"
    assert resolve_child_cwd({"resume_session_id": UUID, "resume_cwd": "/orig/proj"}, session_dir) \
        != session_dir
    assert resolve_child_cwd({}, session_dir) == session_dir  # fresh unchanged


def test_m4_1_dry_run_reports_child_cwd_as_resume_cwd(tmp_path):
    """End-to-end at the operator's verification surface: launch_child dry-run
    reports child_cwd == resume_cwd (this is what an operator inspects pre-launch)."""
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", config=cfg, launcher=al.OperatorLauncher(), hub_port="none",
                          dry_run=True, resume_session_id=UUID, resume_cwd="/orig/proj")
    assert rep.ok is True and rep.child_cwd == "/orig/proj"


def test_m4_2_context_reader_honours_transcript_cwd(tmp_path):
    """Sabotage (2): the transcript reader ignoring transcript_cwd. Seam: the
    fallback estimate must read the transcript under the RESUME cwd's mangled dir,
    not the harness session_dir's (which has none)."""
    projects = tmp_path / "projects"
    resume_cwd = tmp_path / "orig"
    resume_cwd.mkdir()
    d = projects / claude_project_dir_name(str(resume_cwd))
    d.mkdir(parents=True)
    (d / "s.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"usage": {"input_tokens": 50000}}}) + "\n"
    )
    session_dir = str(tmp_path / "harness_session")
    reader = default_context_reader(session_dir, projects_root=str(projects),
                                    transcript_cwd=str(resume_cwd))
    assert reader() is not None  # read the resume_cwd transcript
    # Pointed at the harness session_dir (fresh default) there is no transcript.
    assert default_context_reader(session_dir, projects_root=str(projects))() is None


def test_m4_3_agent_start_payload_reads_cfg_resume(tmp_path):
    """Sabotage (3): main() building the agent-start payload from {} not the config.
    Seam: build_agent_start_payload must carry resumed:true / resumed_session_id from
    a resume cfg, and resumed:false from an empty cfg — so passing {} would lose it."""
    h = {"agent_depth": 0, "agent_launch_enabled": False, "launched_by": None,
         "stall": {"enabled": True, "agent_idle_seconds": 1, "operator_idle_seconds": 2},
         "hook_manifest": {}}
    from_cfg = build_agent_start_payload({"resume_session_id": UUID}, h,
                                         instance_id="t", session_dir="/s", pid=1, hub_registered=False)
    from_empty = build_agent_start_payload({}, h,
                                           instance_id="t", session_dir="/s", pid=1, hub_registered=False)
    assert from_cfg["resumed"] is True and from_cfg["resumed_session_id"] == UUID
    assert from_empty["resumed"] is False and from_empty["resumed_session_id"] is None


def test_m4_4_cli_forwards_the_three_resume_args(tmp_path, monkeypatch):
    """Sabotage (4): the CLI not forwarding the three --resume-* args. Seam: main()
    must pass resume_session_id / resume_cwd / resume_prompt into launch_child."""
    captured = {}

    def fake_launch_child(profile, prompt="", **kw):
        captured["profile"] = profile
        captured["prompt"] = prompt
        captured.update(kw)
        return al.LaunchReport(True, "harnessed", {"kind": "operator"}, profile)

    monkeypatch.setattr(al, "launch_child", fake_launch_child)
    rc = al.main(["launch", "--profile", "target", "--resume-session", UUID,
                  "--resume-cwd", "/orig", "--resume-prompt", "hi", "--dry-run"])
    assert rc == 0
    assert captured["resume_session_id"] == UUID
    assert captured["resume_cwd"] == "/orig"
    assert captured["resume_prompt"] == "hi"


def test_m4_5_launch_child_applies_resume_override_to_argv(tmp_path):
    """Sabotage (5): launch_child ignoring the resume override. Seam: the CLI/caller
    resume args must reach build_child_argv — a plain profile becomes a resume launch
    (--resume + id present, cold-start bootstrap omitted)."""
    cfg = _cfg(tmp_path)
    rep = al.launch_child("target", config=cfg, launcher=al.OperatorLauncher(), hub_port="none",
                          dry_run=True, resume_session_id=UUID, resume_cwd="/orig")
    assert rep.resumed is True
    assert "--resume" in rep.child_argv and UUID in rep.child_argv
    assert "BOOT" not in rep.child_argv  # the cold-start bootstrap was dropped


# ============================================================================
# GROUP B — the built harness.settings.json carries the permissions block
# ============================================================================


def test_groupb_resolve_permissions_reads_user_settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"permissions": {"defaultMode": "dontAsk", "allow": ["Bash(ls:*)"]}}))
    perms = resolve_harness_permissions(str(p))
    assert perms["defaultMode"] == "dontAsk"
    assert perms["allow"] == ["Bash(ls:*)"]


def test_groupb_resolve_permissions_falls_back_when_unreadable(tmp_path):
    assert resolve_harness_permissions(str(tmp_path / "nope.json")) == {"defaultMode": "dontAsk"}


def test_groupb_resolve_permissions_falls_back_when_no_permissions_key(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus"}))
    assert resolve_harness_permissions(str(p)) == {"defaultMode": "dontAsk"}


def test_groupb_built_harness_settings_carries_permissions_block(tmp_path):
    """PRE-CHANGE: prepare_child_launch wrote harness.settings.json with NO
    permissions key, so a --terminal none session blocked on the auto-mode modal."""
    sess = tmp_path / "s"
    (sess / ".claude").mkdir(parents=True)
    argv, listener = prepare_child_launch({"claude_bin": "claude", "bootstrap_prompt": "p"}, str(sess))
    try:
        body = json.loads((sess / ".claude" / "harness.settings.json").read_text())
        assert "permissions" in body
        assert "defaultMode" in body["permissions"]
        # additive: statusLine + hooks still present
        assert body["statusLine"]["type"] == "command"
        assert "hooks" in body
    finally:
        listener.close()


def test_groupb_write_harness_settings_permissions_is_additive(tmp_path):
    path = write_harness_settings(str(tmp_path), "/py", socket_path="/x.sock",
                                  permissions={"defaultMode": "dontAsk"})
    body = json.loads(open(path).read())
    assert body["permissions"] == {"defaultMode": "dontAsk"}
    assert body["statusLine"]["type"] == "command"
    assert "hooks" in body


def test_groupb_write_harness_settings_omits_permissions_by_default(tmp_path):
    """The low-level writer stays byte-identical when no permissions arg is given
    (the fresh unit path is unchanged; only the launch path opts in)."""
    body = json.loads(open(write_harness_settings(str(tmp_path), "/py", compact_hooks=False)).read())
    assert set(body) == {"statusLine"}

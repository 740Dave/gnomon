"""(2026-10-08): the terminal label read
``ctx --%`` for alpha-architect and beta-architect only.

Measured cause: those two sessions RESUME a conversation whose cwd is a
``<other root>/sessions/...`` folder, so the child's cwd is NOT the harness session
dir. The status line hook is told the authoritative state file (``--state
<session_dir>/.claude/context_state.json``) and writes it correctly, but the
label shown (the operator's chained ``statusLine`` stopgap) re-derived the
session dir from the payload's ``cwd``/``workspace.current_dir`` and so read a
file that does not exist.

Invariant (``statusline-keyed-by-session-dir``): what
the status line shows is keyed by the harness SESSION DIR the hook was given,
never by the child's cwd. Every case is run twice, once with cwd == session dir
(fresh / same-dir resume) and once with cwd elsewhere (cross-folder resume); the
two outputs must be identical.
"""

from __future__ import annotations

import io
import json
import os

import pytest

from gnomon.harness import statusline_hook

SESSION = "alpha-architect-20260929-055502"
# A DIFFERENT agent prefix from SESSION, so a label wrongly derived from the cwd cannot coincide.
RESUMED = "beta-architect-20260923-014221"


def _payload(cwd: str, ctx_pct: float) -> str:
    # Realistic shape: rate_limits ALSO carries used_percentage (the 2026-10-04
    # stopgap's first bug picked the weekly 4% instead of the context value).
    return json.dumps({
        "session_id": "00000000-0000-4000-8000-000000000002",
        "transcript_path": f"/x/projects/{RESUMED}/00000000.jsonl",
        "cwd": cwd,
        "workspace": {"current_dir": cwd, "project_dir": cwd},
        "context_window": {"used_percentage": ctx_pct, "context_window_size": 1000000},
        "rate_limits": {"five_hour": {"used_percentage": 12.0, "resets_at": 1},
                        "seven_day": {"used_percentage": 4.0, "resets_at": 2}},
    })


def _layout(tmp_path, cross_folder: bool):
    session_dir = tmp_path / "runtimeroot" / "sessions" / SESSION
    (session_dir / ".claude").mkdir(parents=True)
    if cross_folder:
        cwd = tmp_path / "resumedroot" / "sessions" / RESUMED
        (cwd / ".claude").mkdir(parents=True)  # exists, but holds no context_state.json
    else:
        cwd = session_dir
    return str(session_dir), str(session_dir / ".claude" / "context_state.json"), str(cwd)


def _run(argv, raw: str) -> str:
    out = io.StringIO()
    assert statusline_hook.main(argv, stdin=io.StringIO(raw), stdout=out) == 0
    return out.getvalue()


def _user_settings(tmp_path, command: str) -> str:
    p = tmp_path / "user-settings.json"
    p.write_text(json.dumps({"statusLine": {"type": "command", "command": command}}))
    return str(p)


@pytest.mark.parametrize("cross_folder", [False, True], ids=["same-dir", "cross-folder-resume"])
def test_own_label_names_the_agent_and_shows_context_not_plan(tmp_path, cross_folder):
    """No user statusLine chained: the hook's own line is '<agent>  ctx NN%'."""
    _sd, state, cwd = _layout(tmp_path, cross_folder)
    nouser = str(tmp_path / "absent-settings.json")
    shown = _run(["--state", state, "--chain-user-statusline", "--user-settings", nouser],
                 _payload(cwd, 53.0))
    assert shown == "alpha-architect  ctx 53%\n"


@pytest.mark.parametrize("cross_folder", [False, True], ids=["same-dir", "cross-folder-resume"])
def test_chained_command_is_told_the_authoritative_state_file(tmp_path, cross_folder):
    """A chained user statusLine gets the session dir and the state file the hook
    just wrote, so it never has to re-derive them from the payload's cwd."""
    session_dir, state, cwd = _layout(tmp_path, cross_folder)
    cmd = ('bash -c \'cat >/dev/null; '
           'printf "%s|%s|%s|%s" "$GNOMON_SESSION_DIR" "$GNOMON_CONTEXT_STATE" "$GNOMON_INSTANCE_ID" "$GNOMON_CONTEXT_PCT"\'')
    shown = _run(["--state", state, "--chain-user-statusline",
                  "--user-settings", _user_settings(tmp_path, cmd)], _payload(cwd, 53.0))
    assert shown == f"{session_dir}|{state}|alpha-architect|53\n"
    with open(state) as fh:  # the hook wrote it before running the chained command
        assert json.load(fh)["used_percentage"] == 53.0


@pytest.mark.parametrize("cross_folder", [False, True], ids=["same-dir", "cross-folder-resume"])
def test_unknown_context_is_shown_as_unknown_never_zero(tmp_path, cross_folder):
    _sd, state, cwd = _layout(tmp_path, cross_folder)
    raw = json.dumps({"cwd": cwd, "context_window": {"used_percentage": 0}})
    shown = _run(["--state", state, "--user-settings", str(tmp_path / "none.json")], raw)
    assert shown == "alpha-architect  ctx --\n"
    assert not os.path.exists(state)


# ---- Diamond fix round 1 (DIAMOND-statusline-label-2026-10-08) ----------------------------------

def _write_canary(session_dir: str, instance_id) -> None:
    with open(os.path.join(session_dir, ".claude", "canary.json"), "w", encoding="utf-8") as fh:
        json.dump({"gnomon_canary": True, "instance_id": instance_id}, fh)


@pytest.mark.parametrize("bad", ["ra\u0000x", "ra\ud800x"], ids=["NUL", "lone-surrogate"])
def test_p2_1_hostile_instance_id_with_chained_command_never_breaks_the_hook(tmp_path, bad):
    """P2-1: an instance_id with NUL / a lone surrogate used to make subprocess raise
    ValueError out of main (exit 1). Contract: every failure swallowed, exit 0, a label shown."""
    session_dir, state, cwd = _layout(tmp_path, True)
    _write_canary(session_dir, bad)
    cmd = 'bash -c \'cat >/dev/null; printf "%s" "$GNOMON_INSTANCE_ID"\''
    shown = _run(["--state", state, "--chain-user-statusline",
                  "--user-settings", _user_settings(tmp_path, cmd)], _payload(cwd, 53.0))
    assert shown.endswith("\n") and "\x00" not in shown
    assert not any(0xD800 <= ord(c) <= 0xDFFF for c in shown)
    assert shown.strip() == "rax"  # the hostile characters are dropped, the rest kept


def test_p2_1_run_chained_swallows_pre_exec_errors():
    """The second end: even if a bad value reaches _run_chained, it returns None, never raises."""
    assert statusline_hook._run_chained("true", "x", {"GNOMON_X": "a\x00b"}) is None
    assert statusline_hook._run_chained("true", "a\ud800b", {}) is None


def test_p2_2_fifo_canary_does_not_hang_the_hook(tmp_path):
    """P2-2: a FIFO at <session>/.claude/canary.json used to block open() forever."""
    import subprocess, sys
    session_dir, state, cwd = _layout(tmp_path, True)
    os.mkfifo(os.path.join(session_dir, ".claude", "canary.json"))
    root = os.path.dirname(os.path.dirname(os.path.abspath(statusline_hook.__file__)))
    proc = subprocess.run(
        [sys.executable, "-m", "gnomon.harness.statusline_hook", "--state", state,
         "--user-settings", str(tmp_path / "none.json")],
        input=_payload(cwd, 53.0), capture_output=True, text=True, timeout=20, cwd=root)
    assert proc.returncode == 0
    assert proc.stdout == "alpha-architect  ctx 53%\n"


def test_p2_2_read_json_reads_only_bounded_regular_files(tmp_path):
    from gnomon import liveness
    ok = tmp_path / "ok.json"
    ok.write_text('{"a": 1}')
    assert liveness._read_json(str(ok)) == {"a": 1}
    big = tmp_path / "big.json"
    big.write_text('{"a": "' + "x" * (liveness._READ_JSON_MAX_BYTES + 10) + '"}')
    assert liveness._read_json(str(big)) is None
    # Pins the st_size guard itself: a file whose first MiB is a COMPLETE valid object followed by
    # whitespace would parse fine if only the bounded read protected us; the guard must refuse it.
    padded = tmp_path / "padded.json"
    padded.write_text('{"a": 1}' + " " * (liveness._READ_JSON_MAX_BYTES + 10))
    assert liveness._read_json(str(padded)) is None
    # a non-UTF-8 regular file is "no object" (was UnicodeDecodeError before round 1)
    bad = tmp_path / "bad.json"
    bad.write_bytes(b'{"a": "\xff\xfe"}')
    assert liveness._read_json(str(bad)) is None
    assert liveness._read_json(str(tmp_path / "missing.json")) is None
    assert liveness._read_json(str(tmp_path)) is None  # a directory
    link = tmp_path / "link.json"
    link.symlink_to(ok)
    assert liveness._read_json(str(link)) == {"a": 1}  # a symlink to a regular file still reads

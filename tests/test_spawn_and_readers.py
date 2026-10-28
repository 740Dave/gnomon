"""Unit tests for build_claude_args, sanitize_env, and the context readers."""

import json
import os

from gnomon.harness import (
    JsonlTranscriptContextReader,
    StatuslineHookContextReader,
    build_claude_args,
    sanitize_env,
)


def test_build_claude_args_puts_prompt_as_positional_after_dashdash():
    argv = build_claude_args("/usr/local/bin/claude", "COLD START: read specs first")
    assert argv[0] == "/usr/local/bin/claude"
    assert "--dangerously-skip-permissions" in argv
    # The prompt is the final positional element, immediately after '--'.
    assert argv[-2] == "--"
    assert argv[-1] == "COLD START: read specs first"


def test_build_claude_args_extra_args_precede_dashdash():
    argv = build_claude_args(
        "claude", "prompt", extra_args=["--model", "opus"], dangerously_skip_permissions=False
    )
    assert "--dangerously-skip-permissions" not in argv
    assert argv[:3] == ["claude", "--model", "opus"]
    assert argv[-2:] == ["--", "prompt"]


def test_sanitize_env_strips_proxy_session_vars_and_guarantees_term():
    base = {
        "PATH": "/usr/bin",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SSE_PORT": "51234",
        "CLAUDE_CODE_TRANSCRIPT_PATH": "/tmp/proxy.jsonl",
    }
    env = sanitize_env(base_env=base)
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_SSE_PORT" not in env
    assert "CLAUDE_CODE_TRANSCRIPT_PATH" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["TERM"]  # guaranteed present


def test_sanitize_env_overrides_win():
    env = sanitize_env(base_env={"TERM": "dumb"}, overrides={"TERM": "xterm-256color"})
    assert env["TERM"] == "xterm-256color"


def test_statusline_reader_bare_number(tmp_path):
    p = tmp_path / "ctx.state"
    p.write_text("88.5")
    assert StatuslineHookContextReader(str(p))() == 88.5


def test_statusline_reader_json_object(tmp_path):
    p = tmp_path / "ctx.json"
    p.write_text(json.dumps({"used_percentage": 63}))
    assert StatuslineHookContextReader(str(p))() == 63.0


def test_statusline_reader_missing_is_none(tmp_path):
    assert StatuslineHookContextReader(str(tmp_path / "nope"))() is None


def test_jsonl_reader_uses_max_usage_block(tmp_path):
    p = tmp_path / "transcript.jsonl"
    lines = [
        {"type": "user", "message": {"role": "user", "content": "hi"}},
        {"message": {"usage": {"input_tokens": 1000, "output_tokens": 200}}},
        {"message": {"usage": {"input_tokens": 40000, "cache_read_input_tokens": 10000, "output_tokens": 500}}},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines))
    reader = JsonlTranscriptContextReader(str(p), window_tokens=100_000)
    # max total = 40000 + 10000 + 500 = 50500 -> 50.5%
    assert abs(reader() - 50.5) < 1e-6


def test_jsonl_reader_missing_is_none(tmp_path):
    assert JsonlTranscriptContextReader(str(tmp_path / "none.jsonl"))() is None


def test_jsonl_reader_char_fallback(tmp_path):
    p = tmp_path / "t.jsonl"
    # No usage blocks -> char/4 estimate against the window.
    p.write_text(json.dumps({"message": {"content": "x" * 400}}))
    reader = JsonlTranscriptContextReader(str(p), window_tokens=1000, chars_per_token=4.0)
    val = reader()
    assert val is not None and val > 0

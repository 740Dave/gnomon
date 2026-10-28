"""Spawn invariant + cold-start argv.

MUST spawn via ``pty.fork()`` + ``os.execvpe`` with a sanitized env; the parent
keeps the master fd. The cold-start bootstrap prompt rides as a POSITIONAL argv
element (after ``--``), never typed through the injection path.

Nothing here imports earlier tooling. ``build_claude_args`` re-implements the argv
shape the spec extracts from earlier tooling ``ptycore.py:67-101`` /
an earlier launcher ``session.py:300-307``.
"""

from __future__ import annotations

import os
import pty
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence

# Environment variables stripped by default when sanitizing the child env.
#
# "Sanitize" here means: start from the parent env, drop the variables below,
# guarantee a sane TERM, then apply overrides. The denylist is exactly the
# variables Claude Code exports to mark ITS OWN running session; the child is a
# fresh Claude Code session under the harness, so inheriting the proxy's copies
# would misidentify the child as continuing the proxy's session — pointing it at
# the proxy's SSE port / transcript path and entrypoint instead of its own.
# Dropping them forces the child to establish its own session state. Each entry:
#   CLAUDECODE                    - "am I running under Claude Code" flag
#   CLAUDE_CODE_SSE_PORT          - the proxy session's MCP/SSE rendezvous port
#   CLAUDE_CODE_ENTRYPOINT        - how the proxy session was launched
#   CLAUDE_CODE_SESSION_ID        - the proxy session's id
#   CLAUDE_CODE_TRANSCRIPT_PATH   - the proxy session's transcript file
#   ANTHROPIC_CLI_SESSION         - the proxy session's CLI session marker
# Transcript STOP-THE-LINE (2026-09-29) — added, all session-state markers the
# running :8770 dashboard was MEASURED carrying (it was started from inside a
# Claude Code tool call, so every child it launches inherits them):
#   CLAUDE_CODE_CHILD_SESSION     - ⛔ "I am a child of another session". An
#                                   INTERACTIVE claude that inherits it turns
#                                   TRANSCRIPT SAVING OFF ("Transcript saving is
#                                   off — inherited CLAUDE_CODE_CHILD_SESSION
#                                   marker"), measured in the TUI log of all 3
#                                   pilot sessions whose project dir held no jsonl.
#   CLAUDE_CODE_MESSAGING_SOCKET  - the proxy session's messaging socket
#   CLAUDE_CODE_MESSAGING_TOKEN   - the proxy session's messaging-socket token
#   CLAUDE_CODE_SESSION_ATTENDED  - the proxy session's attended-mode marker
#   CLAUDE_CODE_EXECPATH          - the proxy session's own claude executable
#   CLAUDE_PID                    - the proxy session's claude pid
# This list is a session-state scrub; the CREDENTIAL scrub (security posture D3,
# 2026-09-29) is separate — see ``is_credential_name`` below. Overrides can
# re-introduce any key deliberately.
_DEFAULT_ENV_DENYLIST: tuple[str, ...] = (
    "CLAUDECODE",
    "CLAUDE_CODE_SSE_PORT",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_TRANSCRIPT_PATH",
    "ANTHROPIC_CLI_SESSION",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_PID",
    # D15 fix pass (security audit): CLAUDE_CODE_SIMPLE strips the child to a bare
    # mode (no hooks, no MCP), which would silently drop the harness's statusline,
    # compact and operator-command hooks.
    "CLAUDE_CODE_SIMPLE",
)


# Security posture D3 (operator ruling 2026-09-29): a CREDENTIAL scrub,
# distinct from the session-state denylist above. By default the child does NOT
# inherit env vars whose NAME (case-insensitive) matches ``*_API_KEY``, ``*_TOKEN``,
# ``*_SECRET``, ``AWS_*`` or ``AZURE_*`` (widened in fix pass 3, below). A profile opts specific vars back in with
# ``env_passthrough: [exact names]`` (no globs). ⛔ EXEMPT, never credential-scrubbed:
# Claude Code's own internals (``CLAUDE_CODE_*`` / ``CLAUDECODE*`` — e.g.
# ``CLAUDE_CODE_OAUTH_TOKEN``; ``CLAUDE_CODE_MESSAGING_TOKEN`` powers cross-session
# SendMessage) and the harness's own ``GNOMON_*``. ``OTEL_*`` is left to telemetry.py.
# D46 fix pass 3 (fixpass audit N-E1, 2026-09-29): the ruling's INTENT, not only its
# letter — also bare ``API_KEY``/``TOKEN``/``SECRET``/``PASSWORD``, ``*_KEY`` (so
# ``SECRET_KEY``, ``*_PRIVATE_KEY``, ``OPENAI_KEY``), ``*_PASSWORD``/``*_PASSWD``,
# ``*_CREDENTIALS``, ``PGPASSWORD`` and ``DATABASE_URL``. A suffix must follow ``_`` or
# start the name, so ``MONKEY``/``HOTKEY``/``*_FILE``/``*_DIR`` are kept.
_CREDENTIAL_NAME_RE = re.compile(
    r"(?:^|_)(?:API_KEY|KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)$"
    r"|^(?:AWS_|AZURE_)|^(?:PGPASSWORD|DATABASE_URL)$", re.IGNORECASE)
CREDENTIAL_EXEMPT_PREFIXES: tuple[str, ...] = ("CLAUDE_CODE_", "CLAUDECODE", "GNOMON_")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


#: Pattern-matching names that are CONFIG, not secrets (audit fix pass 2): kept.
NON_SECRET_EXEMPT_NAMES = frozenset({"AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE", "AWS_CONFIG_FILE",
                                     "AZURE_CONFIG_DIR"})


def is_credential_name(name: str) -> bool:
    """True iff ``name`` is credential-shaped AND not an exempt internal/config name."""
    if name.upper().startswith(CREDENTIAL_EXEMPT_PREFIXES) or name.upper() in NON_SECRET_EXEMPT_NAMES:
        return False
    return bool(_CREDENTIAL_NAME_RE.search(name))


def validate_env_passthrough(value: object) -> List[str]:
    """A profile/launch.json ``env_passthrough``: ``None`` -> ``[]``; else a list of
    EXACT env var names (no globs). Anything else raises ``ValueError`` (fail loud)."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("env_passthrough must be a list of exact env var names")
    for item in value:
        if not isinstance(item, str) or not _ENV_NAME_RE.match(item):
            raise ValueError(f"env_passthrough entries must be exact env var names (no globs): {item!r}")
    return list(value)


@dataclass(frozen=True)
class SpawnResult:
    """The parent's handle on a spawned child."""

    pid: int
    master_fd: int


def sanitize_env(
    base_env: Optional[Dict[str, str]] = None,
    overrides: Optional[Dict[str, str]] = None,
    denylist: Sequence[str] = _DEFAULT_ENV_DENYLIST,
    default_term: str = "xterm-256color",
    *,
    scrub_credentials: bool = True,
    passthrough: Sequence[str] = (),
    scrubbed: Optional[List[str]] = None,
) -> Dict[str, str]:
    """Build a sanitized child environment.

    * starts from ``base_env`` (default: a copy of ``os.environ``),
    * removes every key in ``denylist`` (the proxy's own session-injected vars),
    * D3: unless ``scrub_credentials=False``, removes credential-shaped vars
      (:func:`is_credential_name`) except the EXACT names in ``passthrough``; the
      removed NAMES (never values) are appended, sorted, to ``scrubbed`` if given,
    * guarantees ``TERM`` is set,
    * applies ``overrides`` last (they win, and can re-introduce a denied key
      intentionally).
    """
    env: Dict[str, str] = dict(os.environ if base_env is None else base_env)
    for key in denylist:
        env.pop(key, None)
    if scrub_credentials:
        keep = set(passthrough)
        dropped = sorted(k for k in env if k not in keep and is_credential_name(k))
        for key in dropped:
            del env[key]
        if scrubbed is not None:
            scrubbed.extend(dropped)
    env.setdefault("TERM", default_term)
    if overrides:
        env.update(overrides)
    return env


#: Flags the harness itself passes; a profile may not override them (audit F2).
# NOTE (D15 fix pass 2): the two sets below are no longer ENFORCED directly —
# validate_extra_args is an ALLOWLIST (EXTRA_ARGS_ALLOWLIST), which refuses all of
# them and everything else not listed. They are kept as the documented reasons.
_HARNESS_OWNED_FLAGS = frozenset({"--settings", "--setting-sources"})

#: D15 fix pass (security audit): flags a PROFILE may not pass, because each one
#: changes what the child may do or which instructions/tools/servers it loads —
#: permission mode, tool allow/deny lists, the MCP surface, extra readable dirs,
#: plugins/agents, the system prompt — or strips the harness's hooks (--bare).
#: The harness adds the ones it needs (--dangerously-skip-permissions, --add-dir,
#: --mcp-config) itself in build_child_argv, NOT via extra_args. Names checked
#: against ``claude --help`` 2.1.284 (both camelCase and kebab aliases are listed
#: there for the tool lists). --dangerously-load-development-channels is not in
#: 2.1.284's help; rejected anyway (a hidden/dev flag is no safer).
_ESCALATION_FLAGS = frozenset({
    "--bare",
    "--permission-mode",
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--allowedTools", "--allowed-tools",
    "--disallowedTools", "--disallowed-tools",
    "--mcp-config",
    "--strict-mcp-config",
    "--add-dir",
    "--plugin-dir",
    "--plugin-url",
    "--agents",
    "--append-system-prompt",
    "--system-prompt",
    "--dangerously-load-development-channels",
})


#: D15 fix pass 2 (re-audit LOW): ``extra_args`` is an ALLOWLIST. Flag -> how many
#: values it takes: "none" | "one" (required) | "opt" (an optional value: the next
#: token is consumed iff it does not start with ``-``). Sources: the brief's set
#: (--model, --effort, --name, --verbose, --debug) plus what the tree ACTUALLY
#: relies on — the transcript planner's session-selection flags (transcript.py
#: CONTINUE_RESUME_FLAGS / FORK_SESSION_FLAG / an operator --session-id; exercised
#: by tests/test_transcript_persistence.py + test_resume_mode.py). A scan of the 9
#: real ~/gnomon-data/agents/*/profile.json (none set extra_args) and every
#: historical ~/gnomon-data/sessions/*/.claude/launch.json (only ``--model haiku``)
#: found nothing else. Everything else is refused, with this list in the message.
EXTRA_ARGS_ALLOWLIST: Dict[str, str] = {
    "--model": "one",
    "--effort": "one",
    "--name": "one",
    "--verbose": "none",
    "--debug": "opt",
    "--continue": "none", "-c": "none",
    "--resume": "opt", "-r": "opt",
    "--fork-session": "none",
    "--session-id": "one",
}


def _allowlist_text() -> str:
    return ", ".join(sorted(EXTRA_ARGS_ALLOWLIST))


def validate_extra_args(value: object) -> List[str]:
    """Validate a profile/launch.json ``extra_args`` value.

    ``None`` -> ``[]``. Otherwise a list of non-empty strings in which every
    ``-``-leading token is an ALLOWLISTED flag (``--flag`` or ``--flag=value``)
    and every other token is the value of the flag before it (D15 fix pass 2 —
    previously a denylist). No bare ``--`` (it would displace the cold-start
 prompt) and no stray positional (it would become
    the prompt). The harness's own flags (--dangerously-skip-permissions,
    --settings, --add-dir, --mcp-config) are added by ``build_child_argv``, never
    through here. Fails LOUD (ValueError), naming the allowed flags.
    """
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"extra_args must be a JSON list of strings, got {type(value).__name__}")
    out: List[str] = []
    i = 0
    n = len(value)
    while i < n:
        item = value[i]
        if not isinstance(item, str) or not item:
            raise ValueError(f"extra_args[{i}] must be a non-empty string, got {item!r}")
        if item == "--":
            raise ValueError("extra_args may not contain '--' (it would displace the cold-start prompt)")
        if not item.startswith("-"):
            raise ValueError(f"extra_args[{i}] {item!r} is a stray value with no flag before it "
                             f"(it would become the prompt). Allowed flags: {_allowlist_text()}")
        flag, eq, _val = item.partition("=")
        arity = EXTRA_ARGS_ALLOWLIST.get(flag)
        if arity is None:
            raise ValueError(f"extra_args[{i}] {flag!r} is not allowed: extra_args is an allowlist "
                             f"(permissions, tools, MCP servers, dirs, plugins, prompts and settings "
                             f"belong to the harness). Allowed flags: {_allowlist_text()}")
        if eq and arity == "none":
            raise ValueError(f"extra_args[{i}] {flag!r} takes no value. Allowed flags: {_allowlist_text()}")
        out.append(item)
        i += 1
        if eq:
            continue
        if arity == "one":
            if i >= n or not isinstance(value[i], str) or not value[i] or value[i].startswith("-"):
                raise ValueError(f"extra_args: {flag!r} needs a value. Allowed flags: {_allowlist_text()}")
            out.append(value[i])
            i += 1
        elif arity == "opt":
            if i < n and isinstance(value[i], str) and value[i] and not value[i].startswith("-"):
                out.append(value[i])
                i += 1
    return out


def build_claude_args(
    claude_bin: str,
    bootstrap_prompt: str,
    *,
    dangerously_skip_permissions: bool = True,
    extra_args: Optional[Sequence[str]] = None,
) -> List[str]:
    """Build the child argv with the cold-start prompt as a positional element.

    Shape (an earlier launcher's GUI path; this one drops the earlier tool's
    dev-channel arg and wires its own ``.mcp.json``
    via the session dir instead)::

        <claude_bin> --dangerously-skip-permissions <extra...> -- '<prompt>'

    The prompt sits AFTER ``--`` as argv, so Claude Code receives it as the
    initial user turn at spawn — it is never typed, so the injection path and
 the cold-start prompt cannot be confused.
    """
    argv: List[str] = [claude_bin]
    if dangerously_skip_permissions:
        argv.append("--dangerously-skip-permissions")
    if extra_args:
        argv.extend(extra_args)
    argv.append("--")
    argv.append(bootstrap_prompt)
    return argv


class PtySpawner:
    """Spawns a child under a new PTY and hands the parent the master fd."""

    def spawn(
        self,
        argv: Sequence[str],
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
    ) -> SpawnResult:
        """``pty.fork()`` + ``os.execvpe(argv[0], argv, env)``.

        In the child (which owns the slave tty as its controlling terminal) we
        optionally ``chdir(cwd)`` — modelling the launch command's
        ``cd <workspace> && ...`` — then exec. The parent returns the pid and
        the master fd.
        """
        if not argv:
            raise ValueError("argv must be non-empty")
        child_env = sanitize_env() if env is None else env

        pid, master_fd = pty.fork()
        if pid == 0:
            # --- child ---------------------------------------------------
            # stdin/stdout/stderr are already wired to the slave tty by
            # pty.fork(). Replace ourselves with the target program.
            try:
                if cwd:
                    os.chdir(cwd)
                os.execvpe(argv[0], list(argv), child_env)
            except BaseException as exc:  # pragma: no cover - child path
                # We are post-fork in the child; we must not run normal Python
                # teardown. Report and hard-exit.
                os.write(2, f"gnomon: exec failed: {exc}\n".encode())
                os._exit(127)
        # --- parent ------------------------------------------------------
        return SpawnResult(pid=pid, master_fd=master_fd)

"""Config-driven terminal launch-command templates (D4).

The launcher does NOT embed a terminal emulator — it
asks the human's real terminal (iTerm2 / Terminal.app) to open a window and run
a command. That command ``cd``s into the session dir and execs the harness proxy
wrapping ``claude --dangerously-skip-permissions`` (
launch-command-shape).

Two templates ship (D4 — cross-platform templates are a v2 concern; the two
macOS terminals are v1):

* ``iterm2``       — the default; ``osascript`` against iTerm2.
* ``terminal_app`` — stock macOS Terminal.app.

Each template renders to a single ``osascript`` shell command STRING. The string
is what :class:`~gnomon.launch.LaunchManager` returns; actually invoking
it is a separate, guarded step, so tests assert the string and never open a
terminal.

The rendering is deliberately a small, explicit string build — not a full shell
quoter. The one value that lands inside the AppleScript string literal (the inner
command) is escaped for AppleScript (backslash + double-quote); the inner command
itself is built from our own argv via :func:`shell_join`, so it does not carry
attacker-controlled shell metacharacters.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence


def shell_join(argv: Sequence[str]) -> str:
    """POSIX-quote an argv into a single shell command string."""
    return " ".join(shlex.quote(a) for a in argv)


def _applescript_escape(text: str) -> str:
    """Escape a value for embedding inside an AppleScript double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


@dataclass(frozen=True)
class TerminalTemplate:
    """A named terminal launch template.

    ``render(session_dir, inner_command)`` returns the full ``osascript`` shell
    command that opens a window, ``cd``s into ``session_dir`` and runs
    ``inner_command`` (the harness-proxy-wrapping-claude command).
    """

    key: str
    app_name: str
    _render: Callable[[str, str], str]

    def render(self, session_dir: str, inner_command: str) -> str:
        return self._render(session_dir, inner_command)


def _combined_inner(session_dir: str, inner_command: str) -> str:
    """``cd <session_dir> && <inner_command>`` as one AppleScript-escaped literal."""
    combined = f"cd {shlex.quote(session_dir)} && {inner_command}"
    return _applescript_escape(combined)


def _render_iterm2(session_dir: str, inner_command: str) -> str:
    body = _combined_inner(session_dir, inner_command)
    lines = [
        'tell application "iTerm2"',
        "create window with default profile",
        f'tell current session of current window to write text "{body}"',
        "activate",
        "end tell",
    ]
    return "osascript " + " ".join(f"-e {shlex.quote(line)}" for line in lines)


def _render_terminal_app(session_dir: str, inner_command: str) -> str:
    body = _combined_inner(session_dir, inner_command)
    lines = [
        'tell application "Terminal"',
        f'do script "{body}"',
        "activate",
        "end tell",
    ]
    return "osascript " + " ".join(f"-e {shlex.quote(line)}" for line in lines)


TERMINAL_TEMPLATES: Dict[str, TerminalTemplate] = {
    "iterm2": TerminalTemplate("iterm2", "iTerm2", _render_iterm2),
    "terminal_app": TerminalTemplate("terminal_app", "Terminal", _render_terminal_app),
}


def get_template(key: str) -> TerminalTemplate:
    try:
        return TERMINAL_TEMPLATES[key]
    except KeyError:
        raise KeyError(
            f"unknown terminal template {key!r}; known: {sorted(TERMINAL_TEMPLATES)}"
        ) from None


def render_string_template(template_str: str, session_dir: str, inner_command: str) -> str:
    """Render a config-supplied CUSTOM terminal template (D4, slice 4).

    A custom template is a plain command STRING carried in the machine config
    (``GnomonConfig.custom_terminal_templates``), so Gnomon is
    terminal-agnostic BY CONFIG — a new terminal is a data edit, never a code
    change. The string may reference three named placeholders:

    * ``{session_dir}``    — the session working dir (unquoted);
    * ``{inner_command}``  — the harness-proxy-wrapping-claude command;
    * ``{cd_and_command}`` — ``cd <shell-quoted session_dir> && <inner_command>``,
      the ready-to-run body most templates want.

    Because rendering is ``str.format``, a LITERAL brace in the template must be
    doubled (``{{`` / ``}}``). An unknown ``{placeholder}`` is a clean
    ``ValueError`` at render time, not a launch-time surprise.
    """
    combined = f"cd {shlex.quote(session_dir)} && {inner_command}"
    try:
        return template_str.format(
            session_dir=session_dir,
            inner_command=inner_command,
            cd_and_command=combined,
        )
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"custom terminal template references an unknown placeholder {exc}; "
            f"known: {{session_dir}}, {{inner_command}}, {{cd_and_command}}"
        ) from None


def render_terminal_command(
    key: str,
    session_dir: str,
    inner_command: str,
    *,
    custom_templates: Optional[Dict[str, str]] = None,
) -> str:
    """Render the terminal command for template ``key``.

    ``custom_templates`` (from the machine config) is consulted FIRST, so a
    user-added named template takes precedence — including one that shadows a
    built-in key, since the config is the source of truth (D4). Falls back to the
    two built-in ``osascript`` templates (``iterm2`` / ``terminal_app``).
    """
    if custom_templates and key in custom_templates:
        return render_string_template(custom_templates[key], session_dir, inner_command)
    return get_template(key).render(session_dir, inner_command)


def available_terminal_templates(
    custom_templates: Optional[Dict[str, str]] = None,
) -> List[str]:
    """Every launch-template key a launch may name: built-ins + custom, sorted."""
    keys = set(TERMINAL_TEMPLATES)
    if custom_templates:
        keys.update(custom_templates)
    return sorted(keys)

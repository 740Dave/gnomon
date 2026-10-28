"""``read_context`` sources (D6).

``read_context`` is OURS to supply — there is no earlier tooling statusline plumbing
to import. The monitor depends only on a ``Callable[[], Optional[float]]`` that
returns the child's context usage as a percentage (0-100) or ``None`` when it
cannot be read yet. Two implementations share that interface:

* :class:`StatuslineHookContextReader` — the D6 AUTHORITATIVE source. A
  per-session Claude Code statusline hook writes ``used_percentage`` to a small
  state file; this reader tails it. (Installing the hook belongs to the
  Agents-tab / launcher slice; only the reader seam is built here.)

* :class:`JsonlTranscriptContextReader` — the FALLBACK estimate. Parses the
  session transcript ``.jsonl`` and estimates usage. It over-reads (~2x per the
  spec); it exists so the monitor still functions before the hook lands.

* :class:`ProjectTranscriptContextReader` — fix-pass WS1 (2026-09-27): the
  fallback pointed at the transcript Claude Code ACTUALLY writes
  (``~/.claude/projects/<mangled resolved cwd>/*.jsonl``), tail-read, latest
  turn. This is what ``launcher.default_context_reader`` now uses; the
  ``JsonlTranscriptContextReader`` class above is left unchanged.

Both are plain callables, so swapping one for the other — or supplying a live
statusline reader once D6 is wired — is a one-line change at construction. That
is the "clean seam" the spec asks for.
"""

from __future__ import annotations

import glob
import json
import os
import re
from typing import Iterator, Optional, Tuple

from .constants import DEFAULT_CONTEXT_WINDOW_TOKENS


class StatuslineHookContextReader:
    """Reads authoritative ``used_percentage`` from a statusline-hook state file.

    The hook (D6) writes either a bare number or a JSON object carrying
    ``used_percentage`` (also accepts ``percent`` / ``used_pct``). Missing file,
    empty file, or an unparseable body all return ``None`` — an unreadable
    authoritative source is "unknown", never a fabricated 0.
    """

    def __init__(self, state_path: str, key: str = "used_percentage") -> None:
        self._state_path = state_path
        self._key = key

    def __call__(self) -> Optional[float]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                body = fh.read().strip()
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        if not body:
            return None
        # Bare number form.
        try:
            return float(body)
        except ValueError:
            pass
        # JSON object form.
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, (int, float)):
            return float(obj)
        if isinstance(obj, dict):
            for candidate in (self._key, "used_percentage", "percent", "used_pct"):
                val = obj.get(candidate)
                if isinstance(val, (int, float)):
                    return float(val)
        return None

    def read_with_epoch(self) -> Optional[Tuple[float, Optional[float]]]:
        """``(used_percentage, written_epoch)`` — the reading AND when the hook
        wrote it (``None`` epoch for a bare number / an object without one).
        ``None`` when there is no reading. Added 2026-09-29 for the compact gate,
        which must not accept a reading older than the last compaction;
        :meth:`__call__` (the monitor's path) is unchanged."""
        # ONE read of the file, so the value and its epoch come from the same
        # write (the hook rewrites the file continually).
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                body = fh.read().strip()
        except OSError:
            return None
        if not body:
            return None
        try:
            return float(body), None
        except ValueError:
            pass
        try:
            obj = json.loads(body)
        except ValueError:
            return None
        if isinstance(obj, (int, float)) and not isinstance(obj, bool):
            return float(obj), None
        if not isinstance(obj, dict):
            return None
        value: Optional[float] = None
        for candidate in (self._key, "used_percentage", "percent", "used_pct"):
            val = obj.get(candidate)
            if isinstance(val, (int, float)):
                value = float(val)
                break
        if value is None:
            return None
        raw = obj.get("written_epoch")
        epoch = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        return value, epoch


class JsonlTranscriptContextReader:
    """Estimates ``used_percentage`` from a Claude Code transcript ``.jsonl``.

    Strategy (fallback / estimate only):

    * Read the transcript line by line (each line is one JSON event).
    * If any event carries a ``usage`` block (``input_tokens`` +
      ``cache_read_input_tokens`` + ``cache_creation_input_tokens`` +
      ``output_tokens``), track the MAXIMUM such total seen — that approximates
      the high-water context occupancy.
    * Otherwise fall back to a crude ``len(text)/chars_per_token`` accumulation.
    * Return ``100 * tokens / window``, capped at 100.

    This deliberately over-reads (the spec notes ~2x); the authoritative reading
    is the statusline hook. ``None`` is returned when the transcript is absent or
    empty, so a not-yet-started session reads as "unknown", not 0.
    """

    def __init__(
        self,
        transcript_path: str,
        window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        chars_per_token: float = 4.0,
    ) -> None:
        self._transcript_path = transcript_path
        self._window_tokens = window_tokens
        self._chars_per_token = chars_per_token

    @staticmethod
    def _usage_total(usage: dict) -> Optional[int]:
        keys = (
            "input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "output_tokens",
        )
        if not any(k in usage for k in keys):
            return None
        return sum(int(usage.get(k, 0) or 0) for k in keys)

    def _estimate_tokens(self) -> Optional[float]:
        try:
            with open(self._transcript_path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except (FileNotFoundError, IsADirectoryError, PermissionError, OSError):
            return None
        if not lines:
            return None

        max_usage: Optional[int] = None
        char_total = 0
        saw_any = False

        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            saw_any = True
            usage = self._find_usage(event)
            if usage is not None:
                total = self._usage_total(usage)
                if total is not None:
                    max_usage = total if max_usage is None else max(max_usage, total)
            char_total += len(line)

        if not saw_any:
            return None
        if max_usage is not None:
            return float(max_usage)
        # No usage blocks at all — crude char-based estimate.
        return char_total / self._chars_per_token

    @staticmethod
    def _find_usage(event: dict) -> Optional[dict]:
        """Locate a ``usage`` dict in a transcript event (best-effort)."""
        if not isinstance(event, dict):
            return None
        if isinstance(event.get("usage"), dict):
            return event["usage"]
        message = event.get("message")
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            return message["usage"]
        return None

    def __call__(self) -> Optional[float]:
        tokens = self._estimate_tokens()
        if tokens is None:
            return None
        pct = 100.0 * tokens / float(self._window_tokens)
        return min(100.0, max(0.0, pct))


# --- fix-pass WS1 (2026-09-27): the REAL transcript location ----------------
#
# Flight finding #5/#6a: the fallback reader was pointed at
# ``<session_dir>/.claude/transcript.jsonl``, which Claude Code never writes, so
# the gauge read null for the whole flight. Claude Code writes the transcript to
# ``<claude config dir>/projects/<mangled cwd>/<session-uuid>.jsonl`` where the
# mangle replaces EVERY non-alphanumeric character with ``-`` and is applied to
# the RESOLVED cwd (a data root may be a symlink, and the real
# pilot transcript lives under under a path-mangled directory of the resolved location).
# Rule verified 28/28 against the real ``~/.claude/projects`` on the build
# machine (incl. ``_`` -> ``-`` and ``/var`` -> ``/private/var`` resolution).

#: Claude Code truncates very long project-dir names (and appends a suffix). We
#: cannot verify the suffix scheme locally, so beyond this length we fall back to
#: a prefix match rather than guess the suffix. Session dirs are ~90 chars.
_PROJECT_DIR_NAME_SOFT_LIMIT = 200


def claude_config_dir() -> str:
    """``$CLAUDE_CONFIG_DIR`` if set (Claude Code honours it), else ``~/.claude``."""
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".claude")


def claude_project_dir_name(cwd: str) -> str:
    """The ``~/.claude/projects`` directory name Claude Code uses for ``cwd``."""
    return re.sub(r"[^a-zA-Z0-9]", "-", os.path.realpath(cwd))


def claude_transcript_dir(cwd: str, projects_root: Optional[str] = None) -> Optional[str]:
    """The project transcript dir for ``cwd``, or ``None`` if it does not exist yet."""
    root = projects_root if projects_root is not None else os.path.join(claude_config_dir(), "projects")
    name = claude_project_dir_name(cwd)
    exact = os.path.join(root, name)
    if os.path.isdir(exact):
        return exact
    if len(name) > _PROJECT_DIR_NAME_SOFT_LIMIT:
        matches = sorted(glob.glob(os.path.join(root, glob.escape(name[:_PROJECT_DIR_NAME_SOFT_LIMIT]) + "*")))
        dirs = [m for m in matches if os.path.isdir(m)]
        if len(dirs) == 1:
            return dirs[0]
    return None


def _context_tokens(usage: dict) -> Optional[int]:
    """Context occupancy of one assistant turn = input + cache_creation +
    cache_read (output tokens are NOT context yet). Same rule other status HUDs use."""
    keys = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    if not any(k in usage for k in keys):
        return None
    try:
        return sum(int(usage.get(k, 0) or 0) for k in keys)
    except (TypeError, ValueError):
        return None


class ProjectTranscriptContextReader:
    """Fallback ``used_percentage`` from the child's REAL Claude Code transcript.

    Each call:

    * resolves ``<claude config>/projects/<mangled realpath(cwd)>/`` (lazily —
      the dir does not exist until claude writes its first event);
    * picks the NEWEST ``*.jsonl`` there by mtime (``/clear`` starts a new one);
    * TAIL-reads it (the pump polls every ~0.5s; real transcripts reach MBs)
      and takes the LATEST main-chain assistant ``usage`` — not the historical
      maximum, so a ``/compact`` drop is seen and the 90% threshold RE-ARMS.
      Sidechain events and zero-usage synthetic turns are skipped.

    Returns ``None`` (unknown, never a fabricated 0) when there is no transcript
    or no usable usage yet. It is an ESTIMATE against ``window_tokens``; the
    statusline hook (``statusline_hook.py``) is authoritative when present.
    """

    def __init__(
        self,
        cwd: str,
        window_tokens: int = DEFAULT_CONTEXT_WINDOW_TOKENS,
        projects_root: Optional[str] = None,
        tail_bytes: int = 256 * 1024,
        max_tail_bytes: int = 16 * 1024 * 1024,
    ) -> None:
        self._cwd = cwd
        self._window_tokens = window_tokens
        self._projects_root = projects_root
        self._tail_bytes = tail_bytes
        self._max_tail_bytes = max_tail_bytes

    def transcript_path(self) -> Optional[str]:
        directory = claude_transcript_dir(self._cwd, self._projects_root)
        if directory is None:
            return None
        newest: Optional[str] = None
        newest_mtime = -1.0
        try:
            entries = os.listdir(directory)
        except OSError:
            return None
        for entry in entries:
            if not entry.endswith(".jsonl"):
                continue
            path = os.path.join(directory, entry)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime > newest_mtime:
                newest, newest_mtime = path, mtime
        return newest

    @staticmethod
    def _lines_newest_first(fh, size: int, window: int) -> Iterator[bytes]:
        start = max(0, size - window)
        fh.seek(start)
        chunk = fh.read(size - start)
        lines = chunk.split(b"\n")
        if start > 0:
            lines = lines[1:]  # first line is (probably) partial
        return reversed(lines)

    def latest_context_tokens(self) -> Optional[int]:
        path = self.transcript_path()
        if path is None:
            return None
        try:
            with open(path, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                if size == 0:
                    return None
                window = self._tail_bytes
                while True:
                    for raw in self._lines_newest_first(fh, size, window):
                        tokens = self._tokens_from_line(raw)
                        if tokens is not None:
                            return tokens
                    if window >= size or window >= self._max_tail_bytes:
                        return None
                    window = min(size, window * 4, self._max_tail_bytes)
        except OSError:
            return None

    @staticmethod
    def _tokens_from_line(raw: bytes) -> Optional[int]:
        raw = raw.strip()
        if not raw or b"usage" not in raw:
            return None
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None
        if not isinstance(event, dict) or event.get("isSidechain") is True:
            return None
        message = event.get("message")
        usage = None
        if isinstance(message, dict) and isinstance(message.get("usage"), dict):
            usage = message["usage"]
        elif isinstance(event.get("usage"), dict):
            usage = event["usage"]
        if usage is None:
            return None
        tokens = _context_tokens(usage)
        if not tokens:  # None or 0 (synthetic / interrupted turns)
            return None
        return tokens

    def __call__(self) -> Optional[float]:
        tokens = self.latest_context_tokens()
        if tokens is None:
            return None
        pct = 100.0 * tokens / float(self._window_tokens)
        return min(100.0, max(0.0, pct))

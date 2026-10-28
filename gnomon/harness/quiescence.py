"""the "task done -> quiescent" stall state.

The stall guard nudges a parked session even after the agent has said it is
done. X1's idle backoff stretches the interval (capped at 60 min) but never
stops. 3B adds an explicit QUIESCENT state on :class:`~gnomon.harness.
monitor.ContextMonitor`:

* **entry** — ONLY by the agent's explicit declaration, the ``quiesce(reason)``
  self-verb (``self_mcp`` -> SELF-only internal actuator verb). Never inferred:
  a streak of 'idle' replies is X1's backoff, not a licence to go silent. Entry
  is REFUSED when the harness cannot watch the agent's transcript for its next
  turn (fail toward nudging — a woken-then-stuck agent must not sit unnudged
  for the whole valve);
* **while quiescent** — no stall nudge and no stall escalation. The 90 %/95 %
  threshold notices (``ContextMonitor.poll``) and DSD-answer notices
  (``DsdNoticeInbox``) do not pass through the stall path at all, so they are
  structurally unaffected (and still obey the D44 line-hold);
* **exit** — any operator keystroke (X1's ``optap.is_operator_input``), a
  DSD-answer notice, any other accepted hub inject/compact, an agent turn (read
  from the transcript by :func:`agent_turn_since`), a compaction, or the
  max-duration safety valve (default 8 h) which is followed by ONE normal
  nudge (then the usual one-shot-then-escalate discipline);
* **provenance** — every enter / suppressed / exit / refused is a
  ``{"kind": "quiescent"}`` record in the session's injection log; the dashboard
  reads the session's state from those records (``/api/sessions`` ``quiescent``).

Gated by feature flag ``quiescent_state`` (default OFF). Off = the verb
is not advertised, a hand-built frame is refused ``disabled``, and the monitor's
stall behaviour is byte-identical to before.

Pure stdlib; the transcript read is tail-bounded like X1's idle-reply reader.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Optional

from .operator_activity import _TAIL_BYTES, _parse_iso, resolve_transcript_path

#: Feature flag (registered in ``gnomon/flags.py``).
FLAG_NAME = "quiescent_state"

#: The self-verb name (Claude Code exposes it as ``mcp__gnomon-self__quiesce``).
QUIESCE_TOOL_NAME = "quiesce"

#: Safety valve: quiescence ends after this long; ONE nudge follows.
DEFAULT_QUIESCENT_MAX_SECONDS = 8 * 3600.0

#: How often (at most) the transcript is re-read for an agent turn after child
#: output was seen, and the no-output backstop interval.
DEFAULT_CHECK_INTERVAL = 5.0
DEFAULT_BACKSTOP_SECONDS = 60.0

#: The reason is shown on the dashboard and logged; single line, bounded.
MAX_REASON_CHARS = 200

#: Exit triggers (the ``trigger`` field of an exit record).
TRIGGER_OPERATOR = "operator-input"
TRIGGER_DSD = "dsd-answer"
TRIGGER_HUB = "hub-inject"
TRIGGER_AGENT_TURN = "agent-turn"
TRIGGER_UNVERIFIABLE = "agent-turn-unverifiable"
TRIGGER_COMPACT = "compact"
TRIGGER_VALVE = "max-duration"


def clean_reason(reason: Any) -> Optional[str]:
    """A single-line, bounded reason, or ``None`` if there is no usable text."""
    if not isinstance(reason, str):
        return None
    s = " ".join(reason.split())
    if not s:
        return None
    return s[:MAX_REASON_CHARS]


def _is_quiesce_tool(name: Any) -> bool:
    return isinstance(name, str) and (name == QUIESCE_TOOL_NAME or name.endswith("__" + QUIESCE_TOOL_NAME))


#: Text prefixes of ``isMeta`` user rows that are ECHOES of an operator's local
#: slash command (the caveat, the command name/args, its stdout) — not input to
#: the agent. Any other ``isMeta`` user row is input (fix pass, audit MEDIUM).
_ECHO_PREFIXES = ("<local-command-caveat>", "<command-name>", "<command-message>",
                  "<command-args>", "<local-command-stdout>", "<local-command-stderr>")


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content
                       if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str))
    return ""


def _is_turn_row(row: dict) -> bool:
    """A transcript row that means NEW INPUT reached the agent / it is working again.

    Enumerated from a real Claude Code 2.1.28x transcript (fix pass, 2026-09-29):

    * a ``user`` row that is not purely ``tool_result`` blocks — an operator
      prompt, an injected line, a task notification — INCLUDING ``isMeta`` rows,
      which carry the wake-ups (scheduled ticks ``turnOrigin: scheduled``, peer
      UDS messages ``turnOrigin: peer``, ``auto_continuation``), EXCEPT the
      local-command echo class (``<local-command-caveat>`` / ``<command-name>`` /
      ``<local-command-stdout>`` ...);
    * a ``system`` row with ``subtype: scheduled_task_fire``;
    * a ``queue-operation`` row with ``operation: enqueue`` (a prompt / task
      notification queued for the agent; ``dequeue``/``remove`` are bookkeeping);
    * an ``assistant`` row carrying a ``tool_use`` of any tool other than the
      quiesce verb itself (the agent kept working).
    The quiesce call's own tool_result and the agent's closing text are NOT turns.
    """
    kind = row.get("type")
    if kind == "queue-operation":
        return row.get("operation") == "enqueue"
    if kind == "system":
        return row.get("subtype") == "scheduled_task_fire"
    msg = row.get("message") if isinstance(row.get("message"), dict) else {}
    content = msg.get("content")
    if kind == "user":
        if isinstance(content, list) and not any(
                isinstance(c, dict) and c.get("type") != "tool_result" for c in content):
            return False
        if isinstance(content, str) and not content.strip():
            return False
        if not isinstance(content, (str, list)):
            return False
        if row.get("isMeta") and _user_text(content).lstrip().startswith(_ECHO_PREFIXES):
            return False
        return True
    if kind == "assistant" and isinstance(content, list):
        return any(isinstance(c, dict) and c.get("type") == "tool_use" and not _is_quiesce_tool(c.get("name"))
                   for c in content)
    return False


def agent_turn_since(transcript_path: str, since_epoch: float) -> Optional[bool]:
    """True = a new turn row is timestamped at/after ``since_epoch``; False = the
    transcript is readable and has none; None = unreadable (tail-bounded read)."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read()
    except OSError:
        return None
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        ts = _parse_iso(row.get("timestamp"))
        if ts is None or ts < since_epoch:
            continue
        if _is_turn_row(row):
            return True
    return False


def transcript_agent_turn_reader(session_dir: str) -> Callable[[float], Optional[bool]]:
    """``since_epoch -> True / False / None`` over this session's live transcript
    (resolved each call, so it follows the statusline hook's pointer)."""

    def _read(since_epoch: float) -> Optional[bool]:
        p = resolve_transcript_path(session_dir)
        if p is None:
            return None
        return agent_turn_since(p, since_epoch)

    return _read

"""Load-bearing byte constants and default tunables for the PTY harness.

These are extracted from the Gnomon harness spec
 and its narrative source
(``DESIGN.md`` section 4.2/4.3). They are re-implemented here, NOT imported
from earlier tooling. The spec cites earlier tooling ``ptycore.py`` as the requirements
source only.

⛔ Do not "clean these up". The submit byte is a carriage-return, not a
newline; the injection sequence uses two separate escape bytes; the beats are
real. See.
"""

from __future__ import annotations

# --- injection sequence bytes ---------------

#: ESC ESC — clears any text already sitting in the TUI prompt before we type.
ESC_ESC: bytes = b"\x1b\x1b"

#: The submit byte. MUST be a carriage-return. The Claude TUI submits on ``\r``;
#: a newline (``\n``) does NOT submit. This is the single most easily-broken
#: invariant in the whole harness.
SUBMIT: bytes = b"\r"

# --- bracketed paste ------------------------

#: Bracketed-paste start / end. Multi-line injected content is wrapped in these
#: so the TUI treats the whole block as a paste rather than interpreting an
#: embedded newline as a submit.
BRACKETED_PASTE_START: bytes = b"\x1b[200~"
BRACKETED_PASTE_END: bytes = b"\x1b[201~"

# --- timing ----------------------------------------------------------------

#: The inter-phase "beat". A paste-buffering TUI must ingest the cleared-prompt
#: escape (and then the typed text) before the next write lands, or the submit
#: races the text. 0.2s is the live-tuned value from the spec.
DEFAULT_BEAT_SECONDS: float = 0.2

#: a SINGLE-line injection longer than this many UTF-8 bytes is typed as
#: an explicit bracketed paste (``Injector._payload_phases``). A macOS PTY delivers at
#: most 1,022 bytes per read (measured 2026-10-02) and Claude Code kept only the last
#: unbracketed chunk of a longer burst; 512 leaves margin below that split. Applied
#: only with ``inject_long_line_as_paste`` ON (registry default ON).
LONG_LINE_PASTE_BYTES: int = 512
#: Measured (macOS PTY): the reader gets at most this many bytes per ``read()``; Claude Code
#: keeps only the last UNBRACKETED chunk. Used by the leading-"!" screen (r2d, r2c F3) to find chunk starts.
PTY_READ_CHUNK_BYTES: int = 1022

#: round 4 (r3 Diamond F20, checklist g64-amendment-2026-10-02): the LARGEST
#: single write that may carry its own submitting CR, counted WITH the CR. Measured on the
#: real Claude Code 2.1.287: one write of line + CR submitted at ~40 bytes and had its CR
#: ABSORBED (not submitted) at ~64 bytes and above. Anything longer sends the CR as its own
#: write after a beat (``injector.submit_writes``). Re-measure on each Claude Code bump.
SUBMIT_SINGLE_WRITE_MAX_BYTES: int = 40

# --- context monitor ------------------------

#: Hosted-child thresholds, ``HOSTED_CHILD_THRESHOLDS`` in the spec. 65/80 were
#: measured not useful (operator, 2026-08-11); 90/95 are what remain.
HOSTED_CHILD_THRESHOLDS: tuple[int, ...] = (90, 95)

#: The actionable line (D19, operator 2026-09-23). At 90 the agent runs the
#: prepare-for-compact variant; it needs headroom before the 95 backstop.
ACTIONABLE_THRESHOLD: int = 90

#: Hard backstop. A louder FYI, never a forced action.
BACKSTOP_THRESHOLD: int = 95

#: Rough context-window size used by the jsonl-transcript *estimate* reader.
#: The estimate is understood to over-read (~2x per the spec); the authoritative
#: source is the D6 statusline hook. See ``context_readers.py``.
DEFAULT_CONTEXT_WINDOW_TOKENS: int = 200_000

# --- PTY-hooks WS-C: the dual-idle stall trigger (DELTA-4 defaults) ---------

#: The GLOBAL default for the dual-idle stall-trigger (resume keep-alive). A
#: per-instance override (profile ``stall_inject``) is merged OVER this and may
#: set ``enabled: false``. DELTA-4 (autonomous default, flagged for the operator):
#: child idle 300s AND operator idle 300s -> ONE resume inject; still idle 600s
#: later -> ONE escalation record (no second inject); after a compaction the
#: next episode's child-idle threshold is 150s. Consecutive fires with no
#: operator activity back off 2x each, capped at 8x.
#: (Timing widened 2026-09-28 — "fires too quickly" fix: agent/operator idle
#: 180/120 -> 300/300, post-compact 45 -> 150. escalate_after/rearm/backoff
#: unchanged. See the Config-tab UX rework brief.)
DEFAULT_STALL_INJECT: dict = {
    "enabled": True,
    "agent_idle_seconds": 300,
    "operator_idle_seconds": 300,
    "escalate_after_seconds": 600,
    "post_compact_idle_seconds": 150,
    "rearm_grace_seconds": 5,
    "backoff_max": 8,
    "content": (
        "[Gnomon] resume check: you and the operator have both been idle (for the operator that means no console typing; "
        "messages sent through Claude Code's Remote Control, from another device, are not counted). "
        "If you have in-flight work (for example you were mid-task before a /compact), "
        "continue it now from your handover notes. If you are genuinely done, reply "
        "'idle' in one line and wait."
    ),
    # 2026-09-29 cross-harness operator activity (harness/operator_activity.py):
    # "active in session X" = a keystroke/submit within this window.
    "active_window_seconds": 120,
    # Idle backoff: after two consecutive 'idle' replies with no operator input
    # between, the nudge interval doubles per further idle reply, capped here.
    "idle_backoff_cap_seconds": 3600,
    # Compose the nudge at fire time from the shared operator activity. Applies
    # only while ``content`` is the built-in default; a custom content is used
    # verbatim. False = always the static content.
    "context_aware": True,
    # Optional presence-sensor seam: a file path or loopback http URL delivering
    # {"present": bool, "ts": epoch}. None = no sensor.
    "presence_source": None,
}

_STALL_NUMERIC_KEYS = ("agent_idle_seconds", "operator_idle_seconds", "escalate_after_seconds",
                       "post_compact_idle_seconds", "rearm_grace_seconds", "backoff_max",
                       "active_window_seconds", "idle_backoff_cap_seconds")


def merge_stall_inject(global_cfg=None, override=None) -> dict:
    """``DEFAULT <- global <- per-instance override``, validated. Fails LOUD.

    ``escalate_after_seconds`` / ``post_compact_idle_seconds`` may be ``None``
    (disabled). Unknown keys are refused (a typo must not silently no-op).
    """
    out = dict(DEFAULT_STALL_INJECT)
    for layer, name in ((global_cfg, "global stall_inject"), (override, "per-instance stall_inject")):
        if layer is None:
            continue
        if not isinstance(layer, dict):
            raise ValueError(f"{name} must be an object")
        unknown = set(layer) - set(DEFAULT_STALL_INJECT)
        if unknown:
            raise ValueError(f"{name} has unknown key(s) {sorted(unknown)}")
        out.update(layer)
    if not isinstance(out["enabled"], bool):
        raise ValueError("stall_inject 'enabled' must be a boolean")
    if not isinstance(out["content"], str) or not out["content"].strip():
        raise ValueError("stall_inject 'content' must be a non-empty string")
    for key in _STALL_NUMERIC_KEYS:
        val = out[key]
        if val is None and key in ("escalate_after_seconds", "post_compact_idle_seconds"):
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0 or (
                val == 0 and key in ("agent_idle_seconds", "backoff_max", "active_window_seconds",
                                     "idle_backoff_cap_seconds")):
            raise ValueError(f"stall_inject {key!r} must be a positive number")
    if not isinstance(out["context_aware"], bool):
        raise ValueError("stall_inject 'context_aware' must be a boolean")
    ps = out["presence_source"]
    if ps is not None and (not isinstance(ps, str) or not ps.strip()):
        raise ValueError("stall_inject 'presence_source' must be null or a non-empty path/URL string")
    return out

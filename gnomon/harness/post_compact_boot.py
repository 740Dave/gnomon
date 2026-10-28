"""Post-compact boot prompt (operator ruling 2026-09-29).

After a compaction the harness gives the successor its per-role boot line, so the
operator need not hand-type it and it lands before any in-process subagent result.
Behind the flag ``post_compact_boot`` (default OFF; OFF = no object is
built, no file is written, the hook prints exactly what it printed before).

TWO LEGS (the design sketch on the node)

1. ORDERING — the text is appended to the ``SessionStart:compact`` hook's stdout
   (``lifecycle_hook``), which Claude Code adds to the SAME context as the
   compaction summary, so it is present before ANY following turn, including a
   background subagent's task-notification. The hook is a separate process; it
   reads the text from ``<session>/.claude/post-compact-boot.json``, which the
   harness writes at launch (:func:`sync_boot_file`) — ONE resolution, used by
   both legs. The file is REMOVED when the flag is off, so a stale file can never
   turn the leg on.
2. TRIGGER — context alone starts no turn, so at the lifecycle compact/post event
   :class:`PostCompactBoot` submits the same text as a prompt through the PTY:
   only when the operator line is EMPTY (the shared D44 line-hold,
   ``harness/line_hold.py``), ahead of queued notices, ``clear=False`` (never
   interrupts), recorded ``source: harness`` / ``trigger: post-compact-boot`` —
   never attributed to the operator.

CONFIG (the ``post_compact_boot`` object; same shape in launch.json and the
machine config ``~/gnomon-data/config.json``)::

    {"default": "<text>",                      # any role without a custom text
     "roles": {"<role>": "<text>", ...},       # custom text per role
     "settle_seconds": 2.0,                    # wait after the event before typing
     "max_hold_seconds": 1800.0}               # give up (loudly) after this long held

Precedence per role: launch.json > machine config > the DEPLOYMENT table
(``<deploy_root>/defaults/post-compact-boot.json``, same shape; X13 S5-3) > the package
defaults (:mod:`.post_compact_boot_texts`, neutral, one sentence for every role); within a
layer ``roles[role]`` > ``default``.
The ROLE is the launch's instance id (``${ROLE}``).

NO DOUBLE PROMPT (requirement 5): while the boot prompt is pending the monitor's
resume nudge is suppressed; once the boot prompt has been written the
post-compact short-threshold state is consumed (the next idle episode uses the
ordinary interval). If the boot prompt is DENIED or dropped stale the nudge is
released, so a failed boot never leaves the session with no prompt at all.

⛔ The text goes into a PTY: it must be one line of printable characters, must not
start with ``/`` (a slash command) or ``\\`` (the operator command plane).
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import sys
import unicodedata
from typing import Any, Callable, Dict, Optional, Tuple

from .compact_gate import ZERO_WIDTH_CHARS  # the ONE zero-width list (D15 / D50), not a second copy

FLAG_NAME = "post_compact_boot"
#: Provenance ``trigger`` of the PTY-submitted prompt (its ``source`` is
#: ``hooks.SOURCE_HARNESS``) and the ``kind`` of the lifecycle records.
TRIGGER = "post-compact-boot"
RECORD_KIND = "post-compact-boot"
#: The hook-readable resolution, under ``<session_dir>/.claude/``.
BOOT_FILENAME = "post-compact-boot.json"
#: The label line printed before the text in the SessionStart:compact context.
CONTEXT_LABEL = "[Gnomon] Post-compact boot instruction:"

DEFAULT_SETTLE_SECONDS = 2.0
DEFAULT_MAX_HOLD_SECONDS = 1800.0
#: X17 r3: a larger max_hold is invalid config (1e12 would effectively disable the stale-drop).
MAX_HOLD_CEILING_SECONDS = 86400.0
#: X17 r4: settle_seconds must be finite and within 0..60 inclusive.
MAX_SETTLE_SECONDS = 60.0
MAX_TEXT_CHARS = 2000

_CONFIG_KEYS = frozenset({"default", "roles", "settle_seconds", "max_hold_seconds"})


# -- configuration -------------------------------------------------------------


def validate_text(text: Any, what: str) -> str:
    """The text is typed into a PTY: fail LOUD (ValueError) on anything unsafe."""
    if not isinstance(text, str) or not text.strip():
        raise ValueError(f"post_compact_boot {what} must be a non-empty string")
    if len(text) > MAX_TEXT_CHARS:
        raise ValueError(f"post_compact_boot {what} is {len(text)} chars (max {MAX_TEXT_CHARS})")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in text):
        raise ValueError(f"post_compact_boot {what} must be one line of printable characters "
                         "(no control characters — it is typed into the PTY)")
    # X17 round 2 (F2): an invisible leading character (zero-width / format / line-sep)
    # must not hide a leading '/' or '\\'. Reject it outright, before the slash check.
    # r4 (F-R2-1): the category of the RAW first character, BEFORE any strip (lstrip()
    # eats U+2028/U+2029, so checking only the stripped head never saw Zl/Zp).
    raw = text[:1]
    if raw in ZERO_WIDTH_CHARS or unicodedata.category(raw) in ("Cf", "Zl", "Zp"):
        raise ValueError(f"post_compact_boot {what} must not start with an invisible character "
                         "(zero-width / format / line separator)")
    head = text.lstrip()[:1]
    if head and (head in ZERO_WIDTH_CHARS or unicodedata.category(head) in ("Cf", "Zl", "Zp")):
        raise ValueError(f"post_compact_boot {what} must not start with an invisible character "
                         "(zero-width / format) — it could hide a leading '/' or '\\\\'")
    from .leading_bang import has_leading_bang

    if has_leading_bang(text):  # operator ruling bash mode, same rule as the inject screens
        raise ValueError(f"post_compact_boot {what} must not start with '!' "
                         "(Claude Code would run it as a shell command, not a prompt)")
    if head in ("/", "\\"):
        raise ValueError(f"post_compact_boot {what} must not start with '/' or '\\\\' "
                         "(it would be a slash command / operator command, not a prompt)")
    return text


def _number(layer: Dict[str, Any], key: str, what: str, *, positive: bool = False,
            ceiling: Optional[float] = None) -> Optional[float]:
    if key not in layer or layer[key] is None:
        return None
    v = layer[key]
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or v == float("inf") \
            or ((v <= 0) if positive else (v < 0)) or (ceiling is not None and v > ceiling):
        raise ValueError(f"post_compact_boot {what}.{key} must be a finite number "
                         f"{'> 0' if positive else '>= 0'}"
                         f"{f' and <= {ceiling:g}' if ceiling is not None else ''}")
    return float(v)


def _check_layer(layer: Any, what: str) -> Dict[str, Any]:
    if layer is None:
        return {}
    if not isinstance(layer, dict):
        raise ValueError(f"post_compact_boot {what} must be an object")
    unknown = set(layer) - _CONFIG_KEYS
    if unknown:
        raise ValueError(f"post_compact_boot {what} has unknown key(s) {sorted(unknown)}")
    roles = layer.get("roles")
    if roles is not None and (not isinstance(roles, dict) or any(not isinstance(k, str) or not k for k in roles)):
        raise ValueError(f"post_compact_boot {what}.roles must be an object of role -> text")
    return layer


_READ_DEPLOY = object()     # sentinel: "read the deployment table lazily" (None / {} = no deployment layer)


def resolve(role: Optional[str], launch_cfg: Any = None, machine_cfg: Any = None,
            deploy_cfg: Any = _READ_DEPLOY) -> Dict[str, Any]:
    """Resolve the boot text + timing for ``role``. Fails LOUD (ValueError) on a bad config.

    Returns ``{"role", "text", "text_source", "settle_seconds", "max_hold_seconds"}``.
    ``text_source`` names the layer and key the text came from (audit trail)."""
    from . import post_compact_boot_texts as base

    if deploy_cfg is _READ_DEPLOY:
        from .. import deploy_data

        deploy_cfg = deploy_data.load_json("post-compact-boot")
        for path, why in deploy_data.errors().items():
            if path.endswith("post-compact-boot.json"):
                raise ValueError(f"post_compact_boot deployment table {path} is unusable: {why}")
    layers = (("launch.json", _check_layer(launch_cfg, "launch.json post_compact_boot")),
              ("machine-config", _check_layer(machine_cfg, "machine config post_compact_boot")),
              ("deployment", _check_layer(deploy_cfg, "deployment post_compact_boot")),
              ("package", {"default": base.DEFAULT_TEXT, "roles": dict(base.ROLE_TEXTS)}))
    text = source = None
    for name, layer in layers:
        roles = layer.get("roles") or {}
        if role and role in roles:
            text, source = roles[role], f"{name}:roles[{role}]"
            break
        if layer.get("default") is not None:
            text, source = layer["default"], f"{name}:default"
            break
    text = validate_text(text, f"text ({source})")
    settle = max_hold = None
    for name, layer in layers[:3]:
        if settle is None:
            settle = _number(layer, "settle_seconds", name, ceiling=MAX_SETTLE_SECONDS)
        if max_hold is None:
            max_hold = _number(layer, "max_hold_seconds", name, positive=True,
                               ceiling=MAX_HOLD_CEILING_SECONDS)  # F4: never 0; r3: never > 24 h
    return {"role": role, "text": text, "text_source": source,
            "settle_seconds": DEFAULT_SETTLE_SECONDS if settle is None else settle,
            "max_hold_seconds": DEFAULT_MAX_HOLD_SECONDS if max_hold is None else max_hold}


def resolve_for_launch(cfg: Dict[str, Any], machine_config: Any, session_dir: str,
                       flag_on: bool) -> Dict[str, Any]:
    """The ``harness["post_compact_boot"]`` entry. Flag off -> ``{"enabled": False}``
    and NOTHING else is read or validated (a bad boot config never refuses a
    launch that does not use it). Flag ON with a malformed config (F9 ruling): the
    feature is DISABLED for this session, never the launch —
    ``{"enabled": False, "config_error": <message>}``; the launcher prints one loud
    stderr line and writes a provenance record naming the bad field."""
    if not flag_on:
        return {"enabled": False}
    role = cfg.get("instance_id") if isinstance(cfg.get("instance_id"), str) else None
    if not role:
        from ..agent_def import role_for

        role = role_for(session_dir)
    try:
        out = resolve(role, cfg.get("post_compact_boot"), getattr(machine_config, "post_compact_boot", None))
    except ValueError as exc:
        return {"enabled": False, "config_error": str(exc)}
    out["enabled"] = True
    return out


# -- the hook-readable file ------------------------------------------------------


def boot_file_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", BOOT_FILENAME)


def sync_boot_file(session_dir: str, boot: Dict[str, Any]) -> Optional[str]:
    """Write the resolution for the SessionStart:compact hook (atomic), or REMOVE
    the file when the boot prompt is off. Returns the path written, else ``None``.
    Raises OSError on a write failure (the caller reports it loudly)."""
    path = boot_file_path(session_dir)
    if not boot.get("enabled"):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return None
    body = {"schema": 1, "role": boot.get("role"), "text": boot["text"],
            "text_source": boot.get("text_source"), "written_at": time.time()}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".post-compact-boot-", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(body, fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def read_boot_text(session_dir: Optional[str]) -> Optional[str]:
    """The text the SessionStart:compact hook should add, or ``None`` (no file, a
    malformed one, or unsafe text). Never raises: a hook must never fail."""
    if not session_dir:
        return None
    try:
        with open(boot_file_path(session_dir), "r", encoding="utf-8") as fh:
            body = json.load(fh)
        return validate_text(body.get("text"), "boot file text")
    except (OSError, ValueError, AttributeError):
        return None


# -- the PTY leg -------------------------------------------------------------------


class PostCompactBoot:
    """Submits the boot prompt once per compaction, under the D44 line-hold.

    Everything runs on the single pump thread. ``arm()`` is called at the
    lifecycle compact/post event; ``tick()`` is called every pump step (and
    before any queued notice is typed, so the boot prompt goes FIRST).

    Parameters are injected so the class is testable without a PTY:

    * ``inject(text, clear=, source=, trigger=, meta=)`` — the logged injector;
    * ``line_hold_reason()`` — the shared line rule (``None`` = free to type);
    * ``paused()`` / ``ready()`` — operator ``\\pause`` and REPL-ready gates;
    * ``record(kind, **fields)`` — provenance;
    * ``on_pending(bool)`` — tells the monitor to suppress the resume nudge;
    * ``on_written()`` — the boot prompt fired: consume the post-compact state.
    """

    def __init__(
        self,
        *,
        text: str,
        inject: Callable[..., Any],
        line_hold_reason: Callable[[], Optional[str]],
        record: Callable[..., Any],
        paused: Optional[Callable[[], bool]] = None,
        ready: Optional[Callable[[], bool]] = None,
        on_pending: Optional[Callable[[bool], None]] = None,
        on_written: Optional[Callable[[], None]] = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        max_hold_seconds: float = DEFAULT_MAX_HOLD_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        text_source: Optional[str] = None,
    ) -> None:
        self._text = text
        self._inject = inject
        self._line_hold_reason = line_hold_reason
        self._record = record
        self._paused = paused
        self._ready = ready
        self._on_pending = on_pending
        self._on_written = on_written
        try:
            st = float(settle_seconds)
        except (TypeError, ValueError):
            st = float("nan")
        # r4: an out-of-range / non-finite settle falls back to the default (never an unbounded wait)
        self._settle = st if (0.0 <= st <= MAX_SETTLE_SECONDS) else DEFAULT_SETTLE_SECONDS
        # F4: the stale-drop is never disabled — a non-positive / non-finite value falls back to the default
        try:
            mh = float(max_hold_seconds)
        except (TypeError, ValueError):
            mh = 0.0
        self._max_hold = mh if (0 < mh <= MAX_HOLD_CEILING_SECONDS) else DEFAULT_MAX_HOLD_SECONDS
        self._clock = clock
        self._text_source = text_source
        self._seq = 0
        self._armed_at: Optional[float] = None
        self._due: Optional[float] = None
        self._held_reported = False
        self.written = 0

    @property
    def text(self) -> str:
        return self._text

    @property
    def pending(self) -> bool:
        return self._armed_at is not None

    def _rec(self, decision: str, **extra: Any) -> None:
        try:
            self._record(RECORD_KIND, decision=decision, compact_seq=self._seq, **extra)
        except Exception:  # noqa: BLE001 - provenance never breaks the pump
            pass

    def _set_pending(self, value: bool) -> None:
        if self._on_pending is not None:
            try:
                self._on_pending(value)
            except Exception:  # noqa: BLE001
                pass

    def arm(self) -> None:
        """A compaction completed (lifecycle compact/post): owe one boot prompt."""
        if self.pending:
            self._rec("superseded", reason="a newer compaction completed before the boot prompt was delivered")
        self._seq += 1
        now = self._clock()
        self._armed_at = now
        self._due = now + self._settle
        self._held_reported = False
        self._set_pending(True)
        self._rec("armed", trigger=TRIGGER, text_source=self._text_source, settle_seconds=self._settle)

    def _hold_reason(self) -> Optional[str]:
        if self._ready is not None and not self._ready():
            return "child REPL not ready yet"
        if self._paused is not None and self._paused():
            return "operator paused injections"
        try:
            return self._line_hold_reason()
        except Exception:  # noqa: BLE001 - an unreadable line is not an empty line
            return "operator line state unreadable (held)"

    def tick(self) -> bool:
        """Deliver the boot prompt if it is due and the line allows. True iff typed now."""
        if self._armed_at is None or self._due is None:
            return False
        now = self._clock()
        if now < self._due:
            # r4 (F-R2-2): the stale-drop is independent of settle, so no settle value can
            # outlast max_hold (the boot, the nudge and the DSD inbox are never parked).
            if now - self._armed_at >= self._max_hold:
                self._rec("dropped-stale", loud=True,
                          reason=f"boot prompt still in its settle window after {self._max_hold:.0f}s; "
                                 "released — the resume nudge is no longer suppressed")
                self._clear()
            return False
        reason = self._hold_reason()
        if reason is not None:
            if not self._held_reported:
                self._held_reported = True
                self._rec("held", reason=reason)
            if now - self._armed_at >= self._max_hold:
                # never park the session (and its resume nudge) forever behind a stuck line
                self._rec("dropped-stale", loud=True,
                          reason=f"boot prompt still held after {self._max_hold:.0f}s ({reason}); "
                                 "released — the resume nudge is no longer suppressed")
                self._clear()
            return False
        return self._deliver()

    def _clear(self) -> None:
        self._armed_at = None
        self._due = None
        self._set_pending(False)

    def _deliver(self) -> bool:
        from .hooks import SOURCE_HARNESS, Decision, Outcome

        seq = self._seq
        try:
            outcome = self._inject(self._text, clear=False, source=SOURCE_HARNESS, trigger=TRIGGER,
                                   meta={"compact_seq": seq})
        except Exception as exc:  # noqa: BLE001 - loud, and release the nudge
            self._rec("failed", loud=True, error=f"{type(exc).__name__}: {exc}")
            self._clear()
            return False
        decision = outcome.decision if isinstance(outcome, Outcome) else Decision.ALLOW
        self._clear()
        if decision in (Decision.ALLOW, Decision.MODIFY, Decision.DEFER):
            # DEFER = held for the operator's release: the prompt is owed and
            # in the operator's hands, so the resume nudge stays consumed too.
            self.written += 1
            self._rec("written" if decision is not Decision.DEFER else "deferred", trigger=TRIGGER)
            if self._on_written is not None:
                try:
                    self._on_written()
                except Exception:  # noqa: BLE001
                    pass
            return decision is not Decision.DEFER
        self._rec("denied", loud=True, reason=getattr(outcome, "reason", ""),
                  detail="a hook denied the boot prompt; the resume nudge is released")
        return False

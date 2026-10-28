"""The agentic-lifecycle hook bus (PTY-hooks spec 2026-09-27, WS-A / WS-E).

Every event at the harness<->agent boundary becomes a canonical event
routed through an ordered hook pipeline whose decision the HARNESS ENFORCES —
it writes to the PTY, or it doesn't. The enforcement point is the one physical
write site (:class:`~gnomon.harness.writer.SingleWriteSite`): the gate
below is installed ON the writer, so a DENY cannot be bypassed by any actuation
path — the bytes never reach ``os.write(master_fd)``.

Pieces
------
* :class:`InjectionEvent` — ``{source, target, content, parsed, clear, ts,
  session_dir, trigger, meta}``; the canonical inbound-injection event.
* :class:`Decision` — ``ALLOW`` / ``MODIFY`` / ``DENY`` / ``DEFER``. DENY and
  DEFER short-circuit the pipeline.
* :class:`HookPipeline` — an ordered list of handlers (built from a manifest by
  :func:`build_handlers`). Programmatic handlers run synchronously; the
  ``command`` kind delegates to an external process (the seam an agentic screen
  — e.g. a Haiku prompt-injection check — plugs into). Failure policy: a
  handler marked ``security`` FAILS CLOSED (deny); any other fails OPEN (logged).
* :class:`ProvenanceLog` — the append-only per-session
  ``<session_dir>/.claude/injection_log.jsonl``. One record per decision. THIS
  is the artifact that makes "operator or harness?" a recorded fact.
* :class:`InjectionGate` — pipeline + log + the operator's controls
  (``hooks off/on``, ``pause/resume``, the DEFER hold queue). Installed on the
  writer; every programmatic write is decided here exactly once.
* :class:`ClaudeDialect` — the ONLY CLI-specific part (recognising ``/cmd``).
  Universal enforcement, swappable recognition.
* :class:`LifecycleBus` — ``agent-start`` / ``agent-end`` / ``compact`` points
  (WS-E), same handler kinds, fired exactly once per lifecycle.

stdlib only.
"""

from __future__ import annotations

import collections
import dataclasses
import enum
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from . import leading_bang as _LB
from .writer import START_HOLD_PREFIX as _START_HOLD_PREFIX, PendingCapRefused as _PendingCapRefused
from .leading_bang import has_leading_bang

# --- vocabulary --------------------------------------------------------------

SOURCE_OPERATOR = "operator"
SOURCE_SELF_VERB = "self-verb"
SOURCE_MONITOR_GUIDANCE = "monitor-guidance"
SOURCE_BUS = "bus"
SOURCE_BOOTSTRAP = "bootstrap"
#: The folder-trust accepter's modal-hold exemption is bound to ITS call path (startup.FolderTrustAccepter),
#: not to a trigger string a command frame can carry: it stamps ``meta[FOLDER_TRUST_CAP_KEY] = FOLDER_TRUST_CAP``.
#: The capability is a process-local object, so no JSON frame (hub, local control socket) can forge it.
FOLDER_TRUST_CAP_KEY = "_gm_folder_trust_cap"
FOLDER_TRUST_CAP = object()
#: g79 direction rule (2026-10-03): EVERY non-operator source is line-held by default. The ONE other path that
#: legitimately writes onto a partial operator line is the D50 takeover compact (the compact gate saved his text and
#: the ESC ESC clear is mandatory). It is recognised by CALL PATH too: ``Injector.compact(line_takeover=True)``
#: (called only by ``Actuator.compact`` after ``CompactGate.check`` returned a takeover verdict) stamps this
#: process-local object, which no JSON frame can carry. A frame's ``source`` / ``trigger`` / ``meta`` never
#: changes how the line hold treats an event.
LINE_TAKEOVER_CAP_KEY = "_gm_line_takeover_cap"
LINE_TAKEOVER_CAP = object()
#: g79 (round 3 P2-3): the COMPLETE list of harness writes that do NOT pass the line hold in ``decide``. Every
#: other non-operator source is HELD by default. Pinned by tests/test_bus_inject_line_hold.py (this tuple, the
#: capability stamp sites, and the single call site of ``restore_unsent``).
#:   1. operator keystrokes / operator-typed text (SOURCE_OPERATOR): his own line.
#:   2. ``\approve --into-dialog`` (``approve(force=True)``): the operator's explicit force.
#:   3. folder-trust accepter: capability FOLDER_TRUST_CAP, source ``bootstrap`` (startup.py).
#:   4. D50 takeover compact: capability LINE_TAKEOVER_CAP, source ``self-verb`` (injector.compact via
#:      actuator after CompactGate.check); still WAITS (is dropped) for an open dialog.
#:   5. ``optap.restore_unsent`` (the restore of his own saved line after a takeover compact): does NOT go
#:      through ``decide`` (ledgered by ``run_ledgered``, route ``restore``). Bound to the compact gate's call
#:      path by being called ONLY from ``compact_gate`` (call-site pin test) and by an in-tap LINE-EMPTY GUARD
#:      (refuses unless ``line_state == empty``; round 3). Round 4: it ALSO bypasses the DIALOG hold, so
#:      ``CompactGate.on_post_compact`` checks the dialog reader first and defers (never types into a dialog).
#:      Not a capability: stated limit.
LINE_HOLD_EXEMPTIONS = ("operator", "approve-into-dialog", "folder-trust-cap", "d50-takeover-cap",
                        "restore-unsent")
SOURCE_RESUME = "resume"
SOURCE_OPERATOR_BROADCAST = "operator-broadcast"
SOURCE_AGENT_LAUNCH = "agent-launch"
#: D15 amendment (operator-approved 2026-09-29): a DSD-answer
#: notice delivered from the hub's durable queue.
SOURCE_DSD_ANSWER = "dsd-answer"
#: (2026-09-30): a prompt the HARNESS itself composed and submitted on its
#: own initiative (today: the post-compact boot prompt, ``trigger:
#: post-compact-boot``). Never the operator, never an agent verb, and not a hub
#: claim (deliberately NOT in HUB_CLAIMABLE_SOURCES).
SOURCE_HARNESS = "harness"
#: A write that reached the writer WITHOUT a declared source. Production wiring
#: must never produce one; a record with this source is a path someone forgot
#: to attribute (the provenance log makes that visible instead of silent).
SOURCE_UNATTRIBUTED = "unattributed"

SOURCES = frozenset({
    SOURCE_OPERATOR, SOURCE_SELF_VERB, SOURCE_MONITOR_GUIDANCE, SOURCE_BUS,
    SOURCE_BOOTSTRAP, SOURCE_RESUME, SOURCE_OPERATOR_BROADCAST, SOURCE_AGENT_LAUNCH,
    SOURCE_DSD_ANSWER, SOURCE_HARNESS, SOURCE_UNATTRIBUTED,
})

#: Sources a hub (rendezvous) frame may CLAIM. Anything else from the hub is
#: recorded as ``bus``. (The hub is the operator's surface; a frame cannot claim
#: to be the local operator or a self-verb.)
HUB_CLAIMABLE_SOURCES = frozenset({SOURCE_BUS, SOURCE_OPERATOR_BROADCAST, SOURCE_AGENT_LAUNCH,
                                   SOURCE_DSD_ANSWER})

#: Hook points.
POINT_INBOUND = "inbound-injection"
POINT_OPERATOR_COMMAND = "operator-command"
POINT_OPERATOR_COMMAND_POST = "operator-command-post"
POINT_AGENT_START = "agent-start"
POINT_AGENT_END = "agent-end"
POINT_COMPACT = "compact"
POINTS = frozenset({
    POINT_INBOUND, POINT_OPERATOR_COMMAND, POINT_OPERATOR_COMMAND_POST,
    POINT_AGENT_START, POINT_AGENT_END, POINT_COMPACT,
})

#: The provenance log's file name under ``<session_dir>/.claude/``.
INJECTION_LOG_FILENAME = "injection_log.jsonl"

_PREVIEW_CHARS = 120


class Decision(str, enum.Enum):
    ALLOW = "allow"
    MODIFY = "modify"
    DENY = "deny"
    DEFER = "defer"


#: Decisions under which the bytes are written.
WRITING_DECISIONS = frozenset({Decision.ALLOW, Decision.MODIFY})


# --- the dialect adapter (the only CLI-specific piece) ------------------------


class ClaudeDialect:
    """Recognise Claude Code slash commands in content. Pure parse, no policy."""

    name = "claude"

    #: The startup folder-trust dialog, as it appears in the TUI's output once
    #: ANSI escapes AND whitespace are stripped (Ink positions words with cursor
    #: moves, so the rendered text carries no spaces). Measured live 2026-09-27.
    TRUST_PROMPT_MARKERS = ("itrustthisfolder",)
    #: Select "Yes, I trust this folder" (second option; the cursor starts on
    #: "No, exit"): DOWN, then Enter.
    TRUST_ACCEPT_KEYS = (b"\x1b[B", b"\r")

    @staticmethod
    def parse(text: str) -> Optional[Dict[str, str]]:
        if not isinstance(text, str):
            return None
        stripped = text.lstrip()
        if not stripped.startswith("/") or len(stripped) < 2:
            return None
        head, _, rest = stripped.partition(" ")
        if not re.fullmatch(r"/[A-Za-z0-9][A-Za-z0-9:_.-]*", head):
            return None
        return {"kind": "slash", "command": head, "args": rest.strip()}


# --- the event ----------------------------------------------------------------


@dataclass(frozen=True)
class InjectionEvent:
    """One inbound injection (or operator line) as the hook bus sees it."""

    source: str
    content: str
    clear: bool = False
    target: Optional[str] = None
    parsed: Optional[Dict[str, str]] = None
    ts: float = field(default_factory=time.time)
    session_dir: Optional[str] = None
    trigger: Optional[str] = None
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_payload(self) -> Dict[str, Any]:
        """JSON-safe form handed to an external (``command``) handler."""
        return {
            "source": self.source, "content": self.content, "clear": self.clear,
            "target": self.target, "parsed": self.parsed, "ts": self.ts,
            "session_dir": self.session_dir, "trigger": self.trigger,
        }


@dataclass(frozen=True)
class HookResult:
    decision: Decision
    content: Optional[str] = None
    reason: str = ""
    annotations: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineResult:
    decision: Decision
    event: InjectionEvent
    reason: str = ""
    handler: Optional[str] = None
    trace: Tuple[str, ...] = ()
    annotations: Dict[str, Any] = field(default_factory=dict)


# --- handlers -----------------------------------------------------------------


class HookHandler:
    """Base handler: a matcher (sources / commands) + a decision function."""

    kind = "base"

    def __init__(
        self,
        *,
        name: Optional[str] = None,
        sources: Optional[Iterable[str]] = None,
        commands: Optional[Iterable[str]] = None,
        security: bool = False,
    ) -> None:
        self.name = name or self.kind
        self.sources = frozenset(sources) if sources is not None else None
        self.commands = frozenset(commands) if commands is not None else None
        self.security = bool(security)

    def matches(self, event: InjectionEvent) -> bool:
        if self.sources is not None and event.source not in self.sources:
            return False
        if self.commands is not None:
            cmd = (event.parsed or {}).get("command")
            if cmd not in self.commands:
                return False
        return True

    def decide(self, event: InjectionEvent) -> Optional[HookResult]:  # pragma: no cover - abstract
        raise NotImplementedError


class DenyAll(HookHandler):
    kind = "deny_all"

    def decide(self, event):
        return HookResult(Decision.DENY, reason=f"{self.name}: deny_all")


class DenyPattern(HookHandler):
    kind = "deny_pattern"

    def __init__(self, pattern: str, **kw):
        super().__init__(**kw)
        self.pattern = re.compile(pattern)

    def decide(self, event):
        if self.pattern.search(event.content or ""):
            return HookResult(Decision.DENY, reason=f"{self.name}: content matched {self.pattern.pattern!r}")
        return None


class AllowSources(HookHandler):
    """Deny any source NOT in ``allow`` (the source-allow whitelist)."""

    kind = "allow_sources"

    def __init__(self, allow: Iterable[str], **kw):
        super().__init__(**kw)
        self.allow = frozenset(allow)

    def decide(self, event):
        if event.source not in self.allow:
            return HookResult(Decision.DENY, reason=f"{self.name}: source {event.source!r} not allowed")
        return None


class MaxLen(HookHandler):
    kind = "max_len"

    def __init__(self, max: int, **kw):  # noqa: A002 - manifest key
        super().__init__(**kw)
        if not isinstance(max, int) or max <= 0:
            raise ValueError("max_len 'max' must be a positive integer")
        self.max = max

    def decide(self, event):
        if len(event.content or "") > self.max:
            return HookResult(Decision.DENY, reason=f"{self.name}: {len(event.content)} chars > {self.max}")
        return None


class RateLimit(HookHandler):
    kind = "rate_limit"

    def __init__(self, max_per_minute: int, clock: Callable[[], float] = time.monotonic, **kw):
        super().__init__(**kw)
        if not isinstance(max_per_minute, int) or max_per_minute <= 0:
            raise ValueError("rate_limit 'max_per_minute' must be a positive integer")
        self.max_per_minute = max_per_minute
        self._clock = clock
        self._hits: collections.deque = collections.deque()

    def decide(self, event):
        now = self._clock()
        while self._hits and now - self._hits[0] >= 60.0:
            self._hits.popleft()
        if len(self._hits) >= self.max_per_minute:
            return HookResult(Decision.DENY, reason=f"{self.name}: > {self.max_per_minute}/min")
        self._hits.append(now)
        return None


class DeferToOperator(HookHandler):
    """Hold matching events for operator approval (``\\approve`` / ``\\reject``)."""

    kind = "defer"

    def decide(self, event):
        return HookResult(Decision.DEFER, reason=f"{self.name}: held for operator approval")


class Prefix(HookHandler):
    """MODIFY: prepend a fixed string (e.g. a provenance tag) to the content."""

    kind = "prefix"

    def __init__(self, prefix: str, **kw):
        super().__init__(**kw)
        if not isinstance(prefix, str):
            raise ValueError("prefix 'prefix' must be a string")
        self.prefix = prefix

    def decide(self, event):
        return HookResult(Decision.MODIFY, content=self.prefix + (event.content or ""),
                          reason=f"{self.name}: prefixed")


class CommandHandler(HookHandler):
    """Delegate the decision to an external process — the agentic seam.

    The event's JSON payload is written to the process's stdin; it must print
    one JSON object ``{"decision": "allow|deny|modify|defer", "content"?,
    "reason"?}``. A non-zero exit, a timeout, or unparseable output RAISES, and
    the pipeline applies the failure policy (``security`` -> deny, else allow).
    """

    kind = "command"

    def __init__(self, command, timeout: float = 5.0, **kw):
        super().__init__(**kw)
        if isinstance(command, str):
            argv = shlex.split(command)
        elif isinstance(command, list) and all(isinstance(a, str) for a in command):
            argv = list(command)
        else:
            raise ValueError("command handler 'command' must be a string or list of strings")
        if not argv:
            raise ValueError("command handler 'command' must not be empty")
        self.argv = argv
        self.timeout = float(timeout)

    def decide(self, event):
        proc = subprocess.run(
            self.argv, input=json.dumps(event.to_payload()), capture_output=True,
            text=True, timeout=self.timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"exit {proc.returncode}: {proc.stderr.strip()[:200]}")
        out = json.loads(proc.stdout.strip() or "{}")
        if not isinstance(out, dict):
            raise ValueError("handler output is not a JSON object")
        decision = Decision(str(out.get("decision", "allow")).lower())
        return HookResult(decision, content=out.get("content"), reason=str(out.get("reason", "")),
                          annotations=out.get("annotations") or {})


class Annotate(HookHandler):
    """Lifecycle helper: attach fixed annotations (never vetoes)."""

    kind = "annotate"

    def __init__(self, annotations: Dict[str, Any], **kw):
        super().__init__(**kw)
        if not isinstance(annotations, dict):
            raise ValueError("annotate 'annotations' must be an object")
        self.annotations = dict(annotations)

    def decide(self, event):
        return HookResult(Decision.ALLOW, annotations=self.annotations)


HANDLER_KINDS: Dict[str, type] = {
    cls.kind: cls
    for cls in (DenyAll, DenyPattern, AllowSources, MaxLen, RateLimit, DeferToOperator,
                Prefix, CommandHandler, Annotate)
}

_COMMON_KEYS = {"kind", "name", "sources", "commands", "security"}


def build_handler(spec: Dict[str, Any]) -> HookHandler:
    """One manifest entry -> a handler. Fails LOUD on anything malformed."""
    if not isinstance(spec, dict):
        raise ValueError(f"hook spec must be an object, got {type(spec).__name__}")
    kind = spec.get("kind")
    cls = HANDLER_KINDS.get(kind)  # type: ignore[arg-type]
    if cls is None:
        raise ValueError(f"unknown hook kind {kind!r}; known: {sorted(HANDLER_KINDS)}")
    for key in ("sources", "commands"):
        val = spec.get(key)
        if val is not None and (not isinstance(val, list) or not all(isinstance(v, str) for v in val)):
            raise ValueError(f"hook {key!r} must be a list of strings")
    if spec.get("sources"):
        unknown = set(spec["sources"]) - SOURCES
        if unknown:
            raise ValueError(f"hook 'sources' names unknown source(s) {sorted(unknown)}")
    kwargs = {k: v for k, v in spec.items() if k not in _COMMON_KEYS}
    try:
        return cls(name=spec.get("name"), sources=spec.get("sources"), commands=spec.get("commands"),
                   security=bool(spec.get("security", False)), **kwargs)
    except TypeError as exc:
        raise ValueError(f"bad arguments for hook kind {kind!r}: {exc}") from None


def validate_manifest(manifest: Any) -> Dict[str, List[Dict[str, Any]]]:
    """Normalise + validate a hook manifest ``{point: [handler spec, ...]}``."""
    if manifest is None:
        return {}
    if not isinstance(manifest, dict):
        raise ValueError("hook manifest must be a JSON object {point: [handlers]}")
    out: Dict[str, List[Dict[str, Any]]] = {}
    for point, specs in manifest.items():
        if point.startswith("_"):
            continue  # comment keys
        if point not in POINTS:
            raise ValueError(f"unknown hook point {point!r}; known: {sorted(POINTS)}")
        if not isinstance(specs, list):
            raise ValueError(f"hook point {point!r} must map to a list")
        for s in specs:
            build_handler(s)  # validate now; fail loud at launch, not mid-flight
        out[point] = list(specs)
    return out


def build_handlers(manifest: Optional[Dict[str, Any]], point: str) -> List[HookHandler]:
    norm = validate_manifest(manifest)
    return [build_handler(s) for s in norm.get(point, [])]


# --- the pipeline --------------------------------------------------------------


class HookPipeline:
    """Ordered handlers; DENY/DEFER short-circuit; MODIFY rewrites and continues.

 X26 (requirement-noop-events): a pipeline that knows its hook POINT
    (``point``, one of :data:`POINTS`) reports each dispatch to the feature-usage emitter AFTER the
    decision is computed: one normal hook event when at least one handler matched, else (only while
    ``hook_noop_events`` is ON) one ``noop`` event. The emission is guarded: it can never change
    a decision."""

    def __init__(self, handlers: Sequence[HookHandler] = (), point: Optional[str] = None) -> None:
        self.handlers: List[HookHandler] = list(handlers)
        self.point: Optional[str] = point

    def run(self, event: InjectionEvent) -> PipelineResult:
        consumers = [0]
        result = self._run(event, consumers)
        self._note_dispatch(consumers[0], result)
        return result

    def _note_dispatch(self, consumers: int, result: PipelineResult) -> None:
        if self.point is None:
            return
        try:
            from .. import feature_events as _fe

            if result.decision is Decision.DENY:
                outcome = "denied"
            elif result.decision is Decision.DEFER:
                outcome = "refused"
            elif any(":error" in t for t in result.trace):
                outcome = "error"
            else:
                outcome = "ok"
            _fe.emit_hook_dispatch(self.point, consumers, outcome)
        except Exception:  # noqa: BLE001 - telemetry never changes a decision
            pass

    def report_no_consumers(self) -> None:
        """A call site that skips :meth:`run` because no handler is attached (the operator-command-post
        point) still reports the dispatch as a no-op, so the point is provably reached."""
        if self.point is None:
            return
        try:
            from .. import feature_events as _fe

            _fe.emit_hook_dispatch(self.point, 0, "ok")
        except Exception:  # noqa: BLE001
            pass

    def _run(self, event: InjectionEvent, consumers: List[int]) -> PipelineResult:
        decision = Decision.ALLOW
        trace: List[str] = []
        annotations: Dict[str, Any] = {}
        reason = ""
        for h in self.handlers:
            if not h.matches(event):
                continue
            consumers[0] += 1
            try:
                result = h.decide(event)
            except Exception as exc:  # noqa: BLE001 - the failure policy IS the handling
                if h.security:
                    trace.append(f"{h.name}:error->deny")
                    return PipelineResult(Decision.DENY, event, f"{h.name}: security handler failed closed: {exc}",
                                          h.name, tuple(trace), annotations)
                trace.append(f"{h.name}:error->fail-open")
                continue
            if result is None or result.decision is Decision.ALLOW:
                trace.append(f"{h.name}:allow")
                if result is not None:
                    annotations.update(result.annotations)
                continue
            annotations.update(result.annotations)
            if result.decision is Decision.MODIFY:
                if not isinstance(result.content, str):
                    if h.security:
                        return PipelineResult(Decision.DENY, event, f"{h.name}: MODIFY without content",
                                              h.name, tuple(trace), annotations)
                    trace.append(f"{h.name}:bad-modify->ignored")
                    continue
                event = dataclasses.replace(event, content=result.content)
                decision = Decision.MODIFY
                reason = result.reason
                trace.append(f"{h.name}:modify")
                continue
            trace.append(f"{h.name}:{result.decision.value}")
            return PipelineResult(result.decision, event, result.reason, h.name, tuple(trace), annotations)
        return PipelineResult(decision, event, reason, None, tuple(trace), annotations)


# --- the provenance log --------------------------------------------------------


def _preview(text: Optional[str]) -> Optional[str]:
    if text is None:
        return None
    s = text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"
    return s.encode("unicode_escape", "backslashreplace").decode("ascii")


def content_fields(text: Optional[str]) -> Dict[str, Any]:
    if text is None:
        return {"content_len": None, "content_sha256": None, "content_preview": None}
    try:
        raw = text.encode("utf-8", "surrogateescape")
    except UnicodeEncodeError:
        # r2d (r2c F2): a lone surrogate outside the U+DC80-DCFF escape range is not encodable with
        # surrogateescape; the provenance record of its OWN refusal must not raise.
        raw = text.encode("utf-8", "surrogatepass")
    return {"content_len": len(text), "content_sha256": hashlib.sha256(raw).hexdigest(),
            "content_preview": _preview(text)}


def injection_log_path(session_dir: str) -> str:
    return os.path.join(session_dir, ".claude", INJECTION_LOG_FILENAME)


class ProvenanceLog:
    """Append-only JSONL provenance log (in-memory mirror always kept).

    One ``open(..., "a")`` + write + flush per record (``O_APPEND``), so records
    from this proxy and from a sibling process (e.g. a launch-child CLI logging
    into its launcher's session) interleave whole, never torn. A write failure
    is NOT silent: it is counted in :attr:`write_errors`, reported by
    ``\\status``, and printed once to stderr. It does not block the injection
    (a full disk must not lock the operator out of their own session) — the
    in-memory mirror still holds the record.
    """

    def __init__(self, path: Optional[str] = None, clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self._clock = clock
        self._lock = threading.Lock()
        self._seq = 0
        self.records: List[Dict[str, Any]] = []
        self.write_errors = 0
        self._warned = False
        #: an optional non-blocking observer called with a COPY of every
        #: appended record (the OTel span emitter — ``harness/telemetry.py``). The
        #: ONE telemetry hook: every harness-side record passes here. ``None`` (the
        #: default, and always when ``otel_export`` is off) = no call at all.
        #: A sink that raises is counted in :attr:`sink_errors` and never affects
        #: the record, the file write, or the caller.
        self.sink: Optional[Callable[[Dict[str, Any]], None]] = None
        self.sink_errors = 0
        if path:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def append(self, record: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            ts = self._clock()
            full = {"seq": self._seq, "ts": ts,
                    "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                    "pid": os.getpid()}
            full.update(record)
            self.records.append(full)
            if self.path:
                try:
                    with open(self.path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(full, default=str) + "\n")
                        fh.flush()
                except OSError as exc:
                    self.write_errors += 1
                    if not self._warned:
                        self._warned = True
                        try:
                            sys.stderr.write(f"gnomon harness: provenance log write FAILED ({exc}); "
                                             "records kept in memory only\n")
                        except Exception:  # pragma: no cover
                            pass
            sink = self.sink
            if sink is not None:
                try:
                    sink(dict(full))
                except Exception:  # noqa: BLE001 - telemetry must never touch provenance
                    self.sink_errors += 1
            return full


def append_record(path: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """One-shot append to a provenance log file from another process (e.g. the
    launch-child CLI writing into its launcher's session log)."""
    return ProvenanceLog(path).append(record)


# --- the gate (installed on the single write site) -----------------------------


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    reason: str = ""
    held_id: Optional[int] = None
    record_seq: Optional[int] = None

    @property
    def written(self) -> bool:
        return self.decision in WRITING_DECISIONS


#: Round 5: the dialog name ``_modal_reason`` answers when the dialog reader itself raises (fail closed).
DIALOG_READER_FAILED = "dialog-reader-failed"


class InjectionGate:
    """Pipeline + provenance + the operator's controls, enforced at the writer.

    ``decide(event, emit)`` runs the pipeline ONCE, appends ONE provenance
    record, and calls ``emit(event)`` (which writes the bytes) iff the decision
    is ALLOW/MODIFY. A DENY writes nothing. A DEFER holds ``(event, emit)`` for
    :meth:`approve` / :meth:`reject`.

    * ``hooks_enabled`` — ``\\hooks off`` sets False: content hooks are skipped
      (decision ALLOW, reason ``hooks-off``). Provenance is STILL recorded.
    * ``paused`` — ``\\pause``: every non-operator event is DEFERred until
      ``\\resume`` (which releases the held queue in order).
    """

    def __init__(
        self,
        pipeline: Optional[HookPipeline] = None,
        log: Optional[ProvenanceLog] = None,
        *,
        command_pipeline: Optional[HookPipeline] = None,
        command_post_pipeline: Optional[HookPipeline] = None,
        session_dir: Optional[str] = None,
        instance_id: Optional[str] = None,
        effect_ledger: bool = False,
    ) -> None:
        self.pipeline = pipeline if pipeline is not None else HookPipeline()
        self.command_pipeline = command_pipeline if command_pipeline is not None else HookPipeline()
        self.command_post_pipeline = command_post_pipeline if command_post_pipeline is not None else HookPipeline()
        # X26: name each pipeline's hook point (only when the builder did not) so its dispatches reach the emitter
        for _p, _pt in ((self.pipeline, POINT_INBOUND), (self.command_pipeline, POINT_OPERATOR_COMMAND),
                        (self.command_post_pipeline, POINT_OPERATOR_COMMAND_POST)):
            if getattr(_p, "point", None) is None:
                _p.point = _pt
        self.log = log if log is not None else ProvenanceLog()
        self.session_dir = session_dir
        self.instance_id = instance_id
        self.hooks_enabled = True
        self.paused = False
        #: (flag ``effect_ledger``, default OFF): when True every emit
        #: this gate performs is bracketed by started / completed|failed ``effect``
        #: records (harness/effect_ledger.py). False = the emit is called directly and
        #: NOTHING is written (byte-identical to pre-6C). Set once at assembly.
        self.effect_ledger = bool(effect_ledger)
        self.effect_ledger_errors = 0
        self._effect_warned = False
        self._held: "collections.OrderedDict[int, Tuple[InjectionEvent, Callable[[InjectionEvent], None], str]]" = (
            collections.OrderedDict())
        self._next_held = 1
        self._lock = threading.RLock()
        #: (2026-10-03): ``() -> dialog name | None`` (a
        #: :class:`~gnomon.harness.modal_prompt.ModalGuard`.check). While it reports an open
        #: AskUserQuestion / ExitPlanMode (or screen-seen dialog), EVERY non-operator injection is
        #: DEFERred into the held queue (FIFO) and released once, in order, after it closes: a CR
        #: typed into a dialog selects its first option. ``None`` = no hold (legacy).
        self.modal_check: Optional[Callable[[], Optional[str]]] = None
        #: called after each harness write (the guard's collision detector); never raises.
        self.modal_note_write: Optional[Callable[[], None]] = None
        self._modal_ids: set = set()
        #: ``() -> reason | None``: the operator's-line check a FRESH injection passes (g6 / g27:
        #: ``monitor.line_hold_reason``). A modal-held event is re-checked against it at RELEASE:
        #: the hold decouples decide from write by minutes, and he may have started typing since.
        self.line_hold_check: Optional[Callable[[], Optional[str]]] = None
        #: master_write_nonblocking (ON only): ``() -> "first output" | "timeout" | None``, how the writer's start hold ended. A held
        #: injection whose cause was the start hold says so, and says how it was released, in its deferral and release records.
        self.start_hold_cause: Optional[Callable[[], Optional[str]]] = None
        self._start_held: set = set()     # held ids whose hold cause was the writer's start hold
        #: Dialog-keystroke rule (architect ruling 2026-10-03): keys typed WHILE a dialog is open belong to
        #: the dialog. ``modal_snapshot()`` is called on the closed->open edge and returns an opaque
        #: snapshot of the operator-input tap's line state; ``modal_restore(snapshot)`` is called on the
        #: open->closed edge (only for a dialog the TRANSCRIPT confirmed; a screen-marker-only hold may be a
        #: quoted marker, where the operator's keys are real). A guard whose FIRST observation is "open"
        #: has a snapshot only if the tap had seen no operator input yet (pristine empty line); otherwise
        #: the conservative behaviour stays, and it is recorded.
        self.modal_snapshot: Optional[Callable[[bool], Any]] = None
        #: ``modal_restore(snapshot, boundary)``: ``boundary`` is the :class:`CloseBoundary` (the closing row's
        #: stamp and the replay start = stamp minus the measured-latency margin): keys typed from the replay
        #: start on are replayed onto the restored line, never discarded (D1/E2).
        self.modal_restore: Optional[Callable[[Any, Any], None]] = None
        #: ``() -> CloseBoundary | UNKNOWN_CLOSE | None`` (ModalGuard.close_boundary). ``UNKNOWN_CLOSE`` (a
        #: dialog closed at an unknown instant) FORCE-MARKS the operator line BUSY (E1); ``None`` = no
        #: transcript close was recorded (a screen-only hold).
        self.modal_close_time: Optional[Callable[[], Any]] = None
        #: ``modal_force_busy(cause)``: mark the operator's line BUSY whatever the tap's buffer says (cleared
        #: only by his next Enter / Ctrl-U or ``\\approve --into-dialog``). ``modal_line_state()``: the tap's
        #: line state NOW (read at the open edge, so a close with no restore can tell pre-dialog text existed).
        self.modal_force_busy: Optional[Callable[[str], None]] = None
        self.modal_line_state: Optional[Callable[[], str]] = None
        self._modal_open_line: Optional[str] = None
        #: ``modal_open_time() -> monotonic | None`` (ModalGuard.open_boundary) and
        #: ``modal_input_between(t0, t1) -> bool`` (the tap's journal): at the open edge, keys typed between
        #: the open row's stamp and the guard's first sight of the dialog are AMBIGUOUS (E6).
        self.modal_open_time: Optional[Callable[[], Optional[float]]] = None
        self.modal_input_between: Optional[Callable[[Optional[float], float], bool]] = None
        self._modal_open_ambiguous = False
        self._modal_was_open: Optional[bool] = None
        self._modal_snap: Any = None
        self._modal_snap_ok = False
        self._modal_confirmed = False
        #: a stuck hold (older than ``modal_stuck_after`` seconds) is made VISIBLE, never released:
        #: ``on_modal_stuck(info)`` (operator feedback) + a ``modal-guard`` record + ``self.modal_stuck``.
        self.modal_stuck_after: float = 1800.0
        self._modal_reader_failed = False   # round 5: one record per reader outage
        self.on_modal_stuck: Optional[Callable[[Dict[str, Any]], None]] = None
        #: g79: ``on_operator_broadcast_held(info)`` -> the operator's screen line, fired the moment an
        #: ``operator-broadcast`` is held (dialog / line / queue), so he knows his message is WAITING.
        self.on_operator_broadcast_held: Optional[Callable[[Dict[str, Any]], None]] = None
        #: round 3 P2-2: ``on_operator_broadcast_released(ev, since_epoch)`` fires after a HELD operator
        #: broadcast is typed on release, so the launcher can run the X43 delivery check it skipped at hold time.
        self.on_operator_broadcast_released: Optional[Callable[[InjectionEvent, float], None]] = None
        #: ``() -> None``: the operator forced a release into the dialog; the guard forgets what is open.
        self.modal_dismiss: Optional[Callable[[], None]] = None
        self._modal_clock: Callable[[], float] = time.monotonic
        self._modal_since: Dict[int, float] = {}
        self._modal_why: Dict[int, str] = {}
        self._line_noted: set = set()
        self._line_check_failing = False   # one modal-guard record per line-reader outage (round 3 P2-4)
        self.modal_stuck: Optional[Dict[str, Any]] = None

    # -- records -------------------------------------------------------------

    def record(self, kind: str, **fields: Any) -> Dict[str, Any]:
        rec = {"kind": kind, "instance_id": self.instance_id}
        rec.update(fields)
        return self.log.append(rec)

    def _event_record(self, event: InjectionEvent, decision: str, reason: str, **extra: Any) -> Dict[str, Any]:
        fields = {
            "source": event.source, "trigger": event.trigger, "target": event.target,
            "clear": event.clear, "decision": decision, "reason": reason,
            "parsed_command": (event.parsed or {}).get("command"),
            "hooks_enabled": self.hooks_enabled, "paused": self.paused,
        }
        fields.update(content_fields(event.content))
        for k, v in (event.meta or {}).items():
            if k not in fields and isinstance(v, (str, int, float, bool, type(None))):
                fields[k] = v
        fields.update(extra)
        return self.record("injection", **fields)

    # -- the ONE emit chokepoint ------------------------------------

    def _emit(self, ev: InjectionEvent, emit: Callable[[InjectionEvent], None], *, route: str,
              decision_seq: Optional[int], held_id: Optional[int] = None) -> None:
        """Every harness write to the PTY passes here (decide + approve both call it).
        Flag off: a plain ``emit(ev)``. Flag on: the effect ledger brackets it."""
        try:
            if not self.effect_ledger:
                emit(ev)
                return
            from . import effect_ledger as _el

            def _on_err(exc: BaseException) -> None:
                self.effect_ledger_errors += 1
                if not self._effect_warned:
                    self._effect_warned = True
                    sys.stderr.write(f"gnomon harness: effect ledger write FAILED ({exc}); "
                                     "the injection proceeds unledgered\n")

            def _checked_record(kind: str, **fields: Any) -> Dict[str, Any]:
                # ProvenanceLog.append SWALLOWS OSError (counting log.write_errors), so a real
                # disk fault never raises here: detect it by that counter moving (F1, 6C r2).
                # Under concurrent writers another thread's fault may be attributed to this
                # record: the counter is a fault ALARM, not a per-record verdict.
                before = self.log.write_errors
                rec = self.record(kind, **fields)
                if self.log.write_errors > before:
                    _on_err(OSError(f"provenance log write failed ({fields.get('phase')})"))
                return rec

            _el.run_effect(_checked_record, ev, emit, route=route, decision_seq=decision_seq,
                           held_id=held_id, on_ledger_error=_on_err)
        finally:
            # tell the modal guard a harness write just happened
            # (its dialog-after-write collision detector); never raises.
            nw = self.modal_note_write
            if nw is not None:
                try:
                    nw()
                except Exception:  # noqa: BLE001
                    pass

    def run_ledgered(self, ev: InjectionEvent, emit: Callable[[InjectionEvent], None], *, route: str) -> None:
        """Ledger a harness-originated PTY write that is NOT a gate decision (today only
        ``optap.restore_unsent``, route ``restore``): same started / completed|failed
        bracket as :meth:`_emit`, no decision record. Flag off: a plain ``emit(ev)``.

        ⛔ This BRACKETS an already-authorised write; it is NOT a gate decision. It does
        not run the hook pipeline, does not honour ``\\pause``, and cannot deny or defer.
        NEVER use it to write text the gate would hold or deny: such text goes through
        ``writer.admit`` / ``injector.inject_line``. (``restore_unsent`` is deliberately
        ungated: it puts back the operator's OWN unsent text, D50; making this
        pause-aware would change D50.) ``route`` must be one of ``effect_ledger.ROUTES``
        (``ValueError`` otherwise); the callers are pinned by an AST test."""
        from . import effect_ledger as _el

        if route not in _el.ROUTES:
            raise ValueError(f"unknown effect route {route!r}; known: {sorted(_el.ROUTES)}")
        self._emit(ev, emit, route=route, decision_seq=None)

    # -- the decision --------------------------------------------------------

    def evaluate(self, event: InjectionEvent, pipeline: Optional[HookPipeline] = None) -> PipelineResult:
        """Run the content hooks (unless ``\\hooks off``) + the pause gate."""
        pipeline = pipeline if pipeline is not None else self.pipeline
        # operator ruling a leading '!' puts Claude Code in bash mode. UNCONDITIONAL
        # (no flag, not subject to \hooks off or \pause) and for every source but the operator's
        # own keystrokes (SOURCE_OPERATOR): the ONE decision point every gated injection passes.
        if event.source != SOURCE_OPERATOR:
            early = _LB.screen_event(event.content, event.meta)
            if early:
                return PipelineResult(Decision.DENY, event, _LB.gate_reason(early), early[0])
        if self.paused and event.source != SOURCE_OPERATOR:
            return PipelineResult(Decision.DEFER, event, "paused by operator (\\resume to release)", "pause")
        if not self.hooks_enabled:
            result = PipelineResult(Decision.ALLOW, event, "hooks-off", None)
        else:
            result = pipeline.run(event)
        # Round 2b (Diamond r2 F1, g71-amendment-r2): screen the FINAL content. A hook MODIFY
        # handler (hooks.json is agent-writable) can rewrite a clean payload into one that starts
        # with '!' AFTER the check above; whatever the pipeline hands back is what gets typed, so it
        # is re-screened here for every source but the operator's own keystrokes. A DEFER is
        # screened too: nothing that would release as a bang is ever held for approval.
        if result.decision is not Decision.DENY and event.source != SOURCE_OPERATOR:
            final = _LB.screen_event(result.event.content, result.event.meta)
            if final:
                return PipelineResult(Decision.DENY, result.event, _LB.gate_reason(final), final[0],
                                      tuple(result.trace) + (_LB.POST_TRANSFORM_TRACE,), result.annotations)
        return result

    # -- the modal-dialog hold -----------------------------------

    def _modal_reason(self) -> Optional[str]:
        """The open dialog's name, or ``None`` when no dialog is open. FAIL CLOSED (round 5, direction
        rule): a dialog reader that RAISES answers :data:`DIALOG_READER_FAILED` ("a dialog is open"), so
        every consumer (fresh injects, releases, the takeover compact, the restore, bus, broadcast) waits;
        one ``modal-guard`` record per outage, and normal flow resumes when the reader answers again. A
        permanently-raising reader surfaces as the visible stuck-hold. (A reader that answers ``None``
        because it cannot SEE, e.g. a transcript gap, is the separate recorded fail-open.)"""
        chk = self.modal_check
        if chk is None:
            return None
        try:
            reason = chk() or None
        except Exception as exc:  # noqa: BLE001 - an unreadable guard is treated as "a dialog is open"
            if not self._modal_reader_failed:
                self._modal_reader_failed = True
                try:
                    self.record("modal-guard", what="dialog-reader-raised-fail-closed",
                                error=f"{type(exc).__name__}: {exc}",
                                detail="the dialog reader raised; treated as an open dialog (nothing is typed, "
                                       "held items wait) until it answers again; \\approve --into-dialog "
                                       "is the operator override")
                except Exception:  # noqa: BLE001
                    pass
            return DIALOG_READER_FAILED   # no edge update: the last real open/close state is kept
        if self._modal_reader_failed:
            self._modal_reader_failed = False
            try:
                self.record("modal-guard", what="dialog-reader-recovered")
            except Exception:  # noqa: BLE001
                pass
        self._modal_edge(reason)
        return reason

    def _modal_edge(self, reason: Optional[str]) -> None:
        """Track the closed->open / open->closed edges for the dialog-keystroke rule (see
        ``modal_snapshot``). Never raises."""
        try:
            is_open = reason is not None
            was = self._modal_was_open
            self._modal_was_open = is_open
            if is_open and not str(reason).endswith("(screen)"):
                self._modal_confirmed = True
            if is_open and not was:
                self._modal_confirmed = not str(reason).endswith("(screen)")
                self._modal_snap, self._modal_snap_ok = None, False
                self._take_close_time()   # drop close records left by a dialog that opened and closed unseen
                self._modal_open_line = self._read_line_state()
                self._modal_open_ambiguous = self._read_open_ambiguity()
                if self.modal_snapshot is not None:
                    # first observation (was is None): the snapshot is valid only if the tap has seen no
                    # operator input since launch (its line is provably the pristine empty one); the
                    # callable returns None when it cannot vouch for that.
                    snap = self.modal_snapshot(was is None)
                    if snap is not None:
                        self._modal_snap, self._modal_snap_ok = snap, True
                    elif was is None:
                        self.record("modal-guard", what="line-snapshot-unavailable-started-mid-dialog",
                                    detail="the first observation was an open dialog and the operator had "
                                           "already typed; keys typed into it stay counted as a busy line "
                                           "(conservative)")
            elif not is_open and not was:
                # no dialog open, none was: a closing row that appears NOW belongs to a dialog that opened AND
                # closed between two polls (the guard never held anything). Keys typed in that window may have
                # answered it, and its Enter resets the tap's view over text still in the child's prompt.
                bnd = self._take_close_time()
                if bnd is not None and getattr(bnd, "from_transcript", False) and self.modal_input_between is not None:
                    lo = getattr(bnd, "replay_from", None)
                    try:
                        near = bool(self.modal_input_between(lo, time.monotonic()))
                    except Exception:  # noqa: BLE001
                        near = True
                    if near:
                        self._force_busy("dialog-opened-and-closed-unseen")
                        self.record("modal-guard", what="line-marked-busy-dialog-unseen",
                                    detail="a dialog opened and closed between two polls and the operator typed "
                                           "in that window: his line is marked BUSY until his next Enter / "
                                           "Ctrl-U")
            elif was and not is_open:
                bnd = self._take_close_time()
                unknown = bnd is not None and getattr(bnd, "stamp", None) is None
                restorable = self._modal_snap_ok and self._modal_confirmed and self.modal_restore is not None
                if unknown:
                    # a dialog closed but the closing row's time is unusable (absent, unparseable, in the
                    # future): the tap's own buffer cannot be trusted either way (the dialog's own Enter may
                    # have reset it over text still in the child's prompt), so the line is BUSY, explicitly,
                    # until his next Enter / Ctrl-U / \approve --into-dialog. A held item waits.
                    self._force_busy("dialog-close-time-unknown")
                    self.record("modal-guard", what="line-restore-skipped-close-time-unknown",
                                detail="could not determine when the dialog closed (the closing row's "
                                       "timestamp is absent, unparseable or in the future); the operator "
                                       "line is marked BUSY until his next Enter / Ctrl-U or "
                                       "\\approve --into-dialog")
                else:
                    restored = restorable and bnd is not None
                    if restored:
                        self.modal_restore(self._modal_snap, bnd)
                        self.record("modal-guard", what="line-restored-after-dialog")
                    if self._modal_open_ambiguous:
                        # keys were typed between the dialog's open row and the guard's first sight of it: the
                        # tap cannot tell pre-dialog text from the dialog's own keys, so after the close it
                        # cannot vouch the child's prompt is empty (a dialog Enter resets its view over text
                        # still there). BUSY until his next Enter / Ctrl-U.
                        self._force_busy("keys-typed-before-the-dialog-was-seen")
                        self.record("modal-guard", what="line-marked-busy-keys-before-dialog-seen",
                                    detail="operator keys fell between the dialog's open row and the guard's "
                                           "first observation of it; the line is marked BUSY until his next "
                                           "Enter / Ctrl-U")
                    elif not restored and self._modal_open_line not in (None, "empty"):
                        # no restore (no snapshot / a screen-only hold / no transcript close) and the line
                        # held or might have held text when the dialog opened: a dialog Enter would reset the
                        # tap's view of it while the child still holds it -> BUSY (conservative).
                        self._force_busy("dialog-closed-without-snapshot-line-was-" + str(self._modal_open_line))
                        self.record("modal-guard", what="line-marked-busy-no-snapshot",
                                    detail="the dialog closed with no usable line snapshot and the line was not "
                                           "empty when it opened; marked BUSY until his next Enter / Ctrl-U")
                self._modal_snap, self._modal_snap_ok, self._modal_confirmed = None, False, False
                self._modal_open_line = None
        except Exception as exc:  # noqa: BLE001
            try:
                self.record("modal-guard", what="line-edge-error", error=f"{type(exc).__name__}: {exc}")
            except Exception:  # noqa: BLE001
                pass

    def _take_close_time(self) -> Any:
        f = self.modal_close_time
        if f is None:
            return None
        try:
            return f()
        except Exception:  # noqa: BLE001
            from .modal_prompt import UNKNOWN_CLOSE
            return UNKNOWN_CLOSE      # the boundary reader failed: unknown, so BUSY (never "nothing happened")

    def _read_line_state(self) -> Optional[str]:
        f = self.modal_line_state
        if f is None:
            return None
        try:
            return f()
        except Exception:  # noqa: BLE001
            return "unknown"

    def _read_open_ambiguity(self) -> bool:
        """True when operator keys may have been typed before the guard saw the dialog but after it opened."""
        fo, fb = self.modal_open_time, self.modal_input_between
        if fb is None:
            return False
        try:
            t_open = fo() if fo is not None else None
            return bool(fb(None if t_open is None else t_open - 0.005, time.monotonic()))
        except Exception:  # noqa: BLE001
            return True      # cannot tell: assume ambiguous (conservative)

    def _force_busy(self, cause: str) -> None:
        f = self.modal_force_busy
        if f is None:
            return
        try:
            f(cause)
        except Exception:  # noqa: BLE001
            pass

    def modal_held_ids(self) -> List[int]:
        """Ids held for an open dialog (or queued behind one / waiting for the operator's line)."""
        with self._lock:
            return sorted(i for i in self._modal_ids if i in self._held)

    def _line_reason(self) -> Optional[str]:
        chk = self.line_hold_check
        if chk is None:
            return None
        try:
            out = chk() or None
        except Exception as exc:  # noqa: BLE001
            # FAIL CLOSED (g79(5), round 3 P2-4): a reader that cannot answer is "line busy / unknown", never
            # "empty": the write is HELD, and ONE modal-guard record per outage says why.
            if not self._line_check_failing:
                self._line_check_failing = True
                self.record("modal-guard", what="line-check-failed", fail="closed",
                            error=f"{type(exc).__name__}: {exc}",
                            detail="the operator-line reader raised; non-operator writes are HELD until it answers")
            return "line check failed: reader raised (held, fail closed)"
        self._line_check_failing = False
        return out

    def _dialog_queued(self) -> bool:
        """True when some held item waits for an open DIALOG (not merely for the operator's line)."""
        return any(self._modal_why.get(i) == "dialog" for i in self._modal_ids if i in self._held)

    def _check_modal_stuck(self) -> None:
        """Raise (once per episode) a VISIBLE stuck-hold state when the oldest modal-held event has
        waited past ``modal_stuck_after``; clear it when the queue empties. NEVER releases anything."""
        ids = [i for i in self._modal_ids if i in self._held]
        if not ids:
            if self.modal_stuck is not None:
                self.modal_stuck = None
                self.record("modal-guard", what="stuck-hold-cleared")
            return
        if self.modal_stuck is not None:
            self.modal_stuck["held"] = len(ids)
            return
        now = self._modal_clock()
        oldest = min(ids, key=lambda i: self._modal_since.get(i, now))
        waited = now - self._modal_since.get(oldest, now)
        if waited < self.modal_stuck_after:
            return
        why = self._modal_why.get(oldest, "dialog")
        info = {"held": len(ids), "held_seconds": round(waited, 1), "since_ts": time.time() - waited,
                "why": why, "ids": sorted(ids),
                "label": (f"{len(ids)} injection(s) HELD {int(waited // 60)} min: "
                          + (("the dialog reader is FAILING (treated as a dialog)" if self._modal_reader_failed
                              else "a dialog may still be open") if why == "dialog"
                             else "the operator's line is busy")
                          + " (\\approve --into-dialog to force, \\reject to drop)")}
        self.modal_stuck = info
        self.record("modal-guard", what="stuck-hold", **info)
        cb = self.on_modal_stuck
        if cb is not None:
            try:
                cb(dict(info))
            except Exception:  # noqa: BLE001 - visibility never breaks the pump
                pass

    def release_modal_holds(self) -> List[int]:
        """Release the modal-held events, oldest first, once no dialog is open (the proxy ticks this;
        ``decide`` also calls it first so a new injection never jumps the queue). While the operator has
        ``\\pause``d they stay held for ``\\resume``. Stops at the first event that finds a dialog open
        again OR the operator's line busy (order preserved). Also the stuck-hold watchdog: it makes a
        long hold visible, it never releases."""
        released: List[int] = []
        if self.modal_check is not None:
            self._modal_reason()  # observe the open/close edges even with an empty queue
        if not self._modal_ids:
            if self.modal_stuck is not None:
                self._check_modal_stuck()  # the queue emptied (released / rejected): clear the visible state
            return released
        if self.paused:
            return released
        for i in sorted(self._modal_ids):
            if i not in self._held:
                self._modal_ids.discard(i)
                continue
            if self._modal_reason() is not None:
                break
            released.extend(self.approve(i, reason="modal-closed"))
            if i in self._held:
                break  # approve re-checked and found a dialog / a busy line again: the rest stay queued
        if self._modal_ids and self._modal_reason() is None:
            # Round 4 (P1-1): no dialog is open now, so NO item may keep a stale ``dialog`` tag: every item
            # still queued waits for the operator's line, which is what ``_dialog_queued()`` must report.
            for j in self._modal_ids:
                if j in self._held and self._modal_why.get(j) == "dialog":
                    self._modal_why[j] = "line"
        self._check_modal_stuck()
        return released

    @staticmethod
    def _modal_exempt(ev: InjectionEvent) -> bool:
        """The operator's own text, and the folder-trust accepter (its whole purpose is to answer that
        first-run dialog). The accepter is recognised by CALL PATH, not by a string: it stamps the
        event with :data:`FOLDER_TRUST_CAP`, a process-local object that cannot be carried in a JSON
        command frame, and its source must be ``bootstrap`` (a hub frame cannot claim that source).
        A frame whose ``trigger`` merely says ``folder-trust-accept`` is held like any other."""
        if ev.source == SOURCE_OPERATOR:
            return True
        return (ev.source == SOURCE_BOOTSTRAP and isinstance(ev.meta, dict)
                and ev.meta.get(FOLDER_TRUST_CAP_KEY) is FOLDER_TRUST_CAP)

    @staticmethod
    def _line_exempt(ev: InjectionEvent) -> bool:
        """Never line-held: the operator's own text, the folder-trust accepter (both via ``_modal_exempt``,
        by call path) and the D50 takeover compact (:data:`LINE_TAKEOVER_CAP`, by call path, source
        ``self-verb`` only). EVERYTHING else, every source, is held while his line is busy."""
        if InjectionGate._modal_exempt(ev):
            return True
        return (ev.source == SOURCE_SELF_VERB and isinstance(ev.meta, dict)
                and ev.meta.get(LINE_TAKEOVER_CAP_KEY) is LINE_TAKEOVER_CAP)

    def _notify_broadcast_held(self, ev: InjectionEvent, held_id: int, why: str) -> None:
        cb = self.on_operator_broadcast_held
        if cb is None or ev.source != SOURCE_OPERATOR_BROADCAST:
            return
        try:
            cb({"held_id": held_id, "why": why, "from": (ev.meta or {}).get("from_instance"),
                "msg_id": (ev.meta or {}).get("msg_id"),
                "kind": self._modal_why.get(held_id, "line")})
        except Exception:  # noqa: BLE001 - visibility never breaks the gate
            pass

    def decide(self, event: InjectionEvent, emit: Callable[[InjectionEvent], None]) -> Outcome:
        exempt0 = self._modal_exempt(event)
        if not exempt0 and self._modal_ids:
            self.release_modal_holds()  # the queue goes first (order preserved)
        with self._lock:
            result = self.evaluate(event)
            ev = result.event
            if result.decision in WRITING_DECISIONS and not (exempt0 or self._modal_exempt(ev)):
                modal = self._modal_reason()
                behind = bool(self._modal_ids)
                # g79 direction rule (2026-10-03): EVERY non-operator source is a harness write (g6 / g27) and
                # never types onto the operator's unsent / unknown line: the default is HELD. Exempt (by call
                # path, not by a frame field): the operator's keystrokes, the folder-trust accepter, the D50
                # takeover compact. Held in THIS queue (FIFO, released in order by ``release_modal_holds`` once
                # the line is clear, re-checked at release, visible as a stuck-hold). A reader failure fails
                # toward HELD (``_line_reason`` reads the monitor, which reports ``unknown`` on error).
                takeover = self._line_exempt(ev)   # not modal-exempt (checked above): only the D50 takeover cap
                if takeover:
                    # Round 3 P1-1: the takeover saves/restores the operator's line itself, so line-held items
                    # queued ahead of it do not block it. A DIALOG (open now, or items queued for one) does:
                    # then it is DROPPED with a record (never queued: it must not type later, unprompted, after
                    # the operator clears his line); the actuator cancels the takeover and his saved line stays.
                    if modal is not None or self._dialog_queued():
                        why = ("D50 takeover compact DROPPED: " + (f"an interactive {modal} dialog is open"
                               if modal is not None else "injections are held for an open dialog")
                               + "; it is never queued behind a dialog (it would type later, unprompted)")
                        rec = self._event_record(ev, Decision.DENY.value, why, handler="modal-hold",
                                                 trace=list(result.trace) + ["takeover-dropped"],
                                                 modal=modal or "queued")
                        self.record("modal-guard", what="takeover-dropped", modal=modal or "queued")
                        return Outcome(Decision.DENY, why, None, rec["seq"])
                    behind = False
                line = (self._line_reason() if (modal is None and not (exempt0 or takeover)) else None)
                if modal is not None or behind or line is not None:
                    held_id = self._next_held
                    self._next_held += 1
                    if modal is not None:
                        why = f"agent is waiting on an interactive {modal} dialog (a typed CR would answer it)"
                    elif behind:
                        why = "queued behind injections held for an open dialog or the operator's line"
                    elif line.startswith(_START_HOLD_PREFIX):
                        why = f"{line} (source {ev.source!r}; the operator's line is not the cause)"
                        self._start_held.add(held_id)
                    else:
                        why = f"operator's line is busy ({line}): an injection from source {ev.source!r} never types onto it"
                    self._held[held_id] = (ev, emit, why)
                    self._modal_ids.add(held_id)
                    self._modal_since[held_id] = self._modal_clock()
                    # "dialog" only when a dialog is (or was last seen) the reason; items queued behind
                    # LINE-held items are line-held too (so a takeover is not wrongly dropped, round 3 P1-1)
                    self._modal_why[held_id] = ("dialog" if (modal is not None or (behind and self._dialog_queued()))
                                                else "line")
                    rec = self._event_record(ev, Decision.DEFER.value, why, handler="modal-hold",
                                             held_id=held_id, trace=list(result.trace) + ["modal-hold"],
                                             modal=modal or "queued", **({"line_hold": line} if line else {}))
                    self._notify_broadcast_held(ev, held_id, why)
                    return Outcome(Decision.DEFER, why, held_id, rec["seq"])
            if result.decision is Decision.DEFER:
                held_id = self._next_held
                self._next_held += 1
                self._held[held_id] = (ev, emit, result.reason)
                rec = self._event_record(ev, Decision.DEFER.value, result.reason, handler=result.handler,
                                         held_id=held_id, trace=list(result.trace))
                return Outcome(Decision.DEFER, result.reason, held_id, rec["seq"])
            rec = self._event_record(ev, result.decision.value, result.reason, handler=result.handler,
                                     trace=list(result.trace), **({"hook_annotations": result.annotations}
                                                                   if result.annotations else {}))
        if _LB.POST_TRANSFORM_TRACE in result.trace:
            self._record_post_transform_refusal(ev, result)
        if result.decision in WRITING_DECISIONS:
            self._emit(ev, emit, route="decide", decision_seq=rec["seq"])
        return Outcome(result.decision, result.reason, None, rec["seq"])

    def _record_post_transform_refusal(self, ev: InjectionEvent, result: PipelineResult) -> None:
        """The loud half of a post-transform refusal (round 2b): the same ``inject-refused``
        provenance kind the screens write, naming the stage and the handler trace that produced
        the '!'. Never raises (a log fault must not turn a refusal into a write)."""
        try:
            reasons = _LB.screen_event(ev.content, ev.meta) or [result.handler or _LB.REASON]
            self.record("inject-refused", source=ev.source, reason="; ".join(reasons), reasons=reasons,
                        stage="post-transform", trace=list(result.trace), rule=_LB.RULE,
                        detail=_LB.gate_reason(reasons), **content_fields(ev.content))
        except Exception:  # noqa: BLE001
            pass

    # -- operator controls ---------------------------------------------------

    def set_hooks_enabled(self, enabled: bool, by: str = SOURCE_OPERATOR) -> None:
        self.hooks_enabled = bool(enabled)
        self.record("control", control="hooks", value="on" if enabled else "off", by=by)

    def set_paused(self, paused: bool, by: str = SOURCE_OPERATOR) -> List[int]:
        self.paused = bool(paused)
        self.record("control", control="pause", value=self.paused, by=by)
        if not self.paused:
            return self.approve("all", reason="resume")
        return []

    def held(self) -> List[Tuple[int, InjectionEvent, str]]:
        with self._lock:
            return [(i, ev, why) for i, (ev, _e, why) in self._held.items()]

    def _select(self, which: Any) -> List[int]:
        with self._lock:
            if which in (None, "", "all"):
                return list(self._held)
            try:
                i = int(which)
            except (TypeError, ValueError):
                return []
            return [i] if i in self._held else []

    def approve(self, which: Any = "all", reason: str = "approved", *, force: bool = False) -> List[int]:
        """Release held injection(s) in order. ``force`` is the OPERATOR's explicit
        ``\\approve --into-dialog``: it releases even into an open dialog (and tells the guard to forget
        the dialog that is open now). Every other release (``\\resume``, modal-closed, a plain
        ``\\approve``) re-checks first: a dialog still open keeps the item (and, in order, the rest)
        queued; and a MODAL-held item is also re-checked against the operator's line exactly like a
        fresh injection (g6 / g27) and waits while he is mid-line."""
        released: List[int] = []
        dismissed = False
        for i in self._select(which):
            if force:
                if not dismissed and i in self._modal_ids and self.modal_dismiss is not None:
                    dismissed = True
                    try:
                        self.modal_dismiss()
                    except Exception:  # noqa: BLE001
                        pass
            else:
                with self._lock:
                    peek = self._held.get(i)
                if peek is not None and not self._modal_exempt(peek[0]):
                    if self._modal_reason() is not None:
                        self._modal_ids.add(i)  # held for the dialog now, whatever held it before
                        self._modal_since.setdefault(i, self._modal_clock())
                        self._modal_why[i] = "dialog"
                        if reason == "modal-closed":
                            break
                        continue
                    if i in self._modal_ids and not self._line_exempt(peek[0]):
                        busy = self._line_reason()
                        if busy is not None:
                            self._modal_why[i] = "line"
                            if i not in self._line_noted:
                                self._line_noted.add(i)
                                self.record("modal-guard",
                                            what=("release-waits-for-start-hold" if str(busy).startswith(_START_HOLD_PREFIX)
                                                  else "release-waits-for-operator-line"),
                                            held_id=i, reason=busy)
                            if reason == "modal-closed":
                                break
                            continue
            with self._lock:
                entry = self._held.pop(i, None)
            self._modal_ids.discard(i)
            self._modal_since.pop(i, None)
            self._modal_why.pop(i, None)
            self._line_noted.discard(i)
            was_start_held = i in self._start_held
            self._start_held.discard(i)     # whatever happens to the item below, its start-hold mark is spent
            if entry is None:
                continue
            ev, emit, _why = entry
            held_refusal = (_LB.screen_event(ev.content, ev.meta) if ev.source != SOURCE_OPERATOR else None)
            if held_refusal:
                # defence in depth (round 2b/2c): a held event is never released as a bang or with
                # control bytes (the union screen, the same function the gate uses)
                self._event_record(ev, Decision.DENY.value, _LB.gate_reason(held_refusal), held_id=i,
                                   handler=held_refusal[0], trace=[_LB.POST_TRANSFORM_TRACE])
                self._record_post_transform_refusal(ev, PipelineResult(Decision.DENY, ev, _LB.gate_reason(held_refusal),
                                                                       held_refusal[0], (_LB.POST_TRANSFORM_TRACE,)))
                continue
            if was_start_held:
                cause = None
                if self.start_hold_cause is not None:
                    try:
                        cause = self.start_hold_cause()
                    except Exception:  # noqa: BLE001
                        cause = None
                if cause is None and force:
                    reason = (f"{reason}; held for the child's start; released early by the operator's forced approve "
                              "(the start hold had not ended)")
                else:
                    reason = (f"{reason}; held until the child's first output after start; released by "
                              f"{cause or 'the end of the start hold'}")
            rel = self._event_record(ev, "release", reason, held_id=i)
            since = time.time()
            try:
                self._emit(ev, emit, route="release", decision_seq=rel["seq"], held_id=i)
            except _PendingCapRefused as exc:
                # master_write_nonblocking (ON only): the writer refused this injection whole AFTER the release record above; say
                # so in the log, then let the refusal propagate as before.
                self.record("modal-guard", what="release-refused-by-pending-cap", held_id=i, reason=str(exc))
                raise
            if ev.source == SOURCE_OPERATOR_BROADCAST and self.on_operator_broadcast_released is not None:
                try:
                    self.on_operator_broadcast_released(ev, since)
                except Exception:  # noqa: BLE001 - a check never breaks the release
                    pass
            released.append(i)
        return released

    def reject(self, which: Any = "all", reason: str = "rejected by operator") -> List[int]:
        rejected: List[int] = []
        for i in self._select(which):
            with self._lock:
                entry = self._held.pop(i, None)
            self._modal_ids.discard(i)
            self._modal_since.pop(i, None)
            self._modal_why.pop(i, None)
            self._line_noted.discard(i)
            self._start_held.discard(i)
            if entry is None:
                continue
            self._event_record(entry[0], "reject", reason, held_id=i)
            rejected.append(i)
        return rejected

    def status(self) -> Dict[str, Any]:
        return {
            "hooks_enabled": self.hooks_enabled, "paused": self.paused,
            "held": [{"id": i, "source": ev.source, "reason": why, **content_fields(ev.content)}
                     for i, ev, why in self.held()],
            "handlers": [h.name for h in self.pipeline.handlers],
            "log_path": self.log.path, "log_records": len(self.log.records),
            "log_write_errors": self.log.write_errors,
        }


# --- lifecycle points (WS-E) ----------------------------------------------------


@dataclass(frozen=True)
class LifecycleResult:
    point: str
    vetoed: bool
    reason: str = ""
    annotations: Dict[str, Any] = field(default_factory=dict)
    fired: bool = True


class LifecycleBus:
    """``agent-start`` / ``agent-end`` / ``compact`` — each fires ONCE per lifecycle.

    Handlers are the same kinds as injection hooks; the event's ``content`` is
    the JSON-serialised payload and ``source`` is ``bootstrap`` for start/end.
    A DENY/DEFER on ``agent-start`` is a VETO (the launcher refuses / tears
    down the child). ``compact`` fires per phase (``pre`` / ``post``) and is
    observational — it can annotate, not veto.
    """

    def __init__(self, manifest: Optional[Dict[str, Any]] = None, gate: Optional[InjectionGate] = None) -> None:
        self._pipelines = {p: HookPipeline(build_handlers(manifest, p), point=p)
                           for p in (POINT_AGENT_START, POINT_AGENT_END, POINT_COMPACT)}
        self._gate = gate
        self._fired: set = set()

    def fire(self, point: str, payload: Dict[str, Any], *, once_key: Optional[str] = None) -> LifecycleResult:
        key = once_key or point
        if key in self._fired:
            return LifecycleResult(point, False, "already fired", fired=False)
        self._fired.add(key)
        event = InjectionEvent(source=SOURCE_BOOTSTRAP, content=json.dumps(payload, default=str),
                               trigger=point, meta={})
        result = self._pipelines[point].run(event) if point in self._pipelines else PipelineResult(
            Decision.ALLOW, event)
        vetoed = point == POINT_AGENT_START and result.decision in (Decision.DENY, Decision.DEFER)
        if self._gate is not None:
            self._gate.record("lifecycle", point=point, payload=payload, decision=result.decision.value,
                              vetoed=vetoed, reason=result.reason, handler=result.handler,
                              hook_annotations=result.annotations or None)
        return LifecycleResult(point, vetoed, result.reason, dict(result.annotations))

    def fired(self, key: str) -> bool:
        return key in self._fired

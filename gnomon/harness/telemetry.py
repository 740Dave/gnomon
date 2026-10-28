"""OpenTelemetry export, switchable.

Telemetry leaving the machine is PUBLISHING, so everything here is OFF unless
asked for twice: the flag AND a configured endpoint.

The switch (acceptance-1: ``telemetry: as-is | otlp``)
------------------------------------------------------
* ``otel_export`` OFF (the default) = **as-is**: the child env is
  byte-identical, no span is emitted, no record is written. Nothing changes.
* ON, and ``otel_endpoint`` set (launch.json > machine config
  ``~/gnomon-data/config.json``) = **otlp**.
* ON but no (or a malformed) endpoint = NOTHING is exported, and the launcher
  says so on the operator's terminal and in the provenance log.

Two producers (both ours to configure)
--------------------------------------
1. **Claude Code's native OTel** (acceptance-2) — :func:`apply_child_env` puts
   ``CLAUDE_CODE_ENABLE_TELEMETRY=1`` + OTLP metrics + logs pointed at the
   endpoint, with ``OTEL_RESOURCE_ATTRIBUTES=gm.instance=…,gm.session=…``, on the
   child. When it owns export it owns the whole ``OTEL_*`` namespace: inherited
   ``OTEL_*`` (a signal-specific endpoint that would redirect, headers carrying a
   credential for some other backend, beta tracing) are dropped first.
   Every native CONTENT switch is pinned to ``0`` — measured 2026-09-29 against
   claude 2.1.284 (``proposals/otel-capture-inventory-2026-09-29.md``):
   ``OTEL_LOG_USER_PROMPTS=1`` ALSO exports the assistant's visible reply text
   (``assistant_response.response``) unless ``OTEL_LOG_ASSISTANT_RESPONSES=0``;
   ``OTEL_LOG_RAW_API_BODIES`` would export whole request bodies (system prompt,
   conversation, tool results). ``otel_log_prompts`` turns ON user-prompt
   text ONLY (assistant responses stay pinned off).
2. **The harness's own events** (acceptance-3) — :class:`HarnessTelemetry` is
   the ``sink`` on :class:`~gnomon.harness.hooks.ProvenanceLog`, the ONE
   choke point every harness-side record passes (injections with
   source/trigger/decision, compact-gate decisions, line-hold holds/releases,
   hub-link transitions, quiescent enter/exit, lifecycle, operator commands).
   Each record becomes one instantaneous span named ``gm.<kind>`` carrying
   POINTERS ONLY: an allowlist of enumerated/identifier fields, the content
   sha256/len (never the content or its preview), the provenance seq + log path,
   ``gm.session``/``gm.instance``, and when known ``gm.transcript_path`` + the
   transcript's latest message uuid. Free text (``reason``, previews, payloads)
   is NOT exported — follow ``gm.provenance_seq`` into the local log for it.
   Non-blocking by construction: the sink is a bounded ``put_nowait`` (full =
   drop + count); a worker thread builds spans into an OTel
   ``BatchSpanProcessor`` whose exporter runs on its own thread with a short
   timeout. A dead collector never stalls the pump and never raises.

Logfire (acceptance-4): a documented, UNFILLED slot — :data:`LOGFIRE_SLOT`. No
token is read, printed or wired. Records written by OTHER processes into the
same log file (``append_record`` — the self-MCP server, the agent-def render,
the launch-child CLI) do not pass this process's sink and are not spans.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import sys
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import quote, urlparse

FLAG_EXPORT = "otel_export"
FLAG_LOG_PROMPTS = "otel_log_prompts"

MODE_AS_IS = "as-is"
MODE_OTLP = "otlp"

DEFAULT_PROTOCOL = "http/protobuf"
PROTOCOLS = ("http/protobuf", "http/json", "grpc")

#: Claude Code's native content switches (claude 2.1.284 binary). All pinned to
#: "0" whenever the harness owns export ("0" is the value the binary's own help
#: names as off; env beats project settings).
CONTENT_SWITCHES = (
    "OTEL_LOG_USER_PROMPTS",
    "OTEL_LOG_ASSISTANT_RESPONSES",
    "OTEL_LOG_RAW_API_BODIES",
    "OTEL_LOG_TOOL_CONTENT",
    "OTEL_LOG_TOOL_DETAILS",
)

#: audit F1: proxy variables are scrubbed from the child env when the
#: harness owns export, so telemetry is not routed via an inherited proxy. (No
#: config re-enables them yet; this also removes them from Claude's API traffic.)
PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
              "http_proxy", "https_proxy", "all_proxy", "no_proxy")

#: Non-``OTEL_*`` telemetry keys the harness also owns (dropped when exporting).
_OWNED_KEYS = ("CLAUDE_CODE_ENABLE_TELEMETRY", "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA",
               "ENABLE_BETA_TRACING_DETAILED", "BETA_TRACING_ENDPOINT")

#: acceptance-4 — Logfire is an OPTION, documented and deliberately NOT wired.
#: To wire it (an operator decision; the token is the operator's to supply): set
#: ``otel_endpoint`` to ``endpoint`` and have the launcher set
#: ``OTEL_EXPORTER_OTLP_HEADERS=Authorization=<value of $LOGFIRE_TOKEN>`` on the
#: child and ``headers={"Authorization": …}`` on the span exporter. Nothing in
#: this module reads that variable.
LOGFIRE_SLOT: Dict[str, Any] = {
    "wired": False,
    "endpoint": "https://logfire-us.pydantic.dev",
    "endpoint_eu": "https://logfire-eu.pydantic.dev",
    "token_env_var": "LOGFIRE_TOKEN",
    "auth_header": "Authorization: <write token>",
    "protocol": "http/protobuf",
    "provenance": ("LOGFIRE_TOKEN: the variable name used by the installed logfire-api 5.1.1 "
                   "(verified locally 2026-09-29). The ingest URLs are from Logfire's docs and were "
                   "NOT verified against a live backend here."),
}

#: Record keys exported as ``gm.<key>`` span attributes — identifiers and
#: enumerations only. Anything not listed (reason, previews, payload, args, text)
#: never leaves the machine.
POINTER_KEYS = frozenset({
    "kind", "ts_iso", "source", "trigger", "target", "decision", "event", "point",
    "control", "value", "command", "parsed_command", "surface", "branch", "dsd_id",
    "state", "status", "mode", "phase", "by", "held_id", "exit_code", "clear",
    "hooks_enabled", "paused", "content_len", "content_sha256", "from_instance",
    "hub_instance_id", "launcher_kind", "child_instance", "depth", "headless",
    "ch_approved", "reserved", "instance_id",
})
_MAX_ATTR_CHARS = 256
_TAIL_BYTES = 65536


@dataclass(frozen=True)
class TelemetrySettings:
    enabled: bool
    mode: str
    reason: str  # flag-off | no-endpoint | bad-endpoint | otlp | error
    message: str = ""
    endpoint: str = ""
    protocol: str = DEFAULT_PROTOCOL
    log_prompts: bool = False


def _endpoint_problem(raw: Any) -> Optional[str]:
    """Why ``raw`` is not an acceptable collector URL, or ``None``. The reason
    NEVER quotes the value (it may carry a credential)."""
    if not isinstance(raw, str):
        return "is not a string"
    s = raw.strip()
    if not s or any(ord(c) < 33 or ord(c) == 127 for c in s):
        return "is empty or contains whitespace/control characters"
    try:
        u = urlparse(s)
        u.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return "is not a parseable URL"
    if u.scheme not in ("http", "https") or not u.hostname:
        return "is not an http(s) URL with a host"
    if "@" in u.netloc or u.username is not None or u.password is not None:
        return "contains credentials (userinfo) — put secrets in a header slot, never the URL"
    if u.query or u.fragment or "?" in s or "#" in s:
        return "has a query string or fragment (a credential channel) — use a bare base URL"
    return None


def _valid_endpoint(raw: Any) -> Optional[str]:
    return None if _endpoint_problem(raw) else raw.strip().rstrip("/")


def resolve_settings(cfg: Optional[Dict[str, Any]], machine_config: Any) -> TelemetrySettings:
    """The as-is|otlp decision for one launch. Never raises (telemetry must never
    refuse a launch); an internal error resolves to as-is with reason ``error``."""
    try:
        from .. import flags as flags

        cfg = cfg if isinstance(cfg, dict) else {}
        on = flags.flag(FLAG_EXPORT, profile=cfg, config=machine_config)
        if not on:
            return TelemetrySettings(False, MODE_AS_IS, "flag-off")
        prompts = flags.flag(FLAG_LOG_PROMPTS, profile=cfg, config=machine_config)
        raw = cfg.get("otel_endpoint")
        if raw in (None, ""):
            raw = getattr(machine_config, "otel_endpoint", "")
        proto = cfg.get("otel_protocol") or getattr(machine_config, "otel_protocol", DEFAULT_PROTOCOL)
        if proto not in PROTOCOLS:
            proto = DEFAULT_PROTOCOL
        if not (isinstance(raw, str) and raw.strip()):
            return TelemetrySettings(False, MODE_AS_IS, "no-endpoint", message=(
                "OpenTelemetry export is ON (otel_export) but no otel_endpoint is configured — "
                "NOTHING is exported. Set otel_endpoint in ~/gnomon-data/config.json "
                "(e.g. http://127.0.0.1:4318 for a local collector)."), protocol=proto, log_prompts=prompts)
        problem = _endpoint_problem(raw)
        if problem is not None:
            return TelemetrySettings(False, MODE_AS_IS, "bad-endpoint", message=(
                f"OpenTelemetry export is ON (otel_export) but the configured otel_endpoint "
                f"{problem} — NOTHING is exported (value not echoed)."), protocol=proto, log_prompts=prompts)
        ep = raw.strip().rstrip("/")
        return TelemetrySettings(True, MODE_OTLP, "otlp", endpoint=ep, protocol=proto, log_prompts=prompts)
    except Exception as exc:  # noqa: BLE001
        return TelemetrySettings(False, MODE_AS_IS, "error", message=(
            f"OpenTelemetry settings could not be resolved ({type(exc).__name__}: {exc}) — "
            "NOTHING is exported."))


def _session_name(session_dir: Optional[str]) -> str:
    return os.path.basename(os.path.normpath(session_dir)) if session_dir else ""


def resource_attributes(instance_id: Optional[str], session_dir: Optional[str]) -> str:
    """``gm.instance=…,gm.session=…`` with W3C-baggage percent-encoded values."""
    return (f"gm.instance={quote(str(instance_id or ''), safe='')},"
            f"gm.session={quote(_session_name(session_dir), safe='')}")


def apply_child_env(env: Dict[str, str], settings: TelemetrySettings, *,
                    instance_id: Optional[str], session_dir: Optional[str]) -> Dict[str, str]:
    """Return the child env. NOT enabled -> ``env`` itself, untouched (byte-identical)."""
    if not settings.enabled:
        return env
    for k in list(env):
        if k.startswith("OTEL_") or k in _OWNED_KEYS or k in PROXY_VARS:
            del env[k]
    env.update({
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "OTEL_METRICS_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_EXPORTER_OTLP_PROTOCOL": settings.protocol,
        "OTEL_EXPORTER_OTLP_ENDPOINT": settings.endpoint,
        "OTEL_RESOURCE_ATTRIBUTES": resource_attributes(instance_id, session_dir),
    })
    for k in CONTENT_SWITCHES:
        env[k] = "0"
    if settings.log_prompts:
        env["OTEL_LOG_USER_PROMPTS"] = "1"
    return env


def _stderr_warn(msg: str) -> None:
    try:
        sys.stderr.write(f"gnomon harness: ⚠ {msg}\n")
    except Exception:  # pragma: no cover
        pass


def child_env_for_launch(env: Dict[str, str], settings: TelemetrySettings, *,
                         instance_id: Optional[str], session_dir: Optional[str],
                         warn: Optional[Callable[[str], None]] = None) -> Dict[str, str]:
    """The launcher's one call: warn loudly when the flag is on but nothing will be
    exported, then :func:`apply_child_env`."""
    if settings.reason in ("no-endpoint", "bad-endpoint", "error") and settings.message:
        (warn or _stderr_warn)(settings.message)
    return apply_child_env(env, settings, instance_id=instance_id, session_dir=session_dir)


# --- harness spans --------------------------------------------------------------------


#: audit F2: fields an operator can TYPE into (a ``\\<anything>`` first
#: token) are exported only when they name something REGISTERED.
UNREGISTERED = "<unregistered>"
REDACTED = "<redacted>"
#: The Claude slash commands the harness itself injects (parsed_command).
HARNESS_SLASH_COMMANDS = frozenset({"/compact", "/clear"})
#: Instance-id grammar for exported identifiers (lowercase ids).
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
#: Identifier-valued fields: exported only when they match :data:`ID_RE`.
ID_KEYS = frozenset({"from_instance", "child_instance", "hub_instance_id", "instance_id", "by"})
#: Fanout targets that are not instances.
TARGET_WORDS = frozenset({"all"})


def _reserved_commands() -> frozenset:
    try:
        from .optap import RESERVED_COMMANDS

        return frozenset(RESERVED_COMMANDS)
    except Exception:  # noqa: BLE001
        return frozenset({"hooks"})


def _sources() -> frozenset:
    from .hooks import SOURCES

    return frozenset(SOURCES)


def pointer_attributes(record: Dict[str, Any], *, commands: Iterable[str] = (),
                       instances: Iterable[str] = (), self_instance: Optional[str] = None) -> Dict[str, Any]:
    """The allowlisted, scalar, length-capped subset of a provenance record, with
    the audit-F2 policy: ``command`` / ``parsed_command`` only when REGISTERED
    (``commands`` = the live operator-command registry + RESERVED), else
    ``<unregistered>``; ``source`` only from the hooks vocabulary; ``target`` only
    when it is this instance, ``all`` or a known instance (and matches
    :data:`ID_RE`), else ``<redacted>``; other identifier fields only when they
    match :data:`ID_RE`."""
    registered = {c.lstrip("\\") for c in commands if isinstance(c, str)} | _reserved_commands()
    known = {i for i in instances if isinstance(i, str)} | TARGET_WORDS
    if self_instance:
        known.add(self_instance)
    out: Dict[str, Any] = {}
    for k, v in record.items():
        if k not in POINTER_KEYS or v is None:
            continue
        if isinstance(v, bool) or isinstance(v, int) or isinstance(v, float):
            if k in ("command", "parsed_command", "target", "source") or k in ID_KEYS:
                out[f"gm.{k}"] = REDACTED  # a non-string where a name belongs
            else:
                out[f"gm.{k}"] = v
            continue
        if not isinstance(v, str):
            continue
        if k == "command":
            name = v.lstrip("\\")
            out["gm.command"] = name if name in registered else UNREGISTERED
        elif k == "parsed_command":
            ok = v in HARNESS_SLASH_COMMANDS or (v.startswith("\\") and v.lstrip("\\") in registered)
            out["gm.parsed_command"] = v if ok else UNREGISTERED
        elif k == "source":
            out["gm.source"] = v if v in _sources() else UNREGISTERED
        elif k == "target":
            out["gm.target"] = v if (ID_RE.match(v) and v in known) else REDACTED
        elif k in ID_KEYS:
            out[f"gm.{k}"] = v if ID_RE.match(v) else REDACTED
        else:
            out[f"gm.{k}"] = v[:_MAX_ATTR_CHARS]
    seq = record.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        out["gm.provenance_seq"] = seq
    return out


def latest_message_uuid(path: Optional[str]) -> Optional[str]:
    """The ``uuid`` of the last jsonl row carrying one. Reads only the tail; keeps
    ONLY the uuid string (no content survives this function)."""
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            tail = fh.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        uid = row.get("uuid") if isinstance(row, dict) else None
        if isinstance(uid, str) and uid:
            return uid[:_MAX_ATTR_CHARS]
    return None


def _silence_otel_logging() -> None:
    """The harness's stderr IS the operator's terminal: an exporter warning must
    never be painted over the TUI. Failures are counted instead (stats())."""
    lg = logging.getLogger("opentelemetry")
    if not any(isinstance(h, logging.NullHandler) for h in lg.handlers):
        lg.addHandler(logging.NullHandler())
    lg.propagate = False


class _CountingExporter:
    """Delegates to the real exporter; counts outcomes; never raises."""

    def __init__(self, inner: Any, owner: "HarnessTelemetry") -> None:
        self._inner = inner
        self._owner = owner

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            res = self._inner.export(spans)
        except Exception:  # noqa: BLE001
            res = SpanExportResult.FAILURE
        finally:
            self._owner._sdk_done(len(spans))
        with self._owner._lock:
            if res == SpanExportResult.SUCCESS:
                self._owner._stats["exported"] += len(spans)
            else:
                self._owner._stats["export_failures"] += 1
        return res

    def shutdown(self):
        try:
            self._inner.shutdown()
        except Exception:  # noqa: BLE001
            pass

    def force_flush(self, timeout_millis: int = 30000):
        try:
            return self._inner.force_flush(timeout_millis)
        except Exception:  # noqa: BLE001
            return False


class _GatedProcessor:
    """audit F4: fronts the ``BatchSpanProcessor`` so the SDK queue can
    never silently drop. ``pending`` = spans handed to the SDK and not yet
    returned from an export (success or failure); at ``cap`` a span is dropped
    HERE and counted (``sdk_queue_dropped``). ``cap`` equals the SDK's
    ``max_queue_size`` and ``pending`` also counts the in-flight batch, so the
    SDK's own deque is never full."""

    def __init__(self, inner: Any, owner: "HarnessTelemetry", cap: int) -> None:
        self._inner = inner
        self._owner = owner
        self._cap = cap

    def on_start(self, span, parent_context=None):
        self._inner.on_start(span, parent_context=parent_context)

    def on_end(self, span):
        with self._owner._lock:
            if self._owner._pending >= self._cap:
                self._owner._stats["sdk_queue_dropped"] += 1
                return
            self._owner._pending += 1
        self._inner.on_end(span)

    def _on_ending(self, span):  # SDK >= 1.3x hook; delegate if present
        fn = getattr(self._inner, "_on_ending", None)
        if fn is not None:
            fn(span)

    def shutdown(self):
        self._inner.shutdown()

    def force_flush(self, timeout_millis: int = 30000):
        return self._inner.force_flush(timeout_millis)


def _isolated_exporter(endpoint: str, timeout_s: float) -> Any:
    """audit F1: an OTLP/HTTP span exporter that reads NOTHING from the
    harness's own environment — explicit endpoint/timeout/compression/CA, a
    non-empty explicit header dict (the 1.40 exporter falls back to
    ``OTEL_EXPORTER_OTLP_*HEADERS`` when ``headers`` is falsy), no client cert, and
    a ``requests`` session with ``trust_env=False`` (no ``HTTP(S)_PROXY`` / netrc)."""
    import requests
    from opentelemetry.exporter.otlp.proto.http import Compression
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    sess = requests.Session()
    sess.trust_env = False
    exp = OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces", certificate_file=True,
                           headers={"x-gnomon-producer": "gnomon-harness"}, timeout=timeout_s,
                           compression=Compression.NoCompression, session=sess)
    # client-cert/key fall back to OTEL_EXPORTER_OTLP_CLIENT_* when not given;
    # neutralise them (pinned opentelemetry 1.40.0; guarded by a test).
    for attr in ("_client_cert", "_client_certificate_file", "_client_key_file"):
        if hasattr(exp, attr):
            setattr(exp, attr, None)
    return exp


_SENTINEL = object()


class HarnessTelemetry:
    """Provenance records -> OTel spans, off the pump thread. See the module doc."""

    def __init__(self, settings: TelemetrySettings, *, instance_id: Optional[str],
                 session_dir: Optional[str], transcript_path: Optional[str] = None,
                 exporter: Any = None, queue_max: int = 4096, export_timeout_s: float = 1.0,
                 sdk_queue_max: int = 2048,
                 command_names: Optional[Callable[[], Iterable[str]]] = None,
                 known_instances: Optional[Callable[[], Iterable[str]]] = None) -> None:
        self.settings = settings
        self.instance_id = instance_id or ""
        self.session = _session_name(session_dir)
        self.transcript_path = transcript_path
        self.provenance_log: Optional[str] = None
        self.available = False
        self.unavailable_reason: Optional[str] = None
        self._lock = threading.Lock()
        self._stats = {"enqueued": 0, "emit_queue_dropped": 0, "sdk_queue_dropped": 0,
                       "spans": 0, "span_errors": 0, "exported": 0, "export_failures": 0}
        self._pending = 0
        self._command_names = command_names
        self._known_instances = known_instances
        self._instances_cache: tuple = (0.0, frozenset())
        self._q: "queue.Queue[Any]" = queue.Queue(maxsize=max(1, int(queue_max)))
        self._closing = threading.Event()
        try:
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import SpanLimits, TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.trace.sampling import ALWAYS_ON
        except ImportError as exc:
            self.unavailable_reason = f"opentelemetry-sdk not installed ({exc})"
            return
        if exporter is None:
            if settings.protocol == "grpc":
                self.unavailable_reason = ("harness spans use OTLP/HTTP; otel_protocol is grpc (no gRPC "
                                           "exporter installed) — Claude Code's native export is unaffected")
                return
            try:
                exporter = _isolated_exporter(settings.endpoint, export_timeout_s)
            except ImportError as exc:
                self.unavailable_reason = f"opentelemetry-exporter-otlp-proto-http not installed ({exc})"
                return
        _silence_otel_logging()
        # audit F1: an explicit Resource (NO env detectors: OTEL_RESOURCE_ATTRIBUTES /
        # OTEL_SERVICE_NAME in the harness env do not ride along), an explicit
        # sampler, explicit limits, and no silent OTEL_SDK_DISABLED no-op.
        resource = Resource({"service.name": "gnomon-harness",
                             "gm.instance": self.instance_id, "gm.session": self.session})
        self._provider = TracerProvider(
            sampler=ALWAYS_ON, resource=resource, shutdown_on_exit=False,
            span_limits=SpanLimits(max_attributes=128, max_span_attributes=128,
                                   max_attribute_length=_MAX_ATTR_CHARS, max_events=0, max_links=0,
                                   max_event_attributes=0, max_link_attributes=0))
        if getattr(self._provider, "_disabled", False):
            self._provider._disabled = False  # the harness env must not silently disable export
        sdk_cap = max(1, int(sdk_queue_max))
        self._sdk_cap = sdk_cap
        self._processor = BatchSpanProcessor(_CountingExporter(exporter, self),
                                             max_queue_size=sdk_cap,
                                             max_export_batch_size=max(1, min(512, sdk_cap)),
                                             schedule_delay_millis=1000,
                                             export_timeout_millis=int(export_timeout_s * 1000))
        self._provider.add_span_processor(_GatedProcessor(self._processor, self, sdk_cap))
        self._tracer = self._provider.get_tracer("gnomon.harness")
        self._uuid_cache: tuple = (None, None)
        self._worker = threading.Thread(target=self._run, name="gm-otel-emitter", daemon=True)
        self._worker.start()
        self.available = True

    # -- the sink (called under ProvenanceLog's lock: O(1), never blocks) ----------

    def emit(self, record: Dict[str, Any]) -> None:
        if not self.available or self._closing.is_set():
            return
        try:
            self._q.put_nowait(record)
        except queue.Full:
            with self._lock:
                self._stats["emit_queue_dropped"] += 1
            return
        with self._lock:
            self._stats["enqueued"] += 1

    # -- worker ---------------------------------------------------------------------

    def _run(self) -> None:
        while True:
            try:
                item = self._q.get(timeout=0.2)
            except queue.Empty:
                if self._closing.is_set():
                    return
                continue
            if item is _SENTINEL:
                return
            try:
                self._span(item)
                with self._lock:
                    self._stats["spans"] += 1
            except Exception:  # noqa: BLE001
                with self._lock:
                    self._stats["span_errors"] += 1

    def _message_uuid(self) -> Optional[str]:
        p = self.transcript_path
        if not p:
            return None
        try:
            st = os.stat(p)
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return None
        if self._uuid_cache[0] != key:
            self._uuid_cache = (key, latest_message_uuid(p))
        return self._uuid_cache[1]

    def _sdk_done(self, n: int) -> None:
        with self._lock:
            self._pending = max(0, self._pending - n)

    def _instances(self) -> frozenset:
        fn = self._known_instances
        if fn is None:
            return frozenset()
        t, cached = self._instances_cache
        if time.monotonic() - t < 30.0:
            return cached
        try:
            val = frozenset(i for i in fn() if isinstance(i, str))
        except Exception:  # noqa: BLE001
            val = frozenset()
        self._instances_cache = (time.monotonic(), val)
        return val

    def _commands(self) -> Iterable[str]:
        fn = self._command_names
        if fn is None:
            return ()
        try:
            return list(fn())
        except Exception:  # noqa: BLE001
            return ()

    def _span(self, rec: Dict[str, Any]) -> None:
        attrs = pointer_attributes(rec, commands=self._commands(), instances=self._instances(),
                                   self_instance=self.instance_id)
        attrs["gm.session"] = self.session
        attrs["gm.instance"] = self.instance_id
        if self.provenance_log:
            attrs["gm.provenance_log"] = self.provenance_log
        if self.transcript_path:
            attrs["gm.transcript_path"] = self.transcript_path
            uid = self._message_uuid()
            if uid:
                attrs["gm.transcript_message_uuid"] = uid
        ts = rec.get("ts")
        ok_ts = (isinstance(ts, (int, float)) and not isinstance(ts, bool)
                 and ts == ts and 0 < ts < 4e9)  # finite, positive, before 2096
        t_ns = int(ts * 1e9) if ok_ts else time.time_ns()
        kind = rec.get("kind") if isinstance(rec.get("kind"), str) else "record"
        span = self._tracer.start_span(f"gm.{kind[:64]}", start_time=t_ns, attributes=attrs)
        span.end(end_time=t_ns)

    # -- lifecycle --------------------------------------------------------------------

    def close(self, timeout: float = 2.0) -> None:
        """Drain what is queued, flush, shut down — bounded by ``timeout`` overall.
        Never raises; an unreachable collector costs at most ``timeout``."""
        if not self.available:
            return
        deadline = time.monotonic() + max(0.1, timeout)
        self._closing.set()
        try:
            self._q.put_nowait(_SENTINEL)
        except queue.Full:
            pass
        self._worker.join(max(0.0, deadline - time.monotonic()) / 2)

        def _shutdown():
            try:
                self._provider.shutdown()
            except Exception:  # noqa: BLE001
                pass

        t = threading.Thread(target=_shutdown, name="gm-otel-shutdown", daemon=True)
        t.start()
        t.join(max(0.0, deadline - time.monotonic()))

    def stats(self) -> Dict[str, int]:
        """Counters. ``emit_queue_dropped`` = the harness's own bounded queue;
        ``sdk_queue_dropped`` = refused at the SDK gate (:class:`_GatedProcessor`);
        ``dropped`` = both."""
        with self._lock:
            st = dict(self._stats)
        st["dropped"] = st["emit_queue_dropped"] + st["sdk_queue_dropped"]
        return st


def telemetry_for_launch(settings: TelemetrySettings, *, instance_id: Optional[str],
                         session_dir: Optional[str], transcript_path: Optional[str] = None,
                         warn: Optional[Callable[[str], None]] = None,
                         command_names: Optional[Callable[[], Iterable[str]]] = None,
                         known_instances: Optional[Callable[[], Iterable[str]]] = None,
                         ) -> Optional[HarnessTelemetry]:
    """``None`` unless export is enabled AND the span path is available (an
    unavailable span path is announced, never silent)."""
    if not settings.enabled:
        return None
    try:
        tel = HarnessTelemetry(settings, instance_id=instance_id, session_dir=session_dir,
                               transcript_path=transcript_path, command_names=command_names,
                               known_instances=known_instances)
    except Exception as exc:  # noqa: BLE001
        (warn or _stderr_warn)(f"harness OTel spans unavailable ({type(exc).__name__}: {exc}); "
                               "Claude Code's native export is unaffected")
        return None
    if not tel.available:
        (warn or _stderr_warn)(f"harness OTel spans unavailable: {tel.unavailable_reason}")
        return None
    return tel


def attach_telemetry(log_or_gate: Any, tel: Optional[HarnessTelemetry]) -> Optional[HarnessTelemetry]:
    """Install ``tel.emit`` as the provenance log's sink (accepts the log or a gate
    holding ``.log``). ``tel=None`` (as-is) installs nothing."""
    if tel is None:
        return None
    log = getattr(log_or_gate, "log", log_or_gate)
    tel.provenance_log = getattr(log, "path", None)
    log.sink = tel.emit
    return tel

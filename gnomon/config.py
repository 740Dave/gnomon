"""Machine-specific configuration store (slice 4).

Everything machine-specific lives here so ONE Gnomon build runs unmodified
on any employee's machine — the Configuration tab's whole reason to exist. This
module is the SOURCE OF TRUTH for the machine-specific bits that slices 2a/2b/3b
previously carried as hardcoded defaults:

* the two runtime/project roots (``~/gnomon-data`` / ``~/gnomon``, D31);
* the ``claude`` binary path (default ``~/.claude/local/claude``);
* the terminal launch templates, INCLUDING user-added custom named templates
  (``{name: command-template-string}``) so the launcher is terminal-agnostic by
  config (D4);
* the default MCP server SET wired into a launched session and their endpoint
  configs (``lts-engine`` / ``gnomon`` — ⛔ never a
  measurement-surface server, D9);
* launch defaults (model, context thresholds 90/95, bootstrap prompt);
* the DSD-answers corpus path (D14).

It is deliberately package-ROOT (not under ``app/``): ``launch.py`` / ``terminal.py``
read from it, and those are lower-level than the CTk app layer, so the config
must not depend on the app. It is pure stdlib — no CustomTkinter, unit-testable
headlessly.

Persistence is a single plain-JSON file (default ``~/gnomon-data/config.json``).
An ABSENT or PARTIAL file resolves to the sensible defaults below — a fresh
machine with no config still launches; a hand-edited file that carries only a few
keys keeps the defaults for the rest.
"""

from __future__ import annotations

import errno
import json
import os
import stat
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


# --- defaults (mirror the values slices 2a/2b/3b hardcoded, now overridable) --

DEFAULT_RUNTIME_ROOT = "~/gnomon-data"
DEFAULT_PROJECT_ROOT = "~/gnomon"
#: X13 (S1 fix round, ruling-f4-2026-09-30): the physical DEPLOY root (workspaces/<ws>/..., machine.json,
#: and a deployment's converter). PROVISIONAL name: the operator picks the real one (root-names); the
#: rename is a ONE-LINE change here. Read only through ``workspace.deploy``; no runtime consumer yet (S1).
DEFAULT_DEPLOY_ROOT = "~/gnomon-deploy.provisional"
#: The claude binary. slice-2a's LaunchManager defaulted to bare ``claude`` (PATH
#: lookup); the config makes the real per-machine install path the default.
DEFAULT_CLAUDE_BIN = "~/.claude/local/claude"
DEFAULT_TERMINAL = "iterm2"
#: ⛔ a measurement-surface server is deliberately absent — a launcher agent has no business holding
#: a measurement surface (D9). This mirrors profile.DEFAULT_MCP_SERVERS.
DEFAULT_MCP_SERVERS: tuple[str, ...] = ("lts-engine", "gnomon")
#: Servers a launcher agent must never hold in its default set (D9). EMPTY in the public build: the one entry the
#: internal build carried was a private repository name and was removed (round 2, a listed behaviour change).
REFUSED_DEFAULT_MCP_SERVERS: tuple[str, ...] = ()
#: 65/80 were measured not useful (operator, 2026-08-11); 90/95 remain
#: (harness.constants.HOSTED_CHILD_THRESHOLDS). 90 = actionable, 95 = backstop.
DEFAULT_CONTEXT_THRESHOLDS: tuple[int, ...] = (90, 95)

#: the OTLP protocols Claude Code's native exporter accepts.
OTEL_PROTOCOLS: tuple[str, ...] = ("http/protobuf", "http/json", "grpc")
DEFAULT_OTEL_PROTOCOL = "http/protobuf"

#: The default filesystem home of the config file. LEGACY default: it applies only while no per-user roots file is in
#: play (see :func:`default_config_path` / :mod:`gnomon.roots`, X13 S5-4). TRANSITIONAL (Diamond F3): this constant,
#: ``DEFAULT_RUNTIME_ROOT`` / ``DEFAULT_DEPLOY_ROOT`` and the ``roots.adopted()`` gate go when an installer that writes
#: ``roots.toml`` exists; owner and condition are in the :mod:`gnomon.roots` docstring and the PORT-LEDGER.
DEFAULT_CONFIG_PATH = os.path.join(DEFAULT_RUNTIME_ROOT, "config.json")

#: (file, fault) pairs already reported on stderr, so an invalid roots file is LOUD once per process, never per call
_reported_roots: set = set()


def _adopted_roots(home: Optional[str] = None):
    """The resolved per-user roots (:class:`gnomon.roots.Roots`) when a roots file is in play AND usable, else
    ``None``. LAZY (reads the environment and the file when CALLED). A roots file that EXISTS but is unusable is a
    named fault: it is reported on stderr once per (file, fault) per process and then treated as not in play, so a
    machine is never silently redirected to an empty directory by a broken pointer file. ``home`` (X13 S5-4b) resolves
    the DEFAULT pointer file against that home instead of the process HOME (the converter's ``--home``)."""
    from . import roots

    if not roots.adopted(home=home):
        return None
    r = roots.load(home=home)
    if r.ok:
        if r.notes:
            key = (r.config_path, r.notes)
            if key not in _reported_roots:
                _reported_roots.add(key)
                import logging

                logging.getLogger(__name__).warning("[gnomon roots] folder names: " + "; ".join(r.notes))
        return r
    key = (r.config_path, r.errors)
    if key not in _reported_roots:
        _reported_roots.add(key)
        import logging

        logging.getLogger(__name__).warning("[gnomon roots] IGNORING an unusable roots file (the legacy defaults stay in force): "
                                            + "; ".join(r.errors))
    return None


def _legacy_under(default: str, home: Optional[str]) -> str:
    """A legacy ``~/...`` default expanded against ``home`` (or the process HOME when ``home`` is None)."""
    if home is None:
        return os.path.expanduser(default)
    return os.path.abspath(os.path.expanduser(home)) + default[1:]


def runtime_root_default(home: Optional[str] = None) -> str:
    """The DATA root (absolute, ``~`` expanded) every call-time default resolves against: the per-user roots file's
    ``data_root`` when one is in play (X13 S5-4), else the legacy default. The ONE place a default site gets it
    (g69; S5-4b: the event log, the converter and the go-live tool now go through here too). ``home`` expands against
    that home (a tool run with ``--home``) instead of the process HOME."""
    r = _adopted_roots(home)
    return r.data_root if r is not None else _legacy_under(DEFAULT_RUNTIME_ROOT, home)


#: TRANSITIONAL (goes with ``DEFAULT_RUNTIME_ROOT``, the roots.toml-less mode): the folder names the legacy data root used, so a machine WITHOUT a
#: roots file keeps resolving exactly what it resolved before. With a roots file in play the folder keys of :mod:`gnomon.roots` decide.
LEGACY_FOLDERS = {"sessions_root": "sessions", "agents_root": "agents", "transcript_archive_root": "transcript-archive"}


def folder_default(key: str, home: Optional[str] = None) -> str:
    """One of the three folder roots (``sessions_root``, ``agents_root``, ``transcript_archive_root``): the roots file's key when a roots
    file is in play, else the legacy folder under the legacy data root. EVERY reader gets these folders here, never by joining a name
    onto a root (round-2 ruling; ``tests/test_x13s3_folder_roots.py``)."""
    from . import roots

    r = _adopted_roots(home)
    if r is not None:
        return r.folder(key)                                      # raises roots.FolderConflictError when the folder is ambiguous
    return roots.folder_under(key, runtime_root_default(home), LEGACY_FOLDERS[key])


def sessions_root_default(home: Optional[str] = None) -> str:
    return folder_default("sessions_root", home)


def agents_root_default(home: Optional[str] = None) -> str:
    return folder_default("agents_root", home)


def transcript_archive_root_default(home: Optional[str] = None) -> str:
    return folder_default("transcript_archive_root", home)


def default_config_path(home: Optional[str] = None) -> str:
    """Where the machine config lives by default: ``<data_root>/config.json`` when a roots file is in play, else
    :data:`DEFAULT_CONFIG_PATH` (read at CALL time, so a test that patches it is honoured). ``home`` (S5-4b) resolves
    against that home."""
    r = _adopted_roots(home)
    if r is not None:
        return os.path.join(r.data_root, "config.json")
    return DEFAULT_CONFIG_PATH if home is None else os.path.join(_legacy_under(DEFAULT_RUNTIME_ROOT, home), "config.json")


def default_mcp_endpoints() -> Dict[str, dict]:
    """Connection configs for the D9 default server set.

    The ``gnomon`` entry is a placeholder here: its real ``url`` is the
    co-hosted hub's live MCP url, wired at launch by :class:`LaunchManager` once
    the hub has bound a port. The stdio server is launched by command.
    """
    return {
        "lts-engine": {"type": "stdio", "command": "lts-engine"},
        "gnomon": {
            "type": "http",
            "url": None,
            "_note": "hub MCP url wired at launch by the host/GUI",
        },
    }


class PresenceRequired(ValueError):
    """A save would turn a security flag OFF without a presence grant. Nothing was written."""


@dataclass
class GnomonConfig:
    """The machine-specific settings, with sensible defaults for a fresh machine.

    All path fields are stored UNEXPANDED (``~/...``) so a persisted config is
    portable; the ``resolved_*`` / ``*_root`` accessors expand + join. Callers
    that need a live path use those accessors, never the raw fields.
    """

    project_root: str = DEFAULT_PROJECT_ROOT
    runtime_root: str = DEFAULT_RUNTIME_ROOT
    #: X13: the DEPLOY root (see ``DEFAULT_DEPLOY_ROOT``); stored unexpanded like the other roots.
    deploy_root: str = DEFAULT_DEPLOY_ROOT
    claude_bin: str = DEFAULT_CLAUDE_BIN
    default_terminal: str = DEFAULT_TERMINAL
    #: user-added named templates: {name: command-template-string}. The string
    #: may reference ``{session_dir}``, ``{inner_command}`` and ``{cd_and_command}``
    #: (see ``terminal.render_string_template``). Terminal-agnostic-by-config (D4).
    custom_terminal_templates: Dict[str, str] = field(default_factory=dict)
    default_mcp_servers: List[str] = field(
        default_factory=lambda: list(DEFAULT_MCP_SERVERS)
    )
    mcp_endpoints: Dict[str, dict] = field(default_factory=default_mcp_endpoints)
    default_model: str = ""
    context_thresholds: List[int] = field(
        default_factory=lambda: list(DEFAULT_CONTEXT_THRESHOLDS)
    )
    default_bootstrap_prompt: str = ""
    #: empty -> derived as ``<runtime_root>/dsd-answers/dsd-answers.lts``.
    dsd_answers_corpus_path: str = ""

    # --- PTY-hooks WS-H (2026-09-27) ---------------------------------------
    #: Display name used to SIGN operator messages (``\all`` / ``\<id>``):
    #: ``<{user_name}-to-all>``. Distinct from the identity email. Empty -> the
    #: OS login, else the placeholder ``operator``.
    user_name: str = ""
    #: GLOBAL stall-inject default (WS-C). ``None`` = the built-in defaults
    #: (``harness.constants.DEFAULT_STALL_INJECT``); a dict overrides keys. A
    #: profile's ``stall_inject`` is the per-instance override (may set
    #: ``enabled: false``).
    stall_inject: Optional[Dict[str, object]] = None
    #: The hook manifest ``{point: [handler, ...]}`` (``harness.hooks``). Empty =
    #: no content hooks (provenance is always on).
    hook_manifest: Dict[str, object] = field(default_factory=dict)
    #: The ``\`` operator command plane (WS-B) — the sigil toggle.
    operator_command_plane: bool = True
    #: WS-G: the HUB ``launch_agent`` verb + profile-level agent launching.
    #: OFF by default — production auto-launch stays gated until the operator
    #: ratifies the agent-invokes-agent security framework.
    agent_launch_enabled: bool = False
    #: the per-macro ``{{lts: … }}`` budget (APPROXIMATE tokens, chars/4)
    #: applied at agent-def render time to every macro that does not set its own
    #: ``budget=``. ``None`` = OFF (unbounded — the pre-RM7A behaviour, byte-identical
    #: renders). A macro's ``budget=off`` opts out of this default.
    lts_macro_budget: Optional[int] = None
    #: machine-level feature flags ``{<name>: bool}``, read ONLY
    #: through :func:`gnomon.flags.flag` (a profile's ``flags`` overrides
    #: these). Empty = every flag at its registry default. Not tab-editable; the
    #: Config tab lists the effective values read-only (``/api/flags``).
    flags: Dict[str, bool] = field(default_factory=dict)
    #: the post-compact boot prompt's per-role text + timing, read ONLY when
    #: ``post_compact_boot`` is on. ``None`` = the package defaults
    #: (``harness.post_compact_boot_texts``). Shape and precedence:
    #: ``harness.post_compact_boot`` (``{"default", "roles": {role: text},
    #: "settle_seconds", "max_hold_seconds"}``); validated at launch, only when on.
    post_compact_boot: Optional[Dict[str, object]] = None
    #: the OTLP/HTTP collector base URL (e.g. a local collector
    #: ``http://127.0.0.1:4318``). Read ONLY when ``otel_export`` is on;
    #: empty = NOTHING is exported even with the flag on (logged loudly). Telemetry
    #: leaving the machine is publishing, so there is deliberately no default.
    otel_endpoint: str = ""
    #: the OTLP protocol handed to Claude Code's native exporter
    #: (``OTEL_EXPORTER_OTLP_PROTOCOL``): ``http/protobuf`` | ``http/json`` | ``grpc``.
    otel_protocol: str = "http/protobuf"
    #: the restart_service SERVICE REGISTRY, ``{service_id: {host, mechanism,
    #: owner, hosts_hub?, health_url?, notes?}}`` (schema, validation and merge rule:
    #: ``gnomon.service_registry``; WHO may call it is the verb policy, not this field).
    #: ``None`` = only the shipped default (gnomon-web -> local launchd). Kept RAW here on purpose: a
    #: malformed value is rejected loudly at use time (never silently normalised away).
    #: MACHINE-READ under g25 by service_registry.load_registry. Editing it is a
    #: SECURITY-POSTURE change (the operator's call); it is not writable from session state.
    services: Optional[Dict[str, object]] = None
    #: (spec 13): the Gnomon port range ``[low, high]`` the Apps host assigns from when an app's
    #: preferred port is taken or absent. ``None`` = NOT SET: the range is an OPEN question
    #: (open-question-port-range), so no default is shipped; a proposal that needs the
    #: range is refused, naming the gap. A malformed value is dropped to ``None`` (refuse, never guess).
    apps_port_range: Optional[List[int]] = None
    #: the deny guard FAILS CLOSED when it cannot start (a top-level boolean, NOT a flag). Read at session LAUNCH
    #: by ``harness.launcher.deny_guard_hook_settings`` and written into the hook command as one of two fixed tails, so it
    #: reaches a session only at that session's next launch. Default True; anything that is not the boolean false reads as
    #: True. Turning it OFF is a security-posture change (presence check: ``ConfigStore.save``).
    guard_fail_closed: bool = True
    #: (revision 2026-10-27): which subagent transcripts the durable transcript archive keeps:
    #: ``all`` (default) | ``none``. ``flagged-only`` is UNDEFINED in the spec; it is read as ``all`` and the archive
    #: says so on every run. A value that is not a string reads as ``all`` (the direction that keeps data).
    transcript_archive_subagents: str = "all"

    # -- derived accessors ---------------------------------------------------

    def _folder(self, key: str) -> str:
        """A folder root through the resolver. A ``runtime_root`` that came FROM the roots file (not pinned in config.json) uses the roots
        file's folder key; a pinned one joins the pinned root with the legacy folder name (through :func:`gnomon.roots.folder_under`)."""
        from . import roots

        r = _adopted_roots()
        # Round 6 (Opus Diamond P1-3): a PINNED runtime_root goes through the SAME conflict check (Roots.folder raises FolderConflictError when both
        # folders hold data and no table decides); the check is not repeated here, and its answer is used only when runtime_root follows the roots file.
        chosen = r.folder(key) if r is not None else None
        if getattr(self, "_runtime_root_from_roots", False) and chosen is not None:
            return chosen
        return roots.folder_under(key, os.path.expanduser(self.runtime_root), LEGACY_FOLDERS[key])

    def intelligences_root(self) -> str:
        """The agents folder (D31): the roots file's ``agents_root`` when ``runtime_root`` follows it, else ``<runtime_root>/<legacy name>``."""
        return self._folder("agents_root")

    def sessions_root(self) -> str:
        """The hot session working-dir home (D31): the roots file's ``sessions_root`` when ``runtime_root`` follows it, else ``<runtime_root>/sessions``."""
        return self._folder("sessions_root")

    def resolved_project_root(self) -> str:
        return os.path.abspath(os.path.expanduser(self.project_root))

    def resolved_deploy_root(self, home: Optional[str] = None) -> str:
        """The DEPLOY root: an explicit ``deploy_root`` key wins; left at the default it follows the per-user roots
        file's ``deploy_root`` when one is in play (X13 S5-4), else the legacy default. ``home`` (S5-4b) expands the
        legacy default and finds the default pointer file against that home instead of the process HOME."""
        if self.deploy_root == DEFAULT_DEPLOY_ROOT:
            r = _adopted_roots(home)
            if r is not None:
                return r.deploy_root
        if home is not None and self.deploy_root.startswith("~"):
            return os.path.abspath(_legacy_under(self.deploy_root, home))
        return os.path.abspath(os.path.expanduser(self.deploy_root))

    def resolved_claude_bin(self) -> str:
        """Expand ``~`` but keep a bare command (``claude``) bare for PATH lookup."""
        if self.claude_bin.startswith("~") or os.sep in self.claude_bin:
            return os.path.expanduser(self.claude_bin)
        return self.claude_bin

    def resolved_dsd_answers_corpus_path(self) -> str:
        """The explicit corpus path if set, else derived under the runtime root."""
        if self.dsd_answers_corpus_path:
            return os.path.abspath(os.path.expanduser(self.dsd_answers_corpus_path))
        return os.path.join(
            os.path.expanduser(self.runtime_root), "dsd-answers", "dsd-answers.lts"
        )

    def excludes_refused_servers(self) -> bool:
        """D9 guard: the default MCP set must never carry a server named in :data:`REFUSED_DEFAULT_MCP_SERVERS`."""
        return not any(s in self.default_mcp_servers for s in REFUSED_DEFAULT_MCP_SERVERS)

    # -- (de)serialization ---------------------------------------------------

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "GnomonConfig":
        """Lift KNOWN keys; keep defaults for missing ones (partial config).

        Unknown keys are ignored rather than fatal, so a hand-edited file that
        carries an extra note does not brick the launcher.
        """
        if not isinstance(data, dict):
            raise ValueError("config data must be a JSON object")
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        kwargs = {k: v for k, v in data.items() if k in known}
        cfg = cls(**kwargs)  # type: ignore[arg-type]
        # Normalize a few fields a hand-edit could leave malformed.
        if not cfg.default_mcp_servers:
            cfg.default_mcp_servers = list(DEFAULT_MCP_SERVERS)
        if not cfg.context_thresholds:
            cfg.context_thresholds = list(DEFAULT_CONTEXT_THRESHOLDS)
        # PTY-hooks WS-H: a hand-edit that leaves these malformed falls back to
        # the safe default rather than bricking the launcher.
        if not isinstance(cfg.user_name, str):
            cfg.user_name = ""
        if not isinstance(cfg.deploy_root, str) or not cfg.deploy_root.strip():
            cfg.deploy_root = DEFAULT_DEPLOY_ROOT
        if cfg.stall_inject is not None and not isinstance(cfg.stall_inject, dict):
            cfg.stall_inject = None
        if not isinstance(cfg.hook_manifest, dict):
            cfg.hook_manifest = {}
        if not isinstance(cfg.operator_command_plane, bool):
            cfg.operator_command_plane = True
        if not isinstance(cfg.agent_launch_enabled, bool):
            cfg.agent_launch_enabled = False
        # only the boolean false turns the guard's fail-closed behaviour off; every other value reads as true.
        if cfg.guard_fail_closed is not False:
            cfg.guard_fail_closed = True
        if not isinstance(cfg.transcript_archive_subagents, str):
            cfg.transcript_archive_subagents = "all"
        # a malformed budget falls back to OFF (unbounded — the direction
        # that loses no memory), consistent with the other hand-edit normalisations.
        b = cfg.lts_macro_budget
        if b is not None and (isinstance(b, bool) or not isinstance(b, int) or b < 0):
            cfg.lts_macro_budget = None
        # a non-object flags is dropped (every flag -> its default);
        # per-value bool checking is flags.resolve's job (it logs, never coerces).
        if not isinstance(cfg.flags, dict):
            cfg.flags = {}
        # a non-object post_compact_boot is dropped (= the package defaults).
        if cfg.post_compact_boot is not None and not isinstance(cfg.post_compact_boot, dict):
            cfg.post_compact_boot = None
        # a malformed endpoint falls back to "" (= export nothing — the
        # direction that publishes nothing); an unknown protocol to the default.
        if not isinstance(cfg.otel_endpoint, str):
            cfg.otel_endpoint = ""
        if cfg.otel_protocol not in OTEL_PROTOCOLS:
            cfg.otel_protocol = DEFAULT_OTEL_PROTOCOL
        # [low, high] loopback ports, 1024 <= low <= high <= 65535; anything else -> None (unset).
        r = cfg.apps_port_range
        if r is not None and not (isinstance(r, list) and len(r) == 2
                                  and all(isinstance(x, int) and not isinstance(x, bool) for x in r)
                                  and 1024 <= r[0] <= r[1] <= 65535):
            cfg.apps_port_range = None
        return cfg

    def resolved_stall_inject(self, override: Optional[Dict[str, object]] = None) -> Dict[str, object]:
        """``DEFAULT <- this global <- per-instance override`` (WS-C). Fails LOUD."""
        from .harness.constants import merge_stall_inject

        return merge_stall_inject(self.stall_inject, override)

    def resolved_user_name(self) -> str:
        from .harness.opcommands import resolve_user_name

        return resolve_user_name(self.user_name)


_reported_unknown: set = set()      # (path, key) already reported by this process


def unknown_config_keys(data: Dict[str, object]) -> List[str]:
    """The top-level keys of a loaded config that are not fields of :class:`GnomonConfig` (sorted)."""
    return sorted(str(k) for k in data if k not in GnomonConfig.__dataclass_fields__)


class ConfigStore:
    """Load/save a :class:`GnomonConfig` as plain JSON.

    ``path`` defaults to :func:`default_config_path` (``<data_root>/config.json``) and is injectable for
    tests. A missing or corrupt file loads as defaults (the launcher must run on
    a fresh machine); a save is atomic (temp + replace).
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = os.path.abspath(
            os.path.expanduser(path if path is not None else default_config_path())
        )
        #: keys of the last loaded file that this version does not know (cut-away rule-fail-visible: a missed rename is loud,
        #: never silently dropped). Nothing reads, translates or maps them; they are reported and ignored.
        self.unknown_keys: List[str] = []

    @property
    def path(self) -> str:
        return self._path

    def exists(self) -> bool:
        return os.path.isfile(self._path)

    def load(self) -> GnomonConfig:
        """The persisted config, or defaults when absent/partial/corrupt.

        A partial file keeps defaults for the missing keys
        (:meth:`GnomonConfig.from_dict`). A file that cannot be read or
        parsed falls back to full defaults rather than crashing the app — a
        broken config must never brick the launcher.
        """
        try:
            # g48b(c) / X26R F-5: never block on a non-regular file at the config path (a FIFO with no writer would hang
            # every flag read, hook paths included): O_NONBLOCK open, then an fstat regular-file check BEFORE reading.
            fd = os.open(self._path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError(errno.ENOTSUP, "not a regular file")
                fh = os.fdopen(fd, "r", encoding="utf-8")
            except BaseException:
                os.close(fd)
                raise
            with fh:
                data = json.load(fh)
        except FileNotFoundError:
            return self._follow_roots(GnomonConfig(), {})
        except (ValueError, OSError):
            return self._follow_roots(GnomonConfig(), {})
        if not isinstance(data, dict):
            return self._follow_roots(GnomonConfig(), {})
        self.unknown_keys = unknown_config_keys(data)
        for key in self.unknown_keys:
            if (self._path, key) not in _reported_unknown:
                _reported_unknown.add((self._path, key))
                import logging

                logging.getLogger(__name__).error(
                    "[gnomon config] UNKNOWN top-level key %r in %s: it is IGNORED (nothing reads it; a setting under an old "
                    "name has NOT taken effect and its default is in force)", key, self._path)
        return self._follow_roots(GnomonConfig.from_dict(data), data)

    @staticmethod
    def _follow_roots(cfg: GnomonConfig, data: Dict[str, object]) -> GnomonConfig:
        """X13 S5-4: with a per-user roots file in play, ``runtime_root`` defaults to its ``data_root`` unless the
        persisted config names one (an explicit key wins). ``deploy_root`` is followed in ``resolved_deploy_root``."""
        if "runtime_root" not in data:
            r = _adopted_roots()
            if r is not None:
                cfg.runtime_root = r.data_root
                cfg._runtime_root_from_roots = True       # a plain attribute, not a field: never persisted
        return cfg

    @staticmethod
    def _persisted(config: GnomonConfig) -> Dict[str, object]:
        """What a save writes. ``deploy_root`` is persisted ONLY when explicitly set to a value different from
        the default: it is a dataclass field (so it is in the config API payload), and writing the provisional
        default into config.json would pin it and break the operator's one-line rename (``DEFAULT_DEPLOY_ROOT``)."""
        data = config.to_dict()
        if data.get("deploy_root") == DEFAULT_DEPLOY_ROOT:
            del data["deploy_root"]
        r = _adopted_roots()
        if r is not None and data.get("runtime_root") == r.data_root:
            del data["runtime_root"]          # derived from the roots file, never pinned into config.json
        return data

    def security_flags_turned_off(self, config: GnomonConfig) -> List[str]:
        """The SECURITY flags (``flags.SECURITY_FLAGS``) and top-level security settings (``flags.SECURITY_SETTINGS``) that are
        effectively ON in the file now and OFF in ``config``."""
        from . import flags as _flags

        try:
            old = self.load()
        except Exception:  # noqa: BLE001 - an unreadable file reads as the defaults (security flags ON)
            old = GnomonConfig()
        out = [n for n in _flags.SECURITY_FLAGS
               if _flags.flag(n, config=old) and not _flags.flag(n, config=config)]
        # the top-level security settings (flags.SECURITY_SETTINGS) are listed the same way: ON in the file now,
        # OFF in ``config``. A save that changes only other keys, or turns the setting ON, lists nothing.
        for n in _flags.SECURITY_SETTINGS:
            if getattr(old, n, True) is not False and getattr(config, n, True) is False:
                out.append(n)
        return out

    def save(self, config: GnomonConfig, *, presence_grant: Any = None) -> str:
        """Persist ``config`` atomically; returns the written path.

        Turning a SECURITY flag OFF is refused (:class:`PresenceRequired`, nothing written) unless ``presence_grant`` is
        a live grant minted by ``human_confirm.prove_presence_grant`` for exactly that change (delta audit D1: the
        library writer must not be a way round the Config tab's presence check). Turning one ON, and every other
        change, is free."""
        off = self.security_flags_turned_off(config)
        if off:
            from . import human_confirm as _hc

            if not _hc.consume_grant(presence_grant, _hc.change_digest(off)):
                raise PresenceRequired(
                    f"refusing to turn the security setting(s) {', '.join(off)} OFF without a passed human-presence "
                    f"check (use the dashboard's Configuration tab); nothing was written")
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self._persisted(config), fh, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, self._path)
        return self._path

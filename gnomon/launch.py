"""The launch pipeline (launcher, render).

:class:`LaunchManager.launch` turns a :class:`~gnomon.profile.Profile`
into a ready-to-run session:

a. a fresh session dir ``~/gnomon-data/sessions/<instance_id>-<UTC>/.claude/``
 (build-session-dir, D31);
b. ``.claude/CLAUDE.md`` rendered from ``claude_md_source`` with REAL D8
   ``{{lts: … }}`` expansion (``lts_expand.LtsExpander``, 2026-09-29), written as a
   retained ``.claude/agent-def/NNN-<event>-<UTC>.md`` artifact and delivered as
   ``.claude/CLAUDE.md`` (see ``agent_def.py``; the harness re-renders at spawn and
   at every PreCompact);
c. a tracking canary JSON (liveness uses it — see ``liveness.py``);
d. ``.claude/.mcp.json`` wiring the D9 default server set (lts-engine,
   gnomon) — ⛔ never a measurement-surface server (D9);
e. ``.claude/settings.local.json`` with ``permissions.defaultMode =
   "bypassPermissions"`` — the load-bearing pin that survives Claude Code's
 ~8s re-exec (bypass-pin-rationale, D11);
f. the terminal launch COMMAND, built from the config-driven template
   (``terminal.py``). The command is RETURNED; actually opening a terminal is a
   separate, guarded method (:meth:`LaunchManager.open_terminal`) so tests never
   spawn a real window.

The multi-line bootstrap prompt and the proxy's own launch parameters are
written to ``.claude/launch.json`` in the session dir rather than shoved through
argv, so the terminal command stays a short, escape-safe ``... -m
gnomon.harness <session_dir>``. The harness launcher reads launch.json.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from . import buildstamp
from .config import DEFAULT_CLAUDE_BIN, GnomonConfig, default_mcp_endpoints, sessions_root_default
from .profile import Profile, _validate_instance_id, default_intelligences_root, ensure_contained
from .terminal import render_terminal_command, shell_join


# --- CLAUDE.md rendering + the D8 lts-query-expansion seam ------------------

#: The query-reference syntax a profile's CLAUDE.md template MAY carry (D8).
#: Example: ``{{lts: example-corpus:EXAMPLE-NODE }}``. Slice-2a only DETECTS these
#: and routes them to an expander hook; it does NOT resolve them. Resolution
#: (call lts-engine, inline the results) is greenfield owed to a later slice
#: (D8). The default expander leaves the marker
#: verbatim and records it as pending, so a launch is never silently missing the
#: content the marker promised.
#: 2026-09-29 remediation: an optional leading backslash (``\{{lts: … }}``) is the
#: ESCAPE — rendered as the literal marker (backslash dropped), never expanded.
LTS_QUERY_PATTERN = re.compile(r"(?P<esc>\\)?\{\{\s*lts:\s*(?P<query>.*?)\s*\}\}", re.DOTALL)

#: Signature of an lts-query expander. Given the raw query string (the text
#: inside ``{{lts: ... }}``), it returns the inlined replacement text. A later slice delivers
#: the real one; until then the default below is used.
Expander = Callable[[str], str]


def default_expander(query: str) -> str:
    """The DOCUMENTED NO-OP hook (D8 is owed to a later slice).

    ⚠ SEAM ONLY — this does not call lts-engine. It leaves the marker in place so
    the rendered CLAUDE.md is honest about what has not been inlined yet, and the
    renderer records the query in ``lts_expansion_pending`` on the result. Swap
    this callable for the real resolver when that slice delivers the conceptual summary.
    """
    return "{{lts: " + query + " }}"


@dataclass
class RenderResult:
    text: str
    #: Queries detected but NOT resolved: every marker under the default no-op
    #: expander, and every FAILED expansion under the real one (2026-09-29).
    lts_expansion_pending: List[str] = field(default_factory=list)
    #: Per-marker outcome records from a DETAILED expander (one with an
    #: ``expand(query) -> ExpansionOutcome`` method, e.g.
    #: :class:`gnomon.lts_expand.LtsExpander`): query/status/reason/matched.
    lts_expansions: List[dict] = field(default_factory=list)
    #: warnings from the include/lts/symbol pipeline (e.g. an ``{{include}}`` found
    #: inside lts-expanded content: NOT expanded, named here), the fragments the render touched
    #: (name -> sha256) and the ``${symbols}`` it substituted.
    warnings: List[str] = field(default_factory=list)
    fragments: Dict[str, str] = field(default_factory=dict)
    symbols_used: List[str] = field(default_factory=list)


class ClaudeMdRenderer:
    """Renders a profile's CLAUDE.md template into session ``.claude/CLAUDE.md``.

 v1 behaviour: a BASIC copy/render of the
    ``claude_md_source`` template — read the file, expand any ``{{lts: ...}}``
    markers via the injected ``expander`` (default: the no-op D8 seam), write the
    result. It intentionally does NOT do provision.py's externally coupled steps (no
    SSH bootstrap decrypt, no external relay).

    2026-09-29: an expander that exposes ``expand(query)`` (the REAL
    :class:`~gnomon.lts_expand.LtsExpander`) is driven through it, so each
    marker's outcome (ok / empty-ok / failed + reason) is returned in
    ``lts_expansions`` and a failed marker is ALSO listed in
    ``lts_expansion_pending``. A plain callable expander behaves exactly as before.
    """

    def __init__(self, expander: Optional[Expander] = None) -> None:
        self._expander = expander if expander is not None else default_expander
        self._is_default = expander is None

    @property
    def expander(self) -> Expander:
        return self._expander

    @property
    def is_default(self) -> bool:
        return self._is_default

    def render(self, template_text: str) -> RenderResult:
        pending: List[str] = []
        details: List[dict] = []
        detailed = getattr(self._expander, "expand", None)
        accepts = getattr(self._expander, "accepts", None)

        def _sub(match: "re.Match[str]") -> str:
            query = match.group("query")
            if match.group("esc"):
                return match.group(0)[1:]  # escaped: the literal marker, backslash dropped
            if callable(accepts) and not accepts(query):
                # Not macro-shaped (e.g. prose ABOUT the syntax): leave verbatim —
                # never a false FAILED banner. (2026-09-29 remediation, Diamond #1.)
                return match.group(0)
            if self._is_default:
                pending.append(query)
                return self._expander(query)
            if callable(detailed):
                outcome = detailed(query)
                details.append(outcome.to_record())
                if not outcome.ok:
                    pending.append(query)
                return outcome.text
            return self._expander(query)

        rendered = LTS_QUERY_PATTERN.sub(_sub, template_text)
        return RenderResult(text=rendered, lts_expansion_pending=pending, lts_expansions=details)

    def render_file(self, source_path: str, *, role: Optional[str] = None,
                    session_root: Optional[str] = None, paths=None) -> RenderResult:
        """Render a template FILE. the text runs through the ONE renderer
        (``agent_render.run_pipeline``): ``{{include: name}}`` -> ``{{lts: … }}`` (:meth:`render`,
        unchanged) -> ``${symbols}``. ``role`` / ``session_root`` feed the reserved ``${ROLE}`` /
        ``${SESSION_ROOT}`` symbols. Include / symbol errors raise
        :class:`gnomon.agent_render.RenderError`; an unreadable file raises OSError."""
        if not source_path:
            # No template configured: emit a minimal, honest placeholder rather
            # than crashing the launch. A profile SHOULD carry a claude_md_source.
            return RenderResult(
                text="# CLAUDE.md\n\n(no claude_md_source configured for this profile)\n"
            )
        from .agent_render import run_pipeline

        with open(os.path.expanduser(source_path), "r", encoding="utf-8") as fh:
            raw = fh.read()
        out = run_pipeline(raw, lts_render=self.render, paths=paths, role=role, session_root=session_root)
        return RenderResult(text=out.text, lts_expansion_pending=out.lts_pending,
                            lts_expansions=out.lts_expansions, warnings=out.warnings,
                            fragments=out.fragments, symbols_used=out.symbols_used)


# --- the default MCP server catalog ----------------------------------------

def _default_mcp_catalog(hub_url: Optional[str]) -> Dict[str, dict]:
    """Connection configs for the known default servers, wired into ``.mcp.json``.

    These are build-time defaults; a deployment can override the catalog. The
    ``gnomon`` entry points at the co-hosted hub's MCP URL when known.
    """
    catalog: Dict[str, dict] = {
        "lts-engine": {"type": "stdio", "command": "lts-engine"},
    }
    if hub_url:
        catalog["gnomon"] = {"type": "http", "url": hub_url}
    else:
        # No live hub URL yet: an explicit placeholder, so the entry is present
        # (the D9 set is complete) but honest that the host wires the URL at
        # launch once the co-hosted hub has bound its port.
        catalog["gnomon"] = {
            "type": "http",
            "url": None,
            "_note": "hub MCP url wired at launch by the host/GUI",
        }
    return catalog


def _catalog_from_config(config: Optional[GnomonConfig], hub_url: Optional[str]) -> Dict[str, dict]:
    """The MCP endpoint catalog, sourced from the machine config (slice 4).

    Starts from the config's ``mcp_endpoints`` (defaults when no config), then
    forces the ``gnomon`` entry to the LIVE hub url when one is known — the
    hub url is dynamic and must win over any persisted placeholder. With no config
    this is byte-identical to :func:`_default_mcp_catalog`, so direct
    LaunchManager callers (slice-2a tests) are unaffected.
    """
    endpoints = config.mcp_endpoints if config is not None else default_mcp_endpoints()
    catalog: Dict[str, dict] = {name: dict(cfg) for name, cfg in endpoints.items()}
    if hub_url:
        catalog["gnomon"] = {"type": "http", "url": hub_url}
    else:
        catalog.setdefault(
            "gnomon",
            {"type": "http", "url": None, "_note": "hub MCP url wired at launch by the host/GUI"},
        )
    return catalog


@dataclass
class LaunchResult:
    """Everything :meth:`LaunchManager.launch` produced."""

    instance_id: str
    session_dir: str          # the <...>-<UTC>/ dir (its .claude/ holds the files)
    claude_dir: str
    claude_md_path: str
    canary_path: str
    mcp_json_path: str
    settings_path: str
    launch_json_path: str
    launch_command: str       # the osascript terminal command (RETURNED, not run)
    inner_command: str        # the harness-proxy-wrapping-claude command
    lts_expansion_pending: List[str] = field(default_factory=list)
    #: 2026-09-29: the retained agent-def artifact this launch rendered, and its
    #: expansion summary (status ok / failed / none / no-source + failures).
    agent_def_path: Optional[str] = None
    agent_def_expansion: Optional[dict] = None


class LaunchManager:
    """Builds a session from a profile. Headless: no GUI, no terminal opened."""

    def __init__(
        self,
        sessions_root: Optional[str] = None,
        intelligences_root: Optional[str] = None,
        *,
        hub_url: Optional[str] = None,
        hub_rendezvous_port: Optional[int] = None,
        renderer: Optional[ClaudeMdRenderer] = None,
        mcp_catalog: Optional[Dict[str, dict]] = None,
        python_bin: Optional[str] = None,
        claude_bin: Optional[str] = None,
        config: Optional[GnomonConfig] = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        # ``config`` is an OPTIONAL injected source of the machine-specific bits
        # (slice 4). Explicit args still win over it; it wins over the hardcoded
        # defaults; with no config at all the hardcoded defaults hold — so a
        # direct slice-2a caller that passes neither is unchanged.
        self._config = config
        self._sessions_root = os.path.abspath(
            os.path.expanduser(
                sessions_root
                if sessions_root is not None
                else sessions_root_default()
            )
        )
        self._intelligences_root = os.path.abspath(
            os.path.expanduser(intelligences_root or default_intelligences_root())
        )
        self._hub_url = hub_url
        self._hub_rendezvous_port = hub_rendezvous_port
        # 2026-09-29: ``None`` = the REAL lts expander, a fresh one per launch
        # (``agent_def.render_agent_def``). An injected renderer (tests, or a
        # caller that wants the no-op seam) is used verbatim.
        self._renderer = renderer
        if mcp_catalog is not None:
            self._mcp_catalog = mcp_catalog
        elif config is not None:
            self._mcp_catalog = _catalog_from_config(config, hub_url)
        else:
            self._mcp_catalog = _default_mcp_catalog(hub_url)
        self._python_bin = python_bin or sys.executable or "python3"
        # claude bin: explicit arg > config > the ~/.claude/local/claude default.
        if claude_bin is not None:
            self._claude_bin = claude_bin
        elif config is not None:
            self._claude_bin = config.resolved_claude_bin()
        else:
            self._claude_bin = os.path.expanduser(DEFAULT_CLAUDE_BIN)
        # custom terminal templates, from the config (empty when none) — merged
        # ahead of the built-ins at render time (D4, terminal-agnostic-by-config).
        self._custom_terminal_templates: Dict[str, str] = (
            dict(config.custom_terminal_templates) if config is not None else {}
        )
        self._clock = clock

    @property
    def sessions_root(self) -> str:
        return self._sessions_root

    # -- session-dir naming --------------------------------------------------

    def session_dir_name(self, instance_id: str) -> str:
        # ⛔ SECURITY (slice-2a cold audit): the instance_id names an on-disk
        # directory under sessions_root; a traversal-shaped id
        # (e.g. "../../etc/pwned") would otherwise write the bypass-permissions
        # settings.local.json + CLAUDE.md/canary/.mcp.json OUTSIDE
        # ~/gnomon-data/sessions/. Reject separators/traversal/null at the naming
        # site, reusing profile.py's single validator so the rule lives once.
        _validate_instance_id(instance_id)
        ts = self._clock().strftime("%Y%m%d-%H%M%S")
        return f"{instance_id}-{ts}"

    # -- the launch ----------------------------------------------------------

    def launch(self, profile: Profile) -> LaunchResult:
        # ⛔ SECURITY: validate BEFORE any makedirs/write. session_dir_name also
        # validates (defense in depth), but launch is the public entry point and
        # must fail closed on an unsafe id before touching the filesystem.
        _validate_instance_id(profile.instance_id)
        # fix-pass WS4: a malformed extra_args fails the launch LOUD, before any
        # file is written (same validator the harness re-applies at spawn).
        from .harness.spawn import validate_extra_args

        extra_args = validate_extra_args(getattr(profile, "extra_args", None))
        # Security posture D3 (2026-09-29): a malformed env_passthrough (a glob, a
        # non-list) also fails LOUD here, before any file is written.
        from .harness.spawn import validate_env_passthrough

        env_passthrough = validate_env_passthrough(getattr(profile, "env_passthrough", None))
        # RESUME MODE (session adoption, 2026-09-28): a resume profile MUST carry
        # a resume_cwd — `claude --resume <id>` resolves the session id within the
        # project dir, so the child cwd is load-bearing. Fail LOUD here, before any
        # file is written / terminal opens, like extra_args.
        resume_session_id = getattr(profile, "resume_session_id", None)
        resume_cwd = getattr(profile, "resume_cwd", None)
        resume_prompt = getattr(profile, "resume_prompt", None)
        if resume_session_id:
            if not isinstance(resume_session_id, str):
                raise ValueError("resume_session_id must be a string")
            if not isinstance(resume_cwd, str) or not resume_cwd:
                raise ValueError(
                    "a resume profile (resume_session_id set) requires a non-empty string "
                    "'resume_cwd' — claude --resume resolves the session id within the project dir"
                )
        session_dir = os.path.join(self._sessions_root, self.session_dir_name(profile.instance_id))
        claude_dir = os.path.join(session_dir, ".claude")
        # ⛔ SECURITY (slice-2b cold audit MEDIUM): even a SAFE-shaped id yields a
        # session dir a pre-planted symlink could redirect off _sessions_root,
        # sending the bypass-permissions settings.local.json + CLAUDE.md outside.
        # Resolve-and-contain BEFORE makedirs, so a planted symlink is refused.
        ensure_contained(self._sessions_root, claude_dir)
        # X13 S2 (workspace_scope_core, default OFF): validate the scope core's launch preconditions BEFORE any
        # file is written, so a refusal leaves nothing behind. With the flag OFF this block is skipped entirely.
        scope_prepared = self._scope_core_prepare(profile, session_dir, resume_session_id)
        os.makedirs(claude_dir, exist_ok=True)

        # (b) CLAUDE.md — 2026-09-29: rendered through the REAL {{lts: … }} expander
        # into a NEW retained artifact (.claude/agent-def/NNN-<event>-<UTC>.md) and
        # delivered as .claude/CLAUDE.md. Failures carry a loud banner in the file
        # and are recorded in launch.json (below) + the provenance log.
        from .agent_def import apply_to_launch_cfg, record_provenance, render_agent_def, superseded_paths

        agent_def = render_agent_def(
            session_dir, profile.claude_md_source,
            "resume" if resume_session_id else "launch",
            renderer=self._renderer, clock=self._clock, record=False, config=self._config,
            mode="resume" if resume_session_id else "fresh",
            superseded=superseded_paths(resume_cwd) if resume_session_id else None,
        )
        record_provenance(session_dir, agent_def)
        claude_md_path = os.path.join(claude_dir, "CLAUDE.md")

        # (c) tracking canary
        canary_path = os.path.join(claude_dir, "canary.json")
        canary = {
            "gnomon_canary": True,
            "instance_id": profile.instance_id,
            "display_name": profile.display_name,
            "session_dir": session_dir,
            "created_utc": self._clock().isoformat(),
            "claude_md": claude_md_path,
        }
        _write_json(canary_path, canary)

        # (d) .mcp.json — the D9 default set (never a measurement-surface server)
        mcp_json_path = os.path.join(claude_dir, ".mcp.json")
        _write_json(mcp_json_path, self._build_mcp_json(profile))

        # (e) settings.local.json — the bypass pin (survives the re-exec)
        settings_path = os.path.join(claude_dir, "settings.local.json")
        _write_json(settings_path, {"permissions": {"defaultMode": "bypassPermissions"}})

        # launch.json — the harness launcher's own params (read by the proxy)
        launch_json_path = os.path.join(claude_dir, "launch.json")
        launch_cfg = {
            "instance_id": profile.instance_id,
            "claude_bin": self._claude_bin,
            "bootstrap_prompt": profile.bootstrap_prompt,
            "model": profile.model,
            "hub_rendezvous_port": self._hub_rendezvous_port,
            "session_dir": session_dir,
            # The context-monitor thresholds (final-audit MEDIUM: the field was
            # editable+persisted but never consumed — the monitor was always
            # 90/95). Written here and read by ``harness.launcher.assemble_proxy``,
            # which passes them to ``ContextMonitor``. ``None`` (no override, no
            # config, or an empty list) means "unset" → the harness falls back to
            # ``HOSTED_CHILD_THRESHOLDS`` (90/95).
            #
            # PRECEDENCE (fix-pass 2026-09-27): a profile's non-empty
            # ``context_thresholds`` OVERRIDES the machine-config list; when the
            # profile leaves it ``None``/empty the config-derived value is used
            # UNCHANGED — preserving current behaviour for every existing profile.
            "context_thresholds": self._resolve_context_thresholds(profile),
            # fix-pass (2026-09-27): the ENABLE path for the fail-loud
            # compaction-start confirmer. ``harness.launcher`` reads these at
            # startup (``validate_confirm_compact_config``) but nothing wrote them
            # — only ``context_thresholds`` was propagated. ``confirm_compact``
            # defaults False (confirmer OFF). ``confirm_compact_timeout`` is
            # written only when the profile SETS it; ``None`` → the key is omitted
            # so the harness uses its own default (validated against the self-MCP
            # response-timeout ceiling in ``validate_confirm_compact_config``).
            "confirm_compact": bool(getattr(profile, "confirm_compact", False)),
            # fix-pass WS4 (2026-09-27): the profile's extra claude CLI args,
            # read by ``harness.launcher.build_child_argv`` (e.g. a dev-channel
            # ``--dangerously-load-development-channels server:<name>``).
            "extra_args": extra_args,
        }
        # fix-pass (2026-09-27): only EMIT ``confirm_compact_timeout`` when the
        # profile sets it. Omitting the key (rather than writing ``null``) lets the
        # harness fall through to its own default, and keeps launch.json unchanged
        # for every profile that leaves the field at its ``None`` default.
        cc_timeout = getattr(profile, "confirm_compact_timeout", None)
        if cc_timeout is not None:
            launch_cfg["confirm_compact_timeout"] = float(cc_timeout)
        # RESUME MODE (2026-09-28): emit the resume keys ONLY when a resume session
        # id is set, so launch.json is byte-identical for every fresh profile. The
        # harness (`build_child_argv` / `resolve_child_cwd`) reads them; when absent
        # the cold-start path is unchanged. resume_cwd was validated above.
        if resume_session_id:
            launch_cfg["resume_session_id"] = resume_session_id
            launch_cfg["resume_cwd"] = resume_cwd
            if resume_prompt:
                launch_cfg["resume_prompt"] = resume_prompt
        # PTY-hooks WS-H (2026-09-27): the harness settings, resolved ONCE here
        # from the machine config + this profile (the harness also resolves them
        # itself when a pre-hooks dashboard wrote launch.json without them).
        launch_cfg.update(self._harness_launch_keys(profile))
        # Security posture D3: emitted ONLY when set, so launch.json is unchanged for
        # every profile without one. The harness reads it at the claude spawn.
        if env_passthrough:
            launch_cfg["env_passthrough"] = env_passthrough
        # 2026-09-29: the agent-def render record (agent_def_rendered_at / _source /
        # _path / _expansion + the agent_def_renders history). The harness re-renders
        # at spawn and the PreCompact hook at every compaction, reading
        # agent_def_source from here.
        apply_to_launch_cfg(launch_cfg, agent_def)
        # (2026-09-29): the build id of the tree ON DISK now — the code the
        # new session will import. ``build_id`` (+ ``build_stamp``), or ``build_id:
        # None`` + ``build_error``; never raises. Read back by the web Live list's
        # stale-build chip and by tools/golive_smoke.py.
        launch_cfg.update(buildstamp.launch_stamp())
        _write_json(launch_json_path, launch_cfg)
        # X13 S2: the relaunch bundle + the session's scope state (flag ON only).
        if scope_prepared is not None:
            scope_prepared.write(session_dir, claude_md_path)

        # (f) terminal command from the template
        inner_command = self._build_inner_command(session_dir)
        launch_command = render_terminal_command(
            profile.terminal,
            session_dir,
            inner_command,
            custom_templates=self._custom_terminal_templates,
        )

        return LaunchResult(
            instance_id=profile.instance_id,
            session_dir=session_dir,
            claude_dir=claude_dir,
            claude_md_path=claude_md_path,
            canary_path=canary_path,
            mcp_json_path=mcp_json_path,
            settings_path=settings_path,
            launch_json_path=launch_json_path,
            launch_command=launch_command,
            inner_command=inner_command,
            lts_expansion_pending=list(agent_def.get("pending") or []),
            agent_def_path=agent_def.get("path"),
            agent_def_expansion=launch_cfg.get("agent_def_expansion"),
        )

    # -- pieces --------------------------------------------------------------

    def _scope_core_prepare(self, profile: Profile, session_dir: str, resume_session_id: Optional[str]):
        """X13 S2: ``None`` when ``workspace_scope_core`` is OFF (no file is read or written and no
        scope-core module is loaded; the ONLY side effect is importing ``gnomon.workspace`` and its stdlib-only
        ``scopebase``, for the flag name; measured by the S2 Diamond);
        else the validated launch plan of the scope core (``workspace.launchhook.prepare``), which raises a
        ``ScopeError`` (home workspace unresolved/archived, machine.json unreadable) BEFORE the launch writes anything.
        With no injected machine config the machine flags are treated as empty (hermetic: a direct caller never reads the
        live config)."""
        from . import flags
        from .workspace.scopebase import SCOPE_CORE_FLAG

        cfg = self._config if self._config is not None else _NO_MACHINE_FLAGS
        if not flags.flag(SCOPE_CORE_FLAG, profile=profile, config=cfg):
            return None
        from .config import DEFAULT_DEPLOY_ROOT
        from .workspace import launchhook

        deploy_root = self._config.resolved_deploy_root() if self._config is not None else os.path.abspath(os.path.expanduser(DEFAULT_DEPLOY_ROOT))
        return launchhook.prepare(instance_id=profile.instance_id, deploy_root=deploy_root, profile_raw=profile.to_dict(),
                                  sessions_root=self._sessions_root, session_dir=session_dir, conversation_key=resume_session_id,
                                  clock=self._clock, enabled=True)      # the flag was resolved ON just above (a profile override counts)

    def _resolve_context_thresholds(self, profile: Profile) -> Optional[List[int]]:
        """The context-monitor thresholds for launch.json, profile-over-config.

        A profile's non-empty ``context_thresholds`` list WINS (per-instance
        override). Otherwise the machine-config list is used, exactly as before
        (final-audit MEDIUM wiring). ``None`` when neither supplies one → the
        harness falls back to ``HOSTED_CHILD_THRESHOLDS`` (90/95). Returns a fresh
        list so the caller can never mutate the profile's or the config's.
        """
        prof_override = getattr(profile, "context_thresholds", None)
        if prof_override:
            return list(prof_override)
        if self._config is not None and self._config.context_thresholds:
            return list(self._config.context_thresholds)
        return None

    def _harness_launch_keys(self, profile: Profile) -> dict:
        """PTY-hooks keys for launch.json. Fails LOUD on a malformed stall
        override (before the terminal opens), like ``extra_args``."""
        from .harness.constants import merge_stall_inject
        from .harness.hooks import validate_manifest
        from .harness.opcommands import resolve_user_name

        cfg = self._config
        override = getattr(profile, "stall_inject", None)
        stall = merge_stall_inject(getattr(cfg, "stall_inject", None), override)
        manifest = validate_manifest(getattr(cfg, "hook_manifest", None) or {})
        global_launch = bool(getattr(cfg, "agent_launch_enabled", False))
        return {
            "user_name": resolve_user_name(getattr(cfg, "user_name", None)),
            "stall_inject": stall,
            "hook_manifest": manifest,
            "operator_command_plane": bool(getattr(cfg, "operator_command_plane", True)),
            # WS-G: a dashboard launch is an operator ROOT (depth 0); it may
            # launch agents only if BOTH the machine config and the profile say so.
            "agent_depth": 0,
            "agent_launch_enabled": global_launch and bool(getattr(profile, "agent_launch_enabled", False)),
        }

    def _build_mcp_json(self, profile: Profile) -> dict:
        servers: Dict[str, dict] = {}
        for name in profile.mcp_servers:
            servers[name] = dict(
                self._mcp_catalog.get(name, {"_note": f"config owed for {name}"})
            )
        return {"mcpServers": servers}

    def _build_inner_command(self, session_dir: str) -> str:
        """The harness-proxy-wrapping-claude command run inside the terminal.

        ``<python> -m gnomon.harness <session_dir>`` — the harness
        launcher (``harness/launcher.py``) reads ``.claude/launch.json`` for the
        claude bin, bootstrap prompt and hub port, then spawns claude under the
 PTY proxy (launch-command-shape). ``exec`` so the
        proxy replaces the shell and owns the tty.
        """
        argv = [self._python_bin, "-m", "gnomon.harness", session_dir]
        return "exec " + shell_join(argv)

    # -- the guarded side effect (NOT exercised by tests) --------------------

    def open_terminal(self, result: LaunchResult, runner: Optional[Callable[[str], int]] = None) -> int:
        """ACTUALLY open the terminal. Guarded: tests pass a fake ``runner`` or
        never call this. The default runner shells out to ``osascript`` via the
        rendered command — a real side effect, deliberately isolated here.
        """
        if runner is None:  # pragma: no cover - real side effect
            import subprocess

            return subprocess.call(result.launch_command, shell=True)
        return runner(result.launch_command)


class _NoMachineFlags:
    """A machine-config stand-in with no flags set (X13 S2: LaunchManager without an injected config reads none)."""

    flags: Dict[str, bool] = {}


_NO_MACHINE_FLAGS = _NoMachineFlags()


# --- small IO helpers -------------------------------------------------------

def _write_text(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _write_json(path: str, obj: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2)
        fh.write("\n")

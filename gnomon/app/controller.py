"""The GUI Controller — all app logic, ZERO CustomTkinter dependency.

The slice-2b GUI is split MVC-style so the logic is testable without a display
([task] Architecture — MVC separation):

* THIS module (:class:`Controller`) holds every decision the app makes — list
  profiles, create/delete/edit them, launch a session, scan liveness, read
  per-session context %, and own the co-hosted :class:`~gnomon.hub.Hub`'s
  lifecycle. It imports NO ``customtkinter`` and never touches a widget, so it is
  unit-tested headlessly (mock the store/launcher/hub; no display, no terminal).
* ``gui.py`` is the ONLY module that imports ``customtkinter``; its widgets are
  thin and call straight into this Controller.

Everything the Controller does is built ON slices 1+2a — it constructs and calls
:class:`~gnomon.profile.ProfileStore`,
:class:`~gnomon.launch.LaunchManager`,
:class:`~gnomon.liveness.LivenessProbe` and
:class:`~gnomon.hub.Hub`. It re-implements none of them.

Side effects are GUARDED the same way slice-2a guards ``open_terminal``: opening
a real terminal and opening the OS editor both take an injectable runner/opener,
so tests exercise the wiring without spawning a window.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..config import GnomonConfig
from ..dsd.notice import NoticeQueue
from ..hub.rendezvous import rendezvous_path
from ..hub import DSDStore, Hub, SessionRegistry
from ..hub.apps import AppLifecycle, AppRegistry, default_registry_path, default_state_root
from ..hub.registry import UnknownSession
from ..launch import LaunchManager, LaunchResult
from ..liveness import LivenessProbe, SessionStatus
from ..profile import (
    Profile,
    ProfileStore,
    default_intelligences_root,
)
from ..terminal import available_terminal_templates


def default_sessions_root() -> str:
    """``<data_root>/sessions`` — the hot session working-dir home (D31; X13 S5-4: ``config.runtime_root_default``)."""
    from ..config import sessions_root_default

    return sessions_root_default()


@dataclass(frozen=True)
class LiveSessionView:
    """One row in the Agents-tab live-sessions view.

    Merges :class:`~gnomon.liveness.SessionStatus` (on-disk canary +
    process + transcript freshness) with the proxy-reported ``used_percentage``
    the co-hosted hub holds in its live registry, when the session is registered.
    """

    instance_id: str
    session_dir: str
    live: bool
    reason: str
    used_percentage: Optional[float]


class _ReResolvingDecisionWriter:
    """A ``DecisionWriter`` that re-resolves its real target from the LIVE config
    at ``mint()``/``status()`` time ([final-audit LOW]).

    The DSD tab holds its :class:`DsdTabController` — and thus its writer — from
    app-init. Before this, a mid-session ``reload_config`` that moved the DSD-answer
    corpus path left that already-open tab writing to the OLD target. This wrapper
    closes the gap the lazy way: it holds a ``path_provider`` (the Controller's
    ``dsd_answers_corpus_path`` bound method, which reads the CURRENT config) and
    builds/rebuilds the real writer whenever the resolved path changes. So a config
    save propagates to where answers land WITHOUT rebuilding the cached controller.
    """

    def __init__(self, path_provider: Callable[[], str]) -> None:
        self._path_provider = path_provider
        self._built_for: Optional[str] = None
        self._delegate = None

    def _resolve(self):
        from ..dsd.writer import make_decision_writer

        path = self._path_provider()
        if self._delegate is None or self._built_for != path:
            self._delegate = make_decision_writer(path)
            self._built_for = path
        return self._delegate

    def mint(self, nodes):
        return self._resolve().mint(nodes)

    @property
    def path(self) -> str:
        return getattr(self._resolve(), "path", "") or ""

    @property
    def corpus_id(self) -> str:
        from ..dsd.live import default_corpus_id

        return getattr(self._resolve(), "corpus_id", None) or default_corpus_id()

    def status(self):
        from ..dsd.live import DsdCorpusError
        from ..dsd.writer import WriterStatus, writer_status

        try:
            return writer_status(self._resolve())
        except DsdCorpusError as exc:      # g68 (S5-4b): a bad dsd-corpus.json REFUSES, visibly; a status read never raises
            return WriterStatus(kind="unknown", target=self._built_for or "", detail=f"REFUSED: {exc}")


def _hub_caller_binding_flag(config: Any) -> bool:
    """hub_caller_binding from the machine config (registry default OFF). Never raises: a read fault is OFF."""
    try:
        from .. import flags as flags

        return bool(flags.flag("hub_caller_binding", config=config))
    except Exception:  # noqa: BLE001
        return False


def _flag_from_config(name: str, config: Any) -> bool:
    """A feature flag from a machine config object (registry default when unset). Never raises: a read fault is OFF."""
    try:
        from .. import flags as flags

        return bool(flags.flag(name, config=config))
    except Exception:  # noqa: BLE001
        return False


class Controller:
    """Headless application logic for the Gnomon GUI.

    All collaborators are injectable so the whole surface is unit-testable
    without a display, a real terminal, or a bound socket. In production the
    defaults wire the real slice-1/2a components; :meth:`start_hub` binds the
    co-hosted hub and (re)builds the default launcher against its live URL so a
    launched session's ``.mcp.json`` points at the running hub.
    """

    def __init__(
        self,
        *,
        profile_store: Optional[ProfileStore] = None,
        launch_manager: Optional[LaunchManager] = None,
        liveness_probe: Optional[LivenessProbe] = None,
        hub: Optional[Hub] = None,
        intelligences_root: Optional[str] = None,
        sessions_root: Optional[str] = None,
        default_terminal: Optional[str] = None,
        config: Optional[GnomonConfig] = None,
    ) -> None:
        # The machine config is an OPTIONAL injected source (slice 4). When none
        # is injected, in-memory defaults are used (hermetic for tests); the real
        # app injects ``ConfigStore().load()`` at start-up. Explicit constructor
        # args (roots/terminal) still win over the config, so tmp-root tests are
        # unaffected and the persisted config never leaks into them.
        self._config = config if config is not None else GnomonConfig()
        self._intelligences_root = os.path.abspath(
            os.path.expanduser(intelligences_root or self._config.intelligences_root())
        )
        self._sessions_root = os.path.abspath(
            os.path.expanduser(sessions_root or self._config.sessions_root())
        )
        self._store = profile_store if profile_store is not None else ProfileStore(self._intelligences_root)
        self._probe = liveness_probe if liveness_probe is not None else LivenessProbe()
        # ⛔ FINAL-AUDIT HIGH — the Hub's DSD WRITE-side must share the SAME
        # config-resolved agents root the Controller reads from. A bare
        # ``Hub()`` builds ``GmMCPService`` with no ``dsd_store``, so it falls back
        # to ``DSDStore(registry=...)`` whose root defaults to the HARDCODED
        # ``profile.default_intelligences_root()`` (``~/gnomon-data/agents``),
        # independent of this Controller's config-driven ``_intelligences_root``.
        # If ``runtime_root`` is changed via the Config tab, ``send_dsd_for_human_
        # review`` would then write DSD records UNDER THE OLD ROOT while the DSD tab
        # (``dsd_controller()``) reads from the NEW one — silent misdirection (the
        # agent gets ``ok:true``, the human never sees the DSD, no error). Construct
        # the Hub's ``DSDStore`` on ``self._intelligences_root`` so the write-side
        # and the read-side share one root.
        if hub is not None:
            self._hub = hub
        else:
            registry = SessionRegistry()
            #: the app whitelist + launch logs are rooted
            # on the SAME config-resolved runtime root (<runtime_root>/apps.json,
            # <runtime_root>/apps/logs), mirroring the DSD root-drift fix above.
            # Lazy: nothing is read from disk until a lifecycle tool is called.
            runtime_root = self._config.runtime_root
            self._hub = Hub(
                registry=registry,
                dsd_store=DSDStore(root=self._intelligences_root, registry=registry),
                app_lifecycle=AppLifecycle(
                    registry=AppRegistry(default_registry_path(runtime_root)),
                    state_root=default_state_root(runtime_root),
                ),
                # fix-pass WS2b (2026-09-27): the operator credential for the
                # hub's actuation tools is written 0600 beside the runtime home
                # (<runtime>/hub/operator.token) when the hub starts. Derived from
                # THIS Controller's resolved agents root (<runtime>/
                # agents), NOT the config default, so a tmp-rooted
                # Controller (tests, a second process) can never overwrite the
                # live hub's token file.
                operator_token_file=os.path.join(
                    os.path.dirname(self._intelligences_root), "hub", "operator.token"
                ),
                # PTY-hooks WS-G: the hub launch_agent verb — OFF unless the LIVE
                # machine config says agent_launch_enabled (read per call, so a
                # Config-tab save takes effect without a restart once this build
                # is running).
                # (spec 13): the Apps host, built lazily on first use, and its flag
                # (apps_tab, default OFF), read from the machine config per call.
                apps_host=self.apps_host,
                apps_enabled=self.apps_enabled,
                agent_launcher=self.launch_agent,
                agent_launch_enabled=lambda: bool(getattr(self._config, "agent_launch_enabled", False)),
                # D15 amendment (operator-approved 2026-09-29): hub discovery + the durable
                # DSD-answer notice queue, on the SAME runtime home as operator.token
                # (derived from THIS Controller's agents root, so a tmp-rooted
                # Controller can never touch the live hub's files).
                # FIX PASS (audit HIGH): the discovery file is EPHEMERAL, machine-
                # local state — NOT under a synced runtime root. It lives
                # in the per-uid run dir, keyed by this runtime root's real path.
                rendezvous_file=rendezvous_path(os.path.dirname(self._intelligences_root)),
                notice_queue=NoticeQueue(
                    os.path.join(os.path.dirname(self._intelligences_root), "notices")
                ),
                # (hub_caller_binding, default OFF): the hub binds callers to their
                # launch credential. Its audit log defaults to caller-audit.jsonl beside rendezvous.json (the
                # machine-local run dir, never the synced runtime root).
                caller_binding=_hub_caller_binding_flag(self._config),
            )
        # An INJECTED launcher is used verbatim (tests); otherwise one is built on
        # demand so it can pick up the hub URL once start_hub has bound a port.
        self._injected_launcher = launch_manager
        self._default_terminal = default_terminal or self._config.default_terminal
        # (stage 1a): wire the lazy Scheduler into the hub's verb service. Nothing is built or read here;
        # with scheduler OFF no scheduler verb is advertised or dispatched.
        try:
            self._hub.service.set_scheduler(self.scheduler, self.scheduler_enabled)
        except AttributeError:   # an injected hub double without a real service
            pass
        self._hub_started: bool = False
        self._rendezvous_port: Optional[int] = None

    # -- the Apps host (lazy; shared by the hub verbs and the web routes) ---------------
    def apps_enabled(self) -> bool:
        from .. import flags as _flags
        from ..appshost.verbs import FLAG as _APPS_FLAG

        return _flags.flag(_APPS_FLAG)

    def apps_host(self):
        """The ONE :class:`~gnomon.appshost.host.AppsHost` of this process (built on first use; nothing
        is read or written at construction). The DATA root is this Controller's config ``runtime_root``."""
        host = getattr(self, "_apps_host", None)
        if host is None:
            from ..appshost import deps as _deps
            from ..appshost.host import AppsHost
            from ..appshost.launchd import RealLaunchd, package_root

            def _root() -> str:
                return os.path.abspath(os.path.expanduser(self._config.runtime_root))

            def _range():
                r = self._config.apps_port_range
                return (int(r[0]), int(r[1])) if r else None

            host = AppsHost(
                _root, RealLaunchd(),
                services=lambda: _deps.service_health_urls(self._config.services),
                port_range=_range,
                source_roots=(package_root(), os.path.expanduser(self._config.project_root)),
            )
            self._apps_host = host
        return host

    # -- the Scheduler (lazy; shared by the hub verbs, the web routes and the tick) -----
    def scheduler_enabled(self) -> bool:
        """scheduler AND confirm_proof: a job is confirmed only after the macOS device-owner check,
 so the scheduler is OFF whenever that proof is off (requirement-flag-off-until-confirm-proof)."""
        from .. import flags as _flags
        from ..human_confirm import FLAG as _PROOF_FLAG
        from ..scheduler.vocab import FLAG as _SCHED_FLAG

        return _flags.flag(_SCHED_FLAG) and _flags.flag(_PROOF_FLAG)

    def scheduler(self):
        """The ONE :class:`~gnomon.scheduler.engine.Scheduler` of this process (built on first use; nothing is
        read or written at construction). The store is ``<runtime_root>/scheduler`` (the DATA root, g69)."""
        sch = getattr(self, "_scheduler", None)
        if sch is None:
            from ..scheduler.engine import Scheduler

            root = os.path.join(os.path.abspath(os.path.expanduser(self._config.runtime_root)), "scheduler")

            def _effect_ledger():
                from .. import flags as _flags

                return _flags.flag("effect_ledger")

            sch = Scheduler(root, registry=lambda: self._hub.registry, effect_ledger_on=_effect_ledger)
            self._scheduler = sch
        return sch

    # -- introspection -------------------------------------------------------

    @property
    def intelligences_root(self) -> str:
        return self._intelligences_root

    @property
    def sessions_root(self) -> str:
        return self._sessions_root

    @property
    def hub(self) -> Hub:
        return self._hub

    @property
    def hub_url(self) -> Optional[str]:
        return self._hub.mcp_url

    @property
    def config(self) -> GnomonConfig:
        """The live machine config feeding launches (slice 4)."""
        return self._config

    def reload_config(self, config: GnomonConfig) -> None:
        """Swap in a freshly-saved machine config (from the Configuration tab).

        Launch-time bits (claude path, MCP endpoints, custom terminal templates,
        new-profile defaults, DSD corpus path) take effect on the NEXT launch,
        since ``_launch_manager`` builds fresh each time. The runtime/project
        ROOTS are resolved once at construction and are NOT moved mid-session (a
        live hub is bound and stores are open under them) — a root change applies
        on the next app start. The Configuration tab states this."""
        self._config = config
        self._default_terminal = config.default_terminal

    # -- profiles (Agents tab) ----------------------------------------

    def list_profile_ids(self) -> List[str]:
        return self._store.list_ids()

    def list_profiles(self) -> List[Profile]:
        """Every profile that parses. A single corrupt ``profile.json`` must not
        blank the whole list, so unreadable ones are skipped (the GUI can still
        offer Edit/Delete on the id via :meth:`list_profile_ids`)."""
        out: List[Profile] = []
        for instance_id in self._store.list_ids():
            try:
                out.append(self._store.read(instance_id))
            except (ValueError, KeyError, OSError):
                continue
        return out

    def profile_path(self, instance_id: str) -> str:
        return self._store.profile_path(instance_id)

    def new_profile(self, instance_id: str, **overrides: object) -> str:
        """Write a fresh ``profile.json`` SKELETON and return its path.

        The skeleton is a valid, plain-JSON default profile (D7/AQ1: NO form —
        editing is editing the JSON). Refuses to clobber an existing profile
        (``ProfileStore.create`` raises ``FileExistsError``). ``instance_id`` is
        validated by the store (traversal/separators rejected)."""
        base: Dict[str, object] = {
            "instance_id": instance_id,
            "display_name": instance_id,
            # Machine-config-driven defaults (slice 4): a new profile inherits the
            # configured default MCP set (D9 — excludes a measurement-surface server), terminal,
            # model and bootstrap unless the caller overrides them.
            "mcp_servers": list(self._config.default_mcp_servers),
            "terminal": self._config.default_terminal,
            "model": self._config.default_model,
            "bootstrap_prompt": self._config.default_bootstrap_prompt,
        }
        base.update(overrides)
        profile = Profile.from_dict(base)
        return self._store.create(profile)

    def delete_profile(self, instance_id: str) -> None:
        self._store.delete(instance_id)

    def open_profile_in_editor(
        self, instance_id: str, opener: Optional[Callable[[str], int]] = None
    ) -> int:
        """Open a profile's JSON in the OS default editor (D7/AQ1: edit = edit the
        JSON). GUARDED like ``LaunchManager.open_terminal``: tests pass a fake
        ``opener``; the default shells out to macOS ``open`` — the real side
        effect, isolated here. Returns the opener's exit code."""
        path = self._store.profile_path(instance_id)
        if not os.path.exists(path):
            raise KeyError(f"no profile for instance_id {instance_id!r}")
        if opener is None:  # pragma: no cover - real side effect
            import subprocess

            return subprocess.call(["open", path])
        return opener(path)

    # -- launch (Agents tab) ------------------------------------------

    def _launch_manager(self) -> LaunchManager:
        if self._injected_launcher is not None:
            return self._injected_launcher
        # Build fresh against the CURRENT hub state so a launched session wires
        # the live hub URL + rendezvous port into its .mcp.json / launch.json.
        # ``config`` feeds the machine-specific bits (claude path, MCP endpoints,
        # custom terminal templates); roots are passed explicitly since the
        # Controller already resolved them (explicit-wins).
        return LaunchManager(
            sessions_root=self._sessions_root,
            intelligences_root=self._intelligences_root,
            hub_url=self.hub_url,
            hub_rendezvous_port=self._rendezvous_port,
            config=self._config,
        )

    def launch(
        self,
        instance_id: str,
        *,
        open_terminal: bool = True,
        terminal_runner: Optional[Callable[[str], int]] = None,
    ) -> LaunchResult:
        """Read the profile, build the session (slice-2a ``LaunchManager.launch``)
        and, unless suppressed, open the terminal (the guarded ``open_terminal``).
        ``terminal_runner`` is forwarded so tests never spawn a window."""
        profile = self._store.read(instance_id)
        manager = self._launch_manager()
        result = manager.launch(profile)
        if open_terminal:
            manager.open_terminal(result, runner=terminal_runner)
        return result

    def launch_agent(self, args: Dict[str, object], *, launch_fn: Optional[Callable[..., object]] = None) -> Dict[str, object]:
        """PTY-hooks WS-G: the hub ``launch_agent`` verb's body.

        Launches with OPERATOR authority (the hub process has no harness
        ancestor, so ``agent_launch`` resolves it as the operator: a depth-0
        root). Headless runs in the background (the HTTP call must not block on
        a long ``claude -p``). Gating (``agent_launch_enabled``) is enforced by
        the MCP service before this is called."""
        if launch_fn is None:
            from ..harness.agent_launch import launch_child as launch_fn  # type: ignore[no-redef]
        headless = bool(args.get("headless", False))
        report = launch_fn(
            str(args["profile"]), str(args.get("prompt", "") or ""), headless=headless,
            instance_id=args.get("instance_id"), terminal=args.get("terminal"),
            enable_child_launch=bool(args.get("enable_agent_launch", False)),
            background=headless, config=self._config,
        )
        out = report.to_dict() if hasattr(report, "to_dict") else dict(report)  # type: ignore[arg-type]
        out["ok"] = bool(out.get("ok"))
        return out

    # -- liveness (Agents tab live-sessions view) ---------------------

    def scan_sessions(self) -> List[SessionStatus]:
        return self._probe.scan(self._sessions_root)

    def session_context(self, instance_id: str) -> Optional[float]:
        """The proxy-reported ``used_percentage`` from the live hub registry, or
        ``None`` when the session is not currently registered."""
        try:
            return self._hub.registry.status(instance_id)["used_percentage"]
        except UnknownSession:
            return None

    def live_sessions(self) -> List[LiveSessionView]:
        """The Agents-tab live-sessions rows: on-disk liveness merged with
        the live context % the hub holds for registered sessions."""
        rows: List[LiveSessionView] = []
        for status in self.scan_sessions():
            rows.append(
                LiveSessionView(
                    instance_id=status.instance_id,
                    session_dir=status.session_dir,
                    live=status.live,
                    reason=status.reason,
                    used_percentage=self.session_context(status.instance_id),
                )
            )
        return rows

    # -- hub lifecycle -------------------------------------------------------

    def start_hub(self, mcp_port: int = 0, rendezvous_port: int = 0) -> Dict[str, object]:
        """Start the co-hosted hub (MCP HTTP + rendezvous) on background threads.
        Idempotent: a second call is a no-op that returns the current addresses."""
        if self._hub_started:
            return {
                "mcp_url": self.hub_url,
                "rendezvous_address": self._hub.rendezvous_address,
                "already_started": True,
            }
        info = self._hub.start(mcp_port=mcp_port, rendezvous_port=rendezvous_port)
        self._hub_started = True
        rv = self._hub.rendezvous_address
        self._rendezvous_port = rv[1] if rv else None
        return info

    def stop_hub(self) -> None:
        if not self._hub_started:
            return
        self._hub.stop()
        self._hub_started = False

    # -- DSD tab (slice 3b) --------------------------------------------------

    #: pre-review fix round (D-F9): the ONE function the DSD flags are read through. The web job sets ``flag_reader`` to its own
    #: ``_flag`` so the tab, the held count AND the node fields mint decision cannot disagree; without one (the desktop shell, tests)
    #: it reads this Controller's own config.
    flag_reader: Optional[Callable[[str], bool]] = None

    def dsd_flag(self, name: str) -> bool:
        if self.flag_reader is not None:
            try:
                return bool(self.flag_reader(name))
            except Exception:  # noqa: BLE001
                return False
        return _flag_from_config(name, self._config)

    def dsd_answers_corpus_path(self) -> str:
        """The D14 DSD-answer corpus — a corpus SEPARATE from Gnomon's own
 spec corpus (on-save), living in the runtime home
 (``~/gnomon-data``), external to the git checkout.

        An explicit ``dsd_answers_corpus_path`` in the machine config wins (slice
        4). Otherwise it is DERIVED from this Controller's resolved agents
        root, so an explicit tmp root (tests) keeps the corpus hermetic under it
        rather than jumping to the config's default runtime home."""
        if self._config.dsd_answers_corpus_path:
            return os.path.abspath(os.path.expanduser(self._config.dsd_answers_corpus_path))
        runtime_home = os.path.dirname(self._intelligences_root)
        return os.path.join(runtime_home, "dsd-answers", "dsd-answers.lts")

    def dsd_controller(self):
        """Build the headless :class:`~gnomon.app.dsd_controller.
        DsdTabController` over the co-hosted hub's live registry (so a saved answer
        injects the root node-id back to the requesting proxy, D15), the DSD inbox
        (read side of the hub's on-disk DSD store), the REAL index-refreshing
        writer (carry-forward item 1), and the bounded supersession resolver."""
        from ..dsd.inbox import DsdInbox
        from ..dsd.roundtrip import DsdRoundTrip
        from ..dsd.resolve import make_supersession_resolver
        from .dsd_controller import DsdTabController

        inbox = DsdInbox(self._intelligences_root)
        # ⛔ FINAL-AUDIT LOW — the writer re-resolves its target from the CURRENT
        # config at persist time, so a mid-session ``reload_config`` (Config-tab
        # save) that moves ``dsd_answers_corpus_path`` propagates even to a DSD tab
        # holding this controller from app-init. ``dsd_answers_corpus_path`` (the
        # bound method) is the live path provider.
        writer = _ReResolvingDecisionWriter(self.dsd_answers_corpus_path)
        # D15 amendment: the durable notice queue is the hub's (the hub drains it
        # on every (re)register and deletes only on the harness's ack).
        roundtrip = DsdRoundTrip(
            self._hub.registry, writer,
            notice_queue=getattr(self._hub, "notice_queue", None),
            answered_by=(getattr(self._config, "user_name", "") or "").strip() or None,
            answer_fields=lambda: self.dsd_flag("dsd_notice_inline"),
        )
        # Bounded text-search fallback; the live
        # cross-corpus supersede probe is a HELD dependency (-
        # PROBE), so the fallback returns no referrers and the browse view surfaces
        # the bounded caveat rather than asserting an unbounded "current".
        resolver = make_supersession_resolver(find_fn=lambda _t: [])
        return DsdTabController(inbox, roundtrip, resolver=resolver, writer=writer)

    # -- configuration tab (read-only surface for v1) ------------------------

    def config_summary(self) -> Dict[str, object]:
        """The read-only settings the Configuration placeholder surfaces in v1
        (full editable Config is slice 4): the terminal-template choice and the
 ``~/gnomon-data`` roots. Machine-specific."""
        return {
            "default_terminal": self._default_terminal,
            "terminal_templates": available_terminal_templates(
                self._config.custom_terminal_templates
            ),
            "intelligences_root": self._intelligences_root,
            "sessions_root": self._sessions_root,
            "hub_url": self.hub_url,
            "editable_in": "slice 4",
        }

"""The Configuration-tab Controller — all Config logic, ZERO CustomTkinter dep.

MVC split, mirroring the app :class:`~gnomon.app.controller.Controller`:

* THIS module (:class:`ConfigController`) owns every decision the Configuration
  tab makes — load the machine config, edit its fields with validation, add/remove
  a custom terminal template, save it back to the store, and describe the fields
  for a generic view. It imports NO ``customtkinter`` and is unit-tested headless.
* ``config_tab.py`` is a THIN CTk widget that renders the fields this controller
  describes and forwards edits/saves straight into it.

The machine config itself (schema + persistence) lives in
:mod:`gnomon.config`; this controller is the tab's editing surface over a
:class:`~gnomon.config.ConfigStore`. On :meth:`save` it persists, and (when
wired) notifies the app Controller so launch-time bits take effect on the next
launch (slice 4).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from ..config import REFUSED_DEFAULT_MCP_SERVERS, ConfigStore, GnomonConfig
from ..terminal import TERMINAL_TEMPLATES, available_terminal_templates


#: The built-in terminal templates a custom entry may NOT be required to redefine
#: (it may shadow one, but these always exist).
BUILTIN_TEMPLATE_KEYS = tuple(sorted(TERMINAL_TEMPLATES))


@dataclass(frozen=True)
class FieldSpec:
    """Describes one editable config field for the generic view.

    ``kind`` drives the widget the tab renders and how :meth:`ConfigController.set`
    parses the string the user typed back into the typed config value.
    """

    key: str
    label: str
    # "path" | "text" | "csv" | "int_csv" | "int" | "bool" | "multiline" | "json"
    # "int"       — a plain scalar integer (blank -> None only where the schema permits).
    # "multiline" — a multi-line free-text value (rendered as a textarea).
    kind: str
    help: str = ""
    #: A unit hint the view may render beside the control (e.g. "seconds"). Purely
    #: presentational; parsing is driven by ``kind``.
    unit: str = ""


#: The scalar / list fields the tab renders as one entry each. Custom terminal
#: templates and MCP endpoints get their own dedicated surfaces (below).
FIELD_SPECS: tuple[FieldSpec, ...] = (
    FieldSpec("runtime_root", "Runtime root", "path",
              "Runtime/session-data home (~/gnomon-data). Holds agents/ + sessions/. Root change applies on next app start."),
    FieldSpec("project_root", "Project root (reserved)", "path",
              "Informational / reserved — used when the devops repo is connected; "
              "editing it drives no behavior today (the git checkout home, ~/gnomon)."),
    FieldSpec("claude_bin", "claude binary", "path",
              "Path to the claude binary (default ~/.claude/local/claude); a bare name is looked up on PATH."),
    FieldSpec("default_terminal", "Default terminal", "text",
              "Launch-template key for new profiles (a built-in or a custom template name)."),
    FieldSpec("default_model", "Default model", "text",
              "Model seeded into a new profile (blank = launcher/claude default)."),
    FieldSpec("default_bootstrap_prompt", "Default bootstrap", "text",
              "Cold-start prompt seeded into a new profile."),
    FieldSpec("context_thresholds", "Context thresholds", "int_csv",
              "Ascending context %% the harness monitor fires at (default 90,95)."),
    FieldSpec("default_mcp_servers", "Default MCP servers", "csv",
              "MCP set wired into a new launch. Must NOT include a measurement-surface server (D9)."),
    FieldSpec("dsd_answers_corpus_path", "DSD-answers corpus", "path",
              "Explicit DSD-answer corpus path; blank = derived under the runtime root."),
    # --- PTY-hooks WS-H (2026-09-27). Harness-side keys apply on the NEXT launch.
    FieldSpec("user_name", "User name", "text",
              "Display name that signs your \\all / \\<instance> messages: <NAME-to-all>. "
              "Blank = your OS login."),
    # --- Stall inject (global default), structured sub-fields --------------------
    # These REPLACE the former single ``stall_inject`` json textarea. Each is a
    # dotted key the controller routes into the ``stall_inject`` dict; the whole
    # assembled dict is validated via ``merge_stall_inject`` on every set. Blank
    # everything back to the built-ins collapses the stored value to ``None``.
    # (A raw-JSON escape hatch on the bare ``stall_inject`` key is still honoured
    # by get_display/set for programmatic callers — it is simply not rendered.)
    FieldSpec("stall_inject.enabled", "Enabled", "bool",
              "Master enable for the dual-idle resume keep-alive (global default). A "
              "per-instance profile may still set its own stall_inject.enabled:false."),
    FieldSpec("stall_inject.agent_idle_seconds", "Agent idle", "int",
              "The agent must be idle at least this long before a resume is considered.",
              unit="seconds"),
    FieldSpec("stall_inject.operator_idle_seconds", "Operator idle", "int",
              "The operator must ALSO be idle at least this long (dual-idle) before a resume fires.",
              unit="seconds"),
    FieldSpec("stall_inject.escalate_after_seconds", "Escalate after", "int",
              "Still idle this long after the one resume -> ONE escalation record (no second "
              "inject). Blank = escalation disabled.",
              unit="seconds"),
    FieldSpec("stall_inject.post_compact_idle_seconds", "Post-compact idle", "int",
              "Shorter agent-idle threshold used ONCE right after a /compact. Blank = disabled "
              "(use the normal agent-idle threshold).",
              unit="seconds"),
    FieldSpec("stall_inject.rearm_grace_seconds", "Re-arm grace", "int",
              "Grace window after activity before the trigger re-arms for the next episode.",
              unit="seconds"),
    FieldSpec("stall_inject.backoff_max", "Backoff max", "int",
              "Cap on the 2x consecutive-fire backoff multiplier (no operator activity between fires)."),
    FieldSpec("stall_inject.content", "Content", "multiline",
              "The message injected on a resume. Must be non-empty."),
    FieldSpec("hook_manifest", "Hook manifest", "json",
              "Harness hook manifest {point: [handler, ...]}; points: inbound-injection, "
              "operator-command, operator-command-post, agent-start, agent-end, compact. Blank = none."),
    FieldSpec("operator_command_plane", "Operator \\ command plane", "bool",
              "Intercept lines starting with a backslash as operator commands (\\hooks, \\pause, \\all ...)."),
    # the Config-tab toggle for no-op hook reporting. A ``flags.<flag>`` key routes
    # into the machine config's ``flags`` dict (the existing flag mechanism); only a flag with a row here is editable.
    FieldSpec("flags.hook_noop_events", "No-op hook reporting", "bool",
              "Record an event when a hook point fires and nothing is attached to it (outcome noop), so unused hook "
              "points show up in the feature-usage report. OFF by default: it adds log lines. Turn it on for a day to "
              "look for automation opportunities, then off. Hook processes read it on every invocation; a running "
              "harness reads it at its next relaunch. Needs the feature-events flag (on by default)."),
    # g56: the Config-tab toggle for the DSD tab's open-by-id box.
    FieldSpec("flags.dsd_id_lookup", "DSD tab: open a DSD by id", "bool",
              "Show a box on the DSD tab where you paste or type a DSD id (for example) and the DSD "
              "opens: as a form if it is still pending, read-only if it is answered. OFF by default; the DSD tab is "
              "unchanged while it is off."),
    # confirm-proof fix round (audit F1): the two SECURITY flags. Turning one OFF here needs a passed presence check
    # (the server asks macOS); the dashboard shows a warning banner while either is OFF.
    FieldSpec("flags.confirm_proof", "Security: Apps confirm needs your Mac password", "bool",
              "ON (default): the Apps tab confirm, settings and action buttons make the server ask macOS for your login "
              "password / Touch ID after you click, before anything is committed. Turning this OFF asks for the same "
              "check now, and the dashboard shows a warning banner while it is off."),
    FieldSpec("flags.dsd_submit_proof", "Security: a DSD submit must come from your browser tab", "bool",
              "ON (default): a DSD answer is accepted only with recent real keyboard/pointer activity from this tab, "
              "and that evidence is recorded on the answer. Turning this OFF asks for your Mac password / Touch ID now, "
              "and the dashboard shows a warning banner while it is off."),
    # (g56): the Config-tab toggle for app activity in the idle messages.
    FieldSpec("flags.app_activity_presence", "App activity in idle messages", "bool",
              "When you are active in a registered app (the Apps tab), idle agents are told which app and how long ago "
              "(\"<your name> is active in <app> (12s ago). Do what you can without him.\"). Only the time of your last input "
              "is read from the app, never what you are doing. Information only: it changes no approval or snooze. "
              "OFF by default; the web job reads it at once, a running agent session at its next relaunch."),
    # (g56, g145): the Config-tab toggle for the waiting-on-operator alert. Needs the alerts flag (and modal_hold).
    FieldSpec("flags.waiting_on_operator_alert", "Alert when an agent waits on you", "bool",
              "When an agent's terminal shows a permission prompt that waits on you, raise one warning alert and a macOS "
              "notification naming the agent, the question and the first line of the command (secret-shaped text masked), "
              "with the time left when the prompt shows a countdown, and show a waiting badge on that agent's row. No alert "
              "for the agent whose console you typed in during the last 5 minutes. NEEDS the alerts flag: with alerts OFF "
              "nothing is delivered (the badge still shows). A running agent session reads this at its next launch."),
    # (security setting, g82): a TOP-LEVEL key, not a flag. Turning it OFF needs a passed presence check (ConfigStore.save).
    FieldSpec("guard_fail_closed", "Security: block commands when the deny guard cannot start", "bool",
              "ON (default): when the deny guard cannot start, every command is refused with a plain message that names the failure and "
              "the rescue. Turning this OFF asks for your Mac password / Touch ID, and then a guard that cannot start only writes a log "
              "line and lets the command through. A change reaches each session at its NEXT launch; a running session keeps the "
              "setting it was launched with."),
    FieldSpec("agent_launch_enabled", "Agent launch enabled", "bool",
              "Allow the hub launch_agent verb and profile-level agent launching. OFF by default "
              "(interim rails: depth 1, 2 concurrent children)."),
)

_TRUE = {"true", "yes", "on", "1"}
_FALSE = {"false", "no", "off", "0"}

#: The stall sub-fields whose schema permits a blank (``None`` = that mechanism
#: disabled). Every other stall numeric sub-field requires a value.
_STALL_NULLABLE = ("escalate_after_seconds", "post_compact_idle_seconds")


class ConfigController:
    """Headless editing surface over the machine :class:`GnomonConfig`."""

    def __init__(
        self,
        store: Optional[ConfigStore] = None,
        *,
        config: Optional[GnomonConfig] = None,
        on_saved: Optional[Callable[[GnomonConfig], None]] = None,
    ) -> None:
        self._store = store if store is not None else ConfigStore()
        # An injected config is used verbatim (tests); otherwise load from the
        # store (absent/partial -> defaults). A fresh machine still edits fine.
        self._config = config if config is not None else self._store.load()
        self._on_saved = on_saved

    # -- introspection -------------------------------------------------------

    @property
    def config(self) -> GnomonConfig:
        return self._config

    @property
    def store_path(self) -> str:
        return self._store.path

    def config_exists(self) -> bool:
        return self._store.exists()

    def as_dict(self) -> Dict[str, object]:
        return self._config.to_dict()

    def field_specs(self) -> tuple[FieldSpec, ...]:
        return FIELD_SPECS

    def field_groups(self) -> List[Dict[str, object]]:
        """Ordered master groups for the tab's master-detail view.

        Each group is ``{key, title, field_keys:[...]}`` and covers a slice of
        :data:`FIELD_SPECS`; the union of every ``field_keys`` equals the full set
        of field-spec keys (no field is orphaned). Two groups also carry a
        ``surface`` marker naming a dedicated editor the tab renders in that
        group's detail pane in addition to its plain fields:

        * ``"terminal_templates"`` — the custom terminal-templates add/remove list;
        * ``"mcp_endpoints"`` — the read-only MCP endpoints list.

        This is additive: :meth:`field_specs` is unchanged and existing generic
        callers keep working.
        """
        stall_keys = [s.key for s in FIELD_SPECS if s.key.startswith("stall_inject.")]
        return [
            {"key": "paths", "title": "Paths",
             "field_keys": ["runtime_root", "project_root", "claude_bin",
                            "dsd_answers_corpus_path"]},
            {"key": "launch", "title": "Launch defaults",
             "field_keys": ["default_model", "default_bootstrap_prompt",
                            "default_mcp_servers", "user_name"]},
            {"key": "terminal", "title": "Default terminal",
             "field_keys": ["default_terminal"], "surface": "terminal_templates"},
            {"key": "context", "title": "Context thresholds",
             "field_keys": ["context_thresholds"]},
            {"key": "stall", "title": "Stall inject",
             "field_keys": stall_keys},
            {"key": "advanced", "title": "Harness / advanced",
             "field_keys": ["hook_manifest", "operator_command_plane",
                            "flags.hook_noop_events", "flags.dsd_id_lookup", "agent_launch_enabled",
                            "flags.confirm_proof", "flags.dsd_submit_proof",
                            "flags.app_activity_presence", "flags.waiting_on_operator_alert", "guard_fail_closed"]},
            {"key": "mcp", "title": "MCP endpoints",
             "field_keys": [], "surface": "mcp_endpoints"},
        ]

    # -- stall_inject helpers (structured sub-fields) ------------------------

    @staticmethod
    def _parse_int(label: str, raw: str, *, nullable: bool) -> Optional[int]:
        text = raw.strip()
        if not text:
            if nullable:
                return None
            raise ValueError(f"{label} must be an integer")
        try:
            return int(text)
        except ValueError:
            raise ValueError(f"{label} must be an integer") from None

    @staticmethod
    def _parse_bool(label: str, raw: str) -> bool:
        tok = raw.strip().lower()
        if tok in _TRUE:
            return True
        if tok in _FALSE:
            return False
        raise ValueError(f"{label} must be true or false")

    def _stall_effective(self) -> Dict[str, object]:
        """The current effective stall dict (built-ins <- stored global), validated."""
        from ..harness.constants import merge_stall_inject

        return merge_stall_inject(self._config.stall_inject)

    def _set_stall_json(self, raw: str) -> None:
        """Raw-JSON escape hatch on the bare ``stall_inject`` key (programmatic).

        Not rendered by the tab (the structured sub-fields are), but kept so
        callers that post a whole JSON object still validate + persist. Blank ->
        ``None`` (the built-in defaults)."""
        from ..harness.constants import merge_stall_inject

        text = raw.strip()
        if not text:
            self._config.stall_inject = None
            return
        try:
            value = json.loads(text)
        except ValueError as exc:
            raise ValueError(f"Stall inject (global) is not valid JSON: {exc}") from None
        if not isinstance(value, dict):
            raise ValueError("Stall inject (global) must be a JSON object")
        merge_stall_inject(value)  # validates; raises ValueError
        self._config.stall_inject = value

    def _set_stall_subfield(self, key: str, raw: str) -> None:
        """Route ``stall_inject.<sub>`` into the stall dict, validated as a whole,
        but PERSIST ONLY A SPARSE DELTA.

        Materializes the current effective dict, applies the one sub-field per its
        kind, and validates the ASSEMBLED dict via ``merge_stall_inject`` (surfacing
        its ValueError) exactly as before. It then stores ONLY the keys whose value
        differs from :data:`DEFAULT_STALL_INJECT` — never the full 8-key dict — so an
        unedited key keeps inheriting the built-in default and a later default bump
        (e.g. 180->300) still reaches this user (``merge_stall_inject`` fills the
        rest, sparse-over-defaults). Collapses to ``None`` when nothing differs
        (blank = built-ins). Fold-in of finding 5: a nullable numeric explicitly set
        blank -> ``None`` (disabled) differs from its default and is stored as an
        explicit ``None`` override, kept distinct from ``0``."""
        from ..harness.constants import merge_stall_inject, DEFAULT_STALL_INJECT

        sub = key.split(".", 1)[1]
        spec = next((s for s in FIELD_SPECS if s.key == key), None)
        if spec is None:
            raise ValueError(f"unknown config field: {key!r}")
        effective = self._stall_effective()
        if spec.kind == "bool":
            effective[sub] = self._parse_bool(spec.label, raw)
        elif spec.kind == "int":
            effective[sub] = self._parse_int(spec.label, raw, nullable=(sub in _STALL_NULLABLE))
        elif spec.kind == "multiline":
            effective[sub] = raw
        else:  # pragma: no cover - guarded by FIELD_SPECS
            raise ValueError(f"unhandled stall sub-field kind: {spec.kind!r}")
        merge_stall_inject(effective)  # validates the assembled dict; raises ValueError
        # Store only the differences from the built-in default. Note the compare is
        # against the CURRENT default: a nullable key blanked to None (!= its default)
        # is a real override and is preserved; an unedited key never enters the delta,
        # so it re-resolves to whatever the default is at read time.
        delta = {k: v for k, v in effective.items() if v != DEFAULT_STALL_INJECT.get(k)}
        self._config.stall_inject = delta or None

    def _set_flag_row(self, key: str, raw: str) -> None:
        """``flags.<flag>``: set one feature flag in the machine config's ``flags`` (the existing flag mechanism).
        Other flags are kept as they are. A value equal to the registry default is NOT written unless the flag is already
        in the config: a page that posts the whole state must not pin a default it never changed (a later default change
        would be silently overridden)."""
        from .. import flags as _flags

        name = key.split(".", 1)[1]
        if name not in _flags.KNOWN_FLAGS:
            raise ValueError(f"unknown feature flag: {name!r}")
        label = next((s.label for s in FIELD_SPECS if s.key == key), key)
        value = self._parse_bool(label, raw)
        current = dict(self._config.flags) if isinstance(self._config.flags, dict) else {}
        if name in current or value != _flags.KNOWN_FLAGS[name].default:
            current[name] = value
        self._config.flags = current

    def get_display(self, key: str) -> str:
        """The string a scalar/list field shows in its entry widget."""
        # Stall sub-fields: show the EFFECTIVE value (built-ins when unset), so a
        # fresh machine renders the defaults the user is editing against.
        if key.startswith("stall_inject."):
            sub = key.split(".", 1)[1]
            spec = next((s for s in FIELD_SPECS if s.key == key), None)
            val = self._stall_effective().get(sub)
            if spec is not None and spec.kind == "bool":
                return "true" if val else "false"
            return "" if val is None else str(val)
        # Flag rows show the EFFECTIVE value (machine config, else the registry default).
        if key.startswith("flags."):
            from .. import flags as _flags

            return "true" if _flags.flag(key.split(".", 1)[1], config=self._config) else "false"
        value = getattr(self._config, key)
        spec = next((s for s in FIELD_SPECS if s.key == key), None)
        # Bare stall_inject escape hatch: raw JSON (or "" for the built-in default).
        if key == "stall_inject":
            return "" if value in (None, {}) else json.dumps(value)
        if spec is not None and spec.kind == "json":
            return "" if value in (None, {}) else json.dumps(value)
        if spec is not None and spec.kind == "bool":
            return "true" if value else "false"
        if isinstance(value, (list, tuple)):
            return ", ".join(str(v) for v in value)
        return "" if value is None else str(value)

    # -- editing (validated) -------------------------------------------------

    def set(self, key: str, raw: str) -> None:
        """Parse ``raw`` (as the field's ``kind`` dictates) and set it, validated.

        Raises ``ValueError`` with a human message on a bad value, so the tab can
        surface it rather than persisting garbage.
        """
        # Bare stall_inject: the raw-JSON escape hatch (not rendered by the tab).
        if key == "stall_inject":
            self._set_stall_json(raw)
            return
        # Structured stall sub-fields route into the stall dict, validated whole.
        if key.startswith("stall_inject."):
            self._set_stall_subfield(key, raw)
            return
        spec = next((s for s in FIELD_SPECS if s.key == key), None)
        if spec is None:
            raise ValueError(f"unknown config field: {key!r}")
        if key.startswith("flags."):
            self._set_flag_row(key, raw)
            return
        if spec.kind in ("path", "text"):
            setattr(self._config, key, raw.strip())
        elif spec.kind == "multiline":
            setattr(self._config, key, raw)
        elif spec.kind == "int":
            setattr(self._config, key, self._parse_int(spec.label, raw, nullable=False))
        elif spec.kind == "csv":
            items = [tok.strip() for tok in raw.split(",") if tok.strip()]
            if key == "default_mcp_servers":
                if not items:
                    raise ValueError("default MCP servers must not be empty")
                refused = [i for i in items if i in REFUSED_DEFAULT_MCP_SERVERS]
                if refused:
                    raise ValueError(
                        "%s must not be in the default MCP set (D9): a launcher "
                        "agent holds no such server" % ", ".join(refused)
                    )
            setattr(self._config, key, items)
        elif spec.kind == "int_csv":
            try:
                ints = [int(tok.strip()) for tok in raw.split(",") if tok.strip()]
            except ValueError:
                raise ValueError(f"{spec.label} must be a comma-separated list of integers") from None
            if not ints:
                raise ValueError(f"{spec.label} must not be empty")
            setattr(self._config, key, ints)
        elif spec.kind == "bool":
            tok = raw.strip().lower()
            if tok in _TRUE:
                setattr(self._config, key, True)
            elif tok in _FALSE:
                setattr(self._config, key, False)
            else:
                raise ValueError(f"{spec.label} must be true or false")
        elif spec.kind == "json":
            text = raw.strip()
            if not text:
                value = None if key == "stall_inject" else {}
            else:
                try:
                    value = json.loads(text)
                except ValueError as exc:
                    raise ValueError(f"{spec.label} is not valid JSON: {exc}") from None
                if not isinstance(value, dict):
                    raise ValueError(f"{spec.label} must be a JSON object")
                if key == "stall_inject":
                    from ..harness.constants import merge_stall_inject

                    merge_stall_inject(value)  # validates; raises ValueError
                elif key == "hook_manifest":
                    from ..harness.hooks import validate_manifest

                    validate_manifest(value)  # validates; raises ValueError
            setattr(self._config, key, value)
        else:  # pragma: no cover - guarded by FIELD_SPECS
            raise ValueError(f"unhandled field kind: {spec.kind!r}")

    # -- custom terminal templates (D4) --------------------------------------

    def custom_templates(self) -> Dict[str, str]:
        return dict(self._config.custom_terminal_templates)

    def available_templates(self) -> List[str]:
        """Built-in + custom template keys a profile may name."""
        return available_terminal_templates(self._config.custom_terminal_templates)

    def add_terminal_template(self, name: str, template: str) -> None:
        """Add/replace a named custom terminal template (``{name: cmd-string}``)."""
        name = name.strip()
        if not name:
            raise ValueError("custom template name must not be empty")
        if not template.strip():
            raise ValueError("custom template command string must not be empty")
        self._config.custom_terminal_templates[name] = template

    def remove_terminal_template(self, name: str) -> None:
        self._config.custom_terminal_templates.pop(name, None)

    # -- MCP endpoints (advanced) --------------------------------------------

    def mcp_endpoints(self) -> Dict[str, dict]:
        return {k: dict(v) for k, v in self._config.mcp_endpoints.items()}

    def set_mcp_endpoint(self, name: str, endpoint: dict) -> None:
        name = name.strip()
        if not name:
            raise ValueError("MCP server name must not be empty")
        if not isinstance(endpoint, dict):
            raise ValueError("MCP endpoint must be a JSON object")
        self._config.mcp_endpoints[name] = dict(endpoint)

    # -- guards --------------------------------------------------------------

    def excludes_refused_servers(self) -> bool:
        """D9 invariant on the default MCP set."""
        return self._config.excludes_refused_servers()

    # -- persistence ---------------------------------------------------------

    def save(self, presence_grant: object = None) -> str:
        """Persist the edited config; notify the app Controller when wired. ``presence_grant`` (a
        ``human_confirm.PresenceGrant``) is needed only to turn a security flag OFF (``ConfigStore.save``)."""
        if not self._config.excludes_refused_servers():
            raise ValueError(
                "refusing to save: a refused server is in the default MCP set (D9)"
            )
        path = self._store.save(self._config, presence_grant=presence_grant)
        if self._on_saved is not None:
            self._on_saved(self._config)
        return path

    def reload(self) -> GnomonConfig:
        """Discard in-memory edits and re-read from the store."""
        self._config = self._store.load()
        return self._config

"""The LAUNCH-TIME half of the scope core: prepare (validate, no writes) and write (the relaunch bundle + the session's
``scope-state.json``). Called by ``LaunchManager.launch`` ONLY when ``workspace_scope_core`` is ON; with it OFF
this module is never even imported by the launch path (flag-off identity, tested).

NOT a safety path: launch is ordinary work and may read workspace config (the home workspace's ``workspace.json`` and
``projects.json``). What it WRITES is what makes the later RELAUNCH a safety path that needs no workspace read.

Two steps so that a refusal leaves nothing behind:

1. :func:`prepare` runs BEFORE the launch creates any file. It fails LOUD (a :class:`~.scopebase.ScopeError`) when the
   live ``machine.json`` is unreadable, the role has no home workspace (no ``workspaces/<ws>/roles/<id>.md``), or its
   home workspace is ARCHIVED (ENT-ROLE-DATA: a role whose home is archived must be re-homed before its next launch).
   An unreadable home ``workspace.json`` does NOT fail the launch: its settings layer is SKIPPED loudly (I6).
2. :meth:`Prepared.write` runs after the session dir exists: the bundle, the scope state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, Mapping, Optional

from . import events as ev
from . import settings_registry as SR
from .bundle import Bundle, write_bundle
from .machine import load_machine
from .model import home_workspace_of, load_model, read_workspace_record
from .scope import ScopeSession, initial_current_project, save_session_scope
from .scopebase import ModelError, ScopeCoreDisabled, ScopeRefusal, read_json, require_enabled
from .settings_resolve import Layer, SettingsResolver, layer_from_canonical, layer_from_raw, unavailable


def find_previous_scope(sessions_root: str, instance_id: str, conversation_key: Optional[str], *, exclude_dir: str = "") -> Optional[Mapping[str, Any]]:
    """The scope state of the PREVIOUS session-run of the same conversation (same Claude session id), newest first, or
    None. Reads session dirs only (never ``workspaces/**``)."""
    if not conversation_key or not os.path.isdir(sessions_root):
        return None
    for name in sorted((n for n in os.listdir(sessions_root) if n.startswith(instance_id + "-")), reverse=True):
        if name == os.path.basename(exclude_dir.rstrip("/")):
            continue
        cdir = os.path.join(sessions_root, name, ".claude")
        try:
            lj = read_json(os.path.join(cdir, "launch.json"))
            state = read_json(os.path.join(cdir, "scope-state.json"))
        except (OSError, ValueError):
            continue
        if conversation_key in (lj.get("session_id"), lj.get("resume_session_id")):
            return state
    return None


#: the unforgeable-by-accident capability of a :class:`Prepared`: an identity-compared object private to this module, held ONLY by a
#: ``Prepared`` that :func:`prepare` minted after its own gate. It is not a bool (nothing to pass as ``enabled=True``), it is not an
#: ``__init__`` parameter, and ``dataclasses.replace`` does not copy it (X13 runtime gate, audit-x13-f2-detector F-5). Python cannot hide
#: a module attribute, so this stops a hand-built or ``replace``d ``Prepared``, not code that deliberately reaches into this module.
_GRANT = object()


@dataclass(frozen=True)
class Prepared:
    instance_id: str
    deploy_root: str
    home: str
    home_state: str
    layers: Mapping[str, Layer]
    resolver: SettingsResolver
    scope: ScopeSession
    event_log_path: str
    clock: Optional[Callable[[], datetime]]
    #: set ONLY by :func:`prepare` (``object.__setattr__``, after its own ``require_enabled`` passed); ``init=False``, so
    #: ``Prepared(..., enabled=True)`` is a TypeError and ``dataclasses.replace`` yields a copy WITHOUT it, which refuses to write (F-5)
    _grant: object = field(default=None, init=False, repr=False, compare=False)
    #: the flag arguments :func:`prepare` resolved with, kept so :meth:`write` re-reads the REAL flag the same way at write time
    _flag: Optional[bool] = field(default=None, init=False, repr=False, compare=False)
    _config: Any = field(default=None, init=False, repr=False, compare=False)

    def write(self, session_dir: str, claude_md_path: str) -> Dict[str, Any]:
        """Write the scope state and the relaunch bundle into ``session_dir`` (after the launch created it). Gates ITSELF, first, with the
        SAME ``enabled`` / ``config`` :func:`prepare` resolved with, kept on the object (X13 g54b e, audit-x13-runtime-gate P2-3). Precisely:

        * minted with ``enabled=None`` (the flag resolved through ``flags.flag`` / an injected ``config``): the REAL flag is read AGAIN
          here, so flag OFF at write time refuses even if it was ON at prepare time;
        * minted with ``enabled=True`` (what ``LaunchManager`` passes, because a profile override can be the only thing turning the flag
          ON): a SNAPSHOT of the value prepare resolved. It cannot be a live re-read: the registry flag is OFF in exactly that case, and a
          real re-read would break every flag-ON launch (7-mode differential, audit mutant M3). The window is one ``LaunchManager.launch``
          call; what it never does is write for a ``Prepared`` that :func:`prepare` did not mint (hand-built, ``dataclasses.replace``d,
          ``Prepared(..., enabled=True)`` is a TypeError): those carry no grant and refuse.

        The primitives it writes through (``save_session_scope``, ``write_bundle``, ``atomic_write_*``) receive the same snapshot, never a
        literal ``True``. A refusal raises ``ScopeCoreDisabled`` and writes nothing."""
        require_enabled(self._flag if self._grant is _GRANT else False, self._config, "Prepared.write")
        flag = self._flag                                    # the gate above passed, so this Prepared carries the grant
        with open(claude_md_path, "r", encoding="utf-8") as fh:
            role_def = fh.read()
        save_session_scope(session_dir, self.scope, enabled=flag, config=self._config)
        resolved = {n: {"value": r.value, "source": r.source} for n, r in self.resolver.effective().items()}
        b: Bundle = write_bundle(session_dir, instance_id=self.instance_id, deploy_root=self.deploy_root, layers=self.layers, resolved=resolved,
                                 role_def_text=role_def, home_workspace={"slug": self.home, "state": self.home_state}, clock=self.clock, enabled=flag, config=self._config)
        return {"bundle": b.path, "home-workspace": self.home, "current-project": self.scope.current_project}


def prepare(*, instance_id: str, deploy_root: str, profile_raw: Mapping[str, Any], sessions_root: str, session_dir: str,
            conversation_key: Optional[str] = None, reg: Optional[dict] = None,
            clock: Optional[Callable[[], datetime]] = None, enabled: Optional[bool] = None, config: Any = None) -> Prepared:
    """Validate the launch (no file under the session dir). Its one write is an ALERT (a skipped layer, an unreadable model), so
    it gates ITSELF (g46 d, architect ruling 2026-10-01): with ``workspace_scope_core`` OFF it raises
    :class:`~.scopebase.ScopeCoreDisabled` before reading or writing anything, whoever calls it. ``enabled`` is the caller's
    already-resolved flag (``LaunchManager`` passes it: the flag may be ON only through a profile override); ``None`` resolves the
    registry default through ``flags.flag`` (``config`` injectable)."""
    require_enabled(enabled, config, "launchhook.prepare")
    reg = reg if reg is not None else SR.load()
    deploy_root = os.path.abspath(deploy_root)
    machine = load_machine(deploy_root)                     # loud stop when unreadable
    machine_layer = layer_from_canonical(reg, "machine", machine.settings)
    home = home_workspace_of(deploy_root, instance_id)
    if home is None:
        raise ScopeRefusal("home-workspace-unresolved", f"role {instance_id!r} has no definition under any workspace's roles/ directory: "
                           "its home workspace cannot be resolved, so no relaunch bundle can be written",
                           "give the role a definition in its home workspace before launching with the scope core on")
    rec, reason = read_workspace_record(deploy_root, home)
    if rec is None:
        ws_layer = unavailable("workspace", reason)
        home_state = "unknown"
    else:
        home_state = str(rec.get("state"))
        if home_state == "archived":
            raise ScopeRefusal("home-workspace-archived", f"role {instance_id!r}'s home workspace {home!r} is archived", "re-home the role before its next launch")
        ws_layer = layer_from_canonical(reg, "workspace", rec.get("settings") or {})
    lc_raw = {k: v for k, v in profile_raw.items() if v is not None and k != "instance_id"}
    lc_layer = layer_from_raw(reg, "launch-config", lc_raw)
    sess_layer = Layer("session", {})
    # the event log path is a MACHINE setting (I6): resolved from the machine layer only
    log_path = ev.event_log_path_from(reg, machine_layer)

    def alert(e: Dict[str, Any]) -> None:
        # passes the SAME resolved enabled/config as prepare (g54b e, audit-x13-runtime-gate P2-1), never a literal True: with enabled=None
        # (config-minted) this re-reads the real flag at each alert, so an alert after the flag went OFF is refused. A refusal drops the
        # alert (a settings read must not crash because the flag flipped; nothing is written, which is the point).
        e = dict(e)
        try:
            ev.append_event(log_path, e.pop("kind"), clock=clock, enabled=enabled, config=config, **e)
        except ScopeCoreDisabled:
            pass

    resolver = SettingsResolver(reg, machine=machine_layer, workspace=ws_layer, launch_config=lc_layer, session=sess_layer, alert=alert)
    # current-project-initial: the conversation's previous run's last value, else default-project, else none
    current: Optional[str] = None
    try:
        model = load_model(deploy_root)
        prev = find_previous_scope(sessions_root, instance_id, conversation_key, exclude_dir=session_dir)
        current = initial_current_project(model, previous_value=(prev or {}).get("current-project"),
                                          default_project=resolver.value("default_project") or None, allowed_workspaces=(home,))
    except ModelError as exc:
        alert({"kind": "scope-model-unreadable", "severity": "warn", "message": f"launch of {instance_id!r}: the workspace model is unreadable ({exc.message}); current-project starts empty"})
    scope = ScopeSession(launch_workspace=home, current_project=current, role_workspaces=(home,))
    prepared = Prepared(instance_id, deploy_root, home, home_state, {"workspace": ws_layer, "launch-config": lc_layer, "session": sess_layer},
                        resolver, scope, log_path, clock)
    object.__setattr__(prepared, "_grant", _GRANT)           # mint: only here, after this function's own require_enabled passed
    object.__setattr__(prepared, "_flag", enabled)
    object.__setattr__(prepared, "_config", config)
    return prepared

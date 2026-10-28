"""App lifecycle — launch / terminate / list whitelisted desktop tools.

 — the THIRD tool cluster on the SAME localhost MCP
server (``rides-existing-hub``, D10: one server owns DSD + actuation + now
app-lifecycle). App-agnostic: any whitelisted app, not cluster_viewer-specific.

Surface (wired into :class:`~gnomon.hub.mcp.GmMCPService`):

* ``launch_app(app_id, args?)`` ->
  ``{handle, pid, status: running|exited, exit_code?, log_path, started_at}``
* ``terminate_app(handle_or_pid)`` -> ``{was_running, method: SIGTERM|SIGKILL, pid}``
* ``list_running_apps()`` ->
  ``[{pid, app_id, argv, cwd, started_at, source: hub_launched|discovered, log_path?}]``

The REGISTRY is the safety boundary — a WHITELIST ``app_id -> {entrypoint,
venv, args_schema?, single_instance_policy}``. Unknown ``app_id`` is refused;
there is NO shell anywhere in this module (argv lists only, never
``shell=True``). It is plain, hand-editable JSON (like the agent-profile
registry: adding a tool needs no code change), re-read on EVERY call, at
``<runtime_root>/apps.json`` (default ``~/gnomon-data/apps.json``)::

    {"apps": {
      "cluster_viewer": {
        "entrypoint": "~/tools/example_viewer/example_viewer.py",
        "venv": "~/tools/example_viewer/.venv",
        "single_instance_policy": "replace",
        "cwd": "~/tools/example_viewer",      # optional
        "settle_s": 3.0,                                # optional prove-up settle
        "terminate_grace_s": 15.0,                      # optional SIGTERM grace
        "kill_wait_s": 5.0,                             # optional post-SIGKILL wait
        "args_schema": null                             # carried; v2 enforces
      }}}

Argv construction: with ``venv`` -> ``[<venv>/bin/python, <entrypoint>, *args]``;
without -> ``[<entrypoint>, *args]`` (entrypoint must then be executable). A
missing entrypoint / venv interpreter is a LOUD refusal (empty-socket rule),
never a guessed alternate.

The two load-bearing invariants:

* ``invariant-terminate-reaped-before-return`` — see
  :meth:`AppLifecycle.terminate_app`. Returns only once the pid is CONFIRMED
  reaped (so any SIGTERM exit-handler / save-on-exit flush has completed);
  death-unconfirmed (SIGKILL failed / zombie) is FAILURE, never success.
* ``invariant-prove-up-past-init`` — see :meth:`AppLifecycle.launch_app`.
  ``status=running`` only after the pid is re-confirmed alive past a settle
  window; an app that dies in init returns ``exited`` + ``exit_code`` +
  ``log_path``.

``lsof-secondary-only``: no lsof query gates anything here. argv+cwd bound to
the entrypoint is the load-bearing discovery key.

``boundary``: the app-side seatbelt (a viewer refusing to clobber a file
changed underneath it) is the app's own defence-in-depth, not the hub's.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import procinfo

POLICIES = ("refuse", "adopt", "replace", "allow")

DEFAULT_REGISTRY_FILENAME = "apps.json"
DEFAULT_SETTLE_S = 3.0
DEFAULT_TERMINATE_GRACE_S = 15.0
DEFAULT_KILL_WAIT_S = 5.0
_POLL_S = 0.05

_APP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_HANDLE_RE = re.compile(r"^app-[0-9a-f]{12}$")


class AppLifecycleError(Exception):
    """A lifecycle call that failed with a message safe to return to the agent.

    ``detail`` carries structured context (e.g. the candidate pids for a
    refused ambiguous selector) that the service surfaces verbatim.
    """

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail


def _runtime_root(runtime_root: Optional[str]) -> str:
    """An explicit root, else the DATA root default (X13 S5-4: ``config.runtime_root_default``)."""
    from ..config import runtime_root_default

    return os.path.expanduser(runtime_root) if runtime_root else runtime_root_default()


def default_registry_path(runtime_root: Optional[str] = None) -> str:
    return os.path.join(_runtime_root(runtime_root), DEFAULT_REGISTRY_FILENAME)


def default_state_root(runtime_root: Optional[str] = None) -> str:
    return os.path.join(_runtime_root(runtime_root), "apps")


def _iso(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


# --------------------------------------------------------------------------
# registry (the whitelist)
# --------------------------------------------------------------------------

@dataclass
class AppSpec:
    app_id: str
    entrypoint: str
    single_instance_policy: str
    venv: Optional[str] = None
    cwd: Optional[str] = None
    args_schema: Optional[Any] = None
    settle_s: float = DEFAULT_SETTLE_S
    terminate_grace_s: float = DEFAULT_TERMINATE_GRACE_S
    kill_wait_s: float = DEFAULT_KILL_WAIT_S

    @property
    def entrypoint_real(self) -> str:
        return _real(self.entrypoint)

    def interpreter(self) -> Optional[str]:
        if not self.venv:
            return None
        return os.path.join(os.path.expanduser(self.venv), "bin", "python")

    def resolved_cwd(self) -> str:
        if self.cwd:
            return os.path.abspath(os.path.expanduser(self.cwd))
        return os.path.dirname(os.path.abspath(os.path.expanduser(self.entrypoint)))

    def build_argv(self, args: List[str]) -> List[str]:
        """The RESOLVED argv — verified at the object (fail loud, never guess)."""
        entry = os.path.abspath(os.path.expanduser(self.entrypoint))
        if not os.path.isfile(entry):
            raise AppLifecycleError(
                f"app {self.app_id!r}: entrypoint does not exist: {entry}", app_id=self.app_id
            )
        interp = self.interpreter()
        if interp is not None:
            if not (os.path.isfile(interp) and os.access(interp, os.X_OK)):
                raise AppLifecycleError(
                    f"app {self.app_id!r}: venv interpreter missing or not executable: {interp}",
                    app_id=self.app_id,
                )
            return [interp, entry, *args]
        if not os.access(entry, os.X_OK):
            raise AppLifecycleError(
                f"app {self.app_id!r}: no venv configured and entrypoint is not executable: {entry}",
                app_id=self.app_id,
            )
        return [entry, *args]


def _parse_spec(app_id: str, raw: Any) -> AppSpec:
    if not _APP_ID_RE.match(app_id):
        raise ValueError(f"invalid app_id {app_id!r}")
    if not isinstance(raw, dict):
        raise ValueError("entry must be a JSON object")
    entry = raw.get("entrypoint")
    if not isinstance(entry, str) or not entry:
        raise ValueError("'entrypoint' is required (non-empty string)")
    policy = raw.get("single_instance_policy")
    if policy not in POLICIES:
        raise ValueError(f"'single_instance_policy' is required, one of {list(POLICIES)} (got {policy!r})")
    venv = raw.get("venv")
    if venv is not None and (not isinstance(venv, str) or not venv):
        raise ValueError("'venv' must be a non-empty string or null")
    cwd = raw.get("cwd")
    if cwd is not None and (not isinstance(cwd, str) or not cwd):
        raise ValueError("'cwd' must be a non-empty string or null")

    def _num(key: str, default: float) -> float:
        v = raw.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"'{key}' must be a non-negative number")
        return float(v)

    return AppSpec(
        app_id=app_id,
        entrypoint=entry,
        single_instance_policy=policy,
        venv=venv,
        cwd=cwd,
        args_schema=raw.get("args_schema"),
        settle_s=_num("settle_s", DEFAULT_SETTLE_S),
        terminate_grace_s=_num("terminate_grace_s", DEFAULT_TERMINATE_GRACE_S),
        kill_wait_s=_num("kill_wait_s", DEFAULT_KILL_WAIT_S),
    )


class AppRegistry:
    """The whitelist, re-read from disk on every call (hand-editable JSON).

    A missing file is an EMPTY whitelist and every launch is refused with a
    message naming the path — never a silent fallback. A malformed entry makes
    THAT app unusable (reported in ``errors``); well-formed siblings still work.
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = os.path.abspath(os.path.expanduser(path or default_registry_path()))

    @property
    def path(self) -> str:
        return self._path

    def load(self) -> Tuple[Dict[str, AppSpec], Dict[str, str]]:
        """``(specs, errors)`` — errors map app_id (or ``"<file>"``) -> reason."""
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}, {"<file>": f"no app registry at {self._path}"}
        except (OSError, ValueError) as exc:
            return {}, {"<file>": f"app registry unreadable at {self._path}: {exc}"}
        apps = data.get("apps") if isinstance(data, dict) else None
        if not isinstance(apps, dict):
            return {}, {"<file>": f"app registry at {self._path} has no 'apps' object"}
        specs: Dict[str, AppSpec] = {}
        errors: Dict[str, str] = {}
        for app_id, raw in apps.items():
            if isinstance(raw, dict) and "manifest" in raw and "entrypoint" not in raw:
                # (spec 13): a manifest-registered app belongs to the Apps host
                # (gnomon.appshost), never to this legacy launcher. Skipped, not an error.
                continue
            try:
                specs[app_id] = _parse_spec(app_id, raw)
            except ValueError as exc:
                errors[app_id] = str(exc)
        return specs, errors

    def get(self, app_id: str) -> AppSpec:
        specs, errors = self.load()
        if app_id in specs:
            return specs[app_id]
        if app_id in errors:
            raise AppLifecycleError(
                f"app {app_id!r} is registered but malformed in {self._path}: {errors[app_id]}",
                app_id=app_id,
            )
        why = errors.get("<file>")
        raise AppLifecycleError(
            f"unknown app_id {app_id!r} — not in the whitelist"
            + (f" ({why})" if why else f" at {self._path}; registered: {sorted(specs)}"),
            app_id=app_id,
        )


# --------------------------------------------------------------------------
# records + signature discovery
# --------------------------------------------------------------------------

@dataclass
class _Record:
    """A hub spawn-record (or an adopted stranger that was minted a handle)."""

    handle: str
    app_id: str
    pid: int
    argv: List[str]
    cwd: str
    started_at: float
    source: str  # hub_launched | discovered
    log_path: Optional[str] = None
    popen: Optional[subprocess.Popen] = field(default=None, repr=False)
    entrypoint_real: Optional[str] = None


@dataclass
class RunningApp:
    pid: int
    app_id: str
    argv: List[str]
    cwd: Optional[str]
    started_at: float
    source: str
    log_path: Optional[str] = None
    handle: Optional[str] = None
    popen: Optional[subprocess.Popen] = field(default=None, repr=False)
    #: realpath of the whitelisted entrypoint this instance runs. Single-instance
    #: policy and app_id selectors compare on THIS, so two app_ids registered
    #: over one entrypoint (e.g. two modes of one viewer) see each other — the
    #: same binary is the same clobber risk.
    entrypoint_real: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "pid": self.pid,
            "app_id": self.app_id,
            "argv": list(self.argv),
            "cwd": self.cwd,
            "started_at": _iso(self.started_at),
            "started_at_epoch": self.started_at,
            "source": self.source,
        }
        if self.log_path is not None:
            out["log_path"] = self.log_path
        if self.handle is not None:
            out["handle"] = self.handle
        return out


def _looks_like_python(arg0: str) -> bool:
    base = os.path.basename(arg0).lower()
    return base == "python" or base.startswith("python")


def match_signature(argv: List[str], cwd: Optional[str], specs: Dict[str, AppSpec]) -> Optional[str]:
    """Which whitelisted app (if any) is this process running?

    Tight on purpose — a match authorises terminate/replace, so an editor or
    ``cat`` holding the path as an argument must NOT match:

    * ``argv[0]`` resolves to the entrypoint (an executable entrypoint), or
    * ``argv[0]`` is a python interpreter and the FIRST non-option argument
      resolves to the entrypoint (relative paths resolved against the
      process's own cwd — the argv+cwd key).

    ``-m module`` / ``-c code`` invocations never match.
    """
    if not argv:
        return None
    by_real: Dict[str, str] = {}
    for s in sorted(specs.values(), key=lambda s: s.app_id, reverse=True):
        by_real[s.entrypoint_real] = s.app_id  # shared entrypoint -> sorted-first app_id

    def _resolve(p: str) -> Optional[str]:
        if not p:
            return None
        if not os.path.isabs(p):
            if not cwd:
                return None
            p = os.path.join(cwd, p)
        return os.path.realpath(p)

    r0 = _resolve(argv[0])
    if r0 in by_real:
        return by_real[r0]
    if not _looks_like_python(argv[0]):
        return None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-m", "-c"):
            return None
        if a in ("-W", "-X", "--check-hash-based-pycs"):
            i += 2
            continue
        if a.startswith("-"):
            i += 1
            continue
        r = _resolve(a)
        return by_real.get(r) if r else None
    return None


# --------------------------------------------------------------------------
# the lifecycle controller
# --------------------------------------------------------------------------

class AppLifecycle:
    """launch / terminate / list over the whitelist, hub records + discovery."""

    def __init__(
        self,
        registry: Optional[AppRegistry] = None,
        state_root: Optional[str] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.registry = registry if registry is not None else AppRegistry()
        self._state_root = os.path.abspath(os.path.expanduser(state_root or default_state_root()))
        self._clock = clock
        self._records: Dict[str, _Record] = {}
        self._rec_lock = threading.Lock()
        # Serialises launch/terminate so two concurrent `replace` launches can
        # not both see "none running" and double-start (the clobber case).
        self._op_lock = threading.RLock()

    @property
    def log_dir(self) -> str:
        return os.path.join(self._state_root, "logs")

    # -- list ---------------------------------------------------------------

    def list_running_apps(self) -> Dict[str, Any]:
        specs, errors = self.registry.load()
        apps, discovery_error = self._union(specs)
        out: Dict[str, Any] = {
            "ok": True,
            "apps": [a.to_dict() for a in apps],
            "registry_path": self.registry.path,
        }
        if errors:
            out["registry_errors"] = errors
        if discovery_error:
            # LOUD: the union is incomplete — a stray would be invisible.
            out["discovery_error"] = discovery_error
            out["complete"] = False
        else:
            out["complete"] = True
        return out

    def _live_records(self) -> List[_Record]:
        """Hub records, liveness re-confirmed AT THE OBJECT; dead ones pruned."""
        live: List[_Record] = []
        with self._rec_lock:
            recs = list(self._records.values())
        for rec in recs:
            if rec.popen is not None:
                alive = rec.popen.poll() is None  # poll() reaps our own child
            else:
                try:
                    st = procinfo.probe(rec.pid)
                    alive = st.state == procinfo.ALIVE and procinfo.same_process(st, rec.started_at)
                except procinfo.ProcIntrospectionError:
                    alive = True  # unconfirmable -> keep listing it (conservative)
            if alive:
                live.append(rec)
            else:
                with self._rec_lock:
                    self._records.pop(rec.handle, None)
        return live

    def _union(self, specs: Dict[str, AppSpec]) -> Tuple[List[RunningApp], Optional[str]]:
        result: Dict[int, RunningApp] = {}
        for rec in self._live_records():
            result[rec.pid] = RunningApp(
                pid=rec.pid, app_id=rec.app_id, argv=list(rec.argv), cwd=rec.cwd,
                started_at=rec.started_at, source=rec.source, log_path=rec.log_path,
                handle=rec.handle, popen=rec.popen, entrypoint_real=rec.entrypoint_real,
            )
        discovery_error: Optional[str] = None
        if specs:
            me = os.getpid()
            try:
                pids = procinfo.all_pids()
                # POSITIVE CONTROL on the instrument: if it cannot read even our
                # own argv, every "no stray found" below would be a silent lie.
                if not procinfo.argv_of(me):
                    raise procinfo.ProcIntrospectionError(
                        "argv introspection failed its self-check (cannot read own argv)"
                    )
            except procinfo.ProcIntrospectionError as exc:
                pids = []
                discovery_error = str(exc)
            for pid in pids:
                if pid == me or pid in result:
                    continue
                try:
                    argv = procinfo.argv_of(pid)
                    if not argv:
                        continue
                    cwd = procinfo.cwd_of(pid)
                    app_id = match_signature(argv, cwd, specs)
                    if app_id is None:
                        continue
                    st = procinfo.probe(pid)
                except procinfo.ProcIntrospectionError as exc:
                    discovery_error = str(exc)
                    continue
                if st.state != procinfo.ALIVE:
                    continue
                result[pid] = RunningApp(
                    pid=pid, app_id=app_id, argv=argv, cwd=cwd,
                    started_at=st.start_time or 0.0, source="discovered",
                    entrypoint_real=specs[app_id].entrypoint_real,
                )
        return sorted(result.values(), key=lambda a: (a.app_id, a.started_at, a.pid)), discovery_error

    # -- launch -------------------------------------------------------------

    def launch_app(self, app_id: str, args: Optional[List[str]] = None) -> Dict[str, Any]:
        if not isinstance(app_id, str) or not app_id:
            raise AppLifecycleError("'app_id' is required and must be a non-empty string")
        if args is None:
            args = []
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise AppLifecycleError("'args' must be a list of strings (no shell; argv pass-through)")
        spec = self.registry.get(app_id)          # whitelist gate
        argv = spec.build_argv(args)              # fail loud on a missing socket
        cwd = spec.resolved_cwd()
        if not os.path.isdir(cwd):
            raise AppLifecycleError(f"app {app_id!r}: cwd does not exist: {cwd}", app_id=app_id)

        with self._op_lock:
            specs, _errors = self.registry.load()
            specs.setdefault(app_id, spec)
            union, discovery_error = self._union(specs)
            live = [a for a in union
                    if a.app_id == app_id or a.entrypoint_real == spec.entrypoint_real]
            policy = spec.single_instance_policy
            replaced: List[Dict[str, Any]] = []

            if policy != "allow" and discovery_error:
                # The single-instance check itself is blind — refuse rather
                # than risk starting a second writer next to an unseen one.
                raise AppLifecycleError(
                    f"app {app_id!r} (policy={policy}): cannot enforce single-instance, "
                    f"process discovery failed: {discovery_error}", app_id=app_id,
                )
            if live and policy == "refuse":
                raise AppLifecycleError(
                    f"app {app_id!r} is already running (policy=refuse)",
                    app_id=app_id, candidates=[a.to_dict() for a in live],
                )
            if live and policy == "adopt":
                if len(live) > 1:
                    raise AppLifecycleError(
                        f"app {app_id!r}: {len(live)} instances running, cannot adopt one "
                        f"unambiguously (policy=adopt); terminate by pid/handle first",
                        app_id=app_id, candidates=[a.to_dict() for a in live],
                    )
                return self._adopt(live[0])
            if live and policy == "replace":
                for inst in live:
                    res = self._terminate_target(inst)
                    replaced.append(res)
                    if not res.get("ok"):
                        raise AppLifecycleError(
                            f"app {app_id!r}: replace aborted — could not confirm the old "
                            f"instance pid {inst.pid} reaped; NOT launching a second instance",
                            app_id=app_id, replaced=replaced,
                        )

            out = self._spawn_and_prove_up(spec, argv, cwd)
            out["policy"] = policy
            out["policy_action"] = "replaced" if replaced else "launched"
            if replaced:
                out["replaced"] = replaced
            return out

    def _adopt(self, inst: RunningApp) -> Dict[str, Any]:
        handle = inst.handle
        if handle is None:
            handle = self._new_handle()
            with self._rec_lock:
                self._records[handle] = _Record(
                    handle=handle, app_id=inst.app_id, pid=inst.pid, argv=list(inst.argv),
                    cwd=inst.cwd or "", started_at=inst.started_at, source=inst.source,
                    entrypoint_real=inst.entrypoint_real,
                )
        out = inst.to_dict()
        out.update({
            "ok": True, "handle": handle, "status": "running",
            "log_path": inst.log_path, "policy": "adopt", "policy_action": "adopted",
        })
        return out

    def _new_handle(self) -> str:
        return f"app-{uuid.uuid4().hex[:12]}"

    def _spawn_and_prove_up(self, spec: AppSpec, argv: List[str], cwd: str) -> Dict[str, Any]:
        os.makedirs(self.log_dir, exist_ok=True)
        handle = self._new_handle()
        started = self._clock()
        stamp = _dt.datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S")
        log_path = os.path.join(self.log_dir, f"{spec.app_id}-{stamp}-{handle}.log")
        env = dict(os.environ)
        if spec.venv:
            venv = os.path.abspath(os.path.expanduser(spec.venv))
            env["VIRTUAL_ENV"] = venv
            env["PATH"] = os.path.join(venv, "bin") + os.pathsep + env.get("PATH", "")
            env.pop("PYTHONHOME", None)
        env["GNOMON_APP_HANDLE"] = handle
        with open(log_path, "ab") as log:
            try:
                popen = subprocess.Popen(
                    argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True, close_fds=True,
                )
            except OSError as exc:
                raise AppLifecycleError(
                    f"app {spec.app_id!r}: spawn failed: {exc}", app_id=spec.app_id, log_path=log_path
                ) from None
        try:
            st_start = procinfo.probe(popen.pid).start_time
        except procinfo.ProcIntrospectionError:
            st_start = None
        started_at = st_start if st_start else started
        rec = _Record(
            handle=handle, app_id=spec.app_id, pid=popen.pid, argv=list(argv), cwd=cwd,
            started_at=started_at, source="hub_launched", log_path=log_path, popen=popen,
            entrypoint_real=spec.entrypoint_real,
        )
        with self._rec_lock:
            self._records[handle] = rec

        # ⛔ invariant-prove-up-past-init: forked is NOT up. Hold for the settle
        # window, polling so an early death returns promptly, and only report
        # `running` if the pid is STILL alive at the END of the settle.
        deadline = time.monotonic() + spec.settle_s
        while True:
            rc = popen.poll()
            if rc is not None:
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
        rc = popen.poll()  # the final, authoritative re-check at t+settle

        out: Dict[str, Any] = {
            "ok": True,
            "app_id": spec.app_id,
            "handle": handle,
            "pid": popen.pid,
            "log_path": log_path,
            "started_at": _iso(started_at),
            "started_at_epoch": started_at,
            "argv": list(argv),
            "cwd": cwd,
            "settle_s": spec.settle_s,
        }
        if rc is None:
            out["status"] = "running"
        else:
            with self._rec_lock:
                self._records.pop(handle, None)
            out["status"] = "exited"
            out["exit_code"] = rc
            if rc < 0:
                try:
                    out["exit_signal"] = signal.Signals(-rc).name
                except ValueError:
                    pass
        return out

    # -- terminate ----------------------------------------------------------

    def terminate_app(self, handle_or_pid: Any) -> Dict[str, Any]:
        with self._op_lock:
            target = self._resolve_selector(handle_or_pid)
            return self._terminate_target(target)

    def _resolve_selector(self, sel: Any) -> RunningApp:
        """PID/handle-specific ONLY. Never a fan-out."""
        specs, _errors = self.registry.load()
        if isinstance(sel, bool) or sel is None:
            raise AppLifecycleError("'handle_or_pid' must be a pid (int) or a handle string")
        pid: Optional[int] = None
        handle: Optional[str] = None
        if isinstance(sel, int):
            pid = sel
        elif isinstance(sel, str) and sel.strip().isdigit():
            pid = int(sel.strip())
        elif isinstance(sel, str) and _HANDLE_RE.match(sel):
            handle = sel
        elif isinstance(sel, str) and sel:
            # Not a pid, not a handle. If it names an app, REFUSE and name the
            # live candidates — terminate never resolves a signature/app_id to
            # kills (1 or many); the caller must pick a specific instance.
            union, _ = self._union(specs)
            ep = specs[sel].entrypoint_real if sel in specs else None
            cands = [a.to_dict() for a in union
                     if a.app_id == sel or (ep is not None and a.entrypoint_real == ep)]
            raise AppLifecycleError(
                f"terminate_app is PID/handle-specific only; {sel!r} is not a pid or handle"
                + (f" — it matches {len(cands)} running instance(s) of that app; "
                   f"pass one pid/handle from 'candidates'" if cands else ""),
                candidates=cands,
            )
        else:
            raise AppLifecycleError("'handle_or_pid' must be a pid (int) or a handle string")

        union, discovery_error = self._union(specs)
        if handle is not None:
            with self._rec_lock:
                rec = self._records.get(handle)
            if rec is None:
                # A handle whose record is gone: the instance was already
                # confirmed exited (pruned) or was never ours.
                raise AppLifecycleError(
                    f"unknown or no-longer-running handle {handle!r}", handle=handle
                )
            matches = [a for a in union if a.handle == handle]
            if not matches:
                # Record existed but the process is gone -> not running.
                return RunningApp(pid=rec.pid, app_id=rec.app_id, argv=rec.argv, cwd=rec.cwd,
                                  started_at=rec.started_at, source=rec.source,
                                  log_path=rec.log_path, handle=handle, popen=rec.popen)
        else:
            matches = [a for a in union if a.pid == pid]
            if not matches:
                try:
                    st = procinfo.probe(pid)  # type: ignore[arg-type]
                except procinfo.ProcIntrospectionError as exc:
                    raise AppLifecycleError(f"pid {pid}: cannot introspect: {exc}", pid=pid) from None
                if st.state == procinfo.ABSENT:
                    return RunningApp(pid=pid, app_id="", argv=[], cwd=None,  # type: ignore[arg-type]
                                      started_at=0.0, source="discovered")
                raise AppLifecycleError(
                    f"pid {pid} is not a running whitelisted app — refusing (terminate never "
                    f"targets arbitrary processes)"
                    + (f"; discovery incomplete: {discovery_error}" if discovery_error else ""),
                    pid=pid,
                )
        if len(matches) > 1:  # defensive: a selector must name ONE live pid
            raise AppLifecycleError(
                f"selector {sel!r} resolves to {len(matches)} live pids — refused, no fan-out",
                candidates=[m.to_dict() for m in matches],
            )
        return matches[0]

    def _reaped(self, target: RunningApp) -> bool:
        """Is the target CONFIRMED reaped (gone from the process table)?"""
        if target.popen is not None:
            return target.popen.poll() is not None  # waitpid: our child is reaped
        try:
            st = procinfo.probe(target.pid)
        except procinfo.ProcIntrospectionError:
            return False  # cannot confirm -> NOT reaped (never a false "safe")
        if st.state == procinfo.ABSENT:
            return True
        if not procinfo.same_process(st, target.started_at):
            return True  # pid reused by a different process -> ours is gone
        return False  # ALIVE or ZOMBIE (not yet reaped) -> not confirmed

    def _wait_reaped(self, target: RunningApp, timeout: float) -> bool:
        if target.popen is not None:
            try:
                target.popen.wait(timeout=timeout)
                return True
            except subprocess.TimeoutExpired:
                return False
        deadline = time.monotonic() + timeout
        while True:
            if self._reaped(target):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(_POLL_S)

    def _grace_for(self, app_id: str) -> Tuple[float, float]:
        specs, _ = self.registry.load()
        spec = specs.get(app_id)
        if spec is None:
            return DEFAULT_TERMINATE_GRACE_S, DEFAULT_KILL_WAIT_S
        return spec.terminate_grace_s, spec.kill_wait_s

    def _terminate_target(self, target: RunningApp) -> Dict[str, Any]:
        """⛔ invariant-terminate-reaped-before-return.

        SIGTERM -> block until CONFIRMED reaped (an app's SIGTERM exit-handler /
        save-on-exit flush runs to completion before the kernel reaps it) ->
        else SIGKILL -> block until confirmed reaped -> else FAILURE. Returning
        ``ok: True`` therefore means "safe to mutate the file now".
        """
        pid = target.pid
        base = {"pid": pid, "app_id": target.app_id or None, "handle": target.handle}
        if self._reaped(target):
            self._forget(target)
            return {**base, "ok": True, "was_running": False, "method": None, "reaped": True}

        grace, kill_wait = self._grace_for(target.app_id)
        t0 = time.monotonic()
        method = "SIGTERM"
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            return {**base, "ok": False, "was_running": True, "method": None, "reaped": False,
                    "error": f"cannot signal pid {pid}: {exc}"}
        reaped = self._wait_reaped(target, grace)
        if not reaped:
            method = "SIGKILL"
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                return {**base, "ok": False, "was_running": True, "method": method, "reaped": False,
                        "error": f"cannot SIGKILL pid {pid}: {exc}"}
            reaped = self._wait_reaped(target, kill_wait)
        elapsed = round(time.monotonic() - t0, 3)
        if not reaped:
            try:
                st_state = procinfo.probe(pid).state
            except procinfo.ProcIntrospectionError as exc:
                st_state = f"unreadable ({exc})"
            return {**base, "ok": False, "was_running": True, "method": method, "reaped": False,
                    "state": st_state, "elapsed_s": elapsed,
                    "error": f"death UNCONFIRMED for pid {pid} after {method} (state={st_state}); "
                             f"NOT safe to mutate its files"}
        self._forget(target)
        out = {**base, "ok": True, "was_running": True, "method": method, "reaped": True,
               "elapsed_s": elapsed}
        if target.popen is not None:
            out["exit_code"] = target.popen.returncode
        return out

    def _forget(self, target: RunningApp) -> None:
        with self._rec_lock:
            if target.handle:
                self._records.pop(target.handle, None)
            for h, r in list(self._records.items()):
                if r.pid == target.pid:
                    self._records.pop(h, None)


# --------------------------------------------------------------------------
# MCP tool descriptors for the cluster
# --------------------------------------------------------------------------

APP_LIFECYCLE_TOOLS: List[Dict[str, Any]] = [
    {
        "name": "launch_app",
        "description": (
            "Launch a WHITELISTED desktop app by app_id (unknown app_id refused; no shell). "
            "Applies the app's single_instance_policy (refuse|adopt|replace|allow). Returns "
            "{handle, pid, status: running|exited, exit_code?, log_path, started_at}; "
            "status=running only after the pid survives the init settle window."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string", "description": "Registry key of the app."},
                "args": {
                    "type": "array", "items": {"type": "string"},
                    "description": "Optional argv pass-through (no shell).",
                },
            },
            "required": ["app_id"],
        },
    },
    {
        "name": "terminate_app",
        "description": (
            "Terminate ONE running whitelisted app by pid or handle (never by app_id — an "
            "ambiguous selector is refused and names the candidates). Blocks until the pid "
            "is CONFIRMED REAPED (SIGTERM, then SIGKILL); ok=true means it is safe to mutate "
            "the app's files now. Returns {was_running, method: SIGTERM|SIGKILL, pid}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "handle_or_pid": {
                    "type": ["string", "integer"],
                    "description": "A pid, or a handle returned by launch_app.",
                },
            },
            "required": ["handle_or_pid"],
        },
    },
    {
        "name": "list_running_apps",
        "description": (
            "List running whitelisted apps: the UNION of hub-launched records and apps "
            "discovered by argv+cwd signature. Each: {pid, app_id, argv, cwd, started_at, "
            "source: hub_launched|discovered, log_path?}; liveness re-confirmed at the object."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

APP_LIFECYCLE_TOOL_NAMES = frozenset(t["name"] for t in APP_LIFECYCLE_TOOLS)

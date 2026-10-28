"""``gnomon-app-launch <id>`` — the preflight wrapper launchd runs for every Gnomon app (
requirement-preflight-wrapper; architect rulings R6 and R18).

Invoked by the generated agent as ``<python> -m gnomon.appshost.launch <id> --data-root <DATA>``. At EVERY
start, hub up or not, it:

1. runs :func:`gnomon.appshost.preflight.check_all` (every precondition);
2. on a refusal: records ``runtime/preflight.json`` {outcome: refused, reason, detail}, prints
   ``refused(<reason>): <detail>`` to stderr (the app log), and exits :data:`REFUSAL_EXIT` WITHOUT exec'ing;
3. builds the app's CLOSED environment (g73-amendment-4 rule 4: an allow-list, never ours minus a deny-list),
   re-verifies both pins one last time (so an edit made during the rest of the preflight is caught: round-2 F8),
   and, if the manifest declares ``ui_token``, mints a fresh token, writes it to ``runtime/ui_token`` (mode 0600,
   replaced atomically) and passes it ONLY in the environment variable ``GNOMON_APP_UI_TOKEN`` (never argv); then
   ``exec``s the substituted start (a Python interpreter gets -I; rule 3) in the manifest's directory. The pid launchd
   supervises is therefore the app's own pid.

The wrapper never prints the token, the path values' contents or a licence's contents.
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
from typing import List, Optional

from . import pins as _pins
from . import preflight as _pf
from . import rules as _rules
from .state import HostPaths, atomic_write_bytes

UI_TOKEN_ENV = "GNOMON_APP_UI_TOKEN"
#: an unexpected preflight fault (EX_SOFTWARE): the app is not started and the status shows failed + the log
ERROR_EXIT = 70


def mint_ui_token(paths: HostPaths, app_id: str) -> str:
    token = secrets.token_urlsafe(32)
    os.makedirs(paths.runtime(app_id), mode=0o700, exist_ok=True)
    atomic_write_bytes(paths.ui_token(app_id), token.encode("ascii"), mode=0o600)
    return token


def _production_deps(paths: HostPaths) -> _pf.Deps:
    from . import deps as _deps
    from .launchd import RealLaunchd, label_for

    try:
        from ..config import ConfigStore

        services_cfg = ConfigStore().load().services
    except Exception:  # noqa: BLE001 - an unreadable config means no services are known (deps refuse loudly)
        services_cfg = None
    services = _deps.service_health_urls(services_cfg)
    launchd = RealLaunchd()

    def start_dependency(dep_id: str) -> None:
        if _deps.resolve(paths, dep_id, services) != "app":
            raise _pf.Refusal("requires", f"dependency {dep_id!r} is not an app Gnomon owns; mode start is refused")
        label = label_for(dep_id)
        launchd.bootstrap(label)
        launchd.kickstart(label)

    return _pf.Deps(dependency_healthy=_deps.make_health(paths, services, launchd), start_dependency=start_dependency)


# --------------------------------------------------------------------------------------------------------
# F6: the post-start listener check runs INSIDE the wrapper, so a boot start with no hub is checked too
# --------------------------------------------------------------------------------------------------------

#: the watcher samples the app's listening sockets for the app's WHOLE LIFE (round-3 fix for R2-F7: a bind at 32 s was
#: LAN-reachable until a status poll): every 0.5 s for the first 30 s, then every 2 s. It is still a tripwire, not a
#: boundary: an app can kill its own same-uid watcher, and a status poll catches what the watcher missed.
POST_START_WATCH_S = 30.0
POST_START_WATCH_INTERVAL_S = 0.5
POST_START_SLOW_INTERVAL_S = 2.0


def _pid_alive(pid: int) -> bool:
    from ..hub import procinfo

    try:
        return procinfo.probe(pid).state == procinfo.ALIVE
    except Exception:  # noqa: BLE001 - cannot tell: keep watching (the window is bounded)
        return True


def watch_listeners(paths: HostPaths, app_id: str, pid: int, *, probe=None, window_s: Optional[float] = POST_START_WATCH_S,
                    interval_s: float = POST_START_WATCH_INTERVAL_S, monotonic=None, sleep=None,
                    alive=None, kill=None, slow_after_s: float = POST_START_WATCH_S,
                    slow_interval_s: float = POST_START_SLOW_INTERVAL_S) -> str:
    """Sample the listening sockets of ``pid``'s process tree for ``window_s`` (None = until the process exits, at
    ``interval_s`` and, after ``slow_after_s``, at ``slow_interval_s``). A listener on anything but
    127.0.0.1 / ::1 STOPS the app (SIGTERM, then SIGKILL) and records refused(non-loopback) naming the address.
    Returns "refused" | "clean" | "exited"."""
    import signal as _signal
    import time as _time

    from .ports import ListenerProbe, ProbeError, process_tree
    from .state import append_audit, atomic_write_json, utc_now_iso

    probe = probe or ListenerProbe()
    mono = monotonic or _time.monotonic
    slp = sleep or _time.sleep
    is_alive = alive or _pid_alive
    do_kill = kill or os.kill
    started = mono()
    deadline = None if window_s is None else started + window_s
    warned = False
    while deadline is None or mono() < deadline:
        if not is_alive(pid):
            return "exited"
        try:
            bad = [l for l in probe.listeners_of(process_tree(pid)) if not l.loopback]
        except ProbeError as exc:
            if not warned:
                sys.stderr.write(f"gnomon-app-launch {app_id}: listener check could not run: {exc}\n")
                warned = True
            bad = []
        if bad:
            detail = f"{bad[0].describe()}; only 127.0.0.1 and ::1 are allowed"
            try:
                atomic_write_json(paths.refusal_record(app_id), {"reason": "non-loopback", "ts": utc_now_iso(),
                                                                 "detail": detail, "by": "wrapper"})
                append_audit(paths, app_id, verb="non-loopback-refused", actor="gnomon (wrapper listener check)",
                             actor_verified=True, outcome="refused",
                             detail={"address": bad[0].address, "port": bad[0].port, "pid": bad[0].pid})
            except OSError:
                pass
            sys.stderr.write(f"gnomon-app-launch {app_id}: refused(non-loopback): {detail}; stopping the app\n")
            sys.stderr.flush()
            try:
                do_kill(pid, _signal.SIGTERM)
                end = mono() + 5.0
                while mono() < end and is_alive(pid):
                    slp(0.1)
                if is_alive(pid):
                    do_kill(pid, _signal.SIGKILL)
            except ProcessLookupError:
                pass
            return "refused"
        slp(interval_s if (mono() - started) < slow_after_s else slow_interval_s)
    return "clean"


def _spawn_watcher(paths: HostPaths, app_id: str, pid: int) -> None:
    """Double-fork a detached watcher for ``pid`` (the wrapper's own pid, which exec keeps)."""
    child = os.fork()
    if child == 0:
        try:
            os.setsid()
            if os.fork() == 0:
                try:
                    devnull = os.open(os.devnull, os.O_RDONLY)
                    os.dup2(devnull, 0)
                    watch_listeners(paths, app_id, pid, window_s=None)
                finally:
                    os._exit(0)
        finally:
            os._exit(0)
    os.waitpid(child, 0)


def run(app_id: str, data_root: str, deps: Optional[_pf.Deps] = None, *, exec_fn=os.execve,
        watch: bool = True) -> int:
    paths = HostPaths(data_root)
    try:
        d = deps if deps is not None else _production_deps(paths)
        p = _pf.check_all(paths, app_id, d)
        argv = _pf.exec_argv(p, "start")                 # round 3c: the single choke point, immediately before exec
        argv.verify()                             # R3E-F1: the judged files are still the judged files
        env = _pf.build_env(paths, p, dict(os.environ))
        sig = _pins.live_signature((p.pin.get("code") or {}).get("root") or p.cwd, p.manifest_path,
                                   paths.settings(app_id))             # round 3c: BEFORE the verify, so a later edit shows
        _pf.load_installed(paths, app_id)       # round-2 F8: re-verify both pins immediately before exec
    except _rules.RuleViolation as exc:
        _pf.record_preflight(paths, app_id, "refused", "code-changed", exc.message)
        sys.stderr.write(f"gnomon-app-launch {app_id}: refused(code-changed): {exc.message}\n")
        sys.stderr.flush()
        return _pf.REFUSAL_EXIT
    except _pf.Refusal as r:
        _pf.record_preflight(paths, app_id, "refused", r.reason, r.detail)
        sys.stderr.write(f"gnomon-app-launch {app_id}: refused({r.reason}): {r.detail}\n")
        sys.stderr.flush()
        return _pf.REFUSAL_EXIT
    except Exception as exc:  # noqa: BLE001 - an unexpected fault is LOUD and never execs the app
        _pf.record_preflight(paths, app_id, "error", detail=f"{type(exc).__name__}: {exc}")
        sys.stderr.write(f"gnomon-app-launch {app_id}: preflight error, not starting: {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        return ERROR_EXIT
    if p.manifest.ui_token:
        env[UI_TOKEN_ENV] = mint_ui_token(paths, app_id)
    try:
        from .state import atomic_write_json as _awj

        _awj(paths.start_sig(app_id), {"sig": sig, "pid": os.getpid()})
    except OSError:
        pass                                    # no record = the status falls back to the full verify (never silent-OK)
    _pf.record_preflight(paths, app_id, "exec", warnings=p.warnings)
    for w in p.warnings:
        sys.stderr.write(f"gnomon-app-launch {app_id}: warning: {w}\n")
    sys.stderr.flush()
    os.chdir(p.cwd)
    if watch:
        _spawn_watcher(paths, app_id, os.getpid())
    try:
        argv.verify()                    # immediately before exec: (st_dev, st_ino) of interpreter and script
    except _rules.RuleViolation as exc:
        _pf.record_preflight(paths, app_id, "refused", "code-changed", exc.message)
        sys.stderr.write(f"gnomon-app-launch {app_id}: refused(code-changed): {exc.message}\n")
        sys.stderr.flush()
        return _pf.REFUSAL_EXIT
    exec_fn(argv.judged.exec_target(), list(argv), env)   # R3F-F1: the literal if it is plain and the same file, else the realpath
    return 0  # only reached with an injected exec_fn


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="gnomon-app-launch", description="Gnomon app preflight wrapper (launchd)")
    ap.add_argument("app_id")
    ap.add_argument("--data-root", default=None, help="the Gnomon DATA root (default: the machine config's)")
    ns = ap.parse_args(argv)
    if ns.data_root is None:
        from .state import data_root

        ns.data_root = data_root()
    return run(ns.app_id, ns.data_root)


if __name__ == "__main__":
    sys.exit(main())

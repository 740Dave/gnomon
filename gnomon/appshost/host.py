"""The Apps host controller: the one object the hub verbs and the web routes share.

Everything that changes an app's installed or running state goes through :class:`AppsHost` and appends ONE
audit entry through :func:`.state.append_audit` (g73-amendment item 2). Every start goes through the preflight
(:mod:`.preflight`) and through launchd via the injected adapter; the host never spawns the app itself.

Statuses: exactly one of ``stopped``, ``starting``, ``running``, ``port-conflict``,
``failed``, ``refused``, with a display ``label`` that refines two of them honestly:
``stopped (exited 0)`` and ``running (health unverified)``; a refusal's label is ``refused(<reason>)``.
A not-yet-confirmed proposal is ``pending`` and is not an installed app.

Builder choices (each [A, builder], listed in the build report):

* the port range for the fallback assignment is read from the machine config key ``apps_port_range``
  ``[low, high]``; it is UNSET by default because the range is an OPEN question (open-question-port-range).
  With no range, a proposal whose preferred port is taken (or that has none) is refused, naming the gap;
* the confirm takes the path values and settings in its body; a missing required path value fails the confirm
  at step ``values`` and leaves the app uninstalled;
* a backup is RECORDED, not made: the operator names an existing backup location and the host records it after
  checking it exists (the spec says "exists and is recorded"; making the copy is not specified);
* Open from the PAGE refuses an app that is not running; the ``app_open`` verb (g73-amendment-4b: app id only, no URL or
  path from the caller) starts it first through the same pinned, confirmed start path, then opens the app's loopback URL;
* stop = the declared signal through launchd, the declared grace, then SIGKILL, then the agent is unloaded
  (``bootout``) so the next start always loads a freshly generated plist.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import subprocess
import threading
import time
import webbrowser
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from . import deps as _deps
from . import licence as _licence
from . import pins as _pins
from . import preflight as _pf
from . import redact as _redact_mod
from . import rules as _rules
from . import fsafe as _fsafe
from .fsafe import open_regular
from .launchd import LaunchdAdapter, build_plist, label_for
from .manifest import (ID_RE, WORKSPACE_RE, Manifest, ManifestError, load_manifest, parse_manifest,
                       read_manifest_bytes, validate_path_value, validate_setting_value)
from .ports import Listener, ListenerProbe, ProbeError, port_is_free, process_tree
from .state import (AppsError, HostPaths, append_audit, archive_app_dir, atomic_write_json, host_lock, manifest_entries,
                    read_audit, read_json, read_ports, read_registry, tail_lines, unlink_quiet, utc_now_iso,
                    write_ports, write_registry)

STATUSES = ("stopped", "starting", "running", "port-conflict", "failed", "refused")
DEFAULT_START_SETTLE_S = 1.5
DEFAULT_OPEN_WAIT_S = 10.0
KILL_WAIT_S = 5.0
_POLL_S = 0.05
UI_TOKEN_QUERY = "gnomon_ui_token"          # [A, builder] the query parameter the Open URL carries



def _proved(fn):
    """Run ``fn`` with the presence evidence (``proof=`` keyword) riding every audit line it writes (confirm-proof).
    A decorator, so the wrapped function keeps its own name (the exec-site table names ``AppsHost.run_action``)."""
    import functools

    @functools.wraps(fn)
    def wrapper(self, *args, proof=None, **kwargs):
        with self._op_lock:
            self._cur_proof = proof
            try:
                return fn(self, *args, **kwargs)
            finally:
                self._cur_proof = None
    return wrapper


class ActChanged(AppsError):
    """The act that was authenticated is not the act that would commit (the proposal / settings changed during the
    presence prompt). Refused; nothing changed."""

@dataclass(frozen=True)
class Actor:
    id: str
    verified: bool = False


def _default_opener(url: str) -> None:
    """The REAL opener. Refuses to run under pytest (g31/g74 pattern: tests inject their own opener)."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the real app opener refuses to run under pytest (inject an opener)")
    webbrowser.open_new(url)


class AppsHost:
    def __init__(
        self,
        data_root: Any,
        launchd: LaunchdAdapter,
        *,
        probe: Any = None,
        opener: Optional[Callable[[str], None]] = None,
        services: Optional[Callable[[], Dict[str, Optional[str]]]] = None,
        port_range: Optional[Callable[[], Optional[Tuple[int, int]]]] = None,
        source_roots: Tuple[str, ...] = (),
        http_health: Callable[[int, str], bool] = _pf.http_healthy,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        today: Optional[Callable[[], _dt.date]] = None,
        start_settle_s: float = DEFAULT_START_SETTLE_S,
        open_wait_s: float = DEFAULT_OPEN_WAIT_S,
        binding_on: Callable[[], bool] = lambda: False,
        python: Optional[str] = None,
        requires_start_budget_s: float = _pf.REQUIRES_START_BUDGET_S,
        require_digest: bool = True,
    ) -> None:
        #: delta2 F-1: a confirm / settings / action call that carries NO act digest is refused whenever the state under
        #: the op lock has anything that needs proof, unless the caller passes ``proof_waived=True`` (the server does
        #: so only when ``confirm_proof`` is OFF = the previous behaviour). The test host (tests/appshost_support)
        #: builds with False so the many direct host-level tests keep calling the host as a library.
        self.require_digest = require_digest
        self._root = data_root if callable(data_root) else (lambda r=data_root: r)
        self.launchd = launchd
        self.probe = probe if probe is not None else ListenerProbe()
        self.opener = opener or _default_opener
        self._services = services or (lambda: {})
        self._port_range = port_range or (lambda: None)
        self._source_roots = tuple(os.path.realpath(os.path.expanduser(r)) for r in source_roots)
        self._http = http_health
        self._clock = clock
        self._mono = monotonic
        self._sleep = sleep
        self._today = today or (lambda: _dt.datetime.now(_dt.timezone.utc).date())
        self.start_settle_s = start_settle_s
        self.open_wait_s = open_wait_s
        self._binding_on = binding_on
        self._python = python
        self._requires_budget = requires_start_budget_s
        self._op_lock = threading.RLock()
        self._cur_proof: Optional[Dict[str, Any]] = None
        #: the reaper threads of running actions (each appends the action's exit to the audit)
        self.action_threads: List[threading.Thread] = []

    # ------------------------------------------------------------------------------------------------
    # plumbing
    # ------------------------------------------------------------------------------------------------
    @property
    def paths(self) -> HostPaths:
        return HostPaths(self._root())

    def actor(self, caller: Optional[str]) -> Actor:
        """The actor of a hub call: the caller the transport established, marked verified only when the hub
        caller binding (g60b) is on. This tree has no binding, so every hub actor is unverified."""
        if caller:
            return Actor(caller, bool(self._binding_on()))
        return Actor("unidentified hub caller", False)

    def _audit(self, app_id: str, verb: str, actor: Actor, outcome: str, **detail: Any) -> None:
        proof = getattr(self, "_cur_proof", None)
        if proof and "proof" not in detail:     # confirm-proof (g76 amendment): the evidence rides every audit line
            detail["proof"] = proof
        append_audit(self.paths, app_id, verb=verb, actor=actor.id, actor_verified=actor.verified,
                     outcome=outcome, detail=detail or None, ts=self._clock())

    def _installed(self) -> Dict[str, str]:
        return manifest_entries(self.paths)

    def _require_installed(self, app_id: Any) -> str:
        if not isinstance(app_id, str) or not ID_RE.match(app_id):
            raise AppsError(f"invalid app id {app_id!r}")
        entries = self._installed()
        if app_id not in entries:
            if os.path.exists(self.paths.proposal(app_id)):
                raise AppsError(f"{app_id!r} is proposed but NOT installed: an install takes effect only when the operator "
                                "confirms it on the Apps tab", status="pending")
            raise AppsError(f"no installed app {app_id!r}")
        return entries[app_id]

    def _load(self, app_id: str) -> Tuple[Optional[Manifest], str, Dict[str, Any]]:
        """Lenient load for status/detail (never raises on a changed manifest)."""
        mpath = self._installed()[app_id]
        settings = read_json(self.paths.settings(app_id), default={}) or {}
        try:
            m = parse_manifest(json.loads(read_manifest_bytes(mpath).decode("utf-8")))
        except (ManifestError, ValueError, UnicodeDecodeError):
            m = None
        return m, mpath, settings

    def _pinned(self, app_id: str) -> Tuple[Optional[Manifest], Optional[Dict[str, Any]]]:
        """The manifest AS PINNED at confirm (from the sha-checked snapshot), and the pin. The host takes every
        argv it runs itself, and every behaviour-setting field, from here, never from the file on disk (F3)."""
        pin = read_json(self.paths.pin(app_id), default=None)
        if not isinstance(pin, dict):
            return None, None
        raw = _pins.pinned_manifest_bytes(self.paths.pin_snapshot(app_id), pin)
        if raw is None:
            return None, pin
        try:
            return parse_manifest(json.loads(raw.decode("utf-8"))), pin
        except (ManifestError, ValueError, UnicodeDecodeError):
            return None, pin

    def _redact(self, app_id: str, lines: List[str]) -> List[str]:
        """Rule 5 / R2-F6: a log tail is redacted by PATTERN on every poll (see :mod:`.redact`), independent of any
        token file; the current ui_token is also replaced verbatim when its file still exists (belt and braces)."""
        tok = ""
        try:
            with open_regular(self.paths.ui_token(app_id)) as fh:      # the app can reach ../runtime: never block on a FIFO
                tok = fh.read().decode("ascii", "replace").strip()
        except OSError:
            pass
        out = []
        for l in lines:
            if tok:
                l = l.replace(tok, _redact_mod.REDACTED)
            out.append(_redact_mod.redact_line(l))
        return out

    def _port(self, app_id: str) -> Optional[int]:
        row = read_ports(self.paths).get(app_id)
        return int(row["port"]) if row else None

    def _services_map(self) -> Dict[str, Optional[str]]:
        try:
            return dict(self._services() or {})
        except Exception:  # noqa: BLE001 - an unreadable services registry knows no services (deps refuse)
            return {}

    def _deps(self, actor: Actor, chain: Tuple[str, ...]) -> _pf.Deps:
        services = self._services_map()

        def start_dependency(dep_id: str) -> None:
            if _deps.resolve(self.paths, dep_id, services) != "app":
                raise _pf.Refusal("requires", f"dependency {dep_id!r} is not an app Gnomon owns; mode start refused")
            if dep_id in chain:
                raise _pf.Refusal("requires", f"dependency cycle through {dep_id!r}")
            res = self._start(dep_id, actor, chain + (dep_id,))
            if not res.get("ok"):
                raise _pf.Refusal("requires", f"dependency {dep_id!r} did not start: {res.get('error') or res.get('label')}")

        return _pf.Deps(dependency_healthy=_deps.make_health(self.paths, services, self.launchd, http=self._http),
                        start_dependency=start_dependency, today=self._today, sleep=self._sleep,
                        monotonic=self._mono, requires_start_budget_s=self._requires_budget)

    # ------------------------------------------------------------------------------------------------
    # listing + status
    # ------------------------------------------------------------------------------------------------
    def list(self) -> Dict[str, Any]:
        apps = []
        for app_id in sorted(self._installed()):
            m, _mp, settings = self._load(app_id)
            st = self.status(app_id)
            apps.append({"id": app_id, "name": m.name if m else app_id, "status": st["status"],
                         "label": st["label"], "scope": settings.get("scope", m.scope if m else "all"),
                         "headless": (m.headless if m else True)})
        return {"ok": True, "apps": apps, "pending": self.pending(), "empty": not apps,
                "workspace_note": "one workspace: scope is stored and shown, and filters nothing yet"}

    def pending(self) -> List[Dict[str, Any]]:
        out = []
        try:
            names = sorted(os.listdir(self.paths.proposals))
        except FileNotFoundError:
            return []
        for fn in names:
            if not fn.endswith(".json"):
                continue
            rec = read_json(os.path.join(self.paths.proposals, fn), default=None)
            if not isinstance(rec, dict):
                continue
            valid, why = self._proposal_valid(rec)
            out.append({"id": rec.get("app_id"), "kind": rec.get("kind"), "name": rec.get("name"),
                        "proposed_by": rec.get("proposed_by"), "proposed_by_verified": rec.get("proposed_by_verified"),
                        "proposed_at": rec.get("proposed_at"), "valid": valid, "invalid_reason": why})
        return out

    def _proposal_valid(self, rec: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        if rec.get("kind") == "uninstall":
            return True, None
        try:
            m, raw = load_manifest(rec["manifest_path"])
        except ManifestError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"
        if _pins.sha256_bytes(raw) != rec.get("manifest_sha256"):
            return False, "the manifest changed after it was proposed; it must be proposed again"
        if m.id != rec.get("app_id"):
            return False, "the manifest's id changed after it was proposed"
        return True, None

    def _foreign(self, app_id: str, port: int, tree: Set[int]) -> Optional[Listener]:
        ls = self.probe.listeners_on_port(port)
        foreign = [l for l in ls if l.pid not in tree]
        if not foreign:
            unlink_quiet(self.paths.foreign_record(app_id))
            return None
        holder = foreign[0]
        seen = read_json(self.paths.foreign_record(app_id), default={}) or {}
        key = f"{holder.pid}:{holder.command}"
        if seen.get("key") != key:
            atomic_write_json(self.paths.foreign_record(app_id), {"key": key, "ts": utc_now_iso(self._clock())})
            self._audit(app_id, "foreign-listener-detected", Actor("gnomon (status check)", True), "port-conflict",
                        pid=holder.pid, command=holder.command, address=holder.address, port=port)
        return holder

    def _alive(self, app_id: str) -> Tuple[bool, Any]:
        st = self.launchd.state(label_for(app_id))
        return bool(st.loaded and st.pid), st

    def status(self, app_id: str) -> Dict[str, Any]:
        self._require_installed(app_id)
        m, mpath, settings = self._load(app_id)
        pm, pin = self._pinned(app_id)            # F3: behaviour and argv come from the PINNED manifest only
        alive, st = self._alive(app_id)
        port = self._port(app_id) if (pm is None or pm.has_port) else None
        out: Dict[str, Any] = {"ok": True, "app_id": app_id, "warnings": []}
        tree: Set[int] = process_tree(st.pid) if alive else set()

        drift: List[Tuple[str, str]] = []

        def done(status: str, label: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
            assert status in STATUSES
            lab = label or status
            if drift and status in ("running", "starting"):
                # R3-F10: a RUNNING app whose pinned code or manifest changed is never plain "running"
                lab = f"{lab} (changed since confirm: {drift[0][0]}; refused on the next start)"
                extra.update(drift=True, drift_reason=drift[0][0], drift_detail=drift[0][1])
                out["warnings"].append(f"the app's pinned code or manifest changed on disk since it was confirmed "
                                       f"({drift[0][0]}: {drift[0][1]}); the running process is the code the operator "
                                       "confirmed, but it will be refused on the next start until re-confirmed")
            out.update(status=status, label=lab, running=status in ("running", "starting"), **extra)
            if status not in ("running", "starting"):
                unlink_quiet(self.paths.ui_token(app_id))         # a token never outlives its process
            return out

        if port is not None:
            try:
                holder = self._foreign(app_id, port, tree)
            except ProbeError as exc:
                holder = None
                out["warnings"].append(f"the port probe could not run, so a foreign listener cannot be ruled out: {exc}")
            if holder is not None:
                return done("port-conflict", f"port-conflict: {holder.describe()}",
                            holder={"pid": holder.pid, "command": holder.command, "address": holder.address})
        if alive:
            try:
                bad = [l for l in self.probe.listeners_of(tree) if not l.loopback]
            except ProbeError as exc:
                bad = []
                out["warnings"].append(f"the listener check could not run: {exc}")
            if bad:
                self._refuse_non_loopback(app_id, bad[0])
                return done("refused", f"refused(non-loopback): {bad[0].address}", reason="non-loopback",
                            detail=f"{bad[0].describe()}; only 127.0.0.1 and ::1 are allowed")
            if pm is None:
                out["warnings"].append("the pinned manifest copy is missing or altered: health is not checked "
                                       "(re-confirm the app)")
                return done("running", "running (health unverified)", pid=st.pid, health_verified=False)
            bad_run = self._running_drift(app_id, pin, settings, mpath)
            if bad_run is not None:
                drift.append(bad_run)
            if pm.has_port and port is not None:
                if self._http(port, pm.health_path or "/"):
                    return done("running", pid=st.pid)
                return done("starting", "starting/unhealthy", pid=st.pid)
            return done("running", "running (health unverified)", pid=st.pid, health_verified=False)
        # not alive
        ref = read_json(self.paths.refusal_record(app_id), default=None)
        if isinstance(ref, dict) and ref.get("reason"):
            return done("refused", f"refused({ref['reason']})", reason=ref["reason"], detail=ref.get("detail"))
        pre = read_json(self.paths.preflight_record(app_id), default=None)
        if isinstance(pre, dict) and pre.get("outcome") == "refused":
            return done("refused", f"refused({pre.get('reason')})", reason=pre.get("reason"), detail=pre.get("detail"))
        if isinstance(pin, dict) and isinstance(pin.get("manifest_path"), str):
            try:
                raw = read_manifest_bytes(pin["manifest_path"])
            except ManifestError:
                raw = b""
            try:
                cur = parse_manifest(json.loads(raw.decode("utf-8")))
            except (ManifestError, ValueError, UnicodeDecodeError):
                cur = None
            bad_pin = _pins.verify(pin, cur, raw, settings, manifest_path=mpath)
            if bad_pin is not None:
                return done("refused", f"refused({bad_pin[0]})", reason=bad_pin[0], detail=bad_pin[1])
        if os.path.exists(self.paths.stop_record(app_id)):
            return done("stopped")
        if isinstance(pre, dict) and pre.get("outcome") == "error":
            return done("failed", detail=pre.get("detail"),
                        log_tail=self._redact(app_id, tail_lines(self.paths.app_log(app_id))))
        if st.loaded and st.last_exit is not None:
            if st.last_exit == 0:
                return done("stopped", "stopped (exited 0)", exit_code=0)
            return done("failed", f"failed (exit {st.last_exit})", exit_code=st.last_exit,
                        log_tail=self._redact(app_id, tail_lines(self.paths.app_log(app_id))))
        return done("stopped")

    def _running_drift(self, app_id: str, pin: Any, settings: Any, mpath: Any) -> Optional[Tuple[str, str]]:
        """R3-F10: ``None`` when the pinned inputs of a RUNNING app are untouched, else ``(reason, detail)``.

        Cost design (measured in RUN_SUMMARY.md, R3-F9): the wrapper records a stat-only signature right before exec
        (:func:`pins.live_signature`); a poll recomputes it (stat only, no file opened) and, when it is equal, stops.
        Only when it differs, or no record exists, does it run the full :func:`pins.verify` (whole-tree re-hash). Reads
        files only; runs no process."""
        if not isinstance(pin, dict) or not isinstance(pin.get("manifest_path"), str):
            return ("manifest-changed", "the app has no readable pin")
        root = (pin.get("code") or {}).get("root")
        rec = read_json(self.paths.start_sig(app_id), default=None)
        if isinstance(root, str) and isinstance(rec, dict) and isinstance(rec.get("sig"), str):
            try:
                if _pins.live_signature(root, pin["manifest_path"], self.paths.settings(app_id)) == rec["sig"]:
                    return None
            except OSError:
                pass
        try:
            raw = read_manifest_bytes(pin["manifest_path"])
        except ManifestError:
            raw = b""
        try:
            cur = parse_manifest(json.loads(raw.decode("utf-8")))
        except (ManifestError, ValueError, UnicodeDecodeError):
            cur = None
        return _pins.verify(pin, cur, raw, settings, manifest_path=mpath)

    def _refuse_non_loopback(self, app_id: str, listener: Listener) -> None:
        atomic_write_json(self.paths.refusal_record(app_id), {
            "reason": "non-loopback", "ts": utc_now_iso(self._clock()),
            "detail": f"{listener.describe()}; only 127.0.0.1 and ::1 are allowed"})
        self._stop_process(app_id, None)
        self._audit(app_id, "non-loopback-refused", Actor("gnomon (listener check)", True), "refused",
                    address=listener.address, port=listener.port, pid=listener.pid)

    # ------------------------------------------------------------------------------------------------
    # start / stop / open
    # ------------------------------------------------------------------------------------------------
    def start(self, app_id: Any, actor: Actor) -> Dict[str, Any]:
        with self._op_lock:
            try:
                self._require_installed(app_id)
            except AppsError as exc:
                if isinstance(app_id, str) and ID_RE.match(app_id) and os.path.isdir(self.paths.app(app_id)):
                    self._audit(app_id, "start", actor, "refused", error=str(exc))
                raise
            return self._start(app_id, actor, (app_id,))

    def _start(self, app_id: str, actor: Actor, chain: Tuple[str, ...]) -> Dict[str, Any]:
        label = label_for(app_id)
        alive, _st = self._alive(app_id)
        if alive:
            s = self.status(app_id)
            self._audit(app_id, "start", actor, "already-running", status=s["label"])
            return {**s, "ok": True, "already_running": True}
        m = self._pinned(app_id)[0]          # never the on-disk manifest: a missing pinned copy is refused by check_all
        port = self._port(app_id) if (m is not None and m.has_port) else None
        if port is not None:
            try:
                holder = self._foreign(app_id, port, set())
            except ProbeError as exc:
                self._audit(app_id, "start", actor, "refused", error=f"port probe failed: {exc}")
                return {"ok": False, "app_id": app_id, "status": "port-conflict",
                        "error": f"cannot verify the port is free (probe failed: {exc}); not starting"}
            if holder is not None:
                self._audit(app_id, "start", actor, "port-conflict", pid=holder.pid, command=holder.command)
                return {"ok": False, "app_id": app_id, "status": "port-conflict",
                        "label": f"port-conflict: {holder.describe()}",
                        "error": f"port {port} is held by a process Gnomon did not start: {holder.describe()}",
                        "holder": {"pid": holder.pid, "command": holder.command, "address": holder.address}}
        try:
            prep = _pf.check_all(self.paths, app_id, self._deps(actor, chain))
        except _pf.Refusal as r:
            _pf.record_preflight(self.paths, app_id, "refused", r.reason, r.detail)
            unlink_quiet(self.paths.stop_record(app_id))
            self._audit(app_id, "start", actor, f"refused({r.reason})", detail=r.detail)
            return {"ok": False, "app_id": app_id, "status": "refused", "label": f"refused({r.reason})",
                    "reason": r.reason, "error": f"refused({r.reason}): {r.detail}"}
        for rec in (self.paths.stop_record(app_id), self.paths.refusal_record(app_id),
                    self.paths.preflight_record(app_id), self.paths.foreign_record(app_id),
                    self.paths.start_sig(app_id)):
            unlink_quiet(rec)
        os.makedirs(self.paths.logs(app_id), exist_ok=True)
        plist = build_plist(app_id, data_root=self.paths.root, run_at_boot=bool(prep.settings.get("run_at_boot")),
                            log_path=self.paths.app_log(app_id), exit_timeout_s=prep.manifest.stop_grace_s,
                            python=self._python)
        self.launchd.write_plist(label, plist)
        if self.launchd.state(label).loaded:
            # a loaded, not-running agent keeps its OLD definition until it is unloaded: reload the fresh plist
            self.launchd.bootout(label)
        self.launchd.bootstrap(label)
        self.launchd.kickstart(label)
        deadline = self._mono() + self.start_settle_s
        while self._mono() < deadline:
            alive, st = self._alive(app_id)
            if not alive and st.last_exit is not None:
                break
            pre = read_json(self.paths.preflight_record(app_id), default=None)
            if alive and isinstance(pre, dict) and pre.get("outcome") == "exec":
                break
            self._sleep(_POLL_S)
        s = self.status(app_id)
        self._audit(app_id, "start", actor, "ok" if s["status"] in ("running", "starting") else s["label"],
                    status=s["label"], warnings=prep.warnings or None)
        return {**s, "ok": s["status"] in ("running", "starting"), "warnings": s.get("warnings", []) + prep.warnings}

    def stop(self, app_id: Any, actor: Actor) -> Dict[str, Any]:
        with self._op_lock:
            self._require_installed(app_id)
            res = self._stop_process(app_id, actor)
            return res

    def _stop_process(self, app_id: str, actor: Optional[Actor]) -> Dict[str, Any]:
        m = self._pinned(app_id)[0]                      # the stop the operator confirmed, not the file on disk
        sig = m.stop_signal if m else "SIGTERM"
        grace = m.stop_grace_s if m else 10.0
        label = label_for(app_id)
        alive, st = self._alive(app_id)
        out: Dict[str, Any] = {"ok": True, "app_id": app_id, "was_running": alive, "signal": None,
                               "killed": False, "grace_s": grace}
        if alive:
            self.launchd.kill(label, sig)
            out["signal"] = sig
            if not self._wait_dead(app_id, grace):
                self.launchd.kill(label, "SIGKILL")
                out["killed"] = True
                if not self._wait_dead(app_id, KILL_WAIT_S):
                    out.update(ok=False, error=f"{app_id}: the process did not exit after SIGKILL")
        if out["ok"]:
            self.launchd.bootout(label)
            atomic_write_json(self.paths.stop_record(app_id), {"ts": utc_now_iso(self._clock()),
                                                                "by": actor.id if actor else "gnomon"})
        unlink_quiet(self.paths.ui_token(app_id))
        if actor is not None:
            self._audit(app_id, "stop", actor, "ok" if out["ok"] else "failed",
                        was_running=alive, signal=out["signal"], killed=out["killed"])
        return out

    def _wait_dead(self, app_id: str, timeout: float) -> bool:
        deadline = self._mono() + timeout
        while True:
            alive, _ = self._alive(app_id)
            if not alive:
                return True
            if self._mono() >= deadline:
                return False
            self._sleep(_POLL_S)

    def open(self, app_id: Any, actor: Actor, *, start_if_needed: bool = False) -> Dict[str, Any]:
        """Open the app's UI in a NEW browser window. The page's Open (``start_if_needed=False``) refuses an app that
        is not running; the ``app_open`` verb (``True``, g73-amendment-4b) first starts it through :meth:`_start`
        (the same pinned, confirmed path: every round-3 refusal applies). A headless app is refused BEFORE any start.
        The URL handed to the browser is built from the PINNED manifest and the host's port, never from the caller;
        it carries the ui_token; the returned dict NEVER does."""
        with self._op_lock:
            self._require_installed(app_id)
            m = self._pinned(app_id)[0]                  # ui_path / ui_token as confirmed
            if m is None:
                raise AppsError(f"{app_id}: the pinned manifest copy is missing or altered; re-confirm on the Apps tab")
            if m.headless:
                self._audit(app_id, "open", actor, "refused", error="headless")
                return {"ok": False, "app_id": app_id,
                        "error": f"{app_id!r} is headless (its manifest has no ui_path): there is no UI to open"}
            s = self.status(app_id)
            if s["status"] not in ("running", "starting"):
                if not start_if_needed:
                    self._audit(app_id, "open", actor, "refused", status=s["label"])
                    return {"ok": False, "app_id": app_id, "status": s["status"], "label": s["label"],
                            "error": f"{app_id!r} is not running ({s['label']}); start it first"}
                started = self._start(app_id, actor, (app_id,))
                if not started.get("ok"):
                    self._audit(app_id, "open", actor, "refused", status=started.get("label"))
                    return {"ok": False, "app_id": app_id, "status": started.get("status"),
                            "label": started.get("label"), "error": started.get("error") or started.get("label"),
                            **({"reason": started["reason"]} if started.get("reason") else {})}
            deadline = self._mono() + self.open_wait_s
            while True:
                s = self.status(app_id)
                if s["status"] == "running" or self._mono() >= deadline or s["status"] not in ("running", "starting"):
                    break
                self._sleep(0.1)
            if s["status"] != "running":
                self._audit(app_id, "open", actor, "refused", status=s["label"])
                return {"ok": False, "app_id": app_id, "status": s["status"], "label": s["label"],
                        "error": f"{app_id!r} is {s['label']}; not opening a window onto a port that does not answer"}
            if s.get("drift"):                        # R3-F10: no window onto a running app whose pinned inputs changed
                self._audit(app_id, "open", actor, "refused", status=s["label"])
                return {"ok": False, "app_id": app_id, "status": s["status"], "label": s["label"],
                        "reason": s.get("drift_reason"),
                        "error": f"{app_id!r}: its pinned code or manifest changed since it was confirmed "
                                 f"({s.get('drift_reason')}); stop it and re-confirm before opening it"}
            port = self._port(app_id)
            url = self.ui_url(m, port)
            browser_url = url
            if m.ui_token:
                try:
                    with open_regular(self.paths.ui_token(app_id)) as fh:
                        token = fh.read().decode("ascii", "replace").strip()
                except OSError:
                    token = ""
                if not token:
                    self._audit(app_id, "open", actor, "refused", error="ui_token file missing")
                    return {"ok": False, "app_id": app_id,
                            "error": f"{app_id!r} declares ui_token but no token file exists; restart the app"}
                browser_url = url + ("&" if "?" in url else "?") + f"{UI_TOKEN_QUERY}={token}"
            try:
                self.opener(browser_url)
            except Exception as exc:  # noqa: BLE001 - R3-F17: an opener that raises must not carry the token out
                del browser_url
                self._audit(app_id, "open", actor, "failed", error=f"the browser opener failed ({type(exc).__name__})")
                return {"ok": False, "app_id": app_id,
                        "error": f"the browser could not be opened ({type(exc).__name__}); nothing was opened"}
            del browser_url
            self._audit(app_id, "open", actor, "ok", url=url)
            return {"ok": True, "app_id": app_id, "opened": True, "url": url, "new_window": True,
                    "token_in_url": bool(m.ui_token)}

    # ------------------------------------------------------------------------------------------------
    # proposals (install / uninstall), confirm, decline
    # ------------------------------------------------------------------------------------------------
    def _inside_source(self, path: str) -> bool:
        return any(_rules.inside(path, r) for r in self._source_roots)

    def _check_requires_registration(self, m: Manifest, extra_graph: Optional[Dict[str, List[str]]] = None) -> None:
        services = self._services_map()
        installed = self._installed()
        for i, r in enumerate(m.requires):
            kind = _deps.resolve(self.paths, r.id, services)
            if kind is None:
                raise ManifestError(f"requires[{i}].id", f"{r.id!r} is not registered (neither an installed app nor "
                                    "a machine-config service)")
            if r.mode == "start" and kind != "app":
                raise ManifestError(f"requires[{i}].mode", f"mode start is allowed only for apps Gnomon owns; "
                                    f"{r.id!r} is an external service (use mode check)")
            if r.mode == "check" and kind == "service-no-health":
                raise ManifestError(f"requires[{i}].id", f"the service {r.id!r} declares no health_url, so a "
                                    "mode check dependency on it cannot be health-checked")
        graph: Dict[str, List[str]] = {}
        for aid, mp in installed.items():
            try:
                dm = parse_manifest(json.loads(read_manifest_bytes(mp).decode("utf-8")))
            except (ManifestError, ValueError, UnicodeDecodeError):
                continue
            graph[aid] = [r.id for r in dm.requires if r.mode == "start"]
        graph[m.id] = [r.id for r in m.requires if r.mode == "start"]
        # DFS from m.id for a cycle
        seen: Set[str] = set()
        stack: List[Tuple[str, List[str]]] = [(m.id, [m.id])]
        while stack:
            node, trail = stack.pop()
            for nxt in graph.get(node, []):
                if nxt == m.id:
                    raise ManifestError("requires", "a dependency cycle among mode start requires: "
                                        + " -> ".join(trail + [nxt]))
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append((nxt, trail + [nxt]))

    def _assign_port(self, m: Manifest) -> Optional[int]:
        if not m.has_port:
            return None
        table = read_ports(self.paths)
        taken = {int(v["port"]) for k, v in table.items() if k != m.id}
        if m.preferred_port is not None and m.preferred_port not in taken and port_is_free(m.preferred_port):
            return m.preferred_port
        rng = self._port_range()
        if not rng:
            raise ManifestError("preferred_port", "no free preferred port, and the Gnomon port range is not set "
                                "(OPEN: open-question-port-range; set apps_port_range "
                                "[low, high] in the machine config)")
        lo, hi = int(rng[0]), int(rng[1])
        for p in range(lo, hi + 1):
            if p not in taken and port_is_free(p):
                return p
        raise ManifestError("preferred_port", f"no free port in the Gnomon range {lo}-{hi}")

    def propose_install(self, manifest_path: Any, actor: Actor) -> Dict[str, Any]:
        if not isinstance(manifest_path, str) or not manifest_path.strip():
            raise AppsError("'manifest_path' is required: the absolute path of the app's manifest file")
        mp = os.path.expanduser(manifest_path)           # R3E-F1: never abspath/normpath; the judged string is the read string
        if not os.path.isabs(mp):
            raise AppsError("'manifest_path' must be absolute (the manifest ships in the app's own directory)")
        if self._inside_source(mp):
            raise AppsError("refused: the manifest is inside Gnomon's source tree; a manifest ships in the app's "
                            "own directory, outside Gnomon (requirement-location)")
        with self._op_lock, host_lock(self.paths):
            try:
                m, raw = load_manifest(mp)
            except ManifestError as exc:
                return {"ok": False, "error": f"manifest refused at registration: {exc}", "field": exc.field,
                        "registered": False, "port_assigned": False}
            try:
                _pins.check_install_rules(m, mp)                           # g73-amendment-4 rule 2
            except _rules.RuleViolation as exc:
                return {"ok": False, "error": f"manifest refused at registration: {exc}", "field": exc.field,
                        "registered": False, "port_assigned": False}
            except _pins.PinError as exc:
                return {"ok": False, "error": f"manifest refused at registration: app_root: {exc}", "field": "app_root",
                        "registered": False, "port_assigned": False}
            reg = read_registry(self.paths)["apps"]
            if m.id in reg:
                why = "already installed" if isinstance(reg[m.id], dict) and "manifest" in reg[m.id] else \
                    "already registered in apps.json as a legacy app"
                return {"ok": False, "error": f"{m.id!r} is {why}", "registered": False, "port_assigned": False}
            if os.path.exists(self.paths.proposal(m.id)):
                return {"ok": False, "error": f"{m.id!r} already has a pending proposal", "registered": False,
                        "port_assigned": False}
            try:
                self._check_requires_registration(m)
                port = self._assign_port(m)
            except ManifestError as exc:
                return {"ok": False, "error": f"manifest refused at registration: {exc}", "field": exc.field,
                        "registered": False, "port_assigned": False}
            if port is not None:
                table = read_ports(self.paths)
                table[m.id] = {"port": port, "state": "proposed", "assigned_at": utc_now_iso(self._clock())}
                write_ports(self.paths, table)
            rec = {"kind": "install", "app_id": m.id, "name": m.name, "manifest_path": mp,
                   "manifest_sha256": _pins.sha256_bytes(raw), "proposed_by": actor.id,
                   "proposed_by_verified": actor.verified, "proposed_at": utc_now_iso(self._clock())}
            atomic_write_json(self.paths.proposal(m.id), rec)
            self._audit(m.id, "propose-install", actor, "pending", manifest_path=mp)
        return {"ok": True, "app_id": m.id, "status": "pending", "registered": True,
                "note": "PROPOSED only: nothing is installed and nothing runs until the operator confirms it on the "
                        "Apps tab (Pending section)"}

    def propose_uninstall(self, app_id: Any, actor: Actor) -> Dict[str, Any]:
        with self._op_lock, host_lock(self.paths):
            self._require_installed(app_id)
            if os.path.exists(self.paths.proposal(app_id)):
                return {"ok": False, "error": f"{app_id!r} already has a pending proposal"}
            m, _mp, _s = self._load(app_id)
            atomic_write_json(self.paths.proposal(app_id), {
                "kind": "uninstall", "app_id": app_id, "name": m.name if m else app_id, "proposed_by": actor.id,
                "proposed_by_verified": actor.verified, "proposed_at": utc_now_iso(self._clock())})
            self._audit(app_id, "propose-uninstall", actor, "pending")
        return {"ok": True, "app_id": app_id, "status": "pending-uninstall",
                "note": "PROPOSED only: the app stays installed until the operator confirms the uninstall on the Apps tab"}

    def decline(self, app_id: Any, actor: Actor) -> Dict[str, Any]:
        with self._op_lock, host_lock(self.paths):
            if not isinstance(app_id, str) or not ID_RE.match(app_id):
                raise AppsError(f"invalid app id {app_id!r}")
            rec = read_json(self.paths.proposal(app_id), default=None)
            if not isinstance(rec, dict):
                raise AppsError(f"no pending proposal for {app_id!r}")
            released = None
            if rec.get("kind") == "install":
                table = read_ports(self.paths)
                row = table.get(app_id)
                if row and row.get("state") == "proposed":
                    released = row["port"]
                    del table[app_id]
                    write_ports(self.paths, table)
            unlink_quiet(self.paths.proposal(app_id))
            self._audit(app_id, f"decline-{rec.get('kind')}", actor, "declined", port_released=released is not None)
        return {"ok": True, "app_id": app_id, "declined": rec.get("kind"), "port_released": released is not None}

    def _validate_values(self, m: Manifest, body: Dict[str, Any], base: Dict[str, Any],
                         require_paths: bool) -> Dict[str, Any]:
        """Validate host settings + path values + user values; returns the new settings. Raises AppsError."""
        new = json.loads(json.dumps(base)) if base else {}
        new.setdefault("run_at_boot", m.run_at_boot_default)
        new.setdefault("scope", m.scope)
        new.setdefault("paths", {})
        new.setdefault("user", {})
        errors: List[str] = []
        known = {"run_at_boot", "scope", "paths", "user"}
        for k in body:
            if k not in known:
                errors.append(f"{k}: unknown setting")
        if "run_at_boot" in body:
            if not isinstance(body["run_at_boot"], bool):
                errors.append("run_at_boot: must be true or false")
            else:
                new["run_at_boot"] = body["run_at_boot"]
        if "scope" in body:
            sc = body["scope"]
            if not isinstance(sc, str) or not (sc == "all" or WORKSPACE_RE.match(sc)):
                errors.append('scope: must be "all" or one named workspace')
            elif m.scope != "all" and sc != m.scope:
                errors.append(f"scope: refused: the manifest scopes this app to workspace {m.scope!r}; it cannot be "
                              f"moved to {sc!r} or widened (the manifest sets the ceiling)")
            else:
                new["scope"] = sc
        for name, v in (body.get("paths") or {}).items() if isinstance(body.get("paths", {}), dict) else []:
            pp = m.path(name)
            if pp is None:
                errors.append(f"paths.{name}: not a declared path parameter")
                continue
            if v in ("", None):
                new["paths"].pop(name, None)
                continue
            try:
                new["paths"][name] = validate_path_value(name, pp.kind, v)
            except ValueError as exc:
                errors.append(f"paths.{exc}")
        if "paths" in body and not isinstance(body["paths"], dict):
            errors.append("paths: must be an object")
        for name, v in (body.get("user") or {}).items() if isinstance(body.get("user", {}), dict) else []:
            s = m.setting(name)
            if s is None:
                errors.append(f"user.{name}: not a declared user_editable setting")
                continue
            try:
                new["user"][name] = validate_setting_value(s, v)
            except ValueError as exc:
                errors.append(f"user.{exc}")
        if "user" in body and not isinstance(body["user"], dict):
            errors.append("user: must be an object")
        if require_paths:
            for pp in m.paths:
                if not pp.optional and not new["paths"].get(pp.name):
                    errors.append(f"paths.{pp.name}: a value is required (the app's {pp.access} {pp.kind})")
        if errors:
            raise AppsError("refused: " + "; ".join(errors), errors=errors)
        return new

    def _check_boot_rule(self, m: Manifest, run_at_boot: bool) -> None:
        """Ruling R6: a requires mode start dependency of a run-at-boot app must itself be run-at-boot."""
        if not run_at_boot:
            return
        for r in m.requires:
            if r.mode != "start":
                continue
            ds = read_json(self.paths.settings(r.id), default={}) or {}
            if not ds.get("run_at_boot"):
                raise AppsError(f"refused: {m.id!r} is run-at-boot but its requires mode start dependency {r.id!r} "
                                "is not run-at-boot (ruling R6)", dependency=r.id)

    @staticmethod
    def _check_seen(seen: Any, require: bool, manifest_sha256: str, tree_sha256: str) -> None:
        """Round-2 F11: a confirm is bound to what the operator SAW. The screen shows (and the click echoes) the manifest's
        sha256 and the whole-tree hash; if either differs at click time the confirm is refused, so a change that
        landed between the screen and the click is never pinned unseen."""
        if seen is None and not require:
            return
        if (not isinstance(seen, dict) or seen.get("manifest_sha256") != manifest_sha256
                or seen.get("tree_sha256") != tree_sha256):
            raise AppsError("refused: what was shown on the screen is not what is on disk now (the manifest or the "
                            "app's files changed after the page loaded, or the confirm did not echo what was shown); "
                            "reload the app's page, read it again, and confirm again")

    def _proof_state(self, app_id: Any, op: str, action: Any) -> Optional[Dict[str, Any]]:
        """ONE read of everything the presence prompt and its digest are built from (delta audit D6): the pending
        proposal, whether the app is installed, the pin and the current settings file, and the ``kind`` the prompt
        will name, derived from THAT read. Caller holds the op lock."""
        if not isinstance(app_id, str) or not ID_RE.match(app_id):
            return None
        installed = app_id in self._installed()
        proposal = read_json(self.paths.proposal(app_id), default=None)
        kind: Optional[str]
        if op == "settings":
            kind = "change the settings of" if installed else None
        elif op == "action":
            kind = None
            if installed and isinstance(action, str):
                m, _mp, _s = self._load(app_id)
                if m is not None and m.action(action) is not None:
                    kind = f"run the action {action} of"
        elif isinstance(proposal, dict):
            kind = "uninstall" if proposal.get("kind") == "uninstall" else "install"
        else:
            kind = "re-confirm" if installed else None
        return {"op": op, "app": app_id, "installed": installed, "proposal": proposal, "kind": kind,
                "pin": read_json(self.paths.pin(app_id), default=None),
                "settings_now": read_json(self.paths.settings(app_id), default=None)}

    def proof_snapshot(self, app_id: Any, op: str, *, values: Any = None, seen: Any = None, settings: Any = None,
                       action: Any = None) -> Tuple[Optional[str], Optional[str]]:
        """``(what, digest)`` for a presence prompt, both from ONE locked read of the state (delta audit D6): the
        prompt names the act in ``what`` and the digest binds the very same snapshot plus the request's own payload
        (values / seen / settings / action name). ``what`` is None when there is nothing to confirm (the host will
        refuse the call itself; the operator is never prompted for a request that cannot take effect). The server prompts
        FROM this snapshot; ``confirm`` / ``save_settings`` / ``run_action`` re-read under the op lock and refuse on
        a difference, so what commits is what the prompt named and what was authenticated."""
        with self._op_lock:
            st = self._proof_state(app_id, op, action)
        if st is None:
            return None, None
        st = dict(st, values=values, seen=seen, settings_req=settings, action=action)
        raw = json.dumps(st, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
        return st["kind"], hashlib.sha256(raw).hexdigest()

    def act_digest(self, app_id: Any, op: str, *, values: Any = None, seen: Any = None, settings: Any = None,
                   action: Any = None) -> Optional[str]:
        """The digest half of :meth:`proof_snapshot` (spec 13 requirement-act-digest)."""
        return self.proof_snapshot(app_id, op, values=values, seen=seen, settings=settings, action=action)[1]

    def _check_digest(self, app_id: Any, verb: str, actor: Actor, expect: Optional[str], op: str, *,
                      waived: bool = False, **payload: Any) -> None:
        """Refuse (and audit) when the act differs from the one that was authenticated, or (delta2 F-1) when the call
        carries no digest at all although the state under the lock needs proof."""
        valid = isinstance(app_id, str) and bool(ID_RE.match(app_id))
        if expect is None:
            if not self.require_digest or waived or not valid:
                return
            st = self._proof_state(app_id, op, payload.get("action"))
            if st is None or st["kind"] is None:
                return                                   # nothing needs proof: the host refuses the call itself
            self._audit(app_id, verb, actor, "refused-no-digest", step="presence-digest")
            raise ActChanged(f"NOT done: {app_id!r} needs a human-presence check and this call carried none "
                             f"(a request landed after the page was read). Nothing was changed; confirm again")
        if self.act_digest(app_id, op, **payload) != expect:
            if valid:
                self._audit(app_id, verb, actor, "refused-act-changed", step="presence-digest")
            raise ActChanged(f"NOT done: the pending request for {app_id!r} changed while you were confirming "
                             f"(the check was for a different act). Nothing was changed; review it and confirm again")

    def proof_kind(self, app_id: Any, op: str, action: Any = None) -> Optional[str]:
        """What a confirm-proof prompt should say (the ``what`` half of :meth:`proof_snapshot`), or None."""
        return self.proof_snapshot(app_id, op, action=action)[0]

    def record_proof_refusal(self, app_id: Any, verb: str, actor: Actor, proof: Dict[str, Any]) -> None:
        """The presence check failed: nothing was committed; the refusal and its evidence go to the audit log."""
        if isinstance(app_id, str) and ID_RE.match(app_id):
            with self._op_lock:
                self._cur_proof = proof
                try:
                    self._audit(app_id, verb, actor, "refused-no-proof", step="presence")
                finally:
                    self._cur_proof = None

    def confirm(self, app_id: Any, actor: Actor, body: Optional[Dict[str, Any]] = None, *, seen: Any = None,
                require_seen: bool = False, proof: Optional[Dict[str, Any]] = None,
                expect_digest: Optional[str] = None, proof_waived: bool = False) -> Dict[str, Any]:
        """The operator's confirm (operator-only: reachable from the web page, never from an agent verb). ``seen`` is what
        the screen showed ({manifest_sha256, tree_sha256}); the web route requires it (``require_seen``)."""
        body = body or {}
        with self._op_lock, host_lock(self.paths):
            self._cur_proof = proof
            try:
                if not isinstance(app_id, str) or not ID_RE.match(app_id):
                    raise AppsError(f"invalid app id {app_id!r}")
                self._check_digest(app_id, "confirm", actor, expect_digest, "confirm", waived=proof_waived,
                                   values=body, seen=seen)
                rec = read_json(self.paths.proposal(app_id), default=None)
                if not isinstance(rec, dict):
                    if app_id in self._installed():
                        return self._reconfirm(app_id, actor, seen, require_seen)
                    raise AppsError(f"no pending proposal for {app_id!r}")
                if rec.get("kind") == "uninstall":
                    return self._confirm_uninstall(app_id, actor)
                return self._confirm_install(app_id, rec, actor, body, seen, require_seen)
            finally:
                self._cur_proof = None

    def _confirm_install(self, app_id: str, rec: Dict[str, Any], actor: Actor, body: Dict[str, Any],
                         seen: Any = None, require_seen: bool = False) -> Dict[str, Any]:
        step = "manifest"
        label = label_for(app_id)
        wrote: List[str] = []
        try:
            valid, why = self._proposal_valid(rec)
            if not valid:
                raise AppsError(f"the proposal is invalid and cannot be confirmed: {why}")
            m, raw = load_manifest(rec["manifest_path"])
            step = "values"
            settings = self._validate_values(m, body, {}, require_paths=True)
            _pins.check_install_rules(m, rec["manifest_path"], settings["paths"])      # argv + writes-outside-root
            step = "requires"
            self._check_requires_registration(m)
            self._check_boot_rule(m, bool(settings["run_at_boot"]))
            step = "port"
            port = None
            if m.has_port:
                row = read_ports(self.paths).get(app_id)
                if row is None:
                    raise AppsError("no port is reserved for this proposal")
                port = row["port"]
            step = "pin"
            pin = _pins.compute_pin(m, rec["manifest_path"], raw, settings)
            self._check_seen(seen, require_seen, pin["manifest_sha256"], pin["code"]["tree_sha256"])
            pin["pinned_at"] = utc_now_iso(self._clock())
            pin["pinned_by"] = actor.id
            step = "settings"
            atomic_write_json(self.paths.settings(app_id), settings)
            wrote.append("settings")
            atomic_write_json(self.paths.pin(app_id), pin)
            wrote.append("pin")
            _pins.write_snapshot(self.paths.pin_snapshot(app_id), pin, raw)
            os.makedirs(self.paths.logs(app_id), exist_ok=True)
            step = "agent"
            self.launchd.write_plist(label, build_plist(
                app_id, data_root=self.paths.root, run_at_boot=bool(settings["run_at_boot"]),
                log_path=self.paths.app_log(app_id), exit_timeout_s=m.stop_grace_s, python=self._python))
            wrote.append("plist")
            step = "register"
            data = read_registry(self.paths)
            data["apps"][app_id] = {"manifest": rec["manifest_path"]}
            write_registry(self.paths, data)
            wrote.append("registry")
            if port is not None:
                table = read_ports(self.paths)
                table[app_id] = {**table[app_id], "state": "installed"}
                write_ports(self.paths, table)
        except (AppsError, ManifestError, _pins.PinError, _rules.RuleViolation, OSError, ValueError) as exc:
            self._rollback_install(app_id, wrote)
            self._audit(app_id, "confirm-install", actor, "failed", step=step, error=str(exc))
            return {"ok": False, "app_id": app_id, "step": step, "installed": False,
                    "error": f"confirm failed at step {step!r}: {exc}"}
        except Exception as exc:  # noqa: BLE001 - e.g. a launchd adapter fault: still uninstalled + reported
            self._rollback_install(app_id, wrote)
            self._audit(app_id, "confirm-install", actor, "failed", step=step, error=f"{type(exc).__name__}: {exc}")
            return {"ok": False, "app_id": app_id, "step": step, "installed": False,
                    "error": f"confirm failed at step {step!r}: {type(exc).__name__}: {exc}"}
        unlink_quiet(self.paths.proposal(app_id))
        self._audit(app_id, "confirm-install", actor, "installed", manifest_sha256=pin["manifest_sha256"],
                    code=pin["code"].get("commit") or pin["code"].get("tree_sha256"))
        return {"ok": True, "app_id": app_id, "installed": True}

    def _rollback_install(self, app_id: str, wrote: List[str]) -> None:
        if "registry" in wrote:
            try:
                data = read_registry(self.paths)
                data["apps"].pop(app_id, None)
                write_registry(self.paths, data)
            except Exception:  # noqa: BLE001
                pass
        if "plist" in wrote:
            try:
                self.launchd.remove_plist(label_for(app_id))
            except Exception:  # noqa: BLE001
                pass
        for name in ("settings", "pin"):
            if name in wrote:
                unlink_quiet(getattr(self.paths, name)(app_id))

    def _reconfirm(self, app_id: str, actor: Actor, seen: Any = None, require_seen: bool = False) -> Dict[str, Any]:
        mpath = self._installed()[app_id]
        try:
            m, raw = load_manifest(mpath)
        except ManifestError as exc:
            return {"ok": False, "app_id": app_id, "error": f"the manifest no longer validates: {exc}"}
        if m.id != app_id:
            return {"ok": False, "app_id": app_id, "error": f"the manifest's id is now {m.id!r}: that is a different "
                                                            "app, never an upgrade; propose it as a new install"}
        settings = read_json(self.paths.settings(app_id), default={}) or {}
        old = read_json(self.paths.pin(app_id), default={}) or {}
        if old and _pins.verify(old, m, raw, settings, manifest_path=mpath) is None:
            return {"ok": False, "app_id": app_id, "error": "nothing to re-confirm: both pins hold"}
        try:
            pin = _pins.compute_pin(m, mpath, raw, settings)
        except (_pins.PinError, _rules.RuleViolation) as exc:
            self._audit(app_id, "reconfirm", actor, "failed", error=str(exc))
            return {"ok": False, "app_id": app_id, "step": "pin", "error": str(exc)}
        try:
            self._check_seen(seen, require_seen, pin["manifest_sha256"], pin["code"]["tree_sha256"])
        except AppsError as exc:
            self._audit(app_id, "reconfirm", actor, "failed", error=str(exc))
            return {"ok": False, "app_id": app_id, "step": "pin", "error": str(exc)}
        pin["pinned_at"] = utc_now_iso(self._clock())
        pin["pinned_by"] = actor.id
        atomic_write_json(self.paths.pin(app_id), pin)
        _pins.write_snapshot(self.paths.pin_snapshot(app_id), pin, raw)
        unlink_quiet(self.paths.preflight_record(app_id))
        self._audit(app_id, "reconfirm", actor, "re-pinned", manifest_sha256=pin["manifest_sha256"])
        return {"ok": True, "app_id": app_id, "reconfirmed": True}

    def _confirm_uninstall(self, app_id: str, actor: Actor) -> Dict[str, Any]:
        label = label_for(app_id)
        self._stop_process(app_id, None)
        try:
            self.launchd.bootout(label)
        except Exception as exc:  # noqa: BLE001
            self._audit(app_id, "confirm-uninstall", actor, "failed", step="agent", error=str(exc))
            return {"ok": False, "app_id": app_id, "step": "agent", "error": f"bootout failed: {exc}"}
        self.launchd.remove_plist(label)
        data = read_registry(self.paths)
        data["apps"].pop(app_id, None)
        write_registry(self.paths, data)
        table = read_ports(self.paths)
        released = table.pop(app_id, None)
        write_ports(self.paths, table)
        unlink_quiet(self.paths.proposal(app_id))
        self._audit(app_id, "confirm-uninstall", actor, "uninstalled", port_released=bool(released))
        archive = archive_app_dir(self.paths, app_id, self._clock())
        return {"ok": True, "app_id": app_id, "uninstalled": True, "archived_to": archive,
                "port_released": bool(released)}

    # ------------------------------------------------------------------------------------------------
    # settings, backups, actions
    # ------------------------------------------------------------------------------------------------
    def save_settings(self, app_id: Any, body: Dict[str, Any], actor: Actor, *,
                      proof: Optional[Dict[str, Any]] = None, expect_digest: Optional[str] = None,
                      proof_waived: bool = False) -> Dict[str, Any]:
        with self._op_lock:
            self._cur_proof = proof
            try:
                self._check_digest(app_id, "settings", actor, expect_digest, "settings", waived=proof_waived,
                                   settings=body)
                return self._save_settings(app_id, body, actor)
            finally:
                self._cur_proof = None

    def _save_settings(self, app_id: Any, body: Dict[str, Any], actor: Actor) -> Dict[str, Any]:
        with self._op_lock:           # re-entrant (RLock) or no-op: see __init__
            self._require_installed(app_id)
            m, _mp, settings = self._load(app_id)
            if m is None:
                raise AppsError(f"{app_id}: the manifest does not load; re-confirm before editing settings")
            if not isinstance(body, dict):
                raise AppsError("settings must be an object")
            new = self._validate_values(m, body, settings, require_paths=False)
            pin0 = read_json(self.paths.pin(app_id), default=None)
            root0 = ((pin0 or {}).get("code") or {}).get("root") if isinstance(pin0, dict) else None
            if root0:
                why = _rules.check_writes_outside_root(m, new["paths"], root0)
                if why:
                    raise AppsError("refused: " + why)
            if new.get("run_at_boot") and not settings.get("run_at_boot"):
                self._check_boot_rule(m, True)
            atomic_write_json(self.paths.settings(app_id), new)
            if new.get("run_at_boot") != settings.get("run_at_boot"):
                self.launchd.write_plist(label_for(app_id), build_plist(
                    app_id, data_root=self.paths.root, run_at_boot=bool(new["run_at_boot"]),
                    log_path=self.paths.app_log(app_id), exit_timeout_s=m.stop_grace_s, python=self._python))
            changed = sorted(k for k in ("run_at_boot", "scope") if new.get(k) != settings.get(k))
            changed += [f"paths.{k}" for k in sorted(set(new["paths"]) | set(settings.get("paths", {})))
                        if new["paths"].get(k) != (settings.get("paths") or {}).get(k)]
            changed += [f"user.{k}" for k in sorted(set(new["user"]) | set(settings.get("user", {})))
                        if new["user"].get(k) != (settings.get("user") or {}).get(k)]
            self._audit(app_id, "settings", actor, "saved", changed=changed)
            return {"ok": True, "app_id": app_id, "changed": changed}

    def record_backup(self, app_id: Any, name: Any, backup_path: Any, actor: Actor) -> Dict[str, Any]:
        with self._op_lock:
            self._require_installed(app_id)
            m, _mp, _s = self._load(app_id)
            if m is None or name not in m.backup_before_first_start:
                raise AppsError(f"{name!r} is not a backup_before_first_start path of {app_id!r}")
            if not isinstance(backup_path, str) or not os.path.isabs(os.path.expanduser(backup_path)):
                raise AppsError("the backup location must be an absolute path")
            bp = os.path.expanduser(backup_path)
            if not os.path.exists(bp):
                raise AppsError(f"no backup exists at {bp}: a backup must exist before it is recorded")
            rec = read_json(self.paths.backups(app_id), default={}) or {}
            rec[name] = {"recorded_at": utc_now_iso(self._clock()), "backup_path": bp, "recorded_by": actor.id}
            atomic_write_json(self.paths.backups(app_id), rec)
            self._audit(app_id, "record-backup", actor, "recorded", name=name, backup_path=bp)
            return {"ok": True, "app_id": app_id, "name": name}

    @_proved
    def run_action(self, app_id: Any, name: Any, actor: Actor, *, confirmed: bool,
                   expect_digest: Optional[str] = None, proof_waived: bool = False) -> Dict[str, Any]:
        with self._op_lock:
            self._require_installed(app_id)
            self._check_digest(app_id, f"action:{name}", actor, expect_digest, "action", waived=proof_waived,
                               action=name)
            if confirmed is not True:
                raise AppsError("an action runs only after the operator confirms it on the detail page")
            m, _mp, _s = self._load(app_id)
            if m is None:
                raise AppsError(f"{app_id}: the manifest does not load; re-confirm first")
            a = m.action(name) if isinstance(name, str) else None
            if a is None:
                raise AppsError(f"{app_id!r} declares no action {name!r}")
            alive, _st = self._alive(app_id)
            if a.requires_stopped and alive:
                self._audit(app_id, f"action:{a.name}", actor, "refused", error="app is running")
                return {"ok": False, "app_id": app_id, "action": a.name,
                        "error": f"action {a.name!r} requires the app to be stopped; stop it first"}
            try:
                prep = _pf.check_all(self.paths, app_id, self._deps(actor, (app_id,)))
                argv = _pf.exec_argv(prep, "action", a.name)
            except _pf.Refusal as r:
                self._audit(app_id, f"action:{a.name}", actor, f"refused({r.reason})", detail=r.detail)
                return {"ok": False, "app_id": app_id, "action": a.name, "reason": r.reason,
                        "error": f"refused({r.reason}): {r.detail}"}
            env = _pf.build_env(self.paths, prep, dict(os.environ))       # the CLOSED env (rule 4)
            os.makedirs(self.paths.logs(app_id), exist_ok=True)
            stamp = _dt.datetime.fromtimestamp(self._clock(), tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log_path = os.path.join(self.paths.logs(app_id), f"action-{a.name}-{stamp}.log")
            judged = argv.judged                                           # R3E-F1: exec the path that was judged
            try:
                log_fd = _fsafe.open_append_fd(log_path)                   # R3E-F2: non-blocking, regular files only
            except OSError as exc:
                self._audit(app_id, f"action:{a.name}", actor, "failed", error=f"log not writable: {exc}")
                return {"ok": False, "app_id": app_id, "action": a.name,
                        "error": f"could not open the action log {log_path}: {exc}"}
            with os.fdopen(log_fd, "ab") as log:
                try:
                    argv.verify()                                         # identities unchanged since the judgement
                    proc = subprocess.Popen(list(argv), executable=judged.exec_target(), cwd=prep.cwd, env=env,
                                            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                            start_new_session=True)
                except _rules.RuleViolation as exc:
                    self._audit(app_id, f"action:{a.name}", actor, "refused(code-changed)", detail=exc.message)
                    return {"ok": False, "app_id": app_id, "action": a.name, "reason": "code-changed",
                            "error": f"refused(code-changed): {exc.message}"}
                except OSError as exc:
                    self._audit(app_id, f"action:{a.name}", actor, "failed", error=str(exc))
                    return {"ok": False, "app_id": app_id, "action": a.name, "error": f"could not run: {exc}"}
            self._audit(app_id, f"action:{a.name}", actor, "started", pid=proc.pid, log=log_path)

            def _reap() -> None:
                rc = proc.wait()
                self._audit(app_id, f"action:{a.name}", actor, "finished", exit_code=rc)

            t = threading.Thread(target=_reap, name=f"apps-action-{app_id}-{a.name}", daemon=True)
            t.start()
            self.action_threads.append(t)
            return {"ok": True, "app_id": app_id, "action": a.name, "pid": proc.pid, "log": log_path}

    # ------------------------------------------------------------------------------------------------
    # the detail page model
    # ------------------------------------------------------------------------------------------------
    def _last(self, app_id: str, verb: str) -> Optional[Dict[str, Any]]:
        for e in reversed(read_audit(self.paths, app_id)):
            if e.get("verb") == verb:
                return {"ts": e.get("ts"), "actor": e.get("actor"), "actor_verified": e.get("actor_verified"),
                        "outcome": e.get("outcome")}
        return None

    @staticmethod
    def ui_url(m: Manifest, port: Any) -> Optional[str]:
        """The app's UI address, built in ONE place for both the Open action and the detail page so the two cannot
        disagree. Never carries the ui_token (Open appends that to the browser URL only). None for a headless app
        or an app with no port."""
        if m is None or m.headless or port is None:
            return None
        return f"http://127.0.0.1:{port}{m.ui_path}"

    @staticmethod
    def manifest_view(m: Manifest) -> Dict[str, Any]:
        return {
            "id": m.id, "name": m.name, "version": m.version, "purpose": m.purpose, "owner": m.owner,
            "schema_version": m.schema_version, "scope": m.scope, "run_at_boot_default": m.run_at_boot_default,
            "start": list(m.start), "port_arg": list(m.port_arg) if m.port_arg else None,
            "health_path": m.health_path, "activity_path": m.activity_path,
            "ui_path": m.ui_path, "headless": m.headless,
            "paths": [{"name": p.name, "description": p.description, "kind": p.kind, "access": p.access,
                       "optional": p.optional, "flag": p.flag} for p in m.paths],
            "backup_before_first_start": list(m.backup_before_first_start),
            "requires": [{"id": r.id, "mode": r.mode} for r in m.requires],
            "requires_files": list(m.requires_files),
            "licences": [{"name": l.name, "path_param": l.path_param} for l in m.licences],
            "hardware": ({"device_class": m.hardware.device_class, "presence_check": list(m.hardware.presence_check)}
                         if m.hardware else None),
            "user_editable": [{"name": s.name, "type": s.type, "default": s.default, "description": s.description,
                               "options": list(s.options), "kind": s.kind} for s in m.user_editable],
            "env": {k: v for k, v in m.env},
            "stop": {"signal": m.stop_signal, "grace": m.stop_grace_s},
            "actions": [{"name": a.name, "argv": list(a.argv), "requires_stopped": a.requires_stopped,
                         "description": a.description} for a in m.actions],
            "ui_token": "open-only" if m.ui_token else None,
        }

    def detail(self, app_id: Any) -> Dict[str, Any]:
        if not isinstance(app_id, str) or not ID_RE.match(app_id):
            raise AppsError(f"invalid app id {app_id!r}")
        rec = read_json(self.paths.proposal(app_id), default=None)
        installed = app_id in self._installed()
        if isinstance(rec, dict) and rec.get("kind") == "install" and not installed:
            return self._proposal_detail(app_id, rec)
        if not installed:
            raise AppsError(f"no installed app or pending proposal {app_id!r}")
        m, mpath, settings = self._load(app_id)
        st = self.status(app_id)
        out: Dict[str, Any] = {"ok": True, "app_id": app_id, "installed": True, "manifest_path": mpath,
                               "status": st, "port": self._port(app_id),
                               "last_start": self._last(app_id, "start"), "last_stop": self._last(app_id, "stop"),
                               "pending_uninstall": isinstance(rec, dict) and rec.get("kind") == "uninstall",
                               "uninstall_proposal": rec if isinstance(rec, dict) and rec.get("kind") == "uninstall" else None}
        raw_b: Optional[bytes] = None
        try:
            raw_b = read_manifest_bytes(mpath)
            out["manifest_raw"] = raw_b.decode("utf-8", errors="replace")
        except ManifestError as exc:
            out["manifest_raw"] = None
            out["manifest_error"] = exc.message
        if m is None:
            out["manifest"] = None
            out["manifest_error"] = out.get("manifest_error") or "the manifest no longer validates"
        else:
            out["manifest"] = self.manifest_view(m)
            out["ui_url"] = self.ui_url(m, out["port"])
            vals = settings.get("paths") or {}
            out["settings"] = {
                "run_at_boot": bool(settings.get("run_at_boot")), "scope": settings.get("scope", m.scope),
                "scope_ceiling": m.scope,
                "paths": [{"name": p.name, "value": vals.get(p.name), "access": p.access, "kind": p.kind,
                           "optional": p.optional, "flag": p.flag, "description": p.description} for p in m.paths],
                "user": [{"name": s.name, "type": s.type, "value": (settings.get("user") or {}).get(s.name, s.default),
                          "default": s.default, "options": list(s.options), "kind": s.kind,
                          "description": s.description} for s in m.user_editable],
            }
            lic = []
            for l in m.licences:
                pth = vals.get(l.path_param)
                row: Dict[str, Any] = {"name": l.name}
                try:
                    info = _licence.read_licence(pth) if pth else None
                    if info is None:
                        row.update(state="unset", message="licence path not set")
                    else:
                        verdict, msg = _licence.assess(info, self._today())
                        row.update(state=verdict, message=msg)
                except _licence.LicenceUnreadable as exc:
                    row.update(state="unreadable", message=str(exc))
                lic.append(row)
            out["licences"] = lic
            out["backups"] = read_json(self.paths.backups(app_id), default={}) or {}
        pin = read_json(self.paths.pin(app_id), default=None)
        if isinstance(pin, dict) and st.get("reason") in ("manifest-changed", "manifest-moved", "code-changed"):
            out["change"] = _pins.describe_change(pin, self.paths.pin_snapshot(app_id), mpath, m, settings)
            code = (out["change"] or {}).get("code") or {}
            try:
                out["seen"] = ({"manifest_sha256": _pins.sha256_bytes(raw_b), "tree_sha256": code["tree_sha256"]}
                               if raw_b is not None and "tree_sha256" in code else None)
            except KeyError:
                out["seen"] = None
        return out

    def _proposal_detail(self, app_id: str, rec: Dict[str, Any]) -> Dict[str, Any]:
        valid, why = self._proposal_valid(rec)
        out: Dict[str, Any] = {"ok": True, "app_id": app_id, "installed": False, "proposal": {
            k: rec.get(k) for k in ("kind", "proposed_by", "proposed_by_verified", "proposed_at", "manifest_path")},
            "valid": valid, "invalid_reason": why}
        # NOTE: the approval screen never shows the port (architect reading)
        try:
            m, raw = load_manifest(rec["manifest_path"])
            out["manifest"] = self.manifest_view(m)
            out["manifest_raw"] = raw.decode("utf-8", errors="replace")
            out["to_fill"] = [{"name": p.name, "description": p.description, "kind": p.kind, "access": p.access,
                               "optional": p.optional, "flag": p.flag} for p in m.paths]
            try:   # what this screen shows, echoed by the confirm click (round-2 F11)
                root = _pins.app_root(m, rec["manifest_path"])
                out["seen"] = {"manifest_sha256": _pins.sha256_bytes(raw),
                               "tree_sha256": _pins._digest(_pins.walk_root(root))}
            except _pins.PinError as exc:
                out["seen"] = None
                out["seen_error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            out["manifest"] = None
            out["manifest_error"] = str(exc)
        return out

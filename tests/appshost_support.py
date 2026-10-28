"""Test support for the Apps host: the launchd DOUBLE, a fixture app, manifest builders.

g31: nothing here touches ~/Library/LaunchAgents or launchctl. :class:`FakeLaunchd` keeps plists in memory and
runs an agent's ProgramArguments (the REAL preflight wrapper, ``python -m gnomon.appshost.launch``) as a
plain child process, so the wrapper's preflight, its exec, the ui_token mint and the app's argv/env are all real.
The spawned env carries ``PYTEST_CURRENT_TEST`` so any :class:`RealLaunchd` call inside the wrapper refuses.
Every fixture lives under pytest's tmp_path; no real app, no real data.
"""

from __future__ import annotations

import copy
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import time

from typing import Any, Dict, List, Optional, Set

from gnomon.appshost.launchd import AgentState
from gnomon.config import DEFAULT_RUNTIME_ROOT
from gnomon.feature_events import VIEWER_ACTOR

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
#: the interpreter every fixture manifest names: absolute, and (g73-amendment-4 rule 2) OUTSIDE the user's home.
#: realpath: a venv's python is a symlink to the base interpreter, which is a system/framework path.
PYTHON = os.path.realpath(sys.executable)

FIXTURE_APP = textwrap.dedent(r'''
    import argparse, json, os, signal, sys, time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs

    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="serve")
    ap.add_argument("--evidence", default=os.environ.get("APP_FIXTURE_EVIDENCE"))
    ap.add_argument("--port", type=int)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--healthy-file")
    ns, rest = ap.parse_known_args()
    ev = {"argv": sys.argv, "rest": rest, "pid": os.getpid(), "cwd": os.getcwd(),
          "token": os.environ.get("GNOMON_APP_UI_TOKEN"), "path_access": os.environ.get("GNOMON_APP_PATH_ACCESS"),
          "app_id": os.environ.get("GNOMON_APP_ID"), "env_extra": os.environ.get("APP_FIXTURE_ENV"),
          "pycache_prefix": os.environ.get("PYTHONPYCACHEPREFIX"), "env": dict(os.environ),
          "flags": {"isolated": sys.flags.isolated, "dont_write_bytecode": sys.flags.dont_write_bytecode,
                    "no_user_site": sys.flags.no_user_site, "ignore_environment": sys.flags.ignore_environment},
          "executable": sys.executable}
    with open(ns.evidence, "a") as fh:
        fh.write(json.dumps(ev) + "\n")
    sys.stdout.write("fixture app started mode=%s\n" % ns.mode); sys.stdout.flush()
    if ns.mode == "exit0":
        sys.exit(0)
    if ns.mode == "leak":
        # a log full of secret-shaped strings (round 3, rule 5): the host must redact them by PATTERN
        sys.stdout.write("FIXTURE-LEAK start\n")
        t = os.environ.get("GNOMON_APP_UI_TOKEN") or ""
        sys.stdout.write("url http://127.0.0.1:1/ui?gnomon_ui_token=%s tok=%s\n" % (t, t))
        sys.stdout.write("bare %s\n" % t)
        for l in os.environ.get("APP_FIXTURE_LEAK", "").split("|"):
            sys.stdout.write(l + "\n")
        sys.stdout.flush()
        sys.exit(3)
    if ns.mode == "exit1":
        sys.stdout.write("FIXTURE-FAIL-LINE: something broke\n"); sys.stdout.flush()
        sys.exit(1)
    if ns.mode == "ignore_term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if ns.mode in ("sleep", "ignore_term") or ns.port is None:
        while True:
            time.sleep(0.2)
    token = os.environ.get("GNOMON_APP_UI_TOKEN")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def do_GET(self):
            u = urlparse(self.path)
            if u.path == "/health":
                ok = True if not ns.healthy_file else os.path.exists(ns.healthy_file)
                self.send_response(200 if ok else 503); self.end_headers(); self.wfile.write(b"ok" if ok else b"no")
                return
            if u.path == "/activity":
                # X72: serve the JSON file named by APP_FIXTURE_ACTIVITY (the test writes what the app "reports")
                f = os.environ.get("APP_FIXTURE_ACTIVITY")
                try:
                    body = open(f, "rb").read()
                except Exception:
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers()
                self.wfile.write(body)
                return
            if u.path.startswith("/ui"):
                q = parse_qs(u.query).get("gnomon_ui_token", [None])[0]
                good = (token is None) or (q == token)
                self.send_response(200 if good else 403); self.end_headers(); return
            self.send_response(404); self.end_headers()

    HTTPServer((ns.bind, ns.port), H).serve_forever()
''')


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def free_range(n: int = 6) -> int:
    """The low end of ``n`` consecutive free loopback ports. Random start (Diamond F13: two concurrent suites
    picked the same deterministic range)."""
    import random

    start = random.randrange(20000, 55000)
    for base in list(range(start, 60000, 37)) + list(range(20000, start, 37)):
        ok = True
        for p in range(base, base + n):
            s = socket.socket()
            try:
                s.bind(("127.0.0.1", p))
            except OSError:
                ok = False
            finally:
                s.close()
            if not ok:
                break
        if ok:
            return base
    raise RuntimeError("no free port range")


class FakeLaunchd:
    """An in-memory launchd. ``calls`` records every operation (label + verb) in order."""

    def __init__(self, home: str) -> None:
        self.home = home
        self.plists: Dict[str, Dict[str, Any]] = {}
        self.loaded: Set[str] = set()
        self.procs: Dict[str, subprocess.Popen] = {}
        self.calls: List[tuple] = []
        self.fail_on: Set[str] = set()
        #: EVERY process ever spawned (Diamond F13: cleanup kills them all, not just the latest per label)
        self.all_procs: List[subprocess.Popen] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise RuntimeError(f"injected launchd failure on {op}")

    def write_plist(self, label: str, plist: Dict[str, Any]) -> None:
        self._maybe_fail("write_plist")
        self.calls.append(("write_plist", label))
        self.plists[label] = copy.deepcopy(plist)

    def remove_plist(self, label: str) -> None:
        self.calls.append(("remove_plist", label))
        self.plists.pop(label, None)

    def read_plist(self, label: str) -> Optional[Dict[str, Any]]:
        return copy.deepcopy(self.plists.get(label))

    def bootstrap(self, label: str) -> None:
        self._maybe_fail("bootstrap")
        self.calls.append(("bootstrap", label))
        assert label in self.plists, f"bootstrap of {label} with no plist"
        if label in self.loaded:
            return
        self.loaded.add(label)
        if self.plists[label].get("RunAtLoad"):
            self._spawn(label)

    def bootout(self, label: str) -> None:
        self.calls.append(("bootout", label))
        p = self.procs.get(label)
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait(5)
        self.loaded.discard(label)
        self.procs.pop(label, None)

    def kickstart(self, label: str) -> None:
        self.calls.append(("kickstart", label))
        assert label in self.loaded, f"kickstart of unloaded {label}"
        p = self.procs.get(label)
        if p is not None and p.poll() is None:
            return
        self._spawn(label)

    def kill(self, label: str, signal_name: str) -> None:
        self.calls.append(("kill", label, signal_name))
        p = self.procs.get(label)
        if p is not None and p.poll() is None:
            p.send_signal(signal.Signals[signal_name])

    def state(self, label: str) -> AgentState:
        if label not in self.loaded:
            return AgentState(loaded=False)
        p = self.procs.get(label)
        if p is None:
            return AgentState(loaded=True)
        rc = p.poll()
        if rc is None:
            return AgentState(loaded=True, pid=p.pid)
        return AgentState(loaded=True, pid=None, last_exit=rc)

    def labels(self) -> Set[str]:
        return set(self.loaded)

    def _spawn(self, label: str) -> None:
        pl = self.plists[label]
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": self.home, "PYTEST_CURRENT_TEST": "fake-launchd"}
        env.update(pl.get("EnvironmentVariables") or {})
        log = pl["StandardOutPath"]
        os.makedirs(os.path.dirname(log), exist_ok=True)
        with open(log, "ab") as fh:
            self.procs[label] = subprocess.Popen(pl["ProgramArguments"], env=env, stdout=fh, stderr=subprocess.STDOUT,
                                                 stdin=subprocess.DEVNULL, start_new_session=True)
        self.all_procs.append(self.procs[label])

    def cleanup(self) -> None:
        for p in list(self.procs.values()) + self.all_procs:
            if p.poll() is None:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except OSError:
                    p.kill()
                try:
                    p.wait(5)
                except subprocess.TimeoutExpired:
                    pass


class Env:
    """One test's world: a HOME, a DATA root, an app directory, the fake launchd, a host."""

    def __init__(self, tmp_path, *, port_range=None, services=None, probe=None, opener=None, binding_on=False,
                 clock=None, monotonic=None, sleep=None, today=None, requires_budget=None) -> None:
        from gnomon.appshost.host import AppsHost

        self.tmp = str(tmp_path)
        self.home = os.path.join(self.tmp, "home")
        self.data = os.path.join(self.home, os.path.basename(DEFAULT_RUNTIME_ROOT))
        self.apps_dir = os.path.join(self.tmp, "apps-src")
        os.makedirs(self.data, exist_ok=True)
        os.makedirs(self.apps_dir, exist_ok=True)
        self.launchd = FakeLaunchd(self.home)
        self.opened: List[str] = []
        self.services = dict(services or {})
        self.write_machine_config()
        kw = {}
        if clock is not None:
            kw["clock"] = clock
        if monotonic is not None:
            kw["monotonic"] = monotonic
        if sleep is not None:
            kw["sleep"] = sleep
        if today is not None:
            kw["today"] = today
        if requires_budget is not None:
            kw["requires_start_budget_s"] = requires_budget
        self.host = AppsHost(self.data, self.launchd, probe=probe, opener=opener or self.opened.append,
                             services=lambda: dict(self.services), port_range=(lambda: port_range),
                             source_roots=(ROOT,), binding_on=lambda: binding_on, start_settle_s=3.0,
                             open_wait_s=5.0, require_digest=False, **kw)

    def write_machine_config(self) -> None:
        """The wrapper subprocess reads the machine config (services) under its HOME."""
        cfg = {"runtime_root": self.data}
        if self.services:
            cfg["services"] = {sid: {"host": "local", "mechanism": {"launchd": f"com.example.{sid}"},
                                     "owner": "test", "health_url": url} for sid, url in self.services.items()}
        with open(os.path.join(self.data, "config.json"), "w") as fh:
            json.dump(cfg, fh)

    # -- fixture apps ----------------------------------------------------------------------------------
    def app_dir(self, name: str) -> str:
        d = os.path.join(self.apps_dir, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "app.py"), "w") as fh:
            fh.write(FIXTURE_APP)
        return d

    def manifest(self, app_id: str, *, mode: str = "serve", port: Optional[int] = None, with_port: bool = True,
                 extra_start: Optional[List[str]] = None, dirname: Optional[str] = None, **fields: Any) -> str:
        d = self.app_dir(dirname or app_id)
        self.evidence_path(app_id)
        m: Dict[str, Any] = {
            "schema_version": 1, "id": app_id, "name": f"Fixture {app_id}", "version": "1.0.0",
            "purpose": "a fixture app for the Apps host tests", "owner": "test",
            "start": [PYTHON, "app.py", "--mode", mode] + (extra_start or []),
            # the evidence file is app OUTPUT: passed in env (APP_-prefixed: the only names a manifest may set),
            # never as a literal argv path (which the code pin would treat as a file the start names)
            "env": {"APP_FIXTURE_EVIDENCE": self.evidence_path(app_id)},
        }
        if with_port:
            m["port_arg"] = "--port {port}"
            m["health_path"] = "/health"
            m["preferred_port"] = port if port is not None else free_port()
        if "env" in fields:
            m["env"] = {**m["env"], **fields.pop("env")}
        m.update(fields)
        p = os.path.join(d, "gnomon-app.json")
        with open(p, "w") as fh:
            json.dump(m, fh, indent=2)
        return p

    def evidence_path(self, app_id: str) -> str:
        return os.path.join(self.tmp, f"evidence-{app_id}.jsonl")

    def evidence(self, app_id: str) -> List[Dict[str, Any]]:
        try:
            with open(self.evidence_path(app_id)) as fh:
                return [json.loads(l) for l in fh if l.strip()]
        except FileNotFoundError:
            return []

    def install(self, manifest_path: str, values: Optional[Dict[str, Any]] = None, actor=None) -> str:
        from gnomon.appshost.host import Actor

        a = actor or Actor(VIEWER_ACTOR, False)
        r = self.host.propose_install(manifest_path, Actor("agent-x", False))
        assert r["ok"], r
        c = self.host.confirm(r["app_id"], a, values or {})
        assert c["ok"], c
        return r["app_id"]

    def wait_status(self, app_id: str, want, timeout: float = 15.0) -> Dict[str, Any]:
        want = (want,) if isinstance(want, str) else tuple(want)
        deadline = time.monotonic() + timeout
        s = None
        while time.monotonic() < deadline:
            s = self.host.status(app_id)
            if s["status"] in want:
                return s
            time.sleep(0.1)
        raise AssertionError(f"{app_id}: status never became {want}; last {s}")

    def wait_evidence(self, app_id: str, n: int = 1, timeout: float = 15.0) -> List[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            ev = self.evidence(app_id)
            if len(ev) >= n:
                return ev
            time.sleep(0.05)
        raise AssertionError(f"{app_id}: no evidence (log: {self.log(app_id)})")

    def log(self, app_id: str) -> str:
        try:
            with open(os.path.join(self.data, "apps", app_id, "logs", "app.log")) as fh:
                return fh.read()
        except FileNotFoundError:
            return ""

    def audit(self, app_id: str) -> List[Dict[str, Any]]:
        from gnomon.appshost.state import read_audit

        return read_audit(self.host.paths, app_id)

    def cleanup(self) -> None:
        self.launchd.cleanup()


class ForeignListener:
    """A process Gnomon did not start, listening on a loopback port."""

    def __init__(self, port: int) -> None:
        code = ("import socket,time\n"
                f"s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',{port}));"
                "s.listen(5);print('up',flush=True)\nwhile True: time.sleep(1)\n")
        self.proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.proc.stdout.readline()
        self.pid = self.proc.pid

    def close(self) -> None:
        self.proc.kill()
        self.proc.wait(5)


class FakeProbe:
    """Wrap the real probe and inject listeners for given pids (the non-loopback case)."""

    def __init__(self, real, inject=None) -> None:
        self.real = real
        self.inject = inject   # callable(pids) -> list[Listener]

    def listeners_on_port(self, port):
        return self.real.listeners_on_port(port)

    def listeners_of(self, pids):
        extra = self.inject(set(pids)) if self.inject else []
        return list(self.real.listeners_of(pids)) + list(extra)

"""run ``tools/golive_smoke.py`` automatically on every :8770 start.

The web server's lifespan (``web/backend/server.py`` — the real :8770 entrypoint,
which co-hosts the hub in-process) builds ONE :class:`GoliveMonitor` per process
start and calls :meth:`GoliveMonitor.schedule` with ~20 s delay, so relaunched
harnesses have time to re-register before the smoke looks for them.

Guarantees:

* **Never blocks startup** — the run is on a daemon thread; ``schedule`` returns
  at once. Server shutdown :meth:`cancel`\\ s a still-pending run AND kills an
  in-flight smoke subprocess by its own recorded pid (never by pattern), then joins
  the thread with a short timeout.
* **Never takes the server down** — the smoke runs as a SUBPROCESS (a crash, hang
  or even a segfault in it is contained; a hang is bounded by a timeout) and every
  failure path in this module becomes a recorded ``FAIL`` line, never an exception.
* **Loud** — each run logs a count line plus one line per FAIL to the daemon log
  (stdout, ``[gm-web] golive ...``) and is exposed at ``GET /api/golive`` for the
  header chip. A crashed/timed-out/unparsable smoke is a FAIL, not an empty PASS.
* **Read-only** — the smoke itself never writes (see its docstring); the subprocess
  runs with ``-B`` + ``PYTHONDONTWRITEBYTECODE=1`` so not even a ``.pyc`` lands. The
  ONLY write is this module's own result file,
  ``<run_base>/rt-<key>/golive.json`` (0600, dir 0700, atomic) — beside the hub's
  ``rendezvous.json``, via the same run-dir helpers, machine-local (never synced).

stdlib only (+ the hub's rendezvous helpers).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .hub import rendezvous as _rv

#: The roadmap-cited flag (gnomon: ``flag:``). Read with an explicit
#: ``default=True`` by the server (see ``_golive_enabled``).
FLAG = "auto_golive_smoke"
RESULT_FILENAME = "golive.json"
DEFAULT_DELAY_S = 20.0
SMOKE_TIMEOUT_S = 180.0

SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SMOKE_PATH = os.path.join(SRC_ROOT, "tools", "golive_smoke.py")

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

Runner = Callable[[int, str, str, float], Tuple[int, str, str]]


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def result_path(runtime_root: str) -> str:
    """``golive.json`` in the SAME per-runtime-root run dir as ``rendezvous.json``."""
    return os.path.join(os.path.dirname(_rv.rendezvous_path(runtime_root)), RESULT_FILENAME)


def default_runner(port: int, runtime_root: str, run_base: str, timeout: float, *,
                   on_spawn: Optional[Callable[[Optional["subprocess.Popen"]], None]] = None,
                   argv: Optional[List[str]] = None) -> Tuple[int, str, str]:
    """Run the real smoke as a subprocess (``--json``); returns (exit, stdout, stderr).

    ``on_spawn(proc)`` is called with the child right after it starts and with
    ``None`` once it has been reaped, so the monitor can kill THIS child — by its own
    pid, never by a pattern — on server shutdown. ``argv`` is a test seam.
    A timeout kills the child and raises ``subprocess.TimeoutExpired`` — the caller
    records it as a FAIL."""
    env = dict(os.environ)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    argv = list(argv) if argv else [
        sys.executable, "-B", SMOKE_PATH, "--json", "--port", str(int(port)),
        "--runtime-root", str(runtime_root), "--run-base", str(run_base)]
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env=env, cwd=SRC_ROOT, stdin=subprocess.DEVNULL)
    if on_spawn is not None:
        on_spawn(proc)
    try:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            raise
    finally:
        if on_spawn is not None:
            on_spawn(None)
    return proc.returncode, out or "", err or ""


def parse_output(code: Optional[int], stdout: str, stderr: str) -> List[Dict[str, str]]:
    """The smoke's ``--json`` lines -> checks. Anything that is not a check line, and
    any disagreement between the exit code and the parsed lines, becomes a FAIL."""
    checks: List[Dict[str, str]] = []
    junk: List[str] = []
    for raw in (stdout or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            junk.append(line)
            continue
        if (isinstance(obj, dict) and isinstance(obj.get("check"), str)
                and obj.get("status") in (PASS, FAIL, SKIP)):
            checks.append({"check": obj["check"], "status": obj["status"],
                           "evidence": str(obj.get("evidence", ""))})
        else:
            junk.append(line)
    tail = (stderr or "").strip().splitlines()[-3:]
    if junk:
        checks.append({"check": "smoke.output", "status": FAIL,
                       "evidence": "unparsable smoke output: " + " | ".join(junk)[:400]})
    if code not in (0, 1):
        checks.append({"check": "smoke.exit", "status": FAIL,
                       "evidence": f"smoke exited {code} (internal error)"
                                   + (": " + " | ".join(tail)[:400] if tail else "")})
    elif code == 1 and not any(c["status"] == FAIL for c in checks):
        checks.append({"check": "smoke.exit", "status": FAIL,
                       "evidence": "smoke exited 1 (something FAILed) but no FAIL line was parsed"})
    if not checks:
        checks.append({"check": "smoke.output", "status": FAIL,
                       "evidence": f"smoke produced no checks (exit {code})"})
    elif not any(c["status"] in (PASS, FAIL) for c in checks):
        # rm1a2: an all-SKIP run verified nothing — it must never read as a PASS.
        checks.append({"check": "smoke.no-pass", "status": FAIL,
                       "evidence": f"smoke produced {len(checks)} check(s), all SKIP, 0 PASS "
                                   "(nothing was verified)"})
    return checks


def _accepts_on_spawn(runner: Callable[..., Any]) -> bool:
    """Whether ``runner`` takes the ``on_spawn`` keyword (the default runner and any
    ``functools.partial`` of it do; the 4-arg test runners do not)."""
    import inspect

    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return False
    return "on_spawn" in params or any(p.kind is p.VAR_KEYWORD for p in params.values())


def _write_result(path: str, record: Dict[str, Any]) -> None:
    """Atomic 0600 write into a 0700 dir (mirrors ``rendezvous.write_rendezvous``)."""
    directory = os.path.dirname(path)
    if os.path.dirname(directory) == _rv.run_base_dir():
        _rv._secure_dir(os.path.dirname(directory))
    _rv._secure_dir(directory)
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class GoliveMonitor:
    """One per server start: schedules ONE smoke run and holds its latest result."""

    def __init__(self, *, port: int, runtime_root: str, build_id: Optional[str],
                 runner: Optional[Runner] = None, log: Callable[[str], None] = None,
                 timeout: float = SMOKE_TIMEOUT_S) -> None:
        self.port = int(port)
        self.runtime_root = str(runtime_root)
        self.build_id = build_id
        self._runner: Runner = runner or default_runner
        self._log = log or (lambda line: print(line, flush=True))
        self._timeout = float(timeout)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._child: Optional[subprocess.Popen] = None
        self._runner_takes_on_spawn = _accepts_on_spawn(self._runner)
        self._state: Dict[str, Any] = {
            "enabled": True, "state": "idle", "build_id": build_id, "server_pid": os.getpid(),
            "port": self.port, "delay_s": None, "scheduled_at": None, "started_at": None,
            "finished_at": None, "exit_code": None, "counts": None, "checks": [],
            "result_file": None, "write_error": None, "error": None, "on_demand": False,
        }

    # -- state --------------------------------------------------------------------------
    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def _set(self, **kw: Any) -> None:
        with self._lock:
            self._state.update(kw)

    def _on_spawn(self, proc: Optional[subprocess.Popen]) -> None:
        with self._lock:
            self._child = proc
            self._state["child_pid"] = proc.pid if proc is not None else None
        if proc is not None and self._cancel.is_set():
            self._kill_child()   # shutdown raced the spawn: never leave it running

    def child_pid(self) -> Optional[int]:
        """The pid of the in-flight smoke subprocess, or None."""
        with self._lock:
            return self._child.pid if self._child is not None else None

    def _kill_child(self) -> Optional[int]:
        """Kill the in-flight smoke BY ITS OWN Popen handle (its recorded pid; the
        handle is still unreaped, so the pid cannot have been reused). Never by
        pattern. Returns the killed pid, or None when nothing was in flight."""
        with self._lock:
            proc = self._child
        if proc is None or proc.poll() is not None:
            return None
        try:
            proc.kill()
        except OSError:
            return None
        self._set(killed_pid=proc.pid)
        self._log(f"[gm-web] golive: server shutdown — killed in-flight smoke pid {proc.pid}")
        return proc.pid

    def mark_disabled(self, reason: str) -> None:
        self._set(enabled=False, state="disabled", error=None, disabled_reason=reason)

    # -- the run ------------------------------------------------------------------------
    def run_once(self) -> Dict[str, Any]:
        """Run the smoke once; record + persist + log. Never raises."""
        self._set(state="running", started_at=_now_iso())
        run_base = _rv.run_base_dir()
        try:
            extra = {"on_spawn": self._on_spawn} if self._runner_takes_on_spawn else {}
            code, out, err = self._runner(self.port, self.runtime_root, run_base, self._timeout, **extra)
            if self._cancel.is_set():
                self._set(state="cancelled", finished_at=_now_iso(), exit_code=code)
                self._log("[gm-web] golive: run cancelled by server shutdown (no result recorded)")
                return self.snapshot()
            checks = parse_output(code, out, err)
        except subprocess.TimeoutExpired:
            code = None
            checks = [{"check": "smoke.timeout", "status": FAIL,
                       "evidence": f"smoke did not finish within {self._timeout:.0f}s (killed)"}]
        except Exception as exc:  # noqa: BLE001 - a smoke that cannot run is a FAIL, loudly
            code = None
            checks = [{"check": "smoke.crashed", "status": FAIL,
                       "evidence": f"smoke could not run: {type(exc).__name__}: {exc}"}]
        counts = {s: sum(1 for c in checks if c["status"] == s) for s in (PASS, FAIL, SKIP)}
        path = result_path(self.runtime_root)
        self._set(state="done", finished_at=_now_iso(), exit_code=code, counts=counts,
                  checks=checks, result_file=path, write_error=None)
        try:
            _write_result(path, self.snapshot())
        except Exception as exc:  # noqa: BLE001 - reported, never fatal
            self._set(write_error=f"{type(exc).__name__}: {exc}")
            self._log(f"[gm-web] golive: could not write {path}: {type(exc).__name__}: {exc}")
        verdict = "FAIL" if counts[FAIL] else "PASS"
        self._log(f"[gm-web] golive {verdict}: {counts[PASS]} PASS, {counts[FAIL]} FAIL, "
                  f"{counts[SKIP]} SKIP (build {self.build_id}; {path})")
        for c in checks:
            if c["status"] == FAIL:
                self._log(f"[gm-web] golive FAIL  {c['check']}  {c['evidence']}")
        return self.snapshot()

    # -- scheduling ---------------------------------------------------------------------
    def schedule(self, delay: float = DEFAULT_DELAY_S) -> threading.Thread:
        """Start the delayed run on a daemon thread and return at once."""
        self._set(state="scheduled", delay_s=float(delay), scheduled_at=_now_iso())

        def _body() -> None:
            try:
                if self._cancel.wait(max(0.0, float(delay))):
                    self._set(state="cancelled")
                    return
                self.run_once()
            except Exception as exc:  # noqa: BLE001 - the thread must never raise
                self._set(state="error", error=f"{type(exc).__name__}: {exc}")
                try:
                    self._log(f"[gm-web] golive ERROR: the smoke thread crashed: "
                              f"{type(exc).__name__}: {exc}")
                except Exception:  # noqa: BLE001
                    pass

        th = threading.Thread(target=_body, name="gm-golive-smoke", daemon=True)
        self._thread = th
        th.start()
        return th

    def cancel(self, join_timeout: float = 1.0) -> None:
        """Server shutdown. A pending run is cancelled; an IN-FLIGHT smoke subprocess
        is killed by its own recorded pid (rm1a2 audit fix — never by pattern); the
        thread is then joined with a short timeout so the recorded state is
        ``cancelled`` by the time this returns. Never raises."""
        self._cancel.set()
        try:
            self._kill_child()
        except Exception:  # noqa: BLE001 - shutdown must never raise
            pass
        th = self._thread
        if th is not None and th.is_alive() and th is not threading.current_thread():
            th.join(join_timeout)

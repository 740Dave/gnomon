"""launchd supervision through an INJECTED adapter (g31, g73-amendment item 4).

Every Gnomon-started app is a per-app launchd USER agent labelled ``<label prefix><id>`` (:func:`label_prefix`) whose program is the
preflight wrapper (``gnomon-app-launch <id>``, run as ``<python> -m gnomon.appshost.launch <id>``); launchd
NEVER runs the manifest's start directly. Run-at-boot is ``RunAtLoad``.

The host talks to launchd only through a :class:`LaunchdAdapter`. :class:`RealLaunchd` is the production one; it
REFUSES to act while running under pytest (``PYTEST_CURRENT_TEST`` set) unless it was constructed with
``allow_under_pytest=True``, which only the explicitly flagged opt-in test does (g31: tests never touch
``~/Library/LaunchAgents`` or ``launchctl``). Tests use a double.

Restart policy: the generated agent carries NO ``KeepAlive`` key, so a crashed app is left ``failed`` and never
restarted by launchd. That is the reading the spec's ``failed`` status implies; the policy itself is still
OPEN (open-question-restart-after-crash) and this is the conservative choice.
"""

from __future__ import annotations

import os
import time
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Set

#: The NEUTRAL launchd label prefix. A deployment's own prefix is deploy data (``<deploy_root>/defaults/apps-host.json``,
#: key ``launchd-label-prefix``; owner ruling 2026-10-04, batch P7): changing it orphans agents already installed under
#: the old one, so a deployment that has apps installed keeps its prefix there. Read at CALL time, never at import.
NEUTRAL_LABEL_PREFIX = "com.gnomon.app."


def label_prefix() -> str:
    """The launchd label prefix in force: the deployment's ``launchd-label-prefix`` (a dotted, lower-case, '.'-ended
    string) or the neutral default. A malformed value is recorded as a fault and ignored (degrade-visibly)."""
    from .. import deploy_data

    v = deploy_data.section("apps-host", "launchd-label-prefix")
    if v is None:
        return NEUTRAL_LABEL_PREFIX
    if not (isinstance(v, str) and re.fullmatch(r"[a-z][a-z0-9]*(\.[a-z][a-z0-9-]*)+\.", v)):
        deploy_data.record_fault("apps-host", "key 'launchd-label-prefix' must look like 'com.example.app.'; ignored")
        return NEUTRAL_LABEL_PREFIX
    return v

LAUNCHCTL = "/bin/launchctl"
WRAPPER_MODULE = "gnomon.appshost.launch"
#: the wrapper's documented command name (a console script in pyproject.toml)
WRAPPER_NAME = "gnomon-app-launch"
#: g73-amendment-4 rule 4: the ONE PATH the host gives an app and its own wrapper: system directories only, none of
#: which a user-level process can write (no /usr/local/bin, no /opt/homebrew/bin, nothing under the home).
SAFE_APP_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def label_for(app_id: str) -> str:
    return f"{label_prefix()}{app_id}"


def package_root() -> str:
    """The directory that CONTAINS the ``gnomon`` package (so ``-m`` works without an install)."""
    here = os.path.dirname(os.path.abspath(__file__))           # .../gnomon/appshost
    return os.path.dirname(os.path.dirname(here))


def wrapper_argv(app_id: str, data_root: str, python: Optional[str] = None) -> List[str]:
    """The launchd ProgramArguments: the preflight wrapper for ``app_id``, pinned to this DATA root. The wrapper is a
    Python the host runs, so it runs isolated (-I: no PYTHON* env, no user site, no cwd on sys.path; rule 3); the
    one directory it needs on sys.path, the package root, is inserted by the bootstrap itself."""
    boot = (f"import sys; sys.path.insert(0, {package_root()!r}); "
            f"from {WRAPPER_MODULE} import main; sys.exit(main())")
    return [python or sys.executable, "-I", "-c", boot, app_id, "--data-root", data_root]


def build_plist(app_id: str, *, data_root: str, run_at_boot: bool, log_path: str, exit_timeout_s: float,
                python: Optional[str] = None, path_env: Optional[str] = None) -> Dict[str, Any]:
    """The generated agent definition (pure; no I/O)."""
    env = {"PATH": path_env or SAFE_APP_PATH}
    return {
        "Label": label_for(app_id),
        "ProgramArguments": wrapper_argv(app_id, data_root, python),
        "RunAtLoad": bool(run_at_boot),
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": env,
        "ExitTimeOut": int(max(1, round(exit_timeout_s)) + 1),
        "ProcessType": "Standard",
    }


@dataclass
class AgentState:
    loaded: bool
    pid: Optional[int] = None
    last_exit: Optional[int] = None     # None = never exited (or unknown)


class LaunchdAdapter(Protocol):
    def write_plist(self, label: str, plist: Dict[str, Any]) -> None: ...
    def remove_plist(self, label: str) -> None: ...
    def read_plist(self, label: str) -> Optional[Dict[str, Any]]: ...
    def bootstrap(self, label: str) -> None: ...
    def bootout(self, label: str) -> None: ...
    def kickstart(self, label: str) -> None: ...
    def kill(self, label: str, signal_name: str) -> None: ...
    def state(self, label: str) -> AgentState: ...
    def labels(self) -> Set[str]: ...


class LaunchdError(RuntimeError):
    pass


class RealLaunchd:
    """The production adapter: ``launchctl`` in the user's gui domain, plists in ``~/Library/LaunchAgents``."""

    def __init__(self, *, allow_under_pytest: bool = False, launchctl: str = LAUNCHCTL,
                 plist_dir: Optional[str] = None) -> None:
        self._allow = allow_under_pytest
        self._launchctl = launchctl
        self._plist_dir = plist_dir

    # -- guards -----------------------------------------------------------------------------------------
    def _guard(self, what: str) -> None:
        if os.environ.get("PYTEST_CURRENT_TEST") and not self._allow:
            raise LaunchdError(f"RealLaunchd.{what} refused under pytest (g31): tests use the launchd double")

    def _check_label(self, label: str) -> None:
        pre = label_prefix()
        if not label.startswith(pre) or not re.match(r"^[a-z][a-z0-9-]{1,40}$", label[len(pre):]):
            raise LaunchdError(f"refusing a label outside {pre}<id>: {label!r}")

    def plist_dir(self) -> str:
        return self._plist_dir or os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents")

    def plist_path(self, label: str) -> str:
        return os.path.join(self.plist_dir(), f"{label}.plist")

    def _domain(self) -> str:
        return f"gui/{os.getuid()}"

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        r = subprocess.run([self._launchctl, *args], capture_output=True, text=True, timeout=30)
        if check and r.returncode != 0:
            raise LaunchdError(f"launchctl {' '.join(args)} exited {r.returncode}: "
                               f"{(r.stderr or r.stdout).strip()[:300]}")
        return r

    # -- plist ------------------------------------------------------------------------------------------
    def write_plist(self, label: str, plist: Dict[str, Any]) -> None:
        self._guard("write_plist")
        self._check_label(label)
        path = self.plist_path(label)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        from .fsafe import open_new_fd

        data = plistlib.dumps(plist)
        d = os.path.dirname(path)
        # R3E-F2: a UNIQUE temp name opened O_EXCL | O_NONBLOCK (never a fixed ``<label>.plist.tmp`` an app-side process
        # could pre-create as a FIFO), fstat-checked regular, then renamed over the plist
        tmp = os.path.join(d, f".{label}.plist.tmp-{os.getpid()}-{time.monotonic_ns()}-{os.urandom(4).hex()}")
        fd = open_new_fd(tmp, 0o644)
        try:
            with os.fdopen(fd, "wb", closefd=False) as fh:
                fh.write(data)
                fh.flush()
            os.close(fd)
            fd = -1
            os.replace(tmp, path)
        except BaseException:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def remove_plist(self, label: str) -> None:
        self._guard("remove_plist")
        self._check_label(label)
        try:
            os.unlink(self.plist_path(label))
        except FileNotFoundError:
            pass

    def read_plist(self, label: str) -> Optional[Dict[str, Any]]:
        self._check_label(label)
        try:
            from .fsafe import open_regular

            with open_regular(self.plist_path(label)) as fh:          # R3C-F9: never block on a FIFO (the host does not call this today)
                return plistlib.load(fh)
        except FileNotFoundError:
            return None

    # -- launchctl --------------------------------------------------------------------------------------
    def bootstrap(self, label: str) -> None:
        self._guard("bootstrap")
        self._check_label(label)
        if self.state(label).loaded:
            return
        self._run("bootstrap", self._domain(), self.plist_path(label))

    def bootout(self, label: str) -> None:
        self._guard("bootout")
        self._check_label(label)
        if not self.state(label).loaded:
            return
        self._run("bootout", f"{self._domain()}/{label}")

    def kickstart(self, label: str) -> None:
        self._guard("kickstart")
        self._check_label(label)
        if self.state(label).pid:          # already running (e.g. RunAtLoad started it at bootstrap): nothing to do
            return
        self._run("kickstart", f"{self._domain()}/{label}")

    def kill(self, label: str, signal_name: str) -> None:
        self._guard("kill")
        self._check_label(label)
        import signal as _signal

        r = self._run("kill", str(int(_signal.Signals[signal_name])), f"{self._domain()}/{label}", check=False)
        if r.returncode != 0 and self.state(label).pid:   # a job that exited on its own meanwhile is not a fault
            raise LaunchdError(f"launchctl kill {signal_name} {label} exited {r.returncode}: "
                               f"{(r.stderr or r.stdout).strip()[:300]}")

    def state(self, label: str) -> AgentState:
        self._guard("state")
        self._check_label(label)
        r = self._run("print", f"{self._domain()}/{label}", check=False)
        if r.returncode != 0:
            return AgentState(loaded=False)
        return parse_print(r.stdout)

    def labels(self) -> Set[str]:
        self._guard("labels")
        r = self._run("list", check=False)
        out: Set[str] = set()
        for line in r.stdout.splitlines()[1:]:
            parts = line.split("\t")
            if len(parts) == 3 and parts[2].startswith(label_prefix()):
                out.add(parts[2])
        return out


def parse_print(text: str) -> AgentState:
    """Parse ``launchctl print gui/<uid>/<label>``."""
    pid = None
    last = None
    m = re.search(r"^\s*pid = (\d+)\s*$", text, re.M)
    if m:
        pid = int(m.group(1))
    m = re.search(r"^\s*last exit code = (-?\d+)", text, re.M)
    if m:
        last = int(m.group(1))
    return AgentState(loaded=True, pid=pid, last_exit=last)

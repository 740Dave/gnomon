"""Host state under the Gnomon DATA root (ruling R15; g10, g69, g73-amendment items 2-3).

Layout (all under ``<DATA>``)::

    apps.json                         the registry/whitelist: legacy entries + {"<id>": {"manifest": "<path>"}}
    apps/ports.json                   the port table {"<id>": {"port": N, "state": "proposed"|"installed"}}
    apps/_proposals/<id>.json         a pending install / uninstall proposal
    apps/_uninstalled/<id>-<ts>/      the ARCHIVE of an uninstalled app's host state (never deleted)
    apps/<id>/settings.json           run_at_boot, scope, path values, user_editable values
    apps/<id>/pin.json                the manifest pin + the code pin (+ pin_snapshot/ for the re-confirm diff)
    apps/<id>/backups.json            the backup records
    apps/<id>/audit.jsonl             the audit log (ONE writer: :func:`append_audit`)
    apps/<id>/logs/app.log            the app's stdout/stderr (launchd StandardOut/ErrorPath)
    apps/<id>/runtime/                ui_token (0600, while running), preflight.json, refusal.json, ...

The DATA root: this tree (~/live-tree) has NO roots.py (it exists in ~/gnomon only, X13 S5-4). The single
resolver here is :func:`data_root`, which reads the machine config's ``runtime_root`` at CALL time (never at
import, never a literal path). When this code moves to the gnomon tree, :func:`data_root` is the one line that
changes to ``config.runtime_root_default()`` (g69).

Host state NEVER lives in the app's directory.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import errno
import fcntl
import json
import os
import shutil
import time
from typing import Any, Dict, Iterator, List, Optional

from .fsafe import open_append_fd


class AppsError(Exception):
    """A refused host operation with a message safe to show (never a token, never licence content). Defined here (and
    re-exported by ``host``) so the state layer can refuse cleanly too (R3E-F3)."""

    def __init__(self, message: str, **detail: Any) -> None:
        super().__init__(message)
        self.detail = detail

PROPOSALS_DIRNAME = "_proposals"
UNINSTALLED_DIRNAME = "_uninstalled"
LOCK_FILENAME = ".host.lock"
REGISTRY_FILENAME = "apps.json"
AUDIT_FILENAME = "audit.jsonl"
#: how many trailing log lines a failed status carries
LOG_TAIL_LINES = 20
LOG_TAIL_MAX_BYTES = 64 * 1024


def data_root(config: Any = None) -> str:
    """The Gnomon DATA root, resolved at call time from the machine config (``runtime_root``)."""
    if config is None:
        from ..config import ConfigStore

        config = ConfigStore().load()
    root = config.runtime_root
    if not isinstance(root, str) or not root.strip():
        raise ValueError("machine config has no runtime_root (the DATA root)")
    return os.path.abspath(os.path.expanduser(root))


def utc_now_iso(ts: Optional[float] = None) -> str:
    t = time.time() if ts is None else ts
    return _dt.datetime.fromtimestamp(t, tz=_dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class HostPaths:
    """Every host-state path for one DATA root. Pure path arithmetic; creates nothing."""

    def __init__(self, root: str) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))

    @property
    def registry(self) -> str:
        return os.path.join(self.root, REGISTRY_FILENAME)

    @property
    def apps(self) -> str:
        return os.path.join(self.root, "apps")

    @property
    def ports(self) -> str:
        return os.path.join(self.apps, "ports.json")

    @property
    def lock(self) -> str:
        return os.path.join(self.apps, LOCK_FILENAME)

    @property
    def proposals(self) -> str:
        return os.path.join(self.apps, PROPOSALS_DIRNAME)

    @property
    def uninstalled(self) -> str:
        return os.path.join(self.apps, UNINSTALLED_DIRNAME)

    def proposal(self, app_id: str) -> str:
        return os.path.join(self.proposals, f"{app_id}.json")

    def app(self, app_id: str) -> str:
        return os.path.join(self.apps, app_id)

    def settings(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), "settings.json")

    def pin(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), "pin.json")

    def pin_snapshot(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), "pin_snapshot")

    def backups(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), "backups.json")

    def audit(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), AUDIT_FILENAME)

    def logs(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), "logs")

    def app_log(self, app_id: str) -> str:
        return os.path.join(self.logs(app_id), "app.log")

    def runtime(self, app_id: str) -> str:
        return os.path.join(self.app(app_id), "runtime")

    def app_home(self, app_id: str) -> str:
        """The HOME every process the host runs for this app gets (g73-amendment-4 rule 4): host-owned, 0700, under the
        DATA root, never the user's real home."""
        return os.path.join(self.app(app_id), "home")

    def ui_token(self, app_id: str) -> str:
        return os.path.join(self.runtime(app_id), "ui_token")

    def preflight_record(self, app_id: str) -> str:
        return os.path.join(self.runtime(app_id), "preflight.json")

    def start_sig(self, app_id: str) -> str:
        """Round 3c (R3-F10): the cheap stat signature of the pinned inputs, written by the wrapper right before exec."""
        return os.path.join(self.runtime(app_id), "start_sig.json")

    def refusal_record(self, app_id: str) -> str:
        return os.path.join(self.runtime(app_id), "refusal.json")

    def stop_record(self, app_id: str) -> str:
        return os.path.join(self.runtime(app_id), "stopped.json")

    def foreign_record(self, app_id: str) -> str:
        return os.path.join(self.runtime(app_id), "foreign.json")


# --------------------------------------------------------------------------------------------------------
# file primitives
# --------------------------------------------------------------------------------------------------------

def read_json(path: str, default: Any = None) -> Any:
    """Read a host data file (settings, pin, start_sig, ...). R3C-F4: through ``fsafe.open_regular``, so a same-uid swap
    of the file for a FIFO never blocks a poll; a non-regular file reads like a corrupt one (ValueError)."""
    from .fsafe import NotRegularFile, open_regular

    try:
        with open_regular(path) as fh:
            return json.loads(fh.read().decode("utf-8"))
    except FileNotFoundError:
        return default
    except NotRegularFile as exc:
        raise ValueError(f"{path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise ValueError(f"{path}: not UTF-8 ({exc})") from exc


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    """tmp + fsync + rename in the same directory: a reader sees the old or the new file, never a part."""
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{os.path.basename(path)}.tmp-{os.getpid()}-{time.monotonic_ns()}")
    from .fsafe import open_new_fd

    fd = open_new_fd(tmp, mode)
    try:
        with os.fdopen(fd, "wb", closefd=False) as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.close(fd)
    try:
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_write_json(path: str, obj: Any, mode: int = 0o600) -> None:
    atomic_write_bytes(path, (json.dumps(obj, indent=2, sort_keys=False) + "\n").encode("utf-8"), mode)


def unlink_quiet(path: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)


@contextlib.contextmanager
def host_lock(paths: HostPaths) -> Iterator[None]:
    """One process-crossing lock for every mutation of the port table, the registry and proposals."""
    os.makedirs(paths.apps, exist_ok=True)
    from .fsafe import open_lock_fd

    try:
        fd = open_lock_fd(paths.lock)
    except OSError as exc:                       # R3E-F3: a FIFO / device / symlink at the lock name is a clean refusal
        raise AppsError(f"the host lock file {paths.lock} cannot be used ({exc.strerror or exc}); it must be a regular "
                        "file: remove it or restore it, then retry (nothing was changed)") from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except OSError as exc:
            raise AppsError(f"the host lock {paths.lock} cannot be locked ({exc.strerror or exc}); nothing was "
                            "changed") from exc
        yield
    finally:
        try:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


# --------------------------------------------------------------------------------------------------------
# the single audit writer (g73-amendment item 2)
# --------------------------------------------------------------------------------------------------------

def append_audit(paths: HostPaths, app_id: str, *, verb: str, actor: str, actor_verified: bool, outcome: str,
                 detail: Optional[Dict[str, Any]] = None, ts: Optional[float] = None) -> Dict[str, Any]:
    """Append ONE audit entry to ``<DATA>/apps/<id>/audit.jsonl``. The only audit writer in the host."""
    entry: Dict[str, Any] = {
        "ts": utc_now_iso(ts),
        "verb": verb,
        "app_id": app_id,
        "actor": actor,
        "actor_verified": bool(actor_verified),
        "outcome": outcome,
    }
    if detail:
        entry["detail"] = detail
    line = (json.dumps(entry, sort_keys=False) + "\n").encode("utf-8")
    os.makedirs(paths.app(app_id), exist_ok=True)
    fd = open_append_fd(paths.audit(app_id))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        os.close(fd)
    return entry


def append_host_audit(paths: HostPaths, *, verb: str, actor: str, actor_verified: bool, outcome: str,
                      detail: Optional[Dict[str, Any]] = None, ts: Optional[float] = None) -> Dict[str, Any]:
    """The host-level audit (``<DATA>/apps/audit.jsonl``) for a verb call that names no installed or proposed app
    (app_list; a proposal whose manifest was refused before its id was known; an unknown id)."""
    entry: Dict[str, Any] = {"ts": utc_now_iso(ts), "verb": verb, "app_id": None, "actor": actor,
                             "actor_verified": bool(actor_verified), "outcome": outcome}
    if detail:
        entry["detail"] = detail
    os.makedirs(paths.apps, exist_ok=True)
    fd = open_append_fd(os.path.join(paths.apps, AUDIT_FILENAME))
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, (json.dumps(entry) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    return entry


def read_audit(paths: HostPaths, app_id: str, limit: int = 500) -> List[Dict[str, Any]]:
    from .fsafe import NotRegularFile, open_regular

    out: List[Dict[str, Any]] = []
    try:
        with open_regular(paths.audit(app_id)) as bfh:          # R3C-F4: never block on a FIFO
            text = bfh.read().decode("utf-8", errors="replace")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                out.append({"verb": "<unreadable audit line>", "outcome": "error"})
    except FileNotFoundError:
        return []
    except NotRegularFile:
        return [{"verb": "<audit log is not a regular file>", "outcome": "error"}]
    return out[-limit:]


# --------------------------------------------------------------------------------------------------------
# the registry (apps.json) — legacy entries are preserved untouched
# --------------------------------------------------------------------------------------------------------

def read_registry(paths: HostPaths) -> Dict[str, Any]:
    data = read_json(paths.registry, default=None)
    if data is None:
        return {"apps": {}}
    if not isinstance(data, dict) or not isinstance(data.get("apps"), dict):
        raise ValueError(f"{paths.registry} has no 'apps' object")
    return data


def manifest_entries(paths: HostPaths) -> Dict[str, str]:
    """``{app_id: manifest_path}`` for every manifest-registered (installed) app in apps.json."""
    try:
        apps = read_registry(paths)["apps"]
    except ValueError:
        return {}
    out: Dict[str, str] = {}
    for k, v in apps.items():
        if isinstance(v, dict) and isinstance(v.get("manifest"), str):
            out[k] = v["manifest"]
    return out


def write_registry(paths: HostPaths, data: Dict[str, Any]) -> None:
    # apps.json predates the host and is hand-editable: keep its existing mode (0644 when it is new).
    try:
        mode = os.stat(paths.registry).st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    atomic_write_json(paths.registry, data, mode=mode)


# --------------------------------------------------------------------------------------------------------
# the port table
# --------------------------------------------------------------------------------------------------------

def read_ports(paths: HostPaths) -> Dict[str, Dict[str, Any]]:
    data = read_json(paths.ports, default={})
    if not isinstance(data, dict):
        raise ValueError(f"{paths.ports} is not a JSON object")
    return {k: v for k, v in data.items() if isinstance(v, dict) and isinstance(v.get("port"), int)}


def write_ports(paths: HostPaths, table: Dict[str, Dict[str, Any]]) -> None:
    atomic_write_json(paths.ports, table)


# --------------------------------------------------------------------------------------------------------
# archive on uninstall (never delete)
# --------------------------------------------------------------------------------------------------------

def archive_app_dir(paths: HostPaths, app_id: str, ts: Optional[float] = None) -> Optional[str]:
    """Move ``<DATA>/apps/<id>/`` to ``<DATA>/apps/_uninstalled/<id>-<ts>/``. Returns the archive path."""
    src = paths.app(app_id)
    if not os.path.isdir(src):
        return None
    stamp = _dt.datetime.fromtimestamp(time.time() if ts is None else ts, tz=_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    os.makedirs(paths.uninstalled, exist_ok=True)
    dst = os.path.join(paths.uninstalled, f"{app_id}-{stamp}")
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = os.path.join(paths.uninstalled, f"{app_id}-{stamp}-{n}")
    # runtime/ holds a live token and pid records: never archive a secret
    unlink_quiet(paths.ui_token(app_id))
    try:
        os.rename(src, dst)
    except OSError as exc:  # cross-device would be a layout error; never fall back to delete
        if exc.errno == errno.EXDEV:
            shutil.copytree(src, dst, symlinks=True)
            shutil.rmtree(src)
        else:
            raise
    return dst


def tail_lines(path: str, n: int = LOG_TAIL_LINES) -> List[str]:
    from .fsafe import open_regular            # the app controls its own log file: never block on a FIFO there

    try:
        with open_regular(path) as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            start = max(0, size - LOG_TAIL_MAX_BYTES)
            if start > 0:
                fh.seek(start - 1)
                at_line_start = fh.read(1) == b"\n"
            else:
                fh.seek(0)
                at_line_start = True
            data = fh.read()
    except OSError:                              # missing, unreadable, or not a regular file: no tail, no raise
        return []
    if not at_line_start:
        # R3C-F3: the window starts mid-line; its first fragment may be the tail of a secret the shape rules cannot
        # recognise. Drop it (through the first newline; a window with no newline at all is one long fragment: no tail).
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return lines[-n:]

"""Hub discovery — the well-known ``rendezvous.json`` (
``hub-discovery``; D15 amendment, operator-approved 2026-09-29).

The hub binds its rendezvous listener on a FRESH ephemeral port every start, so a
port handed to a harness at spawn goes stale at the first dashboard restart. The
hub therefore publishes where it is, and who it is, in ONE file:

    <run_base>/rt-<sha256(realpath(runtime_root))[:12]>/rendezvous.json
        (0600, dirs 0700, written atomically)

    <run_base> = $GNOMON_RUN_DIR, else ~/Library/Application Support/Gnomon/run
    (macOS), else $XDG_RUNTIME_DIR/gnomon, else /tmp/gm-<uid>.

⚠ FIX PASS (2026-09-29, independent audit): the file is EPHEMERAL, machine-local
state. It is deliberately NOT under the runtime root (a synced
symlink): a synced copy would redirect harnesses on another machine, and two
machines sharing the tree would clobber each other. The runtime-root hash keeps a
second (e.g. tmp-rooted) dashboard from clobbering the live one.

Reads are HARDENED: O_NONBLOCK|O_NOFOLLOW open (a FIFO can never freeze the pump,
a symlink is refused), then fstat — regular file, owned by this uid, not group/
other writable, size-capped — and the host must be loopback (127.0.0.1 / ::1 /
localhost). Boundary (stated): a same-uid process can still write this file; the
bar is "no off-machine or foreign-uid redirect", not a hostile same-uid adversary.

    {"schema": 1, "rendezvous_host": "127.0.0.1", "rendezvous_port": 64480,
     "hub_instance_id": "<uuid4 hex, new every start>", "mcp_url": "...",
     "hub_pid": 1234, "started_epoch": 1790659253.1}

Harnesses of every launch type re-read it and reconnect + re-register when
``(rendezvous_port, hub_instance_id)`` changes or their connection drops. A
launch-time port is only a first hint.

The file is REMOVED on a clean hub stop, and only by the hub that wrote it (a
newer hub's file is never removed by an older one's shutdown). A crashed hub
leaves a stale file whose port refuses connections — a harness just backs off.

stdlib only; shared by :mod:`gnomon.hub.server` (writer) and
:mod:`gnomon.harness.hublink` (reader).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

RENDEZVOUS_FILENAME = "rendezvous.json"
#: Env override for the per-uid run-dir base (tests point it at a tmp dir).
ENV_RUN_DIR = "GNOMON_RUN_DIR"
#: FIX PASS 2: literal loopback IPs ONLY (``localhost`` is a name, resolvable to
#: anything a hosts file says).
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1"})


def is_loopback_http_url(url: Any) -> bool:
    """True only for ``http://127.0.0.1:<port>/...`` or ``http://[::1]:<port>/...``
    with no userinfo — the only MCP url an operator Bearer token may be sent to
    (FIX PASS 2, re-audit MEDIUM)."""
    if not isinstance(url, str) or len(url) > 2048:
        return False
    # FIX PASS 3: no whitespace (incl. Unicode spaces / line separators) and no
    # control character ANYWHERE — urlsplit silently strips some of them.
    if any(ch.isspace() or ord(ch) < 0x20 or 0x7F <= ord(ch) <= 0x9F for ch in url):
        return False
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return False
    if parts.scheme != "http" or parts.username is not None or parts.password is not None:
        return False
    return parts.hostname in LOOPBACK_HOSTS and port is not None and 1 <= port <= 65535
SCHEMA = 1
#: A discovery file larger than this is not ours — refuse to parse it.
_MAX_BYTES = 64 * 1024


@dataclass(frozen=True)
class RendezvousInfo:
    host: str
    port: int
    hub_instance_id: Optional[str]
    mcp_url: Optional[str] = None
    hub_pid: Optional[int] = None
    started_epoch: Optional[float] = None
    #: ``"on"`` when the hub binds callers; ``"off"`` when it says so OR the key is absent
    #: (a hub with the flag OFF, or one that predates the key, writes none). Anything else reads as ``None``.
    binding: Optional[str] = None

    @property
    def identity(self) -> Tuple[str, int, Optional[str]]:
        """What a harness compares to decide "this is a different hub"."""
        return (self.host, self.port, self.hub_instance_id)


def run_base_dir() -> str:
    """The machine-local, per-uid base for ephemeral hub state (never synced)."""
    override = os.environ.get(ENV_RUN_DIR)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                            "Gnomon", "run")
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "gnomon")
    return os.path.join("/tmp", f"gm-{os.getuid()}")


def rendezvous_path(runtime_root: str) -> str:
    """The discovery file for the dashboard whose runtime root is ``runtime_root``.

    Keyed by the REAL path of the runtime root, so the hub (which derives it from
    its agents root) and a harness (from the machine config) agree even
    through a symlinked data root."""
    real = os.path.realpath(os.path.expanduser(runtime_root))
    key = hashlib.sha256(real.encode("utf-8")).hexdigest()[:12]
    return os.path.join(run_base_dir(), f"rt-{key}", RENDEZVOUS_FILENAME)


def _secure_dir(path: str) -> None:
    """Create ``path`` 0700 (and missing parents) and REFUSE one that is not a
    real directory owned by this uid with no group/other write."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{path} is not a directory")
    if st.st_uid != os.getuid():
        raise OSError(f"{path} is not owned by uid {os.getuid()}")
    if stat.S_IMODE(st.st_mode) != 0o700:
        os.chmod(path, 0o700)


def safe_read_bytes(path: str, max_bytes: int) -> bytes:
    """Read a small state file WITHOUT trusting it: non-blocking + no-follow open,
    then fstat: regular file, owned by this uid, not group/other writable, size
    capped. Raises ``OSError`` with a reason on any refusal (FileNotFoundError when
    absent)."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise OSError("refused: is a symlink") from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError("refused: not a regular file")
        if st.st_uid != os.getuid():
            raise OSError(f"refused: owner uid {st.st_uid} is not {os.getuid()}")
        if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise OSError("refused: group/other writable")
        if st.st_size > max_bytes:
            raise OSError("refused: oversized")
        chunks = []
        remaining = max_bytes + 1
        while remaining > 0:
            b = os.read(fd, min(65536, remaining))
            if not b:
                break
            chunks.append(b)
            remaining -= len(b)
        data = b"".join(chunks)
        if len(data) > max_bytes:
            raise OSError("refused: oversized")
        return data
    finally:
        os.close(fd)


def write_rendezvous(
    path: str,
    *,
    host: str,
    port: int,
    hub_instance_id: str,
    mcp_url: Optional[str] = None,
    hub_pid: Optional[int] = None,
    started_epoch: Optional[float] = None,
    binding: Optional[str] = None,
) -> Dict[str, Any]:
    """Atomically publish the hub's discovery record (temp 0600 + ``os.replace``). ``binding`` (caller binding,
    ``"on"``/``"off"``) is written only when given, so a hub with the flag OFF writes today's record unchanged."""
    record = {
        "schema": SCHEMA,
        "rendezvous_host": str(host),
        "rendezvous_port": int(port),
        "hub_instance_id": str(hub_instance_id),
        "mcp_url": mcp_url,
        "hub_pid": int(hub_pid if hub_pid is not None else os.getpid()),
        "started_epoch": float(started_epoch if started_epoch is not None else time.time()),
    }
    if binding is not None:
        record["binding"] = str(binding)
    directory = os.path.dirname(path) or "."
    parent = os.path.dirname(directory)
    if parent == run_base_dir():
        _secure_dir(parent)   # the per-uid run base itself: 0700, ours
    _secure_dir(directory)
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        try:
            os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    try:
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return record


def remove_rendezvous_if_ours(path: str, hub_instance_id: str) -> bool:
    """Remove the discovery file iff it still names ``hub_instance_id``.

    Returns whether it was removed. A file another (newer) hub has since written
    is left alone; an unreadable/corrupt file is left alone too (not provably
    ours — a harness treats it as "no hub" anyway)."""
    info, _err = read_rendezvous(path)
    if info is None or info.hub_instance_id != hub_instance_id:
        return False
    try:
        os.unlink(path)
        return True
    except OSError:
        return False


def read_rendezvous(path: Optional[str]) -> Tuple[Optional[RendezvousInfo], Optional[str]]:
    """Parse the discovery file. Returns ``(info, None)`` or ``(None, reason)``.

    NEVER raises: a missing file is ``(None, "absent")``; anything malformed (bad
    JSON, not an object, a non-int / out-of-range port, non-UTF-8, oversized) is
    ``(None, "<reason>")`` so the caller can report it loudly and keep running."""
    if not path:
        return None, "no rendezvous file configured"
    try:
        raw = safe_read_bytes(path, _MAX_BYTES)
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        msg = str(exc)
        if "not a regular file" in msg:
            return None, "refused: not a regular file"
        return None, (msg if msg.startswith("refused") else f"unreadable: {exc}")
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return None, f"corrupt: {type(exc).__name__}"
    if not isinstance(obj, dict):
        return None, "corrupt: not a JSON object"
    port = obj.get("rendezvous_port")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        return None, f"corrupt: bad rendezvous_port {port!r}"
    host = obj.get("rendezvous_host") or "127.0.0.1"
    if not isinstance(host, str):
        return None, "corrupt: bad rendezvous_host"
    if host not in LOOPBACK_HOSTS:
        return None, f"refused: rendezvous_host {host!r} is not loopback"
    hub_id = obj.get("hub_instance_id")
    if hub_id is not None and not isinstance(hub_id, str):
        return None, "corrupt: bad hub_instance_id"
    mcp_url = obj.get("mcp_url") if is_loopback_http_url(obj.get("mcp_url")) else None
    pid = obj.get("hub_pid") if isinstance(obj.get("hub_pid"), int) else None
    started = obj.get("started_epoch") if isinstance(obj.get("started_epoch"), (int, float)) else None
    raw_binding = obj.get("binding", "off")
    binding = raw_binding if raw_binding in ("on", "off") else None
    return RendezvousInfo(host=host, port=port, hub_instance_id=hub_id, mcp_url=mcp_url,
                          hub_pid=pid, started_epoch=started, binding=binding), None

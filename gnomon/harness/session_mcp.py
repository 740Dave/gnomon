"""Deliver the profile's ``mcp_servers`` to the child (flag ``session_mcp_from_profile``).

Root cause this fixes (investigation-hub-mcp-and-tool-limits-2026-10-01, part 1):
``LaunchManager`` writes the profile's MCP servers to ``<session>/.claude/.mcp.json``, a path
Claude Code never reads, and the harness passes ONLY ``harness.mcp.json`` (the self verbs) via
``--mcp-config``. So the hub (``gnomon``) was never given to the child.

With the flag ON, :func:`resolve_session_servers` turns the servers in ``.claude/.mcp.json`` into the
extra entries ``write_self_mcp_config`` merges into ``harness.mcp.json``:

* the ``gnomon`` hub is resolved to the CURRENT hub MCP url at launch (the rendezvous file's
  ``mcp_url``; the hub port is dynamic per start, so the url frozen at provision time can be stale);
* a server is included only if it resolves to a runnable stdio command or a live http url;
* a server the user/project scope already provides is NOT duplicated (noted);
* anything else is SKIPPED with a loud warning, never silently dropped.

HOOK REACHABILITY (g47/g48e): the harness hooks import ``harness.launcher`` (measured), so this module is
imported ONLY lazily, from ``prepare_child_launch``, and only when the flag is ON. Nothing here runs at
import time and nothing here is imported by a hook entry module. Flag OFF -> this file is never loaded.

CALLER BINDING (rev 2; this replaces the build's original "the hub does not identify the
calling session" blocker): the launcher delivers profile servers ONLY when the HUB publishes ``binding: on`` in
its rendezvous file (:func:`hub_binding_state`; the launcher reads the hub's state, not its own flag view). The
hub entry then carries a per-launch caller token in its ``X-Gnomon-Caller`` header, written ONLY into a 0600
credential config outside the (synced) session dir (:func:`write_credential_config`) that is passed as a second
``--mcp-config`` file and deleted on clean exit (:func:`remove_credential_config`). The bound is
:data:`HUB_BINDING_NOTE`, stated whenever the hub is included.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

FLAG_NAME = "session_mcp_from_profile"
HUB_SERVER_NAME = "gnomon"
SELF_SERVER_NAME = "gnomon-self"
_MAX_JSON_BYTES = 64 * 1024 * 1024

#: Where the per-launch credential configs live: a 0700 dir under the machine-local, per-uid run base (never the
#: synced runtime root), one 0600 file per session dir.
CREDENTIAL_DIRNAME = "caller-credentials"

HUB_BINDING_NOTE = (
    "the hub is delivered with a per-launch caller credential (the hub publishes binding: on), so it files and "
    "answers as THIS session only. Bound: this stops one session acting as another through tool arguments, "
    "stops a rogue re-registration, and stops registering any instance without writing the credential dir; it "
    "does NOT stop a same-uid process that READS this session's 0600 credential file (this one session) or "
    "WRITES a pending-launch record there (any instance with no current holder), and while agent tool limits "
    "are unenforced a Bash-capable agent can do both."
)

Event = Dict[str, Any]


def _read_json(path: str) -> Tuple[Optional[Any], Optional[str]]:
    try:
        if os.path.getsize(path) > _MAX_JSON_BYTES:
            return None, "oversized"
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh), None
    except FileNotFoundError:
        return None, "absent"
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {type(exc).__name__}"


def _servers_of(obj: Any) -> Dict[str, Any]:
    if isinstance(obj, dict) and isinstance(obj.get("mcpServers"), dict):
        return obj["mcpServers"]
    return {}


def other_scope_servers(cwd: Optional[str], home: Optional[str] = None) -> Tuple[Dict[str, str], List[Event]]:
    """Server names Claude Code already loads from scopes other than ``--mcp-config``.

    Returns ``({name: where}, events)``; an unreadable file is reported as an event (a duplicate is then
    possible, so it is loud) and never raises."""
    home = home or os.path.expanduser("~")
    found: Dict[str, str] = {}
    events: List[Event] = []
    candidates: List[Tuple[str, str]] = [
        (os.path.join(home, ".claude.json"), "user scope (~/.claude.json)"),
        (os.path.join(home, ".claude", "settings.json"), "user scope (~/.claude/settings.json)"),
        (os.path.join(home, ".claude", "settings.local.json"), "user scope (~/.claude/settings.local.json)"),
    ]
    if cwd:
        candidates.append((os.path.join(cwd, ".mcp.json"), "project scope (<cwd>/.mcp.json)"))
    for path, where in candidates:
        obj, err = _read_json(path)
        if err == "absent":
            continue
        if err:
            events.append({"server": None, "action": "warn", "reason": f"could not read {path} ({err}); "
                           "a server it provides may be duplicated"})
            continue
        for name in _servers_of(obj):
            found.setdefault(name, where)
        if cwd and os.path.basename(path) == ".claude.json" and isinstance(obj, dict):
            proj = (obj.get("projects") or {}).get(cwd) if isinstance(obj.get("projects"), dict) else None
            for name in _servers_of(proj):
                found.setdefault(name, "local scope (~/.claude.json projects[cwd])")
    return found, events


def tcp_alive(url: str, timeout: float = 0.5) -> bool:
    """Bounded liveness probe: can a TCP connection be opened to the url's host:port?"""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port or (443 if parts.scheme == "https" else 80)
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ValueError):
        return False


def stdio_runnable(command: Any, which: Callable[[str], Optional[str]] = shutil.which) -> bool:
    if not isinstance(command, str) or not command.strip():
        return False
    if os.sep in command:
        p = os.path.expanduser(command)
        return os.path.isfile(p) and os.access(p, os.X_OK)
    return which(command) is not None


def _clean(entry: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in entry.items() if not str(k).startswith("_")}


def current_hub_url(rendezvous_file: Optional[str]) -> Tuple[Optional[str], str]:
    """The CURRENT hub MCP url from the rendezvous file (loopback-validated by ``read_rendezvous``)."""
    from ..hub.rendezvous import read_rendezvous

    info, reason = read_rendezvous(rendezvous_file)
    if info is None:
        return None, f"rendezvous {reason}"
    if not info.mcp_url:
        return None, "rendezvous file carries no (loopback) mcp_url"
    return info.mcp_url, "rendezvous"


def hub_binding_state(rendezvous_file: Optional[str]) -> Tuple[Optional[str], str]:
    """``(state, how)``: the HUB's published caller-binding state, ``"on"`` / ``"off"`` (an absent key is off),
    or ``None`` when there is no readable rendezvous file."""
    from ..hub.rendezvous import read_rendezvous

    info, reason = read_rendezvous(rendezvous_file)
    if info is None:
        return None, f"rendezvous {reason}"
    return info.binding, "rendezvous"


def credential_path(session_dir: str) -> str:
    """The credential config for ``session_dir`` (keyed by its real path), under the per-uid run base."""
    from ..hub.rendezvous import run_base_dir

    key = hashlib.sha256(os.path.realpath(os.path.expanduser(session_dir)).encode("utf-8")).hexdigest()[:24]
    return os.path.join(run_base_dir(), CREDENTIAL_DIRNAME, f"{key}.mcp.json")


def write_credential_config(session_dir: str, servers: Dict[str, Dict[str, Any]]) -> str:
    """Write ``{"mcpServers": servers}`` (it holds the caller token) with ``os.open(..., 0o600)`` into a 0700 dir,
    atomically. Never via the 0644 ``_write_json_file`` path. Returns the path."""
    from ..hub.rendezvous import _secure_dir, run_base_dir

    path = credential_path(session_dir)
    directory = os.path.dirname(path)
    _secure_dir(run_base_dir())
    _secure_dir(directory)
    tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            os.write(fd, (json.dumps({"mcpServers": servers}, indent=2) + "\n").encode("utf-8"))
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
    return path


def remove_credential_config(path: Optional[str]) -> bool:
    """Delete a credential config written by :func:`write_credential_config`. Only a path inside the credential
    dir is touched. Returns whether a file was removed."""
    if not path:
        return False
    from ..hub.rendezvous import run_base_dir

    base = os.path.realpath(os.path.join(run_base_dir(), CREDENTIAL_DIRNAME))
    if os.path.dirname(os.path.realpath(path)) != base:
        return False
    try:
        os.unlink(path)
        return True
    except FileNotFoundError:
        return False


def resolve_session_servers(
    session_dir: str,
    *,
    cwd: Optional[str] = None,
    rendezvous_file: Optional[str] = None,
    home: Optional[str] = None,
    alive: Callable[[str], bool] = tcp_alive,
    which: Callable[[str], Optional[str]] = shutil.which,
) -> Tuple[Dict[str, Dict[str, Any]], List[Event], List[str]]:
    """``(extra_servers, events, profile_names)``.

    ``profile_names`` are the servers the profile asked for (from ``.claude/.mcp.json``); every one of them is
    either in ``extra_servers`` or has a ``skip`` event with a reason (the diff the checklist rule demands)."""
    events: List[Event] = []
    path = os.path.join(session_dir, ".claude", ".mcp.json")
    obj, err = _read_json(path)
    if err:
        events.append({"server": None, "action": "warn",
                       "reason": f"profile MCP list {path} is {err}; nothing to add"})
        return {}, events, []
    profile = _servers_of(obj)
    names = [n for n in profile if n != SELF_SERVER_NAME]
    other, other_events = other_scope_servers(cwd, home)
    events.extend(other_events)
    extra: Dict[str, Dict[str, Any]] = {}
    for name in names:
        entry = profile[name]
        if not isinstance(entry, dict):
            events.append({"server": name, "action": "skip", "reason": "entry is not an object"})
            continue
        if name in other:
            events.append({"server": name, "action": "skip",
                           "reason": f"already provided by {other[name]}; not duplicated"})
            continue
        typ = entry.get("type", "stdio")
        if name == HUB_SERVER_NAME:
            url, how = current_hub_url(rendezvous_file)
            if url is None:
                frozen = entry.get("url")
                if isinstance(frozen, str) and frozen:
                    url, how = frozen, f"provision-time url ({how})"
            if not url:
                events.append({"server": name, "action": "skip",
                               "reason": f"no hub url resolvable ({how}); hub not running or not published"})
                continue
            if not alive(url):
                events.append({"server": name, "action": "skip",
                               "reason": f"hub url {url} from {how} refused a connection (stale or down)"})
                continue
            extra[name] = {"type": "http", "url": url}
            events.append({"server": name, "action": "include", "reason": f"hub url {url} from {how}, live"})
            events.append({"server": name, "action": "warn", "reason": HUB_BINDING_NOTE})
            continue
        if typ == "stdio":
            if not stdio_runnable(entry.get("command"), which):
                events.append({"server": name, "action": "skip",
                               "reason": f"stdio command {entry.get('command')!r} is not a runnable executable"})
                continue
            extra[name] = _clean(entry)
            events.append({"server": name, "action": "include", "reason": "stdio command is runnable"})
        elif typ in ("http", "sse"):
            url = entry.get("url")
            if not isinstance(url, str) or not url:
                events.append({"server": name, "action": "skip", "reason": "no url to connect to"})
            elif not alive(url):
                events.append({"server": name, "action": "skip", "reason": f"url {url} refused a connection"})
            else:
                extra[name] = _clean(entry)
                events.append({"server": name, "action": "include", "reason": "url is live"})
        else:
            events.append({"server": name, "action": "skip", "reason": f"unknown server type {typ!r}"})
    return extra, events, names

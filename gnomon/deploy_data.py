"""DEPLOY-root DATA: the one place a deployment's role-keyed defaults are read from (X13 S5-3).

Gnomon ships NEUTRAL built-in defaults. Anything that names a deployment (a role id, a corpus id, a service label, a
tab, a brand) lives as DATA in the deployment, under ``<deploy_root>/defaults/<name>.json`` (``verb-policy.toml`` for
the verb policy), and is read through this module. The directory is MACHINE-level and deliberately NOT under
``workspaces/**``: several of these tables feed machine safety paths, and I6 forbids those from opening
``workspaces/**``.

LAZY BY CONSTRUCTION (g47b / g67). Importing this module defines constants and functions only; nothing touches the
filesystem or the config until a function is CALLED. Hook-closure modules import it inside the function that needs it,
never at module level.

NEVER WRITES. ``load_json`` / ``read_bytes`` only read; the installer (not the resolver) creates the directory.

A table that is ABSENT is the neutral default (``None``), silently and by design (a fresh install has no deployment
data). A table that is PRESENT but unusable (not a regular file, over the size cap, not UTF-8, not JSON, not an
object) is also treated as absent so a hand-edit never bricks a launch, BUT the fault is recorded: ``errors()``
returns it and every consumer that reports status should surface it (an errored read and an absent one must never
look the same). The verb policy is the exception: it fails CLOSED (see ``harness.verb_policy``).

EXTENSION POINT (g16): a new role-keyed table, default corpus/MCP/service id or brand value is added here as a NEW
``<name>.json`` read through :func:`load_json` with a neutral built-in default, never as a literal in the package,
and gets a line in ``registry_snapshots()`` (tests/neutrality_patterns.py).
"""

from __future__ import annotations

import json
import os
import re
import stat
import threading
from typing import Any, Dict, List, Optional, Tuple

#: ``<deploy_root>/defaults/``: machine-level deployment data (never under ``workspaces/``: I6).
DATA_DIRNAME = "defaults"
#: a data file larger than this is treated as unusable (the tables are small; a runaway file must not be slurped).
MAX_BYTES = 256 * 1024
_NAME_RE = re.compile(r"\A[a-z][a-z0-9-]{0,63}\Z")

#: FAILURE CLASS of every deploy-data table (g68-amendment, X13 S5-4b round 2). ``fail-closed``: the table gates a SAFETY
#: or EVIDENCE path, so a present-but-unusable file REFUSES that feature and names the file (the consumer contains the
#: refusal at its own handler boundary: g70). ``degrade-visibly``: a PRESENTATION table; a fault falls back to the neutral
#: built-in and is NEVER silent: it is recorded (:func:`errors`), reported once per process on stderr, and surfaced by
#: :func:`fault_summary` (the web ``/api/health`` ``deploy_data_faults`` key). A NEW table declares its class HERE at
#: creation (a test scans the package for table names and fails on an undeclared one), and in ``registry_snapshots()``.
FAIL_CLOSED = "fail-closed"
DEGRADE_VISIBLY = "degrade-visibly"
TABLE_CLASSES: Dict[str, str] = {
    "verb-policy": FAIL_CLOSED,             # who may act (harness.verb_policy refuses a bad file)
    "dsd-corpus": FAIL_CLOSED,              # where decisions are written (dsd.live.DsdCorpusError)
    "role-labels": DEGRADE_VISIBLY,
    "post-compact-boot": DEGRADE_VISIBLY,
    "corpus-tabs": DEGRADE_VISIBLY,
    "optimizer": DEGRADE_VISIBLY,
    "services": DEGRADE_VISIBLY,            # an empty registry is safe by construction (the restart verb is hidden)
    "legacy-session-roots": DEGRADE_VISIBLY,
    "document-roots": DEGRADE_VISIBLY,      # extra allow-listed roots for the DSD Documents / Open policy; absent = NONE extra (narrower, safe)
    "apps-host": DEGRADE_VISIBLY,           # the Apps host's launchd label prefix; a bad value falls back to the neutral prefix visibly
    "open-work": DEGRADE_VISIBLY,           # the workspace default reminder interval; a bad value falls back to 60 visibly
    "status-setters": FAIL_CLOSED,          # who may declare the operator's status (a bad table = nobody)
    "folder-names": DEGRADE_VISIBLY,        # the deployment's names for the sessions / agents / transcript-archive folders (read by roots.py itself: deploy_data would recurse)
    "branding": DEGRADE_VISIBLY,            # the header logo (round 5 item 8); a bad or refused table means NO logo (the empty span), fault recorded
}

_lock = threading.Lock()
_cache: Dict[str, Tuple[Tuple[int, int], Any]] = {}     # path -> ((mtime_ns, size), parsed)
_errors: Dict[str, str] = {}                            # path -> last fault ("" never stored)
_reported: set = set()                                  # (path, fault) already said on stderr


def table_class(name: str) -> str:
    """The declared failure class of ``name`` (without extension). An undeclared name is ``degrade-visibly`` at run time
    (never fail-open silently), and a test fails on it so the declaration is made at creation."""
    return TABLE_CLASSES.get(name, DEGRADE_VISIBLY)


def _class_of_path(path: str) -> str:
    base = os.path.basename(path)
    return table_class(base.rsplit(".", 1)[0])


def _say_once(path: str, fault: str) -> None:
    """A fault in a PRESENTATION table is never silent: one stderr line per (file, fault) per process."""
    if _class_of_path(path) != DEGRADE_VISIBLY or fault == "absent":
        return
    key = (path, fault)
    with _lock:
        if key in _reported:
            return
        _reported.add(key)
    import sys

    print(f"[gnomon deploy-data] {path}: {fault}; presentation table, using the neutral built-in", file=sys.stderr)


def fault_summary() -> List[Dict[str, str]]:
    """Every recorded fault as ``{"file", "fault", "class"}`` (sorted by file): the provenance surface (g68-amendment)."""
    with _lock:
        items = sorted(_errors.items())
    return [{"file": p, "fault": f, "class": _class_of_path(p)} for p, f in items if f and f != "absent"]


def deploy_root() -> str:
    """The DEPLOY root: the ONE seam for deployment data (tests patch THIS attribute; the real chain is
    :func:`_resolve_root`)."""
    return _resolve_root()


def _resolve_root() -> str:
    """The real resolution: ``ConfigStore().load().resolved_deploy_root()``, which follows an explicit ``deploy_root``
    config key, else the per-user roots pointer (:mod:`gnomon.roots`, X13 S5-4) when one is in play, else the
    legacy default. LAZY (nothing at import). Returns ``""`` when it cannot be resolved, which every reader treats as
    "no deployment data"."""
    try:
        from .config import ConfigStore

        return ConfigStore().load().resolved_deploy_root()
    except Exception:  # noqa: BLE001 - an unreadable config means no deployment data, never a crash in a hook path
        return ""


def data_dir(root: Optional[str] = None) -> str:
    r = deploy_root() if root is None else root
    return os.path.join(r, DATA_DIRNAME) if r else ""


def data_path(filename: str, root: Optional[str] = None) -> str:
    """``<deploy_root>/defaults/<filename>`` (``""`` when no root resolves). ``filename`` is a bare file name."""
    d = data_dir(root)
    return os.path.join(d, filename) if d else ""


def errors() -> Dict[str, str]:
    """A copy of the faults recorded by reads of PRESENT-but-unusable data files (path -> reason)."""
    with _lock:
        return dict(_errors)


def record_fault(name: str, message: str, root: Optional[str] = None) -> None:
    """Record a SEMANTIC fault in a table that parsed fine (a field of the wrong shape): the consumer ignores the field
    and falls back to the neutral default, and the fault stays visible through :func:`errors`."""
    path = data_path(name + ".json", root) or name
    with _lock:
        _errors[path] = message
    _say_once(path, message)


def clear_fault(name: str, root: Optional[str] = None) -> None:
    """Forget a recorded fault for table ``name`` (round 7 item 5, P3-6): a consumer that finds its table usable again calls this, so a repaired
    table does not keep reporting the fault it had. Also lets the same fault be said again if it recurs. No-op when none is recorded."""
    path = data_path(name + ".json", root) or name
    with _lock:
        _errors.pop(path, None)
        for key in [k for k in _reported if isinstance(k, tuple) and k and k[0] == path]:
            _reported.discard(key)


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()
        _errors.clear()
        _reported.clear()


def read_bytes(path: str) -> Tuple[Optional[bytes], str]:
    """``(bytes, "")`` | ``(None, "absent")`` | ``(None, "<reason>")``. Never raises. A FIFO / device / directory is
    refused without blocking (O_NONBLOCK + fstat)."""
    if not path:
        return None, "absent"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, "absent"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, "not a regular file"
        if st.st_size > MAX_BYTES:
            return None, f"larger than {MAX_BYTES} bytes"
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(MAX_BYTES + 1), ""
    except OSError as exc:
        return None, f"unreadable: {exc}"
    finally:
        os.close(fd)


def load_json(name: str, root: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The deployment table ``<deploy_root>/defaults/<name>.json`` as a dict, or ``None`` (absent = the neutral
    default; unusable = ``None`` plus a recorded fault, see :func:`errors`). ``name`` is validated (no path
    separators). Cached per (path, mtime, size), so an edit is picked up on the next call, a long-running web
    process included."""
    if not isinstance(name, str) or not _NAME_RE.match(name):
        raise ValueError(f"invalid deployment data name {name!r}")
    path = data_path(name + ".json", root)
    if not path:
        return None
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            _cache.pop(path, None)
            _errors.pop(path, None)
        return None
    with _lock:
        hit = _cache.get(path)
        if hit is not None and hit[0] == key:
            return hit[1]
    raw, why = read_bytes(path)
    value: Optional[Dict[str, Any]] = None
    fault = ""
    if raw is None:
        fault = why
    else:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                value = parsed
            else:
                fault = "top level is not a JSON object"
        except (UnicodeDecodeError, ValueError) as exc:
            fault = f"not valid UTF-8 JSON: {exc}"
    with _lock:
        _cache[path] = (key, value)
        if fault:
            _errors[path] = fault
        else:
            _errors.pop(path, None)
    if fault:
        _say_once(path, fault)
    return value


def section(name: str, key: Optional[str] = None, root: Optional[str] = None) -> Any:
    """``load_json(name)`` (or one top-level ``key`` of it); ``None`` when the table or the key is absent."""
    data = load_json(name, root)
    if data is None:
        return None
    return data if key is None else data.get(key)


def legacy_session_roots(root: Optional[str] = None) -> Tuple[str, ...]:
    """The deployment's LEGACY session sources (``defaults/legacy-session-roots.json``, ``{"roots": ["~/path", ...]}``):
    older session layouts the usage index and the transcript archive still scan. Each is expanded against HOME when
    read. Neutral default: none (X13 S5-4, survey row 23: Gnomon has no runtime dependency on any earlier tool's layout).
    A malformed table (not a list, a non-string / empty / relative entry) is ignored entry by entry and recorded."""
    raw = section("legacy-session-roots", "roots", root)
    if raw is None:
        return ()
    if not isinstance(raw, list):
        record_fault("legacy-session-roots", "'roots' must be a list of paths; ignored", root)
        return ()
    out: List[str] = []
    for v in raw:
        if isinstance(v, str) and v.strip() and "\0" not in v and (v.startswith("~") or os.path.isabs(v)):
            out.append(os.path.abspath(os.path.expanduser(v)))
        else:
            record_fault("legacy-session-roots", f"entry {v!r} must be an absolute or ~-prefixed path; skipped", root)
    return tuple(out)


def str_map(value: Any) -> Dict[str, str]:
    """A ``{str: str}`` view of ``value`` (non-string entries dropped); ``{}`` for anything that is not an object.
    Display tables are cosmetic: a bad row is skipped, never an error."""
    if not isinstance(value, dict):
        return {}
    return {k: v for k, v in value.items() if isinstance(k, str) and k and isinstance(v, str)}


def str_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [v for v in value if isinstance(v, str) and v]

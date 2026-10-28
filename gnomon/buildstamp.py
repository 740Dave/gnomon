"""Build/version stamp.

The Gnomon tree has no git, so "which code is this process running?" had no
answer beyond mtimes. This module computes a deterministic BUILD ID for a source
tree: sha256 over the SORTED ``(relative posix path, sha256(content))`` pairs of the
files in a named SCOPE — one id per component, compared like with like:

* ``session`` — the modules the HARNESS PROCESS actually loads, derived by
  importing ``gnomon.harness.launcher`` in a child ``python -B`` and reading
  ``sys.modules`` (+ the entry's startup imports) — the SAME derivation as
  ``tools/golive_smoke.py``'s harness-code check, via the shared
  :mod:`gnomon.importscope` (build BSTAMP 2026-09-30). Web-server-only code
  (``dsd/*``, ``app/*``) is NOT in it, so a server-only edit no longer marks every
  session stale. Build BSEED (2026-09-30, audit-BSTAMP N1/N2) widened it to every
  FUNCTION-LEVEL import reachable from the harness (``notice_inbox.offer`` ->
  ``dsd.notice``, ``memory.remember`` -> ``app.graph_viewer``, …) and to the
  session-lifetime ``python -m`` processes ``harness.self_mcp`` and
  ``harness.headless``; per-call hook processes (``deny_guard``, ``*_hook``) are
  always fresh from disk and stay out. This is the id stamped into ``launch.json``
  (``build_id`` + ``build_scope: session`` + ``build_scope_version: 3``). If the import probe
  fails the id is NOT computed (``RuntimeError``) — there is no fallback set, so
  two stampers can never derive it two different ways and read as "stale".
* ``server``  — the package plus every ``.py`` under ``web/backend/`` (what the
  :8770 process runs). Unchanged by BSTAMP.

Stamp versions: a session stamp whose ``build_scope_version`` is not today's (3)
was hashed over a different file set — v1 the WHOLE package (rm1a2), v2
the pre-BSEED harness set — and is not comparable to today's session id;
:func:`stamped_build_id` reads it as unknown (None), never a false "stale".

``web/static`` is in NEITHER: static files are served from disk per request, so a
browser reload picks up an edit without any restart (audit fix pass rm1a2, which
replaced the first cut's single mixed-scope id).

Excluded everywhere: any path segment containing ``.bak-`` (the synced-tree
backup convention), ``__pycache__``, any ``tests``/``test`` directory, ``test_*.py``
/ ``*_test.py`` files, and hidden (dot) files such as ``.DS_Store``.

The id is content-only: an mtime touch does not change it, a rename or an edit
does. It is location-independent (relative paths), so two copies of one tree agree.

Three read paths:

* :func:`compute_build` — uncached; raises ``FileNotFoundError`` when the root has
  no ``gnomon/`` package (fail loud, never an empty-tree hash).
* :func:`build_info` — cached for the life of the PROCESS: "the code this process
  loaded" (call it at startup; later edits do not change it).
* :func:`current_build` — the tree ON DISK now, recomputed at most every ``ttl``
  seconds (default 30).

:func:`launch_stamp` is the never-raises form written into ``launch.json``.
Pure stdlib; no app-layer imports.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import importscope

#: the source tree root (the directory holding ``gnomon/`` and ``web/``).
DEFAULT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SESSION, SERVER = "session", "server"

#: scope name -> ((subdir, suffix-or-None), ...). None = every file. The SESSION
#: scope is not a directory walk: it is DERIVED (see :func:`_session_files`).
SCOPES: Dict[str, Tuple[Tuple[str, Optional[str]], ...]] = {
    SESSION: (),
    SERVER: (("gnomon", ".py"), ("web/backend", ".py")),
}

#: the stamp format of each scope. A stamp whose ``build_scope_version`` (absent =
#: 1) differs was computed over a different file set and is not comparable.
#: session v1 = every package.py (/rm1a2); v2 = harness-loaded modules (BSTAMP);
#: v3 = v2 + function-level imports + session-process entries (BSEED).
SCOPE_VERSION: Dict[str, int] = {SESSION: 3, SERVER: 1}

ID_LEN = 12
CURRENT_TTL_S = 30.0

_EXCLUDED_DIRS = {"__pycache__", "tests", "test"}


def _excluded_name(name: str) -> bool:
    return name.startswith(".") or ".bak-" in name


def _excluded_file(name: str) -> bool:
    if _excluded_name(name):
        return True
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def _scope(scope: str) -> Tuple[Tuple[str, Optional[str]], ...]:
    try:
        return SCOPES[scope]
    except KeyError:
        raise ValueError(f"buildstamp: unknown scope {scope!r} (known: {sorted(SCOPES)})") from None


def iter_build_files(root: Optional[str] = None, *, scope: str = SESSION) -> List[Tuple[str, str]]:
    """Sorted ``(relpath, abspath)`` of every in-scope file. Raises
    ``FileNotFoundError`` when ``<root>/gnomon`` is not a directory and
    ``ValueError`` for an unknown scope."""
    spec = _scope(scope)
    root = os.path.abspath(root or DEFAULT_ROOT)
    pkg = os.path.join(root, "gnomon")
    if not os.path.isdir(pkg):
        raise FileNotFoundError(f"buildstamp: no gnomon/ package under {root}")
    if scope == SESSION:
        return _session_files(root)
    out: List[Tuple[str, str]] = []
    for sub, suffix in spec:
        base = os.path.join(root, *sub.split("/"))
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _EXCLUDED_DIRS and not _excluded_name(d)]
            for name in filenames:
                if _excluded_file(name) or (suffix is not None and not name.endswith(suffix)):
                    continue
                full = os.path.join(dirpath, name)
                if not os.path.isfile(full):
                    continue
                rel = os.path.relpath(full, root).replace(os.sep, "/")
                out.append((rel, full))
    out.sort()
    return out


def _session_files(root: str) -> List[Tuple[str, str]]:
    """Sorted ``(relpath, abspath)`` of the modules the harness process loads —
    ``importscope.harness_scope(strict=True)``: raises ``RuntimeError`` when the
    child import probe fails (never a guessed, over-inclusive set)."""
    files, _note = importscope.harness_scope(root, strict=True)
    out = [(os.path.relpath(f, root).replace(os.sep, "/"), f) for f in files if os.path.isfile(f)]
    if not out:
        raise RuntimeError(f"buildstamp: the harness import probe loaded no in-tree modules under {root}")
    out.sort()
    return out


def compute_build(root: Optional[str] = None, *, scope: str = SESSION) -> Dict[str, Any]:
    """``{build_id, scope, computed_at, file_count, newest_mtime}`` for ``root``."""
    files = iter_build_files(root, scope=scope)
    outer = hashlib.sha256()
    newest = None
    for rel, full in files:
        with open(full, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        outer.update(rel.encode("utf-8") + b"\0" + digest.encode("ascii") + b"\n")
        m = os.path.getmtime(full)
        newest = m if newest is None or m > newest else newest
    return {
        "build_id": outer.hexdigest()[:ID_LEN],
        "scope": scope,
        "scope_version": SCOPE_VERSION[scope],
        "computed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file_count": len(files),
        "newest_mtime": newest,
    }


_lock = threading.Lock()
_process_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}
_current_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, Any]]] = {}
_current_inflight: Dict[Tuple[str, str], threading.Event] = {}


def _key(root: Optional[str], scope: str) -> Tuple[str, str]:
    _scope(scope)
    return os.path.abspath(root or DEFAULT_ROOT), scope


def build_info(root: Optional[str] = None, *, scope: str = SESSION) -> Dict[str, Any]:
    """The build id of the tree as first seen by THIS process (cached forever)."""
    k = _key(root, scope)
    with _lock:
        hit = _process_cache.get(k)
    if hit is not None:
        return hit
    info = compute_build(k[0], scope=scope)
    with _lock:
        return _process_cache.setdefault(k, info)


def current_build(root: Optional[str] = None, *, scope: str = SESSION, ttl: float = CURRENT_TTL_S,
                  clock: Callable[[], float] = time.monotonic) -> Dict[str, Any]:
    """The build id of the tree on disk NOW, recomputed at most every ``ttl`` s.

    SINGLE-FLIGHT (poll-pileup fix, 2026-10-04): the SESSION-scope compute is an AST import-closure walk
    (~2 s of pure-Python CPU that holds the GIL). It used to be recomputed by EVERY thread that saw the
    expired entry at once (a stampede every 30 s on a polled endpoint). Now exactly one thread computes
    per key. Others: if a previous value exists they return it at once (stale-while-revalidate: at most
    one compute-time older than the TTL); if there is none yet they wait for the one in-flight compute.
    A failed compute is raised to the thread that ran it and is not cached; the next caller retries."""
    k = _key(root, scope)
    while True:
        now = clock()
        with _lock:
            hit = _current_cache.get(k)
            if hit is not None and now - hit[0] < ttl:
                return hit[1]
            flight = _current_inflight.get(k)
            if flight is None:
                flight = threading.Event()
                _current_inflight[k] = flight
                mine = True
            else:
                mine = False
        if mine:
            try:
                info = compute_build(k[0], scope=scope)
                with _lock:
                    _current_cache[k] = (clock(), info)
                return info
            finally:
                with _lock:
                    _current_inflight.pop(k, None)
                flight.set()
        if hit is not None:
            return hit[1]            # stale-while-revalidate: someone else is already recomputing
        flight.wait()                # first ever value: share the one in-flight compute, then re-check


def _reset_caches_for_tests() -> None:
    with _lock:
        _process_cache.clear()
        _current_cache.clear()
        _current_inflight.clear()


def launch_stamp(root: Optional[str] = None) -> Dict[str, Any]:
    """The launch.json keys (SESSION scope). Computed FRESH (the new session will
    import the tree as it is on disk now). Never raises: a failure is recorded as
    ``build_error`` with ``build_id: None`` so the chip reads "unknown build", never
    a false "current"."""
    try:
        info = compute_build(root, scope=SESSION)
    except Exception as exc:  # noqa: BLE001 - a stamp failure must never block a launch
        return {"build_id": None, "build_scope": SESSION,
                "build_scope_version": SCOPE_VERSION[SESSION],
                "build_error": f"{type(exc).__name__}: {exc}"}
    return {"build_id": info["build_id"], "build_scope": SESSION,
            "build_scope_version": SCOPE_VERSION[SESSION], "build_stamp": info}


def stamped_build_id(rec: Any, scope: str) -> Optional[str]:
    """The ``build_id`` of a stamp record (a launch.json, a /api/health body) when it
    was stamped in ``scope`` AT TODAY'S scope version; else None ("unknown").

    A missing ``build_scope`` (first-cut 1A, mixed scope) or an older
    ``build_scope_version`` (absent = 1; a pre-BSTAMP session stamp hashed the whole
    package) is not comparable to today's id — reading it as a build id would turn
    every such session into a false "stale"."""
    if not isinstance(rec, dict) or rec.get("build_scope") != scope:
        return None
    if rec.get("build_scope_version", 1) != SCOPE_VERSION.get(scope):
        return None
    bid = rec.get("build_id")
    return bid if isinstance(bid, str) and bid else None


def read_session_build_id(session_dir: str) -> Optional[str]:
    """The SESSION-scoped ``build_id`` recorded in ``<session_dir>/.claude/launch.json``.

 None when absent (every earlier launch), unreadable, not a string, or not
    ``build_scope: session`` at today's ``build_scope_version`` — a first-cut 1A stamp
    (no ``build_scope``, mixed scope incl. web/static), a pre-BSTAMP session stamp
    (whole package) or a pre-BSEED v2 stamp is not comparable to today's session id, so it reads "unknown",
    never a false "current" or "stale" (see :func:`stamped_build_id`)."""
    try:
        with open(os.path.join(session_dir, ".claude", "launch.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    return stamped_build_id(cfg, SESSION)


def build_state(session_build_id: Optional[str], current_build_id: Optional[str]) -> str:
    """``current`` / ``stale`` / ``unknown`` (either side unknown)."""
    if not session_build_id or not current_build_id:
        return "unknown"
    return "current" if session_build_id == current_build_id else "stale"

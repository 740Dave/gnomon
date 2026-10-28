"""The ONE per-user root resolver: ``roots.toml`` (X13 S5-4; s5-0-rulings-2026-10-01 Q2).

Gnomon needs to find its DATA root (sessions, config.json, metrics: the machine's runtime state) and its DEPLOY root
(the deployment's data: ``defaults/``, ``machine.json``, ``workspaces/``) BEFORE it can read any config that lives in
them. One small per-user POINTER file answers that::

    # ~/.config/gnomon/roots.toml      (override the path with the environment variable GNOMON_CONFIG)
    data_root   = "~/gnomon-data"
    deploy_root = "~/gnomon-deploy"

A machine that already has a runtime home points ``data_root`` AT it, so no session data moves (the S3
never-move-live-folders rule holds by construction).

RULES (each has a test):

* LAZY (g47b / g67). Importing this module touches nothing; every function reads the environment and the file when
  CALLED (mtime-cached), so a hook-closure module may import it inside a function.
* The RESOLVER NEVER WRITES: not at import, not on read. A missing file is not created; the INSTALLER writes it
  (:func:`write_roots_file`, the one explicit, never-overwriting primitive, called by nothing in this package).
* A missing file means the NEUTRAL defaults (``~/gnomon-data``, ``~/gnomon-deploy``), ``source == "default"``.
* A file that EXISTS but is unusable (not TOML, wrong types, an unknown key, a relative path, a NUL or newline) is a
  NAMED FAULT: ``Roots.errors`` is non-empty, ``source == "invalid"``, and the neutral defaults are returned for BOTH
  roots. It is never read as "no file", and a caller that must not guess (the config seam) checks ``errors``.
* Values are absolute paths or ``~``-prefixed (expanded against HOME when READ, never stored expanded).

``adopted()`` says whether a roots file is in play (``GNOMON_CONFIG`` set, or the default path exists). The config
seam (:mod:`gnomon.config`) follows this resolver only when it is adopted; until the installer has written a file,
nothing changes for a machine that has none.

TRANSITIONAL GATE (Diamond F3, X13 S5-4b): gating adoption on the file's PRESENCE is NOT in the s5-0 Q2 ruling (Q2 says a missing file is written by the installer with the neutral defaults); it is a builder's invention kept because there is
STILL NO INSTALLER (:func:`write_roots_file` has no caller anywhere in this tree, and no installer path writes
``roots.toml``), so removing the gate would silently move every machine without a file to ``~/gnomon-data``. REMOVAL
CONDITION: an installer path exists that writes ``roots.toml``. REMOVAL OWNER: the slice that builds that installer
(S3 / the install step of the S5 port; NOT yet assigned: proposed owner for the architect to confirm, with "gate and
legacy constants (``config.DEFAULT_RUNTIME_ROOT`` / ``DEFAULT_DEPLOY_ROOT``, ``_adopted_roots``'s ``adopted()`` test)
removed" added to S5-10's acceptance, per Diamond F3). Until then the S5-4 acceptance "grep-clean of the legacy data
root" holds for every file except ``config.py`` (the one legacy constant).

EXTENSION POINT (g16): a new per-user root (a third pointer) is a new key here, validated here, exposed by a new
accessor here, with a test; never a new environment variable or an inline default in a consumer.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple

ENV_CONFIG = "GNOMON_CONFIG"
#: relative to HOME
DEFAULT_CONFIG_RELPATH = os.path.join(".config", "gnomon", "roots.toml")
NEUTRAL_DATA_ROOT = "~/gnomon-data"
NEUTRAL_DEPLOY_ROOT = "~/gnomon-deploy"
#: The three FOLDER keys (round-2 ruling 2026-10-25, D55 split; checklist g127: a closed list read in several places lives in ONE place).
#: Each names the folder where that kind of data lives. Unset, it is ``<data_root>/<neutral name>`` (a new install changes nothing);
#: a machine whose existing folders carry another name sets the key in roots.toml. No module outside this one joins these folder
#: names onto a root (``tests/test_x13s3_folder_roots.py`` fails if one does).
FOLDER_KEYS = ("sessions_root", "agents_root", "transcript_archive_root")
NEUTRAL_FOLDERS = {"sessions_root": "sessions", "agents_root": "agents", "transcript_archive_root": "transcript-archive"}
#: A folder key a roots file does NOT set takes its name from the DEPLOYMENT's layout table, ``<deploy_root>/defaults/folder-names.json``
#: (``{"sessions_root": "<name>", ...}``, any subset), so a machine whose existing layout carries other names is resolved exactly as it
#: was before these keys existed, in EVERY state of its roots file (round-2 fix, P0-1). The neutral names above apply only when there is
#: no such table (a fresh neutral install). Read here, not through ``deploy_data``: that module resolves its root through the config seam,
#: which resolves through this module. A table that is present but unusable is ignored and the fault is left in ``Roots.notes``.
FOLDER_NAMES_TABLE = os.path.join("defaults", "folder-names.json")
_NAME_OK = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
KEYS = ("data_root", "deploy_root") + FOLDER_KEYS
MAX_BYTES = 16 * 1024

_lock = threading.Lock()
_cache: Dict[str, Tuple[Tuple[int, int], "Roots", Optional[Tuple[int, int]]]] = {}


class FolderConflictError(RuntimeError):
    """Both the previous release's folder and the neutral one exist, both hold data, and no layout table says which is meant: a reader that needs
    that folder FAILS with this (naming both paths) instead of guessing. Raised by the folder accessors (here and in ``config``), never by :func:`load`."""


@dataclass(frozen=True)
class Roots:
    """The resolved roots. ``data_root`` / ``deploy_root`` are ABSOLUTE (``~`` expanded against the HOME in force when
    they were read)."""

    data_root: str
    deploy_root: str
    #: ``"file"`` (read from ``config_path``) | ``"default"`` (no file: the neutral defaults) | ``"invalid"``
    source: str
    config_path: str
    errors: Tuple[str, ...] = field(default=())
    #: the folder roots (absolute); "" means the neutral default under ``data_root`` (filled in by ``__post_init__``)
    sessions_root: str = ""
    agents_root: str = ""
    transcript_archive_root: str = ""
    #: faults that do NOT invalidate the roots file (an unusable layout table): named, never silent (``config`` reports them once)
    notes: Tuple[str, ...] = field(default=())
    #: ``((folder key, message), ...)`` for a folder that cannot be told apart (see :class:`FolderConflictError`)
    conflicts: Tuple[Tuple[str, str], ...] = field(default=())

    def folder(self, key: str) -> str:
        """The folder for ``key``; raises :class:`FolderConflictError` when it is ambiguous."""
        for k, msg in self.conflicts:
            if k == key:
                raise FolderConflictError(msg)
        return getattr(self, key)

    def __post_init__(self) -> None:
        for k in FOLDER_KEYS:
            if not getattr(self, k):
                object.__setattr__(self, k, folder_under(k, self.data_root))

    @property
    def ok(self) -> bool:
        return not self.errors


def folder_under(key: str, base: str, name: Optional[str] = None) -> str:
    """The ONE place a folder key is joined onto a root: ``<base>/<name or the neutral name>``. A caller with a LEGACY name (the
    transitional no-roots-file mode of ``config``) passes it; nothing else spells or joins these folder names."""
    return os.path.join(base, name or NEUTRAL_FOLDERS[key])


def _home(home: Optional[str]) -> str:
    return os.path.abspath(os.path.expanduser(home if home is not None else "~"))


def config_path(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> str:
    """The pointer file's path: ``$GNOMON_CONFIG`` (expanded) if set and non-empty, else ``<HOME>/.config/gnomon/roots.toml``."""
    e = os.environ if env is None else env
    override = e.get(ENV_CONFIG)
    if override:
        h = _home(home)
        return os.path.abspath(h + override[1:] if override.startswith("~") else override)
    return os.path.join(_home(home), DEFAULT_CONFIG_RELPATH)


def adopted(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> bool:
    """True when a roots file is in play: ``GNOMON_CONFIG`` is set, or the default file exists. Reads nothing else."""
    e = os.environ if env is None else env
    if e.get(ENV_CONFIG):
        return True
    return os.path.isfile(os.path.join(_home(home), DEFAULT_CONFIG_RELPATH))


def _expand(value: str, home: str) -> str:
    return os.path.abspath(home + value[1:] if value.startswith("~") else value)


def _neutral(home: str, path: str, source: str, errors: Tuple[str, ...] = ()) -> Roots:
    return Roots(_expand(NEUTRAL_DATA_ROOT, home), _expand(NEUTRAL_DEPLOY_ROOT, home), source, path, errors)


def _read(path: str) -> Tuple[Optional[bytes], str]:
    """``(bytes, "")`` | ``(None, "absent")`` | ``(None, reason)``. A FIFO / directory is refused without blocking."""
    import stat

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


def _table_key(deploy_root: str) -> Optional[Tuple[int, int]]:
    try:
        st = os.stat(os.path.join(deploy_root, FOLDER_NAMES_TABLE))
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _is_empty_dir(path: str) -> bool:
    """A folder with nothing in it (OS metadata ``.DS_Store`` / ``._*`` do not count)."""
    try:
        return not [n for n in os.listdir(path) if n != ".DS_Store" and not n.startswith("._")]
    except OSError:
        return False


def _legacy_fallback(key: str, data_root: str, notes: Tuple[str, ...]) -> Tuple[Optional[str], Tuple[str, ...], Optional[str]]:
    """WRONG-LAND-ORDER SAFETY (Diamond P2-F1, F1 of the second delta): a roots file that sets no folder key, with NO layout table, on a machine whose data root
    already holds the previous release's folder. ``(folder or None, notes, conflict message or None)``.
    * only the legacy folder exists: use it (note names the missing table);
    * both exist and the neutral one is EMPTY: use the legacy one (same note) - a stray empty folder is not a decision;
    * both exist and both hold data: no guess - a conflict message naming both paths (the folder accessors raise on it);
    * the neutral one only, or neither: the neutral name (a fresh install).
    The previous release's names are ``config.LEGACY_FOLDERS`` (the one transitional list). The note is reported once PER PROCESS (so every hook process
    prints it too in this state: noise, never a block)."""
    from .config import LEGACY_FOLDERS

    legacy = os.path.join(data_root, LEGACY_FOLDERS[key])
    neutral = folder_under(key, data_root)
    if legacy == neutral or not os.path.isdir(legacy):
        return None, notes, None
    if os.path.lexists(neutral) and not (os.path.isdir(neutral) and _is_empty_dir(neutral)):
        if _is_empty_dir(legacy):
            return None, notes, None                                  # the legacy folder is the empty one: the neutral folder is in use
        return None, notes, (f"cannot tell which folder holds the data for {key}: BOTH {legacy} and {neutral} exist and both hold data, and there is no folder-name table at "
                             f"<deploy_root>/{FOLDER_NAMES_TABLE} to say which is meant. Install the table (or move one folder away); nothing is guessed.")
    return legacy, notes + (f"no folder-name table at <deploy_root>/{FOLDER_NAMES_TABLE}: using the existing folder {legacy} for {key} (install the table BEFORE the "
                            f"code that reads the folder keys; restart long-lived processes after it lands)",), None


def folder_names(deploy_root: str) -> Tuple[Dict[str, str], Tuple[str, ...]]:
    """``({folder key: name}, notes)`` from the deployment's layout table. Absent table: ``({}, ())`` (the neutral names apply)."""
    import json

    path = os.path.join(deploy_root, FOLDER_NAMES_TABLE)
    raw, why = _read(path)
    if raw is None:
        return {}, (() if why == "absent" else (f"{path}: {why}",))
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return {}, (f"{path}: not UTF-8 JSON: {exc}",)
    if not isinstance(data, dict):
        return {}, (f"{path}: not an object",)
    names: Dict[str, str] = {}
    notes = []
    for k, v in data.items():
        if k not in FOLDER_KEYS:
            notes.append(f"{path}: unknown key {k!r}")
        elif not isinstance(v, str) or not _NAME_OK.match(v) or v in (".", ".."):
            notes.append(f"{path}: {k} must be a plain folder name (no slash), got {v!r}")
        else:
            names[k] = v
    return names, tuple(notes)


def _check(key: str, value: object) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return f"{key} must be a non-empty string"
    if "\0" in value or "\n" in value or "\r" in value:
        return f"{key} must not contain a NUL or a newline"
    if not (value.startswith("~") or os.path.isabs(value)):
        return f"{key} must be an absolute path or start with '~' (got a relative path)"
    return None


def load(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> Roots:
    """Resolve the roots NOW (never raises, never writes). See the module doc for the missing / invalid cases."""
    import tomllib

    h = _home(home)
    path = config_path(env, home)
    try:
        st = os.stat(path)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        with _lock:
            _cache.pop(path + "|" + h, None)
        return _neutral(h, path, "default")
    ck = path + "|" + h
    with _lock:
        hit = _cache.get(ck)
        if hit is not None and hit[0] == key and _table_key(hit[1].deploy_root) == hit[2]:     # a table that appears or changes later is noticed
            return hit[1]
    raw, why = _read(path)
    if raw is None:
        res = _neutral(h, path, "default" if why == "absent" else "invalid",
                       () if why == "absent" else (f"{path}: {why}",))
    else:
        errs = []
        data: Mapping[str, object] = {}
        try:
            data = tomllib.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            errs.append(f"{path}: not valid UTF-8 TOML: {exc}")
        unknown = sorted(k for k in data if k not in KEYS)
        if unknown:
            errs.append(f"{path}: unknown key(s) {unknown} (known: {list(KEYS)})")
        vals = {}
        for k in KEYS:
            if k in data:
                bad = _check(k, data[k])
                if bad:
                    errs.append(f"{path}: {bad}")
                else:
                    vals[k] = data[k]
        if errs:
            res = _neutral(h, path, "invalid", tuple(errs))
        else:
            data_root_ = _expand(vals.get("data_root", NEUTRAL_DATA_ROOT), h)
            deploy_root_ = _expand(vals.get("deploy_root", NEUTRAL_DEPLOY_ROOT), h)
            names, notes = folder_names(deploy_root_)
            folders: Dict[str, str] = {}
            conflicts: List[Tuple[str, str]] = []
            for k in FOLDER_KEYS:
                if k in vals:
                    folders[k] = _expand(vals[k], h)
                elif k in names:
                    folders[k] = folder_under(k, data_root_, names[k])
                else:
                    fb, notes, conflict = _legacy_fallback(k, data_root_, notes)
                    folders[k] = fb or folder_under(k, data_root_)
                    if conflict:
                        conflicts.append((k, conflict))
            res = Roots(data_root_, deploy_root_, "file", path, notes=notes, conflicts=tuple(conflicts), **folders)
    with _lock:
        _cache[ck] = (key, res, _table_key(res.deploy_root))
    return res


def data_root(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> str:
    return load(env, home).data_root


def deploy_root(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> str:
    return load(env, home).deploy_root


def sessions_root(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> str:
    return load(env, home).folder("sessions_root")


def agents_root(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> str:
    return load(env, home).folder("agents_root")


def transcript_archive_root(env: Optional[Mapping[str, str]] = None, home: Optional[str] = None) -> str:
    return load(env, home).folder("transcript_archive_root")


def reset_for_tests() -> None:
    with _lock:
        _cache.clear()


def write_roots_file(path: str, data_root: str = NEUTRAL_DATA_ROOT, deploy_root: str = NEUTRAL_DEPLOY_ROOT, *,
                     overwrite: bool = False, folders: Optional[Mapping[str, str]] = None) -> str:
    """THE INSTALLER'S PRIMITIVE: write a roots file (atomically, mode 0600). The only writer in this module, called by
    nothing here: the resolver never writes. Refuses to overwrite an existing file unless ``overwrite`` (a pointer file
    is the one thing that decides where a machine's data lives). Validates both values with the reader's own rules and
    raises ``ValueError`` BEFORE touching the disk. Returns the path written."""
    import json
    import tempfile

    folders = dict(folders or {})
    unknown = sorted(k for k in folders if k not in FOLDER_KEYS)
    if unknown:
        raise ValueError(f"unknown folder key(s) {unknown} (known: {list(FOLDER_KEYS)})")
    for k, v in (("data_root", data_root), ("deploy_root", deploy_root), *folders.items()):
        bad = _check(k, v)
        if bad:
            raise ValueError(bad)
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.lexists(path) and not overwrite:
        raise FileExistsError(f"{path} exists; refusing to overwrite a roots file (pass overwrite=True deliberately)")
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    body = ("# Gnomon per-user roots (X13 S5-4). Written by the installer; edited by the operator.\n"
            f"data_root = {json.dumps(data_root)}\n"
            f"deploy_root = {json.dumps(deploy_root)}\n"
            + "".join(f"{k} = {json.dumps(folders[k])}\n" for k in FOLDER_KEYS if k in folders))
    fd, tmp = tempfile.mkstemp(prefix=".roots-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    reset_for_tests()
    return path

"""The manifest pin and the code pin (requirement-pin-manifest / -pin-code / -reconfirm-shows-change;
the instruction to build both pins now; architect rulings R6, R11b; REDESIGNED in round 3 by
g73-amendment-4-2026-10-02 after two rounds of per-file holes in a Python import-graph pin).

THREAT FRAME (stated so it is not overclaimed): apps run as the same uid as the agents, and an agent with a shell can
run any code itself. The pin's purpose is that the host never runs unreviewed code ON THE operator'S BEHALF, and never lets
an agent WITHOUT a shell get code executed through the host. It is not a barrier against a same-uid agent with a shell.

* MANIFEST pin: the manifest's REAL path and the sha256 of its bytes at confirm. Launch and verification use the
  pinned path; ``apps.json`` is only an index (``refused(manifest-moved)``, F2).
* CODE pin (rule 2): a content hash of EVERY file under the app root, recursively, with NO exclusions: ``.git``,
  ``__pycache__/*.pyc``, dotfiles, ignored files, everything. A symlink is resolved and its TARGET's content is hashed
  (a symlinked directory is walked, cycles are cut). Git is never asked what is "clean" and NO git process is ever run
  by this module (a repo's own ``core.fsmonitor`` / filter config would execute on the operator's behalf, found in round 3).
  The app root = the manifest's ``app_root`` (relative to the manifest's directory), else the directory that holds a
  ``.git`` above the manifest, else the manifest's directory. It must contain the manifest, must not be ``/`` and must
  not contain the user's home.
* What lies OUTSIDE the root and may run is closed by rule, not by hashing it: the start/action/presence argv's
  program is an absolute interpreter outside the user's home (its content is pinned in ``extra``) or a file under the
  root, its script is a file under the root, a writes path is outside the root (:mod:`.rules`), and the path values
  that reach an argv are pinned by VALUE (``argv_paths``): changing one needs a re-confirm.
* VENV (ruling R11b): a path parameter named ``venv`` records its path, the realpath + sha256 of ``<venv>/bin/python``
  and of ``pyvenv.cfg``. Nothing is executed (the round-2 pin ran ``<venv>/bin/python --version`` on every poll).

Both pins are checked at EVERY start by the preflight (ruling R6) and, to report refused(...), by the status poll: a
poll only READS and HASHES files; it never runs a process.

Limits (OPEN, open-question-pin-scope): any file an app writes inside its own root at run time changes the hash
(refused(code-changed) at the next start: fail closed, and writes paths inside the root are refused at install);
executables a script launches INDIRECTLY are covered only if they are under the root; code the app loads from outside
the root after it starts (static assets, a sibling package above it) is not pinned; a root over :data:`MAX_TREE_FILES`
files or :data:`MAX_TREE_BYTES` bytes is refused at confirm (declare a narrower ``app_root``).
"""

from __future__ import annotations

import difflib
import hashlib
import os
import shutil
from typing import Any, Dict, List, Optional, Tuple

from . import rules as _rules
from .fsafe import open_append_fd, open_regular
from .manifest import Manifest

MAX_TREE_FILES = 20000
MAX_TREE_BYTES = 512 * 1024 * 1024
SNAPSHOT_MAX_FILE_BYTES = 256 * 1024
SNAPSHOT_MAX_TOTAL_BYTES = 16 * 1024 * 1024
DIFF_MAX_CHARS = 200_000
MANIFEST_SNAPSHOT_NAME = "__manifest__.json"
#: directories the re-confirm SNAPSHOT (a display aid, never the pin) does not copy: they would eat its budget
_SNAPSHOT_SKIP_DIRS = frozenset({".git", "node_modules", "__pycache__", ".venv", "venv"})
#: names never copied into the re-confirm snapshot (F15: app-dir secrets must not move into DATA)
_SECRETISH = (".env", ".pem", ".key", ".p12", ".pfx", "id_rsa", "id_ed25519", ".netrc", ".npmrc", ".pypirc",
              "credential", "cred", "secret", "token", "passw", "api_key", "apikey", "private", "keychain")
PIN_VERSION = 3


class PinError(RuntimeError):
    """The pin could not be computed or verified (named step)."""


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: str) -> str:
    h = hashlib.sha256()
    try:
        with open_regular(p) as fh:           # R3-F6: a FIFO/device is refused (PinError), never a blocking open
            while True:
                b = fh.read(1 << 20)
                if not b:
                    break
                h.update(b)
    except OSError as exc:
        raise PinError(f"cannot read {p}: {exc.strerror or exc}")
    return h.hexdigest()


_inside = _rules.inside


# --------------------------------------------------------------------------------------------------------
# scope (pure path/file reads; no process is run)
# --------------------------------------------------------------------------------------------------------

def _find_repo_top(d: str) -> Optional[str]:
    """The nearest ancestor (inclusive) that holds a ``.git`` entry, found by walking up. No git process."""
    cur = os.path.realpath(d)
    while True:
        if os.path.lexists(os.path.join(cur, ".git")):
            return cur
        parent = os.path.dirname(cur)
        if parent == cur:
            return None
        cur = parent


def _git_commit(top: str) -> str:
    """HEAD's commit id read from the files (``.git/HEAD`` + the ref), for the record only. '' when unreadable."""
    try:
        g = os.path.join(top, ".git")
        if os.path.isfile(g):                                     # a worktree/submodule: ``gitdir: <path>``
            with open_regular(g) as fh:
                line = fh.read().decode("utf-8", "replace").strip()
            if line.startswith("gitdir:"):
                g = os.path.realpath(os.path.join(top, line.split(":", 1)[1].strip()))
        with open_regular(os.path.join(g, "HEAD")) as fh:
            head = fh.read().decode("utf-8", "replace").strip()
        if not head.startswith("ref:"):
            return head
        ref = head.split(":", 1)[1].strip()
        try:
            with open_regular(os.path.join(g, ref)) as fh:
                return fh.read().decode("utf-8", "replace").strip()
        except OSError:
            with open_regular(os.path.join(g, "packed-refs")) as fh:
                for ln in fh.read().decode("utf-8", "replace").splitlines():
                    if ln.strip().endswith(" " + ref):
                        return ln.split()[0]
    except OSError:
        pass
    return ""


def app_root(manifest: Manifest, manifest_path: str) -> str:
    mdir = os.path.dirname(os.path.realpath(manifest_path))
    homes = _rules.user_homes()
    if manifest.app_root is not None:
        r = os.path.expanduser(manifest.app_root)
        root = os.path.realpath(r if os.path.isabs(r) else os.path.join(mdir, r))
        if not os.path.isdir(root):
            raise PinError(f"app_root {manifest.app_root!r} is not a directory")
    else:
        top = _find_repo_top(mdir)
        root = top if (top is not None and not any(_inside(h, top) for h in homes)) else mdir
    if root == "/" or any(_inside(h, root) for h in homes):
        raise PinError(f"the app root {root} is the filesystem root or contains your home directory; declare a narrower "
                       "app_root (the directory that holds the app's code)")
    if not _inside(os.path.realpath(manifest_path), root):
        raise PinError(f"the app root {root} does not contain the manifest")
    return root


# --------------------------------------------------------------------------------------------------------
# the whole-tree hash
# --------------------------------------------------------------------------------------------------------

def walk_root(root: str) -> Dict[str, str]:
    """``{relpath: digest}`` over EVERY file under ``root``: no exclusions (content of symlink TARGETS; symlinked dirs
    walked, cycles cut). An unreadable or vanishing file is a :class:`PinError`, never an uncaught OSError."""
    out: Dict[str, str] = {}
    total = [0]
    seen_dirs = set()

    def add(rel: str, path: str) -> None:
        if len(out) >= MAX_TREE_FILES:
            raise PinError(f"the app root has more than {MAX_TREE_FILES} files; too large to pin (declare a narrower "
                           "app_root in the manifest)")
        try:
            real = os.path.realpath(path)
            if not os.path.exists(real):
                out[rel] = "dangling:" + (os.readlink(path) if os.path.islink(path) else "")
                return
            if not os.path.isfile(real):
                out[rel] = "special"
                return
            total[0] += os.path.getsize(real)
            if total[0] > MAX_TREE_BYTES:
                raise PinError(f"the app root is larger than {MAX_TREE_BYTES} bytes; too large to pin")
            d = sha256_file(real)
            out[rel] = (f"link:{real}:{d}" if os.path.islink(path) else d)
        except OSError as exc:
            raise PinError(f"cannot read {path}: {exc.strerror or exc}")

    def walk(dir_path: str, rel_base: str) -> None:
        real = os.path.realpath(dir_path)
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError as exc:
            raise PinError(f"cannot read {dir_path}: {exc.strerror or exc}")
        for e in entries:
            rel = os.path.join(rel_base, e.name) if rel_base else e.name
            try:
                is_dir = e.is_dir(follow_symlinks=True) and not (e.is_symlink() and not os.path.exists(e.path))
            except OSError as exc:
                raise PinError(f"cannot read {e.path}: {exc.strerror or exc}")
            if is_dir:
                if e.is_symlink():
                    out[rel + "/"] = "dirlink:" + os.path.realpath(e.path)
                walk(e.path, rel)
            else:
                add(rel, e.path)

    walk(root, "")
    return out


def live_signature(root: str, manifest_path: str, settings_path: str) -> str:
    """Round 3c (R3-F10): a CHEAP signature of everything the pin covers, from ``stat`` only (no file is opened or
    hashed): per file under the root (symlinked dirs followed, cycles cut, as in :func:`walk_root`) its relative path,
    size, mtime_ns, ctime_ns and inode, plus the same for the manifest and the settings file. ctime cannot be set by
    a user process, so restoring size and mtime does not hide an edit. It is a CHANGE DETECTOR for the status of a
    RUNNING app: equal means "nothing touched since the wrapper recorded it" and skips the whole-tree re-hash; unequal
    (or unrecorded) triggers the full :func:`verify`. It never replaces verify: a start always re-hashes."""
    h = hashlib.sha256()
    seen_dirs = set()
    n = [0]

    def rec(label: str, path: str) -> None:
        try:
            st = os.stat(path)
            link = os.readlink(path) if os.path.islink(path) else ""
            h.update(f"{label}\0{st.st_mode:o}\0{st.st_size}\0{st.st_mtime_ns}\0{st.st_ctime_ns}\0{st.st_ino}\0{link}\n"
                     .encode("utf-8", "surrogateescape"))
        except OSError as exc:
            h.update(f"{label}\0ERR\0{exc.errno}\n".encode("utf-8", "surrogateescape"))

    def walk(dir_path: str, rel_base: str) -> None:
        real = os.path.realpath(dir_path)
        if real in seen_dirs:
            return
        seen_dirs.add(real)
        try:
            entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
        except OSError as exc:
            h.update(f"{rel_base}\0SCANERR\0{exc.errno}\n".encode("utf-8", "surrogateescape"))
            return
        for e in entries:
            rel = os.path.join(rel_base, e.name) if rel_base else e.name
            n[0] += 1
            if n[0] > MAX_TREE_FILES:
                h.update(b"TOOMANY\n")
                return
            try:
                is_dir = e.is_dir(follow_symlinks=True)
            except OSError:
                is_dir = False
            if is_dir:
                rec(rel + "/", e.path)
                walk(e.path, rel)
            else:
                rec(rel, e.path)

    walk(root, "")
    rec("<manifest>", manifest_path)
    rec("<settings>", settings_path)
    return h.hexdigest()


def _digest(files: Dict[str, str]) -> str:
    h = hashlib.sha256()
    for p in sorted(files):
        h.update(p.encode("utf-8", "surrogateescape") + b"\0" + files[p].encode("utf-8", "surrogateescape") + b"\n")
    return h.hexdigest()


def venv_record(venv_dir: Optional[str]) -> Dict[str, Any]:
    """A venv's identity WITHOUT running anything: the interpreter file's realpath + sha256 and pyvenv.cfg's sha256."""
    if not venv_dir:
        return {"path": None, "interpreter": "unset"}
    py = os.path.join(venv_dir, "bin", "python")
    rec: Dict[str, Any] = {"path": venv_dir}
    if not os.path.exists(py):
        rec["interpreter"] = "missing"
        return rec
    rp = os.path.realpath(py)
    rec["interpreter"] = rp
    if not os.path.isfile(rp):                 # R3-F6: a FIFO / device / directory there is a refusal, never an open
        raise PinError(f"the venv interpreter {rp} is not a regular file")
    try:
        rec["interpreter_sha256"] = sha256_file(rp)
    except PinError as exc:
        rec["interpreter_sha256"] = f"unreadable ({exc})"
    cfg = os.path.join(venv_dir, "pyvenv.cfg")
    try:
        rec["cfg_sha256"] = sha256_file(cfg) if os.path.isfile(cfg) else None
    except PinError as exc:
        rec["cfg_sha256"] = f"unreadable ({exc})"
    return rec


def _argv_paths(manifest: Manifest, settings: Dict[str, Any]) -> Dict[str, Any]:
    """The path values that reach an argv, by value AND resolved target (rule 2: a change needs a re-confirm)."""
    out: Dict[str, Any] = {}
    pvals = settings.get("paths") or {}
    for name in _rules.argv_path_names(manifest):
        v = pvals.get(name)
        out[f"paths.{name}"] = ({"value": v, "real": os.path.realpath(os.path.expanduser(v))} if v else None)
    uvals = settings.get("user") or {}
    for name in _rules.argv_setting_path_names(manifest):
        s = manifest.setting(name)
        v = uvals.get(name, s.default if s is not None else None)
        out[f"user.{name}"] = ({"value": v, "real": os.path.realpath(os.path.expanduser(v))}
                               if isinstance(v, str) and v else None)
    return out


def check_install_rules(manifest: Manifest, manifest_path: str, path_values: Optional[Dict[str, Any]] = None) -> str:
    """The rule-2 checks that need the filesystem, for propose / confirm / settings save / every verify. Returns the
    app root. Raises :class:`PinError` (root problems) or :class:`~.rules.RuleViolation` (argv / writes problems)."""
    root = app_root(manifest, manifest_path)
    cwd = os.path.dirname(os.path.realpath(manifest_path))
    _rules.check_manifest_argvs(manifest, cwd, root)
    if path_values is not None:
        why = _rules.check_writes_outside_root(manifest, path_values, root)
        if why:
            raise _rules.RuleViolation("paths", why)
    return root


def code_identity(manifest: Manifest, manifest_path: str, settings: Dict[str, Any], root: Optional[str] = None,
                  with_commit: bool = False) -> Dict[str, Any]:
    """The code identity NOW. Reads and hashes files; runs nothing."""
    root = root or app_root(manifest, manifest_path)
    files = walk_root(root)
    cwd = os.path.dirname(os.path.realpath(manifest_path))
    extra: Dict[str, str] = {}
    for _where, argv in _rules.argv_groups(manifest):
        a0 = argv[0]
        rp = os.path.realpath(a0 if os.path.isabs(a0) else os.path.join(cwd, a0))
        if os.path.isfile(rp) and not _inside(rp, root):
            extra[rp] = sha256_file(rp)
    code: Dict[str, Any] = {"kind": "root", "root": root, "files": files, "tree_sha256": _digest(files),
                            "extra": dict(sorted(extra.items()))}
    if with_commit:
        top = _find_repo_top(root)
        if top is not None:
            code["git"] = {"repo": top, "scope": os.path.relpath(root, top), "commit": _git_commit(top)}
    venv = None
    if manifest.path("venv") is not None:
        venv = venv_record((settings.get("paths") or {}).get("venv"))
    return {"code": code, "venv": venv, "argv_paths": _argv_paths(manifest, settings)}


def compute_pin(manifest: Manifest, manifest_path: str, raw: bytes, settings: Dict[str, Any]) -> Dict[str, Any]:
    try:
        check_install_rules(manifest, manifest_path, settings.get("paths") or {})
    except _rules.RuleViolation as exc:
        raise PinError(str(exc))
    ident = code_identity(manifest, manifest_path, settings, with_commit=True)
    return {"pin_version": PIN_VERSION, "manifest_path": os.path.realpath(manifest_path),
            "manifest_sha256": sha256_bytes(raw), **ident}


# --------------------------------------------------------------------------------------------------------
# the pinned manifest copy (the host's only source of manifest behaviour once installed)
# --------------------------------------------------------------------------------------------------------

def _secretish(name: str) -> bool:
    n = name.lower()
    return n.startswith(".") or any(s in n for s in _SECRETISH)


def write_snapshot(snapshot_dir: str, pin: Dict[str, Any], raw_manifest: bytes) -> None:
    """Keep the PINNED manifest bytes and the small non-secret regular text files the re-confirm diff needs (for every
    root: git is never asked). Never follows a symlink; dir 0700, files 0600 (F15)."""
    if os.path.isdir(snapshot_dir):
        shutil.rmtree(snapshot_dir)
    os.makedirs(snapshot_dir, mode=0o700)
    os.chmod(snapshot_dir, 0o700)

    def put(rel: str, data: bytes) -> None:
        dst = os.path.join(snapshot_dir, rel)
        os.makedirs(os.path.dirname(dst), mode=0o700, exist_ok=True)
        fd = open_append_fd(dst, truncate=True)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

    put(MANIFEST_SNAPSHOT_NAME, raw_manifest)
    code = pin.get("code") or {}
    root = code.get("root")
    budget = SNAPSHOT_MAX_TOTAL_BYTES
    for rel in sorted(code.get("files", {})):
        parts = rel.rstrip("/").split(os.sep)
        if rel.endswith("/") or any(_secretish(part) for part in parts) or any(p in _SNAPSHOT_SKIP_DIRS for p in parts[:-1]):
            continue
        p = os.path.join(root, rel)
        try:
            if os.path.islink(p) or not os.path.isfile(p) or os.path.getsize(p) > SNAPSHOT_MAX_FILE_BYTES:
                continue
            with open_regular(p) as fh:
                data = fh.read()
            data.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        budget -= len(data)
        if budget < 0:
            break
        put(os.path.join("files", rel), data)


def pinned_manifest_bytes(snapshot_dir: str, pin: Dict[str, Any]) -> Optional[bytes]:
    """The manifest bytes pinned at confirm, or None if the copy is missing or does not match the pinned sha."""
    try:
        with open_regular(os.path.join(snapshot_dir, MANIFEST_SNAPSHOT_NAME)) as fh:
            raw = fh.read()
    except OSError:
        return None
    return raw if sha256_bytes(raw) == pin.get("manifest_sha256") else None


# --------------------------------------------------------------------------------------------------------
# verification
# --------------------------------------------------------------------------------------------------------

def _changes(old: Dict[str, str], new: Dict[str, str]) -> List[Dict[str, str]]:
    out = []
    for p in sorted(set(old) | set(new)):
        if p not in new:
            out.append({"path": p, "change": "removed"})
        elif p not in old:
            out.append({"path": p, "change": "added"})
        elif old[p] != new[p]:
            out.append({"path": p, "change": "modified"})
    return out


def _path_changes(old: Dict[str, Any], new: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [{"name": k, "confirmed": (old or {}).get(k), "now": (new or {}).get(k)}
            for k in sorted(set(old or {}) | set(new or {})) if (old or {}).get(k) != (new or {}).get(k)]


def verify(pin: Dict[str, Any], manifest: Optional[Manifest], raw: bytes,
           settings: Dict[str, Any], manifest_path: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """``None`` when both pins hold, else ``(reason, detail)``: manifest-moved | manifest-changed | code-changed.
    Reads and hashes files only; runs no process (rule 1)."""
    if pin.get("pin_version") != PIN_VERSION:
        return ("code-changed", "the pin predates the current pin format; re-confirm")
    if manifest_path is not None and os.path.realpath(manifest_path) != pin.get("manifest_path"):
        return ("manifest-moved", f"apps.json now names {os.path.realpath(manifest_path)}, but the confirmed "
                                  f"manifest is {pin.get('manifest_path')}")
    if sha256_bytes(raw) != pin.get("manifest_sha256"):
        return ("manifest-changed", "the manifest's sha256 differs from the one pinned at confirm")
    if manifest is None:
        return ("manifest-changed", "the manifest no longer validates")
    old = pin.get("code") or {}
    try:
        check_install_rules(manifest, pin["manifest_path"], settings.get("paths") or {})
        now = code_identity(manifest, pin["manifest_path"], settings, root=old.get("root"))
    except _rules.RuleViolation as exc:
        return ("code-changed", exc.message)
    except PinError as exc:
        return ("code-changed", str(exc))
    new = now["code"]
    if new["root"] != old.get("root"):
        return ("code-changed", "the app root changed")
    changed = _changes(old.get("files", {}), new["files"]) + _changes(old.get("extra", {}), new["extra"])
    changed.sort(key=lambda c: c["path"].startswith(".git/"))          # the app's own files first, git metadata last
    if changed:
        return ("code-changed", f"{len(changed)} file(s) changed since confirm: " +
                ", ".join(f"{c['change']} {c['path']}" for c in changed[:5]))
    # a REQUIRED path that is now unset is reported by the preflight as refused(path-unset), not as a pin change
    pc = [c for c in _path_changes(pin.get("argv_paths") or {}, now["argv_paths"])
          if not (c["now"] is None and c["name"].startswith("paths.")
                  and manifest.path(c["name"][len("paths."):]) is not None
                  and not manifest.path(c["name"][len("paths."):]).optional)]
    if pc:
        return ("code-changed", f"{len(pc)} path value(s) used in a command changed since confirm "
                                f"({', '.join(c['name'] for c in pc[:5])}); re-confirm")
    ov, nv = pin.get("venv"), now.get("venv")
    if (ov or nv) and ov != nv:
        return ("code-changed", f"the venv changed: {ov} -> {nv}")
    return None


# --------------------------------------------------------------------------------------------------------
# the re-confirm screen
# --------------------------------------------------------------------------------------------------------

def _cap(s: str) -> str:
    return s if len(s) <= DIFF_MAX_CHARS else s[:DIFF_MAX_CHARS] + "\n... (diff truncated)"


def _text(path: str) -> Optional[List[str]]:
    try:
        if os.path.islink(path) or os.path.getsize(path) > SNAPSHOT_MAX_FILE_BYTES:
            return None
        with open_regular(path) as fh:
            return fh.read().decode("utf-8").splitlines(keepends=True)
    except (OSError, UnicodeDecodeError):
        return None


def describe_change(pin: Dict[str, Any], snapshot_dir: str, manifest_path: str, manifest: Optional[Manifest],
                    settings: Dict[str, Any]) -> Dict[str, Any]:
    """WHAT changed since the confirm: the manifest diff, the changed files with a unified diff for text files (from
    the snapshot taken at confirm), and the changed path values. Reads files only: runs nothing."""
    out: Dict[str, Any] = {"manifest": None, "code": None, "venv": None, "moved": None, "paths": None}
    if os.path.realpath(manifest_path) != pin.get("manifest_path"):
        out["moved"] = {"confirmed": pin.get("manifest_path"), "now": os.path.realpath(manifest_path)}
    old_m = _text(os.path.join(snapshot_dir, MANIFEST_SNAPSHOT_NAME)) or []
    new_m = _text(manifest_path) or []
    if old_m != new_m:
        out["manifest"] = {"diff": _cap("".join(difflib.unified_diff(old_m, new_m, "manifest (confirmed)",
                                                                      "manifest (now)")))}
    if manifest is None:
        return out
    code = pin.get("code") or {}
    try:
        now = code_identity(manifest, pin["manifest_path"], settings, root=code.get("root"))
    except PinError as exc:
        out["code"] = {"error": str(exc)}
        return out
    new = now["code"]
    files = _changes(code.get("files", {}), new["files"])
    extra = _changes(code.get("extra", {}), new["extra"])
    view: Dict[str, Any] = {"kind": "tree", "files": files, "outside_root": extra, "tree_sha256": new["tree_sha256"],
                            "confirmed_commit": (code.get("git") or {}).get("commit") or None}
    root = code.get("root") or ""
    files_dir = os.path.join(snapshot_dir, "files")
    for c in files:
        old_lines = _text(os.path.join(files_dir, c["path"])) if c["change"] != "added" else []
        new_lines = _text(os.path.join(root, c["path"])) if c["change"] != "removed" else []
        if old_lines is None or new_lines is None:
            c["diff"] = "(binary, large, secret-named or unsnapshotted file: no text diff)"
        else:
            c["diff"] = _cap("".join(difflib.unified_diff(old_lines, new_lines, c["path"] + " (confirmed)",
                                                          c["path"] + " (now)")))
    out["code"] = view
    pc = _path_changes(pin.get("argv_paths") or {}, now["argv_paths"])
    if pc:
        out["paths"] = pc
    if pin.get("venv") != now.get("venv"):
        out["venv"] = {"confirmed": pin.get("venv"), "now": now.get("venv")}
    return out

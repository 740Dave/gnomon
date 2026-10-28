"""DSD documents: attach documents to a DSD / a decision, record what was
sent, and open them for the operator through ONE guarded path.

The grammar (``**Documents:** path; path``) is parsed in :mod:`.model`; this module is
everything that touches the FILESYSTEM:

* :func:`classify` - the ONE policy: a listed path is openable only if it is an existing
  REGULAR file, resolves (symlinks followed) under an allow-listed root, has a document
  extension, and carries NO executable bit. Every refusal has a NAME (``REFUSAL_*``).
  Persist-time hashing and the open endpoint both call it, so they cannot disagree.
* :func:`describe_for_persist` - the send-time record per document: sha256, size, mtime.
  A missing document warns loudly but still persists (the DSD still sends). A document
  the policy refuses is NOT read (no hashing of an arbitrary file an agent named); it is
  recorded with the named refusal.
* :func:`view_documents` - what the UI shows: per document the send-time facts, the
  CURRENT sha, and ``changed`` ("changed since sent").
* :func:`open_document` - the operator Open action. It takes ``(record, scope, index)``,
  NEVER a path: only a document the record lists can be opened. The launcher is an
  INJECTED ``opener`` (tests never launch anything); the default real opener refuses to
  run under pytest.

.html/.htm/.svg open as TEXT (``open -t``), never in a browser. Nothing here executes a document: ``open`` is handed a path that already passed the
extension allow-list and the no-executable-bit rule. There is deliberately no agent verb
for any of this (the Open action is a web-UI route only).

g31/g69 notes: the root names below are home-relative and expanded at CALL time (no
import-time path capture); every function takes an injectable ``roots`` / ``home``.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .model import MAX_DOCS_PER_SCOPE, parse_dsd

#: document extensions the operator may Open (lower-case, with the dot).
DOC_EXTENSIONS = frozenset(
    "." + e for e in ("md", "txt", "lts", "smd", "json", "jsonl", "csv", "tsv", "pdf",
                      "html", "htm", "png", "jpg", "jpeg", "svg", "log"))


def allowed_relpaths() -> Tuple[str, ...]:
    """Extra allow-listed roots, RELATIVE to the user's home: the deployment's ``document-roots`` table
    (``<deploy_root>/defaults/document-roots.json``, key ``relpaths``; owner ruling 2026-10-04, batch P7). Gnomon ships NO
    deployment directory name: the neutral default is none (the per-user DATA root is always allowed, see
    :func:`allowed_roots`). Each entry must be a plain relative path (no ``..``, no absolute or ``~`` form, no NUL); an
    entry that is not is dropped and the fault recorded (degrade-visibly: the list only ever gets narrower)."""
    from .. import deploy_data

    raw = deploy_data.section("document-roots", "relpaths")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        deploy_data.record_fault("document-roots", "key 'relpaths' must be a list; ignored")
        return ()
    out = []
    for r in raw:
        ok = (isinstance(r, str) and r and not r.startswith(("/", "~")) and "\0" not in r
              and ".." not in r.replace("\\", "/").split("/"))
        if ok:
            out.append(r)
        else:
            deploy_data.record_fault("document-roots", f"relpath {r!r} dropped (must be a plain relative path)")
    return tuple(out)


#: extensions opened as TEXT (``open -t``, the default text editor), never in a browser:
#: they are active content (alpha-architect ruling 2026-10-02). Everything else uses plain ``open``.
TEXT_OPEN_EXTENSIONS = frozenset("." + e for e in (
    # active content a browser would run
    "html", "htm", "svg", "xhtml",
    # source / script types: the default handler could EXECUTE them (e.g. Python Launcher), so never plain-open
    "py", "pyw", "pyc", "sh", "bash", "zsh", "command", "js", "mjs", "cjs", "ts", "tsx", "jsx", "rb", "pl", "php",
    "ps1", "bat", "cmd", "scpt", "applescript", "swift", "go", "rs", "java", "c", "h", "cpp",
    # text / data / config types (a text editor is the right, inert viewer)
    "lts", "smd", "json", "jsonl", "csv", "tsv", "log", "css", "yaml", "yml", "toml", "ini", "cfg", "rst",
    "xml", "sql", "ipynb", "diff", "patch"))

#: The EXPLICIT inert list: the only types ever handed to the default application with a plain ``open``.
#: A type on neither list is only REVEALED in Finder (``open -R``), never launched (F1, 2026-10-03).
INERT_OPEN_EXTENSIONS = frozenset("." + e for e in ("pdf", "md", "txt", "png", "jpg", "jpeg", "gif", "webp"))

#: do not read more than this many bytes of one document (hash skipped, named).
MAX_HASH_BYTES = 256 * 1024 * 1024

# -- named refusals / statuses ------------------------------------------------
STATUS_OK = "ok"
REFUSAL_MISSING = "missing"
REFUSAL_RELATIVE = "relative-path"
REFUSAL_NOT_REGULAR = "not-a-regular-file"
REFUSAL_OUTSIDE_ROOTS = "outside-allowed-roots"
REFUSAL_EXTENSION = "extension-not-allowed"
REFUSAL_EXECUTABLE = "executable-bit-set"
REFUSAL_UNREADABLE = "unreadable"
REFUSAL_TOO_LARGE = "too-large-to-hash"
REFUSAL_BAD_SCOPE = "unknown-scope"
REFUSAL_BAD_INDEX = "unknown-document-index"
REFUSAL_UNKNOWN_DSD = "unknown-dsd"
REFUSAL_OPENER_FAILED = "opener-failed"

_SCOPE_RE = re.compile(r"^(top|D\d+)$")


class DocumentRefused(Exception):
    """A document operation refused for a NAMED reason (``code`` is one of the
    ``REFUSAL_*`` constants; ``message`` says what and why)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def allowed_roots(home: Optional[str] = None) -> List[str]:
    """The allow-listed roots as REAL paths (symlinks resolved, e.g. a cloud-sync-symlinked
    data root). Evaluated at call time; roots that do not exist are omitted."""
    base = home if home is not None else os.path.expanduser("~")
    out: List[str] = []
    for rel in allowed_relpaths():
        real = os.path.realpath(os.path.join(base, rel))
        if os.path.isdir(real) and real not in out:
            out.append(real)
    if home is None:  # X13 S5-4: the per-user DATA root (roots.toml) is an allowed root too, through the resolver
        try:
            from ..config import runtime_root_default

            real = os.path.realpath(runtime_root_default())
            if os.path.isdir(real) and real not in out:
                out.append(real)
        except Exception:  # noqa: BLE001 - a broken resolver adds nothing; the fixed list still applies
            pass
    return out


def _inside(real_path: str, real_root: str) -> bool:
    try:
        return os.path.commonpath([real_path, real_root]) == real_root
    except ValueError:                      # different drives / mixed abs-rel
        return False


def classify(path: str, roots: Optional[Sequence[str]] = None,
             *, home: Optional[str] = None,
             extensions: Optional[frozenset] = None) -> Dict[str, Any]:
    """Apply the open policy to a listed ``path``. Returns
    ``{ok, code, reason, resolved, size, mtime}``: ``ok`` True means openable; otherwise
    ``code`` is the NAMED refusal. Order: relative -> missing -> not-regular ->
    outside-roots -> extension -> executable-bit. Never raises.

    ``extensions`` (2026-10-03, the Deliverables Open button): the extension allow-list to apply;
    ``None`` = :data:`DOC_EXTENSIONS` exactly as before (every existing caller)."""
    allowed_ext = DOC_EXTENSIONS if extensions is None else extensions
    res: Dict[str, Any] = {"ok": False, "code": "", "reason": "", "resolved": "",
                           "size": None, "mtime": None}

    def refuse(code: str, reason: str) -> Dict[str, Any]:
        res.update(code=code, reason=reason)
        return res

    try:
        raw = str(path or "")
        if not raw.strip() or "\x00" in raw:
            return refuse(REFUSAL_MISSING, "empty or NUL-containing path")
        expanded = os.path.expanduser(raw) if raw.startswith("~") else raw
        if not os.path.isabs(expanded):
            return refuse(REFUSAL_RELATIVE,
                          f"{raw!r} is not absolute or ~-relative; relative paths are refused")
        real = os.path.realpath(expanded)
        res["resolved"] = real
        try:
            st = os.stat(real)
        except FileNotFoundError:
            return refuse(REFUSAL_MISSING, f"no such file: {raw}")
        except OSError as exc:
            return refuse(REFUSAL_UNREADABLE, f"cannot stat {raw}: {type(exc).__name__}")
        res["size"], res["mtime"] = st.st_size, st.st_mtime
        if not stat.S_ISREG(st.st_mode):
            return refuse(REFUSAL_NOT_REGULAR,
                          f"{os.path.basename(real) or raw} is not a regular file "
                          f"(a directory, .app bundle, device, fifo or socket is never opened)")
        use_roots = list(roots) if roots is not None else allowed_roots(home)
        real_roots = [os.path.realpath(r) for r in use_roots]
        if not any(_inside(real, r) for r in real_roots):
            return refuse(REFUSAL_OUTSIDE_ROOTS,
                          f"{real} is outside the allow-listed roots (checked after "
                          f"resolving symlinks)")
        ext_real = os.path.splitext(real)[1].lower()
        ext_listed = os.path.splitext(expanded)[1].lower()
        for ext in (ext_real, ext_listed):
            if ext not in allowed_ext:
                return refuse(REFUSAL_EXTENSION,
                              f"extension {ext or '(none)'!r} is not on the document list "
                              f"({', '.join(sorted(e[1:] for e in allowed_ext))})")
        if st.st_mode & 0o111:
            return refuse(REFUSAL_EXECUTABLE,
                          f"{os.path.basename(real)} has an executable bit set "
                          f"(mode {stat.S_IMODE(st.st_mode):04o}); executables are never opened")
        res.update(ok=True, code=STATUS_OK, reason="")
        return res
    except Exception as exc:                # noqa: BLE001 - classify never raises
        return refuse(REFUSAL_UNREADABLE, f"{type(exc).__name__}: {exc}")


def sha256_of(real_path: str, *, max_bytes: int = MAX_HASH_BYTES) -> Tuple[str, str]:
    """``(hex_sha256, "")`` or ``("", named reason)``. Streams; refuses past
    ``max_bytes`` rather than reading unboundedly. The caller has already established
    the path is a regular file (FIFOs would block here)."""
    try:
        if os.path.getsize(real_path) > max_bytes:
            return "", REFUSAL_TOO_LARGE
        h = hashlib.sha256()
        with open(real_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest(), ""
    except OSError as exc:
        return "", f"{REFUSAL_UNREADABLE} ({type(exc).__name__})"


# -- record listing -----------------------------------------------------------

def authored_documents(markdown: str) -> List[Dict[str, Any]]:
    """Every document the DSD markdown lists, flattened with its scope and index:
    ``[{scope: "top"|"D<n>", index, path, name}]`` (DSD level first)."""
    doc = parse_dsd(markdown or "")
    out: List[Dict[str, Any]] = []
    for i, d in enumerate(doc.documents[:MAX_DOCS_PER_SCOPE]):
        out.append({"scope": "top", "index": i, "path": d["path"], "name": d["name"]})
    for dec in doc.decisions:
        for i, d in enumerate(dec.documents[:MAX_DOCS_PER_SCOPE]):
            out.append({"scope": dec.id, "index": i, "path": d["path"], "name": d["name"]})
    return out


def describe_for_persist(markdown: str, roots: Optional[Sequence[str]] = None,
                         *, home: Optional[str] = None) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The send-time record of every listed document + the loud WARNINGS.

    Per entry: ``scope, index, path, name, status, sha256, size, mtime, warning``.
    ``status`` is ``ok`` or the named refusal. A missing/refused document still gets an
    entry (the DSD still sends); only an ``ok`` one is read and hashed. Never raises."""
    entries: List[Dict[str, Any]] = []
    warnings: List[str] = []
    for a in authored_documents(markdown):
        c = classify(a["path"], roots, home=home)
        sha, hnote = ("", "")
        if c["ok"]:
            sha, hnote = sha256_of(c["resolved"])
        status = c["code"] if not c["ok"] else (STATUS_OK if sha else (hnote or REFUSAL_UNREADABLE))
        warning = ""
        if status != STATUS_OK:
            warning = (f"DOCUMENT NOT RECORDED ({status}) for {a['scope']}: {a['path']} - "
                       f"{c['reason'] or hnote}")
            warnings.append(warning)
        entries.append({
            "scope": a["scope"], "index": a["index"], "path": a["path"], "name": a["name"],
            "status": status, "sha256": sha if status == STATUS_OK else "",
            "size": c["size"], "mtime": c["mtime"], "warning": warning})
    return entries, warnings


def entries_for_record(raw: Dict[str, Any], markdown: str) -> List[Dict[str, Any]]:
    """The documents a stored DSD record lists. The send-time ``documents`` the hub
    persisted when present; else (a record written before this feature, or by a store
    that did not describe them) the markdown's own listing with no send-time facts."""
    persisted = raw.get("documents") if isinstance(raw, dict) else None
    if isinstance(persisted, list) and persisted:
        return [e for e in persisted if isinstance(e, dict)]
    return [dict(a, status="not-recorded", sha256="", size=None, mtime=None, warning="")
            for a in authored_documents(markdown)]


def view_documents(raw: Dict[str, Any], markdown: str, roots: Optional[Sequence[str]] = None,
                   *, home: Optional[str] = None) -> List[Dict[str, Any]]:
    """The UI model, one dict per listed document: send-time facts, CURRENT facts
    (``current_sha256``), ``changed`` ("changed since sent": the current sha differs
    from the recorded one, or the file has since gone), ``openable`` + the named
    ``refusal`` so the button can show why it is disabled. Never raises."""
    out: List[Dict[str, Any]] = []
    for e in entries_for_record(raw, markdown):
        c = classify(str(e.get("path", "")), roots, home=home)
        cur = ""
        if c["ok"]:
            cur, _ = sha256_of(c["resolved"])
        sent = str(e.get("sha256") or "")
        changed = bool(sent) and (cur != sent)
        out.append({
            "scope": str(e.get("scope", "")), "index": int(e.get("index", 0) or 0),
            "name": str(e.get("name", "")), "path": str(e.get("path", "")),
            "sent_status": str(e.get("status", "")), "sent_sha256": sent,
            "sent_size": e.get("size"), "sent_mtime": e.get("mtime"),
            "warning": str(e.get("warning") or ""),
            "openable": bool(c["ok"]), "refusal": "" if c["ok"] else c["code"],
            "refusal_reason": "" if c["ok"] else c["reason"],
            "current_sha256": cur, "changed": changed,
            "missing_now": c["code"] == REFUSAL_MISSING})
    return out


def documents_at_send(entries: Sequence[Dict[str, Any]], scope: Optional[str] = None) -> str:
    """The ``documents-at-send`` answer-node value: ``name sha256:<hex>`` per document,
    ``;``-joined (``scope:`` prefixed when ``scope`` is None, i.e. on the root node).
    A document with no recorded sha says why: ``sha256:(<status>)``. Single line."""
    parts: List[str] = []
    for e in entries:
        if scope is not None and e.get("scope") != scope:
            continue
        name = re.sub(r"\s+", " ", str(e.get("name", ""))).strip()
        sha = str(e.get("sha256") or "") or f"({e.get('status') or 'not-recorded'})"
        label = name if scope is not None else f"{e.get('scope')}:{name}"
        parts.append(f"{label} sha256:{sha}")
    return "; ".join(parts)


# -- open ----------------------------------------------------------------------

def opens_as_text(path: str) -> bool:
    """True when ``path`` is an active-content type that must open in a text editor."""
    return os.path.splitext(path)[1].lower() in TEXT_OPEN_EXTENSIONS


def open_argv(path: str, as_text: bool = False) -> list:
    """The ONLY place an ``open`` argv is built. Open must NEVER be able to execute a file:
    directory -> ``-R`` (reveal); text/source/script type (or ``as_text``) -> ``-t`` (default text editor);
    explicit inert document/image type -> plain ``open``; anything else -> ``-R`` (reveal, never launch)."""
    ext = os.path.splitext(path)[1].lower()
    if os.path.isdir(path):
        return ["/usr/bin/open", "-R", path]
    if as_text or ext in TEXT_OPEN_EXTENSIONS:
        return ["/usr/bin/open", "-t", path]
    if ext in INERT_OPEN_EXTENSIONS:
        return ["/usr/bin/open", path]
    return ["/usr/bin/open", "-R", path]


def system_opener(path: str, as_text: bool = False) -> None:
    """The REAL opener: macOS ``open <path>`` (default application), or ``open -t <path>``
    (the default text editor) when ``as_text``. Refuses to run under
    pytest (tests inject their own opener; nothing in a test run may launch an app).
    Raises ``DocumentRefused(opener-failed)`` on a non-zero exit."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("the real document opener refuses to run under pytest "
                           "(inject an opener)")
    try:
        argv = open_argv(path, as_text)
        p = subprocess.run(argv, capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocumentRefused(REFUSAL_OPENER_FAILED, f"open failed: {type(exc).__name__}: {exc}")
    if p.returncode != 0:
        raise DocumentRefused(REFUSAL_OPENER_FAILED,
                              f"open exited {p.returncode}: {p.stderr.decode('utf-8', 'replace')[:200]}")


def open_document(raw: Optional[Dict[str, Any]], markdown: str, scope: Any, index: Any,
                  *, opener: Callable[..., None],
                  roots: Optional[Sequence[str]] = None,
                  home: Optional[str] = None) -> Dict[str, Any]:
    """Open document ``(scope, index)`` of one stored DSD record. ``raw`` None means the
    DSD id is unknown. NEVER takes a path. Raises :class:`DocumentRefused` (named) on any
    refusal; on success calls ``opener(resolved_path, as_text)`` exactly once (``as_text`` True for .html/.htm/.svg, which open in a text editor, never a browser) and returns
    ``{ok, name, scope, index, resolved, sha256}``."""
    if raw is None:
        raise DocumentRefused(REFUSAL_UNKNOWN_DSD, "no such DSD record")
    if not isinstance(scope, str) or not _SCOPE_RE.match(scope):
        raise DocumentRefused(REFUSAL_BAD_SCOPE,
                              f"scope must be 'top' or a decision id like 'D1', got {scope!r}")
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        raise DocumentRefused(REFUSAL_BAD_INDEX, f"index must be a non-negative integer, got {index!r}")
    in_scope = [e for e in entries_for_record(raw, markdown) if e.get("scope") == scope]
    match = [e for e in in_scope if e.get("index") == index]
    if not match:
        raise DocumentRefused(REFUSAL_BAD_INDEX,
                              f"{scope} lists {len(in_scope)} document(s); index {index} does not exist")
    entry = match[0]
    c = classify(str(entry.get("path", "")), roots, home=home)
    if not c["ok"]:
        raise DocumentRefused(c["code"], c["reason"])
    as_text = opens_as_text(c["resolved"]) or opens_as_text(str(entry.get("path", "")))
    opener(c["resolved"], as_text)
    return {"ok": True, "name": str(entry.get("name", "")), "scope": scope, "index": index,
            "resolved": c["resolved"]}

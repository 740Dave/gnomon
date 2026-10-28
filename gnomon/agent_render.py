"""Markdown fragments, ``{{include: name}}`` and ``${SYMBOLS}`` for agent definitions.

ONE renderer, two render paths
==============================

* AGENT defs (a profile's ``claude_md_source``): the harness render
  (:func:`gnomon.agent_def.render_agent_def`, at launch/spawn/pre-compact) drives
  :meth:`gnomon.launch.ClaudeMdRenderer.render_file`, which calls :func:`run_pipeline`.
* SUBAGENT defs (Claude Code reads ``~/.claude/agents/*.md`` raw): the CLI ``gnomon-render-agents``
  (:func:`main`) renders each ``~/.claude/agents-src/<name>.md`` to ``~/.claude/agents/<name>.md``
  through the same :func:`run_pipeline`.

GRAMMAR AND ORDER (fixed; g17/g18/g19)
========================================================

    include  ->  ${symbols}  ->  lts          (single pass each, in that order)

* ``{{include: name}}`` pulls ``<fragments>/<name>.md`` (one trailing newline stripped, so an
  inline include never adds a blank line). Fragments may include fragments: depth <= 3, a cycle is an
  error naming the cycle, a missing fragment is an error. ``\\{{include: name}}`` is the escape
  (renders the literal marker). A fragment may carry ``{{lts: … }}`` - it is expanded by the lts
  pass (the "nested lts" live view).
* ``{{lts: … }}`` is the existing expander (:mod:`gnomon.lts_expand`), unchanged. An
  ``{{include}}`` appearing INSIDE lts-expanded content is NOT expanded and yields a WARNING naming
  the node (no re-entry).
* ``${NAME}`` -> a reserved symbol (``USER_ROOT``, ``DATA_ROOT``, ``SESSION_ROOT`` (agent
  path only), ``ROLE`` - computed here, centrally, never redefinable by the sidecar) or a sidecar
  symbol (``<claude_dir>/agent-symbols.json``, a flat ``{"NAME": "value"}`` object). An undefined
  symbol is an ERROR. ``$$`` is a literal ``$``. Symbols apply to def and fragment text ONLY (F1 ruling,
  2026-09-30): they are expanded BEFORE the lts pass, so lts-expanded node content is inserted LITERALLY
  (a ``${X}`` or ``$$`` inside a graph node is data and is never interpreted). To render a literal ``$`` in
  a def or fragment, write ``$$``.

ERRORS are :class:`RenderError` - loud; the CLI exits non-zero and leaves the previous rendered
file intact (atomic tmp + rename); the harness path records status ``render-error`` and leaves a previous CLAUDE.md intact (a brand-new session with none gets the unexpanded source under a loud banner, F2).

FRESHNESS (g18): every CLI render records ``<claude_dir>/agents/.render-manifest.json``: the source
hash, every fragment hash, the symbol-sidecar hash, every lts node's content hash the expansion touched
and the output hash. :func:`verify_def` (used by ``tools/golive_smoke.py`` check ``agent-renders``)
FAILS on a stale input, a hand edit, or a def that no longer equals ``render(source)``.

SYMLINKS: a def in ``agents/`` that is a symlink (earlier tooling/FactLattice-owned) is FOREIGN - the CLI
refuses to write over it (``os.replace`` onto a symlink would replace the link, not the target).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

#: include grammar - name is a plain fragment file stem
FRAGMENT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
INCLUDE_RE = re.compile(r"(?P<esc>\\)?\{\{\s*include:\s*(?P<name>[^{}]*?)\s*\}\}")
#: ``$$`` (literal $) or ``${NAME}``
SYMBOL_RE = re.compile(r"\$\$|\$\{(?P<name>[A-Za-z_][A-Za-z0-9_]*)\}")
SYMBOL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
MAX_INCLUDE_DEPTH = 3
RESERVED_SYMBOLS = ("USER_ROOT", "DATA_ROOT", "SESSION_ROOT", "ROLE")

FRAGMENTS_DIRNAME = "agent-fragments"
SYMBOLS_FILENAME = "agent-symbols.json"
SRC_DIRNAME = "agents-src"
OUT_DIRNAME = "agents"
MANIFEST_FILENAME = ".render-manifest.json"
MANIFEST_VERSION = 1
CLAUDE_DIR_ENV = "GNOMON_CLAUDE_DIR"
STANDING_ORDER_FRAGMENT = "standing-order"
STANDING_ORDER_INCLUDE = "{{include: standing-order}}"


class RenderError(Exception):
    """A loud render failure. ``kind`` is one of: include-missing, include-cycle, include-depth,
    include-malformed, symbol-undefined, symbol-sidecar, lts-failed, source, target-symlink,
    frontmatter."""

    def __init__(self, message: str, kind: str = "render"):
        super().__init__(message)
        self.kind = kind


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> Optional[str]:
    try:
        with open(path, "rb") as fh:
            return "sha256:" + hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


# --- paths ----------------------------------------------------------------------------


@dataclass
class RenderPaths:
    """Every machine-level location the renderer touches (tests point them at tmp dirs)."""

    claude_dir: str

    @property
    def fragments_dir(self) -> str:
        return os.path.join(self.claude_dir, FRAGMENTS_DIRNAME)

    @property
    def symbols_path(self) -> str:
        return os.path.join(self.claude_dir, SYMBOLS_FILENAME)

    @property
    def src_dir(self) -> str:
        return os.path.join(self.claude_dir, SRC_DIRNAME)

    @property
    def out_dir(self) -> str:
        return os.path.join(self.claude_dir, OUT_DIRNAME)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.out_dir, MANIFEST_FILENAME)


def default_paths(claude_dir: Optional[str] = None) -> RenderPaths:
    """``claude_dir`` arg, else ``$GNOMON_CLAUDE_DIR``, else ``~/.claude`` (expanded at CALL time so
    a test that sets HOME never reaches the real one)."""
    base = claude_dir or os.environ.get(CLAUDE_DIR_ENV) or "~/.claude"
    return RenderPaths(os.path.abspath(os.path.expanduser(base)))


# --- symbols --------------------------------------------------------------------------


def reserved_symbols(role: Optional[str], session_root: Optional[str] = None) -> Dict[str, str]:
    """The centrally computed symbols. ``SESSION_ROOT`` exists ONLY on the agent path
    (``session_root`` given); ``ROLE`` only when a role is known."""
    out = {"USER_ROOT": os.path.expanduser("~"),
           "DATA_ROOT": os.path.join(os.path.expanduser("~"), "gnomon-data")}
    if session_root:
        out["SESSION_ROOT"] = os.path.abspath(os.path.expanduser(session_root))
    if role:
        out["ROLE"] = str(role)
    return out


def load_sidecar(path: str) -> Dict[str, str]:
    """The custom-symbol sidecar. Absent file = no custom symbols (not an error). A malformed
    file, a non-string value, a bad name, or a reserved name is a loud error."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RenderError(f"symbol sidecar {path} unreadable: {type(exc).__name__}: {exc}", "symbol-sidecar")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise RenderError(f"symbol sidecar {path} is not valid JSON: {exc}", "symbol-sidecar")
    if not isinstance(data, dict):
        raise RenderError(f"symbol sidecar {path} must be a JSON object of NAME -> string", "symbol-sidecar")
    for k, v in data.items():
        if not isinstance(k, str) or not SYMBOL_NAME_RE.match(k):
            raise RenderError(f"symbol sidecar {path}: bad symbol name {k!r}", "symbol-sidecar")
        if k in RESERVED_SYMBOLS:
            raise RenderError(f"symbol sidecar {path}: {k!r} is a RESERVED symbol (computed centrally; "
                              "a sidecar cannot redefine it)", "symbol-sidecar")
        if not isinstance(v, str):
            raise RenderError(f"symbol sidecar {path}: value of {k!r} must be a string", "symbol-sidecar")
    return dict(data)


_LTS_OPEN_RE = re.compile(r"\{\{(?=\s*lts:)")


def escape_lts_markers(value: str) -> str:
    """P3 hardening (F-3): a symbol VALUE (sidecar or reserved: ${ROLE}, the session root ...) is
    substituted BEFORE the lts pass, so a ``{{lts: ...}}`` inside it would EXECUTE as a macro. Put the lts
    escape backslash in front of every ``{{`` that opens one; the lts pass drops exactly that backslash,
    so the value renders byte-exact. (``{{include:`` needs nothing: the include pass already ran and
    substituted text is never re-scanned. A bare ``}}`` cannot open a macro.)"""
    return _LTS_OPEN_RE.sub(lambda m: "\\{{", value)


def symbol_pass(text: str, symbols: Dict[str, str]) -> Tuple[str, List[str]]:
    """Single-scan ``${NAME}`` substitution + ``$$`` -> ``$``. Returns (text, names used).
    Any undefined name is ONE error naming them all."""
    used: List[str] = []
    undefined: List[str] = []

    def _sub(m: "re.Match[str]") -> str:
        name = m.group("name")
        if name is None:
            return "$"
        if name in symbols:
            if name not in used:
                used.append(name)
            return symbols[name]
        if name not in undefined:
            undefined.append(name)
        return m.group(0)

    out = SYMBOL_RE.sub(_sub, text)
    if undefined:
        raise RenderError("undefined symbol(s): " + ", ".join("${%s}" % n for n in undefined)
                          + " (define in the symbol sidecar, or write $$ for a literal $;"
                          + " SESSION_ROOT exists only for agent defs)", "symbol-undefined")
    return out, used


# --- includes -------------------------------------------------------------------------


def _read_fragment(fragments_dir: str, name: str) -> str:
    path = os.path.join(fragments_dir, name + ".md")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        raise RenderError(f"missing fragment {name!r}: {path} does not exist", "include-missing")
    except OSError as exc:
        raise RenderError(f"fragment {name!r} unreadable ({path}): {type(exc).__name__}: {exc}", "include-missing")


def include_pass(text: str, fragments_dir: str, fragments: Dict[str, str],
                 _stack: Optional[List[str]] = None) -> str:
    """Expand ``{{include: name}}`` (recursively, depth <= 3, cycle-checked). ``fragments`` is filled
    with name -> sha256 of every fragment file touched (transitively)."""
    stack = _stack if _stack is not None else []

    def _sub(m: "re.Match[str]") -> str:
        if m.group("esc"):
            return m.group(0)[1:]
        name = m.group("name")
        if not FRAGMENT_NAME_RE.match(name or ""):
            raise RenderError(f"malformed include name {name!r} (allowed: letters, digits, _ . -)",
                              "include-malformed")
        if name in stack:
            cyc = " -> ".join(stack[stack.index(name):] + [name])
            raise RenderError(f"include cycle: {cyc}", "include-cycle")
        if len(stack) + 1 > MAX_INCLUDE_DEPTH:
            raise RenderError(f"include depth > {MAX_INCLUDE_DEPTH}: {' -> '.join(stack + [name])}",
                              "include-depth")
        body = _read_fragment(fragments_dir, name)
        fragments[name] = sha256_text(body)
        inner = include_pass(body, fragments_dir, fragments, stack + [name])
        return inner[:-1] if inner.endswith("\n") else inner

    return INCLUDE_RE.sub(_sub, text)


# --- the pipeline ---------------------------------------------------------------------


@dataclass
class PipelineOutput:
    text: str
    fragments: Dict[str, str] = field(default_factory=dict)      # name -> sha256
    symbols_used: List[str] = field(default_factory=list)
    sidecar_sha256: Optional[str] = None
    lts_expansions: List[dict] = field(default_factory=list)
    lts_pending: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def lts_manifest(self) -> List[dict]:
        return [{"query": e.get("query"), "corpus": e.get("corpus"), "status": e.get("status"),
                 "nodes": dict(e.get("node_hashes") or {})} for e in self.lts_expansions]


def run_pipeline(text: str, *, lts_render: Callable[[str], Any], paths: Optional[RenderPaths] = None,
                 role: Optional[str] = None, session_root: Optional[str] = None,
                 strict_lts: bool = False) -> PipelineOutput:
    """include -> ${symbols} -> lts over ``text`` (F1: lts output is never symbol-expanded). ``lts_render(text)`` is the existing
    ``ClaudeMdRenderer.render`` (returns .text/.lts_expansions/.lts_expansion_pending). With
    ``strict_lts`` any FAILED lts expansion is a :class:`RenderError` (the CLI); without it the
    banner stays in the text and is recorded (the harness path, unchanged behaviour)."""
    paths = paths or default_paths()
    fragments: Dict[str, str] = {}
    step1 = include_pass(text, paths.fragments_dir, fragments)
    # F1 (architect ruling 2026-09-30): symbols expand in def + fragment text ONLY. They run HERE, on the
    # post-include, PRE-lts text, so lts output (graph node text is DATA: ``${NAME}`` / ``$$`` occur in it
    # legitimately) is inserted LITERALLY and can never fail or be rewritten by the symbol pass.
    sidecar = load_sidecar(paths.symbols_path)
    symbols = dict(sidecar)
    symbols.update(reserved_symbols(role, session_root))     # reserved always wins (and can't collide)
    step2, used = symbol_pass(step1, {k: escape_lts_markers(v) for k, v in symbols.items()})
    res = lts_render(step2)
    expansions = list(getattr(res, "lts_expansions", []) or [])
    pending = list(getattr(res, "lts_expansion_pending", []) or [])
    warnings: List[str] = []
    for e in expansions:
        for nid in e.get("reentrant_include_ids") or []:
            warnings.append(f"lts node {e.get('corpus')}:{nid} (macro `{e.get('query')}`) contains an "
                            "{{include: …}} marker: NOT expanded (single pass, no re-entry); rendered literally")
    failed = [e for e in expansions if e.get("status") == "failed"]
    if strict_lts and failed:
        raise RenderError("lts expansion FAILED: " + "; ".join(
            f"{e.get('query')}: {e.get('reason')}" for e in failed), "lts-failed")
    final = res.text
    return PipelineOutput(text=final, fragments=fragments, symbols_used=used,
                          sidecar_sha256=sha256_file(paths.symbols_path), lts_expansions=expansions,
                          lts_pending=pending, warnings=warnings)


# --- subagent path: agents-src -> agents ----------------------------------------------


_FRONTMATTER_RE = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n", re.DOTALL)


def split_frontmatter(text: str) -> Tuple[str, str]:
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "", text
    return text[:m.end()], text[m.end():]


def generated_header(name: str) -> str:
    return f"<!-- GENERATED from agents-src/{name}.md by gnomon-render-agents; edit the source -->\n"


def _default_lts_render() -> Callable[[str], Any]:
    from .launch import ClaudeMdRenderer
    from .lts_expand import LtsExpander

    return ClaudeMdRenderer(expander=LtsExpander()).render


def render_source(name: str, paths: RenderPaths, *,
                  lts_render: Optional[Callable[[str], Any]] = None) -> Tuple[str, PipelineOutput]:
    """Render ``agents-src/<name>.md`` (in memory). Frontmatter is kept byte-for-byte and first;
    the passes run on the BODY only; the GENERATED header follows the frontmatter."""
    src = os.path.join(paths.src_dir, name + ".md")
    try:
        with open(src, "r", encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise RenderError(f"source {src} unreadable: {type(exc).__name__}: {exc}", "source")
    fm, body = split_frontmatter(raw)
    if not fm:
        raise RenderError(f"source {src} has no YAML frontmatter (a subagent def must start with '---')",
                          "frontmatter")
    out = run_pipeline(body, lts_render=lts_render or _default_lts_render(), paths=paths, role=name,
                       strict_lts=True)
    return fm + generated_header(name) + out.text, out


def _atomic_write(path: str, text: str) -> None:
    tmp = f"{path}.{os.getpid()}.{time.time_ns()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


@contextlib.contextmanager
def _lock(path: str):
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def read_manifest(paths: RenderPaths) -> Dict[str, Any]:
    try:
        with open(paths.manifest_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("defs"), dict):
            return data
    except (OSError, ValueError):
        pass
    return {"version": MANIFEST_VERSION, "defs": {}}


def list_sources(paths: RenderPaths) -> List[str]:
    try:
        return sorted(f[:-3] for f in os.listdir(paths.src_dir) if f.endswith(".md") and not f.startswith("."))
    except FileNotFoundError:
        return []


def render_one(name: str, paths: RenderPaths, *,
               lts_render: Optional[Callable[[str], Any]] = None) -> Tuple[Dict[str, Any], List[str]]:
    """Render + atomically write ONE def and return (manifest entry, warnings). Raises
    :class:`RenderError` BEFORE any write (a failed render leaves the previous file intact)."""
    target = os.path.join(paths.out_dir, name + ".md")
    if os.path.islink(target):
        raise RenderError(f"REFUSING to write over symlink {target} -> {os.readlink(target)} "
                          "(a symlinked def is foreign-owned; never touched)", "target-symlink")
    text, out = render_source(name, paths, lts_render=lts_render)
    os.makedirs(paths.out_dir, exist_ok=True)
    _atomic_write(target, text)
    src = os.path.join(paths.src_dir, name + ".md")
    entry = {"source": src, "source_sha256": sha256_file(src), "fragments": dict(out.fragments),
             "symbols_sidecar_sha256": out.sidecar_sha256, "symbols_used": list(out.symbols_used),
             "lts": out.lts_manifest(), "output_sha256": sha256_text(text),
             "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    return entry, out.warnings


def render_all(paths: RenderPaths, *, only: Optional[List[str]] = None,
               lts_render: Optional[Callable[[str], Any]] = None,
               echo: Callable[[str], None] = print) -> int:
    """Render every source (or ``only``). Each def is independent + atomic; ANY error makes the
    return code 1 (and never touches that def's previous file or manifest entry)."""
    names = list_sources(paths)
    if only:
        missing = [n for n in only if n not in names]
        for n in missing:
            echo(f"ERROR {n}: no source {os.path.join(paths.src_dir, n + '.md')}")
        names = [n for n in names if n in only]
    if not names and not only:
        echo(f"ERROR: no sources found in {paths.src_dir}")
        return 1
    rc = 1 if only and any(n not in list_sources(paths) for n in only) else 0
    if os.path.islink(paths.out_dir):     # F11: a symlinked agents/ would render INTO a foreign directory
        echo(f"ERROR: REFUSING to render: {paths.out_dir} is a symlink -> {os.readlink(paths.out_dir)} "
             "(a foreign-owned directory is never written)")
        return 1
    if os.path.islink(paths.manifest_path):   # F11: os.replace would swap the link for a regular file
        echo(f"ERROR: REFUSING to render: {paths.manifest_path} is a symlink -> "
             f"{os.readlink(paths.manifest_path)} (never replaced)")
        return 1
    os.makedirs(paths.out_dir, exist_ok=True)
    entries: Dict[str, Dict[str, Any]] = {}
    for name in names:
        try:
            entry, warns = render_one(name, paths, lts_render=lts_render)
        except RenderError as exc:
            echo(f"ERROR {name}: {exc}")
            rc = 1
            continue
        except Exception as exc:  # noqa: BLE001 - anything unexpected is loud too
            echo(f"ERROR {name}: unexpected {type(exc).__name__}: {exc}")
            rc = 1
            continue
        entries[name + ".md"] = entry
        for w in warns:
            echo(f"WARNING {name}: {w}")
        echo(f"rendered {name}.md")
    if entries:
        with _lock(os.path.join(paths.out_dir, ".render-manifest.lock")):
            man = read_manifest(paths)
            man["version"] = MANIFEST_VERSION
            man["defs"].update(entries)
            _atomic_write(paths.manifest_path, json.dumps(man, indent=2, sort_keys=True) + "\n")
    return rc


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="gnomon-render-agents",
        description="Render ~/.claude/agents-src/*.md to ~/.claude/agents/*.md (include -> ${symbols} -> lts).")
    ap.add_argument("names", nargs="*", help="render only these defs (default: all sources)")
    ap.add_argument("--claude-dir", default=None,
                    help=f"default: ${CLAUDE_DIR_ENV} or ~/.claude")
    args = ap.parse_args(argv)
    return render_all(default_paths(args.claude_dir), only=args.names or None)


# --- verification (golive_smoke check `agent-renders`) --------------------------------


@dataclass
class DefVerdict:
    name: str
    problems: List[str] = field(default_factory=list)     # -> FAIL
    unverified: List[str] = field(default_factory=list)   # -> SKIP when there are no problems


def verify_def(name: str, paths: RenderPaths, manifest: Dict[str, Any], *,
               lts_render: Optional[Callable[[str], Any]] = None) -> DefVerdict:
    """READ-ONLY. Is ``agents/<name>.md`` fresh, un-hand-edited and equal to render(source)?"""
    v = DefVerdict(name)
    fname = name + ".md"
    out_path = os.path.join(paths.out_dir, fname)
    src_path = os.path.join(paths.src_dir, fname)
    entry = (manifest.get("defs") or {}).get(fname)
    if entry is None:
        v.problems.append("no manifest entry (never rendered by gnomon-render-agents)")
        return v
    if os.path.islink(out_path):
        v.problems.append("rendered def is a symlink (foreign); gnomon-render-agents must never write it")
        return v
    if not os.path.isfile(out_path):
        v.problems.append(f"rendered def missing: {out_path}")
        return v
    if not os.path.isfile(src_path):
        v.problems.append(f"source missing: {src_path}")
        return v
    out_sha = sha256_file(out_path)
    if out_sha != entry.get("output_sha256"):
        v.problems.append("HAND-EDITED: rendered file hash differs from the manifest (edit agents-src/, then re-render)")
    if sha256_file(src_path) != entry.get("source_sha256"):
        v.problems.append("STALE: source changed since the last render")
    out_mtime = os.path.getmtime(out_path)
    if os.path.getmtime(src_path) > out_mtime:
        v.problems.append("STALE: rendered def is older than its source")
    for frag, want in sorted((entry.get("fragments") or {}).items()):
        fp = os.path.join(paths.fragments_dir, frag + ".md")
        have = sha256_file(fp)
        if have is None:
            v.problems.append(f"STALE: fragment {frag!r} is missing")
        elif have != want:
            v.problems.append(f"STALE: fragment {frag!r} changed since the last render")
        elif os.path.getmtime(fp) > out_mtime:
            v.problems.append(f"STALE: rendered def is older than fragment {frag!r}")
    if sha256_file(paths.symbols_path) != entry.get("symbols_sidecar_sha256"):
        v.problems.append("STALE: symbol sidecar changed since the last render")
    try:
        text, out = render_source(name, paths, lts_render=lts_render)
    except RenderError as exc:
        if exc.kind == "lts-failed":
            # F7a: 'cannot verify' is a FAILURE state for a gate, never a SKIP (a graph outage must not
            # let a hand-edited or stale lts-dependent def look fine).
            v.problems.append(f"FAIL-UNVERIFIABLE: lts nodes NOT re-hashed and render(source) NOT compared "
                              f"(graph unreachable or expansion failed): {exc}")
        else:
            v.problems.append(f"render(source) now FAILS: {exc}")
        return v
    old_nodes: Dict[str, str] = {}
    for e in entry.get("lts") or []:
        for nid, h in (e.get("nodes") or {}).items():
            old_nodes[f"{e.get('corpus')}:{nid}"] = h
    new_nodes: Dict[str, str] = {}
    for e in out.lts_manifest():
        for nid, h in (e.get("nodes") or {}).items():
            new_nodes[f"{e.get('corpus')}:{nid}"] = h
    for key in sorted(set(old_nodes) | set(new_nodes)):
        if old_nodes.get(key) != new_nodes.get(key):
            v.problems.append(f"STALE: lts node {key} changed (recorded {old_nodes.get(key)}, now {new_nodes.get(key)})")
    with open(out_path, "r", encoding="utf-8") as fh:
        on_disk = fh.read()
    if on_disk != text:
        v.problems.append("content differs from render(source)")
    return v


GENERATED_HEADER_RE = re.compile(r"^<!-- GENERATED from agents-src/[^ ]+\.md by gnomon-render-agents", re.M)


def find_orphans(paths: RenderPaths, known_names: List[str]) -> List[Tuple[str, str]]:
    """F7b. REGULAR files in ``agents/`` that carry the GENERATED header but whose name is in neither
    ``agents-src/`` nor the manifest: a generated def whose source or manifest entry is gone (it would
    otherwise be invisible to every check). Symlinked defs are foreign and never listed.
    Returns [(name, reason)]."""
    found: List[Tuple[str, str]] = []
    try:
        entries = sorted(os.listdir(paths.out_dir))
    except FileNotFoundError:
        return found
    for f in entries:
        if not f.endswith(".md") or f.startswith("."):
            continue
        fp = os.path.join(paths.out_dir, f)
        if os.path.islink(fp) or not os.path.isfile(fp):
            continue
        name = f[:-3]
        if name in known_names:
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        if GENERATED_HEADER_RE.search(head):
            found.append((name, f"ORPHAN: agents/{f} carries the GENERATED header but agents-src/{f} and its "
                                "manifest entry are both gone (delete it, or restore the source)"))
    return found


# --- the standing-order inclusion rule (g17) ------------------------------------------


def def_includes_standing_order(name: str, paths: RenderPaths, canonical_core: Optional[str]) -> Tuple[str, str]:
    """(state, evidence) for one def named in the standing order's ``copies``:
    ``ok`` (source includes the fragment, and the deliverable carries it), ``pending``
    (an unmigrated def that still carries the verbatim core; the owner has not opted in), ``fail``,
    or ``foreign`` (a symlink: not ours)."""
    marker_ok = STANDING_ORDER_INCLUDE
    src = os.path.join(paths.src_dir, name + ".md")
    live = os.path.join(paths.out_dir, name + ".md")
    if os.path.islink(live):
        return "foreign", f"{live} is a symlink"
    if os.path.isfile(src):
        with open(src, "r", encoding="utf-8") as fh:
            stext = fh.read()
        if not re.search(r"\{\{\s*include:\s*" + re.escape(STANDING_ORDER_FRAGMENT) + r"\s*\}\}", stext):
            return "fail", f"agents-src/{name}.md does not include {marker_ok}"
        return "ok", f"agents-src/{name}.md includes {STANDING_ORDER_FRAGMENT}"
    if not os.path.isfile(live):
        return "fail", f"no def found for {name} (neither agents-src/ nor agents/)"
    with open(live, "r", encoding="utf-8") as fh:
        ltext = fh.read()
    if re.search(r"\{\{\s*include:\s*" + re.escape(STANDING_ORDER_FRAGMENT) + r"\s*\}\}", ltext):
        return "ok", f"agents/{name}.md (agent def) includes {STANDING_ORDER_FRAGMENT}"
    if canonical_core and canonical_core in ltext:
        return "pending", f"agents/{name}.md still carries the verbatim core (migrates when its owner opts in)"
    return "fail", f"agents/{name}.md has neither {marker_ok} nor the canonical core"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

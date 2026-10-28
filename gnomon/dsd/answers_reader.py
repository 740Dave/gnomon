"""The ONE reader of the DSD-answer corpus (pre-review fix round; Diamond findings O-F1 and D-F1).

The answers corpus is a DIRECTORY. Since 2026-10-02 the production typed writer (``dsd.writer.TypedLtsDecisionWriter``) writes each DSD's
nodes to its OWN file ``<corpus dir>/<dsd_id>.lts`` and never writes the configured (legacy) single file again; the legacy file
``dsd-answers.lts`` still holds the older answers, and ``00-*.lts`` files are conventions, not answers. This reader is used by the
inline answer notice, ``my_dsds`` and the bin-analysis helper, so all three see the same nodes.

* ``path`` is the configured answers file (``config.resolved_dsd_answers_corpus_path()``) OR the corpus directory itself.
* With a ``dsd_id``: that DSD's shard first, the legacy file second (nodes of the DSD found in either are returned, shard first).
* Without one: every ``*.lts`` in the directory (``00-*`` skipped), the legacy file included.
* ``files_read`` names every file that was opened. A path that does not exist, a directory that cannot be listed or a file that cannot
  be read RAISES :class:`AnswersReadError`; it is never answered with an empty list (an unreadable corpus must not read as "no answers").
  A missing shard is NOT an error (that DSD simply has no nodes there).

Headless, stdlib only.
"""
from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

NODE_HEAD_RE = re.compile(r"^#{2,6}\s.*\[(DEC-DSD-[A-Z0-9]+-D[0-9]+)\]\s*$")
_PROP_RE = re.compile(r"^- ([a-z][a-z0-9_-]*):\s?(.*)$")
_SHARD_STEM_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
LEGACY_NAME = "dsd-answers.lts"


class AnswersReadError(Exception):
    """The answers corpus (or a file of it) could not be read; the message names the path and the reason."""


def _corpus_dir_and_legacy(path: str) -> Tuple[str, Optional[str]]:
    p = os.path.abspath(os.path.expanduser(str(path)))
    if os.path.isdir(p):
        legacy = os.path.join(p, LEGACY_NAME)
        return p, legacy
    return os.path.dirname(p) or ".", p                 # a file: the configured legacy file; its directory is the corpus


def parse_nodes(text: str) -> List[Dict[str, Any]]:
    """Per-decision answer nodes (``DEC-DSD-<ID>-D<n>``) of one file's text, each ``{node_id, props}``."""
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for line in text.split("\n"):
        m = NODE_HEAD_RE.match(line)
        if m:
            cur = {"node_id": m.group(1), "props": {}}
            out.append(cur)
        elif line.startswith("#"):
            cur = None
        elif cur is not None:
            pm = _PROP_RE.match(line)
            if pm:
                cur["props"][pm.group(1)] = pm.group(2)
    return out


def _read_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        raise AnswersReadError(f"cannot read the answers file {path}: {exc.__class__.__name__}: {exc}") from None


def read_answers(path: str, dsd_id: Optional[str] = None) -> Dict[str, Any]:
    """``{"nodes": [{node_id, props}], "files_read": [path, ...]}`` (see the module docstring). Raises :class:`AnswersReadError`."""
    if not path:
        raise AnswersReadError("no answers corpus path given")
    corpus_dir, legacy = _corpus_dir_and_legacy(path)
    if not os.path.isdir(corpus_dir):
        raise AnswersReadError(f"the answers corpus {path} does not exist (no directory {corpus_dir})")
    files: List[str] = []
    nodes: List[Dict[str, Any]] = []
    if dsd_id is not None:
        if not _SHARD_STEM_RE.match(str(dsd_id)) or ".." in str(dsd_id):
            raise AnswersReadError(f"{dsd_id!r} is not a usable DSD id")
        from .persist import root_node_id

        prefix = root_node_id(str(dsd_id)) + "-"
        candidates = [os.path.join(corpus_dir, str(dsd_id) + ".lts")]
        if legacy and os.path.abspath(legacy) != os.path.abspath(candidates[0]):
            candidates.append(legacy)
        for c in candidates:
            if not os.path.exists(c):
                continue
            if os.path.isdir(c):
                raise AnswersReadError(f"{c} is a directory, not an answers file")
            text = _read_file(c)
            files.append(c)
            nodes += [n for n in parse_nodes(text) if n["node_id"].startswith(prefix)]
        return {"nodes": nodes, "files_read": files}
    try:
        names = sorted(os.listdir(corpus_dir))
    except OSError as exc:
        raise AnswersReadError(f"cannot list the answers corpus directory {corpus_dir}: {exc}") from None
    for name in names:
        if not name.endswith(".lts") or name.startswith("00-") or name.startswith("."):
            continue
        fp = os.path.join(corpus_dir, name)
        if not os.path.isfile(fp):
            continue
        files.append(fp)
        nodes += parse_nodes(_read_file(fp))
    if os.path.isfile(path) and os.path.abspath(path) not in [os.path.abspath(f) for f in files]:
        raise AnswersReadError(f"the configured answers file {path} is not a readable .lts file of its corpus")
    return {"nodes": nodes, "files_read": files}

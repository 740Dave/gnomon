"""Substrates the S2 typed writers persist to (all injected roots, never a live default).

* :class:`LtsFiles`: minimal, atomic property-line editing of ``.lts`` nodes (an item is an lts node whose scope is the
  property ``project: <ws>/<slug>`` and whose re-file history is the repeated property ``project-history``).
* :class:`CorpusRegistry`: the lts corpus registry JSON (``corpus-config``): roots per corpus, plus the ``owner``,
  ``projects`` and ``visible-in`` keys ENT-CORPUS says an entry gains. Read and, for ``add_corpus``, written.
* :class:`TopicStore`: DATA ``workspaces/<ws>/topics/TOP-nnn.json`` plus the machine-wide counter ``topic-counter.json``.

Everything here is used by ordinary work (writers), never by a machine safety path (I6).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from . import ltsread
from .scopebase import SLUG_RE, ModelError, ScopeRefusal, atomic_write_json, read_json, require_enabled

_FENCE = re.compile(r"^\s*(```|~~~)")
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_PROP = re.compile(r"^- ([a-z][a-z0-9_-]*):\s?(.*)$")
_NODE_ID = re.compile(r"[A-Z][A-Z0-9._-]*")
_KEY = re.compile(r"[a-z][a-z0-9_-]*")
_HEADING_OUT = re.compile(r"(#{1,6}) (.*) \[([A-Z][A-Z0-9._-]*)\]")
# ASCII digits only: ``\d`` matches every Unicode decimal digit (Arabic-Indic, fullwidth, ...), so ``TOP-٠٠١`` passed validation.
TOPIC_ID_RE = re.compile(r"TOP-[0-9]{3,}", re.ASCII)


def _unsafe(what: str, value: object, why: str) -> ScopeRefusal:
    shown = repr(value) if len(repr(value)) <= 60 else repr(value)[:57] + "...'"
    return ScopeRefusal("unsafe-text", f"{what} {shown} {why}", "use plain single-line text")


def check_value(what: str, value: object) -> str:
    """A property VALUE (or a ``by`` / session-run / answered-by field stored in one): a str on ONE line (no CR, LF, VT, FF,
    NEL, U+2028/9, NUL or other control character; ``splitlines`` and the lts reader must agree there is one line) that
    does not itself parse as a property line or a heading. Returns it unchanged, else raises ``unsafe-text``."""
    if not isinstance(value, str):
        raise _unsafe(what, value, "is not text")
    if len(("a" + value + "b").splitlines()) != 1 or any((ord(c) < 32 and c != "\t") or c == "\x7f" for c in value):
        raise _unsafe(what, value, "contains a line break or control character")
    v = value.lstrip()
    if _PROP.match(v) or _HEADING.match(v):
        raise _unsafe(what, value, "would parse as a property or heading line")
    return value


def check_title(title: object) -> str:
    """A node TITLE: one safe line (as :func:`check_value`) that carries no ``[ID]`` token of its own (the heading's id is
    its first or last token, so a token in the title could forge the node id) and is not empty."""
    check_value("title", title)
    if not title.strip():                                            # type: ignore[union-attr]
        raise _unsafe("title", title, "is empty")
    if ltsread._TOKEN_RE.search(title):                              # type: ignore[arg-type]
        raise _unsafe("title", title, "contains an [ID] token")
    return title                                                      # type: ignore[return-value]


def check_node_id(node_id: object) -> str:
    if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
        raise _unsafe("node id", node_id, "is not an upper-case id token")
    return node_id


def check_key(key: object) -> str:
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise _unsafe("property key", key, "does not match ^[a-z][a-z0-9_-]*$")
    return key


class LtsFiles:
    """Line-level lts node editing. Text in, text out (pure) plus atomic file helpers."""

    @staticmethod
    def find_span(lines: Sequence[str], node_id: str) -> Optional[Tuple[int, int]]:
        """``(start, end)`` line indexes (0-based, end exclusive) of the node ``node_id``: from its heading to the next
        heading of any kind (headings inside fences are ignored), or the end of the file."""
        in_fence = False
        start: Optional[int] = None
        for i, raw in enumerate(lines):
            if _FENCE.match(raw):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            m = _HEADING.match(raw)
            if not m:
                continue
            if start is not None:
                return start, i
            hid = ltsread.heading_id(m.group(2))
            if hid is not None and hid[1] == node_id:
                start = i
        return (start, len(lines)) if start is not None else None

    @staticmethod
    def _prop_lines(lines: Sequence[str], start: int, end: int) -> List[int]:
        return [i for i in range(start + 1, end) if _PROP.match(lines[i])]

    @classmethod
    def get_props(cls, text: str, node_id: str) -> Optional[Dict[str, List[str]]]:
        lines = text.split("\n")
        span = cls.find_span(lines, node_id)
        if span is None:
            return None
        out: Dict[str, List[str]] = {}
        for i in cls._prop_lines(lines, *span):
            m = _PROP.match(lines[i])
            out.setdefault(m.group(1), []).append(m.group(2).strip())
        return out

    @classmethod
    def add_property(cls, text: str, node_id: str, key: str, value: str) -> str:
        """Append a ``- key: value`` line after the node's last property (a repeated key is a list value)."""
        check_node_id(node_id)
        check_key(key)
        check_value(f"value of {key}", value)
        lines = text.split("\n")
        span = cls.find_span(lines, node_id)
        if span is None:
            raise ScopeRefusal("node-unknown", f"node {node_id!r} not found")
        props = cls._prop_lines(lines, *span)
        at = (props[-1] + 1) if props else span[0] + 1
        if not props and at < len(lines) and lines[at].strip() == "":
            at += 1
        lines.insert(at, f"- {key}: {value}")
        return "\n".join(lines)

    @classmethod
    def set_property(cls, text: str, node_id: str, key: str, value: str) -> str:
        """Replace the node's single ``key`` line (inserting it if absent). Refuses a multi-valued key."""
        check_node_id(node_id)
        check_key(key)
        check_value(f"value of {key}", value)
        lines = text.split("\n")
        span = cls.find_span(lines, node_id)
        if span is None:
            raise ScopeRefusal("node-unknown", f"node {node_id!r} not found")
        hits = [i for i in cls._prop_lines(lines, *span) if _PROP.match(lines[i]).group(1) == key]
        if len(hits) > 1:
            raise ScopeRefusal("property-multivalued", f"{node_id}.{key} has {len(hits)} lines; cannot set a single value")
        if hits:
            lines[hits[0]] = f"- {key}: {value}"
            return "\n".join(lines)
        return cls.add_property(text, node_id, key, value)

    @staticmethod
    def append_node(text: str, heading: str, props: Sequence[Tuple[str, str]]) -> str:
        if not isinstance(heading, str) or len(("a" + heading + "b").splitlines()) != 1:
            raise _unsafe("heading", heading, "is not one line")
        m = _HEADING_OUT.fullmatch(heading)
        if m is None:
            raise _unsafe("heading", heading, "is not '<#s> <title> [ID]'")
        check_title(m.group(2))
        for k, v in props:
            check_key(k)
            check_value(f"value of {k}", v)
        body = "\n".join(f"- {k}: {v}" for k, v in props)
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        return f"{text}{sep}{heading}\n\n{body}\n"


class CorpusRegistry:
    """The lts corpus registry file (``corpus-config``) as a JSON object ``{corpus_id: {roots:[{path,area}], ...}}``."""

    def __init__(self, path: str) -> None:
        self.path = path

    def load(self) -> Dict[str, Any]:
        try:
            cfg = read_json(self.path)
        except (OSError, ValueError) as exc:
            raise ModelError(f"corpus registry unreadable at {self.path!r}: {exc}") from exc
        if not isinstance(cfg, dict):
            raise ModelError(f"{self.path!r}: not an object")
        return cfg

    def roots(self, corpus: str) -> List[str]:
        spec = self.load().get(corpus)
        if not isinstance(spec, dict) or not spec.get("roots"):
            raise ScopeRefusal("corpus-unknown", f"corpus {corpus!r} is not in the corpus registry")
        return [r["path"] for r in spec["roots"]]

    def files(self, corpus: str) -> List[str]:
        out: List[str] = []
        for root in self.roots(corpus):
            out.extend(ltsread.iter_lts_files(root))
        return out

    def find_node(self, corpus: str, node_id: str) -> Optional[str]:
        """The file (of the corpus) that defines ``node_id``, or None."""
        for f in self.files(corpus):
            with open(f, "r", encoding="utf-8") as fh:
                if LtsFiles.get_props(fh.read(), node_id) is not None:
                    return f
        return None

    def add(self, corpus: str, entry: Mapping[str, Any], *, enabled: Optional[bool] = None, config: Any = None) -> None:
        """Register a corpus. Gates ITSELF, first (X13 runtime gate, audit-x13-f2-detector F-1): with ``workspace_scope_core`` OFF
        this raises ``ScopeCoreDisabled`` and writes nothing, whoever calls it and however the call is spelled. ``enabled`` / ``config`` are
        the caller's already-resolved flag, exactly as for ``require_enabled`` (``ScopeWriter`` passes its own); omitted, the flag is read here."""
        require_enabled(enabled, config, "CorpusRegistry.add")
        cfg = self.load()
        if corpus in cfg:
            raise ScopeRefusal("corpus-exists", f"corpus {corpus!r} is already registered")
        cfg[corpus] = dict(entry)
        atomic_write_json(self.path, cfg, enabled=enabled, config=config)


class TopicStore:
    """DATA ``<data>/workspaces/<ws>/topics/TOP-nnn.json`` and the machine-wide counter ``<data>/topic-counter.json``."""

    def __init__(self, data_root: str) -> None:
        self.data_root = data_root

    def topics_dir(self, ws: str) -> str:
        return os.path.join(self.data_root, "workspaces", ws, "topics")

    def checked_topics_dir(self, ws: str) -> str:
        """:meth:`topics_dir`, REFUSED (``topic-path-escape``) unless it is a real directory path: no component under the data
        root (``workspaces``, ``<ws>``, ``topics``) may be a symlink (F-R2-5; ruling: refuse, not follow; the same code and
        stance as :meth:`find`'s symlinked topic file). Compared against the data root's own realpath, so a data root that
        itself lives behind a symlink (``/tmp`` on macOS) is fine."""
        d = self.topics_dir(ws)
        want = os.path.join(os.path.realpath(self.data_root), "workspaces", ws, "topics")
        if os.path.realpath(d) != want:
            raise ScopeRefusal("topic-path-escape", f"{d!r} resolves to {os.path.realpath(d)!r}, not {want!r}: a topics directory is never a symlink out of its workspace")
        return d

    def path(self, ws: str, topic_id: str) -> str:
        """``<data>/workspaces/<ws>/topics/<id>.json``. The id is validated by FULLMATCH (``TOP-nnn``) and the workspace is a
        slug, so the result can never leave the workspace's topics dir (F5: a traversal id is refused, not joined)."""
        if not isinstance(topic_id, str) or not TOPIC_ID_RE.fullmatch(topic_id):
            raise ScopeRefusal("topic-id-malformed", f"topic id {topic_id!r} is not TOP-nnn")
        if not isinstance(ws, str) or not SLUG_RE.fullmatch(ws):
            raise ScopeRefusal("topic-id-malformed", f"workspace {ws!r} is not a slug")
        d = self.checked_topics_dir(ws)
        p = os.path.join(d, f"{topic_id}.json")
        if os.path.realpath(os.path.dirname(p)) != os.path.realpath(d):
            raise ScopeRefusal("topic-path-escape", f"{p!r} does not resolve inside {d!r}")
        return p

    def _counter_path(self) -> str:
        return os.path.join(self.data_root, "topic-counter.json")

    def allocate_id(self, *, enabled: Optional[bool] = None, config: Any = None) -> str:
        """The next machine-wide ``TOP-nnn`` id (the counter file is advanced atomically). Gates ITSELF, first (F-1): flag OFF ->
        ``ScopeCoreDisabled``, counter untouched. ``enabled`` / ``config``: as for :func:`~.scopebase.require_enabled`."""
        require_enabled(enabled, config, "TopicStore.allocate_id")
        cp = self._counter_path()
        try:
            cur = int(read_json(cp)["next"])
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ModelError(f"topic counter unreadable at {cp!r}: {exc}") from exc
        atomic_write_json(cp, {"next": cur + 1, "note": "TOP counter is machine-wide (ENT-TOPIC)"}, enabled=enabled, config=config)
        return f"TOP-{cur:03d}"

    def write(self, ws: str, rec: Mapping[str, Any], *, enabled: Optional[bool] = None, config: Any = None) -> str:
        """Write a topic record. Gates ITSELF, first (F-1): flag OFF -> ``ScopeCoreDisabled``, nothing written. ``enabled`` / ``config``:
        as for :func:`~.scopebase.require_enabled`."""
        require_enabled(enabled, config, "TopicStore.write")
        p = self.path(ws, rec["id"])
        atomic_write_json(p, dict(rec), enabled=enabled, config=config)
        return p

    def find(self, topic_id: str) -> Optional[Tuple[str, str, Dict[str, Any]]]:
        """``(workspace, path, record)`` of a topic wherever it lives (machine-wide id)."""
        if not isinstance(topic_id, str) or not TOPIC_ID_RE.fullmatch(topic_id):
            raise ScopeRefusal("topic-id-malformed", f"topic id {topic_id!r} is not TOP-nnn")
        root = os.path.join(self.data_root, "workspaces")
        if not os.path.isdir(root):
            return None
        for ws in sorted(os.listdir(root)):
            if not SLUG_RE.fullmatch(ws):
                continue
            p = self.path(ws, topic_id)
            if os.path.islink(p):
                raise ScopeRefusal("topic-path-escape", f"{p!r} is a symlink: a topic entry is never followed out of its dir")
            if os.path.isfile(p):
                return ws, p, read_json(p)
        return None

    def iter_all(self) -> Iterator[Tuple[str, str, Dict[str, Any]]]:
        root = os.path.join(self.data_root, "workspaces")
        if not os.path.isdir(root):
            return
        for ws in sorted(os.listdir(root)):
            d = self.topics_dir(ws)
            if not os.path.lexists(d):
                continue
            d = self.checked_topics_dir(ws)                  # F-R2-5: a symlinked workspace or topics dir is refused, loudly (never skipped: a skipped dir hides topics)
            if not os.path.isdir(d):
                continue
            for fn in sorted(os.listdir(d)):
                if re.fullmatch(r"TOP-[0-9]{3,}\.json", fn, re.ASCII) and not os.path.islink(os.path.join(d, fn)):
                    p = os.path.join(d, fn)
                    yield ws, p, read_json(p)

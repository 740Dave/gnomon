"""The REAL DSD decision writer + the writer factory.

⭐ D1/D2 (operator-decided). The write path mints each answered DSD as
typed ``[DEC-DSD-*]`` nodes into the D14 DSD-answer corpus ``.lts`` FILE, via the
TYPED factlattice authoring writer (``factlattice_engine.authoring.add_node`` — the
exact function ``lts_add_node`` fronts), so an illegal property key CANNOT be
constructed (the key-grammar hazard) and every write is gate-verified against the
real parser. ``factlattice`` is pip-installed (editable) — an in-process import, no
earlier tooling dependency (clean-room), no shell, no MCP
round-trip.

⭐ SHARDED (2026-10-02): the nodes of each answered DSD go into their OWN file
``<corpus dir>/<dsd_id>.lts`` (the dsd-answers corpus root is the DIRECTORY), never
into one shared file: each typed write re-parses the whole file it writes to, so a
shared 250 KB file cost ~1 s per node (a 14-node save took 20-60 s). The configured
``dsd-answers.lts`` is the LEGACY file: read-only history, consulted only to detect an
id already answered there. Anything else that writes or reads answer nodes must treat
the DIRECTORY as the corpus (specs: rule owed).

⛔ D2 — NO LOCAL INDEX. This writer deliberately does NOT build or refresh a local
``graph_index.json``, and there is no ``index_refresh_owed`` concept. A local index
would hide a connectivity problem with the lts-engine (the operator's reasoning); Gnomon
relies SOLELY on the live lts-engine, which reads the corpus content directly. The
daemon picks up the freshly-minted nodes on its own watch/self-heal. Whether the
answer is actually live-queryable is a LIVE question answered by
:mod:`gnomon.dsd.live` (:func:`~gnomon.dsd.live.check_live_status`) —
never a build-time flag — and when it is not live the UI complains LOUDLY (D2: no
silent degrade to stale local data).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .live import LiveDaemonStatus, default_corpus_id, default_domain_suffix
from .persist import (
    corpus_header,
    DecisionWriter,
    DsdMintRefused,
    DsdNode,
    DsdRollbackIncomplete,
    LtsFileDecisionWriter,
    corpus_lock,
    root_node_id,
)


@dataclass
class WriterStatus:
    """What the UI needs to know about where an answer landed + whether it is live.

    D2 dropped the local-index fields (``index_refreshed`` / ``index_refresh_owed`` /
    ``index_path``) and the hardcoded ``registration_held`` guess. ``live`` is the
    LIVE engine verdict (attached by the controller, which knows the corpus id and
    can probe a just-written node); ``None`` means no live check was run for this
    status object."""

    kind: str                       # "typed" | "file-append" | "fake" | "unknown"
    target: str = ""                # the corpus file / human target string
    detail: str = ""
    live: Optional[LiveDaemonStatus] = None


class TypedLtsDecisionWriter:
    """The REAL writer: typed per-node authoring into the D14 corpus ``.lts`` file.

    ``mint`` writes each node via ``factlattice_engine.authoring.add_node`` (a
    gate-verified typed write — illegal keys impossible). ``factlattice`` is imported
    LAZILY inside ``mint`` so importing this module never requires it; the factory
    decides availability up front. A node whose id already exists (an idempotent
    re-save) is skipped, not double-written.

    ⛔ D2: NO index refresh here. The live engine reads the corpus content; the local
    index is gone. See :mod:`gnomon.dsd.live` for the live-queryable check.

    ⭐ SHARDED (2026-10-02, perf fix). Every typed ``add_node`` re-parses the WHOLE file
    it writes to (the gate), and the key-vocabulary check makes that cost grow with the
    file: 2.8 s/node on the 250 KB shared ``dsd-answers.lts``, i.e. ~40 s for a 14-node
    save. So each DSD's nodes go into their OWN file ``<dsd_id>.lts`` in the corpus
    DIRECTORY (the corpus root is the directory, not one file), where the gate parses
    only a few KB. ``corpus_path`` (the configured / legacy file) is NEVER written
    again: it is only READ, to detect that a node id already lives there (idempotent
    re-save; never a duplicate id across the corpus). Node ids are unchanged."""

    def __init__(
        self,
        corpus_path: str,
        *,
        corpus_id: Optional[str] = None,
        domain_suffix: Optional[str] = None,
        node_level: int = 2,
    ) -> None:
        self._path = os.path.abspath(os.path.expanduser(corpus_path))
        self._corpus_id = corpus_id or default_corpus_id()
        self._domain_suffix = domain_suffix or default_domain_suffix()
        self._level = node_level
        self.last_detail: str = ""

    @property
    def path(self) -> str:
        return self._path

    @property
    def corpus_id(self) -> str:
        return self._corpus_id

    def status(self) -> WriterStatus:
        return WriterStatus(kind="typed", target=self._path, detail=self.last_detail)

    @property
    def corpus_dir(self) -> str:
        """The corpus ROOT: the directory holding the legacy file and every shard."""
        return os.path.dirname(self._path) or "."

    def shard_path(self, dsd_id: str) -> str:
        """The per-DSD shard file ``<corpus_dir>/<dsd_id>.lts``. Refuses an id that is
        not a plain file stem (no separators / dots-only), and one that would land on
        the legacy file itself (a shard must never become the file we only read)."""
        if not _SHARD_STEM_RE.fullmatch(dsd_id or "") or dsd_id in (".", ".."):
            raise ValueError(f"dsd id not usable as a shard file name: {dsd_id!r}")
        path = os.path.join(self.corpus_dir, dsd_id + ".lts")
        if os.path.abspath(path) == self._path:
            raise ValueError(
                f"dsd id {dsd_id!r} would shard onto the legacy corpus file {self._path}")
        # F2 (Diamond): macOS APFS is case-insensitive, so ``DSD-ANSWERS`` IS the legacy
        # file, and any ``00-*`` stem is a conventions file, never a shard.
        legacy_stem = os.path.splitext(os.path.basename(self._path))[0].lower()
        if dsd_id.lower() == legacy_stem:
            raise ValueError(
                f"dsd id {dsd_id!r} equals the legacy corpus file stem {legacy_stem!r} "
                f"(case-insensitively): it would write into {self._path}")
        if dsd_id.startswith("00-") or dsd_id.lower() == "00-dsd-answers":
            raise ValueError(
                f"dsd id {dsd_id!r} is a reserved corpus-conventions stem (00-*): "
                f"it would write into a conventions file, not a shard")
        # F3 (Diamond): node ids derive from the id by ``root_node_id`` (non-alphanumerics
        # -> '-', upper-cased), which is many-to-one, while the shard stem is not. Two
        # different dsd ids that normalise alike (``dsd-a.b`` / ``dsd-a-b``) would mint
        # IDENTICAL node ids into two shards: refuse the second.
        mine = root_node_id(dsd_id)
        try:
            names = os.listdir(self.corpus_dir)
        except FileNotFoundError:
            names = []
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext != ".lts" or stem.lower() == dsd_id.lower():
                continue                       # not a shard, or this DSD's own shard
            if root_node_id(stem) == mine:
                raise ValueError(
                    f"dsd id {dsd_id!r} maps to node id {mine!r}, already minted by the "
                    f"different dsd id {stem!r} (shard {name}): refusing a duplicate id")
        return path

    # -- the write --------------------------------------------------------------

    def mint(self, nodes: List[DsdNode]) -> str:
        """All-or-nothing (X20). Every node is first written to a scratch COPY of
        the corpus through the same typed gate; only if every node parses does the
        real file get touched. A refusal raises :class:`DsdMintRefused` (gate reason
        attached) with the real corpus byte-identical to before. If a real write
        still fails midway (a race the preflight cannot see), THIS mint is undone
        before the error propagates, without clobbering any other write and without
        leaving a header-only seed behind on a first-ever mint (X20 N4, see
        :meth:`_rollback`). Preflight, snapshot, write and rollback all run under
        :func:`~gnomon.dsd.persist.corpus_lock`."""
        from factlattice_engine.authoring import add_node as _add_node

        for node in nodes:
            for k in node.properties:
                if not _is_legal_key(k):
                    raise ValueError(
                        f"illegal property key (not ^[a-z][a-z0-9_-]*$): {k!r}")

        if not nodes:
            self.last_detail = "no nodes to mint"
            return self._path
        dsd_id = _dsd_id_of(nodes)
        shard = self.shard_path(dsd_id)
        os.makedirs(self.corpus_dir, exist_ok=True)
        with corpus_lock(shard):
            # Idempotent re-save across the whole corpus: an id already in the legacy
            # file is NOT written again (the shard's own copy is caught by add_node).
            legacy_ids = _heading_ids(self._path)
            fresh = [n for n in nodes if n.node_id not in legacy_ids]
            skipped = len(nodes) - len(fresh)
            written = (self._mint_locked(_add_node, fresh, shard, dsd_id)
                       if fresh else 0)

        self.last_detail = (
            f"minted {written} node(s) to {shard}"
            + (f" ({skipped} already in the legacy file {self._path}, skipped)"
               if skipped else "")
            + " (no local index — the live lts-engine reads corpus content)")
        return shard

    def _mint_locked(self, _add_node, nodes: List[DsdNode], path: str,
                     dsd_id: str) -> int:
        # -- preflight: dry-run every node against a scratch copy ---------------
        # (X20b: the real corpus seed is created only AFTER the preflight passes, so
        # a first-ever refused mint leaves no header-only file behind.)
        scratch_dir = tempfile.mkdtemp(prefix="dsd-preflight-")
        try:
            scratch = os.path.join(scratch_dir, os.path.basename(path))
            if os.path.exists(path):
                shutil.copyfile(path, scratch)
            else:
                with open(scratch, "w", encoding="utf-8") as fh:
                    fh.write(_shard_header(dsd_id))
            for node in nodes:
                self._write_one(_add_node, scratch, node)
        finally:
            shutil.rmtree(scratch_dir, ignore_errors=True)

        # -- real write, with a non-clobbering rollback if it fails anyway -----
        seeded = self._ensure_corpus_seed(path, dsd_id)
        snapshot = _read_bytes(path)
        expected = snapshot        # what the file holds if only THIS mint wrote to it
        mine: List[Tuple[str, Optional[bytes]]] = []  # (node id, bytes it inserted)
        written = 0
        try:
            for node in nodes:
                if self._write_one(_add_node, path, node):
                    written += 1
                    after = _read_bytes(path)
                    mine.append((node.node_id,
                                 _own_insert(expected, after, node.node_id)))
                    expected = after
        except BaseException as exc:
            self._rollback(path, dsd_id, snapshot, expected, mine, seeded, exc)
            raise
        return written

    def _rollback(self, path: str, dsd_id: str, snapshot: Optional[bytes], expected: Optional[bytes],
                  mine: List[Tuple[str, Optional[bytes]]], seeded: bool,
                  exc: BaseException) -> None:
        """Undo THIS mint only (X20 N4).

        * The file is exactly what our own writes left: restore the snapshot, or
          remove the file if this mint created it (a first-ever mint must not leave
          a header-only seed behind).
        * Something else changed it meanwhile (a writer outside the lock): cut out
          only the bytes our writes inserted. If that cannot be done unambiguously,
          leave the file as found and raise :class:`DsdRollbackIncomplete` (chained
          to the original error) rather than clobber the other write."""
        current = _read_bytes(path)
        if current is None:
            return  # removed underneath us: nothing of ours left to undo
        if current == expected:
            text = snapshot if snapshot is not None else b""
        else:
            text = current
            for node_id, ins in reversed(mine):
                if ins is None or text.count(ins) != 1:
                    raise DsdRollbackIncomplete(
                        str(getattr(exc, "node_id", "") or node_id),
                        f"{type(exc).__name__}: "
                        f"{str(getattr(exc, 'reason', exc))[:300]}",
                        [n for n, _ in mine]) from exc
                text = text.replace(ins, b"", 1)
        if seeded and text == _shard_header(dsd_id).encode("utf-8"):
            os.unlink(path)
        elif text != current:
            _atomic_write_bytes(path, text)

    def _write_one(self, add_node, path: str, node: DsdNode) -> bool:
        """Write one node via the typed gate. True = written, False = already
        present (idempotent re-save). Raises DsdMintRefused on a gate refusal."""
        receipt = add_node(path, node.node_id, node.title,
                           dict(node.properties), level=self._level)
        if receipt.get("written"):
            return True
        reason = str(receipt.get("reason", "")) + str(receipt.get("report", ""))
        if "already" in reason.lower() or "exists" in reason.lower():
            return False
        raise DsdMintRefused(
            node.node_id, str(receipt.get("reason", "") or reason)[:400])

    def _ensure_corpus_seed(self, path: str, dsd_id: str) -> bool:
        """Create the shard with its header if it is missing; True iff created."""
        if os.path.exists(path):
            return False
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_shard_header(dsd_id))
        return True


_SHARD_STEM_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HEADING_ID_RE = re.compile(r"^#+ .*\[([^\[\]]+)\]\s*$")


def _dsd_id_of(nodes: List[DsdNode]) -> str:
    """The one DSD id every node of this mint belongs to (its ``dsd-id`` property)."""
    ids = {n.properties.get("dsd-id", "") for n in nodes}
    if len(ids) != 1 or not next(iter(ids)):
        raise ValueError(
            f"a mint must carry exactly one non-empty dsd-id property, got {sorted(ids)!r}")
    return next(iter(ids))


def _heading_ids(path: str) -> frozenset:
    """Every ``[ID]`` carried by a heading line of ``path`` (empty when absent). A
    plain line scan, not a parse: it is the cheap duplicate-id guard against the
    legacy file, and it stays linear in the file size (milliseconds at 20k lines)."""
    ids = set()
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("#"):
                    m = _HEADING_ID_RE.match(line.rstrip("\n"))
                    if m:
                        ids.add(m.group(1))
    except FileNotFoundError:
        pass
    return frozenset(ids)


#: the root id every per-DSD shard header is built on (document tag + the GM- spec-id family): ``-<DSD-ID>``
SHARD_ROOT_ID = "SPEC:GM-DSD-ANSWERS"


def _shard_header(dsd_id: str) -> str:
    """Front matter + root heading for one shard. Same corpus header the shared file
    uses, but the root heading id is unique per shard so N shards never collide on
    the unsharded root heading id when the engine indexes the directory."""
    header = corpus_header()  # neutral defaults overlaid by the deployment's dsd-corpus.json, at CALL time
    m = re.search(r"\[(SPEC:[A-Z0-9-]+)\]\s*$", header.rstrip("\n"))
    if m is None:  # fail loud: a header without its root heading id cannot be sharded
        raise ValueError("corpus_header() has no root heading id to shard")
    # frontmatter id == root heading id. Every NEW answer id carries the product's own spec-id prefix (GM-), whatever the deployment's
    # corpus id is; the frozen old collection's ids are never touched.
    return corpus_header(root_id=f"{SHARD_ROOT_ID}-{dsd_id.upper()}")


def _read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def _atomic_write_bytes(path: str, data: bytes) -> None:
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".dec-dsd-rollback-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _own_insert(before: Optional[bytes], after: Optional[bytes],
                node_id: str) -> Optional[bytes]:
    """The contiguous bytes one ``add_node`` inserted (``after`` is ``before`` with
    one block spliced in), or None when the change is not a single insertion that
    carries exactly one heading, this node's (then it cannot be undone surgically)."""
    if before is None or after is None or len(after) <= len(before):
        return None
    lim = len(before)
    p = 0
    while p < lim and before[p] == after[p]:
        p += 1
    q = 0
    while q < lim - p and before[-1 - q] == after[-1 - q]:
        q += 1
    ins = after[p:len(after) - q]
    if before[:p] + ins + before[lim - q:] != after:
        return None
    headings = [ln for ln in ins.split(b"\n") if ln.lstrip().startswith(b"#")]
    if len(headings) != 1 or ("[" + node_id + "]").encode("utf-8") not in headings[0]:
        return None
    return ins


class RefreshRecordingWriter:
    """An INSPECTABLE fake writer — the test double for the wired write path. It
    records the minted nodes so a headless test can assert the wired path persists.

    (Name kept for continuity; D2 removed the index-refresh it once modelled. It no
    longer claims any local-index state — the live-queryable question is answered by
    :func:`gnomon.dsd.live.check_live_status`, not by the writer.)"""

    def __init__(self) -> None:
        self.minted: List[DsdNode] = []
        self.calls: int = 0
        self.corpus_id: str = default_corpus_id()

    def mint(self, nodes: List[DsdNode]) -> str:
        self.calls += 1
        self.minted.extend(nodes)
        return f"typed-fake:{len(self.minted)} nodes"

    def status(self) -> WriterStatus:
        return WriterStatus(kind="fake", target="typed-fake",
                            detail=f"{self.calls} mint call(s)")

    def node_ids(self) -> List[str]:
        return [n.node_id for n in self.minted]

    def by_id(self, node_id: str) -> Optional[DsdNode]:
        for n in self.minted:
            if n.node_id == node_id:
                return n
        return None


# -- the factory -------------------------------------------------------------

def factlattice_available() -> bool:
    """True when the typed factlattice authoring writer is importable in-process."""
    try:
        import factlattice_engine.authoring  # noqa: F401
        import factlattice  # noqa: F401

        return True
    except Exception:  # noqa: BLE001
        return False


def make_decision_writer(
    corpus_path: str,
    *,
    prefer_typed: bool = True,
) -> DecisionWriter:
    """Build the REAL DecisionWriter.

    Prefers :class:`TypedLtsDecisionWriter` (typed, gate-verified) when
    ``factlattice`` is importable. Falls back to
    :class:`~gnomon.dsd.persist.LtsFileDecisionWriter` (a plain file-append to
    the same corpus ``.lts``) ONLY when it is not — both write the file the live
    engine reads; neither builds a local index (D2)."""
    if prefer_typed and factlattice_available():
        return TypedLtsDecisionWriter(corpus_path)
    return LtsFileDecisionWriter(corpus_path)


def writer_status(writer: object) -> WriterStatus:
    """Duck-type ANY DecisionWriter into a :class:`WriterStatus` for the UI —
    including the file-append writer (no ``status()`` method)."""
    status = getattr(writer, "status", None)
    if callable(status):
        result = status()
        if isinstance(result, WriterStatus):
            return result
    target = getattr(writer, "path", "") or ""
    kind = "file-append" if isinstance(writer, LtsFileDecisionWriter) else "unknown"
    return WriterStatus(
        kind=kind, target=str(target),
        detail=("file-append writer — nodes appended to the corpus file (no local "
                "index; the live lts-engine reads corpus content)"))


def _is_legal_key(key: str) -> bool:
    import re

    return bool(re.fullmatch(r"[a-z][a-z0-9_-]*", key))

"""The durable DSD-answer notice queue (D15 amendment,
operator-approved 2026-09-29).

On a save that minted nodes, the hub composes ONE small, actionable notice — a
POINTER, never the answer text (D15's no-content rule holds) — and ENQUEUES it
durably BEFORE attempting delivery:

    <runtime_root>/notices/<instance_id>/<dsd_id>.json      (0600, dirs 0700)

Delivery rides the harness's provenance-logged inject path (``source=dsd-answer``,
``clear=False``). The file is deleted ONLY when the harness sends
``{"type": "notice_ack", "dsd_id": ...}`` after its injector logged the write.
A live proxy's pending notices are re-sent on every (re)register, so a session
that was not live receives its notices at its next boot / re-attach.

Guarantee: at-least-once, de-duplicated by ``dsd_id`` on the harness side.

stdlib only.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any, Callable, Dict, List, Optional

from ..profile import _validate_instance_id, ensure_contained
from .live import DsdCorpusError, default_corpus_id

#: The provenance source a DSD-answer notice is recorded under (harness side).
NOTICE_SOURCE = "dsd-answer"
#: The provenance ``trigger`` for the same records.
NOTICE_TRIGGER = "dsd-answer"

#: FIX PASS: a notice older than this is moved to ``<instance>/expired/`` instead
#: of being delivered (an instance that never boots again must not accrete a
#: stale queue, and a 3-week-old "answered" pointer is noise, not news).
NOTICE_TTL_SECONDS = 14 * 86400
#: A notice file larger than this is not ours.
MAX_NOTICE_BYTES = 64 * 1024
EXPIRED_DIRNAME = "expired"
CORRUPT_SUFFIX = ".corrupt"

_DSD_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

#: FIX PASS 2 (re-audit HIGH): the STRICT notice-id grammar. Real ids are
#: ``dsd-<hex ms timestamp>`` (hub/mcp.py) and ``DEC-DSD-<UPPER>`` (persist.
#: root_node_id), e.g. /. A notice is typed
#: ONLY as the fixed template below, rebuilt from these two validated ids — never
#: from a ``text`` field (notices/ lives on a synced tree, 0644 is
#: normal there, so a planted file's text must be inert).
NOTICE_DSD_ID_RE = re.compile(r"\Adsd-[0-9a-f]{6,32}\Z")
NOTICE_ROOT_ID_RE = re.compile(r"\ADEC-DSD-[0-9A-F]{6,32}\Z")
NOTICE_TEMPLATE = ('[DSD ANSWERED] {dsd_id} — read the answer: '
                   'lts_node(corpus="{corpus}", node_id="{root_id}")')


def valid_notice_ids(dsd_id: Any, root_id: Any) -> bool:
    """Both ids match the strict grammar AND the root derives from the dsd_id."""
    return (isinstance(dsd_id, str) and isinstance(root_id, str)
            and NOTICE_DSD_ID_RE.match(dsd_id) is not None
            and NOTICE_ROOT_ID_RE.match(root_id) is not None
            and root_id == "DEC-DSD-" + dsd_id[4:].upper())


def notice_line(dsd_id: str, root_id: str) -> str:
    """THE only text ever typed for a DSD-answer notice. Raises ValueError on ids
    outside the strict grammar."""
    if not valid_notice_ids(dsd_id, root_id):
        raise ValueError(f"notice ids refused by the strict grammar: {dsd_id!r} / {root_id!r}")
    return NOTICE_TEMPLATE.format(dsd_id=dsd_id, root_id=root_id, corpus=default_corpus_id())


# --- the INLINE answer (pre-review B2; flag dsd_notice_inline, default OFF) --------------------------------------------------
#
# With the flag ON the harness types, instead of the one pointer line, the pointer line PLUS the answer itself. The text is built HERE
# from the MINTED answer nodes (the corpus, the source of record), never from a request body, a notice file or a frame: the file's
# ``text`` is still ignored (fix pass 2). Every continuation line starts with ``| ``, control characters in his comment are shown as
# ``\xNN`` escapes, and the harness re-screens the finished text (``validate_inline_text`` and ``screen_hub_inject``) before it types
# a byte; any failure at all means the agent gets today's pointer line.

#: the whole inline notice, in characters. Over this, comments are cut (INLINE_CUT_MARK) at a stated length.
INLINE_LIMIT_CHARS = 4000
INLINE_CUT_MARK = "(cut; full text at the node)"
INLINE_LINE_PREFIX = "| "
#: comment lengths tried, longest first, until the whole notice fits INLINE_LIMIT_CHARS.
INLINE_CUT_LENGTHS = (1500, 800, 400, 200, 100, 50)


def _shown(text: Any) -> str:
    """``text`` with every control character (C0 including ESC, DEL, C1, the line/paragraph separators) shown as a literal
    ``\\xNN`` / ``\\uNNNN`` escape and a tab as a space, so nothing in it can act as a key, a line break or an escape sequence."""
    out = []
    for c in str(text):
        o = ord(c)
        if c == "\t":
            out.append(" ")
        elif o < 0x20 or o == 0x7F or 0x80 <= o <= 0x9F:
            out.append("\\x%02x" % o)
        elif o in (0x2028, 0x2029):
            out.append("\\u%04x" % o)
        else:
            out.append(c)
    return "".join(out)


def _node_lines(props: Dict[str, str], node_id: str, cut: Optional[int]) -> List[str]:
    ref = _shown(props.get("decision-ref", "?"))
    chosen = _shown(props.get("chosen", "(not recorded)"))
    where = ""
    pos, cnt = props.get("option-position-chosen"), props.get("option-count")
    if pos and cnt and pos.isdigit() and cnt.isdigit() and int(pos) > 0:
        where = f"option {int(pos)} of {int(cnt)}: "
    agreed = props.get("agreed-with-recommendation", "n/a")
    agreed_words = {"true": "yes", "false": "no"}.get(agreed, "n/a (no recommendation)")
    comment = props.get("comment", "(none)")
    mark = ""
    if cut is not None and len(comment) > cut:
        comment, mark = comment[:cut], " " + INLINE_CUT_MARK
    return [f"{INLINE_LINE_PREFIX}{ref}: chose {where}{chosen}; matched your recommendation: {agreed_words}",
            f"{INLINE_LINE_PREFIX}comment: {_shown(comment)}{mark}",
            f"{INLINE_LINE_PREFIX}node: {_shown(node_id)}"]


def compose_inline_notice(dsd_id: str, root_id: str, nodes: List[Dict[str, Any]], *, limit: int = INLINE_LIMIT_CHARS) -> str:
    """The inline notice: today's pointer line, then per answered decision its ref, the option chosen (text and position), whether
    it matched the recommendation, his comment verbatim (control characters shown as escapes) and the answer node id. Over ``limit``
    characters the comments are cut at a stated length with :data:`INLINE_CUT_MARK`. Raises ``ValueError`` on bad ids or no nodes."""
    head = notice_line(dsd_id, root_id)
    if not nodes:
        raise ValueError("no answer nodes to build the inline notice from")
    for cut in (None,) + INLINE_CUT_LENGTHS:
        lines = [head]
        for n in nodes:
            lines += _node_lines(n.get("props") or {}, str(n.get("node_id", "?")), cut)
        if cut is not None:
            lines.append(f"{INLINE_LINE_PREFIX}(comments over {cut} characters are cut; full text at the node)")
        text = "\n".join(lines)
        if len(text) <= limit:
            return text
    raise ValueError(f"the inline notice does not fit {limit} characters even with the shortest comments")


def validate_inline_text(text: Any, dsd_id: str, root_id: str) -> Optional[str]:
    """``None`` when ``text`` is a well-formed inline notice for these ids, else the reason. The harness calls this on the finished
    text before typing it; it does not trust the builder."""
    if not isinstance(text, str):
        return "not text"
    if len(text) > INLINE_LIMIT_CHARS:
        return "over the length limit"
    head = notice_line(dsd_id, root_id)
    lines = text.split("\n")
    if lines[0] != head:
        return "does not start with the pointer line"
    if any(not ln.startswith(INLINE_LINE_PREFIX) for ln in lines[1:]):
        return "a continuation line does not start with the fixed prefix"
    from ..harness.compact_gate import screen_hub_inject
    from ..harness.leading_bang import screen_text

    if screen_text(text) or screen_hub_inject(text):
        return "refused by the inject screens"
    return None


def inline_text_for(dsd_id: str, root_id: str, *, config: Any = None, answers_path: Optional[str] = None) -> Optional[str]:
    """What the harness asks at ``offer``: ``None`` = type today's pointer line (flag OFF, or nothing to build from); a string = the
    inline notice, built from the minted nodes. Raises on a real failure (the caller falls back and says so)."""
    from .. import flags as flags

    if config is None:
        from ..config import ConfigStore

        config = ConfigStore().load()
    if not flags.flag("dsd_notice_inline", config=config):
        return None
    from . import dsd_ops

    path = answers_path if answers_path is not None else dsd_ops.answers_path_for(config)
    nodes = dsd_ops.read_answer_nodes(path, dsd_id)
    if not nodes:
        raise ValueError(f"no answer nodes for {dsd_id} in {path}")
    return compose_inline_notice(dsd_id, root_id, nodes)


def validate_dsd_id(dsd_id: Any) -> str:
    """A dsd_id names a FILE here — reject separators, traversal, dot-files."""
    if not isinstance(dsd_id, str) or not _DSD_ID_RE.match(dsd_id) or ".." in dsd_id:
        raise ValueError(f"invalid dsd_id {dsd_id!r}")
    return dsd_id


def compose_notice(
    dsd_id: str,
    root_id: str,
    *,
    n_decisions: int = 0,
    answered_by: Optional[str] = None,
    corpus: Optional[str] = None,
) -> str:
    """The one-line notice (FIX PASS 2: the FIXED template, see :func:`notice_line`).

    ``n_decisions`` / ``answered_by`` / ``corpus`` are accepted for call
    compatibility and deliberately NOT rendered: nothing free-form reaches the
    typed line. Names the root ``DEC-DSD-*`` node and exactly how to read it; one
    line, so the injector never wraps it in a bracketed paste."""
    return notice_line(dsd_id, root_id)


def notice_command(notice: Dict[str, Any]) -> Dict[str, Any]:
    """The hub->proxy frame for one queued notice (the logged inject path)."""
    return {
        "cmd": "inject",
        "text": notice_line(notice["dsd_id"], notice["root_id"]),  # fix pass 2: rebuilt
        "source": NOTICE_SOURCE,
        "trigger": NOTICE_TRIGGER,
        "clear": False,
        "dsd_id": notice["dsd_id"],
        "root_id": notice["root_id"],
    }


class NoticeQueue:
    """File-per-notice durable queue under ``root`` (``<runtime_root>/notices``)."""

    def __init__(self, root: str, *, on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
                 ttl_seconds: float = NOTICE_TTL_SECONDS,
                 clock: Callable[[], float] = time.time) -> None:
        self.root = os.path.abspath(os.path.expanduser(root))
        # FIX PASS: a LOUD sink for corrupt / refused / expired notices. Default:
        # one line on stderr (the :8770 daemon log). Tests inject a list.
        self._on_event = on_event
        self._ttl = float(ttl_seconds)
        self._clock = clock

    def _event(self, **ev: Any) -> None:
        if self._on_event is not None:
            try:
                self._on_event(dict(ev))
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            sys.stderr.write("[gm-notice] " + json.dumps(ev, default=str) + "\n")
        except Exception:  # noqa: BLE001
            pass

    # -- paths -----------------------------------------------------------------

    def _dir(self, instance_id: str) -> str:
        _validate_instance_id(instance_id)
        if instance_id.startswith("."):
            raise ValueError(f"unsafe instance_id: {instance_id!r}")
        # symlink-escape guard (same primitive the profile/DSD stores use)
        ensure_contained(self.root, os.path.join(self.root, instance_id))
        return os.path.join(self.root, instance_id)

    def path_for(self, instance_id: str, dsd_id: str) -> str:
        return os.path.join(self._dir(instance_id), validate_dsd_id(dsd_id) + ".json")

    # (fix pass 2) enqueue/pending enforce the STRICT grammar via notice_line.

    # -- write -----------------------------------------------------------------

    def enqueue(
        self,
        instance_id: str,
        dsd_id: str,
        *,
        root_id: str,
        text: str,
        n_decisions: Optional[int] = None,
        answered_by: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Durably write one notice (temp 0600 + fsync + ``os.replace``). Idempotent
        per ``(instance_id, dsd_id)``: re-enqueueing replaces the file."""
        path = self.path_for(instance_id, dsd_id)
        # FIX PASS 2: the stored text is the fixed template, rebuilt here; the
        # caller's ``text`` is ignored (and the harness never types the file's
        # text anyway — it rebuilds from the validated ids too).
        text = notice_line(dsd_id, root_id)
        record = {
            "schema": 1,
            "instance_id": instance_id,
            "dsd_id": dsd_id,
            "root_id": root_id,
            "text": str(text),
            "n_decisions": n_decisions,
            "answered_by": answered_by,
            "enqueued_epoch": time.time(),
        }
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{time.monotonic_ns()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                os.write(fd, (json.dumps(record, sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException:
            try:                      # FIX PASS: never leave a .tmp behind
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        try:  # make the rename itself durable
            dfd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
        record["path"] = path
        return record

    # -- read ------------------------------------------------------------------

    def pending(self, instance_id: str) -> List[Dict[str, Any]]:
        """Every deliverable queued notice for ``instance_id``, oldest first.

        FIX PASS (independent audit): reads are HARDENED like rendezvous.json —
        non-blocking, no-follow open, then regular-file / owner-uid / not group-
        other-writable / size-capped. A notice that is unreadable-as-ours, corrupt
        or malformed is MOVED ASIDE to ``<name>.corrupt`` (never deleted, never
        delivered) with a LOUD event. One older than the TTL (14 days) is MOVED to
        ``<instance>/expired/`` instead of being delivered."""
        directory = self._dir(instance_id)
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            return []
        out: List[Dict[str, Any]] = []
        now = self._clock()
        for name in names:
            if not name.endswith(".json") or name.startswith("."):
                continue
            path = os.path.join(directory, name)
            dsd_id = name[:-len(".json")]
            try:
                rec, reason = self._read_notice(path, dsd_id)
            except FileNotFoundError:
                continue
            if reason is not None:
                moved = self._move_aside(path, path + CORRUPT_SUFFIX)
                self._event(event="notice-corrupt", instance_id=instance_id, dsd_id=dsd_id,
                            reason=reason, moved_to=moved)
                continue
            enq = rec.get("enqueued_epoch")
            if self._past_ttl(rec, now):
                exp_dir = os.path.join(directory, EXPIRED_DIRNAME)
                try:
                    os.makedirs(exp_dir, mode=0o700, exist_ok=True)
                except OSError:
                    pass
                moved = self._move_aside(path, os.path.join(exp_dir, name))
                self._event(event="notice-expired", instance_id=instance_id, dsd_id=dsd_id,
                            age_days=round((now - float(enq)) / 86400, 1), moved_to=moved)
                continue
            rec["path"] = path
            out.append(rec)
        out.sort(key=lambda r: (r.get("enqueued_epoch") or 0.0, r["dsd_id"]))
        return out

    # -- shared classification (pending + peek agree by construction) ---------

    @staticmethod
    def _read_notice(path: str, dsd_id: str):
        """Read + validate one queued notice file WITHOUT side effects. Returns
        ``(record, None)`` for a deliverable-shaped record (its ``text`` REBUILT
        from the validated ids) or ``(None, reason)`` for one that is unreadable-as-
        ours / corrupt / outside the strict id grammar. Raises FileNotFoundError
        when the file vanished (an ack raced the read)."""
        from ..hub.rendezvous import safe_read_bytes

        try:
            rec = json.loads(safe_read_bytes(path, MAX_NOTICE_BYTES).decode("utf-8"))
        except FileNotFoundError:
            raise
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            return None, f"{type(exc).__name__}: {exc}"
        if (not isinstance(rec, dict) or rec.get("dsd_id") != dsd_id
                or not valid_notice_ids(rec.get("dsd_id"), rec.get("root_id"))):
            return None, "malformed notice record (ids outside the strict grammar)"
        # FIX PASS 2: whatever the file says, the text is REBUILT
        try:
            rec["text"] = notice_line(rec["dsd_id"], rec["root_id"])
        except DsdCorpusError:
            # g70: a bad dsd-corpus.json must not break READING the queue (display, drain, quarantine). The record is
            # still a valid notice; its text is rebuilt (and refused, by name) where it is SENT (notice_command /
            # NoticeInbox.offer), so a fixed file delivers it. Nothing is quarantined for a config fault.
            rec["text"] = ""
        return rec, None

    def _past_ttl(self, rec: Dict[str, Any], now: float) -> bool:
        enq = rec.get("enqueued_epoch")
        return isinstance(enq, (int, float)) and now - float(enq) > self._ttl

    # -- PURE read (the dashboard's "queued for <instance>" display) --

    def peek(self, instance_id: str) -> Dict[str, Any]:
        """A SIDE-EFFECT-FREE snapshot of ``instance_id``'s queue, for display.

        ⛔ Unlike :meth:`pending` (the delivery path), this NEVER moves, renames,
        creates or deletes anything and emits no event: a dashboard poll every few
        seconds must not quarantine or expire files (the go-live smoke finding).
        It classifies exactly as ``pending`` would — via the same helpers — so what
        it reports is what the next drain will do:

        * ``queued``      — deliverable notices (``dsd_id``, ``root_id``,
                            ``enqueued_epoch``), oldest first;
        * ``expired``     — files already in ``expired/`` PLUS queued files past the
                            TTL that the next drain will move there (``expired_ids``);
        * ``corrupt``     — ``*.corrupt`` files PLUS queued files the next drain
                            would quarantine (unreadable-as-ours / malformed)."""
        directory = self._dir(instance_id)
        snap: Dict[str, Any] = {"instance_id": instance_id, "queued": [], "expired": 0,
                                "expired_ids": [], "corrupt": 0}
        try:
            names = os.listdir(directory)
        except (FileNotFoundError, NotADirectoryError):
            return snap
        now = self._clock()
        for name in names:
            if name.startswith("."):
                continue
            if name.endswith(CORRUPT_SUFFIX):
                snap["corrupt"] += 1
                continue
            if not name.endswith(".json"):
                continue
            dsd_id = name[:-len(".json")]
            try:
                rec, reason = self._read_notice(os.path.join(directory, name), dsd_id)
            except FileNotFoundError:
                continue
            if reason is not None:
                snap["corrupt"] += 1
            elif self._past_ttl(rec, now):
                snap["expired"] += 1
                snap["expired_ids"].append(dsd_id)
            else:
                snap["queued"].append({"dsd_id": rec["dsd_id"], "root_id": rec["root_id"],
                                       "enqueued_epoch": rec.get("enqueued_epoch")})
        try:
            for name in os.listdir(os.path.join(directory, EXPIRED_DIRNAME)):
                if name.endswith(".json") and not name.startswith("."):
                    snap["expired"] += 1
                    snap["expired_ids"].append(name[:-len(".json")])
        except (FileNotFoundError, NotADirectoryError):
            pass
        snap["queued"].sort(key=lambda r: (r.get("enqueued_epoch") or 0.0, r["dsd_id"]))
        snap["expired_ids"].sort()
        return snap

    def peek_all(self) -> Dict[str, Dict[str, Any]]:
        """:meth:`peek` for every instance directory (pure; bad ids skipped)."""
        out: Dict[str, Dict[str, Any]] = {}
        for inst in self.instances():
            try:
                out[inst] = self.peek(inst)
            except ValueError:
                continue
        return out

    @staticmethod
    def _move_aside(src: str, dst: str) -> Optional[str]:
        try:
            os.replace(src, dst)
            return dst
        except OSError:
            return None

    def instances(self) -> List[str]:
        try:
            return sorted(n for n in os.listdir(self.root)
                          if os.path.isdir(os.path.join(self.root, n)))
        except FileNotFoundError:
            return []

    # -- ack -------------------------------------------------------------------

    def ack(self, instance_id: str, dsd_id: str) -> bool:
        """Delete one notice — called ONLY on the owning harness's ``notice_ack``.
        Returns whether a file was removed. Invalid ids are refused (False)."""
        try:
            path = self.path_for(instance_id, dsd_id)
        except ValueError:
            return False
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False


def written_notice_times(log_path: Optional[str]) -> Dict[str, float]:
    """``{dsd_id: ts}`` of every DSD-answer notice a harness provably WROTE, read
 (pure) from one session's ``injection_log.jsonl`` ("delivered <time>").

    Only ``{kind: dsd-notice, decision: written}`` counts — it is appended AFTER the
    bytes + CR (harness.notice_inbox); the gate's ``allow`` record precedes the
    write and proves nothing. The FIRST written record per id is the delivery."""
    out: Dict[str, float] = {}
    if not log_path:
        return out
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"dsd-notice"' not in line or '"written"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if (isinstance(rec, dict) and rec.get("kind") == "dsd-notice"
                        and rec.get("decision") == "written"
                        and isinstance(rec.get("dsd_id"), str)
                        and isinstance(rec.get("ts"), (int, float))
                        and rec["dsd_id"] not in out):
                    out[rec["dsd_id"]] = float(rec["ts"])
    except OSError:
        return {}
    return out

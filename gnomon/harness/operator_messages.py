"""Operator messages: a FULL copy of every one, and a delivery check.

Why this exists (2026-10-02T04:40:19Z): a 1,097-character ``\\all`` broadcast reached
every session as its last 85 characters only, and the text was unrecoverable because
``injection_log.jsonl`` keeps a ~120-character preview. Two additive pieces:

* :class:`OperatorMessageLog` (requirement 2): every operator message (``\\all``,
  ``\\<instance>`` DM, every ``\\command`` line) is appended IN FULL, with its sha256,
  to ``<DATA root>/operator-messages/operator-messages.jsonl`` (dir 0700, file 0600,
  size-rotated). The injection_log records carry the same sha256 (``content_sha256``
  / ``text_sha256``) and the broadcast's ``msg_id``, so the two link. A delivery
  record adds the recipients once the hub answers.
* :class:`DeliveryChecker` (requirement 3): after a broadcast is typed into a
  session, it reads that session's Claude Code transcript (read-only) for the prompt
  the TUI actually submitted, normalises it (Claude Code wraps a long paste in
  ``<pasted_content id=..>`` and escapes nested tags) and compares hashes. A
  different text is a ``mismatch``; nothing seen inside the window is
  ``unverified`` (never reported as a pass).

Both are behind default-ON flags (purely additive: they never change what is typed),
read once at launch. Never raises into a caller; every failure is counted/recorded.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

#: Flags (registered in ``gnomon.flags.KNOWN_FLAGS``; default ON).
FLAG_LOG = "operator_messages_log"
FLAG_DELIVERY_CHECK = "operator_delivery_check"

#: Env override for the log directory (tests point it at a temp dir; never the real one).
ENV_DIR = "GNOMON_OPERATOR_MESSAGES_DIR"
LOG_SUBDIR = "operator-messages"
LOG_FILENAME = "operator-messages.jsonl"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
DEFAULT_KEEP = 5
#: round 3 (Diamond F17): the production log is written INLINE on the operator's
#: typing path (``\\all``, ``\\command``), so its cross-process lock wait is capped here,
#: not at the class default of 5 s. A normal append holds the lock for milliseconds; only a
#: wedged sibling holder reaches the cap, and that is a reported failure (g65(c)).
TYPING_PATH_LOCK_TIMEOUT = 0.3

#: Delivery-check verdicts.
MATCH = "match"
MISMATCH = "mismatch"
UNVERIFIED = "unverified"


def sha256_text(text: str) -> str:
    """Same encoding as ``hooks.content_fields`` so the hashes link byte for byte."""
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


def new_msg_id() -> str:
    return uuid.uuid4().hex[:16]


def default_dir(runtime_root: Optional[str] = None) -> str:
    """``$GNOMON_OPERATOR_MESSAGES_DIR``, else ``<DATA root>/operator-messages``. The DATA
    root is the config's ``runtime_root`` (the per-user data root; a symlink is fine:
    paths are opened, never ``find``-walked)."""
    env = os.environ.get(ENV_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    if not runtime_root:
        from ..config import runtime_root_default

        runtime_root = runtime_root_default()
    return os.path.join(os.path.abspath(os.path.expanduser(runtime_root)), LOG_SUBDIR)


class OperatorMessageLog:
    """Append-only, size-rotated JSONL of FULL operator messages. Thread- AND
    process-safe (round 2, Diamond F5b).

    PATH BOUNDARY (round 2, Diamond F5): ancestors of ``directory`` are TRUSTED and
    may be symlinks: the DATA root is a cloud-sync symlink by
    design. ``directory`` itself (the ``operator-messages/`` leaf) and everything in
    it are NOT: the leaf is opened ``O_DIRECTORY|O_NOFOLLOW`` (a symlinked leaf is
    refused by the kernel, no lstat/chmod TOCTOU), and the log, its rotations and the
    lock file are all addressed relative to that directory fd, files opened
    ``O_NOFOLLOW|O_NONBLOCK`` and required to be regular (a planted symlink or FIFO
    is refused, never followed or blocked on).

    CONCURRENCY: sibling proxies share the file. Rotate-then-append runs under an
    exclusive ``flock`` on ``operator-messages.lock`` in the same directory, so a
    rotation can never race another writer's rename chain or append (measured
    before: 8 of 400 records lost with two writers). The lock wait is bounded
    (``lock_timeout``); a timeout is a write FAILURE, reported like any other.

    FAILURES are never silent (g65(c)): :meth:`append` returns ``None``, counts
    :attr:`write_errors`, keeps :attr:`last_error`, and calls ``on_error(message)``
    (the harness wires that to a loud provenance record plus an operator-visible
    line). A raising ``on_error`` is swallowed.
    """

    LOCK_FILENAME = "operator-messages.lock"

    def __init__(self, directory: str, *, max_bytes: int = DEFAULT_MAX_BYTES, keep: int = DEFAULT_KEEP,
                 clock: Callable[[], float] = time.time,
                 on_error: Optional[Callable[[str], None]] = None, lock_timeout: float = 5.0) -> None:
        self.directory = directory
        self.path = os.path.join(directory, LOG_FILENAME)
        self.max_bytes = int(max_bytes)
        self.keep = max(1, int(keep))
        self._clock = clock
        self._lock = threading.Lock()
        self.on_error = on_error
        self.lock_timeout = float(lock_timeout)
        self.write_errors = 0
        self.last_error: Optional[str] = None

    # -- internals -------------------------------------------------------------

    _FILE_FLAGS = os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)

    def _open_dir(self) -> int:
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        dfd = os.open(self.directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
                      | getattr(os, "O_CLOEXEC", 0))
        try:
            st = os.fstat(dfd)
            if not stat.S_ISDIR(st.st_mode):
                raise OSError(f"{self.directory} is not a directory")
            if stat.S_IMODE(st.st_mode) != 0o700:
                os.fchmod(dfd, 0o700)
        except BaseException:
            os.close(dfd)
            raise
        return dfd

    def _open_regular(self, name: str, flags: int, dfd: int) -> int:
        fd = os.open(name, flags | self._FILE_FLAGS, 0o600, dir_fd=dfd)
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(f"{os.path.join(self.directory, name)} is not a regular file")
            if stat.S_IMODE(st.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
        except BaseException:
            os.close(fd)
            raise
        return fd

    def _open_lock(self, dfd: int) -> int:
        """Open (creating) the lock file. Round 3: MEASURED on macOS APFS, two processes
        racing the FIRST ``openat(O_CREAT)`` of the lock name can get a transient ENOENT
        (1 of 4 writers, about 2 runs in 3, in the concurrency test). Retry briefly; any
        other error, or ENOENT persisting past ~0.25 s, is a reported failure."""
        for _ in range(50):
            try:
                return self._open_regular(self.LOCK_FILENAME, os.O_RDWR | os.O_CREAT, dfd)
            except FileNotFoundError:
                time.sleep(0.005)
        return self._open_regular(self.LOCK_FILENAME, os.O_RDWR | os.O_CREAT, dfd)

    def _acquire(self, lfd: int) -> None:
        import fcntl

        deadline = time.monotonic() + self.lock_timeout
        while True:
            try:
                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"operator-messages lock busy for {self.lock_timeout:.1f}s")
                time.sleep(0.005)

    def _rotate_if_needed(self, dfd: int) -> None:
        try:
            st = os.stat(LOG_FILENAME, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(st.st_mode) or st.st_size < self.max_bytes:
            return
        for i in range(self.keep - 1, 0, -1):
            src = f"{LOG_FILENAME}.{i}"
            try:
                os.rename(src, f"{LOG_FILENAME}.{i + 1}", src_dir_fd=dfd, dst_dir_fd=dfd)
            except FileNotFoundError:
                pass
        os.rename(LOG_FILENAME, f"{LOG_FILENAME}.1", src_dir_fd=dfd, dst_dir_fd=dfd)

    def _write_line(self, line: bytes, dfd: int) -> None:
        fd = self._open_regular(LOG_FILENAME, os.O_WRONLY | os.O_APPEND | os.O_CREAT, dfd)
        try:
            view = memoryview(line)
            while view:
                n = os.write(fd, view)
                if n <= 0:  # pragma: no cover - defensive
                    raise OSError("short write")
                view = view[n:]
        finally:
            os.close(fd)

    def _fail(self, exc: BaseException) -> None:
        self.write_errors += 1
        self.last_error = f"{type(exc).__name__}: {exc}"
        sink = self.on_error
        if sink is not None:
            try:
                sink(self.last_error)
            except Exception:  # noqa: BLE001
                pass

    # -- public ------------------------------------------------------------------

    def append(self, record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Append one record (``ts``/``ts_iso`` added). Returns the record, or ``None``
        on failure (counted, and reported through ``on_error``; never raises)."""
        ts = self._clock()
        full = {"ts": ts, "ts_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)), "pid": os.getpid()}
        full.update(record)
        try:
            line = (json.dumps(full, ensure_ascii=False, default=str) + "\n").encode("utf-8", "surrogateescape")
            with self._lock:
                dfd = self._open_dir()
                try:
                    lfd = self._open_lock(dfd)
                    try:
                        self._acquire(lfd)
                        self._rotate_if_needed(dfd)
                        self._write_line(line, dfd)
                    finally:
                        os.close(lfd)  # closing the fd releases the flock
                finally:
                    os.close(dfd)
            return full
        except Exception as exc:  # noqa: BLE001 - the log must never break an operator command
            self._fail(exc)
            return None

    def log_message(self, *, kind: str, text: str, from_instance: Optional[str], target: Optional[str] = None,
                    msg_id: Optional[str] = None, **extra: Any) -> Optional[Dict[str, Any]]:
        """The FULL text of one operator message plus its sha256 and length."""
        rec: Dict[str, Any] = {"record": "message", "kind": kind, "from_instance": from_instance,
                               "target": target, "msg_id": msg_id, "len": len(text),
                               "sha256": sha256_text(text), "text": text}
        rec.update(extra)
        return self.append(rec)


def read_records(directory: str) -> List[Dict[str, Any]]:
    """Every record in the live file and its rotations, oldest first (for recovery,
    the 'copy last message' affordance and tests)."""
    base = os.path.join(directory, LOG_FILENAME)
    paths = []
    i = 1
    while os.path.exists(f"{base}.{i}"):
        paths.append(f"{base}.{i}")
        i += 1
    paths.reverse()
    paths.append(base)
    out: List[Dict[str, Any]] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8", errors="surrogateescape") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        out.append(obj)
        except OSError:
            continue
    return out


def last_message(directory: str) -> Optional[Dict[str, Any]]:
    msgs = [r for r in read_records(directory) if r.get("record") == "message"]
    return msgs[-1] if msgs else None


# -- delivery check ----------------------------------------------------------------

_PASTE_WRAP = re.compile(r'\A<pasted_content id="([^"]+)">\n(.*)\n</pasted_content id="\1">\Z', re.S)


def normalize_delivered(text: str) -> str:
    """The text Claude Code recorded, back in the form the harness typed.

    Measured on a real delivered broadcast (2026-09-30T16:11:05Z, sha matched):
    Claude Code records a long paste as ``\\n\\n<pasted_content id="X">\\n<text>\\n
    </pasted_content id="X">\\n`` and escapes nested ``<pasted_content`` /
    ``</pasted_content`` tags inside it as ``<\\pasted_content`` / ``<\\/pasted_content``.
    Unwrap one level and unescape; a plain prompt is returned as-is."""
    t = text.strip("\n")
    m = _PASTE_WRAP.match(t)
    if m is None:
        return text
    inner = m.group(2)
    return inner.replace("<\\/pasted_content", "</pasted_content").replace("<\\pasted_content", "<pasted_content")


def _common_prefix(a: str, b: str) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def classify_delivery(sent: str, delivered: str, *, min_overlap: int = 24) -> str:
    """``match`` (same text; Claude Code may trim surrounding whitespace), ``mismatch``
    (a RELATED but different text: truncated, extended, altered), or ``unrelated``
    (some other prompt: keep looking). Operator ``\\command`` lines are never a
    broadcast delivery (the hub refuses them), so they are always unrelated."""
    d = normalize_delivered(delivered)
    if d == sent or d.strip() == sent.strip():
        return MATCH
    ds, ss = d.strip(), sent.strip()
    if not ds or ds.startswith("\\"):
        return "unrelated"
    if len(ds) >= 16 and (ds in ss or ss in ds):
        return MISMATCH
    if _common_prefix(ds, ss) >= min_overlap or _common_prefix(ds[::-1], ss[::-1]) >= min_overlap:
        return MISMATCH
    return "unrelated"


def _parse_iso(ts: Any) -> Optional[float]:
    if not isinstance(ts, str) or not ts:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


_TAIL_BYTES = 1024 * 1024


def submitted_prompts_since(transcript_path: str, since_epoch: float) -> List[str]:
    """Prompt texts Claude Code recorded at/after ``since_epoch``, in order: a
    ``queue-operation`` ``enqueue`` row's ``content`` (the session was busy; written
    within ~200 ms of the submit, measured 2026-10-02) and a ``user`` row whose
    content is a string (an idle session). Tail-bounded; unreadable -> []."""
    try:
        with open(transcript_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            data = fh.read()
    except OSError:
        return []
    out: List[str] = []
    for line in data.splitlines():
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        ts = _parse_iso(row.get("timestamp"))
        if ts is None or ts < since_epoch:
            continue
        if row.get("type") == "queue-operation" and row.get("operation") == "enqueue":
            c = row.get("content")
            if isinstance(c, str):
                out.append(c)
        elif row.get("type") == "user" and isinstance(row.get("message"), dict):
            c = row["message"].get("content")
            if isinstance(c, str):
                out.append(c)
    return out


class DeliveryChecker:
    """Verifies, per broadcast, that the session's TUI submitted the exact text.

    ``transcript_path_fn`` resolves the live transcript (it may appear late);
    ``on_result`` gets ``{msg_id, verdict, sent_sha256, sent_len, delivered_sha256,
    delivered_len, from_instance, trigger}``. :meth:`close` stops all checks.

    TIMEOUTS (both; the round-1 report stated only the second, Diamond F10):

    * ``timeout_seconds`` (production 180 s, ``launcher.assemble_proxy`` default,
      harness key ``delivery_check_timeout_seconds``): while the session's transcript
      CAN be located, poll every ``poll_seconds`` (production 0.5 s) for up to this
      long for the submitted prompt; none related -> ``unverified``.
    * ``no_transcript_seconds`` (15 s): if the transcript cannot be located AT ALL
      for this long, stop early -> ``unverified`` (reason "no transcript located").

    One daemon thread per check, so at most one thread per broadcast per recipient,
    alive for at most ``timeout_seconds``. A ``match`` / ``mismatch`` ends it at once.
    """

    def __init__(self, transcript_path_fn: Callable[[], Optional[str]],
                 on_result: Callable[[Dict[str, Any]], None], *, timeout_seconds: float = 180.0,
                 poll_seconds: float = 0.5, clock: Callable[[], float] = time.time,
                 no_transcript_seconds: float = 15.0) -> None:
        self._path_fn = transcript_path_fn
        #: give up early (``unverified``, reason ``no transcript``) when the session's
        #: transcript cannot even be located for this long: nothing to read, so polling
        #: on would only burn a thread for the whole window.
        self.no_transcript_seconds = float(no_transcript_seconds)
        self._on_result = on_result
        self.timeout_seconds = float(timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self._clock = clock
        self._stop = threading.Event()
        self.errors = 0
        self.threads: List[threading.Thread] = []

    def close(self) -> None:
        self._stop.set()

    def check(self, *, sent_text: str, since_epoch: float, msg_id: Optional[str],
              from_instance: Optional[str], trigger: Optional[str]) -> threading.Thread:
        t = threading.Thread(target=self._run, name=f"gm-delivery-check-{msg_id or 'x'}", daemon=True,
                             args=(sent_text, since_epoch, msg_id, from_instance, trigger))
        self.threads.append(t)
        t.start()
        return t

    def check_now(self, sent_text: str, since_epoch: float) -> Optional[Dict[str, Any]]:
        """One pass: the first related prompt's verdict, or None (nothing related yet)."""
        p = self._path_fn()
        if not p:
            return None
        for prompt in submitted_prompts_since(p, since_epoch):
            verdict = classify_delivery(sent_text, prompt)
            if verdict in (MATCH, MISMATCH):
                d = normalize_delivered(prompt)
                return {"verdict": verdict, "delivered_sha256": sha256_text(d), "delivered_len": len(d)}
        return None

    def _run(self, sent_text: str, since_epoch: float, msg_id: Optional[str], from_instance: Optional[str],
             trigger: Optional[str]) -> None:
        base = {"msg_id": msg_id, "sent_sha256": sha256_text(sent_text), "sent_len": len(sent_text),
                "from_instance": from_instance, "trigger": trigger}
        start = self._clock()
        deadline = start + self.timeout_seconds
        res: Optional[Dict[str, Any]] = None
        try:
            while not self._stop.is_set():
                if not self._path_fn() and self._clock() - start >= self.no_transcript_seconds:
                    res = {"verdict": UNVERIFIED, "delivered_sha256": None, "delivered_len": None,
                           "reason": "no transcript located for this session"}
                    break
                res = self.check_now(sent_text, since_epoch)
                if res is not None or self._clock() >= deadline:
                    break
                self._stop.wait(self.poll_seconds)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            res = {"verdict": UNVERIFIED, "delivered_sha256": None, "delivered_len": None,
                   "error": f"{type(exc).__name__}: {exc}"}
        if self._stop.is_set() and res is None:
            return  # the session ended; nothing to report
        if res is None:
            res = {"verdict": UNVERIFIED, "delivered_sha256": None, "delivered_len": None,
                   "reason": f"no matching prompt in the transcript within {self.timeout_seconds:.0f}s"}
        out = dict(base)
        out.update(res)
        try:
            self._on_result(out)
        except Exception:  # noqa: BLE001
            self.errors += 1

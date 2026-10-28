"""The dashboard banner for "a deny guard could not start" (flag ``guard_start_failure_banner``, default OFF).

When the deny guard cannot START (a broken file, a missing module), the hook line allows the call and appends
``<UTC time> rc=<status> class=<interpreter|start-up|killed|timeout> mode=<blocked|allowed> count=<n>`` to ``<data root>/deny-guard/startup-failures.log`` (gnomon/harness/launcher.py,
``deny_guard_hook_settings``). This module READS that log for the web server and says what the banner shows. It is read-only:
it creates no file, no lock and no directory (g106), and it is imported only by the web server, never by a hook (it is not on the
guard's import closure).

The reading, in order:
  * the file is missing (ENOENT)                    -> state "none"   (no banner)
  * it cannot be read, is over the size bound, is not a regular file, is not text, or ANY non-blank line is not in the form
    a start-up failure record (a UTC time, then rc=, class= and mode=)  -> state "unreadable" (a banner: a reading that could not be taken is never shown
    as a reading of nothing, g112)
  * any non-blank line that is not in that one shape (an older writer's line included) is COUNTED and shown, never dropped: state
    "unreadable" with the text "unreadable guard log lines: N";
  * otherwise, lines whose time is within the last 24 hours -> state "failures" (a banner with the count and the newest time; calls
    that went through unchecked (mode=allowed) and calls the guard refused (mode=blocked) are told apart in the text),
    or "none" when there are none (an empty log, or only older lines).

Never raises. The open is ``O_NONBLOCK`` and the type is checked on the OPEN descriptor, so a FIFO or a device cannot hang the
request. Line text is never copied into the answer (only the line's number, and for a valid line its time); the page draws the answer with
textContent anyway (g107).
"""
import calendar
import math
import os
import re
import stat
import time
from typing import Any, Dict, Optional

FLAG_NAME = "guard_start_failure_banner"
WINDOW_SECONDS = 24 * 3600
MAX_BYTES = 1 << 20                      # 1 MiB: a few tens of thousands of lines; over it the log is "unreadable", loudly
#: ONE line shape. The previous log was archived at the upgrade, so no older shape is read.
#: fix round 1: ``count=`` is the number of occurrences the line stands for (the writer writes identical consecutive failures of
#: one session at most once a minute); ``killed`` / ``timeout`` are a guard that was killed or outlived its watchdog (always blocked).
LINE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})Z rc=(\d{1,3}) class=(interpreter|start-up|killed|timeout) "
                     r"mode=(blocked|allowed) count=(\d{1,6})$")

FAILURE_TEXT = ("A deny guard could not start: commands in at least one session went through unchecked. "
                "%d time(s) in the last 24 hours; most recent %s.")
#: a failure the guard line REFUSED (guard_fail_closed ON, or a killed guard) is shown apart from one it let through; a fleet
#: wedged by a bad swap must show.
BLOCKED_TEXT = ("A deny guard could not start and refused commands: %d time(s) in the last 24 hours; most recent %s. "
                "No command went through unchecked.")
BOTH_TEXT = ("A deny guard could not start: %d time(s) in the last 24 hours let commands through unchecked and %d time(s) refused them; "
             "most recent %s.")
WHICH_SESSION_TEXT = "Which session is not recorded yet. See BREAK-GLASS: the deny guard."
UNREADABLE_TEXT = "The deny guard's failure log could not be read: %s. Treat it as if a guard could not start."


def _unreadable(reason: str, now: float) -> Dict[str, Any]:
    return {"state": "unreadable", "text": UNREADABLE_TEXT % reason, "detail": WHICH_SESSION_TEXT, "reason": reason, "read_at": now}


def _read_bounded(path: str) -> bytes:
    """The bytes of ``path`` (at most MAX_BYTES), or raise ``_Unreadable(reason)``. FileNotFoundError passes through (a missing file)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise _Unreadable("it is not a regular file")
        if st.st_size > MAX_BYTES:
            raise _Unreadable("it is larger than %d bytes" % MAX_BYTES)
        chunks, total = [], 0
        while True:
            b = os.read(fd, min(65536, MAX_BYTES + 1 - total))
            if not b:
                break
            chunks.append(b)
            total += len(b)
            if total > MAX_BYTES:
                raise _Unreadable("it is larger than %d bytes" % MAX_BYTES)
        return b"".join(chunks)
    finally:
        os.close(fd)


class _Unreadable(Exception):
    pass


def read_state(path: str, now: Optional[float] = None) -> Dict[str, Any]:
    """What the banner shows for the log at ``path`` at time ``now`` (epoch seconds; None = the server's clock). Never raises."""
    try:
        t_now = float(time.time() if now is None else now)
        if not math.isfinite(t_now):
            raise ValueError("not finite")
    except (TypeError, ValueError, OverflowError):
        t_now = time.time()                    # a clock that is not a number is not trusted: the server's own clock is used
    try:
        try:
            raw = _read_bounded(path)
        except FileNotFoundError:
            return {"state": "none", "read_at": t_now}
        except _Unreadable as exc:
            return _unreadable(str(exc), t_now)
        except OSError as exc:
            return _unreadable("the operating system refused: %s" % (os.strerror(exc.errno) if exc.errno else type(exc).__name__), t_now)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return _unreadable("it is not valid UTF-8 text", t_now)
        count, blocked, bad, first_bad, newest_epoch, newest_label = 0, 0, 0, None, None, None
        for n, line in enumerate(text.splitlines(), 1):
            if not line.strip():
                continue
            m = LINE_RE.match(line)
            epoch = None
            if m:
                try:
                    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
                    if not (1970 <= y <= 9999 and 1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 61):
                        raise ValueError
                    epoch = calendar.timegm((y, mo, d, h, mi, s, 0, 0, 0))
                    if time.gmtime(epoch)[:6] != (y, mo, d, h, mi, s):
                        raise ValueError                     # 2026-02-31 and the like are not times
                except (ValueError, OverflowError):
                    epoch = None
            if epoch is None:
                bad += 1
                first_bad = n if first_bad is None else first_bad
                continue
            if t_now - epoch <= WINDOW_SECONDS:             # a time AFTER now (clock skew) counts: loud, not silent
                times = int(m.group(10))
                count += times
                blocked += times if m.group(9) == "blocked" else 0
                if newest_epoch is None or epoch > newest_epoch:
                    newest_epoch, newest_label = epoch, "%s-%s-%s %s:%sZ" % (m.group(1), m.group(2), m.group(3), m.group(4), m.group(5))
        if bad:
            return _unreadable("unreadable guard log lines: %d (the first is line %d; each is not a start-up failure record: a UTC time, then "
                               "rc=, class=, mode= and count=)" % (bad, first_bad), t_now)
        if count == 0:
            return {"state": "none", "read_at": t_now}
        allowed = count - blocked
        text = FAILURE_TEXT % (count, newest_label) if blocked == 0 else (
            BLOCKED_TEXT % (blocked, newest_label) if allowed == 0 else BOTH_TEXT % (allowed, blocked, newest_label))
        out = {"state": "failures", "text": text, "detail": WHICH_SESSION_TEXT, "count": count, "newest": newest_label, "read_at": t_now}
        if blocked:                                          # only when there is a refused failure: an all-allowed log answers as before
            out["count_blocked"], out["count_allowed"] = blocked, allowed
        return out
    except Exception as exc:  # noqa: BLE001 - a banner helper must never break the endpoint, and must never read as "nothing"
        return _unreadable("an unexpected %s" % type(exc).__name__, t_now)

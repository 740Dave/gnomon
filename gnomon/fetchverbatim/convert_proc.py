"""HTML-to-markdown in a CHILD PROCESS with a hard wall-clock kill (round 3, Diamond P1-1).

WHY a process: Python's ``html.parser`` is super-quadratic on some inputs (a start tag that contains further ``<``:
``<a <a <a ...`` took 9.7 s at 32 KiB, 61 s at 64 KiB, 268 s at 128 KiB) and a single ``feed()`` cannot be interrupted
from inside the same interpreter, so a deadline checked between chunks does not bound the call. The only thing the parser
cannot block is another process: the parent starts ``python -I -S`` on the pure-stdlib ``extract.py`` (imported by name from its own directory), streams the page
in, and ``kill()``s the child (SIGKILL) at the call's remaining budget.

GUARANTEE (what the tests pin): :func:`convert_html` returns within ``budget + a small constant`` whatever the input, and
it NEVER reports ``complete=True`` unless the child sent its final ``done`` frame and exited 0. On a kill, a crash, a
start failure, or a CPU-limit kill it returns the blocks the child had already streamed (possibly none), with
``complete=False`` and a ``reason``. There is no in-process fallback: if the child cannot be started the content is empty
and the result says so.

The child runs with an empty environment, cwd ``/``, no stderr, its own session, stdin/stdout pipes only, and an
RLIMIT_CPU of budget+2 s as a second kill. RESIDUAL: no memory limit is set on the child.

OPEN CONDITIONS carried from the X42 review (not fixed in this port, to be closed before live wiring):
* TODO P2-2: memory cap. The converter child has no RLIMIT_AS / memory limit.
* TODO P2-1: converter-child contract. The parent/child frame contract is not yet pinned beyond the tests listed above.
* TODO P3: killpg / timeout. The hard kill signals the child pid only (the child has its own session); a process-group
  kill and the timeout handling around it are not yet reviewed.
Source: the builder's conditions as relayed in the port brief; the final Diamond report (PASS_WITH_NOTES at a1bcf23) was not available to the port.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Callable, List, Optional, Tuple

from . import extract
from .extract import ConvertResult

#: bootstrap: put the package directory first on sys.path and import the pure-stdlib ``extract`` module by its own name, so the
#: child imports nothing else of this package (``extract.py`` has no package-relative imports).
_BOOT = "import sys;sys.path.insert(0,sys.argv[1]);import extract as m;sys.exit(m._child_main(sys.argv[2:]))"
MIN_BUDGET_S = 0.05


def _parse_frames(raw: bytes) -> Tuple[str, List[Tuple[str, str]], Optional[dict], int]:
    title, blocks, done, fed = "", [], None, 0
    lines = raw.split(b"\n")
    for line in lines[:-1]:                      # the last element is an unterminated (killed mid-write) fragment or ''
        try:
            fr = json.loads(line)
        except ValueError:
            continue
        if not isinstance(fr, dict):
            continue
        if "b" in fr and isinstance(fr["b"], list) and len(fr["b"]) == 2:
            blocks.append((str(fr["b"][0]), str(fr["b"][1])))
            fed = max(fed, int(fr.get("p", 0)))
        elif "t" in fr:
            title = str(fr["t"])
        elif fr.get("done") is True:
            done = fr
    return title, blocks, done, fed


def convert_html(html: str, deadline: float, *, clock: Callable[[], float] = time.monotonic) -> ConvertResult:
    """Convert ``html`` inside the budget ``deadline - clock()``; see the module docstring for the guarantee."""
    remaining = deadline - clock()
    if remaining < MIN_BUDGET_S:
        return ConvertResult("", 0, False, 0, "the call's time budget was spent before conversion could start")
    if not sys.executable:
        return ConvertResult("", 0, False, 0, "no Python executable to run the converter process")
    try:
        proc = subprocess.Popen(
            [sys.executable, "-I", "-S", "-c", _BOOT, os.path.dirname(os.path.abspath(extract.__file__)), repr(remaining)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={}, cwd="/", close_fds=True, start_new_session=True)
    except OSError as exc:
        return ConvertResult("", 0, False, 0, f"the converter process could not be started ({type(exc).__name__})")
    timed_out = False
    try:
        try:
            raw, _ = proc.communicate(input=html.encode("utf-8", errors="surrogatepass"), timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()                           # SIGKILL: nothing in the child can block this
            raw, _ = proc.communicate()
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    title, blocks, done, fed = _parse_frames(raw or b"")
    complete = bool(done) and proc.returncode == 0 and not timed_out
    if complete:
        md, omitted = extract.assemble_parts(str(done.get("title", title)), blocks, int(done.get("omitted", 0)))
        return ConvertResult(md, omitted, True, int(done.get("consumed", len(html))), None)
    if timed_out:
        reason = "the conversion hit the call's time budget and the converter process was killed"
    else:
        reason = f"the converter process ended abnormally (exit {proc.returncode}) before finishing"
    md, omitted = extract.assemble_parts(title, blocks, 0)
    return ConvertResult(md, omitted, False, fed, reason)

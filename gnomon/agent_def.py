"""Freshly-expanded agent definitions, re-rendered at EVERY lifecycle point (2026-09-29).

WHAT
    :func:`render_agent_def` renders a profile's ``claude_md_source`` through the REAL
    ``{{lts: … }}`` expander (:mod:`gnomon.lts_expand`) and writes it as a NEW,
    retained, numbered artifact::

        <session_dir>/.claude/agent-def/NNN-<event>-<UTCstamp>.md   (never overwritten)
        <session_dir>/.claude/agent-def/current.md -> NNN-…md        (symlink, for humans)
        <session_dir>/.claude/CLAUDE.md                              (the DELIVERED copy)

    and records the render (path, event, source, expansion status + every failure) in
    ``launch.json`` (``agent_def_*`` keys + the ``agent_def_renders`` history) and as an
    ``agent-def-render`` record in the session provenance log. The artifact directory is
    therefore a record of exactly what the agent booted with at each point.

WHEN (the lifecycle points)
    * ``launch`` / ``resume`` — :class:`~gnomon.launch.LaunchManager` at launch.
    * ``spawn`` — the harness launcher immediately before it spawns claude (so a
      session re-run later, or adopted via the agent-launch path, boots FRESH).
    * ``pre-compact`` — the harness's own PreCompact hook
      (``python -m gnomon.harness.lifecycle_hook --event pre-compact``).

HOW IT REACHES THE CHILD REGARDLESS OF CWD (evidence, claude 2.1.284)
    The delivered file is ``<session_dir>/.claude/CLAUDE.md``. Fresh mode: that IS the
    child's cwd project memory. RESUME mode (child cwd = the ORIGINAL session dir, whose
    own ``.claude/CLAUDE.md`` is externally provisioned and stale): the harness passes
    ``--add-dir <session_dir>`` and sets ``CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD=1``
    — the binary then loads ``<dir>/CLAUDE.md`` and ``<dir>/.claude/CLAUDE.md`` for every
    ``--add-dir`` as Project memory. That file is NEVER written.

    Compaction: Claude Code caches memory files per process (a mid-session edit is NOT
    seen) and clears that cache in its post-compact cleanup (``Tet("compact")`` →
    ``load_reason: "compact"``), re-reading CLAUDE.md after compaction. A render written
    by the PreCompact hook is therefore what the post-compact context loads. See the
    build report for the binary strings + the live probe.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

AGENT_DEF_DIRNAME = "agent-def"
CURRENT_LINK = "current.md"
CLAUDE_MD = "CLAUDE.md"
PROVENANCE_KIND = "agent-def-render"
EVENTS = ("launch", "resume", "spawn", "pre-compact", "post-compact")
#: env var that makes claude load CLAUDE.md from every --add-dir (binary-verified).
ADDITIONAL_DIRS_CLAUDE_MD_ENV = "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD"

_SEQ_RE = re.compile(r"^(\d{3,})-")
#: a render larger than this is RECORDED as large (launch.json ``oversize`` + the
#: neutral ``large`` flag the dashboard shows) — never truncated, and NO warning line
#: in the rendered def or the hook messages (operator ruling,
#: 2026-09-29: "No warning: show agent-def SIZE on the dashboard"). The per-query
#: lts_expand.EXPANSION_WARN_CHARS is a separate mechanism, unchanged.
LARGE_DEF_CHARS = 60_000
#: the token figure is an APPROXIMATION (chars / 4), always labelled as such.
APPROX_CHARS_PER_TOKEN = 4
APPROX_BASIS = "approximate: chars / 4 (not a tokenizer count)"
ARTIFACT_LOCK = ".lock"
LAUNCH_JSON_LOCK = "launch.json.lock"


class LockTimeout(OSError):
    """``_flock(..., timeout=)`` could not take the lock within the bound (relaunch fix round 3)."""


@contextlib.contextmanager
def _flock(path: str, timeout: Optional[float] = None):
    """Exclusive advisory lock (fcntl.flock) — serialises renders / launch.json RMW
    across threads AND processes (each holder opens its own descriptor).

    ``timeout`` None (every pre-existing caller) = wait for ever, exactly as before. A number = poll without
    blocking for at most that many seconds, then raise :class:`LockTimeout` (the descriptor is closed)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        else:
            deadline = time.monotonic() + max(0.0, timeout)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"could not take {path} within {timeout:g}s") from None
                    time.sleep(0.02)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _tmp_name(path: str) -> str:
    return f"{path}.{os.getpid()}.{threading.get_ident()}.{time.time_ns()}.tmp"


def superseded_paths(resume_cwd: Optional[str]) -> List[str]:
    """The ORIGINAL-cwd CLAUDE.md files a resume render supersedes (and the harness
    excludes via ``claudeMdExcludes``). Absolute, ``~``-expanded; empty when fresh."""
    if not resume_cwd:
        return []
    base = os.path.abspath(os.path.expanduser(resume_cwd))
    return [os.path.join(base, ".claude", CLAUDE_MD), os.path.join(base, CLAUDE_MD)]


def size_info(chars: Optional[int]) -> Optional[Dict[str, Any]]:
    """The agent-def size as the dashboard shows it: chars, an APPROXIMATE token
    count (chars // 4, labelled), and a neutral ``large`` flag (> LARGE_DEF_CHARS).
    ``None`` in, ``None`` out (no size known)."""
    if chars is None:
        return None
    chars = int(chars)
    return {"chars": chars, "approx_tokens": chars // APPROX_CHARS_PER_TOKEN, "approx": True,
            "approx_basis": APPROX_BASIS, "large": chars > LARGE_DEF_CHARS}


def _utc_stamp(now: datetime) -> str:
    return now.strftime("%Y%m%dT%H%M%S") + f"{now.microsecond // 1000:03d}Z"


def _atomic_write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = _tmp_name(path)
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _next_seq(artifact_dir: str) -> int:
    top = 0
    try:
        for name in os.listdir(artifact_dir):
            m = _SEQ_RE.match(name)
            if m:
                top = max(top, int(m.group(1)))
    except FileNotFoundError:
        pass
    return top + 1


def _header(event: str, rendered_at: str, source: str, artifact: str, status: str,
            failures: List[Dict[str, Any]], superseded: Optional[List[str]] = None) -> str:
    lines = [
        "<!-- Gnomon harness-rendered agent definition -->",
        f"<!-- rendered_at={rendered_at} event={event} artifact={artifact} -->",
        f"<!-- source={source} lts_expansion={status} -->",
        "",
        "> **This is your agent definition, freshly rendered by the Gnomon harness "
        f"({event}, {rendered_at}).** It supersedes any other project CLAUDE.md loaded from "
        "the working directory (e.g. an externally provisioned file), which may be stale.",
    ]
    if superseded:
        lines.append(">")
        lines.append("> Superseded (and excluded from loading via `claudeMdExcludes` for this resumed "
                     "session): " + ", ".join(f"`{p}`" for p in superseded) + ".")
    if failures:
        lines.append(">")
        lines.append(f"> ⚠ **{len(failures)} lts expansion(s) FAILED in this render** — see the banners "
                     "below; that memory was NOT loaded. Flag it to the pilot.")
    lines.append("")
    return "\n".join(lines) + "\n"


def _config_store():
    """The machine config store (a seam: tests point it at a tmp file)."""
    from .config import ConfigStore

    return ConfigStore()


def _expander_factory(default_budget: Optional[int]):
    """Build the REAL expander with the config's per-macro budget default (a seam)."""
    from .lts_expand import LtsExpander

    return LtsExpander(default_budget=default_budget)


def macro_budget_default(config: Any = None) -> Optional[int]:
    """the machine-config ``lts_macro_budget`` (None = OFF). ``config`` wins;
    else the persisted config store. A config that cannot be read is OFF (unbounded),
    never a render failure — the budget only ever withholds, so OFF loses nothing."""
    try:
        cfg = config if config is not None else _config_store().load()
        b = getattr(cfg, "lts_macro_budget", None)
    except Exception:  # noqa: BLE001
        return None
    if b is None or isinstance(b, bool) or not isinstance(b, int) or b < 0:
        return None
    return b


_SESSION_TS_TAIL = re.compile(r"-\d{8}-\d{6}$")


def role_for(session_dir: str) -> Optional[str]:
    """The ``${ROLE}`` symbol for an agent render: launch.json ``instance_id`` when
    present, else the session dir's basename minus its ``-YYYYMMDD-HHMMSS`` tail."""
    try:
        with open(os.path.join(session_dir, ".claude", "launch.json"), "r", encoding="utf-8") as fh:
            iid = json.load(fh).get("instance_id")
        if isinstance(iid, str) and iid:
            return iid
    except (OSError, ValueError, AttributeError):
        pass
    base = os.path.basename(os.path.normpath(session_dir))
    return _SESSION_TS_TAIL.sub("", base) or None


def macro_sizes(expansions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """the per-macro size rows recorded next to the D4 size fields."""
    keys = ("query", "status", "budget", "budget_source", "matched", "shown", "withheld",
            "chars", "approx_tokens_rendered", "approx_tokens_withheld", "remainder_query")
    return [{k: e.get(k) for k in keys} for e in expansions or []]


def _deliver_artifact(artifact_dir: str, claude_dir: str, delivered: str, event: str, now: datetime,
                      make_text: Callable[[str], str]) -> Tuple[int, str, str]:
    """Allocate the next retained artifact, write it and the delivered CLAUDE.md, repoint ``current``.
    ⛔ Serialised: two concurrent renders (e.g. a spawn and a PreCompact) must never share an NNN or
    cross their deliveries. Returns (seq, artifact path, text written)."""
    with _flock(os.path.join(artifact_dir, ARTIFACT_LOCK)):
        seq = _next_seq(artifact_dir)
        while True:  # O_EXCL claim: never overwrite a retained artifact
            name = f"{seq:03d}-{event}-{_utc_stamp(now)}.md"
            artifact = os.path.join(artifact_dir, name)
            try:
                os.close(os.open(artifact, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
                break
            except FileExistsError:
                seq += 1
        text = make_text(name)
        _atomic_write(artifact, text)
        _atomic_write(delivered, text)
        link = os.path.join(artifact_dir, CURRENT_LINK)
        try:
            tmp_link = _tmp_name(link)
            os.symlink(name, tmp_link)
            os.replace(tmp_link, link)
        except OSError:
            pass  # the launch.json record + CLAUDE.md are the contract; the link is a convenience
    return seq, artifact, text


def _has_usable_def(delivered: str) -> bool:
    """A delivered CLAUDE.md counts only when it is a file with non-whitespace content (F-1b: an empty
    file is NO def)."""
    try:
        with open(delivered, "r", encoding="utf-8", errors="replace") as fh:
            return bool(fh.read().strip())
    except (OSError, ValueError):
        return False


KEPT_BANNER_MARK = "> ⚠ [GM-RENDER-FAILED]"


def _banner_kept_def(rec: Dict[str, Any], delivered: str, headline: str, exc: BaseException) -> None:
    """P2-3: a usable PREVIOUS def is kept after a render failure, but the agent must still get an
    in-context signal. Prepend ONE banner line (a prior banner of ours is replaced, never stacked); the
    write is atomic and byte-preserving. Never raises (a failure is recorded, the def stays as it was)."""
    try:
        with open(delivered, "rb") as fh:
            old = fh.read()
        prefix = KEPT_BANNER_MARK.encode("utf-8")
        if old.startswith(prefix):
            head, sep, rest = old.partition(b"\n\n")
            old = rest if sep else b""
        one_line = " ".join(f"{headline}: {exc}".split())
        banner = (f"{KEPT_BANNER_MARK} {one_line}. This is your PREVIOUS definition, kept because the new "
                  "render failed; it may be stale. Flag it to the pilot.\n\n")
        tmp = _tmp_name(delivered)
        with open(tmp, "wb") as fh:
            fh.write(banner.encode("utf-8") + old)
        os.replace(tmp, delivered)
        rec["fallback"] = "previous-def-kept-with-banner"
    except Exception as berr:  # noqa: BLE001
        rec.setdefault("record_errors", []).append(f"kept-def banner not written: {type(berr).__name__}: {berr}")


def _ship_fallback(rec: Dict[str, Any], session_dir: str, claude_dir: str, artifact_dir: str,
                   source_path: Optional[str], event: str, now: datetime, rendered_at: str,
                   superseded: Optional[List[str]], role: Optional[str], headline: str,
                   unreadable_path: Optional[str], exc: BaseException, status: str) -> None:
    """INVARIANT: a launch or compact must never leave a session with no usable def. When no usable
    delivered CLAUDE.md exists, deliver the UNEXPANDED source under a loud banner, or (source unreadable)
    a banner-only def naming the path + error and pointing at ``~/.claude/agents/<id>.md``. A usable
    previous def is left untouched. Never raises (a failure is recorded in ``record_errors``)."""
    delivered = os.path.join(claude_dir, CLAUDE_MD)
    if _has_usable_def(delivered):
        _banner_kept_def(rec, delivered, headline, exc)
        return
    raw = None
    if source_path:
        try:
            with open(os.path.expanduser(source_path), "r", encoding="utf-8") as fh:
                raw = fh.read()
        except (OSError, ValueError):   # ValueError covers UnicodeDecodeError (a non-UTF-8 source)
            raw = None
        if raw is not None and not raw.strip():
            raw = None
    try:
        ident = role or role_for(session_dir) or "<id>"
        if raw is not None:
            fallback = "unexpanded-source-with-banner"
            body = (f"> ⚠ **{headline}: {exc}**\n>\n"
                    "> This is the UNEXPANDED source: no `{{include}}`, `{{lts}}` or `${symbols}` were "
                    "resolved, and no previous definition existed to fall back on. Flag it to the pilot.\n\n") + raw
        else:
            fallback = "banner-only"
            shown = unreadable_path if unreadable_path is not None else (source_path or "(none)")
            body = (f"> ⚠ **{headline}: the agent-definition source could not be used.**\n>\n"
                    f"> Source path: `{shown}`\n> Error: `{type(exc).__name__}: {exc}`\n>\n"
                    f"> No usable definition existed to fall back on. Read `~/.claude/agents/{ident}.md` "
                    "directly to recover your role, and flag this to the pilot.\n")
        make = lambda name: _header(event, rendered_at, source_path, name, status, [], superseded) + body  # noqa: E731
        try:
            seq, artifact, text = _deliver_artifact(artifact_dir, claude_dir, delivered, event, now, make)
            rec.update(seq=seq, path=artifact, delivered_path=delivered, chars=len(text),
                       approx_tokens=len(text) // APPROX_CHARS_PER_TOKEN, fallback=fallback,
                       superseded=list(superseded or []))
        except Exception as derr:  # noqa: BLE001
            # P2-1: the artifact dir/lock is unusable: write the def DIRECTLY to the delivered path so the
            # session is never defless (the artifact is a convenience; CLAUDE.md is the contract).
            rec.setdefault("record_errors", []).append(
                f"fallback artifact not delivered: {type(derr).__name__}: {derr}")
            text = make("(artifact dir unwritable)")
            _atomic_write(delivered, text)
            rec.update(path=None, delivered_path=delivered, chars=len(text),
                       approx_tokens=len(text) // APPROX_CHARS_PER_TOKEN, fallback=fallback + "-direct",
                       superseded=list(superseded or []))
    except Exception as ferr:  # noqa: BLE001
        rec.setdefault("record_errors", []).append(f"fallback def not delivered: {type(ferr).__name__}: {ferr}")


def _render_agent_def(
    session_dir: str,
    source_path: Optional[str],
    event: str,
    *,
    expander: Any = None,
    renderer: Any = None,
    mode: Optional[str] = None,
    superseded: Optional[List[str]] = None,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    record: bool = True,
    config: Any = None,
    role: Optional[str] = None,
    render_paths: Any = None,
) -> Dict[str, Any]:
    """Render + write a NEW agent-def artifact, update the delivered CLAUDE.md, record it.

    Returns the render record (merged into launch.json when ``record`` and the file
    exists, and appended to the provenance log). NEVER overwrites a prior artifact.

    ``renderer`` (a ``ClaudeMdRenderer``) wins; else a renderer over ``expander``;
    else over a fresh REAL :class:`~gnomon.lts_expand.LtsExpander` whose
    per-macro budget default is the machine config's ``lts_macro_budget`` (``config``,
 else the persisted store; OFF unless someone opts in —).

    An empty ``source_path`` renders the renderer's honest placeholder (status
    ``no-source``), as LaunchManager always did. An UNREADABLE source is a loud
    recorded status ``source-unreadable``; a usable delivered CLAUDE.md is left untouched, and when
    none exists a banner-only def is shipped. No render path may leave a session defless.
    """
    from .launch import ClaudeMdRenderer

    if event not in EVENTS:
        raise ValueError(f"agent-def render event must be one of {EVENTS}, got {event!r}")
    session_dir = os.path.abspath(os.path.expanduser(session_dir))
    now = clock()
    rendered_at = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
    claude_dir = os.path.join(session_dir, ".claude")
    artifact_dir = os.path.join(claude_dir, AGENT_DEF_DIRNAME)
    rec_pre_errors: List[str] = []
    try:
        os.makedirs(artifact_dir, exist_ok=True)
    except OSError as exc:  # the delivery below will fail too and be handled loudly; do not abort here
        rec_pre_errors.append(f"artifact dir not creatable: {type(exc).__name__}: {exc}")
    if renderer is None:
        renderer = ClaudeMdRenderer(expander=expander if expander is not None
                                    else _expander_factory(macro_budget_default(config)))

    rec: Dict[str, Any] = {"event": event, "rendered_at": rendered_at, "source": source_path or None}
    if rec_pre_errors:
        rec["record_errors"] = list(rec_pre_errors)
    if mode:
        rec["mode"] = mode
    from .agent_render import RenderError

    try:
        # include -> ${symbols} -> lts (one renderer; see agent_render)
        result = renderer.render_file(source_path or "", role=role or role_for(session_dir),
                                      session_root=session_dir, paths=render_paths)
    except OSError as exc:
        # F-1a: an unreadable source must still never leave a first launch defless.
        reason = f"claude_md_source unreadable — {type(exc).__name__}: {exc}"
        rec.update(status="source-unreadable", path=None, expansions=[], failures=[], pending=[], reason=reason)
        _ship_fallback(rec, session_dir, claude_dir, artifact_dir, source_path, event, now, rendered_at,
                       superseded, role, "claude_md_source unreadable", str(source_path or ""), exc, "source-unreadable")
        if record:
            _record(session_dir, rec)
        return rec
    except RenderError as exc:
        # LOUD and recorded. With a usable PREVIOUS delivered CLAUDE.md it is left intact (never replaced by a
        # broken render). With NONE (a brand-new session dir, or an empty file) it must never launch defless
        # (F2): ship the UNEXPANDED source under a loud top banner naming the error.
        reason = f"include/symbol render failed ({exc.kind}): {exc}"
        rec.update(status="render-error", path=None, expansions=[], failures=[], pending=[], reason=reason)
        _ship_fallback(rec, session_dir, claude_dir, artifact_dir, source_path, event, now, rendered_at,
                       superseded, role, f"AGENT DEFINITION RENDER FAILED ({exc.kind})", None, exc, "render-error")
        if record:
            _record(session_dir, rec)
        return rec
    except Exception as exc:  # noqa: BLE001
        # F-1c: a NON-RenderError out of the render must never abort a launch/compact: same as a render error.
        reason = f"agent-def render raised {type(exc).__name__}: {exc}"
        rec.update(status="render-error", path=None, expansions=[], failures=[], pending=[], reason=reason)
        _ship_fallback(rec, session_dir, claude_dir, artifact_dir, source_path, event, now, rendered_at,
                       superseded, role, f"AGENT DEFINITION RENDER FAILED ({type(exc).__name__})", None, exc,
                       "render-error")
        if record:
            _record(session_dir, rec)
        return rec

    expansions = list(getattr(result, "lts_expansions", []) or [])
    failures = [e for e in expansions if e.get("status") == "failed"]
    if not source_path:
        status = "no-source"
        rec["reason"] = "no claude_md_source configured for this profile — placeholder rendered"
    elif getattr(renderer, "is_default", False) and result.lts_expansion_pending:
        status = "not-expanded"  # the no-op D8 seam was injected: markers left verbatim
    elif failures:
        status = "failed"
    elif expansions:
        status = "ok"
    else:
        status = "none"
    rec["pending"] = list(result.lts_expansion_pending)
    rec["warnings"] = list(getattr(result, "warnings", []) or [])
    rec["fragments"] = dict(getattr(result, "fragments", {}) or {})
    rec["symbols_used"] = list(getattr(result, "symbols_used", []) or [])
    oversize = [e.get("query") for e in expansions if e.get("oversize")]
    body_chars = len(result.text)
    if body_chars > LARGE_DEF_CHARS and not oversize:
        oversize = ["(whole definition)"]
    delivered = os.path.join(claude_dir, CLAUDE_MD)
    if source_path and not str(result.text).strip():
        # P3-3: an empty / whitespace-only source is loud, never a silent header-only def.
        exc = ValueError("source is empty")
        reason = f"claude_md_source is empty — {source_path}"
        rec.update(status="source-empty", path=None, expansions=[], failures=[], pending=[], reason=reason)
        _ship_fallback(rec, session_dir, claude_dir, artifact_dir, source_path, event, now, rendered_at,
                       superseded, role, "claude_md_source is empty", str(source_path), exc, "source-empty")
        if record:
            _record(session_dir, rec)
        return rec
    # the agent's own open-work digest, in a block that is visibly NOT definition text. "" unless open_work_list
    # is ON (then this render is byte-identical to before); the package is imported only when the flag is ON.
    owl_digest = ""
    try:
        from . import flags as _owl_flags

        if _owl_flags.flag("open_work_list", config=config):
            from .worklist.digest import render_block as _owl_block

            owl_digest = _owl_block(config, role or role_for(session_dir), now.timestamp())
    except Exception:  # noqa: BLE001 - the digest can never break a render
        owl_digest = ""
    try:
        seq, artifact, text = _deliver_artifact(
            artifact_dir, claude_dir, delivered, event, now,
            lambda name: _header(event, rendered_at, source_path, name, status, failures, superseded) + result.text + owl_digest)
    except Exception as exc:  # noqa: BLE001
        # P2-1: an unwritable artifact dir / full disk on the SUCCESS path is a loud recorded render error,
        # never an abort. A usable previous def stays; else the fallback tries (and records if it cannot).
        reason = f"agent-def delivery failed: {type(exc).__name__}: {exc}"
        rec.update(status="render-error", path=None, expansions=[], failures=[], pending=[], reason=reason)
        rec.setdefault("record_errors", []).append(reason)
        _ship_fallback(rec, session_dir, claude_dir, artifact_dir, source_path, event, now, rendered_at,
                       superseded, role, f"AGENT DEFINITION DELIVERY FAILED ({type(exc).__name__})", None, exc,
                       "render-error")
        if record:
            _record(session_dir, rec)
        return rec
    rec.update(status=status, seq=seq, path=artifact, delivered_path=delivered,
               expansions=expansions, failures=failures, chars=len(text),
               approx_tokens=len(text) // APPROX_CHARS_PER_TOKEN, oversize=oversize,
               superseded=list(superseded or []), macros=macro_sizes(expansions))
    if record:
        _record(session_dir, rec)
    return rec


def render_agent_def(session_dir: str, source_path: Optional[str], event: str, **kwargs: Any) -> Dict[str, Any]:
    """Public entry (see :func:`_render_agent_def` for the parameters). INVARIANT: NO exception from the
    render path reaches the caller (LaunchManager, the spawn hook, the PreCompact hook); the only raise is
    a programmer error (an unknown ``event``). Anything unforeseen is a recorded render-error and the
    fallback def is attempted."""
    if event not in EVENTS:
        raise ValueError(f"agent-def render event must be one of {EVENTS}, got {event!r}")
    try:
        return _render_agent_def(session_dir, source_path, event, **kwargs)
    except Exception as exc:  # noqa: BLE001  -- last-resort net
        rec: Dict[str, Any] = {"event": event, "source": source_path or None, "status": "render-error",
                               "path": None, "expansions": [], "failures": [], "pending": [],
                               "reason": f"agent-def render raised {type(exc).__name__}: {exc}",
                               "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"}
        try:
            sd = os.path.abspath(os.path.expanduser(session_dir))
            cd = os.path.join(sd, ".claude")
            _ship_fallback(rec, sd, cd, os.path.join(cd, AGENT_DEF_DIRNAME), source_path, event,
                           datetime.now(timezone.utc), rec["rendered_at"], kwargs.get("superseded"),
                           kwargs.get("role"), f"AGENT DEFINITION RENDER FAILED ({type(exc).__name__})", None,
                           exc, "render-error")
            if kwargs.get("record", True):
                _record(sd, rec)
        except Exception as e2:  # noqa: BLE001
            rec.setdefault("record_errors", []).append(f"{type(e2).__name__}: {e2}")
        return rec


def _record(session_dir: str, rec: Dict[str, Any]) -> None:
    """launch.json (atomic merge, when present) + the provenance log. Never raises."""
    claude_dir = os.path.join(session_dir, ".claude")
    lj = os.path.join(claude_dir, "launch.json")
    if os.path.isfile(lj):
        try:
            update_launch_json(lj, rec)
        except Exception as exc:  # noqa: BLE001  (never raises into the launch)
            rec.setdefault("record_errors", []).append(f"launch.json: {exc}")
    record_provenance(session_dir, rec)


def record_provenance(session_dir: str, rec: Dict[str, Any]) -> None:
    """Append one ``agent-def-render`` record to the session provenance log. Never raises."""
    try:
        from .harness.hooks import append_record, injection_log_path

        append_record(injection_log_path(session_dir), {
            "kind": PROVENANCE_KIND, "event": rec.get("event"), "status": rec.get("status"),
            "path": rec.get("path"), "source": rec.get("source"),
            "failures": [{"query": f.get("query"), "reason": f.get("reason")} for f in rec.get("failures") or []],
            "expansions": len(rec.get("expansions") or []),
            **({"warnings": rec["warnings"]} if rec.get("warnings") else {}),
            **({"reason": rec["reason"]} if rec.get("reason") else {}),
        })
    except Exception as exc:  # noqa: BLE001
        rec.setdefault("record_errors", []).append(f"provenance: {exc}")


def summarize(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The compact launch.json history row for one render."""
    return {k: rec.get(k) for k in ("seq", "event", "mode", "rendered_at", "status", "path", "source",
                                    "chars", "approx_tokens")} | {
        "failures": [{"query": f.get("query"), "reason": f.get("reason")} for f in rec.get("failures") or []],
        **({"reason": rec["reason"]} if rec.get("reason") else {}),
    }


def merge_launch_json(session_dir: str, keys: Dict[str, Any]) -> None:
    """Atomically merge top-level ``keys`` into ``<session_dir>/.claude/launch.json``."""
    path = os.path.join(session_dir, ".claude", "launch.json")
    _rewrite_launch_json(path, lambda cfg: cfg.update(keys))


class SameDirUnknown(OSError):
    """A directory identity could not be established (empty input, missing, not a directory, not stat-able)."""


def dir_identity(path: Optional[str]) -> Tuple[int, int]:
    """``(st_dev, st_ino)`` of a directory, after ``~`` expansion and symlink resolution.

    File identity, not path spelling: on a case-insensitive volume ``/x/FRESH-1`` and ``/x/fresh-1`` are ONE directory
    but different strings, and ``realpath`` does not normalise case. Raises :class:`SameDirUnknown` when ``path`` is
    empty, cannot be stat'ed, or is not a directory: the caller must refuse, never guess."""
    if not isinstance(path, str) or not path:
        raise SameDirUnknown(f"directory path is empty or not a string ({path!r})")
    real = os.path.realpath(os.path.expanduser(path))
    try:
        st = os.stat(real)
    except OSError as exc:
        raise SameDirUnknown(f"cannot stat {path!r} (resolved {real!r}): {exc}") from exc
    if not stat.S_ISDIR(st.st_mode):
        raise SameDirUnknown(f"{path!r} (resolved {real!r}) is not a directory")
    return (st.st_dev, st.st_ino)


def is_same_dir(a: Optional[str], b: Optional[str]) -> bool:
    """True when two paths name the SAME directory by file identity ``(st_dev, st_ino)``.

    Symlinks, trailing/double slashes, ``/.`` and letter-case variants on a case-insensitive volume are all the same
    directory; a different directory with the same name elsewhere is not. If either side cannot be identified this
    RAISES :class:`SameDirUnknown` (an ``OSError``): the relaunch fix refuses loudly rather than guess."""
    return dir_identity(a) == dir_identity(b)


#: how many ``launch.json.bak-<UTC>`` files :func:`edit_launch_json` keeps (the newest).
LAUNCH_JSON_BACKUPS_KEPT = 3
LAUNCH_JSON_BACKUP_PREFIX = "launch.json.bak-"


def _prune_backups(claude_dir: str, keep: int) -> None:
    try:
        names = sorted(n for n in os.listdir(claude_dir)
                       if n.startswith(LAUNCH_JSON_BACKUP_PREFIX) and not n.endswith(".tmp"))
    except OSError:
        return
    for n in names[:-keep] if keep > 0 else names:
        try:
            os.remove(os.path.join(claude_dir, n))
        except OSError:
            pass


def edit_launch_json(session_dir: str, *, set_keys: Optional[Dict[str, Any]] = None,
                     remove_keys: Sequence[str] = (), backup: bool = False,
                     mutate: Optional[Callable[[Dict[str, Any]], Any]] = None,
                     lock_timeout: Optional[float] = None) -> Optional[str]:
    """Locked, ATOMIC read-modify-write of ``launch.json``: set ``set_keys``, delete ``remove_keys``, then ``mutate``.

    Used by the relaunch fix (relaunch_derives_resume_keys). tmp + ``os.replace`` under ``launch.json.lock``,
    so a crash at any point leaves the previous file whole. The lock serialises only writers that TAKE it (this
    function, ``merge_launch_json``, the agent-def renders); ``record_transcript_location`` and ``record_exit_check`` do
    not. The file's permission bits are kept (a 0600 launch.json stays 0600, as does its backup).
    ``backup=True`` first copies the CURRENT file to ``launch.json.bak-<UTC>`` (tmp + replace, same mode) inside the
    lock, returns that path, and prunes to the newest :data:`LAUNCH_JSON_BACKUPS_KEPT`; a failed backup leaves no
    ``.tmp`` and no partial backup. Raises on any failure (nothing is changed unless the final replace succeeded).
    ``mutate`` runs INSIDE the lock on the freshly read file and may raise to abort (a backup it caused is removed).
    ``lock_timeout`` (seconds, default None = wait) raises :class:`LockTimeout` instead of waiting."""
    path = os.path.join(session_dir, ".claude", "launch.json")
    cdir = os.path.dirname(path)

    def _write_tmp(dest: str, writer: Callable[[Any], None], mode: int, binary: bool) -> str:
        tmp = _tmp_name(dest)
        try:
            with open(tmp, "wb" if binary else "w", **({} if binary else {"encoding": "utf-8"})) as fh:
                writer(fh)
            os.chmod(tmp, mode)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return tmp

    with _flock(os.path.join(cdir, LAUNCH_JSON_LOCK), lock_timeout):
        mode = stat.S_IMODE(os.stat(path).st_mode)
        with open(path, "rb") as fh:
            data = fh.read()
        cfg = json.loads(data.decode("utf-8"))
        if not isinstance(cfg, dict):
            raise ValueError("launch.json is not an object")
        bak: Optional[str] = None
        if backup:
            bak = os.path.join(cdir, f"{LAUNCH_JSON_BACKUP_PREFIX}{_utc_stamp(datetime.now(timezone.utc))}")
            tmp = _write_tmp(bak, lambda fh: fh.write(data), mode, True)
            try:
                os.replace(tmp, bak)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            _prune_backups(cdir, LAUNCH_JSON_BACKUPS_KEPT)
        for k in remove_keys:
            cfg.pop(k, None)
        cfg.update(set_keys or {})
        if mutate is not None:
            try:
                mutate(cfg)
            except BaseException:
                if bak is not None:          # an aborted edit leaves no backup of its own behind
                    try:
                        os.remove(bak)
                    except OSError:
                        pass
                raise

        def _dump(fh: Any) -> None:
            json.dump(cfg, fh, indent=2)
            fh.write("\n")
        tmp = _write_tmp(path, _dump, mode, False)
        try:
            os.replace(tmp, path)
        except BaseException:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
        return bak


def update_launch_json(path: str, rec: Dict[str, Any]) -> None:
    _rewrite_launch_json(path, lambda cfg: apply_to_launch_cfg(cfg, rec))


def _rewrite_launch_json(path: str, mutate: Callable[[Dict[str, Any]], Any]) -> None:
    """Locked read-modify-write (Diamond #4: unlocked RMW lost history rows under
    concurrency). The lock file sits beside launch.json."""
    with _flock(os.path.join(os.path.dirname(path), LAUNCH_JSON_LOCK)):
        _rewrite_launch_json_locked(path, mutate)


def _rewrite_launch_json_locked(path: str, mutate: Callable[[Dict[str, Any]], Any]) -> None:
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    if not isinstance(cfg, dict):
        raise ValueError("launch.json is not an object")
    mutate(cfg)
    tmp = _tmp_name(path)
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def apply_to_launch_cfg(cfg: Dict[str, Any], rec: Dict[str, Any]) -> None:
    """Merge one render record into a launch config dict (in place)."""
    cfg["agent_def_rendered_at"] = rec.get("rendered_at")
    cfg["agent_def_event"] = rec.get("event")
    cfg["agent_def_source"] = rec.get("source")
    cfg["agent_def_path"] = rec.get("path")
    cfg["agent_def_expansion"] = {
        "status": rec.get("status"),
        "expansions": len(rec.get("expansions") or []),
        "failures": [{"query": f.get("query"), "reason": f.get("reason")} for f in rec.get("failures") or []],
        "chars": rec.get("chars"),
        "approx_tokens": rec.get("approx_tokens"),
        "approx_basis": APPROX_BASIS,
        **({"fallback": rec["fallback"]} if rec.get("fallback") else {}),
        "oversize": list(rec.get("oversize") or []),
        "superseded": list(rec.get("superseded") or []),
        # renderer warnings (e.g. an {{include}} inside lts content), fragments + symbols used
        "warnings": list(rec.get("warnings") or []),
        "fragments": dict(rec.get("fragments") or {}),
        "symbols_used": list(rec.get("symbols_used") or []),
        # per-macro sizes (approximate, chars/4) + the total withheld by budgets
        "macros": list(rec.get("macros") or macro_sizes(rec.get("expansions") or [])),
        "approx_tokens_withheld": sum(int(m.get("approx_tokens_withheld") or 0)
                                      for m in (rec.get("macros") or macro_sizes(rec.get("expansions") or []))),
        **({"reason": rec["reason"]} if rec.get("reason") else {}),
    }
    hist = cfg.get("agent_def_renders")
    if not isinstance(hist, list):
        hist = []
    hist.append(summarize(rec))
    cfg["agent_def_renders"] = hist


def status_line(session_dir: str) -> Optional[str]:
    """A one-line human/agent-facing summary of the CURRENT render (for post-compact)."""
    try:
        with open(os.path.join(session_dir, ".claude", "launch.json"), "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return None
    exp = cfg.get("agent_def_expansion") or {}
    if not cfg.get("agent_def_rendered_at"):
        return None
    status = exp.get("status")
    base = (f"[Gnomon] Agent definition re-rendered at {cfg.get('agent_def_rendered_at')} "
            f"({cfg.get('agent_def_event')}): {cfg.get('agent_def_path')}.")
    if status == "failed":
        qs = "; ".join(f"{f.get('query')}: {f.get('reason')}" for f in exp.get("failures") or [])
        return base + f" ⚠ lts expansion FAILED ({qs}) — that memory was NOT loaded; flag it to the pilot."
    if status in ("no-source", "source-unreadable", "source-empty", "render-error"):
        fb = exp.get("fallback")
        if fb:   # a banner was written at the top of the delivered CLAUDE.md
            extra = (" The PREVIOUS definition was kept; a warning banner is at the top of your CLAUDE.md."
                     if str(fb).startswith("previous-def-kept") else
                     " A fallback definition was delivered (see the banner at the top of your CLAUDE.md).")
        elif status in ("render-error", "source-unreadable", "source-empty"):
            extra = " No banner could be written; the existing CLAUDE.md (if any) remains as it was."
        else:
            extra = ""
        return base.replace(": None.", ".") + f" ⚠ agent-def render {status}: {exp.get('reason')}" + extra
    # No size warning here (operator ruling D4): the size lives on the dashboard.
    return base + f" lts expansion: {status}."

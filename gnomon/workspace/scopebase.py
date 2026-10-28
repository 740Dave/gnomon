"""Shared base of the X13 S2 scope core (slice S2): names, errors and small IO helpers.

FLAG: :data:`SCOPE_CORE_FLAG` (``workspace_scope_core``, registered in ``flags.KNOWN_FLAGS``, default
OFF). Every S2 entry point that PERSISTS something calls :func:`require_enabled` first, so with the flag OFF no
S2 code path writes a byte (g2: the default lives in the flags registry; no caller passes its own).

Nothing here names a deployment (source neutrality D1): workspace ids, project slugs and role ids
are DATA handed in by callers.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Tuple

#: the S2 flag (g2: read only through flags.flag(); its default lives in KNOWN_FLAGS).
SCOPE_CORE_FLAG = "workspace_scope_core"
#: the reserved project of every workspace (reserved-project).
RESERVED_PROJECT = "unassigned"

# F-R2-3: ``$`` also matches before a trailing newline, so ``"ws\n"`` passed ``.match``. These patterns end in ``\Z`` (no newline
# allowance) AND every caller uses ``.fullmatch``: a trailing newline is rejected twice over. ASCII only (a-z, 0-9).
SLUG_RE = re.compile(r"[a-z][a-z0-9-]*\Z", re.ASCII)
QUALIFIED_RE = re.compile(r"([a-z][a-z0-9-]*)/([a-z][a-z0-9-]*)\Z", re.ASCII)


class ScopeError(Exception):
    """Base of every S2 error. ``code`` is a stable machine-readable reason; ``prompt`` is what a writer shows
    the person or agent to fix it (e.g. the re-pick prompt of I2)."""

    def __init__(self, code: str, message: str, prompt: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.prompt = prompt
        #: events the refusing code wants logged even though the write was refused (e.g. 'current-project archived')
        self.events: list = []


class ScopeRefusal(ScopeError):
    """A write the scope rules refuse (nothing was changed)."""


class ScopeCoreDisabled(ScopeError):
    """An S2 persisting entry point was called with ``workspace_scope_core`` OFF (nothing was written)."""

    def __init__(self, what: str = "") -> None:
        super().__init__("scope-core-disabled", f"workspace_scope_core is OFF{': ' + what if what else ''}; nothing written")


class ModelError(ScopeError):
    """The deployment model (machine.json / workspace.json / projects.json) is missing or malformed: a loud
    stop, never a guessed default."""

    def __init__(self, message: str) -> None:
        super().__init__("model-unreadable", message)


def require_enabled(enabled: Optional[bool] = None, config: Any = None, what: str = "") -> None:
    """Raise :class:`ScopeCoreDisabled` unless the S2 flag is ON.

    ``enabled`` is the caller's already-resolved value (an integration point resolves the flag once and passes
    it down); ``None`` resolves it here through ``flags.flag`` (``config`` is an injectable machine config).

    STRICT (g54b e, audit-x13-runtime-gate P3): the ONLY accepted resolved value is a real ``bool`` ``True``. ``False``, a truthy
    non-bool (``"false"``, ``"0"``, ``1``, ``"x"``) and anything else refuse, so a stringly-typed or numeric flag can never open the gate."""
    if enabled is None:
        from .. import flags

        enabled = flags.flag(SCOPE_CORE_FLAG, config=config)
    if enabled is not True:
        raise ScopeCoreDisabled(what)


class _Unpassed:
    """The type of :data:`UNPASSED`: a primitive's ``enabled`` default, which is NOT a flag value."""

    def __repr__(self) -> str:
        return "UNPASSED"


#: default of a write PRIMITIVE's ``enabled`` argument: "the caller did not pass its flag". The primitive then refuses (fail closed).
UNPASSED = _Unpassed()


def _gate_primitive(enabled: Any, config: Any, what: str) -> None:
    """The gate of a write PRIMITIVE (``atomic_write_*``, ``fingerprint.dump``): the caller MUST pass its own ``enabled`` / ``config``
    (``None`` = read the real flag, exactly as :func:`require_enabled`); not passing it refuses. This makes the primitive layer, not
    only the objects above it, hold the gate at runtime (g54b e, audit-x13-runtime-gate P2-2): a ``ScopeWriter`` subclass whose
    ``_gate`` is a no-op, or a class defined outside the package, still cannot write through a primitive with the flag OFF."""
    if enabled is UNPASSED:
        raise ScopeCoreDisabled(f"{what}: the caller did not pass enabled/config (a write primitive fails closed)")
    require_enabled(enabled, config, what)


def qualify(workspace: str, slug: str) -> str:
    return f"{workspace}/{slug}"


def split_qualified(project: Any) -> Tuple[str, str]:
    """``'<ws>/<slug>'`` -> ``(ws, slug)``; a bare slug or anything else is refused (I4: always qualified)."""
    if not isinstance(project, str) or not QUALIFIED_RE.fullmatch(project):
        raise ScopeRefusal("project-not-qualified", f"project {project!r} is not a qualified <ws>/<slug> (I4)",
                           "name the project as <workspace>/<project>")
    ws, slug = project.split("/", 1)
    return ws, slug


def now_iso(clock: Optional[Callable[[], datetime]] = None) -> str:
    t = (clock or (lambda: datetime.now(timezone.utc)))()
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def atomic_write_text(path: str, text: str, *, enabled: Any = UNPASSED, config: Any = None) -> None:
    """Write ``text`` to ``path`` atomically (temp file in the same dir + ``os.replace``). Gates ITSELF, first: ``enabled`` / ``config``
    are the caller's flag arguments (see :func:`_gate_primitive`); omitted, it refuses and writes nothing."""
    _gate_primitive(enabled, config, "atomic_write_text")
    d = os.path.dirname(path) or "."
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".s2-", suffix=".tmp", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: str, obj: Any, *, enabled: Any = UNPASSED, config: Any = None) -> None:
    """:func:`atomic_write_text` of ``obj`` as JSON; gates ITSELF, first, the same way (omitted flag arguments refuse)."""
    _gate_primitive(enabled, config, "atomic_write_json")
    atomic_write_text(path, json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n", enabled=enabled, config=config)

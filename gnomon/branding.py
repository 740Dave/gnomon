"""Deployment branding: the header logo as DEPLOYMENT DATA (round 5 item 8).

The neutral tree ships NO brand asset. A deployment may carry ``<deploy_root>/defaults/branding.json``::

    {"logo": "branding/logo.png"}

``logo`` is a path RELATIVE to ``<deploy_root>/defaults/`` (the deploy root comes from the roots resolver via :mod:`deploy_data`). The web job
serves exactly that one file at ``GET /api/branding/logo`` (no parameter, no directory serving). Rules, each with a test:

* the path must be relative, free of ``..``, NUL, empty and dot-prefixed segments; its real path (symlinks followed) must stay inside
  ``<deploy_root>/defaults/``; it must be a regular file;
* the content type comes from the file EXTENSION (png, jpg, jpeg, gif, webp, ico); anything else is refused (no svg: it can carry script);
* the file is capped at :data:`MAX_BYTES`;
* a missing, unusable or refused table means NO logo (the page keeps its empty span); the reason is recorded through ``deploy_data``.

LAZY: nothing is read at import.
"""
from __future__ import annotations

import hashlib
import os
from typing import Dict, Optional, Tuple

MAX_BYTES = 512 * 1024
CONTENT_TYPES: Dict[str, str] = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
                                 ".webp": "image/webp", ".ico": "image/x-icon"}


def _fault(name: str, message: str, root: Optional[str]) -> None:
    from . import deploy_data

    deploy_data.record_fault(name, message, root)


def _clear_fault(root: Optional[str]) -> None:
    from . import deploy_data

    deploy_data.clear_fault("branding", root)


def logo_path(root: Optional[str] = None) -> Optional[Tuple[str, str]]:
    """``(absolute real path, content type)`` of the deployment's logo, or ``None`` (no table, or a refused one)."""
    from . import deploy_data

    table = deploy_data.load_json("branding", root)
    if not isinstance(table, dict) or "logo" not in table:
        if table is None and os.path.exists(deploy_data.data_path("branding.json", root) or "\0"):
            return None                       # a table that is present but unusable: load_json recorded why; keep it
        _clear_fault(root)                    # no table at all, or a usable one that names no logo: nothing to report
        return None
    rel = table["logo"]
    if not isinstance(rel, str) or not rel.strip() or "\0" in rel or os.path.isabs(rel) or rel.startswith("~"):
        _fault("branding", "'logo' must be a relative path string; no logo", root)
        return None
    parts = rel.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") or p.startswith(".") for p in parts):
        _fault("branding", f"'logo' path {rel!r} has an empty, dot or dot-prefixed segment; no logo", root)
        return None
    ctype = CONTENT_TYPES.get(os.path.splitext(rel)[1].lower())
    if ctype is None:
        _fault("branding", f"'logo' extension of {rel!r} is not one of {sorted(CONTENT_TYPES)}; no logo", root)
        return None
    base = os.path.realpath(deploy_data.data_dir(root))
    real = os.path.realpath(os.path.join(base, *parts))
    if not (real == base or real.startswith(base + os.sep)) or real == base:
        _fault("branding", f"'logo' path {rel!r} resolves outside the deployment data directory; no logo", root)
        return None
    try:
        st = os.stat(real)
    except OSError:
        _fault("branding", f"'logo' file {rel!r} is missing; no logo", root)
        return None
    import stat as _stat

    if not _stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES or st.st_size == 0:
        _fault("branding", f"'logo' file {rel!r} is not a regular file of 1 to {MAX_BYTES} bytes; no logo", root)
        return None
    _clear_fault(root)                        # table and asset are good: a fault recorded earlier (before a repair) is stale
    return real, ctype


def logo_asset(root: Optional[str] = None) -> Optional[Tuple[bytes, str]]:
    """``(bytes, content type)`` for the route, or ``None``. Reads at most MAX_BYTES + 1 bytes."""
    found = logo_path(root)
    if found is None:
        return None
    path, ctype = found
    try:
        with open(path, "rb") as fh:
            data = fh.read(MAX_BYTES + 1)
    except OSError:
        return None
    if not data or len(data) > MAX_BYTES:
        return None
    return data, ctype


def logo_info(root: Optional[str] = None) -> Optional[Dict[str, object]]:
    """What the deployment-data gate compares: ``None`` (the neutral default) or ``{content_type, bytes, sha256}`` of the served asset."""
    a = logo_asset(root)
    if a is None:
        return None
    return {"content_type": a[1], "bytes": len(a[0]), "sha256": hashlib.sha256(a[0]).hexdigest()}

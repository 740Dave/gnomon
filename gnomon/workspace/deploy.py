"""Where the DEPLOY root is (X13 S1 fix round, ruling-f4-2026-09-30).

ONE config key, ``deploy_root`` (``config.GnomonConfig.deploy_root``, default
``config.DEFAULT_DEPLOY_ROOT``), names the physical DEPLOY root. The operator's eventual rename of the root is a
ONE-LINE change: edit ``DEFAULT_DEPLOY_ROOT`` in ``config.py`` (or set ``deploy_root`` in config.json)
and move the directory. Nothing else in the package spells the root's name.

This module only RESOLVES paths; it reads the config file read-only and creates nothing.
"""

from __future__ import annotations

import os
from typing import Optional

from ..config import ConfigStore


def deploy_root(home: Optional[str] = None, override: Optional[str] = None) -> str:
    """The DEPLOY root: ``override`` if given, else ``ConfigStore.resolved_deploy_root(home)`` (g69, X13 S5-4b): an explicit
    ``deploy_root`` key of the machine config wins; otherwise the per-user roots file's ``deploy_root`` when one is in play;
    otherwise the legacy default. The config file is the DATA root's ``config.json`` (roots-aware). ``~`` expands against
    ``home`` (never the real HOME when a test supplies one)."""
    from .. import config as _config

    home_abs = os.path.abspath(os.path.expanduser(home or "~"))
    if override:
        raw = override
        if raw.startswith("~"):
            raw = home_abs + raw[1:]
        return os.path.abspath(raw)
    store = ConfigStore(_config.default_config_path(home_abs))
    return store.load().resolved_deploy_root(home_abs)


def converter_dir(root: str, workspace: str) -> str:
    """``<root>/workspaces/<workspace>/converter``: where a deployment keeps its converter rules and data."""
    return os.path.join(root, "workspaces", workspace, "converter")

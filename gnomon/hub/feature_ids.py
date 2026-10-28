"""the hub MCP tools' closed feature-id set (spec).

Lives in the hub package so the harness (which imports :mod:`gnomon.feature_sites`) never acquires the
hub / DSD code as a dependency. Neutral (g3).
"""

from __future__ import annotations

from typing import Any, FrozenSet, Optional


def hub_tool_names() -> FrozenSet[str]:
    """The hub's OWN tool table (a closed set), never the request body."""
    from . import mcp

    try:
        from ..appshost.verb_tools import APP_VERB_NAMES
    except Exception:  # noqa: BLE001 - F5: a broken Apps host must not cost every other tool its feature id
        APP_VERB_NAMES = frozenset()
    try:
        from ..scheduler.verb_tools import SCHED_VERB_NAMES
    except Exception:  # noqa: BLE001 - a broken scheduler must not cost every other tool its feature id
        SCHED_VERB_NAMES = frozenset()
    return frozenset({t["name"] for t in mcp.ALL_TOOLS} | {mcp.LAUNCH_AGENT_TOOL["name"]} | set(APP_VERB_NAMES)
                     | set(SCHED_VERB_NAMES))


def hub_tool_feature_id(name: Any) -> Optional[str]:
    """The tool name with underscores as hyphens, only for a name in the hub's own tool table."""
    try:
        if isinstance(name, str) and name in hub_tool_names():
            return name.replace("_", "-")
    except Exception:  # noqa: BLE001
        pass
    return None

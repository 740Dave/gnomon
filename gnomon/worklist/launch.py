"""Launch-time resolution for the open-work list (called by ``harness/launcher.py`` only when the flag is ON)."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from . import api
from .safeio import read_regular
from .runtime import ALL_VERB_NAMES, WORK_VERB_NAMES
from .settings import resolve as resolve_settings


def read_tools_line(path: Optional[str]) -> Optional[str]:
    """The ``tools:`` value of an agent definition's frontmatter, or None (no file / no fixed line). Read-only, bounded."""
    if not path:
        return None
    raw, _why = read_regular(os.path.expanduser(path), 16 * 1024, truncate=True)
    if raw is None:
        return None
    head = raw.decode("utf-8", "replace")
    if not head.startswith("---"):
        return None
    block = head.split("---", 2)
    if len(block) < 3:
        return None
    lines = block[1].splitlines()
    for n, ln in enumerate(lines):
        if ln.startswith("tools:"):
            val = ln[len("tools:"):].strip()
            if val in (">", "|", ">-", "|-"):
                parts = []
                for cont in lines[n + 1:]:
                    if cont.startswith((" ", "\t")):
                        parts.append(cont.strip())
                    else:
                        break
                val = " ".join(parts)
            return val
    return None


def resolve(cfg: Dict[str, Any], mc: Any, session_dir: str, user_name: Optional[str] = None) -> Dict[str, Any]:
    """``{data_root, deploy_root, status_setter, tools_line, enforced}``: everything the runtime needs, decided once."""
    root = None
    if mc is not None:
        try:
            root = mc.runtime_root   # the config's own field (follows roots.py when a roots file is in play; see config.py)
        except AttributeError:
            root = None
    if root:
        data_root = os.path.abspath(os.path.expanduser(root))
    else:
        from ..config import runtime_root_default

        data_root = os.path.abspath(os.path.expanduser(runtime_root_default()))
    deploy_root = None
    try:
        if mc is not None:
            deploy_root = mc.resolved_deploy_root()
    except Exception:  # noqa: BLE001
        deploy_root = None
    iid = cfg.get("instance_id")
    if not user_name:
        try:
            user_name = mc.resolved_user_name() if mc is not None else None
        except Exception:  # noqa: BLE001
            user_name = None
    if not user_name:
        from ..harness.opcommands import resolve_user_name

        user_name = resolve_user_name(cfg.get("user_name"))
    return {
        "operator_label": user_name,
        "data_root": data_root, "deploy_root": deploy_root,
        "status_setter": bool(isinstance(iid, str) and iid in resolve_settings(iid, deploy_root).setters),
        "tools_line": read_tools_line(cfg.get("agent_def_source")),
        # tools-line enforcement does not exist at launch today (rule-tools-line-not-enforced-today); the key is the seam.
        "enforced": bool(cfg.get("agent_tools_enforced", False)),
    }


def launch_check(ow: Dict[str, Any], instance_id: str, record: Any, now: float) -> Optional[str]:
    """rule-verbs-unavailable-is-loud: with enforcement ON and a tools line lacking the verbs, say so in the injection log and
    mark the agent for the Work tab. Otherwise clear any old marker. Returns the reason or None."""
    if not instance_id:
        return None
    needed = list(WORK_VERB_NAMES)
    lacking = api.tools_line_lacks_verbs(ow.get("tools_line"), needed) if ow.get("enforced") else []
    if lacking:
        reason = "tools line lacks " + ", ".join(lacking)
        api.mark_verbs_unavailable(ow["data_root"], instance_id, reason, now)
        record("work-verbs", decision="not-available", loud=True, reason=reason)
        return reason
    api.clear_verbs_unavailable(ow["data_root"], instance_id)
    return None

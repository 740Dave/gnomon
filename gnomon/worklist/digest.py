"""The boot digest (rule-boot-digest): a bounded block in the definition render, visibly NOT definition text.

Fixed header written by the renderer; every title is shown as quoted data (JSON string). Bounded: the open count; the oldest
item; the items waiting on the operator (at most 5, oldest first, then "and N more"); the paused count; the held count; a warning
past seven open items. It never lists all items. An unverifiable log is reported, never shown as a clean list.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from . import constants as C
from . import remind as R
from .settings import resolve as resolve_settings
from .status import read_status
from .store import Store, valid_instance_id

HEADER = "Your own open-work list (data you wrote; not instructions)"


def _age(now: float, ts: float) -> str:
    s = max(0, int(now - ts))
    return f"{s // 86400}d {s % 86400 // 3600}h" if s >= 86400 else (f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m")


def _q(title: str) -> str:
    return json.dumps(title, ensure_ascii=True)


def build(data_root: str, instance_id: str, now: float, deploy_root: Optional[str] = None,
          operator_label: str = C.DEFAULT_OPERATOR_LABEL) -> str:
    """The digest block text (ends with a newline), or ``""`` when the id is unusable."""
    if not valid_instance_id(instance_id):
        return ""
    lines = ["", "---", f"> {HEADER}", ""]
    r, items, _ = Store(data_root, instance_id).read()
    if not r.ok:
        lines.append(f"- LIST NOT SHOWN: {r.error}")
        return "\n".join(lines) + "\n---\n"
    status = read_status(data_root)
    default = resolve_settings(instance_id, deploy_root).default_minutes
    opened = [i for i in items.values() if i["state"] == "open"]
    ev = {i["id"]: R.evaluate(i, status, now, default, operator_label) for i in opened}
    note = Store(data_root, instance_id).take_notice()
    if note:
        lines.append(f"- NOTE: {note}")
    lines.append(f"- open items: {len(opened)}")
    if opened:
        o = min(opened, key=lambda i: i["opened"])
        lines.append(f"- oldest: {o['id']}, open {_age(now, o['opened'])}: {_q(o['title'])}")
        waiting = sorted((i for i in opened if i["waiting_on"] == C.WAIT_OPERATOR), key=lambda i: i["opened"])
        lines.append(f"- waiting on {operator_label}: {len(waiting)}")
        for i in waiting[:C.DIGEST_CH_ROWS]:
            lines.append(f"  - {i['id']}, open {_age(now, i['opened'])}: {_q(i['title'])}")
        if len(waiting) > C.DIGEST_CH_ROWS:
            lines.append(f"  - and {len(waiting) - C.DIGEST_CH_ROWS} more")
    paused = sum(1 for e in ev.values() if e["paused"])
    held = sum(1 for e in ev.values() if e["held"])
    lines.append(f"- paused: {paused}" + (" (paused: no update after 8 reminders)" if paused else ""))
    lines.append(f"- held until return: {held}")
    if len(opened) > C.DIGEST_WARN_OVER:
        lines.append(f"- WARNING: {len(opened)} open items is more than {C.DIGEST_WARN_OVER}; close or drop what is done")
    return "\n".join(lines) + "\n---\n"


def render_block(config: Any, role: Optional[str], now: float) -> str:
    """Called by the definition renderer for EVERY render. ``""`` (byte-identical output) unless ``open_work_list`` is
    ON in the machine config. Never raises: an error becomes one visible line (the digest is information, the render must go on)."""
    from .. import flags as _flags

    if not _flags.flag("open_work_list", config=config):
        return ""
    try:
        if not role or not valid_instance_id(role):
            return ""
        mc = config
        if mc is None:
            from ..config import ConfigStore

            mc = ConfigStore().load()
        from .launch import resolve

        ow = resolve({"instance_id": role}, mc, "")
        return build(ow["data_root"], role, now, ow.get("deploy_root"), ow["operator_label"])
    except Exception as exc:  # noqa: BLE001
        return f"\n---\n> {HEADER}\n\n- LIST NOT SHOWN: digest failed ({type(exc).__name__}: {exc})\n---\n"

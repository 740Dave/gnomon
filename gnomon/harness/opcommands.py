"""The standard ``\\`` operator commands (PTY-hooks WS-B control + WS-F messaging).

Control family:
  ``\\hooks off|on``   RESERVED fail-safe — content hooks off/on (never gated)
  ``\\pause``          hold every non-operator injection (DEFER) until resume
  ``\\resume``         release the held queue, in order
  ``\\approve [n|all]`` release held injection(s) (the operator veto plane)
  ``\\reject [n|all]``  drop held injection(s) — nothing reaches the child
  ``\\status``         hooks / pause / held / stall / log state
  ``\\help``           list commands

Messaging family (WS-F; HUB-SIDE fan-out — needs the hub to understand the
``operator_fanout`` frame, i.e. a dashboard restart onto this build):
  ``\\all <msg>``       every live registered instance, wrapped
                         ``<{user_name}-to-all@YYYY-MM-DD HH:MM> {msg}``
  ``\\<instance> <msg>`` one instance, wrapped ``<{user_name}-to-{id}@YYYY-MM-DD HH:MM> {msg}``

Memory (2026-09-28, operator design; ``gnomon.memory``):
  ``\\memory [<type>] <short summary> :: <full what>``  record one
                         memory into THIS session's agent's OWN
                         personality corpus (the harness's instance_id — never a
                         caller-named target). Untyped -> the plain MEM class.
                         No `` :: `` -> the text is both summary and what
                         (<= 140 chars). Command plane only: the line never
                         reaches the child. ``/memory`` is NOT intercepted — it
                         is Claude Code's own built-in and passes through.
"""

from __future__ import annotations

import getpass
import json
from typing import Any, Callable, Dict, Optional

from .hooks import InjectionGate
from . import stamps as _stamps
from .optap import CommandContext, OperatorInputTap

#: Placeholder when neither the Config tab nor the OS supplies a user name.
USER_NAME_PLACEHOLDER = "operator"


def resolve_user_name(configured: Optional[str]) -> str:
    """Config-tab ``user_name`` if set; else the OS login; else a placeholder."""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    try:
        name = getpass.getuser()
    except Exception:  # noqa: BLE001
        name = ""
    return name.strip() or USER_NAME_PLACEHOLDER


def wrap_broadcast(user_name: str, msg: str, now: Optional[float] = None) -> str:
    """``<NAME-to-all@YYYY-MM-DD HH:MM> msg`` — stamped when composed (local, 24 h)."""
    return f"<{user_name}-to-all@{_stamps.stamp_now(now)}> {msg}"


def wrap_dm(user_name: str, instance_id: str, msg: str, now: Optional[float] = None) -> str:
    return f"<{user_name}-to-{instance_id}@{_stamps.stamp_now(now)}> {msg}"


def install_standard_commands(
    tap: OperatorInputTap,
    gate: InjectionGate,
    *,
    monitor: Any = None,
    hub_send: Optional[Callable[[Dict[str, Any]], None]] = None,
    user_name: str = USER_NAME_PLACEHOLDER,
    instance_id: Optional[str] = None,
    status_extra: Optional[Callable[[], Dict[str, Any]]] = None,
    remember_fn: Optional[Callable[..., Any]] = None,
    message_log: Any = None,
) -> None:
    """``message_log``: an ``operator_messages.OperatorMessageLog``. When
    set, every \\all / \\<instance> message is saved IN FULL with a ``msg_id`` and
    its sha256 (carried in the hub frame and the injection_log record so they link),
    and every \\command line too (via ``tap.message_log``). None = legacy frames."""
    if message_log is not None:
        tap.message_log = message_log
        tap.message_log_instance = instance_id

    def _hooks(ctx: CommandContext) -> str:
        arg = ctx.args.lower()
        if arg not in ("on", "off"):
            return f"usage: \\hooks on|off (currently {'on' if gate.hooks_enabled else 'off'})"
        gate.set_hooks_enabled(arg == "on")
        return f"content hooks {arg.upper()} (provenance logging continues)"

    def _pause(_ctx: CommandContext) -> str:
        gate.set_paused(True)
        return "paused: harness injections are HELD until \\resume (your own typing is unaffected)"

    def _resume(_ctx: CommandContext) -> str:
        released = gate.set_paused(False)
        return f"resumed; released {len(released)} held injection(s)"

    def _approve(ctx: CommandContext) -> str:
        # F6 (2026-10-03 ruling): a plain \approve never releases an item that is held because a dialog is
        # open (a CR typed into it picks the first option); the explicit force form exists for the operator.
        toks = (ctx.args or "").split()
        # flag-off parity (D6): with the modal hold OFF the token is not parsed (it is an unknown selector,
        # exactly as on the tree before the hold existed)
        hold_on = getattr(gate, "modal_check", None) is not None
        force = hold_on and "--into-dialog" in toks
        rest = [t for t in toks if not (hold_on and t == "--into-dialog")]
        which = " ".join(rest) or "all"
        before = set(gate.modal_held_ids())
        released = gate.approve(which, force=force)
        if force:
            tap.clear_forced_busy()   # his explicit release also ends a forced-busy mark (E1)
        msg = f"approved {released or 'nothing'}"
        still = sorted(i for i in gate.modal_held_ids() if i in before)
        if which not in ("all", "") and still:
            try:
                still = [i for i in still if i == int(which)]
            except ValueError:
                still = []
        if still and not force:
            msg += (f"; {len(still)} held ({still}) because a dialog is open or your line is busy: typing "
                    "them into a dialog would answer it. Close the dialog (or finish/clear your line) and "
                    "they deliver by themselves, or force with \\approve --into-dialog [id]")
        return msg

    def _reject(ctx: CommandContext) -> str:
        which = ctx.args or "all"
        rejected = gate.reject(which)
        return f"rejected {rejected or 'nothing'}"

    def _status(_ctx: CommandContext) -> str:
        st = gate.status()
        if monitor is not None and getattr(monitor, "stall_mode", False):
            st["stall"] = {"fires": monitor.stall_fires, "escalations": monitor.stall_escalations}
        if status_extra is not None:
            try:
                st.update(status_extra())
            except Exception as exc:  # noqa: BLE001
                st["status_extra_error"] = str(exc)
        return json.dumps(st, default=str)

    def _help(_ctx: CommandContext) -> str:
        return "\n".join(f"\\{n}  {h}" for n, h in tap.commands().items()) + \
            "\n\\<instance> <msg>  DM one instance (via hub)"

    def _send(target: str, text: str) -> str:
        if hub_send is None:
            raise RuntimeError("no hub connection — messaging unavailable (nothing injected)")
        frame: Dict[str, Any] = {"type": "operator_fanout", "target": target, "text": text,
                                 "from": instance_id, "user_name": user_name}
        link: Dict[str, Any] = {}
        not_saved = ""
        if message_log is not None:
            # FULL copy first (before the send, so a failed send still
            # leaves the text recoverable); the msg_id + sha256 travel with the frame.
            from .operator_messages import new_msg_id, sha256_text

            link = {"msg_id": new_msg_id(), "sha256": sha256_text(text)}
            frame.update(link)
            saved = message_log.log_message(kind="broadcast" if target == "all" else "dm", text=text,
                                            from_instance=instance_id, target=target, msg_id=link["msg_id"])
            if saved is None:  # round 2 (Diamond F5, g65(c)): never silent, in the reply too
                not_saved = (f" WARNING: the full copy was NOT saved "
                             f"({getattr(message_log, 'last_error', None) or 'write failed'}).")
        hub_send(frame)
        extra = {"msg_id": link["msg_id"], "text_sha256": link["sha256"], "text_len": len(text)} if link else {}
        gate.record("operator-fanout", target=target, from_instance=instance_id, decision="sent-to-hub",
                    text_preview=text[:120], **extra)
        return f"sent to hub for '{target}' (delivery ack follows from the hub)" + not_saved

    def _all(ctx: CommandContext) -> str:
        if not ctx.args:
            return "usage: \\all <message>"
        return _send("all", wrap_broadcast(user_name, ctx.args))

    def _dm(ctx: CommandContext) -> str:
        return _send(ctx.name, wrap_dm(user_name, ctx.name, ctx.args))

    def _memory(ctx: CommandContext) -> str:
        """``\\memory <...>``: parse -> validate -> write via ``memory.remember``.
        The command plane never forwards the line, so a refusal just reports the
        reason here and the operator retypes."""
        from ..memory import PROGRESS_TYPE, MemoryError_, parse_memory_args, progress_flag_on

        door = "\\memory"
        if not instance_id:
            gate.record("memory-remember", source="operator", surface=door, decision="refused",
                        reason="no harness instance_id")
            return "memory REFUSED (nothing written): this harness has no instance_id"
        try:
            kind, summary, what = parse_memory_args(ctx.args, progress=progress_flag_on())
        except MemoryError_ as exc:
            gate.record("memory-remember", source="operator", surface=door, instance_id=instance_id,
                        decision="refused", reason=str(exc))
            return f"memory REFUSED (nothing written): {exc}"
        fn = remember_fn
        if fn is None:
            from ..memory import remember as fn  # type: ignore[no-redef]
        # a progress entry is filed under THIS session (the gate holds the harness's own session_dir)
        kw = {"session_dir": getattr(gate, "session_dir", None) or ""} if kind == PROGRESS_TYPE else {}
        try:
            res = fn(instance_id, kind, summary, what, **kw)
        except Exception as exc:  # noqa: BLE001 - never crash the pump
            gate.record("memory-remember", source="operator", surface=door, instance_id=instance_id,
                        memory_type=kind, decision="error", reason=f"{type(exc).__name__}: {exc}")
            return f"memory FAILED (nothing written): {type(exc).__name__}: {exc}"
        gate.record("memory-remember", source="operator", surface=door, instance_id=instance_id,
                    memory_type=kind, node_id=res.node_id, path=res.path, line=res.line,
                    written=res.written, ok=res.ok, reason=res.reason, summary_preview=summary[:140])
        return res.receipt

    tap.register("hooks", _hooks, "on|off — RESERVED fail-safe: content hooks on/off")
    tap.register("pause", _pause, "hold all harness injections")
    tap.register("resume", _resume, "release held injections")
    tap.register("approve", _approve, "[n|all] release held injection(s)")
    tap.register("reject", _reject, "[n|all] drop held injection(s)")
    tap.register("status", _status, "hooks/pause/held/stall/log state")
    tap.register("help", _help, "this list")
    tap.register("all", _all, "<msg> broadcast to every live instance (via hub)")
    from ..memory import help_types as _help_types, progress_flag_on as _progress_on

    tap.register("memory", _memory, "[" + _help_types(_progress_on()) + "] <summary> :: <what> — remember into "
                 "YOUR OWN personality corpus")
    if hub_send is not None:
        tap.set_fallback(_dm)

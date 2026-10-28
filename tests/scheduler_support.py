"""Test support for the Scheduler (stage 1a): a fake registry that records every routed frame, a fake clock,
fake target-session directories with an injection_log.jsonl, and a harness emulator that writes the records the real
InjectionGate would. Nothing here touches a live hub, a live session or ~/session-root (g31)."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Optional

from gnomon.scheduler.engine import Scheduler

T0 = _dt.datetime(2026, 10, 10, 12, 0, 0, tzinfo=_dt.timezone.utc).timestamp()
PRESENCE_OK = {"ok": True, "result": "authenticated", "method": "stub", "ts": 0.0}


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> float:
        self.t += s
        return self.t


class _Sess:
    def __init__(self, meta: Dict[str, Any]) -> None:
        self.meta = meta


class FakeRegistry:
    """Records every ``route``; ``on_route(iid, frame)`` lets a test emulate the harness; a name in ``dead`` has a dead
    socket (OSError); an unregistered name raises KeyError exactly like ``UnknownSession``."""

    def __init__(self) -> None:
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.routed: List[Any] = []
        self.dead: set = set()
        self.on_route: Optional[Callable[[str, Dict[str, Any]], None]] = None

    def register(self, iid: str, session_dir: Optional[str]) -> None:
        self.sessions[iid] = {"session_dir": session_dir}

    def is_registered(self, iid: str) -> bool:
        return iid in self.sessions

    def get(self, iid: str) -> _Sess:
        if iid not in self.sessions:
            raise KeyError(iid)
        return _Sess(self.sessions[iid])

    def route(self, iid: str, frame: Dict[str, Any]) -> None:
        if iid not in self.sessions:
            raise KeyError(iid)
        if iid in self.dead:
            raise OSError("socket closed")
        self.routed.append((iid, frame))
        if self.on_route:
            self.on_route(iid, frame)


class Target:
    """A fake target session: a session dir whose .claude/injection_log.jsonl a test appends to."""

    def __init__(self, tmp_path: Any, name: str = "agent-a") -> None:
        self.name = name
        self.dir = str(tmp_path / f"sess-{name}")
        os.makedirs(os.path.join(self.dir, ".claude"), exist_ok=True)
        self.log = os.path.join(self.dir, ".claude", "injection_log.jsonl")
        self.seq = 0
        self.clock = None                     # set by make(): records are stamped with the fake clock
        self.append({"kind": "lifecycle", "point": "agent-start", "payload": {}})

    def append(self, rec: Dict[str, Any]) -> None:
        self.seq += 1
        rec = {"seq": self.seq, "ts": self.clock.t if self.clock else 1.0, "ts_iso": "2026-10-10T12:00:00Z", "instance_id": self.name, **rec}
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")

    def compact(self, phase: str) -> None:
        self.append({"kind": "lifecycle", "point": "compact", "payload": {"phase": phase, "trigger": "manual"}})

    def boot(self, decision: str) -> None:
        self.append({"kind": "post-compact-boot", "decision": decision, "trigger": "post-compact-boot"})

    def deliver(self, frame: Dict[str, Any], decision: str = "allow", **extra: Any) -> None:
        """What InjectionGate.decide records for a hub frame (source bus)."""
        self.append({"kind": "injection", "source": "bus", "trigger": frame["trigger"], "decision": decision,
                     "content_sha256": hashlib.sha256(frame["text"].encode()).hexdigest(), **extra})

    def hold(self, frame: Dict[str, Any], held_id: int = 1) -> None:
        self.append({"kind": "injection", "source": "bus", "trigger": frame["trigger"], "decision": "defer",
                     "held_id": held_id, "handler": "modal-hold",
                     "content_sha256": hashlib.sha256(frame["text"].encode()).hexdigest()})

    def release(self, frame: Dict[str, Any], held_id: int = 1) -> None:
        self.deliver(frame, "release", held_id=held_id)


def make(tmp_path: Any, *, owner: str = "agent-a", turn: str = "idle", ledger: Optional[bool] = None,
         register: bool = True):
    clock, reg = Clock(), FakeRegistry()
    tgt = Target(tmp_path, owner)
    tgt.clock = clock
    if register:
        reg.register(owner, tgt.dir)
    sch = Scheduler(str(tmp_path / "data" / "scheduler"), registry=reg, clock=clock, tz=_dt.timezone.utc,
                    turn_state=lambda sd, now: turn, effect_ledger_on=lambda: ledger,
                    nonce=iter(f"{i:06x}" for i in range(1, 10_000)).__next__)
    return sch, clock, reg, tgt


def propose_inject(sch: Scheduler, owner: str = "agent-a", job_id: str = "remind-me", *, trigger: Optional[dict] = None,
                   text: str = "check the inbox", **kw: Any) -> Dict[str, Any]:
    args = {"job_id": job_id, "title": "Reminder", "trigger": trigger or {"once_at": "2026-10-10T12:05:00Z"},
            "action": {"type": "inject", "text": text}}
    args.update(kw)
    return sch.propose(args, proposer=owner, verified=True)


def confirm(sch: Scheduler, job_id: str = "remind-me", presence: Any = None, digest: Any = "auto"):
    d = sch.proposal_digest(job_id) if digest == "auto" else digest
    return sch.confirm(job_id, actor="operator-page", presence=PRESENCE_OK if presence is None else presence,
                       expect_digest=d)


def run_ticks(sch: Scheduler, clock: Clock, seconds: float, step: float = 30.0) -> None:
    end = clock.t + seconds
    while clock.t < end:
        clock.advance(step)
        sch.tick()

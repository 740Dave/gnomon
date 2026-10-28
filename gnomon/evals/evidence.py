"""The evidence one eval run leaves behind, loaded from disk.

A behavioural eval never trusts the agent's own account of what it did. It reads
the artifacts the run left:

* ``<session>/.claude/injection_log.jsonl`` — the session provenance log (WS-A);
* ``<runtime_root>/agent_launch/launch_log.jsonl`` — the OPERATOR launch ledger
  (``launched`` / ``finished`` / ``deny`` / ``error`` records for this run);
* the Claude Code transcript JSONL, found ONLY via the path D40 recorded in
  ``<session>/.claude/launch.json`` (``transcript_path``), with the
  ``.claude/transcript.jsonl`` symlink as the one sanctioned fallback — never by
  globbing ``~/.claude/projects`` for the newest file;
* files the agent wrote (resolved against the session dir, the agent's cwd).

Every load failure is RECORDED (``load_errors``) and every source that loaded is
listed in ``loaded_sources``. A check for the ABSENCE of a record refuses to pass
over a source that never loaded — a search over nothing is not a clean search.

Pure stdlib (no pydantic) so the checks are usable and testable anywhere.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Approximate list prices, USD per million tokens (input, output, cache-write,
#: cache-read). Used ONLY when the transcript carries no ``cost-state`` record;
#: the report then labels the figure ``estimate``. Matched by substring of the
#: model id, most specific first.
PRICE_TABLE_USD_PER_MTOK: Tuple[Tuple[str, Tuple[float, float, float, float]], ...] = (
    ("haiku", (1.00, 5.00, 1.25, 0.10)),
    ("sonnet", (3.00, 15.00, 3.75, 0.30)),
    ("opus", (5.00, 25.00, 6.25, 0.50)),
)

SOURCES = ("injection_log", "launch_log", "transcript", "tool_calls")


def read_jsonl(path: str) -> Tuple[List[Dict[str, Any]], int]:
    """Records of a JSONL file + the count of lines that were not JSON objects.

    Raises ``OSError`` if the file cannot be read (the caller records it)."""
    records: List[Dict[str, Any]] = []
    bad = 0
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            if isinstance(obj, dict):
                records.append(obj)
            else:
                bad += 1
    return records, bad


@dataclass
class ToolCall:
    name: str
    input: Dict[str, Any]
    id: Optional[str] = None
    message_id: Optional[str] = None

    def as_record(self) -> Dict[str, Any]:
        return {"name": self.name, "input": self.input, "id": self.id, "message_id": self.message_id}


@dataclass
class RunEvidence:
    """Everything an evaluator may look at for one case run."""

    case_name: str = ""
    session_dir: Optional[str] = None
    runtime_root: Optional[str] = None
    launch_ok: bool = False
    exit_code: Optional[int] = None
    output: str = ""
    error: Optional[str] = None
    transcript_path: Optional[str] = None
    transcript_check: Dict[str, Any] = field(default_factory=dict)
    launch_report: Dict[str, Any] = field(default_factory=dict)
    injection_log: List[Dict[str, Any]] = field(default_factory=list)
    launch_log: List[Dict[str, Any]] = field(default_factory=list)
    transcript: List[Dict[str, Any]] = field(default_factory=list)
    loaded_sources: List[str] = field(default_factory=list)
    load_errors: List[str] = field(default_factory=list)
    requested_model: Optional[str] = None
    duration_s: float = 0.0

    # -- loading ------------------------------------------------------------

    @classmethod
    def from_session(
        cls,
        session_dir: Optional[str],
        *,
        runtime_root: Optional[str] = None,
        case_name: str = "",
        launch_report: Optional[Dict[str, Any]] = None,
        transcript_check: Optional[Dict[str, Any]] = None,
        requested_model: Optional[str] = None,
        duration_s: float = 0.0,
    ) -> "RunEvidence":
        rep = dict(launch_report or {})
        ev = cls(
            case_name=case_name,
            session_dir=session_dir,
            runtime_root=runtime_root,
            launch_ok=bool(rep.get("ok", False)),
            exit_code=rep.get("exit_code"),
            output=rep.get("output") or "",
            error=rep.get("error"),
            launch_report=rep,
            transcript_check=dict(transcript_check or {}),
            requested_model=requested_model,
            duration_s=duration_s,
        )
        if not session_dir:
            ev.load_errors.append("no session dir — the launch never built a session")
        else:
            ev._load_injection_log()
            ev._load_transcript()
        if runtime_root:
            ev._load_launch_log()
        else:
            ev.load_errors.append("no runtime root — the operator launch ledger was not located")
        return ev

    def _claude_dir(self) -> str:
        return os.path.join(self.session_dir or "", ".claude")

    def _load_injection_log(self) -> None:
        path = os.path.join(self._claude_dir(), "injection_log.jsonl")
        try:
            self.injection_log, bad = read_jsonl(path)
        except OSError as exc:
            self.load_errors.append(f"injection_log unreadable at {path}: {exc}")
            return
        self.loaded_sources.append("injection_log")
        if bad:
            self.load_errors.append(f"injection_log: {bad} non-JSON line(s) skipped")

    def _load_launch_log(self) -> None:
        path = os.path.join(self.runtime_root or "", "agent_launch", "launch_log.jsonl")
        try:
            self.launch_log, bad = read_jsonl(path)
        except OSError as exc:
            self.load_errors.append(f"launch_log unreadable at {path}: {exc}")
            return
        self.loaded_sources.append("launch_log")
        if bad:
            self.load_errors.append(f"launch_log: {bad} non-JSON line(s) skipped")

    def _load_transcript(self) -> None:
        """Via D40's recorded ``transcript_path`` ONLY (symlink as fallback)."""
        recorded: Optional[str] = None
        try:
            with open(os.path.join(self._claude_dir(), "launch.json"), "r", encoding="utf-8") as fh:
                recorded = json.load(fh).get("transcript_path")
        except (OSError, ValueError) as exc:
            self.load_errors.append(f"launch.json unreadable — transcript location unknown: {exc}")
        link = os.path.join(self._claude_dir(), "transcript.jsonl")
        candidates = [p for p in (recorded, link) if p]
        if not recorded:
            self.load_errors.append("launch.json records no transcript_path (D40 location missing)")
        for path in candidates:
            if not os.path.isfile(path):
                continue
            try:
                self.transcript, bad = read_jsonl(path)
            except OSError as exc:
                self.load_errors.append(f"transcript unreadable at {path}: {exc}")
                continue
            self.transcript_path = path
            self.loaded_sources.extend(["transcript", "tool_calls"])
            if bad:
                self.load_errors.append(f"transcript: {bad} non-JSON line(s) skipped")
            return
        self.transcript_path = recorded
        self.load_errors.append(f"transcript NOT found at the recorded path {recorded!r}")

    # -- derived views --------------------------------------------------------

    def tool_calls(self) -> List[ToolCall]:
        """Every ``tool_use`` block in assistant records, deduplicated by tool-use id
        (Claude Code writes one assistant message across several JSONL lines)."""
        calls: List[ToolCall] = []
        seen: set = set()
        for rec in self.transcript:
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            for block in msg.get("content") or []:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                tid = block.get("id")
                if tid is not None and tid in seen:
                    continue
                if tid is not None:
                    seen.add(tid)
                inp = block.get("input") if isinstance(block.get("input"), dict) else {}
                calls.append(ToolCall(str(block.get("name")), inp, tid, msg.get("id")))
        return calls

    def model_requests(self) -> List[Dict[str, Any]]:
        """One entry per distinct assistant message id: ``{id, model, usage}``.

        This is the TURN count the budget guard uses (model requests, not lines)."""
        out: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for i, rec in enumerate(self.transcript):
            if rec.get("type") != "assistant":
                continue
            msg = rec.get("message") or {}
            mid = msg.get("id") or f"<no-id-{i}>"
            if mid not in out:
                order.append(mid)
            # later lines of the same message carry the final usage — keep the last
            out[mid] = {"id": mid, "model": msg.get("model"), "usage": msg.get("usage") or {}}
        return [out[m] for m in order]

    def models_used(self) -> List[str]:
        seen: List[str] = []
        for req in self.model_requests():
            m = req.get("model")
            if m and m not in seen:
                seen.append(m)
        return seen

    def usage_totals(self) -> Dict[str, int]:
        keys = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
        tot = {k: 0 for k in keys}
        for req in self.model_requests():
            u = req.get("usage") or {}
            for k in keys:
                v = u.get(k)
                if isinstance(v, (int, float)):
                    tot[k] += int(v)
        return tot

    def cost(self) -> Tuple[Optional[float], str]:
        """``(usd, source)``. ``source`` is ``cost-state`` (Claude Code's own figure,
        the last one in the transcript), ``estimate`` (usage x the price table), or
        ``unknown`` (no figure could be derived — callers must NOT treat it as 0)."""
        reqs = self.model_requests()
        for rec in reversed(self.transcript):
            if rec.get("type") != "cost-state":
                continue
            # rm2a2 F2: the LATEST cost-state decides. A malformed one (non-number,
            # NaN/inf, negative) is NOT papered over with an estimate, and a 0 that
            # contradicts real token usage is not believed — both are ``unknown``.
            v = rec.get("totalCostUSD")
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                return None, "unknown"
            if v == 0 and any(self.usage_totals().values()):
                return None, "unknown"
            return float(v), "cost-state"
        if not reqs:
            return None, "unknown"
        total = 0.0
        for req in reqs:
            price = _price_for(req.get("model"))
            if price is None:
                return None, "unknown"
            u = req.get("usage") or {}
            p_in, p_out, p_cw, p_cr = price
            total += (int(u.get("input_tokens") or 0) * p_in + int(u.get("output_tokens") or 0) * p_out
                      + int(u.get("cache_creation_input_tokens") or 0) * p_cw
                      + int(u.get("cache_read_input_tokens") or 0) * p_cr) / 1_000_000
        return total, "estimate"

    def records(self, source: str) -> List[Dict[str, Any]]:
        if source == "injection_log":
            return self.injection_log
        if source == "launch_log":
            return self.launch_log
        if source == "transcript":
            return self.transcript
        if source == "tool_calls":
            return [c.as_record() for c in self.tool_calls()]
        raise ValueError(f"unknown evidence source {source!r} (known: {', '.join(SOURCES)})")

    def resolve_path(self, path: str) -> str:
        """A case file path: ``~`` expanded; relative = relative to the session dir
        (the headless agent's cwd)."""
        p = os.path.expanduser(path)
        if not os.path.isabs(p):
            p = os.path.join(self.session_dir or "", p)
        return p

    def summary(self) -> Dict[str, Any]:
        usd, src = self.cost()
        return {
            "session_dir": self.session_dir,
            "runtime_root": self.runtime_root,
            "launch_ok": self.launch_ok,
            "exit_code": self.exit_code,
            "error": self.error,
            "transcript_path": self.transcript_path,
            "transcript_check_status": self.transcript_check.get("status"),
            "requested_model": self.requested_model,
            "models_used": self.models_used(),
            "turns": len(self.model_requests()),
            "tool_calls": [c.name for c in self.tool_calls()],
            "usage": self.usage_totals(),
            "cost_usd": usd,
            "cost_source": src,
            "loaded_sources": list(self.loaded_sources),
            "load_errors": list(self.load_errors),
            "duration_s": round(self.duration_s, 2),
            "output_head": (self.output or "")[:400],
        }


def _price_for(model: Optional[str]) -> Optional[Tuple[float, float, float, float]]:
    if not model:
        return None
    low = model.lower()
    for key, price in PRICE_TABLE_USD_PER_MTOK:
        if key in low:
            return price
    return None

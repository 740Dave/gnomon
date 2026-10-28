"""``restart_service`` — a caller-restricted Gnomon self-verb.

Restarts a service from the SERVICE REGISTRY (:mod:`gnomon.service_registry`,
machine config, operator-owned: host / mechanism / health / hosts_hub) with the D52 etiquette
built in. WHO may call it is decided ONLY by the central verb policy
(:mod:`gnomon.harness.verb_policy`): ``always`` callers run immediately; everyone
else is ``confirm-twice`` (warning first, then a second call with an ``ack``) unless the
policy says ``deny``:

    identity -> flag -> registry -> service -> POLICY (allow | confirm | deny)
      -> PREFLIGHT (silent | disruptive) -> [refuse unless coordinated / acknowledged]
      -> ACT -> VERIFY (bounded) -> NOTIFY -> PROVENANCE

⛔ SAFETY (binding, statically tested by ``tests/test_x30_restart_service.py``)
--------------------------------------------------------------------------
* The ONLY process actions in this module are two fixed argv lists, run by ONE
  ``subprocess.run`` call site (``_run_argv``), never through a shell:

    local  launchd : ``/bin/launchctl kickstart -k gui/<uid>/<label>``
    remote (either): ``/usr/bin/ssh -o BatchMode=yes -o ConnectTimeout=<n> -- <host> <cmd>``
        ``<cmd>`` is ``launchctl kickstart -k gui/$(id -u)/<label>`` (launchd) or
        ``sudo -n systemctl restart <unit>`` (systemd): a FIXED template whose only
        variable parts are a registry-validated label / unit (strict regex).

  ``<label>``, ``<unit>`` and ``<host>`` come ONLY from a registry entry that
  passed validation; the ONLY caller-supplied string that influences them is the
  ``service`` id, used as a dict lookup key. No caller string is ever placed in
  an argv (tested by injecting hostile strings into every parameter).
* No pid is ever signalled, no process is pattern-matched, nothing is killed by
  this module (``os.kill`` / ``signal`` / ``Popen`` are absent; tested).
* Nothing here types into a PTY (g28): a notice is delivered through the EXISTING
  hub ``inject`` path (the hub's operator MCP tool), which lands in the target
  harness's own injector/gate (and its effect ledger). This module only makes an
  authenticated HTTP call to the hub.

Caller policy has TWO layers, both tested: (1) the launcher passes ``--restart-service``
to the self-MCP server only when the flag is ON and the policy does not ``deny`` the
session's identity for every service (confirm-twice keeps the tool VISIBLE to everyone;
only ``deny`` hides it), and ``tools/list`` re-resolves the identity over the control
socket; (2) :func:`run` re-checks identity, flag and the policy per service at CALL time
(the confirm-twice state machine lives there) and a refusal writes a provenance record.

Bounded waits (g27b h): every wait has a floor and a ceiling; a malformed feature
config refuses the call loudly (``config-invalid``) and never launches anything.

stdlib + package-local imports only. Feature-id for the X26 emitter (g1), which
does not exist yet: ``restart_service``.
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import service_registry as SR
from .harness import verb_policy as VP

FLAG_NAME = "restart_service"
#: The X26 feature-id this verb WILL emit once feature_event exists (g1).
FEATURE_ID = "restart_service"
TOOL_NAME = "restart_service"
#: Provenance: kind, source (hooks.SOURCE_HARNESS, never operator-attributable) and trigger.
PROVENANCE_KIND = "restart-service"
PROVENANCE_SOURCE = "harness"
PROVENANCE_TRIGGER = "restart-service"
#: The launch.json key family holding this feature's (tighten-only) durations.
CONFIG_KEY = "restart_service"

LAUNCHCTL = "/bin/launchctl"
SSH = "/usr/bin/ssh"

#: name -> (default, floor, ceiling) seconds. A config value must lie in [floor, ceiling];
#: nothing can raise a wait above its ceiling.
DURATIONS: Dict[str, Tuple[float, float, float]] = {
    "health_wait_seconds": (60.0, 1.0, 60.0),
    "reregister_wait_seconds": (120.0, 1.0, 120.0),
    "notice_wait_seconds": (30.0, 0.0, 120.0),
    "ssh_connect_timeout_seconds": (10.0, 1.0, 10.0),
    "act_timeout_seconds": (30.0, 5.0, 60.0),
    #: the TOTAL wall-clock budget for the whole call, enforced across every wait (F-8, g27b)
    "total_budget_seconds": (180.0, 10.0, 300.0),
}
POLL_SECONDS = 1.0
#: Per-call network timeouts. The TOTAL budget bounds call LATENCY too (F-R2-2): every call is handed
#: min(ceiling, remaining budget), and no new call starts once the remaining budget is under the floor.
CALL_TIMEOUT_SECONDS = 5.0        # hub MCP calls (registration probe, notice inject)
HEALTH_TIMEOUT_SECONDS = 3.0      # /api/health and registered health URLs
CALL_FLOOR_SECONDS = 0.5
#: A restart is never STARTED without the time to verify it: the remaining budget at the act must cover
#: one health probe plus one hub call.
MIN_ACT_BUDGET_SECONDS = HEALTH_TIMEOUT_SECONDS + CALL_TIMEOUT_SECONDS
DEFAULT_WEB_PORT = 8770
MAX_COORDINATED_WITH = 200
#: X30 fix: a DISCONNECTED roster entry that never recorded a pid (a liveness-scan entry) is a stale ghost
#: once its newest activity evidence (canary, transcript, session-dir files) is older than this.
STALE_NO_PID_SECONDS = 24 * 3600.0

_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]")
_LOCK = threading.Lock()


class RunnerTimeout(Exception):
    """The fixed argv did not finish inside its bound."""


class Budget:
    """The TOTAL wall-clock budget of one call (F-8, F-R2-2)."""

    def __init__(self, now: Callable[[], float], t_end: float) -> None:
        self._now, self.t_end = now, t_end

    def remaining(self) -> float:
        return self.t_end - self._now()

    def can_start(self) -> bool:
        return self.remaining() >= CALL_FLOOR_SECONDS

    def timeout(self, ceiling: float) -> float:
        return max(CALL_FLOOR_SECONDS, min(ceiling, self.remaining()))


class _NoBudget(Budget):
    def __init__(self) -> None:
        self._now, self.t_end = time.time, float("inf")


# --- config -----------------------------------------------------------------------


def resolve_waits(launch_cfg: Any) -> Tuple[Dict[str, float], Optional[str]]:
    """``(durations, error)``. ``error`` is a message naming the bad field (the call is
    then refused ``config-invalid``); absent config = the defaults."""
    out = {k: v[0] for k, v in DURATIONS.items()}
    raw = launch_cfg.get(CONFIG_KEY) if isinstance(launch_cfg, dict) else None
    if raw is None:
        return out, None
    if not isinstance(raw, dict):
        return out, f"launch.json '{CONFIG_KEY}' must be an object"
    for key, val in raw.items():
        if key not in DURATIONS:
            return out, f"launch.json '{CONFIG_KEY}.{key}': unknown key (allowed: {sorted(DURATIONS)})"
        _default, lo, hi = DURATIONS[key]
        if isinstance(val, bool) or not isinstance(val, (int, float)) or not math.isfinite(val) \
                or not lo <= float(val) <= hi:
            return out, f"launch.json '{CONFIG_KEY}.{key}' must be a number in [{lo}, {hi}] (got {val!r})"
        out[key] = float(val)
    return out, None


def safe_token(value: Any, limit: int = 60) -> str:
    """A printable, single-line, [A-Za-z0-9._-] rendering of untrusted text."""
    return _SAFE_TOKEN_RE.sub("_", str(value))[:limit]


def _one_line(value: Any, limit: int) -> str:
    text = "".join(ch if (ch.isprintable() and ch not in "\r\n\t") else " " for ch in str(value))
    return " ".join(text.split())[:limit]


# --- the ONLY process actions ------------------------------------------------------


def local_argv(entry: SR.ServiceEntry, uid: int) -> List[str]:
    """``launchctl kickstart -k gui/<uid>/<label>`` for a LOCAL launchd entry."""
    if not entry.is_local or entry.mechanism_kind != SR.MECH_LAUNCHD:
        raise ValueError("local_argv: not a local launchd entry")
    if SR.LAUNCHD_LABEL_RE.match(entry.mechanism_name) is None or not isinstance(uid, int) or uid < 0:
        raise ValueError("local_argv: label or uid failed validation")
    return [LAUNCHCTL, "kickstart", "-k", f"gui/{uid}/{entry.mechanism_name}"]


def remote_command(entry: SR.ServiceEntry) -> str:
    """The FIXED remote command for the entry's mechanism (label / unit regex-checked)."""
    if entry.mechanism_kind == SR.MECH_LAUNCHD:
        if SR.LAUNCHD_LABEL_RE.match(entry.mechanism_name) is None:
            raise ValueError("remote_command: launchd label failed validation")
        return f"launchctl kickstart -k gui/$(id -u)/{entry.mechanism_name}"
    if entry.mechanism_kind == SR.MECH_SYSTEMD:
        if SR.SYSTEMD_UNIT_RE.match(entry.mechanism_name) is None:
            raise ValueError("remote_command: systemd unit failed validation")
        return f"sudo -n systemctl restart {entry.mechanism_name}"
    raise ValueError("remote_command: unknown mechanism")


def remote_argv(entry: SR.ServiceEntry, connect_timeout: float) -> List[str]:
    """``ssh -o BatchMode=yes -o ConnectTimeout=<n> -- <host> <fixed command>``."""
    if entry.is_local or not SR.is_hostname(entry.host) or entry.host.startswith("-"):
        raise ValueError("remote_argv: host failed validation")
    n = int(max(DURATIONS["ssh_connect_timeout_seconds"][1],
                min(float(connect_timeout), DURATIONS["ssh_connect_timeout_seconds"][2])))
    return [SSH, "-o", "BatchMode=yes", "-o", f"ConnectTimeout={n}", "--", entry.host,
            remote_command(entry)]


def restart_argv(entry: SR.ServiceEntry, uid: int, connect_timeout: float) -> List[str]:
    return local_argv(entry, uid) if entry.is_local else remote_argv(entry, connect_timeout)


def _run_argv(argv: List[str], timeout: float) -> Tuple[int, str, str]:
    """THE single ``subprocess.run`` call site of this module (no shell, no stdin)."""
    try:
        proc = subprocess.run(argv, shell=False, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RunnerTimeout(f"no result within {timeout:.0f}s") from None
    return proc.returncode, proc.stdout or "", proc.stderr or ""


# --- seams ---------------------------------------------------------------------------


def pid_alive(pid: Any) -> bool:
    """Liveness of a roster entry's process, via the roster's own probe
    (``harness.operator_activity.pid_alive``: signal-0 semantics, no signal is sent from this module).
    ``False`` only when the pid is a valid int that provably does not exist. A missing or invalid pid is
    NOT proof of death, so it counts as alive (the safe, disruptive side)."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return True
    from .harness.operator_activity import pid_alive as _probe

    return _probe(pid)


@dataclass
class Deps:
    """Every environmental probe, injectable. ``default_deps`` builds the real ones."""

    runner: Callable[[List[str], float], Tuple[int, str, str]] = _run_argv
    now: Callable[[], float] = time.time
    sleep: Callable[[float], None] = time.sleep
    uid: int = field(default_factory=os.getuid)
    #: -> [{"instance_id", "session_dir", "pid", "line_state", "last_keystroke_ts"}]
    list_sessions: Callable[[], List[Dict[str, Any]]] = lambda: []
    #: (instance_id, timeout=) -> True | False | None (None = could not tell)
    hub_registered: Callable[..., Optional[bool]] = lambda iid, timeout=None: None
    #: (url, timeout) -> (status | None, parsed-json | None, error | None)
    http_get: Callable[[str, float], Tuple[Optional[int], Optional[dict], Optional[str]]] = \
        lambda url, timeout: (None, None, "no http_get configured")
    #: () -> (RendezvousInfo | None, reason | None)
    read_rendezvous: Callable[[], Tuple[Any, Optional[str]]] = lambda: (None, "no reader configured")
    #: (session_dir, now) -> ("idle"|"busy"|"unknown", detail)
    turn_state: Callable[[str, float], Tuple[str, str]] = lambda sd, now: ("unknown", "no probe")
    #: (session_dir, now) -> ("clear"|"inflight"|"unknown", [details]): live background subagent work,
    #: the SAME signal gnomon-relaunch's `subagents` rail uses (relaunch.subagent_state)
    subagent_state: Callable[[str, float], Tuple[str, List[str]]] = lambda sd, now: ("unknown", [])
    #: (instance_id, one-line text, timeout=) -> {"ok": bool, "error": str?}
    send_notice: Callable[..., Dict[str, Any]] = lambda iid, text, timeout=None: {"ok": False, "error": "no sender"}
    #: one provenance record (count-only fields) -> None
    provenance: Callable[[Dict[str, Any]], None] = lambda rec: None
    #: web port for the hub-hosting service's default health URL
    web_port: int = DEFAULT_WEB_PORT
    #: pid -> bool: is the process alive (False only when provably dead). Used to tell a stale roster
    #: entry (disconnected AND no live process) from a live peer.
    pid_alive: Callable[[Any], bool] = pid_alive


def web_port_from_env(env: Optional[Dict[str, str]] = None) -> int:
    env = os.environ if env is None else env
    try:
        port = int(env.get("GNOMON_WEB_PORT", str(DEFAULT_WEB_PORT)))
    except ValueError:
        return DEFAULT_WEB_PORT
    return port if 1 <= port <= 65535 else DEFAULT_WEB_PORT


def http_get_json(url: str, timeout: float) -> Tuple[Optional[int], Optional[dict], Optional[str]]:
    """Read-only GET; loopback/registry URLs only (the caller validated the URL)."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 - validated url
            status = resp.status
            raw = resp.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001 - classified so the hub-state axis can tell down from slow
        reason = getattr(exc, "reason", exc)
        if isinstance(reason, ConnectionRefusedError) or isinstance(exc, ConnectionRefusedError):
            return None, None, f"refused: {type(exc).__name__}: {exc}"      # nothing is listening
        if isinstance(reason, TimeoutError) or isinstance(exc, TimeoutError):
            return None, None, f"timeout: {type(exc).__name__}: {exc}"
        return None, None, f"{type(exc).__name__}: {exc}"
    try:
        body = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return status, None, "non-JSON body"
    return status, body if isinstance(body, dict) else None, None


def default_deps(session_dir: str, config: Any = None) -> Deps:
    """The real probes. ``config`` is the machine :class:`GnomonConfig` (loaded when
    omitted). Reads (never writes) the hub run dir, session beacons and session dirs."""
    if config is None:
        from .config import ConfigStore

        config = ConfigStore().load()
    from .hub.rendezvous import is_loopback_http_url, read_rendezvous, rendezvous_path

    rv_path = rendezvous_path(config.runtime_root)
    token_file = os.path.join(os.path.expanduser(config.runtime_root), "hub", "operator.token")

    def _rendezvous() -> Tuple[Any, Optional[str]]:
        return read_rendezvous(rv_path)

    def _mcp_url() -> Optional[str]:
        info, _why = _rendezvous()
        url = getattr(info, "mcp_url", None)
        return url if is_loopback_http_url(url) else None

    def _hub_call(tool: str, arguments: Dict[str, Any], timeout: float = CALL_TIMEOUT_SECONDS
                  ) -> Tuple[Optional[dict], Optional[str]]:
        url = _mcp_url()
        if not url:
            return None, "no hub mcp_url (rendezvous absent or refused)"
        try:
            with open(token_file, "r", encoding="utf-8") as fh:
                token = fh.read().strip()
        except OSError as exc:
            return None, f"operator token unreadable: {exc}"
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": tool, "arguments": arguments}}).encode()
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - loopback validated
                reply = json.loads(resp.read().decode())
            return json.loads(reply["result"]["content"][0]["text"]), None
        except Exception as exc:  # noqa: BLE001
            return None, f"{type(exc).__name__}: {exc}"

    def _registered(iid: str, timeout: Optional[float] = None) -> Optional[bool]:
        inner, _err = _hub_call("context", {"instance_id": iid}, timeout or CALL_TIMEOUT_SECONDS)
        if inner is None:
            return None
        if inner.get("ok"):
            return True
        return False if "no registered session" in str(inner.get("error", "")) else None

    def _notice(iid: str, text: str, timeout: Optional[float] = None) -> Dict[str, Any]:
        inner, err = _hub_call("inject", {"instance_id": iid, "text": text, "clear": False},
                               timeout or CALL_TIMEOUT_SECONDS)
        if inner is None:
            return {"ok": False, "error": err}
        return {"ok": bool(inner.get("ok")), "error": inner.get("error")}

    def _sessions() -> List[Dict[str, Any]]:
        found: Dict[str, Dict[str, Any]] = {}
        try:
            from .harness.operator_activity import read_beacons

            beacons, _diag = read_beacons()
            for rec in (beacons or {}).values():
                iid = rec.get("instance_id")
                if rec.get("session_dir"):       # F-R3-5: a session with no id is KEPT (unknown-state peer)
                    found[iid or "\0" + rec["session_dir"]] = {"instance_id": iid or None,
                                  "session_dir": rec["session_dir"],
                                  "pid": rec.get("pid"), "line_state": rec.get("line_state"),
                                  "last_keystroke_ts": rec.get("last_keystroke_ts"),
                                  "source": "beacon"}
        except Exception as exc:  # noqa: BLE001 - reported by the caller as an empty source
            sys.stderr.write(f"gnomon restart_service: beacon read failed: {exc}\n")
        try:
            from .liveness import LivenessProbe

            for st in LivenessProbe().scan(config.sessions_root()):
                if st.live and (st.instance_id or st.session_dir) and \
                        (st.instance_id or "\0" + st.session_dir) not in found:
                    found[st.instance_id or "\0" + st.session_dir] = {"instance_id": st.instance_id or None,
                                             "session_dir": st.session_dir, "pid": None,
                                             "line_state": None, "last_keystroke_ts": None,
                                             "last_activity_ts": _last_activity_ts(st),
                                             "process_found": st.process_found,
                                             "source": "liveness"}
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(f"gnomon restart_service: liveness scan failed: {exc}\n")
        return list(found.values())

    def _turn(sd: str, now: float) -> Tuple[str, str]:
        from . import relaunch

        return relaunch.turn_state(relaunch.transcript_path(sd), now)

    def _subagents(sd: str, now: float) -> Tuple[str, List[str]]:
        from . import relaunch

        verdict, inflight, _notes = relaunch.subagent_state(relaunch.transcript_path(sd), now)
        return verdict, list(inflight)

    def _provenance(rec: Dict[str, Any]) -> None:
        from .harness.hooks import append_record, injection_log_path

        append_record(injection_log_path(session_dir), {"kind": PROVENANCE_KIND, **rec})

    return Deps(list_sessions=_sessions, hub_registered=_registered, http_get=http_get_json,
                read_rendezvous=_rendezvous, turn_state=_turn, subagent_state=_subagents, send_notice=_notice,
                provenance=_provenance, web_port=web_port_from_env())


# --- preflight ----------------------------------------------------------------------------


def _last_activity_ts(st: Any) -> Optional[float]:
    """Newest activity evidence of a liveness-scan session: the canary, the transcript and the files
    directly under the session dir and its ``.claude`` dir. ``None`` when nothing can be read (missing
    evidence is never proof of death)."""
    stamps: List[float] = [st.transcript_mtime] if isinstance(getattr(st, "transcript_mtime", None),
                                                               (int, float)) else []
    sd = st.session_dir
    for d in (sd, os.path.join(sd, ".claude"), os.path.join(sd, "logs")):
        try:
            stamps.append(os.path.getmtime(d))
            for name in os.listdir(d):
                try:
                    stamps.append(os.path.getmtime(os.path.join(d, name)))
                except OSError:
                    pass
        except OSError:
            pass
    return max(stamps) if stamps else None


def _line_of(rec: Dict[str, Any], now: float) -> Tuple[str, str]:
    """``(empty|held|undetectable, detail)`` from a session's presence-beacon fields, using
    the SHARED D44 rule (``harness.line_hold.line_hold_reason``)."""
    from .harness.line_hold import DEFAULT_UNKNOWN_AFTER_SECONDS, line_hold_reason

    state = rec.get("line_state")
    if state is None:
        return "undetectable", "no presence beacon: the operator's unsent line cannot be seen"
    last = rec.get("last_keystroke_ts")
    silent = now - last if isinstance(last, (int, float)) else float("inf")
    reason = line_hold_reason(state, silent, DEFAULT_UNKNOWN_AFTER_SECONDS)
    return ("held", reason) if reason else ("empty", f"line_state={state}")


def _stale_evidence(r: Dict[str, Any], now: float, deps: Deps) -> Optional[str]:
    """Why a DISCONNECTED row is provably a dead ghost, else ``None``. (1) a recorded pid that is dead;
    (2) no pid ever recorded and the newest activity evidence is older than ``STALE_NO_PID_SECONDS``.
    Missing evidence, or recent evidence, is never proof."""
    pid = r.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 0:
        return "recorded pid is dead" if not deps.pid_alive(pid) else None
    ts = r.get("last_activity_ts")
    if isinstance(ts, (int, float)) and not isinstance(ts, bool) and now - ts > STALE_NO_PID_SECONDS:
        return f"no pid recorded and no activity for {(now - ts) / 3600:.0f}h"
    return None


def preflight(caller: str, entry: SR.ServiceEntry, deps: Deps, bud: Optional[Budget] = None) -> Dict[str, Any]:
    """D52 preflight. ``classification`` is ``silent`` or ``disruptive``; ``reasons`` says why.

    F-R2-1: the PEER CHECK IS DECOUPLED FROM THE HUB. The candidates are the live sessions from
    hub-INDEPENDENT sources (presence beacons + the liveness scan). For EVERY live candidate other than
    the caller, turn state (``relaunch.turn_state`` on its own transcript) and operator-line state
    (its beacon) are read DIRECTLY, whatever the hub thinks. The restart is DISRUPTIVE iff any such peer
    is mid-turn, has a partial (or recently-unknown) operator line, or has an unknown turn or line state.

    The hub is a SEPARATE axis, ``hub_state``:
    ``down``      connection REFUSED on the health port (nothing listening);
    ``degraded``  a timeout, a 5xx or other non-200 answer, or health OK with an EMPTY registry while
                  live sessions exist: DISRUPTIVE by itself (coordinate first);
    ``up``        health 200 and the registry is not empty.
    ``down`` does not by itself make the restart silent: if the roster is clean the result is
    ``silent`` with ``basis: hub-down`` ("hub-down: silent-by-necessity"); a mid-turn peer still makes
    it disruptive (its in-flight turn may be using hub verbs). Hub REGISTRATION only decides WHO is
    notified and re-verified (and when the hub is not ``up`` the registry is unreliable, so every live
    session is expected to re-register); it never decides silent vs disruptive.

    Non-hub service: nothing here can see who depends on it, so it is ``disruptive`` by
    construction (coordinate first, D52): the caller must pass ``coordinated_with``.

    Every network call is bounded by the total budget (F-R2-2); a call that cannot start counts as
    unknown."""
    bud = bud or _NoBudget()
    now = deps.now()
    if not entry.hosts_hub:
        return {"applicable": False, "classification": "disruptive",
                "reasons": [f"{entry.service_id!r} does not host the Gnomon hub: the harness preflight "
                            "cannot see who depends on it, so it is treated as disruptive. Coordinate "
                            "with whoever uses it first (D52) and pass coordinated=true and "
                            "coordinated_with."],
                "harnesses": [], "expect_reregister": [], "affected": [], "stale": [], "caller": caller,
                "hub_down": False, "hub_state": None, "basis": "non-hub",
                "counts": {"connected": 0, "mid_turn": 0, "partial_line": 0, "unknown_registration": 0,
                           "unknown_state": 0, "no_instance_id": 0, "stale": 0}}
    rows: List[Dict[str, Any]] = []
    for rec in sorted(deps.list_sessions(), key=lambda r: str(r.get("instance_id"))):
        iid, sd = rec.get("instance_id"), rec.get("session_dir")
        unidentified = not isinstance(iid, str) or not iid
        if unidentified:     # F-R3-5: never skipped silently: listed, read directly, counted unknown-state
            iid = "<no-instance-id:" + (os.path.basename(str(sd).rstrip("/")) if isinstance(sd, str) and sd
                                        else "unknown-dir") + ">"
        if not isinstance(sd, str) or not sd:
            sd = ""
        row: Dict[str, Any] = {"instance_id": iid, "connected": None, "is_caller": iid == caller,
                               "unidentified": unidentified, "subagents": None, "pid": rec.get("pid"),
                               "last_activity_ts": rec.get("last_activity_ts"), "stale": False,
                               "turn": None, "turn_detail": None, "line": None, "line_detail": None}
        try:  # hub-INDEPENDENT reads, for every live candidate
            row["turn"], detail = deps.turn_state(sd, now)
            row["turn_detail"] = _one_line(detail, 160)
        except Exception as exc:  # noqa: BLE001 - reported, and unknown counts as disruptive
            row["turn"], row["turn_detail"] = "unknown", f"probe failed: {type(exc).__name__}"
        row["line"], detail = _line_of(rec, now)
        row["line_detail"] = _one_line(detail, 160)
        try:  # the gnomon-relaunch `subagents` rail: an idle main turn can still hide live background work
            verdict, inflight = deps.subagent_state(sd, now)
            row["subagents"] = verdict
            row["subagent_detail"] = _one_line("; ".join(inflight), 160)
        except Exception:  # noqa: BLE001 - undetectable is reported, not blocking (same as the rail)
            row["subagents"], row["subagent_detail"] = "unknown", "probe failed"
        rows.append(row)

    # -- the hub axis (its own probes; registration answers only feed notices / re-registration)
    url = _health_url(entry, deps)
    status, body, herr = None, None, "timeout: no budget left to probe"
    if url and bud.can_start():
        status, body, herr = deps.http_get(url, bud.timeout(HEALTH_TIMEOUT_SECONDS))
    for r in rows:
        if r["unidentified"]:
            continue                                   # no id to ask the hub about
        r["connected"] = (deps.hub_registered(r["instance_id"], timeout=bud.timeout(CALL_TIMEOUT_SECONDS))
                          if bud.can_start() else None)
        # X30 fix: a DISCONNECTED harness whose process is provably dead is a stale roster entry, not a
        # peer. A connected (or unknown-registration) peer is never stale; the caller never is.
        if r["connected"] is False and not r["is_caller"]:
            r["stale"] = _stale_evidence(r, now, deps) is not None
    if status == 200 and isinstance(body, dict) and body.get("ok") is True:
        hub_state = "up"
        others = [r for r in rows if not r["is_caller"] and not r["unidentified"] and not r["stale"]]
        if others and all(r["connected"] is False for r in others):   # F-R3-4: a lone caller proves nothing
            hub_state = "degraded"
    elif status is None and isinstance(herr, str) and herr.startswith("refused"):
        hub_state = "down"
    else:
        hub_state = "degraded"

    reasons: List[str] = []
    mid_turn = partial = unknown_state = no_id = 0
    for r in rows:
        if r["stale"]:
            continue                                   # reported separately in `stale`
        who = r["instance_id"]
        if r["is_caller"]:
            # X30 fix: the caller's own HELD operator line counts (the operator may be typing into it and a
            # restart can lose it). Its busy turn / subagents / unknown states never count: it is always
            # mid-turn when it calls the verb.
            if r["line"] == "held":
                partial += 1
                reasons.append(f"{who} (the CALLER's own session) has an operator line in progress "
                               f"({r['line_detail']}): a restart can lose it")
            continue
        if r["unidentified"]:
            no_id += 1
            reasons.append(f"a live session without an instance_id ({who}) cannot be identified or notified: "
                           "counted as an unknown-state peer")
        if r["subagents"] == "inflight":
            mid_turn += 1
            reasons.append(f"{who} has live background subagent work ({r.get('subagent_detail') or 'in flight'})")
        elif r["subagents"] != "clear":      # unknown (or a failed probe) is disruptive, like every unknown state (g33)
            unknown_state += 1
            reasons.append(f"{who}: subagent-state-unknown ({r.get('subagent_detail') or 'no verdict'}): "
                           "assuming live background work")
        if r["turn"] == "busy":
            mid_turn += 1
            reasons.append(f"{who} is mid-turn ({r['turn_detail']})")
        elif r["turn"] != "idle":
            unknown_state += 1
            reasons.append(f"{who}: turn state unknown ({r['turn_detail']}): assuming mid-turn")
        if r["line"] == "held":
            partial += 1
            reasons.append(f"{who} has an operator line in progress ({r['line_detail']})")
        elif r["line"] != "empty":
            unknown_state += 1
            reasons.append(f"{who}: operator line state unknown ({r['line_detail']}): assuming a line in progress")
    if hub_state == "degraded":
        why = ("health answered ok but its registry is empty while live sessions exist"
               if status == 200 else f"health probe: {status if status is not None else herr}")
        reasons.append(f"the hub is degraded ({why}): registration cannot be trusted, coordinate first")
    hub_down = hub_state == "down" and not reasons
    if hub_down:
        reasons.append("hub-down: silent-by-necessity: nothing is listening on the health port and every "
                       "live peer is idle with an empty operator line; every live session is expected to "
                       "re-register after the restart")
    elif hub_state == "down":
        reasons.append("the hub is down (connection refused), but the restart is still disruptive because of "
                       "the peer state above")
    ided = [r for r in rows if not r["unidentified"] and not r["stale"]]
    stale = [{"instance_id": r["instance_id"], "pid": r["pid"], "connected": r["connected"],
              "why": _stale_evidence(r, now, deps)}
             for r in rows if r["stale"]]
    if hub_state != "up":
        expect = [r["instance_id"] for r in ided]                # registry unreliable: everyone live re-registers
    else:
        expect = [r["instance_id"] for r in ided if r["connected"] is not False]
    affected = [i for i in expect if i != caller]
    return {"applicable": True, "classification": "silent" if hub_down or not reasons else "disruptive",
            "hub_state": hub_state, "hub_down": hub_down, "basis": "hub-down" if hub_down else "roster",
            "reasons": reasons, "harnesses": rows, "expect_reregister": expect, "affected": affected,
            "stale": stale, "caller": caller,
            "counts": {"connected": sum(1 for r in rows if r["connected"] is True),
                       "mid_turn": mid_turn, "partial_line": partial, "unknown_state": unknown_state,
                       "no_instance_id": no_id, "stale": len(stale),
                       "unknown_registration": sum(1 for r in ided if r["connected"] is None)}}


# --- verify --------------------------------------------------------------------------------


def _health_url(entry: SR.ServiceEntry, deps: Deps) -> Optional[str]:
    if entry.health_url:
        return entry.health_url
    if entry.hosts_hub:
        return f"http://127.0.0.1:{deps.web_port}/api/health"
    return None


def _check(name: str, status: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"name": name, "status": status, "detail": detail, **extra}


def verify(entry: SR.ServiceEntry, expect: List[str], rv_before: Any, deps: Deps,
           waits: Dict[str, float], t_end: Optional[float] = None) -> List[Dict[str, Any]]:
    """Bounded post-restart checks. Every failure names exactly what failed. ``t_end`` is the TOTAL
    budget's deadline: a check the budget cut short (or never reached) is ``incomplete``, never
    ``fail`` or ``pass``."""
    checks: List[Dict[str, Any]] = []
    t_end = float("inf") if t_end is None else t_end
    bud = Budget(deps.now, t_end)

    def spent() -> bool:
        return deps.now() >= t_end

    def cut_note(own: float) -> str:
        return f"the total budget ({waits['total_budget_seconds']:.0f}s) ran out (the check's own wait was {own:.0f}s)"

    url = _health_url(entry, deps)
    health_body: Optional[dict] = None
    health_incomplete = False
    if url is None:
        checks.append(_check("health", "unverified",
                             "no health_url is registered for this service: the restart command "
                             "exited 0 and that is ALL this verb knows"))
        if not entry.hosts_hub:
            return checks
    else:
        own = waits["health_wait_seconds"]
        step_end = deps.now() + own
        limited = t_end < step_end
        deadline = min(step_end, t_end)
        last = "no attempt"
        while True:
            if not bud.can_start():                    # F-R2-2: no new call under the per-call floor
                last = "total budget spent before the next probe could start"
                break
            status, body, err = deps.http_get(url, bud.timeout(HEALTH_TIMEOUT_SECONDS))
            if status == 200 and isinstance(body, dict) and body.get("ok") is True:
                health_body = body
                break
            last = err or f"HTTP {status}, body ok={None if body is None else body.get('ok')}"
            if deps.now() >= deadline:
                break
            deps.sleep(POLL_SECONDS)
        if health_body is not None:
            checks.append(_check("health", "pass", f"{url} -> 200 ok"))
        elif limited or bud.remaining() < CALL_FLOOR_SECONDS:
            health_incomplete = True
            checks.append(_check("health", "incomplete", f"{url} had not reported ok when {cut_note(own)} (last: {last})"))
        else:
            checks.append(_check("health", "fail", f"{url} did not report ok within {own:.0f}s (last: {last})"))
        if not entry.hosts_hub:
            return checks

    # -- hub-hosting service only: build id, rendezvous, re-registration
    build_id = health_body.get("build_id") if isinstance(health_body, dict) else None
    if health_body is None:
        checks.append(_check("build-id", "incomplete" if health_incomplete else "skipped",
                             "health did not complete (total budget)" if health_incomplete else "health did not pass"))
    elif isinstance(build_id, str) and build_id.strip():
        checks.append(_check("build-id", "pass", f"the running server reports build {safe_token(build_id, 40)}",
                             build_id=safe_token(build_id, 40)))
    else:
        checks.append(_check("build-id", "fail", "/api/health reported no build id (build_id is null or empty)"))

    if spent():
        checks.append(_check("rendezvous", "incomplete", f"not run: {cut_note(waits['health_wait_seconds'])}"))
        checks.append(_check("reregistration", "incomplete", f"not run: {cut_note(waits['reregister_wait_seconds'])}",
                             missing=sorted(expect), expected=len(expect)))
        return checks
    own = waits["health_wait_seconds"]
    step_end = deps.now() + own
    limited = t_end < step_end
    deadline = min(step_end, t_end)
    before_id = getattr(rv_before, "identity", None)
    info, why = None, "not read"
    while True:
        info, why = deps.read_rendezvous()
        if info is not None and (before_id is None or info.identity != before_id):
            break
        if deps.now() >= deadline:
            break
        deps.sleep(POLL_SECONDS)
    republished = info is not None and (before_id is None or info.identity != before_id)
    if not republished and limited:
        checks.append(_check("rendezvous", "incomplete", f"not yet republished when {cut_note(own)} ({why})"))
    elif info is None:
        checks.append(_check("rendezvous", "fail", f"rendezvous.json not republished ({why})"))
    elif before_id is not None and info.identity == before_id:
        checks.append(_check("rendezvous", "fail",
                             "rendezvous.json still names the PRE-restart hub (same port and hub_instance_id): "
                             "the new hub has not published"))
    else:
        hub_url = health_body.get("hub_url") if isinstance(health_body, dict) else None
        if hub_url and info.mcp_url and hub_url != info.mcp_url:
            checks.append(_check("rendezvous", "fail",
                                 "rendezvous.json mcp_url does not match /api/health hub_url: it was written "
                                 "by a different hub than the one serving health"))
        else:
            checks.append(_check("rendezvous", "pass",
                                 f"republished: hub_instance_id {safe_token(info.hub_instance_id, 12)}..., "
                                 f"port {info.port}"))

    if health_body is None:
        checks.append(_check("reregistration", "incomplete" if health_incomplete else "skipped",
                             "health did not complete (total budget)" if health_incomplete
                             else "health did not pass; not waiting on harnesses"))
        return checks
    if spent():
        checks.append(_check("reregistration", "incomplete", f"not run: {cut_note(waits['reregister_wait_seconds'])}",
                             missing=sorted(expect), expected=len(expect)))
        return checks
    own = waits["reregister_wait_seconds"]
    step_end = deps.now() + own
    limited = t_end < step_end
    deadline = min(step_end, t_end)
    pending = list(expect)
    while pending:
        still: List[str] = []
        for iid in pending:                              # a round stops starting calls once the budget is spent
            if not bud.can_start() or deps.now() >= deadline:
                still.append(iid)
            elif deps.hub_registered(iid, timeout=bud.timeout(CALL_TIMEOUT_SECONDS)) is not True:
                still.append(iid)
        pending = still
        if not pending or deps.now() >= deadline or not bud.can_start():
            break
        deps.sleep(POLL_SECONDS)
    if pending and (limited or not bud.can_start()):
        checks.append(_check("reregistration", "incomplete",
                             f"{len(pending)} of {len(expect)} previously connected harness(es) had not "
                             f"re-registered when {cut_note(own)}: " + ", ".join(sorted(pending)),
                             missing=sorted(pending), expected=len(expect)))
    elif pending:
        checks.append(_check("reregistration", "fail",
                             f"{len(pending)} of {len(expect)} previously connected harness(es) did NOT "
                             f"re-register within {own:.0f}s: " + ", ".join(sorted(pending)),
                             missing=sorted(pending), expected=len(expect)))
    else:
        checks.append(_check("reregistration", "pass",
                             f"all {len(expect)} previously connected harness(es) re-registered",
                             expected=len(expect)))
    return checks


# --- notify --------------------------------------------------------------------------------


def notice_text(*, caller: str, service: str, mode: str, ok: bool, build_id: Optional[str],
                reregistered: int, expected: int, failed_checks: List[str], missing: List[str],
                act_failed: bool = False, incomplete: Optional[List[str]] = None) -> str:
    """The ONE-LINE harness notice (printable, no leading ``/`` or ``\\``, <= 400 chars)."""
    who, svc = safe_token(caller), safe_token(service)
    if act_failed:
        text = (f"[GNOMON NOTICE] {who} tried to restart {svc} ({mode}) and the restart command FAILED; "
                f"nothing is expected to have changed. If your hub link looks dead, tell {who}.")
    elif ok:
        text = (f"[GNOMON NOTICE] {who} restarted {svc} ({mode}). Health OK, build "
                f"{safe_token(build_id or 'unknown', 40)}, {reregistered}/{expected} harnesses "
                f"re-registered. No action needed.")
    elif incomplete and not failed_checks:
        text = (f"[GNOMON NOTICE] {who} restarted {svc} ({mode}) but verification did NOT COMPLETE within its "
                f"time budget ({', '.join(safe_token(c, 30) for c in incomplete)}). Your hub link may not be "
                f"back yet; {who} is checking.")
    else:
        miss = f"; not re-registered: {', '.join(safe_token(m) for m in missing[:8])}" if missing else ""
        text = (f"[GNOMON NOTICE] {who} restarted {svc} ({mode}) but VERIFICATION FAILED "
                f"({', '.join(safe_token(c, 30) for c in failed_checks)}){miss}. Your hub link may be "
                f"down; {who} is investigating.")
    return _one_line(text, 400)


def notify(affected: List[str], text: str, deps: Deps, wait_seconds: float,
           t_end: Optional[float] = None) -> Dict[str, Any]:
    """Deliver ``text`` to each affected session through the hub's existing inject path.

    D44 line-hold (g27a): a session whose operator line is partial (or unknown and recently
    active) is HELD, re-checked every poll for at most ``wait_seconds`` (a bounded wait, g27b),
    then reported as ``held``, never typed onto the line. The line state is RE-READ every round
    (a stale snapshot would hold, or worse release, on old data)."""
    from .harness.compact_gate import screen_hub_inject

    result: Dict[str, Any] = {"delivered": [], "failed": [], "held": [], "unsent": [], "text_chars": len(text)}
    reasons = screen_hub_inject(text)
    if reasons:  # a bug in the template: loud, and nothing is sent
        result["failed"] = [{"instance_id": iid, "error": f"notice text refused by the hub screen: {reasons}"}
                            for iid in affected]
        return result
    pending = list(affected)
    deadline = deps.now() + max(0.0, wait_seconds)
    bud = Budget(deps.now, t_end) if t_end is not None else _NoBudget()
    if t_end is not None:
        deadline = min(deadline, t_end)
    while pending:
        still: List[str] = []
        try:
            sessions = {x["instance_id"]: x for x in deps.list_sessions() if isinstance(x.get("instance_id"), str)}
        except Exception:  # noqa: BLE001 - unreadable beacons: undetectable, not blocking
            sessions = {}
        for iid in pending:
            line, _detail = _line_of(sessions.get(iid, {}), deps.now())
            if line == "held":
                still.append(iid)
                continue
            if not bud.can_start():                     # F-R2-2: the final pass is bounded too
                result["unsent"].append(iid)
                continue
            try:
                res = deps.send_notice(iid, text, timeout=bud.timeout(CALL_TIMEOUT_SECONDS))
            except Exception as exc:  # noqa: BLE001
                res = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if res.get("ok"):
                result["delivered"].append(iid)
            else:
                result["failed"].append({"instance_id": iid, "error": _one_line(res.get("error") or "unknown", 200)})
        pending = still
        if not pending or deps.now() >= deadline:
            break
        deps.sleep(POLL_SECONDS)
    result["held"] = pending
    return result


# --- the verb --------------------------------------------------------------------------------


def _record(deps: Deps, caller: Optional[str], **fields: Any) -> None:
    rec = {"source": PROVENANCE_SOURCE, "trigger": PROVENANCE_TRIGGER, "source_harness": caller,
           **fields}
    try:
        deps.provenance(rec)
    except Exception as exc:  # noqa: BLE001 - loud, never blocks the verb
        sys.stderr.write(f"gnomon restart_service: provenance write FAILED: {exc}\n")


def _refuse(deps: Deps, caller: Optional[str], reason: str, message: str, **extra: Any) -> Dict[str, Any]:
    _record(deps, caller, decision="refused", reason=reason, service=extra.get("service_for_log"))
    out = {"ok": False, "decision": "refused", "reason": reason, "error": message}
    out.update({k: v for k, v in extra.items() if k != "service_for_log"})
    return out


def run(**kw: Any) -> Dict[str, Any]:
    """The whole verb; it NEVER raises. A dependency that raises (an unreadable beacon directory, a
    broken probe) becomes a LOUD ``internal-error`` refusal with a provenance record: before the act it
    says nothing was restarted; after the act it says the restart command may have run and to verify by
    hand (``acted`` tells which). Arguments as :func:`_run`."""
    progress: Dict[str, bool] = {"acted": False}
    try:
        return _run(_progress=progress, **kw)
    except Exception as exc:  # noqa: BLE001 - the contract: never raise
        acted = progress["acted"]
        msg = (f"restart_service INTERNAL ERROR ({type(exc).__name__}: {exc}). " + (
            "The restart command MAY HAVE RUN before this error: verify the service by hand." if acted
            else "Nothing was restarted."))
        deps = kw.get("deps")
        try:
            if deps is not None:
                _record(deps, kw.get("caller"), decision="failed" if acted else "refused", reason="internal-error",
                        verb=TOOL_NAME, acted=acted, error_type=type(exc).__name__)
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "decision": "failed" if acted else "refused", "reason": "internal-error",
                "acted": acted, "error": msg}


def _run(*, caller: Optional[str], service: Any, coordinated: Any = False, coordinated_with: Any = "",
         ack: Any = None, deps: Deps, registry: SR.Registry, policy_ctx: VP.Ctx, flag_on: bool,
         waits: Dict[str, float], config_error: Optional[str] = None,
         _progress: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """The whole verb, unwrapped. ``caller`` is the harness identity resolved from ``launch_context``
    (never an argument). Never raises; every refusal and failure is explicit and specific."""
    svc_log = service if isinstance(service, str) and SR.SERVICE_ID_RE.match(service) else None
    if not isinstance(caller, str) or not caller:
        return _refuse(deps, None, "identity-unresolved",
                       "restart_service REFUSED: the caller's harness identity could not be resolved",
                       service_for_log=svc_log)
    if not flag_on:
        return _refuse(deps, caller, "flag-off",
                       f"restart_service REFUSED: feature flag {FLAG_NAME} is OFF for this session",
                       service_for_log=svc_log)
    if config_error:
        return _refuse(deps, caller, "config-invalid",
                       f"restart_service REFUSED (config-invalid): {config_error}", service_for_log=svc_log)
    if registry.disabled:
        return _refuse(deps, caller, "registry-disabled",
                       f"restart_service REFUSED: the service registry is unusable ({registry.disabled_reason})",
                       errors=list(registry.errors), service_for_log=svc_log)
    if policy_ctx.policy.invalid:
        return _refuse(deps, caller, "policy-invalid",
                       f"restart_service REFUSED: the verb policy is invalid ({policy_ctx.policy.invalid_reason}); "
                       f"the pilot's file is {policy_ctx.policy.override_path or policy_ctx.policy.source_path}",
                       errors=list(policy_ctx.policy.errors), service_for_log=svc_log)
    offered = VP.visible_targets(policy_ctx.policy, TOOL_NAME, caller, sorted(registry.entries), policy_ctx.facts)
    if not offered:
        return _refuse(deps, caller, "policy-deny",
                       f"restart_service REFUSED: the verb policy does not permit {safe_token(caller)!r} to "
                       "restart any registered service", service_for_log=svc_log)
    if not isinstance(service, str) or service not in registry.entries:
        return _refuse(deps, caller, "unknown-service",
                       f"restart_service REFUSED: unknown service {_one_line(service, 60)!r}; "
                       f"services you may restart: {offered}", allowed=offered, service_for_log=None)
    if not isinstance(coordinated, bool):
        return _refuse(deps, caller, "invalid-arguments", "restart_service REFUSED: 'coordinated' must be a boolean",
                       service_for_log=service)
    if not isinstance(coordinated_with, str) or len(coordinated_with) > MAX_COORDINATED_WITH:
        return _refuse(deps, caller, "invalid-arguments",
                       f"restart_service REFUSED: 'coordinated_with' must be a string <= {MAX_COORDINATED_WITH} chars",
                       service_for_log=service)
    if ack is not None and not isinstance(ack, str):
        return _refuse(deps, caller, "invalid-arguments", "restart_service REFUSED: 'ack' must be a string",
                       service_for_log=service)
    t_end = deps.now() + waits["total_budget_seconds"]      # the TOTAL wall-clock budget (F-8)
    decision = VP.check(TOOL_NAME, caller, service, ack, policy_ctx, consume=False)
    if decision.outcome == VP.DENY:
        return _refuse(deps, caller, "policy-deny", f"restart_service REFUSED: {decision.message}",
                       allowed=offered, service_for_log=service, policy_reason=decision.reason)
    if decision.outcome == VP.CONFIRM:
        _record(deps, caller, decision="confirm-requested", reason=decision.reason, verb=TOOL_NAME,
                service=service, policy_path=decision.policy_path)
        return {"ok": False, "decision": "confirm-required", "reason": decision.reason, "needs_ack": True,
                "service": service, "confirm_window_seconds": decision.window_seconds,
                "message": decision.message, "error": "NOTHING WAS RESTARTED. " + decision.message}
    confirmed = decision.reason == "confirmed"
    ack_note = decision.ack if confirmed else None
    warned = decision.reason == "warn"
    if warned:  # policy mode `warn`: proceeds, and the engine alerts (provenance + result + alert_to)
        _record(deps, caller, decision="policy-alert", reason="warn", verb=TOOL_NAME, service=service,
                alert_to_count=len(decision.alert_to), policy_path=decision.policy_path)
    entry = registry.entries[service]
    #: the ONLY rendering of ``coordinated_with`` that is logged-by-length or typed into a notice
    who = re.sub(r"[^A-Za-z0-9 ,._+-]", "_", _one_line(coordinated_with, 80)).strip()

    if not _LOCK.acquire(blocking=False):
        return _refuse(deps, caller, "in-progress", "restart_service REFUSED: another restart is already running",
                       service_for_log=service)
    try:
        pf = preflight(caller, entry, deps, Budget(deps.now, t_end))
        left = t_end - deps.now()
        if left < MIN_ACT_BUDGET_SECONDS:
            return _refuse(deps, caller, "budget-exhausted",
                           f"restart_service REFUSED: only {max(0.0, left):.0f}s of the {waits['total_budget_seconds']:.0f}s "
                           f"total budget is left after the preflight, less than the {MIN_ACT_BUDGET_SECONDS:.0f}s needed "
                           "to verify a restart (a restart is never started without time to verify it); "
                           "nothing was restarted", preflight=pf, service_for_log=service)
        # D52: the preflight runs for EVERYONE. An `always` (or allow) caller must still coordinate
        # a disruptive restart; a caller who passed confirm-twice made that statement in the ack.
        if pf["classification"] == "disruptive" and not (coordinated and who) and not confirmed:
            return _refuse(deps, caller, "disruptive-uncoordinated",
                           "restart_service REFUSED: the restart would be DISRUPTIVE (D52) and you have not "
                           "coordinated. " + "; ".join(pf["reasons"]) + ". Coordinate with the affected "
                           "colleague(s) first, then call again with coordinated=true and coordinated_with "
                           "naming who agreed.", preflight=pf, service_for_log=service)
        if pf["classification"] == "silent":
            mode = "hub-down: silent-by-necessity" if pf.get("hub_down") else "silent"
        elif coordinated and who:
            mode = f"coordinated with {who}"
        else:
            mode = "confirmed by the caller's ack: " + re.sub(r"[^A-Za-z0-9 ,._+-]", "_", ack_note or "")[:80]
        rv_before, _why = (deps.read_rendezvous() if entry.hosts_hub else (None, None))
        try:
            argv = restart_argv(entry, deps.uid, waits["ssh_connect_timeout_seconds"])
        except ValueError as exc:  # a registry entry that slipped past validation: refuse, never run
            return _refuse(deps, caller, "argv-invalid", f"restart_service REFUSED: {exc}", preflight=pf,
                           service_for_log=service)
        if confirmed:  # spent only now: validation, lock and budget checks above cannot burn the ack (P3-1)
            VP.consume_confirmation(policy_ctx, caller, TOOL_NAME, service)
        counts = pf["counts"]
        _record(deps, caller, decision="started", hub_down=bool(pf.get("hub_down")), verb=TOOL_NAME, service=service,
                policy_reason=decision.reason, ack=ack_note,
                host_kind="local" if entry.is_local else "remote",
                classification=pf["classification"], coordinated=bool(coordinated and who),
                coordinated_with_chars=len(who), connected=counts["connected"], mid_turn=counts["mid_turn"],
                partial_line=counts["partial_line"], expected_reregister=len(pf["expect_reregister"]))

        act_error: Optional[str] = None
        if _progress is not None:
            _progress["acted"] = True
        try:
            act_budget = (waits["act_timeout_seconds"] if entry.is_local
                          else waits["act_timeout_seconds"] + waits["ssh_connect_timeout_seconds"])
            rc, out, err = deps.runner(argv, max(1.0, min(act_budget, t_end - deps.now())))
            if rc != 0:
                act_error = f"restart command exited {rc}: {_one_line(err or out, 240) or '(no output)'}"
        except RunnerTimeout as exc:
            act_error = f"restart command timed out ({exc})"
        except OSError as exc:
            act_error = f"restart command could not run: {type(exc).__name__}: {exc}"

        checks: List[Dict[str, Any]] = []
        if act_error is None:
            checks = verify(entry, pf["expect_reregister"], rv_before, deps, waits, t_end)
        failed = [c["name"] for c in checks if c["status"] == "fail"]
        incomplete = [c["name"] for c in checks if c["status"] == "incomplete"]
        unverified = [c["name"] for c in checks if c["status"] == "unverified"]
        ok = act_error is None and not failed and not incomplete
        if act_error:
            failed = ["restart-command"]
        missing = next((c.get("missing", []) for c in checks if c["name"] == "reregistration"), [])
        build_id = next((c.get("build_id") for c in checks if c["name"] == "build-id" and c["status"] == "pass"), None)
        rereg = next((c for c in checks if c["name"] == "reregistration"), None)
        n_rereg = (rereg["expected"] - len(rereg.get("missing", []))) if rereg and "expected" in rereg else 0

        notices: Dict[str, Any] = {"applicable": entry.hosts_hub}
        if entry.hosts_hub and pf["affected"]:
            text = notice_text(caller=caller, service=service, mode=mode, ok=ok, build_id=build_id,
                               reregistered=n_rereg, expected=len(pf["expect_reregister"]),
                               failed_checks=failed, missing=missing, act_failed=act_error is not None,
                               incomplete=incomplete)
            notices.update(notify(pf["affected"], text, deps, waits["notice_wait_seconds"], t_end))
        elif not entry.hosts_hub:
            notices["note"] = ("this service does not host the hub, so no harness is auto-notified; "
                               "the coordination you did (coordinated_with) is the notice")
        else:
            notices.update({"delivered": [], "failed": [], "held": [], "unsent": [], "note": "no other harness was connected"})

        alert: Optional[Dict[str, Any]] = None
        if warned and decision.alert_to:
            alert_text = _one_line(f"[GNOMON ALERT] {caller} restarted {service} without being on its allowlist "
                                   f"(verb policy: warn, outcome {'ok' if ok else 'FAILED'}). {decision.message}", 400)
            alert = notify(list(decision.alert_to), alert_text, deps, waits["notice_wait_seconds"], t_end)
        outcome = "restarted" if ok else "failed"
        _record(deps, caller, decision=outcome, verb=TOOL_NAME, service=service, ok=ok,
                policy_reason=decision.reason, ack=ack_note,
                classification=pf["classification"], coordinated=bool(coordinated and who),
                coordinated_with_chars=len(who), connected=counts["connected"],
                mid_turn=counts["mid_turn"], partial_line=counts["partial_line"],
                failed_checks=len(failed), unverified_checks=len(unverified), incomplete_checks=len(incomplete),
                hub_down=bool(pf.get("hub_down")),
                reregistered=n_rereg, missing=len(missing),
                notices_delivered=len(notices.get("delivered", [])), notices_failed=len(notices.get("failed", [])),
                notices_held=len(notices.get("held", [])), notices_unsent=len(notices.get("unsent", [])), act_failed=act_error is not None,
                alerted=len(alert.get("delivered", [])) if alert else 0)
        result: Dict[str, Any] = {
            "ok": ok, "decision": outcome, "service": service, "mode": mode, "preflight": pf,
            "policy": decision.to_dict(),
            "argv_shape": "local-launchd" if entry.is_local else f"ssh-{entry.mechanism_kind}",
            "checks": checks, "failures": failed, "unverified": unverified,
            "verified": bool(checks) and not unverified and not failed and not incomplete,
            "partial": bool(incomplete), "incomplete": incomplete, "notices": notices}
        if warned:
            result["warning"] = decision.message
            result["alert"] = alert if alert is not None else {"alert_to": []}
        if act_error:
            result["error"] = f"restart_service FAILED at the restart command: {act_error}"
        elif failed:
            result["error"] = ("restart_service: the restart ran but VERIFICATION FAILED: "
                               + "; ".join(c["detail"] for c in checks if c["status"] == "fail"))
        elif incomplete:
            result["error"] = (f"restart_service: PARTIAL result: the restart command ran, but the total budget "
                               f"({waits['total_budget_seconds']:.0f}s) ran out and these checks did not complete: "
                               f"{', '.join(incomplete)}. Verify by hand before relying on it.")
        return result
    finally:
        _LOCK.release()

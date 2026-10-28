"""Agent-invokes-agent — the launch-child capability (PTY-hooks WS-G), INTERIM-RAILED.

Launch another agent by profile, either

* **harnessed** — a full keeper-harnessed session (``python -m
  gnomon.harness <session_dir>``, the same inner command the dashboard
  runs), opened in the profile's terminal (iTerm2 / Terminal.app) or, with
  ``terminal="none"``, as a DETACHED background harness (no window; the TUI's
  output goes to ``<session>/.claude/harness_stdout.log``); or
* **headless** — ``claude -p <prompt>`` under a marker wrapper
  (``python -m gnomon.harness.headless``), returning its output.

Callable (a) from a shell — ``python -m gnomon.harness.agent_launch
launch --profile <id> ...`` — so an operator (or an architect agent) can launch a pilot
with NO hub restart, and (b) as the ``launch_agent`` verb on the self-MCP surface
(off by default; only advertised when the launching session is enabled).

Interim safety rails (security framework deferred to the operator — DELTA-3)
--------------------------------------------------------------------
* WHO is launching is decided by PROCESS ANCESTRY (``hub.procinfo``, reused), not
  by environment or files the agent can edit: the nearest ancestor running
  ``-m gnomon.harness <dir>`` (an agent), ``-m
  gnomon.harness.headless`` (a headless agent — which may NEVER launch),
  or none (the operator). An environment that CLAIMS a harnessed launcher
  (``GNOMON_AGENT_DEPTH`` set) with no harness ancestor is REFUSED (fail closed).
* An agent's depth / enabled flag / caps are the values its OWN harness proxy
  holds in memory (read from launch.json at ITS startup, before the agent ran),
  queried over the proxy's private control socket with the SERVER pid verified
  against the ancestor — not re-read from a file the agent could have edited.
* DEPTH CAP: an operator launch creates a depth-0 root; an agent at depth d may
  launch only if ``d + 1 <= max_depth`` (default ``N = 1``): the operator's pilot
  (0) may launch a child (1); that child may not launch a grandchild.
* FAN-OUT CAP: at most ``K`` (default 2) concurrent live children per launching
  agent — enforced by an atomic reserve/commit/release against the proxy's
  in-memory ledger (the proxy is single-threaded, so two racing launches cannot
  both take the last slot).
* OFF BY DEFAULT: an agent may launch only if its session has
  ``agent_launch_enabled: true`` (the pilot path sets it explicitly with
  ``--enable-agent-launch``). The hub verb is additionally gated by the
  machine config's ``agent_launch_enabled`` (default false).
* FULL PROVENANCE: every launch AND every refusal is recorded (WS-A) —
  ``{launcher_instance, target_profile, headless, depth, ts, decision, reason}``
  — in the launcher's session ``injection_log.jsonl`` (agent) or
  ``<runtime>/agent_launch/launch_log.jsonl`` (operator).
* NO autonomous production launch: nothing in the harness or hub calls this on
  its own; it runs only when a principal (or an enabled agent) invokes it.
* Instance-id collision guard: a harnessed launch whose instance_id is already
  REGISTERED on the live hub is refused (the hub registry would otherwise
  silently re-point the live session's traffic at the new child).

⚠ Residual (stated): a same-uid process that fully detaches (double-fork /
``setsid``) and scrubs ``GNOMON_AGENT_DEPTH`` loses its ancestry and looks like the
operator. The bar is "keep honest agents honest", not a hostile same-uid
adversary — the same bar as the hub-auth token.
"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import json
import os
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

ENV_DEPTH = "GNOMON_AGENT_DEPTH"
ENV_INSTANCE = "GNOMON_INSTANCE_ID"
ENV_SESSION = "GNOMON_SESSION_DIR"

DEFAULT_MAX_DEPTH = 1
DEFAULT_MAX_CHILDREN = 2
DEFAULT_HEADLESS_TIMEOUT = 600.0
RESERVATION_TTL_SECONDS = 180.0

HARNESS_MODULE = "gnomon.harness"
HEADLESS_MODULE = "gnomon.harness.headless"

KIND_OPERATOR = "operator"
KIND_AGENT = "agent"
KIND_HEADLESS = "headless-agent"


class LaunchRefused(Exception):
    """A rail refused the launch. The message is the logged reason."""


# --- ancestry ----------------------------------------------------------------


@dataclass(frozen=True)
class Ancestor:
    pid: int
    kind: str          # KIND_AGENT (harness) | KIND_HEADLESS (headless wrapper)
    session_dir: str
    argv: Tuple[str, ...] = ()


def _module_target(argv: List[str]) -> Optional[Tuple[str, str]]:
    """``(module, session_dir)`` if argv is ``... -m <gnomon harness module> <dir> ...``."""
    for i, a in enumerate(argv):
        if a == "-m" and i + 1 < len(argv) and argv[i + 1] in (HARNESS_MODULE, HEADLESS_MODULE):
            if i + 2 < len(argv) and not argv[i + 2].startswith("-"):
                return argv[i + 1], argv[i + 2]
    return None


def find_gm_ancestor(
    pid: Optional[int] = None,
    *,
    probe: Optional[Callable[[int], Any]] = None,
    argv_of: Optional[Callable[[int], Optional[List[str]]]] = None,
    max_hops: int = 64,
) -> Optional[Ancestor]:
    """Nearest ancestor (starting at ``pid``'s PARENT) that is a gnomon harness or a
    headless wrapper. ``hub.procinfo`` is reused for ppid + argv."""
    if probe is None or argv_of is None:
        from ..hub import procinfo

        probe = probe or procinfo.probe
        argv_of = argv_of or procinfo.argv_of
    current = os.getpid() if pid is None else pid
    for _ in range(max_hops):
        state = probe(current)
        ppid = getattr(state, "ppid", None)
        if not ppid or ppid <= 1 or ppid == current:
            return None
        current = ppid
        argv = argv_of(current) or []
        hit = _module_target(list(argv))
        if hit is not None:
            module, sdir = hit
            kind = KIND_HEADLESS if module == HEADLESS_MODULE else KIND_AGENT
            return Ancestor(current, kind, sdir, tuple(argv))
    return None


# --- the proxy-side ledger (authoritative, in memory) -------------------------


class LaunchLedger:
    """Held by a harness proxy: this session's launch identity + live children.

    Values are fixed at proxy startup from launch.json (before the agent ran)."""

    def __init__(
        self,
        *,
        instance_id: str,
        session_dir: str,
        depth: int = 0,
        enabled: bool = False,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_children: int = DEFAULT_MAX_CHILDREN,
        gate: Any = None,
        is_alive: Optional[Callable[[int, Optional[float]], bool]] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.instance_id = instance_id
        self.session_dir = session_dir
        self.depth = int(depth)
        self.enabled = bool(enabled)
        self.max_depth = int(max_depth)
        self.max_children = int(max_children)
        self._gate = gate
        self._is_alive = is_alive or _pid_alive
        self._clock = clock
        self._slots: Dict[str, Dict[str, Any]] = {}
        self._n = 0

    def _prune(self) -> None:
        now = self._clock()
        for rid in list(self._slots):
            slot = self._slots[rid]
            if slot["state"] == "reserved" and now - slot["reserved_at"] > RESERVATION_TTL_SECONDS:
                self._slots.pop(rid)
            elif slot["state"] == "live" and not self._is_alive(slot["pid"], slot.get("start_time")):
                self._slots.pop(rid)

    def live_count(self) -> int:
        self._prune()
        return len(self._slots)

    def context(self) -> Dict[str, Any]:
        return {"ok": True, "instance_id": self.instance_id, "session_dir": self.session_dir,
                "depth": self.depth, "enabled": self.enabled, "max_depth": self.max_depth,
                "max_children": self.max_children, "live_children": self.live_count()}

    def _record(self, **fields: Any) -> None:
        if self._gate is not None:
            self._gate.record("agent-launch", launcher_instance=self.instance_id, launcher_kind=KIND_AGENT,
                              **fields)

    def reserve(self, target_profile: str, headless: bool) -> Dict[str, Any]:
        child_depth = self.depth + 1
        base = {"target_profile": target_profile, "headless": headless, "depth": child_depth}
        reason = None
        if not self.enabled:
            reason = f"agent_launch_enabled is false for {self.instance_id!r} (off by default)"
        elif child_depth > self.max_depth:
            reason = (f"depth cap: launcher depth {self.depth} + 1 = {child_depth} exceeds "
                      f"max_depth {self.max_depth}")
        elif self.live_count() >= self.max_children:
            reason = f"fan-out cap: {self.live_count()} live children >= max_children {self.max_children}"
        if reason is not None:
            self._record(decision="deny", reason=reason, **base)
            return {"ok": False, "refused": True, "error": reason, **base}
        self._n += 1
        rid = f"r{self._n}"
        self._slots[rid] = {"state": "reserved", "reserved_at": self._clock(), "pid": None, **base}
        self._record(decision="reserve", reservation=rid, **base)
        return {"ok": True, "reservation": rid, "child_depth": child_depth}

    def commit(self, reservation: str, pid: Optional[int], start_time: Optional[float] = None,
               **info: Any) -> Dict[str, Any]:
        slot = self._slots.get(reservation)
        if slot is None:
            return {"ok": False, "error": f"unknown or expired reservation {reservation!r}"}
        slot.update(state="live", pid=pid, start_time=start_time)
        self._record(decision="launched", reservation=reservation, child_pid=pid,
                     target_profile=slot["target_profile"], headless=slot["headless"], depth=slot["depth"],
                     **{k: v for k, v in info.items() if isinstance(v, (str, int, float, bool, type(None)))})
        return {"ok": True}

    def release(self, reservation: str, reason: str = "") -> Dict[str, Any]:
        slot = self._slots.pop(reservation, None)
        if slot is not None:
            self._record(decision="released", reservation=reservation, reason=reason,
                         target_profile=slot["target_profile"], headless=slot["headless"], depth=slot["depth"])
        return {"ok": True}

    def handle(self, name: str, command: Dict[str, Any]) -> Dict[str, Any]:
        if name == "launch_context":
            return self.context()
        if name == "launch_reserve":
            tp = command.get("target_profile")
            if not isinstance(tp, str) or not tp:
                return {"ok": False, "error": "target_profile required"}
            return self.reserve(tp, bool(command.get("headless", False)))
        if name == "launch_commit":
            pid = command.get("pid")
            return self.commit(str(command.get("reservation")), pid if isinstance(pid, int) else None,
                               command.get("start_time"),
                               child_instance=command.get("child_instance"),
                               child_session_dir=command.get("child_session_dir"))
        if name == "launch_release":
            return self.release(str(command.get("reservation")), str(command.get("reason", "")))
        return {"ok": False, "error": f"unknown launch verb {name!r}"}


LEDGER_VERBS = ("launch_context", "launch_reserve", "launch_commit", "launch_release")


def _pid_alive(pid: Optional[int], start_time: Optional[float]) -> bool:
    if not pid:
        return False
    try:
        from ..hub import procinfo

        st = procinfo.probe(pid)
        return st.state == procinfo.ALIVE and procinfo.same_process(st, start_time)
    except Exception:  # noqa: BLE001 - unverifiable counts as alive (fail closed on the cap)
        return True


# --- the launcher identity (client side) -------------------------------------


def _socket_query(socket_path: str, command: Dict[str, Any], expect_server_pid: Optional[int],
                  timeout: float = 5.0) -> Dict[str, Any]:
    from .control import peer_pid

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(socket_path)
        if expect_server_pid is not None:
            server = peer_pid(s)
            if server != expect_server_pid:
                raise LaunchRefused(f"control socket server pid {server} is not the harness ancestor "
                                    f"{expect_server_pid} — refusing (fail closed)")
        s.sendall((json.dumps(command) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    reply = json.loads(buf.split(b"\n", 1)[0] or b"{}")
    if not isinstance(reply, dict):
        raise LaunchRefused("non-object reply from the harness control socket")
    return reply


def _socket_path_for(session_dir: str) -> Optional[str]:
    path = os.path.join(session_dir, ".claude", "harness.mcp.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        args = cfg["mcpServers"]["gnomon-self"]["args"]
        return args[args.index("--socket") + 1]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


class Launcher:
    """Who is launching, and the rail operations for that launcher kind."""

    kind = KIND_OPERATOR
    instance_id: Optional[str] = None
    session_dir: Optional[str] = None
    depth: Optional[int] = None  # None = the operator (children are depth-0 roots)
    #: The runtime home whose ``agent_launch/launch_log.jsonl`` receives operator
    #: provenance. ``launch_child`` sets it from the config IT was given, so a
    #: caller's config (a test's tmp root) is honoured — never a silent fallback
    #: to the machine config.
    runtime_root: Optional[str] = None

    def child_depth(self) -> int:
        return 0

    def reserve(self, target_profile: str, headless: bool) -> Dict[str, Any]:
        return {"ok": True, "reservation": None, "child_depth": 0}

    def commit(self, reservation: Any, **info: Any) -> None:
        pass

    def release(self, reservation: Any, reason: str) -> None:
        pass

    def log_path(self) -> str:
        runtime = self.runtime_root
        if runtime is None:
            from ..config import ConfigStore

            runtime = ConfigStore().load().runtime_root
        return os.path.join(os.path.expanduser(runtime), "agent_launch", "launch_log.jsonl")

    def record(self, **fields: Any) -> None:
        from .hooks import append_record

        try:
            append_record(self.log_path(), {"kind": "agent-launch", "launcher_kind": self.kind,
                                             "launcher_instance": self.instance_id, **fields})
        except OSError as exc:
            sys.stderr.write(f"agent_launch: provenance write FAILED: {exc}\n")

    def describe(self) -> Dict[str, Any]:
        return {"kind": self.kind, "instance_id": self.instance_id, "session_dir": self.session_dir,
                "depth": self.depth}


class OperatorLauncher(Launcher):
    """No gnomon ancestor: the principal. Children are depth-0 roots; no fan-out cap."""


class AgentLauncher(Launcher):
    kind = KIND_AGENT

    def __init__(self, ancestor: Ancestor, query: Callable[[Dict[str, Any]], Dict[str, Any]]) -> None:
        self._anc = ancestor
        self._query = query
        ctx = query({"cmd": "launch_context"})
        if not ctx.get("ok"):
            raise LaunchRefused(f"harness refused launch_context: {ctx.get('error')}")
        self.instance_id = ctx.get("instance_id")
        self.session_dir = ctx.get("session_dir") or ancestor.session_dir
        self.depth = int(ctx.get("depth", 0))
        self.context = ctx
        self._last_depth = self.depth + 1

    def child_depth(self) -> int:
        return self._last_depth

    def reserve(self, target_profile: str, headless: bool) -> Dict[str, Any]:
        reply = self._query({"cmd": "launch_reserve", "target_profile": target_profile, "headless": headless})
        if reply.get("ok"):
            self._last_depth = int(reply.get("child_depth", self.depth + 1))
        return reply

    def commit(self, reservation: Any, **info: Any) -> None:
        self._query({"cmd": "launch_commit", "reservation": reservation, **info})

    def release(self, reservation: Any, reason: str) -> None:
        try:
            self._query({"cmd": "launch_release", "reservation": reservation, "reason": reason})
        except (OSError, LaunchRefused, ValueError):
            pass

    def log_path(self) -> str:
        from .hooks import injection_log_path

        return injection_log_path(self.session_dir or self._anc.session_dir)

    def record(self, **fields: Any) -> None:
        # The proxy's ledger already records reserve/deny/launched/released in
        # this session's log; the CLI adds the outcome detail record.
        super().record(**fields)


def resolve_launcher(
    *,
    env: Optional[Dict[str, str]] = None,
    pid: Optional[int] = None,
    find_ancestor: Optional[Callable[..., Optional[Ancestor]]] = None,
    query_factory: Optional[Callable[[Ancestor], Callable[[Dict[str, Any]], Dict[str, Any]]]] = None,
) -> Launcher:
    """Decide who is launching (see module docstring). Raises :class:`LaunchRefused`."""
    # D15 fix pass: resolved at CALL time (was a def-time default), so the test
    # suite's isolation fixture can sever process ancestry to the live harness.
    if find_ancestor is None:
        find_ancestor = find_gm_ancestor
    env = dict(os.environ if env is None else env)
    anc = find_ancestor(pid)
    if anc is None:
        if env.get(ENV_DEPTH) not in (None, ""):
            raise LaunchRefused(
                f"environment claims a harnessed launcher ({ENV_DEPTH}={env.get(ENV_DEPTH)!r}) but no "
                "gnomon harness ancestor was found — refusing (fail closed)")
        return OperatorLauncher()
    if anc.kind == KIND_HEADLESS:
        raise LaunchRefused("a headless agent may not launch agents (interim rail)")
    if query_factory is None:
        def query_factory(a: Ancestor):  # type: ignore[no-redef]
            sock = _socket_path_for(a.session_dir)
            if not sock:
                raise LaunchRefused(f"harness ancestor {a.pid} has no self-control socket "
                                    "(self_actuation off?) — cannot verify its launch rails; refusing")
            return lambda cmd: _socket_query(sock, cmd, a.pid)
    try:
        return AgentLauncher(anc, query_factory(anc))
    except (OSError, ValueError) as exc:
        raise LaunchRefused(f"cannot reach harness ancestor {anc.pid}'s control socket: {exc}") from None


# --- hub discovery ------------------------------------------------------------


def _connectable(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_hub(sessions_root: str, rendezvous_file: Optional[str] = None) -> Tuple[Optional[int], Optional[str]]:
    """The live hub's rendezvous port (+ MCP url).

    D15 amendment (2026-09-29): ``<runtime_root>/hub/rendezvous.json`` is the
    authoritative source — if it names a connectable port, that (and its mcp_url)
    wins. Otherwise FALL BACK to the legacy scrape: the NEWEST session launch.json
    whose port currently accepts a connection (a hub that predates the discovery
    file). Either way the port is only the child's launch-time HINT — its harness
    re-discovers via rendezvous.json for life. A raw connect+close registers
    nothing on the hub."""
    if rendezvous_file:
        from ..hub.rendezvous import read_rendezvous

        info, _err = read_rendezvous(rendezvous_file)
        if info is not None and _connectable(info.port):
            # FIX PASS 2 (re-audit MEDIUM): the operator Bearer token is sent to this
            # url — only a literal-loopback http url is returned (read_rendezvous
            # already drops any other; re-checked here at the point of use).
            from ..hub.rendezvous import is_loopback_http_url

            return info.port, (info.mcp_url if is_loopback_http_url(info.mcp_url) else None)
    try:
        names = sorted(os.listdir(sessions_root),
                       key=lambda n: os.path.getmtime(os.path.join(sessions_root, n)), reverse=True)
    except OSError:
        return None, None
    port: Optional[int] = None
    url: Optional[str] = None
    for name in names:
        cdir = os.path.join(sessions_root, name, ".claude")
        if port is None:
            try:
                with open(os.path.join(cdir, "launch.json"), "r", encoding="utf-8") as fh:
                    p = json.load(fh).get("hub_rendezvous_port")
                if isinstance(p, int) and _connectable(p):
                    port = p
            except (OSError, ValueError):
                pass
        if url is None:
            try:
                with open(os.path.join(cdir, ".mcp.json"), "r", encoding="utf-8") as fh:
                    u = json.load(fh).get("mcpServers", {}).get("gnomon", {}).get("url")
                from ..hub.rendezvous import is_loopback_http_url

                if is_loopback_http_url(u):
                    hp = int(u.split(":")[2].split("/")[0])
                    if _connectable(hp):
                        url = u
            except (OSError, ValueError, AttributeError, IndexError):
                pass
        if port is not None and url is not None:
            break
    if url is None:
        url = _hub_url_from_web_health()
    return port, url


def _hub_url_from_web_health() -> Optional[str]:
    """The running web dashboard reports its co-hosted hub's MCP url at
    ``/api/health`` (port ``GNOMON_WEB_PORT``, default 8770). Read-only probe."""
    try:
        import urllib.request

        port = int(os.environ.get("GNOMON_WEB_PORT", "8770"))
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as resp:
            u = json.loads(resp.read().decode()).get("hub_url")
        return u if isinstance(u, str) and u.startswith("http://127.0.0.1:") else None
    except Exception:  # noqa: BLE001
        return None


def hub_registered(instance_id: str, mcp_url: Optional[str], token_file: Optional[str]) -> Optional[bool]:
    """Is ``instance_id`` registered on the live hub? ``None`` = could not tell."""
    if not mcp_url or not token_file:
        return None
    try:
        with open(token_file, "r", encoding="utf-8") as fh:
            token = fh.read().strip()
        import urllib.request

        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                           "params": {"name": "context", "arguments": {"instance_id": instance_id}}}).encode()
        req = urllib.request.Request(mcp_url, data=body, headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            reply = json.loads(resp.read().decode())
        inner = json.loads(reply["result"]["content"][0]["text"])
        return bool(inner.get("ok"))
    except Exception:  # noqa: BLE001
        return None


# --- the launch ----------------------------------------------------------------


@dataclass
class LaunchReport:
    ok: bool
    mode: str                      # "harnessed" | "headless"
    launcher: Dict[str, Any]
    target_profile: str
    child_instance: Optional[str] = None
    child_depth: Optional[int] = None
    session_dir: Optional[str] = None
    pid: Optional[int] = None
    terminal: Optional[str] = None
    command: Optional[str] = None
    output: Optional[str] = None
    output_path: Optional[str] = None
    exit_code: Optional[int] = None
    hub_rendezvous_port: Optional[int] = None
    hub_registered: Optional[bool] = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)
    #: RESUME MODE (session adoption, 2026-09-28) — populated on --dry-run so an
    #: operator can verify the child argv + cwd BEFORE launching. ``resumed`` is
    #: True when a resume session id is in force; ``child_argv``/``child_cwd`` are
    #: the exact argv the harness would spawn and the cwd it would spawn in.
    resumed: Optional[bool] = None
    child_argv: Optional[List[str]] = None
    child_cwd: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses.asdict(self)


def _load_profile(target: str, store_root: Optional[str], allow_file: bool):
    from ..profile import Profile, ProfileStore

    if allow_file and (target.endswith(".json") or os.sep in target) and os.path.isfile(os.path.expanduser(target)):
        with open(os.path.expanduser(target), "r", encoding="utf-8") as fh:
            return Profile.from_dict(json.load(fh))
    return ProfileStore(store_root).read(target)


def _update_launch_json(session_dir: str, updates: Dict[str, Any]) -> None:
    path = os.path.join(session_dir, ".claude", "launch.json")
    with open(path, "r", encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg.update(updates)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


def _find_harness_pid(session_dir: str, timeout: float, all_pids=None, argv_of=None) -> Optional[int]:
    from ..hub import procinfo

    all_pids = all_pids or procinfo.all_pids
    argv_of = argv_of or procinfo.argv_of
    deadline = time.time() + timeout
    while True:
        for p in all_pids():
            try:
                argv = argv_of(p) or []
            except Exception:  # noqa: BLE001
                continue
            hit = _module_target(argv)
            if hit and hit[0] == HARNESS_MODULE and os.path.realpath(hit[1]) == os.path.realpath(session_dir):
                return p
        if time.time() >= deadline:
            return None
        time.sleep(0.25)


def launch_child(
    target_profile: str,
    prompt: str = "",
    *,
    headless: bool = False,
    instance_id: Optional[str] = None,
    terminal: Optional[str] = None,
    enable_child_launch: bool = False,
    hub_port: Any = "auto",
    timeout: float = DEFAULT_HEADLESS_TIMEOUT,
    background: bool = False,
    config: Any = None,
    launcher: Optional[Launcher] = None,
    allow_profile_file: Optional[bool] = None,
    terminal_runner: Optional[Callable[[str], int]] = None,
    popen: Callable[..., Any] = subprocess.Popen,
    find_pid: Optional[Callable[[str, float], Optional[int]]] = None,
    registered_check: Optional[Callable[[str], Optional[bool]]] = None,
    dry_run: bool = False,
    accept_folder_trust: bool = False,
    resume_session_id: Optional[str] = None,
    resume_cwd: Optional[str] = None,
    resume_prompt: Optional[str] = None,
) -> LaunchReport:
    """Launch one child agent. Never raises for a rail refusal — returns ``ok=False``.

    RESUME MODE (session adoption, 2026-09-28): pass ``resume_session_id`` +
    ``resume_cwd`` (and optionally ``resume_prompt``) to RESTORE an existing claude
    session under the harness instead of cold-starting. These override the loaded
    profile's own resume fields. In resume mode ``build_child_argv`` omits the
    cold-start bootstrap and the child is spawned in ``resume_cwd``.
    """
    from ..config import ConfigStore
    from ..launch import LaunchManager
    from .spawn import sanitize_env

    mode = "headless" if headless else "harnessed"
    try:
        launcher = launcher if launcher is not None else resolve_launcher()
    except LaunchRefused as exc:
        rep = LaunchReport(False, mode, {"kind": "refused-before-identity"}, target_profile, error=str(exc))
        OperatorLauncher().record(decision="deny", reason=str(exc), target_profile=target_profile,
                                  headless=headless, depth=None)
        return rep
    report = LaunchReport(False, mode, launcher.describe(), target_profile)

    if config is None:
        config = ConfigStore().load()
    if launcher.runtime_root is None:
        launcher.runtime_root = os.path.expanduser(config.runtime_root)
    if allow_profile_file is None:
        allow_profile_file = launcher.kind == KIND_OPERATOR

    # -- rails: enable / depth / fan-out (atomic reserve at the proxy for agents)
    res = launcher.reserve(target_profile, headless)
    if not res.get("ok"):
        report.error = res.get("error") or "refused"
        report.child_depth = res.get("depth")
        # operator refusals are logged here; agent refusals by the proxy ledger.
        if launcher.kind == KIND_OPERATOR:
            launcher.record(decision="deny", reason=report.error, target_profile=target_profile, headless=headless)
        return report
    reservation = res.get("reservation")
    child_depth = int(res.get("child_depth", launcher.child_depth()))
    report.child_depth = child_depth

    def _fail(msg: str) -> LaunchReport:
        # M1 (fix-pass 2026-09-28): a dry-run sets report.ok=True BEFORE the
        # try-block that may call _fail (build_child_argv / resolve_child_cwd can
        # raise, e.g. --resume-session with no --resume-cwd). Every _fail path is an
        # error path, so force ok=False here — otherwise a failed dry-run kept the
        # optimistic ok=True and main() exited 0 on a real error.
        report.ok = False
        report.error = msg
        launcher.release(reservation, msg)
        launcher.record(decision="error", reason=msg, target_profile=target_profile, headless=headless,
                        depth=child_depth, child_instance=report.child_instance)
        return report

    try:
        profile = _load_profile(target_profile, config.intelligences_root(), bool(allow_profile_file))
    except (KeyError, ValueError, OSError) as exc:
        return _fail(f"profile {target_profile!r}: {exc}")

    stamp = time.strftime("%H%M%S")
    child_id = instance_id or (profile.instance_id if launcher.kind == KIND_OPERATOR
                               else f"{profile.instance_id}-c{stamp}")
    report.child_instance = child_id
    bootstrap = profile.bootstrap_prompt
    if prompt:
        bootstrap = (bootstrap + "\n\n" + prompt) if (bootstrap and not headless) else prompt
    child_profile = dataclasses.replace(profile, instance_id=child_id, bootstrap_prompt=bootstrap)
    term = terminal or profile.terminal
    if term != "none":
        child_profile = dataclasses.replace(child_profile, terminal=term)
    else:
        # "none" is not a terminal template; LaunchManager still renders a
        # (never-run) command string, so give it a real key.
        child_profile = dataclasses.replace(child_profile, terminal="terminal_app")

    # RESUME MODE (2026-09-28): the CLI/caller resume args OVERRIDE the profile's
    # own resume fields (operator path). A supplied value wins; ``None`` leaves the
    # profile's field untouched. Only touch the dataclass when something is set, so
    # a plain fresh launch is byte-identical.
    if resume_session_id is not None or resume_cwd is not None or resume_prompt is not None:
        child_profile = dataclasses.replace(
            child_profile,
            resume_session_id=(resume_session_id if resume_session_id is not None
                               else getattr(profile, "resume_session_id", None)),
            resume_cwd=(resume_cwd if resume_cwd is not None
                        else getattr(profile, "resume_cwd", None)),
            resume_prompt=(resume_prompt if resume_prompt is not None
                           else getattr(profile, "resume_prompt", None)),
        )

    # -- resume-mode guards (fix-pass 2026-09-28) --------------------------------
    # The effective resume id is the post-merge value (CLI override OR the loaded
    # profile's own field), so a resume launch is caught however it was requested.
    effective_resume_id = getattr(child_profile, "resume_session_id", None)
    if effective_resume_id:
        # L1: a resume id that starts with "-" or is not UUID-shaped is refused
        # LOUD. A "--"-leading id would otherwise be spliced into the child argv as
        # a flag (pushing --mcp-config into the prompt slot); a non-UUID id can
        # never resolve a real transcript. Validated at this single chokepoint so
        # both the real spawn and --dry-run refuse it.
        from .launcher import validate_resume_session_id

        try:
            validate_resume_session_id(effective_resume_id)
        except ValueError as exc:
            return _fail(f"invalid resume_session_id: {exc}")
        # M2: --headless cannot adopt a session. headless.py runs `claude -p`
        # fresh with NO resume handling, so the combination silently ran a cold
        # session while dry-run falsely reported resumed:true. Refuse it LOUD.
        if headless:
            return _fail(
                "--headless cannot be combined with a resume session id "
                f"({effective_resume_id!r}) — headless runs `claude -p` fresh and "
                "cannot adopt an existing session; drop --headless or the resume id"
            )
        # M3: a task --prompt is silently dropped in resume mode (it is merged into
        # the cold-start bootstrap, which the resume argv discards). Refuse it LOUD;
        # the sanctioned re-orient channel is --resume-prompt.
        if prompt:
            return _fail(
                "--prompt cannot be combined with a resume session id — a resumed "
                "session already carries its context and the cold-start bootstrap is "
                "discarded, so the prompt would be silently dropped; use --resume-prompt "
                "for a gentle re-orient turn instead"
            )

    # -- hub discovery (the child registers on the live hub if we find it)
    port: Optional[int] = None
    url: Optional[str] = None
    if hub_port == "auto":
        from ..hub.rendezvous import rendezvous_path

        rv_port, rv_url = discover_hub(os.devnull, rendezvous_path(config.runtime_root))
        if rv_port is not None:
            port, url = rv_port, rv_url   # D15 amendment: the discovery file wins
        else:
            if launcher.kind == KIND_AGENT and launcher.session_dir:
                try:
                    with open(os.path.join(launcher.session_dir, ".claude", "launch.json"), "r") as fh:
                        port = json.load(fh).get("hub_rendezvous_port")
                except (OSError, ValueError):
                    port = None
            if port is None:
                port, url = discover_hub(config.sessions_root())
            else:
                _p, url = discover_hub(config.sessions_root())
    elif hub_port not in (None, "none", ""):
        port = int(hub_port)
    report.hub_rendezvous_port = port

    token_file = os.path.join(os.path.expanduser(config.runtime_root), "hub", "operator.token")
    check = registered_check or (lambda iid: hub_registered(iid, url, token_file))
    if not headless and port:
        already = check(child_id)
        if already:
            return _fail(f"instance_id {child_id!r} is already registered on the live hub — pass a distinct "
                         "--instance-id (a second registration would re-point the live session's traffic)")

    if dry_run:
        report.ok = True
        report.notes.append("dry-run: rails passed; nothing launched")
        # DEMONSTRATION (no spawn): compute the exact child argv + cwd the harness
        # WOULD build, so an operator can verify resume mode (no cold-start
        # bootstrap, --resume present, cwd=resume_cwd) before a real launch. Uses
        # representative settings/mcp paths under a placeholder session dir — the
        # resume branch affects the argv TAIL (--resume vs -- bootstrap) + the cwd,
        # both fully determined here.
        from .launcher import build_child_argv, resolve_child_cwd

        demo_session_dir = os.path.join(config.sessions_root(), f"{child_id}-<dry-run>")
        demo_cfg = {
            "claude_bin": config.resolved_claude_bin(),
            "bootstrap_prompt": child_profile.bootstrap_prompt,
            "extra_args": child_profile.extra_args,
            "resume_session_id": getattr(child_profile, "resume_session_id", None),
            "resume_cwd": getattr(child_profile, "resume_cwd", None),
            "resume_prompt": getattr(child_profile, "resume_prompt", None),
            # 2026-09-29 remediation (Diamond #5): mirror what launcher.main() adds
            # so the dry-run argv is EXACT — the profile's --model, and in RESUME
            # mode the agent-def delivery (--add-dir <session_dir>) armed at spawn.
            "model": getattr(child_profile, "model", None),
        }
        if demo_cfg["resume_session_id"]:
            demo_cfg["agent_def_add_dir"] = demo_session_dir
        demo_settings = os.path.join(demo_session_dir, ".claude", "harness.settings.json")
        demo_mcp = os.path.join(demo_session_dir, ".claude", "harness.mcp.json")
        try:
            report.child_argv = build_child_argv(
                demo_cfg, demo_session_dir, settings_path=demo_settings, mcp_config_path=demo_mcp
            )
            report.child_cwd = resolve_child_cwd(demo_cfg, demo_session_dir)
        except ValueError as exc:
            return _fail(f"dry-run: {exc}")
        report.resumed = bool(getattr(child_profile, "resume_session_id", None))
        report.notes.append(
            "dry-run RESUME argv + cwd shown (child_argv / child_cwd / resumed)"
            if report.resumed else "dry-run fresh argv + cwd shown (child_argv / child_cwd)"
        )
        launcher.release(reservation, "dry-run")
        return report

    manager = LaunchManager(sessions_root=config.sessions_root(), intelligences_root=config.intelligences_root(),
                            hub_url=url, hub_rendezvous_port=port, config=config)
    try:
        result = manager.launch(child_profile)
    except Exception as exc:  # noqa: BLE001 - a refusal is returned, never raised
        return _fail(f"session build failed: {type(exc).__name__}: {exc}")
    report.session_dir = result.session_dir
    child_enabled = bool(enable_child_launch) and not headless
    _update_launch_json(result.session_dir, {
        "agent_depth": child_depth,
        "agent_launch_enabled": child_enabled,
        "agent_launch": {"max_depth": DEFAULT_MAX_DEPTH, "max_children": DEFAULT_MAX_CHILDREN},
        "launched_by": launcher.describe(),
        # Opt-in (operator flag): answer Claude Code's first-run folder-trust
        # dialog for THIS freshly-created session dir (harness/startup.py).
        "accept_folder_trust": bool(accept_folder_trust) and not headless,
    })
    env_overrides = {ENV_DEPTH: str(child_depth), ENV_INSTANCE: child_id, ENV_SESSION: result.session_dir}

    if headless:
        return _run_headless(report, launcher, reservation, result.session_dir, bootstrap, config, timeout,
                             background, env_overrides, popen, child_depth, target_profile)

    # -- harnessed
    report.terminal = term
    finder = find_pid or (lambda sd, t: _find_harness_pid(sd, t))
    if term == "none":
        log_path = os.path.join(result.session_dir, ".claude", "harness_stdout.log")
        argv = [sys.executable or "python3", "-m", HARNESS_MODULE, result.session_dir]
        report.command = " ".join(argv)
        # Security posture D3 (2026-09-29): this hop spawns the HARNESS (trusted
        # code), not claude — so no credential scrub here. The harness scrubs at its
        # own claude spawn, honouring the profile's env_passthrough; scrubbing now
        # would make a passthrough credential unreachable there.
        env = sanitize_env(overrides=env_overrides, scrub_credentials=False)
        try:
            with open(log_path, "ab") as out, open(os.devnull, "rb") as devnull:
                proc = popen(argv, stdin=devnull, stdout=out, stderr=subprocess.STDOUT, cwd=result.session_dir,
                             env=env, start_new_session=True)
        except OSError as exc:
            return _fail(f"background harness spawn failed: {exc}")
        report.pid = proc.pid
        report.output_path = log_path
        report.notes.append("background harness (no terminal window); TUI output -> " + log_path)
    else:
        report.command = result.launch_command
        try:
            rc = manager.open_terminal(result, runner=terminal_runner)
        except Exception as exc:  # noqa: BLE001
            return _fail(f"terminal open failed: {exc}")
        if rc not in (0, None):
            return _fail(f"terminal command exited {rc}")
        report.pid = finder(result.session_dir, 20.0)
        if report.pid is None:
            return _fail("terminal opened but no `python -m gnomon.harness <session_dir>` process "
                         "appeared within 20s — spawn NOT observed")
    start_time = None
    try:
        from ..hub import procinfo

        start_time = procinfo.probe(report.pid).start_time
    except Exception:  # noqa: BLE001
        pass
    launcher.commit(reservation, pid=report.pid, start_time=start_time, child_instance=child_id,
                    child_session_dir=result.session_dir)
    launcher.record(decision="launched", target_profile=target_profile, headless=False, depth=child_depth,
                    child_instance=child_id, child_pid=report.pid, session_dir=result.session_dir, terminal=term)
    report.ok = True
    return report


def _run_headless(report, launcher, reservation, session_dir, prompt, config, timeout, background,
                  env_overrides, popen, child_depth, target_profile) -> LaunchReport:
    from .spawn import sanitize_env

    out_path = os.path.join(session_dir, ".claude", "headless_output.txt")
    # Transcript STOP-THE-LINE (2026-09-29): pre-decide + record the headless
    # claude's transcript location (launch.json) and pass the id through.
    from .transcript import plan_transcript, record_transcript_location

    sid_args: List[str] = []
    try:
        plan = plan_transcript({}, session_dir)
        record_transcript_location(session_dir, plan)
        sid_args = ["--session-id", plan["session_id"]]
    except (OSError, ValueError) as exc:
        report.notes.append(f"transcript location NOT recorded: {exc}")
    argv = [sys.executable or "python3", "-m", HEADLESS_MODULE, session_dir,
            "--claude-bin", config.resolved_claude_bin(), "--output", out_path,
            "--timeout", str(int(timeout)), "--depth", str(child_depth), *sid_args, "--", prompt]
    report.command = " ".join(argv[:-1]) + " -- <prompt>"
    report.output_path = out_path
    env = sanitize_env(overrides=env_overrides)
    try:
        proc = popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                     cwd=session_dir, env=env, start_new_session=bool(background), text=True)
    except OSError as exc:
        report.error = f"headless spawn failed: {exc}"
        launcher.release(reservation, report.error)
        launcher.record(decision="error", reason=report.error, target_profile=target_profile, headless=True,
                        depth=child_depth)
        return report
    report.pid = proc.pid
    launcher.commit(reservation, pid=proc.pid, child_instance=report.child_instance,
                    child_session_dir=session_dir)
    launcher.record(decision="launched", target_profile=target_profile, headless=True, depth=child_depth,
                    child_instance=report.child_instance, child_pid=proc.pid, session_dir=session_dir,
                    background=bool(background))
    if background:
        report.ok = True
        report.notes.append(f"headless running in background; output -> {out_path}")
        return report
    try:
        stdout, stderr = proc.communicate(timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        report.notes.append("headless wrapper timed out and was killed")
    report.exit_code = proc.returncode
    report.output = stdout
    if stderr and stderr.strip():
        report.notes.append("stderr: " + stderr.strip()[:500])
    report.ok = proc.returncode == 0
    if not report.ok:
        report.error = f"headless exit {proc.returncode}"
    launcher.release(reservation, f"headless finished exit={proc.returncode}")
    launcher.record(decision="finished", target_profile=target_profile, headless=True, depth=child_depth,
                    child_instance=report.child_instance, exit_code=proc.returncode,
                    output_len=len(stdout or ""))
    return report


# --- list-resumable: enumerate adoptable claude sessions -------------------------


def _first_user_line(content: Any) -> Optional[str]:
    """The text of a transcript user turn — content is a string or a block list."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = str(block.get("text", "")).strip()
                if text:
                    return text
    return None


def _session_summary(path: str) -> Dict[str, Any]:
    """One resumable session: id · cwd · last-modified · first-user-line preview.

    ``cwd`` and the first user line are read FROM the transcript (Claude Code
    records ``cwd`` on each event), so ``--all`` need not un-mangle the dir name.
    """
    sid = os.path.splitext(os.path.basename(path))[0]
    try:
        mtime = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat()
    except OSError:
        mtime = None
    cwd: Optional[str] = None
    first_user: Optional[str] = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict):
                    continue
                if cwd is None and isinstance(obj.get("cwd"), str):
                    cwd = obj["cwd"]
                if first_user is None and obj.get("type") == "user":
                    msg = obj.get("message")
                    if isinstance(msg, dict):
                        first_user = _first_user_line(msg.get("content"))
                if cwd is not None and first_user is not None:
                    break
    except OSError:
        pass
    preview = first_user[:200] if first_user else None
    return {"id": sid, "cwd": cwd, "last_modified": mtime, "first_user_line": preview, "path": path}


def list_resumable_sessions(
    cwd: Optional[str] = None,
    *,
    all_projects: bool = False,
    projects_root: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Resumable claude sessions under ``~/.claude/projects/<mangled-cwd>/*.jsonl``.

    * default — the project dir for ``cwd`` (or the current dir when ``cwd`` is
      ``None``), resolved via the same mangle Claude Code uses (``/`` and every
      non-alphanumeric char → ``-`` on the RESOLVED realpath);
    * ``all_projects=True`` — every project dir under the projects root.

    Each entry: ``{id, cwd, last_modified, first_user_line, path}``, newest first.
    ``projects_root`` is injectable for tests.
    """
    from .context_readers import claude_config_dir, claude_project_dir_name, claude_transcript_dir

    root = projects_root if projects_root is not None else os.path.join(claude_config_dir(), "projects")
    dirs: List[str] = []
    if all_projects:
        if os.path.isdir(root):
            dirs = sorted(
                os.path.join(root, name)
                for name in os.listdir(root)
                if os.path.isdir(os.path.join(root, name))
            )
    else:
        target_cwd = cwd if cwd is not None else os.getcwd()
        # claude_transcript_dir resolves realpath + the soft-limit prefix match;
        # fall back to the plain mangled name so a not-yet-existing dir is reported
        # as empty rather than crashing.
        d = claude_transcript_dir(target_cwd, projects_root=root)
        dirs = [d or os.path.join(root, claude_project_dir_name(target_cwd))]
    out: List[Dict[str, Any]] = []
    for d in dirs:
        if not os.path.isdir(d):
            continue
        for path in sorted(glob.glob(os.path.join(d, "*.jsonl"))):
            out.append(_session_summary(path))
    out.sort(key=lambda r: r.get("last_modified") or "", reverse=True)
    return out


# --- CLI -------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m gnomon.harness.agent_launch",
                                     description="Launch an agent (harnessed or headless) — interim-railed.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    lp = sub.add_parser("launch", help="launch a child agent")
    lp.add_argument("--profile", required=True, help="profile instance_id (or, for the operator, a profile.json path)")
    lp.add_argument("--prompt", default="", help="task prompt (appended to the bootstrap; the whole -p prompt if headless)")
    lp.add_argument("--headless", action="store_true", help="run `claude -p` and return its output")
    lp.add_argument("--background", action="store_true", help="headless: do not wait; output goes to a file")
    lp.add_argument("--instance-id", default=None, help="child instance_id (default: profile id for the operator)")
    lp.add_argument("--terminal", default=None,
                    help="iterm2 | terminal_app | <custom> | none (none = detached background harness, no window)")
    lp.add_argument("--enable-agent-launch", action="store_true",
                    help="let the launched (harnessed) child itself launch agents (depth/fan-out caps still apply)")
    lp.add_argument("--hub-port", default="auto", help="auto | none | <port>")
    lp.add_argument("--timeout", type=float, default=DEFAULT_HEADLESS_TIMEOUT)
    lp.add_argument("--dry-run", action="store_true", help="run every rail; launch nothing")
    lp.add_argument("--accept-folder-trust", action="store_true",
                    help="UNATTENDED launches: auto-answer Claude Code's first-run folder-trust dialog for the "
                         "freshly created session dir (once, startup window only, recorded). Opt-in.")
    lp.add_argument("--resume-session", default=None,
                    help="RESUME MODE: restore this EXISTING claude session id under the harness (omits the "
                         "cold-start bootstrap). Overrides the profile's resume_session_id.")
    lp.add_argument("--resume-cwd", default=None,
                    help="RESUME MODE: the ORIGINAL session's cwd (REQUIRED with --resume-session; "
                         "claude --resume resolves the id within ~/.claude/projects/<mangled-cwd>/).")
    lp.add_argument("--resume-prompt", default=None,
                    help="RESUME MODE: optional gentle re-orient turn (appended after --). Default: no turn.")
    sub.add_parser("whoami", help="print the resolved launcher identity + rails")
    lr = sub.add_parser("list-resumable",
                        help="list resumable claude sessions (~/.claude/projects/<mangled-cwd>/*.jsonl)")
    lrg = lr.add_mutually_exclusive_group()
    lrg.add_argument("--cwd", default=None,
                     help="scan only this cwd's project dir (default: the current working directory)")
    lrg.add_argument("--all", action="store_true",
                     help="scan EVERY project dir under ~/.claude/projects")
    args = parser.parse_args(argv)

    if args.cmd == "list-resumable":
        print(json.dumps(list_resumable_sessions(cwd=args.cwd, all_projects=args.all), indent=2))
        return 0

    if args.cmd == "whoami":
        try:
            l = resolve_launcher()
            out = l.describe()
            if isinstance(l, AgentLauncher):
                out["context"] = l.context
        except LaunchRefused as exc:
            out = {"refused": str(exc)}
        print(json.dumps(out, indent=2))
        return 0

    report = launch_child(args.profile, args.prompt, headless=args.headless, instance_id=args.instance_id,
                          terminal=args.terminal, enable_child_launch=args.enable_agent_launch,
                          hub_port=args.hub_port, timeout=args.timeout, background=args.background,
                          dry_run=args.dry_run, accept_folder_trust=args.accept_folder_trust,
                          resume_session_id=args.resume_session, resume_cwd=args.resume_cwd,
                          resume_prompt=args.resume_prompt)
    if report.ok and report.mode == "harnessed" and report.hub_rendezvous_port and report.child_instance:
        # Best-effort witness that the child registered on the live hub.
        from ..config import ConfigStore

        cfg = ConfigStore().load()
        _p, url = discover_hub(cfg.sessions_root())
        tok = os.path.join(os.path.expanduser(cfg.runtime_root), "hub", "operator.token")
        for _ in range(40):
            reg = hub_registered(report.child_instance, url, tok)
            if reg:
                break
            time.sleep(0.5)
        report.hub_registered = reg
    print(json.dumps(report.to_dict(), indent=2))
    return 0 if report.ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""The Hub — co-hosts the MCP server + the rendezvous, sharing one registry.

Both transports run in ONE process so a
later CTk GUI can construct :class:`Hub` and read the same live
:class:`~gnomon.hub.registry.SessionRegistry` in memory — no IPC, no
second source of truth.

Transport choice (JUSTIFIED — "choose and justify"):

* **MCP surface -> streamable-HTTP** over stdlib ``ThreadingHTTPServer`` on
  127.0.0.1. A single ``POST /mcp`` endpoint takes a JSON-RPC request and returns
  a JSON response. Chosen over SSE because the tool surface is synchronous
  request/response; a long-lived SSE GET stream adds connection lifecycle we do
  not need, and streamable-HTTP is the current MCP direction (it supersedes the
  deprecated HTTP+SSE dual-endpoint transport) while degrading cleanly to plain
  request/response. Running it on a background thread inside the GUI process lets
  the handler close over the shared registry — the co-hosting the spec calls for.
* **Rendezvous -> a line-delimited JSON socket** (127.0.0.1 TCP). This is NOT
  MCP: it is our own duplex to the proxies, and the slice-1 ControlChannel
  already speaks exactly newline-delimited JSON, so the hub->proxy leg reuses
  that vocabulary with zero additions. A proxy connects, sends a ``register``
  frame, then the same socket carries injects DOWN and status frames UP.

Both servers bind to an ephemeral port when asked (``port=0``), so tests never
fight over a fixed port.

Operator auth (fix-pass WS2b, 2026-09-27)
-----------------------------------------
The actuation tools (``mcp.OPERATOR_TOOLS`` = inject / request_compact /
context) REQUIRE ``Authorization: Bearer <operator token>`` over HTTP. A call
without it — or with a wrong one — is REJECTED with HTTP 403 and a JSON-RPC
error (code -32001) and is never dispatched, so nothing reaches any session.
Discovery (``initialize`` / ``tools/list``) and the agent-facing non-actuation
tools (``send_dsd_for_human_review``, app lifecycle) are unchanged.

* The token is per-Hub random (``secrets.token_urlsafe(32)``) unless one is
  injected. With ``operator_token_file`` it is written 0600 (dir 0700) when the
  HTTP server starts and removed on :meth:`stop`, so the operator can
  ``curl -H "Authorization: Bearer $(cat <file>)" ...``.
* ⚠ BOUNDARY (stated, not hidden): this is localhost-client auth. It stops a
  raw curl from any session — the exposure the flight demonstrated — but a
  process running as the SAME Unix user that deliberately reads the token file
  can present it. The bar is "keep honest agents honest", not a hostile
  same-uid adversary.
* The rendezvous socket is NOT gated here (out of this pass's scope): it only
  carries hub->proxy commands DOWN and status UP; it cannot originate an inject.
  (A same-uid client could still register an impostor instance_id — recorded as
  a follow-up in the fix-pass SUMMARY, not fixed here.)
"""

from __future__ import annotations

import collections
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple

from .apps import AppLifecycle
from .caller_binding import (BINDING_ON, CALLER_HEADER, REGISTER_HASH_KEY, CallerAudit, Principal,
                             check_pending_record, consume_pending_record, host_origin_ok, is_wellformed_hash,
                             is_wellformed_token, principal_from_headers, token_hash)
from ..feature_events import VIEWER_ACTOR  # X26: the operator-authenticated caller's actor id
from .mcp import OPERATOR_TOOLS, DSDStore, GmMCPService
from .registry import SessionRegistry, SocketDownlink
from .rendezvous import remove_rendezvous_if_ours, write_rendezvous

#: the audit log's default name, beside rendezvous.json (machine-local, per uid).
CALLER_AUDIT_FILENAME = "caller-audit.jsonl"

#: JSON-RPC error code for a rejected (unauthenticated) operator call.
OPERATOR_AUTH_ERROR_CODE = -32001

#: PTY-hooks WS-F: an ``operator_fanout`` frame's text MUST carry the principal
#: prefix for its target — ``<{user}-to-all[@stamp]> `` / ``<{user}-to-{id}[@stamp]> `` — so the
#: fan-out cannot be used to type arbitrary UNSIGNED text into every session.
_FANOUT_PREFIX = re.compile(r"<[A-Za-z0-9 ._@+-]{1,64}-to-(?P<target>[A-Za-z0-9][A-Za-z0-9._-]{0,254})(?:@\d{4}-\d{2}-\d{2} \d{2}:\d{2})?> ")
MAX_FANOUT_TEXT = 20000
#: an operator message id (operator_messages.new_msg_id: 16 hex chars).
_MSG_ID_RE = re.compile(r"[0-9a-f]{8,32}")


def _bearer(header_value: Optional[str]) -> Optional[str]:
    if not header_value:
        return None
    scheme, _, value = header_value.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = value.strip()
    return value or None


def _requires_operator(request: Any) -> bool:
    """Does this JSON-RPC request invoke an operator-gated tool?"""
    if not isinstance(request, dict) or request.get("method") != "tools/call":
        return False
    params = request.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    return isinstance(name, str) and name in OPERATOR_TOOLS


class Hub:
    def __init__(
        self,
        registry: Optional[SessionRegistry] = None,
        service: Optional[GmMCPService] = None,
        dsd_store: Optional[DSDStore] = None,
        host: str = "127.0.0.1",
        app_lifecycle: Optional[AppLifecycle] = None,
        operator_token: Optional[str] = None,
        operator_token_file: Optional[str] = None,
        agent_launcher: Optional[Any] = None,
        agent_launch_enabled: Optional[Any] = None,
        rendezvous_file: Optional[str] = None,
        notice_queue: Optional[Any] = None,
        caller_binding: bool = False,
        caller_audit_file: Optional[str] = None,
        pending_dir: Optional[str] = None,
        apps_host: Optional[Any] = None,
        apps_enabled: Optional[Any] = None,
    ) -> None:
        self.registry = registry if registry is not None else SessionRegistry()
        # per-message delivery verdicts (bounded) for the mismatch warning.
        self._delivery_state: "collections.OrderedDict[str, Dict[str, set]]" = collections.OrderedDict()
        self._delivery_lock = threading.Lock()
        self.service = service if service is not None else GmMCPService(
            self.registry, dsd_store, app_lifecycle=app_lifecycle,
            agent_launcher=agent_launcher, agent_launch_enabled=agent_launch_enabled,
            apps_host=apps_host, apps_enabled=apps_enabled,
        )
        self._host = host
        # Operator credential for the actuation tools (WS2b). Always present —
        # the gate fails CLOSED; there is no "auth disabled" mode.
        if operator_token is not None and not operator_token:
            raise ValueError("operator_token must be non-empty when given")
        self._operator_token: str = operator_token or secrets.token_urlsafe(32)
        self._operator_token_file = operator_token_file

        self._http: Optional[ThreadingHTTPServer] = None
        self._http_thread: Optional[threading.Thread] = None

        self._rv_sock: Optional[socket.socket] = None
        self._rv_thread: Optional[threading.Thread] = None
        self._rv_stop = threading.Event()

        # D15 amendment (operator-approved 2026-09-29) — hub discovery + the durable
        # DSD-answer notice queue. Both default to None: a bare ``Hub()`` (every
        # unit test) publishes NO discovery file and queues nothing. Only the
        # Controller wires the live runtime paths (<runtime_root>/hub/rendezvous.json,
        # <runtime_root>/notices).
        #: A fresh identity per Hub object: harnesses re-register when it changes.
        self.hub_instance_id: str = uuid.uuid4().hex
        self._rendezvous_file = rendezvous_file
        self._rendezvous_published = False
        self._notice_queue = notice_queue
        # FIX PASS 2 (re-audit MEDIUM): connections SUPERSEDED for an instance_id,
        # kept open and parked (oldest first). When the holder's connection goes
        # away, the most recently parked one is sent ``vacated`` and re-registers.
        self._parked: Dict[str, list] = {}
        self._parked_lock = threading.Lock()
        # (hub_caller_binding, default OFF). OFF: none of the below is consulted and
        # the hub is today's, byte-identical. ON: per registered instance, sha256 of the CURRENT holder's
        # per-launch token; calls are bound by it, and a re-register without it is parked + audited.
        self._caller_binding = bool(caller_binding)
        self._bindings: Dict[str, Optional[str]] = {}
        self._bind_lock = threading.Lock()
        #: Where launchers write pending-launch records (None = the machine-local credential dir, resolved per check).
        self._pending_dir = pending_dir
        self.caller_audit: Optional[CallerAudit] = None
        if self._caller_binding:
            if caller_audit_file is None and rendezvous_file:
                caller_audit_file = os.path.join(os.path.dirname(rendezvous_file), CALLER_AUDIT_FILENAME)
            self.caller_audit = CallerAudit(caller_audit_file)
            self.service.enable_caller_binding(self.caller_audit)

    # -- operator credential -------------------------------------------------

    @property
    def operator_token(self) -> str:
        """The operator bearer token (in-process holders: the GUI / tests)."""
        return self._operator_token

    @property
    def operator_token_file(self) -> Optional[str]:
        return self._operator_token_file

    def is_operator(self, authorization_header: Optional[str]) -> bool:
        presented = _bearer(authorization_header)
        if presented is None:
            return False
        return hmac.compare_digest(presented.encode("utf-8"), self._operator_token.encode("utf-8"))

    def _write_token_file(self) -> None:
        path = self._operator_token_file
        if not path:
            return
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
        tmp = f"{path}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, (self._operator_token + "\n").encode("utf-8"))
        finally:
            os.close(fd)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)

    def _remove_token_file(self) -> None:
        path = self._operator_token_file
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                if fh.read().strip() != self._operator_token:
                    return  # another hub has since written its own — not ours
            os.unlink(path)
        except OSError:
            pass

    # -- caller binding ------------------------------

    @property
    def caller_binding(self) -> bool:
        return self._caller_binding

    def binding_status(self, recent: int = 5) -> Optional[Dict[str, Any]]:
        """Round 3 (R2-P2-1): the operator-visible summary of the caller binding (None when OFF). Refused
        registrations are counted and the most recent are listed, so a session that is dark under an ON hub is
        visible to the operator (the web footer reads this through /api/health)."""
        if not self._caller_binding or self.caller_audit is None:
            return None
        parked = self.caller_audit.events("register-parked")
        return {
            "binding": BINDING_ON,
            "registered": self.registry.instance_ids(),
            "register_parked": len(parked),
            "recent_register_parked": [{"ts": r.get("ts"), "instance_id": r.get("instance_id"),
                                        "reason": r.get("reason")} for r in parked[-recent:]],
        }

    def bound_instance_for(self, token: str) -> Optional[str]:
        """The instance whose CURRENT holder registered sha256(``token``), or None (UNBOUND). Compared with
        ``hmac.compare_digest`` against every stored hash."""
        if not self._caller_binding or not is_wellformed_token(token):
            return None
        presented = token_hash(token)
        with self._bind_lock:
            items = list(self._bindings.items())
        matches = [iid for iid, stored in items
                   if stored and hmac.compare_digest(presented.encode("ascii"), stored.encode("ascii"))]
        if len(matches) != 1:
            return None  # none, or ambiguous (registration refuses a reused hash, so this is belt and braces)
        return matches[0] if self.registry.is_registered(matches[0]) else None

    def _hash_held_elsewhere(self, instance_id: str, presented: str) -> Optional[str]:
        with self._bind_lock:
            items = list(self._bindings.items())
        for iid, stored in items:
            if (iid != instance_id and stored and self.registry.is_registered(iid)
                    and hmac.compare_digest(presented.encode("ascii"), stored.encode("ascii"))):
                return iid
        return None

    def principal_for_http(self, headers: Any) -> Principal:
        """The transport's half: ESTABLISH who is calling (operator Bearer, caller header). Authorization is
        decided in GmMCPService.dispatch."""
        return principal_from_headers(headers.get_all(CALLER_HEADER) or [],
                                      operator=self.is_operator(headers.get("Authorization")),
                                      resolve=self.bound_instance_for)

    def _binding_hash(self, instance_id: str) -> Optional[str]:
        with self._bind_lock:
            return self._bindings.get(instance_id)

    def _set_binding(self, instance_id: str, presented: Optional[str]) -> None:
        with self._bind_lock:
            self._bindings[instance_id] = presented

    def _drop_binding(self, instance_id: str) -> None:
        with self._bind_lock:
            self._bindings.pop(instance_id, None)

    # -- D15 amendment: discovery file + notice queue ------------------------

    @property
    def rendezvous_file(self) -> Optional[str]:
        return self._rendezvous_file

    @property
    def notice_queue(self) -> Optional[Any]:
        return self._notice_queue

    def _publish_rendezvous(self) -> None:
        """Write <runtime_root>/hub/rendezvous.json (atomic, 0600). A write failure
        is LOUD on stderr but never stops the hub — the launch-time port hint
        still works for dashboard-launched sessions."""
        if not self._rendezvous_file or self._rv_sock is None:
            return
        host, port = self._rv_sock.getsockname()[:2]
        try:
            if self._caller_binding:  # ON publishes binding: on; OFF writes no key (absent = off; byte-identical)
                write_rendezvous(self._rendezvous_file, host=str(host), port=int(port),
                                 hub_instance_id=self.hub_instance_id, mcp_url=self.mcp_url,
                                 binding=BINDING_ON)
            else:
                write_rendezvous(self._rendezvous_file, host=str(host), port=int(port),
                                 hub_instance_id=self.hub_instance_id, mcp_url=self.mcp_url)
            self._rendezvous_published = True
        except OSError as exc:
            sys.stderr.write(f"[gm-hub] rendezvous.json publish FAILED ({exc}); harnesses "
                             "cannot discover this hub except via a launch-time port\n")

    def _drain_notices(self, instance_id: str, downlink: Any) -> int:
        """Re-send every queued DSD-answer notice for a (re)registered proxy. The
        files stay until that proxy ACKs; the harness de-duplicates by dsd_id."""
        if self._notice_queue is None:
            return 0
        from ..dsd.live import DsdCorpusError          # lazy: g47b/g67 (this module is in the hook closure)
        from ..dsd.notice import notice_command

        sent = 0
        try:
            pending = self._notice_queue.pending(instance_id)
        except (OSError, ValueError) as exc:
            sys.stderr.write(f"[gm-hub] notice queue unreadable for {instance_id!r}: {exc}\n")
            return 0
        except DsdCorpusError as exc:
            # g70: a fail-closed DSD refusal closes the DSD FEATURE, never the connection: this runs on every proxy
            # (re)register, so letting it escape would unregister the instance. The files stay queued for after the fix.
            sys.stderr.write(f"[gm-hub] DSD notices for {instance_id!r} NOT drained (refused, the files stay queued): {exc}\n")
            return 0
        for notice in pending:
            try:
                downlink.send(notice_command(notice))
                sent += 1
            except OSError:
                break  # the proxy went away mid-drain; it re-drains on re-register
            except DsdCorpusError as exc:
                sys.stderr.write(f"[gm-hub] DSD notice {notice.get('dsd_id')!r} for {instance_id!r} NOT sent "
                                 f"(refused, stays queued): {exc}\n")
                break
        if sent:
            sys.stderr.write(f"[gm-hub] drained {sent} queued DSD notice(s) to {instance_id}\n")
        return sent

    # -- MCP HTTP server -----------------------------------------------------

    def start_mcp_http(self, port: int = 0) -> Tuple[str, int]:
        service = self.service
        hub = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: Any) -> None:  # silence stderr spam
                pass

            def _send_json(self, status: int, obj: Any) -> None:
                body = json.dumps(obj).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self) -> None:  # noqa: N802 (stdlib name)
                if self.path.rstrip("/") not in ("/mcp", ""):
                    self._send_json(404, {"error": "not found"})
                    return
                if hub.caller_binding:
                    # loopback Host naming this port; Origin absent or the same origin.
                    ok, why = host_origin_ok(self.headers.get_all("Host"), self.headers.get_all("Origin"),
                                             hub.mcp_port)
                    if not ok:
                        if hub.caller_audit is not None:
                            hub.caller_audit.record("http-refused", reason=why)
                        self._send_json(403, {
                            "jsonrpc": "2.0", "id": None,
                            "error": {"code": OPERATOR_AUTH_ERROR_CODE, "message": f"request refused: {why}"},
                        })
                        return
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    request = json.loads(raw.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    self._send_json(400, {
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    })
                    return
                if not isinstance(request, dict):
                    # Batch / non-object bodies are not supported by this shell;
                    # refuse them up front so nothing can reach dispatch around
                    # the operator gate below.
                    self._send_json(400, {
                        "jsonrpc": "2.0", "id": None,
                        "error": {"code": -32600, "message": "invalid request: expected one JSON-RPC object"},
                    })
                    return
                if hub.caller_binding:
                    # the transport ESTABLISHES the principal; dispatch decides.
                    response = service.handle_jsonrpc(request, principal=hub.principal_for_http(self.headers))
                    if response is None:
                        self.send_response(202)
                        self.end_headers()
                        return
                    self._send_json(200, response)
                    return
                if _requires_operator(request) and not hub.is_operator(self.headers.get("Authorization")):
                    # ⛔ WS2b: REJECT, never dispatch. Do not echo the header.
                    self._send_json(403, {
                        "jsonrpc": "2.0", "id": request.get("id"),
                        "error": {
                            "code": OPERATOR_AUTH_ERROR_CODE,
                            "message": (
                                "operator credential required: actuation tools "
                                f"{sorted(OPERATOR_TOOLS)} need 'Authorization: Bearer "
                                "<operator token>'. A harnessed agent self-actuates via "
                                "its local gnomon-self tools, not this hub."
                            ),
                        },
                    })
                    return
                # X26: the operator gate above is the hub's ONLY caller identification; an operator-gated
                # call that got here is the operator (viewer). Everything else has no known caller.
                if _requires_operator(request):
                    response = service.handle_jsonrpc(request, caller=VIEWER_ACTOR)
                else:
                    response = service.handle_jsonrpc(request)
                if response is None:
                    # A notification: acknowledge with 202 and no body.
                    self.send_response(202)
                    self.end_headers()
                    return
                self._send_json(200, response)

        self._http = ThreadingHTTPServer((self._host, port), _Handler)
        self._write_token_file()
        bound_host, bound_port = self._http.server_address[:2]
        self._http_thread = threading.Thread(
            target=self._http.serve_forever, name="gm-hub-mcp-http", daemon=True
        )
        self._http_thread.start()
        return str(bound_host), int(bound_port)

    @property
    def mcp_port(self) -> Optional[int]:
        if self._http is None:
            return None
        return int(self._http.server_address[1])

    @property
    def mcp_url(self) -> Optional[str]:
        if self._http is None:
            return None
        host, port = self._http.server_address[:2]
        return f"http://{host}:{port}/mcp"

    # -- rendezvous TCP listener ---------------------------------------------

    def start_rendezvous(self, port: int = 0, backlog: int = 16) -> Tuple[str, int]:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, port))
        sock.listen(backlog)
        sock.settimeout(0.5)  # so the accept loop can notice the stop event
        self._rv_sock = sock
        self._rv_stop.clear()
        self._rv_thread = threading.Thread(
            target=self._accept_loop, name="gm-hub-rendezvous", daemon=True
        )
        self._rv_thread.start()
        host, bound_port = sock.getsockname()
        self._publish_rendezvous()
        return str(host), int(bound_port)

    @property
    def rendezvous_address(self) -> Optional[Tuple[str, int]]:
        if self._rv_sock is None:
            return None
        host, port = self._rv_sock.getsockname()
        return str(host), int(port)

    def _accept_loop(self) -> None:
        assert self._rv_sock is not None
        while not self._rv_stop.is_set():
            try:
                conn, _addr = self._rv_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(
                target=self.handle_connection, args=(conn,), daemon=True
            ).start()

    def handle_connection(self, conn: socket.socket) -> None:
        """Serve one proxy connection: a ``register`` frame, then status frames.

        Exposed (not private) so the rendezvous protocol is unit-testable with a
        plain ``socketpair`` without spinning the accept loop.
        """
        instance_id: Optional[str] = None
        # D15 amendment: the downlink THIS connection registered, so teardown only
        # unregisters its own registration (never a newer re-registration).
        state: Dict[str, Any] = {"downlink": None}
        buf = b""
        try:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    instance_id = self._handle_frame(conn, line, instance_id, state)
        finally:
            if instance_id is not None:
                mine = state.get("downlink")
                removed = self.registry.unregister(instance_id, mine)
                if removed and self._caller_binding:
                    self._drop_binding(instance_id)  # unregistering revokes the holder's credential
                with self._parked_lock:
                    lst = self._parked.get(instance_id, [])
                    if mine in lst:
                        lst.remove(mine)
                # FIX PASS 3 (final re-audit: two rogues could pin the legit harness
                # — vacated went to ONE parked link, a rogue that ignores it): on ANY
                # close, if the instance now has NO holder, tell EVERY parked link.
                # The first reclaim wins; later reclaims re-park (see register).
                if removed or not self.registry.is_registered(instance_id):
                    self._send_vacated(instance_id)

    def _send_vacated(self, instance_id: str) -> None:
        """No holder: tell EVERY parked connection for ``instance_id`` (FIX PASS 3).
        Entries stay parked until they re-register or close; dead ones are dropped."""
        with self._parked_lock:
            candidates = list(self._parked.get(instance_id) or [])
        if not candidates:
            return
        told = 0
        for candidate in candidates:
            try:
                candidate.send({"cmd": "vacated", "instance_id": instance_id,
                                "hub_instance_id": self.hub_instance_id})
                told += 1
            except OSError:
                with self._parked_lock:
                    lst = self._parked.get(instance_id, [])
                    if candidate in lst:
                        lst.remove(candidate)
        sys.stderr.write(f"[gm-hub] instance {instance_id!r} VACATED — told {told} parked "
                         "harness(es); the first to re-register wins\n")

    def _is_holder(self, instance_id: str, state: Optional[Dict[str, Any]]) -> bool:
        if state is None:
            return True  # legacy direct callers
        try:
            return self.registry.get(instance_id).downlink is state.get("downlink")
        except KeyError:
            return False

    def _handle_frame(self, conn: socket.socket, line: bytes, instance_id: Optional[str],
                      state: Optional[Dict[str, Any]] = None) -> Optional[str]:
        try:
            frame = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return instance_id
        if not isinstance(frame, dict):
            return instance_id
        ftype = frame.get("type")
        if ftype == "register":
            new_id = frame.get("instance_id")
            if isinstance(new_id, str) and new_id:
                # FIX PASS 2: one downlink object per connection (a parked link that
                # re-registers on the SAME socket keeps its identity).
                downlink = (state.get("downlink") if state is not None and state.get("downlink")
                            is not None else SocketDownlink(conn))
                meta = frame.get("meta") if isinstance(frame.get("meta"), dict) else {}
                # FIX PASS 3: a RECLAIM (a parked link answering vacated) while someone
                # already holds the id loses the race: it is re-parked + told
                # superseded, never takes over (first reclaim wins, no ping-pong).
                if meta.get("reclaimed"):
                    try:
                        holder = self.registry.get(new_id).downlink
                    except KeyError:
                        holder = None
                    if holder is not None and holder is not downlink:
                        if state is not None:
                            state["downlink"] = downlink
                        with self._parked_lock:
                            lst = self._parked.setdefault(new_id, [])
                            if downlink not in lst:
                                lst.append(downlink)
                        try:
                            downlink.send({"cmd": "superseded", "instance_id": new_id,
                                           "hub_instance_id": self.hub_instance_id,
                                           "by": "reclaim-race"})
                        except OSError:
                            pass
                        return new_id
                presented: Optional[str] = None
                if self._caller_binding:
                    presented = frame.get(REGISTER_HASH_KEY)
                    if not is_wellformed_hash(presented):
                        presented = None
                    admitted, why, via_record = self._binding_admits(new_id, downlink, presented)
                    if not admitted:
                        self._park_refused(new_id, downlink, why, meta, state)
                        return new_id
                    if presented and self._hash_held_elsewhere(new_id, presented):
                        # a hash already bound to ANOTHER live instance is never bound twice: the registration
                        # stands (injects still route) but its calls stay unbound; audited.
                        if self.caller_audit is not None:
                            self.caller_audit.record("register-hash-reused", instance_id=new_id,
                                                     reason="caller hash already bound to another instance")
                        presented = None
                with self._parked_lock:
                    lst = self._parked.get(new_id, [])
                    if downlink in lst:
                        lst.remove(downlink)
                # FIX PASS (audit LOW-MED): a SECOND harness registering the same
                # instance_id supersedes the first. Tell the old one, so it knows
                # it is no longer reachable (it logs loudly and stops competing
                # for this hub instance) instead of silently going dark.
                try:
                    previous = self.registry.get(new_id).downlink
                except KeyError:
                    previous = None
                # F3: a fresh per-registration secret, held in the registry and sent ONLY down this
                # connection in hub_hello (the harness keeps it in memory; mail-verb tickets are MACed with it).
                # Round 5 (Diamond P2-1): the hub cannot read a session's flag, but the register frame's harness-supplied meta says
                # whether agent_mail is on there (only a harness with mail ON sets it). With mail OFF nothing is minted or sent, so the
                # greeting frame and the registration are byte-identical to the build before mail.
                _meta = frame.get("meta") or {}
                mail_on = isinstance(_meta, dict) and _meta.get("agent_mail") is True
                session_secret = secrets.token_bytes(32) if mail_on else None
                if mail_on:
                    self.registry.register(new_id, downlink, meta=_meta, secret=session_secret)
                else:
                    self.registry.register(new_id, downlink, meta=frame.get("meta") or {})
                if self._caller_binding:
                    self._set_binding(new_id, presented)
                    if via_record and presented:
                        consume_pending_record(new_id, presented, self._pending_dir)  # one bind per record
                if state is not None:
                    state["downlink"] = downlink
                if previous is not None and previous is not downlink:
                    with self._parked_lock:
                        self._parked.setdefault(new_id, []).append(previous)
                    try:
                        previous.send({"cmd": "superseded", "instance_id": new_id,
                                       "hub_instance_id": self.hub_instance_id,
                                       "by": (frame.get("meta") or {}).get("session_dir")})
                    except (OSError, AttributeError):
                        pass
                    sys.stderr.write(f"[gm-hub] instance {new_id!r} RE-REGISTERED by another "
                                     "connection — the previous harness was told it is superseded\n")
                # FIX PASS (audit HIGH): greet every registration. The harness's
                # backoff resets only on a greeted link that stays up >= 30s.
                try:
                    hello = {"cmd": "hub_hello", "hub_instance_id": self.hub_instance_id}
                    if session_secret is not None:
                        hello["session_secret"] = session_secret.hex()
                    downlink.send(hello)
                except OSError:
                    pass
                # D15 amendment: every (re)register drains that instance's queue.
                self._drain_notices(new_id, downlink)
                return new_id
        elif ftype == "notice_ack" and instance_id is not None and self._is_holder(instance_id, state):
            # Only the registered owner's ack deletes; NoticeQueue validates ids.
            dsd_id = frame.get("dsd_id")
            if self._notice_queue is not None and isinstance(dsd_id, str):
                try:
                    if self._notice_queue.ack(instance_id, dsd_id):
                        sys.stderr.write(f"[gm-hub] DSD notice {dsd_id} ACKED by {instance_id} (queue file removed)\n")
                except (OSError, ValueError):
                    pass
        elif ftype == "status" and instance_id is not None and self._is_holder(instance_id, state):
            self.registry.report_status(
                instance_id,
                used_percentage=frame.get("used_percentage"),
                alive=frame.get("alive"),
            )
        elif ftype == "operator_fanout" and instance_id is not None:
            if self._caller_binding and not self._is_holder(instance_id, state):
                if self.caller_audit is not None:
                    self.caller_audit.record("fanout-refused", instance_id=instance_id,
                                             reason="operator_fanout from a connection that does not hold the id")
                return instance_id
            self.operator_fanout(conn, instance_id, frame)
        elif ftype == "delivery_report" and instance_id is not None and self._is_holder(instance_id, state):
            self.delivery_report(instance_id, frame)  #
        return instance_id

    def _binding_admits(self, new_id: str, downlink: Any,
                        presented: Optional[str]) -> "Tuple[bool, str, bool]":
        """Caller binding ON: may this connection become (or stay) the holder of ``new_id``?

        * a HELD instance (another connection holds it): only with the holder's own hash (a reconnect);
        * an UNHELD instance (first launch, hub restart, holder departed, a parked link answering ``vacated``):
          only with a valid pending-launch record for exactly (``new_id``, presented hash). A parked connection is
          therefore never promoted on its own, and a hash cannot bind an instance its record does not name.
        Returns ``(admitted, reason, via_record)``."""
        try:
            holder = self.registry.get(new_id).downlink
        except KeyError:
            holder = None
        held = self._binding_hash(new_id)
        if holder is not None and holder is not downlink:
            if presented and held and hmac.compare_digest(presented.encode("ascii"), held.encode("ascii")):
                return True, "reconnect with the holder's hash", False
            return False, ("re-register without the current holder's caller hash" if presented
                           else "re-register with no caller hash"), False
        if holder is not None and holder is downlink and presented and held and \
                hmac.compare_digest(presented.encode("ascii"), held.encode("ascii")):
            return True, "same connection, same hash", False
        if not presented:
            return False, "register with no caller hash (a pending-launch record is required)", False
        ok, why = check_pending_record(new_id, presented, directory=self._pending_dir)
        if ok:
            return True, "pending-launch record", True
        return False, why, False

    def _park_refused(self, new_id: str, downlink: Any, reason: str,
                      meta: Dict[str, Any], state: Optional[Dict[str, Any]]) -> None:
        """Refused registration: PARK it (no hello, no notices, no injects) and audit. Never a silent supersede."""
        if state is not None:
            state["downlink"] = downlink
        with self._parked_lock:
            lst = self._parked.setdefault(new_id, [])
            if downlink not in lst:
                lst.append(downlink)
        try:
            downlink.send({"cmd": "superseded", "instance_id": new_id, "hub_instance_id": self.hub_instance_id,
                           "by": "caller-binding"})
        except OSError:
            pass
        if self.caller_audit is not None:
            self.caller_audit.record(
                "register-parked", instance_id=new_id, reason=reason,
                session_dir=meta.get("session_dir") if isinstance(meta, dict) else None)

    # -- PTY-hooks WS-F: operator \all / \<instance> fan-out ----------------

    def operator_fanout(self, conn: Any, sender: str, frame: Dict[str, Any]) -> Dict[str, Any]:
        """Deliver an operator broadcast / DM typed in ``sender``'s terminal.

        ``target`` is ``"all"`` (every live registered instance, exactly once
        each — the registry's keys are unique — INCLUDING the sender) or one
        instance_id (unknown -> error, NOTHING injected). Each target gets
        ``{"cmd": "inject", "text": <wrapped>, "source": "operator-broadcast",
        "trigger": "all"|"dm", "from": sender}``; its proxy records it as
        ``source: operator-broadcast``. The sender gets one
        ``operator_feedback`` frame with the delivery result.

        ⚠ BOUNDARY (stated): the frame is trusted to come from the operator at
        ``sender``'s terminal because it arrives on that proxy's rendezvous
        socket — the same unauthenticated rendezvous the fix-pass recorded as a
        follow-up (a same-uid process could register and send one). The prefix
        check below stops unsigned text; it does not authenticate the sender.
        """
        target = frame.get("target")
        text = frame.get("text")
        result: Dict[str, Any] = {"delivered": [], "failed": [], "error": None}
        if not isinstance(target, str) or not target or not isinstance(text, str):
            result["error"] = "operator_fanout needs string 'target' and 'text'"
        elif len(text) > MAX_FANOUT_TEXT:
            result["error"] = f"message too long ({len(text)} > {MAX_FANOUT_TEXT} chars)"
        else:
            m = _FANOUT_PREFIX.match(text)
            if m is None or m.group("target") != target:
                result["error"] = f"message is not signed for target {target!r} (<NAME-to-{target}> or <NAME-to-{target}@YYYY-MM-DD HH:MM> prefix required)"
            elif target != "all" and not self.registry.is_registered(target):
                result["error"] = f"unknown instance {target!r} — nothing injected"
        if result["error"] is None:
            ids = self.registry.instance_ids() if target == "all" else [target]
            command = {"cmd": "inject", "text": text, "source": "operator-broadcast",
                       "trigger": "all" if target == "all" else "dm", "from": sender}
            # carry the sender's msg_id + the sha256 of the text AS THE HUB
            # RECEIVED IT (recomputed here, never trusted from the frame), so each
            # recipient's injection record and delivery check link to the operator log.
            msg_id = frame.get("msg_id")
            if isinstance(msg_id, str) and _MSG_ID_RE.fullmatch(msg_id):
                command["msg_id"] = msg_id
                command["sent_sha256"] = hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()
                result["msg_id"] = msg_id
                result["sha256"] = command["sent_sha256"]
            for iid in ids:
                try:
                    self.registry.route(iid, dict(command))
                    result["delivered"].append(iid)
                except Exception as exc:  # noqa: BLE001 - one dead proxy must not stop the rest
                    result["failed"].append({"instance_id": iid, "error": str(exc)})
        if result["error"] is not None:
            feedback = f"\\{target}: {result['error']}"
        else:
            feedback = (f"delivered to {len(result['delivered'])}: {', '.join(result['delivered']) or '-'}"
                        + (f"; FAILED: {result['failed']}" if result["failed"] else ""))
        fb: Dict[str, Any] = {"cmd": "operator_feedback", "text": feedback}
        if "msg_id" in result:  # structured fields for the sender's operator log
            fb.update({"msg_id": result["msg_id"], "sha256": result["sha256"], "target": target,
                       "delivered": list(result["delivered"]),
                       "failed": [f.get("instance_id") for f in result["failed"]]})
        try:
            SocketDownlink(conn).send(fb)
        except (OSError, AttributeError):
            pass
        return result

    # -- per-recipient delivery check -> operator warning ------------

    def delivery_report(self, reporter: str, frame: Dict[str, Any]) -> Dict[str, Any]:
        """A recipient harness reports what its TUI actually submitted for broadcast
        ``msg_id``. A ``mismatch`` warns the SENDING operator (``operator_feedback``
        routed to ``from``), naming every session that got a different text so far
        for that message. ``match`` / ``unverified`` are counted, never warned.
        Fields are validated; nothing from the frame reaches the terminal unchecked."""
        out: Dict[str, Any] = {"warned": False, "error": None}
        msg_id, sender, verdict = frame.get("msg_id"), frame.get("from"), frame.get("verdict")
        if not (isinstance(msg_id, str) and _MSG_ID_RE.fullmatch(msg_id)):
            out["error"] = "bad msg_id"
            return out
        if verdict not in ("match", "mismatch", "unverified", "held"):
            out["error"] = "bad verdict"
            return out
        if verdict == "held":
            return self._delivery_held(reporter, msg_id, sender, frame.get("why"), out)
        with self._delivery_lock:
            state = self._delivery_state
            entry = state.setdefault(msg_id, {"match": set(), "mismatch": set(), "unverified": set(),
                                              "held": set()})
            state.move_to_end(msg_id)
            entry.setdefault("held", set()).discard(reporter)   # released and checked: no longer held
            while len(state) > 256:
                state.popitem(last=False)
            if reporter in entry["mismatch"] or reporter in entry["match"] or reporter in entry["unverified"]:
                return out  # one report per recipient per message
            entry[verdict].add(reporter)
            if verdict != "mismatch":
                return out
            mismatched = sorted(entry["mismatch"])

        def _int(v: Any) -> Optional[int]:
            return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None

        dlen, slen = _int(frame.get("delivered_len")), _int(frame.get("sent_len"))
        names = ", ".join(mismatched)
        text = (f"WARNING: operator message {msg_id} did NOT arrive intact: {len(mismatched)} "
                f"session(s) got a DIFFERENT text: {names} (latest: {reporter} got "
                f"{dlen if dlen is not None else '?'} of {slen if slen is not None else '?'} chars). "
                "The full text is in the operator-messages log.")
        sys.stderr.write(f"[gm-hub] DELIVERY MISMATCH msg {msg_id}: {names}\n")
        if isinstance(sender, str) and self.registry.is_registered(sender):
            try:
                self.registry.route(sender, {"cmd": "operator_feedback", "text": text, "msg_id": msg_id,
                                             "delivery_mismatch": mismatched})
                out["warned"] = True
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"warning not routed: {exc}"
        else:
            out["error"] = "sender not registered; warning on hub stderr only"
        return out

    def _delivery_held(self, reporter: str, msg_id: str, sender: Any, why: Any, out: Dict[str, Any]) -> Dict[str, Any]:
        """Round 3 P2-2 (g79): a recipient HELD the broadcast (operator line busy / a dialog open). The hub's
        first feedback ("delivered to N") only means SENT; this tells the SENDER which sessions are holding
        it. Text is built here from validated fields only."""
        with self._delivery_lock:
            state = self._delivery_state
            entry = state.setdefault(msg_id, {"match": set(), "mismatch": set(), "unverified": set(),
                                              "held": set()})
            state.move_to_end(msg_id)
            while len(state) > 256:
                state.popitem(last=False)
            held = entry.setdefault("held", set())
            if reporter in held:
                return out
            held.add(reporter)
            names = sorted(held)
        reason = "a dialog is open" if why == "dialog" else "operator line busy"
        text = (f"held at {len(names)} ({reason}): {', '.join(names)}. Operator message {msg_id} was sent "
                "but is NOT typed there yet; it is delivered when that operator's line is empty.")
        out["held"] = names
        if isinstance(sender, str) and self.registry.is_registered(sender):
            try:
                self.registry.route(sender, {"cmd": "operator_feedback", "text": text, "msg_id": msg_id,
                                             "delivery_held": names})
                out["warned"] = True
            except Exception as exc:  # noqa: BLE001
                out["error"] = f"held notice not routed: {exc}"
        else:
            out["error"] = "sender not registered; held notice dropped"
        return out

    # -- lifecycle -----------------------------------------------------------

    def start(self, mcp_port: int = 0, rendezvous_port: int = 0) -> Dict[str, Any]:
        mcp_host, mcp_bound = self.start_mcp_http(mcp_port)
        rv_host, rv_bound = self.start_rendezvous(rendezvous_port)
        self._publish_rendezvous()  # again, now that mcp_url is known too
        return {
            "mcp_url": self.mcp_url,
            "mcp_address": (mcp_host, mcp_bound),
            "rendezvous_address": (rv_host, rv_bound),
        }

    def stop(self) -> None:
        # ⚠ IN-FLIGHT CONNECTIONS (slice-2a cold audit): per-proxy connection
        # sockets are served on their own daemon threads by handle_connection and
        # are NOT force-closed here. Closing the listen socket + setting _rv_stop
        # stops NEW accepts, but an already-accepted connection's recv keeps its
        # thread alive until the proxy disconnects (or the process — the threads
        # are daemon — exits). That is acceptable for v1 (a clean GUI close
        # follows process exit), but a later slice that needs deterministic
        # teardown must TRACK live connection sockets and shutdown()/close() each
        # here so their recv loops unblock immediately. Not tracked yet.
        self._rv_stop.set()
        # D15 amendment: withdraw discovery FIRST (only if the file is still ours),
        # so no harness reconnects to a hub that is going away.
        if self._rendezvous_file and self._rendezvous_published:
            try:
                remove_rendezvous_if_ours(self._rendezvous_file, self.hub_instance_id)
            except OSError:
                pass
            self._rendezvous_published = False
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
            self._remove_token_file()
        if self._rv_sock is not None:
            try:
                self._rv_sock.close()
            except OSError:
                pass
            self._rv_sock = None
        if self._rv_thread is not None:
            self._rv_thread.join(timeout=2.0)
            self._rv_thread = None
        if self._http_thread is not None:
            self._http_thread.join(timeout=2.0)
            self._http_thread = None

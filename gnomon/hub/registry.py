"""The live session registry (the rendezvous state).

Maps ``instance_id`` -> a registered proxy connection. It carries two directions:

* DOWN — :meth:`SessionRegistry.route` writes an actuation command
  (``{"cmd": "inject"|"compact"|"context", ...}``) to the proxy's control
  channel. This is the SAME newline-delimited JSON vocabulary the slice-1
  :class:`~gnomon.harness.control.ControlChannel` already decodes, so the
  hub->proxy path needs no new vocabulary.
* UP — :meth:`SessionRegistry.report_status` records the proxy's last-known
  ``used_percentage`` / liveness, which the ``context`` MCP tool reads without
  blocking on a round trip.

A *downlink* is any object with ``send(command: dict) -> None``. The real one,
:class:`SocketDownlink`, writes a JSON line to the proxy's socket;
:class:`RecordingDownlink` captures for tests.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol


class Downlink(Protocol):
    def send(self, command: Dict[str, Any]) -> None: ...


class SocketDownlink:
    """Writes newline-delimited JSON commands down a proxy's socket."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        # PTY-hooks WS-F: a fan-out thread and an HTTP tool thread may send to
        # the same proxy concurrently; frames must never interleave on the wire.
        self._send_lock = threading.Lock()

    def send(self, command: Dict[str, Any]) -> None:
        payload = (json.dumps(command) + "\n").encode("utf-8")
        with self._send_lock:
            self._sock.sendall(payload)

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class RecordingDownlink:
    """A downlink that captures commands in memory (tests / GUI preview)."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []

    def send(self, command: Dict[str, Any]) -> None:
        self.sent.append(command)


@dataclass
class RegisteredSession:
    instance_id: str
    downlink: Downlink
    registered_at: float = field(default_factory=time.time)
    used_percentage: Optional[float] = None
    alive: bool = True
    last_status_at: Optional[float] = None
    meta: Dict[str, Any] = field(default_factory=dict)
    #: F3: this registration's per-session secret (in memory only; sent to that session's own proxy in
    #: hub_hello). Never in ``snapshot``, ``repr`` or any log.
    secret: bytes = field(default=b"", repr=False)


class UnknownSession(KeyError):
    """Raised when routing to an instance that is not registered."""


class SessionRegistry:
    """Thread-safe registry of live proxy connections."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._sessions: Dict[str, RegisteredSession] = {}
        self._lock = threading.RLock()
        self._clock = clock
        from ..mail.ticket import NonceCache

        self._nonces = NonceCache()

    # -- membership ----------------------------------------------------------

    def register(self, instance_id: str, downlink: Downlink, meta: Optional[Dict[str, Any]] = None,
                 secret: bytes = b"") -> RegisteredSession:
        with self._lock:
            session = RegisteredSession(
                instance_id=instance_id,
                downlink=downlink,
                registered_at=self._clock(),
                meta=dict(meta or {}),
                secret=bytes(secret or b""),
            )
            self._sessions[instance_id] = session
            return session

    def unregister(self, instance_id: str, downlink: Optional[Downlink] = None) -> bool:
        """Remove ``instance_id``. With ``downlink`` given, remove it ONLY if that
        is still the registered downlink (D15 amendment, 2026-09-29): a harness
        that RE-registers on a new connection must not be evicted when its OLD
        connection's teardown runs afterwards. Returns whether it was removed."""
        with self._lock:
            session = self._sessions.get(instance_id)
            if session is None:
                return False
            if downlink is not None and session.downlink is not downlink:
                return False
            del self._sessions[instance_id]
            return True

    def verify_ticket(self, ticket: Any, op: str, args: Any, now: Optional[float] = None) -> Optional[str]:
        """F3: the instance id a mail-verb ticket authenticates, or None. The identity is the ticket's
        own claim CHECKED against the secret this registry holds for that instance (a body-supplied identity is
        never used). Single-use, bound to ``op`` and ``args``, 30 s expiry; a re-registration (new secret)
        invalidates every older ticket. Never raises."""
        try:
            from ..mail import ticket as T

            parsed = T.parse(ticket)
            if parsed is None or not isinstance(op, str):
                return None
            with self._lock:
                session = self._sessions.get(parsed[0])
                secret = session.secret if session is not None else b""
                if not secret:
                    return None
                t = time.time() if now is None else now
                ok = T.verify(secret, ticket, op, T.args_digest(op, args), now=t)
                if ok is None:
                    return None
                if not self._nonces.consume(f"{ok[0]}:{ok[1]}", t):
                    return None
                return ok[0]
        except Exception:  # noqa: BLE001 - an authentication check fails closed, never raises
            return None

    def is_registered(self, instance_id: str) -> bool:
        with self._lock:
            return instance_id in self._sessions

    def instance_ids(self) -> List[str]:
        with self._lock:
            return sorted(self._sessions)

    def get(self, instance_id: str) -> RegisteredSession:
        with self._lock:
            try:
                return self._sessions[instance_id]
            except KeyError:
                raise UnknownSession(instance_id) from None

    # -- DOWN: route an actuation command to the proxy -----------------------

    def route(self, instance_id: str, command: Dict[str, Any]) -> None:
        """Send an actuation command down to the matching proxy."""
        with self._lock:
            session = self._sessions.get(instance_id)
            if session is None:
                raise UnknownSession(instance_id)
            downlink = session.downlink
        # Send outside the lock — a slow socket must not block the whole hub.
        downlink.send(command)

    # -- UP: record status the proxy reports ---------------------------------

    def report_status(
        self,
        instance_id: str,
        used_percentage: Optional[float] = None,
        alive: Optional[bool] = None,
    ) -> None:
        with self._lock:
            session = self._sessions.get(instance_id)
            if session is None:
                # Status for an unregistered instance is ignored, not an error —
                # a late status frame after unregister is benign.
                return
            if used_percentage is not None:
                session.used_percentage = used_percentage
            if alive is not None:
                session.alive = alive
            session.last_status_at = self._clock()

    def status(self, instance_id: str) -> Dict[str, Any]:
        with self._lock:
            try:
                session = self._sessions[instance_id]
            except KeyError:
                raise UnknownSession(instance_id) from None
            return {
                "instance_id": session.instance_id,
                "used_percentage": session.used_percentage,
                "alive": session.alive,
                "registered_at": session.registered_at,
                "last_status_at": session.last_status_at,
            }

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [
                {
                    "instance_id": s.instance_id,
                    "used_percentage": s.used_percentage,
                    "alive": s.alive,
                    "registered_at": s.registered_at,
                    "last_status_at": s.last_status_at,
                }
                for s in sorted(self._sessions.values(), key=lambda s: s.instance_id)
            ]

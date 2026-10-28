"""Proxy side of the rendezvous (the proxy connects).

The harness proxy opens ONE socket to the hub's rendezvous port, sends a
``register`` frame, and then that same socket is both:

* the DOWN channel — the hub writes ``{"cmd": ...}`` actuation lines, which the
  slice-1 :class:`~gnomon.harness.control.ControlChannel` decodes and
  drives into the child; and
* the UP channel — :meth:`ProxyHubConnection.send_status` writes ``status``
  frames the hub records (``used_percentage`` / liveness).

The control channel's own JSON responses also travel up this socket; the hub
ignores frames without a ``type``, so they are harmless.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict, Optional


class ProxyHubConnection:
    """A proxy's single socket to the hub. Register once, then duplex."""

    def __init__(self, host: str, port: int, instance_id: str) -> None:
        self._host = host
        self._port = port
        self._instance_id = instance_id
        self._sock: Optional[socket.socket] = None

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def sock(self) -> socket.socket:
        if self._sock is None:
            raise RuntimeError("not connected")
        return self._sock

    def fileno(self) -> int:
        return self.sock.fileno()

    def connect(self, meta: Optional[Dict[str, Any]] = None, timeout: Optional[float] = None,
                caller_hash: Optional[str] = None) -> None:
        # ``timeout`` bounds only the CONNECT (D15 amendment: the reconnecting
        # HubLink runs on the pump thread and must never hang on it); the socket
        # is blocking afterwards, exactly as before.
        sock = socket.create_connection((self._host, self._port), timeout=timeout)
        sock.settimeout(None)
        self._sock = sock
        frame: Dict[str, Any] = {"type": "register", "instance_id": self._instance_id, "meta": meta or {}}
        if caller_hash:
            # sha256 of this launch's caller token (never the token). Absent when the
            # harness minted none, so the frame is byte-identical to before.
            frame["caller_hash"] = caller_hash
        try:
            self._send_frame(frame)
        except BaseException:
            self.close()
            raise

    def send_status(self, used_percentage: Optional[float] = None, alive: bool = True) -> None:
        self._send_frame({
            "type": "status",
            "instance_id": self._instance_id,
            "used_percentage": used_percentage,
            "alive": alive,
        })

    def send_frame(self, frame: Dict[str, Any]) -> None:
        """Send one arbitrary UP frame (PTY-hooks WS-F: ``operator_fanout``)."""
        self._send_frame(frame)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _send_frame(self, frame: Dict[str, Any]) -> None:
        payload = (json.dumps(frame) + "\n").encode("utf-8")
        self.sock.sendall(payload)

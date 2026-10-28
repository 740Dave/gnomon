"""Per-session verb tickets (Diamond F3): how the Gnomon server learns WHICH agent is calling the mail broker.

The harness proxy of each session holds a per-registration SECRET that only the hub (server) and that proxy know:
the hub generates it at ``register`` and sends it down that session's own hub connection in the ``hub_hello`` frame;
the proxy keeps it in memory only (never written to a file, a log or the hub-frame sink). A mail verb asks its OWN
proxy over the per-session control socket (the one whose listener admits only this claude's process tree, exactly
as ``inject`` / ``memory`` identity works) for a ticket for ONE (op, args) call; the proxy mints it with its own
instance id. The server verifies the MAC with the secret it holds for the claimed instance, and the instance id in
the broker request body is NOT trusted (the body may not carry one at all).

    ticket = v1.<instance_id>.<expiry epoch>.<nonce>.<hex HMAC-SHA256(secret, "<iid>|<exp>|<nonce>|<op>|<args digest>")>

A ticket is bound to its op and args digest, expires in 30 s, and is single-use (the server remembers nonces).
Boundary, stated plainly (this is HYGIENE, NOT A BOUNDARY): the ticket stops one agent impersonating ANOTHER agent
through the mail verbs, and stops a forged request body. It does not stop any process running as the operator's user. An
agent's shell can mint a ticket for its own session over the per-session control socket (the same way it can call
``inject``); it can read the token file and call Gmail directly; and it can register on the hub's loopback port as
another instance id, which supersedes (and visibly parks) the real session and hands the intruder that id's fresh
secret, so it can then mint tickets as that agent until it leaves. Hub ``register`` is unauthenticated (pre-existing).

stdlib only; shared by the hub registry (verify) and the harness actuator (mint).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Dict, Optional, Tuple

VERSION = "v1"
TTL_S = 30.0
NONCE_CACHE_MAX = 4096
_IID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def args_digest(op: str, args: Any) -> str:
    """sha256 over the canonical JSON of {op, args}: what a ticket is bound to."""
    blob = json.dumps({"op": op, "args": args}, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      default=str).encode("ascii")
    return hashlib.sha256(blob).hexdigest()


def _mac(secret: bytes, iid: str, exp: str, nonce: str, op: str, digest: str) -> str:
    return hmac.new(secret, f"{iid}|{exp}|{nonce}|{op}|{digest}".encode("utf-8"), hashlib.sha256).hexdigest()


def mint(secret: bytes, instance_id: str, op: str, digest: str, *, now: Optional[float] = None,
         nonce: Optional[str] = None, ttl: float = TTL_S) -> str:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) < 16:
        raise ValueError("no usable session secret")
    if not _IID.match(instance_id or ""):
        raise ValueError("bad instance id")
    exp = repr(float((time.time() if now is None else now) + ttl))
    nonce = nonce or os.urandom(12).hex()
    return ".".join((VERSION, instance_id, exp, nonce, _mac(bytes(secret), instance_id, exp, nonce, op, digest)))


def parse(ticket: Any) -> Optional[Tuple[str, str, str, str]]:
    """(instance_id, exp, nonce, mac) of a well-formed ticket, else None."""
    if not isinstance(ticket, str) or len(ticket) > 400:
        return None
    parts = ticket.split(".")
    # exp is a float repr and contains a '.', so the ticket splits into 6 pieces: v1, iid, exp-int, exp-frac, nonce, mac
    if len(parts) != 6 or parts[0] != VERSION:
        return None
    iid, exp, nonce, mac = parts[1], parts[2] + "." + parts[3], parts[4], parts[5]
    if not _IID.match(iid) or not re.fullmatch(r"[0-9a-f]{8,64}", nonce) or not re.fullmatch(r"[0-9a-f]{64}", mac):
        return None
    if not re.fullmatch(r"[0-9]{1,14}\.[0-9]{1,12}(?:e[+-]?[0-9]{1,3})?", exp):
        return None
    return iid, exp, nonce, mac


def verify(secret: bytes, ticket: Any, op: str, digest: str, *, now: Optional[float] = None) -> Optional[Tuple[str, str]]:
    """(instance_id, nonce) when the MAC, op, digest and expiry all hold; else None. Does not consume the nonce."""
    p = parse(ticket)
    if p is None or not secret:
        return None
    iid, exp, nonce, mac = p
    if not hmac.compare_digest(mac, _mac(bytes(secret), iid, exp, nonce, op, digest)):
        return None
    t = time.time() if now is None else now
    try:
        e = float(exp)
    except ValueError:
        return None
    if t > e or e - t > TTL_S + 5:
        return None
    return iid, nonce


class NonceCache:
    """Single-use nonces, bounded (oldest dropped; an entry is only needed until its ticket expires)."""

    def __init__(self, maxsize: int = NONCE_CACHE_MAX) -> None:
        self._seen: Dict[str, float] = {}
        self._max = maxsize

    def consume(self, key: str, now: float) -> bool:
        for k in [k for k, exp in self._seen.items() if exp < now]:
            del self._seen[k]
        if key in self._seen:
            return False
        if len(self._seen) >= self._max:
            self._seen.pop(next(iter(self._seen)))
        self._seen[key] = now + TTL_S + 10
        return True

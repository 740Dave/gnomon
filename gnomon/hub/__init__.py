"""Gnomon hub — the localhost rendezvous + MCP server.

Two roles in ONE co-hostable process:

1. **Control-channel rendezvous.** Each harness proxy connects on start and
   registers its ``instance_id``; that connection carries injects DOWN (to the
   matching proxy) and status UP (context %, liveness). Owned by
   :class:`~gnomon.hub.registry.SessionRegistry`.

2. **Gnomon MCP server.** The agent-facing tool surface — ``inject``,
   ``request_compact``, ``context``, ``send_dsd_for_human_review`` — dispatched
   by :class:`~gnomon.hub.mcp.GmMCPService` and mapped onto the hub->proxy
   route. One server owns BOTH surfaces (D10).

:class:`~gnomon.hub.server.Hub` co-hosts both on localhost so a later CTk
GUI process can construct it and share the live registry in memory.
"""

from __future__ import annotations

from .mcp import DSDStore, GmMCPService, TOOLS, ALL_TOOLS
from .apps import AppLifecycle, AppRegistry, APP_LIFECYCLE_TOOLS
from .registry import RegisteredSession, SessionRegistry, SocketDownlink
from .server import Hub

__all__ = [
    "ALL_TOOLS",
    "APP_LIFECYCLE_TOOLS",
    "AppLifecycle",
    "AppRegistry",
    "DSDStore",
    "Hub",
    "RegisteredSession",
    "SessionRegistry",
    "SocketDownlink",
    "TOOLS",
    "GmMCPService",
]

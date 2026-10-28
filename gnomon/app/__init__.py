"""Gnomon app package — the CTk-free Controller layer.

The CustomTkinter view was removed 2026-09-27 (superseded by the web ZWD under
``web/``). What remains is the display-independent MVC logic reused by the web
backend:

* :class:`~gnomon.app.controller.Controller` — ALL app logic, no GUI
  dependency (list / launch / liveness / hub-lifecycle).
* ``config_controller`` / ``dsd_controller`` — the CTk-free controllers the web
  API drives.
"""

from __future__ import annotations

from .controller import Controller, LiveSessionView, default_sessions_root

__all__ = ["Controller", "LiveSessionView", "default_sessions_root"]

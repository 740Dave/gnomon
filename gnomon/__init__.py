"""Gnomon — a launcher for agent sessions.

Slices shipped:

* slice 1 — the PTY harness core (``gnomon.harness``).
* slice 2a — the HEADLESS functional core: the launch pipeline
  (``gnomon.profile`` / ``gnomon.launch`` / ``gnomon.terminal``),
  per-session liveness (``gnomon.liveness``), and the control/MCP hub
  (``gnomon.hub``). NO GUI — the CustomTkinter app is slice 2b.
"""

__version__ = "0.2.0"

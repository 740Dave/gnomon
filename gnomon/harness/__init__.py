"""Gnomon PTY harness core (slice 1).

A standalone, importable, testable re-implementation of the load-bearing PTY
harness behaviour specified in the harness spec. Clean-room
Python + stdlib only; nothing here imports earlier tooling — the cited
source files are the requirements source, not a dependency.

This slice is harness-core ONLY: no GUI, no MCP server, no tabs. The control
channel is a byte-stream fd standing in for the localhost MCP/HTTP rendezvous a
later slice supplies; the actuation vocabulary it carries is stable across that
swap.

Public surface
--------------
Spawn        : ``PtySpawner``, ``build_claude_args``, ``sanitize_env``, ``SpawnResult``
Write site   : ``SingleWriteSite``, ``InjectionRecord``
Injection    : ``Injector``
Raw mode     : ``RawMode``
Monitor      : ``ContextMonitor``, ``Notice``, ``NoticeKind``
read_context : ``JsonlTranscriptContextReader``, ``StatuslineHookContextReader``
Actuation    : ``Actuator``, ``ActuationError``
Control      : ``ControlChannel``
Pump         : ``HarnessProxy``
Constants    : ``ESC_ESC``, ``SUBMIT``, ``BRACKETED_PASTE_START/END``, thresholds...
"""

from __future__ import annotations

from .actuator import ActuationError, Actuator
from .constants import (
    ACTIONABLE_THRESHOLD,
    BACKSTOP_THRESHOLD,
    BRACKETED_PASTE_END,
    BRACKETED_PASTE_START,
    DEFAULT_BEAT_SECONDS,
    DEFAULT_CONTEXT_WINDOW_TOKENS,
    ESC_ESC,
    HOSTED_CHILD_THRESHOLDS,
    SUBMIT,
)
from .context_readers import JsonlTranscriptContextReader, StatuslineHookContextReader
from .control import ControlChannel
from .injector import Injector
from .launcher import assemble_proxy, read_launch_config
from .monitor import ContextMonitor, Notice, NoticeKind
from .proxy import HarnessProxy
from .rawmode import RawMode
from .spawn import PtySpawner, SpawnResult, build_claude_args, sanitize_env
from .writer import InjectionRecord, SingleWriteSite

__all__ = [
    "ActuationError",
    "Actuator",
    "assemble_proxy",
    "read_launch_config",
    "ACTIONABLE_THRESHOLD",
    "BACKSTOP_THRESHOLD",
    "BRACKETED_PASTE_END",
    "BRACKETED_PASTE_START",
    "ContextMonitor",
    "ControlChannel",
    "DEFAULT_BEAT_SECONDS",
    "DEFAULT_CONTEXT_WINDOW_TOKENS",
    "ESC_ESC",
    "HarnessProxy",
    "HOSTED_CHILD_THRESHOLDS",
    "InjectionRecord",
    "Injector",
    "JsonlTranscriptContextReader",
    "Notice",
    "NoticeKind",
    "PtySpawner",
    "RawMode",
    "SUBMIT",
    "SingleWriteSite",
    "SpawnResult",
    "StatuslineHookContextReader",
    "build_claude_args",
    "sanitize_env",
]

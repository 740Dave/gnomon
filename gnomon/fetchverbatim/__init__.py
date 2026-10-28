"""Guarded verbatim web fetch. Standalone module: see tool.py for the entry point and the deferred wiring."""
from .guard import FetchError, GuardRefusal, classify_address, validate_url  # noqa: F401
from .fetcher import Fetcher, Limits  # noqa: F401
from .tool import FLAG, TOOL_NAME, FetchVerbatim, allowlist_authorizer, build_tool, deny_all, tool_definition  # noqa: F401

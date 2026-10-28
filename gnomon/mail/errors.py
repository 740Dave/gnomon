"""Mail errors. ``MailRefused`` = a policy refusal (decision ``refused``); ``MailError`` = anything else."""

from __future__ import annotations


class MailError(Exception):
    """A failure (backend, config, token). The message never carries a token or a message body."""


class MailRefused(MailError):
    """A deliberate policy refusal (label scope, allow-list, limits). ``reason`` is a short stable code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class ScopeError(MailError):
    """The granted OAuth scope set is not exactly the allowed set."""


class TokenPermissionError(MailError):
    """The token file is not private (0600, owned by this uid, a regular file)."""

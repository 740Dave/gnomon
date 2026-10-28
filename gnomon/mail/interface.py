"""The ONLY mail-backend surface the verbs may use.

There is deliberately NO send method here (no ``send_message``, no ``send_draft``): the verbs are built
against this ABC, so a verb cannot send even by mistake. ``tests/test_x53_mail.py`` pins the method set and
checks that no name containing "send" exists on it, and that ``service`` only ever calls these methods.

A message is a NORMALISED dict (the backend does the Gmail-payload parsing):
    {"id", "threadId", "labelIds": [...], "headers": {lowercase-name: value}, "snippet", "text"}
``text`` is only filled when ``full=True``.
"""

from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional

#: The headers the backend must return (lowercase keys).
HEADERS_WANTED = ("from", "to", "cc", "subject", "date", "message-id", "references", "in-reply-to", "reply-to")

#: The complete public method set of a backend (pinned by a test).
BACKEND_METHODS = frozenset({
    "profile_email", "find_label_id", "list_message_ids", "get_message", "create_draft", "insert_message",
})


class MailBackend(abc.ABC):
    @abc.abstractmethod
    def profile_email(self) -> str:
        """The address of the connected mailbox (users.getProfile; readonly scope)."""

    @abc.abstractmethod
    def find_label_id(self, name: str) -> Optional[str]:
        """The id of the label with EXACTLY this name, or None (never a fuzzy match)."""

    @abc.abstractmethod
    def list_message_ids(self, label_id: str, max_results: int) -> List[Dict[str, str]]:
        """Newest-first ``[{"id", "threadId"}]`` of messages carrying ``label_id`` (at most ``max_results``)."""

    @abc.abstractmethod
    def get_message(self, message_id: str, full: bool = False) -> Dict[str, Any]:
        """One normalised message (headers always; ``text`` only when ``full``)."""

    @abc.abstractmethod
    def create_draft(self, raw: bytes, thread_id: Optional[str] = None) -> Dict[str, Any]:
        """Create a Gmail DRAFT from an RFC 5322 message. Returns ``{"id", "message_id", "thread_id"}``."""

    @abc.abstractmethod
    def insert_message(self, raw: bytes, label_ids: List[str]) -> Dict[str, Any]:
        """users.messages.insert into the connected mailbox (nothing is sent). Returns ``{"id", "thread_id"}``."""

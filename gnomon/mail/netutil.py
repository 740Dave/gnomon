"""Shared transport helpers for the mail package (stdlib only)."""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request
from typing import Any

from .errors import MailError


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would resend the Authorization bearer to the redirect target (Diamond F13).
    A 3xx surfaces as an HTTPError, which the callers turn into a MailError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def open_no_redirect(req: urllib.request.Request, timeout: float):
    return urllib.request.build_opener(NoRedirect()).open(req, timeout=timeout)


#: Everything a urllib / http.client transport can raise that is NOT already a MailError. Callers turn these into
#: MailError so a transport failure can never escape as a surprising exception type (Diamond F1).
TRANSPORT_ERRORS = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, UnicodeError)


def describe(exc: BaseException) -> str:
    return type(exc).__name__

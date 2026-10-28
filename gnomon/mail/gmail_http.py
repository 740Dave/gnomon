"""The Gmail REST implementation of ``MailBackend`` over plain HTTPS (``urllib``; stdlib only).

google-api-python-client is NOT installed on this machine (``import googleapiclient`` fails) and the mail
tool needs six endpoints, so this is a thin urllib client with an OAuth bearer. The bearer is fetched per
request from ``token_provider`` (the server's ``TokenManager.access_token``) and never logged or put in an
error message.

HARD ALLOW-LIST (defence in depth under the interface): ``_request`` refuses, BEFORE any network call, every
(method, path) not in ``ALLOWED``. ``.../messages/send``, ``.../drafts/send`` and ``.../messages/import`` are
not in it, so even a future caller of ``_request`` cannot send.
"""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from typing import Any, Callable, Dict, List, Optional

from . import netutil
from .errors import MailError
from .interface import HEADERS_WANTED, MailBackend

BASE = "https://gmail.googleapis.com"
#: A message id: a short token that is NOT a Gmail method name (F9: GET .../messages/send is harmless but the
#: allow-list must not read as if send were reachable).
_ID = r"(?!(?i:send|import|trash|untrash|modify|batchmodify|batchdelete)\Z)[A-Za-z0-9_-]{1,64}"
#: The Gmail API root for the authenticated mailbox: the one place the external API path is spelled (every request
#: path and the ALLOWED table derive from it). Plain spelling on purpose: the neutrality gate forbids spelling around a token.
_ME = "/gmail/v1/users/me"
ALLOWED = (
    ("GET", re.compile(r"\A" + _ME + r"/profile\Z")),
    ("GET", re.compile(r"\A" + _ME + r"/labels\Z")),
    ("GET", re.compile(r"\A" + _ME + r"/messages\Z")),
    ("GET", re.compile(r"\A" + _ME + r"/messages/" + _ID + r"\Z")),
    ("POST", re.compile(r"\A" + _ME + r"/drafts\Z")),
    ("POST", re.compile(r"\A" + _ME + r"/messages\Z")),  # users.messages.insert (NOT /send, NOT /import)
)
TIMEOUT_S = 20.0
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
TEXT_CAP = 100_000
_ID_RE = re.compile(r"\A" + _ID + r"\Z")


class _Strip(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in ("br", "p", "div", "tr", "li"):
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Strip()
    try:
        p.feed(html)
    except Exception:  # noqa: BLE001 - a malformed page degrades to what was parsed
        pass
    return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()


def _decode_part(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")
    except (ValueError, TypeError):
        return ""


def extract_text(payload: Dict[str, Any]) -> str:
    """Prefer text/plain anywhere in the MIME tree; else strip text/html."""
    plain: List[str] = []
    html: List[str] = []

    def walk(part: Dict[str, Any], depth: int) -> None:
        if depth > 12 or not isinstance(part, dict):
            return
        mt = str(part.get("mimeType", "")).lower()
        data = (part.get("body") or {}).get("data")
        if data and mt == "text/plain":
            plain.append(_decode_part(data))
        elif data and mt == "text/html":
            html.append(_decode_part(data))
        for sub in part.get("parts") or []:
            walk(sub, depth + 1)

    walk(payload, 0)
    text = "\n".join(plain) if plain else html_to_text("\n".join(html))
    return text[:TEXT_CAP]


def normalize_message(raw: Dict[str, Any], full: bool) -> Dict[str, Any]:
    payload = raw.get("payload") or {}
    headers: Dict[str, str] = {}
    for h in payload.get("headers") or []:
        name = str(h.get("name", "")).lower()
        if name in HEADERS_WANTED and name not in headers:
            headers[name] = str(h.get("value", ""))
    return {"id": str(raw.get("id", "")), "threadId": str(raw.get("threadId", "")),
            "labelIds": [str(x) for x in raw.get("labelIds") or []], "headers": headers,
            "snippet": str(raw.get("snippet", "")), "text": extract_text(payload) if full else ""}


Opener = Callable[[urllib.request.Request, float], Any]


def _default_opener(req: urllib.request.Request, timeout: float):
    return netutil.open_no_redirect(req, timeout)  # fixed https Gmail endpoint; never follows a redirect (F13)


class GmailHttpBackend(MailBackend):
    def __init__(self, token_provider: Callable[[], str], opener: Opener = _default_opener) -> None:
        self._token = token_provider
        self._open = opener

    # --- transport ---
    def _request(self, method: str, path: str, query: Optional[Dict[str, Any]] = None,
                 body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not any(m == method and rx.match(path) for m, rx in ALLOWED):
            raise MailError(f"gmail request refused by the allow-list: {method} {path}")
        url = BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Authorization": "Bearer " + self._token(), "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._open(req, TIMEOUT_S) as resp:
                raw = resp.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str((json.loads(exc.read().decode("utf-8")).get("error") or {}).get("message", ""))[:160]
            except Exception:  # noqa: BLE001
                pass
            raise MailError(f"gmail {method} {path} failed: HTTP {exc.code} {detail}".strip()) from None
        except netutil.TRANSPORT_ERRORS as exc:  # URLError, HTTPException (IncompleteRead...), timeout, OSError
            raise MailError(f"gmail {method} {path} network error: {netutil.describe(exc)}") from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MailError("gmail response too large")
        try:
            obj = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise MailError("gmail returned a non-JSON response") from None
        return obj if isinstance(obj, dict) else {}

    # --- MailBackend ---
    def profile_email(self) -> str:
        return str(self._request("GET", _ME + "/profile").get("emailAddress", "")).lower()

    def find_label_id(self, name: str) -> Optional[str]:
        for lab in self._request("GET", _ME + "/labels").get("labels") or []:
            if lab.get("name") == name:
                return str(lab.get("id"))
        return None

    def list_message_ids(self, label_id: str, max_results: int) -> List[Dict[str, str]]:
        out = self._request("GET", _ME + "/messages",
                            {"labelIds": [label_id], "maxResults": int(max_results)})
        return [{"id": str(m["id"]), "threadId": str(m.get("threadId", ""))} for m in out.get("messages") or []]

    def get_message(self, message_id: str, full: bool = False) -> Dict[str, Any]:
        if not isinstance(message_id, str) or not _ID_RE.match(message_id):
            raise MailError("malformed message id")
        q: Dict[str, Any] = {"format": "full"} if full else {
            "format": "metadata", "metadataHeaders": [h.title() if h != "message-id" else "Message-ID"
                                                     for h in HEADERS_WANTED]}
        return normalize_message(self._request("GET", f"{_ME}/messages/{message_id}", q), full)

    def create_draft(self, raw: bytes, thread_id: Optional[str] = None) -> Dict[str, Any]:
        msg: Dict[str, Any] = {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}
        if thread_id:
            msg["threadId"] = thread_id
        out = self._request("POST", _ME + "/drafts", body={"message": msg})
        m = out.get("message") or {}
        return {"id": str(out.get("id", "")), "message_id": str(m.get("id", "")), "thread_id": str(m.get("threadId", ""))}

    def insert_message(self, raw: bytes, label_ids: List[str]) -> Dict[str, Any]:
        out = self._request("POST", _ME + "/messages",
                            body={"raw": base64.urlsafe_b64encode(raw).decode("ascii"), "labelIds": list(label_ids)})
        return {"id": str(out.get("id", "")), "thread_id": str(out.get("threadId", ""))}

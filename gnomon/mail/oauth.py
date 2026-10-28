"""Installed-app OAuth for the mail tool: PKCE, a loopback redirect to the Gnomon server, EXACT scopes.

Allowed scope set (operator, V1 design + the coordinator's D2 reconciliation): EXACTLY
``gmail.compose`` + ``gmail.readonly`` + ``gmail.insert``. ``assert_scopes`` refuses ANY other set: a
token carrying ``gmail.send``, ``gmail.modify`` or the full ``mail.google.com`` scope is refused (and so is
a token MISSING one of the three). The check runs when a token is minted, on every refresh response that
reports a scope, and when a stored token is loaded.

⚠ Negative-claim bound, stated plainly: ``gmail.compose`` is a Google scope that itself PERMITS sending
(drafts.send / messages.send). "No send" in V1 is enforced by THIS CODE (the backend interface has no send
method and the HTTP layer's allow-list has no send path), not by the token. See RUN_SUMMARY-mail-v1.md.

All network access goes through the injected ``post_form`` (a fake in tests). No credential is created or
read by the build; the OAuth CLIENT (id/secret of a Desktop-app client the operator creates) is read from
``<DATA>/mail/oauth_client.json`` at connect time only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Optional, Tuple

from . import netutil
from .errors import MailError, ScopeError

SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_INSERT = "https://www.googleapis.com/auth/gmail.insert"
ALLOWED_SCOPES = frozenset({SCOPE_COMPOSE, SCOPE_READONLY, SCOPE_INSERT})
#: Named so the refusal message can say what it saw; the check itself is set EQUALITY, not this list.
KNOWN_DANGEROUS = {
    "https://www.googleapis.com/auth/gmail.send": "gmail.send",
    "https://mail.google.com/": "full mail access (mail.google.com)",
    "https://www.googleapis.com/auth/gmail.modify": "gmail.modify",
    "https://www.googleapis.com/auth/gmail.settings.basic": "gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing": "gmail.settings.sharing",
}

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
PENDING_TTL_S = 600.0
MAX_PENDING = 4
EXPIRY_SKEW_S = 60.0


def parse_scopes(granted: Any) -> frozenset:
    if isinstance(granted, (list, tuple, set, frozenset)):
        return frozenset(str(s) for s in granted)
    if not isinstance(granted, str):
        return frozenset()
    return frozenset(granted.split())


def assert_scopes(granted: Any) -> None:
    """Raise ``ScopeError`` unless ``granted`` is EXACTLY the allowed set."""
    got = parse_scopes(granted)
    if got == ALLOWED_SCOPES:
        return
    bad = sorted(KNOWN_DANGEROUS[s] for s in got if s in KNOWN_DANGEROUS)
    extra = sorted(got - ALLOWED_SCOPES)
    missing = sorted(ALLOWED_SCOPES - got)
    parts = []
    if bad:
        parts.append("carries forbidden scope(s): " + ", ".join(bad))
    if extra and not bad:
        parts.append("carries unexpected scope(s): " + ", ".join(extra))
    elif extra:
        others = [s for s in extra if s not in KNOWN_DANGEROUS]
        if others:
            parts.append("also unexpected: " + ", ".join(others))
    if missing:
        parts.append("missing required scope(s): " + ", ".join(m.rsplit("/", 1)[-1] for m in missing))
    raise ScopeError("token REFUSED, scope set must be exactly gmail.compose + gmail.readonly + gmail.insert; it "
                     + "; ".join(parts or ["is empty"]))


# --- the OAuth client (Desktop app) -------------------------------------------------------------------


def load_client(path: str) -> Dict[str, str]:
    """Read the operator's downloaded client JSON (``{"installed": {...}}`` or a flat object). Loud on a miss."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            obj = json.load(fh)
    except FileNotFoundError:
        raise MailError(f"no OAuth client file at {path!r}: follow docs/mail-v1-setup.md step 2") from None
    except (OSError, ValueError) as exc:
        raise MailError(f"OAuth client file {path!r} unreadable: {type(exc).__name__}") from None
    inner = obj.get("installed") if isinstance(obj, dict) and isinstance(obj.get("installed"), dict) else obj
    if not isinstance(inner, dict):
        raise MailError("OAuth client file must be a JSON object")
    cid, sec = inner.get("client_id"), inner.get("client_secret")
    if not isinstance(cid, str) or not cid or not isinstance(sec, str) or not sec:
        raise MailError("OAuth client file needs client_id and client_secret (a Desktop-app client)")
    return {"client_id": cid, "client_secret": sec}


# --- the HTTP seam ---------------------------------------------------------------------------------------


def default_post_form(url: str, data: Dict[str, str], timeout: float = 20.0) -> Tuple[int, Dict[str, Any]]:
    """POST a form; return ``(status, parsed JSON or {})``. Never includes the request in an error."""
    body = urllib.parse.urlencode(data).encode("ascii")
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        with netutil.open_no_redirect(req, timeout) as resp:  # fixed https Google endpoints; never follows a redirect
            raw, status = resp.read(), resp.status
    except urllib.error.HTTPError as exc:
        try:
            raw, status = exc.read(), exc.code
        except netutil.TRANSPORT_ERRORS:
            raw, status = b"", exc.code
    except netutil.TRANSPORT_ERRORS as exc:
        # URLError, http.client.HTTPException (BadStatusLine, IncompleteRead), timeouts, OSError: all become a
        # MailError, so no transport failure can escape as another type (Diamond F1).
        raise MailError(f"network error talking to {urllib.parse.urlparse(url).netloc}: {netutil.describe(exc)}") from None
    try:
        out = json.loads(raw.decode("utf-8") or "{}")
    except ValueError:
        return status, {}
    return status, out if isinstance(out, dict) else {}


PostForm = Callable[[str, Dict[str, str]], Tuple[int, Dict[str, Any]]]


# --- the flow ---------------------------------------------------------------------------------------------


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


class OAuthFlow:
    def __init__(self, client: Dict[str, str], redirect_uri: str, post_form: PostForm = default_post_form,
                 clock: Callable[[], float] = time.time) -> None:
        self.client = client
        self.redirect_uri = redirect_uri
        self.post_form = post_form
        self.clock = clock
        self._pending: Dict[str, Tuple[str, float]] = {}

    def start(self, login_hint: str = "") -> str:
        now = self.clock()
        self._pending = {k: v for k, v in self._pending.items() if now - v[1] < PENDING_TTL_S}
        if len(self._pending) >= MAX_PENDING:
            oldest = min(self._pending, key=lambda k: self._pending[k][1])
            self._pending.pop(oldest)
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(48)
        self._pending[state] = (verifier, now)
        q = {
            "client_id": self.client["client_id"], "redirect_uri": self.redirect_uri, "response_type": "code",
            "scope": " ".join(sorted(ALLOWED_SCOPES)), "access_type": "offline", "prompt": "consent",
            "include_granted_scopes": "false", "state": state,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode("ascii")).digest()),
            "code_challenge_method": "S256",
        }
        if login_hint:
            q["login_hint"] = login_hint
        return AUTH_URL + "?" + urllib.parse.urlencode(q)

    def _revoke_grant(self, resp: Dict[str, Any]) -> bool:
        """Best-effort revoke of a just-minted grant (refresh token, else access token). Never raises."""
        tok = resp.get("refresh_token") or resp.get("access_token")
        if not tok:
            return False
        try:
            status, _ = self.post_form(REVOKE_URL, {"token": str(tok)})
            return status == 200
        except Exception:  # noqa: BLE001 - the caller reports the failure
            return False

    def complete(self, state: str, code: str) -> Dict[str, Any]:
        """Exchange the code. Asserts the scope set BEFORE returning a token dict (nothing stored here)."""
        entry = self._pending.pop(state or "", None)
        if entry is None or self.clock() - entry[1] >= PENDING_TTL_S:
            raise MailError("unknown or expired consent state: start 'Connect mail' again")
        if not isinstance(code, str) or not code:
            raise MailError("no authorization code")
        status, resp = self.post_form(TOKEN_URL, {
            "code": code, "client_id": self.client["client_id"], "client_secret": self.client["client_secret"],
            "redirect_uri": self.redirect_uri, "grant_type": "authorization_code", "code_verifier": entry[0]})
        if status != 200 or not resp.get("access_token"):
            raise MailError(f"token exchange failed (HTTP {status}): {str(resp.get('error') or 'no access_token')[:80]}")
        try:
            assert_scopes(resp.get("scope", ""))
        except ScopeError as exc:
            # F4: the grant exists at Google already. Revoke it BEFORE discarding, as the wrong-mailbox path does.
            confirmed = self._revoke_grant(resp)
            raise ScopeError(str(exc) + ("; the grant was revoked at Google" if confirmed else
                                         "; revoking it at Google FAILED: remove the app at "
                                         "https://myaccount.google.com/permissions")) from None
        if not resp.get("refresh_token"):
            raise MailError("Google returned no refresh_token (revoke the app at myaccount.google.com/permissions "
                            "and connect again)")
        return {"refresh_token": resp["refresh_token"], "access_token": resp["access_token"],
                "expires_at": self.clock() + float(resp.get("expires_in", 3600)),
                "scope": " ".join(sorted(parse_scopes(resp.get("scope", "")))), "created_at": self.clock()}


class TokenManager:
    """Hands out a valid access token from the stored one, refreshing when due. Server-side only."""

    def __init__(self, store: Any, client_loader: Callable[[], Dict[str, str]],
                 post_form: PostForm = default_post_form, clock: Callable[[], float] = time.time) -> None:
        self.store, self.client_loader, self.post_form, self.clock = store, client_loader, post_form, clock

    def _load(self) -> Dict[str, Any]:
        tok = self.store.load()
        if not tok:
            raise MailError("mail is not connected (use 'Connect mail' on the dashboard)")
        assert_scopes(tok.get("scope", ""))  # a stored token with a bad scope set is refused on load
        return tok

    def connected(self) -> bool:
        try:
            self._load()
            return True
        except MailError:
            return False

    def access_token(self) -> str:
        tok = self._load()
        if tok.get("access_token") and self.clock() < float(tok.get("expires_at", 0)) - EXPIRY_SKEW_S:
            return str(tok["access_token"])
        client = self.client_loader()
        status, resp = self.post_form(TOKEN_URL, {
            "client_id": client["client_id"], "client_secret": client["client_secret"],
            "refresh_token": tok["refresh_token"], "grant_type": "refresh_token"})
        if status != 200 or not resp.get("access_token"):
            raise MailError(f"token refresh failed (HTTP {status}): {str(resp.get('error') or 'no access_token')[:80]}; "
                            "reconnect mail from the dashboard")
        if "scope" in resp:
            assert_scopes(resp["scope"])
        tok["access_token"] = resp["access_token"]
        tok["expires_at"] = self.clock() + float(resp.get("expires_in", 3600))
        self.store.save(tok)
        return str(tok["access_token"])

    def revoke_and_delete(self) -> Dict[str, Any]:
        """Revoke at Google, then ALWAYS delete the local file (Diamond F1, the operator's hard stop).

        The revoke runs inside ``try/except Exception`` and the delete runs in a ``finally``, so no failure of the
        load or the revoke (any exception type) can leave the token on disk. A revoke failure is REPORTED (the
        returned ``warning``), never a reason to keep the token. ``revoked`` says whether Google confirmed;
        ``ok`` is False only if the local delete itself failed."""
        revoked, notes = False, []
        deleted, delete_error = False, ""
        try:
            try:
                tok = self.store.load()
            except Exception as exc:  # noqa: BLE001 - an unreadable file is still deleted below
                tok = None
                notes.append(f"the token file could not be read ({type(exc).__name__}), so it could not be revoked")
            if tok and tok.get("refresh_token"):
                try:
                    status, _ = self.post_form(REVOKE_URL, {"token": str(tok["refresh_token"])})
                    revoked = status == 200
                    if not revoked:
                        notes.append(f"Google answered HTTP {status} to the revoke")
                except Exception as exc:  # noqa: BLE001 - ANY revoke failure is reported, never blocks the delete
                    notes.append(f"the revoke failed ({type(exc).__name__})")
            elif tok is None and not notes:
                notes.append("no token was stored")
        finally:
            try:
                deleted = self.store.delete()
            except Exception as exc:  # noqa: BLE001
                delete_error = f"{type(exc).__name__}: {exc}"
        out: Dict[str, Any] = {"ok": not delete_error, "revoked": revoked, "deleted": deleted}
        if delete_error:
            notes.append(f"the local token file could NOT be deleted ({delete_error}): delete it by hand")
        if notes or not revoked:
            out["warning"] = ("; ".join(notes) + "; " if notes else "") + (
                "the local token is deleted; if 'revoked' is false, also remove the app at "
                "https://myaccount.google.com/permissions" if not delete_error else
                "remove the app at https://myaccount.google.com/permissions")
        return out

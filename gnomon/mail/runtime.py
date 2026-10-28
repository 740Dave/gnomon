"""``MailRuntime``: what the Gnomon web server (and its tests) hold. One per server process.

Owns the mail state (token 0600, config 0600, activity log, OAuth client file) in ``mail_state_dir()``
(``~/Library/Application Support/Gnomon/mail``, 0700: OUTSIDE the cloud-synced DATA root, Diamond F2), the OAuth
flow, and the broker the harness verbs call (``verb``). The token never leaves this process: the harness's
self-verb process gets only the verb RESULT over loopback. The caller's identity is authenticated by a per-session
TICKET (``gnomon.mail.ticket``; Diamond F3), never taken from the request body.

A token left at the OLD location (``<DATA>/mail/token.json``) is REFUSED, never loaded: the operator is told to reconnect.

Test seams: ``post_form`` (the OAuth token endpoint), ``backend_factory`` (a fake ``MailBackend``),
``profile_fn`` (the connect-time mailbox check), ``ticket_verifier`` (the hub registry's ``verify_ticket``),
``clock``; ``state_dir`` / the ``GNOMON_MAIL_STATE_DIR`` env override are for tests only.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Any, Callable, Dict, Optional

from . import oauth
from .activity import ActivityLog
from .config import INSTANCE_RE, MailConfigStore, normalize_address
from .errors import MailError, ScopeError
from .gmail_http import GmailHttpBackend
from .interface import MailBackend
from .service import MailService
from .tokenstore import TokenStore

#: The CLOSED argument set of each broker op (a verb may not smuggle in an extra key such as ``label``/``from``).
VERB_ARGS = {
    "mail_list": frozenset({"max"}),
    "mail_read": frozenset({"id"}),
    "mail_draft": frozenset({"to", "subject", "body", "reply_to_message_id"}),
    "mail_to_operator": frozenset({"subject", "body"}),
}
CLIENT_FILENAME = "oauth_client.json"


ENV_STATE_DIR = "GNOMON_MAIL_STATE_DIR"  # test-only override


def mail_state_dir() -> str:
    """Where the mail state lives: ``~/Library/Application Support/Gnomon/mail`` (never the cloud-synced DATA
    root). ``GNOMON_MAIL_STATE_DIR`` overrides it, for tests only."""
    env = os.environ.get(ENV_STATE_DIR)
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "Gnomon", "mail")


def legacy_mail_dir(data_root: str) -> str:
    """The pre-fix location (``<DATA>/mail``): only ever checked for a leftover token, never used."""
    return os.path.join(os.path.abspath(os.path.expanduser(data_root)), "mail")


def _err(line: str) -> None:
    try:
        sys.stderr.write("[gm-mail] " + line + "\n")
    except Exception:  # noqa: BLE001
        pass


class MailRuntime:
    def __init__(self, data_root: Optional[str] = None, *, state_dir: Optional[str] = None,
                 post_form: oauth.PostForm = oauth.default_post_form,
                 backend_factory: Optional[Callable[[], MailBackend]] = None,
                 profile_fn: Optional[Callable[[str], str]] = None,
                 ticket_verifier: Optional[Callable[[Any, str, Any], Optional[str]]] = None,
                 clock: Callable[[], float] = time.time) -> None:
        self.dir = os.path.abspath(state_dir) if state_dir else mail_state_dir()
        legacy = legacy_mail_dir(data_root) if data_root else None
        self.legacy_dir = legacy if legacy and os.path.abspath(legacy) != self.dir else None
        self.clock = clock
        self.post_form = post_form
        self._ticket_verifier = ticket_verifier
        self._legacy_logged = False
        self.tokens = TokenStore(self.dir)
        self.config_store = MailConfigStore(self.dir)
        self.activity = ActivityLog(os.path.join(self.dir, "activity.jsonl"), clock)
        self.client_path = os.path.join(self.dir, CLIENT_FILENAME)
        self.manager = oauth.TokenManager(self.tokens, lambda: oauth.load_client(self.client_path), post_form, clock)
        self._backend_factory = backend_factory
        self._profile_fn = profile_fn
        self._flow: Optional[oauth.OAuthFlow] = None
        self._flow_redirect = ""
        self._service: Optional[MailService] = None

    # --- the service ---
    def service(self) -> MailService:
        if self._service is None:
            backend = self._backend_factory() if self._backend_factory else GmailHttpBackend(self.manager.access_token)
            self._service = MailService(backend, self.config_store, self.activity, self.clock)
        return self._service

    # --- status / config / activity (operator panel) ---
    def status(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"state_dir": self.dir, "client_file_present": os.path.isfile(self.client_path)}
        legacy = self._legacy_token()
        if legacy:
            out["legacy_token_found"] = legacy
            out["legacy_token_message"] = (
                "A mail token was found at the OLD location (" + legacy + ", inside the cloud-synced folder). It was "
                "NOT loaded and is not used. Press Disconnect to revoke and delete it, then press Connect mail to "
                "reconnect (the new token is kept outside cloud-sync).")
        try:
            cfg = self.config_store.load()
            out.update(account=cfg.account, label_prefix=cfg.label_prefix, allowlist_count=len(cfg.allowlist))
        except MailError as exc:
            out["config_error"] = str(exc)
        try:
            tok = self.tokens.load()
            out["connected"] = bool(tok)
            if tok:
                out["scopes"] = sorted(oauth.parse_scopes(tok.get("scope", "")))
                try:
                    oauth.assert_scopes(tok.get("scope", ""))
                    out["scopes_ok"] = True
                except ScopeError as exc:
                    out["scopes_ok"], out["scope_error"] = False, str(exc)
        except MailError as exc:
            out.update(connected=False, token_error=str(exc))
        return out

    def get_config(self) -> Dict[str, Any]:
        cfg = self.config_store.load()
        d = cfg.to_dict()
        d["derived"] = {"example_plus_address": cfg.plus_address("alpha-architect"),
                        "example_label": cfg.label_name("alpha-architect"),
                        "example_signature": cfg.signature("alpha-architect")}
        return d

    def save_config(self, data: Dict[str, Any]) -> Dict[str, Any]:
        self.config_store.save(data)
        return self.get_config()

    # --- the legacy (cloud-sync) token: refused, logged, reported ---
    def _legacy_token(self) -> Optional[str]:
        if not self.legacy_dir:
            return None
        p = os.path.join(self.legacy_dir, TokenStore.FILENAME)
        if not os.path.lexists(p):
            return None
        if not self._legacy_logged:
            self._legacy_logged = True
            _err(f"legacy mail token found at {p}: REFUSED (not loaded); the operator must reconnect (Disconnect, then Connect mail)")
            try:
                self.activity.record({"instance": "operator", "op": "legacy-token", "decision": "refused", "reason": "old-location"})
            except Exception:  # noqa: BLE001 - a log failure never hides the finding in the UI
                pass
        return p

    # --- connect / disconnect ---
    def connect_start(self, redirect_uri: str, login_hint: str = "") -> str:
        client = oauth.load_client(self.client_path)
        if self._flow is None or self._flow_redirect != redirect_uri:
            self._flow = oauth.OAuthFlow(client, redirect_uri, self.post_form, self.clock)
            self._flow_redirect = redirect_uri
        else:
            self._flow.client = client
        return self._flow.start(login_hint or self.config_store.load().account)

    def connect_complete(self, state: str, code: str) -> Dict[str, Any]:
        if self._flow is None:
            raise MailError("no consent in progress: press 'Connect mail' again")
        tok = self._flow.complete(state, code)  # asserts the exact scope set BEFORE returning
        cfg = self.config_store.load()
        seen = self._profile(tok["access_token"])
        if seen != normalize_address(cfg.account):
            # The token is NOT stored: a consent for the wrong Google account is not usable here.
            try:
                self.post_form(oauth.REVOKE_URL, {"token": tok["refresh_token"]})
            except MailError:
                pass
            raise MailError(f"connected mailbox is {seen!r} but the configured account is {cfg.account!r}: "
                            "token discarded and revoked; fix the account setting or consent with the right account")
        self.tokens.save(tok)
        self._service = None
        return {"ok": True, "account": seen, "scopes": sorted(oauth.parse_scopes(tok["scope"]))}

    def _profile(self, access_token: str) -> str:
        if self._profile_fn:
            return self._profile_fn(access_token).lower()
        return GmailHttpBackend(lambda: access_token).profile_email()

    def disconnect(self) -> Dict[str, Any]:
        """Revoke and ALWAYS delete (F1), also removing a token left at the legacy location. A revoke failure, for
        the current token or the legacy one, is REPORTED to the caller (the UI message) and logged (activity row +
        a stderr line), never a reason to keep a token."""
        out = self.manager.revoke_and_delete()
        if out.get("warning") and "no token was stored" in out["warning"]:
            out["warning"] = out["warning"].replace("no token was stored", "no token was stored at the current location")
        legacy = self._legacy_token()
        legacy_notes = []
        if legacy:
            removed, revoked, notes = self._remove_legacy(legacy)
            out["legacy_removed"], out["legacy_revoked"] = removed, revoked
            legacy_notes = notes
            if not removed:
                out["ok"] = False
            if not revoked or not removed:
                msg = (f"the LEGACY token at {legacy} (the one that sat in the cloud-synced folder): "
                       + ("; ".join(legacy_notes) if legacy_notes else "its revoke was not confirmed") + ". Its grant may "
                       "still be LIVE at Google: remove the app at https://myaccount.google.com/permissions")
                out["warning"] = (out["warning"] + "; " if out.get("warning") else "") + msg
        self._service = None
        self._flow = None
        try:
            unconfirmed = (not out.get("revoked")) or (legacy and not out.get("legacy_revoked"))
            self.activity.record({"instance": "operator", "op": "disconnect", "decision": "ok" if out.get("ok") else "failed",
                                  "reason": "revoke-not-confirmed" if unconfirmed else None})
        except Exception:  # noqa: BLE001
            pass
        _err(f"disconnect: deleted={out.get('deleted')} revoked={out.get('revoked')}"
             + (f" legacy_removed={out.get('legacy_removed')} legacy_revoked={out.get('legacy_revoked')}" if legacy else "")
             + (f" warning={out['warning']}" if out.get("warning") else ""))
        return out

    def _remove_legacy(self, path: str):
        """Best-effort revoke of the old token's grant (the file is read ONLY for that), then delete it.
        Returns (removed, revoked, notes); every failure is named, none blocks the delete."""
        notes = []
        revoked = False
        try:
            import json as _json

            with open(path, "r", encoding="utf-8") as fh:
                tok = _json.load(fh)
            if isinstance(tok, dict) and tok.get("refresh_token"):
                try:
                    status, _ = self.post_form(oauth.REVOKE_URL, {"token": str(tok["refresh_token"])})
                    revoked = status == 200
                    if not revoked:
                        notes.append(f"Google answered HTTP {status} to the revoke")
                except Exception as exc:  # noqa: BLE001 - reported, never blocks the delete
                    notes.append(f"the revoke failed ({type(exc).__name__})")
            else:
                notes.append("it held no refresh token to revoke")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"the file could not be read ({type(exc).__name__}), so it could not be revoked")
        removed = False
        try:
            os.unlink(path)
            removed = True
        except OSError as exc:
            notes.append(f"the file could NOT be deleted ({type(exc).__name__}): delete it by hand")
            _err(f"could not delete the legacy token {path}: {type(exc).__name__}")
        return removed, revoked, notes

    # --- the verb broker ---
    def verb(self, ticket: Any, op: Any, args: Any) -> Dict[str, Any]:
        """The broker entry: the caller is whoever the TICKET authenticates (never a name in the request). The
        op and args shape are checked first (so a malformed call never consumes a ticket)."""
        pre = self._check_call(op, args)
        if pre is not None:
            return pre
        verifier = self._ticket_verifier
        if not callable(verifier):
            return {"ok": False, "decision": "refused", "reason": "identity-unverified",
                    "error": "the session registry is not available, so the caller cannot be authenticated: refused"}
        try:
            instance = verifier(ticket, op, args)
        except Exception:  # noqa: BLE001
            instance = None
        if not instance:
            return {"ok": False, "decision": "refused", "reason": "identity-unverified",
                    "error": "the caller could not be authenticated (missing, expired, replayed or forged ticket)"}
        return self.verb_as(instance, op, args)

    @staticmethod
    def _check_call(op: Any, args: Any) -> Optional[Dict[str, Any]]:
        if op not in VERB_ARGS:
            return {"ok": False, "decision": "refused", "reason": "unknown-op", "error": f"unknown mail op {str(op)[:40]!r}"}
        if not isinstance(args, dict):
            return {"ok": False, "decision": "refused", "reason": "bad-args", "error": "args must be an object"}
        extra = sorted(set(args) - VERB_ARGS[op])
        if extra:
            return {"ok": False, "decision": "refused", "reason": "bad-args", "error": f"unknown argument(s): {extra}"}
        return None

    def verb_as(self, instance: Any, op: Any, args: Any) -> Dict[str, Any]:
        """Run an op for an ALREADY AUTHENTICATED instance (called by ``verb`` after the ticket check; never
        exposed over HTTP)."""
        if not isinstance(instance, str) or not INSTANCE_RE.match(instance):
            return {"ok": False, "decision": "refused", "reason": "bad-identity", "error": "invalid agent identity"}
        pre = self._check_call(op, args)
        if pre is not None:
            return pre
        self._legacy_token()
        svc = self.service()
        if op == "mail_list":
            return svc.list(instance, args.get("max"))
        if op == "mail_read":
            return svc.read(instance, args.get("id"))
        if op == "mail_draft":
            return svc.draft(instance, args.get("to"), args.get("subject"), args.get("body"),
                             args.get("reply_to_message_id"))
        return svc.to_operator(instance, args.get("subject"), args.get("body"))

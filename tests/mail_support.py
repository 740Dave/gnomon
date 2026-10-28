"""Shared fakes for the mail tests. NOTHING here touches Google or a real mailbox."""

import base64
import email
import email.policy
from typing import Any, Dict, List, Optional

from gnomon.mail.interface import MailBackend

ACCOUNT = "operator@example.test"
LBL_ARCH = "Label_arch"
LBL_B = "Label_b"


def msg(mid, labels, subject="Hello", sender="pat@example.test", thread="t-" , text="body-of-%s", **hdr):
    h = {"from": sender, "to": "operator+alpha-architect@example.test", "subject": subject, "date": "Fri, 03 Oct 2026 10:00:00 +0000",
         "message-id": f"<{mid}@mail.example.test>"}
    h.update({k.replace("_", "-"): v for k, v in hdr.items()})
    return {"id": mid, "threadId": thread + mid, "labelIds": list(labels), "headers": h, "snippet": "snip " + mid,
            "text": (text % mid) if "%s" in text else text}


class FakeBackend(MailBackend):
    """An in-memory Gmail. Records every call. Has NO send method (it implements only the interface)."""

    def __init__(self):
        self.labels = {"agents/alpha-architect": LBL_ARCH, "agents/beta-analyst": LBL_B}
        self.messages: Dict[str, Dict[str, Any]] = {}
        self.calls: List[tuple] = []
        self.drafts: List[Dict[str, Any]] = []
        self.inserted: List[Dict[str, Any]] = []
        self.email = ACCOUNT
        self.full_fetched: List[str] = []
        self.list_returns_extra: List[Dict[str, str]] = []
        self.drop_labels_on_full = False  # simulate the label being removed between the metadata and the full fetch

    def add(self, m):
        self.messages[m["id"]] = m
        return m

    def profile_email(self):
        self.calls.append(("profile_email",))
        return self.email

    def find_label_id(self, name):
        self.calls.append(("find_label_id", name))
        return self.labels.get(name)

    def list_message_ids(self, label_id, max_results):
        self.calls.append(("list_message_ids", label_id, max_results))
        ids = [{"id": m["id"], "threadId": m["threadId"]} for m in self.messages.values() if label_id in m["labelIds"]]
        return (self.list_returns_extra + ids)[:max_results + len(self.list_returns_extra)]

    def get_message(self, message_id, full=False):
        self.calls.append(("get_message", message_id, full))
        if message_id not in self.messages:
            from gnomon.mail.errors import MailError
            raise MailError("gmail GET failed: HTTP 404")
        m = dict(self.messages[message_id])
        if full:
            self.full_fetched.append(message_id)
            if self.drop_labels_on_full:
                m["labelIds"] = ["INBOX"]
        else:
            m["text"] = ""
        return m

    def create_draft(self, raw, thread_id=None):
        self.calls.append(("create_draft", thread_id))
        self.drafts.append({"raw": raw, "thread_id": thread_id, "parsed": email.message_from_bytes(raw, policy=email.policy.default)})
        return {"id": f"d{len(self.drafts)}", "message_id": f"dm{len(self.drafts)}", "thread_id": thread_id or "newthread"}

    def insert_message(self, raw, label_ids):
        self.calls.append(("insert_message", tuple(label_ids)))
        self.inserted.append({"raw": raw, "label_ids": list(label_ids), "parsed": email.message_from_bytes(raw, policy=email.policy.default)})
        return {"id": f"i{len(self.inserted)}", "thread_id": "ith"}


def standard_backend():
    b = FakeBackend()
    b.add(msg("m1", ["INBOX", LBL_ARCH], "First", sender="Pat <pat@example.test>", thread="th-"))
    b.add(msg("m2", ["INBOX", LBL_ARCH], "Second", references="<a@x> <b@x>"))
    b.add(msg("b1", ["INBOX", LBL_B], "For the analyst", text="SECRET-B-BODY"))
    b.add(msg("plain", ["INBOX"], "Private to the operator", text="SECRET-operator-BODY"))
    return b


# ---- fix round (Diamond DIAMOND-mail-v1-2026-10-03): shared OAuth fake + a runtime wired to a real SessionRegistry

import json as _json
import os as _os

from gnomon.mail import oauth as _oauth

ALL3 = " ".join(sorted(_oauth.ALLOWED_SCOPES))
CLIENT = {"client_id": "cid", "client_secret": "csec"}


class FakeGoogle:
    """The OAuth token + revoke endpoints."""

    def __init__(self, scope=ALL3, status=200, revoke_status=200, revoke_raises=None):
        self.scope, self.status, self.revoke_status, self.revoke_raises, self.calls = scope, status, revoke_status, revoke_raises, []

    def __call__(self, url, data):
        self.calls.append((url, dict(data)))
        if url == _oauth.REVOKE_URL:
            if self.revoke_raises is not None:
                raise self.revoke_raises
            return self.revoke_status, {}
        if data.get("grant_type") == "refresh_token":
            return self.status, {"access_token": "AT2", "expires_in": 3600, **({"scope": self.scope} if self.scope else {})}
        return self.status, {"access_token": "AT1", "refresh_token": "RT1", "expires_in": 3600, "scope": self.scope}

    def revoked_tokens(self):
        return [d.get("token") for u, d in self.calls if u == _oauth.REVOKE_URL]


def make_runtime(tmp_path, post=None, backend=None, live=("alpha-architect", "beta-analyst")):
    """(runtime, registry): state under tmp_path/state, a legacy DATA root under tmp_path/data, and a real
    SessionRegistry holding one random per-session secret per live instance (the ticket verifier)."""
    from gnomon.hub.registry import RecordingDownlink, SessionRegistry
    from gnomon.mail.runtime import MailRuntime

    be = backend or standard_backend()
    reg = SessionRegistry()
    for iid in live:
        reg.register(iid, RecordingDownlink(), secret=_os.urandom(32))
    rt = MailRuntime(data_root=str(tmp_path / "data"), state_dir=str(tmp_path / "state"),
                     post_form=post or FakeGoogle(), backend_factory=lambda: be,
                     profile_fn=lambda tok: be.email, ticket_verifier=reg.verify_ticket)
    rt.config_store.save({"account": ACCOUNT, "allowlist": ["pat@example.test"]})
    return rt, reg


def ticket_for(reg, iid, op, args, now=None, nonce=None):
    """What the harness's Actuator mints for a verb call (the real mint function, the registry's secret)."""
    import time as _t

    from gnomon.mail import ticket as T

    return T.mint(reg.get(iid).secret, iid, op, T.args_digest(op, args), now=_t.time() if now is None else now, nonce=nonce)

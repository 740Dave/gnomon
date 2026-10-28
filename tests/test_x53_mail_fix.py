"""V1 FIX ROUND (Diamond DIAMOND-mail-v1-2026-10-03, architect rulings F1-F13). RED first: this file was run
against the pre-fix tree before any fix (see docs/build-logs/RUN_SUMMARY-mail-v1.md, 'Fix round').
Fake Gmail only; no network, no credentials."""

import http.client
import json
import os
import socket
import stat
import sys
import urllib.parse

import pytest

from mail_support import (ACCOUNT, ALL3, CLIENT, LBL_ARCH, FakeGoogle, make_runtime, msg, standard_backend,
                          ticket_for)

from gnomon.mail import gmail_http, oauth
from gnomon.mail.errors import MailError, ScopeError
from gnomon.mail.tokenstore import TokenStore

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ======================================================================== F1: Disconnect ALWAYS deletes the token

RAISERS = [http.client.BadStatusLine("junk"), http.client.IncompleteRead(b"x"), ValueError("v"), KeyError("k"),
           socket.timeout("t"), TimeoutError("t2"), OSError("boom"), RuntimeError("r"), MailError("m")]


@pytest.mark.parametrize("exc", RAISERS, ids=lambda e: type(e).__name__)
def test_f1_revoke_raising_anything_still_deletes_the_token(tmp_path, exc):
    store = TokenStore(str(tmp_path / "mail"))
    store.save({"refresh_token": "RT1", "scope": ALL3})
    mgr = oauth.TokenManager(store, lambda: CLIENT, FakeGoogle(revoke_raises=exc))
    out = mgr.revoke_and_delete()                       # must not raise
    assert not os.path.exists(store.path), "the token file survived a failed revoke"
    assert out["deleted"] is True and out["revoked"] is False and "permissions" in out["warning"]


def test_f1_an_unreadable_token_file_is_still_deleted_even_if_load_raises_something_unexpected(tmp_path, monkeypatch):
    store = TokenStore(str(tmp_path / "mail"))
    store.save({"refresh_token": "RT1", "scope": ALL3})
    monkeypatch.setattr(store, "load", lambda: (_ for _ in ()).throw(PermissionError("denied")))
    out = oauth.TokenManager(store, lambda: CLIENT, FakeGoogle()).revoke_and_delete()
    assert not os.path.exists(store.path) and out["deleted"] is True


def test_f1_delete_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    store = TokenStore(str(tmp_path / "mail"))
    store.save({"refresh_token": "RT1", "scope": ALL3})
    monkeypatch.setattr(store, "delete", lambda: (_ for _ in ()).throw(OSError("read-only fs")))
    out = oauth.TokenManager(store, lambda: CLIENT, FakeGoogle()).revoke_and_delete()
    assert out["deleted"] is False and out["ok"] is False and "read-only fs" in out["warning"]


@pytest.mark.parametrize("exc", [http.client.BadStatusLine("junk"), http.client.IncompleteRead(b"x"),
                                 ValueError("v"), socket.timeout("t")], ids=lambda e: type(e).__name__)
def test_f1_default_post_form_turns_transport_failures_into_mailerror(monkeypatch, exc):
    import urllib.request

    class Op:
        def open(self, *a, **k):
            raise exc
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(exc))
    monkeypatch.setattr(urllib.request, "build_opener", lambda *a, **k: Op())
    with pytest.raises(MailError):
        oauth.default_post_form("https://oauth2.googleapis.com/revoke", {"token": "x"})


def test_f1_http_client_failures_become_mailerror_in_the_gmail_client_too():
    def boom(req, timeout):
        raise http.client.IncompleteRead(b"x")
    with pytest.raises(MailError):
        gmail_http.GmailHttpBackend(lambda: "T", boom).profile_email()


def test_f1_runtime_disconnect_with_a_raising_revoke_logs_and_reports(tmp_path, capsys):
    rt, _ = make_runtime(tmp_path, post=FakeGoogle(revoke_raises=http.client.BadStatusLine("junk")))
    rt.tokens.save({"refresh_token": "RT1", "scope": ALL3})
    out = rt.disconnect()
    assert out["deleted"] is True and out["revoked"] is False and out["warning"]
    assert not os.path.exists(rt.tokens.path)
    rows = rt.activity.tail(5)
    assert rows[0]["op"] == "disconnect" and rows[0]["decision"] == "ok" and rows[0].get("reason") == "revoke-not-confirmed"
    assert "gm-mail" in capsys.readouterr().err        # the log line


# ======================================================================== F2: the token lives OUTSIDE cloud-sync

def test_f2_default_state_dir_is_application_support_gnomon_mail(monkeypatch, tmp_path):
    from gnomon.mail import runtime as R

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("GNOMON_MAIL_STATE_DIR", raising=False)
    assert R.mail_state_dir() == os.path.join(str(tmp_path), "Library", "Application Support", "Gnomon", "mail")
    monkeypatch.setenv("GNOMON_MAIL_STATE_DIR", str(tmp_path / "override"))
    assert R.mail_state_dir() == str(tmp_path / "override")          # the test-only override


def test_f2_token_dir_and_files_are_created_private_and_never_chmodded(tmp_path, monkeypatch):
    old = os.umask(0)                                               # a hostile umask must not widen anything
    try:
        def no_chmod(*a, **k):
            raise AssertionError("chmod/fchmod must never be called on mail state")
        monkeypatch.setattr(os, "chmod", no_chmod)
        monkeypatch.setattr(os, "fchmod", no_chmod, raising=False)
        base = tmp_path / "Gnomon"
        store = TokenStore(str(base / "mail"))
        store.save({"refresh_token": "R", "scope": ALL3})
        assert stat.S_IMODE(os.stat(store.dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(base).st_mode) == 0o700           # the parent too (created by us)
        assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    finally:
        os.umask(old)


def test_f2_a_loose_mode_state_dir_is_refused_not_fixed_up(tmp_path):
    d = tmp_path / "mail"
    d.mkdir(mode=0o755)
    os.chmod(d, 0o755)
    with pytest.raises(MailError, match="mode"):
        TokenStore(str(d)).save({"refresh_token": "R"})
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o755                # still untouched


def test_f2_a_token_at_the_old_data_root_path_is_refused_logged_and_reported(tmp_path, capsys):
    rt, _ = make_runtime(tmp_path)
    old = tmp_path / "data" / "mail"
    old.mkdir(parents=True, mode=0o700)
    (old / "token.json").write_text(json.dumps({"refresh_token": "OLD", "access_token": "x", "expires_at": 9e12, "scope": ALL3}))
    os.chmod(old / "token.json", 0o600)
    st = rt.status()
    assert st["connected"] is False and st["legacy_token_found"] == str(old / "token.json")
    assert "reconnect" in st["legacy_token_message"].lower()
    assert rt.manager.connected() is False                            # never loaded
    rows = rt.activity.tail(5)
    assert any(r["op"] == "legacy-token" and r["decision"] == "refused" for r in rows)
    assert "legacy" in capsys.readouterr().err.lower()
    with pytest.raises(MailError, match="not connected"):
        rt.manager.access_token()


def test_f2_disconnect_also_revokes_and_removes_a_legacy_token(tmp_path):
    g = FakeGoogle()
    rt, _ = make_runtime(tmp_path, post=g)
    old = tmp_path / "data" / "mail"
    old.mkdir(parents=True, mode=0o700)
    (old / "token.json").write_text(json.dumps({"refresh_token": "OLD", "scope": ALL3}))
    os.chmod(old / "token.json", 0o600)
    out = rt.disconnect()
    assert out["legacy_removed"] is True and not (old / "token.json").exists() and "OLD" in g.revoked_tokens()


def test_f2_state_files_all_live_in_the_state_dir_not_under_the_data_root(tmp_path):
    rt, _ = make_runtime(tmp_path)
    rt.tokens.save({"refresh_token": "R", "scope": ALL3})
    rt.activity.record({"instance": "a", "op": "list", "decision": "ok"})
    for p in (rt.tokens.path, rt.config_store.path, rt.activity.path, rt.client_path):
        assert p.startswith(str(tmp_path / "state")) and str(tmp_path / "data") not in p
    assert not (tmp_path / "data").exists() or not list((tmp_path / "data").rglob("*"))


def test_f2_ui_tells_the_operator_to_reconnect_when_a_legacy_token_exists():
    import shutil, subprocess
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    js = os.path.join(ROOT, "web", "mail_ui", "mail_panel.js")
    out = subprocess.run([node, "-e", f"const m=require({json.dumps(js)});console.log(JSON.stringify(m.buildStatus("
                          "{client_file_present:true,connected:false,legacy_token_found:'/x/token.json',"
                          "legacy_token_message:'A token from the old location was NOT used: reconnect.'})))"],
                         capture_output=True, text=True, timeout=30)
    v = json.loads(out.stdout)
    assert any("reconnect" in n.lower() for n in v["notes"]) and v["chip"]["text"] == "not connected"


# ======================================================================== F3: identity is bound by the per-session ticket

def test_f3_registry_ticket_binds_identity_and_rejects_everything_else(tmp_path):
    from gnomon.mail import ticket as T

    rt, reg = make_runtime(tmp_path)
    args = {"max": 3}
    t = ticket_for(reg, "alpha-architect", "mail_list", args)
    assert reg.verify_ticket(t, "mail_list", args) == "alpha-architect"
    assert reg.verify_ticket(t, "mail_list", args) is None                       # single use (replay)
    t = ticket_for(reg, "alpha-architect", "mail_list", args)
    assert reg.verify_ticket(t, "mail_read", args) is None                       # bound to the op
    t = ticket_for(reg, "alpha-architect", "mail_list", args)
    assert reg.verify_ticket(t, "mail_list", {"max": 4}) is None                 # bound to the args
    t = ticket_for(reg, "alpha-architect", "mail_list", args, now=1.0)
    assert reg.verify_ticket(t, "mail_list", args) is None                       # expired
    forged = T.mint(os.urandom(32), "alpha-architect", "mail_list", T.args_digest("mail_list", args), now=__import__("time").time())
    assert reg.verify_ticket(forged, "mail_list", args) is None                  # wrong secret
    # a ticket minted by ds's secret but CLAIMING alpha-architect is not valid for alpha-architect
    cross = T.mint(reg.get("beta-analyst").secret, "alpha-architect", "mail_list", T.args_digest("mail_list", args), now=__import__("time").time())
    assert reg.verify_ticket(cross, "mail_list", args) is None
    for junk in ("", "x", "v1.a.b", None, 5, "v1." + "." * 6, "v1.alpha-architect.9e99.n.zz"):
        assert reg.verify_ticket(junk, "mail_list", args) is None
    assert reg.verify_ticket(ticket_for(reg, "ghost-never-registered", "mail_list", args) if False else "v1.ghost.9e99.n.aa", "mail_list", args) is None


def test_f3_a_reregistration_invalidates_old_tickets(tmp_path):
    from gnomon.hub.registry import RecordingDownlink

    rt, reg = make_runtime(tmp_path)
    t = ticket_for(reg, "alpha-architect", "mail_list", {})
    reg.register("alpha-architect", RecordingDownlink(), secret=os.urandom(32))     # superseded / restarted session
    assert reg.verify_ticket(t, "mail_list", {}) is None


def test_f3_broker_refuses_a_body_instance_id_and_a_missing_or_bad_ticket(tmp_path):
    rt, reg = make_runtime(tmp_path)
    for body in ({"op": "mail_list", "args": {}},                                   # no ticket
                 {"ticket": "nope", "op": "mail_list", "args": {}},
                 {"ticket": ticket_for(reg, "alpha-architect", "mail_list", {}), "op": "mail_read", "args": {"id": "m1"}}):
        out = rt.verb(body.get("ticket"), body["op"], body["args"])
        assert out["ok"] is False and out["reason"] in ("identity-unverified", "bad-ticket"), out
    # the authenticated identity is the TICKET's, whatever else is asked for
    out = rt.verb(ticket_for(reg, "beta-analyst", "mail_read", {"id": "m1"}), "mail_read", {"id": "m1"})
    assert out["reason"] == "not-in-label"                                        # ds cannot read alpha-architect's m1
    ok = rt.verb(ticket_for(reg, "alpha-architect", "mail_list", {}), "mail_list", {})
    assert ok["ok"] and [m["id"] for m in ok["messages"]] == ["m1", "m2"]


def test_f3_actuator_mints_only_with_the_flag_a_secret_and_origin_self(tmp_path):
    from gnomon.harness import ContextMonitor, Injector, SingleWriteSite
    from gnomon.harness.actuator import ActuationError, Actuator
    from gnomon.mail import ticket as T

    def act(**kw):
        w = SingleWriteSite(master_fd=-1, writer=lambda fd, d: len(d))
        return Actuator(Injector(w, sleep=lambda s: None), ContextMonitor(read_context=lambda: 5.0), **kw)
    cmd = {"cmd": "mail_ticket", "op": "mail_list", "args_sha256": T.args_digest("mail_list", {})}
    off = act().dispatch(cmd, origin="self")
    assert off["ok"] is False and off["decision"] == "disabled"
    on = act(mail_identity="alpha-architect")
    assert on.dispatch(cmd, origin="self")["decision"] == "no-hub-session"        # no secret yet: refused, never a guess
    on.dispatch({"cmd": "hub_hello", "hub_instance_id": "h", "session_secret": os.urandom(32).hex()}, origin="hub")
    out = on.dispatch(cmd, origin="self")
    assert out["ok"] and out["ticket"].startswith("v1.alpha-architect.")
    with pytest.raises(ActuationError):
        on.dispatch(cmd, origin="hub")                                             # origin self only
    with pytest.raises(ActuationError):
        on.dispatch({"cmd": "hub_hello", "session_secret": "aa"}, origin="self")   # a secret from a non-hub origin
    assert "mail_ticket" not in Actuator.VOCABULARY


def test_f3_the_secret_never_reaches_the_hub_frame_sink_or_a_log(tmp_path):
    from gnomon.harness import ContextMonitor, Injector, SingleWriteSite
    from gnomon.harness.actuator import Actuator

    seen = []
    w = SingleWriteSite(master_fd=-1, writer=lambda fd, d: len(d))
    a = Actuator(Injector(w, sleep=lambda s: None), ContextMonitor(read_context=lambda: 5.0),
                 on_hub_frame=lambda k, f: seen.append((k, dict(f))), mail_identity="alpha-architect")
    secret = os.urandom(32).hex()
    a.dispatch({"cmd": "hub_hello", "hub_instance_id": "h", "session_secret": secret}, origin="hub")
    assert seen and secret not in json.dumps(seen) and "session_secret" not in seen[0][1]


def test_f3_hub_hello_carries_a_fresh_secret_and_the_registry_holds_the_same_one(tmp_path):
    from gnomon.hub import Hub

    hub = Hub()
    _h, port = hub.start_rendezvous(0)
    got = []
    try:
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", port))
            s.sendall(b'{"type":"register","instance_id":"alpha-x","meta":{"agent_mail":true}}\n')
            s.settimeout(2)
            frame = json.loads(s.recv(4096).split(b"\n")[0])
            got.append(frame["session_secret"])
            assert bytes.fromhex(frame["session_secret"]) == hub.registry.get("alpha-x").secret
            s.close()
        assert got[0] != got[1] and len(bytes.fromhex(got[0])) == 32
    finally:
        hub.stop()


def test_f3_self_mcp_sends_a_ticket_and_no_instance_id(monkeypatch, tmp_path):
    from gnomon.harness import self_mcp

    os.makedirs(tmp_path / ".claude")
    seen = {}

    def fake_actuate(sp, cmd):
        if cmd["cmd"] == "launch_context":
            return {"ok": True, "instance_id": "alpha-architect", "session_dir": str(tmp_path)}
        seen["ticket_req"] = cmd
        return {"ok": True, "ticket": "v1.alpha-architect.1.n.mac"}
    monkeypatch.setattr(self_mcp, "actuate", fake_actuate)
    monkeypatch.delenv("GNOMON_INSTANCE_ID", raising=False)
    posted = []
    out = self_mcp.mail_self("/s", {"cmd": "mail_read", "id": "m1"},
                             post_fn=lambda p: posted.append(p) or {"ok": True, "id": "m1", "subject": "S"})
    assert out["ok"] and posted == [{"ticket": "v1.alpha-architect.1.n.mac", "op": "mail_read", "args": {"id": "m1"}}]
    assert "instance_id" not in json.dumps(posted)
    from gnomon.mail import ticket as T
    assert seen["ticket_req"] == {"cmd": "mail_ticket", "op": "mail_read", "args_sha256": T.args_digest("mail_read", {"id": "m1"})}
    # no ticket available -> a loud refusal, the broker is never called
    monkeypatch.setattr(self_mcp, "actuate", lambda sp, cmd: ({"ok": True, "instance_id": "alpha-architect", "session_dir": str(tmp_path)}
                                                              if cmd["cmd"] == "launch_context" else {"ok": False, "decision": "no-hub-session", "error": "no hub"}))
    out = self_mcp.mail_self("/s", {"cmd": "mail_list"}, post_fn=lambda p: pytest.fail("must not call the broker"))
    assert out["ok"] is False and "no hub" in out["error"]


# ---- the web routes (flag on, real registry, real tickets)

pytest.importorskip("fastapi")
_WEB = os.path.join(ROOT, "web")
if _WEB not in sys.path:
    sys.path.insert(0, _WEB)
from fastapi.testclient import TestClient  # noqa: E402

import backend.server as srv  # noqa: E402
from gnomon.config import ConfigStore, GnomonConfig  # noqa: E402
from gnomon.mail import FLAG_NAME  # noqa: E402

BASE = "http://127.0.0.1:8770"


@pytest.fixture
def web(tmp_path, monkeypatch):
    store = ConfigStore(str(tmp_path / "config.json"))

    def flag(on):
        store.save(GnomonConfig(flags={FLAG_NAME: on}))
    flag(True)
    rt, reg = make_runtime(tmp_path / "w")
    monkeypatch.setattr(srv, "_config_store", store)
    monkeypatch.setattr(srv, "_mail_runtime_override", rt)
    hdr = {"Origin": BASE, "Sec-Fetch-Site": "same-origin", "X-Gnomon-CSRF": srv._APPS_CSRF}
    page = TestClient(srv.app, base_url=BASE, headers=hdr)
    plain = TestClient(srv.app, base_url=BASE)
    yield page, plain, rt, reg, flag


def test_f3_a_forged_instance_id_is_refused_and_leaks_nothing(web):
    page, plain, rt, reg, flag = web
    forged = plain.post("/api/mail/verb", json={"instance_id": "beta-analyst", "op": "mail_read", "args": {"id": "ds1"}})
    assert forged.status_code in (400, 401, 403) and "SECRET-B-BODY" not in forged.text
    noticket = plain.post("/api/mail/verb", json={"op": "mail_list", "args": {}})
    assert noticket.status_code == 200 and noticket.json()["ok"] is False and noticket.json()["reason"] in ("identity-unverified", "bad-ticket")
    both = plain.post("/api/mail/verb", json={"ticket": ticket_for(reg, "alpha-architect", "mail_list", {}), "instance_id": "beta-analyst",
                                              "op": "mail_list", "args": {}})
    assert both.status_code == 400                                               # the body may not name an identity at all


def test_f3_a_real_ticket_works_once_over_the_route(web):
    page, plain, rt, reg, flag = web
    body = {"ticket": ticket_for(reg, "alpha-architect", "mail_list", {}), "op": "mail_list", "args": {}}
    r = plain.post("/api/mail/verb", json=body)
    assert r.status_code == 200 and r.json()["ok"] and [m["id"] for m in r.json()["messages"]] == ["m1", "m2"]
    again = plain.post("/api/mail/verb", json=body)
    assert again.json()["ok"] is False                                           # replay refused


def test_f3_config_writes_need_the_browser_headers_no_origin_is_refused(web):
    page, plain, rt, reg, flag = web
    body = {"account": ACCOUNT, "allowlist": ["evil@example.test"]}
    assert plain.post("/api/mail/config", json=body).status_code == 403          # no Origin at all
    csrf = plain.get("/api/mail/status").json()["csrf"]
    assert csrf == srv._APPS_CSRF                                               # the SAME token the Apps guard uses
    assert plain.post("/api/mail/config", json=body, headers={"X-Gnomon-CSRF": csrf}).status_code == 403   # token but no Origin
    assert plain.post("/api/mail/config", json=body, headers={"X-Gnomon-CSRF": csrf, "Origin": BASE}).status_code == 403  # no Sec-Fetch-Site
    assert rt.config_store.load().allowlist == ["pat@example.test"]
    assert page.post("/api/mail/config", json=body).status_code == 200            # the page's real request shape


def test_f3_config_guard_is_the_apps_guard_not_a_copy():
    import inspect
    src = inspect.getsource(srv._mail_guard)
    assert "_apps_guard" in src
    assert not hasattr(srv, "_MAIL_CSRF")


# ======================================================================== F4: a wrong-scope grant is revoked at Google

def test_f4_a_send_capable_grant_is_revoked_before_it_is_discarded():
    g = FakeGoogle(scope=ALL3 + " https://www.googleapis.com/auth/gmail.send")
    flow = oauth.OAuthFlow(CLIENT, "http://127.0.0.1:1/cb", g)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(flow.start()).query)["state"][0]
    with pytest.raises(ScopeError, match="revoked"):
        flow.complete(state, "code")
    assert g.revoked_tokens() == ["RT1"]


def test_f4_a_missing_scope_grant_is_revoked_too_and_a_failing_revoke_still_raises_scopeerror():
    g = FakeGoogle(scope="https://www.googleapis.com/auth/gmail.compose")
    flow = oauth.OAuthFlow(CLIENT, "http://127.0.0.1:1/cb", g)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(flow.start()).query)["state"][0]
    with pytest.raises(ScopeError):
        flow.complete(state, "code")
    assert g.revoked_tokens() == ["RT1"]
    g2 = FakeGoogle(scope="https://mail.google.com/", revoke_raises=http.client.BadStatusLine("junk"))
    flow = oauth.OAuthFlow(CLIENT, "http://127.0.0.1:1/cb", g2)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(flow.start()).query)["state"][0]
    with pytest.raises(ScopeError, match="myaccount.google.com/permissions"):
        flow.complete(state, "code")


def test_f4_through_the_runtime_nothing_is_stored(tmp_path):
    g = FakeGoogle(scope=ALL3 + " https://www.googleapis.com/auth/gmail.send")
    rt, _ = make_runtime(tmp_path, post=g)
    os.makedirs(rt.dir, exist_ok=True)
    with open(rt.client_path, "w") as fh:
        json.dump({"installed": CLIENT}, fh)
    state = urllib.parse.parse_qs(urllib.parse.urlparse(rt.connect_start("http://127.0.0.1:1/cb")).query)["state"][0]
    with pytest.raises(ScopeError):
        rt.connect_complete(state, "code")
    assert rt.tokens.load() is None and "RT1" in g.revoked_tokens()


# ======================================================================== F5: the From header is not authenticated

def test_f5_the_allowlist_flag_is_named_for_what_it_is(tmp_path):
    rt, reg = make_runtime(tmp_path)
    b = standard_backend()
    b.add(msg("sp", ["INBOX", LBL_ARCH], "spoof", sender="Pat <pat@example.test>"))
    rt2, reg2 = make_runtime(tmp_path / "x", backend=b)
    out = rt2.verb(ticket_for(reg2, "alpha-architect", "mail_read", {"id": "sp"}), "mail_read", {"id": "sp"})
    assert out["ok"] and out["from_header_on_allowlist"] is True and out["from_header_unverified"] is True
    assert "from_on_allowlist" not in out
    assert "not authenticated" in out["notice"].lower()
    from gnomon.harness import self_mcp
    assert "not authenticated" in self_mcp.MAIL_READ_TOOL["description"].lower()
    assert "not authenticated" in self_mcp.MAIL_LIST_TOOL["description"].lower()
    rows = rt2.activity.tail(3)
    assert rows[0].get("from_header_allowlisted") is True and "from_allowlisted" not in rows[0]


# ======================================================================== P3s

def test_f9_reserved_words_are_not_message_ids():
    for bad in ("send", "SEND", "import", "trash", "untrash", "batchModify", "batchDelete", "modify"):
        assert not gmail_http._ID_RE.match(bad), bad
        assert not any(m == "GET" and rx.match(gmail_http._ME + "/messages/" + bad) for m, rx in gmail_http.ALLOWED), bad
    assert gmail_http._ID_RE.match("18c0ffee12345678") and gmail_http._ID_RE.match("m1")


def test_f11_a_bidi_override_or_line_separator_in_a_subject_is_refused(tmp_path):
    from mail_support import standard_backend as sb
    from gnomon.mail.service import MailService
    from gnomon.mail.activity import ActivityLog
    from gnomon.mail.config import MailConfigStore
    d = str(tmp_path / "m")
    store = MailConfigStore(d)
    store.save({"account": ACCOUNT, "allowlist": ["pat@example.test"]})
    svc = MailService(sb(), store, ActivityLog(os.path.join(d, "a.jsonl")))
    for bad in ("evil‮gnp.exe", "a⁦b", "a‏b", "a b", "a\u0085b", "a؜b"):
        r = svc.draft("alpha-architect", "pat@example.test", bad, "b")
        assert r["ok"] is False and r["reason"] == "bad-subject", repr(bad)
    assert svc.draft("alpha-architect", "pat@example.test", "Plain ascii and café", "b")["ok"]


def test_f13_the_real_openers_never_follow_a_redirect(monkeypatch):
    import urllib.request
    from gnomon.mail import netutil

    h = netutil.NoRedirect()
    req = urllib.request.Request("https://gmail.googleapis.com/x", headers={"Authorization": "Bearer T"})
    assert h.redirect_request(req, None, 302, "Found", {}, "https://evil.example/") is None
    seen = []
    monkeypatch.setattr(urllib.request, "build_opener", lambda *hs: seen.extend(hs) or type("O", (), {"open": lambda s, *a, **k: (_ for _ in ()).throw(ValueError("x"))})())
    with pytest.raises(MailError):                      # the default opener's failure is mapped by the client
        gmail_http.GmailHttpBackend(lambda: "T").profile_email()
    assert any(isinstance(x, netutil.NoRedirect) or x is netutil.NoRedirect for x in seen)


def test_f10_trailing_slash_and_unknown_mail_paths_are_a_plain_404_flag_off_and_on(web):
    page, plain, rt, reg, flag = web
    paths = ["/api/mail/verb/", "/api/mail/status/", "/api/mail/config/", "/api/mail/nothing", "/api/mail/", "/api/mail",
             "/mail-ui/mail_panel.js/", "/mail-ui/", "/mail-ui/nothing.js"]
    for state in (False, True):
        flag(state)
        for p in paths:
            for m in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
                r = page.request(m, p, content=b"{}" if m in ("POST", "PUT", "PATCH") else None,
                                 headers={"Content-Type": "application/json"}, follow_redirects=False)
                assert r.status_code == 404, (state, m, p, r.status_code)
                if m != "HEAD":
                    assert r.json() == {"detail": "Not Found"}, (state, m, p)


def test_p3_the_catch_all_rows_are_in_the_route_table_as_no_event():
    from gnomon import feature_sites as FS
    for m in ("GET", "POST"):
        assert (m, "/api/mail/{tail:path}") in FS.WEB_NO_EVENT and (m, "/mail-ui/{tail:path}") in FS.WEB_NO_EVENT

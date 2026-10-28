"""Diamond P2-1: the hub greeting carries a mail session secret ONLY when the harness's register meta says agent_mail is on.

With mail OFF (no key, or anything but the boolean true) nothing is minted or sent: the hub_hello frame is exactly the one the build
before mail sent, and the registry holds no secret. The harness says so through build_hub_link(mail=...)."""
import json
import socket

import pytest

from gnomon.hub import Hub
from gnomon.harness import launcher as L

#: the frame the hub sent before mail existed (fe63f64): these two keys, in this order, nothing else
def _expected_off(hub):
    return {"cmd": "hub_hello", "hub_instance_id": hub.hub_instance_id}


def _register(port, meta_json):
    s = socket.create_connection(("127.0.0.1", port))
    try:
        s.sendall(b'{"type":"register","instance_id":"alpha-x","meta":' + meta_json + b'}\n')
        s.settimeout(3)
        raw = s.recv(4096).split(b"\n")[0]
        return raw
    finally:
        s.close()


@pytest.mark.parametrize("meta", [b"{}", b'{"session_dir":"/s"}', b'{"agent_mail":false}', b'{"agent_mail":"yes"}', b'{"agent_mail":1}', b"null", b"[]"])
def test_mail_off_the_greeting_is_byte_identical_to_the_pre_mail_frame(meta):
    hub = Hub()
    _h, port = hub.start_rendezvous(0)
    try:
        raw = _register(port, meta)
        assert raw == json.dumps(_expected_off(hub)).encode()          # the exact bytes, key order included
        assert hub.registry.get("alpha-x").secret == b""
    finally:
        hub.stop()


def test_mail_on_the_greeting_carries_a_64_hex_secret_the_registry_also_holds():
    hub = Hub()
    _h, port = hub.start_rendezvous(0)
    try:
        frame = json.loads(_register(port, b'{"session_dir":"/s","agent_mail":true}'))
        assert set(frame) == {"cmd", "hub_instance_id", "session_secret"} and frame["cmd"] == "hub_hello"
        assert len(frame["session_secret"]) == 64 and bytes.fromhex(frame["session_secret"]) == hub.registry.get("alpha-x").secret
    finally:
        hub.stop()


def test_the_harness_link_tells_the_hub_only_when_mail_is_on(tmp_path):
    cfg = {"hub_rendezvous_port": None}
    on = L.build_hub_link(cfg, "alpha-x", str(tmp_path / "s"), None, mail=True)
    off = L.build_hub_link(dict(cfg), "alpha-x", str(tmp_path / "s"), None)
    assert on._meta == {"session_dir": str(tmp_path / "s"), "agent_mail": True}
    assert off._meta == {"session_dir": str(tmp_path / "s")}                     # today's frame, unchanged

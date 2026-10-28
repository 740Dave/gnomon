"""The public test subset's own guard: it must be able to FAIL. Each check below is the thing conftest.py promises."""

import os
import socket

import pytest

import gnomon


def test_package_under_test_is_this_checkout():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    assert os.path.realpath(os.path.dirname(gnomon.__file__)) == os.path.realpath(os.path.join(root, "gnomon"))


def test_home_is_a_throwaway_directory(gm_session_home):
    assert os.path.realpath(os.environ["HOME"]) == os.path.realpath(gm_session_home)
    assert os.path.basename(os.path.dirname(os.path.realpath(gm_session_home))).startswith("gmtestiso.")
    assert os.path.expanduser("~") == os.environ["HOME"]


def test_session_variables_are_not_inherited():
    for var in ("GNOMON_AGENT_DEPTH", "GNOMON_INSTANCE_ID", "GNOMON_SESSION_DIR", "GNOMON_PROBE"):
        assert var not in os.environ


def test_lookup_of_a_remote_name_fails_loudly():
    with pytest.raises(OSError, match="network disabled"):
        socket.getaddrinfo("example.com", 443)


def test_connect_to_a_remote_address_fails_loudly():
    s = socket.socket()
    try:
        with pytest.raises(OSError, match="network disabled"):
            s.connect(("93.184.216.34", 80))
    finally:
        s.close()

"""the egress guard, unit level (every blocked class named in the spec's acceptance)."""

import pytest

from gnomon.fetchverbatim import guard
from gnomon.fetchverbatim.guard import GuardRefusal, check_addresses, classify_address, validate_url

REFUSED_ADDRESSES = [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "10.255.255.255", "172.16.0.1", "172.31.255.255", "192.168.0.1",
    "169.254.169.254", "169.254.1.1", "100.64.0.1", "100.100.100.200", "100.127.255.255", "0.0.0.0", "224.0.0.1",
    "239.255.255.255", "255.255.255.255", "240.0.0.1", "192.0.0.192", "192.0.2.1", "198.18.0.1", "198.51.100.1",
    "::1", "::", "fe80::1", "fe80::1%en0", "fc00::1", "fd00:ec2::254", "ff02::1", "ff0e::1", "fec0::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:100.64.0.1", "::ffff:8.8.8.8",
    "::127.0.0.1", "64:ff9b::7f00:1", "64:ff9b::808:808", "2002:7f00:1::", "2001:0:4136:e378:8000:63bf:3fff:fdd2",
    "not-an-ip",
]
ALLOWED_ADDRESSES = ["93.184.216.34", "8.8.8.8", "151.101.1.69", "2606:4700:4700::1111", "2001:4860:4860::8888"]


@pytest.mark.parametrize("ip", REFUSED_ADDRESSES)
def test_refused_address_classes(ip):
    assert classify_address(ip) is not None


@pytest.mark.parametrize("ip", ALLOWED_ADDRESSES)
def test_public_addresses_pass(ip):
    assert classify_address(ip) is None


def test_one_private_answer_among_public_ones_refuses_the_name():
    with pytest.raises(GuardRefusal) as e:
        check_addresses("x.example.com", ["93.184.216.34", "10.0.0.5"])
    assert e.value.code == "address_blocked"
    with pytest.raises(GuardRefusal) as e:
        check_addresses("x.example.com", [])
    assert e.value.code == "dns_failure"
    assert check_addresses("x.example.com", ["93.184.216.34", "93.184.216.34"]) == ["93.184.216.34"]


@pytest.mark.parametrize("url,code", [
    ("file:///etc/passwd", "scheme_not_allowed"),
    ("gopher://example.com/", "scheme_not_allowed"),
    ("ftp://example.com/x", "scheme_not_allowed"),
    ("javascript:alert(1)", "scheme_not_allowed"),
    ("data:text/plain,hi", "scheme_not_allowed"),
    ("//example.com/x", "scheme_not_allowed"),
    ("example.com/x", "scheme_not_allowed"),
    ("http://user:pw@example.com/", "userinfo_not_allowed"),
    ("http://user@example.com/", "userinfo_not_allowed"),
    ("http://example.com@127.0.0.1/", "userinfo_not_allowed"),
    ("http://x.local/", "host_not_allowed"),
    ("http://printer.lan/", "host_not_allowed"),
    ("http://nas.home.arpa/", "host_not_allowed"),
    ("http://svc.internal/", "host_not_allowed"),
    ("http://a.localhost/", "host_not_allowed"),
    ("http://localhost/", "host_not_allowed"),
    ("http://intranet/", "host_not_allowed"),
    ("http://2130706433/", "host_not_allowed"),
    ("http://localhost./", "host_not_allowed"),
    ("http://example.com:8080/", "port_not_allowed"),
    ("http://example.com:22/", "port_not_allowed"),
    ("https://example.com:80/", "port_not_allowed"),
    ("http://example.com:443/", "port_not_allowed"),
    ("http://127.0.0.1:8770/api", "port_not_allowed"),
    ("http://example.com:99999/", "bad_url"),
    ("http://exa mple.com/", "bad_url"),
    ("http://example.com/\r\nX: y", "bad_url"),
    ("http://example.com\\@127.0.0.1/", "bad_url"),
    ("http://", "bad_url"),
    ("", "bad_url"),
    ("http://" + "a" * 3000 + ".com/", "bad_url"),
])
def test_url_refusals(url, code):
    with pytest.raises(GuardRefusal) as e:
        validate_url(url)
    assert e.value.code == code, e.value.message


def test_url_not_a_string():
    for bad in (None, 5, b"http://example.com/", ["http://example.com/"]):
        with pytest.raises(GuardRefusal):
            validate_url(bad)


def test_valid_urls_parse_and_normalise():
    p = validate_url("HTTPS://Example.COM/a/b?x=1#frag")
    assert (p.scheme, p.host, p.port, p.path_query) == ("https", "example.com", 443, "/a/b?x=1")
    assert p.url() == "https://example.com/a/b?x=1"
    assert validate_url("http://example.com").path_query == "/"
    assert validate_url("http://example.com./x").host == "example.com"
    assert validate_url("http://8.8.8.8/").is_ip_literal is True       # literal syntax passes; the ADDRESS check decides
    assert validate_url("http://[2606:4700:4700::1111]/").host == "2606:4700:4700::1111"
    assert validate_url("http://bücher.example/").host == "xn--bcher-kva.example"


def test_literal_private_hosts_pass_url_syntax_but_fail_the_address_check():
    """Literal IPs are vetted by the ADDRESS check (the one that also covers DNS answers), not by string matching."""
    for u in ("http://127.0.0.1/", "http://[::1]/", "http://10.0.0.1/", "http://169.254.169.254/", "http://[::ffff:127.0.0.1]/"):
        p = validate_url(u)
        assert p.is_ip_literal
        with pytest.raises(GuardRefusal):
            check_addresses(p.host, [p.host])


def test_allowed_ports_are_deployment_data_and_default_empty():
    with pytest.raises(GuardRefusal):
        validate_url("http://example.com:8080/")
    assert validate_url("http://example.com:8080/", allowed_ports=[8080]).port == 8080
    with pytest.raises(GuardRefusal):
        validate_url("http://example.com:8081/", allowed_ports=[8080])

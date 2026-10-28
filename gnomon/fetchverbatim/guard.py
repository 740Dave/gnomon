"""The egress guard for guarded outbound fetches (requirement-egress-guard).

ONE implementation: any code that makes an outbound request on an agent's behalf calls
:func:`validate_url` on every URL (first request and every redirect hop) and
:func:`check_addresses` on every resolved address set BEFORE it connects. Nothing in this module can be
relaxed by an argument, a flag or an environment variable: the policy is the module's constants.

Policy (all refusals are :class:`GuardRefusal` with a stable ``code``):

* scheme must be http or https; no userinfo; no control characters, whitespace or backslash; URL length capped;
* host must not be empty, a bare single label ("localhost", "intranet", "2130706433"), or end in
  .local / .internal / .lan / .home.arpa / .localhost; the port must be the scheme's default unless the
  caller's explicit ``allowed_ports`` (deployment data, empty by default) names it;
* EVERY resolved address must be globally routable unicast: loopback, private (RFC1918, ULA), CGNAT
  100.64/10, link-local (incl. 169.254.169.254), multicast, unspecified, reserved, documentation and every other
  non-global range are refused, as are IPv6 forms that embed an IPv4 address (IPv4-mapped ::ffff:0:0/96,
  IPv4-compatible ::/96, NAT64 64:ff9b::/96, 6to4 2002::/16, Teredo), which are unwrapped and re-checked
  or refused outright.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from typing import FrozenSet, Iterable, List, Optional, Tuple
from urllib.parse import quote, urlsplit

MAX_URL_LENGTH = 2048
DEFAULT_PORTS = {"http": 80, "https": 443}
BLOCKED_SUFFIXES: Tuple[str, ...] = (".local", ".internal", ".lan", ".home.arpa", ".localhost")

_BAD_URL_CHARS = re.compile(r"[\x00-\x20\x7f\\]")
_LABEL = re.compile(r"^[a-z0-9_]([a-z0-9_-]{0,61}[a-z0-9_])?$")

#: IPv6 prefixes that embed an IPv4 address or translate to one. Refused outright (no legitimate public page
#: needs them) in addition to the unwrap-and-recheck of the mapped form.
_V6_EMBEDDING = tuple(ipaddress.ip_network(n) for n in (
    "::/96", "64:ff9b::/96", "64:ff9b:1::/48", "2002::/16", "2001::/32"))


class FetchError(Exception):
    """Any refusal or failure of a guarded fetch. ``code`` is stable and tested; ``trace`` (set by the fetcher)
    carries the last URL attempted and what was seen, so a refusal can still be audited."""

    def __init__(self, code: str, message: str, **detail: object):
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.trace: Optional[object] = None


class GuardRefusal(FetchError):
    """A URL or address the egress guard refuses."""


@dataclass(frozen=True)
class ParsedUrl:
    scheme: str
    host: str          # ascii (punycode) lowercase, no brackets
    port: int
    path_query: str    # "/path?query", never empty, no fragment
    is_ip_literal: bool

    @property
    def origin(self) -> Tuple[str, str, int]:
        return (self.scheme, self.host, self.port)

    def url(self) -> str:
        default = DEFAULT_PORTS[self.scheme]
        host = f"[{self.host}]" if ":" in self.host else self.host
        netloc = host if self.port == default else f"{host}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path_query}"


def _refuse(code: str, message: str):
    raise GuardRefusal(code, message)


def _ip_literal(host: str) -> Optional[ipaddress._BaseAddress]:
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def validate_url(url: object, *, allowed_ports: Iterable[int] = ()) -> ParsedUrl:
    """Parse and vet a URL string. Returns a :class:`ParsedUrl` or raises :class:`GuardRefusal`."""
    if not isinstance(url, str) or not url:
        _refuse("bad_url", "url must be a non-empty string")
    if len(url) > MAX_URL_LENGTH:
        _refuse("bad_url", f"url longer than {MAX_URL_LENGTH} characters")
    if _BAD_URL_CHARS.search(url):
        _refuse("bad_url", "url contains whitespace, control characters or a backslash")
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        _refuse("bad_url", f"url does not parse: {exc}")
    scheme = (parts.scheme or "").lower()
    if scheme not in DEFAULT_PORTS:
        _refuse("scheme_not_allowed", f"scheme {scheme or '(none)'!r} is not http or https")
    netloc = parts.netloc
    if "@" in netloc or parts.username is not None or parts.password is not None:
        _refuse("userinfo_not_allowed", "credentials (userinfo) in the url are refused")
    try:
        port = parts.port
    except ValueError:
        _refuse("bad_url", "url port is not a valid number")
    raw_host = parts.hostname
    if not raw_host:
        _refuse("bad_url", "url has no host")
    raw_host = raw_host.rstrip(".")  # a trailing-dot FQDN is the same name
    if not raw_host:
        _refuse("bad_url", "url has no host")
    literal = _ip_literal(raw_host.split("%", 1)[0])  # a zone id (fe80::1%en0) still parses as the address
    if literal is not None:
        host, is_literal = str(literal) if "%" not in raw_host else raw_host, True
    else:
        is_literal = False
        try:
            host = raw_host.encode("idna").decode("ascii").lower()
        except UnicodeError:
            _refuse("bad_url", "host is not a valid (IDNA) name")
        if "." not in host:
            _refuse("host_not_allowed", f"bare single-label host {host!r} is refused")
        for suffix in BLOCKED_SUFFIXES:
            if host.endswith(suffix):
                _refuse("host_not_allowed", f"host suffix {suffix!r} is refused")
        if not all(_LABEL.match(lbl) for lbl in host.split(".")):
            _refuse("bad_url", "host has an invalid label")
    default = DEFAULT_PORTS[scheme]
    eff_port = default if port is None else port
    if eff_port != default and eff_port not in frozenset(allowed_ports):
        _refuse("port_not_allowed", f"port {eff_port} is not the default for {scheme} and is not allow-listed")
    if not (0 < eff_port < 65536):
        _refuse("bad_url", "port out of range")
    path_query = parts.path or "/"
    if parts.query:
        path_query += "?" + parts.query
    path_query = quote(path_query, safe="/?%:@!$&'()*+,;=~-._#[]")   # non-ASCII is percent-encoded; http.client needs ASCII
    return ParsedUrl(scheme, host, eff_port, path_query, is_literal)


def classify_address(ip: object) -> Optional[str]:
    """Why ``ip`` (str or ipaddress object) must be refused, or ``None`` if it is a public unicast address."""
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0] if isinstance(ip, str) else ip)
    except ValueError:
        return "unparseable address"
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.ipv4_mapped is not None:
            inner = classify_address(addr.ipv4_mapped)
            return f"IPv4-mapped IPv6 of a refused address ({inner})" if inner else "IPv4-mapped IPv6 form (refused outright)"
    for name in ("is_unspecified", "is_loopback", "is_link_local", "is_multicast", "is_private", "is_reserved"):
        if getattr(addr, name):
            return name[3:].replace("_", "-")
    if isinstance(addr, ipaddress.IPv6Address):
        if addr.is_site_local:
            return "site-local"
        for net in _V6_EMBEDDING:
            if addr in net:
                return f"IPv6 prefix {net} embeds or translates an IPv4 address"
        if addr.sixtofour is not None or addr.teredo is not None:
            return "6to4 / Teredo address"
    if addr in ipaddress.ip_network("100.64.0.0/10"):
        return "CGNAT 100.64.0.0/10"
    if not addr.is_global:
        return "not globally routable"
    return None


def check_addresses(host: str, addresses: Iterable[str]) -> List[str]:
    """Return the (deduplicated, order-preserving) addresses if ALL are public; raise :class:`GuardRefusal` if
    there are none or ANY is refused (one private answer among public ones refuses the whole name)."""
    seen: List[str] = []
    for a in addresses:
        if a not in seen:
            seen.append(a)
    if not seen:
        _refuse("dns_failure", f"{host!r} resolved to no addresses")
    for a in seen:
        why = classify_address(a)
        if why is not None:
            _refuse("address_blocked", f"{host!r} resolves to {a}, which is refused ({why})")
    return seen

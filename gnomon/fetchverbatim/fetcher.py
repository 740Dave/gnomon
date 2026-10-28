"""The guarded fetch core: GET only, pinned-IP connect, per-hop re-check, size and time caps,
robots.txt, a polite per-host pace.

SECURITY SHAPE (what the Diamond should attack):

1. Every URL, first request and each redirect hop, goes through :func:`guard.validate_url`.
2. A host is resolved ONCE per :meth:`Fetcher.fetch` call (``pins``); ALL answers go through
   :func:`guard.check_addresses` (one private answer refuses the name); the connection is made to the checked
   IP (``_open_socket(ip, port)``), the Host header and TLS server name stay the hostname. There is no second
   lookup in the call, so a rebinding resolver cannot change the answer between check and connect.
3. The guard sits in :meth:`Fetcher._pin`, in the BASE class, between ``_resolve`` and ``_open_socket``. The
   test seam (below) can replace what is resolved and where the socket goes, never what the guard accepts.
4. No cookie jar, no auth header, no proxy, no POST; ``Accept-Encoding: identity`` and any other
   ``Content-Encoding`` is refused (no decompression bomb surface).

TEST SEAM and why production cannot reach it: tests need to talk to a fixture server on 127.0.0.1, which the
guard (correctly) refuses. The seam is five underscore methods on :class:`Fetcher` (``_resolve``,
``_open_socket``, ``_ssl_context``, ``_now``, ``_sleep``), overridable by a SUBCLASS, and
``Fetcher.__init_subclass__`` raises TypeError unless the subclass is defined in a test module
(``test_*`` or ``fv_support``). A stock ``Fetcher`` instance is SEALED after construction (``__slots__`` plus a
``__setattr__`` that raises), so ``fetcher._resolve = ...`` or any other instance-attribute override raises
AttributeError. The tool layer no longer accepts a fetcher at all (``FetchVerbatim`` builds the stock one; its own
test-module-only subclass hook is ``_make_fetcher``). The guard also runs AT THE CONNECT SITE (immediately before
``_open_socket``), not only in the pin step, so a subclass that replaces ``_pin`` still cannot connect to a refused address. The fixture subclass resolves fixture names to PUBLIC-looking addresses and
redirects the TCP connection, so the guard still runs on those public addresses and the seam never relaxes it;
a test proves a seam-using fetcher still refuses a URL or a resolution that names 127.0.0.1. Production wiring
(:func:`tool.build_tool`) takes no transport argument. A source scan test asserts no module under
``gnomon/`` subclasses ``Fetcher``. HONEST BOUND: arbitrary in-process Python can always forge
``__module__`` or monkeypatch; this stops accidental and configuration-driven reach, not a hostile in-process author.
"""

from __future__ import annotations

import http.client
import ipaddress
import os
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin

from . import guard, robots
from .guard import FetchError, GuardRefusal, ParsedUrl

DEFAULT_USER_AGENT = "fetch-verbatim/1.0 (automated fetcher; honours robots.txt)"
ROBOTS_PRODUCT_TOKEN = "fetch-verbatim"
REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_CHUNK = 64 * 1024
_SUBCLASS_OK = re.compile(r"^(tests\.)?(test_[A-Za-z0-9_]+|fv_support)$")


@dataclass(frozen=True)
class Limits:
    max_body_bytes: int = 2 * 1024 * 1024        # 2 MiB
    robots_max_bytes: int = 512 * 1024
    timeout_s: float = 20.0                       # wall clock for the WHOLE call (all hops, robots, DNS)
    max_redirects: int = 5
    min_host_interval_s: float = 2.0
    robots_cache_ttl_s: float = 300.0
    user_agent: str = DEFAULT_USER_AGENT
    allowed_ports: frozenset = frozenset()        # deployment data; empty = default ports only

    def __post_init__(self) -> None:
        if self.max_body_bytes <= 0 or self.robots_max_bytes <= 0 or self.timeout_s <= 0 or self.max_redirects < 0:
            raise ValueError("limits must be positive")


@dataclass
class Trace:
    requested_url: str
    current_url: str
    status: Optional[int] = None
    redirects: List[str] = field(default_factory=list)
    bytes_read: int = 0
    robots: Dict[str, object] = field(default_factory=dict)


@dataclass
class FetchResult:
    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: bytes
    truncated: bool
    redirects: List[str]
    robots: Dict[str, object]
    fetched_at_utc: str


_HTML_TYPES = ("text/html", "application/xhtml+xml")


def media_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def is_supported_type(content_type: str) -> bool:
    m = media_type(content_type)
    return (m.startswith("text/") or m in ("application/json", "application/xml", "application/xhtml+xml")
            or m.endswith("+xml") or m.endswith("+json"))


def _build_ssl_context() -> ssl.SSLContext:
    """TLS client context with certificate AND hostname verification, trust anchors from certifi, else from the
    compiled-in OpenSSL default locations. It deliberately does NOT use ``ssl.create_default_context()`` /
    ``load_default_certs()``: those honour the SSL_CERT_FILE and SSL_CERT_DIR environment variables, which would
    let whoever controls the service environment add a trust anchor. Fails loudly if no CA bundle can be loaded."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.check_hostname = True
    loaded = False
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        loaded = True
    except Exception:  # noqa: BLE001 - fall through to the compiled-in locations
        pass
    if not loaded:
        vp = ssl.get_default_verify_paths()
        if vp.openssl_cafile and os.path.isfile(vp.openssl_cafile):
            ctx.load_verify_locations(cafile=vp.openssl_cafile)
            loaded = True
        if vp.openssl_capath and os.path.isdir(vp.openssl_capath):
            ctx.load_verify_locations(capath=vp.openssl_capath)
            loaded = True
    if not loaded:
        raise FetchError("tls_unavailable", "no CA bundle could be loaded (certifi missing and no compiled-in OpenSSL CA location)")
    return ctx


class Fetcher:
    __slots__ = ("limits", "_lock", "_next_ok", "_robots_cache", "_sealed")

    def __setattr__(self, name: str, value) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError(f"a stock Fetcher is sealed: {name!r} cannot be set (test seams subclass in a test module)")
        object.__setattr__(self, name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_sealed", False):
            raise AttributeError(f"a stock Fetcher is sealed: {name!r} cannot be deleted")
        object.__delattr__(self, name)

    def __init_subclass__(cls, **kw) -> None:
        super().__init_subclass__(**kw)
        if not _SUBCLASS_OK.match(cls.__module__ or ""):
            raise TypeError(
                f"{cls.__module__}.{cls.__name__}: Fetcher's transport seam may only be overridden in a test module")

    def __init__(self, limits: Optional[Limits] = None) -> None:
        self.limits = limits or Limits()
        self._lock = threading.Lock()
        self._next_ok: Dict[str, float] = {}
        self._robots_cache: Dict[Tuple[str, str, int], Tuple[float, str, str]] = {}
        if type(self) is Fetcher:
            object.__setattr__(self, "_sealed", True)

    # ------------------------------------------------------------------ test seam (see module docstring)
    def _resolve(self, host: str, port: int) -> List[str]:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [i[4][0] for i in infos]

    def _open_socket(self, ip: str, port: int, timeout: float) -> socket.socket:
        fam = socket.AF_INET6 if ":" in ip else socket.AF_INET
        s = socket.socket(fam, socket.SOCK_STREAM)
        try:
            s.settimeout(timeout)
            s.connect((ip, port))
        except BaseException:
            s.close()
            raise
        return s

    def _ssl_context(self) -> ssl.SSLContext:
        return _build_ssl_context()

    def _now(self) -> float:
        return time.monotonic()

    def _sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    # ------------------------------------------------------------------ helpers
    def _remaining(self, deadline: float, what: str) -> float:
        rem = deadline - self._now()
        if rem <= 0:
            raise FetchError("timeout", f"timed out ({what}); the call's {self.limits.timeout_s:g} s budget is spent")
        return rem

    def _pin(self, parsed: ParsedUrl, pins: Dict[str, List[str]], deadline: float) -> List[str]:
        """Resolve ``parsed.host`` at most once per call and return the CHECKED addresses (the guard runs here)."""
        if parsed.host in pins:
            return pins[parsed.host]
        if parsed.is_ip_literal:
            ips = guard.check_addresses(parsed.host, [parsed.host])
        else:
            box: Dict[str, object] = {}

            def work() -> None:
                try:
                    box["v"] = self._resolve(parsed.host, parsed.port)
                except BaseException as exc:  # noqa: BLE001
                    box["e"] = exc

            t = threading.Thread(target=work, daemon=True)
            t.start()
            t.join(self._remaining(deadline, "DNS"))
            if t.is_alive():
                raise FetchError("timeout", "timed out resolving the host name")
            if "e" in box:
                raise FetchError("dns_failure", f"could not resolve {parsed.host!r}: {box['e']}")
            ips = guard.check_addresses(parsed.host, box["v"])  # type: ignore[arg-type]
        pins[parsed.host] = ips
        return ips

    def _pace(self, host: str, deadline: float) -> None:
        with self._lock:
            now = self._now()
            if len(self._next_ok) > 1000:
                for h in [h for h, t in self._next_ok.items() if t <= now]:
                    del self._next_ok[h]
            nxt = self._next_ok.get(host, 0.0)
            wait = max(0.0, nxt - now)
            self._next_ok[host] = max(now, nxt) + self.limits.min_host_interval_s
        if wait > 0:
            if self._now() + wait >= deadline:
                raise FetchError("timeout", f"the polite per-host pace ({wait:.1f} s wait) would exceed the call's time budget")
            self._sleep(wait)

    def _request(self, parsed: ParsedUrl, pins: Dict[str, List[str]], deadline: float, *, max_bytes: int,
                 require_text: bool, trace: Trace) -> Tuple[int, http.client.HTTPMessage, bytes, bool]:
        """ONE GET to the pinned address. Redirect responses return an empty body."""
        ips = self._pin(parsed, pins, deadline)
        self._pace(parsed.host, deadline)
        sock = None
        last_exc: Optional[BaseException] = None
        for ip in ips:
            guard.check_addresses(parsed.host, [ip])   # the guard AT THE CONNECT SITE: no overridable step can skip it
            try:
                sock = self._open_socket(ip, parsed.port, self._remaining(deadline, "connect"))
                break
            except (socket.timeout, TimeoutError):
                raise FetchError("timeout", "timed out connecting to the server")
            except OSError as exc:
                last_exc = exc
        if sock is None:
            raise FetchError("connect_failed", f"could not connect to {parsed.host}: {last_exc}")
        conn = None
        target = [sock]
        fired = threading.Event()

        def _watchdog() -> None:                 # the WALL-CLOCK deadline: kills a blocked read however slowly the peer drips
            fired.set()
            try:
                target[0].shutdown(socket.SHUT_RDWR)
            except Exception:  # noqa: BLE001
                pass

        timer = threading.Timer(self._remaining(deadline, "connect"), _watchdog)
        timer.daemon = True
        timer.start()
        try:
            if parsed.scheme == "https":
                try:
                    sock.settimeout(self._remaining(deadline, "TLS"))
                    sock = self._ssl_context().wrap_socket(sock, server_hostname=parsed.host)
                    target[0] = sock
                except ssl.SSLError as exc:
                    raise FetchError("tls_failed", f"TLS handshake with {parsed.host} failed: {exc}")
            conn = http.client.HTTPSConnection(parsed.host, parsed.port) if parsed.scheme == "https" \
                else http.client.HTTPConnection(parsed.host, parsed.port)
            conn.sock = sock
            headers = {"User-Agent": self.limits.user_agent, "Accept": "text/html,text/*;q=0.9,*/*;q=0.1",
                       "Accept-Encoding": "identity", "Connection": "close"}
            sock.settimeout(self._remaining(deadline, "send"))
            conn.request("GET", parsed.path_query, headers=headers)
            sock.settimeout(self._remaining(deadline, "response"))
            resp = conn.getresponse()
            if fired.is_set():           # the watchdog's shutdown makes a header read look like a clean EOF
                raise FetchError("timeout", "timed out (the call's time budget ended while reading headers)")
            status = resp.status
            trace.status = status
            if status in REDIRECT_STATUSES:
                return status, resp.msg, b"", False
            enc = (resp.getheader("Content-Encoding") or "identity").strip().lower()
            if enc not in ("", "identity"):
                raise FetchError("unsupported_content_encoding", f"server sent Content-Encoding {enc!r}; refused")
            ctype = resp.getheader("Content-Type") or ""
            if require_text and (200 <= status < 300 or ctype) and not is_supported_type(ctype):
                raise FetchError("unsupported_content_type", f"content type {ctype or '(none)'!r} is not text", content_type=ctype)
            buf = bytearray()
            while len(buf) < max_bytes:
                if resp.isclosed():      # the response closed itself after the last byte (the socket is gone: no settimeout)
                    break
                sock.settimeout(self._remaining(deadline, "reading the body"))
                chunk = resp.read1(min(_CHUNK, max_bytes - len(buf)))   # read1: one recv, so the deadline is re-checked every iteration
                if not chunk:
                    break
                buf += chunk
            truncated = False
            if len(buf) >= max_bytes and not resp.isclosed():
                sock.settimeout(self._remaining(deadline, "reading the body"))
                truncated = bool(resp.read1(1))
            trace.bytes_read = len(buf)
            if fired.is_set():
                raise FetchError("timeout", "timed out (the call's time budget ended while reading the body)")
            if not truncated and resp.length not in (None, 0) and len(buf) < max_bytes:
                raise FetchError("network_error", "the server closed the connection before sending the whole body (Content-Length not met)")
            return status, resp.msg, bytes(buf), truncated
        except (socket.timeout, TimeoutError):
            raise FetchError("timeout", "timed out waiting for the server")
        except (http.client.HTTPException, ConnectionError, OSError) as exc:
            if fired.is_set():
                raise FetchError("timeout", "timed out (the call's time budget ended while reading)")
            raise FetchError("network_error", f"network error: {type(exc).__name__}: {exc}")
        finally:
            timer.cancel()
            try:
                if conn is not None:
                    conn.close()
                elif sock is not None:
                    sock.close()
            except Exception:  # noqa: BLE001
                pass

    def _follow(self, start: ParsedUrl, pins: Dict[str, List[str]], deadline: float, *, max_bytes: int,
                require_text: bool, check_robots: bool, trace: Trace) -> Tuple[ParsedUrl, int, http.client.HTTPMessage, bytes, bool]:
        cur = start
        for hop in range(self.limits.max_redirects + 1):
            trace.current_url = cur.url()
            self._pin(cur, pins, deadline)       # the guard sees the page's host BEFORE anything (robots.txt included) is fetched
            if check_robots:
                self._robots_gate(cur, pins, deadline, trace)
            status, headers, body, truncated = self._request(cur, pins, deadline, max_bytes=max_bytes,
                                                             require_text=require_text, trace=trace)
            if status in REDIRECT_STATUSES:
                loc = (headers.get("Location") or "").strip()
                if not loc:
                    raise FetchError("bad_redirect", f"HTTP {status} without a Location header")
                if hop >= self.limits.max_redirects:
                    raise FetchError("too_many_redirects", f"more than {self.limits.max_redirects} redirects")
                cur = guard.validate_url(urljoin(cur.url(), loc), allowed_ports=self.limits.allowed_ports)
                trace.redirects.append(cur.url())
                continue
            return cur, status, headers, body, truncated
        raise FetchError("too_many_redirects", f"more than {self.limits.max_redirects} redirects")  # pragma: no cover

    # ------------------------------------------------------------------ robots.txt
    def _robots_text(self, origin_url: ParsedUrl, pins: Dict[str, List[str]], deadline: float,
                     trace: Trace) -> Tuple[str, str]:
        key = origin_url.origin
        hit = self._robots_cache.get(key)
        if hit and hit[0] > self._now():
            return hit[1], hit[2]
        rob = ParsedUrl(origin_url.scheme, origin_url.host, origin_url.port, "/robots.txt", origin_url.is_ip_literal)
        sub = Trace(rob.url(), rob.url())
        try:
            _, status, _, body, _ = self._follow(rob, pins, deadline, max_bytes=self.limits.robots_max_bytes,
                                                 require_text=False, check_robots=False, trace=sub)
        except GuardRefusal:
            raise                                  # a guard refusal is never relabelled as a mere robots problem
        except FetchError as exc:
            raise FetchError("robots_unavailable",
                             f"robots.txt for {origin_url.host} could not be read ({exc.code}: {exc.message}); refusing (fail closed)",
                             site=origin_url.host)
        if 200 <= status < 300:
            kind, text = "rules", body.decode("utf-8", errors="replace")
        elif 400 <= status < 500:
            kind, text = "allow_all", ""
        else:
            raise FetchError("robots_unavailable",
                             f"robots.txt for {origin_url.host} answered HTTP {status}; refusing (fail closed)", site=origin_url.host)
        self._robots_cache[key] = (self._now() + self.limits.robots_cache_ttl_s, kind, text)
        return kind, text

    def _robots_gate(self, parsed: ParsedUrl, pins: Dict[str, List[str]], deadline: float, trace: Trace) -> None:
        kind, text = self._robots_text(parsed, pins, deadline, trace)
        if kind == "allow_all":
            trace.robots = {"checked": True, "origin": f"{parsed.scheme}://{parsed.host}", "outcome": "no robots.txt (HTTP 4xx): no restrictions"}
            return
        dec = robots.evaluate(text, ROBOTS_PRODUCT_TOKEN, parsed.path_query)
        trace.robots = {"checked": True, "origin": f"{parsed.scheme}://{parsed.host}",
                        "outcome": "allowed" if dec.allowed else "blocked", "rule": dec.rule, "group": dec.group}
        if not dec.allowed:
            raise FetchError("robots_blocked", f"blocked by robots.txt: {dec.rule}", site=parsed.host, rule=dec.rule)

    # ------------------------------------------------------------------ public
    def fetch(self, url: str, *, user_initiated: bool = False) -> FetchResult:
        """GET ``url`` through the guard. ``user_initiated=True`` skips ONLY the robots.txt check (the tool layer
        gates it on a quoted human request); every other control is unchanged. Raises :class:`FetchError`
        (``.trace`` populated) on any refusal or failure."""
        trace = Trace(url if isinstance(url, str) else repr(url), url if isinstance(url, str) else "")
        deadline = self._now() + self.limits.timeout_s
        pins: Dict[str, List[str]] = {}
        try:
            start = guard.validate_url(url, allowed_ports=self.limits.allowed_ports)
            trace.current_url = start.url()
            if user_initiated:
                trace.robots = {"checked": False, "outcome": "not checked (user-initiated mode)"}
            cur, status, headers, body, truncated = self._follow(
                start, pins, deadline, max_bytes=self.limits.max_body_bytes, require_text=True,
                check_robots=not user_initiated, trace=trace)
        except FetchError as exc:
            exc.trace = trace
            raise
        except Exception as exc:  # noqa: BLE001 - nothing unexpected escapes as a bare exception; the caller audits it
            err = FetchError("internal_error", f"unexpected {type(exc).__name__}: {exc}")
            err.trace = trace
            raise err from exc
        return FetchResult(
            requested_url=trace.requested_url, final_url=cur.url(), status=status,
            content_type=headers.get("Content-Type") or "", body=body, truncated=truncated,
            redirects=list(trace.redirects), robots=dict(trace.robots),
            fetched_at_utc=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))

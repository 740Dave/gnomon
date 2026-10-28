"""The harness's self-healing link to the hub (
``proxy-attach-invariant``; D15 amendment, operator-approved 2026-09-29).

Before this, a harness registered ONCE, on a port frozen at spawn: every :8770
restart (fresh ephemeral port + empty SessionRegistry) orphaned every harnessed
session from ALL hub commands, and CLI/resume launches (``hub_rendezvous_port:
null``) never registered at all.

:class:`HubLink` keeps trying to be registered for the harness's whole life:

* DISCOVERY — ``<runtime_root>/hub/rendezvous.json`` is authoritative. The
  launch-time ``hub_rendezvous_port`` is only a HINT, used while no readable
  discovery file exists (a hub that predates the file).
* (RE)CONNECT — when not connected, try on a bounded exponential backoff
  (``backoff_initial`` doubling to ``backoff_max``); on success send ``register``
  (the hub then drains this instance's queued notices down the new socket).
* HUB CHANGE — while connected, re-read the file every ``recheck_interval``; a
  different ``(host, port, hub_instance_id)`` drops the old socket and
  re-registers at once. EOF / a socket error does the same.
* NEVER FATAL — no hub, a corrupt file, a refused/failed connect, a factory that
  raises: all are recorded as events and retried; nothing propagates into the
  pump, nothing blocks it beyond one bounded localhost connect.

FIX PASS (2026-09-29, independent audit HIGH — measured 31,491 registrations in
3s, 87% CPU, ~60k provenance events against a hub that accepts and slams the
door): a successful connect NO LONGER resets the backoff. The failure count is
reset only once the link is STABLE — up for ``stable_after`` (30s) AND the hub
has greeted it (a ``hub_hello`` frame, sent by the hub after every register).
Every disconnect of a not-yet-stable link counts as a failure and waits out the
backoff; only a stable link's drop retries at once. Provenance events are rate-
limited per kind (one per ``event_interval``, 30s), carrying a ``suppressed``
count. A hub ``superseded`` frame (another harness registered this instance_id)
parks the link for THAT hub instance — it does not re-register and ping-pong —
until the hub's identity changes.

It is driven by the proxy's single ``select()`` pump (``fds`` / ``on_readable`` /
``tick``) — no thread.
"""

from __future__ import annotations

import collections
import os
import time
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..hub.client import ProxyHubConnection
from ..hub.rendezvous import read_rendezvous


class RateLimiter:
    """At most one ``allow`` per ``interval`` per kind; ``allow`` returns
    ``(allowed, suppressed_since_last_allowed)`` (FIX PASS 3: shared by hub-link
    events AND the launcher's superseded/vacated records + operator feedback)."""

    def __init__(self, interval: float = 30.0, clock: Callable[[], float] = time.monotonic) -> None:
        self._interval = max(0.0, float(interval))
        self._clock = clock
        self._last: Dict[str, float] = {}
        self._suppressed: Dict[str, int] = {}

    def allow(self, kind: str) -> Tuple[bool, int]:
        now = self._clock()
        last = self._last.get(kind)
        if last is not None and now - last < self._interval:
            self._suppressed[kind] = self._suppressed.get(kind, 0) + 1
            return False, 0
        self._last[kind] = now
        return True, self._suppressed.pop(kind, 0)


class HubLink:
    def __init__(
        self,
        instance_id: str,
        *,
        rendezvous_file: Optional[str] = None,
        hint_port: Optional[int] = None,
        host: str = "127.0.0.1",
        meta: Optional[Dict[str, Any]] = None,
        clock: Callable[[], float] = time.monotonic,
        backoff_initial: float = 2.0,
        backoff_max: float = 30.0,
        recheck_interval: float = 2.0,
        connect_timeout: float = 1.0,
        connection_factory: Callable[..., Any] = ProxyHubConnection,
        on_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        stable_after: float = 30.0,
        event_interval: float = 30.0,
        caller_hash: Optional[str] = None,
        pending_writer: Optional[Callable[[], None]] = None,
        caller_hash_provider: Optional[Callable[[], Optional[str]]] = None,
    ) -> None:
        self._instance_id = instance_id
        # Round 2 (Diamond F-P2-1/2/3): called before EVERY register attempt, so the hub finds a fresh
        # pending-launch record for (this instance, this hash). None = nothing written (today's behaviour).
        self._pending_writer = pending_writer
        # Round 3 (R2-P2-1): decide the registration hash at REGISTER time, from the hub's CURRENT binding, so a
        # session started while the hub was down or OFF still binds once the hub is up ON. The provider returns
        # the hash (stable once minted) or None. None provider = the static caller_hash above.
        self._caller_hash_provider = caller_hash_provider
        #: Round 3: a registration refused by the hub's caller binding (visible in status(), and as an event).
        self._binding_refused = False
        self._binding_refusals = 0
        self._binding_refused_reason: Optional[str] = None
        self._rebind_retry = False
        # sha256 of this launch's caller token, sent in every register frame so the hub
        # binds this session's calls (and refuses a rogue re-register). None = today's frames, unchanged.
        self._caller_hash = caller_hash
        self._rendezvous_file = rendezvous_file
        self._hint_port = hint_port if (isinstance(hint_port, int) and not isinstance(hint_port, bool)
                                        and 1 <= hint_port <= 65535) else None
        self._hint_host = host
        self._meta = dict(meta or {})
        self._clock = clock
        self._backoff_initial = max(0.0, float(backoff_initial))
        self._backoff_max = max(self._backoff_initial, float(backoff_max))
        self._recheck = max(0.0, float(recheck_interval))
        self._connect_timeout = connect_timeout
        self._factory = connection_factory
        self._on_event = on_event

        self._actuator: Any = None
        self._conn: Any = None
        self._channel: Any = None
        self._eof = False
        self._target: Optional[Tuple[str, int, Optional[str]]] = None
        self._failures = 0
        self._next_attempt = 0.0
        self._next_recheck = 0.0
        self._last_unreadable: Optional[str] = None
        self._stable_after = max(0.0, float(stable_after))
        self._event_interval = max(0.0, float(event_interval))
        self._event_limiter = RateLimiter(self._event_interval, clock=clock)
        self._connected_at: Optional[float] = None
        self._hello = False
        self._stable = False
        self._parked = False
        self.registrations = 0
        self.events: Deque[Dict[str, Any]] = collections.deque(maxlen=200)

    # -- wiring / introspection -----------------------------------------------

    def attach(self, actuator: Any) -> None:
        """The Actuator hub-origin commands are dispatched to (set before pumping)."""
        self._actuator = actuator
        if self._conn is not None and self._channel is None:
            self._channel = self._make_channel(self._conn)

    def set_on_event(self, on_event: Optional[Callable[[Dict[str, Any]], None]]) -> None:
        """Set the state-transition sink after construction (the launcher points
        it at the provenance log once the gate exists)."""
        self._on_event = on_event

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def rendezvous_file(self) -> Optional[str]:
        return self._rendezvous_file

    @property
    def hint_port(self) -> Optional[int]:
        return self._hint_port

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def superseded(self) -> bool:
        """Parked: connected but NOT registered (another harness holds the id)."""
        return self._parked

    # -- hub-origin control frames (dispatched by the Actuator) ------------------

    def note_hub_hello(self, hub_instance_id: Optional[str] = None) -> None:
        """The hub greeted this registration — a VALID hub frame (one leg of the
        stability condition that resets the backoff)."""
        if self._conn is None:
            return
        if (hub_instance_id and self._target is not None and self._target[2]
                and hub_instance_id != self._target[2]):
            return  # a hello from some other hub identity is not ours
        self._hello = True
        self._binding_refused = False
        self._binding_refused_reason = None

    def note_superseded(self, frame: Optional[Dict[str, Any]] = None) -> None:
        """The hub registered ANOTHER harness under this instance_id. Stop being
        registered on THIS hub instance (no ping-pong); resume only if the hub's
        identity changes. Loud: an event (and the launcher's operator feedback)."""
        # FIX PASS 2 (re-audit MEDIUM: a parked harness never recovered): stay
        # CONNECTED but PARKED — no re-register (no ping-pong), no status UP. The
        # hub sends ``vacated`` when the holder's connection goes away, and we
        # re-register on this same socket. A hub restart (EOF) un-parks too.
        target = self._target
        by = (frame or {}).get("by")
        self._event("superseded", target=list(target) if target else None, by=by)
        self._parked = True
        if by == "caller-binding":
            # Round 3: refused by the hub's caller binding: LOUD (event + status), never silently dark.
            self._binding_refused = True
            self._binding_refusals += 1
            self._binding_refused_reason = ("registered with no caller hash" if not self._caller_hash
                                            else "caller hash not admitted (no pending-launch record, or another "
                                                 "harness holds the instance)")
            self._event("registration-refused", reason=self._binding_refused_reason,
                        refusals=self._binding_refusals)
            if not self._caller_hash:
                # nothing to wait for on this socket: on the next tick, drop it and retry (with backoff) so the
                # next attempt re-reads the hub's binding and mints a hash (deferred: we are inside a frame read)
                self._rebind_retry = True

    def note_vacated(self, frame: Optional[Dict[str, Any]] = None) -> None:
        """The holder left: re-register on the current socket."""
        if not self._parked or self._conn is None:
            return
        self._resolve_caller_hash()
        self._write_pending_record()
        frame: Dict[str, Any] = {"type": "register", "instance_id": self._instance_id,
                                 "meta": {**self._meta, "hub_link_registrations": self.registrations,
                                          "reclaimed": True}}
        if self._caller_hash:
            frame["caller_hash"] = self._caller_hash
        try:
            self._conn.send_frame(frame)
        except (OSError, RuntimeError) as exc:
            self._disconnect(f"re-register after vacated failed: {exc}")
            return
        self._parked = False
        self._hello = False
        self._connected_at = self._clock()
        self.registrations += 1
        self._event("reclaimed", target=list(self._target) if self._target else None)

    @property
    def target(self) -> Optional[Tuple[str, int, Optional[str]]]:
        return self._target

    @property
    def next_attempt_in(self) -> float:
        return max(0.0, self._next_attempt - self._clock())

    def status(self) -> Dict[str, Any]:
        out = {"connected": self.connected, "target": self._target,
               "registrations": self.registrations, "rendezvous_file": self._rendezvous_file,
               "hint_port": self._hint_port, "next_attempt_in": self.next_attempt_in}
        if self._caller_hash or self._binding_refusals:
            # Round 3: caller-binding state (only once a hash or a refusal exists, so OFF is unchanged).
            out.update({"registered": bool(self.connected and self._hello and not self._parked),
                        "binding_refused": self._binding_refused, "binding_refusals": self._binding_refusals,
                        "binding_refused_reason": self._binding_refused_reason})
        return out

    # -- pump surface ------------------------------------------------------------

    def fileno(self) -> int:
        """Legacy ``hub_connection`` compatibility: the CURRENT socket fd."""
        if self._conn is None:
            raise OSError("hub link not connected")
        return self._conn.fileno()

    def fds(self) -> List[int]:
        if self._conn is None:
            return []
        try:
            return [self._conn.fileno()]
        except (OSError, RuntimeError, ValueError):
            self._disconnect("fd unavailable")
            return []

    def owns(self, fd: int) -> bool:
        return self._conn is not None and fd in self.fds()

    def on_readable(self, fd: int) -> None:
        if self._conn is None or self._channel is None:
            return
        try:
            self._channel.feed_readable()
        except OSError as exc:
            self._disconnect(f"socket error: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - a bad frame must not kill the pump
            self._event("frame-error", error=f"{type(exc).__name__}: {exc}")
        if self._eof:
            self._disconnect("hub closed the connection")

    def tick(self, now: Optional[float] = None) -> None:
        try:
            self._tick(self._clock() if now is None else now)
        except Exception as exc:  # noqa: BLE001 - NEVER into the pump
            self._event("tick-error", error=f"{type(exc).__name__}: {exc}")

    # -- UP frames ----------------------------------------------------------------

    def send_status(self, used_percentage: Optional[float] = None, alive: bool = True) -> None:
        if self._parked:
            raise OSError("parked (superseded): not reporting status for an id another harness holds")
        self.send_frame({"type": "status", "instance_id": self._instance_id,
                         "used_percentage": used_percentage, "alive": alive})

    def send_frame(self, frame: Dict[str, Any]) -> None:
        """Send one UP frame. Raises ``OSError`` when not connected (callers treat
        that as "the hub will learn on re-register")."""
        if self._conn is None:
            raise OSError("no hub connection (reconnecting)")
        try:
            self._conn.send_frame(frame)
        except (OSError, RuntimeError) as exc:
            self._disconnect(f"send failed: {exc}")
            raise OSError(f"hub send failed: {exc}") from exc

    def close(self) -> None:
        self._disconnect("closed", schedule=False, record=False)

    # -- internals ------------------------------------------------------------------

    def _event(self, kind: str, **fields: Any) -> None:
        now = self._clock()
        ev = {"event": kind, "t": now, **fields}
        self.events.append(ev)
        # FIX PASS: RATE-LIMITED per kind (at most one per event_interval), with a
        # count of what was suppressed — a flapping or absent hub can never flood
        # the provenance log.
        if self._on_event is None:
            return
        allowed, suppressed = self._event_limiter.allow(kind)
        if not allowed:
            return
        out = dict(ev)
        if suppressed:
            out["suppressed"] = suppressed
        try:
            self._on_event(out)
        except Exception:  # noqa: BLE001
            pass

    def _resolve_caller_hash(self) -> None:
        """Round 3: ask the provider (it reads the hub's CURRENT binding) for this attempt's hash. Never fatal."""
        if self._caller_hash_provider is None or self._caller_hash:
            return
        try:
            h = self._caller_hash_provider()
        except Exception as exc:  # noqa: BLE001
            self._event("caller-hash-failed", error=f"{type(exc).__name__}: {exc}")
            return
        if isinstance(h, str) and h:
            self._caller_hash = h
            self._event("caller-hash-minted")

    def _write_pending_record(self) -> None:
        """Never fatal: a failed write is an event; the hub then refuses the registration (and audits it)."""
        if self._pending_writer is None or not self._caller_hash:
            return
        try:
            self._pending_writer()
        except Exception as exc:  # noqa: BLE001
            self._event("pending-record-failed", error=f"{type(exc).__name__}: {exc}")

    def _maybe_mark_stable(self, now: float) -> None:
        if (not self._stable and self._conn is not None and self._hello
                and self._connected_at is not None and now - self._connected_at >= self._stable_after):
            self._stable = True
            self._failures = 0

    def _discover(self) -> Optional[Tuple[str, int, Optional[str]]]:
        info, err = read_rendezvous(self._rendezvous_file) if self._rendezvous_file else (None, "none")
        if info is not None:
            self._last_unreadable = None
            return info.identity
        if err not in ("absent", "none") and err != self._last_unreadable:
            self._last_unreadable = err
            self._event("rendezvous-unreadable", error=err, path=self._rendezvous_file)
        if self._hint_port is not None:
            return (self._hint_host, self._hint_port, None)
        return None

    def _schedule_retry(self, now: float) -> None:
        delay = min(self._backoff_initial * (2 ** self._failures), self._backoff_max)
        self._failures = min(self._failures + 1, 32)
        self._next_attempt = now + delay

    def _tick(self, now: float) -> None:
        if self._rebind_retry:
            self._rebind_retry = False
            if self._conn is not None:
                self._disconnect("registration refused by caller binding (no hash): retrying")
        if self._conn is not None:
            self._maybe_mark_stable(now)
            if now < self._next_recheck:
                return
            self._next_recheck = now + self._recheck
            info, _err = read_rendezvous(self._rendezvous_file) if self._rendezvous_file else (None, None)
            if info is None or self._target is None:
                return
            if info.identity == self._target:
                return
            if (self._target[2] is None and info.host == self._target[0]
                    and info.port == self._target[1]):
                self._target = info.identity  # connected via the hint to this same hub
                return
            self._event("hub-changed", old=list(self._target), new=list(info.identity))
            self._disconnect("hub changed", schedule=False)
            self._next_attempt = now  # re-register at once, no backoff
        if now < self._next_attempt:
            return
        target = self._discover()
        if target is None:
            self._event("no-hub", path=self._rendezvous_file)
            self._schedule_retry(now)
            return
        host, port, _hub_id = target
        self._resolve_caller_hash()
        self._write_pending_record()
        try:
            conn = self._factory(host, port, self._instance_id)
            if self._caller_hash:
                conn.connect(meta={**self._meta, "hub_link_registrations": self.registrations},
                             timeout=self._connect_timeout, caller_hash=self._caller_hash)
            else:
                conn.connect(meta={**self._meta, "hub_link_registrations": self.registrations},
                             timeout=self._connect_timeout)
        except Exception as exc:  # noqa: BLE001 - refused / timeout / anything
            self._event("connect-failed", host=host, port=port, error=f"{type(exc).__name__}: {exc}")
            self._schedule_retry(now)
            return
        self._conn = conn
        self._eof = False
        self._target = target
        # FIX PASS: NOT reset here — only a stable, greeted link resets it.
        self._connected_at = now
        self._hello = False
        self._stable = False
        self._next_recheck = now + self._recheck
        self.registrations += 1
        self._channel = self._make_channel(conn) if self._actuator is not None else None
        self._event("registered", host=host, port=port, hub_instance_id=target[2],
                    registrations=self.registrations)

    def _make_channel(self, conn: Any) -> Any:
        from .actuator import ORIGIN_HUB
        from .control import ControlChannel

        link = self

        def _reader(fd: int, n: int) -> bytes:
            data = os.read(fd, n)
            if not data:
                link._eof = True
            return data

        return ControlChannel(conn.fileno(), self._actuator, reader=_reader, origin=ORIGIN_HUB)

    def _disconnect(self, reason: str, *, schedule: bool = True, record: bool = True) -> None:
        conn = self._conn
        self._conn = None
        self._channel = None
        self._eof = False
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
            if record:
                self._event("disconnected", reason=reason)
            if schedule:
                if self._stable:
                    # a long-lived, greeted link dropped: the hub restarted —
                    # retry at once (the new hub is usually already up)
                    self._next_attempt = self._clock()
                else:
                    # FIX PASS: a link that never became stable is a FAILURE —
                    # back off (this is what bounds a flapping hub)
                    self._schedule_retry(self._clock())
        self._stable = False
        self._hello = False
        self._connected_at = None
        self._parked = False

"""Human-presence proof for act-class confirms (operator ruling checklist g76).

``prove_presence(reason)`` asks macOS for DEVICE-OWNER authentication
(``LAPolicyDeviceOwnerAuthentication``): Touch ID when a sensor exists, else the login PASSWORD (this Mac has no
Touch ID sensor, so it is the password dialog). The SYSTEM draws the dialog and takes the answer through
WindowServer secure input: nothing is read from this process's stdin, any PTY, or any agent channel.

Mechanism (decided, and why): a tiny Swift helper compiled with ``swiftc`` from the checked-in
``native/presence_helper.swift``. pyobjc is NOT installed on this machine (``import LocalAuthentication`` fails),
and a hard dependency on it would be a new install for one call; ``swiftc`` ships with the Xcode CLT already
required here. The helper is built on first use into a 0700 cache dir. Pins: the SOURCE sha256 is a constant in
this module (``HELPER_SOURCE_SHA256``; a changed source is refused, so editing the .swift needs a deliberate
re-pin), and the compiled binary's sha256 is recorded at build time in a sidecar and re-verified before EVERY run.

Contract: blocks until the operator answers; times out (default 120 s) as a REFUSAL; every failure path (no swiftc, bad
pin, helper crash, unavailable policy, timeout, cancel) returns False. There is no env/flag bypass in this
module: the only OFF switch is the caller's flag ``confirm_proof``.

FRAME (stated honestly): this stops an over-eager agent from performing the operator's confirm. It does NOT stop a
hostile same-uid process, which could replace the helper + its sidecar, or drive the HTTP route the confirm
uses; a real credential on the loopback server is the FIDO roadmap item (operator comment).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, asdict
from typing import Any, Callable, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
HELPER_SOURCE = os.path.join(HERE, "native", "presence_helper.swift")
#: sha256 of native/presence_helper.swift. A mismatch refuses every presence check (fail closed).
HELPER_SOURCE_SHA256 = "b7d512b562b6e489f573adf586e4c1a6d0f7fe385fe28bca4e61ccb62c0e8a15"
HELPER_NAME = "gnomon-confirm"            # the process name macOS shows in the dialog
DEFAULT_TIMEOUT_S = 120.0
METHOD = "macos-LAPolicyDeviceOwnerAuthentication"
FLAG = "confirm_proof"

EXIT_RESULTS = {0: "authenticated", 1: "refused", 2: "timeout", 3: "unavailable"}


@dataclass(frozen=True)
class ProofResult:
    ok: bool
    result: str          # authenticated | refused | timeout | unavailable | error | busy
    method: str
    ts: float
    detail: str = ""

    def evidence(self) -> Dict[str, Any]:
        d = asdict(self)
        d["ok"] = bool(self.ok)
        return d


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def helper_dir() -> str:
    return os.environ.get("GNOMON_PRESENCE_DIR") or os.path.join(
        os.path.expanduser("~"), "Library", "Caches", "gnomon", "confirm-presence")


def _secure_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    st = os.stat(path)
    if st.st_uid != os.getuid() or (st.st_mode & 0o022):
        raise RuntimeError(f"presence helper dir {path} is not owned by this user or is group/world-writable")


def ensure_helper() -> str:
    """Build (if needed) and verify the helper; return its path. Raises RuntimeError on ANY problem."""
    if not os.path.isfile(HELPER_SOURCE):
        raise RuntimeError(f"presence helper source missing: {HELPER_SOURCE}")
    src = _sha256_file(HELPER_SOURCE)
    if src != HELPER_SOURCE_SHA256:
        raise RuntimeError(f"presence helper source sha256 {src} != pinned {HELPER_SOURCE_SHA256}; re-pin deliberately")
    d = helper_dir()
    _secure_dir(d)
    binp, side = os.path.join(d, HELPER_NAME), os.path.join(d, HELPER_NAME + ".sha256.json")
    rec = None
    try:
        with open(side, "r", encoding="utf-8") as f:
            rec = json.load(f)
    except (OSError, ValueError):
        rec = None
    good = (isinstance(rec, dict) and rec.get("source_sha256") == src and os.path.isfile(binp)
            and rec.get("binary_sha256") == _sha256_file(binp))
    if not good:
        swiftc = "/usr/bin/swiftc"
        if not os.path.exists(swiftc):
            raise RuntimeError("swiftc not found; install the Xcode command line tools")
        tmp = binp + f".build-{os.getpid()}"
        p = subprocess.run([swiftc, "-O", HELPER_SOURCE, "-o", tmp], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=180)
        if p.returncode != 0 or not os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise RuntimeError(f"swiftc failed ({p.returncode}): {p.stderr[-300:]}")
        os.chmod(tmp, 0o700)
        os.replace(tmp, binp)
        with open(side, "w", encoding="utf-8") as f:
            json.dump({"source_sha256": src, "binary_sha256": _sha256_file(binp), "built_at": time.time()}, f)
    return binp


_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean_reason(reason: Any) -> str:
    s = _CTRL.sub(" ", str(reason or "")).strip()
    s = (s[:200] or "confirm an action")
    return "Gnomon: " + s


_busy = threading.Lock()


def _real_runner(reason: str, timeout_s: float) -> ProofResult:
    now = time.time()
    try:
        binp = ensure_helper()
    except Exception as exc:  # noqa: BLE001 - every failure is a refusal, with the reason
        return ProofResult(False, "error", METHOD, now, f"{type(exc).__name__}: {exc}")
    try:
        p = subprocess.run([binp, str(float(timeout_s)), reason], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=timeout_s + 15, start_new_session=True,
                           env={"PATH": "/usr/bin:/bin", "HOME": os.path.expanduser("~")})
    except subprocess.TimeoutExpired:
        return ProofResult(False, "timeout", METHOD, time.time(), "helper exceeded its hard timeout and was killed")
    except Exception as exc:  # noqa: BLE001
        return ProofResult(False, "error", METHOD, time.time(), f"{type(exc).__name__}: {exc}")
    res = EXIT_RESULTS.get(p.returncode, "error")
    return ProofResult(p.returncode == 0, res, METHOD, time.time(), (p.stderr or "").strip()[:200])


#: Test seam: replace with ``fn(reason, timeout_s) -> ProofResult``. Only code (a test) can set it; there is no
#: environment or config switch.
_runner: Callable[[str, float], ProofResult] = _real_runner


def set_runner(fn: Optional[Callable[[str, float], ProofResult]]) -> None:
    global _runner
    _runner = fn or _real_runner


def prove_presence_detail(reason: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> ProofResult:
    """Run the check and return the full evidence. Never raises. One dialog at a time (a second concurrent
    request is refused as ``busy``, never queued behind an unanswered prompt)."""
    if not _busy.acquire(blocking=False):
        return ProofResult(False, "busy", METHOD, time.time(), "another presence check is waiting for the pilot")
    try:
        try:
            r = _runner(clean_reason(reason), float(timeout_s))
        except Exception as exc:  # noqa: BLE001
            return ProofResult(False, "error", METHOD, time.time(), f"{type(exc).__name__}: {exc}")
        if not isinstance(r, ProofResult):
            return ProofResult(False, "error", METHOD, time.time(), "runner returned a non-ProofResult")
        return r
    finally:
        _busy.release()


def prove_presence(reason: str, *, timeout_s: float = DEFAULT_TIMEOUT_S) -> bool:
    return prove_presence_detail(reason, timeout_s=timeout_s).ok


# --- presence GRANTS: a passed check, bound to one change, that a library writer can require (delta audit D1) --------
# ``ConfigStore.save`` refuses to turn a SECURITY flag OFF unless it is handed a grant that (a) this module minted
# after a PASSED presence check, (b) is bound to exactly that change, (c) is unexpired and (d) has not been used.
# The only way to obtain one is :func:`prove_presence_grant`, which raises the operator's dialog. A grant is unforgeable from
# ANOTHER process (the token is random and held only in this module's memory); IN-PROCESS code can mint one (write
# ``_grants``, patch ``consume_grant`` / ``prove_presence_detail``, or ``set_runner`` a stub). FRAME: stops an over-eager agent calling the
# library; a same-uid process can reach this module's memory or write the file by other means.
GRANT_TTL_S = 120.0
_grants: Dict[str, tuple] = {}
_grants_lock = threading.Lock()


def change_digest(names: Any) -> str:
    """The binding of a grant: sha256 of the sorted names of the security flags being turned OFF."""
    blob = json.dumps({"turn_off": sorted(str(n) for n in names)}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class PresenceGrant:
    """Opaque proof of one passed presence check for one change. Construct only via :func:`prove_presence_grant`."""
    __slots__ = ("_token", "digest")

    def __init__(self, token: str, digest: str) -> None:
        self._token, self.digest = token, digest

    def __repr__(self) -> str:   # never print the token
        return f"<PresenceGrant {self.digest[:12]}>"


def prove_presence_grant(reason: str, digest: str, *, timeout_s: float = DEFAULT_TIMEOUT_S):
    """Run the presence check; on a pass also mint a one-shot grant bound to ``digest``. Returns
    ``(ProofResult, PresenceGrant | None)``; the grant is None unless the check passed."""
    res = prove_presence_detail(reason, timeout_s=timeout_s)
    if not res.ok:
        return res, None
    token = secrets.token_hex(16)
    with _grants_lock:
        for t in [t for t, (_d, exp) in _grants.items() if exp < time.time()]:
            _grants.pop(t, None)
        _grants[token] = (digest, time.time() + GRANT_TTL_S)
    return res, PresenceGrant(token, digest)


def consume_grant(grant: Any, digest: str) -> bool:
    """True iff ``grant`` is a live, unused grant bound to ``digest``; it is spent either way a match is found."""
    if not isinstance(grant, PresenceGrant):
        return False
    with _grants_lock:
        rec = _grants.pop(grant._token, None)
    return bool(rec and rec[0] == digest == grant.digest and rec[1] >= time.time())
